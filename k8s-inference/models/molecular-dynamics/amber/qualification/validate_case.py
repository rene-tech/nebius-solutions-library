"""Same native scientific gates for local or downloaded AMBER workflow artifacts."""

import argparse
import csv
import json
import math
import re
import statistics
from pathlib import Path

import numpy as np
from netCDF4 import Dataset
from fs2_gromacs.files import inventory
from fs2_amber.validation import NUMBER, restart_metadata, validate_pmemd
from ti_validation import validate_ti


def samples(path):
    rows, current, elapsed, ns_day = [], None, None, None
    summaries, region = False, 1
    with path.open() as handle:
        for line in handle:
            summaries |= "A V E R A G E S" in line or "R M S  F L U C T U A T I O N S" in line
            if match := re.search(rf"Elapsed\(s\)\s*=\s*({NUMBER})", line):
                elapsed = float(match.group(1))
            if match := re.search(rf"ns/day\s*=\s*({NUMBER})", line):
                ns_day = float(match.group(1))
            if summaries:
                continue
            if match := re.search(r"\| TI region\s+(\d+)", line):
                region = int(match.group(1))
            if match := re.search(rf"NSTEP\s*=\s*(\d+)\s+TIME\(PS\)\s*=\s*({NUMBER})\s+TEMP\(K\)\s*=\s*({NUMBER})", line):
                current = {"step": int(match.group(1)), "time_ps": float(match.group(2)), "temperature_k": float(match.group(3)), "ti_region": region}
                rows.append(current)
            if current is not None:
                for key in ("Etot", "EKtot", "EPtot", "VOLUME", "DV/DL"):
                    if match := re.search(rf"\b{key}\s*=\s*({NUMBER})", line):
                        current[key] = float(match.group(1))
    return rows, elapsed, ns_day


