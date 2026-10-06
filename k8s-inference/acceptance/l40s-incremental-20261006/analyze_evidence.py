"""Verify retained artifacts and summarize native measurements without pooling hosts."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import statistics


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_inventory(root):
    items = json.loads((root / "inventory.json").read_text())
    for item in items:
        path = (root / item["path"]).resolve()
        if (not path.is_relative_to(root.resolve()) or path.stat().st_size != item["bytes"]
                or sha(path) != item["sha256"]):
            raise ValueError("Retained artifact identity differs: " + item["path"])
    return {"files_verified": len(items), "bytes_verified": sum(i["bytes"] for i in items)}


def gpu_samples(path):
    if not path.is_file():
        return {}
    columns = {}
    with path.open() as source:
        for row in csv.DictReader(source, skipinitialspace=True):
            for name, value in row.items():
                if name.startswith("timestamp"):
                    continue
                match = re.match(r"\s*([\d.]+)", value or "")
                if match:
                    columns.setdefault(name, []).append(float(match[1]))
    return {name: {"samples": len(values), "mean": statistics.mean(values), "min": min(values), "max": max(values)}
            for name, values in columns.items()}


def interval_union(intervals):
    total, current_start, current_end = 0, None, None
    for start, end in sorted(intervals):
        if current_start is None:
            current_start, current_end = start, end
        elif start > current_end:
            total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    return total + (current_end - current_start if current_start is not None else 0)


def profile_summary(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        result = {"sqlite_sha256": sha(path), "tables": sorted(tables)}
        intervals = []
        for table in ("CUPTI_ACTIVITY_KIND_KERNEL", "CUPTI_ACTIVITY_KIND_MEMCPY", "CUPTI_ACTIVITY_KIND_MEMSET"):
            if table in tables:
                intervals += [(row[0], row[1]) for row in connection.execute(f"select start,end from {table}")]
        if intervals:
            span = max(end for _, end in intervals) - min(start for start, _ in intervals)
            busy = interval_union(intervals)
            result["gpu_timeline"] = {"events": len(intervals), "observed_event_span_seconds": span / 1e9,
                                       "busy_union_seconds": busy / 1e9, "idle_inside_event_span_seconds": (span - busy) / 1e9,
                                       "busy_fraction_of_event_span": busy / span}
        for table, name_field, destination in (
            ("CUPTI_ACTIVITY_KIND_KERNEL", "demangledName", "kernels"),
            ("CUPTI_ACTIVITY_KIND_RUNTIME", "nameId", "cuda_runtime_calls"),
            ("CUPTI_ACTIVITY_KIND_DRIVER", "nameId", "cuda_driver_calls"),
        ):
            if table in tables:
                result[destination] = [dict(row) for row in connection.execute(
                    f"select s.value as name,count(*) as calls,sum(t.end-t.start)/1e9 as total_seconds "
                    f"from {table} t join StringIds s on s.id=t.{name_field} "
                    "group by s.value order by total_seconds desc limit 20")]
        if "CUPTI_ACTIVITY_KIND_MEMCPY" in tables:
            result["copies"] = [dict(row) for row in connection.execute(
                "select copyKind,count(*) as copies,sum(bytes) as bytes,sum(end-start)/1e9 as total_seconds "
                "from CUPTI_ACTIVITY_KIND_MEMCPY group by copyKind")]
            if "ENUM_CUDA_MEMCPY_OPER" in tables:
                result["copy_kind_enum"] = [dict(row) for row in connection.execute("select * from ENUM_CUDA_MEMCPY_OPER")]
        return result


def analyze(root):
    result = {"evidence_root": str(root), "inventory": verify_inventory(root),
              "summary": json.loads((root / "summary.json").read_text()), "runs": []}
    for path in sorted(root.glob("*/receipt.json")):
        record = json.loads(path.read_text())
        process_wall = record["process_wall_seconds"]
        cpu = record.get("cpu_stat_delta", {})
        result["runs"].append({
            "case": record["case"], "repeat": record["repeat"], "steps": record["steps"],
            "simulated_ns": record["simulated_ns"], "process_wall_seconds": process_wall,
            "process_inclusive_ns_per_day": record["process_inclusive_ns_per_day"],
            "native_ns_per_day": record["native_ns_per_day"], "segments": len(record["segments"]),
            "validation_passed": record["validation"]["passed"],
            "mean_cpu_cores_including_native_checkpoint_inspection": cpu.get("usage_usec", 0) / 1e6 / record["component_wall_seconds"],
            "cgroup_throttled_seconds": cpu.get("throttled_usec", 0) / 1e6,
            "gpu": gpu_samples(path.parent / "gpu-samples.csv"),
        })
    for path in root.glob("profile/*.sqlite"):
        result["profile"] = profile_summary(path)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze(args.results.resolve())
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"inventory": result["inventory"], "validated_runs": sum(r["validation_passed"] for r in result["runs"]),
                      "profile_captured": "profile" in result, "output": str(args.output)}))
