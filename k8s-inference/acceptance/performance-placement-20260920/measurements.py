"""Retain observed phases and bind hardware to actual Pod/node identities.

This never derives GPU type from the requested pool. Unknown/heterogeneous
environments stay unprofiled. End-to-end latency is measured by the caller.
"""

import hashlib
import json
from datetime import datetime


def get(admin, path):
    response = admin.get(path)
    response.raise_for_status()
    return response.json()["data"]


def observed(measurement, unit):
    if measurement and measurement.get("unit") == unit and measurement.get("evidence") == "measured":
        return measurement.get("value")
    return None


def hardware(nodes, node_ids, gpu_count, image, execution_identity):
    by_id = {node["uid"]: node for node in nodes}
    if not node_ids or not node_ids <= by_id.keys() or not image:
        return None
    selected = [by_id[node_id] for node_id in sorted(node_ids)]
    fields = ("pool", "gpu_product", "gpus_per_node", "cpu_arch", "driver_version", "local_storage")
    if any(any(node.get(field) in (None, "") for field in fields) for node in selected):
        return None
    identity = {field: selected[0][field] for field in fields}
    if any(any(node[field] != identity[field] for field in fields) for node in selected):
        return None
    identity["gpus_per_node"] = int(identity["gpus_per_node"])
    identity.update(gpu_count=gpu_count, runtime_image=image,
                    topology="multi-node" if len(selected) > 1 else "single-device" if gpu_count == 1 else "single-node")
    identity["runtime_fingerprint"] = hashlib.sha256(json.dumps(
        {"hardware": identity, "execution_identity": execution_identity}, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return identity


def collect(admin, evidence, model):
    observations, metrics = {}, {}
    try:
        nodes = get(admin, "/admin/api/v1/performance/hardware")
        observations["hardware_inventory"] = nodes
        if "scientific_receipt" in evidence:
            receipt = evidence["scientific_receipt"]
            detail = get(admin, "/admin/api/v1/scientific-runs/" + evidence["operation_id"])
            observations["scientific_phases"] = detail["lifecycle_phases"]
            observations["gpu_accounting"] = detail["run"]["gpu_accounting"]
            phases = {row["phase"]: row["duration"] for row in detail["lifecycle_phases"]}
            metrics["queue_seconds"] = observed(phases.get("queue"), "seconds")
            metrics["execution_seconds"] = observed(phases.get("active-compute"), "seconds")
            metrics["gpu_occupied_seconds"] = observed(detail["run"]["gpu_accounting"].get("allocated"), "gpu-seconds")
            attempts = [a for a in receipt["attempts"] if (a.get("scheduling_admission") or {}).get("accelerator_count", 0)]
            node_ids = {uid for attempt in attempts for uid in attempt["node_uids"]}
            gpu_ids = {uid for attempt in attempts for uid in attempt["gpu_uuids"]}
            execution_identity = receipt["execution_identity"]
            # Multiple GPU stages or retries need a per-stage profile, not one
            # fabricated single-runtime hardware identity for the whole DAG.
            if len(attempts) == 1 and gpu_ids:
                metrics["hardware"] = hardware(nodes["nodes"], node_ids, len(gpu_ids),
                    execution_identity["runtime_image_digest"], execution_identity)
        else:
            operations = evidence.get("operations", [])
            observations["runtime_interval_semantics"] = "public completed_at minus started_at; includes runtime adapter work"
            if operations and all(op.get("started_at") and op.get("completed_at") for op in operations):
                metrics["execution_seconds"] = sum((datetime.fromisoformat(op["completed_at"]) -
                    datetime.fromisoformat(op["started_at"])).total_seconds() for op in operations)
            if operations and all(op.get("cold_start_seconds") is not None for op in operations):
                metrics["startup_seconds"] = sum(op["cold_start_seconds"] for op in operations)
            identities = {json.dumps(op["runtime"], sort_keys=True) for op in operations}
            if operations and len(identities) == 1:
                runtime = operations[0]["runtime"]
                apps = get(admin, "/admin/api/v1/apps")["items"]
                app = next((item for item in apps if item["public_model_id"] == model), None)
                if app and runtime.get("pod_uid") and runtime.get("node_uid"):
                    containers = get(admin, f"/admin/api/v1/apps/{app['app_id']}/containers")["items"]
                    matches = [c for c in containers if c["pod_uid"] == runtime["pod_uid"] and c["gpu_resources"]]
                    if len(matches) == 1:
                        metrics["hardware"] = hardware(nodes["nodes"], {runtime["node_uid"]}, runtime["gpu_count"],
                            matches[0]["image"], {"model_revision": operations[0]["model_revision"]})
    except Exception as error:
        # Instrumentation failure is visible but cannot change a valid scientific
        # result into a model failure or manufacture missing timings.
        observations["measurement_gap"] = type(error).__name__
    return metrics, observations
