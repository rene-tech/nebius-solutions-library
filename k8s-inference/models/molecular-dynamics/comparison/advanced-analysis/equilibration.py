#!/usr/bin/env python3
"""Read-only, stage-aware diagnostics of the four frozen alanine trajectories.

This is post-processing, not a simulation or equilibrium certification. Native
observables remain separate from derived density, RMSD and velocity temperature.
"""
from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

import numpy as np
from scipy.io import netcdf_file

# Reuse strict, previously qualified native readers without modifying them.
ANALYSIS = Path(__file__).resolve().parents[1] / "analysis"
sys.path.insert(0, str(ANALYSIS))
from compare import file_receipt, master_data, sha256, validate_protocol
from geometry import ValidationError, dihedral, finite, kabsch, make_whole
from native import frames, validate_timeline
from shake_boundary import load_policy
from spec_paths import resolve_spec
from thermo import AMU_A3_TO_G_CM3, NUMBER, native_thermo, number

ENGINES = ("gromacs", "namd", "amber", "lammps")
STAGES = ("nvt", "npt", "production")
PROPERTIES = ("potential_kJ_mol", "temperature_K", "pressure_bar",
              "density_cell_g_cm3", "backbone_rmsd_A")
LABELS = {"potential_kJ_mol": "Potential energy (kJ/mol)",
          "temperature_K": "Native temperature (K)", "pressure_bar": "Native pressure (bar)",
          "density_cell_g_cm3": "Mass / native box volume (g/cm³)",
          "backbone_rmsd_A": "Backbone RMSD to master (Å)",
          "saved_velocity_temperature_K": "AMBER current-velocity temperature (K)"}
BLOCK_PS = (20, 50, 100, 200)
DT_PS = .002
KB_AMBER = 8.31441 / 4184.
AMBER_NVT_SOURCE_REVIEW = {
    "licensed_source_file": "pmemd26_src/src/pmemd/src/runmd.F90",
    "sha256": "d3f7c49398aa14f580c34b55197f2078e2513a7ddf68f2ebd9133947f8b80803",
    "locations": "674 (need_virials condition), 919–947 (ntp>0 pressure calculation)",
    "method": "read-only inspection of exact source already hash-bound in delivered temperature note; no code redistributed",
    "manual": "AMBER26, chapter 24 pmemd, printed page 498: imin=5 trajectory analysis unsupported",
    "manual_sha256": "7f12b0c947685899eac077e632b1c8b234238047ea7ceb9b9c6fd8452ec66778",
    "replay_status": "no proven exact NVT virial replay in supplied native workflow; no speculative reconstruction attempted",
}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def write_csv(path, rows):
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def acf_trace(values):
    """Biased (1/N) centered trace autocovariance, normalized at lag zero.

    For circular variables the columns are cos(theta), sin(theta). The trace is
    invariant to an angular origin rotation; arithmetic angles are never used.
    Constant samples do not identify a mixing time or an effective sample size.
    """
    x = finite(values, "autocorrelation input")
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or len(x) < 4:
        raise ValidationError("ACF requires at least four scalar/vector samples")
    centered = x - x.mean(axis=0)
    variance = float(np.sum(centered**2) / len(x))
    if variance <= 1e-24 * max(1., float(np.mean(x**2))):
        return None
    size = 1 << (2 * len(x) - 1).bit_length()
    spectrum = np.fft.rfft(centered, n=size, axis=0)
    covariance = np.fft.irfft(spectrum.conj() * spectrum, n=size, axis=0)[:len(x)].sum(axis=1) / len(x)
    return covariance / variance


def correlation(values, dt_ps=1.):
    n = len(values)
    acf = acf_trace(values)
    if acf is None:
        return {"status": "undefined: constant observed series", "n": n,
                "g": None, "tau_int_ps": None, "Neff": None, "acf": None}
    # Geyer pairs include lag zero: (rho0+rho1), (rho2+rho3), ... .
    # Biased covariance avoids the exploding noisy N-lag denominator at long lag.
    cap = n // 2
    raw_pairs = [float(acf[i] + acf[i + 1]) for i in range(0, cap, 2)]
    pairs = []
    for value in raw_pairs:
        if value <= 0:
            break
        pairs.append(min(value, pairs[-1]) if pairs else value)
    unclamped = -1. + 2. * sum(pairs)
    g = max(1., unclamped)  # conservative: no Neff > N for anticorrelation
    capped = len(pairs) == len(raw_pairs)
    warnings = []
    if capped:
        warnings.append("positive paired sequence reached N/2 lag cap")
    if n / g < 20:
        warnings.append("fewer than 20 estimated effective samples")
    return {"status": "finite-trajectory diagnostic", "n": n, "dt_ps": dt_ps,
            "g": g, "unclamped_g": unclamped, "tau_int_ps": .5 * g * dt_ps,
            "Neff": n / g, "last_integrated_lag_ps": (2 * len(pairs) - 1) * dt_ps if pairs else 0.,
            "lag_cap_ps": cap * dt_ps, "cap_reached": capped,
            "warnings": warnings, "acf": acf[:cap + 1].tolist(),
            "method": "centered trace, 1/N lag covariance; Geyer initial-positive monotonized pairs; g>=1",
            "assumption": "stationarity and adequate mixing; reversible-chain theorem is not asserted for these finite-step MD integrators"}


def circular_correlation(degrees, dt_ps=1.):
    theta = np.deg2rad(finite(degrees, "angles"))
    if theta.ndim != 1:
        raise ValidationError("one angular series required")
    embedding = np.column_stack((np.cos(theta), np.sin(theta)))
    result = correlation(embedding, dt_ps)
    result["cos_component"] = correlation(embedding[:, 0], dt_ps)
    result["sin_component"] = correlation(embedding[:, 1], dt_ps)
    result["resultant_length"] = float(np.linalg.norm(embedding.mean(axis=0)))
    result["circular_mean_degrees"] = float(np.rad2deg(np.arctan2(embedding[:, 1].mean(), embedding[:, 0].mean())))
    result["scope"] = "observed angular fluctuation correlation, not unvisited-basin residence time"
    return result


