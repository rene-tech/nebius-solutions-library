#!/usr/bin/env python3
"""Read-only completion, molecular-integrity and coordinate/CV umbrella gates.

This is not a compiled-bias, WHAM, equilibrium or sampling-convergence gate.
The output directory is created once, outside every original input directory.
"""
import argparse
import csv
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import sys

import numpy as np

ANALYSIS = Path(__file__).resolve().parents[1] / "analysis"
sys.path.insert(0, str(ANALYSIS))
from compare import file_receipt, master_data, sha256
from geometry import ValidationError, dihedral, finite, make_whole, minimum_image, validate_cell
from native import Frame, validate_timeline

STEPS, DT_PS, XTC_EVERY, PULL_EVERY = 1_000_000, .002, 500, 50
CONSTRAINT_TOLERANCE_A = 1e-4
BOND_RANGE_A = (.6, 2.2)
PULL_FORMAT_SOURCE = "https://github.com/gromacs/gromacs/blob/v2026.2/src/gromacs/pulling/output.cpp"


class GateError(ValidationError):
    def __init__(self, gate, message):
        self.gate = gate
        super().__init__(f"{gate}: {message}")


def require(condition, gate, message):
    if not condition:
        raise GateError(gate, message)


def circular_delta(a, b):
    return (np.asarray(a) - np.asarray(b) + 180.) % 360. - 180.


def safe_relative(value):
    path = PurePosixPath(value)
    require(not path.is_absolute() and bool(path.parts) and ".." not in path.parts,
            "inventory", f"unsafe relative path {value!r}")
    return Path(*path.parts)


def verify_inventory(data, records):
    seen, verified = set(), []
    require(bool(records), "inventory", "empty native output inventory")
    for item in records:
        rel = safe_relative(item["path"])
        require(str(rel) not in seen, "inventory", f"duplicate record {rel}")
        seen.add(str(rel))
        path = data / rel
        require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(data.resolve()),
                "inventory", f"missing/unsafe native artifact {rel}")
        record = file_receipt(path)
        require(record["bytes"] == item["size_bytes"] and record["sha256"] == item["sha256"],
                "inventory", f"native artifact hash/size mismatch {rel}")
        verified.append(record)
    return verified


def parse_mdp(text):
    values = {}
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        require("=" in line, "native-controls", "malformed MDP line")
        key, value = (part.strip() for part in line.split("=", 1))
        key = key.lower().replace("_", "-")
        require(key not in values, "native-controls", f"duplicate MDP key {key}")
        values[key] = value
    return values


def controls(text):
    values = parse_mdp(text)
    expected = {"nsteps": STEPS, "dt": DT_PS, "nstxout-compressed": XTC_EVERY,
                "pull-nstxout": PULL_EVERY, "compressed-x-precision": 1_000_000}
    for key, value in expected.items():
        require(key in values and float(values[key]) == value, "native-controls", f"unexpected {key}")
    for key in ("pull-xout-average", "pull-print-ref-value", "pull-print-components", "pull-print-com"):
        require(values.get(key, "").lower() == "no", "native-controls", f"{key} must be explicitly no")
    require(values.get("pull") == "yes" and values.get("pull-ncoords") == "2",
            "native-controls", "expected two native pull coordinates")
    return values


def verify_topology(topology, frozen):
    require(topology.read_bytes() == frozen.read_bytes(), "topology", "native topology differs from frozen delivery bytes")


