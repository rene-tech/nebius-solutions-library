"""One owned2x1 MPI peer eviction after a durable native checkpoint.

Uses the existing system/qa key and public REST. The only fault injection is an
Eviction with the exact owned rank1 Pod UID, never a node/JobSet/customer delete.
Saved admission and eviction receipts make interrupted runs resumable.
"""

import argparse
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx2


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def kubectl(args, *command, document=None):
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            args.context,
            "--request-timeout=20s",
            "-n",
            "fs2-models",
            *command,
        ],
        input=None if document is None else json.dumps(document),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def owned_peer(pods, operation, attempt):
    """Refuse any ambiguous owner, shape or peer before asking for eviction."""
    if len(pods) != 2 or len({pod["spec"].get("nodeName") for pod in pods}) != 2:
        raise ValueError("Expected exactly two owned1GPU Pods on separate nodes")
    ranks = {}
    for pod in pods:
        labels = pod["metadata"]["labels"]
        if (
            labels.get("fs2.nebius.ai/operation-id") != operation
            or labels.get("fs2.nebius.ai/attempt-id") != attempt
            or labels.get("fs2.nebius.ai/model-id") != "gromacs-mpi"
            or pod["metadata"].get("namespace") != "fs2-models"
            or pod["metadata"].get("deletionTimestamp")
        ):
            raise ValueError("Refusing to evict an unowned, stale or foreign Pod")
        ranks[labels["jobset.sigs.k8s.io/job-index"]] = pod
        if (
            sum(
                int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0))
                for c in pod["spec"]["containers"]
            )
            != 1
        ):
            raise ValueError("Peer-loss qualification is bounded to one GPU per node")
    if set(ranks) != {"0", "1"}:
        raise ValueError("The MPI gang does not contain unique leader and peer ranks")
    return ranks["1"]


def retained_files(before, after):
    indexed = {item["path"]: item for item in after["files"]}
    preserved = 0
    for item in before["files"]:
        if item["path"].endswith(".tpr") or re.search(
            r"\.part\d+\.(?:xtc|trr|edr|gro|log)$", item["path"]
        ):
            current = indexed.get(item["path"], {})
            if (item["sha256"], item["size_bytes"]) != (
                current.get("sha256"),
                current.get("size_bytes"),
            ):
                raise ValueError(
                    "Recovery changed an already closed native trajectory, log or TPR"
                )
            preserved += 1
    if not preserved:
        raise ValueError("No retained native history was verified")
    return preserved


