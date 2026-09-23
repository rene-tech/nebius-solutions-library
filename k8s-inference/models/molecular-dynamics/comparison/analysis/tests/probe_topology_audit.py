#!/usr/bin/env python3
"""Negative scientific-integrity tests against a caller-selected topology audit.

Run on copies only. These deliberate topology changes are NOT simulations and
never modify the canonical/conversion source. A valid audit must reject them.
Kept outside automatic unit discovery because the parent owns the audit module
and exact master/conversion fixture. Receipts retain every unexpected pass.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def change_input(directory, text):
    path = directory / "lammps/system.input"
    native = path.read_text()
    path.write_text(native.replace("thermo_style", text + "\nthermo_style", 1))


def duplicate_bond(directory):
    path = directory / "lammps/system.lmp"
    text = path.read_text()
    count = int(re.search(r"^(\d+) bonds$", text, re.M).group(1))
    first = re.search(r"(?m)^Bonds[^\n]*\n\s*\n(\s*\d+[^\n]+)", text).group(1).split()
    extra = " ".join([str(count + 1), *first[1:]])
    text = re.sub(r"(?m)^\d+ bonds$", f"{count + 1} bonds", text, count=1)
    text = text.replace("\nAngles\n", f"\n{extra}\n\nAngles\n", 1)
    path.write_text(text)


def tilted_cell(directory):
    path = directory / "lammps/system.lmp"
    path.write_text(re.sub(r"(?m)^\s*\S+\s+\S+\s+\S+ xy xz yz$", " 1.0 0.0 0.0 xy xz yz", path.read_text(), count=1))


def cross_nbfix(directory):
    path = directory / "gromacs/system.top"
    path.write_text(path.read_text().replace("[ moleculetype ]", "[ nonbond_params ]\nCT OW 1 0.9 20.0\n\n[ moleculetype ]", 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--converted", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rehash-mutants", action="store_true", help="Also test semantic gates independently of stale manifest hashes")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be new; preserve prior audit evidence")
    args.output.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location("reviewed_topology_audit", args.audit)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inputs = {str(p.resolve()): digest(p) for p in [args.audit, *args.master.glob("*"), *args.converted.rglob("*")] if p.is_file()}
    baseline = module.audit(args.master, args.converted)
    (args.output / "baseline.json").write_text(json.dumps(baseline, indent=2) + "\n")
    mutations = {
        "lammps-off-diagonal-lj": lambda p: change_input(p, "pair_coeff 2 9 5.0 9.0"),
        "lammps-later-mixing-override": lambda p: change_input(p, "pair_modify mix geometric"),
        "lammps-later-special-bonds-override": lambda p: change_input(p, "special_bonds lj 0.0 0.0 0.9 coul 0.0 0.0 0.4"),
        "lammps-duplicate-bond": duplicate_bond,
        "lammps-nonzero-tilt": tilted_cell,
        "gromacs-cross-nbfix": cross_nbfix,
    }
    results = []
    for name, mutate in mutations.items():
        directory = args.output / name
        shutil.copytree(args.converted, directory)
        mutate(directory)
        if args.rehash_mutants:
            path = directory / "conversion.json"
            manifest = json.loads(path.read_text())
            for row in manifest["files"]:
                artifact = directory / row["path"]
                row.update(sha256=digest(artifact), bytes=artifact.stat().st_size)
            manifest["synthetic_negative_fixture"] = name
            path.write_text(json.dumps(manifest, indent=2) + "\n")
        try:
            result = module.audit(args.master, directory)
            rejected = result["status"] != "passed"
        except Exception as exc:
            result = {"status": "rejected-with-error", "error": f"{type(exc).__name__}: {exc}"}
            rejected = True
        result.update(negative_fixture=name, expected="reject deliberate physical-parameter change", rejected=rejected, fixture_files={str(path.relative_to(directory)): digest(path) for path in directory.rglob("*") if path.is_file()})
        (directory / "negative-test-receipt.json").write_text(json.dumps(result, indent=2) + "\n")
        results.append({"case": name, "rejected": rejected, "observed_status": result["status"]})
        print(json.dumps(results[-1]), flush=True)
    unchanged = all(digest(Path(path)) == value for path, value in inputs.items())
    report = {"kind": "negative scientific-integrity tests; no dynamics executed", "audit_source_sha256": inputs[str(args.audit.resolve())], "baseline_status": baseline["status"], "results": results, "source_inputs_unchanged": unchanged, "input_hashes": inputs, "status": "passed" if baseline["status"] == "passed" and unchanged and all(x["rejected"] for x in results) else "failed"}
    (args.output / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
