"""Verify retained benchmark bytes and summarize every cold/warm outcome."""

import argparse
import csv
import json
from pathlib import Path
import re
import statistics

from benchmark_sm89 import sha256


def mean(values):
    return statistics.mean(values) if values else None


def summarize(directory):
    records = json.loads((directory / "measurements.json").read_text())
    runs = []
    for original in records:
        path = directory / original["cohort"]
        for item in original["output_inventory"]:
            source = path / item["name"]
            assert source.stat().st_size == item["bytes"] and sha256(source) == item["sha256"], source
        row = {key: value for key, value in original.items() if key != "output_inventory"}
        row["verified_files"] = len(original["output_inventory"])
        timing = (path / "time.txt").read_text()
        for name, pattern in {
            "cpu_percent": r"Percent of CPU this job got:\s+([\d.]+)%",
            "max_resident_kib": r"Maximum resident set size \(kbytes\):\s+(\d+)",
        }.items():
            match = re.search(pattern, timing)
            row[name] = float(match[1]) if match else None
        gpu, power = [], []
        for sample in csv.DictReader((path / "gpu-samples.csv").open()):
            for key, value in sample.items():
                target = gpu if key.strip().startswith("utilization.gpu") else power if key.strip().startswith("power.draw") else None
                if target is not None and (match := re.search(r"[\d.]+", value or "")):
                    target.append(float(match[0]))
        row["process_gpu_utilization_percent_mean"] = mean(gpu)
        row["process_gpu_power_watts_mean"] = mean(power)
        row["gpu_samples"] = len(gpu)
        log = (path / "md.log").read_text() if (path / "md.log").exists() else ""
        match = re.search(r"Time:\s+[\d.]+\s+([\d.]+)", log)
        row["native_timed_wall_seconds"] = float(match[1]) if match else None
        row["outside_native_timed_wall_seconds"] = (
            row["process_wall_seconds"] - row["native_timed_wall_seconds"]
            if row["native_timed_wall_seconds"] is not None else None
        )
        row["timing_bucket_percent"] = {}
        for bucket in ("Neighbor search", "Force", "Wait GPU state copy", "Launch GPU ops"):
            match = re.search(r"^" + re.escape(bucket) + r"\s+.+?([\d.]+)\s*$", log, re.MULTILINE)
            if match:
                row["timing_bucket_percent"][bucket] = float(match[1])
        runs.append(row)
    warmed = [row for row in runs if row["cohort"].startswith("warm-")]
    passed = [row for row in warmed if row["exit_code"] == 0 and row.get("validation", {}).get("passed")]
    values = [row["native_ns_per_day"] for row in passed]
    return {
        "name": directory.name, "runs": runs, "warm_attempts": len(warmed),
        "validated_warm_successes": len(passed), "total_attempts": len(runs),
        "total_failures": sum(row["exit_code"] != 0 or not row.get("validation", {}).get("passed") for row in runs),
        "warm_ns_per_day": values, "warm_ns_per_day_median": statistics.median(values) if values else None,
        "warm_ns_per_day_min": min(values) if values else None,
        "warm_ns_per_day_max": max(values) if values else None,
        "warm_ns_per_day_sample_stddev": statistics.stdev(values) if len(values) > 1 else None,
        "warm_process_wall_seconds_median": statistics.median([row["process_wall_seconds"] for row in passed]) if passed else None,
        "all_retained_files_verified": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = [summarize(path.parent) for path in sorted(args.root.glob("*/measurements.json"))]
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps([{key: value for key, value in row.items() if key != "runs"} for row in results], indent=2))


if __name__ == "__main__":
    main()
