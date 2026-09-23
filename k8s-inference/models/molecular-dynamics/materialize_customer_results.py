"""Verify downloaded customer artifacts and recreate native validator workspaces.

This is an offline qualification helper, not a second storage transport. Its
input is the receipt directory written by the released scientific-batch client.
Engine-specific trajectory and scientific checks must still run afterwards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil

MODELS = {"lammps", "namd"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def materialize(model: str, receipt: Path, parameters: Path, output: Path) -> dict:
    if model not in MODELS:
        raise ValueError("unsupported native engine")
    status = json.loads((receipt / "status.json").read_text())
    client = json.loads((receipt / "receipt.json").read_text())
    if (status["operation"]["status"], status["batch"]["result_published"], client["state"]) != (
        "succeeded", True, "verified"
    ):
        raise ValueError("customer operation did not finish with verified downloads")
    manifest = json.loads((receipt / "output-manifest.json").read_text())
    request = json.loads(parameters.read_text())
    jobs = {job["id"]: job for job in request["jobs"]}
    files, results = {}, {}
    for index, entry in enumerate(manifest["entries"]):
        source = receipt / f"output-{index:02d}.artifact"
        artifact = entry["artifact"]
        if source.stat().st_size != artifact["size_bytes"] or digest(source) != artifact["sha256"]:
            raise ValueError("downloaded artifact size or checksum differs from its manifest")
        files[artifact["sha256"]] = source
        if entry["semantic_type"] == f"{model}-workflow-result/v1":
            result = json.loads(source.read_text())
            job = result["job_id"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", job) or job in results or job not in jobs:
                raise ValueError("unexpected or duplicate native result job")
            if (result["operation_id"], result["status"], result["completed_steps"]) != (
                status["operation"]["id"], "succeeded", [step["id"] for step in jobs[job]["steps"]]
            ):
                raise ValueError("native result did not complete this customer's exact job")
            results[job] = (source, result)
    if set(results) != set(jobs):
        raise ValueError("customer result is missing one or more requested jobs")
    # Check the full inventory before writing a derived workspace. Never
    # overwrite the original customer receipt or use native paths as roots.
    for _, result in results.values():
        paths = set()
        for item in result["files"]:
            path = PurePosixPath(item["path"])
            if path.is_absolute() or ".." in path.parts or not path.parts or item["path"] in paths:
                raise ValueError("native inventory path is invalid or duplicated")
            paths.add(item["path"])
            source = files.get(item["sha256"])
            if source is None or source.stat().st_size != item["size_bytes"]:
                raise ValueError("native inventory references a missing downloaded artifact")
    output.mkdir(parents=True, exist_ok=False)
    for job, (source, result) in results.items():
        directory = output / job
        directory.mkdir()
        shutil.copyfile(source, directory / "result.json")
        for item in result["files"]:
            target = directory / "data" / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(files[item["sha256"]], target)
    record = {
        "model_id": model,
        "operation_id": status["operation"]["id"],
        "status": "download-integrity-verified",
        "scientific_validation_complete": False,
        "jobs": sorted(results),
        "manifest_sha256": digest(receipt / "output-manifest.json"),
        "request_sha256": digest(parameters),
        "source_receipt": str(receipt.resolve()),
    }
    (output / "materialization.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=sorted(MODELS), required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--parameters", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(materialize(args.model, args.receipt, args.parameters, args.output)))


if __name__ == "__main__":
    main()
