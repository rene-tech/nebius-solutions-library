"""Strict mirrors of the canonical public scientific result and manifest contracts.

The JSON Schema in ``catalog/runtime/schema`` is the sole owner of this shape.
This module is a typed projection of it so the control plane can build, persist
and re-read a terminal result without redefining the contract or inventing a
look-alike schema. Every constraint here exists in that schema; the tests assert
the two stay in step.

The document carries identity and provenance only. Biological payloads, argv,
environment, credentials and presigned URLs are absent by construction.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, StringConstraints, model_validator

from .models import StrictModel

SCIENTIFIC_RUN_RESULT_SCHEMA: Final = "fs2-serve.nebius.ai/scientific-run-result/v1"
MAX_ATTEMPT_ENTRIES = 655360
MAX_SNAPSHOT_STAGES = 64

OpaqueId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
ModelId = Annotated[str, StringConstraints(max_length=63, pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")]
K8sName = Annotated[str, StringConstraints(max_length=253, pattern=r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")]
RawSha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PrefixedSha256 = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
StageId = Annotated[str, StringConstraints(max_length=63, pattern=r"^[a-z][a-z0-9-]*$")]
PoolId = Annotated[str, StringConstraints(max_length=128, pattern=r"^[a-z0-9](?:[-_a-z0-9.]*[a-z0-9])?$")]
ExtendedResourceName = Annotated[
    str, StringConstraints(max_length=253, pattern=r"^[a-z0-9.-]+/[A-Za-z0-9][A-Za-z0-9._-]*$")
]
ModelRevision = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Uid = Annotated[str, StringConstraints(min_length=1, max_length=128)]
ErrorCode = Annotated[str, StringConstraints(max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")]


class ResultModel(StrictModel):
    """Closed contract model that keeps caller values out of error text."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


class TerminalStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PREEMPTED = "preempted"


class ValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not-run"


class AccessProfile(StrEnum):
    STANDARD = "standard"
    ACADEMIC = "academic"


class AccessState(StrEnum):
    NOT_REQUIRED = "not-required"
    VERIFIED = "verified"


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


class Compression(StrEnum):
    GZIP = "gzip"
    ZSTD = "zstd"
    NONE = "none"


class ArtifactRef(ResultModel):
    """The canonical public artifact pointer; no location ever appears here."""

    artifact_id: OpaqueId
    sha256: RawSha256
    size_bytes: int = Field(ge=0)
    media_type: Annotated[
        str,
        StringConstraints(max_length=128, pattern=r"^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$"),
    ]
    compression: Compression = Compression.NONE


class ExecutionIdentity(ResultModel):
    model_id: ModelId
    model_revision: ModelRevision
    runtime_image_digest: PrefixedSha256
    runtime_recipe_sha256: RawSha256
    workload_recipe_sha256: RawSha256
    model_artifact_manifest_digest: RawSha256
    execution_identity_sha256: RawSha256


class AccessAdmission(ResultModel):
    profile: AccessProfile
    state: AccessState
    receipt_digest: RawSha256 | None = None

    @model_validator(mode="after")
    def academic_access_is_proven(self) -> AccessAdmission:
        if self.profile is AccessProfile.ACADEMIC and (
            self.state is not AccessState.VERIFIED or self.receipt_digest is None
        ):
            raise ValueError("academic access requires a verified state and a receipt digest")
        return self


class SchedulingAdmission(ResultModel):
    """The accelerator identity Kueue actually admitted for one attempt."""

    resolved_pool_id: PoolId | None = None
    admitted_resource_flavor: K8sName | None = None
    accelerator_resource_name: ExtendedResourceName | None = None
    accelerator_count: int = Field(ge=0, le=1024)
    admitted_at: AwareDatetime

    @model_validator(mode="after")
    def accelerator_identity_matches_count(self) -> SchedulingAdmission:
        bound = (self.resolved_pool_id, self.admitted_resource_flavor, self.accelerator_resource_name)
        if self.accelerator_count >= 1 and any(item is None for item in bound):
            raise ValueError("an accelerator admission must name its pool, flavor and resource")
        if self.accelerator_count == 0 and any(item is not None for item in bound):
            raise ValueError("a non-accelerator admission cannot name a pool, flavor or resource")
        return self


