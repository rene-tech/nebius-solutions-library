"""Matched-input CUDA architecture benchmark, inside one task-owned GPU Pod.

The first full run has a fresh private CUDA cache. Three subsequent full runs
reuse it. CUDA initialization is included in process wall; native ns/day is a
different, engine-reported metric. Every run and failure remains in the ledger.
No scientific or output parameters are changed by this harness.
"""

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import time
from pathlib import Path


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def run_logged(argv, output, *, cwd=None, stdin=None, env=None, timeout=600):
    with output.open("wb") as log:
        try:
            result = subprocess.run(
                argv, cwd=cwd, input=stdin, stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout, env=env,
            )
            return result.returncode
        except subprocess.TimeoutExpired:
            return 124


def expected_trajectories(mdp):
    values = {}
    for line in mdp.splitlines():
        key, separator, value = line.split(";", 1)[0].partition("=")
        if separator:
            values[key.strip().replace("_", "-")] = value.strip()
    expected = []
    if any(int(values.get(key, "0")) > 0 for key in ("nstxout", "nstvout", "nstfout")):
        expected.append("md.trr")
    if int(values.get("nstxout-compressed", "0")) > 0:
        expected.append("md.xtc")
    return expected


def cpu_stat():
    """Pod cgroup counters include the low-frequency GPU sampler too."""
    path = Path("/sys/fs/cgroup/cpu.stat")
    return {
        key: int(value)
        for key, value in (line.split() for line in path.read_text().splitlines())
    } if path.exists() else {}


