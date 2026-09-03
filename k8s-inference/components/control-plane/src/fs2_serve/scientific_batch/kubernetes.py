"""Fenced Kubernetes REST implementation for Kueue-managed Jobs and JobSets."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote
from uuid import UUID

import httpx

from .models import (
    COLLECTION_GRACE_SECONDS,
    COLLECTOR_CONTAINER_NAME,
    STAGE_CONTAINER_NAME,
    FailureKind,
    LifecyclePhase,
    ResourceClass,
    SchedulingAdmission,
    StageSchedulingDecision,
    WorkloadKind,
    WorkloadObservation,
    WorkloadRef,
    WorkloadResource,
    WorkloadState,
)
from .podset_envelope import (
    PodSetEnvelopeError,
    WorkloadEnvelope,
    compare_kueue_usage,
    envelope_from_json,
    envelope_from_manifest,
    parse_bytes,
    parse_cpu_millis,
)
from .protocols import BatchRepositoryConflictError

ATTEMPT_LABEL = "fs2.nebius.ai/attempt-id"
OPERATION_LABEL = "fs2.nebius.ai/operation-id"
WORKLOAD_LABEL = "fs2.nebius.ai/workload-id"
MODEL_LABEL = "fs2.nebius.ai/model-id"
VARIANT_LABEL = "fs2.nebius.ai/variant-id"
TENANT_LABEL = "fs2.nebius.ai/tenant-id"
STAGE_LABEL = "fs2.nebius.ai/stage-id"
SHARD_LABEL = "fs2.nebius.ai/shard-id"
SERVICE_CLASS_LABEL = "fs2.nebius.ai/service-class"
LOCAL_QUEUE_LABEL = "fs2.nebius.ai/local-queue"
QUEUE_LABEL = "kueue.x-k8s.io/queue-name"
PRIORITY_LABEL = "kueue.x-k8s.io/priority-class"
MAX_EXECUTION_LABEL = "kueue.x-k8s.io/max-exec-time-seconds"
FENCE_ANNOTATION = "fs2.nebius.ai/scientific-controller-fence"
SNAPSHOT_ANNOTATION = "fs2.nebius.ai/scheduling-snapshot-digest"
VARIANT_ANNOTATION = "fs2.nebius.ai/variant-id"
MANIFEST_ANNOTATION = "fs2.nebius.ai/scientific-manifest-sha256"
CLUSTER_QUEUE_ANNOTATION = "fs2.nebius.ai/cluster-queue"
POOL_PREFERENCE_ANNOTATION = "fs2.nebius.ai/pool-preference"
PREEMPTION_ANNOTATION = "fs2.nebius.ai/preemption-mode"
MAX_QUEUE_ANNOTATION = "fs2.nebius.ai/max-queue-seconds"
ACCELERATOR_RESOURCE_ANNOTATION = "fs2.nebius.ai/accelerator-resource"
ACCELERATOR_COUNT_ANNOTATION = "fs2.nebius.ai/accelerator-count"
WORKLOAD_NAMESPACE_ANNOTATION = "fs2.nebius.ai/workload-namespace"
PODSET_ENVELOPE_ANNOTATION = "fs2.nebius.ai/podset-resource-envelope"
PODSET_ENVELOPE_DIGEST_ANNOTATION = "fs2.nebius.ai/podset-resource-envelope-sha256"
ROUTE_NAMESPACE_ANNOTATION = "fs2.nebius.ai/route-namespace"
KUEUE_JOB_UID_LABEL = "kueue.x-k8s.io/job-uid"
POOL_LABEL = "accelerator.fs2.nebius/pool-id"
NODE_POOL_LABEL = POOL_LABEL

_KUEUE_EXECUTION_TIMEOUT_REASONS = frozenset(
    {
        "MaximumExecutionTimeExceeded",
        "DeactivatedDueToMaximumExecutionTimeExceeded",
        "EvictedDueToDeactivatedDueToMaximumExecutionTimeExceeded",
    }
)
_KUEUE_REQUEUING_LIMIT_REASONS = frozenset(
    {
        "RequeuingLimitExceeded",
        "DeactivatedDueToRequeuingLimitExceeded",
        "EvictedDueToDeactivatedDueToRequeuingLimitExceeded",
    }
)


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


def _timestamp(condition: Mapping[str, Any], label: str) -> datetime:
    value = condition.get("lastTransitionTime")
    if not isinstance(value, str):
        raise ScientificKubernetesError(f"Kueue {label} timestamp is absent")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ScientificKubernetesError(f"Kueue {label} timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ScientificKubernetesError(f"Kueue {label} timestamp is naive")
    return parsed


def _quantity(value: object, *, resource: str) -> int:
    """Parse one admitted Kueue quantity through the canonical envelope units.

    CPU becomes milli-cores and memory becomes bytes, using the same parser the
    frozen envelope uses, so an admitted quantity and a rendered request can
    never be compared in different units.
    """

    try:
        parsed = (
            parse_cpu_millis(value, label="Kueue cpu")
            if resource == "cpu"
            else parse_bytes(value, label=f"Kueue {resource}")
        )
    except PodSetEnvelopeError as error:
        raise ScientificKubernetesError(f"Kueue {resource} admission quantity is invalid") from error
    if parsed <= 0:
        raise ScientificKubernetesError(f"Kueue {resource} admission quantity is not positive and exact")
    return parsed


def _failure(reasons: list[str]) -> tuple[WorkloadState, FailureKind, str]:
    for reason in reasons:
        if reason in _KUEUE_EXECUTION_TIMEOUT_REASONS | _KUEUE_REQUEUING_LIMIT_REASONS:
            return _kueue_eviction(reason)
    normalized = [reason.lower() for reason in reasons if reason]
    selected = reasons[0] if reasons else "workload_failed"
    if "preempted" in normalized:
        return WorkloadState.PREEMPTED, FailureKind.PREEMPTION, selected
    if any(
        reason in {"nodelost", "nodefailure", "shutdown", "unexpectedadmissioncheck"} or "unreachable" in reason
        for reason in normalized
    ):
        return WorkloadState.FAILED, FailureKind.INFRASTRUCTURE, selected
    # Unknown Job/container failures are application failures. Classifying
    # them as infrastructure would silently retry invalid input or model code.
    return WorkloadState.FAILED, FailureKind.APPLICATION, selected


def _kueue_eviction(reason: str) -> tuple[WorkloadState, FailureKind, str]:
    """Classify only Kueue's exact eviction reason; generic Pod eviction is not preemption."""

    if reason in _KUEUE_EXECUTION_TIMEOUT_REASONS:
        return WorkloadState.FAILED, FailureKind.APPLICATION, "EXECUTION_TIMEOUT"
    if reason in _KUEUE_REQUEUING_LIMIT_REASONS:
        return WorkloadState.FAILED, FailureKind.INFRASTRUCTURE, "RequeuingLimitExceeded"
    if reason == "Preempted":
        return WorkloadState.PREEMPTED, FailureKind.PREEMPTION, reason
    if reason in {
        "NodeFailures",
        "PodsReadyTimeout",
        "ClusterQueueStopped",
        "LocalQueueStopped",
        "EvictedOnManagerCluster",
    }:
        return WorkloadState.FAILED, FailureKind.INFRASTRUCTURE, reason
    return WorkloadState.FAILED, FailureKind.APPLICATION, reason


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _container_termination(pod_status: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    """Return the named regular container's terminated state, if it has one."""

    statuses = pod_status.get("containerStatuses", [])
    for container in statuses if isinstance(statuses, list) else []:
        if not isinstance(container, Mapping) or container.get("name") != name:
            continue
        state = container.get("state")
        terminated = state.get("terminated") if isinstance(state, Mapping) else None
        if isinstance(terminated, Mapping):
            return terminated
    return None


def _finished_at(terminated: Mapping[str, Any]) -> datetime | None:
    value = terminated.get("finishedAt")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _stalled_collection(
    pod_statuses: list[Mapping[str, Any]],
    *,
    now: datetime,
) -> tuple[WorkloadState, FailureKind, str] | None:
    """Settle a staged Pod whose model is finished but whose collector is not.

    A staged Pod runs the model and the artifact collector side by side under
    ``restartPolicy: Never``, so Kubernetes only settles the Pod once *both*
    containers terminate.  When the model dies without publishing its output
    the collector keeps polling for a result that can never arrive, and the Job
    holds its admitted GPU reservation until ``activeDeadlineSeconds`` expires.

    The Pod's own container statuses are the authoritative termination marker:
    Kubernetes reports them for every exit path, including the OOM kill that
    would take an in-container supervisor down with the model.  Reusing them
    here keeps the verdict identical to the one the deadline would eventually
    have produced, only hours earlier.
    """

    for pod_status in pod_statuses:
        stage = _container_termination(pod_status, STAGE_CONTAINER_NAME)
        if stage is None or _container_termination(pod_status, COLLECTOR_CONTAINER_NAME) is not None:
            # The model still runs, or both containers are done and the Job
            # status settles this attempt through the existing failure path.
            continue
        exit_code = stage.get("exitCode")
        if not isinstance(exit_code, int):
            continue
        if exit_code != 0:
            reasons = [
                value for value in (stage.get("reason"), pod_status.get("reason")) if isinstance(value, str) and value
            ]
            # Reuse the exact taxonomy the Job-failure path applies, so an
            # OOM-killed or preempted stage keeps its established retry class.
            return _failure(reasons or ["workload_failed"])
        finished_at = _finished_at(stage)
        if finished_at is not None and now - finished_at >= timedelta(seconds=COLLECTION_GRACE_SECONDS):
            # The model published nothing collectable. This is the stage's own
            # result, not an infrastructure fault, so it must not be retried.
            return WorkloadState.FAILED, FailureKind.APPLICATION, "collection_deadline_exceeded"
    return None


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


def _pod_specs(manifest: dict[str, Any], kind: WorkloadKind) -> list[dict[str, Any]]:
    spec = _object(manifest.get("spec"), "Kubernetes workload spec")
    if kind is WorkloadKind.JOB:
        template = _object(spec.get("template"), "Job Pod template")
        return [_object(template.get("spec"), "Job Pod spec")]
    jobs = spec.get("replicatedJobs")
    if not isinstance(jobs, list) or not jobs:
        raise ScientificKubernetesError("JobSet must contain replicatedJobs")
    result: list[dict[str, Any]] = []
    for raw_job in jobs:
        job = _object(raw_job, "JobSet replicated job")
        job_template = _object(job.get("template"), "JobSet Job template")
        job_spec = _object(job_template.get("spec"), "JobSet Job spec")
        pod_template = _object(job_spec.get("template"), "JobSet Pod template")
        result.append(_object(pod_template.get("spec"), "JobSet Pod spec"))
    return result


def _bind_pool_affinity(pod: dict[str, Any], pools: tuple[str, ...]) -> None:
    """Constrain every required node-selector term to the frozen pool set."""

    affinity = _object(pod.setdefault("affinity", {}), "Pod affinity")
    node_affinity = _object(affinity.setdefault("nodeAffinity", {}), "Pod node affinity")
    required = _object(
        node_affinity.setdefault("requiredDuringSchedulingIgnoredDuringExecution", {}),
        "required Pod node affinity",
    )
    terms = required.setdefault("nodeSelectorTerms", [{}])
    if not isinstance(terms, list) or not terms:
        raise ScientificKubernetesError("required Pod node affinity has no node selector terms")
    for raw_term in terms:
        term = _object(raw_term, "Pod node selector term")
        expressions = term.setdefault("matchExpressions", [])
        if not isinstance(expressions, list) or not all(isinstance(item, dict) for item in expressions):
            raise ScientificKubernetesError("Pod node affinity matchExpressions are invalid")
        expressions[:] = [item for item in expressions if item.get("key") != NODE_POOL_LABEL]
        expressions.append({"key": NODE_POOL_LABEL, "operator": "In", "values": list(pools)})


def _set_active_deadline(manifest: dict[str, Any], kind: WorkloadKind, seconds: int) -> None:
    spec = _object(manifest.get("spec"), "Kubernetes workload spec")
    if kind is WorkloadKind.JOB:
        spec["activeDeadlineSeconds"] = seconds
        return
    jobs = spec.get("replicatedJobs")
    if not isinstance(jobs, list) or not jobs:
        raise ScientificKubernetesError("JobSet must contain replicatedJobs")
    for raw_job in jobs:
        job = _object(raw_job, "JobSet replicated job")
        template = _object(job.get("template"), "JobSet Job template")
        job_spec = _object(template.get("spec"), "JobSet Job spec")
        job_spec["activeDeadlineSeconds"] = seconds


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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.token_file = token_file
        self.controller_id = controller_id
        self.fence = fence
        self.renderer = renderer
        self.writes_enabled = writes_enabled
        self.clock = clock or _utcnow
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
        if resource.route_namespace != resource.namespace:
            raise ScientificKubernetesError("workload namespace differs from the routed Kueue LocalQueue namespace")
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
                VARIANT_LABEL: _safe_label(resource.variant_id),
                TENANT_LABEL: _safe_label(resource.tenant_id),
                STAGE_LABEL: resource.stage_id,
                SERVICE_CLASS_LABEL: str(resource.service_class),
                LOCAL_QUEUE_LABEL: resource.scheduling.resolved_local_queue,
                QUEUE_LABEL: resource.scheduling.resolved_local_queue,
                PRIORITY_LABEL: resource.scheduling.workload_priority_class,
            }
        )
        if resource.scheduling.max_execution_seconds is not None:
            labels[MAX_EXECUTION_LABEL] = str(resource.scheduling.max_execution_seconds)
        if resource.shard_id is not None:
            labels[SHARD_LABEL] = resource.shard_id
        annotations = _object(metadata.setdefault("annotations", {}), "Kubernetes annotations")
        annotations[FENCE_ANNOTATION] = f"{self.controller_id}:{controller_fence}"
        annotations[SNAPSHOT_ANNOTATION] = resource.scheduling_snapshot_digest
        annotations[VARIANT_ANNOTATION] = resource.variant_id
        annotations[CLUSTER_QUEUE_ANNOTATION] = resource.scheduling.resolved_cluster_queue
        annotations[POOL_PREFERENCE_ANNOTATION] = ",".join(resource.scheduling.resolved_pool_preference)
        annotations[PREEMPTION_ANNOTATION] = str(resource.scheduling.preemption_mode)
        annotations[ACCELERATOR_RESOURCE_ANNOTATION] = resource.scheduling.accelerator_resource_name or ""
        annotations[ACCELERATOR_COUNT_ANNOTATION] = str(resource.scheduling.accelerator_count)
        annotations[WORKLOAD_NAMESPACE_ANNOTATION] = resource.namespace
        annotations[ROUTE_NAMESPACE_ANNOTATION] = resource.route_namespace
        if resource.scheduling.max_queue_seconds is not None:
            annotations[MAX_QUEUE_ANNOTATION] = str(resource.scheduling.max_queue_seconds)
        for pod_metadata in _template_metadata(manifest, resource.kind):
            pod_labels = _object(pod_metadata.setdefault("labels", {}), "Pod template labels")
            pod_labels.update(labels)
        spec = _object(manifest.get("spec"), "Kubernetes workload spec")
        spec["suspend"] = True
        if resource.scheduling.resource_class is ResourceClass.GPU:
            pools = resource.scheduling.resolved_pool_preference
            if not pools:
                raise ScientificKubernetesError("GPU workload has no frozen pool preference")
            for pod in _pod_specs(manifest, resource.kind):
                _bind_pool_affinity(pod, pools)
        if resource.kind is WorkloadKind.JOB:
            spec["backoffLimit"] = 0
        if resource.scheduling.max_execution_seconds is not None:
            _set_active_deadline(manifest, resource.kind, resource.scheduling.max_execution_seconds)
        envelope = self._frozen_envelope(manifest, resource)
        annotations[PODSET_ENVELOPE_ANNOTATION] = envelope.to_json()
        annotations[PODSET_ENVELOPE_DIGEST_ANNOTATION] = envelope.digest
        annotations[MANIFEST_ANNOTATION] = _manifest_digest(manifest)
        return manifest

    @staticmethod
    def _frozen_envelope(manifest: Mapping[str, Any], resource: WorkloadResource) -> WorkloadEnvelope:
        """Freeze the rendered per-Pod and aggregate envelope for this attempt.

        The envelope is derived from the manifest that will actually be created
        and is checked against the frozen scheduling decision here, before the
        object exists: the per-Pod accelerator request must be exactly the
        decision's ``accelerator_count``, and the replica count must apply the
        attempt's ``gang_size`` exactly once. Both annotations are written
        before the manifest digest, so they are covered by it and an adopted
        pre-existing object with a different envelope is refused as a conflict.
        """

        try:
            envelope = envelope_from_manifest(manifest, resource.kind)
        except PodSetEnvelopeError as error:
            raise ScientificKubernetesError(
                f"rendered workload has no exact PodSet resource envelope: {error}"
            ) from error
        if resource.kind is WorkloadKind.JOB:
            # Independent shards are separate Jobs, so a fanout attempt is
            # exactly one Pod. A Job that fanned out internally would have a
            # replica count no frozen gang size accounts for.
            if envelope.pod_sets[0].count != 1:
                raise ScientificKubernetesError("a fanout Job must reserve exactly one Pod per attempt")
        else:
            gang_size = resource.gang_size
            if gang_size is None:
                raise ScientificKubernetesError("a JobSet attempt has no frozen gang size")
            counts = [item.count for item in envelope.pod_sets]
            if max(counts) != gang_size or any(count > gang_size for count in counts):
                raise ScientificKubernetesError(
                    f"rendered JobSet reserves {max(counts)} Pods per PodSet instead of the frozen gang size "
                    f"{gang_size}"
                )
        accelerator_resource = resource.scheduling.accelerator_resource_name
        if resource.scheduling.resource_class is ResourceClass.GPU:
            if accelerator_resource is None:
                raise ScientificKubernetesError("GPU scheduling has no frozen accelerator resource")
            accelerator_pod_sets = envelope.accelerator_pod_sets(accelerator_resource)
            if not accelerator_pod_sets:
                raise ScientificKubernetesError("rendered GPU workload requests no frozen accelerator")
            for pod_set in accelerator_pod_sets:
                if pod_set.per_replica_requests.accelerator(accelerator_resource) != (
                    resource.scheduling.accelerator_count
                ):
                    raise ScientificKubernetesError(
                        "rendered per-Pod accelerator request differs from the frozen scheduling decision"
                    )
                if resource.kind is WorkloadKind.JOB_SET and pod_set.count != resource.gang_size:
                    raise ScientificKubernetesError("rendered accelerator PodSet does not reserve the frozen gang size")
        elif any(item.per_replica_requests.accelerators for item in envelope.pod_sets):
            raise ScientificKubernetesError("rendered CPU workload requests an accelerator")
        return envelope

    @staticmethod
    def _observed_envelope(value: Mapping[str, Any], ref: WorkloadRef) -> WorkloadEnvelope:
        """Reopen the frozen envelope and prove the live object still renders it.

        The annotation is the frozen claim and the live Pod templates are the
        fact. Recomputing the envelope from the object Kueue actually admitted
        turns any live mutation of a container's resources, of ``parallelism``
        or of a JobSet's ``replicas`` into an attempt conflict rather than a
        silently mis-budgeted reservation.
        """

        annotations = _object(_metadata(value).get("annotations"), "Kubernetes annotations")
        raw = annotations.get(PODSET_ENVELOPE_ANNOTATION)
        digest = annotations.get(PODSET_ENVELOPE_DIGEST_ANNOTATION)
        if not isinstance(raw, str) or not isinstance(digest, str):
            raise BatchRepositoryConflictError("workload has no frozen PodSet resource envelope")
        try:
            frozen = envelope_from_json(raw, kind=ref.kind)
            rendered = envelope_from_manifest(value, ref.kind)
        except PodSetEnvelopeError as error:
            raise BatchRepositoryConflictError(f"workload PodSet resource envelope is not exact: {error}") from error
        if frozen.digest != digest:
            raise BatchRepositoryConflictError("frozen PodSet resource envelope digest does not match its document")
        if frozen != rendered:
            raise BatchRepositoryConflictError("live workload resources differ from the frozen PodSet envelope")
        return frozen

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
        return WorkloadRef(
            namespace=resource.namespace,
            name=resource.name,
            kind=resource.kind,
            uid=uid,
            route_namespace=resource.route_namespace,
        )

    async def observe(
        self,
        ref: WorkloadRef,
        *,
        scheduling: StageSchedulingDecision,
    ) -> WorkloadObservation:
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
        annotations = _object(metadata.get("annotations"), "Kubernetes annotations")
        if (
            annotations.get(WORKLOAD_NAMESPACE_ANNOTATION) != ref.namespace
            or annotations.get(ROUTE_NAMESPACE_ANNOTATION) != ref.route_namespace
            or ref.route_namespace != ref.namespace
        ):
            raise BatchRepositoryConflictError("workload route namespace differs from the frozen attempt")
        expected_cluster_queue = annotations.get(CLUSTER_QUEUE_ANNOTATION)
        expected_accelerator_resource = annotations.get(ACCELERATOR_RESOURCE_ANNOTATION)
        raw_expected_accelerator_count = annotations.get(ACCELERATOR_COUNT_ANNOTATION)
        if (
            expected_cluster_queue != scheduling.resolved_cluster_queue
            or expected_accelerator_resource != (scheduling.accelerator_resource_name or "")
            or raw_expected_accelerator_count != str(scheduling.accelerator_count)
        ):
            raise BatchRepositoryConflictError("workload scheduling annotations differ from the frozen attempt")
        envelope = self._observed_envelope(value, ref)
        status = _object(value.get("status", {}), "Kubernetes workload status")
        phases: list[LifecyclePhase] = []
        scheduling_admission, kueue_workload_uid, admitted, eviction_reason = await self._scheduling_admission(
            ref,
            str(metadata.get("uid", "")),
            envelope=envelope,
            expected_cluster_queue=scheduling.resolved_cluster_queue,
            expected_pool_preference=scheduling.resolved_pool_preference,
            expected_accelerator_resource=scheduling.accelerator_resource_name,
            expected_accelerator_count=scheduling.accelerator_count,
            expected_resource_flavor=scheduling.requested_resource_flavor,
        )
        if admitted:
            phases.append(LifecyclePhase.ADMITTED)

        selector = quote(f"{ATTEMPT_LABEL}={attempt_id}", safe="=,.-")
        pod_response = await self._request(
            "GET", f"/api/v1/namespaces/{quote(ref.namespace, safe='')}/pods?labelSelector={selector}&limit=100"
        )
        pods = [] if pod_response.status_code == 404 else cast(dict[str, Any], pod_response.json()).get("items", [])
        pod_phases: list[str] = []
        waiting_reasons: list[str] = []
        failure_reasons: list[str] = []
        pod_uids: list[str] = []
        pod_statuses: list[Mapping[str, Any]] = []
        scheduled = False
        for raw_pod in pods if isinstance(pods, list) else []:
            if not isinstance(raw_pod, Mapping):
                continue
            pod_metadata = raw_pod.get("metadata")
            pod_uid = pod_metadata.get("uid") if isinstance(pod_metadata, Mapping) else None
            if isinstance(pod_uid, str) and pod_uid:
                pod_uids.append(pod_uid)
            pod_status = raw_pod.get("status")
            if not isinstance(pod_status, Mapping):
                continue
            pod_statuses.append(pod_status)
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
        if (
            not admitted
            and eviction_reason is None
            and (status.get("startTime") or any(phase == "Running" for phase in pod_phases) or succeeded)
        ):
            raise ScientificKubernetesError("Kueue has not admitted a started workload")
        if succeeded:
            state = WorkloadState.SUCCEEDED
        elif eviction_reason is not None:
            workload_state, failure_kind, failure_code = _kueue_eviction(eviction_reason)
            if workload_state is WorkloadState.PREEMPTED:
                phases.append(LifecyclePhase.PREEMPTED)
            return WorkloadObservation(
                ref=ref,
                attempt_id=attempt_id,
                state=workload_state,
                phases=tuple(dict.fromkeys(phases)),
                scheduling_admission=scheduling_admission,
                kueue_workload_uid=kueue_workload_uid,
                pod_uids=tuple(dict.fromkeys(pod_uids)),
                failure_kind=failure_kind,
                failure_code=failure_code[:128],
            )
        elif failed:
            reason = str((failed_condition or {}).get("reason", "workload_failed"))
            workload_state, failure_kind, failure_code = _failure([reason, *failure_reasons])
            return WorkloadObservation(
                ref=ref,
                attempt_id=attempt_id,
                state=workload_state,
                phases=tuple(dict.fromkeys(phases)),
                scheduling_admission=scheduling_admission,
                kueue_workload_uid=kueue_workload_uid,
                pod_uids=tuple(dict.fromkeys(pod_uids)),
                failure_kind=failure_kind,
                failure_code=failure_code[:128],
            )
        elif (stalled := _stalled_collection(pod_statuses, now=self.clock())) is not None:
            workload_state, failure_kind, failure_code = stalled
            if workload_state is WorkloadState.PREEMPTED:
                phases.append(LifecyclePhase.PREEMPTED)
            return WorkloadObservation(
                ref=ref,
                attempt_id=attempt_id,
                state=workload_state,
                phases=tuple(dict.fromkeys(phases)),
                scheduling_admission=scheduling_admission,
                kueue_workload_uid=kueue_workload_uid,
                pod_uids=tuple(dict.fromkeys(pod_uids)),
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
            scheduling_admission=scheduling_admission,
            kueue_workload_uid=kueue_workload_uid,
            pod_uids=tuple(dict.fromkeys(pod_uids)),
        )

    async def _scheduling_admission(
        self,
        ref: WorkloadRef,
        job_uid: str,
        *,
        envelope: WorkloadEnvelope,
        expected_cluster_queue: str,
        expected_pool_preference: tuple[str, ...],
        expected_accelerator_resource: str | None,
        expected_accelerator_count: int,
        expected_resource_flavor: str | None,
    ) -> tuple[SchedulingAdmission | None, str | None, bool, str | None]:
        """Reopen the exact frozen-resource Kueue reservation.

        Every PodSet Kueue admitted is matched by name against the frozen
        envelope, and every core or accelerator quantity it reports must equal
        that PodSet's aggregate request, which is its replica count times its
        per-Pod request. A JobSet may carry a CPU-only coordinator PodSet
        beside its GPU worker PodSet; each is compared on its own terms, and
        extended resources no ClusterQueue budgets are deliberately ignored.

        The recorded ``SchedulingAdmission`` keeps **per-replica** quantities,
        the same units as the frozen ``StageSchedulingDecision``, so a gang's
        immutable attempt evidence never depends on which side multiplied by
        ``gang_size``.
        """

        if not job_uid:
            raise ScientificKubernetesError("Kubernetes workload UID is absent during Kueue admission")
        selector = quote(f"{KUEUE_JOB_UID_LABEL}={job_uid}", safe="=,.-")
        assert ref.route_namespace is not None
        namespace = quote(ref.route_namespace, safe="")
        response = await self._request(
            "GET", f"/apis/kueue.x-k8s.io/v1beta1/namespaces/{namespace}/workloads?labelSelector={selector}&limit=2"
        )
        if response.status_code == 404:
            return None, None, False, None
        items = cast(dict[str, Any], response.json()).get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], Mapping):
            return None, None, False, None
        workload_metadata = items[0].get("metadata")
        workload_uid = workload_metadata.get("uid") if isinstance(workload_metadata, Mapping) else None
        if not isinstance(workload_uid, str) or not workload_uid:
            raise ScientificKubernetesError("Kueue Workload UID is absent")
        workload_status = items[0].get("status")
        if not isinstance(workload_status, Mapping):
            return None, workload_uid, False, None
        admitted = _condition(workload_status, "Admitted")
        quota_reserved = _condition(workload_status, "QuotaReserved")
        evicted = _condition(workload_status, "Evicted")
        deactivation_target = _condition(workload_status, "DeactivationTarget")
        eviction_reason = evicted.get("reason") if evicted is not None else None
        deactivation_reason = deactivation_target.get("reason") if deactivation_target is not None else None
        if eviction_reason is not None and not isinstance(eviction_reason, str):
            raise ScientificKubernetesError("Kueue eviction reason is invalid")
        if deactivation_reason is not None and not isinstance(deactivation_reason, str):
            raise ScientificKubernetesError("Kueue deactivation reason is invalid")
        # Kueue v0.17 first surfaces these terminal causes directly on the
        # temporary DeactivationTarget condition. A later reconciliation may
        # replace that condition with DeactivatedDueTo<Cause> on Evicted (and
        # its emitted event adds EvictedDueTo...). Preserve any exact form so
        # the classifier produces one stable controller failure code from the
        # first observable transition onward.
        if deactivation_reason in _KUEUE_EXECUTION_TIMEOUT_REASONS | _KUEUE_REQUEUING_LIMIT_REASONS:
            eviction_reason = deactivation_reason
        elif eviction_reason == "Deactivated":
            conditions = workload_status.get("conditions", [])
            condition_reasons = {
                item.get("reason")
                for item in conditions
                if isinstance(item, Mapping) and item.get("status") in {"True", "False"}
            }
            for condition_reason in (
                *_KUEUE_EXECUTION_TIMEOUT_REASONS,
                *_KUEUE_REQUEUING_LIMIT_REASONS,
            ):
                if condition_reason in condition_reasons:
                    eviction_reason = condition_reason
                    break
        admission = workload_status.get("admission")
        # QuotaReserved is the immutable assignment boundary. Admitted is a
        # later, distinct lifecycle transition and must never replace its
        # timestamp or make the tuple appear to drift.
        if quota_reserved is None or not isinstance(admission, Mapping):
            return None, workload_uid, False, eviction_reason
        if admission.get("clusterQueue") != expected_cluster_queue:
            raise ScientificKubernetesError("Kueue admitted a different ClusterQueue than the frozen route")
        quota_reserved_at = _timestamp(quota_reserved, "quota reservation")
        admitted_at = None if admitted is None else _timestamp(admitted, "admission")
        assignments = admission.get("podSetAssignments")
        if not isinstance(assignments, list) or not assignments:
            raise ScientificKubernetesError("Kueue admission has no PodSet assignment")
        try:
            comparison = compare_kueue_usage(envelope, assignments, accelerator_resource=expected_accelerator_resource)
        except PodSetEnvelopeError as error:
            raise ScientificKubernetesError(
                f"Kueue resourceUsage differs from the frozen PodSet envelope: {error}"
            ) from error
        accelerator_flavor: str | None = None
        cpu_flavor: str | None = None
        for raw_assignment in assignments:
            if not isinstance(raw_assignment, Mapping):
                raise ScientificKubernetesError("Kueue PodSet assignment is invalid")
            flavors = raw_assignment.get("flavors", {})
            usage = raw_assignment.get("resourceUsage", {})
            if not isinstance(flavors, Mapping) or not isinstance(usage, Mapping):
                raise ScientificKubernetesError("Kueue PodSet assignment resources are invalid")
            if expected_accelerator_resource is None:
                if "cpu" not in usage or "memory" not in usage:
                    raise ScientificKubernetesError("Kueue CPU PodSet admission omits cpu or memory")
                assignment_cpu_flavor = flavors.get("cpu")
                assignment_memory_flavor = flavors.get("memory")
                if (
                    not isinstance(assignment_cpu_flavor, str)
                    or assignment_cpu_flavor != assignment_memory_flavor
                    or assignment_cpu_flavor != expected_resource_flavor
                ):
                    raise ScientificKubernetesError("Kueue CPU admission differs from the frozen ResourceFlavor")
                if cpu_flavor is not None and cpu_flavor != assignment_cpu_flavor:
                    raise ScientificKubernetesError("Kueue admitted multiple CPU ResourceFlavors")
                cpu_flavor = assignment_cpu_flavor
                continue
            if expected_accelerator_resource not in usage:
                continue
            flavor = flavors.get(expected_accelerator_resource)
            if not isinstance(flavor, str) or not flavor:
                raise ScientificKubernetesError("Kueue GPU PodSet admission is not positive and complete")
            if accelerator_flavor is not None and accelerator_flavor != flavor:
                raise ScientificKubernetesError("Kueue admitted multiple ResourceFlavors for one accelerator")
            accelerator_flavor = flavor
        cpu_millis = comparison.cpu_millis_per_replica
        memory_bytes = comparison.memory_bytes_per_replica
        accelerator_count = comparison.accelerator_per_replica
        if expected_accelerator_resource is None:
            if expected_accelerator_count != 0:
                raise ScientificKubernetesError("frozen accelerator count has no exact resource")
            if (
                expected_resource_flavor is None
                or cpu_flavor is None
                or len(expected_pool_preference) != 1
                or cpu_millis < 1
                or memory_bytes < 1
            ):
                raise ScientificKubernetesError("Kueue CPU admission has no exact frozen class identity")
            return (
                SchedulingAdmission(
                    resolved_pool_id=expected_pool_preference[0],
                    admitted_resource_flavor=cpu_flavor,
                    accelerator_resource_name=None,
                    accelerator_count=0,
                    admitted_at=admitted_at,
                    quota_reserved_at=quota_reserved_at,
                    cpu_millis=cpu_millis,
                    memory_bytes=memory_bytes,
                ),
                workload_uid,
                admitted is not None,
                eviction_reason,
            )
        if accelerator_count <= 0 or accelerator_flavor is None:
            raise ScientificKubernetesError("Kueue admission has no positive PodSet for the frozen accelerator")
        if accelerator_count != expected_accelerator_count:
            raise ScientificKubernetesError("Kueue accelerator admission differs from the frozen quantity")
        if comparison.accelerator_aggregate != accelerator_count * sum(
            item.count for item in envelope.accelerator_pod_sets(expected_accelerator_resource)
        ):
            raise ScientificKubernetesError("Kueue accelerator reservation does not aggregate over its PodSets")
        flavor = accelerator_flavor
        flavor_response = await self._request(
            "GET", f"/apis/kueue.x-k8s.io/v1beta1/resourceflavors/{quote(flavor, safe='')}"
        )
        if flavor_response.status_code == 404:
            raise ScientificKubernetesError("Kueue admitted ResourceFlavor is absent")
        flavor_metadata = _metadata(cast(dict[str, Any], flavor_response.json()))
        flavor_labels = _object(flavor_metadata.get("labels", {}), "ResourceFlavor labels")
        pool_id = flavor_labels.get(POOL_LABEL)
        if not isinstance(pool_id, str):
            raise ScientificKubernetesError("Kueue admitted ResourceFlavor has no canonical pool identity")
        if pool_id not in expected_pool_preference:
            raise ScientificKubernetesError("Kueue admitted a pool outside the frozen preference")
        return (
            SchedulingAdmission(
                resolved_pool_id=pool_id,
                admitted_resource_flavor=flavor,
                accelerator_resource_name=expected_accelerator_resource,
                accelerator_count=accelerator_count,
                admitted_at=admitted_at,
                quota_reserved_at=quota_reserved_at,
            ),
            workload_uid,
            admitted is not None,
            eviction_reason,
        )

    async def delete(self, ref: WorkloadRef, *, controller_fence: int) -> None:
        """Delete this attempt's workload without ever losing an update.

        The DELETE carries both preconditions Kubernetes offers: the object's
        ``uid``, so a recreated name is never deleted for this attempt, and its
        ``resourceVersion``, so a delete issued against a version the API
        server has already replaced is refused instead of removing whatever the
        object has since become. The API server answers an accepted deletion
        with HTTP 200 (the object, now carrying a deletion timestamp) or 202,
        depending on the resource and the propagation policy, and both are
        accepted here; :meth:`absent` remains the completion gate.

        A failed precondition on a hot object is usually just the Job
        controller having written status between the read and the delete, so
        one bounded retry re-reads the fresh ``resourceVersion``, re-checks
        ownership and tries once more before surfacing a conflict for the
        controller to retry on its own clock.
        """

        if not self.writes_enabled:
            raise ScientificKubernetesError("scientific Kubernetes writes are disabled")
        _, item = self._paths(ref)
        fenced = False
        for remaining in (1, 0):
            current = await self._request("GET", item)
            if current.status_code == 404:
                return
            value = cast(dict[str, Any], current.json())
            metadata = _metadata(value)
            labels = _object(metadata.get("labels"), "Kubernetes labels")
            annotations = _object(metadata.get("annotations"), "Kubernetes annotations")
            if (
                annotations.get(WORKLOAD_NAMESPACE_ANNOTATION) != ref.namespace
                or annotations.get(ROUTE_NAMESPACE_ANNOTATION) != ref.route_namespace
                or ref.route_namespace != ref.namespace
            ):
                raise BatchRepositoryConflictError("workload route namespace changed before delete")
            try:
                operation_id = UUID(str(labels[OPERATION_LABEL]))
            except (KeyError, ValueError):
                raise BatchRepositoryConflictError("workload operation identity is absent or invalid") from None
            uid = metadata.get("uid")
            if not isinstance(uid, str) or not uid:
                raise ScientificKubernetesError("Kubernetes workload UID is absent before delete")
            if ref.uid is not None and uid != ref.uid:
                raise BatchRepositoryConflictError("workload UID changed before delete")
            resource_version = metadata.get("resourceVersion")
            if not isinstance(resource_version, str) or not resource_version:
                raise ScientificKubernetesError("Kubernetes workload resourceVersion is absent before delete")
            if not fenced:
                await self.fence.assert_fence(
                    operation_id, controller_id=self.controller_id, fencing_token=controller_fence
                )
                fenced = True
            response = await self._request(
                "DELETE",
                item,
                json={
                    "kind": "DeleteOptions",
                    "apiVersion": "v1",
                    "propagationPolicy": "Foreground",
                    "preconditions": {"uid": uid, "resourceVersion": resource_version},
                },
            )
            if response.status_code in {200, 202, 404}:
                return
            if response.status_code != 409:
                raise ScientificKubernetesError(f"Kubernetes workload delete returned HTTP {response.status_code}")
            if not remaining:
                raise BatchRepositoryConflictError("workload UID or resourceVersion precondition failed during delete")
        raise ScientificKubernetesError("Kubernetes workload delete did not settle")

    async def absent(self, ref: WorkloadRef) -> bool:
        """Confirm UID-specific absence after an accepted foreground deletion."""

        _, item = self._paths(ref)
        current = await self._request("GET", item)
        if current.status_code == 404:
            return True
        value = cast(dict[str, Any], current.json())
        metadata = _metadata(value)
        if metadata.get("name") != ref.name or metadata.get("namespace") != ref.namespace:
            raise BatchRepositoryConflictError("workload identity changed while deletion was pending")
        if ref.uid is None or metadata.get("uid") != ref.uid:
            raise BatchRepositoryConflictError("workload UID changed while deletion was pending")
        return False
