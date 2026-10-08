"""Cold-serving failover within qualified pools, without moving live sessions.

Kubernetes still places Pods and KEDA still owns replicas. A pool preference is
not a reservation or a claim of free capacity. Only positively observed, old
node loss permits failover; missing observations and an empty scale-from-zero
pool retain the normal policy. Existing generated workloads persist ordering
across controller restarts, including when node telemetry is unavailable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .model_deployment import PoolEnvelope


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
) -> list[str]:
    """Keep placement sticky, moving only unstarted work off a lost pool.

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
