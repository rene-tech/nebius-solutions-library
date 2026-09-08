"""Customer capacity: GPU reservations are not device utilization or supply guarantees."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import asyncpg
from pydantic import Field

from .admin import AdminReadService
from .admin_adapters import _GPU_RESOURCE, KubernetesCapacityConfig, _capacity_type, _first_label, _pod_gpu_requests
from .admin_models import AdminContext, AdminEnvelope, AdminMeasurement, AdminValueState
from .models import StrictModel


def value(
    number: float | None, unit: str, source: str, reason: str | None = None, *, estimated: bool = False
) -> AdminMeasurement:
    return AdminMeasurement(
        value=number,
        unit=unit,
        source=source,
        reason=reason,
        state=AdminValueState.UNAVAILABLE
        if number is None
        else AdminValueState.ESTIMATED
        if estimated
        else AdminValueState.AVAILABLE,
    )


class CapacityPoolSummary(StrictModel):
    pool_id: str
    gpu_type: str | None
    capacity_type: str
    resource_name: str
    nodes: int
    ready_nodes: int
    not_ready_nodes: int
    cordoned_nodes: int
    total_gpus: AdminMeasurement
    allocated_gpus: AdminMeasurement
    schedulable_free_gpus: AdminMeasurement
    starting_workers: AdminMeasurement
    ready_workers: AdminMeasurement
    configured_min_nodes: int | None = None
    configured_max_nodes: int | None = None
    configured_additional_nodes: AdminMeasurement


class CapacitySummary(StrictModel):
    observed_at: datetime
    pools: list[CapacityPoolSummary]
    gpu_utilization_percent: AdminMeasurement
    loaded_idle_gpus: AdminMeasurement
    pending_customer_runs: AdminMeasurement
    oldest_pending_seconds: AdminMeasurement
    pending_batch_shards: AdminMeasurement
    starting_gpu_workers: AdminMeasurement
    notes: list[str] = Field(default_factory=list)


def ready(item: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in item.get("status", {}).get("conditions", [])
    )


def loaded_idle_gpu_count(pods: list[dict[str, Any]], active_models: set[str]) -> float:
    """Conservative standby count: Ready serving Pods, no logical work for their route.

    Never divide an active shared model among its owners or infer idle from a
    low DCGM sample. Jobs are finite workloads, not loaded reusable workers.
    """
    count = 0.0
    for pod in pods:
        metadata = pod.get("metadata", {})
        if metadata.get("deletionTimestamp") or not ready(pod) or pod.get("status", {}).get("phase") != "Running":
            continue
        if any(owner.get("kind") == "Job" for owner in metadata.get("ownerReferences", [])):
            continue
        labels = metadata.get("labels", {})
        model = labels.get("fs2-serve.nebius.ai/model-id") or labels.get("fs2.nebius.ai/model-id")
        if model and model not in active_models:
            count += sum(
                float(amount)
                for resource, amount in _pod_gpu_requests(pod).items()
                if not resource.startswith("nvidia.com/mig-")
            )
    return count


def project_pools(
    nodes: list[dict[str, Any]],
    pods: list[dict[str, Any]],
    config: KubernetesCapacityConfig,
    contracts: Any = (),
    *,
    complete: bool = True,
) -> list[CapacityPoolSummary]:
    """Node-level subtraction excludes cordoned/unready nodes, unlike pool totals."""
    allocated: dict[tuple[str, str], float] = defaultdict(float)
    workers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pod in pods:
        if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            continue
        node = pod.get("spec", {}).get("nodeName")
        if node:
            for resource, amount in _pod_gpu_requests(pod).items():
                allocated[node, resource] += float(amount)
            workers[node].append(pod)
    grouped: dict[tuple[str, str | None, str, str], list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        labels = node.get("metadata", {}).get("labels", {})
        pool = _first_label(labels, config.pool_label_keys) or "unlabeled"
        gpu = _first_label(labels, config.gpu_class_label_keys)
        resources = node.get("status", {}).get("capacity", {})
        for resource in resources:
            if _GPU_RESOURCE.fullmatch(resource):
                grouped[pool, gpu, str(_capacity_type(labels, config)), resource].append(node)
    result = []
    contract_by_id = {contract.pool_id: contract for contract in contracts}
    observed_pools = set()
    for (pool, gpu, capacity_type, resource), members in grouped.items():
        observed_pools.add(pool)
        total = sum(float(node.get("status", {}).get("capacity", {}).get(resource, 0)) for node in members)
        assigned = sum(allocated[node["metadata"]["name"], resource] for node in members)
        free = sum(
            max(
                0,
                float(node.get("status", {}).get("allocatable", {}).get(resource, 0))
                - allocated[node["metadata"]["name"], resource],
            )
            for node in members
            if ready(node) and not node.get("spec", {}).get("unschedulable", False)
        )
        pod_members = [
            pod
            for node in members
            for pod in workers[node["metadata"]["name"]]
            if resource in _pod_gpu_requests(pod) and not pod.get("metadata", {}).get("deletionTimestamp")
        ]
        contract = contract_by_id.get(pool)
        maximum = getattr(contract, "max_nodes", None)
        minimum = getattr(contract, "min_nodes", None)
        result.append(
            CapacityPoolSummary(
                pool_id=pool,
                gpu_type=gpu,
                capacity_type=capacity_type,
                resource_name=resource,
                nodes=len(members),
                ready_nodes=sum(ready(node) for node in members),
                not_ready_nodes=sum(not ready(node) for node in members),
                cordoned_nodes=sum(bool(node.get("spec", {}).get("unschedulable")) for node in members),
                total_gpus=value(total, "gpus", "kubernetes"),
                allocated_gpus=value(
                    assigned if complete else None,
                    "gpus",
                    "kubernetes",
                    "Scheduled Pod GPU requests in configured workload namespaces; not measured utilization."
                    if complete
                    else "Pod allocation inventory is incomplete.",
                    estimated=complete,
                ),
                schedulable_free_gpus=value(
                    free if complete else None,
                    "gpus",
                    "kubernetes",
                    "Ready, noncordoned allocatable minus workload requests; "
                    "CPU, RAM, taints and model fit still apply."
                    if complete
                    else "Pod allocation inventory is incomplete.",
                    estimated=complete,
                ),
                starting_workers=value(
                    sum(not ready(pod) for pod in pod_members) if complete else None,
                    "workers",
                    "kubernetes",
                    None if complete else "Pod inventory is incomplete.",
                ),
                ready_workers=value(
                    sum(ready(pod) for pod in pod_members) if complete else None,
                    "workers",
                    "kubernetes",
                    None if complete else "Pod inventory is incomplete.",
                ),
                configured_min_nodes=minimum,
                configured_max_nodes=maximum,
                configured_additional_nodes=value(
                    max(0, maximum - len(members)) if maximum is not None else None,
                    "nodes",
                    "configuration",
                    "Configured expansion headroom, not a provider capacity guarantee."
                    if maximum is not None
                    else "No configured node ceiling for this pool.",
                    estimated=maximum is not None,
                ),
            )
        )
    for pool, contract in contract_by_id.items():
        if pool in observed_pools:
            continue
        maximum = contract.max_nodes
        result.append(
            CapacityPoolSummary(
                pool_id=pool,
                gpu_type=getattr(contract, "accelerator_class", None),
                capacity_type=str(getattr(contract, "capacity_type", "unknown")),
                resource_name=getattr(contract, "resource_name", "nvidia.com/gpu"),
                nodes=0,
                ready_nodes=0,
                not_ready_nodes=0,
                cordoned_nodes=0,
                total_gpus=value(0, "gpus", "kubernetes"),
                allocated_gpus=value(
                    0 if complete else None, "gpus", "kubernetes", None if complete else "Pod inventory is incomplete."
                ),
                schedulable_free_gpus=value(0, "gpus", "kubernetes"),
                starting_workers=value(0, "workers", "kubernetes"),
                ready_workers=value(0, "workers", "kubernetes"),
                configured_min_nodes=contract.min_nodes,
                configured_max_nodes=maximum,
                configured_additional_nodes=value(
                    maximum,
                    "nodes",
                    "configuration",
                    "Configured expansion headroom, not a provider capacity guarantee.",
                    estimated=True,
                ),
            )
        )
    return sorted(result, key=lambda row: (row.pool_id, row.resource_name))


class CapacitySummaryService:
    def __init__(
        self,
        admin_read: AdminReadService,
        store: Any,
        *,
        reader: Any = None,
        config: KubernetesCapacityConfig | None = None,
        pools: Any = None,
        prometheus: Any = None,
    ) -> None:
        self.admin_read = admin_read
        self.store = store
        adapter = admin_read.capacity_adapter
        self.reader = reader or getattr(adapter, "reader", None)
        self.config = config or getattr(adapter, "config", None) or KubernetesCapacityConfig()
        self.pools = pools if pools is not None else self.config.node_scaler_pools
        self.prometheus = prometheus

    async def _queue(self, now: datetime) -> tuple[AdminMeasurement, AdminMeasurement]:
        try:
            if hasattr(self.store, "pool"):
                row = await self.store.pool.fetchrow("""
                    SELECT count(*) AS pending,min(o.accepted_at) AS oldest FROM fs2_operations o
                    WHERE o.protocol <> 'scientific-artifact-upload-v1'
                      AND (o.status IN ('queued','activating') OR (o.status='running' AND EXISTS (
                        SELECT 1 FROM fs2_scientific_batches b,
                            LATERAL jsonb_array_elements(b.state->'stages') AS stage_entry(value),
                            LATERAL jsonb_array_elements(stage_entry.value->'attempts') AS attempt_entry(value)
                        WHERE b.operation_id=o.id AND attempt_entry.value->>'outcome'='active'
                          AND attempt_entry.value->>'last_phase' IN ('queued','scheduling','node_pending')
                    )))
                    """)
                count, oldest = row["pending"], row["oldest"]
            else:
                operations = [
                    entry.view
                    for entry in self.store.operations.values()
                    if str(entry.view.status) in {"queued", "activating"}
                    and entry.view.protocol != "scientific-artifact-upload-v1"
                ]
                count = len(operations)
                oldest = min((op.accepted_at for op in operations), default=None)
            return value(float(count), "runs", "postgres"), value(
                max(0, (now - oldest).total_seconds()) if oldest else 0, "seconds", "postgres"
            )
        except (AttributeError, OSError, RuntimeError, ValueError, asyncpg.PostgresError):
            return value(None, "runs", "postgres", "Logical operation queue is unavailable."), value(
                None, "seconds", "postgres", "Logical operation queue is unavailable."
            )

    async def summary(self, context: AdminContext) -> AdminEnvelope[CapacitySummary]:
        original = await self.admin_read.capacity(context)
        now = datetime.now(UTC)
        pools = []
        starting = value(None, "workers", "kubernetes", "GPU Pod inventory is unavailable.")
        idle = value(
            None, "gpus", "kubernetes", "Ready serving Pod and current logical operation inventory are required."
        )
        notes = [
            "Capacity is a current snapshot; usage pages honor the selected historical window.",
            "GPU reservations and measured device utilization are different quantities.",
            "Queued customer runs count logical operations, not polling calls, retries or batch shards.",
            "A partially running scientific batch counts once while a shard waits for admission or a node; "
            "oldest pending age is measured from run acceptance, not the latest stage transition.",
            "Node expansion headroom is configuration, not a promise of available preemptible capacity.",
        ]
        if self.reader:
            try:
                nodes = await self.reader.list("/api/v1/nodes")
                responses = await asyncio.gather(
                    *(
                        self.reader.list(f"/api/v1/namespaces/{namespace}/pods")
                        for namespace in self.config.resolved_queue_namespaces
                    ),
                    return_exceptions=True,
                )
                complete = all(not isinstance(response, BaseException) for response in responses)
                pods = [pod for response in responses if not isinstance(response, BaseException) for pod in response]
                pools = project_pools(nodes, pods, self.config, self.pools, complete=complete)
                if complete:
                    try:
                        if hasattr(self.store, "pool"):
                            rows = await self.store.pool.fetch(
                                "SELECT DISTINCT model_id FROM fs2_operations "
                                "WHERE status IN ('queued','activating','running') "
                                "AND protocol <> 'scientific-artifact-upload-v1'"
                            )
                            active_models = {row["model_id"] for row in rows}
                        else:
                            active_models = {
                                entry.view.model_id
                                for entry in self.store.operations.values()
                                if str(entry.view.status) in {"queued", "activating", "running"}
                                and entry.view.protocol != "scientific-artifact-upload-v1"
                            }
                        idle = value(
                            loaded_idle_gpu_count(pods, active_models),
                            "gpus",
                            "kubernetes",
                            "Ready whole-GPU serving Pods with no pending/running work; "
                            "active models and MIG slices excluded.",
                            estimated=True,
                        )
                    except (AttributeError, OSError, RuntimeError, ValueError):
                        pass
                starting = value(
                    sum(
                        bool(_pod_gpu_requests(pod)) and not ready(pod)
                        for pod in pods
                        if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                        and not pod.get("metadata", {}).get("deletionTimestamp")
                    )
                    if complete
                    else None,
                    "workers",
                    "kubernetes",
                    None if complete else "One or more configured workload namespaces are unavailable.",
                )
            except (OSError, RuntimeError, ValueError):
                notes.append("Live node/Pod inventory is unavailable; no free-capacity count is inferred.")
        utilization = value(None, "percent", "prometheus", "DCGM utilization data is unavailable.")
        if self.prometheus:
            try:
                measured = await self.prometheus.scalar("avg(max by(UUID) (DCGM_FI_DEV_GPU_UTIL))", at=now)
                utilization = value(
                    measured, "percent", "prometheus", None if measured is not None else "No DCGM utilization samples."
                )
            except (OSError, RuntimeError, ValueError, TimeoutError):
                pass
        pending, oldest = await self._queue(now)
        kueue = original.data.kueue
        shard_count = sum(item.workloads.pending.value or 0 for item in kueue.local_queues)
        shards = value(
            shard_count if str(kueue.state) == "available" else None,
            "workloads",
            "kueue",
            "Physical Kueue pending workloads, including scientific shards and online activation; not customer runs."
            if str(kueue.state) == "available"
            else "Kueue inventory is unavailable.",
            estimated=str(kueue.state) == "available",
        )
        data = CapacitySummary(
            observed_at=now,
            pools=pools,
            gpu_utilization_percent=utilization,
            loaded_idle_gpus=idle,
            pending_customer_runs=pending,
            oldest_pending_seconds=oldest,
            pending_batch_shards=shards,
            starting_gpu_workers=starting,
            notes=notes,
        )
        return AdminEnvelope(meta=original.meta, data=data)
