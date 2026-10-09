"""Read-only identity and immutable-input audit of materialized hosted MD output.

This complements, and never substitutes for, the engine's scientific validator.
Only allowlisted metadata is emitted; credentials and caller identity are not.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import tarfile

from fs2_gromacs.contracts import canonical, relative_path
from fs2_gromacs.files import digest_file, inventory


def audit(model: str, fixture: Path, workspace: Path, receipt: Path) -> dict:
    if model not in {"amber", "namd"}:
        raise ValueError("this bounded cohort audit supports AMBER and NAMD only")
    engine = importlib.import_module(f"fs2_{model}")
    contracts = importlib.import_module(f"fs2_{model}.contracts")
    request_path = fixture / "request.json"
    if request_path.read_bytes() != (workspace / "request.json").read_bytes():
        raise ValueError("materialized request differs from the exact frozen fixture")
    request = contracts.normalize(json.loads(request_path.read_text()))
    result_path = workspace / "result.json"
    result = json.loads(result_path.read_text())
    client = json.loads((receipt / "receipt.json").read_text())
    status = json.loads((receipt / "status.json").read_text())
    operation = status["operation"]
    if client["state"] != "verified" or operation["status"] != "succeeded" or not status["batch"]["result_published"]:
        raise ValueError("hosted operation lacks completed verified customer downloads")
    if client["operation_id"] != operation["id"] or result["operation_id"] != operation["id"]:
        raise ValueError("native, customer and operation identities disagree")
    attempts = [a for stage in status["batch"]["stages"] for a in stage["attempts"]]
    if not attempts or not all(a["resource_released"] for a in attempts):
        raise ValueError("hosted operation still owns compute resources")
    if (result["schema"], result["status"], result["engine_id"]) != (engine.RESULT_SCHEMA, "succeeded", engine.ENGINE_ID):
        raise ValueError("result is not successful for the exact native engine")
    jobs = [job for job in request["jobs"] if job["id"] == result["job_id"]]
    if len(jobs) != 1:
        raise ValueError("result job is absent or ambiguous in the exact request")
    completed = [step["id"] for step in jobs[0]["steps"]]
    if result["completed_steps"] != completed:
        raise ValueError("result did not complete every requested ordered stage")
    recipe = hashlib.sha256(canonical({"request": request, "job": result["job_id"], "image": engine.ENGINE_ID})).hexdigest()
    if result["recipe_sha256"] != recipe:
        raise ValueError("native recipe differs from the frozen normalized request")
    commands = result["commands"]
    if {c["step_id"] for c in commands} != set(completed) or any(c["exit_code"] != 0 for c in commands):
        raise ValueError("native commands do not cover successful requested stages")
    data = workspace / "data"
    if inventory(data, max_bytes=request["max_output_bytes"]) != result["files"]:
        raise ValueError("materialized file inventory differs from the native result")
    bundle = fixture / "input.tar.gz"
    bundle_sha = digest_file(bundle)
    if client["identity"]["source_sha256"] != bundle_sha:
        raise ValueError("customer uploaded a different immutable input archive")
    listed = {item["path"]: item for item in result["files"]}
    inputs = []
    with tarfile.open(bundle, "r:gz") as archive:
        seen = set()
        for member in archive:
            if member.isdir():
                continue
            name = relative_path(member.name)
            if not member.isfile() or name in seen or name not in listed:
                raise ValueError("input contains a non-regular, duplicate or unrecorded member")
            seen.add(name)
            with archive.extractfile(member) as stream:
                source_sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if (member.size, source_sha) != (listed[name]["size_bytes"], listed[name]["sha256"]):
                raise ValueError("immutable native input differs from its uploaded bytes: " + name)
            inputs.append({"path": name, "size_bytes": member.size, "sha256": source_sha})
    if not inputs:
        raise ValueError("input bundle is empty")
    return {
        "status": "passed", "model_id": model, "operation_id": operation["id"],
        "job_id": result["job_id"], "engine_id": engine.ENGINE_ID,
        "input_sha256": bundle_sha, "request_sha256": digest_file(request_path),
        "result_sha256": digest_file(result_path), "recipe_sha256": recipe,
        "completed_steps": completed, "native_commands": len(commands),
        "output_files": len(listed), "output_bytes": sum(f["size_bytes"] for f in listed.values()),
        "immutable_input_files": inputs, "resources_released": True,
        "customer_manifest_sha256": digest_file(receipt / "output-manifest.json"),
        "customer_receipt_sha256": digest_file(receipt / "receipt.json"),
        "customer_status_sha256": digest_file(receipt / "status.json"),
        "scientific_validation_performed_by_this_audit": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=["amber", "namd"], required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("preserve previous evidence; choose a new output path")
    record = audit(args.model, args.fixture, args.workspace, args.receipt)
    args.output.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({k: v for k, v in record.items() if k != "immutable_input_files"}))


if __name__ == "__main__":
    main()
