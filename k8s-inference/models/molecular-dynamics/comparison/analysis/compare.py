#!/usr/bin/env python3
"""Strict CPU-only analysis of the actual canonical alanine production files."""
import argparse
import csv
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

import numpy as np

from geometry import ValidationError, align_frame, dihedral, finite, make_whole
from native import frames, validate_timeline
from thermo import AMU_A3_TO_G_CM3, descriptive, native_performance, native_thermo, production_rows

ENGINES = ("gromacs", "namd", "amber", "lammps")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_receipt(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path)}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def source_identity():
    directory = Path(__file__).resolve().parent
    head = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"], capture_output=True, text=True)
    return {"head": head.stdout.strip() if head.returncode == 0 else None, "files": [file_receipt(path) for path in sorted(directory.glob("*.py"))]}


def pressure_observations(spec, trajectory_path, expected_times):
    """Join only actual, provenance-bound observations, never target pressure.

    The parent owns validation of the native virial/replay scientific method.
    This reader verifies transport, source binding, units, finite values and
    exact time coverage. Native original logs remain separate and unchanged.
    """
    if spec["trajectory_sha256"] != sha256(trajectory_path):
        raise ValidationError("pressure observations refer to a different trajectory")
    if not spec.get("method") or not spec.get("provenance_files"):
        raise ValidationError("pressure observation method and native provenance required")
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", spec["engine_image"]):
        raise ValidationError("pressure observation runtime digest required")
    with Path(spec["path"]).open(newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["production_time_ps", "pressure_bar"]:
            raise ValidationError("pressure CSV requires exact production_time_ps,pressure_bar columns")
        rows = list(reader)
    times = finite([float(row["production_time_ps"]) for row in rows], "pressure observation times")
    values = finite([float(row["pressure_bar"]) for row in rows], "pressure observations")
    if len(times) != len(expected_times) or not np.allclose(times, expected_times, atol=.001, rtol=0):
        raise ValidationError("pressure observations do not cover the native production frame schedule")
    provenance = {**spec, "files": [file_receipt(path) for path in [spec["path"], *spec["provenance_files"]]], "scope": "actual provided pressure observations; native virial/replay method acceptance remains parent gate"}
    return dict(zip(times, values)), provenance


def inventory():
    programs = {}
    for name in ("ffmpeg", "ffprobe", "blender", "vmd", "pymol", "povray"):
        binary = shutil.which(name)
        programs[name] = {"path": binary}
        if binary and name in ("ffmpeg", "ffprobe"):
            programs[name]["version"] = subprocess.check_output([binary, "-version"], text=True).splitlines()[0]
    packages = {}
    for name in ("numpy", "scipy", "MDAnalysis", "matplotlib", "Pillow", "pyvista", "vtk", "pyedr"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"python": sys.version, "executable": sys.executable, "platform": platform.platform(), "programs": programs, "packages": packages}


def master_data(directory):
    import MDAnalysis as mda
    from MDAnalysis.lib.mdamath import triclinic_vectors
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "master-manifest.json").read_text())
    for record in manifest["files"]:
        if sha256(directory / record["path"]) != record["sha256"]:
            raise ValidationError(f"master hash mismatch: {record['path']}")
    protocol = json.loads((directory / "protocol.json").read_text())
    u = mda.Universe(str(directory / "system.prmtop"), str(directory / "system.rst7"), format="RESTRT")
    if len(u.atoms) != manifest["atoms"]:
        raise ValidationError("master atom count mismatch")
    peptide = u.select_atoms("resname ACE ALA NME")
    if len(peptide) != manifest["peptide_atoms"] or list(peptide.residues.resnames) != ["ACE", "ALA", "NME"]:
        raise ValidationError("master is not the specified ACE-ALA-NME peptide")
    indices = peptide.indices
    local = {index: i for i, index in enumerate(indices)}
    bonds = np.array([(local[a], local[b]) for a, b in u.bonds.indices if a in local and b in local], dtype=int)
    fit = np.flatnonzero(peptide.elements != "H")
    waters = u.select_atoms("resname WAT and name O").indices
    if len(waters) != manifest["water_molecules"]:
        raise ValidationError("water oxygen count differs from master")
    if not np.isclose(float(u.atoms.charges.sum()), manifest["total_charge_e"], atol=1e-4):
        raise ValidationError("master charge mismatch")
    cell = triclinic_vectors(manifest["box_A_degrees"], dtype=np.float64)
    reference = make_whole(peptide.positions.copy(), bonds, cell)
    reference -= reference[fit].mean(axis=0)

    def atom(resname, name):
        selected = [i for i, a in enumerate(peptide) if a.resname == resname and a.name == name]
        if len(selected) != 1:
            raise ValidationError(f"ambiguous dihedral atom {resname}:{name}")
        return selected[0]

    phi = [atom("ACE", "C"), atom("ALA", "N"), atom("ALA", "CA"), atom("ALA", "C")]
    psi = [atom("ALA", "N"), atom("ALA", "CA"), atom("ALA", "C"), atom("NME", "N")]
    return {"directory": directory, "manifest": manifest, "protocol": protocol, "u": u, "peptide": indices, "bonds": bonds, "fit": fit, "waters": waters, "reference": reference, "phi": phi, "psi": psi, "elements": peptide.elements.tolist()}


