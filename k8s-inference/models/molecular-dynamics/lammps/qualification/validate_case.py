"""Stream-read every trajectory frame and report native scientific/performance gates."""

import argparse
import csv
import json
import math
import re
import statistics
from contextlib import nullcontext
from pathlib import Path


def trajectory(path, expected_atoms):
    steps = []
    with path.open() as handle:
        while line := handle.readline():
            if line.strip() != "ITEM: TIMESTEP":
                raise ValueError(f"invalid trajectory frame header in {path.name}")
            step = int(handle.readline())
            if handle.readline().strip() != "ITEM: NUMBER OF ATOMS":
                raise ValueError("missing atom count")
            count = int(handle.readline())
            if count != expected_atoms:
                raise ValueError(f"atom count changed: {count} != {expected_atoms}")
            if not handle.readline().startswith("ITEM: BOX BOUNDS"):
                raise ValueError("missing box")
            for _ in range(3):
                bounds = [float(x) for x in handle.readline().split()]
                if len(bounds) < 2 or not all(math.isfinite(x) for x in bounds) or bounds[1] <= bounds[0]:
                    raise ValueError("invalid box bounds")
            columns = handle.readline().strip().split()[2:]
            if columns != ["id", "type", "x", "y", "z", "vx", "vy", "vz"]:
                raise ValueError("unexpected native dump columns")
            for identifier in range(1, count + 1):
                row = handle.readline().split()
                if len(row) != len(columns) or int(row[0]) != identifier or not all(math.isfinite(float(x)) for x in row):
                    raise ValueError("incomplete, unsorted or nonfinite native atom record")
            if steps and step <= steps[-1]:
                raise ValueError("native trajectory timesteps fail to advance")
            steps.append(step)
    if not steps:
        raise ValueError("trajectory has no frames")
    return {"name": path.name, "frames": len(steps), "first_step": steps[0], "last_step": steps[-1], "steps": steps}


