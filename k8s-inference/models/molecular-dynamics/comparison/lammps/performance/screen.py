"""Bounded exact-image NPT controls; never change canonical physics or rebuild."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


INPUTS = ("probe.restart", "restart-coefficients.inc", "nonbonded.inc", "thermo.inc", "constraints.inc")
ENERGIES = "ebond eangle edihed eimp evdwl ecoul elong etail pe".split()


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def native_environment(executable, *, launch_blocking=False):
    env = os.environ.copy()
    # Match the existing worker's architecture-specific shared-library setup.
    env["LD_LIBRARY_PATH"] = str(Path(executable).parent.parent / "lib") + ":" + env.get("LD_LIBRARY_PATH", "")
    env["OMP_NUM_THREADS"] = "1"
    if launch_blocking:
        env["CUDA_LAUNCH_BLOCKING"] = "1"
    else:
        env.pop("CUDA_LAUNCH_BLOCKING", None)
    return env


def configuration(variant, steps, warmup, *, static=False):
    if variant not in {"baseline-triclinic", "orthogonal"}:
        raise ValueError("unregistered matched control")
    if steps <= 0 or warmup < 0:
        raise ValueError("invalid bounded measurement length")
    ortho = ('if "$(xy) != 0 || $(xz) != 0 || $(yz) != 0" then "quit 17"\n'
             'change_box all ortho\n') if variant == "orthogonal" else ""
    energy = " ".join("$(" + field + ":%.15g)" for field in ENERGIES)
    common = (
        "package kokkos neigh half newton on\nunits real\natom_style full\nboundary p p p\n"
        "read_restart probe.restart\n" + ortho +
        "include restart-coefficients.inc\ninclude nonbonded.inc\ninclude thermo.inc\n"
    )
    if static:
        # An independent process: a run0 without restored fixes can discard
        # barostat restart records. Never put this before timed dynamics.
        return common + (
            "run 0\nwrite_dump all custom initial-forces.lammpstrj id type x y z fx fy fz modify sort id format float %.15g\n"
            f'print "{energy}" file initial-energy.txt screen no\n'
        )
    runs = f"run {warmup} post no\nrun {steps} pre no\n" if warmup else f"run {steps}\n"
    return common + (
        "fix thermal all langevin 300.0 300.0 1000.0 20260925 zero yes\n"
        "include constraints.inc\n"
        "fix integrate all nph iso 0.9869232667160128 0.9869232667160128 2000.0 ptemp 300.0 mtk yes pchain 3\n"
        "dump coordinates all custom 500 benchmark.lammpstrj id type x y z vx vy vz\n"
        "dump_modify coordinates sort id format float %.15g\n"
        + runs +
        "write_dump all custom final.lammpstrj id type x y z vx vy vz modify sort id format float %.15g\n"
        "write_restart benchmark.restart\n"
    )


def native_command(executable, ranks, input_file):
    if ranks not in (1, 2, 4):
        raise ValueError("only bounded single-GPU rank controls are registered")
    command = [executable, "-k", "on", "g", "1", "t", "1", "-sf", "kk", "-log", "none", "-in", input_file]
    if ranks > 1:
        command = ["mpirun", "--bind-to", "core", "-np", str(ranks)] + command
    return command


def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    source_hashes = {name: sha(args.baseline / name) for name in INPUTS}
    if source_hashes["probe.restart"] != "0458091095da737f80e38befb329ddf4a2549933767b359e58b6a00d9842ea5d":
        raise ValueError("this screen is bound to the qualified canonical probe-03 restart")
    results = []
    for repetition in range(1, args.repetitions + 1):
        for variant in args.variant:
            directory = args.output / f"{variant}-r{repetition}"
            directory.mkdir()
            for name in INPUTS:
                shutil.copyfile(args.baseline / name, directory / name)
                if sha(directory / name) != source_hashes[name]:
                    raise ValueError("source control input changed while copying")
            (directory / "benchmark.in").write_text(configuration(variant, args.steps, args.warmup))
            (directory / "singlepoint.in").write_text(configuration(variant, args.steps, args.warmup, static=True))
            command = native_command(args.executable, args.mpi_ranks, "benchmark.in")
            env = native_environment(args.executable, launch_blocking=args.launch_blocking)
            static_command = command[:-1] + ["singlepoint.in"]
            with (directory / "singlepoint.log").open("w") as stream:
                static = subprocess.run(static_command, cwd=directory, env=env, stdout=stream,
                                        stderr=subprocess.STDOUT, timeout=args.timeout_seconds)
            if args.nsys:
                command = ["nsys", "profile", "--sample=none", "--cpuctxsw=none", "--trace=cuda,nvtx",
                           "--force-overwrite=false", "-o", "timeline"] + command
            start = time.monotonic()
            with (directory / "native.log").open("w") as stream:
                native = subprocess.run(command, cwd=directory, env=env, stdout=stream,
                                        stderr=subprocess.STDOUT, timeout=args.timeout_seconds)
            elapsed = time.monotonic() - start
            log = (directory / "native.log").read_text()
            loops = [{"seconds": float(seconds), "steps": int(steps)} for seconds, steps in
                     re.findall(r"Loop time of ([\d.eE+-]+) on \d+ procs for (\d+) steps", log)]
            measured = [entry for entry in loops if entry["steps"] == args.steps]
            passed = static.returncode == native.returncode == 0 and len(measured) == 1 and (directory / "benchmark.restart").is_file()
            row = {"variant": variant, "repetition": repetition, "status": "native-complete" if passed else "failed",
                   "scientific_validation": "pending", "command": command, "exit_code": native.returncode,
                   "process_wall_seconds": elapsed, "loops": loops,
                   "measured_ns_per_day": args.steps * .000002 / measured[0]["seconds"] * 86400 if passed else None,
                   "warmup_steps": args.warmup, "measured_steps": args.steps, "source_input_sha256": source_hashes,
                   "input_sha256": sha(directory / "benchmark.in"), "log_sha256": sha(directory / "native.log"),
                   "static_command": static_command, "static_exit_code": static.returncode,
                   "static_input_sha256": sha(directory / "singlepoint.in"), "static_log_sha256": sha(directory / "singlepoint.log"),
                   "OMP_NUM_THREADS": "1", "CUDA_LAUNCH_BLOCKING": env.get("CUDA_LAUNCH_BLOCKING"),
                   "LD_LIBRARY_PATH": env["LD_LIBRARY_PATH"],
                   "mpi_ranks": args.mpi_ranks, "gpu_count": 1, "mps_enabled": False,
                   "profiler_enabled": args.nsys, "raw_path": str(directory), "production_evidence": False}
            results.append(row)
            (directory / "measurement.json").write_text(json.dumps(row, indent=2) + "\n")
            (args.output / "measurements.json").write_text(json.dumps(results, indent=2) + "\n")
            print(json.dumps(row), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable", default="/usr/local/lammps/sm90/bin/lmp")
    parser.add_argument("--variant", action="append", choices=("baseline-triclinic", "orthogonal"))
    parser.add_argument("--steps", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=200)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--mpi-ranks", type=int, choices=(1, 2, 4), default=1,
                        help="private same-GPU diagnostic only; not a released distributed execution feature")
    parser.add_argument("--launch-blocking", action="store_true")
    parser.add_argument("--nsys", action="store_true")
    args = parser.parse_args()
    args.variant = args.variant or ["baseline-triclinic", "orthogonal"]
    results = run(args)
    raise SystemExit(0 if results and all(row["status"] == "native-complete" for row in results) else 1)


if __name__ == "__main__":
    main()
