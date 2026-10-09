"""Apply the same science gates to downloaded jobs and verify immutable input parity."""

import argparse
import hashlib
import json
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

MD = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(MD / "lammps/runtime"), str(MD / "gromacs/runtime")]
from fs2_gromacs.files import digest_file, inventory
from fs2_lammps import ENGINE_ID, RESULT_SCHEMA
from fs2_lammps.contracts import canonical, normalize
from validate_case import validate


def input_inventory(path):
    files = {}
    with tarfile.open(path, "r|gz") as source:
        for member in source:
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError("fixture archive includes a non-regular member")
            digest = hashlib.sha256()
            with source.extractfile(member) as handle:
                for block in iter(lambda: handle.read(4 * 1024**2), b""):
                    digest.update(block)
            files[member.name] = {"size_bytes": member.size, "sha256": digest.hexdigest()}
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspaces", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("preserve existing evidence; choose a new receipt path")
    request = normalize(json.loads(args.request.read_text()))
    source_files = input_inventory(args.input)
    tests = []
    for job in request["jobs"]:
        root = args.workspaces / job["id"]
        result = json.loads((root / "result.json").read_text())
        recipe = hashlib.sha256(canonical({"request": request, "job": job["id"], "image": ENGINE_ID})).hexdigest()
        validation = {"status": "failed"}
        try:
            if (result["schema"], result["operation_id"], result["job_id"], result["status"], result["engine_id"], result["recipe_sha256"], result["completed_steps"]) != (RESULT_SCHEMA, args.operation_id, job["id"], "succeeded", ENGINE_ID, recipe, [step["id"] for step in job["steps"]]):
                raise ValueError("downloaded result differs from the frozen native recipe")
            files = inventory(root / "data", max_bytes=request["max_output_bytes"])
            if result["files"] != files:
                raise ValueError("downloaded artifacts differ from native result hashes")
            actual = {item["path"]: {key: item[key] for key in ("size_bytes", "sha256")} for item in files}
            if any(actual.get(name) != value for name, value in source_files.items()):
                raise ValueError("downloaded original inputs differ from the immutable submitted bundle")
            validation = {**validate(root, job_directory=job["id"]), "immutable_input_parity": True, "verified_input_files": len(source_files), "result_inventory_parity": True, "recipe_parity": True}
        except Exception as exc:
            validation = {"status": "failed", "error": str(exc)}
        raw = json.dumps(validation, sort_keys=True, indent=2).encode() + b"\n"
        digest = hashlib.sha256(raw).hexdigest()
        path = root / ("hosted-scientific-validation-" + digest + ".json")
        if not path.exists():
            path.write_bytes(raw)
        tests.append({"case": job["id"], "status": validation["status"], "result_sha256": digest_file(root / "result.json"), "input_sha256": digest_file(args.input), "validation_sha256": digest, "validation_path": str(path), "raw_evidence": str(root), "validation": validation})
        print(json.dumps({"case": job["id"], "status": validation["status"], "validation_sha256": digest}), flush=True)
    receipt = {"model_id": "lammps", "operation_id": args.operation_id, "runtime_image": args.runtime_image, "runtime_identity_source": "parent operation/Pod evidence; separately verify exact deployed image", "engine_id": ENGINE_ID, "recorded_at": datetime.now(timezone.utc).isoformat(), "status": "passed" if all(test["status"] == "passed" for test in tests) else "failed", "request_sha256": digest_file(args.request), "input_sha256": digest_file(args.input), "tests": tests, "customer_ready": False, "scientific_convergence_claimed": False, "gpu_snapshot_qualified": False}
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    raise SystemExit(0 if receipt["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
