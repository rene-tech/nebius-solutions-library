#!/usr/bin/env python3
"""Explicit derivative presentation or exact native dense-motion video.

This does not change scientific analysis or the frozen 1 ns qualification.
Presentation smoothing is labeled, geometry-checked and never passed to analysis.
Dense mode accepts only the actual 20 fs trajectory frames; no temporal smoothing.
"""
import argparse
import json
from pathlib import Path
import time

import numpy as np

from compare import ENGINES, file_receipt, inventory, master_data, source_identity, write_json
from geometry import ValidationError, align_frame, finite, kabsch
from native import frames
from render import SETTINGS, compose_grid, render_clip


def lengths(positions, bonds):
    return np.linalg.norm(positions[:, bonds[:, 0]] - positions[:, bonds[:, 1]], axis=-1)


def jitter(positions, heavy):
    """Display displacement, not physical velocities or dynamical observables."""
    coordinates = np.asarray(positions)[:, heavy]
    return {
        "median_adjacent_rms_A": float(np.median(np.sqrt(np.mean(np.sum(np.diff(coordinates, axis=0) ** 2, axis=-1), axis=-1)))),
        "rms_second_difference_A": float(np.sqrt(np.mean(np.diff(coordinates, n=2, axis=0) ** 2))),
    }


def stabilize(data):
    """Proper consecutive rigid fits; every within-frame distance is preserved."""
    peptide = finite(data["peptide"], "peptide").copy()
    water = finite(data["water"], "water").copy()
    heavy = np.asarray(data["elements"]) != "H"
    if np.count_nonzero(heavy) < 3 or len(peptide) < 3:
        raise ValidationError("three non-collinear heavy atoms and three frames required")
    maximum_error = 0.
    original_lengths = lengths(peptide, data["bonds"])
    for index in range(len(peptide)):
        center = peptide[index, heavy].mean(axis=0)
        peptide[index] -= center
        water[index] -= center
        if index:
            rotation = kabsch(peptide[index, heavy], peptide[index - 1, heavy])
            peptide[index] = peptide[index] @ rotation
            water[index] = water[index] @ rotation
    maximum_error = float(np.max(np.abs(lengths(peptide, data["bonds"]) - original_lengths)))
    if maximum_error > 1e-10:
        raise ValidationError("display stabilization changed molecular geometry")
    return {**data, "peptide": peptide, "water": water}, maximum_error


def triangular_filter(values):
    """Five saved frames, weights 1:2:3:2:1; edge extension, no raw-file writes."""
    padded = np.pad(values, [(2, 2)] + [(0, 0)] * (values.ndim - 1), mode="edge")
    return sum(weight * padded[index:index + len(values)] for index, weight in enumerate((1, 2, 3, 2, 1))) / 9


def project_display_bonds(positions, bonds, target_lengths):
    """Keep averaged display bonds from collapsing; NOT force-field integration.

    Targets are temporally averaged actual bond lengths. This correction is
    visual only and neither fixes bond angles nor claims physical intermediate
    states. Raw scientific geometry is unaffected.
    """
    result = positions.copy()
    for _ in range(150):
        for column, (a, b) in enumerate(bonds):
            delta = result[:, b] - result[:, a]
            distance = np.linalg.norm(delta, axis=-1)
            if np.any(distance < 1e-8):
                raise ValidationError("smoothing collapsed a display bond")
            correction = .5 * delta * ((distance - target_lengths[:, column]) / distance)[:, None]
            result[:, a] += correction
            result[:, b] -= correction
        error = float(np.max(np.abs(lengths(result, bonds) - target_lengths)))
        if error < 1e-6:
            return result, error
    raise ValidationError("display bond projection did not converge")


def chirality(positions, indices):
    center, a, b, c = indices
    return np.einsum("ij,ij->i", np.cross(positions[:, a] - positions[:, center], positions[:, b] - positions[:, center]), positions[:, c] - positions[:, center])


