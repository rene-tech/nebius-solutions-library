"""Verify MPINAT timing completeness from native artifacts, never the envelope.

This is acceptance/report verification, not a new serving or billing service.
An integration can succeed while the requested benchmark report is incomplete.
"""
import hashlib
import json
import math
from pathlib import Path


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def verify_native_timings(request, receipt):
    operation = receipt["operation_id"]
    jobs = {job["id"]: job for job in request["parameters"]["jobs"]}
    results, sources, rows, gaps = {}, [], [], []
    for reference in receipt.get("verified_artifacts", []):
        if reference.get("semantic_type") != "gromacs-workflow-result/v1":
            continue
        path = Path(reference["path"])
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != reference["sha256"] or path.stat().st_size != reference["size_bytes"]:
            raise ValueError("Native timing artifact differs from its verified receipt")
        value = json.loads(path.read_text())
        job_id = value.get("job_id")
        if (value.get("schema") != "fs2-serve.nebius.ai/gromacs-workflow-result/v1"
                or value.get("operation_id") != operation or job_id not in jobs
                or value.get("status") != "succeeded" or job_id in results
                or value.get("inventory_complete", True) is not True):
            raise ValueError("Native timing document does not prove this completed job")
        results[job_id] = value
        sources.append({"job_id": job_id, "path": str(path), "sha256": actual})
    for job_id, job in jobs.items():
        result = results.get(job_id)
        expected = [step["id"] for step in job["steps"]]
        if result is None:
            gaps.append(f"{job_id}: verified native result artifact absent")
            continue
        if result.get("completed_steps") != expected:
            raise ValueError("Native completed steps differ from the requested workflow")
        for step in job["steps"]:
            if step["command"] != "mdrun":
                continue
            commands = [row for row in result.get("commands", []) if row.get("step_id") == step["id"]]
            if not commands or any("mdrun" not in row.get("command", []) or row.get("exit_code") != 0
                                   for row in commands):
                gaps.append(f"{job_id}/{step['id']}: completed native mdrun records absent")
            rates = [row.get("performance_ns_per_day") for row in commands]
            walls = [row.get("wall_seconds") for row in commands]
            if not rates or not all(positive(value) for value in rates):
                gaps.append(f"{job_id}/{step['id']}: native timing rate absent or invalid")
            if not walls or not all(positive(value) for value in walls):
                gaps.append(f"{job_id}/{step['id']}: native command duration absent or invalid")
            rows.append({"job_id": job_id, "step_id": step["id"],
                         "segments": [{key: command.get(key) for key in (
                             "segment", "exit_code", "wall_seconds", "performance_ns_per_day",
                             "checkpoint_step", "finished_at", "log")} for command in commands],
                         "command_wall_seconds": sum(walls) if walls and all(positive(v) for v in walls) else None})
    if not rows:
        gaps.append("No requested simulation timing rows were produced")
    return {"schema": "fs2.gromacs-mpinat-native-timings/v1", "operation_id": operation,
            "benchmark_complete": not gaps, "gaps": gaps, "sources": sources, "repeats": rows,
            "timing_policy": "Includes initialization/tuning; no forced timer reset; segments are not independent repeats",
            "scientific_convergence_claimed": False}
