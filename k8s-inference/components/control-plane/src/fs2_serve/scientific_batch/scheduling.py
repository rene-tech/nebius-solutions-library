"""Freeze admission-time decisions from the authoritative Kueue contract."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .models import (
    PreemptionMode,
    ResourceClass,
    SchedulingSnapshot,
    ScientificBatchPlan,
    ServiceClass,
    StageSchedulingDecision,
)

SCHEDULING_SCHEMA = "fs2-serve.nebius.ai/kueue-scheduling/v1"


class SchedulingContractError(RuntimeError):
    """The Terraform/Kueue scheduling contract cannot produce an admission."""


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise SchedulingContractError(f"{label} is not an object")
    return cast(Mapping[str, Any], value)


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise SchedulingContractError("Kueue scheduling contract exceeds the bound")
        value = json.loads(raw)
    except (OSError, RecursionError, ValueError) as error:
        raise SchedulingContractError("Kueue scheduling contract is unavailable or invalid") from error
    return dict(_object(value, "Kueue scheduling contract"))


class SchedulingContractResolver:
    """Resolve queue, priority, pool, flavor, and preemption exactly once."""

    def __init__(self, contract: Mapping[str, Any]) -> None:
        self.contract = dict(contract)
        if self.contract.get("schema") != SCHEDULING_SCHEMA:
            raise SchedulingContractError("Kueue scheduling contract schema is unsupported")
        canonical = json.dumps(self.contract, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.revision = hashlib.sha256(canonical).hexdigest()
        self.service_classes = _object(self.contract.get("service_classes"), "Kueue service classes")
        self.local_queues = _object(self.contract.get("local_queues"), "Kueue local queues")
        self.cluster_queues = _object(self.contract.get("cluster_queues"), "Kueue cluster queues")
        self.priority_classes = _object(
            self.contract.get("workload_priority_classes"), "Kueue workload priority classes"
        )
        self.local_queue_routes = _object(self.contract.get("local_queue_routes"), "Kueue local queue routes")
        self.pools = _object(self.contract.get("pools"), "Kueue accelerator pools")

    @classmethod
    def load(cls, path: Path) -> SchedulingContractResolver:
        return cls(_read(path))

    def freeze(
        self,
        *,
        service_class: str,
        model_id: str,
        tenant_id: str,
        profile: Mapping[str, Any],
        plan: ScientificBatchPlan,
        captured_at: datetime | None = None,
    ) -> SchedulingSnapshot:
        try:
            selected_class = ServiceClass(service_class)
        except ValueError as error:
            raise SchedulingContractError("service class is outside the controller contract") from error
        policy = _object(self.service_classes.get(service_class), "Kueue service class")
        if policy.get("caller_selectable") is not True:
            raise SchedulingContractError("Kueue service class is not caller-selectable")
        local_queue_name = policy.get("default_local_queue")
        priority_name = policy.get("workload_priority_class")
        priority = policy.get("priority")
        pool_preference = policy.get("pool_preference")
        if (
            not isinstance(local_queue_name, str)
            or not isinstance(priority_name, str)
            or not isinstance(priority, int)
            or isinstance(priority, bool)
            or not isinstance(pool_preference, list)
            or not pool_preference
            or not all(isinstance(item, str) for item in pool_preference)
        ):
            raise SchedulingContractError("Kueue service class is incomplete")

        local_queue = _object(self.local_queues.get(local_queue_name), "Kueue LocalQueue")
        local_metadata = _object(local_queue.get("metadata"), "Kueue LocalQueue metadata")
        local_spec = _object(local_queue.get("spec"), "Kueue LocalQueue spec")
        resolved_local = local_metadata.get("name")
        cluster_queue_name = local_spec.get("clusterQueue")
        if resolved_local != local_queue_name or not isinstance(cluster_queue_name, str):
            raise SchedulingContractError("Kueue LocalQueue identity is inconsistent")
        route = _object(self.local_queue_routes.get(local_queue_name), "Kueue LocalQueue route")
        route_models = route.get("model_ids")
        route_tenants = route.get("tenant_ids")
        if (
            route.get("namespace") != local_metadata.get("namespace")
            or route.get("cluster_queue") != cluster_queue_name
            or not isinstance(route_models, list)
            or not all(isinstance(item, str) for item in route_models)
            or not isinstance(route_tenants, list)
            or not all(isinstance(item, str) for item in route_tenants)
        ):
            raise SchedulingContractError("Kueue LocalQueue route is inconsistent")
        if route_models and model_id not in route_models:
            raise SchedulingContractError("model is not routed to the selected Kueue LocalQueue")
        if route_tenants and tenant_id not in route_tenants:
            raise SchedulingContractError("tenant is not routed to the selected Kueue LocalQueue")
        cluster_queue = _object(self.cluster_queues.get(cluster_queue_name), "Kueue ClusterQueue")
        cluster_metadata = _object(cluster_queue.get("metadata"), "Kueue ClusterQueue metadata")
        cluster_spec = _object(cluster_queue.get("spec"), "Kueue ClusterQueue spec")
        if cluster_metadata.get("name") != cluster_queue_name:
            raise SchedulingContractError("Kueue ClusterQueue identity is inconsistent")

        priority_class = _object(self.priority_classes.get(priority_name), "Kueue WorkloadPriorityClass")
        if priority_class.get("value") != priority:
            raise SchedulingContractError("Kueue WorkloadPriorityClass value is inconsistent")

        profile_resources = _object(profile.get("resources"), "scientific profile resources")
        compatible_pools = profile_resources.get("compatible_pool_ids")
        gpu_count = profile_resources.get("gpu_count")
        if (
            not isinstance(compatible_pools, list)
            or not all(isinstance(item, str) for item in compatible_pools)
            or not isinstance(gpu_count, int)
            or isinstance(gpu_count, bool)
        ):
            raise SchedulingContractError("scientific profile resource contract is incomplete")
        resolved_pools = tuple(item for item in pool_preference if item in compatible_pools)
        if not resolved_pools:
            raise SchedulingContractError("profile has no compatible pool in the Kueue service class")
        accelerator_resources: set[str] = set()
        resource_groups = cluster_spec.get("resourceGroups")
        if not isinstance(resource_groups, list):
            raise SchedulingContractError("selected Kueue ClusterQueue has no resource groups")
        for pool_id in resolved_pools:
            pool = _object(self.pools.get(pool_id), f"Kueue pool {pool_id}")
            resource_name = pool.get("accelerator_resource_name")
            resource_flavor = pool.get("resource_flavor")
            if not isinstance(resource_name, str) or "/" not in resource_name or not isinstance(resource_flavor, str):
                raise SchedulingContractError("Kueue pool accelerator mapping is invalid")
            available = any(
                isinstance(raw_group, Mapping)
                and resource_name in raw_group.get("coveredResources", [])
                and any(
                    isinstance(raw_flavor, Mapping) and raw_flavor.get("name") == resource_flavor
                    for raw_flavor in raw_group.get("flavors", [])
                )
                for raw_group in resource_groups
            )
            if not available:
                raise SchedulingContractError("compatible pool is unavailable from the selected ClusterQueue")
            accelerator_resources.add(resource_name)
        if len(accelerator_resources) != 1:
            raise SchedulingContractError("compatible Kueue pools use ambiguous accelerator resources")
        accelerator_resource = next(iter(accelerator_resources))

        max_queue_seconds = policy.get("max_queue_seconds")
        max_execution_seconds = policy.get("max_execution_seconds")
        for value, label in (
            (max_queue_seconds, "maximum queue seconds"),
            (max_execution_seconds, "maximum execution seconds"),
        ):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                raise SchedulingContractError(f"Kueue {label} is invalid")
        try:
            preemption = PreemptionMode(str(policy.get("preemption_mode")))
        except ValueError as error:
            raise SchedulingContractError("Kueue preemption mode is unsupported") from error

        decisions = tuple(
            StageSchedulingDecision(
                stage_id=stage.stage_id,
                resource_class=stage.resource_class,
                resolved_cluster_queue=cluster_queue_name,
                resolved_local_queue=local_queue_name,
                workload_priority_class=priority_name,
                workload_priority_value=priority,
                resolved_pool_preference=(() if stage.resource_class is ResourceClass.CPU else resolved_pools),
                accelerator_resource_name=(None if stage.resource_class is ResourceClass.CPU else accelerator_resource),
                accelerator_count=0 if stage.resource_class is ResourceClass.CPU else gpu_count,
                max_queue_seconds=max_queue_seconds,
                max_execution_seconds=max_execution_seconds,
                checkpoint_mode=stage.checkpoint_mode,
                preemption_mode=preemption,
            )
            for stage in plan.stages
        )
        return SchedulingSnapshot(
            policy_revision=self.revision,
            captured_at=captured_at or datetime.now(UTC),
            service_class=selected_class,
            tenant_queue=local_queue_name,
            model_lane=model_id,
            stages=decisions,
        )
