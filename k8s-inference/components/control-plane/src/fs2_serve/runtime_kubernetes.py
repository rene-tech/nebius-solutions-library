"""Fail-closed Kubernetes runtime identity and lifecycle observations.

The public inference response is not an attribution authority. This adapter
verifies instrumented response hints against Service/EndpointSlice/Pod facts,
or resolves a single ready ModelDeployment Pod through the Kubernetes API and
uses only Pod/Node status, Kubernetes Events, and annotations written by the
node-local GPU allocation observer.  Ambiguous replicas deliberately produce
no attribution rather than guessing which Pod served a request.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .admin import AdminAdapterUnavailableError
from .admin_adapters import KubernetesListReader
from .model_deployment import MODEL_ID_LABEL
from .models import (
    RuntimeIdentity,
    RuntimeLifecycleObservation,
    RuntimeObservationSource,
    RuntimeObservedPhase,
    RuntimePhaseObservation,
)

GPU_UUIDS_ANNOTATION = "telemetry.fs2.nebius.ai/gpu-uuids"
GPU_ALLOCATION_OBSERVED_AT_ANNOTATION = "telemetry.fs2.nebius.ai/gpu-allocation-observed-at"
GPU_OBSERVER_RESOLUTION_ANNOTATION = "telemetry.fs2.nebius.ai/gpu-observer-resolution-seconds"
PHASE_ANNOTATION_PREFIX = "telemetry.fs2.nebius.ai/phase-"
RESPONSE_IDENTITY_ANNOTATION = "telemetry.fs2.nebius.ai/response-identity"
RESPONSE_IDENTITY_VERSION = "asgi-v1"
COSMOS_RESPONSE_IDENTITY_VERSION = "asgi-cosmos-adapter-v1"

_GPU_RESOURCE = re.compile(r"^(?:nvidia\.com/(?:gpu|mig-[A-Za-z0-9_.-]+)|amd\.com/gpu|gpu\.intel\.com/(?:i915|xe))$")
_GPU_UUID = re.compile(r"^(?:GPU|MIG)-[A-Za-z0-9_.:/-]{1,123}$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, list) else ()


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value)
    if not text.isdigit():
        return None
    parsed = int(text)
    return parsed if 1 <= parsed <= 64 else None


def pod_gpu_count(pod: Mapping[str, Any]) -> int | None:
    total = 0
    containers = _sequence(_mapping(pod.get("spec")).get("containers"))
    if not containers:
        return None
    for raw_container in containers:
        container = _mapping(raw_container)
        resources = _mapping(container.get("resources"))
        requests = _mapping(resources.get("requests"))
        limits = _mapping(resources.get("limits"))
        for name, raw_request in requests.items():
            if not isinstance(name, str) or _GPU_RESOURCE.fullmatch(name) is None:
                continue
            request = _positive_int(raw_request)
            limit = _positive_int(limits.get(name))
            if request is None or request != limit:
                return None
            total += request
    return total if 1 <= total <= 64 else None


def _condition_time(pod: Mapping[str, Any], condition_type: str) -> datetime | None:
    for raw_condition in _sequence(_mapping(pod.get("status")).get("conditions")):
        condition = _mapping(raw_condition)
        if condition.get("type") == condition_type and condition.get("status") == "True":
            return _timestamp(condition.get("lastTransitionTime"))
    return None


def _container_started_at(pod: Mapping[str, Any]) -> datetime | None:
    starts: list[datetime] = []
    for raw_status in _sequence(_mapping(pod.get("status")).get("containerStatuses")):
        status = _mapping(raw_status)
        running = _mapping(_mapping(status.get("state")).get("running"))
        started_at = _timestamp(running.get("startedAt"))
        if started_at is not None:
            starts.append(started_at)
    return min(starts) if starts else None


def _gpu_uuids(annotations: Mapping[str, Any], gpu_count: int) -> tuple[str, ...]:
    raw = annotations.get(GPU_UUIDS_ANNOTATION)
    if not isinstance(raw, str) or len(raw) > 16_384:
        return ()
    try:
        values = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return ()
    if (
        not isinstance(values, list)
        or len(values) != gpu_count
        or any(not isinstance(value, str) or _GPU_UUID.fullmatch(value) is None for value in values)
        or len(set(values)) != len(values)
    ):
        return ()
    return tuple(values)


def _event_time(event: Mapping[str, Any]) -> datetime | None:
    series = _mapping(event.get("series"))
    metadata = _mapping(event.get("metadata"))
    for candidate in (
        event.get("eventTime"),
        series.get("lastObservedTime"),
        event.get("lastTimestamp"),
        event.get("firstTimestamp"),
        metadata.get("creationTimestamp"),
    ):
        parsed = _timestamp(candidate)
        if parsed is not None:
            return parsed
    return None


def _image_pull_observations(events: Sequence[Mapping[str, Any]], pod_uid: str) -> list[RuntimePhaseObservation]:
    grouped: dict[str, list[tuple[datetime, str, str | None]]] = defaultdict(list)
    for event in events:
        involved = _mapping(event.get("involvedObject"))
        reason = event.get("reason")
        if involved.get("uid") != pod_uid or reason not in {"Pulling", "Pulled"}:
            continue
        occurred_at = _event_time(event)
        if occurred_at is None:
            continue
        field_path = involved.get("fieldPath")
        group = field_path if isinstance(field_path, str) and len(field_path) <= 253 else "pod"
        event_uid = _mapping(event.get("metadata")).get("uid")
        grouped[group].append(
            (occurred_at, str(reason), event_uid if isinstance(event_uid, str) and len(event_uid) <= 128 else None)
        )

    observations: list[RuntimePhaseObservation] = []
    for values in grouped.values():
        pending: tuple[datetime, str | None] | None = None
        for occurred_at, reason, event_uid in sorted(values, key=lambda item: (item[0], item[1])):
            if reason == "Pulling":
                pending = (occurred_at, event_uid)
            elif pending is not None and occurred_at >= pending[0]:
                observations.append(
                    RuntimePhaseObservation(
                        phase=RuntimeObservedPhase.IMAGE_PULL,
                        started_at=pending[0],
                        completed_at=occurred_at,
                        source=RuntimeObservationSource.KUBERNETES,
                        source_resolution_seconds=0.001,
                        start_event_uid=pending[1],
                        end_event_uid=event_uid,
                    )
                )
                pending = None
    return observations


def _annotated_phase_observations(annotations: Mapping[str, Any]) -> list[RuntimePhaseObservation]:
    observations: list[RuntimePhaseObservation] = []
    for phase in RuntimeObservedPhase:
        if phase is RuntimeObservedPhase.IMAGE_PULL:
            continue
        prefix = f"{PHASE_ANNOTATION_PREFIX}{phase.value}"
        started_at = _timestamp(annotations.get(f"{prefix}-started-at"))
        completed_at = _timestamp(annotations.get(f"{prefix}-completed-at"))
        if started_at is None or completed_at is None or completed_at < started_at:
            continue
        observations.append(
            RuntimePhaseObservation(
                phase=phase,
                started_at=started_at,
                completed_at=completed_at,
                source=RuntimeObservationSource.CONTROLLER,
                source_resolution_seconds=0.001,
            )
        )
    return observations


def _is_ready_model_pod(pod: Mapping[str, Any], model_id: str) -> bool:
    metadata = _mapping(pod.get("metadata"))
    labels = _mapping(metadata.get("labels"))
    status = _mapping(pod.get("status"))
    return (
        labels.get(MODEL_ID_LABEL) == model_id
        and metadata.get("deletionTimestamp") is None
        and status.get("phase") == "Running"
        and _condition_time(pod, "Ready") is not None
    )


@dataclass(frozen=True)
class KubernetesRuntimeMetadataProvider:
    """Resolve a unique ready model Pod and exact observed lifecycle facts."""

    reader: KubernetesListReader
    namespace: str = "fs2-models"

    def __post_init__(self) -> None:
        if _DNS_LABEL.fullmatch(self.namespace) is None:
            raise ValueError("runtime metadata namespace is invalid")

    async def resolve(self, *, operation_id: UUID, model_id: str) -> RuntimeIdentity:
        observation = await self.resolve_lifecycle(operation_id=operation_id, model_id=model_id)
        return observation.runtime if observation is not None else RuntimeIdentity()

    async def resolve_lifecycle(
        self,
        *,
        operation_id: UUID,
        model_id: str,
    ) -> RuntimeLifecycleObservation | None:
        del operation_id
        return await self._resolve(model_id=model_id)

    async def resolve_response_lifecycle(
        self,
        *,
        operation_id: UUID,
        model_id: str,
        pod_uid: str,
        service_name: str,
        service_namespace: str,
        service_port: int,
        runtime_image_digest: str,
        model_revision: str | None,
    ) -> RuntimeLifecycleObservation | None:
        del operation_id  # The RuntimeClient already verifies operation + attempt echo.
        if service_namespace != self.namespace or _DNS_LABEL.fullmatch(service_name) is None or model_revision is None:
            return None
        return await self._resolve(
            model_id=model_id,
            response_binding={
                "pod_uid": pod_uid,
                "service_name": service_name,
                "service_port": service_port,
                "runtime_image_digest": runtime_image_digest,
                "model_revision": model_revision,
            },
        )

    async def _response_matches_endpoint(
        self,
        pod: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> bool:
        metadata, spec, status = (_mapping(pod.get(key)) for key in ("metadata", "spec", "status"))
        annotations = _mapping(metadata.get("annotations"))
        if (
            metadata.get("namespace") != self.namespace
            or annotations.get(RESPONSE_IDENTITY_ANNOTATION) not in {
                RESPONSE_IDENTITY_VERSION, COSMOS_RESPONSE_IDENTITY_VERSION,
            }
            or annotations.get("fs2.nebius/model-revision") != binding["model_revision"]
            or annotations.get("fs2.nebius/runtime-image-digest") != binding["runtime_image_digest"]
        ):
            return False
        containers = [_mapping(c) for c in _sequence(spec.get("containers"))]
        is_cosmos_adapter = annotations.get(RESPONSE_IDENTITY_ANNOTATION) == COSMOS_RESPONSE_IDENTITY_VERSION
        upstream: list[Mapping[str, Any]] = []
        if is_cosmos_adapter:
            # The media Service terminates on a CPU adapter, not vLLM's port.
            # Its pinned implementation forwards only to 127.0.0.1:8000. Bind
            # that same Pod's actual GPU container as well as the responding
            # adapter; a hint from a separate relay Pod is never sufficient.
            upstream = [
                c for c in containers
                if c.get("name") == "vllm-omni"
                and str(c.get("image", "")).endswith("@" + binding["runtime_image_digest"])
                and pod_gpu_count({"spec": {"containers": [c]}}) is not None
                and any(_mapping(p).get("containerPort") == 8000
                        and _mapping(p).get("protocol", "TCP") == "TCP"
                        for p in _sequence(c.get("ports")))
            ]
            if len(upstream) != 1 or binding["service_port"] != 8080:
                return False
        instrumented = [
            c
            for c in containers
            if str(c.get("image", "")).endswith("@" + binding["runtime_image_digest"])
            and (
                (c.get("name") == "bounded-json-adapter"
                 and _sequence(c.get("command")) == ["python3", "/adapter/adapter.py"]
                 and not _sequence(c.get("args"))
                 and not any(_GPU_RESOURCE.fullmatch(str(resource))
                             for kind in ("requests", "limits")
                             for resource in _mapping(_mapping(c.get("resources")).get(kind))))
                if is_cosmos_adapter else
                "fs2_runtime_identity.RuntimeIdentityMiddleware" in _sequence(c.get("args"))
            )
            and any(
                _mapping(e).get("name") == "FS2_RUNTIME_POD_UID"
                and _mapping(_mapping(_mapping(e).get("valueFrom")).get("fieldRef")).get("fieldPath") == "metadata.uid"
                for e in _sequence(c.get("env"))
            )
        ]
        if len(instrumented) != 1:
            return False
        for component in [*instrumented, *upstream]:
            running_images = [
                _mapping(c).get("imageID")
                for c in _sequence(status.get("containerStatuses"))
                if _mapping(c).get("name") == component.get("name")
            ]
            if (
                len(running_images) != 1
                or not isinstance(running_images[0], str)
                or not running_images[0].endswith("@" + binding["runtime_image_digest"])
            ):
                return False
        services = await self.reader.list(f"/api/v1/namespaces/{self.namespace}/services")
        services = [s for s in services if _mapping(s.get("metadata")).get("name") == binding["service_name"]]
        if len(services) != 1:
            return False
        service_meta, service_spec = (_mapping(services[0].get(k)) for k in ("metadata", "spec"))
        selector, labels = _mapping(service_spec.get("selector")), _mapping(metadata.get("labels"))
        if not service_meta.get("uid") or not selector or any(labels.get(k) != v for k, v in selector.items()):
            return False
        ports = [
            _mapping(p)
            for p in _sequence(service_spec.get("ports"))
            if _mapping(p).get("port") == binding["service_port"] and _mapping(p).get("protocol", "TCP") == "TCP"
        ]
        if len(ports) != 1:
            return False
        target = ports[0].get("targetPort", binding["service_port"])
        container_ports = [_mapping(p) for p in _sequence(instrumented[0].get("ports"))]
        selected = [
            p
            for p in container_ports
            if (p.get("name") == target if isinstance(target, str) else p.get("containerPort") == target)
        ]
        if len(selected) != 1:
            return False
        slices = await self.reader.list(f"/apis/discovery.k8s.io/v1/namespaces/{self.namespace}/endpointslices")
        for item in slices:
            slice_meta = _mapping(item.get("metadata"))
            if (
                _mapping(slice_meta.get("labels")).get("kubernetes.io/service-name") != binding["service_name"]
                or not any(
                    _mapping(o).get("kind") == "Service" and _mapping(o).get("uid") == service_meta["uid"]
                    for o in _sequence(slice_meta.get("ownerReferences"))
                )
                or not any(
                    _mapping(p).get("port") == selected[0].get("containerPort")
                    and _mapping(p).get("protocol", "TCP") == "TCP"
                    for p in _sequence(item.get("ports"))
                )
            ):
                continue
            for raw_endpoint in _sequence(item.get("endpoints")):
                endpoint = _mapping(raw_endpoint)
                ref, conditions = _mapping(endpoint.get("targetRef")), _mapping(endpoint.get("conditions"))
                if (
                    ref.get("kind") == "Pod"
                    and ref.get("uid") == metadata.get("uid")
                    and ref.get("name") == metadata.get("name")
                    and ref.get("namespace") == self.namespace
                    and conditions.get("ready") is True
                    and conditions.get("terminating") is not True
                    and isinstance(status.get("podIP"), str)
                    and status["podIP"] in _sequence(endpoint.get("addresses"))
                ):
                    return True
        return False

    async def _resolve(
        self,
        *,
        model_id: str,
        response_binding: Mapping[str, Any] | None = None,
    ) -> RuntimeLifecycleObservation | None:
        try:
            pods = await self.reader.list(f"/api/v1/namespaces/{self.namespace}/pods")
            candidates = [pod for pod in pods if _is_ready_model_pod(pod, model_id)]
            if response_binding is not None:
                candidates = [
                    pod for pod in candidates if _mapping(pod.get("metadata")).get("uid") == response_binding["pod_uid"]
                ]
            if len(candidates) != 1:
                return None
            pod = candidates[0]
            if response_binding is not None and not await self._response_matches_endpoint(pod, response_binding):
                return None
            metadata = _mapping(pod.get("metadata"))
            spec = _mapping(pod.get("spec"))
            pod_uid = metadata.get("uid")
            pod_name = metadata.get("name")
            node_name = spec.get("nodeName")
            created_at = _timestamp(metadata.get("creationTimestamp"))
            scheduled_at = _condition_time(pod, "PodScheduled")
            gpu_count = pod_gpu_count(pod)
            if not all(isinstance(value, str) and value for value in (pod_uid, pod_name, node_name)):
                return None
            if created_at is None or scheduled_at is None or gpu_count is None:
                return None

            node = await self.reader.get(f"/api/v1/nodes/{node_name}")
            node_metadata = _mapping(node.get("metadata"))
            node_uid = node_metadata.get("uid")
            if not isinstance(node_uid, str) or not node_uid:
                return None
            node_labels = _mapping(node_metadata.get("labels"))
            capacity_type = node_labels.get("capacity.fs2.nebius/type")
            preemptible: bool | None = None
            if isinstance(capacity_type, str):
                if capacity_type.startswith("preemptible"):
                    preemptible = True
                elif capacity_type in {"regular", "regular-capacity-block", "capacity-block"}:
                    preemptible = False

            annotations = _mapping(metadata.get("annotations"))
            gpu_uuids = _gpu_uuids(annotations, gpu_count)
            device_at = _timestamp(annotations.get(GPU_ALLOCATION_OBSERVED_AT_ANNOTATION)) if gpu_uuids else None
            raw_resolution = annotations.get(GPU_OBSERVER_RESOLUTION_ANNOTATION)
            try:
                device_resolution = float(raw_resolution) if raw_resolution is not None else 0.0
            except (TypeError, ValueError):
                device_resolution = 0.0
            if not 0 <= device_resolution <= 300:
                device_resolution = 0.0
            if device_at is None:
                gpu_uuids = ()

            events: list[Mapping[str, Any]] = []
            try:
                events = await self.reader.list(f"/api/v1/namespaces/{self.namespace}/events")
            except AdminAdapterUnavailableError:
                # Event retention/RBAC must not erase the exact Pod identity.
                events = []
            phases = [
                *_image_pull_observations(events, str(pod_uid)),
                *_annotated_phase_observations(annotations),
            ]
            return RuntimeLifecycleObservation(
                runtime=RuntimeIdentity(
                    pod_uid=str(pod_uid),
                    node_uid=node_uid,
                    gpu_uuids=list(gpu_uuids),
                    gpu_count=gpu_count,
                    preemptible=preemptible,
                ),
                namespace=self.namespace,
                pod_name=str(pod_name),
                node_name=str(node_name),
                pod_created_at=created_at,
                pod_scheduled_at=scheduled_at,
                container_started_at=_container_started_at(pod),
                ready_at=_condition_time(pod, "Ready"),
                device_allocation_observed_at=device_at,
                device_observation_resolution_seconds=device_resolution,
                phases=phases,
            )
        except (AdminAdapterUnavailableError, OSError, TypeError, ValueError):
            return None
