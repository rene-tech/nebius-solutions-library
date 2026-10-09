"""Matched native MPS experiment inside one exclusively allocated GPU Pod.

Independent processes repeat identical input for a throughput experiment, NOT
independent scientific ensemble sampling. No host-wide MPS/MIG configuration.
Each cohort owns exactly eight CPU threads and one GPU, including MPS overhead.
"""

import argparse
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import time
from pathlib import Path

from benchmark_sm89 import cpu_stat, expected_trajectories, run_logged, sha256, validate

BINARY = "/usr/local/gromacs/avx2_256/bin/gmx"


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def command(tpr, clients):
    if clients not in (1, 2, 4):
        raise ValueError("Only the bounded one-GPU 1/2/4-client comparison is supported")
    return [BINARY, "mdrun", "-s", str(tpr), "-deffnm", "md", "-ntmpi", "1", "-ntomp", str(8 // clients),
            "-pin", "off", "-nb", "gpu", "-pme", "gpu", "-bonded", "gpu", "-update", "auto", "-nstlist", "200"]


def mps_control(message, env):
    completed = subprocess.run(["nvidia-cuda-mps-control"], input=(message + "\n").encode(),
                               env=env, capture_output=True, timeout=10, check=True)
    return completed.stdout.decode(errors="replace").strip()


def mps_clients(env):
    result = {}
    for server in mps_control("get_server_list", env).split():
        if server.isdigit():
            result[server] = [int(pid) for pid in mps_control("get_client_list " + server, env).split()
                              if pid.isdigit()]
    return result


def run_cohort(args, directory, *, enabled, clients, repetition, trajectories):
    directory.mkdir()
    env = {key: value for key, value in os.environ.items()
           if key not in {"CUDA_MPS_PIPE_DIRECTORY", "CUDA_MPS_LOG_DIRECTORY", "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE"}}
    env.update(CUDA_CACHE_PATH=str(args.output / "cuda-cache"), OMP_NUM_THREADS=str(8 // clients))
    controller = False
    pipes, logs = directory / "mps-pipes", directory / "mps-logs"
    if enabled:
        pipes.mkdir(mode=0o700)
        logs.mkdir(mode=0o700)
        env.update(CUDA_MPS_PIPE_DIRECTORY=str(pipes), CUDA_MPS_LOG_DIRECTORY=str(logs))
        # Same UID and private pipe directory. Do not change compute mode,
        # persistence mode, device-plugin settings or host IPC/network.
        subprocess.run(["nvidia-cuda-mps-control", "-d"], env=env, check=True, timeout=15)
        controller = True
    argv = command(args.tpr, clients)
    processes, streams, observed = [], [], []
    before = cpu_stat()
    started = time.monotonic()
    finished = {}
    gpu_stream = (directory / "gpu-samples.csv").open("wb")
    monitor = subprocess.Popen([
        "nvidia-smi", "--query-gpu=timestamp,uuid,utilization.gpu,utilization.memory,memory.used,power.draw,clocks.sm",
        "--format=csv", "--loop-ms=500"], stdout=gpu_stream, stderr=subprocess.STDOUT)
    try:
        for index in range(clients):
            member = directory / f"simulation-{index + 1}"
            member.mkdir()
            stream = (member / "command.log").open("wb")
            streams.append(stream)
            processes.append(subprocess.Popen(argv, cwd=member, env=env, stdin=subprocess.DEVNULL,
                                              stdout=stream, stderr=subprocess.STDOUT, start_new_session=True))
        while len(finished) != clients:
            now = time.monotonic()
            for index, process in enumerate(processes):
                if index not in finished and process.poll() is not None:
                    finished[index] = {"exit_code": process.returncode, "wall_seconds": now - started}
            if enabled and len(finished) != clients:
                observed.append({"elapsed_seconds": now - started, "servers": mps_clients(env)})
            if now - started > 900:
                raise TimeoutError("Bounded isolated MPS cohort exceeded 900 seconds")
            if len(finished) != clients:
                time.sleep(0.5)
        elapsed = time.monotonic() - started
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=10)
        for stream in streams:
            stream.close()
        monitor.terminate()
        monitor.wait(timeout=10)
        gpu_stream.close()
        if controller:
            mps_control("quit", env)
    after = cpu_stat()
    expected_pids = {process.pid for process in processes}
    seen_pids = {pid for row in observed for pids in row["servers"].values() for pid in pids}
    record = {"mps": enabled, "clients": clients, "repetition": repetition,
              "cpu_threads_total": 8, "threads_per_process": 8 // clients,
              "cohort_wall_seconds": elapsed, "input_sha256": args.sha256,
              "expected_client_pids": sorted(expected_pids), "observed_mps_client_pids": sorted(seen_pids),
              "actual_mps_clients_verified": not enabled or expected_pids <= seen_pids,
              "cgroup_cpu_delta": {key: value - before[key] for key, value in after.items() if key in before},
              "simulations": [], "scope": "native CLI; no REST/MCP sharing or customer isolation claim"}
    save(directory / "mps-client-observations.json", observed)
    for index in range(clients):
        member = directory / f"simulation-{index + 1}"
        text = (member / "command.log").read_text(errors="replace")
        performance = re.findall(r"Performance:\s+([\d.eE+-]+)", text)
        item = {**finished[index], "argv": argv, "index": index + 1,
                "native_ns_per_day": float(performance[-1]) if performance else None}
        if item["exit_code"] == 0:
            item["validation"] = validate(BINARY, member, args.steps, args.steps * 0.002, trajectories)
        item["files"] = [{"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
                         for path in sorted(member.iterdir()) if path.is_file()]
        record["simulations"].append(item)
    record["passed"] = record["actual_mps_clients_verified"] and all(
        item["exit_code"] == 0 and item.get("validation", {}).get("passed")
        for item in record["simulations"])
    record["aggregate_process_ns_per_day"] = clients * args.steps * 0.000002 * 86400 / elapsed
    record["per_simulation_wall_seconds"] = [item["wall_seconds"] for item in record["simulations"]]
    save(directory / "receipt.json", record)
    return record


def main(args):
    args.output = args.output.resolve()
    args.tpr = args.tpr.resolve()
    if sha256(args.tpr) != args.sha256:
        raise ValueError("Exact finite TPR hash mismatch")
    if not shutil.which("nvidia-cuda-mps-control"):
        raise RuntimeError("Missing nvidia-cuda-mps-control in the exact worker image")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cuda-cache").mkdir()
    for name, argv in {
        "version.txt": [BINARY, "--version"],
        "gpu.txt": ["nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total,compute_mode", "--format=csv"],
        "topology.txt": ["nvidia-smi", "topo", "-m"],
        "input-dump.log": [BINARY, "dump", "-s", str(args.tpr), "-om", str(args.output / "input.mdp"), "-quiet"],
    }.items():
        if run_logged(argv, args.output / name, timeout=120):
            raise RuntimeError("Required benchmark environment inspection failed: " + name)
    trajectories = expected_trajectories((args.output / "input.mdp").read_text())
    save(args.output / "environment.json", {
        "harness_sha256": sha256(Path(__file__)), "validation_harness_sha256": sha256(Path(__file__).with_name("benchmark_sm89.py")),
        "cpu_affinity": sorted(os.sched_getaffinity(0)), "mps_control_binary": shutil.which("nvidia-cuda-mps-control"),
        "uid": os.getuid(), "steps": args.steps, "repetitions": args.repetitions,
        "cgroup": {str(path): path.read_text().strip() for path in (
            Path("/sys/fs/cgroup/cpu.max"), Path("/sys/fs/cgroup/cpuset.cpus.effective")) if path.exists()},
    })
    records = []
    # Alternate order between repetitions. Preserve first/fresh measurements;
    # per-cohort output exposes warm-up/thermal order instead of hiding it.
    order = [(False, 1), (True, 1), (True, 2), (False, 2), (False, 4), (True, 4)]
    for repetition in range(1, args.repetitions + 1):
        for enabled, clients in order if repetition % 2 else reversed(order):
            name = f"repeat-{repetition}-mps-{'on' if enabled else 'off'}-clients-{clients}"
            record = run_cohort(args, args.output / name, enabled=enabled, clients=clients,
                                repetition=repetition, trajectories=trajectories)
            records.append(record)
            save(args.output / "measurements.json", records)
            print(json.dumps({key: record[key] for key in (
                "mps", "clients", "repetition", "passed", "cohort_wall_seconds", "aggregate_process_ns_per_day",
                "actual_mps_clients_verified")}), flush=True)
            if not record["passed"]:
                raise RuntimeError("Matched MPS cohort failed; retain evidence instead of claiming throughput")
    summary = [{"mps": enabled, "clients": clients,
                "median_aggregate_process_ns_per_day": statistics.median(row["aggregate_process_ns_per_day"]
                    for row in records if row["mps"] == enabled and row["clients"] == clients)}
               for enabled, clients in order]
    save(args.output / "summary.json", {"passed": True, "groups": summary,
        "scope": "worker-only performance screen; unchanged identical inputs, not independent scientific ensembles"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tpr", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--steps", type=int, choices=(50000, 500000), default=50000)
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=3)
    main(parser.parse_args())