async def run(args):
    values = dict(
        line.split("=", 1)
        for line in args.qa_env.read_text().splitlines()
        if "=" in line
    )
    key = values["SCIENTIFIC_MODELS_API_KEY"]
    if not key.startswith("fs2_pat_56130b22ae09"):
        raise ValueError("Only the existing system/qa identity is permitted")
    spec = importlib.util.spec_from_file_location(
        "scientific_acceptance",
        args.client_root / "scripts/scientific-batch-acceptance.py",
    )
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    args.output.mkdir(parents=True, exist_ok=True)
    state_path = args.output / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    identity = "fs2-peer-recovery-20261005-" + args.label
    async with httpx2.AsyncClient(
        base_url=args.origin,
        headers={"Authorization": "Bearer " + key, "Origin": args.origin},
        timeout=120,
        trust_env=False,
    ) as http:

        async def get(path):
            response = await http.get(path)
            response.raise_for_status()
            return response.json()

        async def checkpoint(label):
            choices = await get(f"/v1/operations/{state['operation_id']}/checkpoints")
            if not choices["jobs"]:
                return None
            pointer = choices["jobs"][0]["checkpoint"]
            await helper.download(http, pointer, args.output / (label + ".json"))
            return json.loads((args.output / (label + ".json")).read_text())

        if "operation_id" not in state:
            archive = args.output / "input.tar.gz"
            if not archive.exists():
                with (
                    tarfile.open(args.fixture / "input.tar.gz", "r:gz") as original,
                    tarfile.open(archive, "w:gz") as out,
                ):
                    for member in original:
                        out.addfile(
                            member,
                            original.extractfile(member) if member.isfile() else None,
                        )
                    out.add(args.prebuilt_tpr, arcname="benchmark.tpr")
            payload = helper.FileSource(archive)
            artifact = await helper.upload(
                http,
                "gromacs-mpi",
                payload,
                "application/x-tar",
                "gzip",
                identity + "-input",
            )
            manifest = {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                "manifest_id": identity,
                "entries": [
                    {
                        "name": "gromacs-inputs",
                        "semantic_type": "gromacs-input-bundle/v1",
                        "artifact": artifact,
                    }
                ],
            }
            pointer = await helper.upload(
                http,
                "gromacs-mpi",
                helper.canonical(manifest),
                "application/vnd.fs2.scientific-manifest+json",
                "none",
                identity + "-manifest",
            )
            parameters = json.loads((args.fixture / "request.json").read_text())
            production = next(
                step
                for step in parameters["jobs"][0]["steps"]
                if step["command"] == "mdrun"
            )
            production["args"] = [
                "-s",
                "benchmark.tpr",
                "-deffnm",
                "repeat1",
                "-nb",
                "gpu",
                "-pme",
                "gpu",
                "-pmefft",
                "gpu",
                "-npme",
                "1",
                "-notunepme",
                "-bonded",
                "cpu",
                "-update",
                "cpu",
            ]
            parameters.update(
                nodes=2,
                gpus_per_node=1,
                threads=8,
                max_wall_seconds=1200,
                segment_minutes=0.2,
                output_destination="customer-bucket",
                output_prefix="runs/" + identity,
                jobs=[{"id": "gang", "steps": [production]}],
            )
            request = {
                "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
                "operation": "run-workflow",
                "service_class": "customer-batch",
                "input_manifest": pointer,
                "parameters": parameters,
                "client_context": {
                    "display_name": "Internal2x1 MPI peer recovery",
                    "correlation_id": identity,
                },
            }
            save(args.output / "request.json", request)
            response = await http.post(
                "/v1/models/gromacs-mpi:submit",
                json=request,
                headers={"Idempotency-Key": identity},
            )
            response.raise_for_status()
            state.update(
                operation_id=str(UUID(response.json()["operation"]["id"])),
                input_sha256=payload.sha256,
            )
            save(state_path, state)
            print(json.dumps({"phase": "admitted", **state}), flush=True)
            replay = await http.post(
                "/v1/models/gromacs-mpi:submit",
                json=request,
                headers={"Idempotency-Key": identity},
            )
            replay.raise_for_status()
            if (
                replay.json()["operation"]["id"] != state["operation_id"]
                or not replay.json()["operation"]["reused"]
            ):
                raise ValueError("Replay did not preserve the exact admission")
        deadline = time.monotonic() + args.timeout
        previous = None
        while time.monotonic() < deadline:
            status = await get(f"/v1/operations/{state['operation_id']}")
            save(args.output / "status.json", status)
            phase = status["batch"]["status"]
            if phase != previous:
                print(
                    json.dumps({"phase": phase, "operation_id": state["operation_id"]}),
                    flush=True,
                )
                previous = phase
            attempts = status["batch"]["stages"][0]["attempts"]
            if "eviction" not in state and phase == "running":
                before = await checkpoint("before-eviction")
                if before and before.get("customer_storage"):
                    saved_step = max(
                        (
                            item.get("checkpoint_step") or 0
                            for item in before["state"]["commands"]
                        ),
                        default=0,
                    )
                    if not 0 < saved_step < args.steps:
                        raise ValueError(
                            "The source is no longer a resumable partial simulation"
                        )
                    attempt = attempts[-1]
                    pods = await asyncio.to_thread(
                        kubectl,
                        args,
                        "get",
                        "pods",
                        "-l",
                        "fs2.nebius.ai/operation-id=" + state["operation_id"],
                        "-o",
                        "json",
                    )
                    peer = owned_peer(
                        pods["items"], state["operation_id"], attempt["attempt_id"]
                    )
                    save(
                        args.output / "fault-pods.json",
                        [
                            {
                                "metadata": pod["metadata"],
                                "status": pod["status"],
                                "node": pod["spec"].get("nodeName"),
                                "containers": [
                                    {
                                        key: c.get(key)
                                        for key in ("name", "image", "resources")
                                    }
                                    for c in pod["spec"]["containers"]
                                ],
                            }
                            for pod in pods["items"]
                        ],
                    )
                    name, uid = peer["metadata"]["name"], peer["metadata"]["uid"]
                    eviction = {
                        "apiVersion": "policy/v1",
                        "kind": "Eviction",
                        "metadata": {"name": name, "namespace": "fs2-models"},
                        "deleteOptions": {"preconditions": {"uid": uid}},
                    }
                    # Persist intent BEFORE injection: reruns never evict a second
                    # peer if the process lost the successful response.
                    state.update(
                        eviction={
                            "pod": name,
                            "uid": uid,
                            "attempt_id": attempt["attempt_id"],
                            "requested_at": datetime.now(UTC).isoformat(),
                        },
                        saved_step=saved_step,
                    )
                    save(state_path, state)
                    result = await asyncio.to_thread(
                        kubectl,
                        args,
                        "create",
                        "--raw",
                        f"/api/v1/namespaces/fs2-models/pods/{name}/eviction",
                        "-f",
                        "-",
                        document=eviction,
                    )
                    save(args.output / "eviction-response.json", result)
                    print(json.dumps({"phase": "peer-evicted", **state}), flush=True)
            if (
                phase in {"succeeded", "failed", "cancelled"}
                and status["batch"]["result_published"]
            ):
                if phase != "succeeded" or "eviction" not in state or len(attempts) < 2:
                    raise ValueError(
                        "Same-operation peer-loss recovery did not succeed; retain exact evidence"
                    )
                if attempts[0]["failure_kind"] not in {"infrastructure", "preemption"}:
                    raise ValueError(
                        "The peer-loss fault was not classified as recoverable infrastructure"
                    )
                result = await get(f"/v1/operations/{state['operation_id']}/result")
                save(args.output / "result.json", result)
                if result["semantic_validation"]["status"] != "passed":
                    raise ValueError("Final native artifact validation did not pass")
                final = await checkpoint("final-checkpoint")
                before = json.loads((args.output / "before-eviction.json").read_text())
                commands = final["state"]["commands"]
                if commands[-1]["checkpoint_step"] != args.steps:
                    raise ValueError(
                        "Recovered native simulation did not reach the unchanged finite target"
                    )
                preserved = retained_files(before, final)
                resumed = [
                    command for command in commands if "-cpi" in command["command"]
                ]
                if not resumed:
                    raise ValueError("No native checkpoint continuation was executed")
                files = {item["path"]: item for item in final["files"]}
                semaphore = asyncio.Semaphore(8)

                async def download(item):
                    path = Path(item["path"])
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("Checkpoint path escaped its workspace")
                    async with semaphore:
                        await helper.download(
                            http,
                            item["artifact"],
                            args.output / "verified-files" / path,
                        )

                for offset in range(0, len(files), 128):
                    await asyncio.gather(
                        *(
                            download(item)
                            for item in list(files.values())[offset : offset + 128]
                        )
                    )
                continuing = []
                for command in resumed:
                    text = (args.output / "verified-files" / command["log"]).read_text()
                    continuing.extend(
                        int(step)
                        for step in re.findall(
                            r"continuing from step\s+(\d+)", text, re.I
                        )
                    )
                if state["saved_step"] not in continuing:
                    raise ValueError(
                        "No retained native log resumes the committed pre-fault step"
                    )
                receipt = {
                    **state,
                    "status": "passed",
                    "final_step": args.steps,
                    "attempts": attempts,
                    "preserved_native_files": preserved,
                    "verified_files": len(files),
                    "customer_key_used": False,
                    "full_budget_soak_tested": False,
                }
                save(args.output / "receipt.json", receipt)
                print(json.dumps(receipt), flush=True)
                return
            await asyncio.sleep(3)
        raise TimeoutError(
            "Owned operation remains recorded; rerun this receipt, do not admit duplicate work"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("qa-env", "client-root", "fixture", "prebuilt-tpr", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--steps", type=int, default=60000)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{1,40}", args.label):
        parser.error("label must be a bounded task-owned identifier")
    os.umask(0o077)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
