"""Retain exact-image CPU single-point diagnostics; no trajectory is generated."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643"


def run(fixture, output):
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((fixture / "fixture-manifest.json").read_text())
    for row in manifest["files"]:
        source = fixture / "data" / row["path"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("Source fixture no longer matches its frozen manifest")
        shutil.copyfile(source, output / row["path"])
    base = ["docker", "run", "--rm", "--interactive", "--user", f"{os.getuid()}:{os.getgid()}",
            "--cpus", "4", "--env", "OMP_NUM_THREADS=4", "--entrypoint",
            "/usr/local/gromacs/avx2_256/bin/gmx", "--mount",
            f"type=bind,source={output.resolve()},target=/work", "--workdir", "/work", IMAGE]
    commands = []

    def execute(label, command, stdin=""):
        start = time.monotonic()
        with (output / (label + ".console.log")).open("wb") as stream:
            result = subprocess.run(base + command, input=stdin.encode(), stdout=stream,
                                    stderr=subprocess.STDOUT, check=False, timeout=300)
        commands.append({"label": label, "argv": base + command, "stdin": stdin,
                         "returncode": result.returncode, "seconds": time.monotonic() - start})
        (output / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        if result.returncode:
            raise RuntimeError("Exact native step failed; retained " + label + ".console.log")

    execute("version", ["--version"])
    for name in ("tail-on", "tail-off"):
        execute(name + "-grompp", ["grompp", "-f", name + ".mdp", "-c", "system.gro",
                                  "-p", "system.top", "-o", name + ".tpr"])
        execute(name + "-mdrun", ["mdrun", "-s", name + ".tpr", "-deffnm", name,
                                 "-ntmpi", "1", "-ntomp", "4", "-nb", "cpu", "-pme", "cpu",
                                 "-bonded", "cpu", "-update", "cpu"])
        execute(name + "-extract", ["energy", "-f", name + ".edr", "-o", name + "-energy.xvg"],
                "Bond\nAngle\nProper-Dih.\nPer.-Imp.-Dih.\nLJ-14\nCoulomb-14\nLJ-(SR)\nCoulomb-(SR)\nCoul.-recip.\nPotential\n" + ("Disper.-corr.\n" if name == "tail-on" else "") + "0\n")
    return {"status": "native-diagnostic-completed-not-cross-engine-validated", "image": IMAGE,
            "integration_steps": 0, "uses_gpu": False, "commands": commands}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = run(args.fixture, args.output)
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(receipt["status"])
