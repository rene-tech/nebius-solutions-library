#!/usr/bin/env python3
"""Run a bounded same-state 1000-step native kernel-placement comparison in Pod.

No targets, force-field parameters, cutoff, mesh, timestep or seeds are tuned.
The exact CUDA+Serial image may reject t4/t8; such failures remain evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host-only", action="store_true", help="bounded follow-up for existing Kokkos host PPPM and zero-tilt orthogonal representation")
    args = parser.parse_args()
    args.output.mkdir(exist_ok=False)
    binary = "/usr/local/lammps/sm90/bin/lmp"
    base_script = "package kokkos neigh half newton on\nunits real\natom_style full\nboundary p p p\nread_restart probe.restart\ninclude restart-coefficients.inc\ninclude nonbonded.inc\ninclude thermo.inc\nfix thermal all langevin 300.0 300.0 1000.0 20260925 zero yes\ninclude constraints.inc\nfix integrate all nph iso 0.9869232667160128 0.9869232667160128 2000.0 ptemp 300.0 mtk yes pchain 3\ndump coordinates all custom 500 benchmark.lammpstrj id type x y z vx vy vz\ndump_modify coordinates sort id format float %.15g\nrun 1000\nwrite_restart benchmark.restart\n"
    variants = [("gpu-t1", 1, "gpu"), ("gpu-t4", 4, "gpu"), ("gpu-t8", 8, "gpu"), ("cpu-pppm-t1", 1, "cpu-pppm"), ("cpu-all-serial", 1, "cpu-all")]
    if args.host_only:
        variants = [("kk-host-pppm", 1, "kk-host"), ("gpu-orthogonal", 1, "ortho")]
    results = []
    for name, threads, placement in variants:
        directory = args.output / name
        directory.mkdir()
        for filename in ("probe.restart", "restart-coefficients.inc", "nonbonded.inc", "thermo.inc", "constraints.inc"):
            shutil.copy2(args.source / filename, directory / filename)
        if placement == "cpu-pppm":
            path = directory / "nonbonded.inc"
            text = path.read_text().replace("kspace_style pppm 1e-5\n", "suffix off\nkspace_style pppm 1e-5\nsuffix kk\n")
            path.write_text(text)
        if placement == "kk-host":
            path = directory / "nonbonded.inc"
            path.write_text(path.read_text().replace("kspace_style pppm 1e-5\n", "kspace_style pppm/kk/host 1e-5\n"))
        script = base_script
        if placement == "cpu-all":
            script = script.replace("package kokkos neigh half newton on\n", "")
        if placement == "ortho":
            script = script.replace("include nonbonded.inc\n", "include nonbonded.inc\nchange_box all ortho\n")
        (directory / "benchmark.in").write_text(script)
        command = [binary] + (["-k", "on", "g", "1", "t", str(threads), "-sf", "kk"] if placement != "cpu-all" else []) + ["-log", "none", "-in", "benchmark.in"]
        before = time.monotonic()
        with (directory / "native.log").open("wb") as log:
            completed = subprocess.run(command, cwd=directory, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "OMP_NUM_THREADS": str(threads), "LD_LIBRARY_PATH": "/usr/local/lammps/sm90/lib:" + os.environ.get("LD_LIBRARY_PATH", "")}, timeout=180)
        text = (directory / "native.log").read_text()
        loops = re.findall(r"Loop time of ([0-9.eE+-]+) on .*? for (\d+) steps", text)
        result = {"variant": name, "command": command, "exit_code": completed.returncode, "wall_seconds": time.monotonic() - before, "loops": loops, "steps": 1000, "initial_restart_sha256": digest(directory / "probe.restart"), "log_sha256": digest(directory / "native.log"), "physical_inputs_unchanged": True, "production_evidence": False}
        results.append(result)
        (args.output / "benchmark.json").write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
