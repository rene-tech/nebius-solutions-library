#!/usr/bin/env python3
"""Periodic umbrella analysis using native GROMACS WHAM, never a local solver.

Real observations and synthetic method validation are deliberately separate CLI
commands. Native WHAM reads original TPRs and degree-valued pull coordinates.
This module supplies validation, temporal block bootstrap and diagnostics.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

import numpy as np

R_KJ = 0.00831446261815324
DEFAULT_IMAGE = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs"
    "@sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643"
)
DEFAULT_GMX = "/usr/local/gromacs/avx2_256/bin/gmx"
SOURCES = [
    "https://manual.gromacs.org/2026.2/onlinehelp/gmx-wham.html",
    "https://manual.gromacs.org/2026.2/user-guide/mdp-options.html#mdp-pull-coord1-k",
    "https://raw.githubusercontent.com/gromacs/gromacs/v2026.2/src/gromacs/gmxana/gmx_wham.cpp",
]


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_json(obj):
    if isinstance(obj, np.ndarray):
        return clean_json(obj.tolist())
    if isinstance(obj, np.generic):
        return clean_json(obj.item())
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {str(k): clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (tuple, list)):
        return [clean_json(v) for v in obj]
    return obj


def write_json(path, value):
    with Path(path).open("x") as stream:
        json.dump(clean_json(value), stream, indent=2, allow_nan=False)
        stream.write("\n")


def file_record(path):
    path = Path(path).resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)}


def software_environment():
    return {"python": sys.version,
            "packages": dict(sorted((d.metadata["Name"], d.version) for d in importlib.metadata.distributions()))}


def wrap_degrees(value):
    """Canonical half-open circle, including exact +180 -> -180."""
    return (np.asarray(value, dtype=float) + 180.0) % 360.0 - 180.0


def harmonic_bias(phi_degrees, center_degrees, k_rad):
    delta = np.deg2rad(wrap_degrees(np.asarray(phi_degrees) - center_degrees))
    return 0.5 * k_rad * delta**2


def read_xvg(path):
    rows = []
    with Path(path).open() as stream:
        for line in stream:
            line = line.strip()
            if not line or line.startswith(("#", "@")):
                continue
            if line.startswith("&"):
                raise ValueError(f"Multiple XVG datasets are not supported: {path}")
            try:
                rows.append([float(v) for v in line.split()])
            except ValueError as exc:
                raise ValueError(f"Non-numeric XVG row in {path}") from exc
    if not rows or len({len(row) for row in rows}) != 1:
        raise ValueError(f"Empty or ragged XVG: {path}")
    data = np.array(rows, dtype=float)
    if not np.isfinite(data).all():
        raise ValueError(f"Non-finite values in {path}")
    return data


def select_production(data, start, end, dt, *, column=1):
    """Keep (start,end], not an extra time-zero observation; never infer units."""
    if data.ndim != 2 or data.shape[1] <= column or column < 1:
        raise ValueError("Missing requested pull coordinate column")
    if not 0 < dt <= end - start or not np.isfinite([start, end, dt]).all():
        raise ValueError("Invalid production interval or output cadence")
    if np.any(np.diff(data[:, 0]) <= 0):
        raise ValueError("Duplicate/nonmonotonic native times; join segments explicitly first")
    eps = max(dt * 0.001, 1e-6)
    selected = data[(data[:, 0] > start + eps) & (data[:, 0] <= end + eps)]
    expected = int(round((end - start) / dt))
    target = start + np.arange(1, expected + 1) * dt
    if len(selected) != expected or not np.allclose(selected[:, 0], target, atol=eps, rtol=0):
        raise ValueError(f"Incomplete production: expected {expected} equally spaced observations")
    if np.any(np.abs(selected[:, column]) > 180.0001):
        raise ValueError("Dihedral must be supplied in native degrees within [-180,180]")
    return selected.copy()


def normalize_profile(native, temperature, reference_index):
    """Mask empty bins; native WHAM -log does not turn zero probability into inf."""
    if native.ndim != 2 or native.shape[1] != 2 or len(native) < 2:
        raise ValueError("Expected two-column native WHAM probability output")
    x, raw = native.T
    if not np.isfinite(native).all() or np.any(raw < 0) or raw.sum() <= 0:
        raise ValueError("Invalid native WHAM probabilities")
    width = 360.0 / len(x)
    expected = -180 + (np.arange(len(x)) + 0.5) * width
    if not np.allclose(x, expected, atol=1e-4, rtol=0):
        raise ValueError("WHAM did not return the explicit full periodic angular grid")
    density = raw / (raw.sum() * width)
    pmf = np.full(len(x), np.nan)
    valid = density > 0
    pmf[valid] = -R_KJ * temperature * np.log(density[valid])
    if not valid[reference_index]:
        raise ValueError("Reference bin unsampled; no silent bootstrap gauge substitution")
    pmf[valid] -= pmf[reference_index]
    return x, density, pmf


def histogram_diagnostics(angles, centers, bins):
    edges = np.linspace(-180, 180, bins + 1)
    counts = np.array([np.histogram(wrap_degrees(a), bins=edges)[0] for a in angles])
    if np.any(counts.sum(axis=1) == 0):
        raise ValueError("An umbrella window has no observations")
    probabilities = counts / counts.sum(axis=1, keepdims=True)
    overlap = np.minimum(probabilities[:, None, :], probabilities[None, :, :]).sum(axis=2)
    bhattacharyya = np.sqrt(probabilities[:, None, :] * probabilities[None, :, :]).sum(axis=2)
    order = np.argsort(wrap_degrees(centers))
    neighbors = [
        {"a": int(i), "b": int(j), "overlap": float(overlap[i, j]),
         "bhattacharyya": float(bhattacharyya[i, j]), "crosses_periodic_seam": n == len(order) - 1}
        for n, (i, j) in enumerate(zip(order, np.roll(order, -1)))
    ]
    seen = {0}
    todo = [0]
    while todo:
        i = todo.pop()
        for j in np.flatnonzero(overlap[i] > 0):
            if int(j) not in seen:
                seen.add(int(j))
                todo.append(int(j))
    return counts, overlap, {
        "adjacent_windows_including_seam": neighbors,
        "minimum_adjacent_overlap": min(p["overlap"] for p in neighbors),
        "observed_support_graph_connected": len(seen) == len(angles),
        "unobserved_bin_indices": np.flatnonzero(counts.sum(axis=0) == 0).tolist(),
        "overlap_definition": "sum_b min(h_i(b)/N_i,h_j(b)/N_j); not an ESS or convergence test",
        "bhattacharyya_matrix": bhattacharyya.tolist(),
    }


def statistical_inefficiency(values):
    """FFT ACF with initial-positive adjacent-pair cutoff; diagnostic, not proof."""
    v = np.asarray(values, dtype=float)
    v = v - v.mean()
    if len(v) < 4 or np.dot(v, v) <= np.finfo(float).eps * len(v):
        return {"g": None, "reason": "constant_or_too_short", "samples": len(v)}
    nfft = 1 << (2 * len(v) - 1).bit_length()
    spectrum = np.fft.rfft(v, nfft)
    ac = np.fft.irfft(spectrum * spectrum.conj(), nfft)[:len(v)]
    ac /= np.arange(len(v), 0, -1)
    ac /= ac[0]
    positive_sum = 0.0
    last = 0
    # Pair positive-lag correlations. Do not integrate a noisy tail indefinitely.
    for j in range(1, min(len(v) - 1, len(v) // 2), 2):
        pair = float(ac[j] + ac[j + 1])
        if pair <= 0:
            break
        positive_sum += pair
        last = j + 1
    g = max(1.0, 1.0 + 2.0 * positive_sum)
    return {"g": g, "effective_samples_estimate": len(v) / g,
            "cutoff_lag_samples": last, "samples": len(v),
            "estimator": "FFT unbiased ACF, initial positive adjacent lag pairs; diagnostic only"}


def angular_mixing(angles, dt):
    radians = np.deg2rad(wrap_degrees(angles))
    observables = {"cos": np.cos(radians), "sin": np.sin(radians),
                   "positive_half_circle": (radians >= 0).astype(float)}
    estimates = {name: statistical_inefficiency(x) for name, x in observables.items()}
    valid_g = [x["g"] for x in estimates.values() if x["g"] is not None]
    half = len(radians) // 2
    first = np.histogram(wrap_degrees(angles[:half]), bins=24, range=(-180, 180))[0]
    last = np.histogram(wrap_degrees(angles[half:]), bins=24, range=(-180, 180))[0]
    tv = 0.5 * np.abs(first / first.sum() - last / last.sum()).sum()
    signs = observables["positive_half_circle"]
    return {"periodic_observables": estimates,
            "max_g_estimate": max(valid_g) if valid_g else None,
            "max_integrated_correlation_time_ps": dt * max(valid_g) / 2 if valid_g else None,
            "first_second_half_histogram_total_variation": float(tv),
            "half_circle_crossings": int(np.count_nonzero(np.diff(signs))),
            "positive_half_circle_occupancy": float(signs.mean()),
            "caveat": "Crossings can include recrossings; no crossing or constant occupancy cannot prove mixing."}


def block_resample_indices(n, block_samples, rng):
    """Circular temporal moving-block bootstrap, independently inside each window."""
    if not 1 <= block_samples <= n:
        raise ValueError("Block must contain at least one sample and not exceed the trajectory")
    starts = rng.integers(0, n, size=math.ceil(n / block_samples))
    indices = ((starts[:, None] + np.arange(block_samples)) % n).ravel()[:n]
    return indices, starts


class NativeGromacs:
    def __init__(self, output, *, image=DEFAULT_IMAGE, gmx=DEFAULT_GMX, input_dirs=(), timeout=300):
        self.output = Path(output).resolve()
        self.image, self.gmx, self.timeout = image, gmx, timeout
        self.input_dirs = sorted({str(Path(p).resolve()) for p in input_dirs})
        self.commands = []

    def run(self, args, directory, name):
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        command = [self.gmx, *map(str, args)]
        container_name = None
        if self.image:
            container_name = f"fs2-wham-{os.getpid()}-{uuid.uuid4().hex[:12]}"
            prefix = ["docker", "run", "--rm", "--network", "none", "--cpus", "2", "--memory", "2g",
                      "--name", container_name,
                      "--user", f"{os.getuid()}:{os.getgid()}", "-e", "OMP_NUM_THREADS=1",
                      "--mount", f"type=bind,src={self.output},dst={self.output}"]
            for path in self.input_dirs:
                if not Path(path).is_relative_to(self.output):
                    prefix.extend(["--mount", f"type=bind,src={path},dst={path},readonly"])
            command = [*prefix, "--workdir", str(directory), "--entrypoint", self.gmx, self.image, *map(str, args)]
        log = directory / f"{name}.log"
        begin = time.monotonic()
        with log.open("xb") as stream:
            try:
                result = subprocess.run(command, cwd=directory, stdout=stream, stderr=subprocess.STDOUT,
                                        check=False, timeout=self.timeout,
                                        env={**os.environ, "OMP_NUM_THREADS": "1", "GMX_MAXBACKUP": "-1"})
            except subprocess.TimeoutExpired:
                if container_name:
                    # The Docker CLI timeout alone need not stop its container.
                    # Only the uniquely named CPU analysis container created above is removed.
                    subprocess.run(["docker", "rm", "--force", container_name], stdout=stream,
                                   stderr=subprocess.STDOUT, timeout=30, check=False)
                self.commands.append({"argv": command, "status": "timeout", "log": file_record(log)})
                raise
        record = {"argv": command, "returncode": result.returncode,
                  "seconds": time.monotonic() - begin, "log": file_record(log)}
        self.commands.append(record)
        if result.returncode:
            raise RuntimeError(f"Native command failed ({result.returncode}); evidence: {log}")
        return log.read_text(errors="replace")

    def version(self):
        text = self.run(["--version"], self.output, "native-version")
        match = re.search(r"GROMACS version:\s*(\S+)", text)
        if not match:
            raise ValueError("Cannot identify the native GROMACS version")
        return {"image": self.image, "binary": self.gmx, "version": match.group(1),
                "version_log": file_record(self.output / "native-version.log")}

    def wham(self, tprs, observations, directory, *, bins, temperature, cyclic=True, selections=None):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "tpr-files.dat").write_text("".join(str(Path(p).resolve()) + "\n" for p in tprs))
        with tempfile.TemporaryDirectory(prefix="wham-pullx-", dir=self.output) as scratch_name:
            scratch = Path(scratch_name)
            paths, hashes = [], []
            for i, data in enumerate(observations):
                path = scratch / f"window-{i:02d}.xvg"
                # Chronological times are replaced only for resampled/selected rows.
                # No -ac: ordering does not affect native histogram WHAM.
                np.savetxt(path, np.column_stack([np.arange(len(data)), data[:, 1:]]), fmt="%.10g")
                paths.append(path)
                hashes.append({"window": i, "samples": len(data), "sha256": sha256(path)})
            (directory / "pullx-files.dat").write_text("".join(str(p) + "\n" for p in paths))
            args = ["wham", "-it", directory / "tpr-files.dat", "-ix", directory / "pullx-files.dat",
                    "-o", directory / "probability.xvg", "-hist", directory / "histograms.xvg",
                    "-min", "-180", "-max", "180", "-bins", bins,
                    "-temp", temperature, "-tol", "1e-8", "-b", "0", "-nolog", "-xvg", "none",
                    "-cycl" if cyclic else "-nocycl"]
            if selections is not None:
                (directory / "coordinate-selection.dat").write_text(
                    "".join(" ".join(map(str, row)) + "\n" for row in selections))
                args.extend(["-is", directory / "coordinate-selection.dat"])
            write_json(directory / "derived-inputs.json", {
                "kind": "deterministic_derived_pullx", "files": hashes,
                "removed_after_native_use": True,
                "reproduction": "Original input, row selection or saved bootstrap block starts, and source reproduce these bytes."})
            text = self.run(args, directory, "wham")
        if "Converged in " not in text:
            raise ValueError(f"Native WHAM convergence was not reported: {directory}")
        return read_xvg(directory / "probability.xvg")


def make_synthetic_tprs(runner, directory, centers, k_rad, *, monitor_psi=False):
    """Four neutral sites, preprocessing only: no synthetic data are MD results."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "system.top").write_text(
        "[ defaults ]\n1 2 no 1 1\n[ atomtypes ]\nX 12.0 0.0 A 0.3 0.0\n"
        "[ moleculetype ]\nTEST 0\n[ atoms ]\n" +
        "".join(f"{i} X 1 T X{i} {i} 0 12\n" for i in range(1, 6 if monitor_psi else 5)) +
        "[ system ]\nSynthetic WHAM metadata only\n[ molecules ]\nTEST 1\n")
    xyz = [(1.0, 1.0, 1.0), (1.1, 1.0, 1.0), (1.1, 1.1, 1.0), (1.2, 1.1, 1.1)]
    if monitor_psi:
        xyz.append((1.3, 1.2, 1.1))
    (directory / "system.gro").write_text(f"Synthetic WHAM metadata\n{len(xyz):5d}\n" + "".join(
        f"{1:5d}{'T':<5}{('X'+str(i)):>5}{i:5d}{x:8.3f}{y:8.3f}{z:8.3f}\n"
        for i, (x, y, z) in enumerate(xyz, 1)) + " 5.0 5.0 5.0\n")
    (directory / "groups.ndx").write_text("".join(f"[ G{i} ]\n{i}\n" for i in range(1, len(xyz) + 1)))
    tprs = []
    for i, center in enumerate(centers):
        mdp = directory / f"window-{i:02d}.mdp"
        mdp.write_text(
            "integrator = md\nnsteps = 0\ndt = 0.002\ncutoff-scheme = Verlet\n"
            "rlist = 1.0\ncoulombtype = Cut-off\nrcoulomb = 1.0\nvdwtype = Cut-off\n"
            "rvdw = 1.0\nconstraints = none\npbc = xyz\ntcoupl = no\npcoupl = no\n"
            "comm-mode = none\nld-seed = 12345\npull = yes\n" +
            f"pull-ngroups = {len(xyz)}\npull-ncoords = {2 if monitor_psi else 1}\n" +
            "".join(f"pull-group{i}-name = G{i}\n" for i in range(1, len(xyz) + 1)) +
            "pull-coord1-type = umbrella\npull-coord1-geometry = dihedral\n"
            "pull-coord1-groups = 1 2 2 3 3 4\npull-coord1-start = no\n"
            f"pull-coord1-init = {center}\npull-coord1-rate = 0\npull-coord1-k = {k_rad}\n"
            "pull-print-com = no\npull-print-ref-value = no\npull-print-components = no\n" +
            ("pull-coord2-type = umbrella\npull-coord2-geometry = dihedral\n"
             "pull-coord2-groups = 2 3 3 4 4 5\npull-coord2-start = no\n"
             "pull-coord2-init = 0\npull-coord2-rate = 0\npull-coord2-k = 0\n" if monitor_psi else ""))
        tpr = directory / f"window-{i:02d}.tpr"
        runner.run(["grompp", "-f", mdp, "-c", directory / "system.gro", "-p", directory / "system.top",
                    "-n", directory / "groups.ndx", "-o", tpr, "-po", directory / f"window-{i:02d}-processed.mdp"],
                   directory, f"grompp-{i:02d}")
        tprs.append(tpr)
    return tprs


