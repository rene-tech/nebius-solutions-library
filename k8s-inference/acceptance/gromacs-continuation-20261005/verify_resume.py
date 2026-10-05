"""Real public timeout/checkpoint/resume acceptance, only existing system/qa.

One operation at a time per invocation; no key/pool/quota changes. Reruns reuse
saved operation IDs and idempotency keys. Never use a customer key here.
"""

import argparse
import asyncio
import importlib.util
import io
import json
import os
import re
import tarfile
import time
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from fs2_serve.live_acceptance import _mcp_result


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


async def run(args):
    values = dict(
        line.split("=", 1)
        for line in args.qa_env.read_text().splitlines()
        if "=" in line
    )
    key = values["SCIENTIFIC_MODELS_API_KEY"]
    if not key.startswith("fs2_pat_56130b22ae09"):
        raise ValueError("Only existing system/qa is permitted")
    spec = importlib.util.spec_from_file_location(
        "scientific_acceptance",
        args.client_root / "scripts/scientific-batch-acceptance.py",
    )
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    args.output.mkdir(parents=True, exist_ok=True)
    state_path = args.output / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if args.source_receipt and not state:
        historical = json.loads(args.source_receipt.read_text())
        state = {key: historical[key] for key in ("source_operation", "input_sha256")}
        state["adopted_internal_source_receipt"] = str(args.source_receipt)
        save(state_path, state)
    identity = "fs2-continuation-20261005-" + args.label
    headers = {"authorization": "Bearer " + key, "origin": args.origin}
    async with httpx2.AsyncClient(
        base_url=args.origin, headers=headers, timeout=120, trust_env=False
    ) as http:
        async with Client(
            streamable_http_client(args.origin + "/mcp", http_client=http),
            mode="2026-07-28",
        ) as mcp:

            async def call(tool, arguments):
                return _mcp_result(await mcp.call_tool(tool, arguments))

            async def get(path):
                response = await http.get(path)
                response.raise_for_status()
                return response.json()

            async def content(pointer, name):
                path = args.output / name
                await helper.download(http, pointer, path)
                return path

            async def poll(operation, label):
                deadline = time.monotonic() + args.timeout
                previous = None
                while time.monotonic() < deadline:
                    value = await get(f"/v1/operations/{operation}")
                    save(args.output / (label + "-status.json"), value)
                    status = value["batch"]["status"]
                    if status != previous:
                        print(
                            json.dumps(
                                {
                                    "label": args.label,
                                    "phase": label,
                                    "operation": operation,
                                    "status": status,
                                }
                            ),
                            flush=True,
                        )
                        previous = status
                    if (
                        status in {"succeeded", "failed", "cancelled"}
                        and value["batch"]["result_published"]
                    ):
                        return value
                    await asyncio.sleep(5)
                raise TimeoutError(
                    "Operation remains recorded; rerun this receipt, do not submit a new study"
                )

            if "source_operation" not in state:
                input_path = args.fixture / "input.tar.gz"
                if args.padding_files or args.prebuilt_tpr:
                    padded = args.output / "input-with-many-files.tar.gz"
                    if not padded.exists():
                        with (
                            tarfile.open(input_path, "r:gz") as original,
                            tarfile.open(padded, "w:gz") as output,
                        ):
                            for member in original:
                                output.addfile(
                                    member,
                                    original.extractfile(member)
                                    if member.isfile()
                                    else None,
                                )
                            if args.prebuilt_tpr:
                                output.add(args.prebuilt_tpr, arcname="benchmark.tpr")
                            for index in range(args.padding_files):
                                content_bytes = (
                                    f"Internal QA continuation file {index}\n".encode()
                                )
                                info = tarfile.TarInfo(
                                    f"continuation-padding/file-{index:05d}.txt"
                                )
                                info.size = len(content_bytes)
                                info.mode = 0o644
                                output.addfile(info, io.BytesIO(content_bytes))
                    input_path = padded
                payload = helper.FileSource(input_path)
                ref = await helper.upload(
                    http,
                    args.model,
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
                            "artifact": ref,
                        }
                    ],
                }
                pointer = await helper.upload(
                    http,
                    args.model,
                    helper.canonical(manifest),
                    "application/vnd.fs2.scientific-manifest+json",
                    "none",
                    identity + "-manifest",
                )
                params = json.loads((args.fixture / "request.json").read_text())
                steps = params["jobs"][0]["steps"]
                converter = next(
                    step for step in steps if step["command"] == "convert-tpr"
                )
                args_list = converter["args"]
                args_list[args_list.index("-nsteps") + 1] = str(args.steps)
                production = next(step for step in steps if step["command"] == "mdrun")
                params["jobs"][0]["steps"] = (
                    [production] if args.prebuilt_tpr else [converter, production]
                )
                params.update(
                    max_wall_seconds=args.source_seconds,
                    segment_minutes=0.2,
                    output_destination="customer-bucket",
                    output_prefix="runs/" + identity,
                )
                body = {
                    "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
                    "operation": "run-workflow",
                    "service_class": "customer-batch",
                    "input_manifest": pointer,
                    "parameters": params,
                    "client_context": {
                        "display_name": "Internal checkpoint continuation "
                        + args.label,
                        "correlation_id": identity,
                    },
                }
                save(args.output / "source-request.json", body)
                response = await http.post(
                    f"/v1/models/{args.model}:submit",
                    json=body,
                    headers={"Idempotency-Key": identity + "-source"},
                )
                response.raise_for_status()
                state.update(
                    source_operation=response.json()["operation"]["id"],
                    input_sha256=payload.sha256,
                )
                save(state_path, state)
            source = state["source_operation"]
            failed = await poll(source, "source")
            if (
                failed["batch"]["status"] != "failed"
                or failed["batch"]["model_id"] != args.model
                or failed["batch"]["failure_code"] != "WORKFLOW_TIME_LIMIT_EXCEEDED"
            ):
                raise ValueError(
                    "Source must fail specifically at the requested execution budget"
                )
            choices = await get(f"/v1/operations/{source}/checkpoints")
            save(args.output / "source-checkpoints.json", choices)
            checkpoint = json.loads(
                (
                    await content(
                        choices["jobs"][0]["checkpoint"], "source-checkpoint.json"
                    )
                ).read_text()
            )
            completed = max(
                (
                    c.get("checkpoint_step") or 0
                    for c in checkpoint["state"]["commands"]
                ),
                default=0,
            )
            if not 0 < completed < args.steps or not checkpoint.get("customer_storage"):
                raise ValueError(
                    "Source has no committed, customer-exported partial native checkpoint"
                )
            state["saved_step"] = completed
            save(state_path, state)
            arguments = {
                "operation_id": source,
                "idempotency_key": identity + "-resume",
                "max_wall_seconds": args.resume_seconds,
            }

            async def resume():
                if args.interface == "mcp":
                    return await call("resume_gromacs_workflow", arguments)
                response = await http.post(
                    f"/v1/operations/{source}:resume",
                    json={"max_wall_seconds": args.resume_seconds},
                    headers={"Idempotency-Key": arguments["idempotency_key"]},
                )
                if response.status_code != 202:
                    save(
                        args.output / "resume-error.json",
                        {"status": response.status_code, "body": response.json()},
                    )
                response.raise_for_status()
                return response.json()

            accepted = await resume()
            resumed = accepted["operation"]["id"]
            if "resume_operation" in state and state["resume_operation"] != resumed:
                raise ValueError("Replay created duplicate work")
            state["resume_operation"] = resumed
            save(state_path, state)
            save(args.output / "resume-response.json", accepted)
            replay = await resume()
            if (
                replay["operation"]["id"] != resumed
                or not replay["operation"]["reused"]
            ):
                raise ValueError(
                    "Idempotent resume replay did not reuse the existing operation"
                )
            final = await poll(resumed, "resumed")
            if final["batch"]["status"] != "succeeded":
                raise ValueError(
                    "Resumed scientific operation failed; inspect retained evidence"
                )
            result = await get(f"/v1/operations/{resumed}/result")
            save(args.output / "result.json", result)
            if result["semantic_validation"]["status"] != "passed":
                raise ValueError("Final artifact validation did not pass")
            final_choices = await call(
                "get_scientific_checkpoints", {"operation_id": resumed}
            )
            final_checkpoint = json.loads(
                (
                    await content(
                        final_choices["jobs"][0]["checkpoint"], "final-checkpoint.json"
                    )
                ).read_text()
            )
            commands = final_checkpoint["state"]["commands"]
            native = [c for c in commands if "mdrun" in c["command"]]
            if (
                not native
                or native[-1]["checkpoint_step"] != args.steps
                or "-cpi" not in native[0]["command"]
            ):
                raise ValueError(
                    "Native execution did not resume and reach the original target"
                )
            files = {f["path"]: f for f in final_checkpoint["files"]}
            log = await content(
                files[native[0]["log"]]["artifact"], "resumed-first-native.log"
            )
            starts = [
                int(n)
                for n in re.findall(
                    r"continuing from step\s+(\d+)", log.read_text(), re.I
                )
            ]
            if starts != [completed]:
                raise ValueError(
                    "Native restart did not start exactly at the saved step"
                )
            preserved = 0
            for old in checkpoint["files"]:
                if (
                    re.search(r"\.part\d+\.(?:xtc|trr|edr|gro|log)$", old["path"])
                    or old["path"].endswith(".tpr")
                    or old["path"].startswith("continuation-padding/")
                ):
                    new = files.get(old["path"], {})
                    if (old["sha256"], old["size_bytes"]) != (
                        new.get("sha256"),
                        new.get("size_bytes"),
                    ):
                        raise ValueError(
                            "Continuation lost or changed an earlier native output/TPR"
                        )
                    preserved += 1
            if not preserved or any(c["step_id"] == "finite-tpr" for c in commands):
                raise ValueError(
                    "Preparation was rerun or prior outputs were not preserved"
                )
            byte_verification = None
            if args.verify_retained_bytes:
                started = time.monotonic()
                verified_bytes = 0
                verified_files = 0
                pending = list(files.values())
                # Eight public reads, bounded to one small cohort at a time;
                # the shared customer-facing API remains the test boundary.
                # The helper validates actual streamed SHA-256 and length.
                semaphore = asyncio.Semaphore(8)

                async def verify_file(item):
                    relative = Path(item["path"])
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError(
                            "Checkpoint path is not a contained relative file"
                        )
                    async with semaphore:
                        await helper.download(
                            http,
                            item["artifact"],
                            args.output / "verified-files" / relative,
                        )
                    return item["size_bytes"]

                for offset in range(0, len(pending), 128):
                    sizes = await asyncio.gather(
                        *(verify_file(item) for item in pending[offset : offset + 128])
                    )
                    verified_bytes += sum(sizes)
                    verified_files += len(sizes)
                    if verified_files % 1024 == 0 or verified_files == len(pending):
                        print(
                            json.dumps(
                                {
                                    "label": args.label,
                                    "phase": "verify-retained-bytes",
                                    "files": verified_files,
                                    "total_files": len(pending),
                                }
                            ),
                            flush=True,
                        )
                byte_verification = {
                    "files": verified_files,
                    "bytes": verified_bytes,
                    "sha256_checked": True,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "parallel_reads": 8,
                }
            receipt = {
                **state,
                "status": "passed",
                "model": args.model,
                "interface": args.interface,
                "finished_step": args.steps,
                "native_resume_step": starts[0],
                "preserved_files": preserved,
                "customer_bucket_export": final_checkpoint["customer_storage"],
                "idempotent_replay": True,
                "budget_seconds": args.resume_seconds,
                "full_budget_soak_tested": False,
                "synthetic_retained_files": args.padding_files,
                "prebuilt_finite_tpr": args.prebuilt_tpr is not None,
                "retained_artifact_verification": byte_verification,
                "customer_key_used": False,
            }
            save(args.output / "receipt.json", receipt)
            print(json.dumps(receipt), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("qa-env", "client-root", "fixture", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--model", choices=("gromacs", "gromacs-mpi"), default="gromacs"
    )
    parser.add_argument("--interface", choices=("rest", "mcp"), required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--steps", type=int, default=60000)
    parser.add_argument("--source-seconds", type=int, default=60)
    parser.add_argument("--padding-files", type=int, default=0)
    parser.add_argument("--prebuilt-tpr", type=Path)
    parser.add_argument(
        "--source-receipt",
        type=Path,
        help="Reuse a prior internal-QA failed source without resubmitting it",
    )
    parser.add_argument("--verify-retained-bytes", action="store_true")
    parser.add_argument("--resume-seconds", type=int, default=1209600)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--origin", default="https://89.169.99.188")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{1,48}", args.label):
        parser.error("label must be a bounded task-owned identifier")
    if not 0 <= args.padding_files <= 30000:
        parser.error("padding-files must be between 0 and 30000")
    if not 60 <= args.resume_seconds <= 1209600:
        parser.error("resume-seconds must be between 60 and 1209600")
    if not 60 <= args.source_seconds <= 600:
        parser.error(
            "source-seconds must be between 60 and 600 for bounded internal QA"
        )
    os.umask(0o077)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
