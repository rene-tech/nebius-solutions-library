"""Recompute small-n statistics and failure counts from retained raw attempts."""
import collections
import datetime
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
unique = {}
for path in sorted((ROOT / "evidence").rglob("attempts.jsonl"), key=lambda p: len(str(p))):
    for line in path.read_text().splitlines():
        row = json.loads(line)
        key = (row["variant"], row["attempt"], row["started_unix"])
        unique.setdefault(key, {**row, "evidence": str(path.relative_to(ROOT))})
groups = collections.defaultdict(list)
for row in unique.values():
    groups[row["variant"]].append(row)
summary = {}
for name, rows in sorted(groups.items()):
    cases = {}
    for fixture in sorted({row["fixture"] for row in rows}):
        selected = [row for row in rows if row["fixture"] == fixture and row["status"] == "valid"
                    and row["cohort"].startswith("repeat-")]
        if not selected:
            continue
        times = [row["http_wall_seconds"] for row in selected]
        validated = [row["validated_wall_seconds"] for row in selected]
        quality = [sample["ca_lddt"] for row in selected for sample in row["quality"]]
        cases[fixture] = {"n": len(times), "http_seconds": times,
                          "http_median_seconds": statistics.median(times),
                          "http_min_seconds": min(times), "http_max_seconds": max(times),
                          "validated_seconds": validated, "validated_median_seconds": statistics.median(validated),
                          "ca_lddt_samples": quality, "ca_lddt_mean": statistics.mean(quality),
                          "ca_lddt_min": min(quality), "ca_lddt_max": max(quality)}
    valid = [row for row in rows if row["status"] == "valid"]
    summary[name] = {"attempts": len(rows), "valid": len(valid), "failed": len(rows) - len(valid),
                     "evidence": sorted({row["evidence"] for row in rows}), "repeated_cases": cases,
                     "first_request": rows[0],
                     "mixed_serial": [row for row in rows if row["cohort"] == "mixed-serial"],
                     "sum_request_wall_seconds": sum(row["validated_wall_seconds"] for row in rows)}

comparisons = {}
for gpu, baseline, persistent, bir in [
    ("h100", "current-h100", "persistent-h100-valid", "bir-h100-matrix"),
    ("l40s", "current-l40s-matrix", "persistent-l40s-matrix", "bir-l40s-seeded"),
]:
    comparisons[gpu] = {}
    for fixture in ("T1031", "T1038", "T1096"):
        values = {}
        validated_values = {}
        for label, key in [("current", baseline), ("persistent", persistent), ("bir", bir),
                           ("persistent_tools", f"persistent-tools-{gpu}-matrix")]:
            case = summary.get(key, {}).get("repeated_cases", {}).get(fixture)
            if case:
                values[label] = case["http_median_seconds"]
                validated_values[label] = case["validated_median_seconds"]
        comparisons[gpu][fixture] = {"http_median_seconds": values, "validated_median_seconds": validated_values}
        if "bir" in values:
            comparisons[gpu][fixture]["ratio_to_bir"] = {key: value / values["bir"] for key, value in values.items() if key != "bir"}
            comparisons[gpu][fixture]["validated_ratio_to_bir"] = {key: value / validated_values["bir"] for key, value in validated_values.items() if key != "bir"}

phases = {}
for path in (ROOT / "raw").glob("*-logs.stdout"):
    entries = []
    for line in path.read_text().splitlines():
        if line.startswith("EVAL_PHASES "):
            entries.append(json.loads(line.removeprefix("EVAL_PHASES ")))
    if entries:
        phases[path.stem] = entries

events_path = ROOT / "raw/evaluation-events.stdout"
allocations = {}
confirmed_allocations = []
for path in (ROOT / "raw").glob("*-final-pod.stdout"):
    pod = json.loads(path.read_text())
    prefix = path.name.removesuffix("-final-pod.stdout")
    deletion = ROOT / "raw" / (prefix + "-delete.command.json")
    if not deletion.exists():
        continue
    end = json.loads(deletion.read_text())["ended_unix"]
    scheduled = next(c["lastTransitionTime"] for c in pod["status"]["conditions"] if c["type"] == "PodScheduled")
    start = datetime.datetime.fromisoformat(scheduled.replace("Z", "+00:00")).timestamp()
    gpu = "h100" if "h100" in pod["metadata"]["name"] else "l40s"
    rows = [r for r in unique.values() if gpu in r["variant"] and start <= r["started_unix"] < end]
    valid = sum(r["status"] == "valid" for r in rows)
    seconds = end - start
    request_seconds = sum(r["validated_wall_seconds"] for r in rows)
    confirmed_allocations.append({"pod": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                                  "gpu": gpu, "scheduled_unix": start, "delete_confirmed_unix": end,
                                  "allocated_gpu_seconds": seconds, "valid_requests": valid,
                                  "attempts": len(rows), "allocated_gpu_seconds_per_valid_request": seconds / valid if valid else None,
                                  "valid_requests_per_allocated_gpu_hour": valid * 3600 / seconds,
                                  "sum_validated_request_seconds": request_seconds,
                                  "outside_recorded_requests_seconds": max(0, seconds - request_seconds),
                                  "outside_request_note": "Includes image/init/startup/loading/idle/operator gaps/collection/termination; not measured GPU-idle telemetry."})
