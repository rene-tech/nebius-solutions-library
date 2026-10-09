"""Reuse the retained task-owned native Pod lifecycle for exact Lynx recipes.

Never a public-path or customer-readiness claim. GPU count, Pod UID, immutable
runtime, inventory and cleanup are checked by the existing native supervisor.
"""

import argparse
import json
import math
from pathlib import Path
import re
import statistics
import sys

from recipes import ATOMS, DT_PS, TPR_SHA256, sha

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gromacs-mpinat-20261003"))
import qualify_candidate as native


def validate(workspace, request, gpu_count, mpi, expected_failure, worker_exit, *, data_dir=None, result_path=None, **_):
    if expected_failure:
        raise ValueError("No expected native failures are silently accepted")
    data_dir = data_dir or workspace / "data"
    result_path = result_path or workspace / "result.json"
    result = json.loads(result_path.read_text())
    errors = []
    for item in result["files"]:
        path = data_dir / item["path"]
        if (not path.resolve().is_relative_to(data_dir.resolve())
                or path.stat().st_size != item["size_bytes"] or sha(path) != item["sha256"]):
            errors.append("inventory mismatch: " + item["path"])
    if sha(data_dir / "original.tpr") != TPR_SHA256:
        errors.append("original TPR changed")
    converter = request["jobs"][0]["steps"][0]["args"]
    steps = int(converter[converter.index("-nsteps") + 1])
    expected = [step["id"] for step in request["jobs"][0]["steps"]]
    if (worker_exit != 0 or result["status"] != "succeeded"
            or result["completed_steps"] != expected or not result["inventory_complete"]):
        errors.append("worker or native workflow did not fully succeed")
    repetitions = sum(step["command"] == "mdrun" for step in request["jobs"][0]["steps"])
    summaries = []
    for repeat in range(1, repetitions + 1):
        commands = [c for c in result["commands"] if c.get("step_id") == f"repeat-{repeat}"]
        previous = 0
        counter_walls = []
        for index, command in enumerate(commands):
            current = command.get("checkpoint_step")
            log = (data_dir / command["log"]).read_text(errors="replace")
            restarts = [int(s) for s in re.findall(r"continuing from step\s+(\d+)", log, re.I)]
            if (command.get("exit_code") != 0 or current is None or not previous < current <= steps
                    or (index and restarts != [previous]) or (not index and restarts)):
                errors.append(f"repeat {repeat}: invalid native step continuity")
            if mpi and (not command.get("rank_binding_evidence_complete")
                        or len(command.get("rank_bindings", [])) != gpu_count):
                errors.append(f"repeat {repeat}: MPI GPU bindings incomplete")
            if "Fatal error" in log or "LINCS WARNING" in log:
                errors.append(f"repeat {repeat}: native correctness warning")
            match = re.search(r"Time:\s+[\d.]+\s+([\d.]+)", log)
            if match:
                counter_walls.append(float(match[1]))
            previous = current if current is not None else previous
        if previous != steps:
            errors.append(f"repeat {repeat}: final native checkpoint is not the target")
        energy = data_dir / f"energy{repeat}.xvg"
        rows = [[float(x) for x in line.split()] for line in energy.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith(("#", "@"))] if energy.exists() else []
        if (not rows or not all(len(row) >= 6 and all(math.isfinite(x) for x in row) for row in rows)
                or not math.isclose(rows[-1][0] - rows[0][0], steps * DT_PS, abs_tol=1e-4)
                or any(row[4] <= 0 for row in rows)):
            errors.append(f"repeat {repeat}: finite energy/time validation failed")
        try:
            coordinates = native.validate_gro(data_dir / f"repeat{repeat}.gro", ATOMS)
        except (ValueError, OSError) as exc:
            errors.append(f"repeat {repeat}: {exc}")
            coordinates = None
        walls = sum(counter_walls) if len(counter_walls) == len(commands) and commands else None
        summaries.append({"repeat": repeat, "segments": len(commands), "steps": previous,
                          "simulated_ns": steps * DT_PS / 1000, "coordinates": coordinates,
                          "native_mdrun_counter_wall_seconds": walls,
                          "native_inclusive_ns_per_day": steps * DT_PS / 1000 * 86400 / walls if walls else None,
                          "native_reported_ns_per_day": [c.get("performance_ns_per_day") for c in commands],
                          "energy_samples": len(rows), "energy_first": rows[0] if rows else None,
                          "energy_last": rows[-1] if rows else None})
    rates = [r["native_inclusive_ns_per_day"] for r in summaries if r["native_inclusive_ns_per_day"]]
    return {"status": "failed" if errors else "passed", "errors": errors,
            "inventory_rehashed_files": len(result["files"]), "input_tpr_sha256": TPR_SHA256,
            "repeats": summaries, "median_native_inclusive_ns_per_day": statistics.median(rates) if rates else None,
            "native_min_ns_per_day": min(rates) if rates else None,
            "native_max_ns_per_day": max(rates) if rates else None,
            "source_sha256": sha(__file__), "native_result_sha256": sha(result_path),
            "customer_path_tested": False, "checkpoint_scope": "local-only",
            "scope": "Exact-input finite performance/finite-energy/coordinate/step-continuity check, not ensemble convergence."}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "name", "image", "source-revision"):
        p.add_argument("--" + name, required=True)
    for name in ("input", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--gpus", type=int, choices=(1, 2, 4, 8), default=1)
    p.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    args = p.parse_args()
    args.expect_failure = args.native_continuation = args.device_probe_only = False
    native.KUBE = ["kubectl", "--context", args.context]
    native.TASK = "lynx-performance-20261005"
    native.validate = validate
    original_call = native.call

    def checked_call(argv, **kwargs):
        if "create" in argv and "-f" in argv:
            original_call([*argv, "--dry-run=client"], **kwargs)
        return original_call(argv, **kwargs)

    native.call = checked_call
    return native.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
