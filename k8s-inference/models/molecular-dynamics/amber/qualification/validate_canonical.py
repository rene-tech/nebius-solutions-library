"""Additional immutable-master, native pressure and velocity trajectory gates."""

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path

import numpy as np
from netCDF4 import Dataset
from fs2_amber.validation import validate_pmemd
from validate_case import validate, samples


def validate_canonical(root, master):
    science = validate(root)
    data = root / "data"
    protocol = json.loads((data / "protocol.json").read_text())
    if protocol["fixture"] != "canonical-alanine-tip3p":
        raise ValueError("this is not the canonical ff14SB/TIP3P fixture")
    input_hashes = {}
    for name in ("system.prmtop", "system.rst7", "system.pdb", "master-manifest.json"):
        raw = (data / name).read_bytes()
        if raw != (master / name).read_bytes():
            raise ValueError("immutable canonical master changed: " + name)
        input_hashes[name] = hashlib.sha256(raw).hexdigest()
    job = json.loads((root / "request.json").read_text())["jobs"][0]
    velocity_trajectories = []
    pressures = []
    for step in job["steps"]:
        if step["id"] not in {"nvt", "npt", "production-001"}:
            continue
        step = {"task": "dynamics", **step}
        native = validate_pmemd(data, step)
        if native["completion"]["dt_ps"] != 0.002:
            raise ValueError("canonical dynamics did not use2fs")
        mdout = (data / (step["output_prefix"] + ".mdout")).read_text()
        if "reported pressure is always 0" in mdout:
            raise ValueError("Monte Carlo placeholder pressure cannot satisfy canonical pressure observable")
        rows, _, _ = samples(data / (step["output_prefix"] + ".mdout"))
        if step["id"] == "production-001":
            pressures = [row["PRESS"] for row in rows if row["step"] > 0]
        with Dataset(data / (step["output_prefix"] + ".mdvel")) as velocities, Dataset(data / (step["output_prefix"] + ".nc")) as coordinates:
            expected = step["expected_nsteps"] // 500
            values = velocities.variables["velocities"][:]
            if values.shape != (expected, 6598, 3) or np.ma.getmaskarray(values).any() or not np.isfinite(values).all():
                raise ValueError("velocity trajectory lacks exact finite1ps frame coverage")
            if not np.array_equal(velocities.variables["time"][:], coordinates.variables["time"][:]):
                raise ValueError("coordinate and velocity timestamps do not match")
            velocity_trajectories.append({"stage": step["id"], "frames": expected, "units": getattr(velocities.variables["velocities"], "units", None), "scale_factor": getattr(velocities.variables["velocities"], "scale_factor", None)})
    if len(pressures) != 1000 or not all(math.isfinite(value) for value in pressures) or max(pressures) - min(pressures) < 0.001:
        raise ValueError("production does not contain1000 genuine varying finite pressure samples")
    return {**science, "canonical_master_parity": True, "master_sha256": input_hashes, "native_barostat": protocol["barostat_method"], "native_pressure_estimator": "molecular virial", "production_pressure_samples": len(pressures), "production_pressure_mean_bar": statistics.mean(pressures), "production_pressure_sd_bar": statistics.stdev(pressures), "production_pressure_min_bar": min(pressures), "production_pressure_max_bar": max(pressures), "velocity_trajectories": velocity_trajectories, "pressure_mean_target_closeness_not_used_as_short_run_convergence_claim": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("retain every prior validation")
    try:
        result = validate_canonical(args.workspace, args.master)
    except Exception as exc:
        result = {"status": "failed", "error": str(exc)}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