def bind_native_inputs(result, request, data_root, output_directory, declared):
    """Bind mixed batch-input/output layouts to actual grompp arguments.

    A batch archives every window's input subtree in each isolated workspace.
    Never select an arbitrary matching filename or another window's topology.
    """
    jobs = [job for job in request["jobs"] if job["id"] == result["job_id"]]
    require(len(jobs) == 1, "input-binding", "request/result job identity differs")
    steps = [step for step in jobs[0]["steps"] if step["id"] == "prepare-production"]
    native = [command for command in result["commands"] if command["step_id"] == "prepare-production"]
    require(len(steps) == len(native) == 1, "input-binding", "ambiguous/missing production preparation")
    step, command = steps[0], native[0]
    require(step["command"] == "grompp" and len(command["command"]) >= 2
            and command["command"][1] == "grompp" and command["command"][2:] == step["args"]
            and command.get("directory", ".") == step.get("directory", "."),
            "input-binding", "native grompp command/directory differs from exact requested step")

    def resolve(directory, argument):
        workdir = Path() if directory == "." else safe_relative(directory)
        relative = workdir / safe_relative(argument)
        require(all(not re.fullmatch(r"window-\d{2}", part) or part == result["job_id"] for part in relative.parts),
                "input-binding", f"cross-window native reference {relative}")
        require(str(relative) in declared, "input-binding", f"native path is not result-inventory-bound: {relative}")
        return data_root / relative

    def option(arguments, flag):
        require(arguments.count(flag) == 1, "input-binding", f"expected one {flag} argument")
        index = arguments.index(flag) + 1
        require(index < len(arguments) and isinstance(arguments[index], str), "input-binding", f"invalid {flag} path")
        return arguments[index]

    resolved = {flag: resolve(command.get("directory", "."), option(command["command"], flag))
                for flag in ("-f", "-p", "-n", "-o")}
    require(resolved["-o"] == output_directory / "production.tpr", "input-binding", "prepared TPR/output directory differs")
    production = [item for item in result["commands"] if item["step_id"] == "production"]
    require(production, "input-binding", "missing production command")
    for item in production:
        require(resolve(item.get("directory", "."), option(item["command"], "-s")) == resolved["-o"],
                "input-binding", "production reads a different TPR")
    return resolved, {"native_grompp_command": command, "requested_prepare_step": step,
                      "resolved_paths": {flag: str(path) for flag, path in resolved.items()},
                      "cross_window_references_allowed": False}


def validate_completion(result, request, native_logs):
    require(result.get("status") == "succeeded" and result.get("error") is None,
            "native-completion", "native result is not successful")
    jobs = [job for job in request["jobs"] if job["id"] == result["job_id"]]
    require(len(jobs) == 1, "native-completion", "request/result job identity differs")
    expected = [step["id"] for step in jobs[0]["steps"]]
    require(len(set(expected)) == len(expected) and result["completed_steps"] == expected,
            "native-completion", "completed workflow inventory differs from frozen request")
    commands = result.get("commands", [])
    require(commands and all(c.get("exit_code") == 0 for c in commands),
            "native-completion", "failed/missing native command")
    require(set(c["step_id"] for c in commands) == set(expected),
            "native-completion", "native command inventory differs")
    production = [c for c in commands if c["step_id"] == "production"]
    require(production and [c["segment"] for c in production] == list(range(1, len(production) + 1)),
            "native-completion", "production segment IDs missing or duplicated")
    checkpoints = [c.get("checkpoint_step") for c in production]
    require(all(type(x) is int and x > 0 for x in checkpoints)
            and all(a < b for a, b in zip(checkpoints, checkpoints[1:]))
            and checkpoints[-1] == STEPS, "native-checkpoint", f"unexpected checkpoint steps {checkpoints}")
    require(len(native_logs) == len(production), "native-completion", "production log/segment count differs")
    summaries = []
    for command, log in zip(production, native_logs):
        args = command["command"]
        require("mdrun" in args and "-notunepme" in args, "native-completion", "production command differs")
        pairs = re.findall(r"^\s*Step\s+Time\s*\n\s*(\d+)\s+([-+\d.eE]+)", log, re.M)
        require(pairs and int(pairs[-1][0]) == command["checkpoint_step"]
                and abs(float(pairs[-1][1]) - command["checkpoint_step"] * DT_PS) < 1e-5,
                "native-completion", "native final step/time differs from recorded checkpoint")
        require("Finished mdrun on rank" in log and re.search(
            rf"Writing checkpoint, step {command['checkpoint_step']}\b", log),
            "native-completion", "native final completion/checkpoint marker missing")
        require(not re.search(r"LINCS WARNING|Fatal error:|constraint failure", log, re.I),
                "native-stability", "native integration/constraint failure marker")
        summaries.append({"segment": command["segment"], "final_step": int(pairs[-1][0]),
                          "final_time_ps": float(pairs[-1][1]),
                          "gpu_H100_reported": "NVIDIA H100" in log})
    return {"checkpoint_steps": checkpoints, "production_commands": production,
            "native_log_completion": summaries,
            "checkpoint_step_provenance": "worker ran gmx dump -cp; result field bound to verified CPT bytes, independently corroborated by native final-step/checkpoint log markers; this CPU reader does not reimplement the CPT format"}