class StageSchedulingDecision(ResultModel):
    stage_id: StageId
    resource_class: ResourceClass
    resolved_cluster_queue: K8sName
    resolved_local_queue: K8sName
    workload_priority_class: K8sName
    workload_priority_value: int
    resolved_pool_preference: tuple[PoolId, ...] = Field(max_length=32)
    accelerator_resource_name: ExtendedResourceName | None = None
    accelerator_count: int = Field(ge=0, le=1024)
    max_queue_seconds: int | None = Field(default=None, ge=1)
    max_execution_seconds: int | None = Field(default=None, ge=1)
    checkpoint_mode: CheckpointMode
    preemption_mode: PreemptionMode

    @model_validator(mode="after")
    def resource_class_matches_accelerators(self) -> StageSchedulingDecision:
        if len(set(self.resolved_pool_preference)) != len(self.resolved_pool_preference):
            raise ValueError("resolved pool preference must be unique")
        if self.resource_class is ResourceClass.GPU:
            if self.accelerator_count < 1 or self.accelerator_resource_name is None:
                raise ValueError("a GPU stage must name a positive accelerator resource")
        elif self.accelerator_count != 0 or self.accelerator_resource_name is not None:
            raise ValueError("a CPU stage cannot reserve accelerators")
        return self


class SchedulingSnapshot(ResultModel):
    """The frozen admission-time decision; it is never refreshed after admission."""

    policy_revision: OpaqueId
    captured_at: AwareDatetime
    service_class: ServiceClass
    tenant_queue: K8sName
    model_lane: K8sName
    workload_namespace: K8sName
    route_namespace: K8sName
    stages: tuple[StageSchedulingDecision, ...] = Field(min_length=1, max_length=MAX_SNAPSHOT_STAGES)

    @model_validator(mode="after")
    def stage_identities_are_unique(self) -> SchedulingSnapshot:
        ids = [stage.stage_id for stage in self.stages]
        if len(set(ids)) != len(ids):
            raise ValueError("scheduling snapshot stages must be unique")
        if self.workload_namespace != self.route_namespace:
            raise ValueError("workload and LocalQueue route namespaces must match")
        return self


class ResultAttempt(ResultModel):
    """One terminal stage/shard attempt with its exact GPU lifecycle identity."""

    attempt_id: OpaqueId
    stage_id: StageId
    shard_id: K8sName | None = None
    attempt_number: int = Field(ge=1, le=10)
    status: AttemptStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime
    scheduling_admission: SchedulingAdmission | None = None
    kueue_workload_uid: Uid | None = None
    k8s_job_uid: Uid | None = None
    pod_uids: tuple[Uid, ...] = ()
    node_uids: tuple[Uid, ...] = ()
    gpu_uuids: tuple[Uid, ...] = ()
    checkpoint_input: ArtifactRef | None = None
    checkpoint_output: ArtifactRef | None = None

    @model_validator(mode="after")
    def attempt_identity_is_consistent(self) -> ResultAttempt:
        if self.completed_at < self.started_at:
            raise ValueError("an attempt cannot complete before it starts")
        for values, label in (
            (self.pod_uids, "pod"),
            (self.node_uids, "node"),
            (self.gpu_uuids, "gpu"),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{label} identities must be unique")
        if self.status in {AttemptStatus.SUCCEEDED, AttemptStatus.PREEMPTED} and self.scheduling_admission is None:
            raise ValueError("an admitted attempt must retain its scheduling admission")
        return self


class SemanticValidation(ResultModel):
    validator_id: K8sName
    status: ValidationStatus
    receipt_digest: RawSha256 | None = None


class RunError(ResultModel):
    code: ErrorCode
    message: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    retryable: bool


class ScientificRunResult(ResultModel):
    """The immutable terminal document published for one scientific operation."""

    schema_: Literal["fs2-serve.nebius.ai/scientific-run-result/v1"] = Field(
        default=SCIENTIFIC_RUN_RESULT_SCHEMA, alias="schema"
    )
    operation_id: OpaqueId
    batch_id: OpaqueId
    workload_id: OpaqueId
    terminal_status: TerminalStatus
    submitted_at: AwareDatetime
    completed_at: AwareDatetime
    execution_identity: ExecutionIdentity
    access_admission: AccessAdmission
    scheduling_snapshot: SchedulingSnapshot
    input_manifest: ArtifactRef
    output_manifest: ArtifactRef | None = None
    attempts: tuple[ResultAttempt, ...] = Field(default=(), max_length=MAX_ATTEMPT_ENTRIES)
    semantic_validation: SemanticValidation
    error: RunError | None = None

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True, populate_by_name=True)

    @model_validator(mode="after")
    def terminal_status_is_proven(self) -> ScientificRunResult:
        if self.completed_at < self.submitted_at:
            raise ValueError("a run cannot complete before it is submitted")
        known_stages = {stage.stage_id for stage in self.scheduling_snapshot.stages}
        seen: set[tuple[str, str | None, int]] = set()
        for attempt in self.attempts:
            if attempt.stage_id not in known_stages:
                raise ValueError("every attempt must belong to a stage in the frozen snapshot")
            key = (attempt.stage_id, attempt.shard_id, attempt.attempt_number)
            if key in seen:
                raise ValueError("attempts must be unique per stage, shard and attempt number")
            seen.add(key)
            if attempt.completed_at > self.completed_at:
                raise ValueError("an attempt cannot outlive the terminal result")
        if self.terminal_status is TerminalStatus.SUCCEEDED:
            if self.output_manifest is None:
                raise ValueError("a succeeded run requires an output manifest")
            if self.error is not None:
                raise ValueError("a succeeded run cannot carry an error")
            if self.semantic_validation.status is not ValidationStatus.PASSED:
                raise ValueError("a succeeded run requires passed semantic validation")
            if self.semantic_validation.receipt_digest is None:
                raise ValueError("passed semantic validation requires a receipt digest")
            if not self.attempts:
                raise ValueError("a succeeded run requires at least one recorded attempt")
        if self.terminal_status is TerminalStatus.FAILED and self.error is None:
            raise ValueError("a failed run requires a structured error")
        return self

    def to_document(self) -> dict[str, Any]:
        """Return the canonical JSON document exactly as the schema defines it."""

        return self.model_dump(mode="json", by_alias=True)

    @property
    def digest(self) -> str:
        return canonical_result_digest(self.to_document())


