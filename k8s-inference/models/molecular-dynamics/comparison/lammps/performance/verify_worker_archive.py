"""Re-use the released worker contract to audit a copied canonical probe workspace."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def audit(workspace, md_root):
    sys.path[:0] = [str(md_root / "lammps/runtime"), str(md_root / "gromacs/runtime"),
                   str(md_root / "lammps/qualification")]
    from fs2_gromacs.files import digest_file, inventory
    from fs2_lammps import ENGINE_ID, RESULT_SCHEMA
    from fs2_lammps.contracts import canonical, normalize
    from validate_hosted import input_inventory

    request = normalize(json.loads((workspace / "request.json").read_text()))
    if len(request["jobs"]) != 1:
        raise ValueError("this receipt covers one canonical native probe")
    job = request["jobs"][0]
    result = json.loads((workspace / "result.json").read_text())
    expected = hashlib.sha256(canonical({"request": request, "job": job["id"], "image": ENGINE_ID})).hexdigest()
    if (result["schema"], result["status"], result["job_id"], result["recipe_sha256"], result["engine_id"], result["completed_steps"]) != (
        RESULT_SCHEMA, "succeeded", job["id"], expected, ENGINE_ID, [step["id"] for step in job["steps"]]
    ):
        raise ValueError("archived result does not match the exact native request")
    files = inventory(workspace / "data", max_bytes=request["max_output_bytes"])
    if files != result["files"]:
        raise ValueError("archived output inventory mismatch")
    original = input_inventory(workspace / "input.tar.gz")
    actual = {row["path"]: {key: row[key] for key in ("size_bytes", "sha256")} for row in files}
    if any(actual.get(name) != value for name, value in original.items()):
        raise ValueError("immutable input member changed")
    return {"status": "passed", "scope": "native worker request/output/input parity, not hosted acceptance",
            "operation_id": result["operation_id"], "job_id": job["id"], "recipe_sha256": expected,
            "verified_output_files": len(files), "verified_output_bytes": sum(row["size_bytes"] for row in files),
            "verified_immutable_input_files": len(original),
            "hashes": {name: digest_file(workspace / name) for name in ("request.json", "input.tar.gz", "result.json", "scientific-validation.json")}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--md-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.workspace, args.md_root)
    with args.output.open("x") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