def pull_rows(path):
    text = path.read_text()
    require(re.search(r'@\s+s0\s+legend\s+"1"', text) and re.search(r'@\s+s1\s+legend\s+"2"', text),
            "pull-columns", "native two-coordinate legend missing or reordered")
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(("#", "@")):
            continue
        tokens = line.split()
        require(len(tokens) == 3, "pull-columns", "expected time, phi, psi only")
        rows.append([float(value) for value in tokens])
    return finite(rows, "native pull time/phi/psi")


def validate_pull_schedule(rows, steps=STEPS):
    count = steps // PULL_EVERY
    require(rows.ndim == 2 and rows.shape in ((count, 3), (count + 1, 3)),
            "pull-cadence", "wrong pull sample count")
    initial = len(rows) == count + 1
    expected = np.arange(0 if initial else PULL_EVERY, steps + 1, PULL_EVERY)
    require(np.allclose(rows[:, 0], expected * DT_PS, atol=5e-5, rtol=0),
            "pull-cadence", "missing, duplicated, shifted or unordered 0.1 ps samples")
    require(np.max(np.abs(rows[:, 1:])) <= 180.000001, "pull-units", "native dihedral is not wrapped degrees")
    return expected, initial


def xvg_rounding_bound(value):
    """Native output.cpp uses %g (six significant digits), stripping zeros."""
    magnitude = abs(float(value))
    return .5 * 10. ** (np.floor(np.log10(magnitude)) - 5) if magnitude else 5e-7


def dihedral_precision_bound(points, coordinate_error_A, printed):
    """Conservative plane-normal angular bound, plus native %g rounding.

    Each atom has component error e; a bond has vector error <=2*sqrt(3)*e.
    The cross-product perturbation is <=d*(|u|+|v|)+d². Summed plane angular
    bounds cover either dihedral sign through the periodic residual.
    """
    bonds = np.diff(finite(points, "CV coordinates"), axis=0)
    d = 2 * np.sqrt(3) * coordinate_error_A
    angles = []
    for u, v in zip(bonds, bonds[1:]):
        normal = np.linalg.norm(np.cross(u, v))
        perturbation = d * (np.linalg.norm(u) + np.linalg.norm(v)) + d * d
        require(normal > 0 and perturbation < normal, "pull-precision", "degenerate/underresolved dihedral")
        angles.append(np.arcsin(perturbation / normal))
    bound = float(np.degrees(sum(angles)) + xvg_rounding_bound(printed) + 1e-5)
    require(bound <= .05, "pull-precision", f"CV representation underresolved ({bound:g} degree bound)")
    return bound


def compare_cv(points, coordinate_error_A, native_value, label):
    observed = dihedral(points)
    bound = dihedral_precision_bound(points, coordinate_error_A, native_value)
    residual = float(circular_delta(observed, native_value))
    require(abs(residual) <= bound, "coordinate-pull-correspondence",
            f"{label}: periodic residual {residual:.9g} deg exceeds precision bound {bound:.9g}; possible CV column/sign, output timing, atom mapping or native representation mismatch, not an equilibrium test")
    return observed, residual, bound


def chirality(whole, indices):
    n, ca, c, cb = whole[indices]
    return float(np.dot(np.cross(n - ca, c - ca), cb - ca))