def synthetic_observations(centers, k_rad, temperature, bins, samples, potential):
    """Deterministic discrete quadrature counts from a known periodic PMF."""
    x = -180 + (np.arange(bins) + 0.5) * 360 / bins
    observations = []
    for center in centers:
        logp = -(potential(x) + harmonic_bias(x, center, k_rad)) / (R_KJ * temperature)
        weights = np.exp(logp - logp.max())
        weights /= weights.sum()
        exact = samples * weights
        counts = np.floor(exact).astype(int)
        count_left = samples - counts.sum()
        if count_left:
            counts[np.argsort(-(exact - counts), kind="stable")[:count_left]] += 1
        phi = np.repeat(x, counts)
        observations.append(np.column_stack([np.arange(samples), phi]))
    return observations


def add_synthetic_psi(data):
    # An arbitrary diagnostic column, not another bias. This tests -is 1 0.
    return [np.column_stack([rows, wrap_degrees(rows[:, 1] * 2 + 45)]) for rows in data]


def synthetic_validation(args):
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    runner = NativeGromacs(out, image=args.image, gmx=args.gmx)
    receipt = {"schema": "fs2-periodic-wham-validation/v1", "evidence_kind": "synthetic-method-validation",
               "status": "running", "real_md_results": False, "source": file_record(__file__),
               "sources": SOURCES, "analysis_environment": software_environment()}
    try:
        receipt["native"] = runner.version()
        centers = np.arange(-180, 180, 15, dtype=float)
        tprs = make_synthetic_tprs(runner, out / "metadata", centers, 200.0, monitor_psi=True)
        selections = [[1, 0] for _ in centers]
        cases = [("flat", lambda x: np.zeros_like(x)),
                 ("asymmetric-periodic", lambda x: 3 * (1 - np.cos(np.deg2rad(x + 60))) + 1.25 * np.sin(2 * np.deg2rad(x)))]
        validations = []
        for name, potential in cases:
            data = add_synthetic_psi(synthetic_observations(centers, 200.0, 300.0, 180, args.samples, potential))
            result = runner.wham(tprs, data, out / name, bins=180, temperature=300, selections=selections)
            x, density, pmf = normalize_profile(result, 300, 60)
            expected = potential(x) - potential(x)[60]
            error = pmf - expected
            validation = {"case": name, "samples_per_window": args.samples,
                          "rms_error_kj_mol": float(np.sqrt(np.mean(error**2))),
                          "max_error_kj_mol": float(np.max(np.abs(error))),
                          "all_bins_finite": bool(np.isfinite(pmf).all()),
                          "density_integral": float(density.sum() * 2),
                          "threshold_max_error_kj_mol": 0.06}
            validation["passed"] = validation["all_bins_finite"] and validation["max_error_kj_mol"] < 0.06
            np.savetxt(out / name / "comparison.csv", np.column_stack([x, pmf, expected, error]),
                       delimiter=",", header="phi_deg,native_pmf_kj_mol,analytic_pmf_kj_mol,error_kj_mol", comments="")
            validations.append(validation)
        # Same biased observations with noncyclic analysis must expose a boundary artifact.
        data = add_synthetic_psi(synthetic_observations(centers, 200.0, 300.0, 180, args.samples, cases[1][1]))
        result = runner.wham(tprs, data, out / "negative-noncyclic", bins=180, temperature=300, cyclic=False, selections=selections)
        x, _, pmf = normalize_profile(result, 300, 60)
        expected = cases[1][1](x) - cases[1][1](x)[60]
        noncyclic_error = float(np.max(np.abs(pmf - expected)))
        wrong_tprs = make_synthetic_tprs(runner, out / "negative-degree-force-constant-metadata", centers,
                                         200.0 * (np.pi / 180)**2, monitor_psi=True)
        result = runner.wham(wrong_tprs, data, out / "negative-degree-force-constant", bins=180,
                             temperature=300, selections=selections)
        _, _, wrong_pmf = normalize_profile(result, 300, 60)
        wrong_units_error = float(np.max(np.abs(wrong_pmf - expected)))
        replay_profiles = []
        replay_starts = []
        for repeat in range(2):
            rng = np.random.default_rng(20260924)
            replay, starts = [], []
            for rows in data:
                rows = rows[::10]
                indices, block_starts = block_resample_indices(len(rows), 100, rng)
                replay.append(rows[indices])
                starts.append(block_starts.tolist())
            replay_profiles.append(runner.wham(tprs, replay, out / f"bootstrap-replay-{repeat}", bins=180,
                                              temperature=300, selections=selections))
            replay_starts.append(starts)
        reproducible = replay_starts[0] == replay_starts[1] and np.array_equal(*replay_profiles)
        write_json(out / "bootstrap-replay-starts.json", {"seed": 20260924, "starts": replay_starts[0], "identical": reproducible})
        receipt.update({"tests": validations, "negative_nocycl_max_error_kj_mol": noncyclic_error,
                        "negative_nocycl_detected": noncyclic_error > 1,
                        "negative_degree_force_constant_max_error_kj_mol": wrong_units_error,
                        "negative_degree_force_constant_detected": wrong_units_error > 1,
                        "native_block_bootstrap_replay_identical": reproducible,
                        "unbiased_psi_monitor_selected_out": True,
                        "status": "passed" if all(v["passed"] for v in validations) and noncyclic_error > 1
                                  and wrong_units_error > 1 and reproducible else "failed"})
    except Exception as exc:
        receipt.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        receipt["commands"] = runner.commands
        receipt["recorded_at"] = datetime.now(timezone.utc).isoformat()
        write_json(out / "receipt.json", receipt)
    print(json.dumps({"status": receipt["status"], "receipt": str(out / "receipt.json")}))
    return 0 if receipt["status"] == "passed" else 1


