#!/usr/bin/env python3
"""Validate actual native stage lengths, frames, constraints and closed progress."""
import argparse
import json
from pathlib import Path
import re
import sys

import numpy as np

from adapter import read_data, sha256

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from geometry import ValidationError, minimum_image
from native import lammps_frames
from thermo import native_thermo, descriptive


def validate(workspace):
    workspace = Path(workspace)
    directory = workspace / "data"
    result = json.loads((workspace / "result.json").read_text())
    if result.get("status") != "succeeded" or result.get("completed_steps") != ["minimize", "nvt", "npt", "production"]:
        raise ValidationError("native workflow has not completed every requested stage")
    proof = json.loads((directory / "adapter-manifest.json").read_text())
    if sha256(directory / "system-shake.lmp") != proof["adapted_data_sha256"]:
        raise ValidationError("adapted data identity mismatch")
    _, data = read_data(directory / "system-shake.lmp")
    types = set(map(str, proof["shake_bond_types"]))
    water_hh = {frozenset((h1, h2)) for _, h1, h2 in proof["water_atom_ids_O_H_H"]}
    selected = [row for row in data["Bonds"] if row[1] in types or frozenset(map(int, row[2:])) in water_hh]
    indices = np.array([[int(r[2]) - 1, int(r[3]) - 1] for r in selected])
    coefficients = {r[0]: float(r[3]) for r in data["Bond Coeffs"]}
    distances = np.array([coefficients[r[1]] for r in selected])
    masses = {r[0]: float(r[1]) for r in data["Masses"]}
    total_mass = sum(masses[r[2]] for r in data["Atoms"])
    stages = []
    for stage, origin, final in (("nvt", 0, 50000), ("npt", 50000, 100000), ("production", 100000, 600000)):
        commands = [command for command in result["commands"] if command["step_id"] == stage]
        if not commands or commands[-1].get("native_restart_step") != final:
            raise ValidationError(f"{stage} final native restart step missing")
        if (directory / f"{stage}-progress.txt").read_text().strip() != str(final):
            raise ValidationError(f"{stage} closed progress differs")
        native_steps, frames_seen, boundary_duplicates, previous = [], [], [], None
        worst_error, thermo_samples = 0., []
        for command in commands:
            if command["exit_code"] != 0:
                raise ValidationError(f"{stage} contains failed native segment")
            log = directory / command["log"]
            # command log paths are workspace-data relative; generated scripts
            # use directory '.', so the contract has no implicit path guessing.
            loops = re.findall(r"Loop time of \S+ on .*? for (\d+) steps", log.read_text())
            if len(loops) != 1:
                raise ValidationError(f"{stage} segment has unexpected native run count")
            native_steps.append(int(loops[0]))
            trajectory = directory / f"{stage}.{command['segment']}.lammpstrj"
            segment_frames = 0
            for frame in lammps_frames(trajectory, .002):
                if frame.positions.shape != (6598, 3):
                    raise ValidationError("native stage atom count differs")
                # Closed timer segments save a real boundary frame twice. Only
                # that exact, geometry-verified boundary may be de-duplicated in
                # the count; originals and duplicate receipt are retained.
                if previous and frame.step == previous.step and segment_frames == 0:
                    if not np.allclose(frame.positions, previous.positions, atol=1e-7, rtol=0) or not np.allclose(frame.cell, previous.cell, atol=1e-7, rtol=0):
                        raise ValidationError("restart boundary coordinates or cell differ")
                    boundary_duplicates.append({"step": frame.step, "segment": command["segment"]})
                else:
                    frames_seen.append(frame.step)
                if frame.step > origin:
                    vector = minimum_image(frame.positions[indices[:, 0]] - frame.positions[indices[:, 1]], frame.cell)
                    worst_error = max(worst_error, float(np.abs(np.linalg.norm(vector, axis=1) - distances).max()))
                previous = frame
                segment_frames += 1
            rows = native_thermo(log, "lammps_log", .002, total_mass)
            thermo_samples.extend(row for row in rows if row["step"] > origin)
        if sum(native_steps) != final - origin or frames_seen != list(range(origin, final + 1, 500)):
            raise ValidationError(f"{stage} native steps/frame schedule incomplete or duplicated")
        if worst_error > 1e-4:
            raise ValidationError(f"{stage} constraint error {worst_error} A")
        # Report unique native output samples only; repeated closed boundaries
        # are recorded above and cannot inflate sample counts.
        unique_thermo = {int(row["step"]): row for row in thermo_samples}
        stages.append({"stage": stage, "origin_step": origin, "final_step": final, "steps": sum(native_steps), "duration_ps": (final - origin) * .002, "unique_native_frames_including_initial": len(frames_seen), "segments": len(commands), "verified_duplicate_segment_boundaries": boundary_duplicates, "max_constraint_distance_error_A": worst_error, "temperature_K": descriptive([row["temperature_K"] for row in unique_thermo.values()]), "pressure_bar": descriptive([row["pressure_bar"] for row in unique_thermo.values()]), "density_g_cm3": descriptive([row["density_g_cm3"] for row in unique_thermo.values()])})
    return {"status": "native-stage-duration-frame-constraint-validation-passed", "scope": "actual complete canonical LAMMPS output semantics; cross-engine equivalence, convergence and combined customer acceptance remain separate gates", "operation_id": result["operation_id"], "job_id": result["job_id"], "native_build": result["native_build"], "native_engine_id": result["engine_id"], "stages": stages, "files": {str(path.relative_to(workspace)): sha256(path) for path in workspace.rglob("*") if path.is_file()}, "scientific_convergence_claimed": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be new")
    try:
        report = validate(args.workspace)
    except Exception as exc:
        report = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        raise
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
