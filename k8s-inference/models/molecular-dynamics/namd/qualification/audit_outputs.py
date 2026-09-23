"""Audit local or downloaded native artifacts against the exact submitted request.

This is qualification tooling, not a new runtime policy. Pass the actual
normalized workflow parameters (including the submitted output prefix), not the
surrounding hosted HTTP submission envelope. Scientific trajectory/bias checks
remain in validate_campaign.py.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile

from fs2_gromacs.contracts import relative_path
from fs2_gromacs.files import digest_file
from fs2_namd import ENGINE_ID, RESULT_SCHEMA
from fs2_namd.contracts import canonical, normalize
from fs2_namd.worker import binary_vectors, colvars_step, xsc_step


def audit_input_bundle(bundle, data, listed):
    """Compare immutable input members without extracting or executing the bundle."""
    seen, total = set(), 0
    with tarfile.open(bundle, "r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            name = relative_path(member.name)
            if not member.isfile() or name in seen or name not in listed:
                raise ValueError("input bundle contains a non-regular, duplicate or unrecorded member")
            seen.add(name)
            path = data / name
            with archive.extractfile(member) as stream:
                source_sha = hashlib.file_digest(stream, "sha256").hexdigest()
            if member.size != path.stat().st_size or source_sha != digest_file(path):
                raise ValueError("immutable input differs from the qualification bundle: " + name)
            total += member.size
    if not seen:
        raise ValueError("qualification input bundle is empty")
    return {"sha256": digest_file(bundle), "verified_files": len(seen), "verified_bytes": total}


def audit(request, result, data, input_bundle=None):
    request = normalize(request)
    if result["schema"] != RESULT_SCHEMA or result["status"] != "succeeded" or result["engine_id"] != ENGINE_ID:
        raise ValueError("native result is not successful for the pinned engine/schema")
    jobs = [job for job in request["jobs"] if job["id"] == result["job_id"]]
    if len(jobs) != 1:
        raise ValueError("result job is absent from the submitted request")
    expected = hashlib.sha256(canonical({"request": request, "job": result["job_id"], "image": ENGINE_ID})).hexdigest()
    if result["recipe_sha256"] != expected:
        raise ValueError("result recipe differs from the actual submitted request")
    steps = jobs[0]["steps"]
    if result["completed_steps"] != [step["id"] for step in steps]:
        raise ValueError("result does not complete the exact requested ordered stages")
    listed, total = set(), 0
    for entry in result["files"]:
        name = relative_path(entry["path"])
        path = data / name
        if name in listed or not path.resolve().is_relative_to(data.resolve()) or path.is_symlink() or not path.is_file():
            raise ValueError("artifact inventory has a duplicate, missing or non-regular file")
        listed.add(name)
        if path.stat().st_size != entry["size_bytes"] or digest_file(path) != entry["sha256"]:
            raise ValueError("artifact bytes differ from the native result inventory: " + name)
        total += entry["size_bytes"]
    if not listed:
        raise ValueError("native result inventory is empty")
    dynamics = []
    if any(command["step_id"] not in result["completed_steps"] for command in result["commands"]):
        raise ValueError("native result contains an unrequested stage")
    for step in steps:
        commands = [command for command in result["commands"] if command["step_id"] == step["id"]]
        if not commands or any(command["exit_code"] != 0 for command in commands):
            raise ValueError("requested stage lacks successful native execution")
        directory = data / step["directory"]
        for output in step["expected_outputs"]:
            if str((directory / output).relative_to(data)) not in listed:
                raise ValueError("requested native output is absent from the inventory")
        if step["mode"] != "dynamics":
            if len(commands) != 1:
                raise ValueError("completion-only native/preparation stage was unexpectedly segmented")
            continue
        current = step["first_step"]
        end = current + step["steps"]
        for number, command in enumerate(commands, start=1):
            next_step = min(current + step["segment_steps"], end)
            if current >= end or command["segment"] != number or command["configured_first_step"] != current or command["checkpoint_step"] != next_step or command["gpu_mode"] != step["gpu_mode"]:
                raise ValueError("native segment boundaries/GPU mode differ from the request")
            current = next_step
        if current != end:
            raise ValueError("native execution did not reach the requested final timestep")
        prefix = directory / step["output_prefix"]
        def output(suffix):
            path = Path(str(prefix) + suffix)
            if str(path.relative_to(data)) not in listed:
                raise ValueError("managed final checkpoint is absent from the inventory")
            return path
        atoms = binary_vectors(output(".coor"))
        if binary_vectors(output(".vel")) != atoms or xsc_step(output(".xsc")) != end:
            raise ValueError("managed final coordinates, velocities and cell disagree")
        bias = Path(str(prefix) + ".colvars.state")
        if bias.exists() and (str(bias.relative_to(data)) not in listed or colvars_step(bias) != end):
            raise ValueError("managed final bias state disagrees with native checkpoint")
        if any(command["atoms"] != atoms for command in commands):
            raise ValueError("native log and checkpoint atom counts disagree")
        dynamics.append({"step": step["id"], "segments": len(commands), "atoms": atoms,
                         "first_step": step["first_step"], "final_step": end, "gpu_mode": step["gpu_mode"]})
    report = {"status": "passed", "job_id": result["job_id"], "recipe_sha256": expected,
              "verified_files": len(listed), "verified_bytes": total, "dynamics": dynamics,
              "scientific_convergence_claimed": False}
    if input_bundle is not None:
        report["immutable_input_bundle"] = audit_input_bundle(input_bundle, data, listed)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input-bundle", type=Path, help="Independently compare every immutable input member with the uploaded fixture")
    args = parser.parse_args()
    report = audit(json.loads(args.request.read_text()), json.loads(args.result.read_text()), args.data, args.input_bundle)
    report.update(request_sha256=digest_file(args.request), result_sha256=digest_file(args.result))
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
