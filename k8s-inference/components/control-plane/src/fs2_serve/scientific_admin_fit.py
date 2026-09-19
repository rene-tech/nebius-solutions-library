"""Negative placement evidence from existing node reads; never a scheduler."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .admin_adapters import KubernetesListReader
from .scientific_admin_models import (
    ScientificNodeUpperBoundFit,
    ScientificPlacementConstraints,
    ScientificPoolUpperBoundFit,
    ScientificStage,
)
from .scientific_batch.podset_envelope import parse_resource_quantity

# The renderer's required nodeAffinity uses this exact identity, not semantic
# hot/burst labels or GPU product names that may span multiple pools.
POOL_LABEL = "accelerator.fs2.nebius/pool-id"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _amount(value: Any, kind: str) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return parse_resource_quantity(kind, value, label="node allocatable")
    except (ValueError, OverflowError):
        return None


def project_node_upper_bounds(
    placement: ScientificPlacementConstraints,
    nodes: Sequence[Mapping[str, Any]],
    *,
    observed_at: datetime,
) -> ScientificNodeUpperBoundFit:
    pools = placement.eligible_pool_ids or ["selector-only"]
    results: list[ScientificPoolUpperBoundFit] = []
    for pool in pools:
        members = [node for node in nodes if not placement.eligible_pool_ids
                   or _mapping(_mapping(node.get("metadata")).get("labels")).get(POOL_LABEL) == pool]
        reasons: Counter[str] = Counter()
        maxima: dict[str, int] = {}
        possible = unknown = 0
        for node in members:
            metadata = _mapping(node.get("metadata"))
            labels = _mapping(metadata.get("labels"))
            spec, status = _mapping(node.get("spec")), _mapping(node.get("status"))
            allocatable = _mapping(status.get("allocatable"))
            blocked: set[str] = set()
            missing = False
            conditions = status.get("conditions")
            if not any(_mapping(c).get("type") == "Ready" and _mapping(c).get("status") == "True"
                       for c in (conditions if isinstance(conditions, list) else [])):
                blocked.add("node_not_ready")
            if metadata.get("deletionTimestamp") or spec.get("unschedulable"):
                blocked.add("node_cordoned_or_deleting")
            if any(labels.get(key) != value for key, value in placement.required_node_labels.items()):
                blocked.add("required_node_label_mismatch")
            requests = {
                "cpu": placement.pod_cpu_millis,
                "memory": placement.pod_memory_bytes,
                "ephemeral-storage": placement.pod_ephemeral_storage_bytes,
            }
            if placement.accelerator_count:
                if placement.accelerator_resource_name:
                    requests[placement.accelerator_resource_name] = placement.accelerator_count
                else:
                    missing = True
            for resource, required in requests.items():
                available = _amount(allocatable.get(resource), resource)
                # Extended resources absent from a valid allocatable map are
                # not exposed on this node. Missing base quantities are unknown.
                if resource not in {"cpu", "memory", "ephemeral-storage"} and available is None:
                    if resource not in allocatable and allocatable:
                        available = 0
                if available is not None:
                    maxima[resource] = max(maxima.get(resource, 0), available)
                if required is None or available is None:
                    missing = True
                elif required > available:
                    key = resource if resource in {"cpu", "memory", "ephemeral-storage"} else "accelerator"
                    blocked.add(f"{key.replace('-', '_')}_request_exceeds_node_allocatable")
            reasons.update(blocked)
            if not blocked:
                if missing:
                    unknown += 1
                else:
                    possible += 1
        if not members:
            reasons["no_current_nodes_in_pool"] = 1
        results.append(ScientificPoolUpperBoundFit(
            pool_id=pool, state="possible" if possible else "unknown" if unknown else "blocked",
            nodes_observed=len(members), possible_nodes=possible, unknown_nodes=unknown,
            blocking_reasons=dict(sorted(reasons.items())),
            max_allocatable_cpu_millis=maxima.get("cpu"),
            max_allocatable_memory_bytes=maxima.get("memory"),
            max_allocatable_ephemeral_storage_bytes=maxima.get("ephemeral-storage"),
            max_allocatable_accelerators=maxima.get(placement.accelerator_resource_name or ""),
        ))
    return ScientificNodeUpperBoundFit(observed_at=observed_at, pools=results)


class ScientificNodeFitAdapter:
    def __init__(self, reader: KubernetesListReader, *, clock: Callable[[], datetime] | None = None) -> None:
        self.reader = reader
        self.clock = clock or (lambda: datetime.now(UTC))

    async def enrich(self, stages: list[ScientificStage]) -> tuple[list[ScientificStage], datetime]:
        # One bounded, paginated existing-reader inventory for all stages.
        nodes = await self.reader.list("/api/v1/nodes")
        observed_at = self.clock()
        return [stage.model_copy(update={"placement": stage.placement.model_copy(update={
            "node_upper_bound_fit": project_node_upper_bounds(stage.placement, nodes, observed_at=observed_at),
        })}) if stage.placement is not None else stage for stage in stages], observed_at
