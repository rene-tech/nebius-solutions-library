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
_RAW_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_VARIANT_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SENSITIVE_KEY_RE = re.compile(r"(?:token|secret|password|credential|private[_-]?key)", re.IGNORECASE)
_CONTROLLER_MOUNT_ROOT = PurePosixPath("/mnt/fs2-scientific")
_RUNTIME_MOUNT_ROOTS = tuple(
    PurePosixPath(value) for value in ("/models", "/databases", "/opt/fs2/artifacts", "/opt/fs2/academic")
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


def _check_variant(value: str) -> None:
    if not value or len(value) > 128 or _VARIANT_RE.fullmatch(value) is None:
        raise ValueError("variant_id must be a DNS-compatible dynamic model variant")


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


def _check_controller_path(value: str, label: str) -> None:
    path = PurePosixPath(value)
    if not path.is_absolute() or _CONTROLLER_MOUNT_ROOT not in path.parents:
        raise ValueError(f"{label} must be inside the controller-owned mount root")


@dataclass(frozen=True, slots=True)
class ArtifactMaterialization:
    """A logical artifact localized before a model invocation starts.

    ``artifact_id`` is deliberately logical: the controller resolves it to the
    original immutable input or to a fenced predecessor commit. Public callers
    never choose a filesystem path.
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
class ScientificInputArtifact:
    """Verified projection of one entry in the public input manifest."""

    logical_artifact_id: str
    semantic_type: str
    artifact_id: UUID
    digest: str
    size_bytes: int
    media_type: str
    compression: str | None = None

    def __post_init__(self) -> None:
        if _ARTIFACT_ID_RE.fullmatch(self.logical_artifact_id) is None:
            raise ValueError("input logical artifact ID is invalid")
        if re.fullmatch(r"^[a-z][a-z0-9_.-]*/v[1-9][0-9]*$", self.semantic_type) is None:
            raise ValueError("input semantic type is invalid")
        _check_digest(self.digest, "input artifact digest")
        if not 0 <= self.size_bytes <= 128 * 1024 * 1024 * 1024:
            raise ValueError("input artifact size is outside the controller bound")
        if re.fullmatch(r"^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$", self.media_type) is None:
            raise ValueError("input artifact media type is invalid")
        if self.compression not in {None, "none", "gzip", "zstd"}:
            raise ValueError("input artifact compression is unsupported")


@dataclass(frozen=True, slots=True)
class VerifiedInputManifest:
    """Manifest and contained entries verified through the artifact service."""

    manifest_id: str
    manifest_artifact_id: UUID
    manifest_digest: str
    entries: tuple[ScientificInputArtifact, ...]

    def __post_init__(self) -> None:
        if _ARTIFACT_ID_RE.fullmatch(self.manifest_id) is None:
            raise ValueError("input manifest identity is invalid")
        _check_digest(self.manifest_digest, "input manifest digest")
        logical = tuple(item.logical_artifact_id for item in self.entries)
        artifact_ids = tuple(item.artifact_id for item in self.entries)
        if not logical or len(logical) != len(set(logical)) or len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("input manifest entries must be non-empty and uniquely identified")

    def artifact(self, logical_artifact_id: str) -> ScientificInputArtifact:
        matches = tuple(item for item in self.entries if item.logical_artifact_id == logical_artifact_id)
        if len(matches) != 1:
            raise ValueError("logical input artifact is absent or ambiguous")
        return matches[0]


@dataclass(frozen=True, slots=True)
class ScientificInputAdmission:
    """One fully resolved public input plus its non-secret access admission."""

    manifest: VerifiedInputManifest
    access_context: ArtifactAccessContext


@dataclass(frozen=True, slots=True)
class RuntimeArtifactFile:
    path: str
    digest: str
    size_bytes: int

    def __post_init__(self) -> None:
        relative = PurePosixPath(self.path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("runtime artifact file path is unsafe")
        _check_digest(self.digest, "runtime artifact file digest")
        if not 0 <= self.size_bytes <= 128 * 1024 * 1024 * 1024:
            raise ValueError("runtime artifact file size is outside the controller bound")


@dataclass(frozen=True, slots=True)
class RuntimeArtifactLocalization:
    """Trusted, exact localization proof frozen before workload admission."""

    logical_artifact_id: str
    mount_path: str
    content_digest: str
    files: tuple[RuntimeArtifactFile, ...]
    localization_receipt_digest: str

    def __post_init__(self) -> None:
        if _ARTIFACT_ID_RE.fullmatch(self.logical_artifact_id) is None:
            raise ValueError("runtime artifact logical ID is invalid")
        path = PurePosixPath(self.mount_path)
        if not path.is_absolute() or path == PurePosixPath("/") or path.as_posix() != self.mount_path:
            raise ValueError("runtime artifact mount path must be normalized and absolute")
        _check_digest(self.content_digest, "runtime artifact content digest")
        _check_digest(self.localization_receipt_digest, "runtime artifact localization receipt")
        paths = tuple(item.path for item in self.files)
        if not paths or len(paths) != len(set(paths)):
            raise ValueError("runtime artifact file manifest must be non-empty and unique")


@dataclass(frozen=True, slots=True)
class RuntimeArtifactMount:
    """Exact adapter binding from a logical runtime artifact to an approved image path."""

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
            if value is not None and _RAW_SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"runtime artifact {label} must be a lowercase SHA-256")
        if len(self.supplemental_groups) != len(set(self.supplemental_groups)) or any(
            group < 1 or group > 2**31 - 1 for group in self.supplemental_groups
        ):
            raise ValueError("runtime artifact supplemental groups are invalid")


@dataclass(frozen=True, slots=True)
class StageInvocation:
    """Immutable, shell-free workload payload attached to one attempt unit."""

    stage_id: str
    shard_id: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    working_directory: str
    consumes: tuple[str, ...]
    produces: str
    collector_id: str
    validator_id: str
    handoff_name: str | None
    max_output_artifacts: int = 1024
    max_output_bytes: int = 128 * 1024 * 1024 * 1024
    materializations: tuple[ArtifactMaterialization, ...] = ()
    runtime_artifacts: tuple[str, ...] = ()
    runtime_mounts: tuple[RuntimeArtifactMount, ...] = ()

    def __post_init__(self) -> None:
        _check_name(self.stage_id, "stage_id")
        _check_name(self.shard_id, "shard_id")
        if not self.argv or self.argv[0] in {"sh", "bash", "/bin/sh", "/bin/bash"} or "-c" in self.argv[:3]:
            raise ValueError("stage argv must be a non-shell exec-form command")
        if any(not value or "\x00" in value for value in self.argv):
            raise ValueError("stage argv contains an invalid argument")
        if len(dict(self.environment)) != len(self.environment):
            raise ValueError("stage environment keys must be unique")
        for key, value in self.environment:
            if _ENV_NAME_RE.fullmatch(key) is None or _SENSITIVE_KEY_RE.search(key) or "\x00" in value:
                raise ValueError("stage environment contains an unsafe key or value")
        _check_controller_path(self.working_directory, "stage working_directory")
        for artifact_id in (*self.consumes, self.produces, *self.runtime_artifacts):
            if _ARTIFACT_ID_RE.fullmatch(artifact_id) is None:
                raise ValueError("stage artifact handoff must use logical IDs")
        if len(set(self.consumes)) != len(self.consumes):
            raise ValueError("stage consumed artifact IDs must be unique")
        if len(set(self.runtime_artifacts)) != len(self.runtime_artifacts):
            raise ValueError("stage runtime artifact IDs must be unique")
        mounted = tuple(item.artifact_id for item in self.runtime_mounts)
        if len(set(mounted)) != len(mounted) or set(mounted) != set(self.runtime_artifacts):
            raise ValueError("runtime mounts must exactly bind every stage runtime artifact")
        marker_path = f"{self.working_directory}/.fs2/runtime-localization.json"
        if self.runtime_artifacts and marker_path not in self.argv:
            raise ValueError("runtime artifact stages must pass the canonical localization marker to argv")
        materialized = tuple(item.artifact_id for item in self.materializations)
        if len(set(materialized)) != len(materialized) or set(materialized) != set(self.consumes):
            raise ValueError("materializations must cover every consumed logical artifact exactly once")
        for value, label in ((self.collector_id, "collector_id"), (self.validator_id, "validator_id")):
            if not value or len(value) > 128 or re.fullmatch(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$", value) is None:
                raise ValueError(f"{label} must be a stable bounded identity")
        if self.handoff_name is not None and _ARTIFACT_ID_RE.fullmatch(self.handoff_name) is None:
            raise ValueError("handoff_name must be a canonical manifest entry name")
        if not 1 <= self.max_output_artifacts <= 10_000:
            raise ValueError("max_output_artifacts is outside the manifest bound")
        if not 1 <= self.max_output_bytes <= 128 * 1024 * 1024 * 1024:
            raise ValueError("max_output_bytes is outside the artifact bound")


@dataclass(frozen=True, slots=True)
class AdapterExecutionPlan:
    """The single catalog-adapter-to-controller execution contract."""

    model_id: str
    variant_id: str
    source_revision: str
    request_sha256: str
    controller_plan: ScientificBatchPlan
    invocations: tuple[StageInvocation, ...]
    required_model_artifacts: tuple[str, ...]

    def __post_init__(self) -> None:
        _check_name(self.model_id, "model_id")
        _check_variant(self.variant_id)
        if _RAW_SHA256_RE.fullmatch(self.request_sha256) is None:
            raise ValueError("request_sha256 must be a lowercase SHA-256")
        if not self.source_revision or len(self.source_revision) > 200:
            raise ValueError("source_revision must be a stable bounded identity")
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
        observed = {artifact_id for item in self.invocations for artifact_id in item.runtime_artifacts}
        if observed != set(self.required_model_artifacts):
            raise ValueError("stage runtime artifacts must exactly cover the execution plan requirements")
        produced = [item.produces for item in self.invocations]
        if len(produced) != len(set(produced)):
            raise ValueError("each stage invocation must produce a unique logical artifact")
        producer_stage = {item.produces: item.stage_id for item in self.invocations}
        consumed_outputs = {
            logical_id for item in self.invocations for logical_id in item.consumes if logical_id in producer_stage
        }
        if any(item.produces in consumed_outputs and item.handoff_name is None for item in self.invocations):
            raise ValueError("a predecessor consumed downstream must declare an exact handoff entry")
        for item in self.invocations:
            for logical_id in item.consumes:
                stage_id = producer_stage.get(logical_id)
                if stage_id is not None and stage_id not in self.controller_plan.stage(item.stage_id).depends_on:
                    raise ValueError("a stage may consume only original input or direct predecessor artifacts")
        depended_on = {dependency for stage in self.controller_plan.stages for dependency in stage.depends_on}
        sink_stages = {stage.stage_id for stage in self.controller_plan.stages if stage.stage_id not in depended_on}
        if len(sink_stages) != 1 or sum(item.stage_id in sink_stages for item in self.invocations) != 1:
            raise ValueError("an executable plan requires one canonical terminal output invocation")

    def invocation(self, stage_id: str, shard_id: str | None) -> StageInvocation:
        key = (stage_id, shard_id or "gang")
        return next(item for item in self.invocations if (item.stage_id, item.shard_id) == key)

    def producer(self, logical_artifact_id: str) -> StageInvocation | None:
        return next((item for item in self.invocations if item.produces == logical_artifact_id), None)


@dataclass(frozen=True, slots=True)
class ArtifactAccessContext:
    """Frozen non-secret tenant/access admission for artifact companions."""

    profile: str
    receipt_digest: str | None
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        if self.profile not in {"public", "restricted", "academic"}:
            raise ValueError("artifact access profile is unsupported")
        if self.profile == "public" and self.receipt_digest is not None:
            raise ValueError("public artifact access cannot carry a receipt")
        if self.profile != "public":
            if self.receipt_digest is None:
                raise ValueError("gated artifact access requires a receipt digest")
            _check_digest(self.receipt_digest, "artifact access receipt")
        if self.tenant_id is not None and (not self.tenant_id or len(self.tenant_id) > 120):
            raise ValueError("artifact access tenant is invalid")


PUBLIC_ARTIFACT_ACCESS_CONTEXT = ArtifactAccessContext(profile="public", receipt_digest=None)


@dataclass(frozen=True, slots=True)
class ResolvedArtifactMaterialization:
    """Attempt-local resolution of one logical input to an artifact-service ID."""

    logical_artifact_id: str
    artifact_id: UUID
    digest: str
    size_bytes: int
    media_type: str
    destination: str
    mode: MaterializationMode
    compression: str | None = None
    yaml_name: str | None = None
    reuse_prefix: str | None = None

    @classmethod
    def resolve(
        cls,
        source: ArtifactMaterialization,
        *,
        artifact_id: UUID,
        digest: str,
        size_bytes: int,
        media_type: str,
        compression: str | None,
    ) -> ResolvedArtifactMaterialization:
        return cls(
            logical_artifact_id=source.artifact_id,
            artifact_id=artifact_id,
            digest=digest,
            size_bytes=size_bytes,
            media_type=media_type,
            destination=source.destination,
            mode=source.mode,
            compression=source.compression or compression,
            yaml_name=source.yaml_name,
            reuse_prefix=source.reuse_prefix,
        )

    def __post_init__(self) -> None:
        ArtifactMaterialization(
            artifact_id=self.logical_artifact_id,
            destination=self.destination,
            mode=self.mode,
            compression=self.compression,
            yaml_name=self.yaml_name,
            reuse_prefix=self.reuse_prefix,
        )
        ScientificInputArtifact(
            logical_artifact_id=self.logical_artifact_id,
            semantic_type="resolved-artifact/v1",
            artifact_id=self.artifact_id,
            digest=self.digest,
            size_bytes=self.size_bytes,
            media_type=self.media_type,
            compression=self.compression,
        )


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
class SchedulingAdmission:
    """Exact Kueue admission observed after the immutable request snapshot."""

    resolved_pool_id: str | None
    admitted_resource_flavor: str | None
    accelerator_resource_name: str | None
    accelerator_count: int
    admitted_at: datetime

    def __post_init__(self) -> None:
        if self.admitted_at.tzinfo is None:
            raise ValueError("Kueue admission time must be timezone-aware")
        if not 0 <= self.accelerator_count <= 1024:
            raise ValueError("Kueue admitted accelerator count is invalid")
        if self.accelerator_count:
            if self.resolved_pool_id is None or self.admitted_resource_flavor is None:
                raise ValueError("GPU admission requires an exact pool and ResourceFlavor")
            if (
                self.accelerator_resource_name is None
                or _RESOURCE_NAME_RE.fullmatch(self.accelerator_resource_name) is None
            ):
                raise ValueError("GPU admission requires an exact accelerator resource")
        elif any(
            value is not None
            for value in (self.resolved_pool_id, self.admitted_resource_flavor, self.accelerator_resource_name)
        ):
            raise ValueError("CPU admission cannot claim accelerator placement")
        for value, label in (
            (self.resolved_pool_id, "resolved_pool_id"),
            (self.admitted_resource_flavor, "admitted_resource_flavor"),
        ):
            if value is not None and (len(value) > 128 or _POOL_RE.fullmatch(value) is None):
                raise ValueError(f"{label} is invalid")


@dataclass(frozen=True, slots=True)
class ScientificAttemptState:
    attempt_id: UUID
    stage_id: str
    shard_id: str | None
    attempt_number: int
    workload: WorkloadRef
    started_at: datetime | None = None
    completed_at: datetime | None = None
    outcome: AttemptOutcome = AttemptOutcome.ACTIVE
    last_phase: LifecyclePhase = LifecyclePhase.SCHEDULING
    resource_released: bool = False
    scheduling_admission: SchedulingAdmission | None = None
    kueue_workload_uid: str | None = None
    pod_uids: tuple[str, ...] = ()
    failure_kind: FailureKind | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _check_name(self.stage_id, "stage_id")
        if self.shard_id is not None:
            _check_name(self.shard_id, "shard_id")
        if not 1 <= self.attempt_number <= 10:
            raise ValueError("attempt_number must be between 1 and 10")
        if self.started_at is not None and self.started_at.tzinfo is None:
            raise ValueError("attempt started_at must be timezone-aware")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError("attempt completed_at must be timezone-aware")
        if self.completed_at is not None and (self.started_at is None or self.completed_at < self.started_at):
            raise ValueError("attempt completion cannot precede its start")
        if self.started_at is not None and ((self.outcome is AttemptOutcome.ACTIVE) != (self.completed_at is None)):
            raise ValueError("only terminal attempts carry completed_at")
        if self.resource_released and self.outcome is AttemptOutcome.ACTIVE:
            raise ValueError("an active attempt cannot have released its workload resource")
        for values, label in ((self.pod_uids, "Pod UID"),):
            if len(values) != len(set(values)) or any(not value or len(value) > 128 for value in values):
                raise ValueError(f"scientific attempt {label} identities are invalid")
        if self.kueue_workload_uid is not None and (not self.kueue_workload_uid or len(self.kueue_workload_uid) > 128):
            raise ValueError("scientific attempt Kueue Workload UID is invalid")


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
    model_id: str
    variant_id: str
    input_artifact_id: UUID
    plan: ScientificBatchPlan
    scheduling: SchedulingSnapshot
    stages: tuple[ScientificStageState, ...]
    execution_plan: AdapterExecutionPlan | None = None
    access_context: ArtifactAccessContext = PUBLIC_ARTIFACT_ACCESS_CONTEXT
    input_manifest: VerifiedInputManifest | None = None
    runtime_artifacts: tuple[RuntimeArtifactLocalization, ...] = ()
    status: BatchStatus = BatchStatus.QUEUED
    revision: int = 0
    cancel_requested: bool = False
    failure_code: str | None = None
    result_published: bool = False

    def __post_init__(self) -> None:
        _check_variant(self.variant_id)
        if self.execution_plan is not None and (
            self.execution_plan.controller_plan != self.plan
            or self.execution_plan.model_id != self.model_id
            or self.execution_plan.variant_id != self.variant_id
        ):
            raise ValueError("adapter execution plan differs from the frozen batch admission")
        if self.execution_plan is not None:
            if self.input_manifest is None or self.input_manifest.manifest_artifact_id != self.input_artifact_id:
                raise ValueError("adapter execution requires a verified contained input manifest")
            required = set(self.execution_plan.required_model_artifacts)
            localized = {item.logical_artifact_id for item in self.runtime_artifacts}
            if required != localized or len(localized) != len(self.runtime_artifacts):
                raise ValueError("every adapter runtime artifact requires one frozen localization proof")
            produced = {item.produces for item in self.execution_plan.invocations}
            available_inputs = {item.logical_artifact_id for item in self.input_manifest.entries}
            for invocation in self.execution_plan.invocations:
                if not set(invocation.consumes).issubset(available_inputs | produced):
                    raise ValueError("stage invocation consumes an unverified logical artifact")
            if self.access_context.tenant_id != self.tenant_id:
                raise ValueError("artifact access context is not bound to the workload tenant")
        plan_ids = tuple(stage.stage_id for stage in self.plan.stages)
        record_ids = tuple(stage.stage_id for stage in self.stages)
        schedule_ids = tuple(stage.stage_id for stage in self.scheduling.stages)
        if record_ids != plan_ids:
            raise ValueError("stage records must match plan order exactly")
        if set(schedule_ids) != set(plan_ids):
            raise ValueError("the frozen scheduling snapshot must cover every stage exactly once")
        if len([stage for stage in self.stages if stage.status is StageStatus.ACTIVE]) > 1:
            raise ValueError("only one DAG stage may hold quota at a time")
        if any(
            stage.status.terminal and any(not attempt.resource_released for attempt in stage.attempts)
            for stage in self.stages
        ):
            raise ValueError("a terminal stage cannot retain Kubernetes workload resources")
        if self.status.terminal and any(
            not attempt.resource_released for stage in self.stages for attempt in stage.attempts
        ):
            raise ValueError("a terminal batch cannot retain Kubernetes workload resources")
        if self.result_published and not self.status.terminal:
            raise ValueError("only a terminal batch can publish its immutable result")
        for plan in self.plan.stages:
            scheduling = self.scheduling.stage(plan.stage_id)
            if scheduling.checkpoint_mode is not plan.checkpoint_mode:
                raise ValueError("the scheduling snapshot must retain the catalog checkpoint mode")
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
        model_id: str,
        variant_id: str,
        input_artifact_id: UUID,
        execution_plan: AdapterExecutionPlan | None = None,
        access_context: ArtifactAccessContext = PUBLIC_ARTIFACT_ACCESS_CONTEXT,
        input_manifest: VerifiedInputManifest | None = None,
        runtime_artifacts: tuple[RuntimeArtifactLocalization, ...] = (),
    ) -> ScientificBatchState:
        _check_name(model_id, "model_id")
        _check_variant(variant_id)
        return cls(
            operation_id=operation_id,
            batch_id=batch_identity(operation_id),
            workload_id=workload_identity(operation_id),
            tenant_id=tenant_id,
            model_id=model_id,
            variant_id=variant_id,
            input_artifact_id=input_artifact_id,
            plan=plan,
            scheduling=scheduling,
            stages=tuple(ScientificStageState(stage.stage_id) for stage in plan.stages),
            execution_plan=execution_plan,
            access_context=access_context,
            input_manifest=input_manifest,
            runtime_artifacts=runtime_artifacts,
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
class AttemptArtifactCommit:
    """Validated logical output from exactly one fenced stage attempt."""

    operation_id: UUID
    stage_id: str
    attempt_ids: tuple[UUID, ...]
    logical_artifact_id: str
    handoff_artifact_id: UUID
    handoff_digest: str
    handoff_size_bytes: int
    handoff_media_type: str
    handoff_compression: str | None
    manifest_artifact_id: UUID
    validation_artifact_id: UUID
    manifest_digest: str
    validation_digest: str
    committed_at: datetime
    validated_at: datetime
    semantic_valid: bool
    collector_id: str = "legacy-collector"
    validator_id: str = "legacy-validator"

    def __post_init__(self) -> None:
        _check_digest(self.manifest_digest, "manifest_digest")
        _check_digest(self.validation_digest, "validation_digest")
        _check_digest(self.handoff_digest, "handoff_digest")
        if not 0 <= self.handoff_size_bytes <= 128 * 1024 * 1024 * 1024:
            raise ValueError("handoff artifact size is outside the controller bound")
        if re.fullmatch(r"^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$", self.handoff_media_type) is None:
            raise ValueError("handoff media type is invalid")
        if self.handoff_compression not in {None, "none", "gzip", "zstd"}:
            raise ValueError("handoff compression is unsupported")
        if not self.attempt_ids or len(self.attempt_ids) != len(set(self.attempt_ids)):
            raise ValueError("attempt artifact commit IDs must be non-empty and unique")
        if len(self.attempt_ids) != 1:
            raise ValueError("each attempt artifact commit must fence exactly one attempt")
        if _ARTIFACT_ID_RE.fullmatch(self.logical_artifact_id) is None:
            raise ValueError("artifact commit logical ID is invalid")
        if self.manifest_artifact_id == self.validation_artifact_id:
            raise ValueError("manifest and validation artifacts must differ")
        if self.committed_at.tzinfo is None or self.validated_at.tzinfo is None:
            raise ValueError("artifact commit timestamps must be timezone-aware")
        if self.validated_at < self.committed_at:
            raise ValueError("semantic validation cannot precede the atomic commit")
        for value, label in ((self.collector_id, "collector_id"), (self.validator_id, "validator_id")):
            if not value or len(value) > 128 or re.fullmatch(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$", value) is None:
                raise ValueError(f"artifact commit {label} is invalid")


@dataclass(frozen=True, slots=True)
class WorkloadResource:
    operation_id: UUID
    batch_id: UUID
    workload_id: UUID
    attempt_id: UUID
    stage_id: str
    shard_id: str | None
    attempt_number: int
    tenant_id: str
    model_id: str
    variant_id: str
    input_artifact_id: UUID
    service_class: ServiceClass
    scheduling_snapshot_digest: str
    namespace: str
    name: str
    kind: WorkloadKind
    scheduling: StageSchedulingDecision
    gang_size: int | None = None
    invocation: StageInvocation | None = None
    materializations: tuple[ResolvedArtifactMaterialization, ...] = ()
    access_context: ArtifactAccessContext = PUBLIC_ARTIFACT_ACCESS_CONTEXT
    runtime_artifacts: tuple[RuntimeArtifactLocalization, ...] = ()

    def __post_init__(self) -> None:
        _check_name(self.namespace, "namespace")
        _check_name(self.name, "workload name")
        _check_name(self.stage_id, "stage_id")
        _check_name(self.model_id, "model_id")
        _check_variant(self.variant_id)
        _check_digest(self.scheduling_snapshot_digest, "scheduling_snapshot_digest")
        if not self.tenant_id or len(self.tenant_id) > 120:
            raise ValueError("tenant_id must be non-empty and at most 120 characters")
        if not 1 <= self.attempt_number <= 10:
            raise ValueError("attempt_number must be between 1 and 10")
        if self.kind is WorkloadKind.JOB:
            if self.shard_id is None or self.gang_size is not None:
                raise ValueError("fanout Jobs require a shard and cannot declare gang_size")
        elif self.shard_id is not None or self.gang_size is None or self.gang_size < 2:
            raise ValueError("a true-gang JobSet requires no shard and gang_size >= 2")
        if self.invocation is not None:
            if self.invocation.stage_id != self.stage_id or self.invocation.shard_id != (self.shard_id or "gang"):
                raise ValueError("workload invocation identity differs from its attempt")
            logical = tuple(item.logical_artifact_id for item in self.materializations)
            expected = tuple(item.artifact_id for item in self.invocation.materializations)
            if logical != expected:
                raise ValueError("resolved materializations must preserve invocation order and identity")
            if self.access_context.tenant_id != self.tenant_id:
                raise ValueError("workload artifact access is not bound to its tenant")
        elif self.materializations:
            raise ValueError("resolved materializations require a stage invocation")
        if self.invocation is not None:
            required = set(self.invocation.runtime_artifacts)
            localized = {item.logical_artifact_id for item in self.runtime_artifacts}
            if required != localized or len(localized) != len(self.runtime_artifacts):
                raise ValueError("workload runtime artifacts are not exactly localized")

    @property
    def ref(self) -> WorkloadRef:
        return WorkloadRef(namespace=self.namespace, name=self.name, kind=self.kind)


@dataclass(frozen=True, slots=True)
class WorkloadObservation:
    ref: WorkloadRef
    attempt_id: UUID
    state: WorkloadState
    phases: tuple[LifecyclePhase, ...]
    scheduling_admission: SchedulingAdmission | None = None
    kueue_workload_uid: str | None = None
    pod_uids: tuple[str, ...] = ()
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
        if len(self.pod_uids) != len(set(self.pod_uids)) or any(
            not value or len(value) > 128 for value in self.pod_uids
        ):
            raise ValueError("observed Pod UIDs must be unique bounded identities")
        if self.kueue_workload_uid is not None and (not self.kueue_workload_uid or len(self.kueue_workload_uid) > 128):
            raise ValueError("observed Kueue Workload UID is invalid")


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
