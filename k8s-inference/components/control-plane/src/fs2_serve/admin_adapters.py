"""Bounded live adapters for the read-only admin capacity and observability API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

import httpx

from .admin import AdminAdapterUnavailableError, AdminObservabilityQueryTemplates, PrometheusQueryTemplates
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
    AdminKubernetesModel,
    AdminKubernetesSnapshot,
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
    AdminPrometheusModel,
    AdminPrometheusSnapshot,
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
MAX_NODE_SCALER_STATUS_BYTES = 512 * 1024
NODE_SCALER_STATUS_MAX_AGE = timedelta(minutes=5)
NODE_SCALER_STATUS_PATH = "/api/v1/namespaces/kube-system/configmaps/cluster-autoscaler-status"
NODE_SCALER_HEALTH_STATUSES = frozenset({"Healthy", "Unhealthy"})
NODE_SCALER_HEALTH_STATUS_MAX_LENGTH = 32
_NAMESPACE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_GPU_RESOURCE = re.compile(r"^(?:nvidia\.com/(?:gpu|mig-[A-Za-z0-9_.-]+)|amd\.com/gpu|gpu\.intel\.com/(?:i915|xe))$")
_SAFE_COMPONENT = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_REASON = re.compile(r"^[A-Za-z0-9_.:/ -]{1,200}$")
_SAFE_HOST = re.compile(r"^(?:[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\.)*[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_MODEL_LABEL_KEYS = (
    "fs2-serve.nebius.ai/model-id",
    "fs2.nebius.ai/model-id",
    "app.kubernetes.io/name",
)
_POD_FAILURE_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "CreateContainerConfigError",
        "CreateContainerError",
        "ErrImagePull",
        "ImagePullBackOff",
        "InvalidImageName",
        "RunContainerError",
    }
)


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

    async def get(self, path: str) -> Mapping[str, Any]: ...


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

    def _headers(self) -> dict[str, str]:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AdminAdapterUnavailableError("Kubernetes service-account token is unavailable") from exc
        if not 32 <= len(token) <= 16 * 1024:
            raise AdminAdapterUnavailableError("Kubernetes service-account token is invalid")
        return {"authorization": f"Bearer {token}", "accept": "application/json"}

    @staticmethod
    def _validate_path(path: str) -> None:
        if not path.startswith("/") or "?" in path or "#" in path or ".." in path:
            raise ValueError("Kubernetes resource path is invalid")

    async def get(self, path: str) -> Mapping[str, Any]:
        self._validate_path(path)
        timeout = httpx.Timeout(self.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._headers(),
                verify=str(self.ca_file),
                timeout=timeout,
                trust_env=False,
            ) as client:
                response = await client.get(path)
                if response.status_code == 404:
                    raise KubernetesResourceNotFoundError("Kubernetes API resource is not installed")
                if response.status_code != 200 or len(response.content) > self.max_response_bytes:
                    raise AdminAdapterUnavailableError("Kubernetes API get failed")
                if "json" not in response.headers.get("content-type", "").lower():
                    raise AdminAdapterUnavailableError("Kubernetes API response is not JSON")
                value = response.json()
                if not isinstance(value, Mapping):
                    raise AdminAdapterUnavailableError("Kubernetes API object shape is invalid")
                return value
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AdminAdapterUnavailableError("Kubernetes API transport failed") from exc

    async def list(self, path: str) -> list[Mapping[str, Any]]:
        self._validate_path(path)
        items: list[Mapping[str, Any]] = []
        continuation = ""
        timeout = httpx.Timeout(self.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=self._headers(),
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
class ManagedNodeScalerPoolContract:
    """Exact bounded pool limits from the mounted admin configuration."""

    pool_id: str
    min_nodes: int
    max_nodes: int

    def __post_init__(self) -> None:
        if not 1 <= len(self.pool_id) <= 128:
            raise ValueError("managed node-scaler pool identifier is outside the bound")
        if (
            isinstance(self.min_nodes, bool)
            or isinstance(self.max_nodes, bool)
            or not isinstance(self.min_nodes, int)
            or not isinstance(self.max_nodes, int)
            or not 0 <= self.min_nodes <= self.max_nodes <= 10000
        ):
            raise ValueError("managed node-scaler pool bounds are invalid")


@dataclass(frozen=True)
class ManagedNodeScalerProbe:
    """Bounded evidence published by the managed cluster autoscaler."""

    state: AdminSourceState
    healthy: bool | None
    observed_at: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True)
class KubernetesCapacityConfig:
    model_namespace: str = "fs2-models"
    system_namespace: str = "fs2-system"
    # Kueue LocalQueues and Workloads are namespaced. Scientific lanes such as
    # fs2-academic-poc and fs2-reference-data live outside the model namespace,
    # so the queue projection covers this whole set and reports which
    # namespaces it read. Listing only the model namespace silently hides them.
    queue_namespaces: tuple[str, ...] = ()
    kueue_api_version: str = "v1beta2"
    semantic_pool_label: str = "capacity.fs2.nebius/pool"
    pool_label_fallbacks: tuple[str, ...] = ("accelerator.fs2.nebius/pool-id",)
    # The scheduler renders the stable accelerator class into
    # ResourceFlavor.spec.nodeLabels and onto the node itself; the provider keys
    # follow it for clusters that publish their own vocabulary.
    gpu_class_label_keys: tuple[str, ...] = (
        "accelerator.fs2.nebius/class",
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
    # A ResourceFlavor carries its capacity type as an annotation, because
    # Kueue bounds spec.nodeLabels to eight entries and the flavor spends them
    # on the accelerator class and pool identity.
    capacity_type_annotation_keys: tuple[str, ...] = ("fs2-serve.nebius.ai/capacity-type",)
    node_scaler_provider: str | None = None
    node_scaler_pools: tuple[ManagedNodeScalerPoolContract, ...] = ()

    def __post_init__(self) -> None:
        if _NAMESPACE.fullmatch(self.model_namespace) is None or _NAMESPACE.fullmatch(self.system_namespace) is None:
            raise ValueError("admin Kubernetes namespace is invalid")
        if len(self.queue_namespaces) > 32:
            raise ValueError("admin Kueue queue namespace set exceeds its bound")
        if any(_NAMESPACE.fullmatch(value) is None for value in self.queue_namespaces):
            raise ValueError("admin Kueue queue namespace is invalid")
        if self.kueue_api_version not in {"v1beta1", "v1beta2"}:
            raise ValueError("unsupported Kueue API version")
        label_keys = (
            *self.gpu_class_label_keys,
            *self.capacity_type_label_keys,
            *self.capacity_type_annotation_keys,
            self.semantic_pool_label,
            *self.pool_label_fallbacks,
        )
        if any(not 1 <= len(value) <= 253 for value in label_keys):
            raise ValueError("admin node label key is outside the bound")
        if len(self.node_scaler_pools) > 128:
            raise ValueError("managed node-scaler pool contract exceeds its bound")
        pool_ids = [pool.pool_id for pool in self.node_scaler_pools]
        if len(pool_ids) != len(set(pool_ids)):
            raise ValueError("managed node-scaler pool identifiers must be unique")
        if self.node_scaler_provider not in {None, "nebius-managed-node-group-autoscaler"}:
            raise ValueError("unsupported managed node-scaler provider")

        if self.node_scaler_pools and self.node_scaler_provider is None:
            raise ValueError("managed node-scaler pools require a provider")

    @property
    def resolved_queue_namespaces(self) -> tuple[str, ...]:
        """Namespaces the Kueue queue projection reads, model namespace first."""

        ordered = [self.model_namespace, *self.queue_namespaces]
        return tuple(dict.fromkeys(ordered))

    @property
    def pool_label_keys(self) -> tuple[str, ...]:
        # The provider pool ID is the authoritative identity used by the exact
        # Terraform pool contract. Semantic labels such as ``hot`` or ``burst``
        # are useful classifications, but multiple real node groups may share
        # one and therefore cannot drive node-scaler health.
        return (*self.pool_label_fallbacks, self.semantic_pool_label)


def _capacity_type(
    labels: Mapping[str, object],
    config: KubernetesCapacityConfig,
    annotations: Mapping[str, object] | None = None,
) -> AdminCapacityType:
    candidates = [(key, labels) for key in config.capacity_type_label_keys]
    if annotations is not None:
        candidates.extend((key, annotations) for key in config.capacity_type_annotation_keys)
    for key, source in candidates:
        raw = source.get(key)
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


@dataclass(frozen=True)
class KubernetesModelStateConfig:
    """The fixed workload vocabulary accepted by the model-state reader."""

    model_namespace: str = "fs2-models"
    model_label_keys: tuple[str, ...] = _MODEL_LABEL_KEYS
    capacity_type_label_keys: tuple[str, ...] = (
        "capacity.fs2.nebius/type",
        "karpenter.sh/capacity-type",
        "cloud.google.com/gke-spot",
        "nebius.com/preemptible",
    )

    def __post_init__(self) -> None:
        if _NAMESPACE.fullmatch(self.model_namespace) is None:
            raise ValueError("admin Kubernetes model namespace is invalid")
        if not 1 <= len(self.model_label_keys) <= 8 or not 1 <= len(self.capacity_type_label_keys) <= 16:
            raise ValueError("admin Kubernetes model-state label vocabulary is outside the bound")
        if any(not 1 <= len(value) <= 253 for value in (*self.model_label_keys, *self.capacity_type_label_keys)):
            raise ValueError("admin Kubernetes model-state label key is outside the bound")


def _allowed_model_id(
    value: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    label_keys: Sequence[str],
) -> str | None:
    labels = _mapping(_mapping(value.get("metadata")).get("labels"))
    for key in label_keys:
        model_id = labels.get(key)
        if isinstance(model_id, str) and model_id in allowed:
            return model_id
    return None


def _pod_ready(pod: Mapping[str, Any]) -> bool:
    metadata = _mapping(pod.get("metadata"))
    status = _mapping(pod.get("status"))
    if metadata.get("deletionTimestamp") is not None or status.get("phase") != "Running":
        return False
    return any(
        _mapping(condition).get("type") == "Ready" and _mapping(condition).get("status") == "True"
        for condition in _sequence(status.get("conditions"))
    )


def _pod_failed(pod: Mapping[str, Any]) -> bool:
    status = _mapping(pod.get("status"))
    if status.get("phase") == "Failed":
        return True
    for container_status in (
        *_sequence(status.get("initContainerStatuses")),
        *_sequence(status.get("containerStatuses")),
    ):
        state = _mapping(_mapping(container_status).get("state"))
        waiting_reason = _mapping(state.get("waiting")).get("reason")
        terminated = _mapping(state.get("terminated"))
        if waiting_reason in _POD_FAILURE_REASONS:
            return True
        if terminated and terminated.get("exitCode") not in {None, 0}:
            return True
    return False


def _deployment_failed(deployment: Mapping[str, Any]) -> bool:
    conditions = _sequence(_mapping(deployment.get("status")).get("conditions"))
    for condition_value in conditions:
        condition = _mapping(condition_value)
        if condition.get("type") == "ReplicaFailure" and condition.get("status") == "True":
            return True
        if (
            condition.get("type") == "Progressing"
            and condition.get("status") == "False"
            and condition.get("reason") == "ProgressDeadlineExceeded"
        ):
            return True
    return False


def _service_selects(service: Mapping[str, Any], pod: Mapping[str, Any]) -> bool:
    selector = _mapping(_mapping(service.get("spec")).get("selector"))
    labels = _mapping(_mapping(pod.get("metadata")).get("labels"))
    return bool(selector) and all(
        isinstance(value, str) and labels.get(key) == value for key, value in selector.items()
    )


class KubernetesModelStateAdminAdapter:
    """Project Deployments, Services, Pods, and Nodes into bounded model state."""

    def __init__(
        self,
        reader: KubernetesListReader,
        *,
        config: KubernetesModelStateConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.reader = reader
        self.config = config or KubernetesModelStateConfig()
        self.clock = clock or (lambda: datetime.now(UTC))

    async def snapshot(self, model_ids: tuple[str, ...]) -> AdminKubernetesSnapshot:
        if len(model_ids) > 256 or len(model_ids) != len(set(model_ids)) or any(not model_id for model_id in model_ids):
            raise ValueError("admin Kubernetes model request is outside the bound")
        namespace = self.config.model_namespace
        deployments, services, pods, nodes = await asyncio.gather(
            self.reader.list(f"/apis/apps/v1/namespaces/{namespace}/deployments"),
            self.reader.list(f"/api/v1/namespaces/{namespace}/services"),
            self.reader.list(f"/api/v1/namespaces/{namespace}/pods"),
            self.reader.list("/api/v1/nodes"),
        )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("admin adapter clock must be timezone-aware")
        allowed = frozenset(model_ids)
        try:
            result = self._models(model_ids, allowed=allowed, deployments=deployments, services=services, pods=pods)
            allocatable_gpus, ready_gpu_nodes, preemptible_gpu_nodes = self._gpu_nodes(nodes)
            active_gpu_replicas = sum(
                1
                for pod in pods
                if _allowed_model_id(pod, allowed=allowed, label_keys=self.config.model_label_keys) is not None
                and _pod_ready(pod)
                and sum(_pod_gpu_requests(pod).values(), Decimal()) > 0
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise AdminAdapterUnavailableError("Kubernetes model-state response is invalid") from exc
        return AdminKubernetesSnapshot(
            observed_at=now.astimezone(UTC),
            models=result,
            allocatable_gpus=allocatable_gpus,
            ready_gpu_nodes=ready_gpu_nodes,
            preemptible_gpu_nodes=preemptible_gpu_nodes,
            active_gpu_replicas=active_gpu_replicas,
        )

    def _models(
        self,
        model_ids: tuple[str, ...],
        *,
        allowed: frozenset[str],
        deployments: Sequence[Mapping[str, Any]],
        services: Sequence[Mapping[str, Any]],
        pods: Sequence[Mapping[str, Any]],
    ) -> list[AdminKubernetesModel]:
        deployments_by_model: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
        services_by_model: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
        pods_by_model: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for value, destination in (
            *((deployment, deployments_by_model) for deployment in deployments),
            *((service, services_by_model) for service in services),
            *((pod, pods_by_model) for pod in pods),
        ):
            model_id = _allowed_model_id(value, allowed=allowed, label_keys=self.config.model_label_keys)
            if model_id is not None:
                destination[model_id].append(value)

        result: list[AdminKubernetesModel] = []
        for model_id in model_ids:
            model_deployments = deployments_by_model[model_id]
            model_pods = pods_by_model[model_id]
            model_services = list(services_by_model[model_id])
            for service in services:
                if service in model_services:
                    continue
                if any(_service_selects(service, pod) for pod in model_pods):
                    model_services.append(service)
            desired = 0
            deployment_ready = 0
            for deployment in model_deployments:
                spec = _mapping(deployment.get("spec"))
                status = _mapping(deployment.get("status"))
                desired_value = spec.get("replicas", 1)
                ready_value = status.get("readyReplicas", 0)
                if (
                    not isinstance(desired_value, int)
                    or isinstance(desired_value, bool)
                    or not 0 <= desired_value <= 10000
                    or not isinstance(ready_value, int)
                    or isinstance(ready_value, bool)
                    or not 0 <= ready_value <= 10000
                ):
                    raise ValueError("Kubernetes Deployment replica count is invalid")
                desired += desired_value
                deployment_ready += ready_value
            if desired > 10000 or deployment_ready > 10000:
                raise ValueError("Kubernetes aggregate replica count is invalid")
            served_ready_pods = {
                str(_mapping(pod.get("metadata")).get("uid", _mapping(pod.get("metadata")).get("name", "")))
                for pod in model_pods
                if _pod_ready(pod) and any(_service_selects(service, pod) for service in model_services)
            }
            served_ready_pods.discard("")
            ready = min(desired, deployment_ready, len(served_ready_pods))
            explicit_failure = any(_deployment_failed(value) for value in model_deployments) or any(
                _pod_failed(value) for value in model_pods
            )
            semantic_healthy: bool | None
            if not model_deployments or not model_services:
                semantic_healthy = None
            elif explicit_failure:
                semantic_healthy = False
            else:
                # A non-failing rollout is healthy-but-loading until its
                # Service has a Ready endpoint. Scale-to-zero remains a valid
                # cold state because no endpoint is expected at a zero floor.
                semantic_healthy = True
            result.append(
                AdminKubernetesModel(
                    model_id=model_id,
                    desired_replicas=desired,
                    ready_replicas=ready,
                    semantic_healthy=semantic_healthy,
                )
            )
        return result

    def _gpu_nodes(self, nodes: Sequence[Mapping[str, Any]]) -> tuple[int, int, int]:
        allocatable_gpus = 0
        ready_gpu_nodes = 0
        preemptible_gpu_nodes = 0
        for node in nodes:
            metadata = _mapping(node.get("metadata"))
            labels = _mapping(metadata.get("labels"))
            status = _mapping(node.get("status"))
            allocatable = sum(
                (
                    _gpu_count(value)
                    for resource_name, value in _mapping(status.get("allocatable")).items()
                    if _GPU_RESOURCE.fullmatch(str(resource_name)) is not None
                ),
                Decimal(),
            )
            if allocatable <= 0:
                continue
            allocatable_gpus += int(allocatable)
            ready = any(
                _mapping(condition).get("type") == "Ready" and _mapping(condition).get("status") == "True"
                for condition in _sequence(status.get("conditions"))
            )
            if ready:
                ready_gpu_nodes += 1
            for key in self.config.capacity_type_label_keys:
                value = labels.get(key)
                if isinstance(value, str) and value.casefold() in {"preemptible", "spot", "true"}:
                    preemptible_gpu_nodes += 1
                    break
        if allocatable_gpus > 100000:
            raise ValueError("Kubernetes aggregate GPU count is invalid")
        return allocatable_gpus, ready_gpu_nodes, preemptible_gpu_nodes


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
        node_pools, kueue, hpa, keda, node_scaler_probe = await asyncio.gather(
            self._node_pools(),
            self._kueue(),
            self._hpa(),
            self._keda(),
            self._node_scaler_probe(),
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
                node_scaler=self._node_scaler(
                    node_pools,
                    node_scaler_probe,
                    observed_at=now.astimezone(UTC),
                ),
            ),
        )

    async def _node_scaler_probe(self) -> ManagedNodeScalerProbe:
        if self.config.node_scaler_provider is None:
            return ManagedNodeScalerProbe(
                state=AdminSourceState.UNAVAILABLE,
                healthy=None,
                reason="managed node-scaler provider is not configured",
            )
        try:
            config_map = await self.reader.get(NODE_SCALER_STATUS_PATH)
        except (AdminAdapterUnavailableError, AttributeError):
            return ManagedNodeScalerProbe(
                state=AdminSourceState.UNAVAILABLE,
                healthy=None,
                reason="managed cluster-autoscaler status is unavailable",
            )
        status = _mapping(config_map.get("data")).get("status")
        if not isinstance(status, str) or not status or len(status.encode()) > MAX_NODE_SCALER_STATUS_BYTES:
            return ManagedNodeScalerProbe(
                state=AdminSourceState.UNAVAILABLE,
                healthy=None,
                reason="managed cluster-autoscaler status is malformed",
            )
        cluster_match = re.search(r"(?ms)^clusterWide:\n(?P<body>.*?)(?=^nodeGroups:|\Z)", status)
        health_match = (
            re.search(r"(?ms)^  health:\n(?P<body>.*?)(?=^  [A-Za-z]|\Z)", cluster_match.group("body"))
            if cluster_match is not None
            else None
        )
        health_body = health_match.group("body") if health_match is not None else ""
        health_status = re.search(
            rf"(?m)^    status: ([A-Za-z]{{1,{NODE_SCALER_HEALTH_STATUS_MAX_LENGTH}}})[ \t]*$",
            health_body,
        )
        probe_time = re.search(r'(?m)^    lastProbeTime: "([^"]+)"\s*$', health_body)
        running = re.search(r"(?m)^autoscalerStatus: Running\s*$", status) is not None
        if health_status is None or probe_time is None:
            return ManagedNodeScalerProbe(
                state=AdminSourceState.UNAVAILABLE,
                healthy=None,
                reason="managed cluster-autoscaler health evidence is malformed",
            )
        try:
            observed_at = datetime.fromisoformat(probe_time.group(1).replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return ManagedNodeScalerProbe(
                state=AdminSourceState.UNAVAILABLE,
                healthy=None,
                reason="managed cluster-autoscaler probe time is malformed",
            )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("admin adapter clock must be timezone-aware")
        age = now.astimezone(UTC) - observed_at
        if age < -timedelta(minutes=1) or age > NODE_SCALER_STATUS_MAX_AGE:
            return ManagedNodeScalerProbe(
                state=AdminSourceState.UNAVAILABLE,
                healthy=None,
                observed_at=observed_at,
                reason="managed cluster-autoscaler health evidence is stale",
            )
        reported_health = health_status.group(1)
        if reported_health not in NODE_SCALER_HEALTH_STATUSES:
            return ManagedNodeScalerProbe(
                state=AdminSourceState.UNAVAILABLE,
                healthy=None,
                observed_at=observed_at,
                reason="managed cluster-autoscaler health state is unsupported",
            )
        healthy = running and reported_health == "Healthy"
        reason = None
        if not running:
            reason = "managed cluster-autoscaler is not Running"
        elif reported_health != "Healthy":
            reason = f"managed cluster-autoscaler reports {reported_health}"
        return ManagedNodeScalerProbe(
            state=AdminSourceState.AVAILABLE,
            healthy=healthy,
            observed_at=observed_at,
            reason=reason,
        )

    def _node_scaler(
        self,
        node_pools: AdminNodePoolInventory,
        probe: ManagedNodeScalerProbe,
        *,
        observed_at: datetime,
    ) -> AdminNodeScalerProjection:
        provider = self.config.node_scaler_provider
        contracts = self.config.node_scaler_pools
        if provider is None:
            return AdminNodeScalerProjection(
                state=AdminSourceState.UNAVAILABLE,
                configured=False,
                healthy=None,
                reason="managed node-scaler provider is not configured",
            )
        if not contracts:
            return AdminNodeScalerProjection(
                state=AdminSourceState.UNAVAILABLE,
                provider=provider,
                configured=False,
                healthy=None,
                reason="managed node-scaler pool contract is unavailable",
            )
        if probe.state != AdminSourceState.AVAILABLE or probe.healthy is None:
            return AdminNodeScalerProjection(
                state=AdminSourceState.UNAVAILABLE,
                provider=provider,
                configured=True,
                healthy=None,
                observed_at=probe.observed_at,
                reason=probe.reason or "managed cluster-autoscaler health evidence is unavailable",
            )
        if node_pools.state != AdminSourceState.AVAILABLE:
            return AdminNodeScalerProjection(
                state=AdminSourceState.UNAVAILABLE,
                provider=provider,
                configured=True,
                healthy=None,
                reason="Kubernetes node evidence for the managed node scaler is unavailable",
            )

        observed: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
        configured_pool_ids = {contract.pool_id for contract in contracts}
        for pool in node_pools.items:
            if pool.pool_label not in configured_pool_ids:
                continue
            total = pool.nodes.total.value
            ready = pool.nodes.ready.value
            if (
                pool.nodes.total.state != AdminValueState.AVAILABLE
                or pool.nodes.ready.state != AdminValueState.AVAILABLE
                or total is None
                or ready is None
                or total < 0
                or ready < 0
                or not total.is_integer()
                or not ready.is_integer()
            ):
                return AdminNodeScalerProjection(
                    state=AdminSourceState.UNAVAILABLE,
                    provider=provider,
                    configured=True,
                    healthy=None,
                    reason="Kubernetes managed node-pool counts are incomplete",
                )
            observed[pool.pool_label][0] += int(total)
            observed[pool.pool_label][1] += int(ready)

        bounds_healthy = all(
            contract.min_nodes <= observed[contract.pool_id][0] <= contract.max_nodes
            and observed[contract.pool_id][1] == observed[contract.pool_id][0]
            for contract in contracts
        )
        healthy = probe.healthy and bounds_healthy
        reason = probe.reason
        if probe.healthy and not bounds_healthy:
            reason = "managed node-pool bounds or readiness are unhealthy"
        return AdminNodeScalerProjection(
            state=AdminSourceState.AVAILABLE,
            provider=provider,
            configured=True,
            healthy=healthy,
            observed_at=probe.observed_at or observed_at,
            reason=reason,
        )

    async def _node_pools(self) -> AdminNodePoolInventory:
        # GPU allocation is summed over every namespace that can place a GPU
        # Pod. Counting only the model namespace understates allocation on any
        # node also running an academic or reference-data workload.
        namespaces = self.config.resolved_queue_namespaces
        results = await asyncio.gather(
            self.reader.list("/api/v1/nodes"),
            *(self.reader.list(f"/api/v1/namespaces/{namespace}/pods") for namespace in namespaces),
            return_exceptions=True,
        )
        nodes_result: object = results[0]
        pod_results = results[1:]
        if isinstance(nodes_result, BaseException):
            return AdminNodePoolInventory(
                state=AdminSourceState.UNAVAILABLE,
                reason="Kubernetes node inventory is unavailable",
                items=[],
            )
        pods_available = not any(isinstance(value, BaseException) for value in pod_results)
        nodes = cast(list[Mapping[str, Any]], nodes_result)
        pods = [pod for page in pod_results if isinstance(page, list) for pod in page] if pods_available else []
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
            pool_label = _first_label(labels, self.config.pool_label_keys)
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
        namespaces = self.config.resolved_queue_namespaces
        cluster_scoped = (
            f"{prefix}/resourceflavors",
            f"{prefix}/clusterqueues",
        )
        # LocalQueues and Workloads are namespaced, so every configured lane is
        # listed. A single failing namespace fails the projection rather than
        # returning a queue set that silently omits one lane's workloads.
        namespaced = tuple(
            f"{prefix}/namespaces/{namespace}/{resource}"
            for resource in ("localqueues", "workloads")
            for namespace in namespaces
        )
        paths = (*cluster_scoped, *namespaced, f"{prefix}/cohorts")
        required = len(cluster_scoped) + len(namespaced)
        values = await asyncio.gather(*(self.reader.list(path) for path in paths), return_exceptions=True)
        if any(isinstance(value, BaseException) for value in values[:required]):
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
        listed = [value for value in values[:required] if isinstance(value, list)]
        flavors_raw, cluster_raw = listed[0], listed[1]
        split = 2 + len(namespaces)
        local_raw = [item for page in listed[2:split] for item in page]
        workloads_raw = [item for page in listed[split:required] for item in page]
        cohorts_value = values[required]
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
        annotations = _mapping(metadata.get("annotations"))
        return AdminResourceFlavor(
            name=str(metadata.get("name", "unknown"))[:253],
            capacity_type=_capacity_type(labels, self.config, annotations),
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


class PrometheusModelMetricsReader(PrometheusScalarReader, Protocol):
    async def model_vector(
        self,
        query: str,
        *,
        at: datetime,
        model_ids: tuple[str, ...],
    ) -> Mapping[str, float]: ...


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

    async def _instant(self, query: str, *, at: datetime) -> Mapping[str, Any]:
        if not 1 <= len(query) <= 4096 or any(value in query.casefold() for value in ("tenant", "principal", "token")):
            raise ValueError("Prometheus query is outside the server-owned bound")
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Prometheus query timestamp must be timezone-aware")
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
            return _mapping(body.get("data"))
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AdminAdapterUnavailableError("Prometheus query transport failed") from exc

    async def scalar(self, query: str, *, at: datetime) -> float | None:
        try:
            data = await self._instant(query, at=at)
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
        except ValueError as exc:
            if isinstance(exc, AdminAdapterUnavailableError):
                raise
            raise AdminAdapterUnavailableError("Prometheus scalar is invalid") from exc

    async def model_vector(
        self,
        query: str,
        *,
        at: datetime,
        model_ids: tuple[str, ...],
    ) -> Mapping[str, float]:
        if len(model_ids) > 256 or len(model_ids) != len(set(model_ids)) or any(not model_id for model_id in model_ids):
            raise ValueError("Prometheus model vector request is outside the bound")
        data = await self._instant(query, at=at)
        if data.get("resultType") != "vector":
            raise AdminAdapterUnavailableError("Prometheus model query did not return a vector")
        result = _sequence(data.get("result"))
        if len(result) > len(model_ids):
            raise AdminAdapterUnavailableError("Prometheus model query exceeded its series bound")
        allowed = frozenset(model_ids)
        values: dict[str, float] = {}
        try:
            for series_value in result:
                series = _mapping(series_value)
                metric = _mapping(series.get("metric"))
                if set(metric) != {"model"}:
                    raise AdminAdapterUnavailableError("Prometheus model query returned an unexpected label set")
                model_id = metric.get("model")
                pair = _sequence(series.get("value"))
                if not isinstance(model_id, str) or model_id not in allowed or model_id in values:
                    raise AdminAdapterUnavailableError("Prometheus model query returned an invalid model label")
                if len(pair) != 2 or not isinstance(pair[1], str):
                    raise AdminAdapterUnavailableError("Prometheus model sample shape is invalid")
                value = float(pair[1])
                if value < 0 or value != value or value in {float("inf"), float("-inf")}:
                    raise AdminAdapterUnavailableError("Prometheus model sample is invalid")
                values[model_id] = value
        except ValueError as exc:
            if isinstance(exc, AdminAdapterUnavailableError):
                raise
            raise AdminAdapterUnavailableError("Prometheus model sample is invalid") from exc
        return values


class PrometheusModelMetricsAdminAdapter:
    """Join fixed aggregate and grouped PromQL into the model-metric contract."""

    def __init__(
        self,
        reader: PrometheusModelMetricsReader,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.reader = reader
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _integer(value: float | None, *, required: bool) -> int | None:
        if value is None:
            if required:
                raise AdminAdapterUnavailableError("Prometheus terminal-operation series is unavailable")
            return None
        integer = int(value)
        if float(integer) != value:
            raise AdminAdapterUnavailableError("Prometheus terminal-operation sample is not an integer")
        return integer

    async def snapshot(
        self,
        model_ids: tuple[str, ...],
        *,
        from_at: datetime,
        to_at: datetime,
    ) -> AdminPrometheusSnapshot:
        if len(model_ids) > 256 or len(model_ids) != len(set(model_ids)) or any(not model_id for model_id in model_ids):
            raise ValueError("admin Prometheus model request is outside the bound")
        if (
            from_at.tzinfo is None
            or from_at.utcoffset() is None
            or to_at.tzinfo is None
            or to_at.utcoffset() is None
            or from_at >= to_at
        ):
            raise ValueError("admin Prometheus range is invalid")
        window = int((to_at - from_at).total_seconds())
        aggregate_queries = PrometheusQueryTemplates.for_window(model_id=None, seconds=window)
        vector_queries = PrometheusQueryTemplates.by_model_for_window(seconds=window)
        names = tuple(aggregate_queries)
        results = await asyncio.gather(
            *(self.reader.scalar(aggregate_queries[name], at=to_at) for name in names),
            *(self.reader.model_vector(vector_queries[name], at=to_at, model_ids=model_ids) for name in names),
            return_exceptions=True,
        )
        aggregate_values: dict[str, float | None] = {}
        vector_values: dict[str, Mapping[str, float]] = {}
        for name, value in zip(names, results[: len(names)], strict=True):
            aggregate_values[name] = value if isinstance(value, float | int) else None
        for name, value in zip(names, results[len(names) :], strict=True):
            vector_values[name] = value if isinstance(value, Mapping) else {}

        requests_per_second = aggregate_values["requests_per_second"]
        error_rate = aggregate_values["error_rate"]
        if requests_per_second is None or error_rate is None:
            raise AdminAdapterUnavailableError("Prometheus required model-metric series is unavailable")
        terminal_operations = self._integer(aggregate_values["terminal_operations"], required=True)
        assert terminal_operations is not None
        observed_at = self.clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise RuntimeError("admin adapter clock must be timezone-aware")

        def metric(name: str, model_id: str) -> float | None:
            return vector_values[name].get(model_id)

        return AdminPrometheusSnapshot(
            observed_at=observed_at.astimezone(UTC),
            models=[
                AdminPrometheusModel(
                    model_id=model_id,
                    requests_per_second=metric("requests_per_second", model_id),
                    terminal_operations=self._integer(metric("terminal_operations", model_id), required=False),
                    error_rate=metric("error_rate", model_id),
                    latency_p50_seconds=metric("latency_p50_seconds", model_id),
                    latency_p95_seconds=metric("latency_p95_seconds", model_id),
                    latency_p99_seconds=metric("latency_p99_seconds", model_id),
                )
                for model_id in model_ids
            ],
            requests_per_second=float(requests_per_second),
            terminal_operations=terminal_operations,
            error_rate=float(error_rate),
            latency_p50_seconds=aggregate_values["latency_p50_seconds"],
            latency_p95_seconds=aggregate_values["latency_p95_seconds"],
            latency_p99_seconds=aggregate_values["latency_p99_seconds"],
        )


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
        elif component.installed is False:
            launch = AdminObservabilityLaunch(enabled=False, reason="component is not installed")
        elif configured is None:
            reason = {
                "prometheus": "raw Prometheus stays private; configure the verified Grafana Explore route",
                "loki": "raw Loki stays private; configure the verified Grafana Explore route",
                "otel": "OpenTelemetry has no direct operator UI; configure a verified Grafana dashboard route",
                "dcgm": "DCGM has no direct operator UI; configure a verified Grafana dashboard route",
                "kueue": "Kueue has no direct operator UI; configure a verified Grafana dashboard route",
                "keda": "KEDA has no direct operator UI; configure a verified Grafana dashboard route",
            }.get(component.id, "no verified operator UI route is configured")
            launch = AdminObservabilityLaunch(enabled=False, reason=reason)
        else:
            launch = AdminObservabilityLaunch(enabled=False, reason="component health or data probe did not pass")
        return component.model_copy(update={"launch": launch})
