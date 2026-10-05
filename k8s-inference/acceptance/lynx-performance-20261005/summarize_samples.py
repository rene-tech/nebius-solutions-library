"""Summarize retained public observer samples, never infer allocation or cost."""

from collections import defaultdict
import csv
from datetime import datetime
import json
import math
import statistics


def numeric(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def distribution(values):
    values = [value for value in values if value is not None]
    return {"samples": len(values), "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
            "sample_mean": statistics.mean(values) if values else None}


def summarize(path):
    pods = defaultdict(lambda: {"gpu": defaultdict(list), "cpu": [], "failed_gpu_queries": 0,
                                "failed_cpu_queries": 0})
    if not path.exists():
        return {"status": "missing", "pods": [], "scope": "No utilization or CPU counters available."}
    with path.open() as stream:
        for line in stream:
            row = json.loads(line)
            identity = (row["labels"]["fs2.nebius.ai/operation-id"], row["pod_uid"], row["container"])
            pod = pods[identity]
            pod.update(pod=row["pod"], node=row["node"])
            gpu = row["measurements"].get("gpu", {})
            if gpu.get("returncode") == 0:
                for fields in csv.reader(gpu.get("stdout", "").splitlines(), skipinitialspace=True):
                    if len(fields) != 8:
                        pod["failed_gpu_queries"] += 1
                        continue
                    uuid, name, driver, total, used, utilization, memory_utilization, watts = fields
                    pod["gpu"][(uuid, name, driver)].append({
                        "gpu_utilization_percent": numeric(utilization), "memory_utilization_percent": numeric(memory_utilization),
                        "memory_used_mib": numeric(used), "memory_total_mib": numeric(total), "power_watts": numeric(watts)})
            else:
                pod["failed_gpu_queries"] += 1
            cpu = row["measurements"].get("cpu_memory_counters", {})
            if cpu.get("returncode") != 0:
                pod["failed_cpu_queries"] += 1
                continue
            counters, memory = {}, []
            for text in cpu.get("stdout", "").splitlines():
                fields = text.split()
                if len(fields) == 2:
                    counters[fields[0]] = numeric(fields[1])
                elif len(fields) == 1:
                    memory.append(numeric(fields[0]))
            if counters.get("usage_usec") is None or len(memory) != 2:
                pod["failed_cpu_queries"] += 1
                continue
            pod["cpu"].append({"at": row["observed_at"], "counters": counters,
                               "memory_current_bytes": memory[0], "memory_peak_bytes": memory[1]})
    results = []
    for (operation, uid, container), pod in sorted(pods.items()):
        cpu = sorted(pod["cpu"], key=lambda row: row["at"])
        seconds = ((datetime.fromisoformat(cpu[-1]["at"]) - datetime.fromisoformat(cpu[0]["at"])).total_seconds()
                   if len(cpu) > 1 else None)
        deltas = {}
        resets = 0
        for name in ("usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec"):
            values = [row["counters"].get(name) for row in cpu]
            reset = any(b < a for a, b in zip(values, values[1:]) if a is not None and b is not None)
            resets += int(reset)
            deltas[name] = values[-1] - values[0] if len(values) > 1 and None not in values and not reset else None
        usage = deltas["usage_usec"]
        periods, throttled = deltas["nr_periods"], deltas["nr_throttled"]
        results.append({"operation_id": operation, "pod_uid": uid, "container": container,
                        "pod": pod["pod"], "node": pod["node"], "cpu_samples": len(cpu),
                        "sample_window_seconds": seconds, "counter_delta": deltas, "reset_counter_fields": resets,
                        "mean_cpu_cores_over_sample_window": usage / 1e6 / seconds if usage is not None and seconds else None,
                        "throttled_period_fraction": throttled / periods if throttled is not None and periods else None,
                        "memory_current_bytes": distribution([row["memory_current_bytes"] for row in cpu]),
                        "observed_memory_peak_bytes": max((row["memory_peak_bytes"] for row in cpu
                                                           if row["memory_peak_bytes"] is not None), default=None),
                        "failed_gpu_queries": pod["failed_gpu_queries"], "failed_cpu_queries": pod["failed_cpu_queries"],
                        "devices": [{"uuid": key[0], "name": key[1], "driver": key[2],
                                     **{field: distribution([item[field] for item in samples]) for field in samples[0]}}
                                    for key, samples in sorted(pod["gpu"].items())]})
    return {"status": "observed", "pods": results,
            "scope": "Irregular samples include preparation/analysis/publication; means are unweighted, not native-only "
                     "or allocation integrals. CPU cgroup counter deltas span only the retained sample window. "
                     "throttled_usec is aggregate cgroup throttling, not a lost-work or elapsed-time fraction. "
                     "No energy, cost or scientific occupancy is inferred from utilization."}
