"""Durable scientific artifact provenance and result service.

Artifacts are content addressed and scoped to the exact stage, shard and
attempt that produced them, together with the Kueue admission identity that
attempt actually received. Stage completion publishes exactly one immutable
``scientific-artifact-manifest/v1`` commit, which is the value the scientific
batch controller reads back as its ``ArtifactCommit``. Operation completion
publishes exactly one immutable canonical ``scientific-run-result/v1``.

Two invariants are enforced everywhere, including in SQL:

* Object bytes, presigned URLs, signed headers and credentials are never
  persisted and never logged. Only content addresses and identities are stored.
* A terminal result fences the operation. A superseded attempt cannot write.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
from pydantic import AwareDatetime, ConfigDict, Field, StringConstraints, model_validator

from .models import StrictModel
from .scientific_batch.models import ArtifactCommit, batch_identity, workload_identity
from .scientific_run_result import (
    SCIENTIFIC_ARTIFACT_MANIFEST_SCHEMA,
    SCIENTIFIC_RUN_RESULT_SCHEMA,
    AccessAdmission,
    AccessProfile,
    AccessState,
    ArtifactRef,
    Compression,
    ManifestEntry,
    ResultAttempt,
    SchedulingAdmission,
    ScientificArtifactManifest,
    ScientificRunResult,
)
from .scientific_run_result import (
    AttemptStatus as PublicAttemptStatus,
)

ARTIFACT_RECORD_SCHEMA: Final = "fs2-serve.nebius.ai/scientific-artifact-record/v1"
SCIENTIFIC_ARTIFACT_MIGRATION = "0014_scientific_artifact_results.sql"
MAX_ARTIFACT_BYTES = 1 << 40
MAX_HANDLE_TTL = timedelta(minutes=15)
HANDLE_CLOCK_SKEW = timedelta(minutes=1)
DEFAULT_HANDLE_TTL = timedelta(minutes=10)
DEFAULT_RETENTION = timedelta(days=90)
MAX_RETENTION = timedelta(days=3650)
NO_SHARD = "-"
"""Stored sentinel for a gang-scheduled stage that has no shard identity."""

SHA256_PATTERN = r"^sha256:[a-f0-9]{64}$"
TENANT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
STAGE_PATTERN = r"^[a-z][a-z0-9-]*$"
SHARD_PATTERN = r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$"
MEDIA_TYPE_PATTERN = r"^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$"
UID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

TenantId = Annotated[str, StringConstraints(min_length=1, max_length=120, pattern=TENANT_PATTERN)]
StageId = Annotated[str, StringConstraints(min_length=1, max_length=63, pattern=STAGE_PATTERN)]
ShardId = Annotated[str, StringConstraints(min_length=1, max_length=253, pattern=SHARD_PATTERN)]
Sha256Digest = Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]
MediaType = Annotated[str, StringConstraints(min_length=3, max_length=128, pattern=MEDIA_TYPE_PATTERN)]
Uid = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=UID_PATTERN)]


class ScientificArtifactModel(StrictModel):
    """Strict contract that suppresses caller values in validation errors."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


class ArtifactServiceError(RuntimeError):
    """Base error with a stable, payload-free public code."""

    code = "artifact_service_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class ArtifactNotFoundError(ArtifactServiceError):
    code = "artifact_not_found"


class ArtifactConflictError(ArtifactServiceError):
    code = "artifact_conflict"


class StaleArtifactAttemptError(ArtifactServiceError):
    code = "stale_artifact_attempt"


class ArtifactVerificationError(ArtifactServiceError):
    code = "artifact_verification_failed"


class ArtifactPolicyError(ArtifactServiceError):
    code = "artifact_policy_rejected"


class ResultAlreadyTerminalError(ArtifactServiceError):
    """The operation already published its immutable terminal result."""

    code = "scientific_result_terminal"