def validate_protocol(protocol):
    required = {"production_steps": 500000, "output_every_steps": 500, "timestep_fs": 2.0, "nvt_steps": 50000, "npt_steps": 50000, "production_ensemble": "NPT", "force_field": "Amber ff14SB", "water_model": "TIP3P"}
    for key, value in required.items():
        if protocol.get(key) != value:
            raise ValidationError(f"canonical comparison protocol differs: {key}")


def analyze_run(run, master, output):
    engine = run["engine"]
    if engine not in ENGINES:
        raise ValidationError(f"unknown engine {engine}")
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", run["image"]):
        raise ValidationError("immutable runtime image digest required")
    files = [run["trajectory"], run["native_topology"], run["production_log"], run["thermo"]["path"], *run.get("provenance_files", [])]
    inputs = [file_receipt(path) for path in sorted(set(files))]
    protocol = master["protocol"]
    natoms = master["manifest"]["atoms"]
    dt = protocol["timestep_fs"] / 1000.
    steps, every = protocol["production_steps"], protocol["output_every_steps"]
    permutation = run["canonical_to_native"]
    if permutation == "identity":
        permutation = np.arange(natoms)
    else:
        permutation = np.asarray(permutation)
        if permutation.dtype.kind not in "iu" or not np.array_equal(np.sort(permutation), np.arange(natoms)):
            raise ValidationError("canonical_to_native must be a complete zero-based permutation")
    output.mkdir()
    display_peptide, display_water, metadata, rows = [], [], [], []
    densities, all_sources = [], set()
    # Store only peptide and oxygen display coordinates; validate every raw atom.
    for frame in frames(run["trajectory"], engine, dt, run.get("trajectory_format")):
        if frame.index > steps // every:
            raise ValidationError("extra trajectory frames beyond declared production")
        if frame.positions.shape != (natoms, 3):
            raise ValidationError(f"{engine} frame {frame.index} atom count differs")
        positions = frame.positions[permutation]
        peptide, solvent = align_frame(positions, frame.cell, master["peptide"], master["bonds"], master["fit"], master["reference"], master["waters"])
        # A broken atom mapping or missing PBC correction cannot be hidden by fit.
        bond_lengths = np.linalg.norm(peptide[master["bonds"][:, 0]] - peptide[master["bonds"][:, 1]], axis=1)
        if bond_lengths.min() < 0.6 or bond_lengths.max() > 2.2:
            raise ValidationError(f"{engine} invalid peptide bond distances in frame {frame.index}")
        density = float(master["u"].atoms.masses.sum() / np.linalg.det(frame.cell) * AMU_A3_TO_G_CM3)
        densities.append(density)
        rows.append({"native_frame": frame.index, "native_time_ps": frame.time_ps, "native_step": frame.step, "production_time_ps": frame.time_ps - run["production_origin_time_ps"], "phi_degrees": dihedral(peptide[master["phi"]]), "psi_degrees": dihedral(peptide[master["psi"]]), "density_g_cm3": density, "volume_A3": float(np.linalg.det(frame.cell))})
        display_peptide.append(peptide.astype(np.float32))
        display_water.append(solvent.astype(np.float32))
        all_sources.add(frame.time_source)
        # Validation metadata excludes the large coordinate arrays.
        frame.positions = None
        metadata.append(frame)
    expected_steps, include_zero = validate_timeline(metadata, run["production_origin_step"], run["production_origin_time_ps"], steps, every, dt)
    for row, step in zip(rows, expected_steps):
        row["production_step"] = int(step)
        row["step_source"] = "native" if row["native_step"] is not None else "derived from native time + protocol; no stored step in format"
    offset = int(include_zero)
    np.savez_compressed(output / "display.npz", peptide=np.asarray(display_peptide[offset:]), water=np.asarray(display_water[offset:]), time_ps=np.asarray([r["production_time_ps"] for r in rows[offset:]]), bonds=master["bonds"], elements=np.asarray(master["elements"]), reference=master["reference"])
    with (output / "frames.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    raw_thermo = native_thermo(run["thermo"]["path"], run["thermo"]["kind"], dt, float(master["u"].atoms.masses.sum()))
    thermo_rows = production_rows(raw_thermo, run.get("thermo_origin_step", run["production_origin_step"]), run.get("thermo_origin_time_ps", run["production_origin_time_ps"]), steps, dt)
    pressure_status = "native reported pressure"
    joined_pressure = None
    if all("uncomputed_pressure_placeholder_bar" in row for row in thermo_rows):
        pressure_status = "unavailable: AMBER explicitly reports PRESS=0 because pressure/virial is not calculated; placeholder is not a measured zero"
    if run.get("pressure_observations"):
        if any("pressure_bar" in row for row in thermo_rows):
            raise ValidationError("refusing to replace available native pressure with another source")
        observations, joined_pressure = pressure_observations(run["pressure_observations"], run["trajectory"], np.arange(every, steps + 1, every) * dt)
        for row in thermo_rows:
            matching = [value for t, value in observations.items() if abs(t - row["time_ps"]) <= .001]
            if len(matching) != 1:
                raise ValidationError("thermodynamic sample has no unique actual pressure observation")
            row["pressure_bar"] = float(matching[0])
        inputs.extend(joined_pressure["files"])
        pressure_status = "joined actual pressure observations; original native uncomputed placeholders preserved separately"
    fields = sorted(set().union(*(row.keys() for row in thermo_rows)))
    with (output / "thermodynamics.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(thermo_rows)
    summary = {"engine": engine, "image": run["image"], "status": "native-trajectory-analyzed; not force-field-equivalence or customer-release acceptance", "inputs": inputs, "atoms": natoms, "total_charge_e": master["manifest"]["total_charge_e"], "frame_count": len(rows), "common_frame_count": len(rows) - offset, "initial_frame_present": include_zero, "production_steps": steps, "production_duration_ps": steps * dt, "first_common_time_ps": rows[offset]["production_time_ps"], "last_time_ps": rows[-1]["production_time_ps"], "native_time_sources": sorted(all_sources), "density_from_native_cells_g_cm3": descriptive(densities[offset:]), "performance": native_performance(run["production_log"], engine, steps, dt), "stage_verification": run.get("stage_verification", {"status": "not established here; parent must attach native stage evidence"}), "force_field_equivalence": "separate parent gate; declared atom mapping is not force-field equivalence proof", "initial_potential_energy_kJ_mol": None, "initial_potential_energy_note": "must come from identical canonical-coordinate single-point gate, never substituted with first production frame", "lifecycle_timings": run.get("lifecycle_timings", {}), "provenance": run.get("provenance", {}), "scientific_convergence_claimed": False}
    for field in ("temperature_K", "pressure_bar", "density_g_cm3", "potential_kJ_mol"):
        values = [r[field] for r in thermo_rows if field in r]
        summary[field] = descriptive(values) if values else None
    summary["pressure_status"] = pressure_status
    summary["pressure_observation_provenance"] = joined_pressure
    if summary["pressure_bar"] and summary["pressure_bar"]["min"] == summary["pressure_bar"]["max"] == 0:
        summary["pressure_status"] += "; all values exactly zero: verify native pressure calculation before interpreting physically"
    summary["dihedrals"] = {}
    for field in ("phi_degrees", "psi_degrees"):
        radians = np.radians([row[field] for row in rows[offset:]])
        sine, cosine = float(np.sin(radians).mean()), float(np.cos(radians).mean())
        summary["dihedrals"][field] = {"circular_mean_degrees": float(np.degrees(np.arctan2(sine, cosine))), "resultant_length": float(np.hypot(sine, cosine)), "n": len(radians), "convergence_claimed": False}
    # Detect an incorrectly decoded DCD/NetCDF cell or a volume unit mismatch.
    if summary["density_g_cm3"] and abs(summary["density_g_cm3"]["mean"] - summary["density_from_native_cells_g_cm3"]["mean"]) > .01:
        raise ValidationError("native thermodynamic/trajectory mean densities disagree by >0.01 g/cm3")
    for record in inputs:
        if file_receipt(record["path"]) != record:
            raise ValidationError(f"input changed during analysis: {record['path']}")
    write_json(output / "summary.json", summary)
    return summary


def plots(output, engines):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = dict(zip(ENGINES, ("#3682d8", "#e1a52c", "#b35ad4", "#2caa91")))
    fig, axes = plt.subplots(2, 1, figsize=(11, 6), sharex=True, layout="constrained")
    dist, daxes = plt.subplots(1, 2, figsize=(11, 4), layout="constrained")
    rama, raxes = plt.subplots(2, 2, figsize=(9, 8), layout="constrained")
    thermofig, taxes = plt.subplots(3, 1, figsize=(11, 8), sharex=True, layout="constrained")
    histograms = {}
    for engine in engines:
        data = np.genfromtxt(output / engine / "frames.csv", delimiter=",", names=True, dtype=None, encoding="utf-8")
        data = data[data["production_time_ps"] > .001]
        for axis, column, label in zip(axes, ("phi_degrees", "psi_degrees"), ("phi", "psi")):
            axis.scatter(data["production_time_ps"], data[column], s=2, alpha=.5, color=colors[engine], label=engine.upper())
            axis.set(ylabel=f"{label} (degrees)", ylim=(-180, 180))
        for axis, column, label in zip(daxes, ("phi_degrees", "psi_degrees"), ("phi", "psi")):
            axis.hist(data[column], bins=np.linspace(-180, 180, 37), density=True, histtype="step", linewidth=1.6, label=engine.upper(), color=colors[engine])
            axis.set(xlabel=f"{label} (degrees)", ylabel="sample density / degree", xlim=(-180, 180))
        histograms[engine] = np.histogram2d(data["phi_degrees"], data["psi_degrees"], bins=np.linspace(-180, 180, 37))[0]
        thermodata = np.genfromtxt(output / engine / "thermodynamics.csv", delimiter=",", names=True)
        for axis, field, label in zip(taxes, ("temperature_K", "pressure_bar", "density_g_cm3"), ("temperature (K)", "pressure (bar)", "density (g/cm³)")):
            if field in thermodata.dtype.names:
                axis.plot(thermodata["time_ps"], thermodata[field], linewidth=.5, alpha=.6, label=engine.upper(), color=colors[engine])
            axis.set_ylabel(label)
    max_count = max(h.max() for h in histograms.values())
    for engine, axis in zip(ENGINES, raxes.flat):
        axis.set(title=engine.upper() if engine in engines else f"{engine.upper()} — not supplied", xlabel="phi (degrees)", ylabel="psi (degrees)", xlim=(-180, 180), ylim=(-180, 180))
        if engine in engines:
            heatmap = axis.imshow(histograms[engine].T, origin="lower", extent=(-180, 180, -180, 180), cmap="Blues", vmin=0, vmax=max_count, interpolation="nearest")
    rama.colorbar(heatmap, ax=raxes.ravel().tolist(), label="frames per 10° × 10° bin (common scale)", shrink=.8)
    axes[-1].set_xlabel("production-relative time (ps)")
    axes[0].legend(ncol=4, markerscale=3)
    daxes[0].legend()
    taxes[0].legend(ncol=4)
    taxes[-1].set_xlabel("production-relative time (ps)")
    for figure in (fig, dist, rama, thermofig):
        figure.suptitle("Canonical ff14SB/TIP3P alanine — 1 ns is not a convergence claim")
    for figure, name in ((fig, "phi-psi-timeseries.png"), (dist, "phi-psi-distributions.png"), (rama, "ramachandran.png"), (thermofig, "thermodynamics.png")):
        figure.savefig(output / name, dpi=160)
        plt.close(figure)


def comparison_table(output, summaries):
    columns = ["engine", "atoms", "total_charge_e", "initial_potential_energy_kJ_mol", "mean_temperature_K", "mean_pressure_bar", "mean_density_from_cells_g_cm3", "native_ns_per_day"]
    rows = []
    for summary in summaries:
        rows.append({"engine": summary["engine"], "atoms": summary["atoms"], "total_charge_e": summary["total_charge_e"], "initial_potential_energy_kJ_mol": summary["initial_potential_energy_kJ_mol"], "mean_temperature_K": summary["temperature_K"]["mean"], "mean_pressure_bar": summary["pressure_bar"]["mean"] if summary["pressure_bar"] else None, "mean_density_from_cells_g_cm3": summary["density_from_native_cells_g_cm3"]["mean"], "native_ns_per_day": summary["performance"]["native_ns_per_day"]})
    with (output / "comparison.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# Native trajectory comparison", "", "Descriptive 1 ns production statistics; no convergence or force-field-equivalence claim.", "", "| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        lines.append("| " + " | ".join("not supplied" if row[key] is None else f"{row[key]:.6g}" if isinstance(row[key], float) else str(row[key]) for key in columns) + " |")
    lines.extend(["", "Initial canonical-coordinate potential energy is a separate single-point gate; it is never inferred from production coordinates. Native performance sources/boundaries are recorded per engine in summary.json. Queue/startup/artifact/end-to-end timings remain separate. Sample SDs and counts are in the per-engine JSON, not independent-sample confidence intervals.", ""])
    (output / "comparison.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--inventory", action="store_true")
    args = parser.parse_args()
    if args.inventory:
        print(json.dumps(inventory(), indent=2))
        return
    if not args.spec or not args.output:
        parser.error("--spec and --output required")
    spec = json.loads(args.spec.read_text())
    if args.output.exists():
        parser.error("output directory must not exist; preserve earlier/failed evidence")
    args.output.mkdir(parents=True)
    started = time.time()
    receipt = {"status": "incomplete", "spec": file_receipt(args.spec), "source": source_identity(), "inventory": inventory(), "started_unix": started}
    try:
        if spec.get("evidence_kind") != "real-native-md":
            raise ValidationError("production CLI requires explicit real-native-md evidence kind")
        engines = [r["engine"] for r in spec["runs"]]
        if not engines or len(engines) != len(set(engines)) or not set(engines) <= set(ENGINES):
            raise ValidationError("duplicate/unknown engines")
        master = master_data(spec["master_directory"])
        validate_protocol(master["protocol"])
        receipt["master_manifest"] = file_receipt(master["directory"] / "master-manifest.json")
        receipt["protocol"] = file_receipt(master["directory"] / "protocol.json")
        receipt["runs"] = [analyze_run(run, master, args.output / run["engine"]) for run in spec["runs"]]
        comparison_table(args.output, receipt["runs"])
        plots(args.output, engines)
        receipt["status"] = "analysis-passed" if set(engines) == set(ENGINES) else "partial-analysis-passed; missing engines remain unqualified"
        receipt["missing_engines"] = sorted(set(ENGINES) - set(engines))
        receipt["scientific_convergence_claimed"] = False
    except Exception as exc:
        receipt.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        receipt["analysis_wall_seconds"] = time.time() - started
        receipt["outputs"] = [file_receipt(path) for path in sorted(args.output.rglob("*")) if path.is_file() and path.name != "receipt.json"]
        write_json(args.output / "receipt.json", receipt)


if __name__ == "__main__":
    main()
