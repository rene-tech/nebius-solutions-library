"""Real demo-owned REST import, expected bootstrap stop, then native :resume.

Uses the supplied demo key only. Never changes credentials, quotas, runtime or
customer state. An interrupted runner reuses its recorded IDs/idempotency keys.
All native history and every final artifact are checked by actual SHA-256 bytes.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import time

import httpx2

from prepare_acceptance import sha, native_step, relative_path
from validate_resume import (
    delivery_gate, preserved_history, restart_step, summarize_progress,
    topology_equivalence, verify_customer_storage,
)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as output:
        json.dump(value, output, indent=2, sort_keys=True, allow_nan=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_owner(policy):
    if (policy["tenant_id"], policy["principal_id"]) != ("demo-user", "demo-user"):
        raise ValueError("This acceptance permits only the existing operator-selected demo identity")
    if "gromacs" not in policy["models"] or policy["max_concurrency"] != 2:
        raise ValueError("Demo App/policy differs; do not bypass or raise it")


async def run(args):
    fixture = json.loads((args.fixture / "fixture.json").read_text())
    if (sha(args.fixture / "request.json"), sha(args.fixture / "input.tar.gz")) != (
        fixture["request_sha256"], fixture["input_sha256"],
    ):
        raise ValueError("Prepared native import fixture changed")
    credential = json.loads(args.demo_key.read_text())
    validate_owner(credential["key"])
    spec = importlib.util.spec_from_file_location(
        "scientific_artifact_client", args.client_root / "scripts/scientific-batch-acceptance.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    args.output.mkdir(parents=True, exist_ok=True)
    state_path = args.output / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "fixture_sha256": sha(args.fixture / "fixture.json"), "created_at": now(),
        "label": fixture["label"], "admission_timings": [],
    }
    if state["fixture_sha256"] != sha(args.fixture / "fixture.json"):
        raise ValueError("Do not reuse a prior admission identity for changed parameters")
    save(state_path, state)
    identity = "fs2-lynx-demo-20261005-" + fixture["label"]
    async with httpx2.AsyncClient(base_url=args.origin, timeout=120, trust_env=False,
                                 headers={"authorization": "Bearer " + credential["secret"],
                                          "origin": args.origin}) as http:
        async def get(path):
            for attempt in range(4):
                started = time.monotonic()
                try:
                    response = await http.get(path)
                    if response.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                        response.raise_for_status()
                        return response.json()
                    error = f"HTTP_{response.status_code}"
                except httpx2.TransportError as exc:
                    error = type(exc).__name__
                    if attempt == 3:
                        raise
                with (args.output / "transient-reads.jsonl").open("a") as handle:
                    handle.write(json.dumps({"at": now(), "path": path, "attempt": attempt + 1,
                                             "error": error, "seconds": time.monotonic() - started}) + "\n")
                await asyncio.sleep(2 ** attempt)
            raise AssertionError("unreachable")

        policy = await get("/v1/me")
        validate_owner(policy)
        save(args.output / "caller-policy.json", policy)

        async def post(path, body, key, phase):
            started = time.monotonic()
            response = await http.post(path, json=body, headers={"Idempotency-Key": key})
            state["admission_timings"].append({"phase": phase, "at": now(),
                "seconds": time.monotonic() - started, "status": response.status_code})
            save(state_path, state)
            if response.status_code != 202:
                save(args.output / (phase + "-error.json"), response.json())
            response.raise_for_status()
            return response.json()

        async def download(pointer, name):
            path = args.output / name
            started = time.monotonic()
            receipt = await helper.download(http, pointer, path)
            with (args.output / "transfers.jsonl").open("a") as handle:
                handle.write(json.dumps({"at": now(), "artifact_id": pointer["artifact_id"], "name": name,
                    "sha256": pointer["sha256"], "size_bytes": pointer["size_bytes"],
                    "wall_seconds": time.monotonic() - started,
                    "transfer_attempts": receipt.get("transfer_attempts"),
                    "publication": receipt["publication"]}) + "\n")
            return path

        async def checkpoint(operation, label):
            choices = await get(f"/v1/operations/{operation}/checkpoints")
            if len(choices["jobs"]) != 1:
                raise ValueError("Expected one committed checkpoint for the imported job")
            pointer = choices["jobs"][0]["checkpoint"]
            document = json.loads((await download(pointer, f"{label}-{pointer['sha256']}.json")).read_text())
            preserved_history(fixture, document)
            if document["customer_storage"]["bucket"] != "fs2-demo-user-7cf4fbaf2a81493b":
                raise ValueError("Checkpoint exported outside the assigned demo bucket")
            save(args.output / (label + "-checkpoint.json"), document)
            return document

        async def poll(operation, phase):
            deadline = time.monotonic() + args.timeout
            last_status, next_checkpoint = None, time.monotonic() + 90
            while time.monotonic() < deadline:
                value = await get(f"/v1/operations/{operation}")
                save(args.output / (phase + "-status.json"), value)
                status = value["batch"]["status"]
                if status != last_status:
                    print(json.dumps({"at": now(), "phase": phase, "operation": operation, "status": status}), flush=True)
                    last_status = status
                if status in {"succeeded", "failed", "cancelled"} and value["batch"]["result_published"]:
                    return value
                if phase == "resumed" and time.monotonic() >= next_checkpoint:
                    choices = await get(f"/v1/operations/{operation}/checkpoints")
                    if choices["jobs"]:
                        document = await checkpoint(operation, "observed")
                        evidence = {"at": now(), "operation": operation, **summarize_progress(document)}
                        with (args.output / "progress.jsonl").open("a") as handle:
                            handle.write(json.dumps(evidence) + "\n")
                        print(json.dumps(evidence), flush=True)
                    next_checkpoint = time.monotonic() + 90
                await asyncio.sleep(15)
            raise TimeoutError("Retained operation still runs; rerun this receipt rather than duplicate work")

        if "source_operation" not in state:
            body_path = args.output / "import-request.json"
            if body_path.exists():
                body = json.loads(body_path.read_text())
            else:
                upload_started = time.monotonic()
                artifact = await helper.upload(http, "gromacs", helper.FileSource(args.fixture / "input.tar.gz"),
                    "application/x-tar", "gzip", identity + "-bundle")
                manifest = {"schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1", "manifest_id": identity,
                            "entries": [{"name": "gromacs-inputs", "semantic_type": "gromacs-input-bundle/v1",
                                         "artifact": artifact}]}
                pointer = await helper.upload(http, "gromacs", helper.canonical(manifest),
                    "application/vnd.fs2.scientific-manifest+json", "none", identity + "-manifest")
                body = {"schema": "fs2-serve.nebius.ai/scientific-run-request/v1", "operation": "run-workflow",
                        "service_class": "customer-batch", "input_manifest": pointer,
                        "parameters": json.loads((args.fixture / "request.json").read_text()),
                        "client_context": {"display_name": "Authorized demo late-checkpoint qualification",
                                           "correlation_id": identity}}
                save(body_path, body)
                state["input_upload_seconds"] = time.monotonic() - upload_started
                state["input_upload_bytes"] = (args.fixture / "input.tar.gz").stat().st_size
                save(state_path, state)
            admitted = await post("/v1/models/gromacs:submit", body, identity + "-bootstrap", "bootstrap")
            state["source_operation"] = admitted["operation"]["id"]
            save(state_path, state)
            save(args.output / "bootstrap-admission.json", admitted)
        if not state.get("idempotent_bootstrap_replay"):
            body = json.loads((args.output / "import-request.json").read_text())
            replay = await post("/v1/models/gromacs:submit", body, identity + "-bootstrap", "bootstrap-replay")
            if (replay["operation"]["id"], replay["operation"]["reused"]) != (state["source_operation"], True):
                raise ValueError("Bootstrap replay duplicated native work")
            state["idempotent_bootstrap_replay"] = True
            save(state_path, state)
        bootstrap = await poll(state["source_operation"], "bootstrap")
        if (bootstrap["batch"]["status"], bootstrap["batch"]["failure_code"]) != (
            "failed", "WORKFLOW_TIME_LIMIT_EXCEEDED",
        ):
            save(args.output / "bootstrap-unexpected-result.json",
                 await get(f"/v1/operations/{state['source_operation']}/result"))
            raise ValueError("Bootstrap did not reach its deliberate clean execution-budget stop")
        source = await checkpoint(state["source_operation"], "bootstrap")
        saved = native_step(source)
        if not fixture["source_step"] < saved < fixture["target_step"]:
            raise ValueError("Imported native checkpoint did not make bounded forward progress")
        files = {row["path"]: row for row in source["files"]}
        comparison_command = next(row for row in source["state"]["commands"] if row["step_id"] == "verify-tpr")
        comparison = await download(files[comparison_command["log"]]["artifact"], "full-tpr-comparison.log")
        save(args.output / "topology-equivalence.json", topology_equivalence(comparison.read_text(), fixture["target_step"]))
        first_native = next(row for row in source["state"]["commands"] if "mdrun" in row["command"])
        first_log = await download(files[first_native["log"]]["artifact"], "bootstrap-native.log")
        if restart_step(first_log.read_text()) != fixture["source_step"]:
            raise ValueError("Import restarted at time zero or the wrong late checkpoint")
        state["saved_step"] = saved
        save(state_path, state)

        if "resume_operation" not in state:
            admitted = await post(f"/v1/operations/{state['source_operation']}:resume",
                                  {"max_wall_seconds": 1209600}, identity + "-resume", "resume")
            state["resume_operation"] = admitted["operation"]["id"]
            save(state_path, state)
            save(args.output / "resume-admission.json", admitted)
        if not state.get("idempotent_resume_replay"):
            replay = await post(f"/v1/operations/{state['source_operation']}:resume",
                               {"max_wall_seconds": 1209600}, identity + "-resume", "resume-replay")
            if (replay["operation"]["id"], replay["operation"]["reused"]) != (state["resume_operation"], True):
                raise ValueError("Resume replay duplicated native work")
            state["idempotent_resume_replay"] = True
            save(state_path, state)
        terminal = await poll(state["resume_operation"], "resumed")
        result = await get(f"/v1/operations/{state['resume_operation']}/result")
        save(args.output / "result.json", result)
        if result.get("semantic_validation", {}).get("status") != "passed":
            raise ValueError("Resumed result did not pass semantic validation; failed evidence is retained")
        final = await checkpoint(state["resume_operation"], "final")
        files = {row["path"]: row for row in final["files"]}
        native = [row for row in final["state"]["commands"] if "mdrun" in row["command"]]
        first_log = await download(files[native[0]["log"]]["artifact"], "resumed-native.log")
        if restart_step(first_log.read_text()) != saved:
            raise ValueError("Same-demo :resume did not begin at its exact saved native step")
        gate = delivery_gate(fixture, saved, terminal, final)
        save(args.output / "delivery-gate.json", gate)
        semaphore = asyncio.Semaphore(8)

        async def verify_file(row):
            async with semaphore:
                await download(row["artifact"], "verified-files/" + relative_path(row["path"]))

        verification_started = time.monotonic()
        for offset in range(0, len(final["files"]), 64):
            await asyncio.gather(*(verify_file(row) for row in final["files"][offset:offset + 64]))
        platform_verification_seconds = time.monotonic() - verification_started
        import boto3
        from botocore.config import Config
        storage = json.loads(args.demo_storage.read_text())
        if storage["bucket_name"] != "fs2-demo-user-7cf4fbaf2a81493b":
            raise ValueError("Unexpected storage identity")
        client = boto3.client("s3", endpoint_url=storage["endpoint"], region_name=storage["region"],
            aws_access_key_id=storage["access_key_id"], aws_secret_access_key=storage["secret_access_key"],
            config=Config(max_pool_connections=4, retries={"mode": "standard", "max_attempts": 3}))
        try:
            verification_started = time.monotonic()
            exported = await asyncio.to_thread(verify_customer_storage, client, final,
                                               expected_bucket=storage["bucket_name"])
            exported["wall_seconds"] = time.monotonic() - verification_started
        finally:
            client.close()
        save(args.output / "customer-export-verification.json", exported)
        receipt = {**state, **gate, "preserved_source_files": preserved_history(fixture, final),
                   "verified_files": len(files), "verified_bytes": sum(row["size_bytes"] for row in files.values()),
                   "all_platform_artifact_bytes_checked": True, "customer_key_used": False,
                   "platform_verification_seconds": platform_verification_seconds,
                   "fourteen_day_soak_claimed": False, "fixture_setup_expected_timeout": True,
                   "customer_bucket_export": final["customer_storage"], "customer_bytes_verified": exported}
        save(args.output / "receipt.json", receipt)
        print(json.dumps(receipt), flush=True)
        if gate["status"] != "passed":
            raise ValueError("Native/output checks passed but customer duration/performance gate failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fixture", "demo-key", "demo-storage", "client-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--timeout", type=int, default=43200)
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
