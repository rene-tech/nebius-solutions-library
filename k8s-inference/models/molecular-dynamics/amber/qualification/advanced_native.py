"""Bounded CPU controls from the official Sustiva and Ras–Raf tutorials.

This directly qualifies installed tools before the separately rebuilt typed
worker. It is not, by itself, a hosted/API acceptance receipt.
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


def run(root, label, command, commands):
    env = {**os.environ, "AMBERHOME": "/opt/ambertools", "PATH": "/opt/ambertools/bin:/usr/bin:/bin", "LD_LIBRARY_PATH": "/opt/ambertools/lib", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    started = time.monotonic()
    with (root / (label + ".log")).open("x") as log:
        result = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
    commands.append({"command": command, "exit_code": result.returncode, "wall_seconds": time.monotonic() - started})
    if result.returncode:
        raise ValueError(f"native{label} exited{result.returncode}; retained log")


def energy_rows(path):
    rows = []
    current = None
    for line in path.read_text().splitlines():
        if "BOND" in line and "ANGLE" in line and "DIHED" in line:
            current = {}
            rows.append(current)
        if current is not None:
            for name in ("BOND", "ANGLE", "DIHED", "VDWAALS", "EEL", "EGB", "EPB"):
                if match := re.search(rf"\b{name}\s*=\s*(\S+)", line):
                    current[name] = float(match.group(1))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=5)
    parser.add_argument("--charge-equivalence", type=int, choices=(0, 1, 2), default=1, help="Explicit native antechamber-eq control; default is the untouched native atomic-path equivalence mode")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    commands, tests = [], []
    status, error = "passed", None
    try:
        ligand = args.output / "sustiva-gaff2"
        ligand.mkdir()
        shutil.copy2(args.source / "sustiva_new.pdb", ligand / "sustiva.pdb")
        run(ligand, "antechamber", ["/opt/ambertools/bin/antechamber", "-i", "sustiva.pdb", "-fi", "pdb", "-o", "sustiva.mol2", "-fo", "mol2", "-c", "bcc", "-nc", "0", "-m", "1", "-at", "gaff2", "-rn", "SUS", "-s", "2", "-pf", "n", "-eq", str(args.charge_equivalence)], commands)
        run(ligand, "parmchk2", ["/opt/ambertools/bin/parmchk2", "-i", "sustiva.mol2", "-f", "mol2", "-o", "sustiva.frcmod", "-s", "gaff2"], commands)
        if "Calculation Completed" not in (ligand / "sqm.out").read_text():
            raise ValueError("actual AM1-BCC/SQM calculation did not complete")
        text = (ligand / "sustiva.mol2").read_text()
        atoms = [line.split() for line in text.split("@<TRIPOS>ATOM\n")[1].split("@<TRIPOS>BOND")[0].splitlines() if line.strip()]
        charges = [float(atom[8]) for atom in atoms]
        if len(atoms) != 30 or not all(math.isfinite(value) for value in charges):
            raise ValueError("Sustiva atom count or charge finiteness assertion failed")
        charge_ok = abs(sum(charges)) <= 1e-4
        if "ATTN" in (ligand / "sustiva.frcmod").read_text():
            raise ValueError("GAFF2 generated unresolved parameters requiring manual revision")
        (ligand / "prepare.leap").write_text("source leaprc.gaff2\nloadamberparams sustiva.frcmod\nligand = loadmol2 sustiva.mol2\ncheck ligand\nsaveamberparm ligand sustiva.prmtop sustiva.inpcrd\nquit\n")
        run(ligand, "tleap", ["/opt/ambertools/bin/tleap", "-f", "prepare.leap"], commands)
        if not re.search(r"Exiting LEaP:\s+Errors\s*=\s*0", (ligand / "tleap.log").read_text()):
            raise ValueError("prepared GAFF2 ligand has native LEaP errors")
        (ligand / "inspect.in").write_text("summary\ncheckValidity\nprintInfo CHARGE\nquit\n")
        run(ligand, "parmed", ["/opt/ambertools/bin/parmed", "-p", "sustiva.prmtop", "-c", "sustiva.inpcrd", "-i", "inspect.in"], commands)
        tests.append({"case": "sustiva-am1bcc-gaff2", "status": "passed" if charge_ok else "failed", "atoms": len(atoms), "total_charge_e": sum(charges), "net_charge_tolerance_e": 1e-4, "charge_equivalence": args.charge_equivalence, "sqm_completed": True, "force_field_suitability_claimed": False})
        binding = args.output / "ras-raf"
        shutil.copytree(args.source / "inputs", binding)
        (binding / "mmpbsa.in").write_text(f"Official Ras-Raf tutorial bounded frame control, no entropy estimate\n&general\n startframe=1, endframe={args.frames}, interval=1, verbose=2, keep_files=2, use_sander=1,\n/\n&gb\n igb=2, saltcon=0.100, surften=0.0072,\n/\n&pb\n istrng=0.100,\n/\n")
        command = ["/opt/ambertools/bin/MMPBSA.py", "-O", "-i", "mmpbsa.in", "-o", "binding.dat", "-eo", "binding.csv", "-sp", "ras-raf_solvated.prmtop", "-cp", "ras-raf.prmtop", "-rp", "ras.prmtop", "-lp", "raf.prmtop", "-y", "prod.mdcrd"]
        run(binding, "mmpbsa-prepare-mdins", command + ["-make-mdins"], commands)
        native_gb = binding / "_MMPBSA_gb.mdin"
        shutil.copy2(native_gb, binding / "generated-default-gb.mdin")
        native, count = re.subn(r"(\bextdiel\s*=\s*)[^,\s]+", r"\g<1>78.3", native_gb.read_text())
        if count != 1:
            raise ValueError("native GB external dielectric must be explicit for the matched2010 reference control")
        native_gb.write_text(native)
        run(binding, "mmpbsa", command + ["-use-mdins"], commands)
        comparisons = []
        for component in ("complex", "receptor", "ligand"):
            reference = energy_rows(args.source / "reference" / ("_MMPBSA_" + component + "_gb.mdout"))[:args.frames]
            actual = energy_rows(binding / ("_MMPBSA_" + component + "_gb.mdout.0"))
            if len(reference) != args.frames or len(actual) != args.frames:
                raise ValueError("binding analysis did not cover exactly the declared tutorial frames")
            for term in ("BOND", "ANGLE", "DIHED", "VDWAALS", "EEL", "EGB"):
                differences = [abs(a[term] - b[term]) for a, b in zip(actual, reference)]
                # Reference values are printed to4decimal places;0.01kcal/mol
                # is a predeclared version-comparison gate, not scientific error.
                comparisons.append({"component": component, "term": term, "max_abs_difference_kcal_mol": max(differences), "tolerance_kcal_mol": 0.01, "status": "passed" if max(differences) <= 0.01 else "failed"})
        tests.append({"case": "ras-raf-gb-pb", "status": "passed" if all(item["status"] == "passed" for item in comparisons) else "failed", "frames": args.frames, "reference": "official2010SANDER10 tutorial per-frame GB energies, unchanged topology/radii; explicit native-use-mdins sets extdiel78.3 to match original reference instead of current generated80.0", "comparisons": comparisons, "pb_calculation_executed": True, "pb_version_accuracy_control": "not inferred from GB comparison", "entropy_included": False, "free_energy_convergence_claimed": False})
        if any(test["status"] != "passed" for test in tests):
            status = "failed"
    except Exception as exc:
        status, error = "failed", str(exc)
    result = {"status": status, "error": error, "tests": tests, "commands": commands, "sources": [{"file": str(args.source / name), "sha256": hashlib.sha256((args.source / name).read_bytes()).hexdigest()} for name in ("sustiva_new.pdb", "ras-raf_top_mdcrd.tgz", "pb_gb_output1.tgz")], "worker_api_tested": False, "customer_ready": False}
    (args.output / "advanced-native.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if status == "passed" else 1)


if __name__ == "__main__":
    main()
