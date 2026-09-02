"""Fenced Kubernetes REST implementation for Kueue-managed Jobs and JobSets."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote
from uuid import UUID

import httpx

from .models import (
    FailureKind,
    LifecyclePhase,
    WorkloadKind,
    WorkloadObservation,
    WorkloadRef,
    WorkloadResource,
    WorkloadState,
)
from .protocols import BatchRepositoryConflictError

ATTEMPT_LABEL = "fs2.nebius.ai/attempt-id"
OPERATION_LABEL = "fs2.nebius.ai/operation-id"
WORKLOAD_LABEL = "fs2.nebius.ai/workload-id"
MODEL_LABEL = "fs2.nebius.ai/model-id"
TENANT_LABEL = "fs2.nebius.ai/tenant-id"
STAGE_LABEL = "fs2.nebius.ai/stage-id"
SHARD_LABEL = "fs2.nebius.ai/shard-id"
SERVICE_CLASS_LABEL = "fs2.nebius.ai/service-class"
LOCAL_QUEUE_LABEL = "fs2.nebius.ai/local-queue"
QUEUE_LABEL = "kueue.x-k8s.io/queue-name"
PRIORITY_LABEL = "kueue.x-k8s.io/priority-class"
FENCE_ANNOTATION = "fs2.nebius.ai/scientific-controller-fence"
SNAPSHOT_ANNOTATION = "fs2.nebius.ai/scheduling-snapshot-digest"
MANIFEST_ANNOTATION = "fs2.nebius.ai/scientific-manifest-sha256"


class ScientificManifestRenderer(Protocol):
    """Trusted model-owned renderer; caller request fields never reach this API."""

    def render(self, resource: WorkloadResource) -> Mapping[str, Any]: ...


class ScientificFenceAuthority(Protocol):
    async def assert_fence(self, operation_id: UUID, *, controller_id: str, fencing_token: int) -> None: ...


class ScientificKubernetesError(RuntimeError):
    """Bounded Kubernetes adapter failure."""


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ScientificKubernetesError(f"{label} is not an object")
    return value


def _metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    return _object(value.get("metadata"), "Kubernetes metadata")


def _condition(status: Mapping[str, Any], kind: str, expected: str = "True") -> Mapping[str, Any] | None:
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return None
    return next(
        (
            cast(Mapping[str, Any], item)
            for item in conditions
            if isinstance(item, Mapping) and item.get("type") == kind and item.get("status") == expected
        ),
        None,
    )


def _failure(reasons: list[str]) -> tuple[WorkloadState, FailureKind, str]:
    normalized = [reason.lower() for reason in reasons if reason]
    selected = reasons[0] if reasons else "workload_failed"
    if any("preempt" in reason or "evict" in reason or "deactiv" in reason for reason in normalized):
        return WorkloadState.PREEMPTED, FailureKind.PREEMPTION, selected
    if any(
        reason in {"nodelost", "nodefailure", "shutdown", "unexpectedadmissioncheck"} or "unreachable" in reason
        for reason in normalized
    ):
        return WorkloadState.FAILED, FailureKind.INFRASTRUCTURE, selected
    # Unknown Job/container failures are application failures. Classifying
    # them as infrastructure would silently retry invalid input or model code.
    return WorkloadState.FAILED, FailureKind.APPLICATION, selected


def _safe_label(value: str) -> str:
    """Keep UUID/tenant identities bounded without pretending hashes are raw IDs."""

    if 1 <= len(value) <= 63 and value[0].isalnum() and value[-1].isalnum():
        return value
    return "h-" + hashlib.sha256(value.encode()).hexdigest()[:40]


def _manifest_digest(value: Mapping[str, Any]) -> str:
    body = copy.deepcopy(dict(value))
    annotations = _object(_metadata(body).setdefault("annotations", {}), "Kubernetes annotations")
    annotations.pop(MANIFEST_ANNOTATION, None)
    annotations.pop(FENCE_ANNOTATION, None)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(canonical).hexdigest()


def _template_metadata(manifest: dict[str, Any], kind: WorkloadKind) -> list[dict[str, Any]]:
    spec = _object(manifest.get("spec"), "Kubernetes workload spec")
    if kind is WorkloadKind.JOB:
        template = _object(spec.get("template"), "Job Pod template")
        return [_object(template.setdefault("metadata", {}), "Job Pod metadata")]
    jobs = spec.get("replicatedJobs")
    if not isinstance(jobs, list) or not jobs:
        raise ScientificKubernetesError("JobSet must contain replicatedJobs")
    result: list[dict[str, Any]] = []
    for job in jobs:
        job_spec = _object(_object(job, "JobSet replicated job").get("template"), "JobSet Job template")
        pod = _object(_object(job_spec.get("spec"), "JobSet Job spec").get("template"), "JobSet Pod template")
        result.append(_object(pod.setdefault("metadata", {}), "JobSet Pod metadata"))
    return result


class HttpScientificBatchCluster:
    """Create, observe, and delete only deterministic controller-owned work."""

    def __init__(
        self,
        *,
        base_url: str,
        token_file: Path,
        ca_file: Path,
        controller_id: str,
        fence: ScientificFenceAuthority,
        renderer: ScientificManifestRenderer,
        writes_enabled: bool,
        timeout_seconds: float = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_file = token_file
        self.controller_id = controller_id
        self.fence = fence
        self.renderer = renderer
        self.writes_enabled = writes_enabled
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), verify=str(ca_file), timeout=httpx.Timeout(timeout_seconds)
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        try:
            token = self.token_file.read_text().strip()
        except OSError as error:
            raise ScientificKubernetesError("projected Kubernetes token is unavailable") from error
        if len(token) < 16:
            raise ScientificKubernetesError("projected Kubernetes token is unavailable")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.client.request(method, path, headers=self._headers(), **kwargs)
        except (OSError, httpx.HTTPError) as error:
            raise ScientificKubernetesError("Kubernetes API request failed") from error
        if response.status_code >= 400 and response.status_code not in {404, 409}:
            raise ScientificKubernetesError(f"Kubernetes API returned HTTP {response.status_code}")
        return response

    @staticmethod
    def _paths(ref: WorkloadRef) -> tuple[str, str]:
        namespace = quote(ref.namespace, safe="")
        name = quote(ref.name, safe="")
        if ref.kind is WorkloadKind.JOB:
            base = f"/apis/batch/v1/namespaces/{namespace}/jobs"
        else:
            base = f"/apis/jobset.x-k8s.io/v1alpha2/namespaces/{namespace}/jobsets"
        return base, f"{base}/{name}"

    @staticmethod
    def _owned(value: Mapping[str, Any], ref: WorkloadRef, attempt_id: UUID) -> None:
        metadata = _metadata(value)
        labels = _object(metadata.get("labels"), "Kubernetes labels")
        if (
            metadata.get("name") != ref.name
            or metadata.get("namespace") != ref.namespace
            or labels.get(ATTEMPT_LABEL) != str(attempt_id)
        ):
            raise BatchRepositoryConflictError("deterministic workload name has different ownership")

    def _prepare(self, resource: WorkloadResource, controller_fence: int) -> dict[str, Any]:
        manifest = copy.deepcopy(dict(self.renderer.render(resource)))
        expected_api = "batch/v1" if resource.kind is WorkloadKind.JOB else "jobset.x-k8s.io/v1alpha2"
        if manifest.get("apiVersion") != expected_api or manifest.get("kind") != str(resource.kind):
            raise ScientificKubernetesError("renderer returned the wrong workload kind")
        metadata = _metadata(manifest)
        if metadata.get("name") != resource.name or metadata.get("namespace") != resource.namespace:
            raise ScientificKubernetesError("renderer changed the deterministic workload identity")
        labels = _object(metadata.setdefault("labels", {}), "Kubernetes labels")
        labels.update(
            {
                OPERATION_LABEL: str(resource.operation_id),
                WORKLOAD_LABEL: str(resource.workload_id),
                ATTEMPT_LABEL: str(resource.attempt_id),
                MODEL_LABEL: resource.model_id,
                TENANT_LABEL: _safe_label(resource.tenant_id),
                STAGE_LABEL: resource.stage_id,
                SERVICE_CLASS_LABEL: str(resource.service_class),
                LOCAL_QUEUE_LABEL: resource.scheduling.resolved_local_queue,
                QUEUE_LABEL: resource.scheduling.resolved_local_queue,
                PRIORITY_LABEL: resource.scheduling.workload_priority_class,
            }
        )
        if resource.shard_id is not None:
            labels[SHARD_LABEL] = resource.shard_id
        annotations = _object(metadata.setdefault("annotations", {}), "Kubernetes annotations")
        annotations[FENCE_ANNOTATION] = f"{self.controller_id}:{controller_fence}"
        annotations[SNAPSHOT_ANNOTATION] = resource.scheduling_snapshot_digest
        for pod_metadata in _template_metadata(manifest, resource.kind):
            pod_labels = _object(pod_metadata.setdefault("labels", {}), "Pod template labels")
            pod_labels.update(labels)
        spec = _object(manifest.get("spec"), "Kubernetes workload spec")
        spec["suspend"] = True
        if resource.kind is WorkloadKind.JOB:
            spec["backoffLimit"] = 0
        annotations[MANIFEST_ANNOTATION] = _manifest_digest(manifest)
        return manifest

    async def apply(self, resource: WorkloadResource, *, controller_fence: int) -> WorkloadRef:
        if not self.writes_enabled:
            raise ScientificKubernetesError("scientific Kubernetes writes are disabled")
        await self.fence.assert_fence(
            resource.operation_id, controller_id=self.controller_id, fencing_token=controller_fence
        )
        manifest = self._prepare(resource, controller_fence)
        collection, item = self._paths(resource.ref)
        response = await self._request("POST", collection, json=manifest)
        if response.status_code == 409:
            response = await self._request("GET", item)
        if response.status_code == 404:
            raise ScientificKubernetesError("Kubernetes workload disappeared during apply")
        try:
            value = response.json()
        except ValueError as error:
            raise ScientificKubernetesError("Kubernetes returned invalid JSON") from error
        self._owned(value, resource.ref, resource.attempt_id)
        metadata = _metadata(value)
        live_annotations = _object(metadata.get("annotations"), "Kubernetes annotations")
        if live_annotations.get(MANIFEST_ANNOTATION) != _metadata(manifest)["annotations"][MANIFEST_ANNOTATION]:
            raise BatchRepositoryConflictError("existing workload manifest differs from the frozen attempt")
        uid = metadata.get("uid")
        if not isinstance(uid, str) or not uid:
            raise ScientificKubernetesError("Kubernetes workload UID is absent")
        return WorkloadRef(namespace=resource.namespace, name=resource.name, kind=resource.kind, uid=uid)

    async def observe(self, ref: WorkloadRef) -> WorkloadObservation:
        _, item = self._paths(ref)
        response = await self._request("GET", item)
        if response.status_code == 404:
            raise ScientificKubernetesError("controller-owned workload is absent")
        value = cast(dict[str, Any], response.json())
        metadata = _metadata(value)
        labels = _object(metadata.get("labels"), "Kubernetes labels")
        try:
            attempt_id = UUID(str(labels[ATTEMPT_LABEL]))
        except (KeyError, ValueError):
            raise BatchRepositoryConflictError("workload attempt identity is absent or invalid") from None
        if ref.uid is not None and metadata.get("uid") != ref.uid:
            raise BatchRepositoryConflictError("workload UID changed")
        status = _object(value.get("status", {}), "Kubernetes workload status")
        phases: list[LifecyclePhase] = []
        if _condition(status, "QuotaReserved") or _condition(status, "Admitted") or status.get("startTime"):
            phases.append(LifecyclePhase.ADMITTED)

        selector = quote(f"{ATTEMPT_LABEL}={attempt_id}", safe="=,.-")
        pod_response = await self._request(
            "GET", f"/api/v1/namespaces/{quote(ref.namespace, safe='')}/pods?labelSelector={selector}&limit=100"
        )
        pods = [] if pod_response.status_code == 404 else cast(dict[str, Any], pod_response.json()).get("items", [])
        pod_phases: list[str] = []
        waiting_reasons: list[str] = []
        failure_reasons: list[str] = []
        scheduled = False
        for raw_pod in pods if isinstance(pods, list) else []:
            if not isinstance(raw_pod, Mapping):
                continue
            pod_status = raw_pod.get("status")
            if not isinstance(pod_status, Mapping):
                continue
            pod_phases.append(str(pod_status.get("phase", "Unknown")))
            if isinstance(pod_status.get("reason"), str):
                failure_reasons.append(cast(str, pod_status["reason"]))
            scheduled = scheduled or _condition(pod_status, "PodScheduled") is not None
            statuses = pod_status.get("containerStatuses", [])
            for container in statuses if isinstance(statuses, list) else []:
                if not isinstance(container, Mapping):
                    continue
                state = container.get("state")
                waiting = state.get("waiting") if isinstance(state, Mapping) else None
                if isinstance(waiting, Mapping) and isinstance(waiting.get("reason"), str):
                    waiting_reasons.append(cast(str, waiting["reason"]))
                terminated = state.get("terminated") if isinstance(state, Mapping) else None
                if isinstance(terminated, Mapping) and isinstance(terminated.get("reason"), str):
                    failure_reasons.append(cast(str, terminated["reason"]))
        if pods and not scheduled:
            phases.append(LifecyclePhase.NODE_PENDING)
        if any(reason in {"Pulling", "ErrImagePull", "ImagePullBackOff"} for reason in waiting_reasons):
            phases.append(LifecyclePhase.IMAGE_LOADING)
        if any(phase == "Running" for phase in pod_phases):
            phases.append(LifecyclePhase.ACTIVE_COMPUTE)

        succeeded = int(status.get("succeeded", 0) or 0) > 0 or _condition(status, "Completed") is not None
        failed_condition = _condition(status, "Failed")
        failed = int(status.get("failed", 0) or 0) > 0 or failed_condition is not None
        if succeeded:
            state = WorkloadState.SUCCEEDED
        elif failed:
            reason = str((failed_condition or {}).get("reason", "workload_failed"))
            workload_state, failure_kind, failure_code = _failure([reason, *failure_reasons])
            return WorkloadObservation(
                ref=ref,
                attempt_id=attempt_id,
                state=workload_state,
                phases=tuple(dict.fromkeys(phases)),
                failure_kind=failure_kind,
                failure_code=failure_code[:128],
            )
        elif any(phase == "Running" for phase in pod_phases):
            state = WorkloadState.RUNNING
        else:
            state = WorkloadState.PENDING
        return WorkloadObservation(
            ref=ref,
            attempt_id=attempt_id,
            state=state,
            phases=tuple(dict.fromkeys(phases)),
        )

    async def delete(self, ref: WorkloadRef, *, controller_fence: int) -> None:
        if not self.writes_enabled:
            raise ScientificKubernetesError("scientific Kubernetes writes are disabled")
        _, item = self._paths(ref)
        current = await self._request("GET", item)
        if current.status_code == 404:
            return
        value = cast(dict[str, Any], current.json())
        metadata = _metadata(value)
        labels = _object(metadata.get("labels"), "Kubernetes labels")
        try:
            operation_id = UUID(str(labels[OPERATION_LABEL]))
        except (KeyError, ValueError):
            raise BatchRepositoryConflictError("workload operation identity is absent or invalid") from None
        if ref.uid is not None and metadata.get("uid") != ref.uid:
            raise BatchRepositoryConflictError("workload UID changed before delete")
        await self.fence.assert_fence(operation_id, controller_id=self.controller_id, fencing_token=controller_fence)
        uid = metadata.get("uid")
        await self._request(
            "DELETE",
            item,
            json={
                "kind": "DeleteOptions",
                "apiVersion": "v1",
                "propagationPolicy": "Foreground",
                "preconditions": {"uid": uid},
            },
        )
