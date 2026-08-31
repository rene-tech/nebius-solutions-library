"""Bounded live adapters for the read-only admin capacity and observability API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

import httpx

from .admin import AdminAdapterUnavailableError, AdminObservabilityQueryTemplates
from .admin_models import (
    AdminAutoscalingProjection,
    AdminCapabilityHealth,
    AdminCapacity,
    AdminCapacitySnapshot,
    AdminCapacityType,
    AdminClusterQueue,
    AdminContext,
    AdminGpuResourceCapacity,
    AdminHorizontalAutoscaler,
    AdminHorizontalAutoscalerInventory,
    AdminKedaScaledObject,
    AdminKedaScaledObjectInventory,
    AdminKueueCohort,
    AdminKueueProjection,
    AdminKueueResourceQuota,
    AdminKueueWorkload,
    AdminKueueWorkloadCounts,
    AdminLocalQueue,
    AdminMeasurement,
    AdminNodeCounts,
    AdminNodePool,
    AdminNodePoolInventory,
    AdminNodeScalerProjection,
    AdminObservability,
    AdminObservabilityComponent,
    AdminObservabilityLaunch,
    AdminObservabilitySignals,
    AdminObservabilitySnapshot,
    AdminQuantity,
    AdminResourceFlavor,
    AdminSourceState,
    AdminValueState,
    AdminWorkloadState,
)

MAX_KUBERNETES_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_KUBERNETES_ITEMS = 4096
MAX_KUBERNETES_PAGES = 16
MAX_PROMETHEUS_RESPONSE_BYTES = 512 * 1024
_NAMESPACE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_GPU_RESOURCE = re.compile(r"^(?:nvidia\.com/(?:gpu|mig-[A-Za-z0-9_.-]+)|amd\.com/gpu|gpu\.intel\.com/(?:i915|xe))$")
_SAFE_COMPONENT = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_REASON = re.compile(r"^[A-Za-z0-9_.:/ -]{1,200}$")
_SAFE_HOST = re.compile(r"^(?:[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\.)*[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


def _available(value: float, unit: str, source: str) -> AdminMeasurement:
    return AdminMeasurement(value=value, unit=unit, state=AdminValueState.AVAILABLE, source=source)


def _estimated(value: float, unit: str, source: str, reason: str) -> AdminMeasurement:
    return AdminMeasurement(
        value=value,
        unit=unit,
        state=AdminValueState.ESTIMATED,
        source=source,
        reason=reason,
    )


def _unavailable(unit: str, source: str, reason: str) -> AdminMeasurement:
    return AdminMeasurement(
        value=None,
        unit=unit,
        state=AdminValueState.UNAVAILABLE,
        source=source,
        reason=reason,
    )


def _quantity(value: object | None, *, source: str, reason: str) -> AdminQuantity:
    if isinstance(value, str | int | float) and str(value):
        return AdminQuantity(value=str(value), state=AdminValueState.AVAILABLE, source=source)
    return AdminQuantity(
        value=None,
        state=AdminValueState.UNAVAILABLE,
        source=source,
        reason=reason,
    )


def _bounded_reason(value: object | None) -> str | None:
    if not isinstance(value, str) or _SAFE_REASON.fullmatch(value) is None:
        return None
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, list) else ()


class KubernetesResourceNotFoundError(AdminAdapterUnavailableError):
    pass


class KubernetesListReader(Protocol):
    async def list(self, path: str) -> list[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class HttpKubernetesListReader:
    """Raw in-cluster reader with immutable call sites and bounded pagination."""

    base_url: str
    token_file: Path
    ca_file: Path
    timeout_seconds: float = 1.5
    page_size: int = 500
    max_items: int = MAX_KUBERNETES_ITEMS
    max_response_bytes: int = MAX_KUBERNETES_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            raise ValueError("Kubernetes API URL must be credential-free HTTPS")
        if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("Kubernetes API URL must be an origin")
        if not 50 <= self.page_size <= 1000 or not 1 <= self.max_items <= MAX_KUBERNETES_ITEMS:
            raise ValueError("Kubernetes list bounds are invalid")
        if not 0.1 <= self.timeout_seconds <= 10:
            raise ValueError("Kubernetes timeout is outside the bound")

    async def list(self, path: str) -> list[Mapping[str, Any]]:
        if not path.startswith("/") or "?" in path or "#" in path or ".." in path:
            raise ValueError("Kubernetes resource path is invalid")
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AdminAdapterUnavailableError("Kubernetes service-account token is unavailable") from exc
        if not 32 <= len(token) <= 16 * 1024:
            raise AdminAdapterUnavailableError("Kubernetes service-account token is invalid")
        headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
        items: list[Mapping[str, Any]] = []
        continuation = ""
        timeout = httpx.Timeout(self.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                verify=str(self.ca_file),
                timeout=timeout,
                trust_env=False,
            ) as client:
                for _ in range(MAX_KUBERNETES_PAGES):
                    params = {"limit": str(self.page_size)}
                    if continuation:
                        params["continue"] = continuation
                    response = await client.get(path, params=params)
                    if response.status_code == 404:
                        raise KubernetesResourceNotFoundError("Kubernetes API resource is not installed")
                    if response.status_code != 200 or len(response.content) > self.max_response_bytes:
                        raise AdminAdapterUnavailableError("Kubernetes API list failed")
                    if "json" not in response.headers.get("content-type", "").lower():
                        raise AdminAdapterUnavailableError("Kubernetes API response is not JSON")
                    value = response.json()
                    body = _mapping(value)
                    page = _sequence(body.get("items"))
                    if any(not isinstance(item, Mapping) for item in page):
                        raise AdminAdapterUnavailableError("Kubernetes API list shape is invalid")
                    items.extend(item for item in page if isinstance(item, Mapping))
                    if len(items) > self.max_items:
                        raise AdminAdapterUnavailableError("Kubernetes API list exceeded its item bound")
                    continuation_value = _mapping(body.get("metadata")).get("continue", "")
                    if not isinstance(continuation_value, str) or len(continuation_value) > 4096:
                        raise AdminAdapterUnavailableError("Kubernetes API continuation token is invalid")
                    continuation = continuation_value
                    if not continuation:
                        return items
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AdminAdapterUnavailableError("Kubernetes API transport failed") from exc
        raise AdminAdapterUnavailableError("Kubernetes API pagination exceeded its bound")


@dataclass(frozen=True)
class KubernetesCapacityConfig:
    model_namespace: str = "fs2-models"
    system_namespace: str = "fs2-system"
    kueue_api_version: str = "v1beta2"
    semantic_pool_label: str = "capacity.fs2.nebius/pool"
    gpu_class_label_keys: tuple[str, ...] = (
        "nvidia.com/gpu.product",
        "nebius.com/gpu-name",
        "gpu.nvidia.com/class",
    )
    capacity_type_label_keys: tuple[str, ...] = (
        "capacity.fs2.nebius/type",
        "karpenter.sh/capacity-type",
        "cloud.google.com/gke-spot",
        "nebius.com/preemptible",
    )

    def __post_init__(self) -> None:
        if _NAMESPACE.fullmatch(self.model_namespace) is None or _NAMESPACE.fullmatch(self.system_namespace) is None:
            raise ValueError("admin Kubernetes namespace is invalid")
        if self.kueue_api_version not in {"v1beta1", "v1beta2"}:
            raise ValueError("unsupported Kueue API version")
        label_keys = (*self.gpu_class_label_keys, *self.capacity_type_label_keys, self.semantic_pool_label)
        if any(not 1 <= len(value) <= 253 for value in label_keys):
            raise ValueError("admin node label key is outside the bound")


def _capacity_type(labels: Mapping[str, object], config: KubernetesCapacityConfig) -> AdminCapacityType:
    for key in config.capacity_type_label_keys:
        raw = labels.get(key)
        if not isinstance(raw, str):
            continue
        value = raw.casefold()
        if value in {"preemptible", "spot", "true"}:
            return AdminCapacityType.PREEMPTIBLE
        if value in {"regular", "on-demand", "ondemand", "false"}:
            return AdminCapacityType.REGULAR
    return AdminCapacityType.UNKNOWN


def _first_label(labels: Mapping[str, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = labels.get(key)
        if isinstance(value, str) and 1 <= len(value) <= 128:
            return value
    return None


def _gpu_count(value: object) -> Decimal:
    if not isinstance(value, str | int):
        raise ValueError("GPU resource quantity is invalid")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("GPU resource quantity is invalid") from exc
    if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
        raise ValueError("GPU resource quantity is not an integer")
    return parsed


def _pod_gpu_requests(pod: Mapping[str, Any]) -> Mapping[str, Decimal]:
    spec = _mapping(pod.get("spec"))

    def resources(container: object) -> dict[str, Decimal]:
        resource_spec = _mapping(_mapping(container).get("resources"))
        requests = _mapping(resource_spec.get("requests"))
        limits = _mapping(resource_spec.get("limits"))
        result: dict[str, Decimal] = {}
        for key in set(requests) | set(limits):
            if _GPU_RESOURCE.fullmatch(str(key)) is None:
                continue
            raw = requests.get(key, limits.get(key))
            result[str(key)] = _gpu_count(raw)
        return result

    regular: defaultdict[str, Decimal] = defaultdict(Decimal)
    for container in _sequence(spec.get("containers")):
        for resource_name, value in resources(container).items():
            regular[resource_name] += value
    initial: defaultdict[str, Decimal] = defaultdict(Decimal)
    for container in _sequence(spec.get("initContainers")):
        for resource_name, value in resources(container).items():
            initial[resource_name] = max(initial[resource_name], value)
    overhead = _mapping(spec.get("overhead"))
    names = set(regular) | set(initial) | {str(key) for key in overhead if _GPU_RESOURCE.fullmatch(str(key))}
    return {
        name: max(regular[name], initial[name]) + (_gpu_count(overhead[name]) if name in overhead else Decimal())
        for name in names
    }


class KubernetesCapacityAdminAdapter:
    def __init__(
        self,
        reader: KubernetesListReader,
        *,
        config: KubernetesCapacityConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.reader = reader
        self.config = config or KubernetesCapacityConfig()
        self.clock = clock or (lambda: datetime.now(UTC))

    async def snapshot(self) -> AdminCapacitySnapshot:
        node_pools, kueue, hpa, keda = await asyncio.gather(
            self._node_pools(),
            self._kueue(),
            self._hpa(),
            self._keda(),
        )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("admin adapter clock must be timezone-aware")
        return AdminCapacitySnapshot(
            observed_at=now.astimezone(UTC),
            data=AdminCapacity(
                node_pools=node_pools,
                kueue=kueue,
                autoscaling=AdminAutoscalingProjection(hpa=hpa, keda=keda),
                node_scaler=AdminNodeScalerProjection(
                    state=AdminSourceState.UNAVAILABLE,
                    configured=None,
                    healthy=None,
                    reason="provider node-scaler adapter is not configured",
                ),
            ),
        )

    async def _node_pools(self) -> AdminNodePoolInventory:
        results = await asyncio.gather(
            self.reader.list("/api/v1/nodes"),
            self.reader.list(f"/api/v1/namespaces/{self.config.model_namespace}/pods"),
            return_exceptions=True,
        )
        nodes_result: object = results[0]
        pods_result: object = results[1]
        if isinstance(nodes_result, BaseException):
            return AdminNodePoolInventory(
                state=AdminSourceState.UNAVAILABLE,
                reason="Kubernetes node inventory is unavailable",
                items=[],
            )
        pods_available = not isinstance(pods_result, BaseException)
        nodes = cast(list[Mapping[str, Any]], nodes_result)
        pods = cast(list[Mapping[str, Any]], pods_result) if isinstance(pods_result, list) else []
        allocation_by_node: defaultdict[tuple[str, str], Decimal] = defaultdict(Decimal)
        allocation_valid = pods_available
        if pods_available:
            try:
                for pod in pods:
                    spec = _mapping(pod.get("spec"))
                    status = _mapping(pod.get("status"))
                    if status.get("phase") in {"Succeeded", "Failed"}:
                        continue
                    node_name = spec.get("nodeName")
                    if not isinstance(node_name, str) or not node_name:
                        continue
                    for resource_name, amount in _pod_gpu_requests(pod).items():
                        allocation_by_node[(node_name, resource_name)] += amount
            except ValueError:
                allocation_valid = False

        grouped: dict[tuple[str | None, str | None, str | None, AdminCapacityType], list[Mapping[str, Any]]] = {}
        for node in nodes:
            metadata = _mapping(node.get("metadata"))
            labels = _mapping(metadata.get("labels"))
            pool_label = _first_label(labels, (self.config.semantic_pool_label,))
            instance_type = _first_label(labels, ("node.kubernetes.io/instance-type",))
            gpu_class = _first_label(labels, self.config.gpu_class_label_keys)
            key = (pool_label, instance_type, gpu_class, _capacity_type(labels, self.config))
            grouped.setdefault(key, []).append(node)

        result: list[AdminNodePool] = []
        for (pool_label, instance_type, gpu_class, capacity_type), nodes in grouped.items():
            identity = json.dumps(
                {
                    "capacity_type": capacity_type.value,
                    "gpu_class": gpu_class,
                    "instance_type": instance_type,
                    "pool_label": pool_label,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            pool_id = "pool-" + hashlib.sha256(identity.encode()).hexdigest()[:12]
            ready = 0
            unschedulable = 0
            capacities: defaultdict[str, Decimal] = defaultdict(Decimal)
            allocatable: defaultdict[str, Decimal] = defaultdict(Decimal)
            allocated: defaultdict[str, Decimal] = defaultdict(Decimal)
            capacity_incomplete: set[str] = set()
            allocatable_incomplete: set[str] = set()
            resource_names: set[str] = set()
            for node in nodes:
                metadata = _mapping(node.get("metadata"))
                node_name = metadata.get("name")
                spec = _mapping(node.get("spec"))
                status = _mapping(node.get("status"))
                if spec.get("unschedulable") is True:
                    unschedulable += 1
                conditions = _sequence(status.get("conditions"))
                if any(
                    _mapping(condition).get("type") == "Ready" and _mapping(condition).get("status") == "True"
                    for condition in conditions
                ):
                    ready += 1
                capacity_values = _mapping(status.get("capacity"))
                allocatable_values = _mapping(status.get("allocatable"))
                for resource_name in set(capacity_values) | set(allocatable_values):
                    name = str(resource_name)
                    if _GPU_RESOURCE.fullmatch(name) is None:
                        continue
                    resource_names.add(name)
                    if resource_name in capacity_values:
                        capacities[name] += _gpu_count(capacity_values[resource_name])
                    else:
                        capacity_incomplete.add(name)
                    if resource_name in allocatable_values:
                        allocatable[name] += _gpu_count(allocatable_values[resource_name])
                    else:
                        allocatable_incomplete.add(name)
                    if isinstance(node_name, str):
                        allocated[name] += allocation_by_node[(node_name, name)]
            gpu_resources = [
                AdminGpuResourceCapacity(
                    resource_name=resource_name,
                    capacity=(
                        _unavailable("gpus", "kubernetes", "node capacity field is incomplete")
                        if resource_name in capacity_incomplete
                        else _available(float(capacities[resource_name]), "gpus", "kubernetes")
                    ),
                    allocatable=(
                        _unavailable("gpus", "kubernetes", "node allocatable field is incomplete")
                        if resource_name in allocatable_incomplete
                        else _available(float(allocatable[resource_name]), "gpus", "kubernetes")
                    ),
                    allocated=(
                        _estimated(
                            float(allocated[resource_name]),
                            "gpus",
                            "kubernetes",
                            "derived from scheduled configured-model-namespace Pod requests; not measured device use",
                        )
                        if allocation_valid
                        else _unavailable("gpus", "kubernetes", "scheduled Pod requests are unavailable")
                    ),
                    healthy=_unavailable(
                        "gpus",
                        "dcgm",
                        "explicit GPU health evidence is unavailable",
                    ),
                )
                for resource_name in sorted(resource_names)
            ]
            result.append(
                AdminNodePool(
                    id=pool_id,
                    pool_label=pool_label,
                    instance_type=instance_type,
                    gpu_class=gpu_class,
                    capacity_type=capacity_type,
                    nodes=AdminNodeCounts(
                        total=_available(float(len(nodes)), "nodes", "kubernetes"),
                        ready=_available(float(ready), "nodes", "kubernetes"),
                        not_ready=_available(float(len(nodes) - ready), "nodes", "kubernetes"),
                        unschedulable=_available(float(unschedulable), "nodes", "kubernetes"),
                    ),
                    gpu_resources=gpu_resources,
                )
            )
        result.sort(key=lambda item: item.id)
        return AdminNodePoolInventory(state=AdminSourceState.AVAILABLE, items=result)

    async def _kueue(self) -> AdminKueueProjection:
        prefix = f"/apis/kueue.x-k8s.io/{self.config.kueue_api_version}"
        paths = (
            f"{prefix}/resourceflavors",
            f"{prefix}/clusterqueues",
            f"{prefix}/namespaces/{self.config.model_namespace}/localqueues",
            f"{prefix}/namespaces/{self.config.model_namespace}/workloads",
            f"{prefix}/cohorts",
        )
        values = await asyncio.gather(*(self.reader.list(path) for path in paths), return_exceptions=True)
        if any(isinstance(value, BaseException) for value in values[:4]):
            return AdminKueueProjection(
                state=AdminSourceState.UNAVAILABLE,
                reason="Kueue API projection is unavailable",
                resource_flavors=[],
                cluster_queues=[],
                local_queues=[],
                cohorts=[],
                cohorts_state=AdminSourceState.UNAVAILABLE,
                cohorts_reason="Kueue Cohort API is unavailable",
                workloads=[],
            )
        flavors_raw, cluster_raw, local_raw, workloads_raw = (value for value in values[:4] if isinstance(value, list))
        cohorts_value = values[4]
        cohorts_raw = cohorts_value if isinstance(cohorts_value, list) else []
        cohorts_state = AdminSourceState.AVAILABLE if isinstance(cohorts_value, list) else AdminSourceState.UNAVAILABLE
        cohorts_reason = None if cohorts_state == AdminSourceState.AVAILABLE else "Kueue Cohort API is unavailable"

        flavors = [self._resource_flavor(item) for item in flavors_raw[:128]]
        cluster_queues = [self._cluster_queue(item) for item in cluster_raw[:128]]
        local_queues = [self._local_queue(item) for item in local_raw[:256]]
        cohorts = [self._cohort(item) for item in cohorts_raw[:128]]
        workload_values = [self._workload(item) for item in workloads_raw[:200]]
        return AdminKueueProjection(
            state=AdminSourceState.AVAILABLE,
            resource_flavors=sorted(flavors, key=lambda item: item.name),
            cluster_queues=sorted(cluster_queues, key=lambda item: item.name),
            local_queues=sorted(local_queues, key=lambda item: (item.namespace, item.name)),
            cohorts=sorted(cohorts, key=lambda item: item.name),
            cohorts_state=cohorts_state,
            cohorts_reason=cohorts_reason,
            workloads=sorted(workload_values, key=lambda item: (item.namespace, item.name)),
            workloads_truncated=len(workloads_raw) > 200,
        )

    def _resource_flavor(self, value: Mapping[str, Any]) -> AdminResourceFlavor:
        metadata = _mapping(value.get("metadata"))
        labels = _mapping(_mapping(value.get("spec")).get("nodeLabels"))
        return AdminResourceFlavor(
            name=str(metadata.get("name", "unknown"))[:253],
            capacity_type=_capacity_type(labels, self.config),
            gpu_class=_first_label(labels, self.config.gpu_class_label_keys),
        )

    @staticmethod
    def _workload_counts(status: Mapping[str, Any]) -> AdminKueueWorkloadCounts:
        def count(field: str) -> AdminMeasurement:
            value = status.get(field)
            return (
                _available(float(value), "workloads", "kueue")
                if isinstance(value, int) and value >= 0
                else _unavailable("workloads", "kueue", f"Kueue status.{field} is absent")
            )

        return AdminKueueWorkloadCounts(
            pending=count("pendingWorkloads"),
            reserving=count("reservingWorkloads"),
            admitted=count("admittedWorkloads"),
        )

    @staticmethod
    def _condition(status: Mapping[str, Any], condition_type: str) -> bool | None:
        for raw in _sequence(status.get("conditions")):
            condition = _mapping(raw)
            if condition.get("type") == condition_type:
                state = condition.get("status")
                return True if state == "True" else (False if state == "False" else None)
        return None

    def _cluster_queue(self, value: Mapping[str, Any]) -> AdminClusterQueue:
        metadata = _mapping(value.get("metadata"))
        spec = _mapping(value.get("spec"))
        status = _mapping(value.get("status"))
        reservation = self._flavor_resource_status(status.get("flavorsReservation"))
        usage = self._flavor_resource_status(status.get("flavorsUsage"))
        resources: list[AdminKueueResourceQuota] = []
        for group in _sequence(spec.get("resourceGroups")):
            for flavor in _sequence(_mapping(group).get("flavors")):
                flavor_value = _mapping(flavor)
                flavor_name = str(flavor_value.get("name", "unknown"))[:253]
                for resource in _sequence(flavor_value.get("resources")):
                    resource_value = _mapping(resource)
                    resource_name = str(resource_value.get("name", "unknown"))[:128]
                    reserved = reservation.get((flavor_name, resource_name), {})
                    used = usage.get((flavor_name, resource_name), {})
                    resources.append(
                        AdminKueueResourceQuota(
                            flavor=flavor_name,
                            resource_name=resource_name,
                            nominal_quota=_quantity(
                                resource_value.get("nominalQuota"),
                                source="kueue",
                                reason="Kueue nominal quota is absent",
                            ),
                            reservation=_quantity(
                                reserved.get("total"),
                                source="kueue",
                                reason="Kueue reservation status is absent",
                            ),
                            usage=_quantity(
                                used.get("total"),
                                source="kueue",
                                reason="Kueue usage status is absent",
                            ),
                            borrowed=_quantity(
                                reserved.get("borrowed"),
                                source="kueue",
                                reason="Kueue borrowed status is absent",
                            ),
                        )
                    )
        cohort = spec.get("cohort")
        return AdminClusterQueue(
            name=str(metadata.get("name", "unknown"))[:253],
            cohort=str(cohort)[:253] if isinstance(cohort, str) and cohort else None,
            queueing_strategy=(
                str(spec["queueingStrategy"])[:64] if isinstance(spec.get("queueingStrategy"), str) else None
            ),
            stop_policy=str(spec["stopPolicy"])[:64] if isinstance(spec.get("stopPolicy"), str) else None,
            active=self._condition(status, "Active"),
            resources=resources[:256],
            workloads=self._workload_counts(status),
        )

    @staticmethod
    def _flavor_resource_status(value: object) -> dict[tuple[str, str], Mapping[str, Any]]:
        result: dict[tuple[str, str], Mapping[str, Any]] = {}
        for flavor in _sequence(value):
            flavor_value = _mapping(flavor)
            flavor_name = str(flavor_value.get("name", ""))
            for resource in _sequence(flavor_value.get("resources")):
                resource_value = _mapping(resource)
                resource_name = str(resource_value.get("name", ""))
                if flavor_name and resource_name:
                    result[(flavor_name, resource_name)] = resource_value
        return result

    def _local_queue(self, value: Mapping[str, Any]) -> AdminLocalQueue:
        metadata = _mapping(value.get("metadata"))
        spec = _mapping(value.get("spec"))
        status = _mapping(value.get("status"))
        return AdminLocalQueue(
            namespace=str(metadata.get("namespace", self.config.model_namespace))[:63],
            name=str(metadata.get("name", "unknown"))[:253],
            cluster_queue=str(spec.get("clusterQueue", "unknown"))[:253],
            stop_policy=str(spec["stopPolicy"])[:64] if isinstance(spec.get("stopPolicy"), str) else None,
            active=self._condition(status, "Active"),
            workloads=self._workload_counts(status),
        )

    @staticmethod
    def _cohort(value: Mapping[str, Any]) -> AdminKueueCohort:
        metadata = _mapping(value.get("metadata"))
        spec = _mapping(value.get("spec"))
        parent = spec.get("parentName")
        return AdminKueueCohort(
            name=str(metadata.get("name", "unknown"))[:253],
            parent=str(parent)[:253] if isinstance(parent, str) and parent else None,
        )

    def _workload(self, value: Mapping[str, Any]) -> AdminKueueWorkload:
        metadata = _mapping(value.get("metadata"))
        spec = _mapping(value.get("spec"))
        status = _mapping(value.get("status"))
        finished = self._condition(status, "Finished")
        admitted = self._condition(status, "Admitted")
        state = (
            AdminWorkloadState.FINISHED
            if finished is True
            else (AdminWorkloadState.ADMITTED if admitted is True else AdminWorkloadState.PENDING)
        )
        created_at: datetime | None = None
        timestamp = metadata.get("creationTimestamp")
        if isinstance(timestamp, str):
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                created_at = parsed if parsed.tzinfo is not None else None
            except ValueError:
                created_at = None
        reason = next(
            (
                _bounded_reason(_mapping(condition).get("reason"))
                for condition in _sequence(status.get("conditions"))
                if _bounded_reason(_mapping(condition).get("reason")) is not None
            ),
            None,
        )
        admission = _mapping(status.get("admission"))
        return AdminKueueWorkload(
            namespace=str(metadata.get("namespace", self.config.model_namespace))[:63],
            name=str(metadata.get("name", "unknown"))[:253],
            local_queue=(
                str(spec["queueName"])[:253]
                if isinstance(spec.get("queueName"), str) and spec.get("queueName")
                else None
            ),
            cluster_queue=(
                str(admission["clusterQueue"])[:253]
                if isinstance(admission.get("clusterQueue"), str) and admission.get("clusterQueue")
                else None
            ),
            state=state,
            created_at=created_at,
            reason=reason,
        )

    async def _hpa(self) -> AdminHorizontalAutoscalerInventory:
        paths = (
            f"/apis/autoscaling/v2/namespaces/{self.config.model_namespace}/horizontalpodautoscalers",
            f"/apis/autoscaling/v2/namespaces/{self.config.system_namespace}/horizontalpodautoscalers",
        )
        values = await asyncio.gather(*(self.reader.list(path) for path in paths), return_exceptions=True)
        if any(isinstance(value, BaseException) for value in values):
            return AdminHorizontalAutoscalerInventory(
                state=AdminSourceState.UNAVAILABLE,
                reason="HorizontalPodAutoscaler API is unavailable",
                horizontal_pod_autoscalers=[],
            )
        items = [self._horizontal_autoscaler(item) for value in values if isinstance(value, list) for item in value]
        items.sort(key=lambda item: (item.namespace, item.name))
        return AdminHorizontalAutoscalerInventory(
            state=AdminSourceState.AVAILABLE,
            horizontal_pod_autoscalers=items[:256],
        )

    def _horizontal_autoscaler(self, value: Mapping[str, Any]) -> AdminHorizontalAutoscaler:
        metadata = _mapping(value.get("metadata"))
        spec = _mapping(value.get("spec"))
        status = _mapping(value.get("status"))
        target = _mapping(spec.get("scaleTargetRef"))

        def replicas(field: str, source: Mapping[str, Any]) -> AdminMeasurement:
            raw = source.get(field)
            return (
                _available(float(raw), "replicas", "kubernetes")
                if isinstance(raw, int) and raw >= 0
                else _unavailable("replicas", "kubernetes", f"HPA {field} is absent")
            )

        return AdminHorizontalAutoscaler(
            namespace=str(metadata.get("namespace", "unknown"))[:63],
            name=str(metadata.get("name", "unknown"))[:253],
            target_kind=str(target.get("kind", "unknown"))[:64],
            target_name=str(target.get("name", "unknown"))[:253],
            min_replicas=replicas("minReplicas", spec),
            max_replicas=replicas("maxReplicas", spec),
            current_replicas=replicas("currentReplicas", status),
            desired_replicas=replicas("desiredReplicas", status),
            able_to_scale=self._condition(status, "AbleToScale"),
            scaling_active=self._condition(status, "ScalingActive"),
            scaling_limited=self._condition(status, "ScalingLimited"),
        )

    async def _keda(self) -> AdminKedaScaledObjectInventory:
        path = f"/apis/keda.sh/v1alpha1/namespaces/{self.config.model_namespace}/scaledobjects"
        try:
            values = await self.reader.list(path)
        except (AdminAdapterUnavailableError, OSError, RuntimeError, ValueError):
            return AdminKedaScaledObjectInventory(
                state=AdminSourceState.UNAVAILABLE,
                reason="KEDA ScaledObject API is unavailable",
                keda_scaled_objects=[],
            )
        items = [self._scaled_object(item) for item in values[:256]]
        items.sort(key=lambda item: (item.namespace, item.name))
        return AdminKedaScaledObjectInventory(
            state=AdminSourceState.AVAILABLE,
            keda_scaled_objects=items,
        )

    def _scaled_object(self, value: Mapping[str, Any]) -> AdminKedaScaledObject:
        metadata = _mapping(value.get("metadata"))
        spec = _mapping(value.get("spec"))
        status = _mapping(value.get("status"))
        target = _mapping(spec.get("scaleTargetRef"))

        def replicas(field: str) -> AdminMeasurement:
            raw = spec.get(field)
            return (
                _available(float(raw), "replicas", "keda")
                if isinstance(raw, int) and raw >= 0
                else _unavailable("replicas", "keda", f"ScaledObject {field} is absent")
            )

        return AdminKedaScaledObject(
            namespace=str(metadata.get("namespace", self.config.model_namespace))[:63],
            name=str(metadata.get("name", "unknown"))[:253],
            target_kind=(str(target["kind"])[:64] if isinstance(target.get("kind"), str) else None),
            target_name=str(target.get("name", "unknown"))[:253],
            min_replicas=replicas("minReplicaCount"),
            max_replicas=replicas("maxReplicaCount"),
            ready=self._condition(status, "Ready"),
            active=self._condition(status, "Active"),
            fallback=self._condition(status, "Fallback"),
            paused=self._condition(status, "Paused"),
        )


class PrometheusScalarReader(Protocol):
    async def scalar(self, query: str, *, at: datetime) -> float | None: ...


@dataclass(frozen=True)
class HttpPrometheusScalarReader:
    """Read one scalar from an aggregate, server-owned PromQL expression."""

    base_url: str
    timeout_seconds: float = 1.5
    max_response_bytes: int = MAX_PROMETHEUS_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
            raise ValueError("Prometheus URL must be a credential-free HTTP(S) origin")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.hostname is None:
            raise ValueError("Prometheus URL must be an origin")
        if parsed.scheme == "http" and not parsed.hostname.endswith((".svc", ".svc.cluster.local")):
            raise ValueError("plain HTTP Prometheus URL must be a cluster Service")
        if not 0.1 <= self.timeout_seconds <= 10:
            raise ValueError("Prometheus timeout is outside the bound")

    async def scalar(self, query: str, *, at: datetime) -> float | None:
        if not 1 <= len(query) <= 4096 or any(value in query.casefold() for value in ("tenant", "principal", "token")):
            raise ValueError("Prometheus query is outside the server-owned bound")
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_seconds),
                trust_env=False,
            ) as client:
                response = await client.post(
                    "/api/v1/query",
                    data={"query": query, "time": at.astimezone(UTC).isoformat()},
                    headers={"accept": "application/json"},
                )
            if response.status_code != 200 or len(response.content) > self.max_response_bytes:
                raise AdminAdapterUnavailableError("Prometheus query failed")
            if "json" not in response.headers.get("content-type", "").lower():
                raise AdminAdapterUnavailableError("Prometheus response is not JSON")
            body = _mapping(response.json())
            if body.get("status") != "success":
                raise AdminAdapterUnavailableError("Prometheus returned an unsuccessful query")
            data = _mapping(body.get("data"))
            result_type = data.get("resultType")
            result = data.get("result")
            sample: object | None = None
            if result_type == "scalar":
                sample = result
            elif result_type == "vector":
                values = _sequence(result)
                if len(values) > 1:
                    raise AdminAdapterUnavailableError("Prometheus aggregate returned too many series")
                if values:
                    sample = _mapping(values[0]).get("value")
            else:
                raise AdminAdapterUnavailableError("Prometheus result type is unsupported")
            if sample is None:
                return None
            pair = _sequence(sample)
            if len(pair) != 2 or not isinstance(pair[1], str):
                raise AdminAdapterUnavailableError("Prometheus sample shape is invalid")
            value = float(pair[1])
            if value < 0 or value != value or value in {float("inf"), float("-inf")}:
                raise AdminAdapterUnavailableError("Prometheus scalar is invalid")
            return value
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            if isinstance(exc, AdminAdapterUnavailableError):
                raise
            raise AdminAdapterUnavailableError("Prometheus query transport failed") from exc


@dataclass(frozen=True)
class ObservabilityLinkConfig:
    component_urls: Mapping[str, str]
    allowed_hosts: frozenset[str]

    def __post_init__(self) -> None:
        if len(self.component_urls) > 16 or len(self.allowed_hosts) > 32:
            raise ValueError("observability link allow-list is outside the bound")
        if self.component_urls and not self.allowed_hosts:
            raise ValueError("configured observability links require an allowed host")
        normalized_hosts = frozenset(host.casefold() for host in self.allowed_hosts)
        if any(not 1 <= len(host) <= 253 or _SAFE_HOST.fullmatch(host) is None for host in normalized_hosts):
            raise ValueError("observability allowed host is invalid")
        object.__setattr__(self, "allowed_hosts", normalized_hosts)
        for component_id, value in self.component_urls.items():
            if _SAFE_COMPONENT.fullmatch(component_id) is None:
                raise ValueError("observability component ID is invalid")
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or parsed.hostname is None
                or parsed.hostname.casefold() not in self.allowed_hosts
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("observability launch URL is not allow-listed HTTPS")

    def contextual_url(
        self,
        component_id: str,
        *,
        project: str | None,
        cluster: str | None,
        region: str | None,
        from_at: datetime,
        to_at: datetime,
        model_id: str | None,
        operation_id: UUID | None,
    ) -> str | None:
        configured = self.component_urls.get(component_id)
        if configured is None:
            return None
        parsed = urlsplit(configured)
        values = {
            "from": from_at.astimezone(UTC).isoformat(),
            "to": to_at.astimezone(UTC).isoformat(),
            "var-project": project,
            "var-cluster": cluster,
            "var-region": region,
            "var-model": model_id,
            "var-operation": str(operation_id) if operation_id is not None else None,
        }
        query = urlencode([(key, value) for key, value in values.items() if value is not None])
        result = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", query, ""))
        if len(result) > 2048:
            raise ValueError("contextual observability URL exceeded its bound")
        return result


@dataclass(frozen=True)
class PrometheusObservabilityConfig:
    links: ObservabilityLinkConfig | None = None
    installed_overrides: Mapping[str, bool] | None = None
    versions: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if "prometheus" in (self.installed_overrides or {}):
            raise ValueError("Prometheus installation state must come from its bounded self-probe")
        for values in (self.installed_overrides or {}, self.versions or {}):
            if len(values) > 32 or any(_SAFE_COMPONENT.fullmatch(key) is None for key in values):
                raise ValueError("observability component configuration is outside the bound")
        if any(not 1 <= len(value) <= 64 for value in (self.versions or {}).values()):
            raise ValueError("observability component version is outside the bound")

    @classmethod
    def from_file(cls, path: Path) -> PrometheusObservabilityConfig:
        raw = path.read_bytes()
        if not 1 <= len(raw) <= 64 * 1024:
            raise ValueError("observability configuration is empty or too large")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("observability configuration is not valid JSON") from exc
        body = _mapping(value)
        if set(body) - {"allowed_hosts", "links", "installed", "versions"}:
            raise ValueError("observability configuration contains an unsupported field")
        links_raw = _mapping(body.get("links"))
        hosts_raw = _sequence(body.get("allowed_hosts"))
        installed_raw = _mapping(body.get("installed"))
        versions_raw = _mapping(body.get("versions"))
        if any(not isinstance(key, str) or not isinstance(item, str) for key, item in links_raw.items()):
            raise ValueError("observability links must be string pairs")
        if any(not isinstance(item, str) or not item or "*" in item for item in hosts_raw):
            raise ValueError("observability allowed hosts are invalid")
        if any(not isinstance(key, str) or not isinstance(item, bool) for key, item in installed_raw.items()):
            raise ValueError("observability installation overrides must be booleans")
        if any(not isinstance(key, str) or not isinstance(item, str) for key, item in versions_raw.items()):
            raise ValueError("observability versions must be strings")
        link_values = cast(dict[str, str], dict(links_raw))
        hosts = frozenset(cast(list[str], list(hosts_raw)))
        return cls(
            links=ObservabilityLinkConfig(component_urls=link_values, allowed_hosts=hosts) if link_values else None,
            installed_overrides=cast(dict[str, bool], dict(installed_raw)),
            versions=cast(dict[str, str], dict(versions_raw)),
        )


_COMPONENT_NAMES = {
    "grafana": "Grafana",
    "prometheus": "Prometheus",
    "loki": "Loki",
    "otel": "OpenTelemetry Collector",
    "dcgm": "NVIDIA DCGM",
    "kueue": "Kueue",
    "keda": "KEDA",
    "alertmanager": "Alertmanager",
    "tempo": "Tempo",
}

_COMPONENT_QUERIES = {
    "grafana": (
        'count(up{job=~".*grafana.*"}) or vector(0)',
        'sum(up{job=~".*grafana.*"} == 1) or vector(0)',
        "count(grafana_build_info) or vector(0)",
    ),
    # This self-series is deliberately queried through the same bounded API as
    # every other probe. Configuration alone never proves Prometheus healthy.
    "prometheus": (
        "count(prometheus_build_info) or vector(0)",
        "count(prometheus_build_info) or vector(0)",
        "count(prometheus_build_info) or vector(0)",
    ),
    "loki": (
        'count(up{job=~".*loki.*"}) or vector(0)',
        'sum(up{job=~".*loki.*"} == 1) or vector(0)',
        "count(loki_build_info) or vector(0)",
    ),
    "otel": (
        'count(up{job=~".*otel.*"}) or vector(0)',
        'sum(up{job=~".*otel.*"} == 1) or vector(0)',
        'count({__name__=~"otelcol_.*"}) or vector(0)',
    ),
    "dcgm": (
        'count(up{job=~".*dcgm.*"}) or vector(0)',
        'sum(up{job=~".*dcgm.*"} == 1) or vector(0)',
        'count({__name__=~"DCGM_FI_.*"}) or vector(0)',
    ),
    "kueue": (
        'count(up{job=~".*kueue.*"}) or vector(0)',
        'sum(up{job=~".*kueue.*"} == 1) or vector(0)',
        'count({__name__=~"kueue_.*"}) or vector(0)',
    ),
    "keda": (
        'count(up{job=~".*keda.*"}) or vector(0)',
        'sum(up{job=~".*keda.*"} == 1) or vector(0)',
        'count({__name__=~"keda_.*"}) or vector(0)',
    ),
    "alertmanager": (
        'count(up{job=~".*alertmanager.*"}) or vector(0)',
        'sum(up{job=~".*alertmanager.*"} == 1) or vector(0)',
        "count(alertmanager_build_info) or vector(0)",
    ),
    "tempo": (
        'count(up{job=~".*tempo.*"}) or vector(0)',
        'sum(up{job=~".*tempo.*"} == 1) or vector(0)',
        "count(tempo_build_info) or vector(0)",
    ),
}


class PrometheusObservabilityAdminAdapter:
    def __init__(
        self,
        reader: PrometheusScalarReader,
        *,
        config: PrometheusObservabilityConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.reader = reader
        self.config = config or PrometheusObservabilityConfig()
        self.clock = clock or (lambda: datetime.now(UTC))

    async def snapshot(
        self,
        *,
        context: AdminContext,
        model_id: str | None,
        operation_id: UUID | None,
    ) -> AdminObservabilitySnapshot:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("admin adapter clock must be timezone-aware")
        now = now.astimezone(UTC)
        window = max(60, min(int((context.to_at - context.from_at).total_seconds()), 31 * 24 * 60 * 60))
        component_ids = tuple(_COMPONENT_QUERIES)
        component_tasks = [self._component(component_id, at=now) for component_id in component_ids]
        signal_queries = AdminObservabilityQueryTemplates.for_window(window)
        signal_names = tuple(signal_queries)
        signal_values = await asyncio.gather(
            *component_tasks,
            *(self.reader.scalar(signal_queries[name], at=now) for name in signal_names),
            return_exceptions=True,
        )
        components = [
            value
            if isinstance(value, AdminObservabilityComponent)
            else self._unknown_component(component_id, "component probe is unavailable")
            for component_id, value in zip(component_ids, signal_values[: len(component_ids)], strict=True)
        ]
        components = [
            self._with_launch(
                component,
                project=context.project,
                cluster=context.cluster,
                region=context.region,
                from_at=context.from_at,
                to_at=context.to_at,
                model_id=model_id,
                operation_id=operation_id,
            )
            for component in components
        ]
        raw_signals = signal_values[len(component_ids) :]
        signal_map = {
            name: (value if isinstance(value, float | int) else None)
            for name, value in zip(signal_names, raw_signals, strict=True)
        }
        dcgm = next(component for component in components if component.id == "dcgm")
        otel = next(component for component in components if component.id == "otel")

        def signal(
            name: str,
            *,
            unit: str,
            source: str,
            data_present: bool | None,
        ) -> AdminMeasurement:
            value = signal_map[name]
            if data_present is True and value is not None:
                return _available(float(value), unit, source)
            return _unavailable(unit, source, "accepted metric series is unavailable")

        return AdminObservabilitySnapshot(
            observed_at=now,
            data=AdminObservability(
                components=components,
                signals=AdminObservabilitySignals(
                    gpu_utilization_ratio=signal(
                        "gpu_utilization_ratio",
                        unit="ratio",
                        source="dcgm",
                        data_present=dcgm.data_present,
                    ),
                    gpu_memory_utilization_ratio=signal(
                        "gpu_memory_utilization_ratio",
                        unit="ratio",
                        source="dcgm",
                        data_present=dcgm.data_present,
                    ),
                    otel_refused_items_per_second=signal(
                        "otel_refused_items_per_second",
                        unit="items/second",
                        source="prometheus",
                        data_present=otel.data_present,
                    ),
                    otel_export_failures_per_second=signal(
                        "otel_export_failures_per_second",
                        unit="items/second",
                        source="prometheus",
                        data_present=otel.data_present,
                    ),
                ),
            ),
        )

    async def _component(self, component_id: str, *, at: datetime) -> AdminObservabilityComponent:
        target_query, healthy_query, data_query = _COMPONENT_QUERIES[component_id]
        target_count, healthy_count, data_count = await asyncio.gather(
            self.reader.scalar(target_query, at=at),
            self.reader.scalar(healthy_query, at=at),
            self.reader.scalar(data_query, at=at),
        )
        override = (self.config.installed_overrides or {}).get(component_id)
        installed = override if override is not None else (True if target_count and target_count > 0 else None)
        if installed is False:
            health = AdminCapabilityHealth.UNKNOWN
            reason = "component is configured as not installed"
        elif target_count is None or target_count <= 0 or healthy_count is None:
            health = AdminCapabilityHealth.UNKNOWN
            reason = "component has no accepted Prometheus target"
        elif healthy_count == target_count:
            health = AdminCapabilityHealth.HEALTHY
            reason = None
        elif healthy_count > 0:
            health = AdminCapabilityHealth.DEGRADED
            reason = "one or more component targets are unhealthy"
        else:
            health = AdminCapabilityHealth.UNHEALTHY
            reason = "all component targets are unhealthy"
        data_present = None if data_count is None else data_count > 0
        return AdminObservabilityComponent(
            id=component_id,
            display_name=_COMPONENT_NAMES[component_id],
            installed=installed,
            health=health,
            data_present=data_present,
            launch=AdminObservabilityLaunch(enabled=False, reason="launch evaluation is pending"),
            version=(self.config.versions or {}).get(component_id),
            observed_at=at,
            reason=reason,
        )

    def _unknown_component(self, component_id: str, reason: str) -> AdminObservabilityComponent:
        override = (self.config.installed_overrides or {}).get(component_id)
        return AdminObservabilityComponent(
            id=component_id,
            display_name=_COMPONENT_NAMES[component_id],
            installed=override,
            health=AdminCapabilityHealth.UNKNOWN,
            data_present=None,
            launch=AdminObservabilityLaunch(enabled=False, reason=reason),
            version=(self.config.versions or {}).get(component_id),
            reason=reason,
        )

    def _with_launch(
        self,
        component: AdminObservabilityComponent,
        *,
        project: str | None,
        cluster: str | None,
        region: str | None,
        from_at: datetime,
        to_at: datetime,
        model_id: str | None,
        operation_id: UUID | None,
    ) -> AdminObservabilityComponent:
        configured = None
        if self.config.links is not None:
            configured = self.config.links.contextual_url(
                component.id,
                project=project,
                cluster=cluster,
                region=region,
                from_at=from_at,
                to_at=to_at,
                model_id=model_id,
                operation_id=operation_id,
            )
        launchable = (
            component.installed is True
            and component.health == AdminCapabilityHealth.HEALTHY
            and component.data_present is True
        )
        if configured is not None and launchable:
            launch = AdminObservabilityLaunch(enabled=True, url=configured)
        elif configured is None:
            launch = AdminObservabilityLaunch(enabled=False, reason="operator UI is not configured")
        else:
            launch = AdminObservabilityLaunch(enabled=False, reason="component health or data probe did not pass")
        return component.model_copy(update={"launch": launch})
