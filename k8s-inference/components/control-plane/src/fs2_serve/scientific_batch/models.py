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
from uuid import NAMESPACE_URL, UUID, uuid5

_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
_POOL_RE = re.compile(r"^[a-z0-9](?:[-_a-z0-9.]*[a-z0-9])?$")
_RESOURCE_NAME_RE = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9][A-Za-z0-9._-]*$")
_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


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


class ServiceClass(StrEnum):
    PRESENTATION = "presentation"
    INTERACTIVE = "interactive"
    CUSTOMER_BATCH = "customer-batch"
    BULK_BACKFILL = "bulk-backfill"


class WorkloadKind(StrEnum):
    JOB = "Job"
    JOB_SET = "JobSet"


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

    @property
    def workload_units(self) -> tuple[str | None, ...]:
        if self.mode is ExecutionMode.TRUE_GANG:
            return (None,)
        return self.shards


@dataclass(frozen=True, slots=True)
class ScientificBatchPlan:
    stages: tuple[ScientificStagePlan, ...]

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

    def stage(self, stage_id: str) -> ScientificStagePlan:
        return next(stage for stage in self.stages if stage.stage_id == stage_id)


@dataclass(frozen=True, slots=True)
class StageSchedulingDecision:
    stage_id: str
    resolved_cluster_queue: str
    resolved_local_queue: str
    workload_priority_class: str
    workload_priority_value: int
    resolved_pool_preference: tuple[str, ...]
    admitted_resource_flavor: str | None
    accelerator_resource_name: str
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
        if not self.resolved_pool_preference or len(self.resolved_pool_preference) != len(
            set(self.resolved_pool_preference)
        ):
            raise ValueError("resolved_pool_preference must be non-empty and unique")
        for pool in self.resolved_pool_preference:
            if not pool or len(pool) > 128 or _POOL_RE.fullmatch(pool) is None:
                raise ValueError("resolved pool names must be DNS-compatible and at most 128 characters")
        if (
            len(self.accelerator_resource_name) > 253
            or _RESOURCE_NAME_RE.fullmatch(self.accelerator_resource_name) is None
            or not 0 <= self.accelerator_count <= 1024
        ):
            raise ValueError("accelerator resource name and count must follow the Kueue scheduling contract")
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
    ) -> ScientificBatchState:
        return cls(
            operation_id=operation_id,
            batch_id=batch_identity(operation_id),
            workload_id=workload_identity(operation_id),
            tenant_id=tenant_id,
            plan=plan,
            scheduling=scheduling,
            stages=tuple(ScientificStageState(stage.stage_id) for stage in plan.stages),
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
