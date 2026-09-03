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
    StagePlacementClass,
    StageSchedulingDecision,
    StageToleration,
)

SCHEDULING_SCHEMA = "fs2-serve.nebius.ai/kueue-scheduling/v1"


class SchedulingContractError(RuntimeError):
    """The Terraform/Kueue scheduling contract cannot produce an admission."""


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise SchedulingContractError(f"{label} is not an object")
    return cast(Mapping[str, Any], value)


def _read(path: Path, *, expected_sha256: str | None = None) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise SchedulingContractError("Kueue scheduling contract exceeds the bound")
        observed_sha256 = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and observed_sha256 != expected_sha256:
            raise SchedulingContractError("Kueue scheduling contract raw bytes differ from Terraform")
        value = json.loads(raw)
    except (OSError, RecursionError, ValueError) as error:
        raise SchedulingContractError("Kueue scheduling contract is unavailable or invalid") from error
    return dict(_object(value, "Kueue scheduling contract")), f"sha256:{observed_sha256}"


class SchedulingContractResolver:
    """Resolve queue, priority, pool, flavor, and preemption exactly once."""

    def __init__(self, contract: Mapping[str, Any], *, raw_contract_sha256: str | None = None) -> None:
        self.contract = dict(contract)
        if self.contract.get("schema") != SCHEDULING_SCHEMA:
            raise SchedulingContractError("Kueue scheduling contract schema is unsupported")
        canonical = json.dumps(self.contract, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.revision = hashlib.sha256(canonical).hexdigest()
        self.raw_contract_sha256 = raw_contract_sha256 or f"sha256:{hashlib.sha256(canonical).hexdigest()}"
        self.service_classes = _object(self.contract.get("service_classes"), "Kueue service classes")
        self.local_queues = _object(self.contract.get("local_queues"), "Kueue local queues")
        self.cluster_queues = _object(self.contract.get("cluster_queues"), "Kueue cluster queues")
        self.priority_classes = _object(
            self.contract.get("workload_priority_classes"), "Kueue workload priority classes"
        )
        self.local_queue_routes = _object(self.contract.get("local_queue_routes"), "Kueue local queue routes")
        self.pools = _object(self.contract.get("pools"), "Kueue accelerator pools")
        self.cpu_classes = _object(self.contract.get("cpu_classes"), "Kueue CPU placement classes")
        self.cpu_stage_requests = _object(self.contract.get("cpu_stage_requests"), "Kueue CPU stage requests")
        self.namespace_bound_models = _object(
            self.contract.get("namespace_bound_models"), "Kueue namespace-bound models"
        )
        if not all(
            isinstance(model_id, str) and isinstance(namespace, str)
            for model_id, namespace in self.namespace_bound_models.items()
        ):
            raise SchedulingContractError("Kueue namespace-bound model mapping is invalid")
        if self.contract.get("pool_node_label_key") != "accelerator.fs2.nebius/pool-id":
            raise SchedulingContractError("Kueue contract uses a non-canonical accelerator pool label")

    @classmethod
    def load(cls, path: Path, *, expected_sha256: str | None = None) -> SchedulingContractResolver:
        contract, digest = _read(path, expected_sha256=expected_sha256)
        return cls(contract, raw_contract_sha256=digest)

    def freeze(
        self,
        *,
        service_class: str,
        model_id: str,
        tenant_id: str,
        profile: Mapping[str, Any],
        plan: ScientificBatchPlan,
        workload_namespace: str | None = None,
        captured_at: datetime | None = None,
    ) -> SchedulingSnapshot:
        try:
            selected_class = ServiceClass(service_class)
        except ValueError as error:
            raise SchedulingContractError("service class is outside the controller contract") from error
        policy = _object(self.service_classes.get(service_class), "Kueue service class")
        if policy.get("caller_selectable") is not True:
            raise SchedulingContractError("Kueue service class is not caller-selectable")
        priority_name = policy.get("workload_priority_class")
        priority = policy.get("priority")
        pool_preference = policy.get("pool_preference")
        if (
            not isinstance(policy.get("default_local_queue"), str)
            or not isinstance(priority_name, str)
            or not isinstance(priority, int)
            or isinstance(priority, bool)
            or not isinstance(pool_preference, list)
            or not pool_preference
            or not all(isinstance(item, str) for item in pool_preference)
        ):
            raise SchedulingContractError("Kueue service class is incomplete")

        priority_class = _object(self.priority_classes.get(priority_name), "Kueue WorkloadPriorityClass")
        if priority_class.get("value") != priority:
            raise SchedulingContractError("Kueue WorkloadPriorityClass value is inconsistent")

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
        workload = _object(profile.get("workload"), "scientific profile workload")
        raw_stages = workload.get("stages")
        if not isinstance(raw_stages, list):
            raise SchedulingContractError("scientific profile stages are invalid")
        profile_stages = {
            item.get("id"): item for item in raw_stages if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
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

        decisions: list[StageSchedulingDecision] = []
        for stage in plan.stages:
            raw_stage = _object(profile_stages.get(stage.stage_id), "scientific profile stage")
            raw_placement = raw_stage.get("placement")
            placement = None if raw_placement is None else _object(raw_placement, "scientific stage placement")
            desired_queue = None if placement is None else placement.get("local_queue")
            if desired_queue is not None and not isinstance(desired_queue, str):
                raise SchedulingContractError("scientific stage LocalQueue is invalid")
            placement_class = stage.placement_class
            cpu_class: Mapping[str, Any] | None = None
            if stage.resource_class is ResourceClass.CPU:
                placement_class = placement_class or StagePlacementClass.GENERAL_CPU
                cpu_class = _object(self.cpu_classes.get(placement_class.value), "Kueue CPU placement class")
                cpu_queue = cpu_class.get("local_queue")
                if not isinstance(cpu_queue, str):
                    raise SchedulingContractError("Kueue CPU placement class has no LocalQueue")
                if desired_queue is not None and desired_queue != cpu_queue:
                    raise SchedulingContractError("profile stage LocalQueue differs from the Kueue CPU class")
                local_queue_name, route_namespace, cluster_queue_name = self._route_identity(cpu_queue)
            else:
                local_queue_name, route_namespace, cluster_queue_name = self._resolve_route(
                    service_class=service_class,
                    model_id=model_id,
                    tenant_id=tenant_id,
                    default_local_queue=cast(str, policy["default_local_queue"]),
                    desired_local_queue=desired_queue,
                )
            resolved_namespace = workload_namespace or route_namespace
            if placement is not None and placement.get("namespace") != route_namespace:
                raise SchedulingContractError("profile stage namespace differs from the routed LocalQueue")
            if resolved_namespace != route_namespace:
                raise SchedulingContractError(
                    "scientific execution namespace differs from the routed Kueue LocalQueue namespace"
                )
            if placement is not None and placement.get("cluster_queue") != cluster_queue_name:
                raise SchedulingContractError("profile stage ClusterQueue differs from the routed LocalQueue")

            requested_flavor = None if placement is None else placement.get("resource_flavor")
            if requested_flavor is not None and not isinstance(requested_flavor, str):
                raise SchedulingContractError("profile stage ResourceFlavor is invalid")
            node_selector: tuple[tuple[str, str], ...] = ()
            tolerations: tuple[StageToleration, ...] = ()
            resolved_pools: tuple[str, ...] = ()
            accelerator_resource: str | None = None
            accelerator_count = 0
            if stage.resource_class is ResourceClass.CPU:
                assert cpu_class is not None
                if (
                    cpu_class.get("local_queue") != local_queue_name
                    or cpu_class.get("cluster_queue") != cluster_queue_name
                    or cpu_class.get("namespace") != route_namespace
                ):
                    raise SchedulingContractError("CPU stage placement differs from the Kueue CPU class")
                raw_selector = _object(cpu_class.get("node_selector"), "CPU class node selector")
                if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw_selector.items()):
                    raise SchedulingContractError("CPU class node selector is invalid")
                node_selector = tuple(sorted(cast(Mapping[str, str], raw_selector).items()))
                raw_tolerations = cpu_class.get("tolerations")
                if not isinstance(raw_tolerations, list):
                    raise SchedulingContractError("CPU class tolerations are invalid")
                tolerations = tuple(
                    StageToleration(
                        key=str(item.get("key")),
                        operator=str(item.get("operator")),
                        value=cast(str | None, item.get("value")),
                        effect=str(item.get("effect")),
                    )
                    for item in raw_tolerations
                    if isinstance(item, Mapping)
                )
                if len(tolerations) != len(raw_tolerations):
                    raise SchedulingContractError("CPU class tolerations are invalid")
                resources = stage.resources
                if resources is None:
                    raise SchedulingContractError("CPU stage has no frozen resource envelope")
                capacity = _object(cpu_class.get("schedulable_capacity"), "CPU class schedulable capacity")
                capacity_cpu = capacity.get("cpu_millicores")
                capacity_memory = capacity.get("memory_mib")
                capacity_ephemeral = capacity.get("ephemeral_storage_mib")
                if any(
                    not isinstance(value, int) or isinstance(value, bool) or value < 1
                    for value in (capacity_cpu, capacity_memory, capacity_ephemeral)
                ):
                    raise SchedulingContractError("CPU class schedulable capacity is invalid")
                capacity_cpu = cast(int, capacity_cpu)
                capacity_memory = cast(int, capacity_memory)
                capacity_ephemeral = cast(int, capacity_ephemeral)
                requested_memory_mib = (resources.memory_bytes + 1024**2 - 1) // 1024**2
                requested_ephemeral_mib = (resources.ephemeral_storage_bytes + 1024**2 - 1) // 1024**2
                if (
                    resources.cpu_millis > capacity_cpu
                    or requested_memory_mib > capacity_memory
                    or requested_ephemeral_mib > capacity_ephemeral
                ):
                    raise SchedulingContractError("CPU stage resource envelope exceeds its placement class")
                assert placement_class is not None
                declared_request = self.cpu_stage_requests.get(placement_class.value)
                if declared_request is not None:
                    request = _object(declared_request, "Kueue CPU stage request")
                    request_cpu = request.get("cpu_millicores")
                    request_memory = request.get("memory_mib")
                    if (
                        not isinstance(request_cpu, int)
                        or isinstance(request_cpu, bool)
                        or not isinstance(request_memory, int)
                        or isinstance(request_memory, bool)
                        or resources.cpu_millis > request_cpu
                        or requested_memory_mib > request_memory
                    ):
                        raise SchedulingContractError("CPU stage resource envelope exceeds its declared request")
            else:
                placement_class = placement_class or StagePlacementClass.ACCELERATOR
                stage_accelerator = (
                    None if placement is None else _object(placement.get("accelerator"), "scientific stage accelerator")
                )
                stage_pools = compatible_pools if stage_accelerator is None else stage_accelerator.get("pool_ids")
                stage_gpu_count = gpu_count if stage_accelerator is None else stage_accelerator.get("count")
                if not isinstance(stage_pools, list) or not all(isinstance(item, str) for item in stage_pools):
                    raise SchedulingContractError("scientific stage accelerator pools are invalid")
                if not isinstance(stage_gpu_count, int) or isinstance(stage_gpu_count, bool) or stage_gpu_count < 1:
                    raise SchedulingContractError("scientific stage accelerator count is invalid")
                resolved_pools = tuple(
                    item for item in pool_preference if item in stage_pools and item in compatible_pools
                )
                if not resolved_pools:
                    raise SchedulingContractError("profile stage has no compatible pool in the Kueue service class")
                accelerator_resources: set[str] = set()
                flavors: set[str] = set()
                for pool_id in resolved_pools:
                    pool = _object(self.pools.get(pool_id), f"Kueue pool {pool_id}")
                    resource_name = pool.get("accelerator_resource_name")
                    flavor = pool.get("resource_flavor")
                    if not isinstance(resource_name, str) or "/" not in resource_name or not isinstance(flavor, str):
                        raise SchedulingContractError("Kueue pool accelerator mapping is invalid")
                    accelerator_resources.add(resource_name)
                    flavors.add(flavor)
                if len(accelerator_resources) != 1:
                    raise SchedulingContractError("compatible Kueue pools use ambiguous accelerator resources")
                if requested_flavor is not None and requested_flavor not in flavors:
                    raise SchedulingContractError("profile stage ResourceFlavor is outside its compatible pools")
                accelerator_resource = next(iter(accelerator_resources))
                if stage_accelerator is not None and stage_accelerator.get("resource_name") != accelerator_resource:
                    raise SchedulingContractError("profile stage accelerator resource differs from Kueue")
                accelerator_count = stage_gpu_count

            decisions.append(
                StageSchedulingDecision(
                    stage_id=stage.stage_id,
                    resource_class=stage.resource_class,
                    resolved_cluster_queue=cluster_queue_name,
                    resolved_local_queue=local_queue_name,
                    workload_priority_class=priority_name,
                    workload_priority_value=priority,
                    resolved_pool_preference=resolved_pools,
                    accelerator_resource_name=accelerator_resource,
                    accelerator_count=accelerator_count,
                    max_queue_seconds=max_queue_seconds,
                    max_execution_seconds=max_execution_seconds,
                    checkpoint_mode=stage.checkpoint_mode,
                    preemption_mode=preemption,
                    placement_class=placement_class,
                    workload_namespace=resolved_namespace,
                    route_namespace=route_namespace,
                    requested_resource_flavor=requested_flavor,
                    node_selector=node_selector,
                    tolerations=tolerations,
                )
            )
        namespaces = {item.workload_namespace for item in decisions}
        if len(namespaces) != 1:
            raise SchedulingContractError("one scientific run cannot span execution namespaces")
        frozen_namespace = cast(str, next(iter(namespaces)))
        bound_namespace = self.namespace_bound_models.get(model_id)
        if bound_namespace is not None and frozen_namespace != bound_namespace:
            raise SchedulingContractError("namespace-bound model resolved outside its licensed asset namespace")
        return SchedulingSnapshot(
            policy_revision=self.revision,
            captured_at=captured_at or datetime.now(UTC),
            service_class=selected_class,
            tenant_queue=decisions[-1].resolved_local_queue,
            model_lane=model_id,
            workload_namespace=frozen_namespace,
            route_namespace=frozen_namespace,
            stages=tuple(decisions),
            raw_contract_sha256=self.raw_contract_sha256,
        )

    def _resolve_route(
        self,
        *,
        service_class: str,
        model_id: str,
        tenant_id: str,
        default_local_queue: str,
        desired_local_queue: str | None,
    ) -> tuple[str, str, str]:
        ranked: dict[int, list[str]] = {}
        for queue_name, raw_route in self.local_queue_routes.items():
            if not isinstance(queue_name, str):
                raise SchedulingContractError("Kueue LocalQueue route identity is invalid")
            route = _object(raw_route, "Kueue LocalQueue route")
            selectors: list[list[str]] = []
            for key in ("model_ids", "tenant_ids", "service_classes"):
                values = route.get(key)
                if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                    raise SchedulingContractError("Kueue LocalQueue route selectors are invalid")
                selectors.append(values)
            models, tenants, classes = selectors
            if desired_local_queue is not None and queue_name != desired_local_queue:
                continue
            if (
                (models and model_id not in models)
                or (tenants and tenant_id not in tenants)
                or (classes and service_class not in classes)
            ):
                continue
            specificity = sum(bool(values) for values in (models, tenants, classes))
            if specificity:
                ranked.setdefault(specificity, []).append(queue_name)
        selected = ranked[max(ranked)] if ranked else []
        if len(selected) > 1:
            raise SchedulingContractError("model, tenant, and service class resolve to multiple Kueue LocalQueues")
        if selected:
            local_queue_name = selected[0]
        else:
            if desired_local_queue is not None and desired_local_queue != default_local_queue:
                raise SchedulingContractError("profile stage has no matching Kueue LocalQueue route")
            default_route = _object(self.local_queue_routes.get(default_local_queue), "default Kueue LocalQueue route")
            if any(default_route.get(key) != [] for key in ("model_ids", "tenant_ids", "service_classes")):
                raise SchedulingContractError("Kueue fallback LocalQueue is not explicitly unrestricted")
            local_queue_name = default_local_queue
        return self._route_identity(local_queue_name)

    def _route_identity(self, local_queue_name: str) -> tuple[str, str, str]:
        local_queue = _object(self.local_queues.get(local_queue_name), "Kueue LocalQueue")
        metadata = _object(local_queue.get("metadata"), "Kueue LocalQueue metadata")
        spec = _object(local_queue.get("spec"), "Kueue LocalQueue spec")
        route = _object(self.local_queue_routes.get(local_queue_name), "Kueue LocalQueue route")
        namespace = route.get("namespace")
        cluster_queue = route.get("cluster_queue")
        if (
            metadata.get("name") != local_queue_name
            or metadata.get("namespace") != namespace
            or spec.get("clusterQueue") != cluster_queue
            or not isinstance(namespace, str)
            or not isinstance(cluster_queue, str)
        ):
            raise SchedulingContractError("Kueue LocalQueue route is inconsistent")
        return local_queue_name, namespace, cluster_queue
