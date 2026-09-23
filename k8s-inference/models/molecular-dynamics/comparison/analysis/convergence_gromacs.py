#!/usr/bin/env python3
"""Bounded CPU-only GROMACS run0 mesh diagnostic on immutable-input copies."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from compare import file_receipt, source_identity, write_json
from static_compare import gromacs_energy


def diagnostic_mdp(text, mesh):
    """Require a genuine static baseline; change only the four mesh controls."""
    settings = {}
    for line in text.splitlines():
        content = line.split(";", 1)[0].strip()
        if not content:
            continue
        key, value = (part.strip() for part in content.split("=", 1))
        key = key.lower().replace("_", "-")
        if key in settings:
            raise ValueError(f"duplicate native setting {key}")
        settings[key] = value.lower()
    for key, expected in {"nsteps": "0", "constraints": "none", "coulombtype": "pme", "tcoupl": "no", "pcoupl": "no", "dispcorr": "no"}.items():
        if settings.get(key) != expected:
            raise ValueError(f"static diagnostic requires {key}={expected}")
    if mesh not in (96, 128):
        raise ValueError("this bounded diagnostic only permits96 or128 cubed")
    for key, value in (("fourier-nx", mesh), ("fourier-ny", mesh), ("fourier-nz", mesh), ("pme-order", 6)):
        text, count = re.subn(rf"(?m)^{key}\s*=\s*\d+\s*$", f"{key} = {value}", text)
        if count != 1:
            raise ValueError(f"missing/duplicate native setting {key}")
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source_receipt = json.loads((args.source / "receipt.json").read_text())
    runtime = source_receipt["image"]
    if not re.fullmatch(r"\S+@sha256:[0-9a-f]{64}", runtime):
        raise ValueError("exact baseline runtime image required")
    original = [file_receipt(args.source / name) for name in ("system.top", "system.gro", "tail-off.mdp", "master-manifest.json", "protocol.json", "receipt.json")]
    report = {"status": "incomplete", "scope": "same-potential CPU run0 numerical diagnostic only, not new canonical protocol", "source": source_identity(), "source_files": original, "image": runtime, "uses_gpu": False, "integration_steps": 0, "commands": [], "variants": []}
    try:
        for mesh in (96, 128):
            work = args.output / f"mesh{mesh}-order6"
            work.mkdir()
            for name in ("system.top", "system.gro", "master-manifest.json", "protocol.json"):
                shutil.copy2(args.source / name, work / name)
            mdp = diagnostic_mdp((args.source / "tail-off.mdp").read_text(), mesh)
            (work / "diagnostic.mdp").write_text(mdp)
            base = ["docker", "run", "--rm", "--interactive", "--user", f"{os.getuid()}:{os.getgid()}", "--cpus", "4", "--env", "OMP_NUM_THREADS=4", "--entrypoint", "/usr/local/gromacs/avx2_256/bin/gmx", "--mount", f"type=bind,source={work.resolve()},target=/work", "--workdir", "/work", runtime]
            commands = [("grompp", ["grompp", "-f", "diagnostic.mdp", "-c", "system.gro", "-p", "system.top", "-o", "diagnostic.tpr"], ""),
                        ("mdrun", ["mdrun", "-s", "diagnostic.tpr", "-deffnm", "diagnostic", "-ntmpi", "1", "-ntomp", "4", "-nb", "cpu", "-pme", "cpu", "-bonded", "cpu", "-update", "cpu"], ""),
                        ("energy", ["energy", "-f", "diagnostic.edr", "-o", "diagnostic-energy.xvg"], "Bond\nAngle\nProper-Dih.\nPer.-Imp.-Dih.\nLJ-14\nCoulomb-14\nLJ-(SR)\nCoulomb-(SR)\nCoul.-recip.\nPotential\n0\n")]
            for label, command, stdin in commands:
                started = time.monotonic()
                result = subprocess.run(base + command, input=stdin, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
                (work / (label + ".console.log")).write_text(result.stdout)
                report["commands"].append({"mesh": mesh, "argv": base + command, "stdin": stdin, "seconds": time.monotonic() - started, "returncode": result.returncode})
                if result.returncode:
                    raise RuntimeError(f"native {label} failed; retained console log")
            energy = gromacs_energy(work / "diagnostic-energy.xvg", "off")
            report["variants"].append({"mesh": [mesh] * 3, "order": 6, "energy": energy, "files": [file_receipt(path) for path in sorted(work.iterdir()) if path.is_file()]})
        if [file_receipt(record["path"]) for record in original] != original:
            raise ValueError("canonical source changed during diagnostic")
        report["status"] = "native-cpu-diagnostic-completed; interpret convergence separately"
        report["canonical_inputs_unchanged"] = True
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        write_json(args.output / "receipt.json", report)


if __name__ == "__main__":
    main()