def coverage(trajectories, protocol):
    for prior, current in zip(trajectories, trajectories[1:]):
        if current["first_step"] < prior["last_step"]:
            raise ValueError("trajectory parts overlap out of order")
    cadence = protocol["trajectory_every_steps"]
    expected = set(range(((protocol["warmup_steps"] + cadence - 1) // cadence) * cadence, protocol["target_step"] + 1, cadence))
    observed = {step for part in trajectories for step in part["steps"]}
    if not trajectories or observed != expected:
        raise ValueError("trajectory cadence coverage has missing or unexpected frames")


def restart_continuity(segments, commands):
    boundaries = []
    for index, (prior, current) in enumerate(zip(segments, segments[1:])):
        before, after = prior[-1], current[0]
        closed_step = commands[index]["native_restart_step"]
        if after[0] != closed_step:
            raise ValueError("native restart thermodynamic step disagrees with independently decoded checkpoint")
        boundary = {"step": int(after[0]), "numerical_continuity_measured": before[0] == after[0]}
        if before[0] == after[0]:
            boundary.update({"relative_energy_jump": abs(after[5] - before[5]) / max(abs(before[5]), 1e-12), "relative_temperature_jump": abs(after[2] - before[2]) / max(abs(before[2]), 1e-12), "relative_volume_jump": abs(after[7] - before[7]) / max(abs(before[7]), 1e-12)})
            if max(boundary[k] for k in ("relative_energy_jump", "relative_temperature_jump", "relative_volume_jump")) > 1e-4:
                raise ValueError("native restart boundary changes energy, temperature or volume above the 1e-4 gross-continuity gate")
        else:
            # Native timer expiry does not always emit a final thermo sample.
            # Comparing different steps would falsely label integration as a
            # restart jump. A fixed-boundary recovery control covers this gap.
            boundary["last_pre_checkpoint_thermo_step"] = int(before[0])
        boundaries.append(boundary)
    return boundaries


def validate(root):
    data = root / "data"
    result = json.loads((root / "result.json").read_text())
    protocol = json.loads((data / "protocol.json").read_text())
    if result["status"] != "succeeded" or result["completed_steps"] != ["prepare", "production", "analyze"]:
        raise ValueError("native workflow did not complete all requested stages")
    atoms = protocol["expected_atoms"]
    final = [float(x) for x in (data / "final-thermo.txt").read_text().split()]
    if len(final) != 8 or not all(math.isfinite(x) for x in final):
        raise ValueError("final thermodynamics are absent or nonfinite")
    if int(final[0]) != protocol["target_step"] or int(final[1]) != atoms or final[2] <= 0 or final[-1] <= 0:
        raise ValueError("final step, atom count, temperature or volume invalid")
    trajectories = [trajectory(p, atoms) for p in sorted(data.glob("trajectory.*.lammpstrj"), key=lambda p: int(p.name.split(".")[1]))]
    trajectory(data / "final.lammpstrj", atoms)
    coverage(trajectories, protocol)
    for part in trajectories:
        del part["steps"]
    loops, thermodynamics, warnings, native_timing_seconds = [], [], [], {}
    dangerous_neighbor_builds = 0
    neighbor_checking = True
    segment_thermodynamics = []
    for log in sorted(data.glob("fs2-production-segment-*.log")):
        collecting = False
        segment_samples = []
        for line in log.read_text().splitlines():
            if match := re.search(r"Loop time of ([0-9.eE+-]+) on (\d+) procs for (\d+) steps with (\d+) atoms", line):
                loops.append({"seconds": float(match[1]), "ranks": int(match[2]), "steps": int(match[3]), "atoms": int(match[4])})
            if line.strip().startswith("Step") and "TotEng" in line:
                collecting = True
                continue
            if "WARNING:" in line:
                warnings.append(line.strip())
            if match := re.match(r"^(Pair|Bond|Kspace|Neigh|Comm|Output|Modify|Other)\s*\|[^|]*\|\s*([0-9.eE+-]+)", line):
                native_timing_seconds[match[1]] = native_timing_seconds.get(match[1], 0) + float(match[2])
            if match := re.match(r"Dangerous builds = (\d+)", line):
                dangerous_neighbor_builds += int(match[1])
            if "Dangerous builds not checked" in line:
                neighbor_checking = False
            values = line.split()
            if collecting and len(values) == 8:
                try:
                    numbers = [float(x) for x in values]
                except ValueError:
                    continue
                if not all(math.isfinite(x) for x in numbers):
                    raise ValueError("nonfinite thermodynamic sample")
                thermodynamics.append(numbers)
                segment_samples.append(numbers)
        if segment_samples:
            segment_thermodynamics.append(segment_samples)
    total_steps = sum(x["steps"] for x in loops)
    if total_steps != protocol["production_steps"] or any(x["atoms"] != atoms for x in loops):
        raise ValueError("native loop summaries disagree with requested production length")
    simulation_seconds = sum(x["seconds"] for x in loops)
    steps_per_second = total_steps / simulation_seconds
    energies = [row[5] for row in thermodynamics]
    temperatures = [row[2] for row in thermodynamics]
    if not energies:
        raise ValueError("no readable native production thermodynamics")
    relative_energy_span = (max(energies) - min(energies)) / max(abs(energies[0]), 1e-12)
    finite_ensemble_gate = protocol["ensemble"] != "NVE" or relative_energy_span <= 0.02
    if protocol["ensemble"] == "NPT":
        finite_ensemble_gate = 280 <= statistics.mean(temperatures) <= 320 and min(temperatures) >= 200 and max(temperatures) <= 400
    restart_boundaries = restart_continuity(segment_thermodynamics, [c for c in result["commands"] if c["step_id"] == "production"])
    # A 2% energy-span gate catches gross integration defects; it is not a
    # material-specific accuracy criterion or a thermodynamic convergence claim.
    gpu = []
    gpu_path = root / "gpu.csv"
    with gpu_path.open() if gpu_path.exists() else nullcontext([]) as handle:
        for row in csv.reader(handle):
            if len(row) == 7:
                try:
                    gpu.append([float(x.strip()) for x in row[2:]])
                except ValueError:
                    pass
    time_scale = {"real": 1e-6, "metal": 1e-3, "lj": None}[protocol["units"]]
    return {"fixture": protocol["fixture"], "status": "passed" if finite_ensemble_gate else "failed-ensemble-gate", "scientific_convergence_claimed": False, "atoms": atoms, "production_steps": total_steps, "units": protocol["units"], "timestep": protocol["timestep"], "ensemble": protocol["ensemble"], "native_loop_seconds": simulation_seconds, "native_timing_seconds": native_timing_seconds, "atom_timesteps_per_second": atoms * steps_per_second, "ns_per_day": None if time_scale is None else steps_per_second * protocol["timestep"] * time_scale * 86400, "reduced_time_per_day": steps_per_second * protocol["timestep"] * 86400 if time_scale is None else None, "temperature_min": min(temperatures), "temperature_max": max(temperatures), "temperature_mean": statistics.mean(temperatures), "temperature_stdev": statistics.pstdev(temperatures), "relative_total_energy_span": relative_energy_span, "energy_span_gate": 0.02 if protocol["ensemble"] == "NVE" else None, "dangerous_neighbor_builds": dangerous_neighbor_builds if neighbor_checking else None, "neighbor_checking_enabled": neighbor_checking, "neighbor_convergence_claimed": False, "restart_boundaries": restart_boundaries, "restart_relative_continuity_gate": 1e-4, "trajectories": trajectories, "trajectory_frames": sum(t["frames"] for t in trajectories), "native_segments": len(loops), "native_loops": loops, "gpu_samples": len(gpu), "gpu_utilization_mean_percent": statistics.mean(x[0] for x in gpu) if gpu else None, "gpu_memory_peak_mib": max(x[2] for x in gpu) if gpu else None, "gpu_power_mean_watts": statistics.mean(x[3] for x in gpu) if gpu else None, "warnings": sorted(set(warnings)), "final_thermodynamics": final}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        receipt = validate(args.workspace)
    except Exception as exc:
        receipt = {"status": "failed", "error": str(exc)}
    if args.output:
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    raise SystemExit(0 if receipt["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