def presentation_data(data, chiral_indices):
    """Coordinate-averaged derivative at the original 1000 times, no raw claim."""
    stable, fit_error = stabilize(data)
    heavy = np.asarray(data["elements"]) != "H"
    target_lengths = triangular_filter(lengths(stable["peptide"], data["bonds"]))
    peptide, projection_error = project_display_bonds(triangular_filter(stable["peptide"]), data["bonds"], target_lengths)
    peptide -= peptide[:, heavy].mean(axis=1, keepdims=True)
    original_chirality = chirality(data["peptide"], chiral_indices)
    display_chirality = chirality(peptide, chiral_indices)
    if np.any(original_chirality * display_chirality <= 0):
        raise ValidationError("visual smoothing changed alanine chirality")
    metrics = {
        "original": jitter(data["peptide"], heavy),
        "stabilized": jitter(stable["peptide"], heavy),
        "presentation": jitter(peptide, heavy),
        "rigid_fit_bond_error_A": fit_error,
        "display_bond_projection_error_A": projection_error,
        "alanine_chirality_preserved": True,
        "scope": "display-only metrics; averaged coordinates are not native MD samples",
    }
    if metrics["presentation"]["rms_second_difference_A"] >= metrics["original"]["rms_second_difference_A"]:
        raise ValidationError("presentation did not reduce display jitter")
    return {**stable, "peptide": peptide}, metrics


def load_display(path):
    with np.load(path, allow_pickle=False) as data:
        result = {key: data[key].copy() for key in ("peptide", "water", "time_ps", "bonds", "elements")}
    if len(result["time_ps"]) != 1000 or not np.allclose(result["time_ps"], np.arange(1, 1001), atol=.001, rtol=0):
        raise ValidationError("presentation requires original 1..1000 ps samples")
    return result


def dense_data(run, master):
    """Read and rigidly align actual samples, with explicit native time origin."""
    if run.get("canonical_to_native") != "identity":
        raise ValidationError("this exact continuation requires preserved canonical atom order")
    origin, origin_step = run["origin_time_ps"], run["origin_step"]
    peptide, water, times, observed_steps = [], [], [], []
    initial_seen = False
    for frame in frames(run["trajectory"], run["engine"], .002, run.get("trajectory_format")):
        relative = frame.time_ps - origin
        if abs(relative) < .001:
            if initial_seen or times:
                raise ValidationError("duplicate or out-of-order dense initial frame")
            if frame.step is not None and frame.step != origin_step:
                raise ValidationError("dense initial native step differs")
            initial_seen = True
            continue
        expected = (len(times) + 1) * .02
        if abs(relative - expected) > .001 or len(times) >= 1000:
            raise ValidationError(f"dense frame time {relative} differs from {expected}")
        if frame.step is not None and frame.step != origin_step + (len(times) + 1) * 10:
            raise ValidationError("stored dense native step differs")
        if frame.positions.shape != (master["manifest"]["atoms"], 3):
            raise ValidationError("dense frame atom count differs")
        p, w = align_frame(frame.positions, frame.cell, master["peptide"], master["bonds"], master["fit"], master["reference"], master["waters"])
        peptide.append(p); water.append(w); times.append(relative); observed_steps.append(frame.step)
    if len(times) != 1000:
        raise ValidationError(f"expected 1000 dense native frames, found {len(times)}")
    data = {"peptide": np.asarray(peptide), "water": np.asarray(water), "time_ps": np.asarray(times), "bonds": master["bonds"], "elements": np.asarray(master["elements"])}
    stabilized, error = stabilize(data)
    return stabilized, {
        "actual_native_frames": len(times), "first_time_ps": times[0], "last_time_ps": times[-1],
        "origin_time_ps": origin, "origin_step": origin_step,
        "stored_native_steps_available": all(value is not None for value in observed_steps),
        "rigid_fit_bond_error_A": error, "temporal_smoothing": False, "interpolation": False,
        "scope": "actual new 20 ps continuation; native restart/RNG limitations are in the run receipt",
    }


