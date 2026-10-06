"""Exact owner-authorized Lynx continuation, never an internal benchmark.

The owner explicitly requested customer-key stop/resume on 6 October 2026.
Only the named existing operation is eligible; no new scientific input is built.
Inspect first, run qualification separately, then stop at a committed boundary.
"""

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx2

SOURCE = "a42479f9-5ee0-4ed4-869b-0a094357403f"
TPR = "e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lynx-demo-resume-20261005"))
from run_demo_resume import save  # noqa: E402
from validate_resume import restart_step, summarize_progress  # noqa: E402


def released(status):
    attempts = [attempt for stage in status["batch"]["stages"] for attempt in stage["attempts"]]
    return bool(attempts) and all(attempt["resource_released"] for attempt in attempts)


async def run(args):
    if args.action != "inspect" and not args.owner_authorized:
        raise ValueError("Customer-key continuation requires explicit owner authorization")
    key = json.loads(args.key_file.read_text())
    if key["key"]["tenant_id"] != "lynx":
        raise ValueError("Wrong customer key identity")
    spec = importlib.util.spec_from_file_location("artifact_client", args.artifact_client)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = args.output / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"source_operation": SOURCE}
    async with httpx2.AsyncClient(
        base_url="https://89.169.99.188",
        timeout=120,
        trust_env=False,
        headers={"Authorization": "Bearer " + key["secret"]},
    ) as client:

        async def get(path):
            response = await client.get(path)
            response.raise_for_status()
            return response.json()

        async def checkpoint(operation, label):
            choices = await get(f"/v1/operations/{operation}/checkpoints")
            save(args.output / (label + "-choices.json"), choices)
            if not choices["jobs"]:
                return None
            selected = next(row for row in choices["jobs"] if row["job_id"] == "mas1-20e")
            # Each committed generation is immutable. Keep its bytes under its
            # artifact identity, rather than reusing a previous generation's
            # download destination (which the client correctly refuses).
            directory = args.output / "checkpoints"
            directory.mkdir(mode=0o700, exist_ok=True)
            path = directory / (selected["checkpoint"]["artifact_id"] + ".json")
            await helper.download(client, selected["checkpoint"], path)
            document = json.loads(path.read_text())
            if document["state"]["operation_id"] != operation:
                raise ValueError("Wrong native operation")
            active = document["state"].get("active_step")
            if active and (active["tpr_sha256"], active["target_step"]) != (TPR, 500000000):
                raise ValueError("Scientific input or full target changed")
            if document["customer_storage"]["bucket"] != "fs2-lynx-c327dcc386444425":
                raise ValueError("Customer bucket identity changed")
            save(args.output / (label + "-checkpoint.json"), document)
            return document

        policy = await get("/v1/me")
        if policy["tenant_id"] != "lynx":
            raise ValueError("Live token resolved to a different tenant")
        save(args.output / "caller-policy.json", policy)
        if args.action == "inspect":
            status = await get(f"/v1/operations/{SOURCE}")
            save(args.output / "before-status.json", status)
            document = await checkpoint(SOURCE, "before")
            print(
                json.dumps(
                    {"operation": SOURCE, "status": status["batch"]["status"], "progress": summarize_progress(document)}
                ),
                flush=True,
            )
        elif args.action == "stop":
            status = await get(f"/v1/operations/{SOURCE}")
            if status["batch"]["status"] == "running":
                document = await checkpoint(SOURCE, "before-stop")
                generation = document["state"]["generation"]
                # Cancel immediately after the next durable export. Do not
                # discard the last five minutes just to speed up a rollout.
                deadline = time.monotonic() + 900
                while document["state"]["generation"] <= generation:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("No fresh durable boundary; customer remains running")
                    await asyncio.sleep(5)
                    document = await checkpoint(SOURCE, "before-stop")
                state["stopped_after_generation"] = document["state"]["generation"]
                state["stopped_after_step"] = summarize_progress(document)["checkpoint_step"]
                save(state_path, state)
                response = await client.post(f"/v1/operations/{SOURCE}:cancel")
                response.raise_for_status()
                save(args.output / "cancel-response.json", response.json())
            for _ in range(120):
                status = await get(f"/v1/operations/{SOURCE}")
                save(args.output / "stopped-status.json", status)
                if status["batch"]["status"] in {"failed", "cancelled"} and released(status):
                    document = await checkpoint(SOURCE, "stopped")
                    state["resume_from_step"] = summarize_progress(document)["checkpoint_step"]
                    save(state_path, state)
                    print(json.dumps(state), flush=True)
                    return
                await asyncio.sleep(5)
            raise TimeoutError("Stop not yet released; retain state and do not duplicate the run")
        elif args.action == "resume":
            previous = await get(f"/v1/operations/{SOURCE}")
            if previous["batch"]["status"] not in {"failed", "cancelled"} or not released(previous):
                raise ValueError("Source must stop and release capacity before this authorized continuation")
            before = await checkpoint(SOURCE, "resume-source")
            for iteration in range(2):
                response = await client.post(
                    f"/v1/operations/{SOURCE}:resume",
                    json={},
                    headers={"Idempotency-Key": "lynx-authorized-managed-resume-20261006-a42479f9"},
                )
                if response.status_code != 202:
                    save(args.output / "resume-error.json", {"status": response.status_code, "body": response.json()})
                response.raise_for_status()
                admitted = response.json()
                save(args.output / ("resume-replay.json" if iteration else "resume-admission.json"), admitted)
                if admitted["continuation"]["adjustments"]["profile_id"] != "single-gpu-list200-v1":
                    raise ValueError("Qualified performance defaults were not applied")
                if iteration and (
                    admitted["operation"]["id"] != state["resume_operation"] or not admitted["operation"]["reused"]
                ):
                    raise ValueError("Replay did not return the same authorized run")
                state["resume_operation"] = admitted["operation"]["id"]
                state["resume_from_step"] = summarize_progress(before)["checkpoint_step"]
                save(state_path, state)
            print(json.dumps(state), flush=True)
        else:
            operation = state["resume_operation"]
            last_generation = None
            for _ in range(120):
                status = await get(f"/v1/operations/{operation}")
                save(args.output / "current-status.json", status)
                if status["batch"]["status"] in {"failed", "cancelled"}:
                    raise ValueError("Customer continuation failed; inspect retained status")
                document = await checkpoint(operation, "current")
                if document and document["state"]["generation"] != last_generation:
                    progress = summarize_progress(document)
                    if progress["checkpoint_step"] < state["resume_from_step"]:
                        raise ValueError("Customer scientific progress moved backwards")
                    receipt = {
                        "at": datetime.now(UTC).isoformat(),
                        "operation": operation,
                        "status": status["batch"]["status"],
                        **progress,
                    }
                    with (args.output / "progress.jsonl").open("a") as stream:
                        stream.write(json.dumps(receipt) + "\n")
                    print(json.dumps(receipt), flush=True)
                    last_generation = progress["generation"]
                    if progress["zero_exit_segments"] >= args.minimum_segments:
                        native = [row for row in document["state"]["commands"] if "mdrun" in row["command"]]
                        for row in native:
                            for flag, setting in (("-nb", "gpu"), ("-bonded", "gpu"), ("-nstlist", "200")):
                                if (
                                    flag not in row["command"]
                                    or row["command"][row["command"].index(flag) + 1] != setting
                                ):
                                    raise ValueError("Actual customer native command is not performance optimized")
                        files = {row["path"]: row for row in document["files"]}
                        native_log = args.output / "first-native-segment.log"
                        await helper.download(client, files[native[0]["log"]]["artifact"], native_log)
                        if restart_step(native_log.read_text()) != state["resume_from_step"]:
                            raise ValueError("Actual customer native restart did not use the exact saved step")
                        source = json.loads((args.output / "resume-source-checkpoint.json").read_text())
                        retained = 0
                        for old in source["files"]:
                            if old["path"] == "simulation.tpr" or old["path"].startswith("md.part"):
                                current = files.get(old["path"], {})
                                if (current.get("sha256"), current.get("size_bytes")) != (
                                    old["sha256"],
                                    old["size_bytes"],
                                ):
                                    raise ValueError("Original scientific input/trajectory history was changed or lost")
                                retained += 1
                        accepted = datetime.fromisoformat(status["operation"]["accepted_at"])
                        committed = max(
                            datetime.fromisoformat(row["committed_at"])
                            for row in json.loads((args.output / "current-choices.json").read_text())["jobs"]
                        )
                        elapsed = (committed - accepted).total_seconds()
                        new_ns = (progress["checkpoint_step"] - state["resume_from_step"]) * 0.002 / 1000
                        receipt.update(
                            resume_from_step=state["resume_from_step"],
                            new_ns=new_ns,
                            accepted_to_checkpoint_seconds=elapsed,
                            delivered_ns_per_day=new_ns * 86400 / elapsed,
                            verified_native_start_step=state["resume_from_step"],
                            original_scientific_files_retained=retained,
                            native_performance_flags_verified=True,
                            customer_job_left_running=True,
                        )
                        receipt["minimum_delivered_ns_per_day"] = args.minimum_delivered_ns_per_day
                        receipt["performance_gate"] = (
                            "passed"
                            if receipt["delivered_ns_per_day"] >= args.minimum_delivered_ns_per_day
                            else "pending_longer_observation"
                        )
                        save(args.output / "verification.json", receipt)
                        if receipt["performance_gate"] == "passed":
                            return
                        print(json.dumps(receipt), flush=True)
                await asyncio.sleep(15)
            raise TimeoutError("Observation window exhausted; continuation left running")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "stop", "resume", "observe"])
    parser.add_argument("--owner-authorized", action="store_true")
    parser.add_argument("--minimum-segments", type=int, default=3)
    parser.add_argument("--minimum-delivered-ns-per-day", type=float, default=200.0)
    for name in ("key-file", "artifact-client", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    os.umask(0o077)
    asyncio.run(run(parser.parse_args()))
