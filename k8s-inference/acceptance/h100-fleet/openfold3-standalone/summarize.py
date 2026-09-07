#!/usr/bin/env python3
"""Source-clock summaries; first compilation and cached fresh processes stay separate."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import statistics
from pathlib import Path


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def trial(directory):
    pod = json.loads((directory / "pod-ready.json").read_text())
    requested = json.loads((directory / "created.json").read_text())["requested_at"]
    logs = (directory / "model.log").read_text()
    phases = {}
    for line in logs.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2 or not parts[1].startswith('{"phase":'):
            continue
        entry = json.loads(parts[1])
        phases.setdefault(entry["phase"], entry)
    semantic = json.loads((directory / "semantic-results.json").read_text())
    if len(semantic["cases"]) != 3:
        raise ValueError("Both original inputs and varied protein must pass")
    container = pod["status"]["containerStatuses"][0]
    started = container["state"]["running"]["startedAt"]
    ready = phases["MODEL_READY"]["time"]
    pod_created = pod["metadata"]["creationTimestamp"]
    k8s_ready = next(c["lastTransitionTime"] for c in pod["status"]["conditions"] if c["type"] == "Ready")
    def delta(begin, end):
        return (timestamp(end) - timestamp(begin)).total_seconds()
    first = semantic["cases"][0]
    return {"trial": directory.name, "pod_uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"],
        "image": pod["spec"]["containers"][0]["image"], "observed_image_id": container["imageID"],
        "gpu": (directory / "gpu.txt").read_text().strip(),
        "cache_cohort": pod["metadata"]["annotations"]["fs2.nebius/cache-cohort"],
        "timestamps": {"probe_create_request": requested, "pod_created": pod_created, "container_started": started,
                       "process_started": phases["PROCESS_START"]["time"], "weight_load_started": phases["WEIGHT_LOAD_BEGIN"]["time"],
                       "model_ready": ready, "kubernetes_ready": k8s_ready,
                       "first_semantic_request": first["started_at"], "first_validated_response": first["completed_at"]},
        "seconds": {"probe_create_request_to_model_ready": delta(requested, ready),
                    "pod_created_to_model_ready": delta(pod_created, ready), "container_to_model_ready": delta(started, ready),
                    "process_to_model_ready": delta(phases["PROCESS_START"]["time"], ready),
                    "weight_load_to_model_ready": delta(phases["WEIGHT_LOAD_BEGIN"]["time"], ready),
                    "pod_created_to_kubernetes_ready": delta(pod_created, k8s_ready),
                    "ready_to_first_request_gap": delta(ready, first["started_at"]),
                    "first_request_to_validated_response": first["seconds"]},
        "semantic": semantic, "source_log_sha256": hashlib.sha256((directory / "model.log").read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initial", type=Path)
    parser.add_argument("--cached", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cached = [trial(path) for path in args.cached]
    if len(cached) < 3:
        raise ValueError("Three independently validated fresh processes are required")
    statistics_rows = {}
    for key in cached[0]["seconds"]:
        values = [row["seconds"][key] for row in cached]
        statistics_rows[key] = {"n": len(values), "median": statistics.median(values), "min": min(values), "max": max(values)}
    report = {"schema": "fs2-serve.nebius.ai/openfold3-preview2-startup/v1", "runtime_origin": "upstream-source-preview2-not-nim",
        "initial_compile_cohort": trial(args.initial) if args.initial else None, "cached_image_fresh_process_trials": cached,
        "statistics": statistics_rows, "measurement_notes": [
            "Direct isolated Pods, not public activation requests.", "Weights are baked into the image; no network weight load during process startup.",
            "Runtime compilation cache is retained by exact image and measured driver/SM ABI.",
            "OS page cache is not evicted or independently controlled.", "GPU weights are loaded before ready; first inference compilation is in first-output latency.",
            "No percentile estimate is made from three trials.",
            "Earlier v2 CCD-decoding and v3 inline-MSA adapter failures are retained in private r01/r02; neither is a qualified trial of this v4 image."]}
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(statistics_rows, indent=2))


if __name__ == "__main__":
    main()
