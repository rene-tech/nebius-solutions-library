#!/usr/bin/env python3
"""Check actual paired native force/energy and bounded SHAKE trajectory outputs."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np

from adapter import read_data, sha256

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from geometry import ValidationError, minimum_image
from native import lammps_frames


def force_table(path):
    lines = Path(path).read_text().splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("ITEM: ATOMS "))
    names = lines[header].split()[2:]
    values = np.array([[float(v) for v in line.split()] for line in lines[header + 1:]])
    if not np.isfinite(values).all():
        raise ValidationError("nonfinite native force output")
    order = np.argsort(values[:, names.index("id")])
    values = values[order]
    if not np.array_equal(values[:, names.index("id")], np.arange(1, len(values) + 1)):
        raise ValidationError("force output atom IDs differ")
    return values[:, [names.index(x) for x in ("x", "y", "z", "fx", "fy", "fz")]]


def validate(directory):
    directory = Path(directory)
    proof = json.loads((directory / "adapter-manifest.json").read_text())
    if sha256(directory / "system-shake.lmp") != proof["adapted_data_sha256"]:
        raise ValidationError("adapted data no longer matches adapter receipt")
    first, second = [force_table(directory / f"{name}-forces.lammpstrj") for name in ("original", "adapted")]
    if first.shape != second.shape or first.shape[0] != 6598 or not np.allclose(first[:, :3], second[:, :3], atol=1e-10, rtol=0):
        raise ValidationError("paired native force inputs/order differ")
    max_force_delta = float(np.abs(first[:, 3:] - second[:, 3:]).max())
    energy = [np.loadtxt(directory / f"{name}-energy.txt") for name in ("original", "adapted")]
    if any(value.shape != (9,) or not np.isfinite(value).all() for value in energy):
        raise ValidationError("missing/nonfinite native decomposed energies")
    energy_delta = np.abs(energy[0] - energy[1])
    if max_force_delta > 1e-5 or energy_delta.max() > 1e-5:
        raise ValidationError(f"adapter changes native potential/forces: force {max_force_delta}, energy {energy_delta.max()}")
    frames = list(lammps_frames(directory / "probe.lammpstrj", .002))
    if [frame.step for frame in frames] != [0, 500, 1000, 1500, 2000]:
        raise ValidationError("native probe did not complete expected NVT+NPT schedule")
    _, data = read_data(directory / "system-shake.lmp")
    coefficients = {row[0]: float(row[3]) for row in data["Bond Coeffs"]}
    constrained_types = set(map(str, proof["shake_bond_types"]))
    # Include original water HH in geometric checks even though its bond is not
    # directly named to SHAKE: OH+HOH must enforce that third distance too.
    water_hh = {frozenset((h1, h2)) for _, h1, h2 in proof["water_atom_ids_O_H_H"]}
    selected = [row for row in data["Bonds"] if row[1] in constrained_types or frozenset(map(int, row[2:])) in water_hh]
    indices = np.array([[int(row[2]) - 1, int(row[3]) - 1] for row in selected])
    targets = np.array([coefficients[row[1]] for row in selected])
    errors = []
    for frame in frames[1:]:
        displacement = minimum_image(frame.positions[indices[:, 0]] - frame.positions[indices[:, 1]], frame.cell)
        errors.append(float(np.abs(np.linalg.norm(displacement, axis=1) - targets).max()))
    if max(errors) > 1e-4:
        raise ValidationError(f"native SHAKE geometry mismatch: {max(errors)} A")
    if (directory / "probe-progress.txt").read_text().strip() != "2000":
        raise ValidationError("native closed probe progress missing")
    return {"status": "native-adapter-probe-passed", "scope": "paired run0 + min + 1000NVT and 1000NPT steps; not full canonical dynamics acceptance", "atoms": len(first), "frames": len(frames), "native_probe_final_step": 2000, "max_native_force_delta_kcal_mol_A": max_force_delta, "decomposed_energy_abs_delta_kcal_mol": energy_delta.tolist(), "max_constrained_distance_error_A_by_saved_frame": errors, "constrained_distance_count_including_water_HH": len(selected), "files": {path.name: sha256(path) for path in directory.iterdir() if path.is_file()}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be new")
    try:
        report = validate(args.directory)
    except Exception as exc:
        report = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        raise
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
