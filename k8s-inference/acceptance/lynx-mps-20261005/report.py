"""Summarize complete matched MPS cohorts without confusing throughput and latency.

Read-only over retained experiment files. Native process time excludes later
analysis and object export; this does not qualify hosted shared-GPU admission.
"""

import argparse
import hashlib
import json
from pathlib import Path
import statistics


def spread(values):
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def summarize(records, *, repetitions=3):
    expected = {(enabled, clients, repetition) for enabled in (False, True)
                for clients in (1, 2, 4) for repetition in range(1, repetitions + 1)}
    keys = [(row["mps"], row["clients"], row["repetition"]) for row in records]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError("Require every expected off/on cohort exactly once")
    if len({row["input_sha256"] for row in records}) != 1:
        raise ValueError("Compared cohorts did not use an identical input")
    for row in records:
        simulations = row["simulations"]
        if (not row["passed"] or row["cpu_threads_total"] != 8
                or row["threads_per_process"] * row["clients"] != 8
                or len(simulations) != row["clients"]
                or not all(item["exit_code"] == 0 and item["validation"]["passed"] for item in simulations)):
            raise ValueError("Require valid science outputs and the fixed CPU budget")
        if row["mps"] and not set(row["expected_client_pids"]) <= set(row["observed_mps_client_pids"]):
            raise ValueError("MPS must have observed every real GROMACS client")
    groups = []
    baseline = statistics.median(row["aggregate_process_ns_per_day"] for row in records
                                 if not row["mps"] and row["clients"] == 1)
    for enabled in (False, True):
        for clients in (1, 2, 4):
            rows = [row for row in records if row["mps"] == enabled and row["clients"] == clients]
            rates = [row["aggregate_process_ns_per_day"] for row in rows]
            cpu = [row["cgroup_cpu_delta"].get("usage_usec") for row in rows]
            groups.append({
                "mps": enabled, "clients": clients, "repetitions": repetitions,
                "aggregate_process_ns_per_day": spread(rates),
                "per_simulation_wall_seconds": spread([item["wall_seconds"] for row in rows
                                                       for item in row["simulations"]]),
                "per_simulation_native_ns_per_day": spread([item["native_ns_per_day"] for row in rows
                                                            for item in row["simulations"]]),
                "throughput_ratio_to_one_process_no_mps": statistics.median(rates) / baseline,
                "mean_cpu_cores_per_cohort": spread([
                    value / 1e6 / row["cohort_wall_seconds"] for value, row in zip(cpu, rows, strict=True)
                ]) if all(value is not None for value in cpu) else None,
            })
    return {"input_sha256": records[0]["input_sha256"], "groups": groups,
            "native_trajectories_checked": sum(row["clients"] for row in records),
            "scope": "Native process wall includes startup/output, excludes analysis and remote export. "
                     "Identical replicas are a throughput proxy, not independent ensemble samples.",
            "hosted_gpu_sharing_qualified": False}


def read_run(directory):
    receipt = json.loads((directory / "receipt.json").read_text())
    if not receipt["passed"] or not receipt.get("cleanup"):
        raise ValueError("Require a passed launcher with observed owned-Pod cleanup")
    raw = (directory / "results/measurements.json").read_bytes()
    report = summarize(json.loads(raw))
    environment = json.loads((directory / "results/environment.json").read_text())
    report.update(node=receipt["node"], pod_uid=receipt["pod_uid"], image=receipt["image"],
                  image_id=receipt["image_id"], source=str(directory),
                  measurements_sha256=hashlib.sha256(raw).hexdigest(),
                  environment=environment, cleanup=receipt["cleanup"])
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps({"runs": [read_run(directory) for directory in args.runs]}, indent=2, allow_nan=False))
