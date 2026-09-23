"""Typed native molecular-preparation and binding-energy output assertions."""

import csv
import math
import re


def validate_advanced(cwd, step):
    if step["kind"] == "antechamber":
        text = (cwd / step["output"]).read_text()
        try:
            molecule = text.split("@<TRIPOS>MOLECULE", 1)[1].splitlines()
            expected_atoms, expected_bonds = map(int, molecule[2].split()[:2])
            atoms = [line.split() for line in text.split("@<TRIPOS>ATOM", 1)[1].split("@<TRIPOS>", 1)[0].splitlines() if line.strip()]
            bonds = [line.split() for line in text.split("@<TRIPOS>BOND", 1)[1].split("@<TRIPOS>", 1)[0].splitlines() if line.strip()]
            charges = [float(atom[8]) for atom in atoms]
            numbers = charges + [float(value) for atom in atoms for value in atom[2:5]]
        except (IndexError, ValueError) as exc:
            raise ValueError("Antechamber output is not a complete readable MOL2 molecule") from exc
        if len(atoms) != expected_atoms or len(bonds) != expected_bonds or not atoms or not all(math.isfinite(value) for value in numbers):
            raise ValueError("MOL2 atom/bond counts or finite coordinate/charge checks failed")
        if len({atom[0] for atom in atoms}) != len(atoms) or any(atom[5].lower() in {"du", "un", "unknown"} for atom in atoms):
            raise ValueError("MOL2 contains duplicate atom IDs or unresolved native atom types")
        if any(int(bond[1]) not in range(1, len(atoms) + 1) or int(bond[2]) not in range(1, len(atoms) + 1) for bond in bonds):
            raise ValueError("MOL2 bond references an unknown atom")
        residual = sum(charges) - step["net_charge"]
        if abs(residual) > step["charge_tolerance_e"]:
            raise ValueError(f"MOL2 net-charge residual{residual:.8g}e exceeds explicit tolerance{step['charge_tolerance_e']:.8g}e; charges were not altered")
        if "Calculation Completed" not in (cwd / "sqm.out").read_text():
            raise ValueError("AM1-BCC requires an actually completed native SQM calculation")
        return {"atoms": len(atoms), "bonds": len(bonds), "requested_charge_e": step["net_charge"], "actual_charge_e": sum(charges), "charge_residual_e": residual, "charge_tolerance_e": step["charge_tolerance_e"], "atom_types": step["atom_types"], "charge_model": "AM1-BCC", "sqm_completed": True, "scientific_parameter_suitability_claimed": False}
    if step["kind"] == "parmchk2":
        text = (cwd / step["output"]).read_text()
        if "ATTN" in text.upper() or any(not re.search(rf"(?m)^\s*{section}\s*$", text) for section in ("MASS", "BOND", "ANGLE", "DIHE", "IMPROPER", "NONBON")):
            raise ValueError("parmchk2 FRCMOD is incomplete or contains parameters needing manual revision")
        return {"format": "frcmod", "atom_types": step["atom_types"], "unresolved_parameters": False, "inferred_parameters_require_scientific_review": True}
    prefix = step["output_prefix"]
    report = (cwd / (prefix + ".dat")).read_text()
    if "GENERALIZED BORN" not in report and "POISSON BOLTZMANN" not in report:
        raise ValueError("MMPBSA result lacks a supported native GB/PB energy report")
    tables, current = [], None
    with (cwd / (prefix + ".csv")).open(newline="") as handle:
        for row in csv.reader(handle):
            if not row or not any(item.strip() for item in row):
                continue
            if row[0].strip().lower().startswith("frame"):
                current = {"columns": row, "frames": [], "values": []}
                tables.append(current)
            elif current is not None and re.fullmatch(r"\d+", row[0].strip()):
                values = [float(item) for item in row[1:] if item.strip()]
                if len(values) != len(current["columns"]) - 1 or not all(math.isfinite(value) for value in values):
                    raise ValueError("MMPBSA frame-energy row is missing or nonfinite")
                current["frames"].append(int(row[0]))
                current["values"].append(values)
    if not tables or any(len(table["frames"]) != step["expected_frames"] or len(set(table["frames"])) != step["expected_frames"] for table in tables):
        raise ValueError("MMPBSA did not emit exactly expected_frames for every energy table")
    return {"analysis": "binding" if "receptor_topology" in step else "stability", "frames_per_table": step["expected_frames"], "energy_tables": len(tables), "energy_columns": [table["columns"] for table in tables], "finite_frame_energies": True, "free_energy_convergence_claimed": False, "entropy_or_standard_state_corrections_not_inferred": True}