if events_path.exists():
    for event in json.loads(events_path.read_text())["items"]:
        obj = event["involvedObject"]
        if obj["kind"] != "Pod" or not obj["name"].startswith("boltz2-"):
            continue
        item = allocations.setdefault(obj["uid"], {"pod": obj["name"], "uid": obj["uid"]})
        if event["reason"] in ("Scheduled", "Started", "Killing", "Failed", "BackOff"):
            item.setdefault("events", []).append({"reason": event["reason"], "first": event.get("firstTimestamp"),
                                                   "last": event.get("lastTimestamp"), "message": event["message"]})

allocation_bounds = {}
exact_by_uid = {item["uid"]: item for item in confirmed_allocations}
for gpu in ("h100", "l40s"):
    pods = []
    for item in allocations.values():
        if gpu not in item["pod"]:
            continue
        scheduled = next((event["first"] for event in item.get("events", []) if event["reason"] == "Scheduled"), None)
        if scheduled:
            pods.append((datetime.datetime.fromisoformat(scheduled.replace("Z", "+00:00")).timestamp(), item))
    pods.sort(key=lambda pair: pair[0])
    details = []
    for index, (start, item) in enumerate(pods):
        exact = exact_by_uid.get(item["uid"])
        if exact:
            lower = upper = exact["allocated_gpu_seconds"]
            method = "schedule through confirmed delete response (conservative confirmation tail)"
        else:
            kills = [datetime.datetime.fromisoformat(e["last"].replace("Z", "+00:00")).timestamp()
                     for e in item.get("events", []) if e["reason"] == "Killing" and e.get("last")]
            lower = max(0, max(kills, default=start) - start)
            upper = max(0, pods[index + 1][0] - start) if index + 1 < len(pods) else None
            method = "Killing event lower bound; next one-GPU pod schedule upper bound, including possible unallocated gap"
        details.append({"pod": item["pod"], "uid": item["uid"], "gpu_seconds_lower": lower,
                        "gpu_seconds_upper": upper, "method": method})
    valid = sum(row["status"] == "valid" and gpu in row["variant"] for row in unique.values())
    lower = sum(row["gpu_seconds_lower"] for row in details)
    upper = sum(row["gpu_seconds_upper"] for row in details) if all(row["gpu_seconds_upper"] is not None for row in details) else None
    allocation_bounds[gpu] = {"pod_windows": details, "valid_predictions": valid, "all_attempts_gpu_seconds_lower": lower,
                              "all_attempts_gpu_seconds_upper": upper,
                              "gpu_seconds_per_valid_lower": lower / valid if valid else None,
                              "gpu_seconds_per_valid_upper": upper / valid if valid and upper is not None else None,
                              "valid_per_gpu_hour_lower": valid * 3600 / upper if upper else None,
                              "valid_per_gpu_hour_upper": valid * 3600 / lower if lower else None}

schema_probes = {}
for path in (ROOT / "evidence").glob("*-schema.json"):
    schema_probes[path.name] = json.loads(path.read_text())
result = {"deduplication": "Exact variant/attempt/start clock tuple; redundant backups are not counted twice.",
          "total_prediction_attempts": len(unique), "total_valid_predictions": sum(r["status"] == "valid" for r in unique.values()),
          "total_failed_predictions": sum(r["status"] != "valid" for r in unique.values()),
          "schema_probe_attempts": sum(len(rows) for rows in schema_probes.values()),
          "schema_probe_passes": sum(row["pass"] for rows in schema_probes.values() for row in rows),
          "schema_probe_evidence": schema_probes,
          "startup_failures_separate": ["L40S BIR cache permission", "L40S BIR missing C compiler", "L40S BIR unpack_row import"],
          "non_http_diagnostics_separate": ["One direct current homomer reproduction", "One interrupted in-flight BIR adapter request; see logs"],
          "cohorts": summary, "same_gpu_comparisons": comparisons, "phase_logs": phases,
          "allocation_events": list(allocations.values()),
          "confirmed_pod_allocations": confirmed_allocations,
          "all_pod_allocation_bounds": allocation_bounds,
          "allocation_limitations": "Confirmed pod schedule-to-delete windows include termination confirmation and are conservative; failed early pod windows remain in event evidence, not silently charged zero.",
          "limitations": ["n=3 per representative case; no p95/p99 inference", "CA-lDDT is not all-atom lDDT",
                          "Complex flattened CA-lDDT is exploratory; chain IDs and DockQ need separate gate",
                          "Initial current baseline cannot accept a seed; candidates use 42+request index",
                          "No shared caches flushed; startup/image/artifact cohorts separated",
                          "BIR and upstream torch/runtime versions differ; library+runtime package comparison, not a single-kernel causal experiment"]}
(ROOT / "statistics.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({key: result[key] for key in ("total_prediction_attempts", "total_valid_predictions", "total_failed_predictions", "same_gpu_comparisons")}, indent=2))
