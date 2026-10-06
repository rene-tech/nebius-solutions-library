"""Matched, finite experiments inside an isolated pod; scientific inputs unchanged."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time

import benchmark_sm89 as baseline
import screen_inside as topology_tools

BINARY = "/usr/local/gromacs/avx2_256/bin/gmx"
TPR_SHA256 = topology_tools.TPR_SHA256


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def finite_input(root, original, steps):
    target = root / f"steps-{steps}.tpr"
    if target.exists():
        return target
    if baseline.run_logged([BINARY, "convert-tpr", "-s", str(original), "-o", str(target), "-nsteps", str(steps)], root / f"convert-{steps}.log"):
        raise ValueError("Finite TPR conversion failed")
    baseline.run_logged([BINARY, "check", "-s1", str(original), "-s2", str(target)], root / f"tpr-check-{steps}.log")
    mdp = root / f"steps-{steps}.mdp"
    if baseline.run_logged([BINARY, "dump", "-s", str(target), "-om", str(mdp)], root / f"dump-{steps}.log"):
        raise ValueError("Finite TPR metadata extraction failed")
    original_mdp = baseline.mdp_values((root / "original.mdp").read_text())
    finite_mdp = baseline.mdp_values(mdp.read_text())
    difference = topology_tools.parameter_difference(original_mdp, finite_mdp)
    save(root / f"parameters-{steps}.json", difference)
    return target


def checkpoint_step(binary, directory):
    process = subprocess.Popen([binary, "dump", "-cp", "md.cpt"], cwd=directory,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    step = None
    for line in process.stdout:
        match = re.match(r"\s*step\s*=\s*(\d+)", line)
        if match:
            step = int(match.group(1))
    if process.wait(timeout=30) or step is None:
        raise ValueError("Native checkpoint parsing failed")
    return step


def environment(root, threads, selected=None):
    env = {**os.environ, "OMP_NUM_THREADS": str(threads), "CUDA_CACHE_PATH": str(root / "cuda-cache")}
    for key in ("GMX_CUDA_GRAPH", "OMP_PLACES", "OMP_PROC_BIND", "GOMP_CPU_AFFINITY"):
        env.pop(key, None)
    prefix = []
    if selected:
        env.update(OMP_PROC_BIND="close", OMP_PLACES=",".join("{" + str(c) + "}" for c in selected))
        prefix = ["taskset", "--cpu-list", ",".join(map(str, selected))]
    return env, prefix


def measured_run(root, tpr, case, repeat, steps, *, segmented=False, segment_minutes=5):
    directory = root / f"{case['name']}-r{repeat}"
    directory.mkdir()
    env, prefix = environment(root, case["threads"], case.get("selected"))
    binary = case.get("binary", BINARY)
    base = [*prefix, binary, "mdrun", "-s", str(tpr), "-deffnm", "md", "-ntmpi", "1",
            "-ntomp", str(case["threads"]), "-pin", "auto", "-nb", "gpu", "-pme", "auto",
            "-bonded", "gpu", "-update", "auto", "-nstlist", "200", "-cpt", str(segment_minutes)]
    native = []
    process_wall = 0.0
    start_step = 0
    before = baseline.cpu_stat()
    with (directory / "gpu-samples.csv").open("wb") as stream:
        monitor = subprocess.Popen(["nvidia-smi", "--query-gpu=timestamp,utilization.gpu,utilization.memory,memory.used,power.draw,clocks.sm,clocks.mem,temperature.gpu", "--format=csv", "--loop-ms=1000"], stdout=stream, stderr=subprocess.STDOUT)
        started = time.monotonic()
        try:
            for segment in range(20):
                argv = list(base)
                if segmented:
                    argv += ["-maxh", str(segment_minutes / 60)]
                if segment:
                    argv += ["-cpi", "md.cpt", "-append"]
                start = time.monotonic()
                command_log = directory / f"command-{segment}.log"
                code = baseline.run_logged(["/usr/bin/time", "-v", "-o", str(directory / f"time-{segment}.txt"), *argv], command_log,
                                           cwd=directory, env=env, timeout=1600)
                elapsed = time.monotonic() - start
                process_wall += elapsed
                log = command_log.read_text(errors="replace")
                if code or "Fatal error" in log or "LINCS WARNING" in log:
                    raise ValueError(f"Native run failed: {directory.name}, segment {segment}, code {code}")
                end_step = checkpoint_step(binary, directory)
                if not start_step < end_step <= steps:
                    raise ValueError("Checkpoint does not prove monotonically completed useful work")
                restarts = [int(x) for x in re.findall(r"continuing from step\s+(\d+)", log, re.I)]
                if (segment and restarts != [start_step]) or (not segment and restarts):
                    raise ValueError("Native restart boundary differs from previous checkpoint")
                perf = re.findall(r"Performance:\s+([\d.eE+-]+)", log)
                native.append({"segment": segment, "start_step": start_step, "end_step": end_step,
                               "process_wall_seconds": elapsed, "native_ns_per_day": float(perf[-1]) if perf else None,
                               "argv": argv, "checkpoint_sha256": baseline.sha256(directory / "md.cpt")})
                # Preserve the native checkpoint for each restart boundary.
                shutil.copyfile(directory / "md.cpt", directory / f"segment-{segment}.cpt")
                start_step = end_step
                if end_step == steps:
                    break
                if not segmented:
                    raise ValueError("Continuous run stopped early")
            else:
                raise ValueError("Exceeded finite segment count")
        finally:
            total_wall = time.monotonic() - started
            monitor.terminate()
            monitor.wait(timeout=10)
    after = baseline.cpu_stat()
    validation = baseline.validate(binary, directory, steps, steps * 0.002,
                                   baseline.expected_trajectories((root / "original.mdp").read_text()))
    # Independent finite coordinate/particle check complements energy/checkpoint checks.
    with (directory / "md.gro").open() as source:
        source.readline()
        atoms = int(source.readline())
        coords_ok = atoms == 185486
        for _ in range(atoms):
            line = source.readline()
            coords_ok = coords_ok and all(math.isfinite(float(line[a:b])) for a, b in ((20,28), (28,36), (36,44)))
    validation["finite_coordinates_and_particle_count"] = coords_ok
    validation["passed"] = validation["passed"] and coords_ok
    record = {"case": case["name"], "repeat": repeat, "steps": steps, "simulated_ns": steps * 0.002 / 1000,
              "threads": case["threads"], "binary": binary, "selected_cpus": case.get("selected"),
              "segmented": segmented, "segments": native, "validation": validation,
              "native_ns_per_day": native[-1]["native_ns_per_day"] if len(native) == 1 else None,
              "process_wall_seconds": process_wall, "component_wall_seconds": total_wall,
              "process_inclusive_ns_per_day": steps * 0.002 / 1000 * 86400 / process_wall,
              "cpu_stat_delta": {key: after[key] - value for key, value in before.items() if key in after},
              "input_sha256": baseline.sha256(tpr)}
    save(directory / "receipt.json", record)
    print(json.dumps(record), flush=True)
    if not validation["passed"]:
        raise ValueError("Scientific artifact validation failed")
    return record


def aggregate(records):
    result = {}
    for name in sorted({r["case"] for r in records}):
        selected = [r for r in records if r["case"] == name]
        values = [r["process_inclusive_ns_per_day"] for r in selected]
        native = [r["native_ns_per_day"] for r in selected if r["native_ns_per_day"] is not None]
        result[name] = {"repetitions": len(values), "process_inclusive_median_ns_day": statistics.median(values),
                        "process_inclusive_range_ns_day": [min(values), max(values)],
                        "native_median_ns_day": statistics.median(native) if native else None,
                        "all_valid": all(r["validation"]["passed"] for r in selected)}
    return result


def cpu_experiment(root, original, layout):
    short = finite_input(root, original, 50000)
    measured_run(root, short, {"name": "warmup", "threads": 8}, 0, 50000)
    cases = [{"name": "threads-8", "threads": 8}, {"name": "threads-7", "threads": 7},
             {"name": "threads-6", "threads": 6},
             {"name": "affinity-8", "threads": 8, "selected": layout["selected_cpus"]},
             {"name": "affinity-7", "threads": 7, "selected": layout["selected_cpus"]}]
    alternatives = [p for p in Path("/usr/local/gromacs").glob("*/bin/gmx") if "512" in str(p)]
    for binary in alternatives[:1]:
        cases.append({"name": "avx512-8", "threads": 8, "binary": str(binary)})
    save(root / "cases.json", cases)
    records = []
    for repeat in range(1, 4):
        order = cases if repeat % 2 else list(reversed(cases))
        for case in order:
            records.append(measured_run(root, short, case, repeat, 50000))
            save(root / "screen-progress.json", aggregate(records))
    summary = aggregate(records)
    candidate = max(cases[1:], key=lambda c: summary[c["name"]]["process_inclusive_median_ns_day"])
    # Confirm both arms on this same host even if all short candidates lose.
    long = finite_input(root, original, 500000)
    confirmed = []
    for repeat in range(1, 4):
        for case in ([cases[0], candidate] if repeat % 2 else [candidate, cases[0]]):
            full = {**case, "name": "confirm-" + case["name"]}
            confirmed.append(measured_run(root, long, full, repeat, 500000))
            save(root / "confirmation-progress.json", aggregate(confirmed))
    return {"screen": summary, "confirmation": aggregate(confirmed), "candidate": candidate,
            "avx512_binary_available": bool(alternatives), "records": records + confirmed}


def checkpoint_experiment(root, original):
    measured_run(root, finite_input(root, original, 50000), {"name": "warmup", "threads": 8}, 0, 50000)
    tpr = finite_input(root, original, 500000)
    records = []
    for repeat in range(1, 4):
        order = (False, True) if repeat % 2 else (True, False)
        for segmented in order:
            name = "five-minute-segments" if segmented else "continuous"
            records.append(measured_run(root, tpr, {"name": name, "threads": 8}, repeat, 500000, segmented=segmented))
            save(root / "checkpoint-progress.json", aggregate(records))
    return {"comparison": aggregate(records), "records": records,
            "scope": "Native process/restart cost only; checkpoint writes enabled in both arms. No claim of consistent live S3 export."}


def profile_experiment(root, original):
    tools = {tool: shutil.which(tool) for tool in ("nsys", "ncu", "nvidia-smi", "nvcc", "perf", "numactl", "lstopo")}
    for path in Path("/opt/nvidia").glob("nsight-systems/*/bin/nsys"):
        tools["nsys"] = tools["nsys"] or str(path)
    overlay = Path(__file__).with_name("nsight") / "target-linux-x64/nsys"
    if overlay.is_file():
        tools["nsys"] = tools["nsys"] or str(overlay)
    save(root / "profiling-tools.json", tools)
    if not tools["nsys"]:
        return {"status": "tool-unavailable", "tools": tools, "reason": "Nsight Systems absent in pinned production image; no host changes"}
    baseline.run_logged([tools["nsys"], "--version"], root / "nsys-version.txt")
    baseline.run_logged([tools["nsys"], "profile", "--help"], root / "nsys-help.txt")
    tpr = finite_input(root, original, 50000)
    measured_run(root, tpr, {"name": "warmup", "threads": 8}, 0, 50000)
    work = root / "profile"
    work.mkdir()
    env, _ = environment(root, 8)
    argv = [tools["nsys"], "profile", "--trace=cuda,nvtx,osrt", "--sample=none", "--cpuctxsw=none",
            "--delay=10", "--duration=10", "--kill=none", "--force-overwrite=true", "--export=sqlite",
            "-o", str(work / "timeline"), BINARY, "mdrun", "-s", str(tpr), "-deffnm", "md", "-ntmpi", "1",
            "-ntomp", "8", "-pin", "auto", "-nb", "gpu", "-pme", "auto", "-bonded", "gpu", "-update", "auto", "-nstlist", "200"]
    code = baseline.run_logged(argv, work / "profile.log", cwd=work, env=env, timeout=300)
    stats = None
    if (work / "timeline.nsys-rep").exists():
        stats = baseline.run_logged([tools["nsys"], "stats", "--report", "cuda_gpu_kern_sum,cuda_api_sum,cuda_gpu_mem_time_sum", str(work / "timeline.nsys-rep")], work / "stats.txt", timeout=180)
    return {"status": "captured" if code == 0 and stats == 0 else "capture-failed", "tools": tools,
            "capture_exit": code, "stats_exit": stats, "argv": argv, "timings_are_instrumented": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("cpu", "checkpoint", "profile"), required=True)
    parser.add_argument("--tpr", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if baseline.sha256(args.tpr) != TPR_SHA256:
        raise ValueError("Original input identity differs")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "cuda-cache").mkdir()
    layout = topology_tools.topology()
    save(root / "topology.json", layout)
    baseline.run_logged([BINARY, "--version"], root / "version.txt")
    baseline.run_logged(["lscpu"], root / "cpu.txt")
    baseline.run_logged(["nvidia-smi", "-q"], root / "gpu.txt")
    baseline.run_logged(["nvidia-smi", "topo", "-m"], root / "gpu-topology.txt")
    baseline.run_logged([sys.executable, str(Path(__file__).with_name("collect_env.py")), "--output", str(root / "environment.json")], root / "collector.log")
    baseline.run_logged([BINARY, "dump", "-s", str(args.tpr), "-om", str(root / "original.mdp")], root / "original-dump.log")
    try:
        if args.mode == "cpu":
            result = cpu_experiment(root, args.tpr, layout)
        elif args.mode == "checkpoint":
            result = checkpoint_experiment(root, args.tpr)
        else:
            result = profile_experiment(root, args.tpr)
        result.update(mode=args.mode, original_tpr_sha256=TPR_SHA256)
        save(root / "summary.json", result)
        inventory = [{"path": str(p.relative_to(root)), "bytes": p.stat().st_size, "sha256": baseline.sha256(p)}
                     for p in sorted(root.rglob("*")) if p.is_file() and "cuda-cache" not in p.parts]
        save(root / "inventory.json", inventory)
    except Exception as exc:
        save(root / "summary.json", {"status": "failed", "error": str(exc), "mode": args.mode})
        raise


if __name__ == "__main__":
    main()