def dump_value(text, key):
    match = re.search(r"^\s*" + re.escape(key) + r"\s*=\s*(\S+)", text, re.MULTILINE)
    if match is None:
        raise ValueError(f"Native TPR dump is missing {key}")
    return match.group(1)


def audit_tpr_dump(text, window, temperature):
    """Validate the actual native bias, not merely a sidecar's force constant."""
    if dump_value(text, "pull") != "true" or dump_value(text, "free-energy") != "no":
        raise ValueError("Expected ordinary fixed harmonic umbrella production")
    for key in ("pull-print-COM", "pull-print-ref-value", "pull-print-components", "pull-xout-average"):
        if dump_value(text, key) != "false":
            raise ValueError(f"This pullx contract requires {key}=false; do not silently remove columns/average observations")
    ncoords = int(dump_value(text, "pull-ncoords"))
    if ncoords not in (1, 2):
        raise ValueError("Expected phi umbrella and optional unbiased psi monitor")
    blocks = re.split(r"^\s*pull-coord \d+:\s*$", text, flags=re.MULTILINE)[1:]
    if len(blocks) != ncoords:
        raise ValueError("Cannot parse the native pull-coordinate definitions")
    for i, block in enumerate(blocks):
        if dump_value(block, "type") != "umbrella" or dump_value(block, "geometry") != "dihedral":
            raise ValueError("Only the audited native dihedral harmonic bias is supported")
        if float(dump_value(block, "rate")) != 0 or dump_value(block, "start") != "false":
            raise ValueError("Moving/starting-coordinate-relative umbrellas are not this fixed-window protocol")
        k = float(dump_value(block, "k"))
        if k != float(dump_value(block, "kB")):
            raise ValueError("Unexpected A/B force constant difference")
        if i == 0:
            if not math.isclose(k, window["force_constant_kj_mol_rad2"], rel_tol=1e-6):
                raise ValueError("TPR harmonic force constant disagrees with manifest (native kJ/mol/rad²)")
            center = float(dump_value(block, "init"))
            if abs(float(wrap_degrees(center - window["center_degrees"]))) > 1e-4:
                raise ValueError("TPR bias center disagrees with manifest")
        elif k != 0:
            raise ValueError("Psi monitor is biased; 1D phi WHAM cannot silently ignore its potential")
    match = re.search(r"^\s*ref-t:\s*(.+)$", text, re.MULTILINE)
    if not match or not all(math.isclose(float(t), temperature, abs_tol=1e-4) for t in match.group(1).split()):
        raise ValueError("TPR reference temperatures disagree with the WHAM temperature")
    step = float(dump_value(text, "dt"))
    output_interval = int(dump_value(text, "pull-nstxout")) * step
    if not math.isclose(output_interval, window["expected_dt_ps"], rel_tol=1e-5):
        raise ValueError("TPR pull output cadence disagrees with manifest")
    if int(dump_value(text, "nsteps")) * step + 1e-4 < window["production_end_ps"] - window["production_start_ps"]:
        raise ValueError("TPR does not request the full production interval")
    return {"coordinate_count": ncoords, "selection": [1] + [0] * (ncoords - 1),
            "native_force_constant_units": "kJ/mol/rad^2", "pullx_units": "degrees",
            "center_degrees": float(dump_value(blocks[0], "init")),
            "force_constant_kj_mol_rad2": float(dump_value(blocks[0], "k")),
            "output_dt_ps": output_interval, "psi_unbiased": ncoords == 2}