def molecular_geometry(positions, cell, master, constraint_pairs, constraint_lengths):
    require(positions.shape == (master["manifest"]["atoms"], 3), "coordinates", "atom count/order differs")
    positions, cell = finite(positions, "all atom coordinates"), validate_cell(cell)
    whole = make_whole(positions[master["peptide"]], master["bonds"], cell)
    distances = np.linalg.norm(whole[master["bonds"][:, 0]] - whole[master["bonds"][:, 1]], axis=1)
    require(distances.min() >= BOND_RANGE_A[0] and distances.max() <= BOND_RANGE_A[1],
            "peptide-bonds", f"bond distances outside {BOND_RANGE_A} Angstrom")
    signed = chirality(whole, master["chiral_indices"])
    require(signed * master["reference_chirality"] > 0, "chirality", "alanine inverted or coplanar stereocenter")
    delta = minimum_image(positions[constraint_pairs[:, 0]] - positions[constraint_pairs[:, 1]], cell)
    error = float(np.max(np.abs(np.linalg.norm(delta, axis=1) - constraint_lengths)))
    require(error <= CONSTRAINT_TOLERANCE_A, "constraints", f"maximum constraint residual {error:.9g} A exceeds 1e-4 A")
    return whole, {"bond_min_A": float(distances.min()), "bond_max_A": float(distances.max()),
                   "signed_chirality_A3": signed, "constraint_error_A": error}


def xtc_values(path):
    # Low-level native stream: no MDAnalysis trajectory offset/cache writes.
    from MDAnalysis.lib.formats.libmdaxdr import XTCFile
    with XTCFile(str(path), "r") as stream:
        yield from stream


def source_frame_fingerprint(value):
    import hashlib
    h = hashlib.sha256()
    for array in (value.x, value.box):
        h.update(np.asarray(array).tobytes())
    return (int(value.step), float(value.time), float(value.prec), h.hexdigest())


def validate_source_segments(paths, canonical_fingerprints):
    """Ensure trjcat did not hide dropped/changed interior or restart samples."""
    index, previous, boundaries = 0, None, []
    for segment, path in enumerate(paths):
        count = 0
        for frame_number, value in enumerate(xtc_values(path)):
            current = source_frame_fingerprint(value)
            count += 1
            if previous is not None and current[0] == previous[0] and frame_number == 0:
                require(current == previous, "trajectory-source", "restart boundary native coordinates/cell changed")
                boundaries.append({"step": current[0], "segment": segment + 1,
                                   "action": "exact identical source initialization counted once; originals retained"})
                continue
            require(index < len(canonical_fingerprints) and current == canonical_fingerprints[index],
                    "trajectory-source", "canonical trajectory differs from ordered native samples")
            previous = current
            index += 1
        require(count > 0, "trajectory-source", "empty native trajectory segment")
    require(index == len(canonical_fingerprints), "trajectory-source", "canonical trajectory contains invented/extra frames")
    return boundaries


