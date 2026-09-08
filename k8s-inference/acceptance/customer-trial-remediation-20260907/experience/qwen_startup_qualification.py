"""Explicitly authorized standalone burst qualification; never a background cohort.

Three public clients at most, unique harmless long-generation requests, no
submission retries or configuration writes. Read-only per-Pod metrics establish
actual burst work; raw logs retain restore proof for a separate reviewed verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

import httpx

from interactive_sampler import TERMINAL, hash_json, safe_exception, utc
from qwen_burst_regression import SELECTOR, pod_state


def payload_for(ordinal):
    marker = f"FS2-STARTUP-{ordinal:04d}"
    topic = (
        "how libraries organize books",
        "how the water cycle works",
        "how telescopes observe stars",
    )[(ordinal - 1) % 3]
    return marker, {
        "model": "qwen3-8b",
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Begin with the exact marker {marker}. Then write a clear educational explanation of {topic}, "
                    "with at least 250 words. Use ordinary prose, not a list. Do not include a preamble before the marker."
                ),
            }
        ],
        "temperature": 0,
        "max_tokens": 1024,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def useful_output(text, marker):
    # A bounded semantic smoke test, not a benchmark of factual answer quality.
    return isinstance(text, str) and marker in text[:100] and len(text.split()) >= 100


def success_counter(text):
    rows = []
    for line in text.splitlines():
        match = re.fullmatch(
            r"vllm:request_success_total\{([^}]+)\}\s+([0-9.eE+\-]+)", line
        )
        if match and re.search(r'finished_reason="(?:stop|length)"', match[1]):
            rows.append(float(match[2]))
    return sum(rows) if rows else None


async def public_request(origin, token, ordinal):
    marker, payload = payload_for(ordinal)
    row = {
        "ordinal": ordinal,
        "started_at": utc(),
        "request": payload,
        "request_sha256": hash_json(payload),
        "submission_attempts": 1,
        "http_statuses": [],
        "status": "failed",
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            base_url=origin,
            timeout=30,
            trust_env=False,
            headers={"authorization": "Bearer " + token},
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json=payload,
                headers={
                    "Idempotency-Key": "startup-" + uuid4().hex,
                    "x-fs2-wait-seconds": "0",
                },
            )
            row["http_statuses"].append(response.status_code)
            response.raise_for_status()
            operation = response.json()
            operation_id = response.headers.get("x-fs2-operation-id") or operation["id"]
            row["operation_id"] = operation_id
            while operation.get("status") not in TERMINAL:
                if time.monotonic() - started > 300:
                    raise TimeoutError("bounded public operation deadline")
                await asyncio.sleep(1)
                response = await client.get("/v1/operations/" + operation_id)
                row["http_statuses"].append(response.status_code)
                response.raise_for_status()
                operation = response.json()
            row["operation"] = operation
            if operation["status"] != "succeeded":
                raise ValueError("public operation did not succeed")
            response = await client.get("/v1/operations/" + operation_id + "/result")
            row["http_statuses"].append(response.status_code)
            response.raise_for_status()
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            row.update(
                response_id=result.get("id"),
                response_sha256=hash_json(result),
                output=content,
                usage=result.get("usage"),
                useful_output=useful_output(content, marker),
            )
            row["status"] = "passed" if row["useful_output"] else "failed"
    except Exception as error:
        row["failure"] = safe_exception(error)
    row.update(completed_at=utc(), client_seconds=time.monotonic() - started)
    return row


async def run(args):
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    access = json.loads(args.credentials.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    token = access["credentials"]["inference_access_token"]
    kube = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "--request-timeout=15s",
    ]
    started_at, started = utc(), time.monotonic()
    plan = {
        "started_at": started_at,
        "maximum_clients": 3,
        "maximum_requests": args.max_requests,
        "new_admission_seconds": args.duration,
        "per_operation_timeout_seconds": 300,
        "recovery_seconds": 90,
        "configuration_writes": False,
        "ordinary_sampler_running": False,
        "source_sha256": hash_json(Path(__file__).read_text()),
    }
    (args.output / "plan.json").write_text(json.dumps(plan, indent=2))

    async def command(*parts):
        process = await asyncio.create_subprocess_exec(
            *kube,
            *parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 20)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError("bounded read-only Kubernetes observation") from None
        if process.returncode:
            raise RuntimeError("read-only Kubernetes observation failed")
        return stdout.decode()

    async def observe():
        pods, model = await asyncio.gather(
            command("get", "pods", "-n", "fs2-models", "-l", SELECTOR, "-o", "json"),
            command(
                "get",
                "modeldeployments.inference.fs2.nebius.ai",
                "qwen3-8b",
                "-n",
                "fs2-models",
                "-o",
                "json",
            ),
        )
        model = json.loads(model)
        rows = []
        for pod in json.loads(pods)["items"]:
            row = pod_state(pod)
            row["init_statuses"] = pod.get("status", {}).get(
                "initContainerStatuses", []
            )
            row["container_statuses"] = pod.get("status", {}).get(
                "containerStatuses", []
            )
            if row["ready"] and not row["deleting"]:
                try:
                    metrics = await command(
                        "get",
                        "--raw",
                        f"/api/v1/namespaces/fs2-models/pods/http:{row['name']}:8000/proxy/metrics",
                    )
                    row["successful_requests"] = success_counter(metrics)
                except Exception as error:
                    row["metrics_error"] = safe_exception(error)
            rows.append(row)
        return {
            "observed_at": utc(),
            "pods": rows,
            "spec_sha256": hash_json(model["spec"]),
            "model_phase": model.get("status", {}).get("phase"),
        }

    baseline = await observe()
    hot = {
        p["uid"]
        for p in baseline["pods"]
        if (p["role"] or "").startswith("hot-") and p["ready"]
    }
    old = {p["uid"] for p in baseline["pods"]}
    (args.output / "baseline.json").write_text(json.dumps(baseline, indent=2))
    if not hot:
        raise RuntimeError("no Ready hot baseline; no inference submitted")
    if any((p["role"] or "").startswith("burst-") for p in baseline["pods"]):
        raise RuntimeError(
            "burst already exists; no new-start qualification traffic submitted"
        )
    observations, requests, first_counters, deltas = [baseline], [], {}, {}
    names = {}
    log_receipts, log_stages = [], set()

    async def capture_logs(uid, name, stage):
        # Capture before the ordinary idle scale-down can remove this Pod.
        if (uid, stage) in log_stages:
            return
        log_stages.add((uid, stage))
        try:
            logs = await command(
                "logs",
                "-n",
                "fs2-models",
                name,
                "-c",
                "vllm",
                "--since-time=" + started_at,
                "--timestamps",
                "--limit-bytes=2000000",
            )
            filename = name + "." + stage + ".runtime.log"
            (args.output / filename).write_text(logs)
            log_receipts.append(
                {
                    "uid": uid,
                    "name": name,
                    "stage": stage,
                    "file": filename,
                    "sha256": hash_json(logs),
                }
            )
        except Exception as error:
            log_receipts.append(
                {
                    "uid": uid,
                    "name": name,
                    "stage": stage,
                    "error": safe_exception(error),
                }
            )

    stop, observed_done = asyncio.Event(), asyncio.Event()
    ordinal = 0
    with (
        (args.output / "observations.jsonl").open("x") as obs_file,
        (args.output / "requests.jsonl").open("x") as req_file,
    ):
        obs_file.write(json.dumps(baseline) + "\n")

        async def observer():
            while not observed_done.is_set():
                try:
                    row = await observe()
                    ready_hot = {
                        p["uid"]
                        for p in row["pods"]
                        if p["ready"] and not p["deleting"]
                    }
                    if (
                        not hot.issubset(ready_hot)
                        or row["spec_sha256"] != baseline["spec_sha256"]
                    ):
                        row["baseline_changed"] = True
                        stop.set()
                    for pod in row["pods"]:
                        if pod["uid"] in old or not (pod["role"] or "").startswith(
                            "burst-"
                        ):
                            continue
                        names[pod["uid"]] = pod["name"]
                        value = pod.get("successful_requests")
                        if value is not None:
                            first_counters.setdefault(pod["uid"], value)
                            deltas[pod["uid"]] = value - first_counters[pod["uid"]]
                            await capture_logs(pod["uid"], pod["name"], "ready")
                            if deltas[pod["uid"]] >= 2:
                                await capture_logs(pod["uid"], pod["name"], "useful")
                                stop.set()
                except Exception as error:
                    row = {"observed_at": utc(), "error": safe_exception(error)}
                    stop.set()
                observations.append(row)
                obs_file.write(json.dumps(row) + "\n")
                obs_file.flush()
                try:
                    await asyncio.wait_for(observed_done.wait(), 5)
                except TimeoutError:
                    pass

        async def worker():
            nonlocal ordinal
            while (
                not stop.is_set()
                and ordinal < args.max_requests
                and time.monotonic() - started < args.duration
            ):
                ordinal += 1
                row = await public_request(origin, token, ordinal)
                requests.append(row)
                req_file.write(json.dumps(row) + "\n")
                req_file.flush()
                print(
                    json.dumps(
                        {
                            "ordinal": row["ordinal"],
                            "status": row["status"],
                            "client_seconds": round(row["client_seconds"], 3),
                        }
                    ),
                    flush=True,
                )
                if row["status"] != "passed":
                    stop.set()

        sampler = asyncio.create_task(observer())
        try:
            await asyncio.gather(*(worker() for _ in range(3)))
            # Keep observing bounded startup even if the request cap is reached;
            # this does not silently create more traffic or direct Pod calls.
            while not stop.is_set() and time.monotonic() - started < args.duration:
                await asyncio.sleep(5)
            await asyncio.sleep(90)
        finally:
            observed_done.set()
            await sampler
    for uid, name in names.items():
        if not any(row["uid"] == uid for row in log_receipts):
            await capture_logs(uid, name, "never-ready-final")
    summary = {
        "started_at": started_at,
        "completed_at": utc(),
        "requests": len(requests),
        "passed_requests": sum(r["status"] == "passed" for r in requests),
        "new_burst_pod_names": names,
        "per_pod_success_delta_after_ready_baseline": deltas,
        "runtime_logs": log_receipts,
        "observation_errors": sum(
            "error" in r or r.get("baseline_changed", False) for r in observations
        ),
        "status": "evidence-collected-review-required",
        "qualification_note": "No automatic snapshot/public-routing qualification. Match actual restore and request IDs in these Pod logs, or review isolated per-Pod counter increments with all unique public responses. No direct Pod inference was sent.",
        "unknown_or_nonterminal_operation_ids": [
            r["operation_id"]
            for r in requests
            if r.get("operation_id")
            and r.get("operation", {}).get("status") not in TERMINAL
        ],
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--duration", type=int, choices=range(60, 1201), default=900, metavar="SECONDS")
    parser.add_argument("--max-requests", type=int, choices=range(6, 361), default=180, metavar="COUNT")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
