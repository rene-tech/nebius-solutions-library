"""Feature-gated Kubernetes controller and fail-closed server-side-apply writer.

The controller deliberately has no cloud-provider API.  It can only reconcile
namespaced objects selected by the Terraform-owned infrastructure envelope.  A
Kubernetes Lease is checked before every mutation, server-side apply never
forces conflicts, and deletion always uses UID/resourceVersion preconditions.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import quote
from uuid import uuid4

import asyncpg
import httpx
import uvicorn
from fastapi import FastAPI, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import Field

from .admin import AdminAdapterUnavailableError
from .admin_adapters import HttpPrometheusScalarReader
from .fast_start import (
    FastStartAssessment,
    FastStartAutomaticStatus,
    FastStartLevel,
    FastStartMechanismPoolTransport,
    FastStartMechanismStatus,
    FastStartMode,
    FastStartPathAssessment,
    FastStartQualification,
    FastStartQualificationState,
    FastStartStatus,
)
from .fast_start_identity import mechanism_config_digest
from .fast_start_mechanisms import (
    DECLARED_MECHANISMS,
    FastStartCacheMechanismStatus,
    project_cache_mechanisms,
)
from .fast_start_policy import (
    AutomaticFastStartPolicy,
    AutomaticFastStartState,
    FastStartHistoryWindow,
    FastStartPath,
    evaluate_automatic_fast_start,
)
from .model_deployment import (
    API_VERSION,
    FIELD_MANAGER,
    FINALIZER,
    KIND,
    MODEL_DEPLOYMENT_LABEL,
    WORKLOAD_POOL_ANNOTATION,
    WORKLOAD_ROLE_ANNOTATION,
    AdoptionMode,
    DesiredState,
    DrainObservation,
    InfrastructureEnvelope,
    LegacyManifestRenderer,
    LegacyTemplateBundle,
    ModelDeploymentSpec,
    ObservedResource,
    ReconcileAction,
    ReconcilePlan,
    RenderContext,
    RenderedResource,
    RenderPlan,
    ValidationDisposition,
    bounded_label_value,
    canonical_digest,
    effective_hot_floor,
    operation_demand_promql,
    plan_reconciliation,
    validate_model_deployment,
)
from .models import StrictModel

if TYPE_CHECKING:
    from .settings import Settings

LOGGER = logging.getLogger(__name__)
STATUS_FIELD_MANAGER = "fs2-model-controller-status"
FENCE_ANNOTATION = "inference.fs2.nebius.ai/fence-token"
CONTROLLER_LABEL = "app.kubernetes.io/component=model-controller"


class ControllerError(RuntimeError):
    """A bounded controller failure that should be retried."""


class KubernetesConflictError(ControllerError):
    """The API server rejected optimistic concurrency or SSA ownership."""


class FenceLostError(ControllerError):
    """This process no longer owns the exact live Lease epoch."""


class WriterDisabledError(ControllerError):
    """A mutation reached an explicitly disabled writer."""


class ControllerFiles(StrictModel):
    infrastructure_envelope: InfrastructureEnvelope
    bundles: list[LegacyTemplateBundle] = Field(min_length=1, max_length=512)

    @classmethod
    def load(cls, envelope_file: Path, bundles_file: Path) -> ControllerFiles:
        envelope = InfrastructureEnvelope.model_validate_json(envelope_file.read_bytes())
        raw_bundles = json.loads(bundles_file.read_bytes())
        if not isinstance(raw_bundles, list):
            raise ValueError("model controller bundle file must contain a JSON array")
        return cls(
            infrastructure_envelope=envelope,
            bundles=[LegacyTemplateBundle.model_validate(item) for item in raw_bundles],
        )

    def renderer(self) -> LegacyManifestRenderer:
        indexed = {(item.model_ref, item.template_digest): item for item in self.bundles}
        if len(indexed) != len(self.bundles):
            raise ValueError("model controller bundle identities must be unique")
        return LegacyManifestRenderer(indexed)


class LeaseFence(StrictModel):
    namespace: str
    name: str
    holder_identity: str
    token: str = Field(min_length=32, max_length=128)
    resource_version: str = Field(min_length=1, max_length=128)
    renew_time: datetime
    duration_seconds: int = Field(ge=5, le=120)


class ModelKey(StrictModel):
    namespace: str
    name: str

    @property
    def text(self) -> str:
        return f"{self.namespace}/{self.name}"


class ResourceSnapshot(StrictModel):
    observed: ObservedResource
    resource_version: str = Field(min_length=1, max_length=128)
    generation: int = Field(default=0, ge=0)
    observed_generation: int | None = Field(default=None, ge=0)
    desired_replicas: int | None = Field(default=None, ge=0)
    replicas: int | None = Field(default=None, ge=0)
    updated_replicas: int | None = Field(default=None, ge=0)
    ready_replicas: int | None = Field(default=None, ge=0)
    available_replicas: int | None = Field(default=None, ge=0)
    unavailable_replicas: int | None = Field(default=None, ge=0)
    replica_field_managers: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(exclude=True)


class PodSnapshot(StrictModel):
    """Bounded, read-only lifecycle evidence for one generated runtime Pod."""

    name: str = Field(min_length=1, max_length=253)
    uid: str = Field(min_length=1, max_length=128)
    resource_version: str = Field(min_length=1, max_length=128)
    phase: str = Field(min_length=1, max_length=64)
    scheduled: bool
    initialized: bool
    containers_started: bool
    ready: bool
    deleting: bool = False


class Discovery(StrictModel):
    resources: list[ResourceSnapshot] = Field(max_length=256)
    pods: list[PodSnapshot] = Field(default_factory=list, max_length=10000)
    complete: bool

    def observed(self) -> list[ObservedResource]:
        return [item.observed for item in self.resources]


class ReconcileResult(StrictModel):
    key: ModelKey
    action: str
    generation: int = Field(ge=0)
    wrote: bool = False
    requeue: bool = False
    error_code: str | None = None


class ModelControllerApi(Protocol):
    async def acquire_or_renew_lease(
        self,
        *,
        namespace: str,
        name: str,
        holder_identity: str,
        token: str | None,
        duration_seconds: int,
    ) -> LeaseFence | None: ...

    async def assert_fence(self, fence: LeaseFence) -> None: ...

    async def list_models(self, namespace: str) -> list[dict[str, Any]]: ...

    async def get_model(self, key: ModelKey) -> dict[str, Any] | None: ...

    async def discover(
        self,
        *,
        key: ModelKey,
        owner_uid: str,
        render: RenderPlan,
    ) -> Discovery: ...

    async def apply_resource(
        self,
        resource: RenderedResource,
        *,
        owner_uid: str,
        fence: LeaseFence,
    ) -> ResourceSnapshot: ...

    async def delete_resource(
        self,
        identity: str,
        *,
        owner_uid: str,
        fence: LeaseFence,
    ) -> bool: ...

    async def set_finalizer(
        self,
        key: ModelKey,
        *,
        owner_uid: str,
        present: bool,
        fence: LeaseFence,
    ) -> None: ...

    async def patch_status(
        self,
        key: ModelKey,
        *,
        owner_uid: str,
        generation: int,
        status: dict[str, Any],
        fence: LeaseFence,
    ) -> bool: ...


@dataclass(frozen=True)
class ResourceEndpoint:
    api_version: str
    kind: str
    plural: str

    def collection(self, namespace: str) -> str:
        if self.api_version == "v1":
            return f"/api/v1/namespaces/{quote(namespace, safe='')}/{self.plural}"
        group, version = self.api_version.split("/", 1)
        return f"/apis/{group}/{version}/namespaces/{quote(namespace, safe='')}/{self.plural}"

    def item(self, namespace: str, name: str) -> str:
        return f"{self.collection(namespace)}/{quote(name, safe='')}"


RESOURCE_ENDPOINTS = {
    ("v1", "ConfigMap"): ResourceEndpoint("v1", "ConfigMap", "configmaps"),
    ("v1", "PersistentVolumeClaim"): ResourceEndpoint("v1", "PersistentVolumeClaim", "persistentvolumeclaims"),
    ("v1", "Service"): ResourceEndpoint("v1", "Service", "services"),
    ("v1", "ServiceAccount"): ResourceEndpoint("v1", "ServiceAccount", "serviceaccounts"),
    ("apps/v1", "DaemonSet"): ResourceEndpoint("apps/v1", "DaemonSet", "daemonsets"),
    ("apps/v1", "Deployment"): ResourceEndpoint("apps/v1", "Deployment", "deployments"),
    ("keda.sh/v1alpha1", "ScaledObject"): ResourceEndpoint("keda.sh/v1alpha1", "ScaledObject", "scaledobjects"),
    ("networking.k8s.io/v1", "NetworkPolicy"): ResourceEndpoint(
        "networking.k8s.io/v1", "NetworkPolicy", "networkpolicies"
    ),
}
HPA_ENDPOINT = ResourceEndpoint("autoscaling/v2", "HorizontalPodAutoscaler", "horizontalpodautoscalers")
POD_ENDPOINT = ResourceEndpoint("v1", "Pod", "pods")
MODEL_ENDPOINT = ResourceEndpoint(API_VERSION, KIND, "modeldeployments")
LEASE_ENDPOINT = ResourceEndpoint("coordination.k8s.io/v1", "Lease", "leases")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def _metadata(body: Mapping[str, Any]) -> Mapping[str, Any]:
    value = body.get("metadata")
    if not isinstance(value, Mapping):
        raise ControllerError("Kubernetes object metadata is unavailable")
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _required_metadata(body: Mapping[str, Any], field: str) -> str:
    value = _metadata(body).get(field)
    if not isinstance(value, str) or not value:
        raise ControllerError(f"Kubernetes object metadata.{field} is unavailable")
    return value


def _controller_owner_uid(body: Mapping[str, Any]) -> str | None:
    owners = _metadata(body).get("ownerReferences", [])
    if not isinstance(owners, list):
        return None
    matches = [item.get("uid") for item in owners if isinstance(item, Mapping) and item.get("controller") is True]
    return matches[0] if len(matches) == 1 and isinstance(matches[0], str) else None


def _field_managers(body: Mapping[str, Any]) -> list[str]:
    fields = _metadata(body).get("managedFields", [])
    if not isinstance(fields, list):
        return []
    return sorted(
        {manager for item in fields if isinstance(item, Mapping) and isinstance((manager := item.get("manager")), str)}
    )


def _replica_field_managers(body: Mapping[str, Any]) -> list[str]:
    fields = _metadata(body).get("managedFields", [])
    if not isinstance(fields, list):
        return []
    managers: set[str] = set()
    for item in fields:
        if not isinstance(item, Mapping):
            continue
        manager = item.get("manager")
        fields_v1 = item.get("fieldsV1")
        if not isinstance(manager, str) or not isinstance(fields_v1, Mapping):
            continue
        spec_fields = fields_v1.get("f:spec")
        if isinstance(spec_fields, Mapping) and "f:replicas" in spec_fields:
            managers.add(manager)
    return sorted(managers)


def _nonnegative_status_int(status: Mapping[str, Any], field: str, *, zero_when_observed: bool) -> int | None:
    value = status.get(field)
    if isinstance(value, int) and value >= 0:
        return value
    return 0 if zero_when_observed else None


def _project_like(actual: object, desired: object) -> object:
    """Project API defaults/status away while retaining every desired field."""

    if isinstance(desired, Mapping):
        source = actual if isinstance(actual, Mapping) else {}
        return {key: _project_like(source.get(key), value) for key, value in desired.items()}
    if isinstance(desired, list):
        source_list = actual if isinstance(actual, list) else []
        projected = []
        for index, value in enumerate(desired):
            current = source_list[index] if index < len(source_list) else None
            projected.append(_project_like(current, value))
        return projected
    return actual


def _snapshot(body: dict[str, Any], desired: RenderedResource | None = None) -> ResourceSnapshot:
    metadata = _metadata(body)
    api_version = body.get("apiVersion")
    kind = body.get("kind")
    namespace = metadata.get("namespace")
    name = metadata.get("name")
    if not all(isinstance(value, str) and value for value in (api_version, kind, namespace, name)):
        raise ControllerError("discovered resource identity is incomplete")
    desired_digest = (
        canonical_digest(_project_like(body, desired.manifest))
        if desired is not None
        else canonical_digest(
            {
                "apiVersion": api_version,
                "kind": kind,
                "metadata": {
                    "namespace": namespace,
                    "name": name,
                    "uid": metadata.get("uid"),
                    "resourceVersion": metadata.get("resourceVersion"),
                },
            }
        )
    )
    status = _mapping(body.get("status"))
    spec = _mapping(body.get("spec"))
    observed_generation = status.get("observedGeneration")
    observed_generation = (
        observed_generation if isinstance(observed_generation, int) and observed_generation >= 0 else None
    )
    desired_replicas = spec.get("replicas")
    desired_replicas = desired_replicas if isinstance(desired_replicas, int) and desired_replicas >= 0 else None
    zero_when_observed = kind == "Deployment" and observed_generation is not None
    return ResourceSnapshot(
        observed=ObservedResource(
            api_version=str(api_version),
            kind=str(kind),
            namespace=str(namespace),
            name=str(name),
            uid=_required_metadata(body, "uid"),
            digest=desired_digest,
            controller_owner_uid=_controller_owner_uid(body),
            field_managers=_field_managers(body),
            deleting=isinstance(metadata.get("deletionTimestamp"), str),
        ),
        resource_version=_required_metadata(body, "resourceVersion"),
        generation=int(metadata.get("generation", 0)),
        observed_generation=observed_generation,
        desired_replicas=desired_replicas,
        replicas=_nonnegative_status_int(status, "replicas", zero_when_observed=zero_when_observed),
        updated_replicas=_nonnegative_status_int(status, "updatedReplicas", zero_when_observed=zero_when_observed),
        ready_replicas=_nonnegative_status_int(status, "readyReplicas", zero_when_observed=zero_when_observed),
        available_replicas=_nonnegative_status_int(status, "availableReplicas", zero_when_observed=zero_when_observed),
        unavailable_replicas=_nonnegative_status_int(
            status, "unavailableReplicas", zero_when_observed=zero_when_observed
        ),
        replica_field_managers=_replica_field_managers(body),
        raw=body,
    )


def _pod_snapshot(body: Mapping[str, Any]) -> PodSnapshot:
    metadata = _metadata(body)
    name = _required_metadata(body, "name")
    uid = _required_metadata(body, "uid")
    resource_version = _required_metadata(body, "resourceVersion")
    status = _mapping(body.get("status"))
    conditions = status.get("conditions")
    conditions = conditions if isinstance(conditions, list) else []

    def condition_true(condition_type: str) -> bool:
        return any(
            isinstance(condition, Mapping)
            and condition.get("type") == condition_type
            and condition.get("status") == "True"
            for condition in conditions
        )

    container_statuses = status.get("containerStatuses")
    container_statuses = container_statuses if isinstance(container_statuses, list) else []
    containers_started = bool(container_statuses) and all(
        isinstance(item, Mapping)
        and isinstance(item.get("state"), Mapping)
        and isinstance(_mapping(item.get("state")).get("running"), Mapping)
        for item in container_statuses
    )
    phase = status.get("phase")
    return PodSnapshot(
        name=name,
        uid=uid,
        resource_version=resource_version,
        phase=phase if isinstance(phase, str) and phase else "Unknown",
        scheduled=condition_true("PodScheduled"),
        initialized=condition_true("Initialized"),
        containers_started=containers_started,
        ready=condition_true("Ready"),
        deleting=isinstance(metadata.get("deletionTimestamp"), str),
    )


class HttpKubernetesModelClient:
    """Small in-cluster REST adapter with an independent write kill switch."""

    def __init__(
        self,
        *,
        base_url: str,
        token_file: Path,
        ca_file: Path,
        writes_enabled: bool,
        timeout_seconds: float = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_file = token_file
        self.writes_enabled = writes_enabled
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=str(ca_file),
            timeout=httpx.Timeout(timeout_seconds),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        token = self.token_file.read_text().strip()
        if len(token) < 16:
            raise ControllerError("projected Kubernetes token is unavailable")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.client.request(
                method, path, headers=self._headers(kwargs.pop("content_type", None)), **kwargs
            )
        except (OSError, httpx.HTTPError) as exc:
            raise ControllerError("Kubernetes API request failed") from exc
        if response.status_code == 409:
            raise KubernetesConflictError("Kubernetes optimistic concurrency or field ownership conflict")
        if response.status_code >= 400 and response.status_code != 404:
            raise ControllerError(f"Kubernetes API returned HTTP {response.status_code}")
        return response

    def _allow_write(self) -> None:
        if not self.writes_enabled:
            raise WriterDisabledError("model controller Kubernetes writes are disabled")

    async def acquire_or_renew_lease(
        self,
        *,
        namespace: str,
        name: str,
        holder_identity: str,
        token: str | None,
        duration_seconds: int,
    ) -> LeaseFence | None:
        # Lease writes are also disabled by the global kill switch.  An
        # observe-only process can poll objects without impersonating a leader.
        if not self.writes_enabled:
            return None
        now = _utc_now()
        path = LEASE_ENDPOINT.item(namespace, name)
        response = await self._request("GET", path)
        new_token = token or uuid4().hex
        if response.status_code == 404:
            body = {
                "apiVersion": LEASE_ENDPOINT.api_version,
                "kind": LEASE_ENDPOINT.kind,
                "metadata": {"name": name, "namespace": namespace, "annotations": {FENCE_ANNOTATION: new_token}},
                "spec": {
                    "holderIdentity": holder_identity,
                    "leaseDurationSeconds": duration_seconds,
                    "acquireTime": _timestamp(now),
                    "renewTime": _timestamp(now),
                    "leaseTransitions": 0,
                },
            }
            created = await self._request("POST", LEASE_ENDPOINT.collection(namespace), json=body)
            if created.status_code == 409:  # pragma: no cover - normalized above
                return None
            value = created.json()
        else:
            current = response.json()
            metadata = _metadata(current)
            spec = _mapping(current.get("spec"))
            annotations = _mapping(metadata.get("annotations"))
            current_holder = spec.get("holderIdentity")
            current_token = annotations.get(FENCE_ANNOTATION)
            renew_time = _parse_timestamp(spec.get("renewTime"))
            lease_duration = spec.get("leaseDurationSeconds")
            expires = (
                renew_time + timedelta(seconds=lease_duration)
                if renew_time is not None and isinstance(lease_duration, int)
                else now - timedelta(seconds=1)
            )
            ours = current_holder == holder_identity and token is not None and current_token == token
            if not ours and expires > now:
                return None
            if not ours:
                new_token = uuid4().hex
            transitions = int(spec.get("leaseTransitions", 0)) + (0 if ours else 1)
            patch = {
                "metadata": {
                    "resourceVersion": _required_metadata(current, "resourceVersion"),
                    "annotations": {FENCE_ANNOTATION: new_token},
                },
                "spec": {
                    "holderIdentity": holder_identity,
                    "leaseDurationSeconds": duration_seconds,
                    "acquireTime": spec.get("acquireTime") if ours else _timestamp(now),
                    "renewTime": _timestamp(now),
                    "leaseTransitions": transitions,
                },
            }
            updated = await self._request(
                "PATCH", path, content_type="application/merge-patch+json", content=json.dumps(patch).encode()
            )
            value = updated.json()
        return LeaseFence(
            namespace=namespace,
            name=name,
            holder_identity=holder_identity,
            token=new_token,
            resource_version=_required_metadata(value, "resourceVersion"),
            renew_time=now,
            duration_seconds=duration_seconds,
        )

    async def assert_fence(self, fence: LeaseFence) -> None:
        response = await self._request("GET", LEASE_ENDPOINT.item(fence.namespace, fence.name))
        if response.status_code == 404:
            raise FenceLostError("leader Lease disappeared")
        body = response.json()
        metadata = _metadata(body)
        annotations = _mapping(metadata.get("annotations"))
        spec = _mapping(body.get("spec"))
        renew_time = _parse_timestamp(spec.get("renewTime"))
        duration = spec.get("leaseDurationSeconds")
        if (
            spec.get("holderIdentity") != fence.holder_identity
            or annotations.get(FENCE_ANNOTATION) != fence.token
            or renew_time is None
            or not isinstance(duration, int)
            or renew_time + timedelta(seconds=duration) <= _utc_now()
        ):
            raise FenceLostError("leader Lease holder, epoch, or deadline changed")

    async def list_models(self, namespace: str) -> list[dict[str, Any]]:
        response = await self._request("GET", MODEL_ENDPOINT.collection(namespace), params={"limit": "500"})
        body = response.json()
        items = body.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ControllerError("ModelDeployment list response is invalid")
        return items

    async def get_model(self, key: ModelKey) -> dict[str, Any] | None:
        response = await self._request("GET", MODEL_ENDPOINT.item(key.namespace, key.name))
        return None if response.status_code == 404 else response.json()

    @staticmethod
    def _endpoint(api_version: str, kind: str) -> ResourceEndpoint:
        try:
            return RESOURCE_ENDPOINTS[(api_version, kind)]
        except KeyError as exc:
            raise ControllerError("rendered resource GVK is outside the writer allowlist") from exc

    async def _get_resource(self, api_version: str, kind: str, namespace: str, name: str) -> dict[str, Any] | None:
        endpoint = (
            HPA_ENDPOINT
            if (api_version, kind) == (HPA_ENDPOINT.api_version, HPA_ENDPOINT.kind)
            else self._endpoint(api_version, kind)
        )
        response = await self._request("GET", endpoint.item(namespace, name))
        return None if response.status_code == 404 else response.json()

    async def discover(self, *, key: ModelKey, owner_uid: str, render: RenderPlan) -> Discovery:
        desired = {f"{item.api_version}/{item.kind}/{item.namespace}/{item.name}": item for item in render.resources}
        bodies: dict[str, dict[str, Any]] = {}
        selector = f"{MODEL_DEPLOYMENT_LABEL}={bounded_label_value(key.name)}"
        for endpoint in RESOURCE_ENDPOINTS.values():
            response = await self._request(
                "GET", endpoint.collection(key.namespace), params={"labelSelector": selector}
            )
            raw = response.json().get("items")
            if not isinstance(raw, list):
                raise ControllerError("owned-resource list response is invalid")
            for body in raw:
                if not isinstance(body, dict):
                    raise ControllerError("owned-resource list item is invalid")
                normalized = dict(body)
                for field, expected in (
                    ("apiVersion", endpoint.api_version),
                    ("kind", endpoint.kind),
                ):
                    actual = body.get(field)
                    if actual in (None, ""):
                        normalized[field] = expected
                metadata = _metadata(normalized)
                identity = (
                    f"{normalized['apiVersion']}/{normalized['kind']}/"
                    f"{metadata.get('namespace')}/{metadata.get('name')}"
                )
                bodies[identity] = normalized
        # Exact GETs detect a foreign collision even when it deliberately lacks
        # the controller's discovery label.
        for identity, item in desired.items():
            if identity in bodies:
                continue
            body = await self._get_resource(item.api_version, item.kind, item.namespace, item.name)
            if body is not None:
                bodies[identity] = body
        # KEDA creates each HPA as a child of its ScaledObject, not as a direct
        # child of ModelDeployment. Exact GETs for every desired or stale
        # scaler are required for safe multi-segment ownership handoff and
        # foreground cleanup during drain.
        hpa_identities: set[str] = set()
        scaler_names = sorted(
            {
                str(_metadata(body).get("name"))
                for body in bodies.values()
                if body.get("apiVersion") == "keda.sh/v1alpha1"
                and body.get("kind") == "ScaledObject"
                and isinstance(_metadata(body).get("name"), str)
            }
            | {
                item.name
                for item in render.resources
                if item.api_version == "keda.sh/v1alpha1" and item.kind == "ScaledObject"
            }
        )
        for scaler_name in scaler_names:
            hpa_name = f"keda-hpa-{scaler_name}"
            hpa_identity = f"{HPA_ENDPOINT.api_version}/{HPA_ENDPOINT.kind}/{key.namespace}/{hpa_name}"
            hpa = await self._get_resource(
                HPA_ENDPOINT.api_version,
                HPA_ENDPOINT.kind,
                key.namespace,
                hpa_name,
            )
            if hpa is not None:
                bodies[hpa_identity] = hpa
                hpa_identities.add(hpa_identity)
        pod_response = await self._request(
            "GET",
            POD_ENDPOINT.collection(key.namespace),
            params={"labelSelector": selector, "limit": "10000"},
        )
        raw_pods = pod_response.json().get("items")
        if not isinstance(raw_pods, list) or not all(isinstance(item, dict) for item in raw_pods):
            raise ControllerError("runtime Pod list response is invalid")
        snapshots = [_snapshot(body, desired.get(identity)) for identity, body in sorted(bodies.items())]
        if any(
            item.observed.controller_owner_uid not in {None, owner_uid}
            and item.observed.identity not in desired
            and item.observed.identity not in hpa_identities
            for item in snapshots
        ):
            raise ControllerError("label-selected inventory contains another controller owner")
        pods = sorted((_pod_snapshot(item) for item in raw_pods), key=lambda item: (item.name, item.uid))
        return Discovery(resources=snapshots, pods=pods, complete=True)

    async def apply_resource(
        self,
        resource: RenderedResource,
        *,
        owner_uid: str,
        fence: LeaseFence,
    ) -> ResourceSnapshot:
        self._allow_write()
        if resource.field_manager != FIELD_MANAGER or resource.force_conflicts:
            raise ControllerError("renderer requested an unsafe field-manager policy")
        manifest = copy.deepcopy(resource.manifest)
        if _controller_owner_uid(manifest) != owner_uid:
            raise ControllerError("rendered resource is not controller-owned by the exact CR UID")
        endpoint = self._endpoint(resource.api_version, resource.kind)
        current = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if current is not None:
            current_owner = _controller_owner_uid(current)
            if current_owner not in {None, owner_uid}:
                raise KubernetesConflictError("resource has a foreign controller owner")
            manifest.setdefault("metadata", {})["resourceVersion"] = _required_metadata(current, "resourceVersion")
        await self.assert_fence(fence)
        response = await self._request(
            "PATCH",
            endpoint.item(resource.namespace, resource.name),
            params={"fieldManager": FIELD_MANAGER, "force": "false", "fieldValidation": "Strict"},
            content_type="application/apply-patch+yaml",
            content=json.dumps(manifest, separators=(",", ":")).encode(),
        )
        applied = response.json()
        # Read-after-write is mandatory: an admission controller may have
        # mutated fields, and a successful HTTP response is not ownership proof.
        reread = await self._get_resource(resource.api_version, resource.kind, resource.namespace, resource.name)
        if reread is None or _required_metadata(reread, "uid") != _required_metadata(applied, "uid"):
            raise ControllerError("applied resource failed read-after-write UID verification")
        if _controller_owner_uid(reread) != owner_uid:
            raise KubernetesConflictError("applied resource did not retain the exact controller owner")
        return _snapshot(reread, resource)

    @staticmethod
    def _parse_identity(identity: str) -> tuple[str, str, str, str]:
        try:
            api_version, kind, namespace, name = identity.rsplit("/", 3)
        except ValueError as exc:
            raise ControllerError("resource identity is malformed") from exc
        if not all((api_version, kind, namespace, name)):
            raise ControllerError("resource identity is incomplete")
        return api_version, kind, namespace, name

    async def delete_resource(
        self,
        identity: str,
        *,
        owner_uid: str,
        fence: LeaseFence,
    ) -> bool:
        self._allow_write()
        api_version, kind, namespace, name = self._parse_identity(identity)
        endpoint = self._endpoint(api_version, kind)
        current = await self._get_resource(api_version, kind, namespace, name)
        if current is None:
            return False
        if _controller_owner_uid(current) != owner_uid:
            raise KubernetesConflictError("refusing to delete a resource without the exact controller owner")
        await self.assert_fence(fence)
        response = await self._request(
            "DELETE",
            endpoint.item(namespace, name),
            json={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Foreground" if kind == "ScaledObject" else "Background",
                "preconditions": {
                    "uid": _required_metadata(current, "uid"),
                    "resourceVersion": _required_metadata(current, "resourceVersion"),
                },
            },
        )
        return response.status_code != 404

    async def set_finalizer(
        self,
        key: ModelKey,
        *,
        owner_uid: str,
        present: bool,
        fence: LeaseFence,
    ) -> None:
        self._allow_write()
        current = await self.get_model(key)
        if current is None or _required_metadata(current, "uid") != owner_uid:
            raise KubernetesConflictError("ModelDeployment UID changed before finalizer write")
        metadata = _metadata(current)
        finalizers = _string_list(metadata.get("finalizers"))
        updated = (
            list(dict.fromkeys([*finalizers, FINALIZER]))
            if present
            else [value for value in finalizers if value != FINALIZER]
        )
        if updated == finalizers:
            return
        if present and metadata.get("deletionTimestamp") is not None:
            raise KubernetesConflictError("cannot add the cleanup finalizer after deletion started")
        await self.assert_fence(fence)
        await self._request(
            "PATCH",
            MODEL_ENDPOINT.item(key.namespace, key.name),
            content_type="application/merge-patch+json",
            content=json.dumps(
                {"metadata": {"resourceVersion": _required_metadata(current, "resourceVersion"), "finalizers": updated}}
            ).encode(),
        )

    async def patch_status(
        self,
        key: ModelKey,
        *,
        owner_uid: str,
        generation: int,
        status: dict[str, Any],
        fence: LeaseFence,
    ) -> bool:
        self._allow_write()
        current = await self.get_model(key)
        if current is None or _required_metadata(current, "uid") != owner_uid:
            raise KubernetesConflictError("ModelDeployment UID changed before status write")
        metadata = _metadata(current)
        if int(metadata.get("generation", 0)) != generation:
            raise KubernetesConflictError("ModelDeployment generation changed before status write")
        current_status = _mapping(current.get("status"))
        if int(current_status.get("observedGeneration", 0)) > generation:
            return False
        await self.assert_fence(fence)
        body = {
            "apiVersion": API_VERSION,
            "kind": KIND,
            "metadata": {
                "name": key.name,
                "namespace": key.namespace,
                "resourceVersion": _required_metadata(current, "resourceVersion"),
            },
            "status": status,
        }
        await self._request(
            "PATCH",
            f"{MODEL_ENDPOINT.item(key.namespace, key.name)}/status",
            params={"fieldManager": STATUS_FIELD_MANAGER, "force": "false", "fieldValidation": "Strict"},
            content_type="application/apply-patch+yaml",
            content=json.dumps(body, separators=(",", ":")).encode(),
        )
        return True


class BoundedKeyQueue:
    """Deduplicating bounded queue; overload converges on the next list pass."""

    def __init__(self, maximum: int) -> None:
        self._queue: asyncio.Queue[ModelKey] = asyncio.Queue(maxsize=maximum)
        self._pending: set[str] = set()
        self.dropped = 0

    def put(self, key: ModelKey) -> bool:
        if key.text in self._pending:
            return True
        try:
            self._queue.put_nowait(key)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        self._pending.add(key.text)
        return True

    async def get(self) -> ModelKey:
        return await self._queue.get()

    def done(self, key: ModelKey) -> None:
        self._pending.discard(key.text)
        self._queue.task_done()

    @property
    def depth(self) -> int:
        return self._queue.qsize()


class ControllerMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        self.leader = Gauge(
            "fs2_model_controller_leader", "Whether this replica owns the live Lease", registry=self.registry
        )
        self.queue_depth = Gauge(
            "fs2_model_controller_queue_depth", "Bounded reconcile queue depth", registry=self.registry
        )
        self.queue_dropped = Counter(
            "fs2_model_controller_queue_dropped_total",
            "Keys deferred after bounded queue saturation",
            registry=self.registry,
        )
        self.reconciles = Counter(
            "fs2_model_controller_reconciles_total",
            "Reconciles by bounded outcome",
            ("outcome",),
            registry=self.registry,
        )
        self.duration = Histogram(
            "fs2_model_controller_reconcile_duration_seconds",
            "End-to-end reconcile duration",
            registry=self.registry,
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
        )
        self.modelexpress_configured = Gauge(
            "fs2_model_controller_modelexpress_configured",
            "Whether an exact ModelExpress client binding is configured for a model and pool",
            ("model", "pool", "deployment_mode", "runtime_adapter", "transport_mode"),
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


class ControllerHealth:
    def __init__(self, *, reconcile_error_threshold: int = 3) -> None:
        self.live = True
        self.leader = False
        self.cycle_ready = False
        self.last_cycle_error: str | None = None
        self.last_reconcile_error: str | None = None
        self.reconcile_error_threshold = reconcile_error_threshold
        self.reconcile_errors: dict[str, int] = {}

    @property
    def ready(self) -> bool:
        return self.cycle_ready and all(
            count < self.reconcile_error_threshold for count in self.reconcile_errors.values()
        )

    @property
    def last_error(self) -> str | None:
        return self.last_cycle_error or self.last_reconcile_error

    def cycle_succeeded(self) -> None:
        self.cycle_ready = True
        self.last_cycle_error = None

    def cycle_failed(self, error: str) -> None:
        self.cycle_ready = False
        self.last_cycle_error = error

    def reconcile_succeeded(self, key: ModelKey) -> None:
        self.reconcile_errors.pop(key.text, None)
        self.last_reconcile_error = None

    def reconcile_failed(self, key: ModelKey, error: str) -> None:
        self.reconcile_errors[key.text] = self.reconcile_errors.get(key.text, 0) + 1
        self.last_reconcile_error = error


def _old_conditions(status: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    value = status.get("conditions")
    if not isinstance(value, list):
        return {}
    return {
        str(item["type"]): item for item in value if isinstance(item, Mapping) and isinstance(item.get("type"), str)
    }


def _condition(
    condition_type: str,
    value: Literal["True", "False", "Unknown"],
    reason: str,
    message: str,
    generation: int,
    *,
    previous: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    unchanged = previous is not None and previous.get("status") == value and previous.get("reason") == reason
    transition = previous.get("lastTransitionTime") if unchanged and previous is not None else None
    return {
        "type": condition_type,
        "status": value,
        "observedGeneration": generation,
        "reason": reason,
        "message": message,
        "lastTransitionTime": transition if isinstance(transition, str) else _timestamp(now),
    }


def _known_total(values: list[int | None], *, empty: int = 0) -> int | None:
    if not values:
        return empty
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _resource_snapshot(
    discovery: Discovery, api_version: str, kind: str, namespace: str, name: str
) -> ResourceSnapshot | None:
    identity = f"{api_version}/{kind}/{namespace}/{name}"
    return next((item for item in discovery.resources if item.observed.identity == identity), None)


def _rendered_identity(resource: RenderedResource) -> str:
    return f"{resource.api_version}/{resource.kind}/{resource.namespace}/{resource.name}"


def _autoscaler_pairs(
    render: RenderPlan | None,
) -> list[tuple[RenderedResource, RenderedResource]]:
    """Return every scaler with its unique rendered Deployment target."""

    if render is None:
        return []
    pairs: list[tuple[RenderedResource, RenderedResource]] = []
    target_identities: set[str] = set()
    for scaler in sorted(
        (item for item in render.resources if item.kind == "ScaledObject"),
        key=lambda item: (item.namespace, item.name),
    ):
        target = _mapping(_mapping(scaler.manifest.get("spec")).get("scaleTargetRef"))
        api_version = target.get("apiVersion")
        kind = target.get("kind")
        name = target.get("name")
        matches = [
            item
            for item in render.resources
            if item.api_version == api_version
            and item.kind == kind
            and item.name == name
            and item.namespace == scaler.namespace
        ]
        if len(matches) != 1 or api_version != "apps/v1" or kind != "Deployment":
            raise ControllerError("ScaledObject target is not the exact rendered Deployment")
        identity = _rendered_identity(matches[0])
        if identity in target_identities:
            raise ControllerError("more than one ScaledObject targets the same Deployment")
        target_identities.add(identity)
        pairs.append((scaler, matches[0]))
    return pairs


def _with_deployment_replicas(resource: RenderedResource, replicas: int) -> RenderedResource:
    manifest = copy.deepcopy(resource.manifest)
    spec = manifest.get("spec")
    if resource.kind != "Deployment" or not isinstance(spec, dict):
        raise ControllerError("autoscaler bootstrap target is not a Deployment")
    spec["replicas"] = replicas
    return resource.model_copy(update={"manifest": manifest, "digest": canonical_digest(manifest)})


def _autoscaler_installed(
    scaler: RenderedResource,
    target: RenderedResource,
    discovery: Discovery,
    owner_uid: str,
) -> bool:
    live_target = _resource_snapshot(
        discovery,
        target.api_version,
        target.kind,
        target.namespace,
        target.name,
    )
    if live_target is None:
        return False
    live_scaler = _resource_snapshot(discovery, scaler.api_version, scaler.kind, scaler.namespace, scaler.name)
    if (
        live_scaler is None
        or live_scaler.observed.deleting
        or live_scaler.observed.controller_owner_uid != owner_uid
        or FIELD_MANAGER not in live_scaler.observed.field_managers
        or live_scaler.observed.digest != scaler.digest
    ):
        return False
    expected_hpa_name = f"keda-hpa-{scaler.name}"
    scaler_status = _mapping(live_scaler.raw.get("status"))
    scaler_conditions = scaler_status.get("conditions")
    if (
        scaler_status.get("hpaName") != expected_hpa_name
        or not isinstance(scaler_conditions, list)
        or not any(
            isinstance(condition, Mapping) and condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in scaler_conditions
        )
    ):
        return False
    hpa = _resource_snapshot(
        discovery,
        HPA_ENDPOINT.api_version,
        HPA_ENDPOINT.kind,
        scaler.namespace,
        expected_hpa_name,
    )
    if hpa is None or hpa.observed.deleting or hpa.observed.controller_owner_uid != live_scaler.observed.uid:
        return False
    hpa_status = _mapping(hpa.raw.get("status"))
    hpa_conditions = hpa_status.get("conditions")
    hpa_conditions = hpa_conditions if isinstance(hpa_conditions, list) else []
    able_to_scale = any(
        isinstance(condition, Mapping) and condition.get("type") == "AbleToScale" and condition.get("status") == "True"
        for condition in hpa_conditions
    )
    scaling_active = any(
        isinstance(condition, Mapping)
        and condition.get("type") == "ScalingActive"
        and condition.get("status") == "True"
        for condition in hpa_conditions
    )
    keda_zero_idle = (
        live_target.desired_replicas == 0
        and live_target.replicas == 0
        and hpa_status.get("desiredReplicas") == 0
        and any(
            isinstance(condition, Mapping)
            and condition.get("type") == "ScalingActive"
            and condition.get("status") == "False"
            and condition.get("reason") == "ScalingDisabled"
            for condition in hpa_conditions
        )
        and any(
            isinstance(condition, Mapping)
            and condition.get("type") == "HPAActive"
            and condition.get("status") == "True"
            and condition.get("reason") == "ScalingDisabled"
            for condition in scaler_conditions
        )
    )
    target_ref = _mapping(_mapping(hpa.raw.get("spec")).get("scaleTargetRef"))
    generation_observed = hpa_status.get("observedGeneration")
    generation_converged = generation_observed == hpa.generation or (
        generation_observed is None and hpa.observed_generation is None and hpa.generation == 0
    )
    return (
        able_to_scale
        and (scaling_active or keda_zero_idle)
        and generation_converged
        and target_ref.get("apiVersion") == target.api_version
        and target_ref.get("kind") == target.kind
        and target_ref.get("name") == target.name
    )


def _autoscaler_handoff_complete(render: RenderPlan | None, discovery: Discovery, owner_uid: str) -> bool:
    return all(
        (
            (
                live_target := _resource_snapshot(
                    discovery,
                    target.api_version,
                    target.kind,
                    target.namespace,
                    target.name,
                )
            )
            is not None
            and _autoscaler_installed(scaler, target, discovery, owner_uid)
            and FIELD_MANAGER not in live_target.replica_field_managers
        )
        for scaler, target in _autoscaler_pairs(render)
    )


def _autoscaler_resources_present(discovery: Discovery) -> bool:
    return any(item.observed.kind in {"ScaledObject", HPA_ENDPOINT.kind} for item in discovery.resources)


def _observed_hot_floor(discovery: Discovery) -> int:
    """Return the exact fixed-hot boundary already owned by this model.

    A drain may preserve autoscaled replicas through KEDA's minimum, but fixed
    hot Deployments have a separate replica owner. Losing this boundary while
    active operations exist would replace or delete serving Pods mid-drain.
    """

    total = 0
    for item in discovery.resources:
        if item.observed.kind != "Deployment":
            continue
        annotations = _mapping(_metadata(item.raw).get("annotations"))
        if annotations.get(WORKLOAD_ROLE_ANNOTATION) != "hot":
            continue
        if item.desired_replicas is None:
            raise ControllerError("observed hot Deployment replica count is unavailable during drain")
        total += item.desired_replicas
    return total


def _deployment_rollout_complete(item: ResourceSnapshot) -> bool:
    desired = item.desired_replicas
    return (
        desired is not None
        and item.observed_generation == item.generation
        and item.replicas == desired
        and item.updated_replicas == desired
        and item.ready_replicas == desired
        and item.available_replicas == desired
        and item.unavailable_replicas == 0
    )


def _pod_phase_counts(pods: list[PodSnapshot]) -> dict[str, int]:
    active = [pod for pod in pods if not pod.deleting]
    return {
        "admitted": sum(pod.scheduled for pod in active),
        "nodePending": sum(not pod.scheduled and pod.phase not in {"Failed", "Succeeded"} for pod in active),
        "localizing": sum(
            pod.scheduled and not pod.initialized and pod.phase not in {"Failed", "Succeeded"} for pod in active
        ),
        "runtimeStarting": sum(
            pod.initialized
            and not pod.containers_started
            and not pod.ready
            and pod.phase not in {"Failed", "Succeeded"}
            for pod in active
        ),
        "warming": sum(
            pod.initialized and pod.containers_started and not pod.ready and pod.phase not in {"Failed", "Succeeded"}
            for pod in active
        ),
        "ready": sum(pod.ready for pod in active),
        "failed": sum(pod.phase == "Failed" for pod in active),
    }


def _cache_status(
    *,
    spec: ModelDeploymentSpec,
    phase_counts: Mapping[str, int],
    desired_replicas: int | None,
    previous_status: Mapping[str, Any],
    observed_at: datetime,
) -> dict[str, Any] | None:
    if spec.cache.tier.value == "Disabled":
        return None
    tier = spec.cache.tier.value
    digest = spec.artifact.manifest_digest
    previous = _mapping(previous_status.get("cache"))
    if phase_counts["failed"] > 0:
        return {"state": "Failed", "tier": tier, "digest": digest, "observedAt": _timestamp(observed_at)}
    if phase_counts["localizing"] > 0:
        return {
            "state": "Localizing",
            "tier": tier,
            "digest": digest,
            "observedAt": _timestamp(observed_at),
        }
    if phase_counts["ready"] > 0:
        return {"state": "Cached", "tier": tier, "digest": digest, "observedAt": _timestamp(observed_at)}
    if (
        desired_replicas == 0
        and previous.get("state") == "Cached"
        and previous.get("tier") == tier
        and previous.get("digest") == digest
        and isinstance(previous.get("observedAt"), str)
    ):
        return dict(previous)
    return {"state": "Unknown", "tier": tier, "digest": digest}


def _previous_automatic_state(previous: Mapping[str, Any]) -> AutomaticFastStartState | None:
    detail = _mapping(previous.get("automatic"))
    assigned = previous.get("assignedLevel")
    if not isinstance(assigned, str) or assigned not in {level.value for level in FastStartLevel}:
        return None
    pending = detail.get("pendingLevel")
    pending_level = FastStartLevel(pending) if pending in {level.value for level in FastStartLevel} else None
    pending_since = _parse_timestamp(detail.get("pendingSince"))
    last_transition = _parse_timestamp(detail.get("lastTransitionAt"))
    wins = detail.get("consecutiveWins", 0)
    if not isinstance(wins, int) or wins < 0 or (pending_level is None) != (pending_since is None):
        return None
    try:
        return AutomaticFastStartState(
            assigned_level=FastStartLevel(assigned),
            pending_level=pending_level,
            pending_since=pending_since,
            consecutive_wins=wins,
            last_transition_at=last_transition,
        )
    except ValueError:
        return None


def _automatic_fast_start_assessment(
    *,
    spec: ModelDeploymentSpec,
    envelope: InfrastructureEnvelope | None,
    assessment: FastStartAssessment,
    converged: bool,
    previous: Mapping[str, Any],
    history: tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None,
    now: datetime,
) -> tuple[FastStartAssessment, FastStartAutomaticStatus | None]:
    if spec.fast_start.mode is not FastStartMode.AUTOMATIC or envelope is None:
        return assessment, None
    assert spec.fast_start.minimum_level is not None and spec.fast_start.maximum_level is not None
    previous_detail_raw = _mapping(previous.get("automatic"))
    previous_detail: FastStartAutomaticStatus | None = None
    if previous_detail_raw:
        try:
            previous_detail = FastStartAutomaticStatus.model_validate(previous_detail_raw)
        except ValueError:
            previous_detail = None
    prior_state = _previous_automatic_state(previous)
    if (
        previous_detail is not None
        and prior_state is not None
        and now - previous_detail.evaluated_at < timedelta(minutes=5)
        and spec.fast_start.minimum_level.rank <= prior_state.assigned_level.rank <= spec.fast_start.maximum_level.rank
        and prior_state.assigned_level.rank <= assessment.qualified_level.rank
        and converged
        and history is not None
    ):
        retained_level = prior_state.assigned_level
        qualification = FastStartQualification(
            state=(
                FastStartQualificationState.NO_TARGET
                if retained_level is FastStartLevel.OFF
                else FastStartQualificationState.QUALIFIED
            ),
            reason=previous_detail.reason,
            message=(
                "automatic policy assigned Off; no model-start target is claimed"
                if retained_level is FastStartLevel.OFF
                else f"automatic policy retains qualified {retained_level.value} until its next five-minute evaluation"
            ),
        )
        return (
            FastStartAssessment.model_validate(
                {
                    **assessment.model_dump(),
                    "assigned_level": retained_level,
                    "target_seconds": retained_level.target_seconds,
                    "qualification": qualification,
                }
            ),
            previous_detail,
        )
    pool_paths = [pool.paths for pool in assessment.pools]
    common_mechanisms = (
        set.intersection(*({path.mechanism for path in paths} for paths in pool_paths))
        if pool_paths and all(pool_paths)
        else set()
    )
    paths: list[FastStartPath] = []
    for mechanism in sorted(common_mechanisms):
        selected_paths = [
            min(
                (path for path in pool.paths if path.mechanism == mechanism),
                key=lambda path: (
                    -path.qualified_level.rank,
                    float("inf")
                    if path.model_start is None or path.model_start.p95_seconds is None
                    else path.model_start.p95_seconds,
                    path.compatibility_tuple_digest,
                ),
            )
            for pool in assessment.pools
        ]
        binding = min(
            selected_paths,
            key=lambda path: (
                path.qualified_level.rank,
                -(
                    0.0
                    if path.model_start is None or path.model_start.p95_seconds is None
                    else path.model_start.p95_seconds
                ),
            ),
        )
        statistics = binding.model_start
        paths.append(
            FastStartPath(
                mechanism_id=mechanism,
                qualified_level=binding.qualified_level,
                ready=converged,
                qualification_current=statistics is not None,
                qualified_p95_model_start_seconds=(None if statistics is None else statistics.p95_seconds),
                successful_attempts=(0 if statistics is None else statistics.sample_count - statistics.failed_count),
                failed_attempts=0 if statistics is None else statistics.failed_count,
                hourly_cost=envelope.fast_start_mechanism_hourly_costs.get(mechanism, 0.0),
            )
        )
    if not paths:
        selected_mechanisms = sorted(
            {pool.selected_mechanism for pool in assessment.pools if pool.selected_mechanism is not None}
        )
        mechanism_id = "+".join(selected_mechanisms)[:128] or "conventional"
        statistics = assessment.model_start
        paths.append(
            FastStartPath(
                mechanism_id=mechanism_id,
                qualified_level=assessment.qualified_level,
                ready=converged,
                qualification_current=statistics is not None,
                qualified_p95_model_start_seconds=(None if statistics is None else statistics.p95_seconds),
                successful_attempts=(0 if statistics is None else statistics.sample_count - statistics.failed_count),
                failed_attempts=0 if statistics is None else statistics.failed_count,
                hourly_cost=sum(
                    envelope.fast_start_mechanism_hourly_costs.get(name, 0.0) for name in selected_mechanisms
                ),
            )
        )
    policy = AutomaticFastStartPolicy(
        minimum_level=spec.fast_start.minimum_level,
        maximum_level=spec.fast_start.maximum_level,
        wait_second_value=envelope.fast_start_wait_second_value,
        fallback_policy=spec.fast_start.fallback_policy,
    )
    short_history = None if history is None else history[0]
    long_history = None if history is None else history[1]
    decision = evaluate_automatic_fast_start(
        policy=policy,
        paths=paths,
        short_history=short_history,
        long_history=long_history,
        prior_state=prior_state,
        now=now,
    )
    chosen_mechanism = decision.mechanism_id or decision.fallback_mechanism_id
    selected_statistics = assessment.model_start
    selected_capacity_wait = assessment.capacity_wait
    selected_end_to_end = assessment.end_to_end
    selected_identity_digest = assessment.selected_identity_digest
    if chosen_mechanism is not None:
        selected_pool_paths: list[FastStartPathAssessment] = []
        for pool in assessment.pools:
            candidates = [path for path in pool.paths if path.mechanism == chosen_mechanism]
            if not candidates:
                selected_pool_paths = []
                break
            selected_pool_paths.append(
                min(
                    candidates,
                    key=lambda path: (
                        -path.qualified_level.rank,
                        float("inf")
                        if path.model_start is None or path.model_start.p95_seconds is None
                        else path.model_start.p95_seconds,
                        path.compatibility_tuple_digest,
                    ),
                )
            )
        if selected_pool_paths:
            binding_path = min(
                selected_pool_paths,
                key=lambda path: (
                    path.qualified_level.rank,
                    -(
                        0.0
                        if path.model_start is None or path.model_start.p95_seconds is None
                        else path.model_start.p95_seconds
                    ),
                ),
            )
            selected_statistics = binding_path.model_start
            selected_capacity_wait = binding_path.capacity_wait
            selected_end_to_end = binding_path.end_to_end
            selected_identity_digest = binding_path.identity_digest
    selected = decision.assigned_level if decision.satisfied else decision.fallback_level
    if selected is None:
        qualification = FastStartQualification(
            state=FastStartQualificationState.UNQUALIFIED,
            reason="AutomaticTargetUnavailable",
            message=f"automatic policy {decision.reason.value} has no qualified path inside the required bounds",
        )
    elif selected is FastStartLevel.OFF and decision.satisfied:
        qualification = FastStartQualification(
            state=FastStartQualificationState.NO_TARGET,
            reason=decision.reason.value,
            message="automatic policy assigned Off; no model-start target is claimed",
        )
    elif selected.rank < spec.fast_start.minimum_level.rank:
        qualification = FastStartQualification(
            state=FastStartQualificationState.FALLBACK,
            reason=decision.reason.value,
            message=f"automatic policy fell back to qualified {selected.value} below the configured minimum",
        )
    else:
        qualification = FastStartQualification(
            state=FastStartQualificationState.QUALIFIED,
            reason=decision.reason.value,
            message=(f"automatic policy assigned qualified {selected.value} from rolling demand and mechanism cost"),
        )
    updated = FastStartAssessment.model_validate(
        {
            **assessment.model_dump(),
            "assigned_level": selected,
            "target_seconds": None if selected is None else selected.target_seconds,
            "qualification": qualification,
            "model_start": selected_statistics,
            "capacity_wait": selected_capacity_wait,
            "end_to_end": selected_end_to_end,
            "selected_identity_digest": selected_identity_digest,
        }
    )
    automatic = FastStartAutomaticStatus(
        reason=decision.reason.value,
        evaluated_at=now,
        history_complete=history is not None,
        mechanism_id=decision.mechanism_id or decision.fallback_mechanism_id,
        score=decision.score,
        pending_level=decision.state.pending_level,
        pending_since=decision.state.pending_since,
        consecutive_wins=decision.state.consecutive_wins,
        last_transition_at=decision.state.last_transition_at,
        short_window_requests=0 if short_history is None else short_history.request_count,
        short_window_cold_activations=0 if short_history is None else short_history.cold_activation_count,
        short_window_idle_gap_episodes=0 if short_history is None else short_history.idle_gap_episode_count,
        long_window_requests=0 if long_history is None else long_history.request_count,
        long_window_cold_activations=0 if long_history is None else long_history.cold_activation_count,
        long_window_idle_gap_episodes=0 if long_history is None else long_history.idle_gap_episode_count,
    )
    return updated, automatic


def _fast_start_status(
    *,
    spec: ModelDeploymentSpec,
    envelope: InfrastructureEnvelope | None,
    assessment: FastStartAssessment | None,
    converged: bool,
    ready_replicas: int | None,
    previous_status: Mapping[str, Any],
    history: tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None,
    now: datetime,
    host_residency_pool_refs: set[str] | None = None,
) -> FastStartStatus | None:
    """Add what only observed runtime can tell to the deterministic policy outcome.

    The effective level is claimed only once the desired render has converged.
    Until then the previously effective level, if any, is carried forward so a
    rollout never advertises a startup class it has not reached.
    """

    if assessment is None:
        return None
    previous = _mapping(previous_status.get("fastStart"))
    assessment, automatic = _automatic_fast_start_assessment(
        spec=spec,
        envelope=envelope,
        assessment=assessment,
        converged=converged,
        previous=previous,
        history=history,
        now=now,
    )
    effective: FastStartLevel | None = None
    effective_identity_digest: str | None = None
    if converged and assessment.assigned_level is not None:
        effective = assessment.assigned_level
        effective_identity_digest = assessment.selected_identity_digest
    else:
        carried = previous.get("effectiveLevel")
        carried_identity = previous.get("effectiveIdentityDigest")
        if (
            isinstance(carried, str)
            and carried in {level.value for level in FastStartLevel}
            and isinstance(carried_identity, str)
            and carried_identity == assessment.selected_identity_digest
        ):
            effective = FastStartLevel(carried)
            effective_identity_digest = carried_identity
    mechanisms: dict[str, FastStartMechanismStatus] = {}
    qualification = envelope.qualifications.get(spec.model_ref) if envelope is not None else None
    if qualification is not None and qualification.model_express is not None:
        configured = qualification.model_express
        mechanisms["modelexpress"] = FastStartMechanismStatus(
            state="Configured" if converged else "Pending",
            config_digest=configured.config_digest,
            deployment_mode=configured.deployment_mode,
            endpoint=configured.endpoint,
            metadata_backend=configured.metadata_backend,
            runtime_adapter=configured.runtime_adapter,
            client_package_version=configured.client_package_version,
            coordinator_network_type=configured.coordinator_network_type,
            coordinator_namespace=configured.coordinator_namespace,
            coordinator_pod_labels=configured.coordinator_pod_labels,
            coordinator_cidrs=configured.coordinator_cidrs,
            pool_refs=configured.pool_refs,
            pool_transports={
                pool_ref: FastStartMechanismPoolTransport(
                    mode=transport.mode,
                    rdma_resource_name=transport.rdma_resource_name,
                    rdma_resource_quantity=transport.rdma_resource_quantity,
                    nixl_backend=transport.nixl_backend,
                    rdma_nic_pin=transport.rdma_nic_pin,
                )
                for pool_ref, transport in configured.pool_transports.items()
            },
            configuration_observed=converged,
            # Upstream 0.5.1 does not expose a qualified per-ModelDeployment
            # transfer-path record. Keep these values unavailable rather than
            # inferring them from readiness.
            telemetry_state="Unavailable",
        )
    cache_mechanisms: dict[str, FastStartCacheMechanismStatus] = {}
    if envelope is not None:
        placement_pools = {
            pool_ref: envelope.pools[pool_ref].node_selector
            for pool_ref in sorted(spec.placement.pool_refs)
            if pool_ref in envelope.pools
        }
        placement_pool_max_nodes = {
            pool_ref: envelope.pools[pool_ref].max_nodes
            for pool_ref in sorted(spec.placement.pool_refs)
            if pool_ref in envelope.pools
        }
        storage_contract_digests = {
            item.pool_ref: item.storage_contract_digest
            for item in (qualification.fast_start_runtime_contracts if qualification is not None else [])
            if item.runtime.runtime_image == spec.runtime.image
            and item.runtime.template_digest == spec.runtime.template_ref.digest
        }
        declarations = {}
        if qualification is not None:
            for candidate in DECLARED_MECHANISMS:
                declaration = qualification.mechanism_declaration(candidate)
                if declaration is not None:
                    declarations[candidate] = declaration
        cache_mechanisms = project_cache_mechanisms(
            selected=spec.cache.mechanism,
            declarations=declarations,
            pools=placement_pools,
            pool_max_nodes=placement_pool_max_nodes,
            host_residency_pool_refs=host_residency_pool_refs,
            storage_contract_digests=storage_contract_digests,
            converged=converged,
            configured_hot_replicas=effective_hot_floor(spec.availability, at=now),
            configured_max_replicas=spec.availability.max_replicas,
            mechanism_config_digest=mechanism_config_digest,
        )
    return FastStartStatus(
        **assessment.model_dump(),
        effective_level=effective,
        effective_identity_digest=effective_identity_digest,
        hot=None if ready_replicas is None else ready_replicas > 0,
        automatic=automatic,
        mechanisms=mechanisms,
        cache_mechanisms=cache_mechanisms,
    )


def build_status(
    *,
    spec: ModelDeploymentSpec,
    owner_uid: str,
    generation: int,
    plan: ReconcilePlan,
    discovery: Discovery,
    previous_status: Mapping[str, Any],
    drain: DrainObservation | None,
    now: datetime | None = None,
    envelope: InfrastructureEnvelope | None = None,
    fast_start_history: tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None = None,
) -> dict[str, Any]:
    """Project only observed state; desired state alone never becomes Ready."""

    observed_at = now or _utc_now()
    previous_observed_at = _parse_timestamp(previous_status.get("lastReconcileTime"))
    if previous_observed_at is not None and observed_at <= previous_observed_at:
        observed_at = previous_observed_at + timedelta(microseconds=1)
    old = _old_conditions(previous_status)
    deployments = [item for item in discovery.resources if item.observed.kind == "Deployment"]
    desired = _known_total([item.desired_replicas for item in deployments])
    replicas = _known_total([item.replicas for item in deployments])
    ready = _known_total([item.ready_replicas for item in deployments])
    available = _known_total([item.available_replicas for item in deployments])
    phase_counts = _pod_phase_counts(discovery.pods)
    rollout_complete = bool(deployments) and all(_deployment_rollout_complete(item) for item in deployments)
    autoscaler_handoff_complete = _autoscaler_handoff_complete(plan.render, discovery, owner_uid)
    desired_resources = {
        f"{item.api_version}/{item.kind}/{item.namespace}/{item.name}": item
        for item in (plan.render.resources if plan.render is not None else [])
    }
    observed_by_identity = {item.observed.identity: item.observed for item in discovery.resources}
    converged = bool(desired_resources) and all(
        identity in observed_by_identity
        and observed_by_identity[identity].controller_owner_uid == owner_uid
        and FIELD_MANAGER in observed_by_identity[identity].field_managers
        and observed_by_identity[identity].digest == resource.digest
        for identity, resource in desired_resources.items()
    )

    terminal = "Progressing"
    reason = "Reconciling"
    message = "controller is reconciling the observed resource inventory"
    phase = "Desired"
    if plan.action is ReconcileAction.INFRASTRUCTURE_REQUIRED:
        terminal, reason, phase = "InfrastructureRequired", "TerraformEnvelopeRequired", "InfrastructureRequired"
        message = "requested placement or capability is outside the Terraform-owned envelope"
    elif plan.action is ReconcileAction.REJECT:
        terminal, reason, phase = "Failed", "ValidationRejected", "Failed"
        message = "desired state failed controller validation or ownership checks"
    elif spec.lifecycle.desired_state in {DesiredState.DRAINING, DesiredState.DISABLED} or drain is not None:
        if drain is not None and drain.complete:
            terminal, reason, phase = "Cold", "DrainComplete", "Cold"
            message = "publication is withdrawn and observed runtime demand is zero"
        else:
            terminal, reason, phase = "Draining", "DrainInProgress", "Draining"
            message = "publication, active-operation, or zero-replica observation is incomplete"
    elif converged and rollout_complete and autoscaler_handoff_complete and ready is not None and ready > 0:
        terminal, reason, phase = "Ready", "RuntimeObservedReady", "Ready"
        message = "at least one controller-owned runtime replica is observed ready"
    elif converged and rollout_complete and autoscaler_handoff_complete and desired == 0 and replicas == 0:
        terminal, reason, phase = "Cold", "ScaleToZeroObserved", "Cold"
        message = "enabled model is reconciled and currently scaled to zero"
    elif phase_counts["nodePending"] > 0:
        terminal, reason, phase = "Progressing", "RuntimeNodePending", "NodePending"
        message = "runtime Pods are waiting for a schedulable node"
    elif phase_counts["localizing"] > 0:
        terminal, reason, phase = "Loading", "ArtifactLocalizing", "Localizing"
        message = "runtime Pods are localizing the qualified model artifact"
    elif phase_counts["runtimeStarting"] > 0:
        terminal, reason, phase = "Loading", "RuntimeStarting", "RuntimeStarting"
        message = "runtime containers are waiting to start"
    elif phase_counts["warming"] > 0:
        terminal, reason, phase = "Loading", "RuntimeWarming", "Warming"
        message = "runtime containers are running but readiness has not converged"
    elif replicas is not None and ready is not None and replicas > ready:
        terminal, reason, phase = "Loading", "RuntimeStarting", "RuntimeStarting"
        message = "runtime replicas exist but readiness has not converged"

    cache = _cache_status(
        spec=spec,
        phase_counts=phase_counts,
        desired_replicas=desired,
        previous_status=previous_status,
        observed_at=observed_at,
    )
    condition_types = ("Ready", "Cold", "Loading", "Draining", "InfrastructureRequired", "Failed", "Progressing")
    conditions = [
        _condition(
            condition_type,
            "True" if condition_type == terminal else "False",
            reason if condition_type == terminal else f"Not{condition_type}",
            message if condition_type == terminal else f"{condition_type} is not the current observed state",
            generation,
            previous=old.get(condition_type),
            now=observed_at,
        )
        for condition_type in condition_types
    ]
    raw_cache_state = cache.get("state") if cache is not None else None
    cache_state = raw_cache_state if isinstance(raw_cache_state, str) else None
    cache_conditions: dict[str, tuple[Literal["True", "False", "Unknown"], str, str]] = {
        "Cached": ("True", "ArtifactCacheObserved", "qualified artifact cache was observed through a ready runtime"),
        "Missing": ("False", "ArtifactCacheMissing", "qualified artifact cache is missing"),
        "Failed": ("False", "ArtifactCacheFailed", "artifact cache preparation failed"),
        "Localizing": ("Unknown", "ArtifactCacheLocalizing", "artifact cache preparation is in progress"),
    }
    cache_condition = cache_conditions.get(
        cache_state or "",
        ("Unknown", "ArtifactCacheObservationUnavailable", "artifact cache state is not observed"),
    )
    conditions.append(
        _condition(
            "Cached",
            cache_condition[0],
            cache_condition[1],
            cache_condition[2],
            generation,
            previous=old.get("Cached"),
            now=observed_at,
        )
    )
    host_residency_pool_refs = None
    if plan.render is not None:
        host_residency_pool_refs = {
            pool_ref
            for item in plan.render.resources
            if item.kind == "DaemonSet"
            and isinstance(
                pool_ref := _mapping(_mapping(item.manifest.get("metadata")).get("annotations")).get(
                    WORKLOAD_POOL_ANNOTATION
                ),
                str,
            )
        }
    fast_start = _fast_start_status(
        spec=spec,
        envelope=envelope,
        assessment=plan.validation.fast_start,
        converged=converged,
        ready_replicas=ready,
        previous_status=previous_status,
        history=fast_start_history,
        now=observed_at,
        host_residency_pool_refs=host_residency_pool_refs,
    )
    if fast_start is not None:
        satisfied = fast_start.qualification.state in {
            FastStartQualificationState.NO_TARGET,
            FastStartQualificationState.QUALIFIED,
        }
        conditions.append(
            _condition(
                "FastStartQualified",
                "True" if satisfied else "False",
                fast_start.qualification.reason,
                fast_start.qualification.message,
                generation,
                previous=old.get("FastStartQualified"),
                now=observed_at,
            )
        )
    resources = [
        {
            "identity": item.observed.identity,
            "apiVersion": item.observed.api_version,
            "kind": item.observed.kind,
            "namespace": item.observed.namespace,
            "name": item.observed.name,
            "uid": item.observed.uid,
            "generation": item.generation,
            "digest": item.observed.digest,
        }
        for item in discovery.resources
        if item.observed.controller_owner_uid is not None
    ]
    placements: list[dict[str, Any]] = []
    for item in sorted(deployments, key=lambda value: value.observed.name):
        annotations = _mapping(_metadata(item.raw).get("annotations"))
        pool_ref = annotations.get(WORKLOAD_POOL_ANNOTATION)
        role = annotations.get(WORKLOAD_ROLE_ANNOTATION)
        if not isinstance(pool_ref, str) or role not in {"hot", "burst"}:
            continue
        placement: dict[str, Any] = {
            "deploymentName": item.observed.name,
            "poolRef": pool_ref,
            "role": role,
        }
        for field, value in (
            ("desired", item.desired_replicas),
            ("ready", item.ready_replicas),
            ("available", item.available_replicas),
        ):
            if value is not None:
                placement[field] = value
        placements.append(placement)
    status: dict[str, Any] = {
        "observedGeneration": generation,
        "phase": phase,
        "specDigest": plan.spec_digest,
        "activeRevision": spec.artifact.revision,
        "replicas": {
            "desired": desired,
            "admitted": phase_counts["admitted"],
            "nodePending": phase_counts["nodePending"],
            "localizing": phase_counts["localizing"],
            "runtimeStarting": phase_counts["runtimeStarting"],
            "warming": phase_counts["warming"],
            "ready": ready,
            "available": available,
        },
        "resources": resources,
        "retryCount": 0,
        "lastReconcileTime": _timestamp(observed_at),
        "conditions": conditions,
    }
    if plan.validation.disposition is ValidationDisposition.ACCEPTED:
        status["eligiblePoolRefs"] = sorted(spec.placement.pool_refs)
    if placements:
        status["placements"] = placements
    if cache is not None:
        status["cache"] = cache
    if fast_start is not None:
        # Unavailable measurements are omitted rather than serialised as zero.
        status["fastStart"] = fast_start.model_dump(mode="json", by_alias=True, exclude_none=True)
    if plan.validation.admitted_pool_ref is not None:
        status["admittedPoolRef"] = plan.validation.admitted_pool_ref
    if plan.render is not None:
        status["renderDigest"] = plan.render.render_digest
        endpoint = plan.render.endpoint
        snapshot = next(
            (item for item in discovery.resources if item.observed.identity == endpoint.identity),
            None,
        )
        rendered_service = next(
            (item for item in plan.render.resources if item.kind == "Service" and item.name == endpoint.service_name),
            None,
        )
        if (
            snapshot is not None
            and rendered_service is not None
            and snapshot.observed.controller_owner_uid == owner_uid
            and FIELD_MANAGER in snapshot.observed.field_managers
            and snapshot.observed.digest == rendered_service.digest
        ):
            status["endpoint"] = {
                "namespace": endpoint.namespace,
                "serviceName": endpoint.service_name,
                "servicePort": endpoint.service_port,
                "uid": snapshot.observed.uid,
                "digest": snapshot.observed.digest,
            }
        publication_resources = [
            item
            for item in plan.render.resources
            if item.kind == "ConfigMap"
            and _mapping(item.manifest.get("metadata")).get("labels", {}).get("fs2-serve.nebius.ai/component")
            == "publication-intent"
        ]
        if converged and len(publication_resources) <= 1:
            # This is the exact controller-observed publication intent. The
            # runtime bridge still performs its independent Ready/Cold,
            # policy, endpoint and registry fencing before exposing a route.
            status["publication"] = {
                "openAI": spec.exposure.open_ai if publication_resources else False,
                "mcp": spec.exposure.mcp if publication_resources else False,
                "observedAt": _timestamp(observed_at),
            }
    if plan.action is ReconcileAction.INFRASTRUCTURE_REQUIRED:
        status["infrastructureHandoff"] = {
            "reason": message,
            "owner": "Terraform",
            "requiredInputs": plan.validation.terraform_inputs,
        }
    status["adoption"] = {
        "state": {
            AdoptionMode.NONE: "None",
            AdoptionMode.OBSERVE: "ObserveOnly",
            AdoptionMode.CLAIM: "Owned" if converged else "Claiming",
        }[spec.adoption.mode]
    }
    if spec.adoption.receipt_ref is not None:
        status["adoption"]["receiptDigest"] = spec.adoption.receipt_ref.digest
    return status


class ActiveOperationsReader(Protocol):
    async def active_operations(self, *, tenant_id: str, model_ref: str) -> int | None: ...

    async def fast_start_history(
        self,
        *,
        model_ref: str,
        idle_seconds: int,
        now: datetime,
    ) -> tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None: ...


class UnknownActiveOperations:
    async def active_operations(self, *, tenant_id: str, model_ref: str) -> int | None:
        return None

    async def fast_start_history(
        self,
        *,
        model_ref: str,
        idle_seconds: int,
        now: datetime,
    ) -> tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None:
        return None


class PostgresActiveOperations:
    """Read drain demand under the same per-model fence as admission."""

    def __init__(self, pool: asyncpg.Pool[Any], *, owns_pool: bool = False) -> None:
        self.pool = pool
        self.owns_pool = owns_pool

    @classmethod
    async def connect(cls, database_url: str, *, maximum_connections: int = 2) -> PostgresActiveOperations:
        dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=max(1, maximum_connections),
            command_timeout=30,
            server_settings={"application_name": "fs2-model-controller"},
        )
        assert pool is not None
        return cls(pool, owns_pool=True)

    async def close(self) -> None:
        if self.owns_pool:
            await self.pool.close()

    async def active_operations(self, *, tenant_id: str, model_ref: str) -> int | None:
        del tenant_id
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(fs2_activation_model_lock_key($1))",
                    model_ref,
                )
                value = await connection.fetchval(
                    """
                    SELECT count(*) FROM fs2_operations
                    WHERE model_id=$1 AND status IN ('queued','activating','running')
                    """,
                    model_ref,
                )
        except (asyncpg.PostgresError, TimeoutError):
            LOGGER.warning("durable active-operation evidence is unavailable for model %s", model_ref)
            return None
        return int(value) if isinstance(value, int) and 0 <= value <= 1_000_000_000 else None

    async def fast_start_history(
        self,
        *,
        model_ref: str,
        idle_seconds: int,
        now: datetime,
    ) -> tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None:
        """Read payload-free demand without reusing the legacy cold-start clock."""

        async def window(connection: asyncpg.Connection[Any], started_at: datetime) -> FastStartHistoryWindow:
            row = await connection.fetchrow(
                """
                WITH ordered AS (
                    SELECT accepted_at,
                           lag(accepted_at) OVER (ORDER BY accepted_at,id) AS previous_accepted_at
                    FROM fs2_operations
                    WHERE model_id=$1 AND accepted_at >= $2 AND accepted_at < $3
                )
                SELECT count(*)::bigint AS request_count,
                       count(*) FILTER (
                           WHERE previous_accepted_at IS NOT NULL
                             AND extract(epoch FROM accepted_at-previous_accepted_at) >= $4
                       )::bigint AS idle_gap_episode_count
                FROM ordered
                """,
                model_ref,
                started_at,
                now,
                float(idle_seconds),
            )
            assert row is not None
            return FastStartHistoryWindow(
                started_at=started_at,
                ended_at=now,
                request_count=int(row["request_count"]),
                # activation_started_at is an operation-worker transition and
                # occurs for hot requests too.  Only idle-gap episodes are a
                # defensible cold-start demand proxy until an exact model
                # activation boundary is persisted.
                cold_activation_count=0,
                idle_gap_episode_count=int(row["idle_gap_episode_count"]),
                # The retained accepted-to-ready value includes capacity wait.
                # It must not be re-labelled as a model-start target miss.
                target_miss_count=0,
                complete=True,
            )

        try:
            async with self.pool.acquire() as connection, connection.transaction(readonly=True):
                return (
                    await window(connection, now - timedelta(hours=1)),
                    await window(connection, now - timedelta(days=7)),
                )
        except (asyncpg.PostgresError, TimeoutError, ValueError):
            LOGGER.warning("automatic fast-start demand history is unavailable for model %s", model_ref)
            return None


class PrometheusActiveOperations:
    """Conservatively project in-flight durable demand for controller drains.

    The exported operation metric intentionally has no tenant label.  A model
    drain therefore waits for operations across every tenant using the same
    canonical model reference, which is safer than deleting a shared runtime
    while another tenant still has work in flight.
    """

    def __init__(self, reader: HttpPrometheusScalarReader) -> None:
        self.reader = reader

    async def active_operations(self, *, tenant_id: str, model_ref: str) -> int | None:
        del tenant_id
        query = operation_demand_promql(model_ref)
        try:
            value = await self.reader.scalar(query, at=_utc_now())
        except (AdminAdapterUnavailableError, ValueError):
            LOGGER.warning("active-operation evidence is unavailable for model %s", model_ref)
            return None
        if value is None or value > 1_000_000_000:
            return None
        return math.ceil(value)

    async def fast_start_history(
        self,
        *,
        model_ref: str,
        idle_seconds: int,
        now: datetime,
    ) -> tuple[FastStartHistoryWindow, FastStartHistoryWindow] | None:
        del model_ref, idle_seconds, now
        return None


class ModelDeploymentController:
    def __init__(
        self,
        *,
        api: ModelControllerApi,
        envelope: InfrastructureEnvelope,
        renderer: LegacyManifestRenderer,
        namespace: str,
        holder_identity: str,
        prometheus_server_address: str,
        writes_enabled: bool,
        active_operations: ActiveOperationsReader | None = None,
        lease_namespace: str = "fs2-system",
        lease_name: str = "fs2-model-controller",
        lease_duration_seconds: int = 15,
        queue_capacity: int = 256,
        worker_count: int = 2,
        poll_seconds: float = 5,
    ) -> None:
        self.api = api
        self.envelope = envelope
        self.renderer = renderer
        self.namespace = namespace
        self.holder_identity = holder_identity
        self.prometheus_server_address = prometheus_server_address
        self.writes_enabled = writes_enabled
        self.active_operations = active_operations or UnknownActiveOperations()
        self.lease_namespace = lease_namespace
        self.lease_name = lease_name
        self.lease_duration_seconds = lease_duration_seconds
        self.worker_count = worker_count
        self.poll_seconds = poll_seconds
        self.queue = BoundedKeyQueue(queue_capacity)
        self.metrics = ControllerMetrics()
        for model_ref, qualification in envelope.qualifications.items():
            if qualification.model_express is None:
                continue
            for pool_ref in qualification.model_express.pool_refs:
                self.metrics.modelexpress_configured.labels(
                    model_ref,
                    pool_ref,
                    qualification.model_express.deployment_mode,
                    qualification.model_express.runtime_adapter,
                    qualification.model_express.pool_transports[pool_ref].mode,
                ).set(1)
        self.health = ControllerHealth()
        self._fence: LeaseFence | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _drain_observation(self, spec: ModelDeploymentSpec, discovery: Discovery) -> DrainObservation:
        publication_present = any(
            item.raw.get("metadata", {}).get("labels", {}).get("fs2-serve.nebius.ai/component") == "publication-intent"
            and not item.observed.deleting
            for item in discovery.resources
        )
        deployments = [item for item in discovery.resources if item.observed.kind == "Deployment"]
        return DrainObservation(
            publication_withdrawn=not publication_present,
            active_operations=await self.active_operations.active_operations(
                tenant_id=spec.tenant_id, model_ref=spec.model_ref
            ),
            observed_replicas=_known_total([item.replicas for item in deployments]),
            ready_replicas=_known_total([item.ready_replicas for item in deployments]),
        )

    async def reconcile(self, key: ModelKey, fence: LeaseFence | None = None) -> ReconcileResult:
        fence = fence or self._fence
        raw = await self.api.get_model(key)
        if raw is None:
            return ReconcileResult(key=key, action="gone", generation=0)
        metadata = _metadata(raw)
        uid = _required_metadata(raw, "uid")
        generation = int(metadata.get("generation", 0))
        if generation < 1:
            raise ControllerError("ModelDeployment generation is unavailable")
        spec = ModelDeploymentSpec.model_validate(raw.get("spec"))
        fast_start_history = (
            await self.active_operations.fast_start_history(
                model_ref=spec.model_ref,
                idle_seconds=spec.availability.idle_seconds,
                now=_utc_now(),
            )
            if spec.fast_start.mode is FastStartMode.AUTOMATIC
            else None
        )
        deleting = isinstance(metadata.get("deletionTimestamp"), str)
        finalizers = _string_list(metadata.get("finalizers"))
        has_finalizer = FINALIZER in finalizers

        validation = validate_model_deployment(spec, self.envelope)
        if validation.disposition is ValidationDisposition.ACCEPTED:
            assert validation.admitted_pool_ref is not None
            qualification = self.envelope.qualifications[spec.model_ref]
            context = RenderContext(
                name=key.name,
                namespace=key.namespace,
                uid=uid,
                generation=generation,
                pool=self.envelope.pools[validation.admitted_pool_ref],
                eligible_pools=[self.envelope.pools[pool_ref] for pool_ref in spec.placement.pool_refs],
                prometheus_server_address=self.prometheus_server_address,
                model_express=qualification.model_express,
                regional_cache=qualification.regional_cache,
                host_memory_residency=qualification.host_memory_residency,
                gpu_resident=qualification.gpu_resident,
                residency_holder_image=self.envelope.residency_holder_image,
            )
            render = self.renderer.render(spec, context)
            discovery = await self.api.discover(key=key, owner_uid=uid, render=render)
        else:
            # The planner returns before rendering for invalid or
            # infrastructure-required revisions; an empty authoritative
            # inventory cannot authorize finalizer removal.
            context_pool = next(iter(self.envelope.pools.values()))
            context = RenderContext(
                name=key.name,
                namespace=key.namespace,
                uid=uid,
                generation=generation,
                pool=context_pool,
                prometheus_server_address=self.prometheus_server_address,
            )
            discovery = Discovery(resources=[], complete=not deleting)
        drain = (
            await self._drain_observation(spec, discovery)
            if deleting or spec.lifecycle.desired_state is not DesiredState.ENABLED
            else None
        )
        if drain is not None and drain.preserve_runtime:
            context = context.model_copy(update={"hot_floor_override": _observed_hot_floor(discovery)})
        plan = plan_reconciliation(
            generation=generation,
            deleting=deleting,
            spec=spec,
            envelope=self.envelope,
            renderer=self.renderer,
            render_context=context,
            observed=discovery.observed(),
            discovery_complete=discovery.complete,
            drain_observation=drain,
            adoption_verification=None,
        )

        if not self.writes_enabled:
            return ReconcileResult(
                key=key,
                action=f"observe-only:{plan.action}",
                generation=generation,
                requeue=plan.action not in {ReconcileAction.NOOP, ReconcileAction.OBSERVE},
            )
        if fence is None:
            raise FenceLostError("no live leader fence is available")

        # Never create generated resources before the deletion backstop is
        # durably present. Observe-only adoption intentionally has no finalizer.
        if (
            not deleting
            and plan.action
            not in {
                ReconcileAction.OBSERVE,
                ReconcileAction.REJECT,
                ReconcileAction.INFRASTRUCTURE_REQUIRED,
                ReconcileAction.RETRY,
            }
            and not has_finalizer
        ):
            await self.api.set_finalizer(key, owner_uid=uid, present=True, fence=fence)
            return ReconcileResult(key=key, action="finalizer-added", generation=generation, wrote=True, requeue=True)
        if deleting and not has_finalizer:
            return ReconcileResult(
                key=key,
                action="delete-without-finalizer-blocked",
                generation=generation,
                error_code="finalizer_missing",
            )

        wrote = False
        phase_action: str | None = None
        phase_requeue = False
        # Delete stale scaler/publication resources first.  Applying the
        # explicit drain replica zero in the same pass could race an HPA that
        # still owns the scale subresource.
        if plan.delete_resource_identities:
            handoff_identities = [
                identity
                for identity in plan.delete_resource_identities
                if "/ScaledObject/" in identity or "fs2-model-publication-" in identity
            ]
            if (
                not handoff_identities
                and drain is not None
                and not drain.preserve_runtime
                and _autoscaler_resources_present(discovery)
            ):
                return ReconcileResult(
                    key=key,
                    action="drain:autoscaler-removal-pending",
                    generation=generation,
                    wrote=wrote,
                    requeue=True,
                )
            delete_identities = handoff_identities or plan.delete_resource_identities
            for identity in delete_identities:
                wrote = await self.api.delete_resource(identity, owner_uid=uid, fence=fence) or wrote
            safe_zero_cutover = (
                not handoff_identities
                and plan.action is ReconcileAction.DRAIN
                and drain is not None
                and not drain.preserve_runtime
                and not _autoscaler_resources_present(discovery)
            )
            if not safe_zero_cutover:
                return ReconcileResult(
                    key=key,
                    action=f"{plan.action}:delete-first",
                    generation=generation,
                    wrote=wrote,
                    requeue=True,
                )

        # A drain may write replicas=0 only after the foreground ScaledObject
        # deletion and its generated HPA garbage collection are both observed.
        if drain is not None and not drain.preserve_runtime and _autoscaler_resources_present(discovery):
            phase_action = "drain:autoscaler-removal-pending"
            phase_requeue = True

        autoscaler_pairs = _autoscaler_pairs(plan.render)
        autoscaled_target_identities = {_rendered_identity(target) for _, target in autoscaler_pairs}
        if phase_action is None and autoscaler_pairs:
            live_targets = {
                _rendered_identity(target): _resource_snapshot(
                    discovery,
                    target.api_version,
                    target.kind,
                    target.namespace,
                    target.name,
                )
                for _, target in autoscaler_pairs
            }
            apply_without_targets = [
                resource
                for resource in plan.apply_resources
                if _rendered_identity(resource) not in autoscaled_target_identities
            ]
            missing_targets = [
                target for _, target in autoscaler_pairs if live_targets[_rendered_identity(target)] is None
            ]
            if missing_targets:
                apply_identities = {_rendered_identity(resource) for resource in plan.apply_resources}
                if any(_rendered_identity(target) not in apply_identities for target in missing_targets):
                    raise ControllerError("new autoscaled Deployment is absent from the apply plan")
                for target in missing_targets:
                    bootstrap = await self.api.apply_resource(
                        _with_deployment_replicas(target, 0), owner_uid=uid, fence=fence
                    )
                    if bootstrap.desired_replicas != 0 or FIELD_MANAGER not in bootstrap.replica_field_managers:
                        raise ControllerError("autoscaled Deployment zero-replica bootstrap ownership was not observed")
                    wrote = True
                for resource in apply_without_targets:
                    await self.api.apply_resource(resource, owner_uid=uid, fence=fence)
                    wrote = True
                phase_action = "autoscaler-bootstrap"
                phase_requeue = True
            elif not all(_autoscaler_installed(scaler, target, discovery, uid) for scaler, target in autoscaler_pairs):
                for scaler, target in autoscaler_pairs:
                    live_target = live_targets[_rendered_identity(target)]
                    assert live_target is not None
                    live_scaler = _resource_snapshot(
                        discovery,
                        scaler.api_version,
                        scaler.kind,
                        scaler.namespace,
                        scaler.name,
                    )
                    if live_scaler is None and FIELD_MANAGER not in live_target.replica_field_managers:
                        bootstrap = await self.api.apply_resource(
                            _with_deployment_replicas(target, 0), owner_uid=uid, fence=fence
                        )
                        if bootstrap.desired_replicas != 0 or FIELD_MANAGER not in bootstrap.replica_field_managers:
                            raise ControllerError(
                                "autoscaled Deployment zero-replica bootstrap ownership was not observed"
                            )
                        wrote = True
                for resource in apply_without_targets:
                    await self.api.apply_resource(resource, owner_uid=uid, fence=fence)
                    wrote = True
                phase_action = "autoscaler-install-pending"
                phase_requeue = True
            elif any(
                FIELD_MANAGER in live_target.replica_field_managers
                for live_target in live_targets.values()
                if live_target is not None
            ):
                for resource in apply_without_targets:
                    await self.api.apply_resource(resource, owner_uid=uid, fence=fence)
                    wrote = True
                for _, target in autoscaler_pairs:
                    live_target = live_targets[_rendered_identity(target)]
                    assert live_target is not None
                    if FIELD_MANAGER not in live_target.replica_field_managers:
                        continue
                    relinquished = await self.api.apply_resource(target, owner_uid=uid, fence=fence)
                    if FIELD_MANAGER in relinquished.replica_field_managers:
                        raise ControllerError(
                            "Deployment replica ownership was not relinquished after HPA verification"
                        )
                    wrote = True
                phase_action = "autoscaler-handoff"
                phase_requeue = True

        if phase_action is None:
            for resource in plan.apply_resources:
                await self.api.apply_resource(resource, owner_uid=uid, fence=fence)
                wrote = True
        if plan.remove_finalizer:
            if phase_action is not None:
                # Descendant cleanup must be observed before removing the CR's
                # deletion backstop.
                phase_requeue = True
            else:
                await self.api.set_finalizer(key, owner_uid=uid, present=False, fence=fence)
                return ReconcileResult(key=key, action="finalizer-removed", generation=generation, wrote=True)

        # Re-read after all writes and derive status exclusively from observed
        # objects. Partial apply remains Progressing and is repaired next pass.
        raw_after = await self.api.get_model(key)
        if raw_after is None:
            return ReconcileResult(key=key, action="gone-after-write", generation=generation, wrote=wrote)
        if plan.render is not None:
            discovery = await self.api.discover(key=key, owner_uid=uid, render=plan.render)
        previous = _mapping(raw_after.get("status"))
        status = build_status(
            spec=spec,
            owner_uid=uid,
            generation=generation,
            plan=plan,
            discovery=discovery,
            previous_status=previous,
            drain=drain,
            envelope=self.envelope,
            fast_start_history=fast_start_history,
        )
        wrote = (
            await self.api.patch_status(
                key,
                owner_uid=uid,
                generation=generation,
                status=status,
                fence=fence,
            )
            or wrote
        )
        return ReconcileResult(
            key=key,
            action=phase_action or str(plan.action),
            generation=generation,
            wrote=wrote,
            requeue=(
                phase_requeue
                or bool(plan.apply_resources)
                or plan.action in {ReconcileAction.DRAIN, ReconcileAction.RETRY}
            ),
        )

    async def run_cycle(self) -> bool:
        """Acquire/renew leadership, list exact namespace, and enqueue keys."""

        if not self.writes_enabled:
            models = await self.api.list_models(self.namespace)
            for raw in models:
                metadata = _metadata(raw)
                self.queue.put(ModelKey(namespace=self.namespace, name=str(metadata.get("name"))))
            self.health.cycle_succeeded()
            self.health.leader = False
            return True
        fence = await self.api.acquire_or_renew_lease(
            namespace=self.lease_namespace,
            name=self.lease_name,
            holder_identity=self.holder_identity,
            token=self._fence.token if self._fence is not None else None,
            duration_seconds=self.lease_duration_seconds,
        )
        self._fence = fence
        self.health.leader = fence is not None
        self.metrics.leader.set(1 if fence is not None else 0)
        if fence is None:
            self.health.cycle_succeeded()
            return False
        models = await self.api.list_models(self.namespace)
        for raw in models:
            metadata = _metadata(raw)
            name = metadata.get("name")
            if not isinstance(name, str) or not name:
                raise ControllerError("ModelDeployment list item name is invalid")
            before = self.queue.dropped
            self.queue.put(ModelKey(namespace=self.namespace, name=name))
            if self.queue.dropped > before:
                self.metrics.queue_dropped.inc()
        self.metrics.queue_depth.set(self.queue.depth)
        self.health.cycle_succeeded()
        return True

    async def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                key = await asyncio.wait_for(self.queue.get(), timeout=1)
            except TimeoutError:
                continue
            requeue = False
            try:
                with self.metrics.duration.time():
                    result = await self.reconcile(key)
                self.metrics.reconciles.labels(result.action).inc()
                self.health.reconcile_succeeded(key)
                requeue = result.requeue
            except (ControllerError, ValueError) as exc:
                LOGGER.warning("ModelDeployment reconcile failed for %s: %s", key.text, exc)
                self.metrics.reconciles.labels(type(exc).__name__).inc()
                self.health.reconcile_failed(key, type(exc).__name__)
            finally:
                self.queue.done(key)
                if requeue:
                    before = self.queue.dropped
                    self.queue.put(key)
                    if self.queue.dropped > before:
                        self.metrics.queue_dropped.inc()
                self.metrics.queue_depth.set(self.queue.depth)

    async def run(self) -> None:
        workers = [asyncio.create_task(self._worker()) for _ in range(self.worker_count)]
        try:
            while not self._stop.is_set():
                try:
                    await self.run_cycle()
                except (ControllerError, ValueError) as exc:
                    LOGGER.warning("model controller list/lease cycle failed: %s", exc)
                    self.health.cycle_failed(type(exc).__name__)
                    self._fence = None
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass
        finally:
            self._stop.set()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


def controller_health_app(controller: ModelDeploymentController) -> FastAPI:
    app = FastAPI(
        title="FS2 ModelDeployment controller health",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/livez", include_in_schema=False)
    async def live() -> Response:
        return Response(status_code=200 if controller.health.live else 503)

    @app.get("/readyz", include_in_schema=False)
    async def ready() -> Response:
        return Response(status_code=200 if controller.health.ready else 503)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(controller.metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    return app


async def run_model_controller(settings: Settings) -> None:
    """Run the independent controller process after both source gates pass."""

    if not settings.model_controller_enabled:
        raise RuntimeError("model controller feature gate is disabled")
    holder = settings.model_controller_holder_identity
    if holder is None or ":" not in holder:
        raise RuntimeError("model controller holder identity must bind pod name and UID")
    files = ControllerFiles.load(
        settings.model_controller_envelope_file,
        settings.model_controller_bundles_file,
    )
    api = HttpKubernetesModelClient(
        base_url=settings.model_controller_api_url,
        token_file=settings.model_controller_token_file,
        ca_file=settings.model_controller_ca_file,
        writes_enabled=settings.model_controller_writes_enabled,
        timeout_seconds=settings.model_controller_api_timeout_seconds,
    )
    active_operations = await PostgresActiveOperations.connect(
        settings.database_url,
        maximum_connections=settings.model_controller_workers,
    )
    controller = ModelDeploymentController(
        api=api,
        envelope=files.infrastructure_envelope,
        renderer=files.renderer(),
        namespace=settings.model_controller_namespace,
        holder_identity=holder,
        prometheus_server_address=settings.model_controller_prometheus_server_address,
        writes_enabled=settings.model_controller_writes_enabled,
        active_operations=active_operations,
        lease_namespace=settings.model_controller_system_namespace,
        lease_name=settings.model_controller_lease_name,
        lease_duration_seconds=settings.model_controller_lease_duration_seconds,
        queue_capacity=settings.model_controller_queue_capacity,
        worker_count=settings.model_controller_workers,
        poll_seconds=settings.model_controller_poll_seconds,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            controller_health_app(controller),
            host="0.0.0.0",  # noqa: S104 - pod-local health/metrics endpoint
            port=settings.model_controller_health_port,
            log_level=settings.log_level.lower(),
        )
    )
    controller_task = asyncio.create_task(controller.run())
    try:
        await server.serve()
    finally:
        controller.stop()
        await controller_task
        await active_operations.close()
        await api.close()
