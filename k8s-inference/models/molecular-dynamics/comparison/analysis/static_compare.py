#!/usr/bin/env python3
"""Compare retained identical-coordinate native energies and available forces.

This is a diagnostic, not a blanket force-field-equivalence acceptance gate.
No physical parameter, coordinate, charge or engine output is modified.
"""
import argparse
import csv
import json
from pathlib import Path
import re

import numpy as np

from compare import file_receipt, source_identity, write_json
from geometry import ValidationError, finite
from thermo import NUMBER, number

FIELDS = ("bond", "angle", "torsion", "vdw_including_14_and_tail", "electrostatic_including_14", "potential")
KJ_PER_KCAL = 4.184


def normalized(engine, tail, raw, bond, angle, torsion, vdw, electrostatic, potential):
    finite(list(raw.values()), "native static energy")
    result = {"engine": engine, "tail": tail, "units": "kcal/mol", "raw_terms_kcal_mol": raw,
              **dict(zip(FIELDS, (bond, angle, torsion, vdw, electrostatic, potential)))}
    result["component_sum_minus_native_potential"] = bond + angle + torsion + vdw + electrostatic - potential
    if abs(result["component_sum_minus_native_potential"]) > .01:
        raise ValidationError("static component sum does not reproduce native potential within print/accumulation precision")
    return result


