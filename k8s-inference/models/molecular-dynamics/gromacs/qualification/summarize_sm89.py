"""Verify retained benchmark bytes and summarize every cold/warm outcome."""

import argparse
import csv
import json
import math
from pathlib import Path
import re
import statistics

from benchmark_sm89 import mdp_values, sha256


def mean(values):
    return statistics.mean(values) if values else None


def timing_buckets(log):
    result = {}
    for bucket in ("Neighbor search", "Force", "Wait GPU state copy", "Launch GPU ops", "Rest"):
        match = re.search(r"^[ \t]*" + re.escape(bucket) + r"\s+.+?([\d.]+)\s*$", log, re.MULTILINE)
        if match:
            result[bucket] = float(match[1])
    return result


def summarize(directory):
    records = json.loads((directory / "measurements.json").read_text())
    mdp = mdp_values((directory / "input.mdp").read_text()) if (directory / "input.mdp").exists() else {}
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
        gpu, power, sm_clocks, memory_used = [], [], [], []
        for sample in csv.DictReader((path / "gpu-samples.csv").open()):
            for key, value in sample.items():
                key = key.strip()
                target = (
                    gpu if key.startswith("utilization.gpu")
                    else power if key.startswith("power.draw")
                    else sm_clocks if key.startswith("clocks.current.sm") or key.startswith("clocks.sm")
                    else memory_used if key.startswith("memory.used")
                    else None
                )
                if target is not None and (match := re.search(r"[\d.]+", value or "")):
                    target.append(float(match[0]))
        row["process_gpu_utilization_percent_mean"] = mean(gpu)
        row["process_gpu_power_watts_mean"] = mean(power)
        row["process_sm_clock_mhz_mean"] = mean(sm_clocks)
        row["process_sm_clock_mhz_min"] = min(sm_clocks) if sm_clocks else None
        row["process_sm_clock_mhz_max"] = max(sm_clocks) if sm_clocks else None
        row["process_gpu_memory_mib_max"] = max(memory_used) if memory_used else None
        row["gpu_samples"] = len(gpu)
        delta = row.get("pod_cpu_stat_delta", {})
        row["pod_throttled_period_fraction"] = (
            delta.get("nr_throttled", 0) / delta["nr_periods"]
            if delta.get("nr_periods") else None
        )
        log = (path / "md.log").read_text() if (path / "md.log").exists() else ""
        row["pme_tuning_trial_count"] = len(re.findall(r"^[ \t]*step\s+\d+: timed with pme grid", log, re.MULTILINE))
        pme_final = re.search(r"^\s*final\s+([\d.]+) nm\s+([\d.]+) nm\s+(\d+)\s+(\d+)\s+(\d+)", log, re.MULTILINE)
        row["pme_final_reported"] = ({
            "coulomb_cutoff_nm": float(pme_final[1]), "neighbor_list_nm": float(pme_final[2]),
            "grid": [int(pme_final[index]) for index in (3, 4, 5)],
        } if pme_final else None)
        match = re.search(r"Time:\s+[\d.]+\s+([\d.]+)", log)
        row["native_timed_wall_seconds"] = float(match[1]) if match else None
        row["outside_native_timed_wall_seconds"] = (
            row["process_wall_seconds"] - row["native_timed_wall_seconds"]
            if row["native_timed_wall_seconds"] is not None else None
        )
        row["timing_bucket_percent"] = timing_buckets(log)
        row["native_trajectory_checks"] = {}
        for check in path.glob("md.*.check.log"):
            frames = re.findall(r"Last frame\s+(\d+)\s+time\s+([\d.eE+-]+)", check.read_text())
            assert frames, check
            name = check.name.removesuffix(".check.log")
            row["native_trajectory_checks"][name] = {
                "frames": int(frames[-1][0]) + 1, "last_time_ps": float(frames[-1][1]),
            }
        # The retained unsegmented fixture starts at zero and ends exactly on
        # an XTC output step. Check the promised frame count, not just file size
        # and successful native parsing. Do not infer this count for arbitrary
        # non-aligned or continuation fixtures.
        interval = int(mdp.get("nstxout-compressed", "0"))
        validation = row.get("validation", {})
        if (validation.get("passed") and interval > 0 and int(mdp.get("init-step", "0")) == 0
                and validation.get("expected_steps", -1) % interval == 0):
            trajectory = row["native_trajectory_checks"]["md.xtc"]
            expected = validation["expected_steps"] // interval + 1
            assert trajectory["frames"] == expected, (path, trajectory, expected)
            assert math.isclose(trajectory["last_time_ps"], validation["expected_time_ps"], abs_tol=1e-5)
            trajectory["expected_frames_verified"] = expected
        energy_path = path / "energy-validation.xvg"
        if energy_path.exists():
            energy = energy_path.read_text()
            terms = dict(re.findall(r'^@\s+s(\d+)\s+legend\s+"([^"]+)"', energy, re.MULTILINE))
            values = [
                [float(value) for value in line.split()]
                for line in energy.splitlines()
                if line.strip() and not line.lstrip().startswith(("#", "@", "&"))
            ]
            row["energy_terms"] = {
                name: {"minimum": min(column), "maximum": max(column), "final": column[-1]}
                for index, name in terms.items()
                if (column := [value[int(index) + 1] for value in values])
            }
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