def settings_for(mode):
    settings = {**SETTINGS, "half_width_A": 7.5, "water_display_radius_A": 7.0, "water_fade_inner_A": 5.0, "show_hydrogens": False,
                "water_caption": "Heavy-atom sticks · water O points fade at 5–7 Å · raw data unchanged"}
    if mode == "presentation":
        settings.update(mode_label="PRESENTATION · VISUALLY SMOOTHED", timing_caption="Five-frame averaging · not for scientific measurements · 40 ps/s",
                        metadata_comment="Display-only derivative: progressive proper rigid fits, five-frame triangular averaging, bond-length projection; not native intermediate states; raw scientific data unchanged")
    else:
        settings.update(mode_label="SLOW MOTION · ACTUAL NATIVE FRAMES", duration_ps=20, time_label="New continuation", time_decimal_places=2, frame_interval_ps=.02,
                        timing_caption="20 fs/frame · 0.8 ps/s · no averaging or interpolation",
                        metadata_comment="Actual native 20 fs coordinates from a new 20 ps continuation; proper rigid alignment only; no temporal averaging, interpolation, or generated frames; not identical RNG continuation")
    return settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("presentation", "dense"), required=True)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--analysis", type=Path, help="frozen original analysis directory, presentation only")
    parser.add_argument("--spec", type=Path, help="dense spec with four trajectories, origins and validation_receipt paths")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output directory must not exist")
    if (args.mode == "presentation" and not args.analysis) or (args.mode == "dense" and not args.spec):
        parser.error("presentation needs --analysis; dense needs --spec")
    master = master_data(args.master)
    selected = master["u"].atoms[master["peptide"]]
    chiral = [next(i for i, a in enumerate(selected) if a.resname == "ALA" and a.name == name) for name in ("CA", "N", "C", "CB")]
    runs = [] if not args.spec else json.loads(args.spec.read_text())["runs"]
    if args.mode == "dense" and (len(runs) != 4 or {r["engine"] for r in runs} != set(ENGINES)):
        parser.error("dense spec must contain each requested native engine exactly once")
    inputs = [file_receipt(args.master / "master-manifest.json")]
    if args.mode == "presentation":
        inputs.extend(file_receipt(args.analysis / engine / "display.npz") for engine in ENGINES)
    else:
        inputs.append(file_receipt(args.spec))
        for run in runs:
            for field in ("trajectory", "validation_receipt"):
                paths = run[field] if isinstance(run[field], list) else [run[field]]
                inputs.extend(file_receipt(path) for path in paths)
            validation = json.loads(Path(run["validation_receipt"]).read_text())
            if validation.get("status") != "passed":
                raise ValidationError("dense run's native validation must pass")
    args.output.mkdir(parents=True)
    settings = settings_for(args.mode)
    receipt = {"status": "incomplete", "mode": args.mode, "source": source_identity(), "inventory": inventory(), "inputs": inputs, "settings": settings, "engines": {}}
    started = time.time()
    try:
        for engine in ENGINES:
            if args.mode == "presentation":
                data, metrics = presentation_data(load_display(args.analysis / engine / "display.npz"), chiral)
            else:
                run = next(r for r in runs if r["engine"] == engine)
                data, metrics = dense_data(run, master)
            receipt["engines"][engine] = metrics
            receipt["engines"][engine]["video"] = render_clip(data, engine, args.output / f"{engine}.mp4", settings=settings)
            print(json.dumps({"engine": engine, "status": "rendered", "mode": args.mode}), flush=True)
            write_json(args.output / "receipt.json", receipt)
        receipt["grid"] = compose_grid([args.output / f"{engine}.mp4" for engine in ENGINES], args.output / "four-engine-2x2.mp4", 1000, settings)
        if any(file_receipt(record["path"]) != record for record in inputs):
            raise ValidationError("source artifact changed during rendering")
        receipt["status"] = "passed"
        receipt["input_hashes_unchanged"] = True
    except Exception as exc:
        receipt.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        receipt["wall_seconds"] = time.time() - started
        receipt["outputs"] = [file_receipt(path) for path in sorted(args.output.iterdir()) if path.is_file() and path.name != "receipt.json"]
        write_json(args.output / "receipt.json", receipt)


if __name__ == "__main__":
    main()
