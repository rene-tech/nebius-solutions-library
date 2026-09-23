#!/usr/bin/env python3
"""Explicit canonical TIP3P triangle -> OH/HOH SHAKE constraint adapter.

No original force-field term is removed: HH remains a harmonic bond, and every
new water HOH harmonic angle has exactly zero force constant. Coefficient IDs
are deduplicated without changing their numerical parameters or multiplicity.
"""
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

import numpy as np

SECTIONS = ("Masses", "Pair Coeffs", "Bond Coeffs", "Angle Coeffs", "Dihedral Coeffs", "Improper Coeffs", "Atoms", "Velocities", "Bonds", "Angles", "Dihedrals", "Impropers")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_data(path):
    header, sections, current = [], {}, None
    for raw in Path(path).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line in SECTIONS:
            current = line
            if current in sections:
                raise ValueError(f"duplicate native section {current}")
            sections[current] = []
        elif current is None:
            header.append(raw)
        elif line:
            row = line.split()
            if not row[0].isdigit():
                raise ValueError(f"unsupported native data record {line}")
            sections[current].append(row)
    for name, rows in sections.items():
        ids = [int(row[0]) for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate row IDs in {name}")
    return header, sections


def coefficient_key(row):
    return row[1], tuple(Decimal(value) for value in row[2:])


def interactions(sections, kind):
    coefficient_section = {"Bonds": "Bond Coeffs", "Angles": "Angle Coeffs", "Dihedrals": "Dihedral Coeffs", "Impropers": "Improper Coeffs"}[kind]
    coefficients = {row[0]: coefficient_key(row) for row in sections.get(coefficient_section, [])}
    return Counter((tuple(row[2:]), coefficients[row[1]]) for row in sections.get(kind, []))


def compact_coefficients(sections):
    result, remaps = deepcopy(sections), {}
    for coefficient_section, interaction in (("Bond Coeffs", "Bonds"), ("Angle Coeffs", "Angles"), ("Dihedral Coeffs", "Dihedrals"), ("Improper Coeffs", "Impropers")):
        unique, new_rows, mapping = {}, [], {}
        for row in sections.get(coefficient_section, []):
            key = coefficient_key(row)
            if key not in unique:
                unique[key] = str(len(new_rows) + 1)
                new_rows.append([unique[key], *row[1:]])
            mapping[row[0]] = unique[key]
        if coefficient_section in sections:
            result[coefficient_section] = new_rows
        for row in result.get(interaction, []):
            row[1] = mapping[row[1]]
        remaps[coefficient_section] = mapping
    return result, remaps


def adapt(sections, water_atom_ids):
    result, remaps = compact_coefficients(sections)
    masses = {row[0]: float(row[1]) for row in result["Masses"]}
    atoms = {int(row[0]): row for row in result["Atoms"]}
    coeff = {row[0]: row for row in result["Bond Coeffs"]}
    bonds = {frozenset(map(int, row[2:])): row for row in result["Bonds"]}
    if len(bonds) != len(result["Bonds"]):
        raise ValueError("duplicate bonded atom pairs are outside canonical adapter scope")
    hydrogen = {i for i, row in atoms.items() if 0.9 <= masses[row[2]] <= 1.1}
    water_atoms = set()
    water_hh, water_oh, distances = [], [], []
    angle_types, added_angles = {}, []
    for oxygen, h1, h2 in water_atom_ids:
        if len({oxygen, h1, h2}) != 3 or {oxygen, h1, h2} & water_atoms:
            raise ValueError("overlapping or malformed water mapping")
        water_atoms.update((oxygen, h1, h2))
        if oxygen in hydrogen or not {h1, h2} <= hydrogen:
            raise ValueError("water element/mass ordering differs")
        oh1, oh2, hh = [bonds[frozenset(pair)] for pair in ((oxygen, h1), (oxygen, h2), (h1, h2))]
        types = [coeff[row[1]] for row in (oh1, oh2, hh)]
        if any(row[1] != "harmonic" or len(row) != 4 for row in types):
            raise ValueError("canonical water must have three harmonic bonds")
        first, second, third = [float(row[3]) for row in types]
        if abs(first - second) > 1e-12 or not 0 < third < 2 * first:
            raise ValueError("water is not the expected isosceles rigid triangle")
        angle = float(np.degrees(2 * np.arcsin(third / (2 * first))))
        angle_key = (first, third)
        if angle_key not in angle_types:
            angle_type = str(len(result.get("Angle Coeffs", [])) + 1)
            result.setdefault("Angle Coeffs", []).append([angle_type, "harmonic", "0.0", format(angle, ".15g")])
            angle_types[angle_key] = angle_type
        new_id = str(len(result.get("Angles", [])) + len(added_angles) + 1)
        added_angles.append([new_id, angle_types[angle_key], str(h1), str(oxygen), str(h2)])
        water_hh.append(hh[0])
        water_oh.extend((oh1[0], oh2[0]))
        distances.append((first, third, angle))
    result.setdefault("Angles", []).extend(added_angles)
    hh_ids = set(water_hh)
    constrained, unconstrained = set(), set()
    peptide_h_bonds = 0
    for row in result["Bonds"]:
        a, b = map(int, row[2:])
        selected = bool({a, b} & hydrogen) and row[0] not in hh_ids
        (constrained if selected else unconstrained).add(row[1])
        if selected and not {a, b} & water_atoms:
            peptide_h_bonds += 1
    if constrained & unconstrained:
        raise ValueError("one coefficient type mixes constrained and unconstrained bonds")
    # Independent multiset proof: all original interactions remain, including HH.
    for kind in ("Bonds", "Dihedrals", "Impropers"):
        if interactions(sections, kind) != interactions(result, kind):
            raise ValueError(f"adapter changed {kind} potential terms or multiplicity")
    angle_delta = interactions(result, "Angles") - interactions(sections, "Angles")
    if sum(angle_delta.values()) != len(water_atom_ids) or any(key[1][0] != "harmonic" or key[1][1][0] != 0 for key in angle_delta):
        raise ValueError("new angles do not all have exactly zero potential")
    if interactions(sections, "Angles") - interactions(result, "Angles"):
        raise ValueError("adapter removed original angle terms")
    for name in ("Atoms", "Masses", "Velocities", "Pair Coeffs"):
        if result.get(name) != sections.get(name):
            raise ValueError(f"adapter changed {name}")
    return result, {"coefficient_id_remaps": remaps, "water_count": len(water_atom_ids), "water_oh_bonds": len(water_oh), "water_hh_bonds_retained_unconstrained": len(water_hh), "peptide_hydrogen_bonds_constrained": peptide_h_bonds, "shake_bond_types": sorted(map(int, constrained)), "shake_angle_types": sorted(map(int, angle_types.values())), "new_zero_k_angle_count": len(added_angles), "water_geometry_A_degrees": sorted(set(distances)), "water_atom_ids_O_H_H": water_atom_ids, "all_original_interaction_multisets_unchanged": True, "all_atom_mass_charge_coordinate_records_unchanged": True, "new_angle_energy_and_force_exactly_zero": True}


def write_data(path, header, sections):
    text = "\n".join(header)
    for label, section in (("bonds", "Bonds"), ("angles", "Angles"), ("dihedrals", "Dihedrals"), ("impropers", "Impropers"), ("bond types", "Bond Coeffs"), ("angle types", "Angle Coeffs"), ("dihedral types", "Dihedral Coeffs"), ("improper types", "Improper Coeffs")):
        if section in sections:
            text = re.sub(rf"(?m)^\s*\d+ {label}$", f"{len(sections[section])} {label}", text)
    text += "\n\n"
    for name in SECTIONS:
        if name in sections:
            text += name + "\n\n" + "\n".join(" ".join(row) for row in sections[name]) + "\n\n"
    Path(path).write_text(text)


def generate(master, converted, output):
    import parmed
    master, converted, output = map(Path, (master, converted, output))
    if output.exists():
        raise ValueError("adapter output must be new")
    for directory, manifest_name in ((master, "master-manifest.json"), (converted, "conversion.json")):
        manifest = json.loads((directory / manifest_name).read_text())
        for row in manifest["files"]:
            if sha256(directory / row["path"]) != row["sha256"]:
                raise ValueError(f"source hash mismatch: {directory / row['path']}")
    top = parmed.load_file(str(master / "system.prmtop"), xyz=str(master / "system.rst7"))
    waters = []
    for residue in top.residues:
        if residue.name == "WAT":
            if sorted(a.name for a in residue.atoms) != ["H1", "H2", "O"]:
                raise ValueError("water atom names differ")
            waters.append([next(a.idx + 1 for a in residue.atoms if a.name == name) for name in ("O", "H1", "H2")])
    header, original = read_data(converted / "lammps/system.lmp")
    atom_rows = sorted(original["Atoms"], key=lambda row: int(row[0]))
    if [int(row[0]) for row in atom_rows] != list(range(1, len(top.atoms) + 1)):
        raise ValueError("native atom ID mapping differs")
    if not np.allclose([[float(x) for x in row[4:7]] for row in atom_rows], top.coordinates, atol=1e-7, rtol=0):
        raise ValueError("native coordinates differ from canonical master")
    adapted, proof = adapt(original, waters)
    output.mkdir(parents=True)
    write_data(output / "system-shake.lmp", header, adapted)
    _, reloaded = read_data(output / "system-shake.lmp")
    if reloaded != adapted:
        raise ValueError("serialized adapter did not re-read identically")
    proof.update(schema="fs2-canonical-lammps-shake-adapter/v1", status="parameter-and-geometry-validated; native proof pending", source_data_sha256=sha256(converted / "lammps/system.lmp"), master_topology_sha256=sha256(master / "system.prmtop"), master_coordinates_sha256=sha256(master / "system.rst7"), converted_manifest_sha256=sha256(converted / "conversion.json"), adapter_source_sha256=sha256(__file__), adapted_data_sha256=sha256(output / "system-shake.lmp"), notes=["No original bonds/angles/torsions removed or changed; original HH harmonic remains.", "Fix SHAKE constrains two OH bonds and zero-K HOH angle; HH target follows by cosine law.", "No bitwise stochastic continuation claim: Langevin RNG is not stored by LAMMPS restart."])
    (output / "adapter-manifest.json").write_text(json.dumps(proof, indent=2) + "\n")
    return proof
