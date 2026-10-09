#!/usr/bin/env python3
"""Read-only, exact-file-bound diagnostic of native SHAKE restart setup."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

import numpy as np

from adapter import read_data, sha256
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from geometry import ValidationError
from shake_boundary import projection_metrics, SOURCE_REVISION, WORKER_IMAGE


def edge_frame(path, last, natoms=6598):
    lines = subprocess.check_output(["tail" if last else "head", "-n", str(natoms + 9), str(path)], text=True).splitlines()
    if lines[0] != "ITEM: TIMESTEP" or lines[2:5] != ["ITEM: NUMBER OF ATOMS", str(natoms), "ITEM: BOX BOUNDS pp pp pp"] or lines[8] != "ITEM: ATOMS id type x y z vx vy vz":
        raise ValidationError("diagnostic requires exact canonical native orthogonal dump layout")
    bounds = np.array([[float(x) for x in line.split()] for line in lines[5:8]])
    records = np.array([[float(x) for x in line.split()] for line in lines[9:]])
    if records.shape != (natoms, 8) or not np.array_equal(records[:, 0], np.arange(1, natoms + 1)) or not np.isfinite(records).all():
        raise ValidationError("native boundary atom layout differs")
    return int(lines[1]), bounds, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--first", default="production.1.lammpstrj")
    parser.add_argument("--second", default="production.2.lammpstrj")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-image", choices=[WORKER_IMAGE], required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be new")
    first, second = args.data / args.first, args.data / args.second
    step, bounds, before = edge_frame(first, True)
    next_step, next_bounds, after = edge_frame(second, False)
    if step != next_step or not np.array_equal(bounds, next_bounds) or not np.array_equal(before[:, :2], after[:, :2]):
        raise ValidationError("native boundary step, cell or IDs/types changed")
    proof = json.loads((args.data / "adapter-manifest.json").read_text())
    topology = args.data / "system-shake.lmp"
    if sha256(topology) != proof["adapted_data_sha256"]:
        raise ValidationError("adapted topology identity mismatch")
    _, data = read_data(topology)
    selected = set(map(str, proof["shake_bond_types"]))
    hh = {frozenset((h1, h2)) for _, h1, h2 in proof["water_atom_ids_O_H_H"]}
    bonds = [row for row in data["Bonds"] if row[1] in selected or frozenset(map(int, row[2:])) in hh]
    coefficients = {row[0]: float(row[3]) for row in data["Bond Coeffs"]}
    atom_masses = {row[0]: float(row[1]) for row in data["Masses"]}
    masses = np.array([atom_masses[row[2]] for row in sorted(data["Atoms"], key=lambda row: int(row[0]))])
    pairs = np.array([[int(row[2]) - 1, int(row[3]) - 1] for row in bonds])
    distances = np.array([coefficients[row[1]] for row in bonds])
    metrics = projection_metrics(before[:, 2:5], after[:, 2:5], np.diag(bounds[:, 1] - bounds[:, 0]), masses, pairs, distances)
    metrics.update({"boundary_step": step, "maximum_cell_bounds_difference_A": float(np.abs(bounds - next_bounds).max()), "maximum_velocity_component_difference_A_fs": float(np.abs(after[:, 5:8] - before[:, 5:8]).max())})
    sources = []
    base = "https://raw.githubusercontent.com/lammps/lammps/c7ae612a9497437412cb787b78769570f48653dd/"
    for name, lines in (("src/RIGID/fix_shake.cpp", "454-498"), ("src/KOKKOS/fix_shake_kokkos.cpp", "1768-1843")):
        with urllib.request.urlopen(base + name) as response:
            raw = response.read()
        sources.append({"url": base + name, "sha256": hashlib.sha256(raw).hexdigest(), "relevant_lines": lines})
    report = {"status": "measured diagnostic; not trajectory acceptance", "metrics": metrics, "interpretation": "compare the measured displacement to the independent position-only constraint solution; exact native setup explicitly projects coordinates and restores velocities", "raw_boundary_records_preserved_in_original_dumps": True, "raw_files": {str(path): sha256(path) for path in (first, second, topology, args.data / "adapter-manifest.json", args.data / "production-resume.in", args.data / "constraints.inc")}, "source": sources, "diagnostic_code_sha256": sha256(__file__), "projection_code_sha256": sha256(Path(__file__).resolve().parents[1] / "analysis" / "shake_boundary.py"), "model": {"masses_Da": masses.tolist(), "pairs_zero_based": pairs.tolist(), "distances_A": distances.tolist()}}
    report.update(worker_image=args.worker_image, source_revision=SOURCE_REVISION, topology_sha256=sha256(topology), boundary_files={"previous": {"name": first.name, "sha256": sha256(first)}, "next": {"name": second.name, "sha256": sha256(second)}})
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "metrics": metrics, "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
