"""Immutable domain records for staged scientific batches.

The records intentionally contain no API, PostgreSQL, or Kubernetes client
types.  They are the integration seam between those owners and the controller.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
_POOL_RE = re.compile(r"^[a-z0-9](?:[-_a-z0-9.]*[a-z0-9])?$")
_RESOURCE_NAME_RE = re.compile(
    r"^(?=[^/]{1,253}/)(?:[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?/"
    r"[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$"
)
_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_RAW_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SHA256_RE = _RAW_SHA256_RE
_VARIANT_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SENSITIVE_KEY_RE = re.compile(r"(?:token|secret|password|credential|private[_-]?key)", re.IGNORECASE)
_CONTROLLER_MOUNT_ROOT = PurePosixPath("/mnt/fs2-scientific")
_RUNTIME_MOUNT_ROOTS = tuple(
    PurePosixPath(value)
    for value in ("/models", "/databases", "/reference-data", "/opt/fs2/artifacts", "/opt/fs2/academic")
)
_RUNTIME_EXACT_MOUNT_PATHS = {
    PurePosixPath("/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights"),
    PurePosixPath("/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble"),
}

# The staged Pod runs exactly two regular containers: the model itself and the
# artifact collector that publishes the model's output.  Both the renderer and
# the Kubernetes observer address them by these names, so the collection
# handshake reads the same identities that the renderer wrote.
STAGE_CONTAINER_NAME = "scientific-stage"
COLLECTOR_CONTAINER_NAME = "artifact-collector"

# A model container that terminated successfully has already written its
# output, so the collector only has to publish it.  The bound is deliberately
# generous, because expiring it kills a collection that may still be uploading
# a large artifact; it only has to be far shorter than the multi-hour
# ``activeDeadlineSeconds`` of a scientific stage.  A model that exited
# non-zero gets no grace at all -- its result can never arrive.
COLLECTION_GRACE_SECONDS = 1800

# The collector fails on its own this far before the Job's active deadline, so
# a stalled collection still yields a deterministic controller failure code
# rather than an opaque Kubernetes ``DeadlineExceeded`` kill.
COLLECTION_DEADLINE_MARGIN_SECONDS = 120

STATE_SCHEMA = "fs2-serve.nebius.ai/scientific-batch-state/v8"
PREVIOUS_STATE_SCHEMA = "fs2-serve.nebius.ai/scientific-batch-state/v7"
LEGACY_STATE_SCHEMA = "fs2-serve.nebius.ai/scientific-batch-state/v6"
READABLE_STATE_SCHEMAS = (LEGACY_STATE_SCHEMA, PREVIOUS_STATE_SCHEMA, STATE_SCHEMA)
LEGACY_ADMISSION_FAILURE_CODE = "legacy_state_incompatible"


class ScientificIdentityError(LookupError):
    """A frozen admission has no record for a requested stage or shard identity.

    Every identity lookup below raises this instead of letting a bare
    ``StopIteration`` escape.  Inside a coroutine that would surface as an
    opaque ``RuntimeError``, which a legacy row reopened by a newer controller
    can otherwise trigger for a whole reconcile loop.
    """


_Frozen = TypeVar("_Frozen")


def _only(matches: Iterator[_Frozen], message: str) -> _Frozen:
    match = next(matches, None)
    if match is None:
        raise ScientificIdentityError(message)
    return match


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


class StagePlacementClass(StrEnum):
    """Operator-owned placement lane frozen independently for every stage."""

    REFERENCE_DATA_CPU = "reference-data"
    GENERAL_CPU = "general-cpu"
    ACCELERATOR = "accelerator"


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


class RuntimeArtifactTreeKind(StrEnum):
    """Physical publication/verifier contract for one bounded tree identity."""

    LOCALIZATION_GENERATION = "localization-generation"
    REFERENCE_DATA_PLANE = "reference-data-plane"


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
class StageResourceEnvelope:
    """Exact request and limit bytes frozen from the catalog profile."""

    cpu_millis: int
    memory_bytes: int
    ephemeral_storage_bytes: int
    limit_cpu_millis: int
    limit_memory_bytes: int
    limit_ephemeral_storage_bytes: int

    def __post_init__(self) -> None:
        values = (
            self.cpu_millis,
            self.memory_bytes,
            self.ephemeral_storage_bytes,
            self.limit_cpu_millis,
            self.limit_memory_bytes,
            self.limit_ephemeral_storage_bytes,
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in values):
            raise ValueError("stage resource requests and limits must be positive integers")
        if self.cpu_millis > 512_000 or self.limit_cpu_millis > 512_000:
            raise ValueError("stage CPU resources exceed the controller bound")
        if (
            self.limit_cpu_millis < self.cpu_millis
            or self.limit_memory_bytes < self.memory_bytes
            or self.limit_ephemeral_storage_bytes < self.ephemeral_storage_bytes
        ):
            raise ValueError("stage resource limits cannot be smaller than requests")


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
    placement_class: StagePlacementClass | None = None
    resources: StageResourceEnvelope | None = None

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
        if self.placement_class is not None:
            if self.resource_class is ResourceClass.GPU and self.placement_class is not StagePlacementClass.ACCELERATOR:
                raise ValueError("GPU stages require the accelerator placement class")
            if self.resource_class is ResourceClass.CPU and self.placement_class is StagePlacementClass.ACCELERATOR:
                raise ValueError("CPU stages require a CPU placement class")
        if (self.placement_class is None) != (self.resources is None):
            raise ValueError("stage placement and resources must be frozen together")

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
        return _only(
            (stage for stage in self.stages if stage.stage_id == stage_id),
            f"frozen plan has no stage {stage_id!r}",
        )


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
class RuntimeArtifactAggregateTree:
    """Bounded identity for a published tree too large to enumerate in state."""

    tree_digest: str
    manifest_digest: str
    inventory_digest: str
    file_count: int
    directory_count: int
    expanded_bytes: int
    canonical_path: str
    storage_kind: RuntimeArtifactTreeKind
    manifest_algorithm: str
    marker_relative_path: str

    def __post_init__(self) -> None:
        _check_digest(self.tree_digest, "runtime artifact tree digest")
        _check_digest(self.manifest_digest, "runtime artifact tree manifest digest")
        _check_digest(self.inventory_digest, "runtime artifact tree inventory digest")
        if not 1 <= self.file_count <= 100_000_000:
            raise ValueError("runtime artifact tree file count is outside the bound")
        if not 0 <= self.directory_count <= 100_000_000:
            raise ValueError("runtime artifact tree directory count is outside the bound")
        if not 1 <= self.expanded_bytes <= 128 * 1024**4:
            raise ValueError("runtime artifact expanded bytes are outside the bound")
        path = PurePosixPath(self.canonical_path)
        parts = path.parts
        if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("runtime artifact tree path must be a safe published relative path")
        marker = PurePosixPath(self.marker_relative_path)
        if marker.is_absolute() or any(part in {"", ".", ".."} for part in marker.parts):
            raise ValueError("runtime artifact tree marker path is unsafe")
        if self.storage_kind is RuntimeArtifactTreeKind.LOCALIZATION_GENERATION:
            if self.manifest_algorithm not in {
                "fs2-flat-tree-inventory/v1",
                "fs2-tree-inventory/v2",
                "fs2-tree-manifest/v1",
            }:
                raise ValueError("runtime localization tree algorithm is unsupported")
            if self.inventory_digest != self.tree_digest:
                raise ValueError("runtime localization generation must be named by its inventory digest")
            if parts[-2:] != ("sha256", self.tree_digest.removeprefix("sha256:")):
                raise ValueError("runtime localization generation path differs from its tree digest")
            if self.marker_relative_path != ".fs2-runtime-tree.json":
                raise ValueError("runtime localization generation marker path is unsupported")
        elif self.storage_kind is RuntimeArtifactTreeKind.REFERENCE_DATA_PLANE:
            if self.manifest_algorithm != "fs2-serve.nebius.ai/reference-data-manifest/v1":
                raise ValueError("reference-data manifest schema is unsupported")
            if (
                len(parts) < 5
                or parts[0] != "datasets"
                or parts[-2:]
                != (
                    "sha256",
                    self.tree_digest.removeprefix("sha256:"),
                )
            ):
                raise ValueError("reference-data dataset path differs from its tree digest")
            if self.marker_relative_path != ".fs2-manifest-sha256":
                raise ValueError("reference-data marker path is unsupported")


@dataclass(frozen=True, slots=True)
class RuntimeArtifactLocalization:
    """Trusted, exact localization proof frozen before workload admission."""

    logical_artifact_id: str
    mount_path: str
    content_digest: str
    files: tuple[RuntimeArtifactFile, ...]
    localization_receipt_digest: str
    aggregate_tree: RuntimeArtifactAggregateTree | None = None
    verification_receipt_json: str | None = None

    def __post_init__(self) -> None:
        if _ARTIFACT_ID_RE.fullmatch(self.logical_artifact_id) is None:
            raise ValueError("runtime artifact logical ID is invalid")
        path = PurePosixPath(self.mount_path)
        if not path.is_absolute() or path == PurePosixPath("/") or path.as_posix() != self.mount_path:
            raise ValueError("runtime artifact mount path must be normalized and absolute")
        _check_digest(self.content_digest, "runtime artifact content digest")
        _check_digest(self.localization_receipt_digest, "runtime artifact localization receipt")
        paths = tuple(item.path for item in self.files)
        if len(paths) != len(set(paths)):
            raise ValueError("runtime artifact file manifest must be unique")
        if bool(paths) == (self.aggregate_tree is not None):
            raise ValueError("runtime artifact localization requires either bounded files or one aggregate tree")
        if self.aggregate_tree is not None and self.content_digest != self.aggregate_tree.tree_digest:
            raise ValueError("runtime artifact content digest differs from its aggregate tree")
        receipt: object | None = None
        receipt_json = self.verification_receipt_json
        if receipt_json is not None:
            encoded = receipt_json.encode()
            if len(encoded) > 64 * 1024:
                raise ValueError("runtime artifact verification receipt exceeds the bound")
            try:
                receipt = json.loads(encoded)
            except (UnicodeError, ValueError) as error:
                raise ValueError("runtime artifact verification receipt is invalid") from error
            canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if canonical != receipt_json:
                raise ValueError("runtime artifact verification receipt is not canonical JSON")
        reference_plane = (
            self.aggregate_tree is not None
            and self.aggregate_tree.storage_kind is RuntimeArtifactTreeKind.REFERENCE_DATA_PLANE
        )
        if reference_plane != (receipt is not None):
            raise ValueError("reference-data localization requires exactly one terminal verification receipt")
        if reference_plane:
            assert isinstance(receipt, dict)
            assert self.aggregate_tree is not None
            storage = receipt.get("storage")
            content = receipt.get("content")
            if (
                receipt.get("schema") != "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1"
                or receipt.get("bundle_id") != self.logical_artifact_id
                or not isinstance(storage, dict)
                or storage.get("host_root") != "/mnt/fs2-reference-data/data"
                or storage.get("mount_path") != "/reference-data"
                or storage.get("dataset_sub_path") != self.aggregate_tree.canonical_path
                or storage.get("read_only") is not True
                or not isinstance(content, dict)
                or content.get("tree_sha256") != self.aggregate_tree.tree_digest.removeprefix("sha256:")
                or content.get("manifest_sha256") != self.aggregate_tree.manifest_digest.removeprefix("sha256:")
                or content.get("inventory_sha256") != self.aggregate_tree.inventory_digest.removeprefix("sha256:")
                or content.get("inventory_marker") != self.aggregate_tree.marker_relative_path
                or content.get("file_count") != self.aggregate_tree.file_count
                or content.get("expanded_bytes") != self.aggregate_tree.expanded_bytes
            ):
                raise ValueError("reference-data terminal receipt differs from its frozen aggregate identity")
            assert receipt_json is not None
            digest = "sha256:" + hashlib.sha256(receipt_json.encode()).hexdigest()
            if digest != self.localization_receipt_digest:
                raise ValueError("reference-data terminal receipt digest differs from its localization receipt")


@dataclass(frozen=True, slots=True)
class RuntimeArtifactMount:
    """Exact adapter binding from a logical runtime artifact to an approved image path."""

    artifact_id: str
    mount_path: str
    sub_path: str | None = None
    read_only: bool = True
    expected_content_sha256: str | None = None
    expected_manifest_sha256: str | None = None
    authorization_receipt_sha256: str | None = None
    readiness_receipt_sha256: str | None = None
    supplemental_groups: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if _ARTIFACT_ID_RE.fullmatch(self.artifact_id) is None:
            raise ValueError("runtime mount artifact_id must be a logical artifact ID")
        mount = PurePosixPath(self.mount_path)
        if not mount.is_absolute() or not (
            any(root == mount or root in mount.parents for root in _RUNTIME_MOUNT_ROOTS)
            or mount in _RUNTIME_EXACT_MOUNT_PATHS
        ):
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
            (self.expected_manifest_sha256, "expected manifest digest"),
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
class StageWorkspaceDocument:
    """One adapter-owned canonical JSON document materialized by the controller."""

    relative_path: str
    canonical_json: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.relative_path)
        if (
            path.is_absolute()
            or len(path.parts) < 2
            or path.parts[0] != ".fs2"
            or path.suffix != ".json"
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("stage workspace document must be a safe .fs2 JSON path")
        encoded = self.canonical_json.encode()
        if not encoded or len(encoded) > 1024 * 1024:
            raise ValueError("stage workspace document exceeds the bound")
        try:
            value = json.loads(encoded)
        except (UnicodeError, ValueError) as error:
            raise ValueError("stage workspace document is invalid JSON") from error
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if canonical != self.canonical_json:
            raise ValueError("stage workspace document must use canonical JSON")


@dataclass(frozen=True, slots=True)
class RuntimeArtifactAdmissionRole:
    """Adapter vocabulary for one controller-verified runtime tree role."""

    role: str
    artifact_id: str
    mount_path: str
    identity_field: str = "content-digest"

    def __post_init__(self) -> None:
        if re.fullmatch(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$", self.role) is None:
            raise ValueError("runtime artifact admission role is invalid")
        if _ARTIFACT_ID_RE.fullmatch(self.artifact_id) is None:
            raise ValueError("runtime artifact admission role has an invalid artifact ID")
        mount = PurePosixPath(self.mount_path)
        if not mount.is_absolute() or mount == PurePosixPath("/") or mount.as_posix() != self.mount_path:
            raise ValueError("runtime artifact admission role has an invalid mount path")
        if self.identity_field not in {
            "content-digest",
            "tree-digest",
            "manifest-digest",
            "inventory-digest",
        }:
            raise ValueError("runtime artifact admission identity field is unsupported")


@dataclass(frozen=True, slots=True)
class RuntimeArtifactAdmissionSpec:
    """Controller-filled admission document consumed by a model runtime gate."""

    schema: str
    relative_path: str
    roles: tuple[RuntimeArtifactAdmissionRole, ...]

    def __post_init__(self) -> None:
        if re.fullmatch(r"^[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9._/-]*/v[1-9][0-9]*$", self.schema) is None:
            raise ValueError("runtime artifact admission schema is invalid")
        path = PurePosixPath(self.relative_path)
        if (
            path.is_absolute()
            or len(path.parts) != 2
            or path.parts[0] != ".fs2"
            or path.suffix != ".json"
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("runtime artifact admission must use one safe .fs2 JSON path")
        role_names = tuple(item.role for item in self.roles)
        artifact_ids = tuple(item.artifact_id for item in self.roles)
        if not self.roles or len(role_names) != len(set(role_names)) or len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("runtime artifact admission roles must be non-empty and unique")


@dataclass(frozen=True, slots=True)
class RuntimeTreeBinding:
    """Archive provenance and extracted-tree identity carried by model adapters.

    The controller converts this model-owned logical binding into the stronger
    operator-observed ``RuntimeArtifactLocalization`` before admission. Keeping
    both digests distinct prevents an archive checksum from being mistaken for
    a qualified runtime tree.
    """

    artifact_id: str
    mount_path: str
    archive_sha256: str
    tree_inventory_sha256: str
    entry_count: int

    def __post_init__(self) -> None:
        if _ARTIFACT_ID_RE.fullmatch(self.artifact_id) is None:
            raise ValueError("runtime tree binding artifact_id must be a logical artifact ID")
        path = PurePosixPath(self.mount_path)
        if not path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts[1:]):
            raise ValueError("runtime tree binding mount_path must be a safe absolute path")
        for digest, label in (
            (self.archive_sha256, "archive_sha256"),
            (self.tree_inventory_sha256, "tree_inventory_sha256"),
        ):
            if _SHA256_RE.fullmatch(digest) is None:
                raise ValueError(f"runtime tree binding {label} must be a lowercase SHA-256")
        if self.archive_sha256 == self.tree_inventory_sha256:
            raise ValueError("archive provenance and extracted-tree identity must be distinct digests")
        if not 1 <= self.entry_count <= 1_048_576:
            raise ValueError("runtime tree binding entry_count is outside the bound")


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
    collector_id: str = "controller-unbound"
    validator_id: str = "controller-unbound"
    handoff_name: str | None = None
    max_output_artifacts: int = 1024
    max_output_bytes: int = 128 * 1024 * 1024 * 1024
    materializations: tuple[ArtifactMaterialization, ...] = ()
    runtime_artifacts: tuple[str, ...] = ()
    runtime_trees: tuple[RuntimeTreeBinding, ...] = ()
    runtime_mounts: tuple[RuntimeArtifactMount, ...] = ()
    workspace_documents: tuple[StageWorkspaceDocument, ...] = ()
    runtime_admission: RuntimeArtifactAdmissionSpec | None = None

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
        tree_ids = tuple(item.artifact_id for item in self.runtime_trees)
        if len(set(tree_ids)) != len(tree_ids) or not set(tree_ids).issubset(self.runtime_artifacts):
            raise ValueError("runtime tree bindings must uniquely reference mounted runtime artifacts")
        mounted = tuple(item.artifact_id for item in self.runtime_mounts)
        if self.runtime_mounts and set(mounted) != set(self.runtime_artifacts):
            raise ValueError("explicit runtime mounts must exactly cover every stage runtime artifact")
        mount_targets = tuple(item.mount_path for item in self.runtime_mounts)
        if len(set(mount_targets)) != len(mount_targets):
            raise ValueError("runtime artifact mount targets must be unique")
        document_paths = tuple(item.relative_path for item in self.workspace_documents)
        if len(document_paths) != len(set(document_paths)):
            raise ValueError("stage workspace document paths must be unique")
        if self.runtime_admission is not None:
            if self.runtime_admission.relative_path in document_paths:
                raise ValueError("runtime artifact admission path collides with an adapter document")
            admission_artifacts = {item.artifact_id for item in self.runtime_admission.roles}
            if admission_artifacts != set(self.runtime_artifacts):
                raise ValueError("runtime artifact admission roles must cover every stage runtime artifact")
            bound_paths = {item.artifact_id: item.mount_path for item in self.runtime_mounts}
            if any(bound_paths.get(item.artifact_id) != item.mount_path for item in self.runtime_admission.roles):
                raise ValueError("runtime artifact admission roots differ from the exact stage mounts")
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

    def names_tree_path(self, binding: RuntimeTreeBinding) -> bool:
        """Whether this invocation passes the localized tree to the model."""

        return binding.mount_path in self.argv or any(value == binding.mount_path for _key, value in self.environment)


@dataclass(frozen=True, slots=True)
class StageVolumeBinding:
    """Frozen physical source for one execution-map stage mount."""

    name: str
    kind: str
    claim_name: str | None
    host_path: str | None
    mount_path: str
    sub_path: str | None
    read_only: bool

    def __post_init__(self) -> None:
        _check_name(self.name, "stage volume name")
        if self.kind not in {"artifact-workspace", "reference", "private"}:
            raise ValueError("stage volume kind is unsupported")
        if self.kind == "artifact-workspace":
            if self.claim_name is not None or self.host_path is not None or self.read_only:
                raise ValueError("attempt workspace must be a writable emptyDir")
        elif (self.claim_name is None) == (self.host_path is None) or not self.read_only:
            raise ValueError("runtime stage volumes require one read-only physical source")
        path = PurePosixPath(self.mount_path)
        if not path.is_absolute() or path == PurePosixPath("/"):
            raise ValueError("stage volume mount path must be normalized and absolute")
        if self.sub_path is not None:
            relative = PurePosixPath(self.sub_path)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("stage volume sub_path is unsafe")


@dataclass(frozen=True, slots=True)
class StageExecutionBinding:
    """Exact stage image/resources/source bindings frozen at public admission."""

    stage_id: str
    image: str
    collector_id: str
    validator_id: str
    mounts: tuple[StageVolumeBinding, ...]
    service_account_name: str
    request_cpu: str
    request_memory: str
    request_ephemeral_storage: str
    limit_cpu: str
    limit_memory: str
    limit_ephemeral_storage: str
    active_deadline_seconds: int
    termination_grace_seconds: int
    environment: tuple[tuple[str, str], ...]
    required_node_labels: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _check_name(self.stage_id, "stage execution binding ID")
        if not self.image or len(self.image) > 1024 or "@sha256:" not in self.image:
            raise ValueError("stage execution image must use an immutable digest")
        _check_name(self.service_account_name, "stage execution service account")
        if not self.mounts or len({item.name for item in self.mounts}) != len(self.mounts):
            raise ValueError("stage execution mounts must be non-empty and uniquely named")
        if len(dict(self.environment)) != len(self.environment) or len(dict(self.required_node_labels)) != len(
            self.required_node_labels
        ):
            raise ValueError("stage execution maps must have unique keys")
        if not 1 <= self.active_deadline_seconds <= 7 * 24 * 3600:
            raise ValueError("stage active deadline is outside the bound")
        if not 1 <= self.termination_grace_seconds <= 24 * 3600:
            raise ValueError("stage termination grace is outside the bound")


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
    execution_map_sha256: str | None = None
    stage_bindings: tuple[StageExecutionBinding, ...] = ()

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
        tree_bindings: dict[str, RuntimeTreeBinding] = {}
        for invocation in self.invocations:
            for tree in invocation.runtime_trees:
                existing = tree_bindings.setdefault(tree.artifact_id, tree)
                if existing != tree:
                    raise ValueError("one runtime artifact cannot carry two tree identities")
        for artifact_id, binding in tree_bindings.items():
            if any(
                artifact_id in invocation.runtime_artifacts
                and not any(tree.artifact_id == artifact_id for tree in invocation.runtime_trees)
                for invocation in self.invocations
            ):
                raise ValueError("every stage mounting a localized artifact must carry its tree binding")
            if not any(invocation.names_tree_path(binding) for invocation in self.invocations):
                raise ValueError(
                    "a bound runtime tree must be reachable through some stage's model argv or environment"
                )
        produced = [item.produces for item in self.invocations]
        if len(produced) != len(set(produced)):
            raise ValueError("each stage invocation must produce a unique logical artifact")
        producer_stage = {item.produces: item.stage_id for item in self.invocations}
        for item in self.invocations:
            for logical_id in item.consumes:
                stage_id = producer_stage.get(logical_id)
                if stage_id is not None and stage_id not in self.controller_plan.stage(item.stage_id).depends_on:
                    raise ValueError("a stage may consume only original input or direct predecessor artifacts")
        depended_on = {dependency for stage in self.controller_plan.stages for dependency in stage.depends_on}
        sink_stages = {stage.stage_id for stage in self.controller_plan.stages if stage.stage_id not in depended_on}
        if len(sink_stages) != 1:
            raise ValueError("an executable plan requires one canonical terminal stage")
        if self.execution_map_sha256 is not None:
            _check_digest(self.execution_map_sha256, "execution map digest")
        binding_ids = tuple(item.stage_id for item in self.stage_bindings)
        expected_binding_ids = {stage.stage_id for stage in self.controller_plan.stages}
        if self.stage_bindings and (
            len(binding_ids) != len(set(binding_ids)) or set(binding_ids) != expected_binding_ids
        ):
            raise ValueError("execution-map stage bindings must cover the controller plan exactly")
        if bool(self.stage_bindings) != (self.execution_map_sha256 is not None):
            raise ValueError("execution-map digest and stage bindings must be frozen together")

    def assert_controller_bound(self) -> None:
        """Reject an adapter plan that still lacks trusted execution evidence."""

        produced = {item.produces for item in self.invocations}
        consumed_outputs = {
            logical_id for item in self.invocations for logical_id in item.consumes if logical_id in produced
        }
        for invocation in self.invocations:
            if invocation.collector_id == "controller-unbound" or invocation.validator_id == "controller-unbound":
                raise ValueError("controller execution identities are not bound")
            if invocation.runtime_artifacts and {item.artifact_id for item in invocation.runtime_mounts} != set(
                invocation.runtime_artifacts
            ):
                raise ValueError("controller runtime artifact mounts are not bound")
            if any(item.readiness_receipt_sha256 is None for item in invocation.runtime_mounts):
                raise ValueError("controller runtime localization receipts are not bound")
            if invocation.produces in consumed_outputs and invocation.handoff_name is None:
                raise ValueError("a predecessor consumed downstream must declare an exact handoff entry")
        if self.execution_map_sha256 is None or not self.stage_bindings:
            raise ValueError("controller execution-map image and resource bindings are not frozen")

    def invocation(self, stage_id: str, shard_id: str | None) -> StageInvocation:
        key = (stage_id, shard_id or "gang")
        return _only(
            (item for item in self.invocations if (item.stage_id, item.shard_id) == key),
            f"frozen adapter execution has no invocation for stage {key[0]!r} shard {key[1]!r}",
        )

    def execution_binding(self, stage_id: str) -> StageExecutionBinding:
        return _only(
            (item for item in self.stage_bindings if item.stage_id == stage_id),
            f"frozen adapter execution has no stage binding for {stage_id!r}",
        )

    def producer(self, logical_artifact_id: str) -> StageInvocation | None:
        return next((item for item in self.invocations if item.produces == logical_artifact_id), None)

    @property
    def localized_tree_artifacts(self) -> tuple[str, ...]:
        return tuple(
            sorted({tree.artifact_id for invocation in self.invocations for tree in invocation.runtime_trees})
        )


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
        if self.receipt_digest is not None:
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
class StageToleration:
    key: str
    operator: str
    value: str | None
    effect: str

    def __post_init__(self) -> None:
        if not self.key or len(self.key) > 253:
            raise ValueError("stage toleration key is invalid")
        if self.operator not in {"Equal", "Exists"} or self.effect not in {
            "NoSchedule",
            "PreferNoSchedule",
            "NoExecute",
        }:
            raise ValueError("stage toleration policy is invalid")
        if (self.operator == "Exists") != (self.value is None):
            raise ValueError("Exists tolerations omit value; Equal tolerations require one")


@dataclass(frozen=True, slots=True)
class StageSchedulingDecision:
    stage_id: str
    resource_class: ResourceClass
    resolved_cluster_queue: str
    resolved_local_queue: str
    workload_priority_class: str
    workload_priority_value: int
    resolved_pool_preference: tuple[str, ...]
    accelerator_resource_name: str | None
    accelerator_count: int
    max_queue_seconds: int | None
    max_execution_seconds: int | None
    checkpoint_mode: CheckpointMode
    preemption_mode: PreemptionMode
    admitted_resource_flavor: str | None = None
    placement_class: StagePlacementClass | None = None
    workload_namespace: str | None = None
    route_namespace: str | None = None
    requested_resource_flavor: str | None = None
    node_selector: tuple[tuple[str, str], ...] = ()
    tolerations: tuple[StageToleration, ...] = ()

    def __post_init__(self) -> None:
        _check_name(self.stage_id, "stage_id")
        for value, label in (
            (self.resolved_local_queue, "resolved_local_queue"),
            (self.resolved_cluster_queue, "resolved_cluster_queue"),
            (self.workload_priority_class, "workload_priority_class"),
        ):
            _check_name(value, label)
        if not isinstance(self.resource_class, ResourceClass):
            raise ValueError("resource_class must be a supported scheduling resource class")
        if self.workload_namespace is not None:
            _check_name(self.workload_namespace, "stage workload namespace")
        if self.route_namespace is not None:
            _check_name(self.route_namespace, "stage route namespace")
        if (self.workload_namespace is None) != (self.route_namespace is None):
            raise ValueError("stage workload and route namespaces must be frozen together")
        if self.workload_namespace is not None and self.workload_namespace != self.route_namespace:
            raise ValueError("stage workload namespace differs from its LocalQueue namespace")
        if self.requested_resource_flavor is not None:
            _check_name(self.requested_resource_flavor, "requested resource flavor")
        if self.admitted_resource_flavor is not None:
            _check_name(self.admitted_resource_flavor, "admitted resource flavor")
        if len(dict(self.node_selector)) != len(self.node_selector):
            raise ValueError("stage node selector keys must be unique")
        if len(self.resolved_pool_preference) != len(set(self.resolved_pool_preference)):
            raise ValueError("resolved_pool_preference must be unique")
        for pool in self.resolved_pool_preference:
            if not pool or len(pool) > 128 or _POOL_RE.fullmatch(pool) is None:
                raise ValueError("resolved pool names must be DNS-compatible and at most 128 characters")
        if not 0 <= self.accelerator_count <= 1024:
            raise ValueError("accelerator count must follow the Kueue scheduling contract")
        if self.resource_class is ResourceClass.GPU:
            if (
                not self.resolved_pool_preference
                or self.accelerator_count < 1
                or self.accelerator_resource_name is None
                or len(self.accelerator_resource_name) > 317
                or _RESOURCE_NAME_RE.fullmatch(self.accelerator_resource_name) is None
            ):
                raise ValueError("GPU scheduling requires an exact accelerator request and pool preference")
        elif self.accelerator_resource_name is not None or self.accelerator_count != 0:
            raise ValueError("CPU scheduling cannot retain an accelerator resource")
        elif self.admitted_resource_flavor is not None:
            raise ValueError("CPU scheduling cannot retain an admitted ResourceFlavor")
        elif bool(self.resolved_pool_preference) != (self.requested_resource_flavor is not None):
            raise ValueError("CPU scheduling must retain its placement pools and ResourceFlavor together")
        if self.placement_class is not None:
            if self.resource_class is ResourceClass.GPU and self.placement_class is not StagePlacementClass.ACCELERATOR:
                raise ValueError("GPU scheduling requires accelerator placement")
            if self.resource_class is ResourceClass.CPU and self.placement_class is StagePlacementClass.ACCELERATOR:
                raise ValueError("CPU scheduling requires a CPU placement")
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
    workload_namespace: str
    route_namespace: str
    stages: tuple[StageSchedulingDecision, ...]
    raw_contract_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.policy_revision or len(self.policy_revision) > 200:
            raise ValueError("policy_revision must be a stable non-empty identifier")
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if not isinstance(self.service_class, ServiceClass):
            raise ValueError("service_class must be a supported scheduling class")
        for value, label in (
            (self.tenant_queue, "tenant_queue"),
            (self.model_lane, "model_lane"),
            (self.workload_namespace, "workload_namespace"),
            (self.route_namespace, "route_namespace"),
        ):
            if not value or len(value) > 128 or _POOL_RE.fullmatch(value) is None:
                raise ValueError(f"{label} must be a bounded provider-neutral queue identity")
        ids = [stage.stage_id for stage in self.stages]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("scheduling snapshot stage identities must be non-empty and unique")
        if self.raw_contract_sha256 is not None:
            _check_digest(self.raw_contract_sha256, "raw scheduling contract digest")
        for stage in self.stages:
            if stage.workload_namespace is not None and stage.route_namespace != stage.workload_namespace:
                raise ValueError("stage LocalQueue route namespace is inconsistent")

    def stage(self, stage_id: str) -> StageSchedulingDecision:
        return _only(
            (stage for stage in self.stages if stage.stage_id == stage_id),
            f"frozen scheduling snapshot has no stage {stage_id!r}",
        )

    @property
    def digest(self) -> str:
        value = {
            "policy_revision": self.policy_revision,
            "captured_at": self.captured_at.isoformat(),
            "service_class": self.service_class,
            "tenant_queue": self.tenant_queue,
            "model_lane": self.model_lane,
            "workload_namespace": self.workload_namespace,
            "route_namespace": self.route_namespace,
            "raw_contract_sha256": self.raw_contract_sha256,
            "stages": [
                {
                    "stage_id": item.stage_id,
                    "resource_class": item.resource_class,
                    "resolved_cluster_queue": item.resolved_cluster_queue,
                    "resolved_local_queue": item.resolved_local_queue,
                    "workload_priority_class": item.workload_priority_class,
                    "workload_priority_value": item.workload_priority_value,
                    "resolved_pool_preference": item.resolved_pool_preference,
                    "accelerator_resource_name": item.accelerator_resource_name,
                    "accelerator_count": item.accelerator_count,
                    "max_queue_seconds": item.max_queue_seconds,
                    "max_execution_seconds": item.max_execution_seconds,
                    "checkpoint_mode": item.checkpoint_mode,
                    "preemption_mode": item.preemption_mode,
                    "placement_class": item.placement_class,
                    "workload_namespace": item.workload_namespace,
                    "route_namespace": item.route_namespace,
                    "requested_resource_flavor": item.requested_resource_flavor,
                    "node_selector": item.node_selector,
                    "tolerations": [
                        {
                            "key": toleration.key,
                            "operator": toleration.operator,
                            "value": toleration.value,
                            "effect": toleration.effect,
                        }
                        for toleration in item.tolerations
                    ],
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
    route_namespace: str | None = None

    def __post_init__(self) -> None:
        _check_name(self.namespace, "namespace")
        _check_name(self.name, "workload name")
        if self.route_namespace is None:
            object.__setattr__(self, "route_namespace", self.namespace)
        else:
            _check_name(self.route_namespace, "route namespace")


@dataclass(frozen=True, slots=True)
class SchedulingAdmission:
    """Exact Kueue admission observed after the immutable request snapshot."""

    resolved_pool_id: str | None
    admitted_resource_flavor: str | None
    accelerator_resource_name: str | None
    accelerator_count: int
    admitted_at: datetime | None
    quota_reserved_at: datetime | None = None
    cpu_millis: int = 0
    memory_bytes: int = 0

    def __post_init__(self) -> None:
        for timestamp, label in (
            (self.quota_reserved_at, "Kueue quota reservation time"),
            (self.admitted_at, "Kueue admission time"),
        ):
            if timestamp is not None and timestamp.tzinfo is None:
                raise ValueError(f"{label} must be timezone-aware")
        if self.quota_reserved_at is not None and self.admitted_at is not None:
            if self.admitted_at < self.quota_reserved_at:
                raise ValueError("Kueue admission cannot precede quota reservation")
        if not 0 <= self.cpu_millis <= 10**9 or not 0 <= self.memory_bytes <= 1024**6:
            raise ValueError("Kueue admitted core resource quantities are invalid")
        if not 0 <= self.accelerator_count <= 1024:
            raise ValueError("Kueue admitted accelerator count is invalid")
        if self.accelerator_count:
            if self.resolved_pool_id is None or self.admitted_resource_flavor is None:
                raise ValueError("GPU admission requires an exact pool and ResourceFlavor")
            if (
                self.accelerator_resource_name is None
                or len(self.accelerator_resource_name) > 317
                or _RESOURCE_NAME_RE.fullmatch(self.accelerator_resource_name) is None
            ):
                raise ValueError("GPU admission requires an exact accelerator resource")
        elif self.accelerator_resource_name is not None:
            raise ValueError("CPU admission cannot claim an accelerator resource")
        elif (self.resolved_pool_id is None) != (self.admitted_resource_flavor is None):
            raise ValueError("CPU admission must retain both its pool and ResourceFlavor or neither")
        elif self.resolved_pool_id is not None and (self.cpu_millis < 1 or self.memory_bytes < 1):
            raise ValueError("CPU admission requires positive core resource quantities")
        for identity, label in (
            (self.resolved_pool_id, "resolved_pool_id"),
            (self.admitted_resource_flavor, "admitted_resource_flavor"),
        ):
            if identity is not None and (len(identity) > 128 or _POOL_RE.fullmatch(identity) is None):
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
    deletion_requested: bool = False
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
        if self.deletion_requested and (self.outcome is AttemptOutcome.ACTIVE or self.workload.uid is None):
            raise ValueError("only a terminal applied attempt can have deletion pending")
        if self.resource_released and self.workload.uid is not None and not self.deletion_requested:
            raise ValueError("an applied workload is released only after a persisted delete request")
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
    stored_schema: str = STATE_SCHEMA

    def __post_init__(self) -> None:
        _check_variant(self.variant_id)
        if self.stored_schema not in READABLE_STATE_SCHEMAS:
            raise ValueError("stored scientific-batch state schema is unsupported")
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
            if plan.placement_class is not None and scheduling.placement_class is not plan.placement_class:
                raise ValueError("the scheduling snapshot changed the frozen stage placement class")

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
        return _only(
            (stage for stage in self.stages if stage.stage_id == stage_id),
            f"scientific batch has no stage record {stage_id!r}",
        )

    @property
    def legacy_admission(self) -> bool:
        """Whether this row was frozen before the current state schema.

        A pre-v8 admission has no placement class, no raw scheduling contract
        digest, and no execution-map or stage bindings, because those values
        did not exist when it was written.  The codec reopens them as the
        explicit null/empty representation, so the row stays readable but is
        not executable: the controller can neither render its workloads nor
        prove which image and resources it was admitted against.
        """

        return self.stored_schema != STATE_SCHEMA


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
    route_namespace: str | None = None
    gang_size: int | None = None
    invocation: StageInvocation | None = None
    materializations: tuple[ResolvedArtifactMaterialization, ...] = ()
    access_context: ArtifactAccessContext = PUBLIC_ARTIFACT_ACCESS_CONTEXT
    runtime_artifacts: tuple[RuntimeArtifactLocalization, ...] = ()
    execution_map_sha256: str | None = None
    execution_binding: StageExecutionBinding | None = None

    def __post_init__(self) -> None:
        _check_name(self.namespace, "namespace")
        if self.route_namespace is None:
            object.__setattr__(self, "route_namespace", self.namespace)
        else:
            _check_name(self.route_namespace, "route namespace")
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
            if self.execution_map_sha256 is None or self.execution_binding is None:
                raise ValueError("workload has no frozen execution-map binding")
            _check_digest(self.execution_map_sha256, "workload execution map digest")
            if self.execution_binding.stage_id != self.stage_id:
                raise ValueError("workload execution binding differs from its stage")

    @property
    def ref(self) -> WorkloadRef:
        return WorkloadRef(
            namespace=self.namespace,
            name=self.name,
            kind=self.kind,
            route_namespace=self.route_namespace,
        )


@dataclass(frozen=True, slots=True)
class PodPhaseInterval:
    """One Kubernetes-observed phase window for an immutable Pod UID."""

    phase: LifecyclePhase
    started_at: datetime
    ended_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.phase not in {
            LifecyclePhase.IMAGE_LOADING,
            LifecyclePhase.ARTIFACT_LOADING,
            LifecyclePhase.RESTORING,
            LifecyclePhase.SEMANTIC_WARMUP,
            LifecyclePhase.ACTIVE_COMPUTE,
            LifecyclePhase.ALLOCATED_IDLE,
            LifecyclePhase.GRACE_DRAIN,
            LifecyclePhase.TEARDOWN,
        }:
            raise ValueError("Pod phase interval is not an attributable runtime phase")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("Pod phase interval start must be timezone-aware")
        if self.ended_at is not None and (
            self.ended_at.tzinfo is None
            or self.ended_at.utcoffset() is None
            or self.ended_at < self.started_at
        ):
            raise ValueError("Pod phase interval end is invalid")


@dataclass(frozen=True, slots=True)
class PodLifecycleObservation:
    """Bounded Pod/node/device evidence retained outside controller state.

    The canonical lifecycle ledger is the durable owner of these facts.  The
    scientific controller carries this observation only across one reconcile
    call; it deliberately does not grow a second display or accounting ledger.
    """

    pod_uid: str
    pod_name: str | None
    node_name: str | None
    node_uid: str | None
    observed_at: datetime
    scheduled_at: datetime | None
    gpu_count: int
    gpu_uuids: tuple[str, ...] = ()
    phases: tuple[PodPhaseInterval, ...] = ()

    def __post_init__(self) -> None:
        for value, maximum, label in (
            (self.pod_uid, 128, "Pod UID"),
            (self.pod_name, 253, "Pod name"),
            (self.node_name, 253, "node name"),
            (self.node_uid, 128, "node UID"),
        ):
            if value is not None and (not value or len(value) > maximum):
                raise ValueError(f"observed {label} is invalid")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("Pod lifecycle observation time must be timezone-aware")
        if self.scheduled_at is not None and (
            self.scheduled_at.tzinfo is None or self.scheduled_at.utcoffset() is None
        ):
            raise ValueError("Pod scheduling time must be timezone-aware")
        if not 0 <= self.gpu_count <= 1024:
            raise ValueError("observed Pod GPU count is outside the bound")
        if len(self.gpu_uuids) != len(set(self.gpu_uuids)):
            raise ValueError("observed GPU UUIDs must be unique")
        if self.gpu_uuids and len(self.gpu_uuids) != self.gpu_count:
            raise ValueError("observed GPU UUID count differs from the Pod allocation")
        if any(
            re.fullmatch(r"^(?:GPU|MIG)-[A-Za-z0-9_.:/-]{1,123}$", value) is None
            for value in self.gpu_uuids
        ):
            raise ValueError("observed GPU UUID is invalid")
        identities = [(item.phase, item.started_at) for item in self.phases]
        if len(identities) != len(set(identities)):
            raise ValueError("observed Pod phase starts must be unique")


@dataclass(frozen=True, slots=True)
class WorkloadObservation:
    ref: WorkloadRef
    attempt_id: UUID
    state: WorkloadState
    phases: tuple[LifecyclePhase, ...]
    scheduling_admission: SchedulingAdmission | None = None
    kueue_workload_uid: str | None = None
    pod_uids: tuple[str, ...] = ()
    pod_lifecycle: tuple[PodLifecycleObservation, ...] = ()
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
        lifecycle_uids = tuple(value.pod_uid for value in self.pod_lifecycle)
        if len(lifecycle_uids) != len(set(lifecycle_uids)) or not set(lifecycle_uids).issubset(self.pod_uids):
            raise ValueError("Pod lifecycle evidence must uniquely bind an observed Pod UID")
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
