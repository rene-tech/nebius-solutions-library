"""Check matched canonical forces, SHAKE geometry and unprofiled native timings."""
import argparse
import json
from pathlib import Path
import statistics
import sys

import numpy as np

from screen import sha


def validate(campaign, source, comparison_root, reference_campaign=None):
    sys.path.insert(0, str(comparison_root))
    sys.path.insert(0, str(comparison_root.parent / "analysis"))
    from adapter import read_data
    from geometry import minimum_image
    from native import lammps_frames
    from validate_probe import force_table

    measurements = json.loads((campaign / "measurements.json").read_text())
    baseline = (reference_campaign or campaign) / "baseline-triclinic-r1"
    reference = force_table(baseline / "initial-forces.lammpstrj")
    energy = np.loadtxt(baseline / "initial-energy.txt")
    proof = json.loads((source / "adapter-manifest.json").read_text())
    _, data = read_data(source / "system-shake.lmp")
    coefficients = {row[0]: float(row[3]) for row in data["Bond Coeffs"]}
    constrained = set(map(str, proof["shake_bond_types"]))
    water_hh = {frozenset((h1, h2)) for _, h1, h2 in proof["water_atom_ids_O_H_H"]}
    selected = [row for row in data["Bonds"] if row[1] in constrained or frozenset(map(int, row[2:])) in water_hh]
    indices = np.array([[int(row[2]) - 1, int(row[3]) - 1] for row in selected])
    targets = np.array([coefficients[row[1]] for row in selected])
    results = []
    for row in measurements:
        directory = campaign / f"{row['variant']}-r{row['repetition']}"
        if row["status"] != "native-complete":
            raise ValueError("failed native control is not performance evidence")
        if sha(directory / "native.log") != row["log_sha256"] or sha(directory / "benchmark.in") != row["input_sha256"]:
            raise ValueError("native measurement inputs/log identity changed")
        for name, expected in row["source_input_sha256"].items():
            if sha(directory / name) != expected or sha(source / name) != expected:
                raise ValueError("control changed a canonical input")
        forces = force_table(directory / "initial-forces.lammpstrj")
        terms = np.loadtxt(directory / "initial-energy.txt")
        if reference.shape != forces.shape or forces.shape != (6598, 6) or terms.shape != (9,):
            raise ValueError("incomplete initial force/energy comparison")
        coordinate_delta = float(np.abs(reference[:, :3] - forces[:, :3]).max())
        force_delta = float(np.abs(reference[:, 3:] - forces[:, 3:]).max())
        energy_delta = np.abs(energy - terms)
        if not np.isfinite(energy_delta).all() or coordinate_delta > 1e-10 or force_delta > 1e-5 or energy_delta.max() > 1e-5:
            raise ValueError(f"initial physical outputs differ: coordinates={coordinate_delta}, forces={force_delta}, energy={energy_delta.max()}")
        frames = list(lammps_frames(directory / "benchmark.lammpstrj", .002))
        final = list(lammps_frames(directory / "final.lammpstrj", .002))
        if not frames or len(final) != 1 or final[0].step != 2000 + row["warmup_steps"] + row["measured_steps"]:
            raise ValueError("control did not reach its exact requested dynamic endpoint")
        distances = []
        for frame in frames + final:
            if frame.positions.shape != (6598, 3) or not np.isfinite(frame.positions).all() or np.linalg.det(frame.cell) <= 0:
                raise ValueError("invalid finite periodic native frame")
            displacement = minimum_image(frame.positions[indices[:, 0]] - frame.positions[indices[:, 1]], frame.cell)
            distances.append(float(np.abs(np.linalg.norm(displacement, axis=1) - targets).max()))
        if max(distances) > 1e-4:
            raise ValueError("control violates canonical constrained geometry")
        results.append({"variant": row["variant"], "repetition": row["repetition"], "status": "passed",
                        "max_coordinate_delta_A": coordinate_delta, "max_force_delta_kcal_mol_A": force_delta,
                        "energy_abs_delta_kcal_mol": energy_delta.tolist(), "max_constraint_error_A": max(distances),
                        "finite_saved_frames": len(frames), "final_step": final[0].step,
                        "measured_ns_per_day": row["measured_ns_per_day"],
                        "profiled_or_synchronous": bool(row["profiler_enabled"] or row["CUDA_LAUNCH_BLOCKING"]),
                        "files": {path.name: sha(path) for path in directory.iterdir() if path.is_file()}})
    summary = {}
    for variant in sorted({row["variant"] for row in results}):
        speeds = [row["measured_ns_per_day"] for row in results if row["variant"] == variant and not row["profiled_or_synchronous"]]
        if speeds:
            summary[variant] = {"repetitions": len(speeds), "median_ns_per_day": statistics.median(speeds),
                                "min_ns_per_day": min(speeds), "max_ns_per_day": max(speeds)}
    return {"status": "passed", "scope": "matched short NPT performance controls, not full production acceptance",
            "results": results, "unprofiled_summary": summary, "customer_ready": False,
            "force_reference_path": str(baseline), "force_reference_sha256": sha(baseline / "initial-forces.lammpstrj"),
            "energy_reference_sha256": sha(baseline / "initial-energy.txt"),
            "measurement_sha256": sha(campaign / "measurements.json"), "source_adapter_sha256": sha(source / "adapter-manifest.json")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--comparison-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--reference-campaign", type=Path, help="Compare rank controls with the original one-rank numerical reference")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be new")
    report = validate(args.campaign, args.source, args.comparison_root, args.reference_campaign)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "summary": report["unprofiled_summary"], "sha256": sha(args.output)}))