def resolve_file(base, item, field):
    path = (base / item[field]).resolve()
    if not path.is_file():
        raise ValueError(f"Missing {field}: {path}")
    expected = item.get(f"{field}_sha256")
    if expected and expected != sha256(path):
        raise ValueError(f"Input hash mismatch for {path}")
    return path


def load_manifest(path):
    path = Path(path).resolve()
    spec = json.loads(path.read_text())
    if spec.get("evidence_kind") != "real-native-md":
        raise ValueError("Real analysis requires evidence_kind='real-native-md'; use validate-synthetic separately")
    temperature = float(spec["temperature_k"])
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("Invalid analysis temperature")
    windows = spec["windows"]
    if len(windows) != 24 or len({w["id"] for w in windows}) != 24:
        raise ValueError("This requested cohort requires exactly 24 distinct windows; no missing-window substitution")
    centers = sorted(float(w["center_degrees"]) for w in windows)
    if not np.allclose(centers, np.arange(-180, 180, 15), atol=1e-5, rtol=0):
        raise ValueError("Expected unique 15-degree centers -180 through +165; +180 duplicates -180")
    for window in windows:
        if window.get("status") != "succeeded":
            raise ValueError(f"Window {window['id']} is not a successful complete native operation")
        if not window.get("operation_id"):
            raise ValueError("Real data require stable native operation provenance")
        if not math.isfinite(window["force_constant_kj_mol_rad2"]) or window["force_constant_kj_mol_rad2"] <= 0:
            raise ValueError("Invalid harmonic force constant")
        if not math.isclose(window["production_end_ps"] - window["production_start_ps"], 2000, abs_tol=1e-5):
            raise ValueError("This cohort requires the full requested 2 ns/window, not a synthetic or short substitute")
        window["tpr_path"] = resolve_file(path.parent, window, "tpr")
        window["pullx_path"] = resolve_file(path.parent, window, "pullx")
    unbiased = spec.get("unbiased")
    if not unbiased:
        raise ValueError("Supply the frozen original GROMACS 1 ns frames.csv for the requested overlay")
    unbiased["path"] = resolve_file(path.parent, unbiased, "frames_csv")
    return spec


