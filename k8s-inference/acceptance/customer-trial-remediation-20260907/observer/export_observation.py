"""Export a completed remediation cohort with exact receipt allowlisting."""

from __future__ import annotations

import argparse
import importlib.util
import json
from collections import Counter
from pathlib import Path


def load_previous():
    source = (
        Path(__file__).parents[2]
        / "customer-trial-20260907/observer/summarize_observation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "original_observation_summary", source
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extra_observations(samples):
    qwen_phases, qwen_bursts, disks = Counter(), {}, {}
    qwen_spec_digests = set()
    pod_metric_namespaces = {"pod_cpu_cores": set(), "pod_memory_bytes": set()}
    for sample in samples:
        stamp = sample["started_at"]
        model = sample.get("qwen_model_deployment", {}).get("data", {})
        if model:
            qwen_phases[model.get("status", {}).get("phase", "unavailable")] += 1
            digest = (
                model.get("metadata", {})
                .get("annotations", {})
                .get("inference.fs2.nebius.ai/spec-digest")
            )
            if digest:
                qwen_spec_digests.add(digest)
        by_name = {pod["name"]: pod for pod in sample["pods"].get("data", [])}
        for pod in by_name.values():
            if (
                pod.get("labels", {}).get("fs2-serve.nebius.ai/model-deployment")
                != "qwen3-8b"
            ):
                continue
            if (
                not pod["labels"]
                .get("fs2-serve.nebius.ai/workload-role", "")
                .startswith("burst-")
            ):
                continue
            state = qwen_bursts.setdefault(
                pod["uid"],
                {
                    "name": pod["name"],
                    "uid": pod["uid"],
                    "first_seen": stamp,
                    "states": [],
                },
            )
            conditions = {
                row["type"]: row.get("status") for row in pod.get("conditions", [])
            }
            state["states"].append(
                {
                    "at": stamp,
                    "node": pod.get("node"),
                    "phase": pod.get("phase"),
                    "initialized": conditions.get("Initialized"),
                    "ready": conditions.get("Ready"),
                    "qwen_model_phase": model.get("status", {}).get("phase"),
                }
            )
        for row in (
            sample.get("prom_node_root_disk_available_bytes", {})
            .get("data", {})
            .get("data", {})
            .get("result", [])
        ):
            metric = row["metric"]
            node = by_name.get(metric.get("pod"), {}).get("node")
            key = node or metric.get("instance", "unknown")
            value = float(row["value"][1])
            state = disks.setdefault(
                key,
                {
                    "node": node,
                    "instance": metric.get("instance"),
                    "first_at": stamp,
                    "first_available_bytes": value,
                    "minimum_available_bytes": value,
                    "samples": 0,
                },
            )
            state.update(
                last_at=stamp,
                last_available_bytes=value,
                minimum_available_bytes=min(value, state["minimum_available_bytes"]),
            )
            state["samples"] += 1
        for metric_name, namespaces in pod_metric_namespaces.items():
            rows = sample.get("prom_" + metric_name, {}).get("data", {}).get("data", {}).get("result", [])
            namespaces.update(row["metric"]["namespace"] for row in rows if row.get("metric", {}).get("namespace"))
    return {
        "qwen_model_phase_counts": dict(qwen_phases),
        "qwen_desired_spec_digests": sorted(qwen_spec_digests),
        "qwen_burst_pods": list(qwen_bursts.values()),
        "root_disk_available_bytes_by_node": list(disks.values()),
        "per_pod_metric_observed_namespaces": {
            name: sorted(namespaces) for name, namespaces in pod_metric_namespaces.items()
        },
    }


def campaign_kueue_names(samples, operation_ids):
    names = set()
    for sample in samples:
        workloads = sample.get("workloads", {}).get("data", {}).get("items", [])
        for workload in workloads:
            for pod_set in workload.get("spec", {}).get("podSets", []):
                labels = pod_set.get("template", {}).get("metadata", {}).get("labels", {})
                if labels.get("fs2.nebius.ai/operation-id") in operation_ids:
                    names.add(workload["metadata"]["name"])
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (args.raw / "sampler-completed.json").exists():
        raise ValueError("completed export requires stopped observer receipt")
    samples = [
        json.loads(line)
        for line in (args.raw / "samples.jsonl").read_text().splitlines()
        if line
    ]
    receipts = [
        json.loads(path.read_bytes())
        for path in sorted(args.campaign_dir.glob("*.submitted.json"))
    ]
    operation_ids = {row["operation_id"] for row in receipts}
    if not operation_ids or len(operation_ids) != len(receipts):
        raise ValueError(
            "campaign receipts are missing or contain duplicate operation IDs"
        )
    previous = load_previous()
    result = previous.summarize(samples)
    supporting = Counter(
        (row["model_id"], row["status"])
        for row in result["operations"]
        if row["id"] not in operation_ids
    )
    result["supporting_operation_counts"] = [
        {"model_id": model, "status": status, "count": count}
        for (model, status), count in sorted(supporting.items())
    ]
    result["operations"] = [
        row for row in result["operations"] if row["id"] in operation_ids
    ]
    result["lifecycle"] = [
        row
        for row in result["lifecycle"]
        if row["subject"]["operation_id"] in operation_ids
    ]
    result["campaign_pods"] = [
        row
        for row in result["campaign_pods"]
        if row["labels"].get("fs2.nebius.ai/operation-id") in operation_ids
    ]
    workload_names = campaign_kueue_names(samples, operation_ids)
    result["campaign_kueue_workloads"] = [
        row for row in result["campaign_kueue_workloads"] if row["name"] in workload_names
    ]
    result["campaign_receipt_count"] = len(receipts)
    result["campaign_operation_ids"] = sorted(operation_ids)
    result["limitations"] = [
        note
        for note in result["limitations"]
        if not note.startswith("All-namespace Pod/node")
        and not note.startswith("The installed overview")
        and not note.startswith("No stress-scale SLA")
    ] + [
        "All-namespace Pod/node/Kueue and all-GPU DCGM point samples include academic workloads. "
        "Per-Pod CPU/RAM namespace coverage is exported from observed series; an absent namespace is not zero usage.",
        "Serving Qwen phase and burst Pod observations are sampled every25seconds; lack of observed burst "
        "is a coverage limitation, not proof that autoscaling never occurred.",
        "Detailed operation/lifecycle/Pod records are allowlisted to submitted campaign IDs; "
        "node utilization and total allocation include normal co-tenant traffic.",
        "No stress-scale SLA, model quality or meaningful immunotherapy design is claimed. "
        "Specific bounded priority-preemption recovery is documented only when supported by retained events.",
    ]
    result.update(extra_observations(samples))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # This is a reproducible derived report; private raw cohort evidence is never modified.
    with args.output.open("w") as stream:
        json.dump(result, stream, indent=2)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "samples",
                    "campaign_receipt_count",
                    "api_status_counts",
                    "new_container_restarts",
                    "new_failed_pods",
                    "qwen_model_phase_counts",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
