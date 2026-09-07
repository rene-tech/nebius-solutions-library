"""Prepared-only runner: bounded public Qwen traffic, read-only transition proof.

Run separately from the unchanged 25-second campaign sampler. No retries,
scaling writes, policy updates or automatic cleanup of accepted operations.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from interactive_sampler import hash_json, safe_exception, sample, utc

ROLE_LABEL = "fs2-serve.nebius.ai/workload-role"
SELECTOR = "fs2-serve.nebius.ai/model-deployment=qwen3-8b"


def pod_state(raw):
    metadata, status = raw["metadata"], raw.get("status", {})
    conditions = {
        item["type"]: item.get("status") for item in status.get("conditions", [])
    }
    return {
        "name": metadata["name"],
        "uid": metadata["uid"],
        "created_at": metadata.get("creationTimestamp"),
        "role": metadata.get("labels", {}).get(ROLE_LABEL),
        "node": raw.get("spec", {}).get("nodeName"),
        "phase": status.get("phase"),
        "ready": conditions.get("Ready") == "True",
        "scheduled": conditions.get("PodScheduled") == "True",
        "initialized": conditions.get("Initialized") == "True",
        "deleting": bool(metadata.get("deletionTimestamp")),
        "restarts": sum(
            item.get("restartCount", 0) for item in status.get("containerStatuses", [])
        ),
    }


def transition_observed(observation, baseline_hot_uids):
    pods = observation.get("pods", [])
    hot = [pod for pod in pods if pod["uid"] in baseline_hot_uids]
    burst = [
        pod
        for pod in pods
        if (pod.get("role") or "").startswith("burst-")
        and pod["scheduled"]
        and not pod["initialized"]
        and not pod["deleting"]
        and pod["phase"] not in {"Failed", "Succeeded"}
    ]
    return bool(
        baseline_hot_uids
        and {pod["uid"] for pod in hot} == baseline_hot_uids
        and all(pod["ready"] and not pod["deleting"] for pod in hot)
        and burst
    )


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def verdict(observations, requests):
    baseline = observations[0]
    hot_uids = {
        pod["uid"]
        for pod in baseline.get("pods", [])
        if (pod.get("role") or "").startswith("hot-") and pod["ready"]
    }
    baseline_burst_uids = {
        pod["uid"]
        for pod in baseline.get("pods", [])
        if (pod.get("role") or "").startswith("burst-")
    }
    observed_burst_uids = {
        pod["uid"]
        for row in observations
        for pod in row.get("pods", [])
        if (pod.get("role") or "").startswith("burst-")
    }
    transition_samples = [
        row for row in observations if transition_observed(row, hot_uids)
    ]
    overlapping = [
        request
        for request in requests
        if any(
            timestamp(request["started_at"])
            <= timestamp(row["observed_at"])
            <= timestamp(request["completed_at"])
            for row in transition_samples
        )
    ]
    errors = [row for row in observations if "error" in row]
    policy_hashes = {
        row["spec_sha256"] for row in observations if row.get("spec_sha256")
    }
    hot_losses = [
        row["observed_at"]
        for row in observations
        if "pods" in row
        and {
            pod["uid"] for pod in row["pods"] if pod["ready"] and not pod["deleting"]
        }.intersection(hot_uids)
        != hot_uids
    ]
    failed = [row["ordinal"] for row in requests if row["status"] != "passed"]
    ready_during_transition = bool(transition_samples) and all(
        row.get("model_status", {}).get("phase") == "Ready"
        for row in transition_samples
    )
    covered = bool(transition_samples and overlapping)
    passed = bool(
        requests
        and not failed
        and not hot_losses
        and not errors
        and len(policy_hashes) == 1
    )
    return {
        "status": "passed"
        if passed and covered and ready_during_transition
        else "coverage-not-observed"
        if passed and not covered
        else "failed",
        "request_count": len(requests),
        "failed_ordinals": failed,
        "all_public_calls_succeeded": bool(requests) and not failed,
        "transition_samples": len(transition_samples),
        "overlapping_request_ordinals": [row["ordinal"] for row in overlapping],
        "model_ready_during_transition": ready_during_transition,
        "baseline_hot_uids": sorted(hot_uids),
        "baseline_burst_uids": sorted(baseline_burst_uids),
        "new_burst_pod_uids": sorted(observed_burst_uids - baseline_burst_uids),
        "trigger_attribution": "Temporal observation only; preexisting burst Pods are not counted as test-created.",
        "hot_readiness_losses": hot_losses,
        "observation_errors": len(errors),
        "desired_spec_unchanged": len(policy_hashes) == 1,
        "nonterminal_or_unknown_operation_ids": [
            row["operation_id"]
            for row in requests
            if row.get("operation_id")
            and row.get("operation", {}).get("status")
            not in {"succeeded", "failed", "cancelled", "expired"}
        ],
        "coverage_note": "Sampled evidence, not a continuous availability SLA. No observed overlap means no live transition qualification.",
    }


async def run(args):
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    access = json.loads(args.credentials.read_bytes())
    credentials = access["credentials"]
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    sample_args = SimpleNamespace(
        origin=origin, phase_file=args.output / "unused-phase.json"
    )
    kubectl = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "--request-timeout=10s",
    ]

    async def get(*arguments):
        process = await asyncio.create_subprocess_exec(
            *kubectl,
            "get",
            *arguments,
            "-n",
            "fs2-models",
            "-o",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 15)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError("bounded observation timeout") from None
        if process.returncode:
            raise RuntimeError("read-only observation failed")
        return json.loads(output)

    async def observe():
        try:
            pods, model = await asyncio.gather(
                get("pods", "-l", SELECTOR),
                get("modeldeployments.inference.fs2.nebius.ai", "qwen3-8b"),
            )
            return {
                "observed_at": utc(),
                "pods": [pod_state(item) for item in pods["items"]],
                "model_status": model.get("status", {}),
                "model_resource_version": model["metadata"].get("resourceVersion"),
                "spec_sha256": hash_json(model["spec"]),
            }
        except Exception as error:
            return {"observed_at": utc(), "error": safe_exception(error)}

    observations = [await observe()]
    requests = []
    plan = {
        "started_at": utc(),
        "concurrency": 3,
        "maximum_waves": args.waves,
        "maximum_requests": args.waves * 3,
        "wave_interval_seconds": 10,
        "new_admission_window_seconds": 120,
        "recovery_observation_seconds": 30,
        "submission_attempts_per_request": 1,
        "public_api_only_for_inference": True,
        "configuration_mutations": False,
        "independent_of_normal_sampler": True,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "fixture_source_sha256": hashlib.sha256(
            Path(__file__).with_name("interactive_sampler.py").read_bytes()
        ).hexdigest(),
    }
    with (args.output / "plan.json").open("x") as stream:
        json.dump(plan, stream, indent=2)
    if not any(
        (pod.get("role") or "").startswith("hot-") and pod["ready"]
        for pod in observations[0].get("pods", [])
    ):
        with (args.output / "baseline.json").open("x") as stream:
            json.dump(observations[0], stream)
        raise RuntimeError("no observed ready hot baseline; no requests submitted")

    stop = asyncio.Event()
    with (
        (args.output / "observations.jsonl").open("x") as observations_file,
        (args.output / "requests.jsonl").open("x") as requests_file,
    ):
        observations_file.write(json.dumps(observations[0]) + "\n")
        observations_file.flush()

        async def observe_loop():
            while not stop.is_set():
                row = await observe()
                observations.append(row)
                observations_file.write(json.dumps(row) + "\n")
                observations_file.flush()
                try:
                    await asyncio.wait_for(stop.wait(), 2)
                except TimeoutError:
                    pass

        observer = asyncio.create_task(observe_loop())
        try:
            start = time.monotonic()
            for wave in range(args.waves):
                if time.monotonic() - start >= 120:
                    break
                wave_started = time.monotonic()
                rows = await asyncio.gather(
                    *(
                        sample(sample_args, credentials, wave * 3 + offset)
                        for offset in (1, 2, 3)
                    )
                )
                for row in rows:
                    row["phase"] = "dedicated-burst-regression"
                    encoded = json.dumps(row)
                    if any(
                        value in encoded
                        for value in credentials.values()
                        if isinstance(value, str)
                    ):
                        raise ValueError("credential in receipt")
                    requests_file.write(encoded + "\n")
                    requests_file.flush()
                    requests.append(row)
                print(
                    json.dumps(
                        {
                            "wave": wave + 1,
                            "passed": sum(row["status"] == "passed" for row in rows),
                            "attempts": len(rows),
                        }
                    ),
                    flush=True,
                )
                if any(row["status"] != "passed" for row in rows):
                    break
                await asyncio.sleep(max(0, 10 - (time.monotonic() - wave_started)))
            await asyncio.sleep(30)
        finally:
            stop.set()
            await observer
    result = {**verdict(observations, requests), "completed_at": utc()}
    with (args.output / "summary.json").open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--waves", type=int, choices=range(1, 13), default=12)
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
