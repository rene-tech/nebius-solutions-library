"""Summarize complete matched MPS cohorts without confusing throughput and latency.

Read-only over retained experiment files. Native process time excludes later
analysis and object export; this does not qualify hosted shared-GPU admission.
"""

import argparse
import csv
import hashlib
import json
import math
import statistics
from pathlib import Path


def spread(values):
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def gpu_samples(path):
    """NVIDIA fixed 500 ms samples; missing counters remain unknown, not zero."""
    names = ("gpu_utilization_percent", "memory_utilization_percent", "memory_used_mib",
             "power_watts", "sm_clock_mhz")
    values = {name: [] for name in names}
    malformed, rows = 0, 0
    with path.open() as stream:
        reader = csv.reader(stream, skipinitialspace=True)
        if next(reader, [""])[0] != "timestamp":
            raise ValueError("Unexpected nvidia-smi sample header")
        for row in reader:
            if len(row) != 7:
                malformed += 1
                continue
            rows += 1
            for name, text in zip(names, row[2:], strict=True):
                try:
                    value = float(text.split()[0])
                except (ValueError, IndexError):
                    continue
                if math.isfinite(value) and value >= 0:
                    values[name].append(value)
    return {"rows": rows, "malformed_rows": malformed,
            "scope": "Unweighted native-process sample means, not whole allocation or energy integrals.",
            "metrics": {name: {"samples": len(items), "mean": statistics.mean(items) if items else None,
                               "maximum": max(items) if items else None} for name, items in values.items()}}


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


def verify_files(results, records):
    checked = 0
    for row in records:
        cohort = results / f"repeat-{row['repetition']}-mps-{'on' if row['mps'] else 'off'}-clients-{row['clients']}"
        saved = json.loads((cohort / "receipt.json").read_text())
        if saved != row:
            raise ValueError("Cohort receipt differs from aggregate measurements")
        for simulation in row["simulations"]:
            member = cohort / f"simulation-{simulation['index']}"
            for item in simulation["files"]:
                if Path(item["name"]).name != item["name"]:
                    raise ValueError("Native output name escapes the cohort")
                path = member / item["name"]
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if path.stat().st_size != item["bytes"] or digest != item["sha256"]:
                    raise ValueError("Exported native output failed SHA-256 verification")
                checked += 1
    return checked


def read_run(directory, *, recover_export=False):
    receipt = json.loads((directory / "receipt.json").read_text())
    if receipt.get("exit_code") != 0 or not receipt.get("cleanup"):
        raise ValueError("Require successful native execution and observed owned-Pod cleanup")
    results = directory / receipt.get("results_directory", "results")
    recovered = False
    if not receipt["passed"]:
        if not recover_export or not receipt.get("error"):
            raise ValueError("Failed export requires explicit recovery; do not hide the launcher failure")
        workspace = directory / "failed-workspace"
        for item in receipt["uploaded_sources"]:
            with (workspace / item["name"]).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                    raise ValueError("Recovered input/harness differs from the launched experiment")
        results = workspace / "results"
        recovered = True
    raw = (results / "measurements.json").read_bytes()
    records = json.loads(raw)
    report = summarize(records)
    if report["input_sha256"] != receipt["finite_tpr_sha256"]:
        raise ValueError("Measured input differs from the launched finite fixture")
    checked = verify_files(results, records)
    environment = json.loads((results / "environment.json").read_text())
    if not json.loads((results / "summary.json").read_text())["passed"]:
        raise ValueError("Native summary was not successful")
    report.update(node=receipt["node"], pod_uid=receipt["pod_uid"], image=receipt["image"],
                  image_id=receipt["image_id"], source=str(directory),
                  measurements_sha256=hashlib.sha256(raw).hexdigest(),
                  environment=environment, cleanup=receipt["cleanup"],
                  original_launcher_passed=receipt["passed"], observer_export_recovered=recovered,
                  original_observer_error=receipt.get("error"), verified_native_files=checked)
    report["cohort_gpu_samples"] = [{"cohort": path.parent.name, **gpu_samples(path)}
                                    for path in sorted(results.glob("repeat-*/gpu-samples.csv"))]
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--recover-export", action="store_true",
                        help="Verify retained fallback copy; retain original failed observer outcome")
    parser.add_argument("--output", type=Path, help="Write a separate report, never replace original receipts")
    args = parser.parse_args()
    output = json.dumps({"runs": [read_run(directory, recover_export=args.recover_export) for directory in args.runs]},
                        indent=2, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x") as stream:
            stream.write(output)
        print(json.dumps({"report": str(args.output)}))
    else:
        print(output)