def validate(binary, directory, expected_steps, expected_time, trajectories=()):
    result = {}
    # The TPR decides whether a trajectory is required; never add output or drop
    # an existing obligation to change a throughput result.
    for name, arguments in [("energy-check", ["check", "-e", "md.edr"])]:
        result[name] = run_logged([binary, *arguments], directory / (name + ".log"), cwd=directory)
    for trajectory in sorted(directory.glob("md.*")):
        if trajectory.suffix in {".xtc", ".trr", ".tng"}:
            result[trajectory.name] = run_logged(
                [binary, "check", "-f", trajectory.name],
                directory / (trajectory.name + ".check.log"), cwd=directory,
            )
    result["energy-extract"] = run_logged(
        [binary, "energy", "-f", "md.edr", "-o", "energy-validation.xvg"],
        directory / "energy-extract.log", cwd=directory,
        stdin=b"Potential\nKinetic-En.\nTotal-Energy\nTemperature\nPressure\n0\n",
    )
    rows = []
    energy = directory / "energy-validation.xvg"
    if energy.exists():
        for line in energy.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith(("#", "@")):
                rows.append([float(value) for value in line.split()])
    result["energy_finite"] = bool(rows) and all(math.isfinite(value) for row in rows for value in row)
    result["energy_rows"] = len(rows)
    result["energy_final_time_ps"] = rows[-1][0] if rows else None
    result["expected_time_ps"] = expected_time
    # Native checkpoint parsing reads the whole file. Retain its metadata without
    # storing the millions of coordinate/velocity lines from a large system.
    checkpoint = subprocess.Popen(
        [binary, "dump", "-cp", "md.cpt"], cwd=directory,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    metadata = []
    checkpoint_step = None
    for line in checkpoint.stdout:
        if len(metadata) < 100:
            metadata.append(line)
        match = re.match(r"\s*step\s*=\s*(\d+)", line)
        if match:
            checkpoint_step = int(match.group(1))
    stderr = checkpoint.stderr.read()
    result["checkpoint_parse_exit"] = checkpoint.wait(timeout=30)
    (directory / "checkpoint-header.txt").write_text("".join(metadata) + stderr)
    result["checkpoint_step"] = checkpoint_step
    result["expected_steps"] = expected_steps
    result["final_coordinates_nonempty"] = (directory / "md.gro").stat().st_size > 0
    result["expected_trajectories"] = list(trajectories)
    result["trajectory_obligations_met"] = all(
        (directory / name).is_file() and (directory / name).stat().st_size > 0
        for name in trajectories
    )
    result["passed"] = (
        all(value == 0 for key, value in result.items() if key.endswith("check") or key.endswith(".xtc") or key.endswith(".trr") or key.endswith(".tng"))
        and result["energy-extract"] == 0 and result["energy_finite"]
        and result["checkpoint_parse_exit"] == 0 and checkpoint_step == expected_steps
        and math.isclose(rows[-1][0], expected_time, abs_tol=1e-5)
        and result["final_coordinates_nonempty"]
        and result["trajectory_obligations_met"]
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="/usr/local/gromacs/avx2_256/bin/gmx")
    parser.add_argument("--tpr", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-steps", type=int, required=True)
    parser.add_argument("--expected-time-ps", type=float, required=True)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--pin", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--bonded", choices=("auto", "gpu", "cpu"), default="auto")
    parser.add_argument("--nstlist", type=int)
    parser.add_argument("--warm-repetitions", type=int, default=3)
    args = parser.parse_args()
    if sha256(args.tpr) != args.sha256:
        parser.error("Input checksum differs from the declared immutable fixture")
    args.output.mkdir(parents=True, exist_ok=False)
    cache = args.output.resolve() / "cuda-cache"
    cache.mkdir()
    env = {**os.environ, "CUDA_CACHE_PATH": str(cache), "OMP_NUM_THREADS": str(args.threads)}
    for name, command in {
        "version.txt": [args.binary, "--version"],
        "gpu.txt": ["nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total,power.limit", "--format=csv"],
        "cpu.txt": ["lscpu"],
        "tpr.mdp": [args.binary, "dump", "-s", str(args.tpr), "-om", str(args.output.resolve() / "input.mdp")],
    }.items():
        # -om asks for the original run parameters without dumping coordinates.
        if name == "tpr.mdp":
            command.append("-quiet")
        run_logged(command, args.output / name, timeout=120)
    cgroup = {}
    for path in ["/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/cpuset.cpus.effective", "/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "/sys/fs/cgroup/cpu/cpu.cfs_period_us"]:
        if Path(path).exists():
            cgroup[path] = Path(path).read_text().strip()
    (args.output / "environment.json").write_text(json.dumps({
        "tpr_sha256": args.sha256, "cgroup": cgroup,
        "affinity": sorted(os.sched_getaffinity(0)), "cuda_cache_path": str(cache),
        "threads": args.threads, "binary": args.binary,
        "cuda_dispatch_environment": {key: env[key] for key in ("CUDA_DISABLE_PTX_JIT", "CUDA_FORCE_PTX_JIT", "CUDA_MODULE_LOADING") if key in env},
    }, indent=2) + "\n")
    trajectories = expected_trajectories((args.output / "input.mdp").read_text())
    records = []
    cohorts = ["fresh-cache"] + [f"warm-{i+1}" for i in range(args.warm_repetitions)]
    for cohort in cohorts:
        directory = args.output / cohort
        directory.mkdir()
        argv = [args.binary, "mdrun", "-s", str(args.tpr.resolve()), "-deffnm", "md", "-ntmpi", "1", "-ntomp", str(args.threads), "-pin", args.pin, "-nb", "auto", "-pme", "auto", "-bonded", args.bonded, "-update", "auto"]
        if args.nstlist is not None:
            argv += ["-nstlist", str(args.nstlist)]
        cpu_before = cpu_stat()
        with (directory / "gpu-samples.csv").open("wb") as samples:
            monitor = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=timestamp,utilization.gpu,utilization.memory,memory.used,power.draw,clocks.sm,clocks.mem", "--format=csv", "--loop-ms=200"],
                stdout=samples, stderr=subprocess.STDOUT,
            )
            started = time.monotonic()
            try:
                code = run_logged(["/usr/bin/time", "-v", "-o", str(directory.resolve() / "time.txt"), *argv], directory / "command.log", cwd=directory, env=env)
            finally:
                elapsed = time.monotonic() - started
                monitor.terminate()
                monitor.wait(timeout=10)
        cpu_after = cpu_stat()
        log = (directory / "command.log").read_text(errors="replace")
        performance = re.findall(r"Performance:\s+([\d.eE+-]+)", log)
        record = {
            "cohort": cohort, "argv": argv, "input_sha256": args.sha256,
            "exit_code": code, "process_wall_seconds": elapsed,
            "native_ns_per_day": float(performance[-1]) if performance else None,
            "cuda_cache_files_after": sum(path.is_file() for path in cache.rglob("*")),
            "pod_cpu_stat_delta": {
                key: value - cpu_before[key]
                for key, value in cpu_after.items() if key in cpu_before
            },
        }
        if code == 0:
            try:
                record["validation"] = validate(args.binary, directory, args.expected_steps, args.expected_time_ps, trajectories)
            except Exception as error:
                record["validation"] = {"passed": False, "error": str(error)}
        record["output_inventory"] = [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in sorted(directory.iterdir()) if path.is_file()
        ]
        records.append(record)
        (args.output / "measurements.json").write_text(json.dumps(records, indent=2) + "\n")
        print(json.dumps({key: value for key, value in record.items() if key != "output_inventory"}), flush=True)
    successful = [record["native_ns_per_day"] for record in records if record["cohort"].startswith("warm-") and record.get("validation", {}).get("passed")]
    (args.output / "summary.json").write_text(json.dumps({
        "runs": len(records), "validated_warm_runs": len(successful),
        "warm_median_ns_per_day": statistics.median(successful) if successful else None,
        "warm_min_ns_per_day": min(successful) if successful else None,
        "warm_max_ns_per_day": max(successful) if successful else None,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
