"""Run0 check of repeated orthogonal conversion after an orthogonal restart."""
import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess

from screen import INPUTS, configuration, native_environment, sha


def validate(output):
    receipt = json.loads((output / "receipt.json").read_text())
    if receipt["status"] != "native-complete":
        raise ValueError("incomplete native restart check")
    values = []
    for row in receipt["results"]:
        case = output / row["variant"]
        if row["exit_code"] != 0 or any(sha(case / name) != value for name, value in row["files"].items()):
            raise ValueError("native restart artifact identity changed")
        lines = (case / "initial-forces.lammpstrj").read_text().splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith("ITEM: ATOMS "))
        names = lines[header].split()[2:]
        atoms = sorted(([float(value) for value in line.split()] for line in lines[header + 1:]), key=lambda atom: atom[names.index("id")])
        if len(atoms) != 6598 or [atom[names.index("id")] for atom in atoms] != list(range(1, 6599)):
            raise ValueError("incomplete native atom identity")
        vectors = [[atom[names.index(name)] for name in ("x", "y", "z", "fx", "fy", "fz")] for atom in atoms]
        energies = [float(v) for v in (case / "initial-energy.txt").read_text().split()]
        if len(energies) != 9 or not all(math.isfinite(v) for atom in vectors for v in atom) or not all(map(math.isfinite, energies)):
            raise ValueError("nonfinite native physical outputs")
        values.append((vectors, energies))
    delta = [[abs(a - b) for a, b in zip(first, second)] for first, second in zip(values[0][0], values[1][0])]
    coordinates = max(max(atom[:3]) for atom in delta)
    forces = max(max(atom[3:]) for atom in delta)
    energies = [abs(a - b) for a, b in zip(values[0][1], values[1][1])]
    if coordinates > 1e-10 or forces > 1e-5 or max(energies) > 1e-5:
        raise ValueError("repeated orthogonal conversion changed native physical outputs")
    report = {"status": "passed", "scope": receipt["scope"], "atoms": 6598,
              "max_coordinate_delta_A": coordinates, "max_force_delta_kcal_mol_A": forces,
              "decomposed_energy_abs_delta_kcal_mol": energies,
              "native_receipt_sha256": sha(output / "receipt.json")}
    with (output / "validation.json").open("x") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
    return report


def run(source, output, executable):
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for variant in ("baseline-triclinic", "orthogonal"):
        case = output / variant
        case.mkdir()
        for name in INPUTS:
            shutil.copyfile(source / ("benchmark.restart" if name == "probe.restart" else name), case / name)
        (case / "singlepoint.in").write_text(configuration(variant, 1, 0, static=True))
        command = [executable, "-k", "on", "g", "1", "t", "1", "-sf", "kk", "-log", "none", "-in", "singlepoint.in"]
        with (case / "native.log").open("w") as stream:
            result = subprocess.run(command, cwd=case, env=native_environment(executable), stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=120)
        row = {"variant": variant, "exit_code": result.returncode, "command": command,
               "restart_sha256": sha(case / "probe.restart"), "input_sha256": sha(case / "singlepoint.in"),
               "log_sha256": sha(case / "native.log"), "files": {p.name: sha(p) for p in case.iterdir()}}
        records.append(row)
    report = {"status": "native-complete" if all(r["exit_code"] == 0 for r in records) else "failed",
              "source": str(source), "scope": "already-orthogonal restart; independent run0 numerical comparison", "results": records}
    (output / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable", default="/usr/local/lammps/sm90/bin/lmp")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        validate(args.output)
        raise SystemExit(0)
    if args.source is None:
        parser.error("--source is required for native execution")
    report = run(args.source, args.output, args.executable)
    raise SystemExit(0 if report["status"] == "native-complete" else 1)