class ArtifactDirection(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class ArtifactCompression(StrEnum):
    GZIP = "gzip"
    ZSTD = "zstd"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PREEMPTED = "preempted"

    @property
    def terminal(self) -> bool:
        return self is not AttemptStatus.RUNNING


class ArtifactAccessProfile(StrEnum):
    PUBLIC = "public"
    RESTRICTED = "restricted"
    ACADEMIC = "academic"


class ArtifactEventType(StrEnum):
    ATTEMPT_OPENED = "attempt_opened"
    ATTEMPT_CLOSED = "attempt_closed"
    UPLOAD_BEGUN = "upload_begun"
    ARTIFACT_FINALIZED = "artifact_finalized"
    STAGE_COMMITTED = "stage_committed"
    RESULT_COMMITTED = "result_committed"


class ArtifactAccess(ScientificArtifactModel):
    """Non-secret proof that gated bytes were made available lawfully."""

    profile: ArtifactAccessProfile = ArtifactAccessProfile.PUBLIC
    receipt_digest: Sha256Digest | None = None

    @model_validator(mode="after")
    def receipt_matches_profile(self) -> ArtifactAccess:
        if self.profile is ArtifactAccessProfile.PUBLIC and self.receipt_digest is not None:
            raise ValueError("public artifacts cannot carry a gated-access receipt")
        if self.profile is not ArtifactAccessProfile.PUBLIC and self.receipt_digest is None:
            raise ValueError("gated artifacts require a non-secret access receipt digest")
        return self

    def to_admission(self) -> AccessAdmission:
        """Project onto the canonical public access-admission shape."""

        if self.profile is ArtifactAccessProfile.PUBLIC:
            return AccessAdmission(profile=AccessProfile.STANDARD, state=AccessState.NOT_REQUIRED)
        profile = AccessProfile.ACADEMIC if self.profile is ArtifactAccessProfile.ACADEMIC else AccessProfile.STANDARD
        assert self.receipt_digest is not None
        return AccessAdmission(
            profile=profile,
            state=AccessState.VERIFIED,
            receipt_digest=self.receipt_digest.removeprefix("sha256:"),
        )


class KueueAdmission(ScientificArtifactModel):
    """The accelerator identity Kueue admitted for one stage/shard attempt."""

    resolved_pool_id: Annotated[str, StringConstraints(max_length=128)] | None = None
    admitted_resource_flavor: Annotated[str, StringConstraints(max_length=253)] | None = None
    accelerator_resource_name: Annotated[str, StringConstraints(max_length=253)] | None = None
    accelerator_count: int = Field(default=0, ge=0, le=1024)
    admitted_at: AwareDatetime

    @model_validator(mode="after")
    def identity_matches_count(self) -> KueueAdmission:
        bound = (self.resolved_pool_id, self.admitted_resource_flavor, self.accelerator_resource_name)
        if self.accelerator_count >= 1 and any(item is None for item in bound):
            raise ValueError("an accelerator admission must name its pool, flavor and resource")
        if self.accelerator_count == 0 and any(item is not None for item in bound):
            raise ValueError("a non-accelerator admission cannot name a pool, flavor or resource")
        return self

    def to_public(self) -> SchedulingAdmission:
        return SchedulingAdmission(
            resolved_pool_id=self.resolved_pool_id,
            admitted_resource_flavor=self.admitted_resource_flavor,
            accelerator_resource_name=self.accelerator_resource_name,
            accelerator_count=self.accelerator_count,
            admitted_at=self.admitted_at,
        )


class OpenStageAttempt(ScientificArtifactModel):
    """Register one scheduled stage/shard attempt before it writes anything."""

    attempt_id: UUID
    operation_id: UUID
    tenant_id: TenantId
    stage_id: StageId
    shard_id: ShardId | None = None
    attempt_number: int = Field(ge=1, le=10)
    admission: KueueAdmission | None = None
    kueue_workload_uid: Uid | None = None
    k8s_job_uid: Uid | None = None
    started_at: AwareDatetime


class CloseStageAttempt(ScientificArtifactModel):
    """Record the terminal outcome and observed GPU lifecycle identity."""

    attempt_id: UUID
    operation_id: UUID
    tenant_id: TenantId
    status: AttemptStatus
    completed_at: AwareDatetime
    admission: KueueAdmission | None = None
    kueue_workload_uid: Uid | None = None
    k8s_job_uid: Uid | None = None
    pod_uids: tuple[Uid, ...] = Field(default=(), max_length=1024)
    node_uids: tuple[Uid, ...] = Field(default=(), max_length=1024)
    gpu_uuids: tuple[Uid, ...] = Field(default=(), max_length=1024)

    @model_validator(mode="after")
    def outcome_is_terminal_and_unique(self) -> CloseStageAttempt:
        if not self.status.terminal:
            raise ValueError("closing an attempt requires a terminal outcome")
        for values, label in ((self.pod_uids, "pod"), (self.node_uids, "node"), (self.gpu_uuids, "gpu")):
            if len(set(values)) != len(values):
                raise ValueError(f"{label} identities must be unique")
        return self


class StageAttemptRecord(ScientificArtifactModel):
    """Persisted stage/shard attempt with its frozen admission identity."""

    attempt_id: UUID
    operation_id: UUID
    tenant_id: TenantId
    stage_id: StageId
    shard_id: ShardId | None = None
    attempt_number: int = Field(ge=1, le=10)
    status: AttemptStatus
    admission: KueueAdmission | None = None
    kueue_workload_uid: Uid | None = None
    k8s_job_uid: Uid | None = None
    pod_uids: tuple[Uid, ...] = Field(default=(), max_length=1024)
    node_uids: tuple[Uid, ...] = Field(default=(), max_length=1024)
    gpu_uuids: tuple[Uid, ...] = Field(default=(), max_length=1024)
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    retention_expires_at: AwareDatetime

    @model_validator(mode="after")
    def completion_matches_status(self) -> StageAttemptRecord:
        if self.status.terminal != (self.completed_at is not None):
            raise ValueError("attempt completion time must accompany a terminal outcome")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("an attempt cannot complete before it starts")
        if self.status in {AttemptStatus.SUCCEEDED, AttemptStatus.PREEMPTED} and self.admission is None:
            raise ValueError("an admitted attempt must retain its Kueue admission identity")
        return self

    @property
    def shard_key(self) -> str:
        return self.shard_id or NO_SHARD

    def to_public_attempt(self) -> ResultAttempt:
        """Project onto one canonical ``scientific-run-result/v1`` attempt."""

        if self.completed_at is None:
            raise ArtifactConflictError("a running attempt cannot appear in a terminal result")
        return ResultAttempt(
            attempt_id=str(self.attempt_id),
            stage_id=self.stage_id,
            shard_id=self.shard_id,
            attempt_number=self.attempt_number,
            status=PublicAttemptStatus(self.status.value),
            started_at=self.started_at,
            completed_at=self.completed_at,
            scheduling_admission=self.admission.to_public() if self.admission else None,
            kueue_workload_uid=self.kueue_workload_uid,
            k8s_job_uid=self.k8s_job_uid,
            pod_uids=self.pod_uids,
            node_uids=self.node_uids,
            gpu_uuids=self.gpu_uuids,
        )


class BeginArtifactUpload(ScientificArtifactModel):
    """Idempotent upload intent; callers reuse ``upload_id`` after a timeout."""

    upload_id: UUID
    attempt_id: UUID
    operation_id: UUID
    tenant_id: TenantId
    direction: ArtifactDirection
    expected_digest: Sha256Digest
    expected_size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    media_type: MediaType
    compression: ArtifactCompression | None = None
    access: ArtifactAccess = Field(default_factory=ArtifactAccess)


class FinalizeArtifactUpload(ScientificArtifactModel):
    upload_id: UUID
    operation_id: UUID
    tenant_id: TenantId


class VerifiedStoredObject(ScientificArtifactModel):
    """Metadata independently measured by the trusted object-store adapter."""

    storage_key: str = Field(min_length=1, max_length=1024)
    digest: Sha256Digest
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    media_type: MediaType
    compression: ArtifactCompression | None = None


class ArtifactRecord(ScientificArtifactModel):
    """Internal content address bound to the attempt that produced it."""

    schema_version: Literal["fs2-serve.nebius.ai/scientific-artifact-record/v1"] = ARTIFACT_RECORD_SCHEMA
    artifact_id: UUID
    attempt_id: UUID
    operation_id: UUID
    tenant_id: TenantId
    stage_id: StageId
    shard_id: ShardId | None = None
    direction: ArtifactDirection
    digest: Sha256Digest
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    media_type: MediaType
    compression: ArtifactCompression | None = None
    storage_key: str = Field(min_length=1, max_length=1024)
    access: ArtifactAccess
    retention_expires_at: AwareDatetime
    created_at: AwareDatetime

    @model_validator(mode="after")
    def content_address_matches_scope(self) -> ArtifactRecord:
        expected = artifact_storage_key(
            tenant_id=self.tenant_id,
            operation_id=self.operation_id,
            stage_id=self.stage_id,
            shard_id=self.shard_id,
            attempt_id=self.attempt_id,
            direction=self.direction,
            digest=self.digest,
        )
        if self.storage_key != expected:
            raise ValueError("artifact storage key is not the canonical content address")
        return self

    def to_public_ref(self) -> ArtifactRef:
        """Project onto the canonical public pointer; no location is exposed."""

        return ArtifactRef(
            artifact_id=str(self.artifact_id),
            sha256=self.digest.removeprefix("sha256:"),
            size_bytes=self.size_bytes,
            media_type=self.media_type,
            compression=Compression(self.compression.value) if self.compression else Compression.NONE,
        )


class UploadIntent(ScientificArtifactModel):
    """Durable upload expectation; the signed handle itself is never stored."""

    upload_id: UUID
    attempt_id: UUID
    operation_id: UUID
    tenant_id: TenantId
    stage_id: StageId
    shard_id: ShardId | None = None
    direction: ArtifactDirection
    expected_digest: Sha256Digest
    expected_size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    media_type: MediaType
    compression: ArtifactCompression | None = None
    storage_key: str = Field(min_length=1, max_length=1024)
    access: ArtifactAccess
    begun_at: AwareDatetime
    finalized_at: AwareDatetime | None = None
    artifact_id: UUID | None = None

    @model_validator(mode="after")
    def state_and_scope_are_consistent(self) -> UploadIntent:
        expected = artifact_storage_key(
            tenant_id=self.tenant_id,
            operation_id=self.operation_id,
            stage_id=self.stage_id,
            shard_id=self.shard_id,
            attempt_id=self.attempt_id,
            direction=self.direction,
            digest=self.expected_digest,
        )
        if self.storage_key != expected:
            raise ValueError("upload storage key is not canonical")
        if (self.finalized_at is None) != (self.artifact_id is None):
            raise ValueError("upload finalization state is incomplete")
        return self


class ManifestEntryDraft(ScientificArtifactModel):
    """One named, typed output an adapter publishes for a completed stage."""

    name: Annotated[str, StringConstraints(max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")]
    semantic_type: Annotated[str, StringConstraints(max_length=128, pattern=r"^[a-z][a-z0-9_.-]*/v[1-9][0-9]*$")]
    artifact_id: UUID


class CommitStageResult(ScientificArtifactModel):
    """Publish exactly one validated manifest for a completed stage."""

    operation_id: UUID
    tenant_id: TenantId
    stage_id: StageId
    attempt_ids: tuple[UUID, ...] = Field(min_length=1, max_length=10240)
    entries: tuple[ManifestEntryDraft, ...] = Field(min_length=1, max_length=10000)
    validation_digest: Sha256Digest
    semantic_valid: bool
    committed_at: AwareDatetime
    validated_at: AwareDatetime

    @model_validator(mode="after")
    def commit_identity_is_sound(self) -> CommitStageResult:
        if len(set(self.attempt_ids)) != len(self.attempt_ids):
            raise ValueError("stage commit attempt identities must be unique")
        names = [entry.name for entry in self.entries]
        if len(set(names)) != len(names):
            raise ValueError("stage manifest entry names must be unique")
        if self.validated_at < self.committed_at:
            raise ValueError("semantic validation cannot precede the atomic commit")
        return self


class StageCommitRecord(ScientificArtifactModel):
    """Immutable per-stage manifest commit read back by the batch controller."""

    operation_id: UUID
    tenant_id: TenantId
    stage_id: StageId
    attempt_ids: tuple[UUID, ...] = Field(min_length=1)
    manifest: ScientificArtifactManifest
    manifest_digest: Sha256Digest
    validation_digest: Sha256Digest
    semantic_valid: bool
    committed_at: AwareDatetime
    validated_at: AwareDatetime

    @model_validator(mode="after")
    def digest_matches_manifest(self) -> StageCommitRecord:
        if self.manifest_digest != self.manifest.digest:
            raise ValueError("stage manifest digest does not match its canonical document")
        return self

    def to_controller_commit(self) -> ArtifactCommit:
        """Project the canonical stage commit onto the controller aggregate."""

        return ArtifactCommit(
            operation_id=self.operation_id,
            stage_id=self.stage_id,
            attempt_ids=tuple(self.attempt_ids),
            manifest_digest=self.manifest_digest,
            validation_digest=self.validation_digest,
            committed_at=self.committed_at,
            validated_at=self.validated_at,
            semantic_valid=self.semantic_valid,
        )


class RunResultRecord(ScientificArtifactModel):
    """The stored canonical terminal result plus its immutable commit identity."""

    operation_id: UUID
    tenant_id: TenantId
    result: ScientificRunResult
    result_digest: Sha256Digest
    committed_at: AwareDatetime
    retention_expires_at: AwareDatetime

    @model_validator(mode="after")
    def digest_matches_document(self) -> RunResultRecord:
        if self.result_digest != self.result.digest:
            raise ValueError("terminal result digest does not match its canonical document")
        if self.committed_at < self.result.completed_at:
            raise ValueError("a terminal result cannot be committed before it completes")
        return self


class ArtifactEvent(ScientificArtifactModel):
    """Closed, payload-free durable event; there is no free-form detail map."""

    event_id: int = Field(ge=1)
    event_type: ArtifactEventType
    operation_id: UUID
    tenant_id: TenantId
    stage_id: StageId | None = None
    attempt_id: UUID | None = None
    upload_id: UUID | None = None
    artifact_id: UUID | None = None
    manifest_digest: Sha256Digest | None = None
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def identity_matches_event(self) -> ArtifactEvent:
        required, forbidden = _EVENT_SHAPE[self.event_type]
        present = {
            "stage_id": self.stage_id is not None,
            "attempt_id": self.attempt_id is not None,
            "upload_id": self.upload_id is not None,
            "artifact_id": self.artifact_id is not None,
            "manifest_digest": self.manifest_digest is not None,
        }
        if any(not present[name] for name in required) or any(present[name] for name in forbidden):
            raise ValueError("artifact event has an invalid identity shape")
        return self


_EVENT_SHAPE: Mapping[ArtifactEventType, tuple[frozenset[str], frozenset[str]]] = MappingProxyType(
    {
        ArtifactEventType.ATTEMPT_OPENED: (
            frozenset({"stage_id", "attempt_id"}),
            frozenset({"upload_id", "artifact_id", "manifest_digest"}),
        ),
        ArtifactEventType.ATTEMPT_CLOSED: (
            frozenset({"stage_id", "attempt_id"}),
            frozenset({"upload_id", "artifact_id", "manifest_digest"}),
        ),
        ArtifactEventType.UPLOAD_BEGUN: (
            frozenset({"stage_id", "attempt_id", "upload_id"}),
            frozenset({"artifact_id", "manifest_digest"}),
        ),
        ArtifactEventType.ARTIFACT_FINALIZED: (
            frozenset({"stage_id", "attempt_id", "upload_id", "artifact_id"}),
            frozenset({"manifest_digest"}),
        ),
        ArtifactEventType.STAGE_COMMITTED: (
            frozenset({"stage_id", "manifest_digest"}),
            frozenset({"attempt_id", "upload_id", "artifact_id"}),
        ),
        ArtifactEventType.RESULT_COMMITTED: (
            frozenset({"manifest_digest"}),
            frozenset({"stage_id", "attempt_id", "upload_id", "artifact_id"}),
        ),
    }
)


@dataclass(frozen=True, slots=True)
class EphemeralHandle:
    """Short-lived bearer material returned to a caller and never persisted."""

    method: Literal["GET", "PUT"]
    url: str = field(repr=False)
    expires_at: datetime
    write_once: bool = False
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True, slots=True)
class BeginUploadResult:
    upload: UploadIntent
    handle: EphemeralHandle = field(repr=False)


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    artifact: ArtifactRecord
    handle: EphemeralHandle = field(repr=False)


@dataclass(frozen=True, slots=True)
class RetentionPurge:
    """One completed retention deletion, retained as durable evidence."""

    operation_id: UUID
    tenant_id: str
    artifact_count: int
    byte_count: int
    retention_expired_at: datetime
    purged_at: datetime


def artifact_storage_key(
    *,
    tenant_id: str,
    operation_id: UUID,
    stage_id: str,
    shard_id: str | None,
    attempt_id: UUID,
    direction: ArtifactDirection,
    digest: str,
) -> str:
    """Return the only accepted attempt-scoped, content-addressed object key."""

    if re.fullmatch(TENANT_PATTERN, tenant_id) is None or len(tenant_id) > 120:
        raise ValueError("tenant identity is not canonical")
    if re.fullmatch(STAGE_PATTERN, stage_id) is None or len(stage_id) > 63:
        raise ValueError("stage identity is not canonical")
    shard = shard_id or NO_SHARD
    if shard != NO_SHARD and (re.fullmatch(SHARD_PATTERN, shard) is None or len(shard) > 253):
        raise ValueError("shard identity is not canonical")
    if re.fullmatch(SHA256_PATTERN, digest) is None:
        raise ValueError("artifact digest is not canonical")
    return (
        f"scientific/v1/tenants/{tenant_id}/operations/{operation_id}"
        f"/stages/{stage_id}/shards/{shard}/attempts/{attempt_id}"
        f"/{direction.value}/sha256/{digest.removeprefix('sha256:')}"
    )


def build_stage_manifest(
    *, operation_id: UUID, stage_id: str, entries: Sequence[tuple[ManifestEntryDraft, ArtifactRecord]]
) -> ScientificArtifactManifest:
    """Build the canonical stage manifest in a deterministic entry order."""

    ordered = sorted(entries, key=lambda item: item[0].name)
    return ScientificArtifactManifest(
        schema=SCIENTIFIC_ARTIFACT_MANIFEST_SCHEMA,
        manifest_id=f"{operation_id}:{stage_id}",
        entries=tuple(
            ManifestEntry(name=draft.name, semantic_type=draft.semantic_type, artifact=record.to_public_ref())
            for draft, record in ordered
        ),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_handle(
    handle: EphemeralHandle,
    *,
    method: Literal["GET", "PUT"],
    now: datetime,
    ttl: timedelta,
    require_tls: bool,
) -> None:
    """Reject any handle that is long-lived, reusable, or not bearer-safe.

    The adapter stamps the deadline from the same wall clock the gateway checks,
    so the bound below allows one minute of skew against the service clock.
    """

    deadline = now + ttl + HANDLE_CLOCK_SKEW
    parsed = urlsplit(handle.url)
    allowed_schemes = ("https",) if require_tls else ("https", "http")
    if (
        handle.method != method
        or (method == "PUT") != handle.write_once
        or handle.expires_at.tzinfo is None
        or not now < handle.expires_at <= deadline
        or parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(
            not isinstance(key, str) or not key or not isinstance(value, str) for key, value in handle.headers.items()
        )
    ):
        raise ArtifactPolicyError("artifact handle violates the short-lived bearer policy")


def _same_upload_request(intent: UploadIntent, request: BeginArtifactUpload, storage_key: str) -> bool:
    return (
        intent.upload_id == request.upload_id
        and intent.attempt_id == request.attempt_id
        and intent.operation_id == request.operation_id
        and intent.tenant_id == request.tenant_id
        and intent.direction is request.direction
        and intent.expected_digest == request.expected_digest
        and intent.expected_size_bytes == request.expected_size_bytes
        and intent.media_type == request.media_type
        and intent.compression == request.compression
        and intent.storage_key == storage_key
        and intent.access == request.access
    )


def _verify_object(intent: UploadIntent, verified: VerifiedStoredObject) -> None:
    """Reject any stored object that differs from the declared expectation."""

    if verified.storage_key != intent.storage_key:
        raise ArtifactVerificationError("stored object key differs from the upload intent")
    if verified.digest != intent.expected_digest:
        raise ArtifactVerificationError("stored object digest differs from the upload intent")
    if verified.size_bytes != intent.expected_size_bytes:
        raise ArtifactVerificationError("stored object size differs from the upload intent")
    if verified.media_type != intent.media_type:
        raise ArtifactVerificationError("stored object media type differs from the upload intent")
    if verified.compression != intent.compression:
        raise ArtifactVerificationError("stored object compression differs from the upload intent")


class ArtifactObjectStorePort(Protocol):
    """Trusted adapter that owns bytes, signatures and independent measurement."""

    async def presign_upload(
        self,
        *,
        storage_key: str,
        media_type: str,
        compression: ArtifactCompression | None,
        ttl: timedelta,
    ) -> EphemeralHandle: ...

    async def presign_download(self, *, storage_key: str, ttl: timedelta) -> EphemeralHandle: ...

    async def inspect(self, storage_key: str, *, max_bytes: int | None = None) -> VerifiedStoredObject: ...

    async def delete(self, storage_key: str) -> None: ...


class ArtifactRepository(Protocol):
    """Durable persistence bound to an already-admitted Operation identity."""

    async def open_attempt(self, request: OpenStageAttempt, *, retention: timedelta) -> StageAttemptRecord: ...

    async def close_attempt(self, request: CloseStageAttempt) -> StageAttemptRecord: ...

    async def get_attempt(self, attempt_id: UUID, *, tenant_id: str) -> StageAttemptRecord: ...

    async def list_attempts(self, operation_id: UUID, *, tenant_id: str) -> list[StageAttemptRecord]: ...

    async def begin_upload(
        self, request: BeginArtifactUpload, storage_key: str, *, retention: timedelta
    ) -> UploadIntent: ...

    async def get_upload(self, request: FinalizeArtifactUpload) -> UploadIntent: ...

    async def finalize_upload(
        self, request: FinalizeArtifactUpload, verified: VerifiedStoredObject, *, artifact_id: UUID
    ) -> ArtifactRecord: ...

    async def get_artifact(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactRecord: ...

    async def list_artifacts(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        stage_id: str | None = None,
        attempt_id: UUID | None = None,
    ) -> list[ArtifactRecord]: ...

    async def commit_stage(self, request: CommitStageResult) -> StageCommitRecord: ...

    async def stage_commit(
        self, operation_id: UUID, *, stage_id: str, tenant_id: str | None = None
    ) -> StageCommitRecord | None: ...

    async def commit_run_result(self, record: RunResultRecord) -> RunResultRecord: ...

    async def get_run_result(self, operation_id: UUID, *, tenant_id: str) -> RunResultRecord: ...

    async def list_events(
        self, operation_id: UUID, *, tenant_id: str, after_id: int = 0, limit: int = 500
    ) -> list[ArtifactEvent]: ...

    async def claim_expired(self, *, now: datetime, limit: int) -> list[tuple[UUID, str, datetime]]: ...

    async def purge_operation(self, operation_id: UUID, *, tenant_id: str, now: datetime) -> RetentionPurge: ...

    async def purge_keys(self, operation_id: UUID, *, tenant_id: str) -> list[str]: ...


class ScientificArtifactControllerPort(Protocol):
    """Stable port consumed by the scientific batch controller and routes."""

    async def open_attempt(self, request: OpenStageAttempt) -> StageAttemptRecord: ...

    async def close_attempt(self, request: CloseStageAttempt) -> StageAttemptRecord: ...

    async def begin_upload(
        self, request: BeginArtifactUpload, *, handle_ttl: timedelta | None = None
    ) -> BeginUploadResult: ...

    async def finalize_upload(self, request: FinalizeArtifactUpload) -> ArtifactRecord: ...

    async def download(
        self, artifact_id: UUID, *, tenant_id: str, handle_ttl: timedelta | None = None
    ) -> ArtifactDownload: ...

    async def list_artifacts(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        stage_id: str | None = None,
        attempt_id: UUID | None = None,
    ) -> list[ArtifactRecord]: ...

    async def commit_stage(self, request: CommitStageResult) -> StageCommitRecord: ...

    async def artifact_commit(
        self, operation_id: UUID, *, stage_id: str, tenant_id: str | None = None
    ) -> ArtifactCommit | None: ...

    async def stage_commit(
        self, operation_id: UUID, *, stage_id: str, tenant_id: str | None = None
    ) -> StageCommitRecord | None: ...

    async def commit_run_result(self, draft: RunResultDraft) -> RunResultRecord: ...

    async def get_run_result(self, operation_id: UUID, *, tenant_id: str) -> RunResultRecord: ...

    async def list_events(
        self, operation_id: UUID, *, tenant_id: str, after_id: int = 0, limit: int = 500
    ) -> list[ArtifactEvent]: ...


class RunResultDraft(ScientificArtifactModel):
    """Everything the caller supplies; attempts and digests come from storage."""

    operation_id: UUID
    tenant_id: TenantId
    terminal_status: Literal["succeeded", "failed", "cancelled"]
    submitted_at: AwareDatetime
    completed_at: AwareDatetime
    execution_identity: Mapping[str, Any]
    access: ArtifactAccess = Field(default_factory=ArtifactAccess)
    scheduling_snapshot: Mapping[str, Any]
    input_manifest_artifact_id: UUID
    output_manifest_artifact_id: UUID | None = None
    validator_id: Annotated[str, StringConstraints(max_length=253)]
    validation_status: Literal["passed", "failed", "not-run"]
    validation_receipt_digest: Sha256Digest | None = None
    error_code: Annotated[str, StringConstraints(max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")] | None = None
    error_message: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    error_retryable: bool | None = None

    @model_validator(mode="after")
    def error_fields_travel_together(self) -> RunResultDraft:
        supplied = (self.error_code, self.error_message, self.error_retryable)
        if any(item is None for item in supplied) and any(item is not None for item in supplied):
            raise ValueError("an error must supply a code, a message and a retryable flag together")
        if self.terminal_status == "failed" and self.error_code is None:
            raise ValueError("a failed run requires a structured error")
        if self.terminal_status == "succeeded" and self.error_code is not None:
            raise ValueError("a succeeded run cannot carry an error")
        return self


class ScientificArtifactService:
    """Coordinates verified storage, ephemeral handles and durable provenance."""

    def __init__(
        self,
        *,
        repository: ArtifactRepository,
        object_store: ArtifactObjectStorePort,
        allowed_media_types: Iterable[str],
        max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
        max_handle_ttl: timedelta = MAX_HANDLE_TTL,
        default_handle_ttl: timedelta = DEFAULT_HANDLE_TTL,
        retention: timedelta = DEFAULT_RETENTION,
        require_tls_handles: bool = True,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        allowed = frozenset(allowed_media_types)
        if not allowed or any(re.fullmatch(MEDIA_TYPE_PATTERN, item) is None for item in allowed):
            raise ValueError("allowed media types must be a non-empty exact allowlist")
        if not 0 < max_artifact_bytes <= MAX_ARTIFACT_BYTES:
            raise ValueError("the artifact ceiling is outside the supported range")
        if not timedelta(0) < max_handle_ttl <= MAX_HANDLE_TTL:
            raise ValueError("handle lifetime must be positive and at most fifteen minutes")
        if not timedelta(0) < default_handle_ttl <= max_handle_ttl:
            raise ValueError("the default handle lifetime must not exceed the maximum")
        if not timedelta(0) < retention <= MAX_RETENTION:
            raise ValueError("artifact retention is outside the supported range")
        self._repository = repository
        self._store = object_store
        self._allowed_media_types = allowed
        self._max_artifact_bytes = max_artifact_bytes
        self._max_handle_ttl = max_handle_ttl
        self._default_handle_ttl = default_handle_ttl
        self._retention = retention
        self._require_tls = require_tls_handles
        self._clock = clock

    @property
    def retention(self) -> timedelta:
        return self._retention

    def _check_policy(self, media_type: str, size_bytes: int) -> None:
        if media_type not in self._allowed_media_types:
            raise ArtifactPolicyError("artifact media type is outside the accepted allowlist")
        if size_bytes > self._max_artifact_bytes:
            raise ArtifactPolicyError("artifact exceeds the accepted size ceiling")

    def _ttl(self, ttl: timedelta | None) -> timedelta:
        requested = ttl or self._default_handle_ttl
        if requested <= timedelta(0) or requested > self._max_handle_ttl:
            raise ArtifactPolicyError("requested handle lifetime is outside the accepted range")
        return requested

    async def open_attempt(self, request: OpenStageAttempt) -> StageAttemptRecord:
        return await self._repository.open_attempt(request, retention=self._retention)

    async def close_attempt(self, request: CloseStageAttempt) -> StageAttemptRecord:
        return await self._repository.close_attempt(request)

    async def begin_upload(
        self, request: BeginArtifactUpload, *, handle_ttl: timedelta | None = None
    ) -> BeginUploadResult:
        """Reserve one content address and issue a write-once upload handle."""

        self._check_policy(request.media_type, request.expected_size_bytes)
        attempt = await self._repository.get_attempt(request.attempt_id, tenant_id=request.tenant_id)
        if attempt.operation_id != request.operation_id:
            raise ArtifactNotFoundError("attempt not found")
        if attempt.status.terminal:
            raise StaleArtifactAttemptError("a closed attempt cannot accept new artifacts")
        storage_key = artifact_storage_key(
            tenant_id=request.tenant_id,
            operation_id=request.operation_id,
            stage_id=attempt.stage_id,
            shard_id=attempt.shard_id,
            attempt_id=request.attempt_id,
            direction=request.direction,
            digest=request.expected_digest,
        )
        lifetime = self._ttl(handle_ttl)
        intent = await self._repository.begin_upload(request, storage_key, retention=self._retention)
        if not _same_upload_request(intent, request, storage_key):
            raise ArtifactConflictError("upload identity is already bound to different content")
        handle = await self._store.presign_upload(
            storage_key=storage_key,
            media_type=request.media_type,
            compression=request.compression,
            ttl=lifetime,
        )
        _validate_handle(handle, method="PUT", now=self._clock(), ttl=lifetime, require_tls=self._require_tls)
        return BeginUploadResult(upload=intent, handle=handle)

    async def finalize_upload(self, request: FinalizeArtifactUpload) -> ArtifactRecord:
        """Verify the stored bytes independently, then publish the content address."""

        intent = await self._repository.get_upload(request)
        if intent.artifact_id is not None:
            return await self._repository.get_artifact(intent.artifact_id, tenant_id=intent.tenant_id)
        verified = await self._store.inspect(
            intent.storage_key, max_bytes=min(self._max_artifact_bytes, intent.expected_size_bytes)
        )
        _verify_object(intent, verified)
        self._check_policy(verified.media_type, verified.size_bytes)
        return await self._repository.finalize_upload(request, verified, artifact_id=uuid4())

    async def download(
        self, artifact_id: UUID, *, tenant_id: str, handle_ttl: timedelta | None = None
    ) -> ArtifactDownload:
        record = await self._repository.get_artifact(artifact_id, tenant_id=tenant_id)
        lifetime = self._ttl(handle_ttl)
        handle = await self._store.presign_download(storage_key=record.storage_key, ttl=lifetime)
        _validate_handle(handle, method="GET", now=self._clock(), ttl=lifetime, require_tls=self._require_tls)
        return ArtifactDownload(artifact=record, handle=handle)

    async def list_artifacts(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        stage_id: str | None = None,
        attempt_id: UUID | None = None,
    ) -> list[ArtifactRecord]:
        """Return bounded internal metadata for controller-owned aggregation."""

        return await self._repository.list_artifacts(
            operation_id,
            tenant_id=tenant_id,
            stage_id=stage_id,
            attempt_id=attempt_id,
        )

    async def commit_stage(self, request: CommitStageResult) -> StageCommitRecord:
        """Publish exactly one immutable manifest commit for a completed stage."""

        return await self._repository.commit_stage(request)

    async def stage_commit(
        self, operation_id: UUID, *, stage_id: str, tenant_id: str | None = None
    ) -> StageCommitRecord | None:
        """Return the full immutable manifest commit published for one stage."""

        return await self._repository.stage_commit(operation_id, stage_id=stage_id, tenant_id=tenant_id)

    async def artifact_commit(
        self, operation_id: UUID, *, stage_id: str, tenant_id: str | None = None
    ) -> ArtifactCommit | None:
        """Return the canonical controller aggregate for one committed stage."""

        record = await self.stage_commit(operation_id, stage_id=stage_id, tenant_id=tenant_id)
        return record.to_controller_commit() if record is not None else None

    async def commit_run_result(self, draft: RunResultDraft) -> RunResultRecord:
        """Assemble and durably publish the canonical terminal run result."""

        attempts = await self._repository.list_attempts(draft.operation_id, tenant_id=draft.tenant_id)
        terminal = [attempt for attempt in attempts if attempt.status.terminal]
        if len(terminal) != len(attempts):
            raise ArtifactConflictError("every attempt must be terminal before the run result is published")
        input_manifest = await self._repository.get_artifact(
            draft.input_manifest_artifact_id, tenant_id=draft.tenant_id
        )
        output_manifest = None
        if draft.output_manifest_artifact_id is not None:
            output_manifest = await self._repository.get_artifact(
                draft.output_manifest_artifact_id, tenant_id=draft.tenant_id
            )
            if output_manifest.operation_id != draft.operation_id:
                raise ArtifactConflictError("the output manifest belongs to another operation")
        # Input manifests are immutable tenant-scoped artifacts prepared before
        # submission and can therefore belong to an earlier durable Operation.
        # ``get_artifact`` already enforced the exact tenant boundary.
        error = None
        if draft.error_code is not None:
            error = {
                "code": draft.error_code,
                "message": draft.error_message,
                "retryable": draft.error_retryable,
            }
        result = ScientificRunResult.model_validate(
            {
                "schema": SCIENTIFIC_RUN_RESULT_SCHEMA,
                "operation_id": str(draft.operation_id),
                "batch_id": str(batch_identity(draft.operation_id)),
                "workload_id": str(workload_identity(draft.operation_id)),
                "terminal_status": draft.terminal_status,
                "submitted_at": draft.submitted_at,
                "completed_at": draft.completed_at,
                "execution_identity": dict(draft.execution_identity),
                "access_admission": draft.access.to_admission().model_dump(mode="json"),
                "scheduling_snapshot": dict(draft.scheduling_snapshot),
                "input_manifest": input_manifest.to_public_ref().model_dump(mode="json"),
                "output_manifest": (
                    output_manifest.to_public_ref().model_dump(mode="json") if output_manifest else None
                ),
                "attempts": [
                    attempt.to_public_attempt().model_dump(mode="json")
                    for attempt in sorted(
                        terminal, key=lambda item: (item.stage_id, item.shard_key, item.attempt_number)
                    )
                ],
                "semantic_validation": {
                    "validator_id": draft.validator_id,
                    "status": draft.validation_status,
                    "receipt_digest": (
                        draft.validation_receipt_digest.removeprefix("sha256:")
                        if draft.validation_receipt_digest
                        else None
                    ),
                },
                "error": error,
            }
        )
        now = self._clock()
        record = RunResultRecord(
            operation_id=draft.operation_id,
            tenant_id=draft.tenant_id,
            result=result,
            result_digest=result.digest,
            committed_at=max(now, draft.completed_at),
            retention_expires_at=max(now, draft.completed_at) + self._retention,
        )
        return await self._repository.commit_run_result(record)

    async def get_run_result(self, operation_id: UUID, *, tenant_id: str) -> RunResultRecord:
        return await self._repository.get_run_result(operation_id, tenant_id=tenant_id)

    async def list_events(
        self, operation_id: UUID, *, tenant_id: str, after_id: int = 0, limit: int = 500
    ) -> list[ArtifactEvent]:
        return await self._repository.list_events(operation_id, tenant_id=tenant_id, after_id=after_id, limit=limit)

    async def purge_expired(self, *, limit: int = 50) -> list[RetentionPurge]:
        """Delete retired objects, then their metadata, and record the evidence.

        Object deletion is idempotent and runs before the durable rows are
        removed, so an interrupted purge converges on the next pass instead of
        leaving metadata that points at bytes which are already gone.
        """

        now = self._clock()
        purges: list[RetentionPurge] = []
        for operation_id, tenant_id, _ in await self._repository.claim_expired(now=now, limit=limit):
            for storage_key in await self._repository.purge_keys(operation_id, tenant_id=tenant_id):
                await self._store.delete(storage_key)
            try:
                purges.append(await self._repository.purge_operation(operation_id, tenant_id=tenant_id, now=now))
            except ArtifactConflictError:
                # Another worker claimed this operation between the scan and the
                # delete. Its purge is authoritative, so skip rather than fail.
                continue
        return purges


@dataclass
class _MemoryOperation:
    tenant_id: str


class MemoryArtifactRepository:
    """Reference in-process implementation with the same fences as PostgreSQL.

    Every mutating call holds one lock, so the fencing and exactly-once rules
    are exercised under concurrency exactly as the SQL implementation is.
    """

    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._operations: dict[UUID, _MemoryOperation] = {}
        self._attempts: dict[UUID, StageAttemptRecord] = {}
        self._uploads: dict[UUID, UploadIntent] = {}
        self._artifacts: dict[UUID, ArtifactRecord] = {}
        self._stage_commits: dict[tuple[UUID, str], StageCommitRecord] = {}
        self._run_results: dict[UUID, RunResultRecord] = {}
        self._events: list[ArtifactEvent] = []
        self._purged: set[UUID] = set()
        self._next_event_id = 1

    async def register_operation(self, operation_id: UUID, *, tenant_id: str) -> None:
        async with self._lock:
            self._operations[operation_id] = _MemoryOperation(tenant_id=tenant_id)

    def _assert_writable(self, operation_id: UUID, tenant_id: str) -> None:
        operation = self._operations.get(operation_id)
        if operation is None or operation.tenant_id != tenant_id:
            raise ArtifactNotFoundError("operation not found")
        if operation_id in self._run_results:
            raise ResultAlreadyTerminalError("the operation already published a terminal result")

    def _assert_live_attempt(self, attempt: StageAttemptRecord) -> None:
        newest = max(
            (
                item.attempt_number
                for item in self._attempts.values()
                if item.operation_id == attempt.operation_id
                and item.stage_id == attempt.stage_id
                and item.shard_key == attempt.shard_key
            ),
            default=attempt.attempt_number,
        )
        if attempt.attempt_number < newest:
            raise StaleArtifactAttemptError("a superseded attempt cannot write artifacts")

    def _append_event(
        self,
        event_type: ArtifactEventType,
        *,
        operation_id: UUID,
        tenant_id: str,
        stage_id: str | None = None,
        attempt_id: UUID | None = None,
        upload_id: UUID | None = None,
        artifact_id: UUID | None = None,
        manifest_digest: str | None = None,
        occurred_at: datetime,
    ) -> None:
        self._events.append(
            ArtifactEvent(
                event_id=self._next_event_id,
                event_type=event_type,
                operation_id=operation_id,
                tenant_id=tenant_id,
                stage_id=stage_id,
                attempt_id=attempt_id,
                upload_id=upload_id,
                artifact_id=artifact_id,
                manifest_digest=manifest_digest,
                occurred_at=occurred_at,
            )
        )
        self._next_event_id += 1

    async def open_attempt(self, request: OpenStageAttempt, *, retention: timedelta) -> StageAttemptRecord:
        async with self._lock:
            self._assert_writable(request.operation_id, request.tenant_id)
            existing = self._attempts.get(request.attempt_id)
            record = StageAttemptRecord(
                attempt_id=request.attempt_id,
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
                stage_id=request.stage_id,
                shard_id=request.shard_id,
                attempt_number=request.attempt_number,
                status=AttemptStatus.RUNNING,
                admission=request.admission,
                kueue_workload_uid=request.kueue_workload_uid,
                k8s_job_uid=request.k8s_job_uid,
                started_at=request.started_at,
                retention_expires_at=request.started_at + retention,
            )
            if existing is not None:
                if (
                    existing.operation_id != record.operation_id
                    or existing.stage_id != record.stage_id
                    or existing.shard_key != record.shard_key
                    or existing.attempt_number != record.attempt_number
                ):
                    raise ArtifactConflictError("attempt identity is already bound to another scope")
                return existing
            duplicate = any(
                item.operation_id == record.operation_id
                and item.stage_id == record.stage_id
                and item.shard_key == record.shard_key
                and item.attempt_number == record.attempt_number
                for item in self._attempts.values()
            )
            if duplicate:
                raise ArtifactConflictError("this stage, shard and attempt number already exists")
            self._attempts[record.attempt_id] = record
            self._append_event(
                ArtifactEventType.ATTEMPT_OPENED,
                operation_id=record.operation_id,
                tenant_id=record.tenant_id,
                stage_id=record.stage_id,
                attempt_id=record.attempt_id,
                occurred_at=record.started_at,
            )
            return record

    async def close_attempt(self, request: CloseStageAttempt) -> StageAttemptRecord:
        async with self._lock:
            self._assert_writable(request.operation_id, request.tenant_id)
            existing = self._attempts.get(request.attempt_id)
            if (
                existing is None
                or existing.operation_id != request.operation_id
                or existing.tenant_id != request.tenant_id
            ):
                raise ArtifactNotFoundError("attempt not found")
            if existing.status.terminal:
                if existing.status is not request.status or existing.completed_at != request.completed_at:
                    raise ArtifactConflictError("the attempt already recorded a different outcome")
                return existing
            record = existing.model_copy(
                update={
                    "status": request.status,
                    "completed_at": request.completed_at,
                    "admission": request.admission or existing.admission,
                    "kueue_workload_uid": request.kueue_workload_uid or existing.kueue_workload_uid,
                    "k8s_job_uid": request.k8s_job_uid or existing.k8s_job_uid,
                    "pod_uids": request.pod_uids,
                    "node_uids": request.node_uids,
                    "gpu_uuids": request.gpu_uuids,
                }
            )
            StageAttemptRecord.model_validate(record.model_dump())
            self._attempts[record.attempt_id] = record
            self._append_event(
                ArtifactEventType.ATTEMPT_CLOSED,
                operation_id=record.operation_id,
                tenant_id=record.tenant_id,
                stage_id=record.stage_id,
                attempt_id=record.attempt_id,
                occurred_at=request.completed_at,
            )
            return record

    async def get_attempt(self, attempt_id: UUID, *, tenant_id: str) -> StageAttemptRecord:
        async with self._lock:
            record = self._attempts.get(attempt_id)
            if record is None or record.tenant_id != tenant_id:
                raise ArtifactNotFoundError("attempt not found")
            return record

    async def list_attempts(self, operation_id: UUID, *, tenant_id: str) -> list[StageAttemptRecord]:
        async with self._lock:
            return [
                item
                for item in self._attempts.values()
                if item.operation_id == operation_id and item.tenant_id == tenant_id
            ]

    async def begin_upload(
        self, request: BeginArtifactUpload, storage_key: str, *, retention: timedelta
    ) -> UploadIntent:
        async with self._lock:
            self._assert_writable(request.operation_id, request.tenant_id)
            attempt = self._attempts.get(request.attempt_id)
            if (
                attempt is None
                or attempt.operation_id != request.operation_id
                or attempt.tenant_id != request.tenant_id
            ):
                raise ArtifactNotFoundError("attempt not found")
            self._assert_live_attempt(attempt)
            existing = self._uploads.get(request.upload_id)
            if existing is not None:
                return existing
            now = self._clock()
            intent = UploadIntent(
                upload_id=request.upload_id,
                attempt_id=request.attempt_id,
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
                stage_id=attempt.stage_id,
                shard_id=attempt.shard_id,
                direction=request.direction,
                expected_digest=request.expected_digest,
                expected_size_bytes=request.expected_size_bytes,
                media_type=request.media_type,
                compression=request.compression,
                storage_key=storage_key,
                access=request.access,
                begun_at=now,
            )
            collision = any(
                item.storage_key == storage_key and item.upload_id != intent.upload_id
                for item in self._uploads.values()
            )
            if collision:
                raise ArtifactConflictError("this content address is already reserved")
            self._uploads[intent.upload_id] = intent
            self._append_event(
                ArtifactEventType.UPLOAD_BEGUN,
                operation_id=intent.operation_id,
                tenant_id=intent.tenant_id,
                stage_id=intent.stage_id,
                attempt_id=intent.attempt_id,
                upload_id=intent.upload_id,
                occurred_at=now,
            )
            return intent

    async def get_upload(self, request: FinalizeArtifactUpload) -> UploadIntent:
        async with self._lock:
            intent = self._uploads.get(request.upload_id)
            if intent is None or intent.operation_id != request.operation_id or intent.tenant_id != request.tenant_id:
                raise ArtifactNotFoundError("upload not found")
            return intent

    async def finalize_upload(
        self, request: FinalizeArtifactUpload, verified: VerifiedStoredObject, *, artifact_id: UUID
    ) -> ArtifactRecord:
        async with self._lock:
            self._assert_writable(request.operation_id, request.tenant_id)
            intent = self._uploads.get(request.upload_id)
            if intent is None or intent.operation_id != request.operation_id or intent.tenant_id != request.tenant_id:
                raise ArtifactNotFoundError("upload not found")
            if intent.artifact_id is not None:
                return self._artifacts[intent.artifact_id]
            attempt = self._attempts[intent.attempt_id]
            self._assert_live_attempt(attempt)
            _verify_object(intent, verified)
            now = self._clock()
            record = ArtifactRecord(
                artifact_id=artifact_id,
                attempt_id=intent.attempt_id,
                operation_id=intent.operation_id,
                tenant_id=intent.tenant_id,
                stage_id=intent.stage_id,
                shard_id=intent.shard_id,
                direction=intent.direction,
                digest=verified.digest,
                size_bytes=verified.size_bytes,
                media_type=verified.media_type,
                compression=verified.compression,
                storage_key=verified.storage_key,
                access=intent.access,
                retention_expires_at=now + (attempt.retention_expires_at - attempt.started_at),
                created_at=now,
            )
            self._artifacts[record.artifact_id] = record
            self._uploads[intent.upload_id] = intent.model_copy(
                update={"artifact_id": record.artifact_id, "finalized_at": now}
            )
            self._append_event(
                ArtifactEventType.ARTIFACT_FINALIZED,
                operation_id=record.operation_id,
                tenant_id=record.tenant_id,
                stage_id=record.stage_id,
                attempt_id=record.attempt_id,
                upload_id=intent.upload_id,
                artifact_id=record.artifact_id,
                occurred_at=now,
            )
            return record

    async def get_artifact(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactRecord:
        async with self._lock:
            record = self._artifacts.get(artifact_id)
            if record is None or record.tenant_id != tenant_id:
                raise ArtifactNotFoundError("artifact not found")
            return record

    async def list_artifacts(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        stage_id: str | None = None,
        attempt_id: UUID | None = None,
    ) -> list[ArtifactRecord]:
        async with self._lock:
            return sorted(
                (
                    record
                    for record in self._artifacts.values()
                    if record.operation_id == operation_id
                    and record.tenant_id == tenant_id
                    and (stage_id is None or record.stage_id == stage_id)
                    and (attempt_id is None or record.attempt_id == attempt_id)
                ),
                key=lambda record: (
                    record.stage_id,
                    record.shard_id or "",
                    str(record.attempt_id),
                    str(record.artifact_id),
                ),
            )

    def _validate_commit(self, request: CommitStageResult) -> ScientificArtifactManifest:
        """Prove the commit names exactly the stage's succeeded attempts."""

        succeeded = {
            item.attempt_id
            for item in self._attempts.values()
            if item.operation_id == request.operation_id
            and item.stage_id == request.stage_id
            and item.status is AttemptStatus.SUCCEEDED
        }
        if succeeded != set(request.attempt_ids):
            raise ArtifactConflictError("the commit does not name the stage's succeeded attempts")
        pairs: list[tuple[ManifestEntryDraft, ArtifactRecord]] = []
        for entry in request.entries:
            record = self._artifacts.get(entry.artifact_id)
            if (
                record is None
                or record.tenant_id != request.tenant_id
                or record.operation_id != request.operation_id
                or record.stage_id != request.stage_id
            ):
                raise ArtifactNotFoundError("artifact not found")
            if record.direction is not ArtifactDirection.OUTPUT:
                raise ArtifactConflictError("only output artifacts can be committed to a stage manifest")
            if record.attempt_id not in succeeded:
                raise StaleArtifactAttemptError("a committed artifact belongs to a non-succeeded attempt")
            pairs.append((entry, record))
        return build_stage_manifest(operation_id=request.operation_id, stage_id=request.stage_id, entries=pairs)

    async def commit_stage(self, request: CommitStageResult) -> StageCommitRecord:
        async with self._lock:
            self._assert_writable(request.operation_id, request.tenant_id)
            manifest = self._validate_commit(request)
            record = StageCommitRecord(
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
                stage_id=request.stage_id,
                attempt_ids=tuple(sorted(request.attempt_ids, key=str)),
                manifest=manifest,
                manifest_digest=manifest.digest,
                validation_digest=request.validation_digest,
                semantic_valid=request.semantic_valid,
                committed_at=request.committed_at,
                validated_at=request.validated_at,
            )
            existing = self._stage_commits.get((request.operation_id, request.stage_id))
            if existing is not None:
                if existing.manifest_digest != record.manifest_digest:
                    raise ArtifactConflictError("this stage already committed a different manifest")
                return existing
            self._stage_commits[(request.operation_id, request.stage_id)] = record
            self._append_event(
                ArtifactEventType.STAGE_COMMITTED,
                operation_id=record.operation_id,
                tenant_id=record.tenant_id,
                stage_id=record.stage_id,
                manifest_digest=record.manifest_digest,
                occurred_at=record.committed_at,
            )
            return record

    async def stage_commit(
        self, operation_id: UUID, *, stage_id: str, tenant_id: str | None = None
    ) -> StageCommitRecord | None:
        async with self._lock:
            record = self._stage_commits.get((operation_id, stage_id))
            if record is None or (tenant_id is not None and record.tenant_id != tenant_id):
                return None
            return record

    async def commit_run_result(self, record: RunResultRecord) -> RunResultRecord:
        async with self._lock:
            operation = self._operations.get(record.operation_id)
            if operation is None or operation.tenant_id != record.tenant_id:
                raise ArtifactNotFoundError("operation not found")
            existing = self._run_results.get(record.operation_id)
            if existing is not None:
                if existing.result_digest != record.result_digest:
                    raise ResultAlreadyTerminalError("the operation already published a terminal result")
                return existing
            self._run_results[record.operation_id] = record
            self._append_event(
                ArtifactEventType.RESULT_COMMITTED,
                operation_id=record.operation_id,
                tenant_id=record.tenant_id,
                manifest_digest=record.result_digest,
                occurred_at=record.committed_at,
            )
            return record

    async def get_run_result(self, operation_id: UUID, *, tenant_id: str) -> RunResultRecord:
        async with self._lock:
            record = self._run_results.get(operation_id)
            if record is None or record.tenant_id != tenant_id:
                raise ArtifactNotFoundError("terminal result not found")
            return record

    async def list_events(
        self, operation_id: UUID, *, tenant_id: str, after_id: int = 0, limit: int = 500
    ) -> list[ArtifactEvent]:
        async with self._lock:
            matching = [
                event
                for event in self._events
                if event.operation_id == operation_id and event.tenant_id == tenant_id and event.event_id > after_id
            ]
            return matching[: max(1, limit)]

    async def claim_expired(self, *, now: datetime, limit: int) -> list[tuple[UUID, str, datetime]]:
        async with self._lock:
            return [
                (record.operation_id, record.tenant_id, record.retention_expires_at)
                for record in self._run_results.values()
                if record.retention_expires_at <= now and record.operation_id not in self._purged
            ][: max(1, limit)]

    async def purge_keys(self, operation_id: UUID, *, tenant_id: str) -> list[str]:
        async with self._lock:
            return [
                record.storage_key
                for record in self._artifacts.values()
                if record.operation_id == operation_id and record.tenant_id == tenant_id
            ]

    async def purge_operation(self, operation_id: UUID, *, tenant_id: str, now: datetime) -> RetentionPurge:
        async with self._lock:
            result = self._run_results.get(operation_id)
            if result is None or result.tenant_id != tenant_id:
                raise ArtifactNotFoundError("terminal result not found")
            if operation_id in self._purged:
                raise ArtifactConflictError("this operation was already purged")
            doomed = [
                record
                for record in self._artifacts.values()
                if record.operation_id == operation_id and record.tenant_id == tenant_id
            ]
            purge = RetentionPurge(
                operation_id=operation_id,
                tenant_id=tenant_id,
                artifact_count=len(doomed),
                byte_count=sum(record.size_bytes for record in doomed),
                retention_expired_at=result.retention_expires_at,
                purged_at=now,
            )
            for record in doomed:
                del self._artifacts[record.artifact_id]
            for upload_id in [key for key, item in self._uploads.items() if item.operation_id == operation_id]:
                del self._uploads[upload_id]
            for key in [item for item in self._stage_commits if item[0] == operation_id]:
                del self._stage_commits[key]
            for attempt_id in [key for key, item in self._attempts.items() if item.operation_id == operation_id]:
                del self._attempts[attempt_id]
            self._events = [event for event in self._events if event.operation_id != operation_id]
            self._purged.add(operation_id)
            return purge


MAX_STORED_JSON_BYTES = 8 * 1024 * 1024
_SQLSTATE_ERRORS: Mapping[str, type[ArtifactServiceError]] = MappingProxyType(
    {
        "FS201": StaleArtifactAttemptError,
        "FS202": ArtifactConflictError,
        "FS203": ResultAlreadyTerminalError,
    }
)


def _decode_json_object(value: object, *, label: str) -> dict[str, Any]:
    if isinstance(value, str | bytes | bytearray):
        if len(value) > MAX_STORED_JSON_BYTES:
            raise ArtifactConflictError(f"stored {label} exceeds the accepted size")
        decoded = json.loads(value)
    else:
        decoded = value
    if not isinstance(decoded, dict):
        raise ArtifactConflictError(f"stored {label} is not a JSON object")
    return decoded


def _shard_from_storage(value: str) -> str | None:
    return None if value == NO_SHARD else value


def _access_from_row(row: Mapping[str, Any]) -> ArtifactAccess:
    return ArtifactAccess(
        profile=ArtifactAccessProfile(row["access_profile"]),
        receipt_digest=row["access_receipt_digest"],
    )


def _admission_from_row(row: Mapping[str, Any]) -> KueueAdmission | None:
    if row["admitted_at"] is None:
        return None
    return KueueAdmission(
        resolved_pool_id=row["resolved_pool_id"],
        admitted_resource_flavor=row["admitted_resource_flavor"],
        accelerator_resource_name=row["accelerator_resource_name"],
        accelerator_count=row["accelerator_count"],
        admitted_at=row["admitted_at"],
    )


def _attempt_from_row(row: Mapping[str, Any]) -> StageAttemptRecord:
    return StageAttemptRecord(
        attempt_id=row["attempt_id"],
        operation_id=row["operation_id"],
        tenant_id=row["tenant_id"],
        stage_id=row["stage_id"],
        shard_id=_shard_from_storage(row["shard_id"]),
        attempt_number=row["attempt_number"],
        status=AttemptStatus(row["status"]),
        admission=_admission_from_row(row),
        kueue_workload_uid=row["kueue_workload_uid"],
        k8s_job_uid=row["k8s_job_uid"],
        pod_uids=tuple(row["pod_uids"] or ()),
        node_uids=tuple(row["node_uids"] or ()),
        gpu_uuids=tuple(row["gpu_uuids"] or ()),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        retention_expires_at=row["retention_expires_at"],
    )


def _artifact_from_row(row: Mapping[str, Any]) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=row["id"],
        attempt_id=row["attempt_id"],
        operation_id=row["operation_id"],
        tenant_id=row["tenant_id"],
        stage_id=row["stage_id"],
        shard_id=_shard_from_storage(row["shard_id"]),
        direction=ArtifactDirection(row["direction"]),
        digest=row["digest"],
        size_bytes=row["size_bytes"],
        media_type=row["media_type"],
        compression=ArtifactCompression(row["compression"]) if row["compression"] else None,
        storage_key=row["storage_key"],
        access=_access_from_row(row),
        retention_expires_at=row["retention_expires_at"],
        created_at=row["created_at"],
    )


def _upload_from_row(row: Mapping[str, Any]) -> UploadIntent:
    return UploadIntent(
        upload_id=row["id"],
        attempt_id=row["attempt_id"],
        operation_id=row["operation_id"],
        tenant_id=row["tenant_id"],
        stage_id=row["stage_id"],
        shard_id=_shard_from_storage(row["shard_id"]),
        direction=ArtifactDirection(row["direction"]),
        expected_digest=row["expected_digest"],
        expected_size_bytes=row["expected_size_bytes"],
        media_type=row["media_type"],
        compression=ArtifactCompression(row["compression"]) if row["compression"] else None,
        storage_key=row["storage_key"],
        access=_access_from_row(row),
        begun_at=row["begun_at"],
        finalized_at=row["finalized_at"],
        artifact_id=row["artifact_id"],
    )


def _event_from_row(row: Mapping[str, Any]) -> ArtifactEvent:
    return ArtifactEvent(
        event_id=row["id"],
        event_type=ArtifactEventType(row["event_type"]),
        operation_id=row["operation_id"],
        tenant_id=row["tenant_id"],
        stage_id=row["stage_id"],
        attempt_id=row["attempt_id"],
        upload_id=row["upload_id"],
        artifact_id=row["artifact_id"],
        manifest_digest=row["manifest_digest"],
        occurred_at=row["occurred_at"],
    )


# The column lists below are module-level literals interpolated into otherwise
# parameterised statements; every caller-supplied value travels as a bound
# parameter, which is why the S608 suppressions on those queries are safe.
_ATTEMPT_COLUMNS = """attempt_id,operation_id,tenant_id,stage_id,shard_id,attempt_number,status,
    resolved_pool_id,admitted_resource_flavor,accelerator_resource_name,accelerator_count,admitted_at,
    kueue_workload_uid,k8s_job_uid,pod_uids,node_uids,gpu_uuids,started_at,completed_at,
    retention_expires_at"""
_ARTIFACT_COLUMNS = """id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,digest,size_bytes,
    media_type,compression,storage_key,access_profile,access_receipt_digest,retention_expires_at,created_at"""
_UPLOAD_COLUMNS = """id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,expected_digest,
    expected_size_bytes,media_type,compression,storage_key,access_profile,access_receipt_digest,
    artifact_id,begun_at,finalized_at"""


_SELECT_ARTIFACT_SQL = f"""
    SELECT {_ARTIFACT_COLUMNS} FROM fs2_scientific_artifacts WHERE id=$1 AND tenant_id=$2
"""  # noqa: S608


_CLOSE_ATTEMPT_SQL = f"""
    UPDATE fs2_scientific_stage_attempts SET
        status=$4,
        completed_at=$5,
        resolved_pool_id=COALESCE($6,resolved_pool_id),
        admitted_resource_flavor=COALESCE($7,admitted_resource_flavor),
        accelerator_resource_name=COALESCE($8,accelerator_resource_name),
        accelerator_count=COALESCE($9,accelerator_count),
        admitted_at=COALESCE($10,admitted_at),
        kueue_workload_uid=COALESCE($11,kueue_workload_uid),
        k8s_job_uid=COALESCE($12,k8s_job_uid),
        pod_uids=$13,node_uids=$14,gpu_uuids=$15
    WHERE attempt_id=$1 AND operation_id=$2 AND tenant_id=$3 AND status='running'
    RETURNING {_ATTEMPT_COLUMNS}
"""  # noqa: S608


class PostgresArtifactRepository:
    """Durable repository whose fences are enforced by SQL, not by callers."""

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self.pool = pool

    @staticmethod
    def _translate(error: asyncpg.PostgresError) -> ArtifactServiceError | None:
        failure = _SQLSTATE_ERRORS.get(str(getattr(error, "sqlstate", "")))
        return failure("the scientific artifact store rejected this write") if failure else None

    async def _append_event(
        self,
        connection: asyncpg.Connection[Any],
        event_type: ArtifactEventType,
        *,
        operation_id: UUID,
        tenant_id: str,
        stage_id: str | None = None,
        attempt_id: UUID | None = None,
        upload_id: UUID | None = None,
        artifact_id: UUID | None = None,
        manifest_digest: str | None = None,
        occurred_at: datetime,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO fs2_scientific_artifact_events
                (event_type,operation_id,tenant_id,stage_id,attempt_id,upload_id,artifact_id,
                 manifest_digest,occurred_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            event_type.value,
            operation_id,
            tenant_id,
            stage_id,
            attempt_id,
            upload_id,
            artifact_id,
            manifest_digest,
            occurred_at,
        )

    async def open_attempt(self, request: OpenStageAttempt, *, retention: timedelta) -> StageAttemptRecord:
        admission = request.admission
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    INSERT INTO fs2_scientific_stage_attempts
                        (attempt_id,operation_id,tenant_id,stage_id,shard_id,attempt_number,status,
                         resolved_pool_id,admitted_resource_flavor,accelerator_resource_name,
                         accelerator_count,admitted_at,kueue_workload_uid,k8s_job_uid,
                         started_at,retention_expires_at)
                    VALUES($1,$2,$3,$4,$5,$6,'running',$7,$8,$9,$10,$11,$12,$13,$14,$15)
                    ON CONFLICT (attempt_id) DO NOTHING
                    RETURNING {_ATTEMPT_COLUMNS}
                    """,  # noqa: S608
                    request.attempt_id,
                    request.operation_id,
                    request.tenant_id,
                    request.stage_id,
                    request.shard_id or NO_SHARD,
                    request.attempt_number,
                    admission.resolved_pool_id if admission else None,
                    admission.admitted_resource_flavor if admission else None,
                    admission.accelerator_resource_name if admission else None,
                    admission.accelerator_count if admission else 0,
                    admission.admitted_at if admission else None,
                    request.kueue_workload_uid,
                    request.k8s_job_uid,
                    request.started_at,
                    request.started_at + retention,
                )
                if row is not None:
                    await self._append_event(
                        connection,
                        ArtifactEventType.ATTEMPT_OPENED,
                        operation_id=request.operation_id,
                        tenant_id=request.tenant_id,
                        stage_id=request.stage_id,
                        attempt_id=request.attempt_id,
                        occurred_at=request.started_at,
                    )
                    return _attempt_from_row(row)
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("attempt could not be opened")) from None
        existing = await self.get_attempt(request.attempt_id, tenant_id=request.tenant_id)
        if (
            existing.operation_id != request.operation_id
            or existing.stage_id != request.stage_id
            or existing.shard_key != (request.shard_id or NO_SHARD)
            or existing.attempt_number != request.attempt_number
        ):
            raise ArtifactConflictError("attempt identity is already bound to another scope")
        return existing

    async def close_attempt(self, request: CloseStageAttempt) -> StageAttemptRecord:
        admission = request.admission
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(
                    _CLOSE_ATTEMPT_SQL,
                    request.attempt_id,
                    request.operation_id,
                    request.tenant_id,
                    request.status.value,
                    request.completed_at,
                    admission.resolved_pool_id if admission else None,
                    admission.admitted_resource_flavor if admission else None,
                    admission.accelerator_resource_name if admission else None,
                    admission.accelerator_count if admission else None,
                    admission.admitted_at if admission else None,
                    request.kueue_workload_uid,
                    request.k8s_job_uid,
                    list(request.pod_uids),
                    list(request.node_uids),
                    list(request.gpu_uuids),
                )
                if row is not None:
                    await self._append_event(
                        connection,
                        ArtifactEventType.ATTEMPT_CLOSED,
                        operation_id=request.operation_id,
                        tenant_id=request.tenant_id,
                        stage_id=str(row["stage_id"]),
                        attempt_id=request.attempt_id,
                        occurred_at=request.completed_at,
                    )
                    return _attempt_from_row(row)
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("attempt could not be closed")) from None
        existing = await self.get_attempt(request.attempt_id, tenant_id=request.tenant_id)
        if existing.status is not request.status or existing.completed_at != request.completed_at:
            raise ArtifactConflictError("the attempt already recorded a different outcome")
        return existing

    async def get_attempt(self, attempt_id: UUID, *, tenant_id: str) -> StageAttemptRecord:
        row = await self.pool.fetchrow(
            f"SELECT {_ATTEMPT_COLUMNS} FROM fs2_scientific_stage_attempts "  # noqa: S608
            "WHERE attempt_id=$1 AND tenant_id=$2",
            attempt_id,
            tenant_id,
        )
        if row is None:
            raise ArtifactNotFoundError("attempt not found")
        return _attempt_from_row(row)

    async def list_attempts(self, operation_id: UUID, *, tenant_id: str) -> list[StageAttemptRecord]:
        rows = await self.pool.fetch(
            f"SELECT {_ATTEMPT_COLUMNS} FROM fs2_scientific_stage_attempts "  # noqa: S608
            "WHERE operation_id=$1 AND tenant_id=$2 ORDER BY stage_id,shard_id,attempt_number",
            operation_id,
            tenant_id,
        )
        return [_attempt_from_row(row) for row in rows]

    async def begin_upload(
        self, request: BeginArtifactUpload, storage_key: str, *, retention: timedelta
    ) -> UploadIntent:
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                attempt = await connection.fetchrow(
                    "SELECT stage_id,shard_id FROM fs2_scientific_stage_attempts "
                    "WHERE attempt_id=$1 AND operation_id=$2 AND tenant_id=$3",
                    request.attempt_id,
                    request.operation_id,
                    request.tenant_id,
                )
                if attempt is None:
                    raise ArtifactNotFoundError("attempt not found")
                row = await connection.fetchrow(
                    f"""
                    INSERT INTO fs2_scientific_uploads
                        (id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,expected_digest,
                         expected_size_bytes,media_type,compression,storage_key,access_profile,
                         access_receipt_digest,begun_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,clock_timestamp())
                    ON CONFLICT (id) DO NOTHING
                    RETURNING {_UPLOAD_COLUMNS}
                    """,
                    request.upload_id,
                    request.attempt_id,
                    request.operation_id,
                    request.tenant_id,
                    attempt["stage_id"],
                    attempt["shard_id"],
                    request.direction.value,
                    request.expected_digest,
                    request.expected_size_bytes,
                    request.media_type,
                    request.compression.value if request.compression else None,
                    storage_key,
                    request.access.profile.value,
                    request.access.receipt_digest,
                )
                if row is not None:
                    await self._append_event(
                        connection,
                        ArtifactEventType.UPLOAD_BEGUN,
                        operation_id=request.operation_id,
                        tenant_id=request.tenant_id,
                        stage_id=str(attempt["stage_id"]),
                        attempt_id=request.attempt_id,
                        upload_id=request.upload_id,
                        occurred_at=row["begun_at"],
                    )
                    return _upload_from_row(row)
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("upload could not be reserved")) from None
        return await self.get_upload(
            FinalizeArtifactUpload(
                upload_id=request.upload_id,
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
            )
        )

    async def get_upload(self, request: FinalizeArtifactUpload) -> UploadIntent:
        row = await self.pool.fetchrow(
            f"SELECT {_UPLOAD_COLUMNS} FROM fs2_scientific_uploads "  # noqa: S608
            "WHERE id=$1 AND operation_id=$2 AND tenant_id=$3",
            request.upload_id,
            request.operation_id,
            request.tenant_id,
        )
        if row is None:
            raise ArtifactNotFoundError("upload not found")
        return _upload_from_row(row)

    async def finalize_upload(
        self, request: FinalizeArtifactUpload, verified: VerifiedStoredObject, *, artifact_id: UUID
    ) -> ArtifactRecord:
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                intent_row = await connection.fetchrow(
                    f"SELECT {_UPLOAD_COLUMNS} FROM fs2_scientific_uploads "  # noqa: S608
                    "WHERE id=$1 AND operation_id=$2 AND tenant_id=$3 FOR UPDATE",
                    request.upload_id,
                    request.operation_id,
                    request.tenant_id,
                )
                if intent_row is None:
                    raise ArtifactNotFoundError("upload not found")
                intent = _upload_from_row(intent_row)
                if intent.artifact_id is not None:
                    # Read on the locked connection rather than borrowing a
                    # second one from the pool while this row lock is held.
                    existing = await connection.fetchrow(_SELECT_ARTIFACT_SQL, intent.artifact_id, intent.tenant_id)
                    if existing is None:
                        raise ArtifactNotFoundError("artifact not found")
                    return _artifact_from_row(existing)
                _verify_object(intent, verified)
                retention_row = await connection.fetchrow(
                    "SELECT retention_expires_at,started_at FROM fs2_scientific_stage_attempts WHERE attempt_id=$1",
                    intent.attempt_id,
                )
                if retention_row is None:
                    raise ArtifactNotFoundError("attempt not found")
                window = retention_row["retention_expires_at"] - retention_row["started_at"]
                row = await connection.fetchrow(
                    f"""
                    INSERT INTO fs2_scientific_artifacts
                        (id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,digest,size_bytes,
                         media_type,compression,storage_key,access_profile,access_receipt_digest,
                         retention_expires_at,created_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,clock_timestamp()+$15,
                           clock_timestamp())
                    RETURNING {_ARTIFACT_COLUMNS}
                    """,
                    artifact_id,
                    intent.attempt_id,
                    intent.operation_id,
                    intent.tenant_id,
                    intent.stage_id,
                    intent.shard_id or NO_SHARD,
                    intent.direction.value,
                    verified.digest,
                    verified.size_bytes,
                    verified.media_type,
                    verified.compression.value if verified.compression else None,
                    verified.storage_key,
                    intent.access.profile.value,
                    intent.access.receipt_digest,
                    window,
                )
                assert row is not None
                await connection.execute(
                    "UPDATE fs2_scientific_uploads SET artifact_id=$2,finalized_at=clock_timestamp() WHERE id=$1",
                    request.upload_id,
                    artifact_id,
                )
                await self._append_event(
                    connection,
                    ArtifactEventType.ARTIFACT_FINALIZED,
                    operation_id=intent.operation_id,
                    tenant_id=intent.tenant_id,
                    stage_id=intent.stage_id,
                    attempt_id=intent.attempt_id,
                    upload_id=request.upload_id,
                    artifact_id=artifact_id,
                    occurred_at=row["created_at"],
                )
                return _artifact_from_row(row)
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("artifact could not be published")) from None

    async def get_artifact(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactRecord:
        row = await self.pool.fetchrow(
            f"SELECT {_ARTIFACT_COLUMNS} FROM fs2_scientific_artifacts WHERE id=$1 AND tenant_id=$2",  # noqa: S608
            artifact_id,
            tenant_id,
        )
        if row is None:
            raise ArtifactNotFoundError("artifact not found")
        return _artifact_from_row(row)

    async def list_artifacts(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        stage_id: str | None = None,
        attempt_id: UUID | None = None,
    ) -> list[ArtifactRecord]:
        rows = await self.pool.fetch(
            f"""
            SELECT {_ARTIFACT_COLUMNS} FROM fs2_scientific_artifacts
            WHERE operation_id=$1 AND tenant_id=$2
              AND ($3::text IS NULL OR stage_id=$3)
              AND ($4::uuid IS NULL OR attempt_id=$4)
            ORDER BY stage_id,shard_id,attempt_id,id
            """,  # noqa: S608
            operation_id,
            tenant_id,
            stage_id,
            attempt_id,
        )
        return [_artifact_from_row(row) for row in rows]

    async def commit_stage(self, request: CommitStageResult) -> StageCommitRecord:
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                succeeded_rows = await connection.fetch(
                    "SELECT attempt_id FROM fs2_scientific_stage_attempts "
                    "WHERE operation_id=$1 AND tenant_id=$2 AND stage_id=$3 AND status='succeeded' "
                    "FOR SHARE",
                    request.operation_id,
                    request.tenant_id,
                    request.stage_id,
                )
                succeeded = {row["attempt_id"] for row in succeeded_rows}
                if succeeded != set(request.attempt_ids):
                    raise ArtifactConflictError("the commit does not name the stage's succeeded attempts")
                pairs: list[tuple[ManifestEntryDraft, ArtifactRecord]] = []
                for entry in request.entries:
                    row = await connection.fetchrow(
                        f"SELECT {_ARTIFACT_COLUMNS} FROM fs2_scientific_artifacts "  # noqa: S608
                        "WHERE id=$1 AND tenant_id=$2 AND operation_id=$3 AND stage_id=$4",
                        entry.artifact_id,
                        request.tenant_id,
                        request.operation_id,
                        request.stage_id,
                    )
                    if row is None:
                        raise ArtifactNotFoundError("artifact not found")
                    record = _artifact_from_row(row)
                    if record.direction is not ArtifactDirection.OUTPUT:
                        raise ArtifactConflictError("only output artifacts can be committed to a stage manifest")
                    if record.attempt_id not in succeeded:
                        raise StaleArtifactAttemptError("a committed artifact belongs to a non-succeeded attempt")
                    pairs.append((entry, record))
                manifest = build_stage_manifest(
                    operation_id=request.operation_id, stage_id=request.stage_id, entries=pairs
                )
                inserted = await connection.fetchrow(
                    """
                    INSERT INTO fs2_scientific_stage_commits
                        (operation_id,stage_id,tenant_id,manifest_digest,validation_digest,semantic_valid,
                         manifest,committed_at,validated_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9)
                    ON CONFLICT (operation_id,stage_id) DO NOTHING
                    RETURNING manifest_digest
                    """,
                    request.operation_id,
                    request.stage_id,
                    request.tenant_id,
                    manifest.digest,
                    request.validation_digest,
                    request.semantic_valid,
                    json.dumps(manifest.to_document(), sort_keys=True, separators=(",", ":")),
                    request.committed_at,
                    request.validated_at,
                )
                if inserted is None:
                    existing = await self._read_stage_commit(
                        connection, request.operation_id, stage_id=request.stage_id, tenant_id=None
                    )
                    if existing is None or existing.manifest_digest != manifest.digest:
                        raise ArtifactConflictError("this stage already committed a different manifest")
                    return existing
                await connection.executemany(
                    "INSERT INTO fs2_scientific_stage_commit_attempts"
                    "(operation_id,stage_id,attempt_id) VALUES($1,$2,$3)",
                    [
                        (request.operation_id, request.stage_id, attempt_id)
                        for attempt_id in sorted(request.attempt_ids, key=str)
                    ],
                )
                await self._append_event(
                    connection,
                    ArtifactEventType.STAGE_COMMITTED,
                    operation_id=request.operation_id,
                    tenant_id=request.tenant_id,
                    stage_id=request.stage_id,
                    manifest_digest=manifest.digest,
                    occurred_at=request.committed_at,
                )
                return StageCommitRecord(
                    operation_id=request.operation_id,
                    tenant_id=request.tenant_id,
                    stage_id=request.stage_id,
                    attempt_ids=tuple(sorted(request.attempt_ids, key=str)),
                    manifest=manifest,
                    manifest_digest=manifest.digest,
                    validation_digest=request.validation_digest,
                    semantic_valid=request.semantic_valid,
                    committed_at=request.committed_at,
                    validated_at=request.validated_at,
                )
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("stage commit was rejected")) from None

    @staticmethod
    async def _read_stage_commit(
        connection: asyncpg.Connection[Any], operation_id: UUID, *, stage_id: str, tenant_id: str | None
    ) -> StageCommitRecord | None:
        row = await connection.fetchrow(
            """
            SELECT c.operation_id,c.stage_id,c.tenant_id,c.manifest_digest,c.validation_digest,
                   c.semantic_valid,c.manifest,c.committed_at,c.validated_at,
                   COALESCE(array_agg(a.attempt_id ORDER BY a.attempt_id)
                            FILTER (WHERE a.attempt_id IS NOT NULL),'{}') AS attempt_ids
            FROM fs2_scientific_stage_commits c
            LEFT JOIN fs2_scientific_stage_commit_attempts a
                ON a.operation_id=c.operation_id AND a.stage_id=c.stage_id
            WHERE c.operation_id=$1 AND c.stage_id=$2 AND ($3::text IS NULL OR c.tenant_id=$3)
            GROUP BY c.operation_id,c.stage_id,c.tenant_id,c.manifest_digest,c.validation_digest,
                     c.semantic_valid,c.manifest,c.committed_at,c.validated_at
            """,
            operation_id,
            stage_id,
            tenant_id,
        )
        if row is None:
            return None
        manifest = ScientificArtifactManifest.model_validate(
            _decode_json_object(row["manifest"], label="stage manifest")
        )
        return StageCommitRecord(
            operation_id=row["operation_id"],
            tenant_id=row["tenant_id"],
            stage_id=row["stage_id"],
            attempt_ids=tuple(row["attempt_ids"]),
            manifest=manifest,
            manifest_digest=row["manifest_digest"],
            validation_digest=row["validation_digest"],
            semantic_valid=row["semantic_valid"],
            committed_at=row["committed_at"],
            validated_at=row["validated_at"],
        )

    async def stage_commit(
        self, operation_id: UUID, *, stage_id: str, tenant_id: str | None = None
    ) -> StageCommitRecord | None:
        async with self.pool.acquire() as connection:
            return await self._read_stage_commit(connection, operation_id, stage_id=stage_id, tenant_id=tenant_id)

    async def commit_run_result(self, record: RunResultRecord) -> RunResultRecord:
        document = record.result.to_document()
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                inserted = await connection.fetchrow(
                    """
                    INSERT INTO fs2_scientific_run_results
                        (operation_id,tenant_id,result_digest,terminal_status,semantic_validation_status,
                         document,submitted_at,completed_at,committed_at,retention_expires_at)
                    VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10)
                    ON CONFLICT (operation_id) DO NOTHING
                    RETURNING result_digest
                    """,
                    record.operation_id,
                    record.tenant_id,
                    record.result_digest,
                    record.result.terminal_status.value,
                    record.result.semantic_validation.status.value,
                    json.dumps(document, sort_keys=True, separators=(",", ":")),
                    record.result.submitted_at,
                    record.result.completed_at,
                    record.committed_at,
                    record.retention_expires_at,
                )
                if inserted is None:
                    existing = await self.get_run_result(record.operation_id, tenant_id=record.tenant_id)
                    if existing.result_digest != record.result_digest:
                        raise ResultAlreadyTerminalError("the operation already published a terminal result")
                    return existing
                await self._append_event(
                    connection,
                    ArtifactEventType.RESULT_COMMITTED,
                    operation_id=record.operation_id,
                    tenant_id=record.tenant_id,
                    manifest_digest=record.result_digest,
                    occurred_at=record.committed_at,
                )
                return record
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("terminal result was rejected")) from None

    async def get_run_result(self, operation_id: UUID, *, tenant_id: str) -> RunResultRecord:
        row = await self.pool.fetchrow(
            "SELECT operation_id,tenant_id,result_digest,document,committed_at,retention_expires_at "
            "FROM fs2_scientific_run_results WHERE operation_id=$1 AND tenant_id=$2",
            operation_id,
            tenant_id,
        )
        if row is None:
            raise ArtifactNotFoundError("terminal result not found")
        return RunResultRecord(
            operation_id=row["operation_id"],
            tenant_id=row["tenant_id"],
            result=ScientificRunResult.model_validate(_decode_json_object(row["document"], label="terminal result")),
            result_digest=row["result_digest"],
            committed_at=row["committed_at"],
            retention_expires_at=row["retention_expires_at"],
        )

    async def list_events(
        self, operation_id: UUID, *, tenant_id: str, after_id: int = 0, limit: int = 500
    ) -> list[ArtifactEvent]:
        rows = await self.pool.fetch(
            """
            SELECT id,event_type,operation_id,tenant_id,stage_id,attempt_id,upload_id,artifact_id,
                   manifest_digest,occurred_at
            FROM fs2_scientific_artifact_events
            WHERE operation_id=$1 AND tenant_id=$2 AND id>$3
            ORDER BY id
            LIMIT $4
            """,
            operation_id,
            tenant_id,
            max(0, after_id),
            min(max(1, limit), 1000),
        )
        return [_event_from_row(row) for row in rows]

    async def claim_expired(self, *, now: datetime, limit: int) -> list[tuple[UUID, str, datetime]]:
        rows = await self.pool.fetch(
            """
            SELECT r.operation_id,r.tenant_id,r.retention_expires_at
            FROM fs2_scientific_run_results r
            WHERE r.retention_expires_at<=$1
              AND NOT EXISTS (
                  SELECT 1 FROM fs2_scientific_retention_ledger l WHERE l.operation_id=r.operation_id
              )
            ORDER BY r.retention_expires_at
            LIMIT $2
            """,
            now,
            min(max(1, limit), 500),
        )
        return [(row["operation_id"], row["tenant_id"], row["retention_expires_at"]) for row in rows]

    async def purge_keys(self, operation_id: UUID, *, tenant_id: str) -> list[str]:
        rows = await self.pool.fetch(
            "SELECT storage_key FROM fs2_scientific_artifacts WHERE operation_id=$1 AND tenant_id=$2",
            operation_id,
            tenant_id,
        )
        return [str(row["storage_key"]) for row in rows]

    async def purge_operation(self, operation_id: UUID, *, tenant_id: str, now: datetime) -> RetentionPurge:
        """Delete retired rows under the one session flag the triggers accept."""

        try:
            async with self.pool.acquire() as connection, connection.transaction():
                await connection.execute("SET LOCAL fs2.retention_purge = 'on'")
                result = await connection.fetchrow(
                    "SELECT retention_expires_at FROM fs2_scientific_run_results "
                    "WHERE operation_id=$1 AND tenant_id=$2",
                    operation_id,
                    tenant_id,
                )
                if result is None:
                    raise ArtifactNotFoundError("terminal result not found")
                totals = await connection.fetchrow(
                    "SELECT count(*) AS artifacts,COALESCE(sum(size_bytes),0) AS bytes "
                    "FROM fs2_scientific_artifacts WHERE operation_id=$1 AND tenant_id=$2",
                    operation_id,
                    tenant_id,
                )
                assert totals is not None
                # Claim the purge first. The ledger's unique operation identity is
                # the lock, so a concurrent purge conflicts here rather than racing
                # two deletions. The terminal result row itself stays unlockable
                # because the runtime role deliberately has no UPDATE on it.
                claimed = await connection.fetchrow(
                    """
                    INSERT INTO fs2_scientific_retention_ledger
                        (operation_id,tenant_id,purged_at,artifact_count,byte_count,retention_expired_at)
                    VALUES($1,$2,$3,$4,$5,$6)
                    ON CONFLICT (operation_id) DO NOTHING
                    RETURNING operation_id
                    """,
                    operation_id,
                    tenant_id,
                    now,
                    int(totals["artifacts"]),
                    int(totals["bytes"]),
                    result["retention_expires_at"],
                )
                if claimed is None:
                    raise ArtifactConflictError("this operation was already purged")
                for statement in (
                    "DELETE FROM fs2_scientific_artifact_events WHERE operation_id=$1 AND tenant_id=$2",
                    "DELETE FROM fs2_scientific_stage_commit_attempts WHERE operation_id=$1 AND $2::text IS NOT NULL",
                    "DELETE FROM fs2_scientific_stage_commits WHERE operation_id=$1 AND tenant_id=$2",
                    "DELETE FROM fs2_scientific_uploads WHERE operation_id=$1 AND tenant_id=$2",
                    "DELETE FROM fs2_scientific_artifacts WHERE operation_id=$1 AND tenant_id=$2",
                    "DELETE FROM fs2_scientific_stage_attempts WHERE operation_id=$1 AND tenant_id=$2",
                ):
                    await connection.execute(statement, operation_id, tenant_id)
                return RetentionPurge(
                    operation_id=operation_id,
                    tenant_id=tenant_id,
                    artifact_count=int(totals["artifacts"]),
                    byte_count=int(totals["bytes"]),
                    retention_expired_at=result["retention_expires_at"],
                    purged_at=now,
                )
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("retention purge was rejected")) from None
