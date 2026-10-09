"""Cost-ordered cold-serving fallback within qualified pools.

Kubernetes still places Pods and KEDA still owns replicas. Prefer an observed
whole-replica fit; use the bounded lost-node policy if allocation observations
are unavailable. Existing workloads preserve their layout during loading and
execution, and retain it when telemetry is missing. No live-session migration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .model_deployment import PoolEnvelope
from .scientific_batch.podset_envelope import effective_pod_requests, parse_bytes, parse_count, parse_cpu_millis


def pool_headroom(
    *,
    pools: Sequence[PoolEnvelope],
    nodes: Sequence[Mapping[str, Any]],
    pods: Sequence[Mapping[str, Any]],
    pod_specs: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    """Snapshot of whole replicas that fit, not a reservation or a scheduler.

    Count all namespaces, init/sidecar maxima and overhead. Terminating Pods
    retain their allocation until terminal. Kubernetes remains authoritative
    for races, volumes and other scheduling plugins after this preference.
    """
    result = {pool.pool_id: 0 for pool in pools}
    used: dict[str, list[Any]] = {}
    for pod in pods:
        spec = pod.get("spec", {})
        node_name = spec.get("nodeName")
        if node_name and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}:
            used.setdefault(node_name, []).append(effective_pod_requests(spec))
    for pool in pools:
        template = pod_specs.get(pool.pool_id)
        if template is None:
            continue
        request = effective_pod_requests(template)
        for node in nodes:
            meta, spec, status = node.get("metadata", {}), node.get("spec", {}), node.get("status", {})
            if meta.get("deletionTimestamp") or spec.get("unschedulable"):
                continue
            if not all(meta.get("labels", {}).get(k) == v for k, v in template.get("nodeSelector", {}).items()):
                continue
            if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])):
                continue
            if any(
                taint.get("effect") in {"NoSchedule", "NoExecute"}
                and not any(_tolerates(t, taint) for t in template.get("tolerations", []))
                for taint in spec.get("taints", [])
            ):
                continue
            alloc = status.get("allocatable", {})
            # Missing allocatable observations never imply free capacity.
            if not {"cpu", "memory", "pods"}.issubset(alloc):
                continue
            occupied = used.get(meta.get("name"), [])
            bounds = [parse_count(alloc["pods"], label="pods") - len(occupied)]
            for amount, available, consumed in (
                (request.cpu_millis, parse_cpu_millis(alloc["cpu"]), sum(p.cpu_millis for p in occupied)),
                (
                    request.memory_bytes,
                    parse_bytes(alloc["memory"], label="memory"),
                    sum(p.memory_bytes for p in occupied),
                ),
                (
                    request.ephemeral_storage_bytes,
                    parse_bytes(alloc.get("ephemeral-storage", "0"), label="storage"),
                    sum(p.ephemeral_storage_bytes for p in occupied),
                ),
            ):
                if amount:
                    bounds.append((available - consumed) // amount)
            for resource, amount in request.accelerators:
                if amount:
                    available = parse_count(alloc.get(resource, "0"), label=resource)
                    consumed = sum(dict(p.accelerators).get(resource, 0) for p in occupied)
                    bounds.append((available - consumed) // amount)
            result[pool.pool_id] += max(0, min(bounds))
    return result


def _tolerates(toleration: Mapping[str, Any], taint: Mapping[str, Any]) -> bool:
    return (
        (not toleration.get("effect") or toleration["effect"] == taint.get("effect"))
        and (
            toleration.get("key", "") == taint.get("key")
            or (not toleration.get("key") and toleration.get("operator") == "Exists")
        )
        and (toleration.get("operator") == "Exists" or toleration.get("value", "") == taint.get("value", ""))
    )


def _pool_health(pool: PoolEnvelope, nodes: Sequence[Mapping[str, Any]], now: datetime) -> str:
    matching = [
        node
        for node in nodes
        if all(
            node.get("metadata", {}).get("labels", {}).get(key) == value for key, value in pool.node_selector.items()
        )
    ]
    if not matching:
        return "unknown"
    lost = 0
    for node in matching:
        status, spec = node.get("status", {}), node.get("spec", {})
        ready = next((item for item in status.get("conditions", []) if item.get("type") == "Ready"), {})
        if ready.get("status") == "True" and not spec.get("unschedulable"):
            # No claims about free GPU/CPU/memory: the scheduler checks fit.
            if not node.get("metadata", {}).get("deletionTimestamp"):
                return "ready"
        if ready.get("status") != "Unknown" or ready.get("reason") != "NodeStatusUnknown":
            continue
        try:
            transition = datetime.fromisoformat(ready["lastTransitionTime"].replace("Z", "+00:00"))
            if transition.tzinfo is not None and (now - transition).total_seconds() >= 120:
                lost += 1
        except (KeyError, TypeError, ValueError):
            continue
    return "unavailable" if lost == len(matching) else "unknown"


def serving_pool_order(
    *,
    pools: Sequence[PoolEnvelope],
    default_order: Sequence[str],
    observed_order: Sequence[str],
    nodes: Sequence[Mapping[str, Any]] | None,
    has_scheduled_pods: bool,
    now: datetime,
    headroom: Mapping[str, int] | None = None,
) -> list[str]:
    """Use the cheapest measured available option for a cold activation.

    Never collapse/reorder live demand segments: doing so could scale down a
    healthy sibling with in-flight sessions. An active mixed-pool application
    therefore retains its current layout until cold; this is deliberately not
    live-session migration or general capacity-aware load balancing.
    """
    known = {pool.pool_id: pool for pool in pools}
    order = list(dict.fromkeys(ref for ref in (*observed_order, *default_order) if ref in known))
    if set(order) != set(known):
        raise ValueError("serving pool order must cover exactly the qualified pools")
    if len(order) < 2 or nodes is None or has_scheduled_pods:
        return order
    if headroom is not None:
        # Preferences apply to the next cold activation. Returning cheap
        # capacity may be used again, but never migrate a running session.
        available = [ref for ref in default_order if headroom.get(ref, 0) > 0]
        if available:
            first = available[0]
            return [first, *(ref for ref in default_order if ref != first)]
        return order
    health = {ref: _pool_health(known[ref], nodes, now) for ref in order}
    if health[order[0]] != "unavailable":
        return order
    # A positively ready alternative must exist. Never turn missing telemetry
    # or an absent pool into either an outage or an invented healthy fallback.
    alternatives = [ref for ref in order if health[ref] == "ready"]
    if not alternatives:
        return order
    first = alternatives[0]
    return [first, *(ref for ref in order if ref != first)]
