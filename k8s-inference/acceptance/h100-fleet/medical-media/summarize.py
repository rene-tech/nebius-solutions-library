#!/usr/bin/env python3
"""Reduce private direct-pod receipts without exposing model response bodies."""
import argparse
import datetime as dt
import json
import re
import statistics
from pathlib import Path


def stamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def delta(start, end):
    return round((stamp(end) - stamp(start)).total_seconds(), 6)


def summarize(directory):
    pod = json.loads((directory / "pod-ready.json").read_text())
    created = json.loads((directory / "created.json").read_text())
    semantic = json.loads((directory / "semantic.json").read_text()) if (directory / "semantic.json").exists() else {"status": "FAIL"}
    events = json.loads((directory / "events.json").read_text())["items"]
    main = next(c for c in pod["status"]["containerStatuses"] if c["name"] in ("server", "vllm", "model"))
    lines = (directory / (main["name"] + ".log")).read_text().splitlines()
    ready_line = next(line for line in lines if '"event": "model-ready"' in line or "Application startup complete." in line or "evo2-lean-server: READY runtime_identity_sha256=" in line)
    ready_at = ready_line.split(" ", 1)[0]
    initialized = next(c["lastTransitionTime"] for c in pod["status"]["conditions"] if c["type"] == "Ready")
    started = main["state"]["running"]["startedAt"]
    result = {
        "repetition": directory.name,
        "cohort": "initial-empty-weight-cache" if directory.name == "r01" else "fresh-process-retained-weights-image-and-runtime-caches",
        "pod": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"],
        "node": pod["spec"]["nodeName"], "image": main["imageID"],
        "model_revision": pod["metadata"].get("annotations", {}).get("fs2.nebius/model-revision"),
        "compile_cache_abi": pod["metadata"].get("annotations", {}).get("fs2.nebius/compile-cache-abi"),
        "gpu_identity": (directory / "gpu.txt").read_text().strip().splitlines(),
        "probe_requested_at": created["probe_requested_at"],
        "pod_created_at": pod["metadata"]["creationTimestamp"],
        "container_started_at": started, "application_ready_at": ready_at,
        "kubernetes_ready_at": initialized,
        "pod_creation_to_application_ready_seconds": delta(pod["metadata"]["creationTimestamp"], ready_at),
        "container_start_to_application_ready_seconds": delta(started, ready_at),
        "pod_creation_to_kubernetes_ready_seconds": delta(pod["metadata"]["creationTimestamp"], initialized),
        "source_clock_receipt": ready_line,
        "semantic_status": semantic["status"],
        "events": [{"at": e.get("firstTimestamp") or e.get("eventTime"), "reason": e["reason"], "message": e["message"]} for e in events],
    }
    phases = {}
    for line in lines:
        if '"event": "fs2-startup-phase"' in line:
            at, body = line.split(" ", 1)
            # Startup loader/main threads can append LISTENING immediately
            # after a JSON phase marker on the same source-clock log line.
            phases[json.JSONDecoder().raw_decode(body)[0]["name"]] = {"at": at, "source_clock_receipt": line}
    if phases:
        result["startup_phases"] = phases
        result["startup_phase_seconds"] = {
            phase: delta(phases[phase + "-start"]["at"], phases[phase + "-end"]["at"])
            for phase in ("weight-load", "engine-build-or-compile")
            if phase + "-start" in phases and phase + "-end" in phases}
    if directory.parent.name == "evo2" and directory.name == "r01":
        result["cohort"] = "prelocalized-checkpoint-first-process-first-H100-runtime-cache"
        result["validation_delay_note"] = "Application readiness preceded historical-oracle reconciliation; first explicitly validated request was delayed by investigation, not inference."
    elif directory.parent.name == "evo2" and directory.name == "r02":
        result["cohort"] = "fresh-process-retained-weight-image-runtime-cache-RWO-remount-disk-read"
    elif directory.parent.name == "evo2":
        result["cohort"] = "fresh-process-retained-weight-image-runtime-cache-readonly-mount-holder-no-prewarm"
    if directory.parent.name == "evo2" and int(directory.name[1:]) >= 5:
        result["cohort"] = ("device-context-successor-first-compile-retained-OS-page-cache" if directory.name == "r05"
                            else "device-context-successor-fresh-process-retained-runtime-and-OS-page-cache")
    explicit_cohort = pod["metadata"].get("annotations", {}).get("fs2.nebius/cache-cohort")
    if explicit_cohort:
        result["cohort"] = explicit_cohort
    failures = sorted(path.name for path in directory.glob("failure*.json"))
    if failures:
        result["retained_failure_receipts"] = failures
    if result["semantic_status"] != "PASS":
        result["qualification_note"] = "Readiness timestamp is measured but semantic validation failed; excluded from qualified startup aggregates."
    if (directory / "process-io-at-ready.txt").exists():
        result["process_io_at_ready"] = (directory / "process-io-at-ready.txt").read_text()
    if semantic.get("oracle_profile"):
        result["oracle_profile"] = semantic["oracle_profile"]
        result["oracle_sha256"] = semantic["oracle_sha256"]
    if (directory / "runtime-identity.json").exists():
        result["runtime_identity"] = json.loads((directory / "runtime-identity.json").read_text())
    if '"event": "model-ready"' in ready_line:
        result["runtime_reported_startup_seconds"] = json.loads(ready_line.split(" ", 1)[1])["startup_seconds"]
    if semantic.get("requests"):
        request = semantic["requests"][0]
        result["first_request"] = request
        result["first_response_completed_at"] = (stamp(request["request_at"]) + dt.timedelta(seconds=request["elapsed_seconds"])).isoformat()
    else:
        # Older Segment validator records semantic hashes but no client clock.
        # Retain the actual source POST completion instead of inventing one.
        post = next((line for line in lines if '"POST ' in line and ' 200 ' in line), None)
        result["first_server_http_200_at"] = post.split(" ", 1)[0] if post else None
        result["first_client_response_duration_seconds"] = None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = {"schema": "fs2-h100-fleet-direct-startup/v1", "boundary": "direct isolated Pod; not public activation; source-container timestamps (Kubernetes start timestamps have one-second granularity)", "models": {}}
    for model, folder in (("sdxl", "sdxl"), ("nv-segment-ct", "nv-segment-ct"), ("nv-reason-cxr-3b", "cxr"), ("evo2-40b", "evo2")):
        rows = [summarize(directory) for directory in sorted((args.private_root / folder).glob("r[0-9][0-9]"))
                if (directory / "pod-ready.json").exists() and ((directory / "semantic.json").exists() or list(directory.glob("failure*.json")))]
        if not rows:
            continue
        cached = [row for row in rows if row["repetition"] != "r01" and row["semantic_status"] == "PASS"]
        aggregates = {}
        for field in ("container_start_to_application_ready_seconds", "pod_creation_to_application_ready_seconds", "pod_creation_to_kubernetes_ready_seconds", "runtime_reported_startup_seconds"):
            values = [row[field] for row in cached if field in row]
            if values:
                aggregates[field] = {"n": len(values), "median": statistics.median(values), "min": min(values), "max": max(values)}
        result["models"][model] = {"trials": rows, "cached_fresh_process_statistics": aggregates}
        if model == "evo2-40b":
            result["models"][model]["cached_fresh_process_statistics"] = {}
            cohorts = {}
            for row in cached:
                cohorts.setdefault(row["cohort"], []).append(row)
            result["models"][model]["cache_cohort_statistics"] = {
                cohort: {field: {"n": len(values), "median": statistics.median(values), "min": min(values), "max": max(values)}
                    for field in ("container_start_to_application_ready_seconds", "pod_creation_to_application_ready_seconds")
                    if (values := [row[field] for row in cohort_rows])}
                for cohort, cohort_rows in cohorts.items()}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value["cached_fresh_process_statistics"] for key, value in result["models"].items()}, indent=2))


if __name__ == "__main__":
    main()
