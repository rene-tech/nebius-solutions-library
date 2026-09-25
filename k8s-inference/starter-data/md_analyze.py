#!/usr/bin/env python3
"""Verify downloaded MD outputs and plot actual native phi/psi observations.

Run with the separate analysis requirements, not the lightweight MCP environment.
No engine, GPU, credential, remote input, or interpolation is used by this tool.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

import numpy as np


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def native_attempts(batch):
    """The parent operation coordinates work; GPU admission belongs to its jobs."""
    return [
        {
            "stage_id": stage["stage_id"],
            **{
                field: attempt.get(field)
                for field in (
                    "attempt_id",
                    "attempt_number",
                    "shard_id",
                    "outcome",
                    "resource_released",
                    "workload_name",
                    "workload_namespace",
                    "workload_uid",
                    "scheduling_admission",
                )
            },
        }
        for stage in batch.get("stages", [])
        for attempt in stage.get("attempts", [])
    ]


def materialize(folder, destination):
    """Native names come only from the hash-verified runtime result inventory."""
    receipt = read(folder / "receipt.json")
    require(receipt["state"] == "succeeded", "operation_not_succeeded")
    artifacts = {}
    jsons = []
    for item in receipt["artifacts"]:
        path = folder / item["local_path"]
        require(
            path.resolve().is_relative_to(folder.resolve()) and not path.is_symlink(),
            "unsafe_artifact_path",
        )
        require(
            path.stat().st_size == item["size_bytes"] and sha(path) == item["sha256"],
            "artifact_checksum_mismatch",
        )
        artifacts.setdefault((item["sha256"], item["size_bytes"]), path)
        if (
            item["media_type"] == "application/json"
            and item.get("compression", "none") == "none"
        ):
            jsons.append(read(path))
    native = [
        value
        for value in jsons
        if isinstance(value, dict)
        and value.get("schema", "").endswith("-workflow-result/v1")
    ]
    require(native, "native_result_missing")
    result = read(folder / "result.json")
    if isinstance(result.get("result"), dict):
        result = result["result"]
    require(
        result.get("terminal_status") == "succeeded", "published_result_not_successful"
    )
    require(
        result.get("semantic_validation", {}).get("status") == "passed",
        "runtime_semantics_failed",
    )
    materialized = []
    for value in native:
        require(
            value["status"] == "succeeded"
            and value["operation_id"] == receipt["operation_id"],
            "native_identity_mismatch",
        )
        job = value["job_id"]
        require(
            PurePosixPath(job).name == job and job not in (".", ".."),
            "invalid_native_job",
        )
        target = destination / job
        target.mkdir(parents=True, exist_ok=False)
        for item in value["files"]:
            relative = PurePosixPath(item["path"])
            require(
                not relative.is_absolute()
                and ".." not in relative.parts
                and str(relative) == item["path"],
                "unsafe_native_path",
            )
            source = artifacts[(item["sha256"], item["size_bytes"])]
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            # Hard links avoid duplicating large trajectories; never modify these files.
            try:
                path.hardlink_to(source)
            except OSError:
                shutil.copyfile(source, path)
        write(target / "native-result.json", value)
        materialized.append((target, value))
    return receipt, materialized


def trajectory_paths(root, model, stage):
    if model == "gromacs":
        paths = sorted(root.glob(stage + ".part*.xtc"))
    elif model == "namd":
        paths = sorted(root.glob(stage + ".part*.dcd")) or sorted(
            root.glob(stage + ".dcd")
        )
    elif model == "amber":
        paths = sorted(
            root.glob(("production-001" if stage == "production" else stage) + ".nc")
        )
    else:
        paths = sorted(
            root.glob(stage + ".*.lammpstrj"), key=lambda p: int(p.name.split(".")[-2])
        )
    require(paths, "missing_native_trajectory_" + stage)
    return paths


def stage_frames(paths, model):
    from native import frames

    previous = None
    for path in paths:
        for frame in frames(path, model, 0.002):
            if previous is not None and np.isclose(frame.time_ps, previous, atol=1e-5):
                # Preserve both native boundary states in files. Do not claim bitwise
                # restart equivalence; one timestamp contributes one plotted sample.
                continue
            require(
                previous is None or frame.time_ps > previous,
                "trajectory_time_out_of_order",
            )
            previous = frame.time_ps
            yield frame


def expected_origin(model, stage, expected):
    if expected.get("native_restart"):
        return 1200.0 if model == "amber" else expected["first_step"] * 0.002
    if model == "gromacs":
        return 0.0
    base = 10.0 if model == "namd" else 0.0
    return base + (
        0.0
        if stage == "nvt"
        else expected["nvt_ps"]
        if stage == "npt"
        else expected["nvt_ps"] + expected["npt_ps"]
    )


def validate_umbrella_observations(root, rows, duration):
    """Check native pull columns/cadence against actual saved coordinates.

    The source uses non-averaged phi/psi in degrees at 0.1 ps, with XTC at 1 ps.
    This is a CV correspondence check, not a compiled-bias or WHAM validation.
    """
    paths = sorted(root.glob("production.part*_pullx.xvg"))
    require(paths, "umbrella_pull_observations_missing")
    values = np.concatenate(
        [np.loadtxt(p, comments=("#", "@"), ndmin=2) for p in paths]
    )
    require(
        values.shape[1] == 3 and np.isfinite(values).all(),
        "invalid_umbrella_pull_columns",
    )
    values = values[np.r_[True, np.diff(values[:, 0]) != 0]]
    require(
        len(values) in (int(duration * 10), int(duration * 10) + 1)
        and np.allclose(np.diff(values[:, 0]), 0.1, atol=0.00005, rtol=0)
        and np.isclose(values[-1, 0], duration, atol=0.00005, rtol=0)
        and np.max(np.abs(values[:, 1:])) <= 180.000001,
        "invalid_umbrella_pull_schedule_or_units",
    )
    sampled = {round(row[0], 5): row[1:] for row in values}
    errors = []
    for row in rows:
        native = sampled[round(row["time_ps"], 5)]
        observed = np.array([row["phi_degrees"], row["psi_degrees"]])
        errors.append(np.abs((observed - native + 180) % 360 - 180))
    maximum = float(np.max(errors))
    # Conservative 0.01-degree allowance for the high-precision XTC and native
    # six-significant-digit %g output. Never align/time-shift data to make it fit.
    require(maximum <= 0.01, "umbrella_coordinate_pull_mismatch")
    return {
        "native_pull_samples": len(values),
        "coordinate_samples_compared": len(rows),
        "maximum_periodic_error_degrees": maximum,
        "tolerance_degrees": 0.01,
        "compiled_bias_or_global_pmf_validated": False,
    }


def analyze(args):
    import matplotlib
    import MDAnalysis as mda

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    sys.path.insert(0, str(args.pack / "molecular-dynamics/analysis"))
    from geometry import dihedral, make_whole

    manifest = read(args.pack / "manifest.json")
    case = next(c for c in manifest["cases"] if c["id"] == args.case)
    expected = case["expected"]["engines"][args.model]
    recipe = next(
        r
        for r in read(args.pack / case["recipes"])["recipes"]
        if r["model_id"] == args.model
    )
    args.output.mkdir(parents=True, exist_ok=False)
    receipt, jobs = materialize(args.run, args.output / "native")
    recipe_hash = hashlib.sha256(
        (json.dumps(recipe, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    ).hexdigest()
    require(
        receipt["identity"]["case_id"] == args.case
        and receipt["identity"]["model_id"] == args.model
        and receipt["identity"]["recipe_sha256"] == recipe_hash,
        "receipt_does_not_match_packaged_recipe",
    )
    require(len(jobs) == expected["jobs"], "native_job_count_mismatch")
    universe = mda.Universe(
        str(args.pack / "molecular-dynamics/canonical/system.prmtop"),
        str(args.pack / "molecular-dynamics/canonical/system.rst7"),
        format="RESTRT",
    )
    require(len(universe.atoms) == expected["atoms"], "master_atom_count_mismatch")
    peptide = universe.select_atoms("resname ACE ALA NME")
    local = {int(index): i for i, index in enumerate(peptide.indices)}
    bonds = np.array(
        [
            (local[a], local[b])
            for a, b in universe.bonds.indices
            if a in local and b in local
        ]
    )

    def atom(residue, name):
        values = [
            i for i, a in enumerate(peptide) if a.resname == residue and a.name == name
        ]
        require(len(values) == 1, "ambiguous_dihedral_atom")
        return values[0]

    phi = [atom("ACE", "C"), atom("ALA", "N"), atom("ALA", "CA"), atom("ALA", "C")]
    psi = [atom("ALA", "N"), atom("ALA", "CA"), atom("ALA", "C"), atom("NME", "N")]
    report = []
    figure, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    for root, result in jobs:
        job = next(
            j
            for j in recipe["arguments"]["parameters"]["jobs"]
            if j["id"] == result["job_id"]
        )
        require(
            result["completed_steps"] == [s["id"] for s in job["steps"]],
            "incomplete_native_stages",
        )
        stage_root = root / job["steps"][-1].get("directory", ".")
        rows, stage_reports = [], []
        for stage, duration in [
            ("nvt", expected.get("nvt_ps")),
            ("npt", expected.get("npt_ps")),
            ("production", expected["production_ps"]),
        ]:
            if duration is None:
                continue
            times = []
            for frame in stage_frames(
                trajectory_paths(stage_root, args.model, stage), args.model
            ):
                require(
                    frame.positions.shape == (6598, 3)
                    and np.isfinite(frame.positions).all(),
                    "invalid_native_coordinates",
                )
                times.append(frame.time_ps)
                if stage == "production":
                    positions = make_whole(
                        frame.positions[peptide.indices], bonds, frame.cell
                    )
                    lengths = np.linalg.norm(
                        positions[bonds[:, 0]] - positions[bonds[:, 1]], axis=1
                    )
                    require(
                        bool(((lengths > 0.5) & (lengths < 2.0)).all()),
                        "invalid_peptide_bond_length",
                    )
                    rows.append(
                        {
                            "time_ps": frame.time_ps,
                            "phi_degrees": dihedral(positions[phi]),
                            "psi_degrees": dihedral(positions[psi]),
                            "density_g_cm3": float(
                                universe.atoms.masses.sum()
                                * 1.66053906660
                                / np.linalg.det(frame.cell)
                            ),
                        }
                    )
            require(
                len(times) in (int(duration), int(duration) + 1),
                "native_frame_count_mismatch_" + stage,
            )
            require(
                np.allclose(np.diff(times), 1.0, atol=0.001, rtol=0),
                "native_frame_cadence_mismatch_" + stage,
            )
            origin = expected_origin(args.model, stage, expected)
            first = origin if len(times) == int(duration) + 1 else origin + 1
            require(
                np.isclose(times[0], first, atol=0.001, rtol=0)
                and np.isclose(times[-1], origin + duration, atol=0.001, rtol=0),
                "native_stage_time_origin_mismatch_" + stage,
            )
            stage_reports.append(
                {
                    "stage": stage,
                    "frames": len(times),
                    "first_ps": times[0],
                    "last_ps": times[-1],
                    "requested_ps": duration,
                    "trajectory_sha256": [
                        sha(p) for p in trajectory_paths(stage_root, args.model, stage)
                    ],
                }
            )
        with (args.output / (result["job_id"] + "-phi-psi.csv")).open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        for ax, key in zip(axes, ("phi_degrees", "psi_degrees")):
            ax.scatter(
                [r["time_ps"] for r in rows],
                [r[key] for r in rows],
                s=3,
                label=result["job_id"],
            )
            ax.set_ylabel(key)
        density = np.array([r["density_g_cm3"] for r in rows])
        require(bool(((density > 0.7) & (density < 1.3)).all()), "invalid_bulk_density")
        report.append(
            {
                "job_id": result["job_id"],
                "stages": stage_reports,
                "completed_steps": result["completed_steps"],
                "native_engine_id": result.get("engine_id"),
                "native_recipe_sha256": result.get("recipe_sha256"),
                "native_build": result.get("native_build"),
                "native_gpu_report": result.get("gpu"),
                "density_mean_g_cm3": float(density.mean()),
                "native_restart_used": expected.get("native_restart", False),
                "scientific_convergence_claimed": False,
                "gpu_snapshot_used": result.get("gpu_snapshot_used"),
            }
        )
        if expected.get("umbrella"):
            report[-1]["umbrella_observations"] = validate_umbrella_observations(
                stage_root, rows, expected["production_ps"]
            )
    axes[-1].set_xlabel("Native simulation time (ps)")
    axes[0].legend()
    figure.tight_layout()
    figure.savefig(args.output / "phi-psi.png", dpi=150)
    plt.close(figure)
    summary = {
        "case_id": args.case,
        "model_id": args.model,
        "state": "passed",
        "operation_id": receipt["operation_id"],
        "recipe_sha256": receipt["identity"]["recipe_sha256"],
        "input_sha256": receipt["input_sha256"],
        "jobs": report,
        "checked_artifacts": len(receipt["artifacts"]),
        "observation_seconds": receipt.get("observation_seconds"),
        "scope": "Native completion, actual trajectory count/cadence, finite coordinates/cells, peptide geometry and analysis. Not force equivalence or statistical convergence.",
    }
    observed = read(args.run / "operation.json")
    operation = observed["operation"]
    require(
        operation["id"] == receipt["operation_id"]
        and operation["model_id"] == args.model
        and operation["status"] == "succeeded",
        "operation_identity_mismatch",
    )
    summary.update(
        model_revision=operation["model_revision"],
        parent_operation_runtime=operation["runtime"],
        runtime_scope="Parent coordination is not GPU execution; native_attempts records job admission. Missing native GPU inventory is unknown, not zero GPUs.",
        native_attempts=native_attempts(observed.get("batch", {})),
        cold_start_seconds=operation.get("cold_start_seconds"),
        lifecycle={
            key: operation.get(key)
            for key in ("accepted_at", "started_at", "ready_at", "completed_at")
        },
    )
    if operation.get("accepted_at") and operation.get("completed_at"):
        summary["server_elapsed_seconds"] = (
            datetime.fromisoformat(operation["completed_at"])
            - datetime.fromisoformat(operation["accepted_at"])
        ).total_seconds()
    write(args.output / "validation.json", summary)
    print(
        json.dumps(
            {k: summary[k] for k in ("case_id", "model_id", "state", "operation_id")}
        ),
        flush=True,
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pack", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--case", required=True)
    parser.add_argument(
        "--model", required=True, choices=("gromacs", "namd", "amber", "lammps")
    )
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    analyze(parser.parse_args())