def block_bootstrap_means(values, block, repetitions, rng):
    """Fixed-length circular moving blocks; wrap is within one declared stage."""
    x = finite(values, "bootstrap series")
    if x.ndim != 1 or not 1 <= block <= len(x) or repetitions < 20:
        raise ValidationError("invalid bootstrap dimensions")
    blocks = int(np.ceil(len(x) / block))
    starts = rng.integers(0, len(x), size=(repetitions, blocks))
    indices = (starts[:, :, None] + np.arange(block)) % len(x)
    return x[indices.reshape(repetitions, -1)[:, :len(x)]].mean(axis=1)


def interval(values):
    return np.quantile(values, [.025, .975]).tolist()


def contains_zero(bounds):
    return bounds[0] <= 0 <= bounds[1]


def scalar_diagnostics(values, rng, repetitions=2000, dt_ps=1.):
    x = finite(values, "scalar observations")
    if x.ndim != 1 or len(x) < 40:
        raise ValidationError("at least 40 scalar observations required for block diagnostics")
    corr = correlation(x, dt_ps)
    n = len(x)
    result = {"n": n, "mean": float(x.mean()), "SD": float(x.std(ddof=1)),
              "min": float(x.min()), "max": float(x.max()), "first": float(x[0]),
              "last": float(x[-1]), "correlation": corr,
              "first_half_mean": float(x[:n // 2].mean()),
              "second_half_mean": float(x[n // 2:].mean()), "blocks": []}
    result["fixed_endpoint_windows"] = {
        "first_10_ps_mean": float(x[:int(round(10 / dt_ps))].mean()),
        "last_20_ps_mean": float(x[-int(round(20 / dt_ps)):].mean()),
        "scope": "descriptive fixed windows, not selected equilibration cutoffs or independent estimates",
    }
    result["second_minus_first_half"] = result["second_half_mean"] - result["first_half_mean"]
    if corr["g"] is None:
        result.update(stationarity="not identifiable from a constant series; may be fixed by construction",
                      primary_block_ps=None, mean_95pct_interval=None)
        return result, None
    eligible = [size for size in BLOCK_PS if size / dt_ps <= n // 2]
    required = 5 * corr["tau_int_ps"]
    primary = next((size for size in eligible if size >= required), eligible[-1])
    selected = None
    for size in eligible:
        length = int(round(size / dt_ps))
        boot = block_bootstrap_means(x, length, repetitions, rng)
        half_blocks = min(n // 2, n - n // 2) / length
        # Percentile CI for a half-window contrast: separate within-half block
        # resampling. This is a drift diagnostic conditional on local stationarity.
        # A block spanning an entire half always reproduces its mean; that
        # degenerate bootstrap is NOT a zero-width uncertainty measurement.
        if half_blocks >= 2:
            left = block_bootstrap_means(x[:n // 2], length, repetitions, rng)
            right = block_bootstrap_means(x[n // 2:], length, repetitions, rng)
            delta_ci = interval(right - left)
        else:
            delta_ci = None
        entry = {"block_ps": size, "equivalent_nonoverlapping_blocks": n / length,
                 "half_window_blocks": half_blocks,
                 "mean_95pct_interval": interval(boot), "half_difference_95pct_interval": delta_ci,
                 "resolved_half_difference": None if delta_ci is None else not contains_zero(delta_ci),
                 "block_at_least_5_estimated_tau": size >= required,
                 "few_blocks": n / length < 10 or min(n // 2, n - n // 2) / length < 5}
        result["blocks"].append(entry)
        if size == primary:
            selected, primary_boot = entry, boot
    result.update(primary_block_ps=primary, mean_95pct_interval=selected["mean_95pct_interval"],
                  half_difference_95pct_interval=selected["half_difference_95pct_interval"],
                  block_requirement_met=selected["block_at_least_5_estimated_tau"])
    if not selected["block_at_least_5_estimated_tau"] or corr.get("cap_reached") or selected["half_difference_95pct_interval"] is None:
        verdict = "not resolved: correlation/window too long for available block sensitivity"
    elif selected["resolved_half_difference"]:
        verdict = "detected half-window change (conditional, unadjusted 95% diagnostic)"
    else:
        verdict = "no detected half-window change; equilibrium is not established"
    result["stationarity"] = verdict
    result["interval_scope"] = "pointwise/unadjusted 95%; no multiplicity correction"
    result["caveat"] = ("Percentile moving-block intervals assume within-window stationarity and mixing; "
                        "multiple diagnostics are not family-wise hypothesis tests. Blocks of 200 ps give "
                        "only five blocks per ns. No burn-in was selected to improve agreement.")
    return result, primary_boot


def gromacs_energy(path):
    """Native labeled XVG; density is optional in constant-volume stages."""
    text = Path(path).read_text()
    legends = {int(i): label for i, label in re.findall(r'@\s+s(\d+)\s+legend\s+"([^"]+)"', text)}
    required = {"Potential", "Temperature", "Pressure"}
    if not required <= set(legends.values()) or len(set(legends.values())) != len(legends):
        raise ValidationError("missing/duplicate GROMACS native energy labels")
    mapping = {"Potential": ("potential_kJ_mol", 1.), "Temperature": ("temperature_K", 1.),
               "Pressure": ("pressure_bar", 1.), "Density": ("density_g_cm3", .001)}
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(("#", "@")):
            continue
        values = finite([number(v) for v in line.split()], "XVG row")
        if len(values) != max(legends) + 2:
            raise ValidationError("GROMACS XVG field count differs from legends")
        row = {"time_ps": float(values[0])}
        for index, label in legends.items():
            if label in mapping:
                key, scale = mapping[label]
                row[key] = float(values[index + 1] * scale)
        rows.append(row)
    return rows


def amber_nvt_pressure_gap(rows, control, text):
    # Exact frozen protocol has no native NVT virial observable. Never infer
    # actual pressure from a zero placeholder or from temperature/target pressure.
    if not re.search(r"\bntp\s*=\s*0\b", control, re.I):
        raise ValidationError("AMBER NVT control is not ntp=0")
    if "VIRIAL" in text.upper() or any(row.get("pressure_bar") != 0 for row in rows):
        raise ValidationError("AMBER NVT no-virial/zero-placeholder assumption no longer holds")
    for row in rows:
        row["uncomputed_pressure_placeholder_bar"] = row.pop("pressure_bar")
    return "unavailable: ntp=0; original NVT output contains only zero PRESS placeholders and no virial; no proven replay supplied"


def stage_specs(delivery, run):
    engine = run["engine"]
    data = delivery / "runs" / engine / "data"
    if engine == "namd":
        data /= "alanine"
    result = []
    for stage in STAGES:
        production = stage == "production"
        index = STAGES.index(stage)
        duration = 1000 if production else 100
        origin_time = (0 if engine == "gromacs" else
                       (10 + 100 * index if engine == "namd" else 100 * index))
        origin_step = (0 if engine in ("gromacs", "amber") else int(round(origin_time / DT_PS)))
        item = {"stage": stage, "duration_ps": duration, "origin_time_ps": origin_time,
                "origin_step": origin_step, "elapsed_origin_ps": 100 * index,
                "kind": {"gromacs": "gromacs_xvg", "namd": "namd_log", "amber": "amber_mdout", "lammps": "lammps_log"}[engine]}
        if production:
            item.update(trajectory=run["trajectory"], thermo=run["thermo"]["path"], log=run["production_log"])
        elif engine == "gromacs":
            item.update(trajectory=str(data / f"{stage}.part0001.xtc"), thermo=str(data / f"{stage}-energy.xvg"), log=str(data / f"{stage}.part0001.log"))
        elif engine == "namd":
            item.update(trajectory=str(data / ("nvt.dcd" if stage == "nvt" else "npt.part000001.dcd")),
                        thermo=str(data / f"fs2-{stage}-part000001.log"), log=str(data / f"fs2-{stage}-part000001.log"))
        elif engine == "amber":
            item.update(trajectory=str(data / f"{stage}.nc"), thermo=str(data / f"{stage}.mdout"), log=str(data / f"{stage}.mdout"))
        else:
            item.update(trajectory=str(data / f"{stage}.1.lammpstrj"), thermo=str(data / f"fs2-{stage}-segment-000001.log"), log=str(data / f"fs2-{stage}-segment-000001.log"))
        control = ({"gromacs": f"{stage}.mdp", "namd": f"{stage}.namd",
                    "amber": "production-001.in" if production else f"{stage}.in", "lammps": f"{stage}.in"})[engine]
        item["control"] = str(data / control)
        if engine == "namd":
            item["wrapper"] = str(data / f"fs2-{stage}-part000001.namd")
            wrapper = Path(item["wrapper"]).read_text()
            if f'source "{control}"' not in wrapper:
                raise ValidationError("NAMD wrapper no longer directly sources expected native control")
        if engine == "amber":
            item["velocities"] = str(data / ("production-001.mdvel" if production else f"{stage}.mdvel"))
        if production and "restart_boundary_policy" in run:
            item["restart_boundary_policy"] = run["restart_boundary_policy"]
        result.append(item)
    return result


def stage_thermo(spec, engine, total_mass):
    boundaries = []
    rows = (gromacs_energy(spec["thermo"]) if engine == "gromacs" else
            native_thermo(spec["thermo"], spec["kind"], DT_PS, total_mass, boundary_receipts=boundaries))
    pressure = "native pressure; definition is engine specific"
    control = Path(spec["control"]).read_text()
    if engine == "amber" and spec["stage"] == "nvt":
        pressure = amber_nvt_pressure_gap(rows, control, Path(spec["log"]).read_text())
    elif engine == "namd":
        if not re.search(r"^\s*useGroupPressure\s+yes\s*$", control, re.I | re.M):
            raise ValidationError("frozen NAMD useGroupPressure control differs")
        for row in rows:
            row["pressure_bar"] = row["group_pressure_bar"]
        pressure = "native GPRESSURE (group); atomic PRESSURE also retained"
    elif engine == "amber":
        pressure = "native SCR molecular-virial pressure"
    elif engine == "lammps":
        pressure = "native atomic-virial Press, atm multiplied by 1.01325"
    normalized = []
    for row in rows:
        row = dict(row)
        row["native_time_ps"] = row.pop("time_ps")
        if "step" in row:
            row["native_step"] = row.pop("step")
        relative = row["native_time_ps"] - spec["origin_time_ps"]
        if "native_step" in row and abs(relative - (row["native_step"] - spec["origin_step"]) * DT_PS) > 1e-3:
            raise ValidationError("native thermo stage step/time mismatch")
        row.update(stage=spec["stage"], stage_time_ps=relative,
                   elapsed_dynamics_ps=relative + spec["elapsed_origin_ps"],
                   observation_role="initialization" if abs(relative) < 1e-3 else "dynamic sample")
        normalized.append(row)
    validate_schedule([r["stage_time_ps"] for r in normalized], spec["duration_ps"], "native thermodynamics")
    return normalized, {"pressure": pressure, "restart_boundaries": boundaries}


def validate_schedule(times, duration, label):
    t = finite(times, label)
    if len(t) not in (int(duration), int(duration) + 1):
        raise ValidationError(f"{label}: incomplete 1 ps stage schedule")
    start = 0 if len(t) == duration + 1 else 1
    if not np.allclose(t, np.arange(start, int(duration) + 1), rtol=0, atol=.001):
        raise ValidationError(f"{label}: duplicate, missing, or shifted native stage times")


def backbone_definition(master):
    # Cap carbonyls are included, cap methyls/ALA side chain are not. This is
    # explicitly seven heavy backbone atoms, not a three-atom ALA-only fit.
    selected = [("ACE", "C"), ("ACE", "O"), ("ALA", "N"), ("ALA", "CA"),
                ("ALA", "C"), ("ALA", "O"), ("NME", "N")]
    peptide = master["u"].atoms[master["peptide"]]
    indices = []
    for residue, name in selected:
        found = [i for i, atom in enumerate(peptide) if atom.resname == residue and atom.name == name]
        if len(found) != 1:
            raise ValidationError("ambiguous backbone atom")
        indices.append(found[0])
    return np.asarray(indices), [f"{a}:{b}" for a, b in selected]


def aligned_rmsd(whole, reference, indices):
    mobile = whole[indices] - whole[indices].mean(axis=0)
    target = reference[indices] - reference[indices].mean(axis=0)
    fit = mobile @ kabsch(mobile, target)
    return float(np.sqrt(np.mean(np.sum((fit - target)**2, axis=1))))


def velocity_temperatures(path, masses, expected_times):
    with netcdf_file(path, "r", mmap=False, maskandscale=True) as handle:
        variable = handle.variables["velocities"]
        if variable.units != b"angstrom/picosecond" or float(variable.scale_factor) != 20.455:
            raise ValidationError("native AMBER velocity units/scaling differ")
        times = finite(np.array(handle.variables["time"][:]), "velocity times")
        velocity = finite(np.array(variable[:], dtype=float), "saved velocities")
    if velocity.shape != (len(expected_times), len(masses), 3) or not np.allclose(times, expected_times, rtol=0, atol=.001):
        raise ValidationError("saved velocity/coordinate schedules differ")
    kinetic = .5 * np.einsum("a,fak,fak->f", masses, velocity / 20.455, velocity / 20.455)
    return 2 * kinetic / (13206 * KB_AMBER)


def trajectory_series(spec, engine, master, run):
    policy = None
    boundary = []
    if spec.get("restart_boundary_policy"):
        policy = load_policy(spec["restart_boundary_policy"], run["image"], spec["trajectory"], run["native_topology"])
    fit, labels = backbone_definition(master)
    mass = float(master["u"].atoms.masses.sum())
    metadata, rows = [], []
    for frame in frames(spec["trajectory"], engine, DT_PS, boundary_receipts=boundary, boundary_policy=policy):
        if frame.positions.shape != (6598, 3):
            raise ValidationError("not the canonical full atom inventory")
        whole = make_whole(frame.positions[master["peptide"]], master["bonds"], frame.cell)
        bond_lengths = np.linalg.norm(whole[master["bonds"][:, 0]] - whole[master["bonds"][:, 1]], axis=1)
        if bond_lengths.min() < .6 or bond_lengths.max() > 2.2:
            raise ValidationError("broken peptide atom mapping/bonds")
        relative = frame.time_ps - spec["origin_time_ps"]
        row = {"stage": spec["stage"], "stage_time_ps": relative,
               "elapsed_dynamics_ps": relative + spec["elapsed_origin_ps"],
               "native_time_ps": frame.time_ps, "native_step": frame.step,
               "time_source": frame.time_source, "observation_role": "initialization" if abs(relative) < .001 else "dynamic sample",
               "volume_A3": float(np.linalg.det(frame.cell)),
               "density_cell_g_cm3": mass / float(np.linalg.det(frame.cell)) * AMU_A3_TO_G_CM3,
               "backbone_rmsd_A": aligned_rmsd(whole, master["reference"], fit),
               "phi_degrees": dihedral(whole[master["phi"]]), "psi_degrees": dihedral(whole[master["psi"]])}
        rows.append(row)
        frame.positions = frame.velocities = None
        metadata.append(frame)
    validate_timeline(metadata, spec["origin_step"], spec["origin_time_ps"], int(spec["duration_ps"] / DT_PS), 500, DT_PS)
    validate_schedule([r["stage_time_ps"] for r in rows], spec["duration_ps"], "native coordinates")
    if engine == "amber":
        temperatures = velocity_temperatures(spec["velocities"], master["u"].atoms.masses, [r["native_time_ps"] for r in rows])
        for row, temperature in zip(rows, temperatures):
            row["saved_velocity_temperature_K"] = float(temperature)
    return rows, {"frames": len(rows), "backbone_atoms": labels,
                  "backbone_reference": "unchanged canonical master; whole peptide then proper rotation fit of same seven backbone atoms",
                  "restart_boundaries": boundary}


def running_rows(rows, fields):
    result = []
    # A stage reset is mandatory; initial samples are retained but not averaged.
    sums, counts = {}, {}
    for row in rows:
        value = dict(row)
        stage = row["stage"]
        for field in fields:
            key = (stage, field)
            if field not in row or row[field] is None:
                continue
            if row["stage_time_ps"] > .001:
                sums[key] = sums.get(key, 0.) + row[field]
                counts[key] = counts.get(key, 0) + 1
                value["stage_running_mean_" + field] = sums[key] / counts[key]
                value["stage_running_n_" + field] = counts[key]
        result.append(value)
    return result


def water_identity(master):
    import parmed as pmd
    top = pmd.load_file(str(master["directory"] / "system.prmtop"))
    waters = [r for r in top.residues if r.name == "WAT"]
    residue = waters[0]
    parameters = [{"name": atom.name, "charge_e": float(atom.charge), "mass_Da": float(atom.mass),
                   "sigma_A": float(atom.sigma), "epsilon_kcal_mol": float(atom.epsilon)} for atom in residue.atoms]
    for water in waters:
        values = [{"name": a.name, "charge_e": float(a.charge), "mass_Da": float(a.mass),
                   "sigma_A": float(a.sigma), "epsilon_kcal_mol": float(a.epsilon)} for a in water.atoms]
        if values != parameters:
            raise ValidationError("water parameters differ across master residues")
    bonds = [{"atoms": [b.atom1.name, b.atom2.name], "r0_A": b.type.req} for b in top.bonds if b.atom1.residue is residue and b.atom2.residue is residue]
    peptide_mass = float(master["u"].atoms[master["peptide"]].masses.sum())
    total_mass = float(master["u"].atoms.masses.sum())
    return {"water_molecules": len(waters), "parameters": parameters, "rigid_water_distances": bonds,
            "total_mass_Da": total_mass, "peptide_mass_Da": peptide_mass,
            "peptide_mass_fraction": peptide_mass / total_mass,
            "peptide_molecule_fraction": 1 / (len(waters) + 1),
            "density_scope": "finite periodic dilute peptide solution, not pure water or an infinite-dilution extrapolation"}


def join_observations(thermo, coordinates, spec):
    """Join by validated nominal native schedule, never interpolate observations."""
    result = {}
    for kind, source in (("thermo", thermo), ("coordinate", coordinates)):
        seen = set()
        for row in source:
            nominal = int(round(row["stage_time_ps"]))
            if nominal in seen or abs(row["stage_time_ps"] - nominal) > .001:
                raise ValidationError("ambiguous observed stage time join")
            seen.add(nominal)
            target = result.setdefault(nominal, {"stage": spec["stage"], "stage_time_ps": nominal,
                "elapsed_dynamics_ps": nominal + spec["elapsed_origin_ps"],
                "observation_role": "initialization" if nominal == 0 else "dynamic sample"})
            for key, value in row.items():
                if key in ("stage", "stage_time_ps", "elapsed_dynamics_ps", "observation_role"):
                    continue
                if key in ("native_time_ps", "native_step"):
                    key = key.replace("native_", "native_" + kind + "_")
                if key == "density_g_cm3":
                    key = "density_log_g_cm3"
                if key in target:
                    raise ValidationError(f"conflicting joined observable {key}")
                target[key] = value
    rows = [result[index] for index in sorted(result)]
    validate_schedule([row["stage_time_ps"] for row in rows], spec["duration_ps"], "joined native observations")
    return running_rows(rows, (*PROPERTIES, "saved_velocity_temperature_K"))


def figures(result, series, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = dict(zip(ENGINES, ("#2563eb", "#9333ea", "#d97706", "#059669")))
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    for engine in ENGINES:
        fig, axes = plt.subplots(5, 2, figsize=(14, 15), constrained_layout=True)
        for index, prop in enumerate(PROPERTIES):
            for col, stages in enumerate((("nvt", "npt"), ("production",))):
                ax = axes[index, col]
                for stage in stages:
                    rows = series[engine][stage]
                    xkey = "elapsed_dynamics_ps" if col == 0 else "stage_time_ps"
                    selected = [r for r in rows if prop in r]
                    if selected:
                        ax.plot([r[xkey] for r in selected], [r[prop] for r in selected],
                                lw=.65, alpha=.35, color=colors[engine], label="Native / derived samples")
                        average = [r for r in selected if "stage_running_mean_" + prop in r]
                        ax.plot([r[xkey] for r in average], [r["stage_running_mean_" + prop] for r in average],
                                lw=1.7, color=colors[engine], label="Stage cumulative mean")
                    elif prop == "pressure_bar":
                        ax.text(50 if col == 0 else 500, .5, "NVT pressure unavailable\nzero placeholder excluded",
                                ha="center", va="center", transform=ax.get_xaxis_transform(), fontsize=9)
                    if engine == "amber" and prop == "temperature_K":
                        velocity = [r for r in rows if "saved_velocity_temperature_K" in r]
                        ax.plot([r[xkey] for r in velocity], [r["stage_running_mean_saved_velocity_temperature_K"] for r in velocity],
                                "--", lw=1.2, color="#111827", label="Current-velocity cumulative mean")
                if col == 0:
                    ax.axvspan(0, 100, alpha=.055, color="#64748b")
                    ax.axvline(100, color="gray", lw=.8, ls=":")
                    ax.set_xlim(0, 200)
                else:
                    ax.set_xlim(0, 1000)
                    if engine == "lammps":
                        ax.axvline(592, color="gray", lw=.8, ls=":")
                if prop == "temperature_K":
                    ax.axhline(300, color="gray", ls=":", lw=.8)
                if prop == "pressure_bar":
                    ax.axhline(1, color="gray", ls=":", lw=.8)
                ax.set_ylabel(LABELS[prop])
                ax.grid(alpha=.15)
        axes[0, 0].set_title("NVT: 0–100 ps | NPT: 100–200 ps")
        axes[0, 1].set_title("Production: 1–1000 ps (initialization retained separately)")
        axes[-1, 0].set_xlabel("Elapsed dynamics (ps); minimization has no physical-time axis")
        axes[-1, 1].set_xlabel("Production-relative time (ps)")
        handles, labels = axes[1, 1].get_legend_handles_labels()
        unique = dict(zip(labels, handles))
        fig.legend(unique.values(), unique.keys(), loc="outside lower center", ncol=3)
        fig.suptitle(f"{engine.upper()} — complete frozen native stages; means reset at each stage", fontsize=14)
        fig.savefig(output / f"{engine}-full-stages.png", dpi=145, bbox_inches="tight", pad_inches=.12)
        plt.close(fig)
    fig, axes = plt.subplots(5, 1, figsize=(13, 15), constrained_layout=True)
    for ax, prop in zip(axes, PROPERTIES):
        for engine in ENGINES:
            rows = [r for r in series[engine]["production"] if r["stage_time_ps"] > 0 and prop in r]
            ax.plot([r["stage_time_ps"] for r in rows], [r[prop] for r in rows], color=colors[engine], alpha=.10, lw=.6)
            ax.plot([r["stage_time_ps"] for r in rows], [r["stage_running_mean_" + prop] for r in rows], color=colors[engine], label=engine.upper(), lw=1.7)
        ax.set_ylabel(LABELS[prop])
        ax.grid(alpha=.15)
        ax.set_xlim(1, 1000)
    axes[0].legend(ncol=4)
    axes[-1].set_xlabel("Production-relative time (ps)")
    fig.suptitle("Native/derived samples (faint) and cumulative means — different estimator conventions retained")
    fig.savefig(output / "production-comparison.png", dpi=145, bbox_inches="tight", pad_inches=.12)
    plt.close(fig)
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5), constrained_layout=True)
    for ax, angle in zip(axes, ("phi", "psi")):
        for engine in ENGINES:
            item = result["engines"][engine]["circular_correlation"][angle]
            ax.plot(np.arange(len(item["acf"])), item["acf"], color=colors[engine],
                    label=f"{engine.upper()}: τ={item['tau_int_ps']:.2f} ps, N_eff={item['Neff']:.1f}")
        ax.axhline(0, color="gray", lw=.8)
        ax.set_xlim(0, 200)
        ax.set_xlabel("Lag (ps); CSV/JSON retains lags through 500 ps")
        ax.set_ylabel(f"Centered circular {angle} autocorrelation")
        ax.legend(fontsize=8)
        ax.grid(alpha=.15)
    fig.suptitle("1 ns production: circular trace ACF; short apparent mixing is not a basin-convergence proof")
    fig.savefig(output / "circular-autocorrelation.png", dpi=160, bbox_inches="tight", pad_inches=.12)
    plt.close(fig)
    fig, axes = plt.subplots(5, 4, figsize=(16, 14), constrained_layout=True)
    for col, engine in enumerate(ENGINES):
        for row, prop in enumerate(PROPERTIES):
            ax = axes[row, col]
            diag = result["engines"][engine]["stages"]["production"]["properties"][prop]
            mean = diag["mean"]
            for block in diag["blocks"]:
                lo, hi = block["mean_95pct_interval"]
                x = block["block_ps"]
                ax.plot([x, x], [lo, hi], color=colors[engine], lw=2)
                ax.plot(x, mean, "o", color=colors[engine], ms=3)
            ax.axhline(mean, color="gray", lw=.6)
            ax.set_xticks(BLOCK_PS)
            if col == 0:
                ax.set_ylabel(LABELS[prop])
            if row == 0:
                ax.set_title(engine.upper())
            if row == 4:
                ax.set_xlabel("Bootstrap block length (ps)")
            ax.grid(alpha=.15)
    fig.suptitle("Conditional 95% mean intervals: 20/50/100/200 ps block sensitivity (not equilibrium certification)")
    fig.savefig(output / "block-sensitivity.png", dpi=145, bbox_inches="tight", pad_inches=.12)
    plt.close(fig)


def report(result, output):
    text = ["# Four-engine equilibration and ensemble diagnostics", "",
            "CPU-only analysis of the immutable final delivery. Native NVT (100 ps), NPT preparation (100 ps), "
            "and production (1000 ps) are preserved as separate stages. No simulation, replay, frame interpolation, "
            "or chosen burn-in was added. Initial samples are plotted and retained but excluded from stage means.", "",
            "## Production observables", "",
            "Intervals are conditional, pointwise/unadjusted 95% fixed-length circular moving-block bootstrap intervals; no multiplicity correction. "
            "The first tested block length at least five estimated integrated correlation times is selected; "
            "20/50/100/200 ps sensitivity and inadequate block warnings are retained. They are not equilibrium guarantees.", "",
            "| Engine | Observable | Mean | Conditional 95% interval | Block (ps) | Stationarity diagnostic |",
            "|---|---|---:|---|---:|---|"]
    for engine in ENGINES:
        properties = result["engines"][engine]["stages"]["production"]["properties"]
        for prop, item in properties.items():
            bounds = item.get("mean_95pct_interval")
            if "mean" not in item:
                continue
            text.append(f"| {engine.upper()} | {LABELS.get(prop, prop)} | {item['mean']:.8g} | "
                        f"{bounds[0]:.8g} to {bounds[1]:.8g} | {item['primary_block_ps']} | {item['stationarity']} |")
    text += ["", "## Full-stage assessment", "",
             "All raw samples, native clocks, declared stage origins, per-stage cumulative means, missing fields, "
             "and original initialization rows are in the engine CSVs. Density from mass/native box is explicitly "
             "derived; native logged density remains a different column. RMSD is a seven-heavy-backbone-atom fit "
             "to the unchanged canonical master after making the peptide whole through periodic boundaries.", "",
             "| Engine | Stage | Potential mean (kJ/mol) | Temperature mean (K) | Pressure mean (bar) | Cell density (g/cm³) | Backbone RMSD (Å) |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for engine in ENGINES:
        for stage in STAGES:
            props = result["engines"][engine]["stages"][stage]["properties"]
            values = [f"{props[p]['mean']:.7g}" if "mean" in props[p] else "unavailable" for p in PROPERTIES]
            text.append(f"| {engine.upper()} | {stage.upper()} | " + " | ".join(values) + " |")
    text += ["", "NVT temperature startup and NPT volume relaxation are assessed transients, not production samples. "
             "All four NVT boxes stay near 0.74646 g/cm³ by construction. NPT brings density toward the production "
             "range, so averaging its entire 100 ps as if already stationary is inappropriate. Fixed first-10-ps "
             "and last-20-ps summaries are retained for every observable; no equilibration cutoff was selected.", "",
             "| Engine | Last 20 ps NPT temperature (K) | Last 20 ps NPT pressure (bar) | Last 20 ps NPT density (g/cm³) |",
             "|---|---:|---:|---:|"]
    for engine in ENGINES:
        props = result["engines"][engine]["stages"]["npt"]["properties"]
        values = [f"{props[p]['fixed_endpoint_windows']['last_20_ps_mean']:.7g}" for p in ("temperature_K", "pressure_bar", "density_cell_g_cm3")]
        text.append(f"| {engine.upper()} | " + " | ".join(values) + " |")
    text += ["", "## Cross-engine mean comparisons", "",
             "Each contrast resamples the two finite trajectories separately. The intervals below are pointwise "
             "and unadjusted across engine/property comparisons; they are not a formal equivalence test. "
             "All pressure-contrast intervals include zero, but that does not identify equal pressure estimators. "
             "Several density and potential contrasts are resolved under this conditional calculation.", "",
             "| Left minus right | Observable | Mean difference | Pointwise conditional 95% interval |",
             "|---|---|---:|---|"]
    for contrast in result["ensemble_mean_comparisons"]:
        if contrast["property"] not in ("potential_kJ_mol", "pressure_bar", "density_cell_g_cm3"):
            continue
        bounds = contrast["conditional_95pct_interval"]
        text.append(f"| {contrast['left'].upper()} − {contrast['right'].upper()} | {LABELS[contrast['property']]} | "
                    f"{contrast['mean_difference_left_minus_right']:.7g} | {bounds[0]:.7g} to {bounds[1]:.7g} |")
    text += ["", "The approximately 156–172 kJ/mol potential separation between GROMACS/AMBER and "
             "NAMD/LAMMPS means is larger than the approximately 4.71 kJ/mol canonical-coordinate static range. "
             "Static Coulomb/tail conventions therefore cannot simply be cited as a complete explanation. "
             "Finite-step integrator/constraint/barostat effects and differing sampled configurations are plausible "
             "but unseparated contributors. This post-processing neither identifies a unique cause nor repairs it. "
             "Native potential means must not be confused with total energies or compared by silently replacing "
             "AMBER's printed kinetic temperature."]
    text += ["", "## Circular sampling diagnostics", "",
             "| Engine | Angle | Integrated τ (ps) | g | Estimated N_eff / 1000 | Integrated last lag (ps) |",
             "|---|---|---:|---:|---:|---:|"]
    for engine in ENGINES:
        for angle in ("phi", "psi"):
            item = result["engines"][engine]["circular_correlation"][angle]
            text.append(f"| {engine.upper()} | {angle} | {item['tau_int_ps']:.5g} | {item['g']:.5g} | {item['Neff']:.5g} | {item['last_integrated_lag_ps']:.5g} |")
    text += ["", "Correlation uses the centered trace covariance of (cos θ, sin θ), not arithmetic wrapped angles. "
             "Geyer positive monotonized pairs define g; τ = g Δt/2 and N_eff = N/g. Individual sine/cosine results "
             "and half-trajectory sensitivities are in the JSON. These estimates characterize only visited motion: "
             "an unvisited basin has no measured escape time. The reversible-chain theorem is not a proof for "
             "these finite-step MD processes. Scalar apparent stationarity does not establish conformational equilibrium.", "",
             "## Observable-specific limits", "",
             "- AMBER NVT pressure is unavailable: ntp=0, only zero PRESS placeholders, no original virial or "
             "validated replay. Its zero-valued records are preserved separately and never used as pressure measurements.",
             "- AMBER's original lower printed temperature is retained. It is the source-traced midpoint "
             "kinetic estimator, whereas saved-current-velocity temperature uses native scale 20.455, "
             "KB = 8.31441/4184, and 13,206 DOF. The latter is an explicitly derived observable, not a replacement. "
             "One-ps velocity output cannot reproduce adjacent two-fs midpoint velocities.",
             "- Native pressure definitions differ: NAMD group pressure (atomic also retained), AMBER molecular "
             "virial, and native GROMACS/LAMMPS conventions. Instantaneous pressure is strongly fluctuating; "
             "agreement of noisy means is not identity of estimators.",
             "- Absolute potentials contain the documented Coulomb conversion, mesh and dispersion-tail offsets. "
             "A conditional difference between independent trajectory means is not a force-field equivalence test. "
             "No offsets were fitted away.",
             "- The LAMMPS restart at production 592 ps is not an extra sample. Both original boundary rows and "
             "the narrowly hash-bound SHAKE projection proof are retained. RNG state was not serialized; "
             "initialization pressure/PME splitting changes are not a physical time evolution.",
             "- Temperature, pressure, energy and density stationarity are distinct questions from slow peptide "
             "basin mixing. No multiple-comparison-corrected global pass threshold is implied by the per-property flags.", "",
             "## Density reference and reproducibility", "",
             "See EQUILIBRATION.md for primary literature and water-variant/cutoff caveats. The user's "
             "0.985 g/cm³ is a reference expectation, not an acceptance threshold. Actual topology parameters, "
             "solute fraction and production densities are in summary.json. Native inputs/images, all consumed "
             "file hashes, commands, software versions and output hashes are in receipt.json. All consumed frozen "
             "source hashes were checked again after analysis.", ""]
    (output / "REPORT.md").write_text("\n".join(text))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new output directory only; never inside delivery")
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260924)
    args = parser.parse_args()
    delivery = args.delivery.resolve(strict=True)
    output = args.output.resolve()
    if output == delivery or delivery in output.parents or output.exists():
        parser.error("output must be new and outside immutable delivery")
    if args.bootstrap_replicates < 200:
        parser.error("at least 200 bootstrap replicates required")
    output.mkdir(parents=True)
    started = time.monotonic()
    head = subprocess.run(["git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"], text=True, capture_output=True)
    receipt = {"status": "incomplete", "command": sys.argv, "scope": "CPU-only existing native data; no new dynamics or replay",
               "source_head": head.stdout.strip() if head.returncode == 0 else None,
               "source": [file_receipt(Path(__file__)), *[file_receipt(ANALYSIS / name) for name in ("compare.py", "geometry.py", "native.py", "thermo.py", "shake_boundary.py", "spec_paths.py")]],
               "software": {"python": sys.version, "platform": platform.platform(), "packages": {p: importlib.metadata.version(p) for p in ("numpy", "scipy", "MDAnalysis", "matplotlib", "ParmEd")}},
               "bootstrap_seed": args.seed, "bootstrap_replicates": args.bootstrap_replicates,
               "amber_nvt_pressure_source_review": AMBER_NVT_SOURCE_REVIEW}
    try:
        spec_path = delivery / "analysis-inputs/spec.json"
        spec = resolve_spec(json.loads(spec_path.read_text()), spec_path)
        runs = {run["engine"]: run for run in spec["runs"]}
        if set(runs) != set(ENGINES) or any(r["canonical_to_native"] != "identity" for r in runs.values()):
            raise ValidationError("exact four-engine canonical identity mapping required")
        stages = {engine: stage_specs(delivery, runs[engine]) for engine in ENGINES}
        source_paths = {spec_path}
        for path in (delivery / "master").iterdir():
            if path.is_file():
                source_paths.add(path)
        for engine in ENGINES:
            run = runs[engine]
            source_paths.add(Path(run["native_topology"]))
            source_paths.update(Path(p) for p in run["provenance_files"])
            for item in stages[engine]:
                for key in ("trajectory", "thermo", "log", "control", "velocities", "wrapper"):
                    value = item.get(key, [])
                    source_paths.update(Path(p) for p in (value if isinstance(value, list) else [value]))
                if "restart_boundary_policy" in item:
                    source_paths.add(Path(item["restart_boundary_policy"]["evidence_path"]))
        for name in ("TEMPERATURE_ESTIMATORS.md", "temperature-diagnostic-03.json", "STATIC-REPORT-20260923.md", "dispersion-diagnostic-01.json"):
            source_paths.add(delivery / "diagnostics" / name)
        receipt["inputs"] = [file_receipt(path) for path in sorted(source_paths)]
        master = master_data(delivery / "master")
        validate_protocol(master["protocol"])
        result = {"status": "incomplete", "input_cohort": spec["cohort"], "water_identity": water_identity(master),
                  "common_stage_clock": "NVT 0–100 ps; NPT 100–200 ps; production 200–1200 ps; native clocks separately retained",
                  "running_mean_policy": "reset each stage; exclude initialization time 0; never interpolate missing fields",
                  "engines": {}, "ensemble_mean_comparisons": [],
                  "equilibrium_proven": False}
        series, bootstraps = {}, {}
        rng = np.random.default_rng(args.seed)
        for engine in ENGINES:
            print(f"Extracting {engine}: real NVT, NPT and production", flush=True)
            directory = output / engine
            directory.mkdir()
            run = runs[engine]
            item = {"image": run["image"], "native_provenance": run["provenance"], "stages": {}}
            series[engine], bootstraps[engine] = {}, {}
            all_rows = []
            for stage in stages[engine]:
                name = stage["stage"]
                thermo, thermo_info = stage_thermo(stage, engine, float(master["u"].atoms.masses.sum()))
                geometry, geometry_info = trajectory_series(stage, engine, master, run)
                if [b["step"] for b in thermo_info["restart_boundaries"]] != [b["step"] for b in geometry_info["restart_boundaries"]]:
                    raise ValidationError("native thermo/coordinate restart boundaries differ")
                write_csv(directory / f"{name}-native-thermo.csv", thermo)
                write_csv(directory / f"{name}-native-geometry.csv", geometry)
                joined = join_observations(thermo, geometry, stage)
                series[engine][name] = joined
                all_rows.extend(joined)
                info = {"clock_and_sources": stage, "thermo_observations": thermo_info,
                        "trajectory_observations": geometry_info, "properties": {}}
                for prop in (*PROPERTIES, "saved_velocity_temperature_K"):
                    if prop == "saved_velocity_temperature_K" and engine != "amber":
                        continue
                    nonzero = [r for r in joined if r["stage_time_ps"] > 0]
                    available = [r[prop] for r in nonzero if prop in r]
                    if not available:
                        info["properties"][prop] = {"status": "unavailable", "n": 0, "reason": thermo_info["pressure"] if prop == "pressure_bar" else "not present"}
                    elif len(available) != len(nonzero):
                        raise ValidationError("partial dynamic field coverage is not silently averaged")
                    else:
                        diag, boot = scalar_diagnostics(available, rng, args.bootstrap_replicates)
                        info["properties"][prop] = diag
                        if name == "production":
                            bootstraps[engine][prop] = boot
                item["stages"][name] = info
            write_csv(directory / "all-stages.csv", all_rows)
            item["circular_correlation"] = {}
            for angle in ("phi", "psi"):
                values = [r[f"{angle}_degrees"] for r in series[engine]["production"] if r["stage_time_ps"] > 0]
                corr = circular_correlation(values)
                corr["first_500_ps"] = circular_correlation(values[:500])
                corr["second_500_ps"] = circular_correlation(values[500:])
                item["circular_correlation"][angle] = corr
                write_csv(directory / f"{angle}-autocorrelation.csv", [{"lag_ps": i, "circular_trace_acf": value,
                    "cos_acf": corr["cos_component"]["acf"][i], "sin_acf": corr["sin_component"]["acf"][i]} for i, value in enumerate(corr["acf"])])
            result["engines"][engine] = item
        for i, left in enumerate(ENGINES):
            for right in ENGINES[i + 1:]:
                for prop in PROPERTIES:
                    a, b = bootstraps[left][prop], bootstraps[right][prop]
                    bounds = interval(a - b)
                    means = [result["engines"][e]["stages"]["production"]["properties"][prop]["mean"] for e in (left, right)]
                    result["ensemble_mean_comparisons"].append({"left": left, "right": right, "property": prop,
                        "mean_difference_left_minus_right": means[0] - means[1], "conditional_95pct_interval": bounds,
                        "interval_contains_zero": contains_zero(bounds),
                        "interval_scope": "pointwise/unadjusted 95%; no multiplicity correction",
                        "scope": "independent within-trajectory block resampling; native estimators/finite-step algorithms can differ; not an equivalence test"})
        result["status"] = "completed diagnostics; no equilibrium certification"
        write_json(output / "summary.json", result)
        write_csv(output / "ensemble-mean-comparisons.csv", result["ensemble_mean_comparisons"])
        figures(result, series, output)
        report(result, output)
        notes = Path(__file__).with_name("EQUILIBRATION.md")
        if notes.exists():
            (output / notes.name).write_bytes(notes.read_bytes())
        after = [file_receipt(path) for path in sorted(source_paths)]
        if receipt["inputs"] != after:
            raise ValidationError("a consumed frozen source changed during analysis")
        receipt.update(status="passed", scientific_scope="complete available native observations; AMBER NVT pressure unavailable; stationarity and equilibrium are separate",
                       frozen_consumed_sources_unchanged=True, wall_seconds=time.monotonic() - started,
                       outputs=[file_receipt(path) for path in sorted(output.rglob("*")) if path.is_file()])
    except Exception as exc:
        receipt.update(status="failed", failure_type=type(exc).__name__, failure=str(exc), wall_seconds=time.monotonic() - started)
        write_json(output / "receipt.json", receipt)
        raise
    write_json(output / "receipt.json", receipt)
    print(json.dumps({"status": receipt["status"], "output": str(output), "wall_seconds": receipt["wall_seconds"]}), flush=True)


if __name__ == "__main__":
    main()