def canonical_result_digest(document: dict[str, Any]) -> str:
    """Digest the canonical document with stable key order and no whitespace."""

    body = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def artifact_ref(
    *,
    artifact_id: UUID | str,
    digest: str,
    size_bytes: int,
    media_type: str,
    compression: str | None,
) -> ArtifactRef:
    """Project an internal content address onto the public pointer shape."""

    return ArtifactRef(
        artifact_id=str(artifact_id),
        sha256=digest.removeprefix("sha256:"),
        size_bytes=size_bytes,
        media_type=media_type,
        compression=Compression(compression) if compression else Compression.NONE,
    )


def utc_isoformat(value: datetime) -> str:
    """Render a timezone-aware instant the way the canonical schema expects."""

    return value.isoformat().replace("+00:00", "Z")


SCIENTIFIC_ARTIFACT_MANIFEST_SCHEMA: Final = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"
MAX_MANIFEST_ENTRIES = 10000

EntryName = Annotated[str, StringConstraints(max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")]
SemanticType = Annotated[str, StringConstraints(max_length=128, pattern=r"^[a-z][a-z0-9_.-]*/v[1-9][0-9]*$")]


class ManifestEntry(ResultModel):
    """One named, semantically typed artifact inside a manifest."""

    name: EntryName
    semantic_type: SemanticType
    artifact: ArtifactRef


class ScientificArtifactManifest(ResultModel):
    """Canonical ``scientific-artifact-manifest/v1`` input or output listing."""

    schema_: Literal["fs2-serve.nebius.ai/scientific-artifact-manifest/v1"] = Field(
        default=SCIENTIFIC_ARTIFACT_MANIFEST_SCHEMA, alias="schema"
    )
    manifest_id: OpaqueId
    entries: tuple[ManifestEntry, ...] = Field(min_length=1, max_length=MAX_MANIFEST_ENTRIES)

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True, populate_by_name=True)

    @model_validator(mode="after")
    def entry_names_are_unique(self) -> ScientificArtifactManifest:
        names = [entry.name for entry in self.entries]
        if len(set(names)) != len(names):
            raise ValueError("manifest entry names must be unique")
        return self

    def to_document(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    @property
    def digest(self) -> str:
        return canonical_result_digest(self.to_document())