def load_unbiased(path, bins, temperature, reference_index):
    with Path(path).open() as stream:
        rows = list(csv.DictReader(stream))
    rows = [r for r in rows if 0 < float(r["production_time_ps"]) <= 1000.0001]
    times = np.array([float(r["production_time_ps"]) for r in rows])
    if len(times) != 1000 or not np.allclose(times, np.arange(1, 1001), atol=1e-4, rtol=0):
        raise ValueError("Unbiased overlay must be the original full 1 ns GROMACS series at 1 ps cadence")
    phi = np.array([float(r["phi_degrees"]) for r in rows])
    if not np.isfinite(phi).all():
        raise ValueError("Invalid unbiased phi observations")
    counts = np.histogram(wrap_degrees(phi), bins=bins, range=(-180, 180))[0]
    x = -180 + (np.arange(bins) + 0.5) * 360 / bins
    if counts[reference_index] > 0:
        _, density, pmf = normalize_profile(np.column_stack([x, counts]), temperature, reference_index)
        gauge = "same fixed phi reference bin as umbrella PMF"
    else:
        density = counts / (counts.sum() * (360 / bins))
        pmf = np.full(bins, np.nan)
        gauge = "reference bin unsampled: PMF overlay unavailable, probability overlay retained"
    return density, pmf, {"source": file_record(path), "frames": len(phi), "time_interval_ps": [1, 1000],
                          "empty_bin_indices": np.flatnonzero(counts == 0).tolist(),
                          "gauge": gauge, "pseudocounts": False,
                          "mixing": angular_mixing(phi, 1),
                          "scope": "Single historical unbiased 1 ns realization, not a reference equilibrium distribution"}