def validate_window(workspace, delivery, master, pairs, lengths, output):
    result_path, request_path = workspace / "result.json", workspace / "request.json"
    result, request = json.loads(result_path.read_text()), json.loads(request_path.read_text())
    require(re.fullmatch(r"window-\d{2}", result["job_id"]), "identity", "unexpected window ID")
    data_root = workspace / "data"
    before = [file_receipt(result_path), file_receipt(request_path), *verify_inventory(data_root, result["files"])]
    candidates = list(data_root.rglob("production-canonical.xtc"))
    require(len(candidates) == 1, "inventory", "ambiguous/missing production trajectory")
    trajectory, data = candidates[0], candidates[0].parent
    declared = {str(Path(item["path"])) for item in result["files"]}
    required = ("production.tpr", "fs2-production.cpt", "production-canonical.xtc")
    require(all(str((data / name).relative_to(data_root)) in declared for name in required),
            "inventory", "required native input/output is not bound by result inventory")
    frozen = delivery / "runs/gromacs/data/system.top"
    input_paths, input_binding = bind_native_inputs(result, request, data_root, data, declared)
    verify_topology(input_paths["-p"], frozen)
    settings = controls(input_paths["-f"].read_text())
    log_paths = sorted(data.glob("production.part*.log"))
    completion = validate_completion(result, request, [path.read_text() for path in log_paths])
    pull_paths = sorted(data.glob("production.part*_pullx.xvg"))
    require(len(pull_paths) == len(completion["production_commands"]), "pull-cadence", "pull/segment count differs")
    # Strict duplicate policy: only the identical adjoining initialization row.
    joined, pull_boundaries = [], []
    for path in pull_paths:
        rows = pull_rows(path)
        if joined and rows[0, 0] == joined[-1][-1, 0]:
            require(np.array_equal(rows[0], joined[-1][-1]), "pull-cadence", "changed duplicate pull boundary")
            pull_boundaries.append({"time_ps": float(rows[0, 0]), "file": str(path), "action": "exact duplicate initial row counted once"})
            rows = rows[1:]
        joined.append(rows)
    pull = np.concatenate(joined)
    pull_steps, pull_initial = validate_pull_schedule(pull)
    pull_by_step = {int(step): row for step, row in zip(pull_steps, pull)}
    rows, metadata, fingerprints = [], [], []
    for index, value in enumerate(xtc_values(trajectory)):
        require(index <= STEPS // XTC_EVERY, "frame-cadence", "extra native frame")
        require(value.prec == float(settings["compressed-x-precision"]), "pull-precision", "stored XTC precision differs from declared input")
        # Convert after float64 promotion, avoiding a second float32 rounding.
        positions, cell = value.x.astype(float) * 10., value.box.astype(float) * 10.
        whole, geometry = molecular_geometry(positions, cell, master, pairs, lengths)
        metadata.append(Frame(index, None, None, float(value.time), int(value.step), "native XTC"))
        fingerprints.append(source_frame_fingerprint(value))
        require(int(value.step) in pull_by_step, "pull-cadence", "native XTC step has no matching pull observation")
        native_pull = pull_by_step[int(value.step)]
        require(abs(native_pull[0] - float(value.time)) <= 5e-5, "pull-cadence", "native coordinate/pull times differ")
        # Quantization plus float32 unpacking and periodic-cell image roundoff.
        max_coord_ulp = float(np.max(np.spacing(np.abs(value.x[master["peptide"]]))))
        max_cell_ulp = float(np.max(np.spacing(np.abs(value.box))))
        wraps = float(np.max(np.abs((whole - positions[master["peptide"]]) @ np.linalg.inv(cell))))
        e = 5. / float(value.prec) + 5. * max_coord_ulp + 15. * np.ceil(wraps + 1e-9) * max_cell_ulp
        row = {"native_frame": index, "native_step": int(value.step), "time_ps": float(value.time),
               "cell_volume_A3": float(np.linalg.det(cell)), **geometry,
               "coordinate_component_error_bound_A": e}
        for column, angle in enumerate(("phi", "psi"), 1):
            points = whole[master[angle]]
            observed, residual, bound = compare_cv(points, e, native_pull[column],
                                                   f"frame {index}, step {value.step}, {angle}")
            row.update({f"{angle}_coordinate_degrees": observed, f"{angle}_pull_degrees": float(native_pull[column]),
                        f"{angle}_periodic_residual_degrees": residual, f"{angle}_tolerance_degrees": bound})
        rows.append(row)
    _, include_zero = validate_timeline(metadata, 0, 0., STEPS, XTC_EVERY, DT_PS)
    source_paths = sorted(data.glob("production.part*.xtc"))
    require(len(source_paths) == len(completion["production_commands"]), "trajectory-source", "native trajectory/segment count differs")
    boundaries = validate_source_segments(source_paths, fingerprints)
    for item in before:
        require(sha256(item["path"]) == item["sha256"], "immutability", "original input changed during validation")
    csv_path = output / (result["job_id"] + "-frames.csv")
    with csv_path.open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    return {"status": "passed", "window_id": result["job_id"], "workspace": str(workspace),
            "operation_id": result["operation_id"], "native_engine_id": result.get("engine_id"),
            "native_engine_version": result.get("engine"), "inputs": before,
            "frozen_topology": file_receipt(frozen), "master_atom_order": "identity", "native_input_binding": input_binding,
            "completion": completion, "atoms": master["manifest"]["atoms"],
            "production_steps": STEPS, "production_duration_ps": STEPS * DT_PS,
            "real_nonzero_frames": len(rows) - int(include_zero), "initial_frame_present": bool(include_zero),
            "frame_interval_ps": XTC_EVERY * DT_PS, "pull_interval_ps": PULL_EVERY * DT_PS,
            "pull_nonzero_samples": len(pull) - int(pull_initial), "pull_initial_present": bool(pull_initial),
            "native_segment_boundaries": boundaries, "native_pull_boundaries": pull_boundaries,
            "peptide_bond_range_A": [min(r["bond_min_A"] for r in rows), max(r["bond_max_A"] for r in rows)],
            "chirality_signed_volume_range_A3": [min(r["signed_chirality_A3"] for r in rows), max(r["signed_chirality_A3"] for r in rows)],
            "constraint_count": len(pairs), "maximum_constraint_residual_A": max(r["constraint_error_A"] for r in rows),
            "cv_correspondence": {angle: {"maximum_absolute_periodic_residual_degrees": max(abs(r[f"{angle}_periodic_residual_degrees"]) for r in rows),
                                          "maximum_precision_bound_degrees": max(r[f"{angle}_tolerance_degrees"] for r in rows),
                                          "maximum_residual_fraction_of_bound": max(abs(r[f"{angle}_periodic_residual_degrees"]) / r[f"{angle}_tolerance_degrees"] for r in rows)} for angle in ("phi", "psi")},
            "declared_bias": {"phi_center_degrees": float(settings["pull-coord1-init"]),
                              "phi_k_kJ_mol_rad2": float(settings["pull-coord1-k"]), "psi_k_kJ_mol_rad2": float(settings["pull-coord2-k"]),
                              "scope": "declared input only; compiled TPR/bias-energy audit is separate. Phi is intentionally biased; psi can respond through coupling even when its direct spring is zero"},
            "frame_csv": file_receipt(csv_path), "original_files_unchanged": True,
            "trajectory_repair_performed": False, "scientific_convergence_claimed": False}


def paths_from_manifest(path):
    value = json.loads(path.read_text())
    rows = value["windows"] if isinstance(value, dict) else value
    require(isinstance(rows, list) and rows, "input-list", "nonempty windows list required")
    return [(path.parent / (row["path"] if isinstance(row, dict) else row)).resolve(strict=True) for row in rows]


def guard_output(output, inputs):
    output = output.resolve()
    require(not output.exists() and all(output != root.resolve() and not output.is_relative_to(root.resolve()) for root in inputs),
            "output", "output must be new and outside all original input trees")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--windows", type=Path, nargs="+")
    group.add_argument("--manifest", type=Path)
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    delivery = args.delivery.resolve(strict=True)
    windows = paths_from_manifest(args.manifest.resolve(strict=True)) if args.manifest else [path.resolve(strict=True) for path in args.windows]
    require(len(set(windows)) == len(windows), "input-list", "duplicate workspace paths")
    output = args.output.resolve()
    guard_output(output, [delivery, *windows])
    output.mkdir(parents=True)
    receipt = {"schema": "fs2-umbrella-native-integrity/v1", "status": "incomplete",
               "scope": "materialized native completion, topology, schedule, finite geometry and native-coordinate/pull correspondence; not compiled-bias, equilibrium, WHAM or release acceptance",
               "started_at": datetime.now(timezone.utc).isoformat(), "command": sys.argv,
               "python": platform.python_version(), "packages": {name: importlib.metadata.version(name) for name in ("numpy", "MDAnalysis", "ParmEd")},
               "windows": [], "source_files": [file_receipt(path) for path in (Path(__file__), ANALYSIS / "compare.py", ANALYSIS / "geometry.py", ANALYSIS / "native.py")],
               "tolerances": {"hydrogen_water_constraint_A": CONSTRAINT_TOLERANCE_A, "peptide_bond_range_A": BOND_RANGE_A,
                              "XVG_format": "%.4f time and %g six-significant-digit coordinates", "XVG_format_source": PULL_FORMAT_SOURCE,
                              "dihedral_precision": "XTC stored precision plus float32 coordinate/cell roundoff, geometric plane-normal perturbation bound and native text rounding; hard maximum 0.05 degree"},
               "scientific_convergence_claimed": False}
    if args.manifest:
        receipt["input_manifest"] = file_receipt(args.manifest)
    code = 0
    try:
        master = master_data(delivery / "master")
        import parmed
        structure = parmed.load_file(str(delivery / "master/system.prmtop"))
        selected = [bond for bond in structure.bonds if bond.atom1.atomic_number == 1 or bond.atom2.atomic_number == 1]
        pairs = np.array([[bond.atom1.idx, bond.atom2.idx] for bond in selected])
        lengths = np.array([bond.type.req for bond in selected])
        require(len(pairs) == 6588 and master["manifest"]["atoms"] == 6598, "master", "canonical constraint/atom inventory differs")
        peptide_atoms = master["u"].atoms[master["peptide"]]
        master["chiral_indices"] = [next(i for i, atom in enumerate(peptide_atoms) if atom.resname == "ALA" and atom.name == name) for name in ("N", "CA", "C", "CB")]
        master["reference_chirality"] = chirality(master["reference"], master["chiral_indices"])
        receipt["master_files"] = [file_receipt(path) for path in sorted((delivery / "master").iterdir()) if path.is_file()]
        frozen_result = json.loads((delivery / "runs/gromacs/result.json").read_text())
        frozen_topology = next(item for item in frozen_result["files"] if item["path"] == "system.top")
        require(sha256(delivery / "runs/gromacs/data/system.top") == frozen_topology["sha256"], "topology", "frozen reference inventory changed")
        receipt["reference_native_result"] = file_receipt(delivery / "runs/gromacs/result.json")
        receipt["reference_topology"] = file_receipt(delivery / "runs/gromacs/data/system.top")
        ids = [json.loads((path / "result.json").read_text())["job_id"] for path in windows]
        require(len(set(ids)) == len(ids), "input-list", "duplicate window IDs; do not mix repeated cohorts")
        for workspace in windows:
            try:
                result = validate_window(workspace, delivery, master, pairs, lengths, output)
            except Exception as error:
                result = {"status": "failed", "workspace": str(workspace), "failed_gate": getattr(error, "gate", "native-read-or-geometry"),
                          "error": f"{type(error).__name__}: {error}", "native_result": file_receipt(workspace / "result.json")}
                code = 1
            receipt["windows"].append(result)
            print(json.dumps({key: result[key] for key in ("status", "workspace", "error") if key in result}), flush=True)
        reference_records = [*receipt["master_files"], receipt["reference_native_result"], receipt["reference_topology"]]
        if receipt.get("input_manifest"):
            reference_records.append(receipt["input_manifest"])
        require(all(sha256(item["path"]) == item["sha256"] for item in reference_records),
                "immutability", "frozen reference/manifest changed during validation")
        receipt["frozen_reference_unchanged"] = True
        receipt["status"] = "passed" if not code else "failed"
    except Exception as error:
        receipt.update(status="failed", error=f"{type(error).__name__}: {error}")
        code = 1
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        receipt["source_head"] = subprocess.run(["git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        receipt["outputs"] = [file_receipt(path) for path in sorted(output.iterdir()) if path.is_file()]
        with (output / "receipt.json").open("x") as stream:
            json.dump(receipt, stream, indent=2, allow_nan=False); stream.write("\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
