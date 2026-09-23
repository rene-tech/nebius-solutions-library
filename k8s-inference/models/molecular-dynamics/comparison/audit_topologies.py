"""Independently re-read converted parameters; execution is not this audit.

This validator is deliberately scoped to the ff14SB/TIP3P acceptance fixture,
not a claim that arbitrary force fields or InterMol conversions are supported.
"""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re

import numpy as np
import openmm
from openmm import app, unit
import parmed


def key(indices):
    indices = tuple(int(i) for i in indices)
    return min(indices, indices[::-1])


def torsions(top):
    terms = defaultdict(list)
    for d in top.dihedrals:
        types = d.type if isinstance(d.type, parmed.DihedralTypeList) else [d.type]
        for value in types:
            terms[key((d.atom1.idx, d.atom2.idx, d.atom3.idx, d.atom4.idx))].append(
                (value.phi_k, int(value.per), np.radians(value.phase)))
    return terms


def torsion_curve(terms, angles):
    return sum(k * (1 + np.cos(n * angles - phase)) for k, n, phase in terms)


def explicit_exceptions(top):
    system = top.createSystem(nonbondedMethod=app.NoCutoff, constraints=None,
                              rigidWater=False, removeCMMotion=False)
    nb = next(f for f in system.getForces() if isinstance(f, openmm.NonbondedForce))
    result = {}
    for i in range(nb.getNumExceptions()):
        a, b, qq, sigma, epsilon = nb.getExceptionParameters(i)
        eps = epsilon.value_in_unit(unit.kilocalorie_per_mole)
        result[key((a, b))] = (qq.value_in_unit(unit.elementary_charge**2),
                               sigma.value_in_unit(unit.angstrom) if eps else 0.0, eps)
    return result


def read_lammps(path):
    names = {"Masses", "Bond Coeffs", "Angle Coeffs", "Dihedral Coeffs",
             "Improper Coeffs", "Atoms", "Velocities", "Bonds", "Angles",
             "Dihedrals", "Impropers", "Pair Coeffs"}
    sections, current = defaultdict(list), None
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line in names:
            current = line
        elif line and current:
            fields = line.split()
            if not fields[0].isdigit():
                raise ValueError(f"Unsupported LAMMPS data section/row: {line}")
            sections[current].append(fields)
    return sections