def plot_results(out, x, pmf, lower, upper, density, unbiased_density, unbiased_pmf,
                 counts, centers, overlap, psi_histograms, split_profiles):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    axes[0].plot(x, pmf, label="Native periodic WHAM (2 ns/window)")
    axes[0].fill_between(x, lower, upper, alpha=0.25, label="Temporal block bootstrap 95% interval")
    axes[0].plot(x, unbiased_pmf, "--", label="Original unbiased 1 ns; missing bins left blank")
    axes[0].set_ylabel("PMF relative to fixed reference (kJ/mol)")
    axes[0].legend(fontsize=8)
    axes[1].plot(x, density, label="Reweighted umbrella density")
    axes[1].plot(x, unbiased_density, label="Original unbiased 1 ns density", alpha=0.8)
    axes[1].set(xlabel="φ (degrees); −180 and +180 are the same point", ylabel="Probability density (degree⁻¹)")
    axes[1].legend(fontsize=8)
    fig.suptitle("One-dimensional φ projection; orthogonal ψ equilibration is not established")
    fig.tight_layout()
    fig.savefig(out / "phi-pmf-and-unbiased-overlay.png", dpi=160)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(10, 5))
    for values, label in [(pmf, "Full 2 ns/window"), (split_profiles[0], "First 1 ns/window"),
                          (split_profiles[1], "Second 1 ns/window")]:
        ax.plot(x, values, label=label)
    ax.set(xlabel="φ (degrees)", ylabel="PMF relative to same fixed reference (kJ/mol)",
           title="Time-split consistency; agreement is not proof of orthogonal equilibration")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "phi-time-split.png", dpi=160)
    plt.close(fig)
    order = np.argsort(centers)
    fig, axes = plt.subplots(1, 2, figsize=(13, 6))
    hist = counts / counts.sum(axis=1, keepdims=True)
    im = axes[0].imshow(hist[order], aspect="auto", origin="lower", extent=(-180, 180, -0.5, len(centers) - 0.5))
    axes[0].set(xlabel="Observed φ (degrees)", ylabel="Umbrella center (degrees)")
    axes[0].set_yticks(np.arange(len(centers)), labels=np.array(centers)[order].astype(int), fontsize=7)
    fig.colorbar(im, ax=axes[0], label="Per-window bin probability")
    im = axes[1].imshow(overlap[np.ix_(order, order)], origin="lower", vmin=0, vmax=1)
    axes[1].set(xlabel="Window in angular order", ylabel="Window in angular order", title="Observed histogram overlap")
    fig.colorbar(im, ax=axes[1], label="Σ min(pᵢ,pⱼ)")
    fig.tight_layout()
    fig.savefig(out / "window-overlap.png", dpi=160)
    plt.close(fig)
    if psi_histograms:
        h = np.array(psi_histograms)[order]
        fig, axes = plt.subplots(1, 3, figsize=(15, 6), sharey=True)
        for i, ax in enumerate(axes[:2]):
            im = ax.imshow(h[:, i, :], origin="lower", aspect="auto", extent=(-180, 180, -0.5, len(centers)-0.5), vmin=0, vmax=h.max())
            ax.set(xlabel="Unrestrained ψ (degrees)", title=("First 1 ns", "Second 1 ns")[i])
            fig.colorbar(im, ax=ax)
        diff = h[:, 1] - h[:, 0]
        im = axes[2].imshow(diff, origin="lower", aspect="auto", extent=(-180, 180, -0.5, len(centers)-0.5),
                            cmap="coolwarm", vmin=-max(np.abs(diff).max(), 1e-6), vmax=max(np.abs(diff).max(), 1e-6))
        axes[2].set(xlabel="Unrestrained ψ (degrees)", title="Second minus first half")
        fig.colorbar(im, ax=axes[2])
        axes[0].set_ylabel("Window in angular order")
        fig.suptitle("ψ initialization memory and slow mixing can invalidate a converged-looking 1D φ PMF")
        fig.tight_layout()
        fig.savefig(out / "orthogonal-psi-mixing.png", dpi=160)
        plt.close(fig)