def validate(root):
    result = json.loads((root / "result.json").read_text())
    request = json.loads((root / "request.json").read_text())
    protocol = json.loads((root / "data/protocol.json").read_text())
    job = next(job for job in request["jobs"] if job["id"] == result["job_id"])
    if result["status"] != "succeeded" or result["completed_steps"] != [step["id"] for step in job["steps"]]:
        raise ValueError("native workflow did not complete every frozen stage")
    if inventory(root / "data", max_bytes=request["max_output_bytes"]) != result["files"]:
        raise ValueError("native output inventory hash mismatch")
    production = [step for step in job["steps"] if step["id"].startswith("production-")]
    if sum(step["expected_nsteps"] for step in production) != protocol["production_steps"]:
        raise ValueError("native stage lengths differ from declared production protocol")
    all_rows, trajectories, boundaries, alchemical = [], [], [], []
    native_seconds = 0
    previous = None
    for step in production:
        step = {"task": "dynamics", **step}
        path = root / "data" / step["directory"] if "directory" in step else root / "data"
        checked = validate_pmemd(path, step)
        atoms = checked["restart"]["atoms"]
        if protocol.get("expected_atoms", atoms) != atoms:
            raise ValueError("prepared system atom count differs from the stated fixture")
        rows, seconds, ns_day = samples(path / (step["output_prefix"] + ".mdout"))
        if protocol["ensemble"] == "NVT-TI":
            alchemical.append({"step": step["id"], **validate_ti((path / step["input"]).read_text(), (path / (step["output_prefix"] + ".mdout")).read_text(), step["expected_nsteps"])})
            # Keep one consistent TI region at restart boundaries; comparing
            # region2 of the prior stage against region1 is not an energy jump.
            rows = [row for row in rows if row["ti_region"] == 1]
        if not rows or seconds is None or seconds <= 0:
            raise ValueError("no independently readable production thermodynamics/timing")
        start = restart_metadata(path / step["coordinates"], require_velocities=True)
        expected_end = start["time_ps"] + step["expected_nsteps"] * protocol["timestep_ps"]
        if abs(expected_end - checked["restart"]["time_ps"]) > 0.0011 or abs(checked["completion"]["dt_ps"] - protocol["timestep_ps"]) > 1e-12:
            raise ValueError("native restart time/protocol timestep disagrees with full production duration")
        if previous is not None:
            first = rows[0]
            if abs(start["time_ps"] - previous["time_ps"]) > 0.0011:
                raise ValueError("native stage start restart time is discontinuous")
            boundary = {"time_ps": start["time_ps"], "first_sample_step": first["step"], "same_step_thermodynamics_measured": first["step"] == 0, "exact_stochastic_stream_claimed": False}
            if first["step"] == 0:
                jump = abs(first["Etot"] - previous["Etot"]) / max(abs(previous["Etot"]), 1)
                if jump > 1e-4:
                    raise ValueError("native closed-stage energy discontinuity exceeds1e-4")
                boundary.update(relative_energy_jump=jump, temperature_jump_k=abs(first["temperature_k"] - previous["temperature_k"]))
            boundaries.append(boundary)
        previous = rows[-1]
        all_rows.extend(rows)
        native_seconds += seconds
        trajectory = path / (step["output_prefix"] + ".nc")
        with Dataset(trajectory) as data:
            coordinates = data.variables["coordinates"]
            frames = step["expected_nsteps"] // protocol["trajectory_interval"]
            if coordinates.shape != (frames, atoms, 3):
                raise ValueError("native trajectory lacks the exact expected frames/atoms")
            for index in range(frames):
                frame = coordinates[index]
                if np.ma.getmaskarray(frame).any() or not np.isfinite(frame).all():
                    raise ValueError("native trajectory has missing/nonfinite atomic coordinates")
            times = np.asarray(data.variables["time"][:])
            expected = rows[-1]["time_ps"] - np.arange(frames - 1, -1, -1) * protocol["trajectory_interval"] * protocol["timestep_ps"]
            if not np.isfinite(times).all() or not np.allclose(times, expected, atol=0.002, rtol=0):
                raise ValueError("native trajectory cadence/time mismatch")
            for name in ("cell_lengths", "cell_angles"):
                if name in data.variables:
                    values = data.variables[name][:]
                    if not np.isfinite(values).all() or (values <= 0).any():
                        raise ValueError("native trajectory periodic cells are invalid")
        trajectories.append({"path": str(trajectory.relative_to(root)), "frames": frames, "atoms": atoms, "native_ns_day": ns_day, "native_elapsed_seconds": seconds})
        if alchemical:
            trajectories[-1]["alchemical"] = alchemical[-1]
    energies, temperatures = [row["Etot"] for row in all_rows], [row["temperature_k"] for row in all_rows]
    span = (max(energies) - min(energies)) / max(abs(energies[0]), 1)
    if not all(math.isfinite(value) for value in energies + temperatures) or min(temperatures) <= 0:
        raise ValueError("production thermodynamics are nonfinite/nonphysical")
    if protocol["ensemble"] == "NVE" and span > 0.02:
        raise ValueError("gross NVE energy-span gate exceeded2%; not a convergence criterion")
    if protocol["ensemble"] == "NPT" and not (270 <= statistics.mean(temperatures) <= 330 and min(temperatures) >= 150 and max(temperatures) <= 450):
        raise ValueError("NPT production temperature outside declared300K sanity gate")
    rmsd = []
    for line in (root / "data/rmsd.dat").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        row = [float(value) for value in line.split()]
        if not all(math.isfinite(value) for value in row) or row[-1] < 0:
            raise ValueError("CPPTRAJ RMSD is invalid")
        rmsd.append(row[-1])
    if len(rmsd) != sum(item["frames"] for item in trajectories):
        raise ValueError("CPPTRAJ analysis did not cover every production frame")
    gpu = []
    if (root / "gpu.csv").is_file():
        for row in csv.reader((root / "gpu.csv").open()):
            if len(row) == 7:
                gpu.append([float(value.strip()) for value in row[2:]])
    return {"status": "passed", "fixture": protocol["fixture"], "atoms": atoms, "production_steps": protocol["production_steps"], "production_ns": protocol["production_steps"] * protocol["timestep_ps"] / 1000, "native_elapsed_seconds": native_seconds, "ns_per_day": protocol["production_steps"] * protocol["timestep_ps"] / 1000 / native_seconds * 86400, "ensemble": protocol["ensemble"], "relative_total_energy_span": span, "temperature_mean_k": statistics.mean(temperatures), "temperature_min_k": min(temperatures), "temperature_max_k": max(temperatures), "restart_boundaries": boundaries, "trajectories": trajectories, "rmsd_frames": len(rmsd), "rmsd_max_angstrom": max(rmsd), "gpu_samples": len(gpu), "gpu_utilization_mean_percent": statistics.mean(row[0] for row in gpu) if gpu else None, "gpu_memory_peak_mib": max(row[2] for row in gpu) if gpu else None, "scientific_convergence_claimed": False, "free_energy_convergence_claimed": False, "exact_stochastic_continuation_claimed": False, "gpu_process_snapshot": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("choose new evidence path; do not overwrite a prior validation")
    try:
        result = validate(args.workspace)
    except Exception as exc:
        result = {"status": "failed", "error": str(exc)}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
