#!/usr/bin/env python3
"""Read-only native alanine Ramachandran statistics; no equilibrium guarantee.

Uses the immutable delivery's strict native readers and SHAKE boundary policy,
not display coordinates or interpolated trajectories. All outputs are additive.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
from itertools import combinations
import json
import math
from pathlib import Path
import platform
import sys

import numpy as np

ENGINES = ("gromacs", "namd", "amber", "lammps")
BASINS = ("beta", "PPII", "alphaR", "alphaL", "residual", "beta_PPII")
BLOCKS = (5, 10, 20, 50, 100, 200)
R_KJ_MOL_K = 0.00831446261815324
TEMPERATURE_K = 300.0
CLIENT = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d"
METHOD = {
    "temperature_K": TEMPERATURE_K, "R_kJ_mol_K": R_KJ_MOL_K,
    "angle_interval": "[-180,180), degrees; all intervals half-open",
    "basins": {
        "beta": "-180 <= phi < -90; psi >= 60 or psi < -150",
        "PPII": "-90 <= phi < 0; psi >= 60 or psi < -150",
        "alphaR": "-180 <= phi < 0; -120 <= psi < 60",
        "alphaL": "0 <= phi < 180; -60 <= psi < 120",
        "residual": "complement of beta, PPII, alphaR and alphaL",
        "beta_PPII": "beta plus PPII; aggregate, not a sixth exclusive state",
    },
    "basin_interpretation": "Fixed geometric counting regions, not inferred metastable/committor basins or universal boundaries.",
    "histogram": "10 degree periodic bins, shared edges; probability mass per bin, no pseudocount or smoothing; zero bins masked",
    "free_energy": "F=-RT ln(P_bin); plotted delta F=F-min(F) separately for each engine, one common finite colour scale",
    "correlation": "Biased lag covariance (denominator N), centered cos/sin trace ACF for each circular angle, separate sin/cos and basin indicators; Geyer initial-positive paired sums monotonized, estimation capped at N/2 lag; full ACF retained diagnostically",
    "correlation_equations": "Gamma_k=rho_(2k)+rho_(2k+1); keep initial positive sequence and cumulative minimum; g=max(1,-1+2 sum Gamma); tau_int=g*dt/2; Neff=N/g",
    "correlation_limits": "Reversible stationary-chain estimator used diagnostically for MD; no proof of stationarity, reversibility, hidden-state mixing or convergence. Constant observables have undefined g/tau/Neff.",
    "block_bootstrap": "Fixed-length circular moving blocks, uniform starts, paired basin indicators, independent RNG streams by engine/block length; percentile intervals conditional on observed trajectory and stationarity",
    "block_lengths_ps": list(BLOCKS),
    "primary_block_rule": "Common smallest of 20/50/100/200 ps >= 5 times largest finite estimated tau_int among circular/component/basin diagnostics; if none, 200 ps explicitly inadequate",
    "zero_visit_policy": "Population observed zero is reported, but bootstrap CI, tau and sampling-time extrapolation are null, never [0,0]. No equilibrium upper bound inferred.",
    "comparison_family": "6 engine pairs x 6 basin summaries = 36 comparisons; nominal 95% and Bonferroni familywise-target intervals; no unadjusted significance headline",
    "practical_comparison_margin": 0.05,
    "window": "All positive native 1..1000 ps samples; no post-hoc burn-in exclusion; initial records retained separately",
}
SOURCES = [
    {"title": "Geyer (1992), Practical Markov Chain Monte Carlo", "doi": "10.1214/ss/1177011137", "url": "https://www2.stat.duke.edu/homeweb/scs/Courses/Stat376/Papers/GeyerStatSci1992.pdf", "use": "Initial positive/monotone paired autocovariance estimator and its reversible-chain assumptions."},
    {"title": "Geyer, author's initial-sequence documentation", "url": "https://www.stat.umn.edu/geyer/mcmc/library/mcmc/html/initseq.html", "use": "Exact paired-sum and asymptotic variance definitions."},
    {"title": "Grossfield and Zuckerman (2009), Quantifying uncertainty and sampling quality in biomolecular simulations", "doi": "10.1016/S1574-1400(09)00502-7", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC2865156/", "use": "Observable-specific uncertainty, block-size sensitivity, unvisited states and limited effective sample size."},
    {"title": "Politis and Romano (1991 technical report), A Circular Block-Resampling Procedure for Stationary Data", "url": "https://statistics.stanford.edu/technical-reports/circular-block-resampling-procedure-stationary-data", "use": "Circular block resampling for stationary correlated series; not a stationarity test."},
    {"title": "Holm (1979), A Simple Sequentially Rejective Multiple Test Procedure", "url": "https://www.ime.usp.br/~abe/lista/pdf4R8xPVzCnX.pdf", "use": "Familywise-error rationale and classical Bonferroni union bound; this code uses Bonferroni intervals, not Holm p-values."},
    {"title": "Drozdov, Grossfield and Pappu (2004), Role of Solvent in Determining Conformational Preferences of Alanine Dipeptide in Water", "doi": "10.1021/ja039051x", "url": "https://www2.stat.duke.edu/~scs/SimGroup/PappuSolventPPII.pdf", "use": "Representative alpha/beta/PPII angle nomenclature only. Its different force field/water model is not a population target; our rectangular boundaries are explicit analysis choices."},
]


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def csv_rows(path, rows):
    with Path(path).open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def wrap(angle):
    value = np.asarray(angle, dtype=float)
    if not np.isfinite(value).all():
        raise ValueError("nonfinite dihedral")
    return (value + 180.) % 360. - 180.


def indicators(phi, psi, phi_split=-90., psi_split=60.):
    phi, psi = wrap(phi), wrap(psi)
    if phi.shape != psi.shape or phi.ndim != 1:
        raise ValueError("paired one-dimensional angle arrays required")
    extended = (phi < 0) & ((psi >= psi_split) | (psi < -150))
    beta, ppii = extended & (phi < phi_split), extended & (phi >= phi_split)
    ar = (phi < 0) & (psi >= -120) & (psi < psi_split)
    al = (phi >= 0) & (psi >= -60) & (psi < 120)
    residual = ~(extended | ar | al)
    result = np.column_stack((beta, ppii, ar, al, residual, extended)).astype(float)
    if not np.all(result[:, :5].sum(axis=1) == 1):
        raise ValueError("basins overlap or omit a sample")
    return result


def histogram(phi, psi, width=10):
    if width <= 0 or 360 % width:
        raise ValueError("bin width must divide 360 degrees")
    edges = np.linspace(-180, 180, 360 // width + 1)
    counts = np.histogram2d(wrap(phi), wrap(psi), bins=(edges, edges))[0].astype(int)
    if not counts.sum():
        raise ValueError("empty angular histogram")
    probability = counts / counts.sum()
    energy = np.full(probability.shape, np.nan)
    energy[counts > 0] = -R_KJ_MOL_K * TEMPERATURE_K * np.log(probability[counts > 0])
    relative = energy - np.nanmin(energy)
    return edges, counts, probability, energy, relative


def acf(values):
    """Trace covariance of centered vectors; rotation invariant for cos/sin."""
    a = np.asarray(values, dtype=float)
    if a.ndim == 1:
        a = a[:, None]
    if a.ndim != 2 or len(a) < 4 or not np.isfinite(a).all():
        raise ValueError("finite scalar/vector series of at least four samples required")
    x = a - a.mean(axis=0)
    variance = float(np.sum(x * x) / len(x))
    if variance <= 1e-28:
        return None, variance
    # Zero-padded linear, not cyclic, autocorrelation. N denominator makes
    # the estimator positive-semidefinite and exposes finite-window tapering.
    nfft = 1 << (2 * len(x) - 1).bit_length()
    transform = np.fft.rfft(x, n=nfft, axis=0)
    covariance = np.fft.irfft(np.sum(transform.conj() * transform, axis=1), n=nfft)[:len(x)] / len(x)
    return covariance / variance, variance


def initial_monotone(rho, n, dt=1.):
    if rho is None:
        return {"g": None, "tau_int_ps": None, "Neff": None, "status": "undefined-constant-observable"}
    r = np.asarray(rho, dtype=float)
    if not np.isfinite(r).all() or len(r) < 2 or abs(r[0] - 1) > 1e-8 or dt <= 0:
        raise ValueError("invalid normalized ACF or interval")
    r = r[:min(len(r), n // 2 + 1)]
    paired = r[:len(r) // 2 * 2].reshape(-1, 2).sum(axis=1)
    stop = np.flatnonzero(paired <= 0)
    keep = int(stop[0]) if len(stop) else len(paired)
    monotone = np.minimum.accumulate(paired[:keep])
    raw_g = float(-1 + 2 * monotone.sum())
    g = max(1., raw_g)
    return {"g": g, "unclamped_g": raw_g, "tau_int_ps": g * dt / 2,
            "Neff": n / g, "positive_pairs": keep, "last_included_lag_ps": (2 * keep - 1) * dt if keep else 0,
            "reached_available_lag_limit": not len(stop), "maximum_estimation_lag_ps": (len(r) - 1) * dt,
            "status": "diagnostic-estimate-not-convergence-proof",
            "weak_effective_sample_size": n / g < 20, "conservative_antipersistence_clamp": raw_g < 1}


def correlations(phi, psi, states):
    values = {}
    for name, degrees in (("phi", phi), ("psi", psi)):
        rad = np.radians(wrap(degrees))
        values[name + "_circular"] = np.column_stack((np.cos(rad), np.sin(rad)))
        values[name + "_cos"] = np.cos(rad)
        values[name + "_sin"] = np.sin(rad)
    values.update({"basin_" + name: states[:, i] for i, name in enumerate(BASINS)})
    result, raw = {}, {}
    for name, series in values.items():
        rho, variance = acf(series)
        result[name] = {**initial_monotone(rho, len(phi)), "variance_or_trace": variance}
        raw[name] = rho
    return result, raw


def circular_bootstrap(values, length, repeats, seed):
    a = np.asarray(values, dtype=float)
    if a.ndim == 1:
        a = a[:, None]
    n = len(a)
    if not 1 <= length <= n or n % length or repeats < 1:
        raise ValueError("this bounded bootstrap requires a block length dividing N")
    doubled = np.concatenate((a, a))
    prefix = np.concatenate((np.zeros((1, a.shape[1])), np.cumsum(doubled, axis=0)))
    block_sums = prefix[np.arange(n) + length] - prefix[np.arange(n)]
    rng = np.random.default_rng(seed)
    result = np.empty((repeats, a.shape[1]))
    for first in range(0, repeats, 512):
        size = min(512, repeats - first)
        starts = rng.integers(0, n, size=(size, n // length))
        result[first:first + size] = block_sums[starts].sum(axis=1) / n
    return result


def population_interval(samples, observed, alpha=.05):
    if not 0 < observed < 1:
        return None
    return np.quantile(samples, [alpha / 2, 1 - alpha / 2]).tolist()


def basin_free_energy(p, reference_p, boot_p, boot_reference):
    """Keep zero bootstrap counts as unbounded/undefined, never discard them."""
    if not 0 < p < 1 or not 0 < reference_p <= 1:
        return {"delta_F_to_beta_PPII_kJ_mol": None, "interval_95": None,
                "reason": "unvisited or constant basin/reference; no finite free-energy bound inferred"}
    point = -R_KJ_MOL_K * TEMPERATURE_K * math.log(p / reference_p)
    a, b = np.asarray(boot_p), np.asarray(boot_reference)
    both_zero = (a == 0) & (b == 0)
    if np.any(both_zero):
        return {"delta_F_to_beta_PPII_kJ_mol": point, "interval_95": None,
                "both_zero_bootstrap_fraction": float(both_zero.mean()), "reason": "bootstrap ratio undefined in retained draws; no silent draw deletion"}
    with np.errstate(divide="ignore", invalid="ignore"):
        values = -R_KJ_MOL_K * TEMPERATURE_K * np.log(a / b)
    # Discrete empirical quantiles avoid interpolating between +/-infinity.
    bounds = np.quantile(values, [.025, .975], method="inverted_cdf")
    return {"delta_F_to_beta_PPII_kJ_mol": point,
            "interval_95": [float(x) if np.isfinite(x) else None for x in bounds],
            "lower_unbounded": bool(np.isneginf(bounds[0])), "upper_unbounded": bool(np.isposinf(bounds[1])),
            "zero_basin_bootstrap_fraction": float(np.mean(a == 0)), "zero_reference_bootstrap_fraction": float(np.mean(b == 0)),
            "interpretation": "Observed basin probability-ratio free energy; conditional block bootstrap, no bin-volume correction or convergence claim."}


def sample_runs(indicator):
    x = np.asarray(indicator, dtype=bool)
    starts = np.flatnonzero(x & ~np.r_[False, x[:-1]])
    ends = np.flatnonzero(x & ~np.r_[x[1:], False])
    return {"sampled_runs": len(starts), "entries_after_start": int(np.sum(starts > 0)),
            "exits_before_end": int(np.sum(ends < len(x) - 1)), "longest_sampled_run_ps": int(np.max(ends - starts + 1)) if len(starts) else 0,
            "left_censored": bool(x[0]), "right_censored": bool(x[-1]),
            "interpretation": "Runs at 1 ps sampling, not exact transition rates or independent visits."}


def sampling_requirement(p, g, error, current_ps=1000):
    if not 0 < p < 1 or g is None:
        return None
    neff = 1.959963984540054**2 * p * (1 - p) / error**2
    total = neff * g  # actual sample spacing is 1 ps
    return {"target_95_percent_halfwidth": error, "approximate_required_Neff": neff,
            "plug_in_total_ns": total / 1000, "plug_in_additional_ns": max(0., total - current_ps) / 1000,
            "assumptions": "Stationary visited-state Bernoulli CLT; p and g remain valid in longer runs. Optimistic planning scale, not a sufficient run-length or unvisited-basin bound."}


def js_divergence(p, q):
    p, q = np.asarray(p).ravel(), np.asarray(q).ravel()
    midpoint = (p + q) / 2
    def kl(a):
        positive = a > 0
        return float(np.sum(a[positive] * np.log(a[positive] / midpoint[positive])))
    return (kl(p) + kl(q)) / 2


def verify_delivery(delivery):
    manifest_path = delivery / "delivery-manifest.json"
    manifest = read(manifest_path)
    for item in manifest["files"]:
        path = delivery / item["path"]
        if (path.is_symlink() or not path.resolve().is_relative_to(delivery.resolve()) or not path.is_file()
                or path.stat().st_size != item["bytes"] or sha(path) != item["sha256"]):
            raise ValueError("frozen delivery inventory mismatch: " + item["path"])
    return {"manifest_sha256": sha(manifest_path), "verified_files": len(manifest["files"]),
            "verified_bytes": sum(x["bytes"] for x in manifest["files"]), "writes_to_delivery": False}


def native_angles(delivery, run, master, out):
    from native import frames, validate_timeline
    from geometry import make_whole, dihedral
    from MDAnalysis.lib.distances import calc_dihedrals
    paths = run["trajectory"] if isinstance(run["trajectory"], list) else [run["trajectory"]]
    policy, boundaries = None, []
    if run.get("restart_boundary_policy"):
        from shake_boundary import load_policy
        policy = load_policy(run["restart_boundary_policy"], run["image"], paths, run["native_topology"])
    if run["canonical_to_native"] != "identity":
        raise ValueError("this fixed comparison requires the validated canonical atom identity")
    rows, metadata, independent_error = [], [], 0.
    for frame in frames(run["trajectory"], run["engine"], .002, run.get("trajectory_format"), boundary_receipts=boundaries, boundary_policy=policy):
        if frame.positions.shape != (6598, 3):
            raise ValueError("native atom count differs")
        whole = make_whole(frame.positions[master["peptide"]], master["bonds"], frame.cell)
        angles = [dihedral(whole[master[key]]) for key in ("phi", "psi")]
        for value, key in zip(angles, ("phi", "psi")):
            independent = float(np.degrees(calc_dihedrals(*whole[master[key]])))
            independent_error = max(independent_error, abs(float(wrap(value - independent))))
        rows.append({"native_frame": frame.index, "native_time_ps": frame.time_ps, "native_step": frame.step,
                     "production_time_ps": frame.time_ps - run["production_origin_time_ps"], "phi_degrees": angles[0], "psi_degrees": angles[1]})
        frame.positions, frame.velocities = None, None
        metadata.append(frame)
    _, initial = validate_timeline(metadata, run["production_origin_step"], run["production_origin_time_ps"], 500000, 500, .002)
    old = np.genfromtxt(delivery / "analysis" / run["engine"] / "frames.csv", delimiter=",", names=True)
    if len(rows) != len(old):
        raise ValueError("recomputed frame count differs from frozen common analysis")
    previous_error = max(float(np.max(np.abs(wrap(np.asarray([r[key] for r in rows]) - old[key])))) for key in ("phi_degrees", "psi_degrees"))
    if previous_error > 1e-7 or independent_error > 1e-3:
        raise ValueError("native dihedral sign/unit/atom/order cross-check failed")
    csv_rows(out / "all-native-phi-psi.csv", rows)
    positive = rows[int(initial):]
    if len(positive) != 1000 or not np.allclose([r["production_time_ps"] for r in positive], np.arange(1, 1001), atol=.001, rtol=0):
        raise ValueError("not the complete 1..1000 ps production sample schedule")
    phi, psi = (np.asarray([r[key] for r in positive]) for key in ("phi_degrees", "psi_degrees"))
    state = indicators(phi, psi)
    for i, row in enumerate(positive):
        row["exclusive_basin"] = BASINS[int(np.flatnonzero(state[i, :5])[0])]
    csv_rows(out / "production-phi-psi-basins.csv", positive)
    return phi, psi, state, {"engine": run["engine"], "image": run["image"], "native_frames": len(rows), "positive_frames": 1000,
        "initial_frame": initial, "production_origin_step": run["production_origin_step"], "production_origin_time_ps": run["production_origin_time_ps"],
        "maximum_difference_from_frozen_dihedrals_degrees": previous_error, "maximum_difference_from_independent_MDA_dihedrals_degrees": independent_error,
        "closed_native_segment_boundaries": boundaries, "native_inputs": [{"path": str(Path(p).relative_to(delivery)), "bytes": Path(p).stat().st_size, "sha256": sha(p)} for p in [*paths, run["native_topology"]]],
        "atom_quadruplets_one_based": {key: (master["peptide"][master[key]] + 1).tolist() for key in ("phi", "psi")},
        "source_result_sha256": sha(delivery / "runs" / run["engine"] / "result.json")}


def analyze_engine(engine, phi, psi, state, out, repeats, seed):
    corr, acfs = correlations(phi, psi, state)
    csv_rows(out / "autocorrelations.csv", [{"lag_ps": i, **{k: None if v is None else float(v[i]) for k, v in acfs.items()}} for i in range(1000)])
    edges, counts, probability, energy, relative = histogram(phi, psi)
    grid = [{"phi_left_degrees": edges[i], "psi_left_degrees": edges[j], "count": int(counts[i, j]), "probability_mass": float(probability[i, j]),
             "F_kJ_mol": float(energy[i, j]) if counts[i, j] else None, "delta_F_kJ_mol": float(relative[i, j]) if counts[i, j] else None,
             "zero_bin_mask": not bool(counts[i, j]), "fewer_than_10_samples": bool(counts[i, j] < 10)} for i in range(36) for j in range(36)]
    csv_rows(out / "histogram-free-energy-10deg.csv", grid)
    mean = state.mean(axis=0)
    bootstraps, block_rows, basin_summary = {}, [], {}
    for j, name in enumerate(BASINS):
        p, diagnostic = float(mean[j]), corr["basin_" + name]
        run_info = sample_runs(state[:, j])
        rare = min(int(state[:, j].sum()), len(state) - int(state[:, j].sum())) < 20 or run_info["sampled_runs"] < 10
        basin_summary[name] = {"count": int(state[:, j].sum()), "population": p, "correlation": diagnostic, **sample_runs(state[:, j]),
            "zero_or_constant_warning": "No estimate of unseen opposite state, uncertainty or relaxation from a constant indicator." if p in (0, 1) else None,
            "few_visits_or_samples_warning": rare,
            "effective_successes_diagnostic": diagnostic["Neff"] * p if diagnostic["Neff"] is not None else None,
            "inference_quality": "unresolved-constant" if p in (0, 1) else "weak-few-visits-or-small-Neff" if rare or diagnostic["Neff"] < 20 else "conditional-single-trajectory",
            "sampling_for_absolute_5_percentage_points": sampling_requirement(p, diagnostic["g"], .05) if run_info["sampled_runs"] >= 3 else None,
            "sampling_for_relative_20_percent": sampling_requirement(p, diagnostic["g"], .2 * p) if p > 0 and run_info["sampled_runs"] >= 3 else None,
            "sampling_projection_caution": "No time extrapolation from fewer than three sampled runs. Other projections remain optimistic conditional scales; the warning flags are not coverage guarantees.",
            "block_sensitivity": {}}
    for length in BLOCKS:
        stream_seed = [seed, ENGINES.index(engine), length]
        boot = circular_bootstrap(state, length, repeats, stream_seed)
        bootstraps[length] = boot
        means = state.reshape(-1, length, len(BASINS)).mean(axis=1)
        for i, row in enumerate(means):
            block_rows.append({"block_length_ps": length, "block_index": i, "first_sample_ps": i * length + 1, "last_sample_ps": (i + 1) * length, **dict(zip(BASINS, row))})
        for j, name in enumerate(BASINS):
            info = basin_summary[name]
            info["block_sensitivity"][str(length)] = {"blocks_in_original": len(means), "bootstrap_seed": stream_seed,
                "bootstrap_percentile_95": population_interval(boot[:, j], mean[j]),
                "block_mean_standard_error": float(means[:, j].std(ddof=1) / np.sqrt(len(means))) if 0 < mean[j] < 1 else None,
                "few_original_blocks_warning": len(means) < 10,
                "free_energy_relative_to_beta_PPII": basin_free_energy(mean[j], mean[5], boot[:, j], boot[:, 5])}
    csv_rows(out / "nonoverlapping-block-means.csv", block_rows)
    np.savez_compressed(out / "bootstrap-populations.npz", **{f"block_{length}_ps": values for length, values in bootstraps.items()}, basin_order=np.asarray(BASINS))
    sensitivity = []
    for phi_split in (-100, -90, -80):
        for psi_split in (50, 60, 70):
            population = indicators(phi, psi, phi_split, psi_split).mean(axis=0)
            sensitivity.append({"phi_beta_PPII_split_degrees": phi_split, "psi_extended_alphaR_split_degrees": psi_split, **dict(zip(BASINS, population))})
    csv_rows(out / "basin-boundary-sensitivity.csv", sensitivity)
    for j, name in enumerate(BASINS):
        basin_summary[name]["definition_sensitivity_population_range"] = [min(r[name] for r in sensitivity), max(r[name] for r in sensitivity)]
        basin_summary[name]["half_trajectory_populations"] = [float(x) for x in state[:, j].reshape(2, 500).mean(axis=1)]
    summary = {"engine": engine, "basins": basin_summary, "correlation_diagnostics": corr,
        "histogram_coverage": {"total_bins": 1296, "visited_bins": int(np.count_nonzero(counts)), "unvisited_fraction": float(np.mean(counts == 0)),
                               "visited_bins_with_fewer_than_10_samples": int(np.sum((counts > 0) & (counts < 10)))},
        "free_energy_zero_offset_kJ_mol": float(np.nanmin(energy)), "free_energy_maximum_finite_kJ_mol": float(np.nanmax(relative)),
        "scientific_convergence_claimed": False}
    return summary, bootstraps, (edges, counts, probability, energy, relative)


def primary_block(summaries):
    taus = [entry["tau_int_ps"] for summary in summaries.values() for entry in summary["correlation_diagnostics"].values() if entry["tau_int_ps"] is not None]
    requested = 5 * max(taus)
    adequate = [b for b in (20, 50, 100, 200) if b >= requested]
    chosen = adequate[0] if adequate else 200
    return {"length_ps": chosen, "five_times_maximum_estimated_tau_ps": requested, "meets_estimated_rule": bool(adequate),
            "nonoverlapping_blocks": 1000 // chosen, "few_blocks_warning": 1000 // chosen < 10,
            "not_an_independence_or_stationarity_proof": True}


def agreement(summaries, boots, angles, length):
    rows, distances = [], []
    for a, b in combinations(ENGINES, 2):
        for j, basin in enumerate(BASINS):
            pa, pb = (summaries[e]["basins"][basin]["population"] for e in (a, b))
            delta = pa - pb
            ci, adjusted = None, None
            if 0 < pa < 1 and 0 < pb < 1:
                values = boots[a][length][:, j] - boots[b][length][:, j]
                ci = np.quantile(values, [.025, .975]).tolist()
                adjusted = np.quantile(values, [.05 / 36 / 2, 1 - .05 / 36 / 2]).tolist()
            verdict = "unresolved-constant-or-unvisited-indicator" if adjusted is None else (
                "conditional-interval-excludes-zero" if adjusted[0] > 0 or adjusted[1] < 0 else "difference-not-resolved-not-equivalence")
            rows.append({"engine_a": a, "engine_b": b, "basin": basin, "observed_population_difference_a_minus_b": delta,
                         "nominal_95_interval": ci, "Bonferroni_36_family_interval": adjusted, "conditional_verdict": verdict,
                         "conditional_interval_within_plus_minus_0.05": None if adjusted is None else adjusted[0] >= -.05 and adjusted[1] <= .05,
                         "caution": "One 1 ns trajectory per engine; stationary within-trajectory bootstrap only, not an independent-replica test of engine bias/equivalence."})
        for width in (5, 10, 15, 30):
            p, q = (histogram(*angles[e], width)[2] for e in (a, b))
            distances.append({"engine_a": a, "engine_b": b, "bin_width_degrees": width, "Jensen_Shannon_divergence_nats": js_divergence(p, q),
                              "total_variation_distance": float(np.abs(p - q).sum() / 2),
                              "interpretation": "Empirical shape distances, positively biased by sparse finite sampling; not a p-value."})
    return rows, distances


def plots(output, summaries, grids, length):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize
    vmax = max(float(np.nanmax(value[4])) for value in grids.values())
    fig, axes = plt.subplots(2, 2, figsize=(10, 9), layout="constrained")
    cmap = plt.colormaps["viridis"].copy()
    cmap.set_bad("#dddddd")
    for axis, engine in zip(axes.flat, ENGINES):
        edges, counts, p, energy, relative = grids[engine]
        artist = axis.pcolormesh(edges, edges, np.ma.masked_invalid(relative.T), cmap=cmap, norm=Normalize(0, vmax), rasterized=True)
        axis.set(title=f"{engine.upper()} · {np.count_nonzero(counts)}/1296 bins visited", xlabel="φ (degrees)", ylabel="ψ (degrees)", xticks=[-180, -90, 0, 90, 180], yticks=[-180, -90, 0, 90, 180], aspect="equal")
        axis.axvline(0, color="white", lw=.6, alpha=.5)
        axis.axvline(-90, ymin=2 / 3, color="white", lw=.6, alpha=.5)
        axis.hlines([-150, -120, 60], -180, 0, color="white", lw=.5, alpha=.5)
        axis.hlines([-60, 120], 0, 180, color="white", lw=.5, alpha=.5)
    fig.colorbar(artist, ax=axes, label="Observed ΔF = −RT ln(P/Pmax), kJ/mol · T = 300 K")
    fig.suptitle("Original 1 ns trajectories · unsmoothed 10° bins\nGray = unvisited, not an infinite measured barrier")
    fig.savefig(output / "free-energy-common-scale.png", dpi=180)
    fig.savefig(output / "free-energy-common-scale.pdf")
    plt.close(fig)
    fig, axes = plt.subplots(2, 3, figsize=(13, 7), layout="constrained")
    for axis, basin in zip(axes.flat, BASINS):
        for x, engine in enumerate(ENGINES):
            info = summaries[engine]["basins"][basin]
            p = info["population"]
            ci = info["block_sensitivity"][str(length)]["bootstrap_percentile_95"]
            if ci is None:
                axis.plot(x, p, "x", color="black")
                axis.annotate("unresolved", (x, p), xytext=(0, 7), textcoords="offset points", ha="center", fontsize=7)
            else:
                axis.errorbar(x, p, yerr=[[max(0., p - ci[0])], [max(0., ci[1] - p)]], fmt="o", capsize=4)
        axis.set(title=basin, xticks=range(4), xticklabels=[e[:3].upper() for e in ENGINES], ylabel="Observed population", ylim=(-.035, 1.07))
    fig.suptitle(f"Basin populations · conditional 95% circular-block bootstrap ({length} ps)\nUnvisited states have no estimated confidence interval")
    fig.savefig(output / "basin-populations.png", dpi=180)
    plt.close(fig)
    fig, axes = plt.subplots(2, 3, figsize=(13, 7), layout="constrained")
    for axis, basin in zip(axes.flat, BASINS):
        for engine in ENGINES:
            values = [summaries[engine]["basins"][basin]["block_sensitivity"][str(b)]["block_mean_standard_error"] for b in BLOCKS]
            axis.plot(BLOCKS, [np.nan if x is None else x for x in values], "o-", label=engine)
        axis.set(title=basin, xscale="log", xlabel="Nonoverlapping block length (ps)", ylabel="Block standard error")
    axes.flat[0].legend(fontsize=8)
    fig.suptitle("Block-size sensitivity · 200 ps leaves only 5 blocks\nNo automatic plateau/convergence claim")
    fig.savefig(output / "block-size-sensitivity.png", dpi=180)
    plt.close(fig)
    return {"colour_min_kJ_mol": 0, "colour_max_kJ_mol": vmax, "per_engine_minimum_shift": True, "zero_bins_masked": True}


def markdown_report(output, result):
    length = result["primary_block"]["length_ps"]
    lines = ["# Original four-engine 1 ns Ramachandran analysis", "", "The native data and statistical calculations passed their validation gates. This does **not** establish equilibrium convergence or engine equivalence. All 4,000 positive production frames were recomputed from native coordinates, not display files.", "", "## Basin populations", "", f"Conditional 95% circular-block bootstrap, common {length} ps blocks. `unresolved` means the observed indicator is constant; no zero-width confidence interval is asserted.", "", "| Basin | GROMACS | NAMD | AMBER | LAMMPS |", "| --- | --- | --- | --- | --- |"]
    for basin in BASINS:
        values = []
        for engine in ENGINES:
            info = result["engines"][engine]["basins"][basin]
            ci = info["block_sensitivity"][str(length)]["bootstrap_percentile_95"]
            values.append(f"{info['population']:.3f} " + (f"[{ci[0]:.3f}, {ci[1]:.3f}]" if ci else "(unresolved)"))
        lines.append("| " + " | ".join((basin, *values)) + " |")
    absent = [e for e in ENGINES if result["engines"][e]["basins"]["alphaL"]["count"] == 0]
    residual_counts = "/".join(str(result["engines"][e]["basins"]["residual"]["count"]) for e in ENGINES)
    lines += ["", f"The αL basin was unvisited in: {', '.join(absent) or 'none'}. Observed zeros are not evidence of zero equilibrium population. Residual frame counts in engine order are {residual_counts}; fewer than three sampled runs do not support a physical sampling forecast here.", "", "## Slow-state sampling diagnostics", "", "| Engine | αR sampled runs | αR τ_int (ps) | αR Neff | Conditional total ns for ±0.05 |", "| --- | ---: | ---: | ---: | ---: |"]
    for engine in ENGINES:
        info = result["engines"][engine]["basins"]["alphaR"]
        plan = info["sampling_for_absolute_5_percentage_points"]
        def number(value):
            return "unresolved" if value is None else f"{value:.2f}"
        lines.append(f"| {engine} | {info['sampled_runs']} | {number(info['correlation']['tau_int_ps'])} | {number(info['correlation']['Neff'])} | {number(plan['plug_in_total_ns'] if plan else None)} |")
    weak = [e for e in ENGINES if result["engines"][e]["basins"]["alphaR"]["correlation"].get("Neff") is None or result["engines"][e]["basins"]["alphaR"]["correlation"]["Neff"] < 20]
    lines += ["", f"These runs, τ values and sample-size projections are diagnostics of the visited sequence—not independent-transition counts or trustworthy rare-event kinetics. Engines with fewer than 20 or undefined effective αR samples: {', '.join(weak) or 'none'}. The predeclared five-τ block rule is {'met' if result['primary_block']['meets_estimated_rule'] else 'NOT met'}; the chosen length leaves {1000 // length} original blocks. Neither diagnostic proves independence."]
    lines += ["", "## What the uncertainty does and does not mean", "", f"The common block rule requested at least {result['primary_block']['five_times_maximum_estimated_tau_ps']:.2f} ps (five times the largest estimated integrated correlation time); chosen length is {length} ps, giving {1000 // length} nonoverlapping blocks. The 5, 10, 20, 50, 100 and 200 ps sensitivity outputs remain available. A finite-series correlation estimate or apparent block plateau cannot rule out unseen slow states.", "", "`receipt.json` uses g = 2τ_int/Δt and Neff = N/g consistently for circular trace ACFs, separate trigonometric components and state indicators. Constant indicators have null τ/Neff. All intervals assume sufficient stationarity/mixing within the observed trajectory and omit between-replica variability; one trajectory per engine cannot separate integration/model differences from stochastic undersampling. [Geyer](https://www.stat.umn.edu/geyer/mcmc/library/mcmc/html/initseq.html), [Grossfield and Zuckerman](https://pmc.ncbi.nlm.nih.gov/articles/PMC2865156/).", "", "## Inter-engine comparison", "", "`engine-agreement.json` contains every one of the 36 predeclared pair/basin comparisons with nominal and Bonferroni-family intervals; the combined β/PPII category overlaps its two components. An interval including zero is not proof of equality. Even a family-adjusted exclusion is conditional on the block-bootstrap assumptions, not proof of engine bias. Comparisons involving a constant/unvisited indicator are unresolved. Empirical histogram Jensen–Shannon and total-variation distances are reported at 5°, 10°, 15° and 30° and are not p-values. Multiple-comparison caution follows the union-bound rationale discussed by [Holm](https://www.ime.usp.br/~abe/lista/pdf4R8xPVzCnX.pdf).", "", "## Free-energy and basin conventions", "", "All surfaces use equal 10° bins and 300 K, with no smoothing or pseudocount. F = −RT ln(P_bin), and each surface's observed minimum is subtracted; the colour scale is shared. This is a finite-sample configurational free-energy estimate up to an arbitrary constant, not an absolute thermodynamic free energy. Gray bins are unvisited, not measured infinite barriers. Sparse visited bins are also noisy. Fixed rectangles and ±10° boundary sensitivity are fully specified in `method.json`; the geometric labels follow representative conformer nomenclature, not population targets from a different force field. [Drozdov, Grossfield and Pappu](https://www2.stat.duke.edu/~scs/SimGroup/PappuSolventPPII.pdf).", "", "## Sampling requirements", "", "Per visited basin, the report gives plug-in stationary-Bernoulli planning scales for a nominal 95% population half-width of 0.05 and of 20% of that basin's observed population. These use measured g and p but do not account for unknown slow modes, bias, or uncertainty in g itself. They are optimistic conditional scales, not run-length guarantees. They should motivate independent seeded replicas and reassessment rather than a declaration of sufficient sampling.", "", "For a basin with no visits, its equilibrium population, relaxation time, confidence interval and required physical simulation time are not identifiable here. `rare-state-planning.json` gives only hypothetical independent-sample calculations for assumed populations; it never substitutes these for observed confidence. Umbrella sampling is a separate campaign requiring overlap and orthogonal ψ-mixing checks, not an automatic cure for this limitation.", "", "## Reproducibility", "", "Native inputs, code hashes, exact client image, package versions, seed streams, 50k-default bootstrap replicates, all reconstructed phi/psi values, raw histograms, block means, bootstrap arrays and sensitivity outputs are retained. No frames were trimmed as burn-in; no native simulation or cloud write was performed. Circular block resampling is a stationary-series method ([Politis and Romano](https://statistics.stanford.edu/technical-reports/circular-block-resampling-procedure-stationary-data)); its assumptions are explicit, not tested away by resampling.", ""]
    (output / "REPORT.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260924)
    parser.add_argument("--bootstrap-replicates", type=int, default=50000)
    args = parser.parse_args()
    delivery, output = args.delivery.resolve(), args.output.resolve()
    if delivery == output or delivery in output.parents or output.exists():
        parser.error("output must be new and outside immutable delivery")
    if args.bootstrap_replicates < 2000:
        parser.error("at least 2000 bootstrap replicates required")
    output.mkdir(parents=True)
    save(output / "method.json", {**METHOD, "bootstrap_replicates": args.bootstrap_replicates, "bootstrap_seed": args.seed, "primary_sources": SOURCES})
    try:
        verification = verify_delivery(delivery)
        code = delivery / "analysis-inputs/code"
        sys.path.insert(0, str(code))
        from compare import master_data, validate_protocol
        from spec_paths import resolve_spec
        spec_file = delivery / "analysis-inputs/spec.json"
        spec = resolve_spec(read(spec_file), spec_file)
        if [r["engine"] for r in spec["runs"]] != list(ENGINES):
            raise ValueError("fixed four-engine original primary cohort required")
        master = master_data(spec["master_directory"])
        validate_protocol(master["protocol"])
        summaries, boots, grids, angles, native = {}, {}, {}, {}, {}
        for run in spec["runs"]:
            engine = run["engine"]
            directory = output / engine
            directory.mkdir()
            phi, psi, state, native[engine] = native_angles(delivery, run, master, directory)
            angles[engine] = (phi, psi)
            summaries[engine], boots[engine], grids[engine] = analyze_engine(engine, phi, psi, state, directory, args.bootstrap_replicates, args.seed)
            print(json.dumps({"engine": engine, "frames": 1000, "native_crosscheck": "passed", "populations": {b: summaries[engine]["basins"][b]["population"] for b in BASINS}}), flush=True)
        primary = primary_block(summaries)
        comparison, shape = agreement(summaries, boots, angles, primary["length_ps"])
        save(output / "engine-agreement.json", {"family_size": 36, "primary_block_ps": primary["length_ps"], "comparisons": comparison, "histogram_distances": shape})
        agreement_sensitivity = {}
        for length in BLOCKS:
            entries, _ = agreement(summaries, boots, angles, length)
            agreement_sensitivity[str(length)] = {"comparisons": entries,
                "family_adjusted_exclusions": sum(r["conditional_verdict"] == "conditional-interval-excludes-zero" for r in entries),
                "nominal_exclusions": sum(r["nominal_95_interval"] is not None and (r["nominal_95_interval"][0] > 0 or r["nominal_95_interval"][1] < 0) for r in entries)}
        save(output / "engine-agreement-block-sensitivity.json", {"warning": "Block sizes are sensitivity diagnostics, not independent hypothesis families to select after seeing results.", "block_lengths_ps": agreement_sensitivity})
        scenarios = [{"assumed_equilibrium_probability": p, "independent_samples_for_95_percent_chance_of_at_least_one_visit": math.ceil(math.log(.05) / math.log1p(-p)),
                      "independent_samples_for_95_percent_relative_20_percent_precision_CLT": 1.959963984540054**2 * (1 - p) / (.2**2 * p)} for p in (.1, .01, .001)]
        save(output / "rare-state-planning.json", {"kind": "hypothetical-not-inferred-from-zero-visits", "scenarios": scenarios, "physical_time_prediction": None,
             "limitations": "Unknown equilibrium probability and correlation/first-passage time prevent conversion to a defensible MD duration. No rule-of-three upper bound using 1000 correlated frames is claimed."})
        plot = plots(output, summaries, grids, primary["length_ps"])
        versions = {name: importlib.metadata.version(name) for name in ("numpy", "scipy", "MDAnalysis", "matplotlib")}
        result = {"schema": "fs2-original-alanine-ramachandran-statistics/v1", "status": "passed", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "scope": "CPU-only analysis/validation of actual original data; uncertainty conditional and convergence unproven", "delivery": str(delivery),
            "input_verification": verification, "method_sha256": sha(output / "method.json"), "bootstrap_seed": args.seed, "bootstrap_replicates": args.bootstrap_replicates,
            "primary_block": primary, "engines": summaries, "native_verification": native, "plot_scale": plot,
            "client_image": CLIENT, "environment": {"python": sys.version, "platform": platform.platform(), "packages": versions},
            "source": {"script_sha256": sha(Path(__file__)), "frozen_reader_files": {p.name: sha(p) for p in sorted(code.glob("*.py"))}, "spec_sha256": sha(spec_file)},
            "command": sys.argv, "scientific_convergence_claimed": False, "new_native_runs": 0, "cloud_writes": 0}
        markdown_report(output, result)
        result["outputs"] = [{"path": str(p.relative_to(output)), "bytes": p.stat().st_size, "sha256": sha(p)} for p in sorted(output.rglob("*")) if p.is_file()]
        save(output / "receipt.json", result)
        print(json.dumps({"status": "passed", "output": str(output), "primary_block_ps": primary["length_ps"], "receipt_sha256": sha(output / "receipt.json")}), flush=True)
    except Exception as exc:
        save(output / "failure.json", {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "source_sha256": sha(Path(__file__))})
        raise


if __name__ == "__main__":
    main()
