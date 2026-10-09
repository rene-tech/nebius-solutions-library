"""Legacy GB numerical control using exact published Cartesian trajectory bytes.

Separate from full MMPBSA workflow validation: newer CPPTRAJ alignment/ASCII
rounding must not be confused with evaluating identical coordinates.
"""

import argparse
import hashlib
import json
import math
import re
import shutil
from pathlib import Path

from advanced_native import run


def energy_rows(path):
    rows, current = [], None
    for line in path.read_text().splitlines():
        if "BOND" in line and "ANGLE" in line and "DIHED" in line:
            current = {}
            rows.append(current)
        if current is not None:
            for name in ("BOND", "ANGLE", "DIHED", "VDWAALS", "EEL", "EGB"):
                if match := re.search(rf"(?<!1-4 )\b{name}\s*=\s*(\S+)", line):
                    current[name] = float(match.group(1))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=5)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    tests, commands = [], []
    status, error = "passed", None
    try:
        for component, top in (("complex", "ras-raf.prmtop"), ("receptor", "ras.prmtop"), ("ligand", "raf.prmtop")):
            root = args.output / component
            root.mkdir()
            topology = args.source / "inputs" / top
            atom_match = re.search(r"%FLAG POINTERS[^\n]*\n%FORMAT[^\n]*\n\s*(\d+)", topology.read_text())
            if atom_match is None:
                raise ValueError("native topology POINTERS block not found")
            atoms = int(atom_match.group(1))
            original = args.source / "reference" / ("_MMPBSA_" + component + ".mdcrd")
            lines = original.read_bytes().splitlines(keepends=True)
            frame_lines = math.ceil(3 * atoms / 10)
            raw = b"".join(lines[:1 + args.frames * frame_lines])
            (root / "coordinates.mdcrd").write_bytes(raw)
            values = [float(line[index:index + 8]) for line in raw.decode().splitlines()[1:] for index in range(0, len(line), 8) if line[index:index + 8].strip()]
            if len(values) != args.frames * atoms * 3:
                raise ValueError("exact source trajectory frame/atom slicing failed")
            shutil.copy2(topology, root / "system.prmtop")
            native = args.source / "native-v2-matched-gb/ras-raf"
            shutil.copy2(native / "_MMPBSA_gb.mdin", root / "gb.in")
            shutil.copy2(native / ("_MMPBSA_dummy" + component + ".inpcrd"), root / "dummy.inpcrd")
            run(root, "sander", ["/opt/ambertools/bin/sander", "-O", "-i", "gb.in", "-p", "system.prmtop", "-c", "dummy.inpcrd", "-y", "coordinates.mdcrd", "-o", "gb.mdout", "-r", "unused.rst7"], commands)
            actual = energy_rows(root / "gb.mdout")
            reference_path = args.source / "reference" / ("_MMPBSA_" + component + "_gb.mdout")
            reference = energy_rows(reference_path)[:args.frames]
            if len(actual) != len(reference) or len(actual) != args.frames:
                raise ValueError("native rerun lacks exact declared frame coverage")
            for term in ("BOND", "ANGLE", "DIHED", "VDWAALS", "EEL", "EGB"):
                difference = max(abs(a[term] - b[term]) for a, b in zip(actual, reference))
                tests.append({"component": component, "term": term, "max_abs_difference_kcal_mol": difference, "tolerance_kcal_mol": 0.01, "status": "passed" if math.isfinite(difference) and difference <= 0.01 else "failed"})
            evidence = {"atoms": atoms, "frames": args.frames, "source_trajectory_sha256": hashlib.sha256(original.read_bytes()).hexdigest(), "exact_source_prefix_sha256": hashlib.sha256(raw).hexdigest(), "topology_sha256": hashlib.sha256(topology.read_bytes()).hexdigest(), "reference_energy_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(), "native_energy_sha256": hashlib.sha256((root / "gb.mdout").read_bytes()).hexdigest(), "coordinate_realignment_applied": False, "coordinate_values_reformatted": False}
            (root / "provenance.json").write_text(json.dumps(evidence, indent=2) + "\n")
        if any(test["status"] != "passed" for test in tests):
            status = "failed"
    except Exception as exc:
        status, error = "failed", str(exc)
    result = {"status": status, "error": error, "tests": tests, "commands": commands, "reference": "official2010Ras-RafGB,SANDER10;fixed topology,radii,igb2,salt0.1,extdiel78.3", "coordinate_control": "exact first5 published component trajectory frames,not current CPPTRAJ-aligned outputs", "whole_mmpbsa_workflow_qualified_here": False, "pb_accuracy_qualified_here": False, "free_energy_convergence_claimed": False}
    (args.output / "fixed-coordinate-accuracy.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if status == "passed" else 1)


if __name__ == "__main__":
    main()