def gromacs_energy(path, tail):
    text = Path(path).read_text()
    labels = re.findall(r'@\s+s(\d+)\s+legend\s+"([^"]+)"', text)
    legends = {int(i): name for i, name in labels}
    if len(legends) != len(labels) or len(set(legends.values())) != len(labels):
        raise ValidationError("duplicate GROMACS static legends")
    rows = [[number(x) for x in line.split()] for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", "@"))]
    if len(rows) != 1 or rows[0][0] != 0 or sorted(legends) != list(range(len(legends))) or len(rows[0]) != len(legends) + 1:
        raise ValidationError("GROMACS static XVG must contain one fully labeled time-zero row")
    raw = {name: rows[0][i + 1] / KJ_PER_KCAL for i, name in legends.items()}
    return normalized("gromacs", tail, raw, raw["Bond"], raw["Angle"], raw["Proper Dih."] + raw["Per. Imp. Dih."], raw["LJ (SR)"] + raw["LJ-14"] + raw.get("Disper. corr.", 0.), raw["Coulomb (SR)"] + raw["Coul. recip."] + raw["Coulomb-14"], raw["Potential"])


def lammps_energy(path, tail):
    columns, rows = None, []
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if parts and parts[0] == "Step" and {"E_bond", "PotEng"} <= set(parts):
            if len(parts) != len(set(parts)):
                raise ValidationError("duplicate LAMMPS static fields")
            columns = parts
        elif columns and len(parts) == len(columns) and re.fullmatch(NUMBER, parts[0]):
            rows.append(dict(zip(columns, map(number, parts))))
            columns = None
    if len(rows) != 1 or rows[0]["Step"] != 0 or rows[0]["Atoms"] != 6598:
        raise ValidationError("LAMMPS static log must contain exactly one canonical run0 row")
    raw = {key: value for key, value in rows[0].items() if key.startswith("E_") or key == "PotEng"}
    # LAMMPS E_vdwl already includes its E_tail, so never add E_tail twice.
    return normalized("lammps", tail, raw, raw["E_bond"], raw["E_angle"], raw["E_dihed"] + raw["E_impro"], raw["E_vdwl"], raw["E_coul"] + raw["E_long"], raw["PotEng"])


def namd_energy(path, tail):
    names, rows = None, []
    for line in Path(path).read_text().splitlines():
        if line.startswith("ETITLE:"):
            names = line.split()[1:]
            if len(names) != len(set(names)):
                raise ValidationError("duplicate NAMD static ETITLE fields")
        elif line.startswith("ENERGY:"):
            values = line.split()[1:]
            if names is None or len(values) != len(names):
                raise ValidationError("NAMD static energy requires matching ETITLE")
            rows.append(dict(zip(names, map(number, values))))
    if not rows or any(row["TS"] != 0 for row in rows) or any(row != rows[0] for row in rows[1:]):
        raise ValidationError("NAMD single-point log differs from one time-zero evaluation")
    raw = {key: rows[0][key] for key in ("BOND", "ANGLE", "DIHED", "IMPRP", "VDW", "ELECT", "BOUNDARY", "MISC", "POTENTIAL")}
    if raw["BOUNDARY"] != 0 or raw["MISC"] != 0:
        raise ValidationError("unexpected additional NAMD potential")
    return normalized("namd", tail, raw, raw["BOND"], raw["ANGLE"], raw["DIHED"] + raw["IMPRP"], raw["VDW"], raw["ELECT"], raw["POTENTIAL"])


def amber_energy(path, tail):
    text = Path(path).read_text()
    fields = ("BOND", "ANGLE", "DIHED", "VDWAALS", "EEL", "HBOND", "1-4 VDW", "1-4 EEL", "RESTRAINT")
    labels = "|".join(re.escape(key).replace(r"\ ", r"\s+") for key in fields)
    observations = {key: [] for key in fields}
    for key, value in re.findall(rf"(?<!\w)({labels})\s*=\s*({NUMBER})", text):
        observations[" ".join(key.split())].append(number(value))
    raw = {}
    for key in fields:
        values = observations[key]
        if not values or any(value != values[0] for value in values):
            raise ValidationError(f"AMBER final/initial static {key} values missing or disagree")
        raw[key] = values[0]
    if raw["HBOND"] != 0 or raw["RESTRAINT"] != 0:
        raise ValidationError("unexpected additional AMBER potential")
    # PMEMD prints the minimization total in low-precision scientific notation.
    # Preserve the explicitly labeled sum of its higher-precision native terms.
    result = normalized("amber", tail, raw, raw["BOND"], raw["ANGLE"], raw["DIHED"], raw["VDWAALS"] + raw["1-4 VDW"], raw["EEL"] + raw["1-4 EEL"], sum(raw.values()))
    result["potential_source"] = "sum of native printed components (4 decimals); not fabricated extra precision from rounded total"
    return result


def lammps_forces(path):
    lines = Path(path).read_text().splitlines()
    if lines[:2] != ["ITEM: TIMESTEP", "0"] or lines.count("ITEM: TIMESTEP") != 1:
        raise ValidationError("LAMMPS force dump is not exactly one step-zero frame")
    count = int(lines[3])
    header = next(i for i, value in enumerate(lines) if value.startswith("ITEM: ATOMS "))
    columns = lines[header].split()[2:]
    if len(columns) != len(set(columns)):
        raise ValidationError("duplicate force-dump fields")
    values = finite([[number(value) for value in row.split()] for row in lines[header + 1:]], "LAMMPS static force")
    if values.shape != (count, len(columns)) or count != 6598:
        raise ValidationError("LAMMPS force dump atom count/fields differ")
    values = values[np.argsort(values[:, columns.index("id")])]
    if not np.array_equal(values[:, columns.index("id")], np.arange(1, count + 1)):
        raise ValidationError("LAMMPS force IDs are not unique canonical 1..N")
    return values[:, [columns.index(name) for name in ("x", "y", "z")]], values[:, [columns.index(name) for name in ("fx", "fy", "fz")]]


def gromacs_forces(path, coordinate_path):
    from MDAnalysis.lib.formats.libmdaxdr import TRRFile
    with TRRFile(str(path), "r") as stream:
        records = list(stream)
    if len(records) != 1 or records[0].step != 0 or records[0].time != 0:
        raise ValidationError("GROMACS force TRR is not one step-zero frame")
    value = records[0]
    if not value.hasf or value.f.shape != (6598, 3):
        raise ValidationError("GROMACS TRR must contain all canonical forces")
    if value.hasx:
        coordinates = value.x.astype(np.float64) * 10.
    else:
        # nstfout=1 with nstxout=0 produces a force-only TRR. Coordinates come
        # from the independently hash-bound run0 input, never zero-filled x.
        lines = Path(coordinate_path).read_text().splitlines()
        if int(lines[1]) != 6598 or len(lines) != 6601:
            raise ValidationError("canonical GRO coordinate-only record count differs")
        coordinates = np.asarray([[number(item) for item in line[20:].split()] for line in lines[2:-1]], dtype=np.float64) * 10.
    if coordinates.shape != (6598, 3):
        raise ValidationError("GROMACS static input coordinate count differs")
    return finite(coordinates, "GROMACS static coordinates"), finite(value.f.astype(np.float64) / (10 * KJ_PER_KCAL), "TRR forces")


def force_comparison(gmx_path, lmp_path, coordinate_path):
    gx, gf = gromacs_forces(gmx_path, coordinate_path)
    lx, lf = lammps_forces(lmp_path)
    coordinate_error = float(np.abs(gx - lx).max())
    if coordinate_error > 1e-5:
        raise ValidationError(f"force comparison coordinate/order mismatch: {coordinate_error} A")
    difference = gf - lf
    rms = float(np.sqrt(np.mean(difference**2)))
    reference_rms = float(np.sqrt(np.mean(lf**2)))
    return {"atoms": 6598, "force_components": int(gf.size), "units": "kcal/mol/angstrom", "maximum_coordinate_difference_A": coordinate_error,
            "component_RMS_difference": rms, "component_RMS_reference_lammps": reference_rms, "relative_component_RMS_difference": rms / reference_rms,
            "maximum_absolute_component_difference": float(np.abs(difference).max()), "maximum_vector_norm_difference": float(np.linalg.norm(difference, axis=1).max()),
            "GROMACS_force_unit_conversion": "native kJ/(mol nm) divided by 41.84", "LAMMPS_force_unit_conversion": "native units real: kcal/(mol angstrom)",
            "scope": "available GROMACS and LAMMPS full native force vectors only; not a four-engine force comparison"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gromacs", "lammps", "namd", "amber-on", "amber-off", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    receipt = {"status": "incomplete", "source": source_identity(), "cross_engine_equivalence_proven": False, "scientific_convergence_claimed": False}
    inputs, rows, forces = [], [], {}
    try:
        for tail in ("on", "off"):
            gp = args.gromacs / f"tail-{tail}-energy.xvg"
            lp = args.lammps / f"alanine-static-tail-{tail}/data/fs2-energy-segment-000001.log"
            job = "singlepoint-tail" if tail == "on" else "singlepoint-no-tail"
            np_ = args.namd / job / f"data/alanine/fs2-{job}-part000001.log"
            ap = (args.amber_on if tail == "on" else args.amber_off) / "static.mdout"
            inputs.extend([gp, lp, np_, ap])
            rows.extend([gromacs_energy(gp, tail), lammps_energy(lp, tail), namd_energy(np_, tail), amber_energy(ap, tail)])
            gf = args.gromacs / f"tail-{tail}.trr"
            lf = lp.parent / f"tail-{tail}-forces.lammpstrj"
            gc = args.gromacs / "system.gro"
            inputs.extend([gf, lf, gc])
            forces[tail] = force_comparison(gf, lf, gc)
        by_engine = {engine: {row["tail"]: row for row in rows if row["engine"] == engine} for engine in ("gromacs", "lammps", "namd", "amber")}
        deltas = {engine: {field: pair["on"][field] - pair["off"][field] for field in FIELDS} for engine, pair in by_engine.items()}
        constants = {"amber": 18.2223**2, "lammps": 332.06371, "namd": 332.0636,
                     "gromacs": 1.602176634e-19**2 * 6.02214076e23 / (4 * np.pi * 8.8541878128e-12 * 1e-9 * 1000) * 10 / KJ_PER_KCAL}
        diagnostic = []
        reference = by_engine["amber"]["off"]["electrostatic_including_14"]
        for engine in ("gromacs", "lammps", "namd"):
            predicted = reference * (constants[engine] / constants["amber"] - 1)
            observed = by_engine[engine]["off"]["electrostatic_including_14"] - reference
            diagnostic.append({"engine_minus_amber": engine, "observed_electrostatic_difference_kcal_mol": observed, "constant_ratio_only_predicted_shift_kcal_mol": predicted, "remaining_difference_after_diagnostic_kcal_mol": observed - predicted})
        receipt.update(status="native-static-diagnostic-completed; residual numerical differences not declared resolved", inputs=[file_receipt(path) for path in inputs],
                       energies=rows, tail_on_minus_off=deltas, force_comparisons=forces, source_level_coulomb_constants_kcal_mol_A_e2=constants,
                       coulomb_ratio_diagnostic={"scope": "source-level explanatory inference, not engine rerun or charge/force-field modification; see report for source-to-binary binding limits", "comparisons": diagnostic},
                       unresolved=["nonidentical PME/PPPM error definitions and Ewald coefficients, fixed64cube/order4", "GROMACS mixed precision, NAMD GPU force tables, LAMMPS12bit Coulomb tables, AMBER interpolation/printed precision", "finite-system dispersion pair-count conventions; do not tune force field to match totals", "NAMD and AMBER atom-force vectors were not supplied; four-engine force parity remains unestablished"])
        with (args.output / "energies.csv").open("w", newline="") as stream:
            columns = ["engine", "tail", *FIELDS, "component_sum_minus_native_potential"]
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        lines = ["# Canonical-coordinate native energy/force diagnostic", "", "Units: kcal/mol; matching canonical initial coordinates, constraints disabled, no optimization/integration. Residual numerical differences remain; no blanket force-field-equivalence or trajectory-convergence claim.", "", "| engine | tail | " + " | ".join(FIELDS) + " |", "|---|---|" + "---|" * len(FIELDS)]
        for row in rows:
            lines.append("| " + row["engine"] + " | " + row["tail"] + " | " + " | ".join(f"{row[key]:.8f}" for key in FIELDS) + " |")
        lines.extend(["", "AMBER potential is the sum of four-decimal native component fields. GROMACS mixed-precision total and sum can differ slightly; closure errors remain in JSON/CSV. LAMMPS E_vdwl already includes its tail correction. All native raw decompositions, explicit conversions, input hashes, tail differences and available full force comparisons are preserved in receipt.json.", "", "Constants provide a diagnostic inference only. See the source-reviewed companion report for finite-size tail conventions and unclosed mesh/table/precision differences.", ""])
        (args.output / "report.md").write_text("\n".join(lines))
    except Exception as exc:
        receipt.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        write_json(args.output / "receipt.json", receipt)


if __name__ == "__main__":
    main()
