#!/usr/bin/env python3
"""Autocorrelation/block diagnostics and actual AMBER stored-velocity kinetics.

The native TEMP observable is retained, not replaced by a desired temperature.
Alternative kinetic estimators are explicitly labeled and require native-source
interpretation. Confidence intervals are finite-trajectory diagnostics only.
"""
import argparse
import csv
import json
from pathlib import Path
import re

import numpy as np
from scipy.stats import t as student_t

from compare import file_receipt, source_identity, write_json
from geometry import ValidationError, finite
from thermo import NUMBER, descriptive, number

AMBER_FORTRAN_KB = 1.380658e-23 * 6.0221367e23 / 4184.


def uncertainty(values, target=300., dt_ps=1.):
    values = finite(values, "thermodynamic diagnostic")
    n = len(values)
    if n < 20 or values.ndim != 1 or np.std(values) == 0:
        raise ValidationError("variable scalar series with at least20 samples required")
    centered = values - values.mean()
    covariance = np.correlate(centered, centered, "full")[n - 1:] / np.arange(n, 0, -1)
    acf = covariance / covariance[0]
    stop = 1
    while stop < n // 4 and acf[stop] > 0:
        stop += 1
    inefficiency = max(1., 1 + 2 * float(acf[1:stop].sum()))
    blocks = []
    for size in (10, 20, 50, 100, 200):
        count = n // size
        if count < 5:
            continue
        block_means = values[:count * size].reshape(count, size).mean(axis=1)
        sem = float(block_means.std(ddof=1) / np.sqrt(count))
        interval = student_t.ppf(.975, count - 1) * sem
        mean = float(block_means.mean())
        blocks.append({"block_size_ps": size * dt_ps, "blocks": count, "discarded_tail_samples": n - size * count,
                       "means": block_means.tolist(), "mean": mean, "SEM": sem,
                       "approximate_95pct_student_t_interval": [mean - interval, mean + interval],
                       "target_inside_interval": bool(mean - interval <= target <= mean + interval),
                       "caveat": "requires blocks effectively independent and stationarity; 5-10 blocks give weak uncertainty estimates"})
    return {**descriptive(values), "target": target, "mean_minus_target": float(values.mean() - target),
            "acf_lags_ps": (np.arange(min(n, 101)) * dt_ps).tolist(), "acf": acf[:min(n, 101)].tolist(),
            "first_nonpositive_acf_lag_ps": stop * dt_ps if stop < n // 4 else None,
            "positive_window_statistical_inefficiency": inefficiency,
            "positive_window_effective_samples": n / inefficiency,
            "positive_window_SEM": float(values.std(ddof=1) * np.sqrt(inefficiency / n)),
            "acf_method": "unbiased lag covariance; integrate initial strictly positive ACF through first nonpositive lag (capN/4), g>=1; diagnostic, not independent-replicate proof",
            "block_diagnostics": blocks, "first_half_mean": float(values[:n // 2].mean()), "second_half_mean": float(values[n // 2:].mean())}


def amber_kinetics(directory, expected_topology_sha256):
    from scipy.io import netcdf_file
    import parmed as pmd
    directory = Path(directory)
    paths = [directory / name for name in ("system.prmtop", "production-001.mdout", "production-001.mdvel")]
    inputs = [file_receipt(path) for path in paths]
    if inputs[0]["sha256"] != expected_topology_sha256:
        raise ValidationError("AMBER mass/constraint topology differs from analyzed canonical topology")
    topology = pmd.load_file(str(paths[0]))
    masses = np.asarray([atom.mass for atom in topology.atoms])
    if masses.shape != (6598,) or np.any(masses <= 0):
        raise ValidationError("not canonical6598 atom positive-mass system")
    text = re.split(r"A\s+V\s+E\s+R\s+A\s+G\s+E\s+S", paths[1].read_text())[0]
    samples = re.findall(rf"NSTEP\s*=\s*(\d+)\s+TIME\(PS\)\s*=\s*({NUMBER})\s+TEMP\(K\)\s*=\s*({NUMBER})\s+PRESS\s*=\s*({NUMBER})", text)
    kinetic = finite([number(v) for v in re.findall(rf"EKtot\s*=\s*({NUMBER})", text)], "native printed EKtot")
    rows = finite([[number(x) for x in row] for row in samples], "native sample")
    if rows.shape != (1000, 4) or kinetic.shape != (1000,) or not np.array_equal(rows[:, 0], np.arange(500, 500001, 500)) or not np.array_equal(rows[:, 1], np.arange(201, 1201)):
        raise ValidationError("canonical complete AMBER production samples required")
    with netcdf_file(paths[2], "r", mmap=False, maskandscale=True) as stream:
        variable = stream.variables["velocities"]
        if variable.units != b"angstrom/picosecond" or float(variable.scale_factor) != 20.455:
            raise ValidationError("unexpected native AMBER velocity unit/scaling")
        times = np.array(stream.variables["time"][:], dtype=np.float64)
        velocities = finite(np.array(variable[:], dtype=np.float64), "native scaled velocities")
    if velocities.shape != (1000, 6598, 3) or not np.array_equal(times, rows[:, 1]):
        raise ValidationError("velocity/log time/atom mismatch")
    # NetCDF has now scaled raw AMBER velocities into A/ps. Dividing by its
    # native conversion constant returns the engine's internal kinetic units.
    internal = velocities / 20.455
    saved_kinetic = .5 * np.sum(masses[None, :, None] * internal**2, axis=(1, 2))
    com = np.sum(masses[None, :, None] * internal, axis=1) / masses.sum()
    com_kinetic = .5 * masses.sum() * np.sum(com**2, axis=1)
    dof = 3 * 6598 - 6588
    saved_temperature = 2 * saved_kinetic / (dof * AMBER_FORTRAN_KB)
    com_removed_temperature = 2 * (saved_kinetic - com_kinetic) / ((dof - 3) * AMBER_FORTRAN_KB)
    if [file_receipt(path) for path in paths] != inputs:
        raise ValidationError("source changed during diagnostic")
    return {"inputs": inputs, "native_printed_temperature": uncertainty(rows[:, 2]), "native_pressure": uncertainty(rows[:, 3], target=1.),
            "saved_velocity_temperature_13206_DOF": uncertainty(saved_temperature), "saved_velocity_COM_removed_temperature_13203_DOF": uncertainty(com_removed_temperature),
            "native_printed_EKtot_kcal_mol": descriptive(kinetic), "saved_velocity_kinetic_kcal_mol": descriptive(saved_kinetic),
            "saved_minus_native_kinetic_kcal_mol": descriptive(saved_kinetic - kinetic), "saved_native_kinetic_correlation": float(np.corrcoef(saved_kinetic, kinetic)[0, 1]),
            "native_printed_EK_TEMP_implied_DOF_with_Fortran_KB": descriptive(2 * kinetic / (AMBER_FORTRAN_KB * rows[:, 2])),
            "constants": {"Fortran_KB_kcal_mol_K": AMBER_FORTRAN_KB, "native_velocity_scale_factor_A_ps": 20.455, "unsubtracted_DOF": dof, "COM_subtracted_DOF": dof - 3},
            "interpretation": "distinct kinetic estimators; saved velocities are not used to overwrite native TEMP. Exact CUDA reported estimator/constant definition requires primary-source check; arithmetic alone does not establish ensemble correctness or convergence"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--amber-data", action="append", required=True, help="LABEL=/absolute/data")
    parser.add_argument("--estimator-source-note", type=Path, help="retained exact-engine source-review note; does not redistribute licensed source")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be new")
    analysis_receipt = json.loads((args.analysis / "receipt.json").read_text())
    if analysis_receipt["status"] not in ("analysis-passed", "partial-analysis-passed; missing engines remain unqualified"):
        raise ValidationError("native trajectory analysis has not passed")
    for entry in analysis_receipt["outputs"]:
        if file_receipt(entry["path"]) != entry:
            raise ValidationError("analyzed output changed after native validation")
    amber_summary = json.loads((args.analysis / "amber/summary.json").read_text())
    topology_hashes = {entry["sha256"] for entry in amber_summary["inputs"] if Path(entry["path"]).name == "system.prmtop"}
    if len(topology_hashes) != 1:
        raise ValidationError("one exact analyzed AMBER topology required")
    result = {"status": "incomplete", "source": source_identity(), "analysis_receipt": file_receipt(args.analysis / "receipt.json"), "native_series": {}, "amber_velocity_controls": {}, "scientific_readiness_claimed": False}
    for engine in ("gromacs", "namd", "amber"):
        path = args.analysis / engine / "thermodynamics.csv"
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        if len(rows) != 1000 or not np.allclose([float(row["time_ps"]) for row in rows], np.arange(1, 1001), rtol=0, atol=.001):
            raise ValidationError("full uniform canonical native thermo schedule required")
        result["native_series"][engine] = {"file": file_receipt(path), "temperature": uncertainty([float(row["temperature_K"]) for row in rows]), "pressure": uncertainty([float(row["pressure_bar"]) for row in rows], target=1.)}
    for entry in args.amber_data:
        label, path = entry.split("=", 1)
        if label in result["amber_velocity_controls"]:
            raise ValidationError("duplicate AMBER control label")
        result["amber_velocity_controls"][label] = amber_kinetics(path, next(iter(topology_hashes)))
    if args.estimator_source_note:
        result["estimator_source_review"] = file_receipt(args.estimator_source_note)
        result["source_estimator_definition"] = "native printed EKE=c_ave/8*sum(m|v_current+v_previous|^2); saved current-velocity KE=sum(m|v_current|^2)/2; c_ave=1.001; DOF13206. See bound source note. Adjacent2fs previous velocities are not saved, so exact printed estimator cannot be reconstructed from1ps frames."
    result["status"] = "temperature-estimator-diagnostic-completed; interpretation and scientific readiness remain separate"
    write_json(args.output, result)


if __name__ == "__main__":
    main()
