"""One fixed-science L40S screen inside the task-owned, unchanged eight-CPU Pod."""

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

import benchmark_sm89 as baseline

TPR_SHA256 = "e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10"
BINARY = "/usr/local/gromacs/avx2_256/bin/gmx"


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def parameter_difference(original, finite):
    changed = {key: [original.get(key), finite.get(key)] for key in original.keys() | finite.keys()
               if original.get(key) != finite.get(key)}
    if set(changed) != {"nsteps"} or float(original["verlet-buffer-tolerance"]) <= 0:
        raise ValueError("Finite benchmark changed science beyond nsteps")
    return changed


def select_physical_cpus(allowed, topology, nearby, count=8):
    """Select only the process's allowed CPUs; never set node/cgroup policy."""
    chosen, seen = [], set()
    for cpu in sorted(set(allowed), key=lambda value: (value not in nearby, value)):
        core = tuple(topology[cpu])
        if core in seen:
            continue
        chosen.append(cpu)
        seen.add(core)
        if len(chosen) == count:
            return chosen
    raise ValueError("Fewer than eight distinct physical cores in the allowed mask")


def cpu_list(value):
    result = set()
    for span in value.strip().split(","):
        if not span:
            continue
        ends = span.split("-")
        result.update(range(int(ends[0]), int(ends[-1]) + 1))
    return result