def audit(master, converted):
    manifest = json.loads((converted / "conversion.json").read_text())
    for row in manifest["files"]:
        path = converted / row["path"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Converted file differs from its manifest: " + row["path"])
    master_manifest = json.loads((master / "master-manifest.json").read_text())
    for row in master_manifest["files"]:
        if hashlib.sha256((master / row["path"]).read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Master file differs from its immutable manifest: " + row["path"])
    amber = parmed.load_file(str(master / "system.prmtop"), xyz=str(master / "system.rst7"))
    gmx = parmed.gromacs.GromacsTopologyFile(str(converted / "gromacs/system.top"),
                                           xyz=str(converted / "gromacs/system.gro"),
                                           defines={"FLEXIBLE": True})
    if gmx.parameterset.nbfix_types or any(atom.atom_type.nbfix for atom in gmx.atoms):
        raise ValueError("Cross-type GROMACS LJ overrides are absent from this canonical AMBER master")
    checks = []

    def compare(name, left, right, *, atol=1e-7):
        left, right = np.asarray(left), np.asarray(right)
        same_shape = left.shape == right.shape
        delta = float(np.max(np.abs(left - right))) if same_shape and left.size else 0.0
        passed = same_shape and np.all(np.isfinite(left)) and np.all(np.isfinite(right)) and delta <= atol
        checks.append({"name": name, "passed": bool(passed), "left_shape": list(left.shape),
                       "right_shape": list(right.shape), "max_abs_difference": delta if same_shape else None,
                       "absolute_tolerance": atol})

    def mappings(name, left, right, atol=1e-7):
        identical = set(left) == set(right)
        checks.append({"name": name + ":identities", "passed": identical,
                       "reference_count": len(left), "converted_count": len(right),
                       "missing": [str(k) for k in sorted(set(left) - set(right))[:12]],
                       "unexpected": [str(k) for k in sorted(set(right) - set(left))[:12]]})
        if identical:
            compare(name + ":values", [left[k] for k in sorted(left)], [right[k] for k in sorted(left)], atol=atol)

    atom_values = lambda t: [[a.mass, a.charge, a.sigma, a.epsilon] for a in t.atoms]
    compare("gromacs:atom-mass-charge-sigma-epsilon", atom_values(amber), atom_values(gmx), atol=2e-6)
    checks.append({"name": "gromacs:atom-order-types-names", "passed":
                   [(a.name, a.type) for a in amber.atoms] == [(a.name, a.type) for a in gmx.atoms]})
    compare("gromacs:canonical-coordinates-A", amber.coordinates, gmx.coordinates, atol=1e-7)
    compare("gromacs:box-A-degrees", amber.box, gmx.box, atol=1e-7)
    bonds = lambda t: {key((b.atom1.idx, b.atom2.idx)): (b.type.k, b.type.req) for b in t.bonds}
    angles = lambda t: {key((a.atom1.idx, a.atom2.idx, a.atom3.idx)): (a.type.k, a.type.theteq) for a in t.angles}
    mappings("gromacs:bonds-kcal-A", bonds(amber), bonds(gmx), atol=1e-5)
    mappings("gromacs:angles-kcal-degrees", angles(amber), angles(gmx), atol=1e-5)
    checks.append({"name": "gromacs:bond-angle-multiplicity", "passed":
                   len(amber.bonds) == len(gmx.bonds) == len(bonds(gmx)) and
                   len(amber.angles) == len(gmx.angles) == len(angles(gmx))})
    points = np.linspace(-np.pi, np.pi, 1441)
    amber_torsions = torsions(amber)
    mappings("gromacs:proper-and-improper-potential-curves-kcal",
             {k: torsion_curve(v, points) for k, v in amber_torsions.items()},
             {k: torsion_curve(v, points) for k, v in torsions(gmx).items()}, atol=2e-5)
    exceptions = explicit_exceptions(amber)
    mappings("gromacs:all-exclusions-and-scaled-14", exceptions, explicit_exceptions(gmx), atol=2e-6)
    checks.append({"name": "gromacs:lorentz-berthelot", "passed": gmx.combining_rule == amber.combining_rule == "lorentz"})

    data = read_lammps(converted / "lammps/system.lmp")
    native = (converted / "lammps/system.input").read_text()
    commands = defaultdict(list)
    allowed = {"units", "atom_style", "dimension", "boundary", "bond_style", "angle_style",
               "dihedral_style", "special_bonds", "read_data", "pair_style", "kspace_style",
               "pair_modify", "pair_coeff", "thermo_style", "run"}
    for line in native.splitlines():
        fields = line.split("#", 1)[0].split()
        if not fields:
            continue
        if fields[0] not in allowed:
            raise ValueError("Unreviewed native conversion command: " + fields[0])
        commands[fields[0]].append(fields[1:])
    for name in allowed - {"pair_modify", "pair_coeff"}:
        if len(commands[name]) != 1:
            raise ValueError("Missing or repeated native conversion command: " + name)
    if commands["pair_modify"] != [["shift", "no", "tail", "yes"], ["mix", "arithmetic"]]:
        raise ValueError("Unexpected native pair overrides/mixing convention")
    expected = {"units": ["real"], "atom_style": ["full"], "dimension": ["3"],
                "boundary": ["p", "p", "p"], "read_data": ["system.lmp"],
                "pair_style": ["lj/cut/coul/long", "10.0", "10.0"], "run": ["0"]}
    for name, value in expected.items():
        if commands[name][0] != value:
            raise ValueError("Native conversion setting differs from expected fixture: " + name)
    if any(len(row) != 4 or row[0] != row[1] for row in commands["pair_coeff"]):
        raise ValueError("Unexpected explicit cross-type LJ override")
    if len({row[0] for row in commands["pair_coeff"]}) != len(commands["pair_coeff"]):
        raise ValueError("Repeated native pair coefficients")
    masses = {int(row[0]): float(row[1]) for row in data["Masses"]}
    pairs = {int(row[0]): (float(row[2]), float(row[3])) for row in
             [line.split()[1:] for line in native.splitlines() if line.startswith("pair_coeff ")] if row[0] == row[1]}
    atom_rows = sorted(data["Atoms"], key=lambda row: int(row[0]))
    compare("lammps:atom-id-order", np.arange(1, len(amber.atoms) + 1), [int(row[0]) for row in atom_rows], atol=0)
    compare("lammps:atom-mass-charge-sigma-epsilon", atom_values(amber),
            [[masses[int(row[2])], float(row[3]), pairs[int(row[2])][1], pairs[int(row[2])][0]] for row in atom_rows], atol=2e-6)
    compare("lammps:canonical-coordinates-A", amber.coordinates, [[float(v) for v in row[4:7]] for row in atom_rows], atol=1e-7)
    raw = (converted / "lammps/system.lmp").read_text()
    box = [[float(v) for v in re.search(rf"^\s*(\S+)\s+(\S+)\s+{axis}lo {axis}hi$", raw, re.M).groups()] for axis in "xyz"]
    compare("lammps:box-lengths-A", amber.box[:3], [hi - lo for lo, hi in box], atol=1e-7)
    tilt = re.search(r"^\s*(\S+)\s+(\S+)\s+(\S+) xy xz yz$", raw, re.M)
    if tilt is None:
        raise ValueError("Expected explicit native cell tilt record")
    compare("lammps:orthorhombic-tilt-A", [0, 0, 0], list(map(float, tilt.groups())), atol=1e-12)
    checks.append({"name": "lammps:lorentz-berthelot", "passed": "pair_modify mix arithmetic" in native})
    bond_coeff = {int(r[0]): tuple(map(float, r[2:])) for r in data["Bond Coeffs"] if r[1] == "harmonic"}
    angle_coeff = {int(r[0]): tuple(map(float, r[2:])) for r in data["Angle Coeffs"] if r[1] == "harmonic"}
    native_bonds = {key((int(r[2]) - 1, int(r[3]) - 1)): bond_coeff[int(r[1])] for r in data["Bonds"]}
    native_angles = {key(int(v) - 1 for v in r[2:]): angle_coeff[int(r[1])] for r in data["Angles"]}
    checks.append({"name": "lammps:bond-angle-multiplicity", "passed":
                   len(data["Bonds"]) == len(native_bonds) == len(amber.bonds) and
                   len(data["Angles"]) == len(native_angles) == len(amber.angles)})
    atom_type_names = defaultdict(set)
    for atom, row in zip(amber.atoms, atom_rows, strict=True):
        atom_type_names[int(row[2])].add(atom.type)
    checks.append({"name": "lammps:explicit-atom-type-mapping", "passed":
                   len(atom_type_names) == len({a.type for a in amber.atoms}) and
                   all(len(names) == 1 for names in atom_type_names.values()),
                   "mapping": {str(k): sorted(v) for k, v in atom_type_names.items()}})
    mappings("lammps:bonds-kcal-A", bonds(amber), native_bonds, atol=1e-5)
    mappings("lammps:angles-kcal-degrees", angles(amber), native_angles, atol=1e-5)
    curves = {}
    for row in data["Dihedral Coeffs"]:
        values = list(map(float, row[2:]))
        if row[1] == "charmm":
            k, n, phase, weight = values
            if weight:
                raise ValueError("Unexpected CHARMM implicit1–4 weight; would duplicate native special_bonds")
            curves[int(row[0])] = k * (1 + np.cos(n * points - np.radians(phase)))
        elif row[1] == "multi/harmonic":
            curves[int(row[0])] = sum(value * np.cos(points)**i for i, value in enumerate(values))
        else:
            raise ValueError(f"Unsupported converted dihedral style: {row[1]}")
    lmp_torsions = defaultdict(lambda: np.zeros_like(points))
    for row in data["Dihedrals"]:
        lmp_torsions[key(int(v) - 1 for v in row[2:])] += curves[int(row[1])]
    if data["Impropers"]:
        raise ValueError("Unexpected separate improper style; audit it before use")
    mappings("lammps:proper-and-improper-potential-curves-kcal",
             {k: torsion_curve(v, points) for k, v in amber_torsions.items()}, lmp_torsions, atol=2e-5)
    special = re.search(r"^special_bonds lj (\S+) (\S+) (\S+) coul (\S+) (\S+) (\S+)$", native, re.M)
    if special is None:
        raise ValueError("Explicit LJ/Coulomb1–4 scaling was not exported")
    weights = list(map(float, special.groups()))
    neighbors = defaultdict(set)
    for a, b in native_bonds:
        neighbors[a].add(b)
        neighbors[b].add(a)
    native_exceptions = {}
    for i, atom in enumerate(amber.atoms):
        visited, frontier = {i}, {i}
        for depth in range(1, 4):
            frontier = set().union(*(neighbors[k] for k in frontier)) - visited if frontier else set()
            visited.update(frontier)
            for j in frontier:
                if j <= i:
                    continue
                other = amber.atoms[j]
                eps = np.sqrt(atom.epsilon * other.epsilon) * weights[depth - 1]
                native_exceptions[(i, j)] = (atom.charge * other.charge * weights[depth + 2],
                                             (atom.sigma + other.sigma) / 2 if eps else 0.0, eps)
    mappings("lammps:graph-exclusions-and-scaled-14", exceptions, native_exceptions, atol=2e-6)
    return {"status": "passed" if all(c["passed"] for c in checks) else "failed", "checks": checks,
            "atoms": len(amber.atoms), "bonds": len(amber.bonds), "angles": len(amber.angles),
            "torsion_records": len(amber.dihedrals), "exception_pairs": len(exceptions),
            "notes": ["Full flexible topology parameters audited; dynamics adds explicit native constraints.",
                      "LAMMPS atom type numeric IDs are mapped through native masses/charges/LJ coefficients.",
                      "Periodic improper terms may be represented as native polynomial dihedrals; complete potential curves checked.",
                      "Energy/force comparisons and dynamics are separate gates, not implied by this report."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", required=True, type=Path)
    parser.add_argument("--converted", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Do not overwrite retained audit evidence")
    report = audit(args.master, args.converted)
    report["input_hashes"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in [args.master / "master-manifest.json", args.converted / "conversion.json"]}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["status"] == "passed" else 1)
