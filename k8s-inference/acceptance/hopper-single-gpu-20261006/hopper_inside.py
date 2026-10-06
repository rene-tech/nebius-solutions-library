"""Fixed-science one-GPU screening and paired long confirmation on Hopper."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import experiment_inside as common

BINARY = common.BINARY


def layout(expected_gpu):
    allowed = sorted(os.sched_getaffinity(0))
    cores = {}
    for cpu in allowed:
        base = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        cores[cpu] = [(base / field).read_text().strip() for field in ("physical_package_id", "core_id")]
    query = subprocess.check_output([
        "nvidia-smi", "--query-gpu=uuid,pci.bus_id,name,compute_cap", "--format=csv,noheader"], text=True)
    rows = query.strip().splitlines()
    if len(rows) != 1 or expected_gpu not in rows[0] or rows[0].split(",")[-1].strip() != "9.0":
        raise ValueError("Require exactly one visible requested Hopper GPU (SM90)")
    bus = rows[0].split(",")[1].strip().lower()[-12:]
    numa_file = Path("/sys/bus/pci/devices") / bus / "numa_node"
    numa = int(numa_file.read_text()) if numa_file.exists() else -1
    nearby_file = Path(f"/sys/devices/system/node/node{numa}/cpulist")
    nearby = common.topology_tools.cpu_list(nearby_file.read_text()) if nearby_file.exists() else set(allowed)
    return {"allowed_cpus": allowed, "physical_cores": cores, "gpu": query,
            "gpu_numa_node": numa, "numa_local_cpus": sorted(nearby),
            "selected_cpus": common.topology_tools.select_physical_cpus(allowed, cores, nearby),
            "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
            "affinity_scope": "Only this process and descendants; no host or NUMA memory policy changes"}


def cases_for(mode, cpu_budget, topology, avx512_available):
    base = {"name": "baseline", "threads": 8}
    if mode == "cpu-pme":
        if cpu_budget < 16:
            raise ValueError("Combined CPU/PME experiment requires at least 16 allocated CPUs")
        return [base, {"name": "threads-16", "threads": 16},
                {"name": "original-pme", "threads": 8, "extra_args": ["-notunepme"]}]
    if mode == "cpu-envelope":
        return [base] + [{"name": f"threads-{n}", "threads": n}
                         for n in (16, 32) if n <= cpu_budget]
    cases = [base, {"name": "threads-4", "threads": 4}, {"name": "threads-6", "threads": 6},
             {"name": "bonded-cpu", "threads": 8, "bonded": "cpu"},
             {"name": "original-pme", "threads": 8, "extra_args": ["-notunepme"]},
             {"name": "list-100", "threads": 8, "nstlist": 100},
             {"name": "list-300", "threads": 8, "nstlist": 300},
             {"name": "affinity-8", "threads": 8, "selected": topology["selected_cpus"]}]
    if avx512_available:
        cases.append({"name": "avx512-8", "threads": 8,
                      "binary": "/usr/local/gromacs/avx_512/bin/gmx"})
    return cases


def combined_candidate(cases, summary):
    """At most one combination; every selected one-factor screen must gain 2%."""
    base_rate = summary["baseline"]["process_inclusive_median_ns_day"]
    result = {"name": "combined", "threads": 8}
    groups = (("threads",), ("bonded",), ("extra_args",), ("nstlist",), ("selected",), ("binary",))
    used = []
    for fields in groups:
        eligible = [case for case in cases[1:] if any(field in case and case[field] != cases[0].get(field)
                    for field in fields)]
        # Every case contains its thread count; only actual thread variants belong here.
        if fields == ("threads",):
            eligible = [case for case in eligible if case["threads"] != 8]
        if not eligible:
            continue
        chosen = max(eligible, key=lambda case: summary[case["name"]]["process_inclusive_median_ns_day"])
        if summary[chosen["name"]]["process_inclusive_median_ns_day"] > base_rate * 1.02:
            result.update({field: chosen[field] for field in fields if field in chosen})
            used.append(chosen["name"])
    return (result, used) if len(used) > 1 else (None, used)


def tune(root, original, cases):
    short = common.finite_input(root, original, 50000)
    common.measured_run(root, short, {"name": "warmup", "threads": 8}, 0, 50000)
    common.save(root / "cases.json", cases)
    screened = []
    for repeat in range(1, 4):
        for case in (cases if repeat % 2 else list(reversed(cases))):
            screened.append(common.measured_run(root, short, case, repeat, 50000))
            common.save(root / "screen-progress.json", common.aggregate(screened))
    summary = common.aggregate(screened)
    combination, contributors = combined_candidate(cases, summary)
    if combination:
        for repeat in range(1, 4):
            screened.append(common.measured_run(root, short, combination, repeat, 50000))
        cases.append(combination)
        summary = common.aggregate(screened)
        common.save(root / "screen-progress.json", summary)
    candidate = max(cases[1:], key=lambda case: summary[case["name"]]["process_inclusive_median_ns_day"])
    common.save(root / "selected.json", {"candidate": candidate, "combination": combination,
                                         "combination_contributors": contributors})
    long_tpr = common.finite_input(root, original, 500000)
    confirmed = []
    for repeat in range(1, 4):
        for case in ([cases[0], candidate] if repeat % 2 else [candidate, cases[0]]):
            confirmed.append(common.measured_run(root, long_tpr, {**case, "name": "confirm-" + case["name"]},
                                                 repeat, 500000))
            common.save(root / "confirmation-progress.json", common.aggregate(confirmed))
    return {"screen": summary, "confirmation": common.aggregate(confirmed), "candidate": candidate,
            "combination": combination, "combination_contributors": contributors,
            "records": screened + confirmed, "status": "passed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("tune", "cpu-envelope", "cpu-pme", "profile"), required=True)
    parser.add_argument("--gpu", choices=("H100", "H200"), required=True)
    parser.add_argument("--cpu-budget", type=int, choices=(8, 16, 32), required=True)
    parser.add_argument("--tpr", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if common.baseline.sha256(args.tpr) != common.TPR_SHA256:
        raise ValueError("Not the original approved input")
    if args.mode == "cpu-envelope" and args.cpu_budget == 8:
        raise ValueError("CPU-envelope comparison needs more than eight allocated CPUs")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "cuda-cache").mkdir()
    topology = layout(args.gpu)
    quota, period = topology["cpu_max"].split()
    if quota == "max" or int(quota) / int(period) != args.cpu_budget:
        raise ValueError("Observed CPU quota differs from declared allocation")
    common.save(root / "topology.json", topology)
    for argv, output in (([BINARY, "--version"], "version.txt"), (["lscpu"], "cpu.txt"),
                         (["nvidia-smi", "-q"], "gpu.txt"), (["nvidia-smi", "topo", "-m"], "gpu-topology.txt"),
                         ([BINARY, "mdrun", "-h"], "mdrun-help.txt"), (["ldd", BINARY], "linkage.txt")):
        common.baseline.run_logged(argv, root / output)
    common.baseline.run_logged([sys.executable, str(Path(__file__).with_name("collect_env.py")),
                                "--output", str(root / "environment.json")], root / "collector.log")
    common.baseline.run_logged([BINARY, "dump", "-s", str(args.tpr), "-om", str(root / "original.mdp")], root / "original-dump.log")
    alternative = Path("/usr/local/gromacs/avx_512/bin/gmx")
    avx512 = alternative.is_file() and "avx512f" in Path("/proc/cpuinfo").read_text()
    if avx512:
        common.baseline.run_logged([str(alternative), "--version"], root / "avx512-version.txt")
    try:
        result = (common.profile_experiment(root, args.tpr, steps=250000) if args.mode == "profile" else
                  tune(root, args.tpr, cases_for(args.mode, args.cpu_budget, topology, avx512)))
        result.update(mode=args.mode, gpu=args.gpu, cpu_budget=args.cpu_budget, gpus=1,
                      original_tpr_sha256=common.TPR_SHA256,
                      scope="One-GPU native benchmark; no public or customer-default promotion")
        common.save(root / "summary.json", result)
        common.save(root / "inventory.json", [{"path": str(path.relative_to(root)), "bytes": path.stat().st_size,
                    "sha256": common.baseline.sha256(path)} for path in sorted(root.rglob("*"))
                    if path.is_file() and "cuda-cache" not in path.parts])
    except Exception as error:
        common.save(root / "summary.json", {"status": "failed", "error": str(error), "mode": args.mode})
        raise


if __name__ == "__main__":
    main()
