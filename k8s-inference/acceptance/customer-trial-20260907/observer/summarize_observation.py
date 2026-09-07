#!/usr/bin/env python3
"""Produce payload-free trial evidence from the observer's private JSONL stream."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def seconds(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def stats(values: list[float]) -> dict:
    ordered = sorted(x for x in values if math.isfinite(x))
    if not ordered:
        return {"samples": 0, "min": None, "median": None, "p95": None, "max": None}
    return {
        "samples": len(ordered),
        "min": min(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)],
        "max": max(ordered),
    }


def prom(sample: dict, name: str) -> list[dict]:
    return (
        sample.get("prom_" + name, {}).get("data", {}).get("data", {}).get("result", [])
    )


def data(sample: dict, name: str) -> dict:
    return sample.get(name, {}).get("data", {}).get("data", {})


def pod_gpu_count(pod: dict) -> float:
    regular = sum(
        float(c.get("requests", {}).get("nvidia.com/gpu", 0))
        for c in pod.get("resources", [])
    )
    init = max(
        (
            float(c.get("requests", {}).get("nvidia.com/gpu", 0))
            for c in pod.get("init_resources", [])
        ),
        default=0,
    )
    return max(regular, init)


def summarize(samples: list[dict]) -> dict:
    start, end = samples[0]["started_at"], samples[-1]["completed_at"]
    start_seconds = seconds(start)
    baseline_pods = {p["uid"]: p for p in samples[0]["pods"].get("data", [])}
    latest_pods, latest_ops, lifecycle = {}, {}, {}
    pod_last_seen = {}
    pod_states, operation_states = defaultdict(list), defaultdict(list)
    api_status, api_latency = defaultdict(Counter), defaultdict(list)
    node_conditions, serving_unready, restarts, new_failed_pods = {}, {}, {}, {}
    node_startup_conditions, ready_seen = {}, set()
    metrics, allocations, queue, gpu_by_model = (
        defaultdict(list),
        [],
        [],
        defaultdict(list),
    )
    missing = Counter()
    sampled_integral, sampled_allocated, sampled_allocated_idle = 0.0, 0.0, 0.0
    integral_coverage_seconds, allocation_coverage_seconds = 0.0, 0.0
    total_nodes, total_gpus = [], []
    workload_conditions = {}
    for index, sample in enumerate(samples):
        stamp = sample["started_at"]
        for name in ("capacity", "operations", "telemetry", "overview"):
            reply = sample.get(name, {})
            api_status[name][
                str(reply.get("status", reply.get("error", "missing")))
            ] += 1
            api_latency[name].append(reply.get("elapsed_seconds", math.nan))
        for name, reply in sample.items():
            if name.startswith("prom_") and not prom(sample, name[5:]):
                missing[name[5:]] += 1
        allocated = 0.0
        for pod in sample["pods"].get("data", []):
            latest_pods[pod["uid"]] = pod
            pod_last_seen[pod["uid"]] = stamp
            if pod["node"] and pod["phase"] not in ("Succeeded", "Failed"):
                allocated += pod_gpu_count(pod)
            previous_state = (
                pod_states[pod["uid"]][-1]["phase"] if pod_states[pod["uid"]] else None
            )
            if previous_state != pod["phase"]:
                pod_states[pod["uid"]].append({"at": stamp, "phase": pod["phase"]})
            if pod["uid"] not in baseline_pods and pod["phase"] == "Failed":
                new_failed_pods[pod["uid"]] = {
                    "name": pod["name"],
                    "namespace": pod["namespace"],
                    "at": stamp,
                    "statuses": [
                        {"name": c["name"], "state": c["state"]}
                        for c in pod.get("container_statuses", [])
                    ],
                }
            for c in pod.get("container_statuses", []):
                base = next(
                    (
                        x.get("restartCount", 0)
                        for x in baseline_pods.get(pod["uid"], {}).get(
                            "container_statuses", []
                        )
                        if x["name"] == c["name"]
                    ),
                    0,
                )
                if c.get("restartCount", 0) > base:
                    restarts[pod["uid"] + "/" + c["name"]] = {
                        "pod": pod["name"],
                        "container": c["name"],
                        "new_restarts": c["restartCount"] - base,
                    }
            labels = pod.get("labels", {})
            if (
                labels.get("app.kubernetes.io/component") == "model-runtime"
                and pod["uid"] in baseline_pods
                and pod["phase"] not in ("Succeeded", "Failed")
            ):
                ready = any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in pod.get("conditions", [])
                )
                if not ready:
                    serving_unready[pod["uid"]] = {
                        "name": pod["name"],
                        "last_at": stamp,
                        "phase": pod["phase"],
                    }
        if "data" in sample["pods"]:
            allocations.append(allocated)
        nodes = sample["nodes"].get("data", [])
        if "data" in sample["nodes"]:
            total_nodes.append(len(nodes))
            total_gpus.append(
                sum(
                    float(n["status"].get("allocatable", {}).get("nvidia.com/gpu", 0))
                    for n in nodes
                )
            )
        for node in nodes:
            for condition in node["status"].get("conditions", []):
                if condition["type"] == "Ready" and condition["status"] == "True":
                    ready_seen.add(node["name"])
                bad = (
                    condition["status"] != "True"
                    if condition["type"] == "Ready"
                    else condition["status"] == "True"
                    if condition["type"]
                    in (
                        "MemoryPressure",
                        "DiskPressure",
                        "PIDPressure",
                        "NetworkUnavailable",
                    )
                    else False
                )
                if bad:
                    target = (
                        node_startup_conditions
                        if node["name"] not in ready_seen and index > 0
                        else node_conditions
                    )
                    target[node["name"] + "/" + condition["type"]] = {
                        "node": node["name"],
                        "at": stamp,
                        **condition,
                    }
        for op in data(sample, "operations").get("items", []):
            if seconds(op["accepted_at"]) < start_seconds:
                continue
            latest_ops[op["id"]] = {
                key: op.get(key)
                for key in (
                    "id",
                    "model_id",
                    "operation",
                    "protocol",
                    "status",
                    "outcome",
                    "semantic_outcome",
                    "attempt",
                    "max_attempts",
                    "accepted_at",
                    "completed_at",
                    "error_class",
                    "http_status",
                    "timings",
                    "gpu_count",
                    "preemptible",
                )
            }
            old = (
                operation_states[op["id"]][-1]["state"]
                if operation_states[op["id"]]
                else None
            )
            if old != op["status"]:
                operation_states[op["id"]].append({"at": stamp, "state": op["status"]})
        for item in data(sample, "telemetry").get("items", []):
            subject = item["subject"]
            if seconds(subject["accepted_at"]) < start_seconds:
                continue
            lifecycle[subject["subject_id"]] = {
                "subject": {
                    key: subject.get(key)
                    for key in (
                        "subject_id",
                        "operation_id",
                        "workload_id",
                        "attempt_id",
                        "batch_id",
                        "model_id",
                        "model_revision",
                        "workload_kind",
                        "accepted_at",
                    )
                },
                "rollup": {
                    key: item.get("rollup", {}).get(key)
                    for key in (
                        "generated_at",
                        "terminal",
                        "outcome",
                        "quota_reserved_gpu_seconds",
                        "scheduler_occupied_gpu_seconds",
                        "device_allocated_gpu_seconds",
                        "active_gpu_seconds",
                        "occupied_idle_gpu_seconds",
                        "phase_gpu_seconds",
                        "reconciled",
                        "quality",
                        "data_gaps",
                        "device_scheduler_delta_seconds",
                        "reconciliation_delta_seconds",
                    )
                }
                if item.get("rollup")
                else None,
            }
        if prom(sample, "operations"):
            queue.append(
                sum(
                    float(row["value"][1])
                    for row in prom(sample, "operations")
                    if row["metric"].get("state") == "queued"
                )
            )
        for name in (
            "node_cpu_busy_percent",
            "node_memory_used_percent",
            "node_root_disk_used_percent",
            "pod_cpu_cores",
            "pod_memory_bytes",
        ):
            for row in prom(sample, name):
                metrics[name].append(float(row["value"][1]))
        gpu_rows = prom(sample, "gpu_utilization")
        for row in gpu_rows:
            labels = row["metric"]
            model = (
                labels.get("fs2_nebius_ai_model_id")
                or labels.get("app_kubernetes_io_name")
                or labels.get("pod", "unattributed")
            )
            gpu_by_model[model].append(float(row["value"][1]))
        if index + 1 < len(samples):
            dt = seconds(samples[index + 1]["started_at"]) - seconds(stamp)
            # Left-held sample approximation; retain the sampling caveat.
            if "data" in sample["pods"]:
                sampled_allocated += dt * allocated
                allocation_coverage_seconds += dt
            if gpu_rows:
                integral_coverage_seconds += dt
            by_uuid = {
                row["metric"].get("UUID", str(i)): row for i, row in enumerate(gpu_rows)
            }
            sampled_integral += dt * sum(
                min(100, max(0, float(row["value"][1]))) / 100
                for row in by_uuid.values()
            )
            sampled_allocated_idle += dt * sum(
                row["metric"].get("namespace") in ("fs2-models", "fs2-academic-poc")
                and float(row["value"][1]) <= 1
                for row in by_uuid.values()
            )
        for item in sample.get("workloads", {}).get("data", {}).get("items", []):
            if seconds(item["metadata"]["creationTimestamp"]) >= start_seconds:
                workload_conditions[item["metadata"]["uid"]] = {
                    "name": item["metadata"]["name"],
                    "created_at": item["metadata"]["creationTimestamp"],
                    "conditions": item.get("status", {}).get("conditions", []),
                    "admission": item.get("status", {}).get("admission"),
                    "last_seen": stamp,
                }
    scientific_pods = [
        {
            "uid": uid,
            "name": p["name"],
            "labels": {
                k: v
                for k, v in p.get("labels", {}).items()
                if k.startswith("fs2") and "tenant" not in k
            },
            "created_at": p["created_at"],
            "node": p["node"],
            "last_phase": p["phase"],
            "last_seen_at": pod_last_seen[uid],
            "present_in_final_sample": uid
            in {p["uid"] for p in samples[-1]["pods"].get("data", [])},
            "states": pod_states[uid],
        }
        for uid, p in latest_pods.items()
        if uid not in baseline_pods
        and p["namespace"] in ("fs2-models", "fs2-academic-poc")
    ]
    return {
        "observed_start": start,
        "observed_end": end,
        "samples": len(samples),
        "sample_elapsed_seconds": stats([s["elapsed_seconds"] for s in samples]),
        "baseline_failed_pods_excluded": [
            {
                "name": p["name"],
                "namespace": p["namespace"],
                "created_at": p["created_at"],
            }
            for p in baseline_pods.values()
            if p["phase"] == "Failed"
        ],
        "api_status_counts": {k: dict(v) for k, v in api_status.items()},
        "api_latency_seconds": {k: stats(v) for k, v in api_latency.items()},
        "new_failed_pods": list(new_failed_pods.values()),
        "new_container_restarts": list(restarts.values()),
        "node_condition_incidents": list(node_conditions.values()),
        "new_node_initialization_conditions": list(node_startup_conditions.values()),
        "baseline_serving_pod_unready": list(serving_unready.values()),
        "allocatable_gpus": stats(total_gpus),
        "node_count": stats(total_nodes),
        "scheduler_requested_gpus": stats(allocations),
        "queued_operations": stats(queue),
        "resource_sample_values": {k: stats(v) for k, v in metrics.items()},
        "gpu_utilization_percent_by_label": {
            k: stats(v) for k, v in gpu_by_model.items()
        },
        "approximate_sampled_gpu_seconds": {
            "hardware_busy_fraction_integral": sampled_integral,
            "scheduler_requested": sampled_allocated,
            "allocated_with_utilization_at_most_1_percent": sampled_allocated_idle,
            "dcgm_coverage_seconds": integral_coverage_seconds,
            "scheduler_observation_coverage_seconds": allocation_coverage_seconds,
        },
        "missing_metric_sample_counts": dict(missing),
        "operations": [
            {**op, "observed_states": operation_states[op_id]}
            for op_id, op in latest_ops.items()
        ],
        "lifecycle": list(lifecycle.values()),
        "campaign_pods": scientific_pods,
        "campaign_kueue_workloads": list(workload_conditions.values()),
        "limitations": [
            "25-second point samples can miss short stalls, utilization bursts, and Pods; API requests are measured directly, not sampled latency histograms.",
            "Hardware-busy GPU seconds are a left-held DCGM utilization approximation, not billing or exact kernel execution time. Lifecycle active_compute describes application execution phase, not hardware busy time.",
            "At-most-1%-utilization allocated seconds combine resident-idle, initialization, synchronization and grace; use per-workload lifecycle phases for causal classification.",
            "Only requests accepted after observer start are counted. Latest 200 operations and workload records per sample; very high throughput can exceed this window.",
            "All-namespace Pod/node and all-GPU DCGM samples include academic workloads. Periodic Kueue and per-Pod CPU/RAM queries cover fs2-models/fs2-system only; exact academic scheduling evidence is captured separately. Node CPU/RAM covers all workloads.",
            "The installed overview reports integrated DCGM GPU seconds and TTFT unavailable. Its baseline durable-vs-Prometheus terminal reconciliation appears to compare differing time windows.",
            "No stress-scale SLA, preemption recovery, model quality, or meaningful immunotherapy design is claimed by this synthetic bounded trial.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--campaign-dir",
        type=Path,
        help="Retain detailed records only for *.submitted.json operation IDs",
    )
    args = parser.parse_args()
    samples = []
    with args.input.open() as stream:
        for line in stream:
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                # A live sampler may be appending the final line.
                break
    if not samples:
        raise SystemExit("No complete samples")
    result = summarize(samples)
    if args.campaign_dir:
        submitted = [
            json.loads(path.read_bytes())
            for path in sorted(args.campaign_dir.glob("*.submitted.json"))
        ]
        operation_ids = {item["operation_id"] for item in submitted}
        supporting = Counter(
            (item["model_id"], item["operation"], item["status"])
            for item in result["operations"]
            if item["id"] not in operation_ids
        )
        result["supporting_operation_counts"] = [
            {"model_id": model, "operation": operation, "status": state, "count": count}
            for (model, operation, state), count in sorted(supporting.items())
        ]
        result["operations"] = [
            item for item in result["operations"] if item["id"] in operation_ids
        ]
        result["lifecycle"] = [
            item
            for item in result["lifecycle"]
            if item["subject"]["operation_id"] in operation_ids
        ]
        result["campaign_receipt_count"] = len(submitted)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "observed_start",
                    "observed_end",
                    "samples",
                    "api_status_counts",
                    "new_failed_pods",
                    "new_container_restarts",
                    "node_condition_incidents",
                    "baseline_serving_pod_unready",
                    "allocatable_gpus",
                    "scheduler_requested_gpus",
                    "queued_operations",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
