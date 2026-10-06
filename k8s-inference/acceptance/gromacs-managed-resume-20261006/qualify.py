"""Internal-key late-state acceptance of automatic plain-API resume.

Reuses the retained, authorized copy and existing artifact client. No customer
key, quota changes or production cancellation. Private evidence never goes in Git.
"""

import argparse
import asyncio
import copy
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx2

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lynx-demo-resume-20261005"))
from run_demo_resume import save  # noqa: E402
from validate_resume import delivery_gate, preserved_history, restart_step, topology_equivalence  # noqa: E402


async def run(args):
    values = dict(line.split("=", 1) for line in args.qa_env.read_text().splitlines() if "=" in line)
    credential = values["SCIENTIFIC_MODELS_API_KEY"]
    spec = importlib.util.spec_from_file_location("artifact_client", args.artifact_client)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    fixture = json.loads((args.fixture / "fixture.json").read_text())
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = args.output / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    identity = "gromacs-managed-resume-20261006-" + args.cohort
    async with httpx2.AsyncClient(
        base_url="https://89.169.99.188",
        timeout=120,
        trust_env=False,
        headers={"Authorization": "Bearer " + credential},
    ) as client:

        async def get(path):
            response = await client.get(path)
            response.raise_for_status()
            return response.json()

        async def post(path, body, key):
            response = await client.post(path, json=body, headers={"Idempotency-Key": key})
            if response.status_code != 202:
                save(args.output / "last-api-error.json", {"status": response.status_code, "body": response.json()})
            response.raise_for_status()
            return response.json()

        policy = await get("/v1/me")
        if (policy["tenant_id"], policy["principal_id"]) != ("system", "qa"):
            raise ValueError("Only the existing system/qa identity may run internal acceptance")
        save(args.output / "caller-policy.json", policy)

        async def checkpoint(operation, label):
            choices = await get(f"/v1/operations/{operation}/checkpoints")
            save(args.output / (label + "-choices.json"), choices)
            pointer = choices["jobs"][0]["checkpoint"]
            path = args.output / (label + "-checkpoint.json")
            await helper.download(client, pointer, path)
            return json.loads(path.read_text())

        async def poll(operation, label):
            previous = None
            for _ in range(240):
                status = await get(f"/v1/operations/{operation}")
                save(args.output / (label + "-status.json"), status)
                now = status["batch"]["status"]
                if now != previous:
                    print(
                        json.dumps(
                            {
                                "at": datetime.now(UTC).isoformat(),
                                "phase": label,
                                "operation": operation,
                                "status": now,
                            }
                        ),
                        flush=True,
                    )
                    previous = now
                if now in {"succeeded", "failed", "cancelled"} and status["batch"]["result_published"]:
                    return status
                await asyncio.sleep(10)
            raise TimeoutError("Retain the operation; do not duplicate work")

        if args.phase == "bootstrap":
            if "source_operation" in state:
                print(json.dumps(state))
                return
            request = json.loads((args.fixture / "request.json").read_text())
            # Recreate the original omission, not a pre-tuned source. Preparation
            # and complete science comparison stay exactly as the retained fixture.
            for job in request["jobs"]:
                for step in job["steps"]:
                    if step["command"] == "mdrun":
                        step["args"] = ["-s", "acceptance.tpr", "-deffnm", "continued"]
                    for item in step["args"]:
                        if isinstance(item, dict):
                            item.pop("nonempty", None)
            request["max_wall_seconds"] = 120
            request["output_prefix"] = "runs/" + identity
            artifact = await helper.upload(
                client,
                "gromacs",
                helper.FileSource(args.fixture / "input.tar.gz"),
                "application/x-tar",
                "gzip",
                identity + "-bundle",
            )
            manifest = {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                "manifest_id": identity,
                "entries": [
                    {"name": "gromacs-inputs", "semantic_type": "gromacs-input-bundle/v1", "artifact": artifact}
                ],
            }
            pointer = await helper.upload(
                client,
                "gromacs",
                helper.canonical(manifest),
                "application/vnd.fs2.scientific-manifest+json",
                "none",
                identity + "-manifest",
            )
            body = {
                "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
                "operation": "run-workflow",
                "service_class": "customer-batch",
                "input_manifest": pointer,
                "parameters": request,
            }
            save(args.output / "bootstrap-request.json", body)
            admitted = await post("/v1/models/gromacs:submit", body, identity + "-bootstrap")
            state["source_operation"] = admitted["operation"]["id"]
            save(state_path, state)
            save(args.output / "bootstrap-admission.json", admitted)
            print(json.dumps(state), flush=True)
            return

        bootstrap = await poll(state["source_operation"], "bootstrap")
        if (bootstrap["batch"]["status"], bootstrap["batch"]["failure_code"]) != (
            "failed",
            "WORKFLOW_TIME_LIMIT_EXCEEDED",
        ):
            raise ValueError("Expected bounded bootstrap budget stop was not observed")
        source = await checkpoint(state["source_operation"], "source")
        saved = max(row.get("checkpoint_step") or 0 for row in source["state"]["commands"])
        indexed = {row["path"]: row for row in source["files"]}
        comparison = next(row for row in source["state"]["commands"] if row["step_id"] == "verify-tpr")
        await helper.download(client, indexed[comparison["log"]]["artifact"], args.output / "tpr-comparison.log")
        topology_equivalence((args.output / "tpr-comparison.log").read_text(), fixture["target_step"])
        admitted = await post(f"/v1/operations/{state['source_operation']}:resume", {}, identity + "-resume")
        adjustments = admitted["continuation"]["adjustments"]
        if adjustments["profile_id"] != "single-gpu-list200-v1" or len(adjustments["analysis_selectors"]) != 2:
            raise ValueError("Plain resume failed to apply the qualified defaults and both analysis fixes")
        state["resume_operation"] = admitted["operation"]["id"]
        save(state_path, state)
        save(args.output / "resume-admission.json", admitted)
        replay = await post(f"/v1/operations/{state['source_operation']}:resume", {}, identity + "-resume")
        if replay["operation"]["id"] != state["resume_operation"] or not replay["operation"]["reused"]:
            raise ValueError("Idempotent replay duplicated work")
        save(args.output / "resume-replay.json", replay)
        final_status = await poll(state["resume_operation"], "resumed")
        result = await get(f"/v1/operations/{state['resume_operation']}/result")
        save(args.output / "result.json", result)
        if result.get("semantic_validation", {}).get("status") != "passed":
            raise ValueError("Terminal semantic validation failed")
        final = await checkpoint(state["resume_operation"], "final")
        semaphore = asyncio.Semaphore(8)

        async def verify(row):
            async with semaphore:
                await helper.download(client, row["artifact"], args.output / "verified-files" / row["path"])

        await asyncio.gather(*(verify(row) for row in final["files"]))
        native = [row for row in final["state"]["commands"] if "mdrun" in row["command"]]
        log = (args.output / "verified-files" / native[0]["log"]).read_text()
        if restart_step(log) != saved or "-bonded gpu" not in log or "-nstlist 200" not in log:
            raise ValueError("Native execution differs from the selected checkpoint or tuning")
        bounded = copy.deepcopy(fixture)
        bounded.update(minimum_native_seconds=0, minimum_delivered_ns_per_day=0)
        gate = delivery_gate(bounded, saved, final_status, final)
        receipt = {
            **state,
            **gate,
            "adjustments": adjustments,
            "verified_files": len(final["files"]),
            "history_files": preserved_history(fixture, final),
            "customer_key_used": False,
            "scope": "two-ns API resume; not a new six-hour soak",
        }
        save(args.output / "receipt.json", receipt)
        print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["bootstrap", "qualify"])
    parser.add_argument("--cohort", required=True)
    for name in ("qa-env", "fixture", "artifact-client", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    os.umask(0o077)
    asyncio.run(run(parser.parse_args()))
