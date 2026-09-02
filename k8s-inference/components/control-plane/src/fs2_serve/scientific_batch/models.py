"""Immutable domain records for staged scientific batches.

The records intentionally contain no API, PostgreSQL, or Kubernetes client
types.  They are the integration seam between those owners and the controller.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, UUID, uuid5

_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
_POOL_RE = re.compile(r"^[a-z0-9](?:[-_a-z0-9.]*[a-z0-9])?$")
_RESOURCE_NAME_RE = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9][A-Za-z0-9._-]*$")
_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SENSITIVE_KEY_RE = re.compile(r"(?:token|secret|password|credential|private[_-]?key)", re.IGNORECASE)
_CONTROLLER_MOUNT_ROOT = PurePosixPath("/mnt/fs2-scientific")
_RUNTIME_MOUNT_ROOTS = tuple(
    PurePosixPath(value)
    for value in ("/models", "/databases", "/opt/fs2/artifacts", "/opt/fs2/academic")
)


class BatchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class StageStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class ExecutionMode(StrEnum):
    FANOUT = "independent-jobs"
    TRUE_GANG = "gang-jobset"


class ResourceClass(StrEnum):
    CPU = "cpu"
    GPU = "gpu"


class CheckpointMode(StrEnum):
    NONE = "none"
    RESTART = "restart"
    RESUME = "resume"


class PreemptionMode(StrEnum):
    NON_PREEMPTIBLE = "non_preemptible"
    RESTARTABLE = "restartable"
    CHECKPOINTABLE = "checkpointable"


class StorageAccessMode(StrEnum):
    READ_WRITE_ONCE = "ReadWriteOnce"
    READ_WRITE_MANY = "ReadWriteMany"


class ServiceClass(StrEnum):
    PRESENTATION = "presentation"
    INTERACTIVE = "interactive"
    CUSTOMER_BATCH = "customer-batch"
    BULK_BACKFILL = "bulk-backfill"


class WorkloadKind(StrEnum):
    JOB = "Job"
    JOB_SET = "JobSet"


class MaterializationMode(StrEnum):
    """Controller-owned artifact localization performed before model argv."""

    COPY_FILE = "copy-file"
    EXTRACT_TAR = "extract-tar"
    OVERLAY_TAR = "overlay-tar"
    BOLTZGEN_INPUT = "boltzgen-input"


class AttemptOutcome(StrEnum):
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PREEMPTED = "preempted"
    CANCELLED = "cancelled"


class FailureKind(StrEnum):
    INFRASTRUCTURE = "infrastructure"
    PREEMPTION = "preemption"
    USER_INPUT = "user_input"
    SCIENTIFIC_VALIDATION = "scientific_validation"
    APPLICATION = "application"

    @property
    def retryable(self) -> bool:
        return self in {self.INFRASTRUCTURE, self.PREEMPTION}


class WorkloadState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PREEMPTED = "preempted"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.PREEMPTED}


class LifecyclePhase(StrEnum):
    """Ordered phase markers used for exact GPU lifecycle attribution.

    A phase may be skipped when it does not apply, but an attempt can never
    move backwards.  A retry gets a new attempt identity and starts again.
    """

    QUEUED = "queued"
    SCHEDULING = "scheduling"
    ADMITTED = "admitted"
    NODE_PENDING = "node_pending"
    IMAGE_LOADING = "image_loading"
    ARTIFACT_LOADING = "artifact_loading"
    RESTORING = "restoring"
    SEMANTIC_WARMUP = "semantic_warmup"
    ACTIVE_COMPUTE = "active_compute"
    ALLOCATED_IDLE = "allocated_idle"
    GRACE_DRAIN = "grace_drain"
    PREEMPTED = "preempted"
    TEARDOWN = "teardown"

    @property
    def rank(self) -> int:
        return _LIFECYCLE_RANK[self]


_LIFECYCLE_RANK = {phase: position for position, phase in enumerate(LifecyclePhase)}


class BatchEventKind(StrEnum):
    LIFECYCLE = "lifecycle"
    ATTEMPT_FENCED = "attempt_fenced"
    RETRY_SCHEDULED = "retry_scheduled"
    STAGE_SUCCEEDED = "stage_succeeded"
    STAGE_FAILED = "stage_failed"
    BATCH_SUCCEEDED = "batch_succeeded"
    BATCH_FAILED = "batch_failed"
    BATCH_CANCELLED = "batch_cancelled"


def _check_name(value: str, label: str) -> None:
    if not value or len(value) > 63 or _NAME_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a DNS-compatible name of at most 63 characters")


def _check_digest(value: str, label: str) -> None:
    if _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be an immutable sha256 digest")


@dataclass(frozen=True, slots=True)
class ScientificStageResources:
    cpu_millis: int
    memory_bytes: int
    ephemeral_storage_bytes: int
    gpu_count: int
    cpu_limit_millis: int
    memory_limit_bytes: int
    ephemeral_storage_limit_bytes: int

    def __post_init__(self) -> None:
        if min(
            self.cpu_millis,
            self.memory_bytes,
            self.ephemeral_storage_bytes,
            self.cpu_limit_millis,
            self.memory_limit_bytes,
            self.ephemeral_storage_limit_bytes,
        ) < 1:
            raise ValueError("stage resource requests and limits must be positive")
        if not 0 <= self.gpu_count <= 8:
            raise ValueError("stage gpu_count must be between 0 and 8")
        if self.cpu_limit_millis < self.cpu_millis:
            raise ValueError("stage CPU limit cannot be below its request")
        if self.memory_limit_bytes < self.memory_bytes:
            raise ValueError("stage memory limit cannot be below its request")
        if self.ephemeral_storage_limit_bytes < self.ephemeral_storage_bytes:
            raise ValueError("stage ephemeral-storage limit cannot be below its request")


@dataclass(frozen=True, slots=True)
class ScientificStagePlacement:
    accelerator_class: str | None
    accelerator_resource_name: str | None
    accelerator_count: int
    compatible_pool_ids: tuple[str, ...]
    required_node_labels: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if len(self.compatible_pool_ids) != len(set(self.compatible_pool_ids)):
            raise ValueError("compatible scientific pool IDs must be unique")
        if len(dict(self.required_node_labels)) != len(self.required_node_labels):
            raise ValueError("scientific placement label keys must be unique")
        if self.accelerator_count == 0:
            if (
                self.accelerator_class is not None
                or self.accelerator_resource_name is not None
                or self.compatible_pool_ids
                or self.required_node_labels
            ):
                raise ValueError("CPU stage placement cannot retain accelerator selectors")
            return
        if not 1 <= self.accelerator_count <= 8:
            raise ValueError("GPU stage accelerator count must be between 1 and 8")
        if self.accelerator_class is None or _POOL_RE.fullmatch(self.accelerator_class) is None:
            raise ValueError("GPU stage placement requires a provider-neutral accelerator class")
        if (
            self.accelerator_resource_name is None
            or _RESOURCE_NAME_RE.fullmatch(self.accelerator_resource_name) is None
        ):
            raise ValueError("GPU stage placement requires an accelerator resource name")
        if not self.compatible_pool_ids:
            raise ValueError("GPU stage placement requires resolved deployment pools")
        for pool_id in self.compatible_pool_ids:
            if _POOL_RE.fullmatch(pool_id) is None:
                raise ValueError("scientific placement pool ID is invalid")


@dataclass(frozen=True, slots=True)
class ScientificStorageRequirement:
    storage_id: str
    purpose: str
    minimum_bytes: int
    access_mode: StorageAccessMode
    read_only: bool
    stages: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.storage_id or len(self.storage_id) > 128 or _POOL_RE.fullmatch(self.storage_id) is None:
            raise ValueError("storage_id must be a provider-neutral name of at most 128 characters")
        if self.purpose not in {"reference-data", "run-artifacts", "model-artifacts"}:
            raise ValueError("storage purpose is unsupported")
        if self.minimum_bytes < 1:
            raise ValueError("storage minimum_bytes must be positive")
        if not self.stages or len(self.stages) != len(set(self.stages)):
            raise ValueError("storage stages must be non-empty and unique")
        for stage_id in self.stages:
            _check_name(stage_id, "storage stage")


@dataclass(frozen=True, slots=True)
class ScientificStagePlan:
    stage_id: str
    depends_on: tuple[str, ...] = ()
    mode: ExecutionMode = ExecutionMode.FANOUT
    shards: tuple[str, ...] = ("main",)
    max_attempts: int = 2
    gang_size: int | None = None
    resource_class: ResourceClass = ResourceClass.GPU
    min_parallelism: int = 1
    max_parallelism: int = 1024
    checkpoint_mode: CheckpointMode = CheckpointMode.RESTART
    preemption_mode: PreemptionMode = PreemptionMode.RESTARTABLE
    resources: ScientificStageResources | None = None
    placement: ScientificStagePlacement | None = None

    def __post_init__(self) -> None:
        _check_name(self.stage_id, "stage_id")
        if len(set(self.depends_on)) != len(self.depends_on) or self.stage_id in self.depends_on:
            raise ValueError("stage dependencies must be unique and cannot reference the stage itself")
        for dependency in self.depends_on:
            _check_name(dependency, "stage dependency")
        if not self.shards or len(set(self.shards)) != len(self.shards):
            raise ValueError("stage shards must be non-empty and unique")
        for shard in self.shards:
            _check_name(shard, "shard")
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if not 1 <= self.min_parallelism <= self.max_parallelism <= 1024:
            raise ValueError("parallelism bounds must satisfy 1 <= min <= max <= 1024")
        if self.mode is ExecutionMode.FANOUT and self.gang_size is not None:
            raise ValueError("fanout stages cannot declare gang_size")
        if self.mode is ExecutionMode.TRUE_GANG:
            if self.gang_size is None or self.gang_size < 2:
                raise ValueError("true-gang stages require gang_size >= 2")
            if self.shards != ("gang",):
                raise ValueError("a true-gang stage is one JobSet and must use the single logical shard 'gang'")
        parallelism = self.gang_size if self.gang_size is not None else len(self.shards)
        if not self.min_parallelism <= parallelism <= self.max_parallelism:
            raise ValueError("expanded stage parallelism is outside the catalog profile bounds")
        if self.checkpoint_mode is CheckpointMode.RESUME and self.preemption_mode is not PreemptionMode.CHECKPOINTABLE:
            raise ValueError("resume checkpoints require checkpointable preemption")
        if self.resources is not None:
            if self.resource_class is ResourceClass.CPU and self.resources.gpu_count != 0:
                raise ValueError("CPU stage resources cannot reserve GPUs")
            if self.resource_class is ResourceClass.GPU and self.resources.gpu_count < 1:
                raise ValueError("GPU stage resources require at least one GPU")
        if self.placement is not None:
            expected = 0 if self.resource_class is ResourceClass.CPU else (
                self.resources.gpu_count if self.resources is not None else 1
            )
            if self.placement.accelerator_count != expected:
                raise ValueError("stage placement accelerator count must match stage resources")

    @property
    def workload_units(self) -> tuple[str | None, ...]:
        if self.mode is ExecutionMode.TRUE_GANG:
            return (None,)
        return self.shards


@dataclass(frozen=True, slots=True)
class ScientificBatchPlan:
    stages: tuple[ScientificStagePlan, ...]
    storage: tuple[ScientificStorageRequirement, ...] = ()

    def __post_init__(self) -> None:
        if not self.stages:
            raise ValueError("a batch plan requires at least one stage")
        ids = [stage.stage_id for stage in self.stages]
        if len(ids) != len(set(ids)):
            raise ValueError("stage IDs must be unique")
        known: set[str] = set()
        for stage in self.stages:
            missing = set(stage.depends_on) - set(ids)
            if missing:
                raise ValueError(f"stage {stage.stage_id} has unknown dependencies: {sorted(missing)}")
            forward = set(stage.depends_on) - known
            if forward:
                raise ValueError(
                    f"stage {stage.stage_id} dependencies must precede it in topological order: {sorted(forward)}"
                )
            known.add(stage.stage_id)
        for requirement in self.storage:
            missing = set(requirement.stages) - set(ids)
            if missing:
                raise ValueError(
                    f"storage {requirement.storage_id} has unknown stages: {sorted(missing)}"
                )

    def stage(self, stage_id: str) -> ScientificStagePlan:
        return next(stage for stage in self.stages if stage.stage_id == stage_id)


def _check_controller_path(value: str, label: str) -> None:
    path = PurePosixPath(value)
    if not path.is_absolute() or _CONTROLLER_MOUNT_ROOT not in path.parents:
        raise ValueError(f"{label} must be inside the controller-owned mount root")


@dataclass(frozen=True, slots=True)
class ArtifactMaterialization:
    """One logical artifact localized by the controller before a stage starts.

    Model-facing absolute paths are derived here and never accepted in the
    public request. Archive modes are implemented by the bounded, link-free
    materializer in ``adapters.materialization``.
    """

    artifact_id: str
    destination: str
    mode: MaterializationMode
    compression: str | None = None
    yaml_name: str | None = None
    reuse_prefix: str | None = None

    def __post_init__(self) -> None:
        if _ARTIFACT_ID_RE.fullmatch(self.artifact_id) is None:
            raise ValueError("materialization artifact_id must be a logical artifact ID")
        _check_controller_path(self.destination, "materialization destination")
        if self.compression not in {None, "none", "gzip", "zstd"}:
            raise ValueError("materialization compression is unsupported")
        if self.mode is MaterializationMode.BOLTZGEN_INPUT:
            yaml_path = PurePosixPath(self.yaml_name or "")
            if (
                not self.yaml_name
                or yaml_path.is_absolute()
                or any(part in {"", ".", ".."} for part in yaml_path.parts)
                or "\\" in self.yaml_name
            ):
                raise ValueError("BoltzGen input materialization requires one safe relative YAML path")
            if self.reuse_prefix is not None:
                reuse_path = PurePosixPath(self.reuse_prefix)
                if (
                    reuse_path.is_absolute()
                    or any(part in {"", ".", ".."} for part in reuse_path.parts)
                    or "\\" in self.reuse_prefix
                ):
                    raise ValueError("BoltzGen reuse prefix must be a safe relative archive path")
        elif self.yaml_name is not None or self.reuse_prefix is not None:
            raise ValueError("YAML and reuse fields are only valid for BoltzGen input materialization")


@dataclass(frozen=True, slots=True)
class RuntimeArtifactMount:
    """Deployment-neutral binding from a logical artifact to an image path."""

    artifact_id: str
    mount_path: str
    sub_path: str | None = None
    read_only: bool = True
    expected_content_sha256: str | None = None
    authorization_receipt_sha256: str | None = None
    readiness_receipt_sha256: str | None = None
    supplemental_groups: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if _ARTIFACT_ID_RE.fullmatch(self.artifact_id) is None:
            raise ValueError("runtime mount artifact_id must be a logical artifact ID")
        mount = PurePosixPath(self.mount_path)
        if not mount.is_absolute() or not any(root == mount or root in mount.parents for root in _RUNTIME_MOUNT_ROOTS):
            raise ValueError("runtime artifact mount must use an approved image root")
        if any(part in {"", ".", ".."} for part in mount.parts[1:]):
            raise ValueError("runtime artifact mount path is not canonical")
        if not self.read_only:
            raise ValueError("model and licensed runtime artifacts must be mounted read-only")
        if self.sub_path is not None:
            relative = PurePosixPath(self.sub_path)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("runtime artifact sub_path must be a safe relative path")
        for value, label in (
            (self.expected_content_sha256, "expected content digest"),
            (self.authorization_receipt_sha256, "authorization receipt digest"),
            (self.readiness_receipt_sha256, "readiness receipt digest"),
        ):
            if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"runtime artifact {label} must be a lowercase SHA-256")
        if len(self.supplemental_groups) != len(set(self.supplemental_groups)) or any(
            group < 1 or group > 2**31 - 1 for group in self.supplemental_groups
        ):
            raise ValueError("runtime artifact supplemental groups are invalid")


@dataclass(frozen=True, slots=True)
class StageInvocation:
    """Immutable exec-form workload payload attached to a controller stage."""

    stage_id: str
    shard_id: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    working_directory: str
    consumes: tuple[str, ...]
    produces: str
    materializations: tuple[ArtifactMaterialization, ...] = ()
    runtime_artifacts: tuple[str, ...] = ()
    runtime_mounts: tuple[RuntimeArtifactMount, ...] = ()

    def __post_init__(self) -> None:
        _check_name(self.stage_id, "stage_id")
        _check_name(self.shard_id, "shard_id")
        if not self.argv or self.argv[0] in {"sh", "bash", "/bin/sh", "/bin/bash"}:
            raise ValueError("stage argv must be a non-shell exec-form command")
        if any(not value or "\x00" in value for value in self.argv):
            raise ValueError("stage argv contains an invalid argument")
        if len(dict(self.environment)) != len(self.environment):
            raise ValueError("stage environment keys must be unique")
        for key, value in self.environment:
            if _ENV_NAME_RE.fullmatch(key) is None or _SENSITIVE_KEY_RE.search(key) or "\x00" in value:
                raise ValueError("stage environment contains an unsafe key or value")
        _check_controller_path(self.working_directory, "stage working_directory")
        for artifact_id in (*self.consumes, self.produces):
            if _ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
                raise ValueError("stage artifact handoff must use logical IDs")
        if len(set(self.consumes)) != len(self.consumes):
            raise ValueError("stage consumed artifact IDs must be unique")
        if len(set(self.runtime_artifacts)) != len(self.runtime_artifacts):
            raise ValueError("stage runtime artifact IDs must be unique")
        for artifact_id in self.runtime_artifacts:
            if _ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
                raise ValueError("stage runtime artifact IDs must be logical IDs")
        if self.runtime_mounts:
            mounted = tuple(item.artifact_id for item in self.runtime_mounts)
            if len(set(mounted)) != len(mounted) or set(mounted) != set(self.runtime_artifacts):
                raise ValueError("explicit runtime mounts must exactly bind stage runtime artifacts")
        materialized = tuple(item.artifact_id for item in self.materializations)
        if len(set(materialized)) != len(materialized) or not set(materialized).issubset(self.consumes):
            raise ValueError("materializations must uniquely reference consumed logical artifacts")


@dataclass(frozen=True, slots=True)
class AdapterExecutionPlan:
    """Canonical controller plan plus exact workload invocations."""

    model_id: str
    variant_id: str
    source_revision: str
    request_sha256: str
    controller_plan: ScientificBatchPlan
    invocations: tuple[StageInvocation, ...]
    required_model_artifacts: tuple[str, ...]

    def __post_init__(self) -> None:
        _check_name(self.model_id, "model_id")
        _check_name(self.variant_id, "variant_id")
        expected = {
            (stage.stage_id, shard or "gang") for stage in self.controller_plan.stages for shard in stage.workload_units
        }
        actual = {(item.stage_id, item.shard_id) for item in self.invocations}
        if actual != expected or len(actual) != len(self.invocations):
            raise ValueError("stage invocations do not exactly cover the controller plan")
        if len(set(self.required_model_artifacts)) != len(self.required_model_artifacts):
            raise ValueError("required model artifact IDs must be unique")
        for artifact_id in self.required_model_artifacts:
            if _ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
                raise ValueError("required model artifact IDs must be logical IDs")
        observed = {artifact_id for invocation in self.invocations for artifact_id in invocation.runtime_artifacts}
        if observed != set(self.required_model_artifacts):
            raise ValueError("stage runtime artifacts must exactly cover the execution plan requirements")

    def invocation(self, stage_id: str, shard_id: str | None) -> StageInvocation:
        key = (stage_id, shard_id or "gang")
        return next(item for item in self.invocations if (item.stage_id, item.shard_id) == key)


@dataclass(frozen=True, slots=True)
class StageSchedulingDecision:
    stage_id: str
    resolved_cluster_queue: str
    resolved_local_queue: str
    workload_priority_class: str
    workload_priority_value: int
    resolved_pool_preference: tuple[str, ...]
    admitted_resource_flavor: str | None
    accelerator_resource_name: str | None
    accelerator_count: int
    max_queue_seconds: int | None
    max_execution_seconds: int | None
    checkpoint_mode: CheckpointMode
    preemption_mode: PreemptionMode

    def __post_init__(self) -> None:
        _check_name(self.stage_id, "stage_id")
        for value, label in (
            (self.resolved_local_queue, "resolved_local_queue"),
            (self.resolved_cluster_queue, "resolved_cluster_queue"),
            (self.workload_priority_class, "workload_priority_class"),
        ):
            _check_name(value, label)
        if self.admitted_resource_flavor is not None:
            _check_name(self.admitted_resource_flavor, "admitted_resource_flavor")
        if len(self.resolved_pool_preference) != len(set(self.resolved_pool_preference)):
            raise ValueError("resolved_pool_preference must be unique")
        for pool in self.resolved_pool_preference:
            if not pool or len(pool) > 128 or _POOL_RE.fullmatch(pool) is None:
                raise ValueError("resolved pool names must be DNS-compatible and at most 128 characters")
        if not 0 <= self.accelerator_count <= 1024:
            raise ValueError("accelerator count must follow the Kueue scheduling contract")
        if self.accelerator_count == 0:
            if self.accelerator_resource_name is not None or self.resolved_pool_preference:
                raise ValueError("CPU scheduling cannot retain accelerator resource or pool preferences")
            if self.admitted_resource_flavor is not None:
                raise ValueError("CPU scheduling cannot retain an accelerator resource flavor")
        elif (
            self.accelerator_resource_name is None
            or len(self.accelerator_resource_name) > 253
            or _RESOURCE_NAME_RE.fullmatch(self.accelerator_resource_name) is None
            or not self.resolved_pool_preference
        ):
            raise ValueError("GPU scheduling requires a valid accelerator resource and pool preference")
        if self.max_queue_seconds is not None and self.max_queue_seconds < 1:
            raise ValueError("max_queue_seconds must be positive when set")
        if self.max_execution_seconds is not None and self.max_execution_seconds < 1:
            raise ValueError("max_execution_seconds must be positive when set")


@dataclass(frozen=True, slots=True)
class SchedulingSnapshot:
    """Admission-time scheduling decision; controllers must never refresh it."""

    policy_revision: str
    captured_at: datetime
    service_class: ServiceClass
    tenant_queue: str
    model_lane: str
    stages: tuple[StageSchedulingDecision, ...]

    def __post_init__(self) -> None:
        if not self.policy_revision or len(self.policy_revision) > 200:
            raise ValueError("policy_revision must be a stable non-empty identifier")
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if not isinstance(self.service_class, ServiceClass):
            raise ValueError("service_class must be a supported scheduling class")
        for value, label in ((self.tenant_queue, "tenant_queue"), (self.model_lane, "model_lane")):
            if not value or len(value) > 128 or _POOL_RE.fullmatch(value) is None:
                raise ValueError(f"{label} must be a bounded provider-neutral queue identity")
        ids = [stage.stage_id for stage in self.stages]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("scheduling snapshot stage identities must be non-empty and unique")

    def stage(self, stage_id: str) -> StageSchedulingDecision:
        return next(stage for stage in self.stages if stage.stage_id == stage_id)

    @property
    def digest(self) -> str:
        value = {
            "policy_revision": self.policy_revision,
            "captured_at": self.captured_at.isoformat(),
            "service_class": self.service_class,
            "tenant_queue": self.tenant_queue,
            "model_lane": self.model_lane,
            "stages": [
                {
                    "stage_id": item.stage_id,
                    "resolved_cluster_queue": item.resolved_cluster_queue,
                    "resolved_local_queue": item.resolved_local_queue,
                    "workload_priority_class": item.workload_priority_class,
                    "workload_priority_value": item.workload_priority_value,
                    "resolved_pool_preference": item.resolved_pool_preference,
                    "admitted_resource_flavor": item.admitted_resource_flavor,
                    "accelerator_resource_name": item.accelerator_resource_name,
                    "accelerator_count": item.accelerator_count,
                    "max_queue_seconds": item.max_queue_seconds,
                    "max_execution_seconds": item.max_execution_seconds,
                    "checkpoint_mode": item.checkpoint_mode,
                    "preemption_mode": item.preemption_mode,
                }
                for item in self.stages
            ],
        }
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(body).hexdigest()}"


@dataclass(frozen=True, slots=True)
class WorkloadRef:
    namespace: str
    name: str
    kind: WorkloadKind
    uid: str | None = None

    def __post_init__(self) -> None:
        _check_name(self.namespace, "namespace")
        _check_name(self.name, "workload name")


@dataclass(frozen=True, slots=True)
class ScientificAttemptState:
    attempt_id: UUID
    stage_id: str
    shard_id: str | None
    attempt_number: int
    workload: WorkloadRef
    outcome: AttemptOutcome = AttemptOutcome.ACTIVE
    last_phase: LifecyclePhase = LifecyclePhase.SCHEDULING
    failure_kind: FailureKind | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _check_name(self.stage_id, "stage_id")
        if self.shard_id is not None:
            _check_name(self.shard_id, "shard_id")
        if not 1 <= self.attempt_number <= 10:
            raise ValueError("attempt_number must be between 1 and 10")


@dataclass(frozen=True, slots=True)
class ScientificStageState:
    stage_id: str
    status: StageStatus = StageStatus.PENDING
    attempts: tuple[ScientificAttemptState, ...] = ()
    failure_code: str | None = None

    def latest_attempt(self, shard_id: str | None) -> ScientificAttemptState | None:
        matching = [attempt for attempt in self.attempts if attempt.shard_id == shard_id]
        return max(matching, key=lambda attempt: attempt.attempt_number, default=None)


@dataclass(frozen=True, slots=True)
class ScientificBatchState:
    """Batch extension keyed by an already-durable Operation ID."""

    operation_id: UUID
    batch_id: UUID
    workload_id: UUID
    tenant_id: str
    plan: ScientificBatchPlan
    scheduling: SchedulingSnapshot
    stages: tuple[ScientificStageState, ...]
    execution_plan: AdapterExecutionPlan | None = None
    model_id: str | None = None
    variant_id: str | None = None
    status: BatchStatus = BatchStatus.QUEUED
    revision: int = 0
    cancel_requested: bool = False
    failure_code: str | None = None

    def __post_init__(self) -> None:
        plan_ids = tuple(stage.stage_id for stage in self.plan.stages)
        record_ids = tuple(stage.stage_id for stage in self.stages)
        schedule_ids = tuple(stage.stage_id for stage in self.scheduling.stages)
        if record_ids != plan_ids:
            raise ValueError("stage records must match plan order exactly")
        if self.execution_plan is not None and self.execution_plan.controller_plan != self.plan:
            raise ValueError("adapter execution plan must contain the admitted controller plan")
        if self.execution_plan is not None and (
            self.model_id != self.execution_plan.model_id or self.variant_id != self.execution_plan.variant_id
        ):
            raise ValueError("batch model and variant identity must match its adapter execution plan")
        if (self.model_id is None) != (self.variant_id is None):
            raise ValueError("batch model_id and variant_id must be present together")
        if set(schedule_ids) != set(plan_ids):
            raise ValueError("the frozen scheduling snapshot must cover every stage exactly once")
        if len([stage for stage in self.stages if stage.status is StageStatus.ACTIVE]) > 1:
            raise ValueError("only one DAG stage may hold quota at a time")
        for plan in self.plan.stages:
            scheduling = self.scheduling.stage(plan.stage_id)
            if (
                scheduling.checkpoint_mode is not plan.checkpoint_mode
                or scheduling.preemption_mode is not plan.preemption_mode
            ):
                raise ValueError("the scheduling snapshot must retain catalog checkpoint and preemption modes")
            if plan.resource_class is ResourceClass.CPU and scheduling.accelerator_count != 0:
                raise ValueError("CPU stages cannot reserve accelerators")
            if plan.resource_class is ResourceClass.GPU and scheduling.accelerator_count < 1:
                raise ValueError("GPU stages require a positive accelerator count")

    @classmethod
    def admit(
        cls,
        *,
        operation_id: UUID,
        tenant_id: str,
        plan: ScientificBatchPlan,
        scheduling: SchedulingSnapshot,
        execution_plan: AdapterExecutionPlan | None = None,
    ) -> ScientificBatchState:
        return cls(
            operation_id=operation_id,
            batch_id=batch_identity(operation_id),
            workload_id=workload_identity(operation_id),
            tenant_id=tenant_id,
            plan=plan,
            scheduling=scheduling,
            stages=tuple(ScientificStageState(stage.stage_id) for stage in plan.stages),
            execution_plan=execution_plan,
            model_id=execution_plan.model_id if execution_plan is not None else None,
            variant_id=execution_plan.variant_id if execution_plan is not None else None,
        )

    def stage(self, stage_id: str) -> ScientificStageState:
        return next(stage for stage in self.stages if stage.stage_id == stage_id)


@dataclass(frozen=True, slots=True)
class ArtifactCommit:
    """One atomically visible, semantically evaluated stage manifest."""

    operation_id: UUID
    stage_id: str
    attempt_ids: tuple[UUID, ...]
    manifest_digest: str
    validation_digest: str
    committed_at: datetime
    validated_at: datetime
    semantic_valid: bool

    def __post_init__(self) -> None:
        _check_digest(self.manifest_digest, "manifest_digest")
        _check_digest(self.validation_digest, "validation_digest")
        if not self.attempt_ids or len(self.attempt_ids) != len(set(self.attempt_ids)):
            raise ValueError("artifact commit attempt IDs must be non-empty and unique")
        if self.committed_at.tzinfo is None or self.validated_at.tzinfo is None:
            raise ValueError("artifact commit timestamps must be timezone-aware")
        if self.validated_at < self.committed_at:
            raise ValueError("semantic validation cannot precede the atomic commit")


@dataclass(frozen=True, slots=True)
class WorkloadResource:
    operation_id: UUID
    batch_id: UUID
    workload_id: UUID
    attempt_id: UUID
    stage_id: str
    shard_id: str | None
    attempt_number: int
    namespace: str
    name: str
    kind: WorkloadKind
    scheduling: StageSchedulingDecision
    gang_size: int | None = None
    invocation: StageInvocation | None = None
    model_id: str | None = None
    variant_id: str | None = None
    resources: ScientificStageResources | None = None
    placement: ScientificStagePlacement | None = None
    storage: tuple[ScientificStorageRequirement, ...] = ()

    def __post_init__(self) -> None:
        _check_name(self.namespace, "namespace")
        _check_name(self.name, "workload name")
        _check_name(self.stage_id, "stage_id")
        if not 1 <= self.attempt_number <= 10:
            raise ValueError("attempt_number must be between 1 and 10")
        if self.kind is WorkloadKind.JOB:
            if self.shard_id is None or self.gang_size is not None:
                raise ValueError("fanout Jobs require a shard and cannot declare gang_size")
        elif self.shard_id is not None or self.gang_size is None or self.gang_size < 2:
            raise ValueError("a true-gang JobSet requires no shard and gang_size >= 2")
        if self.invocation is not None and (
            self.invocation.stage_id != self.stage_id or self.invocation.shard_id != (self.shard_id or "gang")
        ):
            raise ValueError("workload invocation identity must match the controller workload")
        if (self.model_id is None) != (self.variant_id is None):
            raise ValueError("workload model_id and variant_id must be present together")
        if self.resources is not None:
            expected_accelerators = self.resources.gpu_count * (self.gang_size or 1)
            if expected_accelerators != self.scheduling.accelerator_count:
                raise ValueError("catalog GPU count must match the frozen scheduling admission")
        if self.placement is not None:
            if self.placement.accelerator_resource_name != self.scheduling.accelerator_resource_name:
                raise ValueError("catalog accelerator resource must match scheduling admission")
            if self.placement.accelerator_count * (self.gang_size or 1) != self.scheduling.accelerator_count:
                raise ValueError("catalog accelerator count must match scheduling admission")
            if not set(self.scheduling.resolved_pool_preference).issubset(
                self.placement.compatible_pool_ids
            ):
                raise ValueError("scheduling pools must be compatible with catalog placement")
        if any(self.stage_id not in requirement.stages for requirement in self.storage):
            raise ValueError("workload storage must apply to its stage")

    @property
    def ref(self) -> WorkloadRef:
        return WorkloadRef(namespace=self.namespace, name=self.name, kind=self.kind)


@dataclass(frozen=True, slots=True)
class WorkloadObservation:
    ref: WorkloadRef
    attempt_id: UUID
    state: WorkloadState
    phases: tuple[LifecyclePhase, ...]
    failure_kind: FailureKind | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        ranks = [phase.rank for phase in self.phases]
        if ranks != sorted(set(ranks)):
            raise ValueError("observed lifecycle phases must be unique and monotonic")
        if self.state in {WorkloadState.FAILED, WorkloadState.PREEMPTED} and self.failure_kind is None:
            raise ValueError("failed and preempted observations require a failure kind")
        if self.state is WorkloadState.SUCCEEDED and self.failure_kind is not None:
            raise ValueError("succeeded observations cannot carry a failure kind")


@dataclass(frozen=True, slots=True)
class BatchClaim:
    operation_id: UUID
    controller_id: str
    fencing_token: int
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if not self.controller_id or self.fencing_token < 1:
            raise ValueError("a controller identity and positive fencing token are required")
        if self.lease_expires_at.tzinfo is None:
            raise ValueError("claim expiry must be timezone-aware")


@dataclass(frozen=True, slots=True)
class BatchEventDraft:
    event_id: str
    operation_id: UUID
    batch_id: UUID
    workload_id: UUID
    kind: BatchEventKind
    stage_id: str | None = None
    shard_id: str | None = None
    attempt_id: UUID | None = None
    phase: LifecyclePhase | None = None
    code: str | None = None
    model_id: str | None = None
    variant_id: str | None = None

    @classmethod
    def build(
        cls,
        *,
        operation_id: UUID,
        batch_id: UUID,
        workload_id: UUID,
        kind: BatchEventKind,
        stage_id: str | None = None,
        shard_id: str | None = None,
        attempt_id: UUID | None = None,
        phase: LifecyclePhase | None = None,
        code: str | None = None,
        model_id: str | None = None,
        variant_id: str | None = None,
    ) -> BatchEventDraft:
        identity = "|".join(
            (
                str(operation_id),
                str(batch_id),
                str(workload_id),
                kind,
                stage_id or "-",
                shard_id or "-",
                str(attempt_id) if attempt_id else "-",
                phase or "-",
                code or "-",
                model_id or "-",
                variant_id or "-",
            )
        )
        event_id = f"sha256:{hashlib.sha256(identity.encode()).hexdigest()}"
        return cls(
            event_id=event_id,
            operation_id=operation_id,
            batch_id=batch_id,
            workload_id=workload_id,
            kind=kind,
            stage_id=stage_id,
            shard_id=shard_id,
            attempt_id=attempt_id,
            phase=phase,
            code=code,
            model_id=model_id,
            variant_id=variant_id,
        )


@dataclass(frozen=True, slots=True)
class BatchEvent:
    sequence: int
    occurred_at: datetime
    draft: BatchEventDraft


def attempt_identity(operation_id: UUID, stage_id: str, shard_id: str | None, attempt_number: int) -> UUID:
    """Return a stable retry identity safe to derive again after a crash."""

    return uuid5(NAMESPACE_URL, f"fs2-scientific-batch:{operation_id}:{stage_id}:{shard_id}:{attempt_number}")


def batch_identity(operation_id: UUID) -> UUID:
    """Return the stable batch identity bound one-to-one to an Operation."""

    return uuid5(NAMESPACE_URL, f"fs2-scientific-batch:{operation_id}:batch")


def workload_identity(operation_id: UUID) -> UUID:
    """Return the stable logical workload identity shared by all retries."""

    return uuid5(NAMESPACE_URL, f"fs2-scientific-batch:{operation_id}:workload")


def workload_name(operation_id: UUID, stage_id: str, shard_id: str | None, attempt_number: int) -> str:
    suffix = hashlib.sha256(f"{operation_id}:{stage_id}:{shard_id}:{attempt_number}".encode()).hexdigest()[:12]
    prefix = f"fs2-{stage_id[:28]}-{(shard_id or 'gang')[:12]}-a{attempt_number}"
    return f"{prefix}-{suffix}"[:63].rstrip("-")