def analyze(args):
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    receipt = {"schema": "fs2-periodic-wham-analysis/v1", "evidence_kind": "real-native-md",
               "status": "running", "source": file_record(__file__), "sources": SOURCES,
               "scientific_convergence": "not-established", "customer_ready": False,
               "analysis_environment": software_environment()}
    runner = None
    input_records = []
    try:
        if args.bins < 12 or 360 % args.bins or args.bootstrap < 2 or args.block_ps <= 0:
            raise ValueError("Use an integral-degree periodic grid, >=2 bootstrap draws and a positive block length")
        spec = load_manifest(args.manifest)
        windows, temperature = spec["windows"], spec["temperature_k"]
        input_records = [file_record(args.manifest), file_record(spec["unbiased"]["path"])]
        for w in windows:
            input_records.extend([file_record(w["tpr_path"]), file_record(w["pullx_path"])])
        runner = NativeGromacs(out, image=args.image, gmx=args.gmx,
                              input_dirs=[w["tpr_path"].parent for w in windows])
        receipt["native"] = runner.version()
        observations, selections, diagnostics, psi_histograms = [], [], [], []
        for i, w in enumerate(windows):
            dump = runner.run(["dump", "-s", w["tpr_path"]], out / "tpr-audit", f"window-{i:02d}")
            audit = audit_tpr_dump(dump, w, temperature)
            raw = read_xvg(w["pullx_path"])
            if raw.shape[1] != audit["coordinate_count"] + 1:
                raise ValueError(f"Window {w['id']}: native pullx column count disagrees with audited TPR")
            data = select_production(raw, w["production_start_ps"], w["production_end_ps"], w["expected_dt_ps"])
            observations.append(data)
            selections.append(audit["selection"])
            phi_diag = angular_mixing(data[:, 1], w["expected_dt_ps"])
            diag = {"window": w["id"], "operation_id": w["operation_id"], "samples": len(data),
                    "native_tpr": audit, "phi": phi_diag}
            if data.shape[1] == 3:
                diag["psi"] = angular_mixing(data[:, 2], w["expected_dt_ps"])
                h = [np.histogram(wrap_degrees(part), bins=36, range=(-180, 180))[0]
                     for part in np.array_split(data[:, 2], 2)]
                psi_histograms.append([row / row.sum() for row in h])
            diagnostics.append(diag)
        if len({len(s) for s in selections}) != 1:
            raise ValueError("All windows must share the same coordinate/output layout")
        centers = [w["center_degrees"] for w in windows]
        counts, overlap, support = histogram_diagnostics([d[:, 1] for d in observations], centers, args.bins)
        receipt["overlap"] = support
        write_json(out / "window-diagnostics.json", diagnostics)
        np.savetxt(out / "window-bin-counts.csv", counts, delimiter=",", fmt="%d")
        np.savetxt(out / "window-overlap.csv", overlap, delimiter=",")
        if not support["observed_support_graph_connected"]:
            raise ValueError("Disconnected observed histogram support: relative basin free energies are not identified")
        tprs = [w["tpr_path"] for w in windows]
        reference_index = int((float(wrap_degrees(args.reference_deg)) + 180) / (360 / args.bins))
        native = runner.wham(tprs, observations, out / "full", bins=args.bins, temperature=temperature, selections=selections)
        x, density, pmf = normalize_profile(native, temperature, reference_index)
        split_profiles = []
        for half in range(2):
            data = [np.array_split(rows, 2)[half] for rows in observations]
            native = runner.wham(tprs, data, out / f"half-{half+1}", bins=args.bins, temperature=temperature, selections=selections)
            _, _, profile = normalize_profile(native, temperature, reference_index)
            split_profiles.append(profile)
        bootstrap_profiles, starts_all, failures = [], [], []
        # Separate child RNG per replicate: replay is stable under interruption and independent batching.
        seed_children = np.random.SeedSequence(args.seed).spawn(args.bootstrap)
        block_samples = [int(round(args.block_ps / w["expected_dt_ps"])) for w in windows]
        if any(n < 1 or n > len(data) for n, data in zip(block_samples, observations)):
            raise ValueError("Invalid temporal block length relative to available production")
        for rep, seed in enumerate(seed_children):
            rng = np.random.default_rng(seed)
            data, starts = [], []
            for rows, length in zip(observations, block_samples):
                indices, begin = block_resample_indices(len(rows), length, rng)
                data.append(rows[indices])
                starts.append(begin.tolist())
            starts_all.append(starts)
            directory = out / f"bootstrap-{rep:04d}"
            try:
                _, _, boot_support = histogram_diagnostics([d[:, 1] for d in data], centers, args.bins)
                if not boot_support["observed_support_graph_connected"]:
                    raise ValueError("Resampled histogram support is disconnected")
                native = runner.wham(tprs, data, directory, bins=args.bins, temperature=temperature, selections=selections)
                _, _, profile = normalize_profile(native, temperature, reference_index)
            except (ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                profile = np.full(args.bins, np.nan)
                failures.append({"replicate": rep, "error": f"{type(exc).__name__}: {exc}"})
            bootstrap_profiles.append(profile)
            if (rep + 1) % 20 == 0:
                print(f"Native WHAM bootstrap {rep+1}/{args.bootstrap}; failures={len(failures)}", flush=True)
        bootstrap = np.array(bootstrap_profiles)
        finite_all = np.isfinite(bootstrap).all(axis=0)
        lower = np.full(args.bins, np.nan)
        upper = np.full(args.bins, np.nan)
        lower[finite_all], upper[finite_all] = np.quantile(bootstrap[:, finite_all], [0.025, 0.975], axis=0)
        finite_fraction = np.isfinite(bootstrap).mean(axis=0)
        write_json(out / "bootstrap-resampling.json", {
            "method": "independent within-window circular temporal moving-block bootstrap",
            "seed": args.seed, "rng": "numpy PCG64 / SeedSequence.spawn, one child per replicate",
            "numpy_version": np.__version__, "block_ps": args.block_ps, "block_samples": block_samples,
            "window_order": [w["id"] for w in windows], "starts_by_replicate_and_window": starts_all,
            "index_rule": "concatenate (start + arange(block_samples)) % N and truncate to N",
            "failures": failures, "all_requested_replicates_retained": True})
        np.savez_compressed(out / "bootstrap-profiles.npz", phi_degrees=x, pmf_kj_mol=bootstrap)
        unbiased_density, unbiased_pmf, unbiased_receipt = load_unbiased(spec["unbiased"]["path"], args.bins, temperature, reference_index)
        np.savetxt(out / "phi-pmf.csv", np.column_stack([x, density, pmf, lower, upper, finite_fraction,
                                                       unbiased_density, unbiased_pmf, *split_profiles]), delimiter=",",
                   header="phi_degrees,density_per_degree,pmf_kj_mol,bootstrap95_low,bootstrap95_high,bootstrap_finite_fraction,unbiased_density,unbiased_pmf_kj_mol,first_half_pmf,second_half_pmf", comments="")
        plot_results(out, x, pmf, lower, upper, density, unbiased_density, unbiased_pmf,
                     counts, centers, overlap, psi_histograms, split_profiles)
        finite_split = np.isfinite(split_profiles).all(axis=0)
        difference = (split_profiles[1] - split_profiles[0])[finite_split]
        warnings = ["A 1D phi PMF does not prove equilibration in psi, solvent or other orthogonal coordinates.",
                    "Within-window bootstrap is conditional on visited states and misses unvisited metastable basins.",
                    "Temporal circular bootstrap wraps the observed time series; it does not create new transitions.",
                    "Original unbiased 1 ns is a finite-sampling overlay, not an equilibrium truth or independent validation."]
        for i, diag in enumerate(diagnostics):
            for coordinate in ("phi", "psi"):
                if coordinate in diag:
                    tau = diag[coordinate]["max_integrated_correlation_time_ps"]
                    if tau is not None and args.block_ps < 5 * tau:
                        warnings.append(f"{diag['window']} {coordinate}: chosen block {args.block_ps:g}ps <5× estimated IACT {tau:g}ps; uncertainty may be optimistic.")
            if "psi" not in diag:
                warnings.append(f"{diag['window']}: no orthogonal psi observations supplied.")
        if support["minimum_adjacent_overlap"] < 0.05:
            warnings.append("At least one adjacent-window overlap is <0.05 (descriptive warning, not a universal acceptance threshold).")
        if not finite_all.all():
            warnings.append("Intervals withheld where any requested bootstrap replicate is missing/nonfinite; no conditional NaN-ignoring interval.")
        receipt.update({"status": "completed-with-sampling-limitations" if not failures else "incomplete-bootstrap",
                        "analysis_completed": not failures, "window_count": len(windows), "production_ps_per_window": 2000,
                        "temperature_k": temperature, "bins": args.bins, "bounds_degrees": [-180, 180], "cyclic": True,
                        "reference_bin_center_degrees": float(x[reference_index]), "time_zero_excluded": True,
                        "point_estimator": "native GROMACS histogram WHAM, ordinary sample weights; no custom WHAM solver",
                        "native_autocorrelation_weighting": False,
                        "bootstrap": {"replicates_requested": args.bootstrap, "failures": failures, "seed": args.seed,
                                      "block_ps": args.block_ps, "blocks_per_window_approximately": 2000 / args.block_ps,
                                      "confidence_interval": "pointwise percentile 2.5–97.5%, fixed reference bin; not simultaneous",
                                      "bins_with_all_replicates_finite": int(finite_all.sum())},
                        "split_half": {"common_finite_bins": int(finite_split.sum()),
                                       "rms_pmf_difference_kj_mol": float(np.sqrt(np.mean(difference**2))),
                                       "max_abs_pmf_difference_kj_mol": float(np.max(np.abs(difference)))},
                        "psi_summary": {"monitored_windows": sum("psi" in d for d in diagnostics),
                                        "windows_without_observed_half_circle_crossings": [
                                            d["window"] for d in diagnostics if "psi" in d and d["psi"]["half_circle_crossings"] == 0],
                                        "maximum_first_second_half_histogram_total_variation": max(
                                            (d["psi"]["first_second_half_histogram_total_variation"] for d in diagnostics if "psi" in d), default=None),
                                        "caveat": "Half-circle crossings are diagnostics, not metastable-state transition counts."},
                        "unbiased_overlay": unbiased_receipt, "warnings": warnings})
        (out / "REPORT.md").write_text(
            "# Native periodic umbrella WHAM\n\n"
            f"Status: **{receipt['status']}**. Scientific convergence is **not established**.\n\n"
            f"24 real native windows × 2 ns, {temperature:g} K. Native GROMACS {receipt['native']['version']}; "
            f"{args.bins} bins on [-180,180), periodic nearest-image harmonic biases.\n\n"
            f"The reference bin is {x[reference_index]:g}°. Time zero was excluded. "
            "Zero-probability bins remain missing; no pseudocounts or interpolation.\n\n"
            f"Minimum adjacent overlap (including seam): {support['minimum_adjacent_overlap']:.4g}. "
            f"First/second-half PMF RMS difference: {receipt['split_half']['rms_pmf_difference_kj_mol']:.4g} kJ/mol.\n\n"
            f"Bootstrap: {args.bootstrap} draws, {args.block_ps:g} ps blocks, seed {args.seed}; "
            f"{len(failures)} failed draws retained. Intervals are pointwise and conditional on observed states.\n\n"
            "## Limitations\n\n" + "".join(f"- {warning}\n" for warning in warnings) +
            "\n## Reproduction\n\nOriginal inputs and source hashes, native command logs, TPR audits, "
            "bootstrap block starts and all native profiles are retained. The original frozen delivery was not modified.\n")
    except Exception as exc:
        receipt.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        receipt["inputs"] = input_records
        receipt["inputs_unchanged"] = all(Path(r["path"]).is_file() and sha256(r["path"]) == r["sha256"] for r in input_records)
        if not receipt["inputs_unchanged"]:
            receipt.update(status="failed", error="Input bytes changed during analysis")
        receipt["commands"] = runner.commands if runner else []
        receipt["recorded_at"] = datetime.now(timezone.utc).isoformat()
        receipt["outputs"] = [file_record(p) for p in sorted(out.rglob("*")) if p.is_file()]
        write_json(out / "receipt.json", receipt)
    print(json.dumps({"status": receipt["status"], "receipt": str(out / "receipt.json")}))
    return 0 if receipt.get("analysis_completed") and receipt["inputs_unchanged"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    synthetic = sub.add_parser("validate-synthetic", help="Native periodic/units validation; not MD")
    synthetic.add_argument("--output", required=True)
    synthetic.add_argument("--image", default=DEFAULT_IMAGE, help="Empty string uses a local native executable")
    synthetic.add_argument("--gmx", default=DEFAULT_GMX)
    synthetic.add_argument("--samples", type=int, default=100000)
    synthetic.set_defaults(func=synthetic_validation)
    real = sub.add_parser("analyze", help="Full24 real native2ns windows; no simulation performed")
    real.add_argument("--manifest", required=True)
    real.add_argument("--output", required=True)
    real.add_argument("--image", default=DEFAULT_IMAGE)
    real.add_argument("--gmx", default=DEFAULT_GMX)
    real.add_argument("--bins", type=int, default=180)
    real.add_argument("--bootstrap", type=int, default=200)
    real.add_argument("--block-ps", type=float, default=100)
    real.add_argument("--seed", type=int, default=20260924)
    real.add_argument("--reference-deg", type=float, default=-60)
    real.set_defaults(func=analyze)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
