#!/usr/bin/env python3
"""Independent read-only numerical/file cross-check of real diagnostic outputs."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def rows(path):
    with Path(path).open(newline="") as stream:
        return list(csv.DictReader(stream))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--ramachandran", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.delivery.resolve() in args.output.resolve().parents:
        parser.error("verification output must be new and outside frozen delivery")
    receipt = json.loads((args.analysis / "receipt.json").read_text())
    summary = json.loads((args.analysis / "summary.json").read_text())
    rama = json.loads((args.ramachandran / "receipt.json").read_text())
    assert receipt["status"] == "passed" and receipt["frozen_consumed_sources_unchanged"]
    verified = 0
    for record in receipt["inputs"] + receipt["outputs"]:
        path = Path(record["path"])
        assert path.stat().st_size == record["bytes"]
        assert digest(path) == record["sha256"], str(path)
        verified += 1
    for record in rama["outputs"]:
        path = args.ramachandran / record["path"]
        assert path.stat().st_size == record["bytes"] and digest(path) == record["sha256"]
    comparisons, plots, sampled = {}, [], 0
    for engine in ("gromacs", "namd", "amber", "lammps"):
        joined = rows(args.analysis / engine / "all-stages.csv")
        for stage, length in (("nvt", 100), ("npt", 100), ("production", 1000)):
            selected = [r for r in joined if r["stage"] == stage and float(r["stage_time_ps"]) > 0]
            assert len(selected) == length
            np.testing.assert_array_equal([float(r["stage_time_ps"]) for r in selected], np.arange(1, length + 1))
            for prop, stats in summary["engines"][engine]["stages"][stage]["properties"].items():
                if stats.get("status") == "unavailable":
                    assert engine == "amber" and stage == "nvt" and prop == "pressure_bar"
                    assert all(not r.get(prop) and float(r["uncomputed_pressure_placeholder_bar"]) == 0 for r in selected)
                    continue
                values = np.array([float(r[prop]) for r in selected])
                assert np.isfinite(values).all()
                np.testing.assert_allclose(values.mean(), stats["mean"], rtol=0, atol=1e-10)
                cumulative = np.cumsum(values) / np.arange(1, len(values) + 1)
                np.testing.assert_allclose(cumulative, [float(r["stage_running_mean_" + prop]) for r in selected], rtol=1e-13, atol=1e-10)
                sampled += len(values)
        production = [r for r in joined if r["stage"] == "production" and float(r["stage_time_ps"]) > 0]
        original = [r for r in rows(args.delivery / "analysis" / engine / "frames.csv") if float(r["production_time_ps"]) > .001]
        peer = rows(args.ramachandran / engine / "production-phi-psi-basins.csv")
        assert len(production) == len(original) == len(peer) == 1000
        maximum_angle_error = 0.
        for angle in ("phi", "psi"):
            values = np.array([float(r[angle + "_degrees"]) for r in production])
            for comparison in (original, peer):
                target = np.array([float(r[angle + "_degrees"]) for r in comparison])
                error = np.max(np.abs((values - target + 180) % 360 - 180))
                assert error < 1e-10
                maximum_angle_error = max(maximum_angle_error, float(error))
            ours = summary["engines"][engine]["circular_correlation"][angle]
            other = rama["engines"][engine]["correlation_diagnostics"][angle + "_circular"]
            for key in ("g", "tau_int_ps", "Neff"):
                np.testing.assert_allclose(ours[key], other[key], atol=1e-10, rtol=0)
        density_error = float(np.max(np.abs(np.array([float(r["density_cell_g_cm3"]) for r in production]) - np.array([float(r["density_g_cm3"]) for r in original]))))
        assert density_error < 1e-12
        thermo = rows(args.delivery / "analysis" / engine / "thermodynamics.csv")
        for prop in ("potential_kJ_mol", "temperature_K", "pressure_bar"):
            np.testing.assert_allclose([float(r[prop]) for r in production], [float(r[prop]) for r in thermo], atol=1e-10, rtol=0)
        comparisons[engine] = {"native_production_frames_compared": 1000,
            "maximum_periodic_angle_difference_degrees": maximum_angle_error,
            "maximum_density_difference_g_cm3": density_error,
            "all_native_production_thermo_matches_frozen_analysis": True,
            "circular_tau_g_Neff_matches_independent_worker": True}
    temperature = json.loads((args.delivery / "diagnostics/temperature-diagnostic-03.json").read_text())
    expected = temperature["amber_velocity_controls"]["H100"]["saved_velocity_temperature_13206_DOF"]["mean"]
    np.testing.assert_allclose(summary["engines"]["amber"]["stages"]["production"]["properties"]["saved_velocity_temperature_K"]["mean"], expected, atol=1e-9, rtol=0)
    for path in sorted(args.analysis.glob("*.png")):
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            assert min(im.size) >= 700
            plots.append({"name": path.name, "width": im.width, "height": im.height, "sha256": digest(path)})
    assert len(plots) == 7
    out = {"status": "passed", "scope": "read-only native output, schedule, missing-field, cumulative-mean and independent-estimator cross-check",
           "analysis_receipt_sha256": digest(args.analysis / "receipt.json"),
           "ramachandran_receipt_sha256": digest(args.ramachandran / "receipt.json"),
           "verifier_sha256": digest(__file__), "files_verified": verified,
           "nonzero_scalar_observations_checked": sampled, "engines": comparisons,
           "AMBER_current_velocity_mean_matches_source_bound_prior_diagnostic": True,
           "plots": plots, "visual_review_is_separate": True}
    args.output.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": "passed", "files_verified": verified, "nonzero_scalar_observations_checked": sampled}))


if __name__ == "__main__":
    main()