def topology():
    allowed = sorted(os.sched_getaffinity(0))
    cores = {}
    for cpu in allowed:
        base = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        cores[cpu] = [(base / field).read_text().strip() for field in ("physical_package_id", "core_id")]
    gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=uuid,pci.bus_id,name", "--format=csv,noheader"], text=True)
    rows = gpu.strip().splitlines()
    if len(rows) != 1 or "L40S" not in rows[0]:
        raise ValueError("Require exactly one visible L40S")
    bus = rows[0].split(",")[1].strip().lower()
    bus = bus[-12:]
    numa_file = Path("/sys/bus/pci/devices") / bus / "numa_node"
    numa = int(numa_file.read_text()) if numa_file.exists() else -1
    nearby_file = Path(f"/sys/devices/system/node/node{numa}/cpulist")
    nearby = cpu_list(nearby_file.read_text()) if numa >= 0 and nearby_file.exists() else set(allowed)
    chosen = select_physical_cpus(allowed, cores, nearby)
    return {"allowed_cpus": allowed, "physical_cores": cores, "gpu": gpu,
            "gpu_numa_node": numa, "numa_local_cpus": sorted(nearby), "selected_cpus": chosen,
            "affinity_scope": "Task process and descendants only; NUMA memory policy unchanged",
            "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip()}


def active_gmx_affinity():
    records = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            argv = (process / "cmdline").read_bytes().split(b"\0")
            if b"mdrun" not in argv or not argv[0].endswith(b"/gmx"):
                continue
            threads = []
            for thread in (process / "task").iterdir():
                status = {line.partition(":")[0]: line.partition(":")[2].strip()
                          for line in (thread / "status").read_text().splitlines()}
                threads.append({key: status.get(key) for key in
                                ("Name", "Pid", "Tgid", "Uid", "Cpus_allowed_list", "Mems_allowed_list")})
            records.append({"pid": int(process.name), "threads": threads})
        except (FileNotFoundError, ProcessLookupError):
            continue
    return records


def run_case(root, tpr, name, nstlist, *, selected=None, graphs=False, warm=3):
    destination = root / name
    env = {**os.environ}
    for key in ("GMX_CUDA_GRAPH", "OMP_PLACES", "OMP_PROC_BIND", "GOMP_CPU_AFFINITY"):
        env.pop(key, None)
    if graphs:
        env["GMX_CUDA_GRAPH"] = "1"
    prefix = []
    if selected:
        env.update(OMP_PROC_BIND="close", OMP_PLACES=",".join("{" + str(c) + "}" for c in selected))
        prefix = ["taskset", "--cpu-list", ",".join(map(str, selected))]
    command = [*prefix, sys.executable, str(Path(baseline.__file__).resolve()),
               "--tpr", str(tpr), "--sha256", baseline.sha256(tpr), "--output", str(destination),
               "--expected-steps", "50000", "--expected-time-ps", "100", "--threads", "8",
               "--bonded", "gpu", "--pin", "auto", "--nstlist", str(nstlist),
               "--warm-repetitions", str(warm)]
    affinity = {}
    started = time.monotonic()
    with (root / (name + ".log")).open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        try:
            while process.poll() is None:
                if time.monotonic() - started > 700:
                    raise TimeoutError("Bounded four-run screen expired")
                for row in active_gmx_affinity():
                    # Capture after OpenMP initialization, not only the initial main thread.
                    if len(row["threads"]) >= 8:
                        # Keep the latest running snapshot, not a transient
                        # pre-pinning OpenMP initialization snapshot.
                        row["observed_elapsed_seconds"] = time.monotonic() - started
                        affinity[row["pid"]] = row
                time.sleep(1)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            raise
    save(root / (name + "-affinity.json"), list(affinity.values()))
    measurements = json.loads((destination / "measurements.json").read_text()) if (destination / "measurements.json").exists() else []
    good = [row["native_ns_per_day"] for row in measurements
            if row["cohort"].startswith("warm-") and row.get("validation", {}).get("passed")]
    logs = "\n".join(path.read_text(errors="replace") for path in destination.glob("*/command.log"))
    record = {"case": name, "nstlist": nstlist, "command": command,
              "environment": {key: env[key] for key in ("GMX_CUDA_GRAPH", "OMP_PLACES", "OMP_PROC_BIND") if key in env},
              "exit_code": process.returncode, "elapsed_seconds": time.monotonic() - started,
              "validated_warm_runs": len(good), "warm_median_ns_per_day": statistics.median(good) if good else None,
              "warm_range_ns_per_day": [min(good), max(good)] if good else None,
              "all_native_valid": bool(measurements) and all(row.get("validation", {}).get("passed") for row in measurements),
              "affinity_observed_native_processes": len(affinity),
              "graph_diagnostics": [line for line in logs.splitlines() if "graph" in line.lower() or "CPU force" in line]}
    save(root / (name + "-receipt.json"), record)
    print(json.dumps(record), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tpr", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if baseline.sha256(args.tpr) != TPR_SHA256:
        raise ValueError("Not the unchanged approved Lynx TPR")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    layout = topology()
    save(root / "topology.json", layout)
    baseline.run_logged(["nvidia-smi", "topo", "-m"], root / "gpu-topology.txt")
    baseline.run_logged([BINARY, "mdrun", "-h"], root / "mdrun-help.txt")
    tpr = root / "benchmark.tpr"
    code = baseline.run_logged([BINARY, "convert-tpr", "-s", str(args.tpr), "-o", str(tpr), "-nsteps", "50000"], root / "convert-tpr.log")
    if code:
        raise ValueError("Finite TPR creation failed")
    # Full native comparison is retained for topology/state review as well as
    # the exact input-parameter assertion below. No TPR is regenerated by grompp.
    baseline.run_logged([BINARY, "check", "-s1", str(args.tpr), "-s2", str(tpr)], root / "full-tpr-comparison.log")
    for name, source in (("original", args.tpr), ("finite", tpr)):
        code = baseline.run_logged([BINARY, "dump", "-s", str(source), "-om", str(root / (name + ".mdp"))], root / (name + "-dump.log"))
        if code:
            raise ValueError("Native input-parameter dump failed")
    original = baseline.mdp_values((root / "original.mdp").read_text())
    finite = baseline.mdp_values((root / "finite.mdp").read_text())
    changed = parameter_difference(original, finite)
    save(root / "finite-parameter-diff.json", changed)
    records = [run_case(root, tpr, f"list-{value}", value) for value in (200, 100, 300)]
    if any(row["validated_warm_runs"] != 3 or not row["all_native_valid"] for row in records):
        raise ValueError("A primary list screen failed; retain results without promoting a winner")
    best = max(records, key=lambda row: row["warm_median_ns_per_day"])
    records.append(run_case(root, tpr, "affinity-local", best["nstlist"], selected=layout["selected_cpus"]))
    # One eligibility run is not a graph performance claim. If unsupported, retain
    # its native failure without changing physics or repeatedly retrying it.
    records.append(run_case(root, tpr, "graph-eligibility", best["nstlist"], graphs=True, warm=0))
    save(root / "summary.json", {"input_tpr_sha256": TPR_SHA256, "records": records,
         "best_public_list_interval": best["nstlist"], "public_recipe_environment_changes": False,
         "scope": "Native screen only; affinity/graph paths are not published API features or delivered-throughput proof"})


if __name__ == "__main__":
    main()
