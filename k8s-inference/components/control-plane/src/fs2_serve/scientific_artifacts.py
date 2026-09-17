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

Bytes reach and leave this service by two exact paths. A presigned handle is
the path for a caller that can reach the object store directly, and remains
the only path for an object above the inline ceiling. The inline path carries
bytes through the gateway itself so an external customer needs nothing but the
public API: an inline write is measured and matched against the immutable
upload intent *before* any object is created, and an inline read is streamed
in bounded chunks so a large result is never buffered.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
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
MULTIPART_PART_BYTES = 128 * 1024 * 1024
MIN_MULTIPART_PART_BYTES = 5 * 1024 * 1024
MULTIPART_FIRST_PART_DESCRIPTION = (
    "multipart-v2 only: lowercase SHA-256 of bytes "
    "[0:min(size_bytes,134217728)); the protocol part size is exactly 134217728 bytes"
)
MAX_SINGLE_PART_BYTES = 5 * 1024 * 1024 * 1024
MAX_MULTIPART_PARTS = 10_000
DEFAULT_TENANT_QUOTA_BYTES = 1 << 40
MAX_TENANT_QUOTA_BYTES = 1 << 40
DEFAULT_TENANT_QUOTA_OBJECTS = 4096
MAX_TENANT_QUOTA_OBJECTS = 1_000_000
MAX_INLINE_CONTENT_BYTES = 256 * 1024 * 1024
DEFAULT_INLINE_CONTENT_BYTES = 16 * 1024 * 1024
MAX_HANDLE_TTL = timedelta(minutes=15)
HANDLE_CLOCK_SKEW = timedelta(minutes=1)
DEFAULT_HANDLE_TTL = timedelta(minutes=10)
DEFAULT_RETENTION = timedelta(days=90)
MAX_RETENTION = timedelta(days=3650)
DEFAULT_UPLOAD_RESERVATION_TTL = timedelta(hours=24)
MAX_UPLOAD_RESERVATION_TTL = timedelta(days=7)
DEFAULT_UPLOAD_COMPLETION_GRACE = timedelta(minutes=15)
MAX_UPLOAD_COMPLETION_GRACE = timedelta(hours=1)
DEFAULT_PROVIDER_STABILITY_GRACE = timedelta(minutes=5)
MAX_PROVIDER_STABILITY_GRACE = timedelta(hours=1)
ARTIFACT_JANITOR_CONCURRENCY: Final = 4
NO_SHARD = "-"
"""Stored sentinel for a gang-scheduled stage that has no shard identity."""

SHA256_PATTERN = r"^sha256:[a-f0-9]{64}$"
EMPTY_VERSION_SET_DIGEST: Final = "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
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


class ArtifactWritesDisabledError(ArtifactServiceError):
    """Forward-compatible application rollback has paused new object writes."""

    code = "artifact_writes_disabled"


class ArtifactQuotaExceededError(ArtifactServiceError):
    """A tenant has no remaining capacity for another immutable reservation."""

    code = "artifact_quota_exceeded"


class ArtifactContentTooLargeError(ArtifactServiceError):
    """The object is larger than the inline gateway path accepts."""

    code = "artifact_content_too_large"


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


class ArtifactQuotaReservationState(StrEnum):
    ACTIVE = "active"
    REMOVING = "removing"
    RELEASED = "released"


class ArtifactQuotaReleaseReason(StrEnum):
    PROVIDER_REMOVED = "provider_removed"


class ArtifactQuotaEventType(StrEnum):
    RESERVED = "reserved"
    RETENTION_EXTENDED = "retention_extended"
    REMOVAL_CLAIMED = "removal_claimed"
    RELEASED = "released"


class ArtifactAccess(ScientificArtifactModel):
    """Non-secret proof that gated bytes were made available lawfully."""

    profile: ArtifactAccessProfile = ArtifactAccessProfile.PUBLIC
    receipt_digest: Sha256Digest | None = None

    @model_validator(mode="after")
    def receipt_matches_profile(self) -> ArtifactAccess:
        if self.profile is ArtifactAccessProfile.PUBLIC and self.receipt_digest is not None:
            raise ValueError("public artifacts cannot carry a gated-access receipt")
        return self

    def to_admission(self) -> AccessAdmission:
        """Project onto the canonical public access-admission shape."""

        if self.profile is ArtifactAccessProfile.PUBLIC:
            return AccessAdmission(profile=AccessProfile.STANDARD, state=AccessState.NOT_REQUIRED)
        profile = AccessProfile.ACADEMIC if self.profile is ArtifactAccessProfile.ACADEMIC else AccessProfile.STANDARD
        return AccessAdmission(
            profile=profile,
            state=AccessState.VERIFIED,
            receipt_digest=(None if self.receipt_digest is None else self.receipt_digest.removeprefix("sha256:")),
        )


class KueueAdmission(ScientificArtifactModel):
    """The accelerator identity Kueue admitted for one stage/shard attempt."""

    resolved_pool_id: Annotated[str, StringConstraints(max_length=128)] | None = None
    admitted_resource_flavor: Annotated[str, StringConstraints(max_length=253)] | None = None
    accelerator_resource_name: Annotated[str, StringConstraints(max_length=317)] | None = None
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
    upload_protocol: Literal["single-put-v1", "multipart-v2"] = "single-put-v1"
    first_part_checksum: Sha256Digest | None = Field(
        default=None,
        description=MULTIPART_FIRST_PART_DESCRIPTION,
    )

    @model_validator(mode="after")
    def multipart_protocol_has_an_exact_first_part(self) -> BeginArtifactUpload:
        if self.upload_protocol == "multipart-v2" and self.first_part_checksum is None:
            raise ValueError("multipart-v2 requires the first part SHA-256")
        if self.upload_protocol == "single-put-v1" and self.first_part_checksum is not None:
            raise ValueError("single-put-v1 derives its only part checksum from the object digest")
        return self


class FinalizeArtifactUpload(ScientificArtifactModel):
    upload_id: UUID
    operation_id: UUID
    tenant_id: TenantId


class VerifiedStoredObject(ScientificArtifactModel):
    """Metadata independently measured by the trusted object-store adapter."""

    storage_key: str = Field(min_length=1, max_length=1024)
    provider_version_id: str = Field(min_length=1, max_length=1024)
    provider_request_id: str = Field(min_length=1, max_length=512)
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
    # Nullable only while the expand-phase bridge resolves retained pre-0031
    # metadata. Byte-serving paths independently select and verify an exact
    # immutable provider version before issuing a handle or stream.
    provider_version_id: str | None = Field(default=None, min_length=1, max_length=1024)
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


class ArtifactQuotaReservation(ScientificArtifactModel):
    """Retained byte/object reservation state for one immutable upload intent."""

    upload_id: UUID
    operation_id: UUID
    attempt_id: UUID
    tenant_id: TenantId
    reserved_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    reserved_objects: Literal[1] = 1
    state: ArtifactQuotaReservationState
    reserved_at: AwareDatetime
    expires_at: AwareDatetime
    latest_upload_capability_expires_at: AwareDatetime | None = None
    upload_completion_grace_seconds: int = Field(default=900, ge=60, le=3600)
    provider_stability_grace_seconds: int = Field(default=300, ge=30, le=3600)
    released_at: AwareDatetime | None = None
    release_reason: ArtifactQuotaReleaseReason | None = None

    @model_validator(mode="after")
    def state_is_complete(self) -> ArtifactQuotaReservation:
        if self.expires_at <= self.reserved_at:
            raise ValueError("artifact quota reservation must have a positive lifetime")
        released = self.state is ArtifactQuotaReservationState.RELEASED
        if released != (self.released_at is not None) or released != (self.release_reason is not None):
            raise ValueError("artifact quota release state is incomplete")
        if self.released_at is not None and self.released_at < self.reserved_at:
            raise ValueError("artifact quota cannot release before it was reserved")
        if (
            self.latest_upload_capability_expires_at is not None
            and self.latest_upload_capability_expires_at < self.reserved_at
        ):
            raise ValueError("upload capability expiry cannot precede the reservation")
        return self


class ArtifactQuotaEvent(ScientificArtifactModel):
    """Append-only byte/object reservation evidence without object payloads."""

    event_id: int = Field(ge=1)
    upload_id: UUID
    operation_id: UUID
    attempt_id: UUID
    tenant_id: TenantId
    event_type: ArtifactQuotaEventType
    reserved_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    reserved_objects: Literal[1] = 1
    expires_at: AwareDatetime
    release_reason: ArtifactQuotaReleaseReason | None = None
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def release_reason_matches_event(self) -> ArtifactQuotaEvent:
        released = self.event_type is ArtifactQuotaEventType.RELEASED
        if released != (self.release_reason is not None):
            raise ValueError("artifact quota event release reason is inconsistent")
        return self


class ArtifactRemovalEvidenceKind(StrEnum):
    """Provider result that can authorize quota release."""

    ABSENCE_CONFIRMED = "absence_confirmed"
    ALL_VERSIONS_REMOVED = "all_versions_removed"


class ArtifactDeletionEvidence(ScientificArtifactModel):
    """Provider-bound result of one exact-key remover attempt."""

    storage_key: str = Field(min_length=1, max_length=1024)
    kind: ArtifactRemovalEvidenceKind
    provider_request_id: str = Field(min_length=1, max_length=512)
    removed_version_count: int = Field(ge=0)
    aborted_upload_count: int = Field(ge=0)
    multipart_list_request_id: str = Field(min_length=1, max_length=512)
    multipart_session_set_digest: Sha256Digest
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def count_matches_kind(self) -> ArtifactDeletionEvidence:
        if self.kind is ArtifactRemovalEvidenceKind.ABSENCE_CONFIRMED and self.removed_version_count != 0:
            raise ValueError("absence evidence cannot report removed versions")
        if self.kind is ArtifactRemovalEvidenceKind.ALL_VERSIONS_REMOVED and self.removed_version_count < 1:
            raise ValueError("version-removal evidence requires at least one removed version")
        return self


class ArtifactRemovalEvidence(ScientificArtifactModel):
    """Stable double-snapshot absence proof bound to one database claim."""

    storage_key: str = Field(min_length=1, max_length=1024)
    kind: Literal[ArtifactRemovalEvidenceKind.ABSENCE_CONFIRMED]
    provider_request_id: str = Field(min_length=1, max_length=512)
    removed_version_count: Literal[0] = 0
    observed_at: AwareDatetime
    latest_upload_capability_expires_at: AwareDatetime
    removal_generation: int = Field(ge=1)
    verification_generation: int = Field(ge=1)
    first_list_request_id: str = Field(min_length=1, max_length=512)
    head_request_id: str = Field(min_length=1, max_length=512)
    second_list_request_id: str = Field(min_length=1, max_length=512)
    first_version_set_digest: Sha256Digest
    second_version_set_digest: Sha256Digest
    first_multipart_list_request_id: str = Field(min_length=1, max_length=512)
    second_multipart_list_request_id: str = Field(min_length=1, max_length=512)
    first_multipart_session_set_digest: Sha256Digest
    second_multipart_session_set_digest: Sha256Digest
    claim_digest: Sha256Digest

    @model_validator(mode="after")
    def snapshots_are_stable(self) -> ArtifactRemovalEvidence:
        if self.first_version_set_digest != self.second_version_set_digest:
            raise ValueError("provider version snapshots are not stable")
        if self.first_version_set_digest != EMPTY_VERSION_SET_DIGEST:
            raise ValueError("absence evidence must contain the exact empty version set")
        if self.first_multipart_session_set_digest != self.second_multipart_session_set_digest:
            raise ValueError("provider multipart-session snapshots are not stable")
        if self.first_multipart_session_set_digest != EMPTY_VERSION_SET_DIGEST:
            raise ValueError("absence evidence must contain the exact empty multipart-session set")
        if self.observed_at < self.latest_upload_capability_expires_at:
            raise ValueError("absence cannot predate the latest upload capability")
        return self


class ArtifactRemovalTarget(ScientificArtifactModel):
    """One quota-counted key durably fenced for provider removal."""

    upload_id: UUID
    operation_id: UUID
    attempt_id: UUID
    tenant_id: TenantId
    storage_key: str = Field(min_length=1, max_length=1024)
    provider_upload_id: str | None = Field(default=None, min_length=1, max_length=1024)
    upload_session_generation: int = Field(default=0, ge=0, le=1_000_000)
    latest_upload_capability_expires_at: AwareDatetime
    removal_generation: int = Field(ge=1)
    verification_generation: int = Field(ge=0)
    removal_claimed_at: AwareDatetime
    verification_claimed_at: AwareDatetime | None = None
    eligible_at: AwareDatetime

    @model_validator(mode="after")
    def claim_is_time_ordered(self) -> ArtifactRemovalTarget:
        if self.removal_claimed_at < self.eligible_at:
            raise ValueError("artifact removal claim predates its write fence")
        if self.verification_generation == 0 and self.verification_claimed_at is not None:
            raise ValueError("unverified removal target cannot carry a verification claim")
        if self.verification_generation > 0 and self.verification_claimed_at is None:
            raise ValueError("verification claim timestamp is required")
        if (
            self.verification_claimed_at is not None
            and self.verification_claimed_at < self.removal_claimed_at
        ):
            raise ValueError("verification claim cannot predate removal")
        return self


class LegacyArtifactVersionTarget(ScientificArtifactModel):
    """Retained pre-0031 artifact that needs one immutable provider version pin."""

    artifact_id: UUID
    upload_id: UUID
    tenant_id: TenantId
    storage_key: str = Field(min_length=1, max_length=1024)
    expected_digest: Sha256Digest
    expected_size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    expected_media_type: MediaType
    expected_compression: ArtifactCompression | None = None
    claim_generation: int = Field(ge=1, le=1_000_000)
    claimed_at: AwareDatetime
    eligible_at: AwareDatetime
    list_key_marker: str | None = Field(default=None, min_length=1, max_length=1024)
    list_version_id_marker: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def provider_cursor_is_complete(self) -> LegacyArtifactVersionTarget:
        if (self.list_key_marker is None) != (self.list_version_id_marker is None):
            raise ValueError("legacy version cursor markers must travel together")
        return self


class LegacyArtifactVersionScan(ScientificArtifactModel):
    """One bounded, provider-observed page in a retained exact-key inventory."""

    provider_request_id: str = Field(min_length=1, max_length=512)
    observed_at: AwareDatetime
    verified: VerifiedStoredObject | None = None
    next_key_marker: str | None = Field(default=None, min_length=1, max_length=1024)
    next_version_id_marker: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def result_or_cursor_is_unambiguous(self) -> LegacyArtifactVersionScan:
        if (self.next_key_marker is None) != (self.next_version_id_marker is None):
            raise ValueError("legacy version next-page markers must travel together")
        if self.verified is not None and self.next_key_marker is not None:
            raise ValueError("a matched immutable version cannot also request another page")
        return self


class LegacyArtifactRolloutStatus(ScientificArtifactModel):
    """Aggregate-only expansion gate; no tenant or object identity is exposed."""

    pending: int = Field(ge=0)
    bound: int = Field(ge=0)
    unresolved: int = Field(ge=0)
    unbound_artifacts: int = Field(ge=0)
    missing_unfinished_upload_sessions: int = Field(ge=0)


class SchemaBridgeDrainEvidence(ScientificArtifactModel):
    """Kubernetes API evidence that the exact predecessor no longer serves."""

    deployment_namespace: str = Field(min_length=1, max_length=63)
    deployment_name: str = Field(min_length=1, max_length=253)
    deployment_uid: str = Field(min_length=1, max_length=128)
    deployment_generation: int = Field(ge=1)
    deployment_observed_generation: int = Field(ge=1)
    deployment_desired_replicas: int = Field(ge=1)
    deployment_updated_replicas: int = Field(ge=1)
    deployment_ready_replicas: int = Field(ge=1)
    deployment_available_replicas: int = Field(ge=1)
    runtime_pod_count: int = Field(ge=1)
    runtime_pod_set_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    kubernetes_audit_id: str = Field(min_length=1, max_length=200)
    kubernetes_observed_at: AwareDatetime

    @model_validator(mode="after")
    def exact_rollout_is_complete(self) -> SchemaBridgeDrainEvidence:
        desired = self.deployment_desired_replicas
        if self.deployment_observed_generation != self.deployment_generation:
            raise ValueError("deployment generation is not observed")
        if any(
            count != desired
            for count in (
                self.deployment_updated_replicas,
                self.deployment_ready_replicas,
                self.deployment_available_replicas,
                self.runtime_pod_count,
            )
        ):
            raise ValueError("deployment rollout is incomplete")
        return self


def artifact_absence_claim_digest(
    target: ArtifactRemovalTarget,
    *,
    observed_at: datetime,
    first_list_request_id: str,
    head_request_id: str,
    second_list_request_id: str,
    first_version_set_digest: str,
    second_version_set_digest: str,
    first_multipart_list_request_id: str,
    second_multipart_list_request_id: str,
    first_multipart_session_set_digest: str,
    second_multipart_session_set_digest: str,
) -> str:
    """Bind provider observations to the exact claimed fencing generations."""

    payload = {
        "attempt_id": str(target.attempt_id),
        "first_list_request_id": first_list_request_id,
        "first_version_set_digest": first_version_set_digest,
        "first_multipart_list_request_id": first_multipart_list_request_id,
        "first_multipart_session_set_digest": first_multipart_session_set_digest,
        "head_request_id": head_request_id,
        "latest_upload_capability_expires_at": target.latest_upload_capability_expires_at.isoformat(),
        "observed_at": observed_at.isoformat(),
        "operation_id": str(target.operation_id),
        "removal_generation": target.removal_generation,
        "provider_upload_id": target.provider_upload_id,
        "second_list_request_id": second_list_request_id,
        "second_version_set_digest": second_version_set_digest,
        "second_multipart_list_request_id": second_multipart_list_request_id,
        "second_multipart_session_set_digest": second_multipart_session_set_digest,
        "storage_key": target.storage_key,
        "tenant_id": target.tenant_id,
        "upload_id": str(target.upload_id),
        "upload_session_generation": target.upload_session_generation,
        "verification_generation": target.verification_generation,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


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


class ArtifactUploadSession(ScientificArtifactModel):
    """Server-owned multipart generation; clients can upload parts but cannot complete it."""

    upload_id: UUID
    tenant_id: TenantId
    storage_key: str = Field(min_length=1, max_length=1024)
    provider_upload_id: str = Field(min_length=1, max_length=1024)
    session_generation: int = Field(ge=1, le=1_000_000)
    part_size_bytes: int = Field(ge=5 * 1024 * 1024, le=5 * 1024 * 1024 * 1024)
    part_count: int = Field(ge=1, le=MAX_MULTIPART_PARTS)
    provider_stability_grace_seconds: int = Field(default=300, ge=30, le=3600)
    initiated_at: AwareDatetime
    state: Literal["active", "legacy", "completed", "aborted"] = "active"
    provider_version_id: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def completion_version_matches_state(self) -> ArtifactUploadSession:
        if (self.state == "completed") != (self.provider_version_id is not None):
            raise ValueError("completed upload session requires exactly one provider version")
        return self


class ArtifactUploadSessionCreationClaim(ScientificArtifactModel):
    """Durable ownership installed before any provider session is created."""

    upload_id: UUID
    tenant_id: TenantId
    storage_key: str = Field(min_length=1, max_length=1024)
    claim_id: UUID
    claim_generation: int = Field(ge=1, le=1_000_000)
    part_size_bytes: int = Field(ge=5 * 1024 * 1024, le=5 * 1024 * 1024 * 1024)
    part_count: int = Field(ge=1, le=MAX_MULTIPART_PARTS)
    provider_stability_grace_seconds: int = Field(default=300, ge=30, le=3600)
    state: Literal["creating", "bound", "reconciling", "reconciled"]
    claimed_at: AwareDatetime
    reconcile_after: AwareDatetime
    provider_upload_id: str | None = Field(default=None, min_length=1, max_length=1024)


class ArtifactUploadSessionCreationTarget(ScientificArtifactModel):
    """Remover-owned claim for bounded reconciliation of a crashed create."""

    upload_id: UUID
    tenant_id: TenantId
    storage_key: str = Field(min_length=1, max_length=1024)
    claim_id: UUID
    claim_generation: int = Field(ge=1, le=1_000_000)
    claimed_at: AwareDatetime


class ArtifactUploadSessionReconciliationEvidence(ScientificArtifactModel):
    """Provider-bound proof that an abandoned exact-key session set is empty."""

    storage_key: str = Field(min_length=1, max_length=1024)
    provider_request_id: str = Field(min_length=1, max_length=512)
    aborted_upload_count: int = Field(ge=0, le=MAX_MULTIPART_PARTS)
    multipart_session_set_digest: Sha256Digest
    observed_at: AwareDatetime


class ArtifactFinalizationLease(ScientificArtifactModel):
    """Bounded provider-mutation fence retained for autonomous reconciliation."""

    lease_id: UUID
    upload_id: UUID
    tenant_id: TenantId
    lease_generation: int = Field(ge=1, le=1_000_000)
    session_generation: int = Field(ge=1, le=1_000_000)
    acquired_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def expiry_follows_acquisition(self) -> ArtifactFinalizationLease:
        if self.expires_at <= self.acquired_at:
            raise ValueError("artifact finalization lease must have a positive lifetime")
        return self


class ArtifactFinalizationRecoveryTarget(ScientificArtifactModel):
    """Renewed controller-owned lease for autonomous completion recovery."""

    request: FinalizeArtifactUpload
    session: ArtifactUploadSession
    lease: ArtifactFinalizationLease


class ArtifactFinalizationFailureEvidence(ScientificArtifactModel):
    """Retained exact-version evidence for a completed but invalid upload."""

    upload_id: UUID
    tenant_id: TenantId
    lease_id: UUID
    lease_generation: int = Field(ge=1, le=1_000_000)
    session_generation: int = Field(ge=1, le=1_000_000)
    provider_upload_id: str = Field(min_length=1, max_length=1024)
    provider_version_id: str = Field(min_length=1, max_length=1024)
    provider_request_id: str = Field(min_length=1, max_length=512)
    failure_code: Literal["content_verification_failed", "artifact_policy_failed"]
    observed_at: AwareDatetime


class StagedUploadPart(ScientificArtifactModel):
    """Server-uploaded bytes still confined to an uncompleted multipart session."""

    storage_key: str = Field(min_length=1, max_length=1024)
    digest: Sha256Digest
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    media_type: MediaType
    compression: ArtifactCompression | None = None
    provider_request_id: str = Field(min_length=1, max_length=512)


class AuthorizeArtifactUploadPart(ScientificArtifactModel):
    """One checksum- and length-bound part capability for an exact session."""

    upload_id: UUID
    operation_id: UUID
    tenant_id: TenantId
    session_generation: int = Field(ge=1, le=1_000_000)
    part_number: int = Field(ge=1, le=MAX_MULTIPART_PARTS)
    size_bytes: int = Field(ge=0, le=5 * 1024 * 1024 * 1024)
    checksum: Sha256Digest


@dataclass(frozen=True, slots=True)
class BeginUploadResult:
    upload: UploadIntent
    session: ArtifactUploadSession
    handle: EphemeralHandle = field(repr=False)


@dataclass(frozen=True, slots=True)
class UploadPartHandleResult:
    session: ArtifactUploadSession
    part_number: int
    handle: EphemeralHandle = field(repr=False)


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    artifact: ArtifactRecord
    handle: EphemeralHandle = field(repr=False)


@dataclass(frozen=True, slots=True)
class InlineUploadReceipt:
    """Evidence that the stored object equals the immutable upload intent."""

    upload: UploadIntent
    stored: StagedUploadPart


@dataclass(frozen=True, slots=True)
class ArtifactContentStream:
    """Verified metadata plus the exact bytes it addresses, never buffered."""

    artifact: ArtifactRecord
    chunks: AsyncIterator[bytes] = field(repr=False)


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
    expected_write_once: bool | None = None,
) -> None:
    """Reject any handle that is long-lived, reusable, or not bearer-safe.

    The adapter stamps the deadline from the same wall clock the gateway checks,
    so the bound below allows one minute of skew against the service clock.
    """

    deadline = now + ttl + HANDLE_CLOCK_SKEW
    parsed = urlsplit(handle.url)
    allowed_schemes = ("https",) if require_tls else ("https", "http")
    write_once = method == "PUT" if expected_write_once is None else expected_write_once
    if (
        handle.method != method
        or handle.write_once != write_once
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

    async def create_upload_session(
        self,
        *,
        storage_key: str,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> tuple[str, datetime]: ...

    async def presign_upload_part(
        self,
        *,
        session: ArtifactUploadSession,
        part_number: int,
        size_bytes: int,
        checksum: str,
        ttl: timedelta,
    ) -> EphemeralHandle: ...

    async def presign_legacy_upload(
        self, *, intent: UploadIntent, ttl: timedelta
    ) -> EphemeralHandle: ...

    async def complete_upload_session(
        self, *, session: ArtifactUploadSession, intent: UploadIntent
    ) -> VerifiedStoredObject: ...

    async def validate_upload_session(
        self, *, session: ArtifactUploadSession, intent: UploadIntent
    ) -> None: ...

    async def stage_inline_upload(
        self, *, session: ArtifactUploadSession, intent: UploadIntent, payload: bytes
    ) -> StagedUploadPart: ...

    async def recover_completed_upload(self, *, intent: UploadIntent) -> VerifiedStoredObject: ...

    async def recover_legacy_upload(self, *, intent: UploadIntent) -> VerifiedStoredObject: ...

    async def recover_legacy_artifact(
        self, *, artifact: ArtifactRecord
    ) -> VerifiedStoredObject: ...

    async def abort_upload_session(self, *, session: ArtifactUploadSession) -> str: ...

    async def reconcile_orphan_upload_sessions(
        self, target: ArtifactUploadSessionCreationTarget
    ) -> ArtifactUploadSessionReconciliationEvidence: ...

    async def presign_download(
        self, *, storage_key: str, provider_version_id: str, ttl: timedelta
    ) -> EphemeralHandle: ...

    async def put_object(
        self,
        *,
        storage_key: str,
        payload: bytes,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> VerifiedStoredObject: ...

    async def put_legacy_object(
        self,
        *,
        storage_key: str,
        payload: bytes,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> VerifiedStoredObject: ...

    def stream_object(
        self, storage_key: str, *, provider_version_id: str, max_bytes: int | None = None
    ) -> AsyncIterator[bytes]: ...

    async def inspect(
        self, storage_key: str, *, provider_version_id: str, max_bytes: int | None = None
    ) -> VerifiedStoredObject: ...

    async def delete(self, target: ArtifactRemovalTarget) -> ArtifactDeletionEvidence: ...

    async def verify_absent(self, target: ArtifactRemovalTarget) -> ArtifactRemovalEvidence: ...

    async def scan_legacy_version(self, target: LegacyArtifactVersionTarget) -> LegacyArtifactVersionScan: ...


class ArtifactRepository(Protocol):
    """Durable persistence bound to an already-admitted Operation identity."""

    async def open_attempt(self, request: OpenStageAttempt, *, retention: timedelta) -> StageAttemptRecord: ...

    async def close_attempt(self, request: CloseStageAttempt) -> StageAttemptRecord: ...

    async def get_attempt(self, attempt_id: UUID, *, tenant_id: str) -> StageAttemptRecord: ...

    async def list_attempts(self, operation_id: UUID, *, tenant_id: str) -> list[StageAttemptRecord]: ...

    async def begin_upload(
        self,
        request: BeginArtifactUpload,
        storage_key: str,
        *,
        retention: timedelta,
        tenant_quota_bytes: int,
        tenant_quota_objects: int,
        reservation_ttl: timedelta,
        upload_completion_grace: timedelta,
        provider_stability_grace: timedelta,
    ) -> UploadIntent: ...

    async def claim_upload_session_creation(
        self,
        upload_id: UUID,
        *,
        tenant_id: str,
        storage_key: str,
        claim_id: UUID,
        part_size_bytes: int,
        part_count: int,
    ) -> ArtifactUploadSessionCreationClaim: ...

    async def bind_upload_session(
        self,
        upload_id: UUID,
        *,
        tenant_id: str,
        storage_key: str,
        claim_id: UUID,
        provider_upload_id: str,
        part_size_bytes: int,
        part_count: int,
        initiated_at: datetime,
    ) -> ArtifactUploadSession: ...

    async def claim_stale_upload_session_creations(
        self, *, limit: int
    ) -> list[ArtifactUploadSessionCreationTarget]: ...

    async def record_upload_session_creation_reconciled(
        self,
        target: ArtifactUploadSessionCreationTarget,
        evidence: ArtifactUploadSessionReconciliationEvidence,
    ) -> None: ...

    async def get_upload_session(
        self, upload_id: UUID, *, tenant_id: str
    ) -> ArtifactUploadSession: ...

    async def record_upload_session_aborted(
        self, session: ArtifactUploadSession, *, provider_request_id: str, observed_at: datetime
    ) -> ArtifactUploadSession: ...

    async def acquire_finalization_lease(
        self,
        request: FinalizeArtifactUpload,
        *,
        session_generation: int,
        lease_id: UUID,
    ) -> ArtifactFinalizationLease: ...

    async def get_finalization_lease(
        self,
        request: FinalizeArtifactUpload,
        *,
        session_generation: int,
    ) -> ArtifactFinalizationLease | None: ...

    async def claim_expired_finalization_leases(
        self, *, limit: int
    ) -> list[ArtifactFinalizationRecoveryTarget]: ...

    async def record_finalization_failure(
        self,
        request: FinalizeArtifactUpload,
        *,
        session: ArtifactUploadSession,
        lease: ArtifactFinalizationLease,
        verified: VerifiedStoredObject,
        failure_code: Literal["content_verification_failed", "artifact_policy_failed"],
    ) -> ArtifactFinalizationFailureEvidence: ...

    async def record_upload_capability(
        self,
        upload_id: UUID,
        *,
        tenant_id: str,
        capability_id: UUID,
        session_generation: int,
        part_number: int,
        size_bytes: int,
        checksum: str,
        media_type: str,
        compression: ArtifactCompression | None,
        expires_at: datetime,
    ) -> ArtifactQuotaReservation: ...

    async def quota_reservation(self, upload_id: UUID, *, tenant_id: str) -> ArtifactQuotaReservation: ...

    async def list_quota_events(
        self, *, tenant_id: str, after_id: int = 0, limit: int = 500
    ) -> list[ArtifactQuotaEvent]: ...

    async def claim_expired_quota_removals(
        self, *, now: datetime, limit: int
    ) -> list[ArtifactRemovalTarget]: ...

    async def claim_quota_verifications(
        self, *, now: datetime, limit: int
    ) -> list[ArtifactRemovalTarget]: ...

    async def record_quota_removal_completion(
        self, target: ArtifactRemovalTarget, evidence: ArtifactDeletionEvidence
    ) -> None: ...

    async def record_quota_verification_failure(self, target: ArtifactRemovalTarget) -> None: ...

    async def record_quota_removal(
        self, target: ArtifactRemovalTarget, evidence: ArtifactRemovalEvidence
    ) -> ArtifactQuotaReservation: ...

    async def claim_legacy_version_pins(self, *, limit: int) -> list[LegacyArtifactVersionTarget]: ...

    async def record_legacy_version_pin(
        self,
        target: LegacyArtifactVersionTarget,
        verified: VerifiedStoredObject,
        *,
        observed_at: datetime,
    ) -> None: ...

    async def record_legacy_version_scan(
        self, target: LegacyArtifactVersionTarget, scan: LegacyArtifactVersionScan
    ) -> None: ...

    async def legacy_version_rollout_status(self) -> LegacyArtifactRolloutStatus: ...

    async def mark_schema_bridge_ready(
        self,
        *,
        bridge_image_ref: str,
        bridge_release_revision: int,
        predecessor_image_ref: str,
        evidence: SchemaBridgeDrainEvidence,
    ) -> None: ...

    async def get_upload(self, request: FinalizeArtifactUpload) -> UploadIntent: ...

    async def get_upload_status(self, request: FinalizeArtifactUpload) -> UploadIntent: ...

    async def get_leased_upload(
        self, request: FinalizeArtifactUpload, *, lease: ArtifactFinalizationLease
    ) -> UploadIntent: ...

    async def finalize_upload(
        self,
        request: FinalizeArtifactUpload,
        verified: VerifiedStoredObject,
        *,
        artifact_id: UUID,
        session: ArtifactUploadSession | None,
        lease: ArtifactFinalizationLease,
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

    async def purge_keys(self, operation_id: UUID, *, tenant_id: str) -> list[ArtifactRemovalTarget]: ...


class ScientificArtifactControllerPort(Protocol):
    """Stable port consumed by the scientific batch controller and routes."""

    async def open_attempt(self, request: OpenStageAttempt) -> StageAttemptRecord: ...

    async def close_attempt(self, request: CloseStageAttempt) -> StageAttemptRecord: ...

    async def begin_upload(
        self, request: BeginArtifactUpload, *, handle_ttl: timedelta | None = None
    ) -> BeginUploadResult: ...

    async def authorize_upload_part(
        self, request: AuthorizeArtifactUploadPart, *, handle_ttl: timedelta | None = None
    ) -> UploadPartHandleResult: ...

    async def store_upload_content(
        self,
        request: FinalizeArtifactUpload,
        *,
        content: bytes,
        declared_media_type: str | None = None,
        declared_size_bytes: int | None = None,
    ) -> InlineUploadReceipt: ...

    async def store_trusted_upload_content(
        self,
        request: FinalizeArtifactUpload,
        *,
        content: bytes,
    ) -> InlineUploadReceipt: ...

    async def finalize_upload(self, request: FinalizeArtifactUpload) -> ArtifactRecord: ...

    async def download(
        self, artifact_id: UUID, *, tenant_id: str, handle_ttl: timedelta | None = None
    ) -> ArtifactDownload: ...

    async def open_content(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactContentStream: ...

    @property
    def max_inline_content_bytes(self) -> int: ...

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
        tenant_quota_bytes: int = DEFAULT_TENANT_QUOTA_BYTES,
        tenant_quota_objects: int = DEFAULT_TENANT_QUOTA_OBJECTS,
        upload_reservation_ttl: timedelta = DEFAULT_UPLOAD_RESERVATION_TTL,
        upload_completion_grace: timedelta = DEFAULT_UPLOAD_COMPLETION_GRACE,
        provider_stability_grace: timedelta = DEFAULT_PROVIDER_STABILITY_GRACE,
        multipart_writes_enabled: bool = True,
        writes_enabled: bool | None = None,
        max_inline_content_bytes: int = DEFAULT_INLINE_CONTENT_BYTES,
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
        if not max_artifact_bytes <= tenant_quota_bytes <= MAX_TENANT_QUOTA_BYTES:
            raise ValueError("the tenant artifact quota must cover one maximum-size artifact")
        if not 1 <= tenant_quota_objects <= MAX_TENANT_QUOTA_OBJECTS:
            raise ValueError("the tenant artifact object quota is outside the supported range")
        if not 0 < max_inline_content_bytes <= MAX_INLINE_CONTENT_BYTES:
            raise ValueError("the inline content ceiling is outside the supported range")
        if not timedelta(0) < max_handle_ttl <= MAX_HANDLE_TTL:
            raise ValueError("handle lifetime must be positive and at most fifteen minutes")
        if not timedelta(0) < default_handle_ttl <= max_handle_ttl:
            raise ValueError("the default handle lifetime must not exceed the maximum")
        if not timedelta(0) < retention <= MAX_RETENTION:
            raise ValueError("artifact retention is outside the supported range")
        if not timedelta(0) < upload_completion_grace <= MAX_UPLOAD_COMPLETION_GRACE:
            raise ValueError("upload completion grace is outside the supported range")
        if not timedelta(0) < provider_stability_grace <= MAX_PROVIDER_STABILITY_GRACE:
            raise ValueError("provider stability grace is outside the supported range")
        if not max_handle_ttl <= upload_reservation_ttl <= min(
            MAX_UPLOAD_RESERVATION_TTL, retention
        ):
            raise ValueError("upload reservation lifetime is outside the supported range")
        self._repository = repository
        self._store = object_store
        self._allowed_media_types = allowed
        self._max_artifact_bytes = max_artifact_bytes
        self._tenant_quota_bytes = tenant_quota_bytes
        self._tenant_quota_objects = tenant_quota_objects
        self._upload_reservation_ttl = upload_reservation_ttl
        self._upload_completion_grace = upload_completion_grace
        self._provider_stability_grace = provider_stability_grace
        # ``writes_enabled`` is retained as a source-compatible constructor
        # alias for older embeddings.  It now controls only admission of a new
        # multipart-v2 generation; disabling that rollout gate must never stop
        # single-put-v1, inline/trusted result publication, or continuation of
        # an upload session that was already durably admitted.
        self._multipart_writes_enabled = (
            multipart_writes_enabled if writes_enabled is None else writes_enabled
        )
        self._max_inline_content_bytes = min(max_inline_content_bytes, max_artifact_bytes)
        self._max_handle_ttl = max_handle_ttl
        self._default_handle_ttl = default_handle_ttl
        self._retention = retention
        self._require_tls = require_tls_handles
        self._clock = clock

    @property
    def retention(self) -> timedelta:
        return self._retention

    @property
    def max_inline_content_bytes(self) -> int:
        """The exact ceiling the inline gateway byte path advertises."""

        return self._max_inline_content_bytes

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
        """Reserve one content address and open a server-completed upload session."""

        if request.upload_protocol == "multipart-v2" and not self._multipart_writes_enabled:
            try:
                existing = await self._repository.get_upload_status(
                    FinalizeArtifactUpload(
                        upload_id=request.upload_id,
                        operation_id=request.operation_id,
                        tenant_id=request.tenant_id,
                    )
                )
            except ArtifactNotFoundError:
                raise ArtifactWritesDisabledError(
                    "new multipart-v2 artifact sessions are temporarily paused"
                ) from None
            if existing.artifact_id is not None:
                raise ArtifactConflictError("a finalized upload cannot issue new write capabilities")
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
        intent = await self._repository.begin_upload(
            request,
            storage_key,
            retention=self._retention,
            tenant_quota_bytes=self._tenant_quota_bytes,
            tenant_quota_objects=self._tenant_quota_objects,
            reservation_ttl=self._upload_reservation_ttl,
            upload_completion_grace=self._upload_completion_grace,
            provider_stability_grace=self._provider_stability_grace,
        )
        if not _same_upload_request(intent, request, storage_key):
            raise ArtifactConflictError("upload identity is already bound to different content")
        if intent.artifact_id is not None:
            raise ArtifactConflictError("a finalized upload cannot issue new write capabilities")
        if request.upload_protocol == "single-put-v1":
            if request.expected_size_bytes > MAX_SINGLE_PART_BYTES:
                raise ArtifactContentTooLargeError(
                    "single-put-v1 accepts at most 5 GiB; use multipart-v2 for larger artifacts"
                )
            part_size_bytes = max(MIN_MULTIPART_PART_BYTES, request.expected_size_bytes)
            part_count = 1
            first_part_checksum = request.expected_digest
        else:
            part_size_bytes = MULTIPART_PART_BYTES
            part_count = max(
                1,
                (request.expected_size_bytes + part_size_bytes - 1) // part_size_bytes,
            )
            assert request.first_part_checksum is not None
            first_part_checksum = request.first_part_checksum
        if part_count > MAX_MULTIPART_PARTS:
            raise ArtifactPolicyError("artifact requires too many bounded upload parts")
        try:
            session = await self._repository.get_upload_session(intent.upload_id, tenant_id=intent.tenant_id)
        except ArtifactNotFoundError:
            claim_id = uuid4()
            claim = await self._repository.claim_upload_session_creation(
                intent.upload_id,
                tenant_id=intent.tenant_id,
                storage_key=storage_key,
                claim_id=claim_id,
                part_size_bytes=part_size_bytes,
                part_count=part_count,
            )
            if claim.claim_id != claim_id or claim.state != "creating":
                raise ArtifactConflictError("upload-session creation is owned or being reconciled")
            provider_upload_id, initiated_at = await self._store.create_upload_session(
                storage_key=storage_key,
                media_type=request.media_type,
                compression=request.compression,
            )
            session = await self._repository.bind_upload_session(
                intent.upload_id,
                tenant_id=intent.tenant_id,
                storage_key=storage_key,
                claim_id=claim_id,
                provider_upload_id=provider_upload_id,
                part_size_bytes=part_size_bytes,
                part_count=part_count,
                initiated_at=initiated_at,
            )
        if session.state == "legacy":
            if request.upload_protocol != "single-put-v1":
                raise ArtifactConflictError("legacy upload is bound to single-put-v1")
            handle = await self._store.presign_legacy_upload(intent=intent, ttl=lifetime)
            _validate_handle(
                handle,
                method="PUT",
                now=self._clock(),
                ttl=lifetime,
                require_tls=self._require_tls,
                expected_write_once=False,
            )
            await self._repository.record_upload_capability(
                intent.upload_id,
                tenant_id=intent.tenant_id,
                capability_id=uuid4(),
                session_generation=session.session_generation,
                part_number=1,
                size_bytes=intent.expected_size_bytes,
                checksum=intent.expected_digest,
                media_type=intent.media_type,
                compression=intent.compression,
                expires_at=handle.expires_at,
            )
            return BeginUploadResult(upload=intent, session=session, handle=handle)
        if session.state != "active":
            raise ArtifactConflictError("upload session is no longer writable")
        if session.part_count != part_count or session.part_size_bytes != part_size_bytes:
            raise ArtifactConflictError("upload session is bound to a different protocol version")
        first_part_size = min(intent.expected_size_bytes, session.part_size_bytes)
        result = await self.authorize_upload_part(
            AuthorizeArtifactUploadPart(
                upload_id=intent.upload_id,
                operation_id=intent.operation_id,
                tenant_id=intent.tenant_id,
                session_generation=session.session_generation,
                part_number=1,
                size_bytes=first_part_size,
                checksum=first_part_checksum,
            ),
            handle_ttl=lifetime,
        )
        return BeginUploadResult(upload=intent, session=session, handle=result.handle)

    def _expected_part_size(self, intent: UploadIntent, session: ArtifactUploadSession, part_number: int) -> int:
        if part_number > session.part_count:
            raise ArtifactPolicyError("upload part number exceeds the reserved session")
        if part_number < session.part_count:
            return session.part_size_bytes
        return intent.expected_size_bytes - session.part_size_bytes * (session.part_count - 1)

    async def authorize_upload_part(
        self, request: AuthorizeArtifactUploadPart, *, handle_ttl: timedelta | None = None
    ) -> UploadPartHandleResult:
        """Issue one exact-session part capability bound to length and checksum."""

        lifetime = self._ttl(handle_ttl)
        intent = await self._repository.get_upload(
            FinalizeArtifactUpload(
                upload_id=request.upload_id,
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
            )
        )
        if intent.artifact_id is not None:
            raise ArtifactConflictError("a finalized upload cannot issue new write capabilities")
        session = await self._repository.get_upload_session(request.upload_id, tenant_id=request.tenant_id)
        if session.state != "active" or session.session_generation != request.session_generation:
            raise ArtifactConflictError("upload session generation is not writable")
        expected_size = self._expected_part_size(intent, session, request.part_number)
        if request.size_bytes != expected_size:
            raise ArtifactVerificationError("upload part size differs from the reserved object shape")
        handle = await self._store.presign_upload_part(
            session=session,
            part_number=request.part_number,
            size_bytes=request.size_bytes,
            checksum=request.checksum,
            ttl=lifetime,
        )
        _validate_handle(handle, method="PUT", now=self._clock(), ttl=lifetime, require_tls=self._require_tls)
        await self._repository.record_upload_capability(
            intent.upload_id,
            tenant_id=intent.tenant_id,
            capability_id=uuid4(),
            session_generation=session.session_generation,
            part_number=request.part_number,
            size_bytes=request.size_bytes,
            checksum=request.checksum,
            media_type=intent.media_type,
            compression=intent.compression,
            expires_at=handle.expires_at,
        )
        return UploadPartHandleResult(session=session, part_number=request.part_number, handle=handle)

    async def store_upload_content(
        self,
        request: FinalizeArtifactUpload,
        *,
        content: bytes,
        declared_media_type: str | None = None,
        declared_size_bytes: int | None = None,
    ) -> InlineUploadReceipt:
        """Write customer bytes to the reserved content address through the gateway.

        The bytes are measured and matched against the immutable upload intent
        before the object is created, so a body that disagrees with the declared
        digest, size, media type or encoding never becomes a stored object and
        can never be finalized. ``declared_*`` carry what the transport claimed;
        a claim that contradicts the intent is rejected without reading further.
        """

        return await self._store_content(
            request,
            content=content,
            declared_media_type=declared_media_type,
            declared_size_bytes=declared_size_bytes,
            max_bytes=self._max_inline_content_bytes,
        )

    async def store_trusted_upload_content(
        self,
        request: FinalizeArtifactUpload,
        *,
        content: bytes,
    ) -> InlineUploadReceipt:
        """Persist bytes already bounded by the runtime response collector.

        This path is not public and therefore does not inherit the smaller HTTP
        inline-upload ceiling. It still enforces the artifact policy, immutable
        upload intent, digest, size and media type before and after storage.
        """

        return await self._store_content(
            request,
            content=content,
            declared_media_type=None,
            declared_size_bytes=None,
            max_bytes=self._max_artifact_bytes,
        )

    async def _store_content(
        self,
        request: FinalizeArtifactUpload,
        *,
        content: bytes,
        declared_media_type: str | None,
        declared_size_bytes: int | None,
        max_bytes: int,
    ) -> InlineUploadReceipt:
        intent = await self._repository.get_upload(request)
        if intent.artifact_id is not None:
            raise ArtifactConflictError("a finalized upload cannot accept new bytes")
        if len(content) > max_bytes:
            raise ArtifactContentTooLargeError("artifact exceeds the selected content ceiling")
        if declared_media_type is not None:
            declared = declared_media_type.split(";", 1)[0].strip().lower()
            if declared != intent.media_type:
                raise ArtifactVerificationError("declared media type differs from the upload intent")
        if declared_size_bytes is not None and declared_size_bytes != intent.expected_size_bytes:
            raise ArtifactVerificationError("declared size differs from the upload intent")
        measured_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if measured_digest != intent.expected_digest or len(content) != intent.expected_size_bytes:
            raise ArtifactVerificationError("inline bytes differ from the immutable upload intent")
        self._check_policy(intent.media_type, len(content))
        session = await self._repository.get_upload_session(intent.upload_id, tenant_id=intent.tenant_id)
        if session.state == "legacy":
            mutation_fence = self._clock() + self._max_handle_ttl
            await self._repository.record_upload_capability(
                intent.upload_id,
                tenant_id=intent.tenant_id,
                capability_id=uuid4(),
                session_generation=session.session_generation,
                part_number=1,
                size_bytes=intent.expected_size_bytes,
                checksum=intent.expected_digest,
                media_type=intent.media_type,
                compression=intent.compression,
                expires_at=mutation_fence,
            )
            verified = await self._store.put_legacy_object(
                storage_key=intent.storage_key,
                payload=content,
                media_type=intent.media_type,
                compression=intent.compression,
            )
            stored = StagedUploadPart(
                storage_key=verified.storage_key,
                digest=verified.digest,
                size_bytes=verified.size_bytes,
                media_type=verified.media_type,
                compression=verified.compression,
                provider_request_id=verified.provider_request_id,
            )
        elif session.state == "active":
            stored = await self._store.stage_inline_upload(
                session=session,
                intent=intent,
                payload=content,
            )
        else:
            raise ArtifactConflictError("upload session is no longer writable")
        if (
            stored.storage_key != intent.storage_key
            or stored.digest != intent.expected_digest
            or stored.size_bytes != intent.expected_size_bytes
            or stored.media_type != intent.media_type
            or stored.compression != intent.compression
        ):
            raise ArtifactVerificationError("staged inline upload differs from its immutable intent")
        return InlineUploadReceipt(upload=intent, stored=stored)

    async def open_content(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactContentStream:
        """Stream one authorized artifact's exact bytes to its own tenant.

        The repository enforces the tenant boundary before any object is read,
        and the returned chunks are the addressed bytes themselves, so the
        caller's own SHA-256 of the stream must equal the artifact digest.
        """

        record = await self._repository.get_artifact(artifact_id, tenant_id=tenant_id)
        if record.provider_version_id is None:
            verified = await self._store.recover_legacy_artifact(artifact=record)
            record = record.model_copy(update={"provider_version_id": verified.provider_version_id})
        return ArtifactContentStream(
            artifact=record,
            chunks=self._store.stream_object(
                record.storage_key,
                provider_version_id=record.provider_version_id,
                max_bytes=record.size_bytes,
            ),
        )

    async def finalize_upload(self, request: FinalizeArtifactUpload) -> ArtifactRecord:
        """Verify the stored bytes independently, then publish the content address."""

        # This status read grants no write authority.  A finalized retry may
        # return its immutable artifact even after the original write fence.
        intent = await self._repository.get_upload_status(request)
        if intent.artifact_id is not None:
            return await self._repository.get_artifact(intent.artifact_id, tenant_id=intent.tenant_id)
        try:
            session = await self._repository.get_upload_session(intent.upload_id, tenant_id=intent.tenant_id)
        except ArtifactNotFoundError:
            raise ArtifactConflictError("upload has no server-owned provider session") from None
        if session.state not in {"active", "legacy", "completed"}:
            raise ArtifactConflictError("upload session cannot be finalized")
        lease = await self._repository.get_finalization_lease(
            request,
            session_generation=session.session_generation,
        )
        if lease is None:
            # A malformed part set cannot install the cleanup-excluding ambiguity
            # fence.  Existing leases skip this preflight because provider
            # completion may already have happened before its DB response.
            if session.state == "active":
                await self._store.validate_upload_session(session=session, intent=intent)
            lease = await self._repository.acquire_finalization_lease(
                request,
                session_generation=session.session_generation,
                lease_id=uuid4(),
            )
        # Only the exact active lease can cross the capability deadline during
        # the configured completion grace.  Shared upload reads used by part
        # authorization and inline writes remain closed at the raw deadline.
        intent = await self._repository.get_leased_upload(request, lease=lease)
        if session.state == "legacy":
            verified = await self._store.recover_legacy_upload(intent=intent)
        elif session.state == "active":
            try:
                verified = await self._store.complete_upload_session(session=session, intent=intent)
            except ArtifactNotFoundError:
                verified = await self._store.recover_completed_upload(intent=intent)
        else:
            verified = await self._store.recover_completed_upload(intent=intent)
        return await self._verify_and_publish(request, intent, session, lease, verified)

    async def _verify_and_publish(
        self,
        request: FinalizeArtifactUpload,
        intent: UploadIntent,
        session: ArtifactUploadSession,
        lease: ArtifactFinalizationLease,
        verified: VerifiedStoredObject,
    ) -> ArtifactRecord:
        try:
            _verify_object(intent, verified)
            self._check_policy(verified.media_type, verified.size_bytes)
        except (ArtifactVerificationError, ArtifactPolicyError) as error:
            await self._repository.record_finalization_failure(
                request,
                session=session,
                lease=lease,
                verified=verified,
                failure_code=(
                    "content_verification_failed"
                    if isinstance(error, ArtifactVerificationError)
                    else "artifact_policy_failed"
                ),
            )
            raise
        return await self._repository.finalize_upload(
            request,
            verified,
            artifact_id=uuid4(),
            session=session,
            lease=lease,
        )

    async def download(
        self, artifact_id: UUID, *, tenant_id: str, handle_ttl: timedelta | None = None
    ) -> ArtifactDownload:
        record = await self._repository.get_artifact(artifact_id, tenant_id=tenant_id)
        if record.provider_version_id is None:
            verified = await self._store.recover_legacy_artifact(artifact=record)
            record = record.model_copy(update={"provider_version_id": verified.provider_version_id})
        lifetime = self._ttl(handle_ttl)
        handle = await self._store.presign_download(
            storage_key=record.storage_key,
            provider_version_id=record.provider_version_id,
            ttl=lifetime,
        )
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
        """Purge metadata only after independent provider absence released quota."""

        now = self._clock()
        purges: list[RetentionPurge] = []
        for operation_id, tenant_id, _ in await self._repository.claim_expired(now=now, limit=limit):
            try:
                purges.append(await self._repository.purge_operation(operation_id, tenant_id=tenant_id, now=now))
            except ArtifactConflictError:
                # Another worker claimed this operation between the scan and the
                # delete. Its purge is authoritative, so skip rather than fail.
                continue
        return purges

    async def remove_expired_quota_objects(self, *, limit: int = 50) -> list[ArtifactDeletionEvidence]:
        """Delete tenant-fair, backoff-fenced targets without releasing quota."""

        now = self._clock()
        targets = await self._repository.claim_expired_quota_removals(now=now, limit=limit)
        semaphore = asyncio.Semaphore(ARTIFACT_JANITOR_CONCURRENCY)

        async def remove_one(target: ArtifactRemovalTarget) -> ArtifactDeletionEvidence | None:
            async with semaphore:
                try:
                    evidence = await self._store.delete(target)
                    await self._repository.record_quota_removal_completion(target, evidence)
                    return evidence
                except ArtifactServiceError:
                    # Provider work is bounded per target; the durable retry
                    # timestamp resumes incomplete keys without holding later
                    # tenants behind a poisoned or high-cardinality key.
                    return None

        return [
            evidence
            for evidence in await asyncio.gather(*(remove_one(target) for target in targets))
            if evidence is not None
        ]

    async def record_finalization_failure(
        self,
        request: FinalizeArtifactUpload,
        *,
        session: ArtifactUploadSession,
        lease: ArtifactFinalizationLease,
        verified: VerifiedStoredObject,
        failure_code: Literal["content_verification_failed", "artifact_policy_failed"],
    ) -> ArtifactFinalizationFailureEvidence:
        return await self._repository.record_finalization_failure(
            request,
            session=session,
            lease=lease,
            verified=verified,
            failure_code=failure_code,
        )

    async def reconcile_stale_upload_session_creations(
        self, *, limit: int = 50
    ) -> list[ArtifactUploadSessionReconciliationEvidence]:
        """Abort provider sessions whose durable pre-create owner disappeared."""

        targets = await self._repository.claim_stale_upload_session_creations(limit=limit)
        semaphore = asyncio.Semaphore(ARTIFACT_JANITOR_CONCURRENCY)

        async def reconcile_one(
            target: ArtifactUploadSessionCreationTarget,
        ) -> ArtifactUploadSessionReconciliationEvidence | None:
            async with semaphore:
                try:
                    evidence = await self._store.reconcile_orphan_upload_sessions(target)
                    await self._repository.record_upload_session_creation_reconciled(target, evidence)
                    return evidence
                except ArtifactServiceError:
                    return None

        return [
            evidence
            for evidence in await asyncio.gather(*(reconcile_one(target) for target in targets))
            if evidence is not None
        ]

    async def recover_expired_finalizations(self, *, limit: int = 50) -> list[ArtifactRecord]:
        """Autonomously complete or recover every expired mutation fence.

        Expiry transfers ownership to this controller path; it never
        authorizes object cleanup or quota release.  A provider timeout keeps
        the renewed lease and is retried after its bounded recovery window.
        """

        targets = await self._repository.claim_expired_finalization_leases(limit=limit)
        semaphore = asyncio.Semaphore(ARTIFACT_JANITOR_CONCURRENCY)

        async def recover_one(target: ArtifactFinalizationRecoveryTarget) -> ArtifactRecord | None:
            async with semaphore:
                try:
                    intent = await self._repository.get_leased_upload(
                        target.request, lease=target.lease
                    )
                    if target.session.state == "legacy":
                        verified = await self._store.recover_legacy_upload(intent=intent)
                    elif target.session.state == "active":
                        try:
                            verified = await self._store.complete_upload_session(
                                session=target.session, intent=intent
                            )
                        except ArtifactNotFoundError:
                            verified = await self._store.recover_completed_upload(intent=intent)
                    else:
                        verified = await self._store.recover_completed_upload(intent=intent)
                    return await self._verify_and_publish(
                        target.request,
                        intent,
                        target.session,
                        target.lease,
                        verified,
                    )
                except ArtifactServiceError:
                    return None

        return [
            record
            for record in await asyncio.gather(*(recover_one(target) for target in targets))
            if record is not None
        ]

    async def verify_expired_quota_absence(self, *, limit: int = 50) -> list[ArtifactRemovalEvidence]:
        """Independently re-fetch absence and only then release retained quota."""

        now = self._clock()
        targets = await self._repository.claim_quota_verifications(now=now, limit=limit)
        semaphore = asyncio.Semaphore(ARTIFACT_JANITOR_CONCURRENCY)

        async def verify_one(target: ArtifactRemovalTarget) -> ArtifactRemovalEvidence | None:
            async with semaphore:
                try:
                    evidence = await self._store.verify_absent(target)
                    await self._repository.record_quota_removal(target, evidence)
                    return evidence
                except ArtifactServiceError:
                    try:
                        await self._repository.record_quota_verification_failure(target)
                    except ArtifactServiceError:
                        # A stale generation or unavailable database cannot
                        # make failed evidence authoritative; quota stays held.
                        pass
                    return None

        return [
            evidence
            for evidence in await asyncio.gather(*(verify_one(target) for target in targets))
            if evidence is not None
        ]

    async def pin_legacy_provider_versions(self, *, limit: int = 50) -> list[VerifiedStoredObject]:
        """Bind retained pre-0031 metadata to immutable read-only provider versions."""

        targets = await self._repository.claim_legacy_version_pins(limit=limit)
        semaphore = asyncio.Semaphore(ARTIFACT_JANITOR_CONCURRENCY)

        async def pin_one(target: LegacyArtifactVersionTarget) -> VerifiedStoredObject | None:
            async with semaphore:
                try:
                    scan = await self._store.scan_legacy_version(target)
                    verified = scan.verified
                    if verified is None:
                        await self._repository.record_legacy_version_scan(target, scan)
                        return None
                    if (
                        verified.storage_key != target.storage_key
                        or verified.digest != target.expected_digest
                        or verified.size_bytes != target.expected_size_bytes
                        or verified.media_type != target.expected_media_type
                        or verified.compression != target.expected_compression
                    ):
                        raise ArtifactVerificationError(
                            "legacy provider version differs from retained metadata"
                        )
                    await self._repository.record_legacy_version_pin(
                        target,
                        verified,
                        observed_at=scan.observed_at,
                    )
                    return verified
                except ArtifactServiceError:
                    return None

        return [
            verified
            for verified in await asyncio.gather(*(pin_one(target) for target in targets))
            if verified is not None
        ]


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
        self._upload_sessions: dict[UUID, ArtifactUploadSession] = {}
        self._upload_session_creation_claims: dict[UUID, ArtifactUploadSessionCreationClaim] = {}
        self._upload_session_reconciliation_empty_at: dict[UUID, datetime] = {}
        self._finalization_leases: dict[UUID, ArtifactFinalizationLease] = {}
        self._completed_finalization_leases: set[UUID] = set()
        self._failed_finalization_leases: set[UUID] = set()
        self._finalization_failures: dict[UUID, ArtifactFinalizationFailureEvidence] = {}
        self._quota_reservations: dict[UUID, ArtifactQuotaReservation] = {}
        self._quota_events: list[ArtifactQuotaEvent] = []
        self._upload_capabilities: dict[
            UUID,
            dict[
                UUID,
                tuple[int, int, int, str, str, ArtifactCompression | None, datetime],
            ],
        ] = {}
        self._removal_evidence: dict[UUID, ArtifactRemovalEvidence] = {}
        self._deletion_evidence: dict[tuple[UUID, int], ArtifactDeletionEvidence] = {}
        self._removal_attempts: dict[UUID, int] = {}
        self._removal_claimed_at: dict[UUID, datetime] = {}
        self._removal_retry_at: dict[UUID, datetime] = {}
        self._verification_attempts: dict[UUID, int] = {}
        self._verification_claimed_at: dict[UUID, datetime] = {}
        self._verification_retry_at: dict[UUID, datetime] = {}
        self._verification_failures: set[tuple[UUID, int, int]] = set()
        self._artifacts: dict[UUID, ArtifactRecord] = {}
        self._stage_commits: dict[tuple[UUID, str], StageCommitRecord] = {}
        self._run_results: dict[UUID, RunResultRecord] = {}
        self._events: list[ArtifactEvent] = []
        self._purged: set[UUID] = set()
        self._next_event_id = 1
        self._next_quota_event_id = 1

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

    def _append_quota_event(
        self,
        reservation: ArtifactQuotaReservation,
        event_type: ArtifactQuotaEventType,
        *,
        occurred_at: datetime,
        release_reason: ArtifactQuotaReleaseReason | None = None,
    ) -> None:
        self._quota_events.append(
            ArtifactQuotaEvent(
                event_id=self._next_quota_event_id,
                upload_id=reservation.upload_id,
                operation_id=reservation.operation_id,
                attempt_id=reservation.attempt_id,
                tenant_id=reservation.tenant_id,
                event_type=event_type,
                reserved_bytes=reservation.reserved_bytes,
                reserved_objects=reservation.reserved_objects,
                expires_at=reservation.expires_at,
                release_reason=release_reason,
                occurred_at=occurred_at,
            )
        )
        self._next_quota_event_id += 1

    def _release_quota_reservation(
        self,
        upload_id: UUID,
        *,
        evidence: ArtifactRemovalEvidence,
        now: datetime,
    ) -> None:
        reservation = self._quota_reservations.get(upload_id)
        if reservation is None or reservation.state is ArtifactQuotaReservationState.RELEASED:
            return
        intent = self._uploads.get(upload_id)
        if (
            reservation.state is not ArtifactQuotaReservationState.REMOVING
            or intent is None
            or intent.storage_key != evidence.storage_key
        ):
            raise ArtifactConflictError("provider removal evidence does not match a fenced upload")
        self._removal_evidence[upload_id] = evidence
        released = reservation.model_copy(
            update={
                "state": ArtifactQuotaReservationState.RELEASED,
                "released_at": now,
                "release_reason": ArtifactQuotaReleaseReason.PROVIDER_REMOVED,
            }
        )
        ArtifactQuotaReservation.model_validate(released.model_dump())
        self._quota_reservations[upload_id] = released
        self._append_quota_event(
            released,
            ArtifactQuotaEventType.RELEASED,
            occurred_at=now,
            release_reason=ArtifactQuotaReleaseReason.PROVIDER_REMOVED,
        )

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
        self,
        request: BeginArtifactUpload,
        storage_key: str,
        *,
        retention: timedelta,
        tenant_quota_bytes: int,
        tenant_quota_objects: int,
        reservation_ttl: timedelta,
        upload_completion_grace: timedelta,
        provider_stability_grace: timedelta,
    ) -> UploadIntent:
        del retention
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
            now = self._clock()
            existing = self._uploads.get(request.upload_id)
            if existing is not None:
                if not _same_upload_request(existing, request, storage_key):
                    return existing
                if existing.artifact_id is not None:
                    raise ArtifactConflictError("a finalized upload cannot issue new write capabilities")
                reservation = self._quota_reservations[request.upload_id]
                if reservation.state is ArtifactQuotaReservationState.ACTIVE:
                    if reservation.expires_at <= now:
                        raise ArtifactConflictError("upload reservation has passed its issuance deadline")
                    return existing
                raise ArtifactConflictError("upload is fenced for or has completed provider removal")
            active = [
                item
                for item in self._quota_reservations.values()
                if item.tenant_id == request.tenant_id
                and item.state is not ArtifactQuotaReservationState.RELEASED
            ]
            if (
                sum(item.reserved_bytes for item in active) + request.expected_size_bytes > tenant_quota_bytes
                or sum(item.reserved_objects for item in active) + 1 > tenant_quota_objects
            ):
                raise ArtifactQuotaExceededError("tenant artifact byte or object quota is exhausted")
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
            reservation = ArtifactQuotaReservation(
                upload_id=intent.upload_id,
                operation_id=intent.operation_id,
                attempt_id=intent.attempt_id,
                tenant_id=intent.tenant_id,
                reserved_bytes=intent.expected_size_bytes,
                state=ArtifactQuotaReservationState.ACTIVE,
                reserved_at=now,
                expires_at=now + reservation_ttl,
                latest_upload_capability_expires_at=now,
                upload_completion_grace_seconds=int(upload_completion_grace.total_seconds()),
                provider_stability_grace_seconds=int(provider_stability_grace.total_seconds()),
            )
            self._quota_reservations[intent.upload_id] = reservation
            self._append_quota_event(
                reservation,
                ArtifactQuotaEventType.RESERVED,
                occurred_at=now,
            )
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

    async def claim_upload_session_creation(
        self,
        upload_id: UUID,
        *,
        tenant_id: str,
        storage_key: str,
        claim_id: UUID,
        part_size_bytes: int,
        part_count: int,
    ) -> ArtifactUploadSessionCreationClaim:
        async with self._lock:
            intent = self._uploads.get(upload_id)
            reservation = self._quota_reservations.get(upload_id)
            if (
                intent is None
                or intent.tenant_id != tenant_id
                or intent.storage_key != storage_key
                or intent.artifact_id is not None
                or reservation is None
                or reservation.state is not ArtifactQuotaReservationState.ACTIVE
                or reservation.expires_at <= self._clock()
            ):
                raise ArtifactConflictError("upload-session creation fence has closed")
            existing = self._upload_session_creation_claims.get(upload_id)
            if existing is not None and existing.state != "reconciled":
                if (
                    existing.storage_key != storage_key
                    or existing.part_size_bytes != part_size_bytes
                    or existing.part_count != part_count
                ):
                    raise ArtifactConflictError("upload-session creation already differs")
                return existing
            generation = 1 if existing is None else existing.claim_generation + 1
            claimed_at = self._clock()
            claim = ArtifactUploadSessionCreationClaim(
                upload_id=upload_id,
                tenant_id=tenant_id,
                storage_key=storage_key,
                claim_id=claim_id,
                claim_generation=generation,
                part_size_bytes=part_size_bytes,
                part_count=part_count,
                provider_stability_grace_seconds=reservation.provider_stability_grace_seconds,
                state="creating",
                claimed_at=claimed_at,
                reconcile_after=claimed_at
                + timedelta(seconds=reservation.upload_completion_grace_seconds),
            )
            self._upload_session_creation_claims[upload_id] = claim
            return claim

    async def bind_upload_session(
        self,
        upload_id: UUID,
        *,
        tenant_id: str,
        storage_key: str,
        claim_id: UUID,
        provider_upload_id: str,
        part_size_bytes: int,
        part_count: int,
        initiated_at: datetime,
    ) -> ArtifactUploadSession:
        async with self._lock:
            intent = self._uploads.get(upload_id)
            claim = self._upload_session_creation_claims.get(upload_id)
            if (
                intent is None
                or intent.tenant_id != tenant_id
                or intent.storage_key != storage_key
                or intent.artifact_id is not None
                or claim is None
                or claim.claim_id != claim_id
                or claim.state != "creating"
            ):
                raise ArtifactConflictError("upload cannot bind a provider session")
            existing = self._upload_sessions.get(upload_id)
            if existing is not None:
                if (
                    existing.storage_key != storage_key
                    or existing.part_size_bytes != part_size_bytes
                    or existing.part_count != part_count
                    or existing.provider_stability_grace_seconds
                    != claim.provider_stability_grace_seconds
                ):
                    raise ArtifactConflictError("upload session already differs")
                return existing
            session = ArtifactUploadSession(
                upload_id=upload_id,
                tenant_id=tenant_id,
                storage_key=storage_key,
                provider_upload_id=provider_upload_id,
                session_generation=1,
                part_size_bytes=part_size_bytes,
                part_count=part_count,
                provider_stability_grace_seconds=claim.provider_stability_grace_seconds,
                initiated_at=initiated_at,
            )
            self._upload_sessions[upload_id] = session
            self._upload_session_creation_claims[upload_id] = claim.model_copy(
                update={"state": "bound", "provider_upload_id": provider_upload_id}
            )
            return session

    async def claim_stale_upload_session_creations(
        self, *, limit: int
    ) -> list[ArtifactUploadSessionCreationTarget]:
        async with self._lock:
            targets: list[ArtifactUploadSessionCreationTarget] = []
            for upload_id, claim in sorted(
                self._upload_session_creation_claims.items(), key=lambda item: item[1].reconcile_after
            ):
                if len(targets) >= limit or claim.state != "creating" or claim.reconcile_after > self._clock():
                    continue
                claimed_at = self._clock()
                reconciling = claim.model_copy(
                    update={"state": "reconciling", "reconcile_after": claimed_at + timedelta(minutes=5)}
                )
                self._upload_session_creation_claims[upload_id] = reconciling
                targets.append(
                    ArtifactUploadSessionCreationTarget(
                        upload_id=upload_id,
                        tenant_id=claim.tenant_id,
                        storage_key=claim.storage_key,
                        claim_id=claim.claim_id,
                        claim_generation=claim.claim_generation,
                        claimed_at=claimed_at,
                    )
                )
            return targets

    async def record_upload_session_creation_reconciled(
        self,
        target: ArtifactUploadSessionCreationTarget,
        evidence: ArtifactUploadSessionReconciliationEvidence,
    ) -> None:
        async with self._lock:
            claim = self._upload_session_creation_claims.get(target.upload_id)
            if (
                claim is None
                or claim.state != "reconciling"
                or claim.claim_id != target.claim_id
                or claim.claim_generation != target.claim_generation
                or claim.storage_key != evidence.storage_key
                or evidence.multipart_session_set_digest != EMPTY_VERSION_SET_DIGEST
            ):
                raise ArtifactConflictError("upload-session reconciliation evidence is stale")
            reservation = self._quota_reservations[target.upload_id]
            prior_empty = self._upload_session_reconciliation_empty_at.get(target.upload_id)
            if evidence.aborted_upload_count > 0 or prior_empty is None:
                self._upload_session_reconciliation_empty_at[target.upload_id] = evidence.observed_at
                self._upload_session_creation_claims[target.upload_id] = claim.model_copy(
                    update={
                        "state": "reconciling",
                        "reconcile_after": evidence.observed_at
                        + timedelta(seconds=reservation.provider_stability_grace_seconds),
                    }
                )
                return
            if evidence.observed_at < prior_empty + timedelta(
                seconds=reservation.provider_stability_grace_seconds
            ):
                raise ArtifactConflictError("upload-session provider quiet interval is incomplete")
            self._upload_session_creation_claims[target.upload_id] = claim.model_copy(
                update={"state": "reconciled"}
            )

    async def get_upload_session(
        self, upload_id: UUID, *, tenant_id: str
    ) -> ArtifactUploadSession:
        async with self._lock:
            session = self._upload_sessions.get(upload_id)
            if session is None or session.tenant_id != tenant_id:
                raise ArtifactNotFoundError("upload session not found")
            return session

    async def record_upload_session_aborted(
        self, session: ArtifactUploadSession, *, provider_request_id: str, observed_at: datetime
    ) -> ArtifactUploadSession:
        del provider_request_id, observed_at
        async with self._lock:
            current = self._upload_sessions.get(session.upload_id)
            if current != session or current.state != "active":
                raise ArtifactConflictError("upload-session abort is stale")
            current = current.model_copy(update={"state": "aborted"})
            ArtifactUploadSession.model_validate(current.model_dump())
            self._upload_sessions[session.upload_id] = current
            return current

    async def acquire_finalization_lease(
        self,
        request: FinalizeArtifactUpload,
        *,
        session_generation: int,
        lease_id: UUID,
    ) -> ArtifactFinalizationLease:
        async with self._lock:
            intent = self._uploads.get(request.upload_id)
            reservation = self._quota_reservations.get(request.upload_id)
            session = self._upload_sessions.get(request.upload_id)
            now = self._clock()
            existing = self._finalization_leases.get(request.upload_id)
            if (
                intent is None
                or intent.operation_id != request.operation_id
                or intent.tenant_id != request.tenant_id
                or intent.artifact_id is not None
                or reservation is None
                or reservation.state is not ArtifactQuotaReservationState.ACTIVE
                or session is None
                or session.session_generation != session_generation
                or session.state not in {"active", "legacy", "completed"}
            ):
                raise ArtifactConflictError("artifact finalization fence has closed")
            if existing is not None:
                if existing.session_generation != session_generation or existing.expires_at <= now:
                    raise ArtifactConflictError("artifact finalization lease already differs")
                return existing
            if (
                max(
                    reservation.expires_at,
                    reservation.latest_upload_capability_expires_at or reservation.reserved_at,
                )
                + timedelta(seconds=reservation.upload_completion_grace_seconds)
                <= now
            ):
                raise ArtifactConflictError("artifact finalization fence has closed")
            lease = ArtifactFinalizationLease(
                lease_id=lease_id,
                upload_id=request.upload_id,
                tenant_id=request.tenant_id,
                lease_generation=1,
                session_generation=session_generation,
                acquired_at=now,
                expires_at=now
                + timedelta(seconds=reservation.upload_completion_grace_seconds),
            )
            self._finalization_leases[request.upload_id] = lease
            return lease

    async def claim_expired_finalization_leases(
        self, *, limit: int
    ) -> list[ArtifactFinalizationRecoveryTarget]:
        async with self._lock:
            targets: list[ArtifactFinalizationRecoveryTarget] = []
            now = self._clock()
            for upload_id, lease in sorted(
                self._finalization_leases.items(), key=lambda item: item[1].expires_at
            ):
                if (
                    len(targets) >= limit
                    or lease.expires_at > now
                    or upload_id in self._completed_finalization_leases
                    or upload_id in self._failed_finalization_leases
                ):
                    continue
                intent = self._uploads.get(upload_id)
                session = self._upload_sessions.get(upload_id)
                reservation = self._quota_reservations.get(upload_id)
                if intent is None or session is None or reservation is None:
                    continue
                renewed = lease.model_copy(
                    update={
                        "lease_generation": lease.lease_generation + 1,
                        "acquired_at": now,
                        "expires_at": now
                        + timedelta(seconds=reservation.upload_completion_grace_seconds),
                    }
                )
                self._finalization_leases[upload_id] = renewed
                targets.append(
                    ArtifactFinalizationRecoveryTarget(
                        request=FinalizeArtifactUpload(
                            upload_id=upload_id,
                            operation_id=intent.operation_id,
                            tenant_id=intent.tenant_id,
                        ),
                        session=session,
                        lease=renewed,
                    )
                )
            return targets

    async def record_finalization_failure(
        self,
        request: FinalizeArtifactUpload,
        *,
        session: ArtifactUploadSession,
        lease: ArtifactFinalizationLease,
        verified: VerifiedStoredObject,
        failure_code: Literal["content_verification_failed", "artifact_policy_failed"],
    ) -> ArtifactFinalizationFailureEvidence:
        async with self._lock:
            current = self._finalization_leases.get(request.upload_id)
            current_session = self._upload_sessions.get(request.upload_id)
            if current != lease or current_session != session:
                raise ArtifactConflictError("artifact finalization failure fence is stale")
            evidence = ArtifactFinalizationFailureEvidence(
                upload_id=request.upload_id,
                tenant_id=request.tenant_id,
                lease_id=lease.lease_id,
                lease_generation=lease.lease_generation,
                session_generation=session.session_generation,
                provider_upload_id=session.provider_upload_id,
                provider_version_id=verified.provider_version_id,
                provider_request_id=verified.provider_request_id,
                failure_code=failure_code,
                observed_at=self._clock(),
            )
            existing = self._finalization_failures.get(request.upload_id)
            if existing is not None and existing != evidence:
                raise ArtifactConflictError("artifact finalization failure already differs")
            self._finalization_failures[request.upload_id] = evidence
            self._failed_finalization_leases.add(request.upload_id)
            completed = session.model_copy(
                update={"state": "completed", "provider_version_id": verified.provider_version_id}
            )
            self._upload_sessions[request.upload_id] = completed
            return evidence

    async def get_finalization_lease(
        self,
        request: FinalizeArtifactUpload,
        *,
        session_generation: int,
    ) -> ArtifactFinalizationLease | None:
        async with self._lock:
            lease = self._finalization_leases.get(request.upload_id)
            if lease is None:
                return None
            if (
                lease.tenant_id != request.tenant_id
                or lease.session_generation != session_generation
                or request.upload_id in self._completed_finalization_leases
                or request.upload_id in self._failed_finalization_leases
                or lease.expires_at <= self._clock()
            ):
                raise ArtifactConflictError("artifact finalization lease already differs")
            return lease

    async def record_upload_capability(
        self,
        upload_id: UUID,
        *,
        tenant_id: str,
        capability_id: UUID,
        session_generation: int,
        part_number: int,
        size_bytes: int,
        checksum: str,
        media_type: str,
        compression: ArtifactCompression | None,
        expires_at: datetime,
    ) -> ArtifactQuotaReservation:
        async with self._lock:
            reservation = self._quota_reservations.get(upload_id)
            if reservation is None or reservation.tenant_id != tenant_id:
                raise ArtifactNotFoundError("upload quota reservation not found")
            if reservation.state is not ArtifactQuotaReservationState.ACTIVE:
                raise ArtifactConflictError("upload capability cannot extend a fenced reservation")
            if reservation.expires_at <= self._clock():
                raise ArtifactConflictError("upload capability cannot extend a passed issuance deadline")
            session = self._upload_sessions.get(upload_id)
            if (
                session is None
                or session.state not in {"active", "legacy"}
                or session.session_generation != session_generation
                or (session.state == "legacy" and (part_number != 1 or session.part_count != 1))
            ):
                raise ArtifactConflictError("upload capability is outside the active session generation")
            intent = self._uploads[upload_id]
            if intent.media_type != media_type or intent.compression != compression:
                raise ArtifactConflictError("upload capability media identity differs")
            if session.state == "legacy" and (
                size_bytes != intent.expected_size_bytes or checksum != intent.expected_digest
            ):
                raise ArtifactConflictError("legacy upload capability content identity differs")
            expected_size = (
                session.part_size_bytes
                if part_number < session.part_count
                else intent.expected_size_bytes - session.part_size_bytes * (session.part_count - 1)
            )
            if not 1 <= part_number <= session.part_count or size_bytes != expected_size:
                raise ArtifactConflictError("upload capability part shape differs")
            capabilities = self._upload_capabilities.setdefault(upload_id, {})
            existing = capabilities.get(capability_id)
            capability = (
                session_generation,
                part_number,
                size_bytes,
                checksum,
                media_type,
                compression,
                expires_at,
            )
            if existing is not None and existing != capability:
                raise ArtifactConflictError("upload capability identity is already bound")
            if any(
                value[0] == session_generation
                and value[1] == part_number
                and (
                    value[2] != size_bytes
                    or value[3] != checksum
                    or value[4] != media_type
                    or value[5] != compression
                )
                for value in capabilities.values()
            ):
                raise ArtifactConflictError("upload part checksum is already bound")
            capabilities[capability_id] = capability
            latest = max(
                expires_at,
                reservation.latest_upload_capability_expires_at or reservation.reserved_at,
            )
            reservation = reservation.model_copy(
                update={"latest_upload_capability_expires_at": latest}
            )
            ArtifactQuotaReservation.model_validate(reservation.model_dump())
            self._quota_reservations[upload_id] = reservation
            return reservation

    async def get_upload(self, request: FinalizeArtifactUpload) -> UploadIntent:
        async with self._lock:
            intent = self._uploads.get(request.upload_id)
            if intent is None or intent.operation_id != request.operation_id or intent.tenant_id != request.tenant_id:
                raise ArtifactNotFoundError("upload not found")
            reservation = self._quota_reservations.get(request.upload_id)
            if reservation is None or reservation.state is not ArtifactQuotaReservationState.ACTIVE:
                raise ArtifactConflictError("upload is fenced for or has completed provider removal")
            if reservation.expires_at <= self._clock():
                raise ArtifactConflictError("upload reservation has passed its issuance deadline")
            return intent

    async def get_upload_status(self, request: FinalizeArtifactUpload) -> UploadIntent:
        async with self._lock:
            intent = self._uploads.get(request.upload_id)
            if (
                intent is None
                or intent.operation_id != request.operation_id
                or intent.tenant_id != request.tenant_id
            ):
                raise ArtifactNotFoundError("upload not found")
            return intent

    async def get_leased_upload(
        self, request: FinalizeArtifactUpload, *, lease: ArtifactFinalizationLease
    ) -> UploadIntent:
        async with self._lock:
            intent = self._uploads.get(request.upload_id)
            reservation = self._quota_reservations.get(request.upload_id)
            current = self._finalization_leases.get(request.upload_id)
            if (
                intent is None
                or intent.operation_id != request.operation_id
                or intent.tenant_id != request.tenant_id
                or intent.artifact_id is not None
                or reservation is None
                or reservation.state is not ArtifactQuotaReservationState.ACTIVE
                or current != lease
                or request.upload_id in self._completed_finalization_leases
            ):
                raise ArtifactConflictError("artifact finalization lease is stale")
            return intent

    async def quota_reservation(self, upload_id: UUID, *, tenant_id: str) -> ArtifactQuotaReservation:
        async with self._lock:
            reservation = self._quota_reservations.get(upload_id)
            if reservation is None or reservation.tenant_id != tenant_id:
                raise ArtifactNotFoundError("upload quota reservation not found")
            return reservation

    async def list_quota_events(
        self, *, tenant_id: str, after_id: int = 0, limit: int = 500
    ) -> list[ArtifactQuotaEvent]:
        async with self._lock:
            return [
                event
                for event in self._quota_events
                if event.tenant_id == tenant_id and event.event_id > after_id
            ][: max(1, min(limit, 1000))]

    async def claim_expired_quota_removals(
        self, *, now: datetime, limit: int
    ) -> list[ArtifactRemovalTarget]:
        async with self._lock:
            targets: list[ArtifactRemovalTarget] = []
            by_tenant: dict[str, list[ArtifactQuotaReservation]] = {}
            for reservation in self._quota_reservations.values():
                if reservation.state is ArtifactQuotaReservationState.RELEASED:
                    continue
                if (
                    reservation.upload_id in self._finalization_leases
                    and reservation.upload_id not in self._completed_finalization_leases
                    and reservation.upload_id not in self._failed_finalization_leases
                ):
                    continue
                capability_expires_at = (
                    reservation.latest_upload_capability_expires_at or reservation.reserved_at
                )
                eligible_at = max(reservation.expires_at, capability_expires_at) + timedelta(
                    seconds=reservation.upload_completion_grace_seconds
                )
                if reservation.state is ArtifactQuotaReservationState.ACTIVE:
                    if eligible_at > now:
                        continue
                else:
                    generation = self._removal_attempts.get(reservation.upload_id, 0)
                    completed = (reservation.upload_id, generation) in self._deletion_evidence
                    failed = any(
                        upload_id == reservation.upload_id and removal_generation == generation
                        for upload_id, removal_generation, _verification_generation in self._verification_failures
                    )
                    if (
                        self._removal_retry_at.get(reservation.upload_id, now) > now
                        or (completed and not failed)
                    ):
                        continue
                by_tenant.setdefault(reservation.tenant_id, []).append(reservation)
            fair = sorted(
                (
                    rank,
                    reservation.expires_at,
                    reservation.tenant_id,
                    reservation.upload_id.int,
                    reservation,
                )
                for tenant_reservations in by_tenant.values()
                for rank, reservation in enumerate(
                    sorted(tenant_reservations, key=lambda item: (item.expires_at, item.upload_id.int)),
                    start=1,
                )
            )
            for _rank, _expires_at, _tenant_id, _upload_order, reservation in fair:
                if len(targets) >= max(1, min(limit, 500)):
                    break
                if reservation.state is ArtifactQuotaReservationState.ACTIVE:
                    reservation = reservation.model_copy(update={"state": ArtifactQuotaReservationState.REMOVING})
                    self._quota_reservations[reservation.upload_id] = reservation
                    self._append_quota_event(
                        reservation,
                        ArtifactQuotaEventType.REMOVAL_CLAIMED,
                        occurred_at=now,
                    )
                attempts = self._removal_attempts.get(reservation.upload_id, 0)
                self._removal_attempts[reservation.upload_id] = attempts + 1
                self._removal_claimed_at[reservation.upload_id] = now
                self._removal_retry_at[reservation.upload_id] = now + timedelta(
                    seconds=min(3600, 30 * 2 ** min(attempts, 6))
                )
                intent = self._uploads[reservation.upload_id]
                session = self._upload_sessions.get(reservation.upload_id)
                capability_expires_at = (
                    reservation.latest_upload_capability_expires_at or reservation.reserved_at
                )
                eligible_at = max(reservation.expires_at, capability_expires_at) + timedelta(
                    seconds=reservation.upload_completion_grace_seconds
                )
                targets.append(
                    ArtifactRemovalTarget(
                        upload_id=reservation.upload_id,
                        operation_id=reservation.operation_id,
                        attempt_id=reservation.attempt_id,
                        tenant_id=reservation.tenant_id,
                        storage_key=intent.storage_key,
                        provider_upload_id=session.provider_upload_id if session else None,
                        upload_session_generation=session.session_generation if session else 0,
                        latest_upload_capability_expires_at=capability_expires_at,
                        removal_generation=attempts + 1,
                        verification_generation=0,
                        removal_claimed_at=now,
                        eligible_at=eligible_at,
                    )
                )
            return targets

    async def claim_quota_verifications(
        self, *, now: datetime, limit: int
    ) -> list[ArtifactRemovalTarget]:
        async with self._lock:
            targets: list[ArtifactRemovalTarget] = []
            by_tenant: dict[str, list[ArtifactQuotaReservation]] = {}
            for reservation in self._quota_reservations.values():
                if reservation.state is not ArtifactQuotaReservationState.REMOVING:
                    continue
                removal_generation = self._removal_attempts.get(reservation.upload_id, 0)
                completion = self._deletion_evidence.get((reservation.upload_id, removal_generation))
                if completion is None:
                    continue
                if any(
                    upload_id == reservation.upload_id and failed_generation == removal_generation
                    for upload_id, failed_generation, _verification_generation in self._verification_failures
                ):
                    continue
                stable_at = completion.observed_at + timedelta(
                    seconds=reservation.provider_stability_grace_seconds
                )
                if stable_at > now:
                    continue
                if self._verification_retry_at.get(reservation.upload_id, now) > now:
                    continue
                by_tenant.setdefault(reservation.tenant_id, []).append(reservation)
            fair = sorted(
                (
                    rank,
                    reservation.expires_at,
                    reservation.tenant_id,
                    reservation.upload_id.int,
                    reservation,
                )
                for tenant_reservations in by_tenant.values()
                for rank, reservation in enumerate(
                    sorted(tenant_reservations, key=lambda item: (item.expires_at, item.upload_id.int)),
                    start=1,
                )
            )
            for _rank, _expires_at, _tenant_id, _upload_order, reservation in fair:
                if len(targets) >= max(1, min(limit, 500)):
                    break
                attempts = self._verification_attempts.get(reservation.upload_id, 0)
                self._verification_attempts[reservation.upload_id] = attempts + 1
                self._verification_claimed_at[reservation.upload_id] = now
                self._verification_retry_at[reservation.upload_id] = now + timedelta(
                    seconds=min(3600, 30 * 2 ** min(attempts, 6))
                )
                intent = self._uploads[reservation.upload_id]
                session = self._upload_sessions.get(reservation.upload_id)
                capability_expires_at = (
                    reservation.latest_upload_capability_expires_at or reservation.reserved_at
                )
                eligible_at = max(reservation.expires_at, capability_expires_at) + timedelta(
                    seconds=reservation.upload_completion_grace_seconds
                )
                targets.append(
                    ArtifactRemovalTarget(
                        upload_id=reservation.upload_id,
                        operation_id=reservation.operation_id,
                        attempt_id=reservation.attempt_id,
                        tenant_id=reservation.tenant_id,
                        storage_key=intent.storage_key,
                        provider_upload_id=session.provider_upload_id if session else None,
                        upload_session_generation=session.session_generation if session else 0,
                        latest_upload_capability_expires_at=capability_expires_at,
                        removal_generation=removal_generation,
                        verification_generation=attempts + 1,
                        removal_claimed_at=self._removal_claimed_at[reservation.upload_id],
                        verification_claimed_at=now,
                        eligible_at=eligible_at,
                    )
                )
            return targets

    async def record_quota_removal_completion(
        self, target: ArtifactRemovalTarget, evidence: ArtifactDeletionEvidence
    ) -> None:
        async with self._lock:
            reservation = self._quota_reservations.get(target.upload_id)
            if reservation is None or reservation.tenant_id != target.tenant_id:
                raise ArtifactNotFoundError("upload quota reservation not found")
            if (
                reservation.state is not ArtifactQuotaReservationState.REMOVING
                or self._removal_attempts.get(target.upload_id) != target.removal_generation
                or self._removal_claimed_at.get(target.upload_id) != target.removal_claimed_at
                or evidence.storage_key != target.storage_key
                or evidence.observed_at < target.removal_claimed_at
            ):
                raise ArtifactConflictError("provider deletion result does not match the current removal claim")
            key = (target.upload_id, target.removal_generation)
            existing = self._deletion_evidence.get(key)
            if existing is not None and existing != evidence:
                raise ArtifactConflictError("provider deletion result already differs")
            self._deletion_evidence[key] = evidence
            session = self._upload_sessions.get(target.upload_id)
            if session is not None and session.state == "active":
                session = session.model_copy(update={"state": "aborted"})
                ArtifactUploadSession.model_validate(session.model_dump())
                self._upload_sessions[target.upload_id] = session
            self._verification_retry_at[target.upload_id] = evidence.observed_at + timedelta(
                seconds=reservation.provider_stability_grace_seconds
            )

    async def record_quota_verification_failure(self, target: ArtifactRemovalTarget) -> None:
        async with self._lock:
            reservation = self._quota_reservations.get(target.upload_id)
            if reservation is None or reservation.tenant_id != target.tenant_id:
                raise ArtifactNotFoundError("upload quota reservation not found")
            if (
                reservation.state is not ArtifactQuotaReservationState.REMOVING
                or self._removal_attempts.get(target.upload_id) != target.removal_generation
                or self._verification_attempts.get(target.upload_id) != target.verification_generation
                or self._verification_claimed_at.get(target.upload_id) != target.verification_claimed_at
            ):
                raise ArtifactConflictError("artifact verification failure is stale")
            self._verification_failures.add(
                (target.upload_id, target.removal_generation, target.verification_generation)
            )
            self._removal_retry_at[target.upload_id] = self._clock()

    async def claim_legacy_version_pins(self, *, limit: int) -> list[LegacyArtifactVersionTarget]:
        del limit
        return []

    async def record_legacy_version_pin(
        self,
        target: LegacyArtifactVersionTarget,
        verified: VerifiedStoredObject,
        *,
        observed_at: datetime,
    ) -> None:
        del target, verified, observed_at
        raise ArtifactConflictError("in-memory artifacts are created with provider version pins")

    async def record_legacy_version_scan(
        self, target: LegacyArtifactVersionTarget, scan: LegacyArtifactVersionScan
    ) -> None:
        del target, scan
        raise ArtifactConflictError("in-memory artifacts never require legacy version scans")

    async def legacy_version_rollout_status(self) -> LegacyArtifactRolloutStatus:
        return LegacyArtifactRolloutStatus(
            pending=0,
            bound=0,
            unresolved=0,
            unbound_artifacts=0,
            missing_unfinished_upload_sessions=0,
        )

    async def mark_schema_bridge_ready(
        self,
        *,
        bridge_image_ref: str,
        bridge_release_revision: int,
        predecessor_image_ref: str,
        evidence: SchemaBridgeDrainEvidence,
    ) -> None:
        del (
            bridge_image_ref,
            bridge_release_revision,
            predecessor_image_ref,
            evidence,
        )

    async def record_quota_removal(
        self, target: ArtifactRemovalTarget, evidence: ArtifactRemovalEvidence
    ) -> ArtifactQuotaReservation:
        async with self._lock:
            if (
                evidence.kind is not ArtifactRemovalEvidenceKind.ABSENCE_CONFIRMED
                or evidence.removed_version_count != 0
            ):
                raise ArtifactConflictError("quota release requires independent absence evidence")
            claimed_at = self._verification_claimed_at.get(target.upload_id)
            reservation = self._quota_reservations.get(target.upload_id)
            completion = self._deletion_evidence.get(
                (target.upload_id, target.removal_generation)
            )
            if (
                claimed_at is None
                or claimed_at != target.verification_claimed_at
                or reservation is None
                or self._removal_attempts.get(target.upload_id) != target.removal_generation
                or self._verification_attempts.get(target.upload_id)
                != target.verification_generation
                or completion is None
                or claimed_at
                < completion.observed_at
                + timedelta(seconds=reservation.provider_stability_grace_seconds)
                or evidence.observed_at < claimed_at
                or evidence.latest_upload_capability_expires_at
                != target.latest_upload_capability_expires_at
                or evidence.removal_generation != target.removal_generation
                or evidence.verification_generation != target.verification_generation
                or evidence.claim_digest
                != artifact_absence_claim_digest(
                    target,
                    observed_at=evidence.observed_at,
                    first_list_request_id=evidence.first_list_request_id,
                    head_request_id=evidence.head_request_id,
                    second_list_request_id=evidence.second_list_request_id,
                    first_version_set_digest=evidence.first_version_set_digest,
                    second_version_set_digest=evidence.second_version_set_digest,
                    first_multipart_list_request_id=evidence.first_multipart_list_request_id,
                    second_multipart_list_request_id=evidence.second_multipart_list_request_id,
                    first_multipart_session_set_digest=evidence.first_multipart_session_set_digest,
                    second_multipart_session_set_digest=evidence.second_multipart_session_set_digest,
                )
            ):
                raise ArtifactConflictError("quota release lacks a current verification claim")
            if reservation is None or reservation.tenant_id != target.tenant_id:
                raise ArtifactNotFoundError("upload quota reservation not found")
            existing = self._removal_evidence.get(target.upload_id)
            if existing is not None:
                if existing != evidence:
                    raise ArtifactConflictError("provider removal evidence already differs")
                return reservation
            self._release_quota_reservation(target.upload_id, evidence=evidence, now=evidence.observed_at)
            return self._quota_reservations[target.upload_id]

    async def finalize_upload(
        self,
        request: FinalizeArtifactUpload,
        verified: VerifiedStoredObject,
        *,
        artifact_id: UUID,
        session: ArtifactUploadSession | None,
        lease: ArtifactFinalizationLease,
    ) -> ArtifactRecord:
        async with self._lock:
            self._assert_writable(request.operation_id, request.tenant_id)
            intent = self._uploads.get(request.upload_id)
            if intent is None or intent.operation_id != request.operation_id or intent.tenant_id != request.tenant_id:
                raise ArtifactNotFoundError("upload not found")
            if intent.artifact_id is not None:
                return self._artifacts[intent.artifact_id]
            now = self._clock()
            reservation = self._quota_reservations.get(request.upload_id)
            if reservation is None or reservation.state is not ArtifactQuotaReservationState.ACTIVE:
                raise ArtifactConflictError("upload is fenced for or has completed provider removal")
            if self._finalization_leases.get(request.upload_id) != lease:
                raise ArtifactConflictError("artifact finalization lease is stale")
            attempt = self._attempts[intent.attempt_id]
            self._assert_live_attempt(attempt)
            _verify_object(intent, verified)
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
                provider_version_id=verified.provider_version_id,
                access=intent.access,
                retention_expires_at=now + (attempt.retention_expires_at - attempt.started_at),
                created_at=now,
            )
            self._artifacts[record.artifact_id] = record
            if session is not None:
                current_session = self._upload_sessions.get(intent.upload_id)
                if current_session != session:
                    raise ArtifactConflictError("upload session changed before finalization")
                if current_session.state == "active":
                    completed_session = current_session.model_copy(
                        update={
                            "state": "completed",
                            "provider_version_id": verified.provider_version_id,
                        }
                    )
                    ArtifactUploadSession.model_validate(completed_session.model_dump())
                    self._upload_sessions[intent.upload_id] = completed_session
                elif current_session.state == "completed" and (
                    current_session.provider_version_id != verified.provider_version_id
                ):
                    raise ArtifactConflictError("upload session is bound to another provider version")
            self._uploads[intent.upload_id] = intent.model_copy(
                update={"artifact_id": record.artifact_id, "finalized_at": now}
            )
            extended = reservation.model_copy(update={"expires_at": record.retention_expires_at})
            ArtifactQuotaReservation.model_validate(extended.model_dump())
            self._quota_reservations[intent.upload_id] = extended
            self._completed_finalization_leases.add(intent.upload_id)
            self._append_quota_event(
                extended,
                ArtifactQuotaEventType.RETENTION_EXTENDED,
                occurred_at=now,
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

    async def purge_keys(self, operation_id: UUID, *, tenant_id: str) -> list[ArtifactRemovalTarget]:
        async with self._lock:
            targets: list[ArtifactRemovalTarget] = []
            now = self._clock()
            for reservation in self._quota_reservations.values():
                if (
                    reservation.operation_id != operation_id
                    or reservation.tenant_id != tenant_id
                    or reservation.state is ArtifactQuotaReservationState.RELEASED
                ):
                    continue
                capability_expires_at = (
                    reservation.latest_upload_capability_expires_at or reservation.reserved_at
                )
                eligible_at = max(reservation.expires_at, capability_expires_at) + timedelta(
                    seconds=reservation.upload_completion_grace_seconds
                )
                if eligible_at > now:
                    continue
                if reservation.state is ArtifactQuotaReservationState.ACTIVE:
                    reservation = reservation.model_copy(update={"state": ArtifactQuotaReservationState.REMOVING})
                    self._quota_reservations[reservation.upload_id] = reservation
                    self._append_quota_event(
                        reservation,
                        ArtifactQuotaEventType.REMOVAL_CLAIMED,
                        occurred_at=now,
                    )
                attempts = self._removal_attempts.get(reservation.upload_id, 0)
                self._removal_attempts[reservation.upload_id] = attempts + 1
                self._removal_claimed_at[reservation.upload_id] = now
                self._removal_retry_at[reservation.upload_id] = now + timedelta(seconds=30)
                targets.append(
                    ArtifactRemovalTarget(
                        upload_id=reservation.upload_id,
                        operation_id=reservation.operation_id,
                        attempt_id=reservation.attempt_id,
                        tenant_id=reservation.tenant_id,
                        storage_key=self._uploads[reservation.upload_id].storage_key,
                        latest_upload_capability_expires_at=capability_expires_at,
                        removal_generation=attempts + 1,
                        verification_generation=0,
                        removal_claimed_at=now,
                        eligible_at=eligible_at,
                    )
                )
            return targets

    async def purge_operation(self, operation_id: UUID, *, tenant_id: str, now: datetime) -> RetentionPurge:
        async with self._lock:
            result = self._run_results.get(operation_id)
            if result is None or result.tenant_id != tenant_id:
                raise ArtifactNotFoundError("terminal result not found")
            if operation_id in self._purged:
                raise ArtifactConflictError("this operation was already purged")
            if any(
                reservation.operation_id == operation_id
                and reservation.tenant_id == tenant_id
                and reservation.state is not ArtifactQuotaReservationState.RELEASED
                for reservation in self._quota_reservations.values()
            ):
                raise ArtifactConflictError("provider removal evidence is incomplete")
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
        provider_version_id=row["provider_version_id"],
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


def _upload_session_from_row(row: Mapping[str, Any]) -> ArtifactUploadSession:
    return ArtifactUploadSession(
        upload_id=row["upload_id"],
        tenant_id=row["tenant_id"],
        storage_key=row["storage_key"],
        provider_upload_id=row["provider_upload_id"],
        session_generation=row["session_generation"],
        part_size_bytes=row["part_size_bytes"],
        part_count=row["part_count"],
        provider_stability_grace_seconds=row["provider_stability_grace_seconds"],
        initiated_at=row["initiated_at"],
        state=row["state"],
        provider_version_id=row["provider_version_id"],
    )


def _upload_session_creation_claim_from_row(
    row: Mapping[str, Any],
) -> ArtifactUploadSessionCreationClaim:
    return ArtifactUploadSessionCreationClaim(
        upload_id=row["upload_id"],
        tenant_id=row["tenant_id"],
        storage_key=row["storage_key"],
        claim_id=row["claim_id"],
        claim_generation=row["claim_generation"],
        part_size_bytes=row["part_size_bytes"],
        part_count=row["part_count"],
        provider_stability_grace_seconds=row["provider_stability_grace_seconds"],
        state=row["state"],
        claimed_at=row["claimed_at"],
        reconcile_after=row["reconcile_after"],
        provider_upload_id=row["provider_upload_id"],
    )


def _finalization_lease_from_row(row: Mapping[str, Any]) -> ArtifactFinalizationLease:
    return ArtifactFinalizationLease(
        lease_id=row["lease_id"],
        upload_id=row["upload_id"],
        tenant_id=row["tenant_id"],
        lease_generation=row["lease_generation"],
        session_generation=row["session_generation"],
        acquired_at=row["acquired_at"],
        expires_at=row["expires_at"],
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


def _quota_reservation_from_row(row: Mapping[str, Any]) -> ArtifactQuotaReservation:
    return ArtifactQuotaReservation(
        upload_id=row["upload_id"],
        operation_id=row["operation_id"],
        attempt_id=row["attempt_id"],
        tenant_id=row["tenant_id"],
        reserved_bytes=row["reserved_bytes"],
        reserved_objects=row["reserved_objects"],
        state=ArtifactQuotaReservationState(row["state"]),
        reserved_at=row["reserved_at"],
        expires_at=row["expires_at"],
        latest_upload_capability_expires_at=row["latest_upload_capability_expires_at"],
        upload_completion_grace_seconds=row["upload_completion_grace_seconds"],
        provider_stability_grace_seconds=row["provider_stability_grace_seconds"],
        released_at=row["released_at"],
        release_reason=(
            ArtifactQuotaReleaseReason(row["release_reason"])
            if row["release_reason"] is not None
            else None
        ),
    )


def _quota_event_from_row(row: Mapping[str, Any]) -> ArtifactQuotaEvent:
    return ArtifactQuotaEvent(
        event_id=row["id"],
        upload_id=row["upload_id"],
        operation_id=row["operation_id"],
        attempt_id=row["attempt_id"],
        tenant_id=row["tenant_id"],
        event_type=ArtifactQuotaEventType(row["event_type"]),
        reserved_bytes=row["reserved_bytes"],
        reserved_objects=row["reserved_objects"],
        expires_at=row["expires_at"],
        release_reason=(
            ArtifactQuotaReleaseReason(row["release_reason"])
            if row["release_reason"] is not None
            else None
        ),
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
    media_type,compression,storage_key,provider_version_id,access_profile,access_receipt_digest,
    retention_expires_at,created_at"""
_UPLOAD_COLUMNS = """id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,expected_digest,
    expected_size_bytes,media_type,compression,storage_key,access_profile,access_receipt_digest,
    artifact_id,begun_at,finalized_at,provider_version_id"""
_UPLOAD_SESSION_COLUMNS = """upload_id,tenant_id,storage_key,provider_upload_id,session_generation,
    part_size_bytes,part_count,provider_stability_grace_seconds,initiated_at,state,provider_version_id"""
_QUOTA_RESERVATION_COLUMNS = """upload_id,operation_id,attempt_id,tenant_id,reserved_bytes,
    reserved_objects,state,reserved_at,expires_at,latest_upload_capability_expires_at,
    upload_completion_grace_seconds,provider_stability_grace_seconds,released_at,release_reason"""
_QUOTA_EVENT_COLUMNS = """id,upload_id,operation_id,attempt_id,tenant_id,event_type,reserved_bytes,
    reserved_objects,expires_at,release_reason,occurred_at"""


_SELECT_ARTIFACT_SQL = f"""
    SELECT {_ARTIFACT_COLUMNS} FROM fs2_scientific_artifacts_versioned WHERE id=$1 AND tenant_id=$2
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

    @staticmethod
    async def _tenant_quota_lock(connection: asyncpg.Connection[Any], tenant_id: str) -> None:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 7221))",
            tenant_id,
        )

    async def open_attempt(self, request: OpenStageAttempt, *, retention: timedelta) -> StageAttemptRecord:
        admission = request.admission
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                await self._tenant_quota_lock(connection, request.tenant_id)
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
                await self._tenant_quota_lock(connection, request.tenant_id)
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
        self,
        request: BeginArtifactUpload,
        storage_key: str,
        *,
        retention: timedelta,
        tenant_quota_bytes: int,
        tenant_quota_objects: int,
        reservation_ttl: timedelta,
        upload_completion_grace: timedelta,
        provider_stability_grace: timedelta,
    ) -> UploadIntent:
        del retention
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                await self._tenant_quota_lock(connection, request.tenant_id)
                existing = await connection.fetchrow(
                    f"SELECT {_UPLOAD_COLUMNS} FROM fs2_scientific_uploads WHERE id=$1 FOR UPDATE",  # noqa: S608
                    request.upload_id,
                )
                if existing is not None:
                    intent = _upload_from_row(existing)
                    if intent.tenant_id != request.tenant_id:
                        raise ArtifactNotFoundError("upload not found")
                    if not _same_upload_request(intent, request, storage_key):
                        return intent
                    if intent.artifact_id is not None:
                        raise ArtifactConflictError("a finalized upload cannot issue new write capabilities")
                    reservation_row = await connection.fetchrow(
                        f"SELECT {_QUOTA_RESERVATION_COLUMNS} "  # noqa: S608
                        "FROM fs2_scientific_artifact_quota_reservations WHERE upload_id=$1 FOR UPDATE",
                        request.upload_id,
                    )
                    if reservation_row is None:
                        raise ArtifactConflictError("upload quota reservation is absent")
                    reservation = _quota_reservation_from_row(reservation_row)
                    if reservation.state is ArtifactQuotaReservationState.ACTIVE:
                        issuance_open = await connection.fetchval(
                            "SELECT expires_at>clock_timestamp() "
                            "FROM fs2_scientific_artifact_quota_reservations WHERE upload_id=$1",
                            request.upload_id,
                        )
                        if issuance_open is not True:
                            raise ArtifactConflictError("upload reservation has passed its issuance deadline")
                        return intent
                    raise ArtifactConflictError("upload is fenced for or has completed provider removal")
                attempt = await connection.fetchrow(
                    "SELECT stage_id,shard_id FROM fs2_scientific_stage_attempts "
                    "WHERE attempt_id=$1 AND operation_id=$2 AND tenant_id=$3",
                    request.attempt_id,
                    request.operation_id,
                    request.tenant_id,
                )
                if attempt is None:
                    raise ArtifactNotFoundError("attempt not found")
                totals = await connection.fetchrow(
                    """
                    SELECT COALESCE(sum(reserved_bytes),0)::bigint AS bytes,
                           COALESCE(sum(reserved_objects),0)::bigint AS objects
                    FROM fs2_scientific_artifact_quota_reservations
                    WHERE tenant_id=$1 AND state<>'released'
                    """,
                    request.tenant_id,
                )
                assert totals is not None
                if (
                    int(totals["bytes"]) + request.expected_size_bytes > tenant_quota_bytes
                    or int(totals["objects"]) + 1 > tenant_quota_objects
                ):
                    raise ArtifactQuotaExceededError("tenant artifact byte or object quota is exhausted")
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
                    reservation = await connection.fetchrow(
                        f"""
                        INSERT INTO fs2_scientific_artifact_quota_reservations(
                            upload_id,operation_id,attempt_id,tenant_id,reserved_bytes,reserved_objects,
                            state,reserved_at,expires_at,latest_upload_capability_expires_at,
                            upload_completion_grace_seconds,provider_stability_grace_seconds
                        ) VALUES($1,$2,$3,$4,$5,1,'active',$6,$6+$7,$6,$8,$9)
                        RETURNING {_QUOTA_RESERVATION_COLUMNS}
                        """,  # noqa: S608
                        request.upload_id,
                        request.operation_id,
                        request.attempt_id,
                        request.tenant_id,
                        request.expected_size_bytes,
                        row["begun_at"],
                        reservation_ttl,
                        int(upload_completion_grace.total_seconds()),
                        int(provider_stability_grace.total_seconds()),
                    )
                    assert reservation is not None
                    await connection.execute(
                        """
                        INSERT INTO fs2_scientific_artifact_quota_events(
                            upload_id,operation_id,attempt_id,tenant_id,event_type,reserved_bytes,
                            reserved_objects,expires_at,occurred_at
                        ) VALUES($1,$2,$3,$4,'reserved',$5,1,$6,$7)
                        """,
                        request.upload_id,
                        request.operation_id,
                        request.attempt_id,
                        request.tenant_id,
                        request.expected_size_bytes,
                        reservation["expires_at"],
                        row["begun_at"],
                    )
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

    async def claim_upload_session_creation(
        self,
        upload_id: UUID,
        *,
        tenant_id: str,
        storage_key: str,
        claim_id: UUID,
        part_size_bytes: int,
        part_count: int,
    ) -> ArtifactUploadSessionCreationClaim:
        try:
            row = await self.pool.fetchrow(
                "SELECT * FROM fs2_scientific_claim_upload_session_creation_v2($1,$2,$3,$4,$5,$6)",
                upload_id,
                tenant_id,
                storage_key,
                claim_id,
                part_size_bytes,
                part_count,
            )
            if row is None:
                raise ArtifactConflictError("upload-session creation returned no claim")
            return _upload_session_creation_claim_from_row(row)
        except asyncpg.PostgresError as error:
            raise (
                self._translate(error)
                or ArtifactConflictError("upload-session creation could not be claimed")
            ) from None

    async def bind_upload_session(
        self,
        upload_id: UUID,
        *,
        tenant_id: str,
        storage_key: str,
        claim_id: UUID,
        provider_upload_id: str,
        part_size_bytes: int,
        part_count: int,
        initiated_at: datetime,
    ) -> ArtifactUploadSession:
        try:
            row = await self.pool.fetchrow(
                "SELECT * FROM fs2_scientific_bind_upload_session_v2($1,$2,$3,$4,$5,$6,$7,$8)",
                upload_id,
                tenant_id,
                storage_key,
                claim_id,
                provider_upload_id,
                part_size_bytes,
                part_count,
                initiated_at,
            )
            if row is None:
                raise ArtifactConflictError("upload-session routine returned no row")
            return _upload_session_from_row(row)
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("upload session could not be bound")) from None

    async def claim_stale_upload_session_creations(
        self, *, limit: int
    ) -> list[ArtifactUploadSessionCreationTarget]:
        rows = await self.pool.fetch(
            "SELECT * FROM fs2_scientific_claim_stale_upload_session_creations_v2($1)",
            min(max(1, limit), 500),
        )
        return [
            ArtifactUploadSessionCreationTarget(
                upload_id=row["upload_id"],
                tenant_id=row["tenant_id"],
                storage_key=row["storage_key"],
                claim_id=row["claim_id"],
                claim_generation=row["claim_generation"],
                claimed_at=row["claimed_at"],
            )
            for row in rows
        ]

    async def record_finalization_failure(
        self,
        request: FinalizeArtifactUpload,
        *,
        session: ArtifactUploadSession,
        lease: ArtifactFinalizationLease,
        verified: VerifiedStoredObject,
        failure_code: Literal["content_verification_failed", "artifact_policy_failed"],
    ) -> ArtifactFinalizationFailureEvidence:
        try:
            row = await self.pool.fetchrow(
                "SELECT * FROM fs2_scientific_record_finalization_failure_v2("
                "$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)",
                request.upload_id,
                request.operation_id,
                request.tenant_id,
                lease.lease_id,
                lease.lease_generation,
                session.session_generation,
                session.provider_upload_id,
                verified.provider_version_id,
                verified.provider_request_id,
                failure_code,
                verified.digest,
                verified.size_bytes,
                verified.media_type,
                verified.compression.value if verified.compression else None,
                _utc_now(),
            )
            if row is None:
                raise ArtifactConflictError("finalization-failure routine returned no evidence")
            return ArtifactFinalizationFailureEvidence(
                upload_id=row["upload_id"],
                tenant_id=row["tenant_id"],
                lease_id=row["lease_id"],
                lease_generation=row["lease_generation"],
                session_generation=row["session_generation"],
                provider_upload_id=row["provider_upload_id"],
                provider_version_id=row["provider_version_id"],
                provider_request_id=row["provider_request_id"],
                failure_code=row["failure_code"],
                observed_at=row["observed_at"],
            )
        except asyncpg.PostgresError as error:
            raise (
                self._translate(error)
                or ArtifactConflictError("artifact finalization failure was rejected")
            ) from None

    async def record_upload_session_creation_reconciled(
        self,
        target: ArtifactUploadSessionCreationTarget,
        evidence: ArtifactUploadSessionReconciliationEvidence,
    ) -> None:
        try:
            await self.pool.execute(
                "SELECT fs2_scientific_record_upload_session_creation_reconciled_v2("
                "$1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                target.upload_id,
                target.tenant_id,
                target.storage_key,
                target.claim_id,
                target.claim_generation,
                target.claimed_at,
                evidence.provider_request_id,
                evidence.aborted_upload_count,
                evidence.multipart_session_set_digest,
                evidence.observed_at,
            )
        except asyncpg.PostgresError as error:
            raise (
                self._translate(error)
                or ArtifactConflictError("upload-session reconciliation was rejected")
            ) from None

    async def get_upload_session(
        self, upload_id: UUID, *, tenant_id: str
    ) -> ArtifactUploadSession:
        row = await self.pool.fetchrow(
            f"SELECT {_UPLOAD_SESSION_COLUMNS} FROM fs2_scientific_artifact_upload_sessions "  # noqa: S608
            "WHERE upload_id=$1 AND tenant_id=$2",
            upload_id,
            tenant_id,
        )
        if row is None:
            raise ArtifactNotFoundError("upload session not found")
        return _upload_session_from_row(row)

    async def record_upload_session_aborted(
        self, session: ArtifactUploadSession, *, provider_request_id: str, observed_at: datetime
    ) -> ArtifactUploadSession:
        try:
            row = await self.pool.fetchrow(
                "SELECT * FROM fs2_scientific_record_upload_session_aborted_v2($1,$2,$3,$4,$5,$6)",
                session.upload_id,
                session.tenant_id,
                session.session_generation,
                session.provider_upload_id,
                provider_request_id,
                observed_at,
            )
            if row is None:
                raise ArtifactConflictError("upload-session abort routine returned no row")
            return _upload_session_from_row(row)
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("upload-session abort was rejected")) from None

    async def acquire_finalization_lease(
        self,
        request: FinalizeArtifactUpload,
        *,
        session_generation: int,
        lease_id: UUID,
    ) -> ArtifactFinalizationLease:
        try:
            row = await self.pool.fetchrow(
                "SELECT * FROM fs2_scientific_acquire_artifact_finalization_lease_v2($1,$2,$3,$4,$5)",
                request.upload_id,
                request.operation_id,
                request.tenant_id,
                session_generation,
                lease_id,
            )
            if row is None:
                raise ArtifactConflictError("finalization-lease routine returned no row")
            return _finalization_lease_from_row(row)
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("finalization fence is closed")) from None

    async def get_finalization_lease(
        self,
        request: FinalizeArtifactUpload,
        *,
        session_generation: int,
    ) -> ArtifactFinalizationLease | None:
        row = await self.pool.fetchrow(
            "SELECT lease_id,upload_id,tenant_id,lease_generation,session_generation,acquired_at,expires_at "
            "FROM fs2_scientific_artifact_finalization_leases "
            "WHERE upload_id=$1 AND tenant_id=$2 AND session_generation=$3 "
            "AND state='active' AND expires_at>clock_timestamp()",
            request.upload_id,
            request.tenant_id,
            session_generation,
        )
        return None if row is None else _finalization_lease_from_row(row)

    async def claim_expired_finalization_leases(
        self, *, limit: int
    ) -> list[ArtifactFinalizationRecoveryTarget]:
        rows = await self.pool.fetch(
            "SELECT * FROM fs2_scientific_claim_expired_finalization_leases_v2($1)",
            min(max(1, limit), 500),
        )
        return [
            ArtifactFinalizationRecoveryTarget(
                request=FinalizeArtifactUpload(
                    upload_id=row["upload_id"],
                    operation_id=row["operation_id"],
                    tenant_id=row["tenant_id"],
                ),
                session=ArtifactUploadSession(
                    upload_id=row["upload_id"],
                    tenant_id=row["tenant_id"],
                    storage_key=row["storage_key"],
                    provider_upload_id=row["provider_upload_id"],
                    session_generation=row["session_generation"],
                    part_size_bytes=row["part_size_bytes"],
                    part_count=row["part_count"],
                    provider_stability_grace_seconds=row[
                        "provider_stability_grace_seconds"
                    ],
                    initiated_at=row["initiated_at"],
                    state=row["session_state"],
                    provider_version_id=row["session_provider_version_id"],
                ),
                lease=ArtifactFinalizationLease(
                    lease_id=row["lease_id"],
                    upload_id=row["upload_id"],
                    tenant_id=row["tenant_id"],
                    lease_generation=row["lease_generation"],
                    session_generation=row["session_generation"],
                    acquired_at=row["acquired_at"],
                    expires_at=row["expires_at"],
                ),
            )
            for row in rows
        ]

    async def record_upload_capability(
        self,
        upload_id: UUID,
        *,
        tenant_id: str,
        capability_id: UUID,
        session_generation: int,
        part_number: int,
        size_bytes: int,
        checksum: str,
        media_type: str,
        compression: ArtifactCompression | None,
        expires_at: datetime,
    ) -> ArtifactQuotaReservation:
        try:
            row = await self.pool.fetchrow(
                "SELECT * FROM fs2_scientific_record_upload_capability_v2("
                "$1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                upload_id,
                tenant_id,
                capability_id,
                session_generation,
                part_number,
                size_bytes,
                checksum,
                media_type,
                compression.value if compression is not None else None,
                expires_at,
            )
            if row is None:
                raise ArtifactConflictError("upload capability routine returned no reservation")
            return _quota_reservation_from_row(row)
        except asyncpg.PostgresError as error:
            raise (
                self._translate(error)
                or ArtifactConflictError("upload capability could not be durably fenced")
            ) from None

    async def get_upload(self, request: FinalizeArtifactUpload) -> UploadIntent:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._tenant_quota_lock(connection, request.tenant_id)
            row = await connection.fetchrow(
                "SELECT upload.* FROM fs2_scientific_uploads upload "
                "JOIN fs2_scientific_artifact_quota_reservations reservation "
                "ON reservation.upload_id=upload.id "
                "WHERE upload.id=$1 AND upload.operation_id=$2 AND upload.tenant_id=$3 "
                "AND reservation.state='active' "
                "AND reservation.expires_at>clock_timestamp()",
                request.upload_id,
                request.operation_id,
                request.tenant_id,
            )
            if row is None:
                exists = await connection.fetchval(
                    "SELECT true FROM fs2_scientific_uploads WHERE id=$1 AND operation_id=$2 AND tenant_id=$3",
                    request.upload_id,
                    request.operation_id,
                    request.tenant_id,
                )
                if exists:
                    raise ArtifactConflictError("upload is fenced for or has completed provider removal")
                raise ArtifactNotFoundError("upload not found")
            return _upload_from_row(row)

    async def get_upload_status(self, request: FinalizeArtifactUpload) -> UploadIntent:
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

    async def get_leased_upload(
        self, request: FinalizeArtifactUpload, *, lease: ArtifactFinalizationLease
    ) -> UploadIntent:
        row = await self.pool.fetchrow(
            "SELECT upload.* FROM fs2_scientific_uploads upload "
            "JOIN fs2_scientific_artifact_quota_reservations reservation "
            "ON reservation.upload_id=upload.id "
            "JOIN fs2_scientific_artifact_finalization_leases lease ON lease.upload_id=upload.id "
            "WHERE upload.id=$1 AND upload.operation_id=$2 AND upload.tenant_id=$3 "
            "AND upload.artifact_id IS NULL AND reservation.state='active' "
            "AND lease.lease_id=$4 AND lease.lease_generation=$5 "
            "AND lease.session_generation=$6 AND lease.state='active'",
            request.upload_id,
            request.operation_id,
            request.tenant_id,
            lease.lease_id,
            lease.lease_generation,
            lease.session_generation,
        )
        if row is None:
            raise ArtifactConflictError("artifact finalization lease is stale")
        return _upload_from_row(row)

    async def quota_reservation(self, upload_id: UUID, *, tenant_id: str) -> ArtifactQuotaReservation:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._tenant_quota_lock(connection, tenant_id)
            row = await connection.fetchrow(
                f"SELECT {_QUOTA_RESERVATION_COLUMNS} "  # noqa: S608
                "FROM fs2_scientific_artifact_quota_reservations WHERE upload_id=$1 AND tenant_id=$2",
                upload_id,
                tenant_id,
            )
            if row is None:
                raise ArtifactNotFoundError("upload quota reservation not found")
            return _quota_reservation_from_row(row)

    async def list_quota_events(
        self, *, tenant_id: str, after_id: int = 0, limit: int = 500
    ) -> list[ArtifactQuotaEvent]:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._tenant_quota_lock(connection, tenant_id)
            rows = await connection.fetch(
                f"SELECT {_QUOTA_EVENT_COLUMNS} FROM fs2_scientific_artifact_quota_events "  # noqa: S608
                "WHERE tenant_id=$1 AND id>$2 ORDER BY id LIMIT $3",
                tenant_id,
                max(0, after_id),
                min(max(1, limit), 1000),
            )
            return [_quota_event_from_row(row) for row in rows]

    async def claim_expired_quota_removals(
        self, *, now: datetime, limit: int
    ) -> list[ArtifactRemovalTarget]:
        del now  # PostgreSQL is the eligibility clock authority.
        rows = await self.pool.fetch(
            """
            SELECT * FROM fs2_scientific_claim_artifact_removals_v2($1,NULL,NULL)
            """,
            min(max(1, limit), 500),
        )
        return [
            ArtifactRemovalTarget(
                upload_id=row["upload_id"],
                operation_id=row["operation_id"],
                attempt_id=row["attempt_id"],
                tenant_id=row["tenant_id"],
                storage_key=row["storage_key"],
                provider_upload_id=row["provider_upload_id"],
                upload_session_generation=row["upload_session_generation"],
                latest_upload_capability_expires_at=row["latest_upload_capability_expires_at"],
                removal_generation=row["removal_generation"],
                verification_generation=0,
                removal_claimed_at=row["removal_claimed_at"],
                eligible_at=row["eligible_at"],
            )
            for row in rows
        ]

    async def claim_quota_verifications(
        self, *, now: datetime, limit: int
    ) -> list[ArtifactRemovalTarget]:
        del now  # PostgreSQL is the eligibility and retry clock authority.
        rows = await self.pool.fetch(
            "SELECT * FROM fs2_scientific_claim_artifact_verifications_v2($1)",
            min(max(1, limit), 500),
        )
        return [
            ArtifactRemovalTarget(
                upload_id=row["upload_id"],
                operation_id=row["operation_id"],
                attempt_id=row["attempt_id"],
                tenant_id=row["tenant_id"],
                storage_key=row["storage_key"],
                provider_upload_id=row["provider_upload_id"],
                upload_session_generation=row["upload_session_generation"],
                latest_upload_capability_expires_at=row["latest_upload_capability_expires_at"],
                removal_generation=row["removal_generation"],
                verification_generation=row["verification_generation"],
                removal_claimed_at=row["removal_claimed_at"],
                verification_claimed_at=row["verification_claimed_at"],
                eligible_at=row["eligible_at"],
            )
            for row in rows
        ]

    async def record_quota_removal_completion(
        self, target: ArtifactRemovalTarget, evidence: ArtifactDeletionEvidence
    ) -> None:
        if target.storage_key != evidence.storage_key:
            raise ArtifactConflictError("provider deletion result addresses another key")
        try:
            await self.pool.execute(
                """
                SELECT fs2_scientific_record_artifact_removal_completion_v2(
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14
                )
                """,
                target.upload_id,
                target.operation_id,
                target.attempt_id,
                target.tenant_id,
                target.storage_key,
                target.removal_generation,
                target.removal_claimed_at,
                evidence.kind.value,
                evidence.provider_request_id,
                evidence.removed_version_count,
                evidence.aborted_upload_count,
                evidence.multipart_list_request_id,
                evidence.multipart_session_set_digest,
                evidence.observed_at,
            )
        except asyncpg.PostgresError as error:
            raise (
                self._translate(error)
                or ArtifactConflictError("provider deletion result was rejected")
            ) from None

    async def record_quota_verification_failure(self, target: ArtifactRemovalTarget) -> None:
        try:
            await self.pool.execute(
                """
                SELECT fs2_scientific_record_artifact_verification_failure_v2(
                    $1,$2,$3,$4,$5
                )
                """,
                target.upload_id,
                target.tenant_id,
                target.removal_generation,
                target.verification_generation,
                target.verification_claimed_at,
            )
        except asyncpg.PostgresError as error:
            raise (
                self._translate(error)
                or ArtifactConflictError("provider verification failure was rejected")
            ) from None

    async def record_quota_removal(
        self, target: ArtifactRemovalTarget, evidence: ArtifactRemovalEvidence
    ) -> ArtifactQuotaReservation:
        if target.storage_key != evidence.storage_key:
            raise ArtifactConflictError("provider removal evidence addresses another key")
        try:
            row = await self.pool.fetchrow(
                """
                SELECT * FROM fs2_scientific_record_artifact_removal_v2(
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
                    $19,$20,$21,$22
                )
                """,
                target.upload_id,
                target.operation_id,
                target.attempt_id,
                target.tenant_id,
                target.storage_key,
                evidence.kind.value,
                evidence.provider_request_id,
                evidence.removed_version_count,
                evidence.observed_at,
                evidence.latest_upload_capability_expires_at,
                evidence.removal_generation,
                evidence.verification_generation,
                evidence.first_list_request_id,
                evidence.head_request_id,
                evidence.second_list_request_id,
                evidence.first_version_set_digest,
                evidence.second_version_set_digest,
                evidence.first_multipart_list_request_id,
                evidence.second_multipart_list_request_id,
                evidence.first_multipart_session_set_digest,
                evidence.second_multipart_session_set_digest,
                evidence.claim_digest,
            )
            if row is None:
                raise ArtifactConflictError("quota removal routine returned no reservation")
            return _quota_reservation_from_row(row)
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("quota removal evidence was rejected")) from None

    async def claim_legacy_version_pins(self, *, limit: int) -> list[LegacyArtifactVersionTarget]:
        try:
            rows = await self.pool.fetch(
                "SELECT * FROM fs2_scientific_claim_legacy_artifact_versions_v2($1)",
                min(max(1, limit), 500),
            )
        except asyncpg.PostgresError as error:
            raise (
                self._translate(error)
                or ArtifactConflictError("legacy artifact version claims were rejected")
            ) from None
        return [
            LegacyArtifactVersionTarget(
                artifact_id=row["artifact_id"],
                upload_id=row["upload_id"],
                tenant_id=row["tenant_id"],
                storage_key=row["storage_key"],
                expected_digest=row["expected_digest"],
                expected_size_bytes=row["expected_size_bytes"],
                expected_media_type=row["expected_media_type"],
                expected_compression=(
                    ArtifactCompression(row["expected_compression"])
                    if row["expected_compression"]
                    else None
                ),
                claim_generation=row["claim_generation"],
                claimed_at=row["claimed_at"],
                eligible_at=row["eligible_at"],
                list_key_marker=row["list_key_marker"],
                list_version_id_marker=row["list_version_id_marker"],
            )
            for row in rows
        ]

    async def record_legacy_version_pin(
        self,
        target: LegacyArtifactVersionTarget,
        verified: VerifiedStoredObject,
        *,
        observed_at: datetime,
    ) -> None:
        try:
            row = await self.pool.fetchrow(
                "SELECT * FROM fs2_scientific_record_legacy_artifact_version_v2("
                "$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                target.artifact_id,
                target.upload_id,
                target.tenant_id,
                target.storage_key,
                target.claim_generation,
                target.claimed_at,
                verified.provider_version_id,
                verified.provider_request_id,
                verified.digest,
                verified.size_bytes,
                verified.media_type,
                verified.compression.value if verified.compression else None,
                observed_at,
            )
            if row is None:
                raise ArtifactConflictError("legacy artifact version routine returned no row")
        except asyncpg.PostgresError as error:
            raise (
                self._translate(error)
                or ArtifactConflictError("legacy artifact version evidence was rejected")
            ) from None

    async def record_legacy_version_scan(
        self, target: LegacyArtifactVersionTarget, scan: LegacyArtifactVersionScan
    ) -> None:
        if scan.verified is not None:
            raise ArtifactConflictError("a matched legacy version cannot be recorded as scan progress")
        try:
            await self.pool.execute(
                "SELECT fs2_scientific_record_legacy_artifact_version_scan_v2("
                "$1,$2,$3,$4,$5,$6,$7,$8,$9)",
                target.artifact_id,
                target.claim_generation,
                target.claimed_at,
                target.list_key_marker,
                target.list_version_id_marker,
                scan.next_key_marker,
                scan.next_version_id_marker,
                scan.provider_request_id,
                scan.observed_at,
            )
        except asyncpg.PostgresError as error:
            raise (
                self._translate(error)
                or ArtifactConflictError("legacy artifact version scan evidence was rejected")
            ) from None

    async def legacy_version_rollout_status(self) -> LegacyArtifactRolloutStatus:
        row = await self.pool.fetchrow(
            "SELECT * FROM fs2_scientific_legacy_version_rollout_status_v2()"
        )
        if row is None:
            raise ArtifactConflictError("legacy artifact rollout status returned no row")
        return LegacyArtifactRolloutStatus(
            pending=row["pending"],
            bound=row["bound"],
            unresolved=row["unresolved"],
            unbound_artifacts=row["unbound_artifacts"],
            missing_unfinished_upload_sessions=row["missing_unfinished_upload_sessions"],
        )

    async def mark_schema_bridge_ready(
        self,
        *,
        bridge_image_ref: str,
        bridge_release_revision: int,
        predecessor_image_ref: str,
        evidence: SchemaBridgeDrainEvidence,
    ) -> None:
        try:
            row = await self.pool.fetchrow(
                "SELECT * FROM fs2_scientific_mark_schema_bridge_ready_v2("
                "$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)",
                bridge_image_ref,
                bridge_release_revision,
                predecessor_image_ref,
                evidence.deployment_namespace,
                evidence.deployment_name,
                evidence.deployment_uid,
                evidence.deployment_generation,
                evidence.deployment_observed_generation,
                evidence.deployment_desired_replicas,
                evidence.deployment_updated_replicas,
                evidence.deployment_ready_replicas,
                evidence.deployment_available_replicas,
                evidence.runtime_pod_count,
                evidence.runtime_pod_set_digest,
                evidence.kubernetes_audit_id,
                evidence.kubernetes_observed_at,
            )
            if row is None:
                raise ArtifactConflictError("schema bridge receipt returned no row")
        except asyncpg.PostgresError as error:
            raise (
                self._translate(error)
                or ArtifactConflictError("schema bridge readiness was rejected")
            ) from None

    async def finalize_upload(
        self,
        request: FinalizeArtifactUpload,
        verified: VerifiedStoredObject,
        *,
        artifact_id: UUID,
        session: ArtifactUploadSession | None,
        lease: ArtifactFinalizationLease,
    ) -> ArtifactRecord:
        if session is None:
            raise ArtifactConflictError("artifact publication requires a provider session")
        try:
            row = await self.pool.fetchrow(
                "SELECT * FROM fs2_scientific_publish_artifact_v2("
                "$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)",
                request.upload_id,
                request.operation_id,
                request.tenant_id,
                artifact_id,
                lease.lease_id,
                lease.lease_generation,
                session.session_generation,
                session.provider_upload_id,
                verified.provider_version_id,
                verified.provider_request_id,
                verified.digest,
                verified.size_bytes,
                verified.media_type,
                verified.compression.value if verified.compression else None,
                self._clock(),
            )
            if row is None:
                raise ArtifactConflictError("artifact publication routine returned no row")
            return _artifact_from_row(row)
        except asyncpg.PostgresError as error:
            raise (self._translate(error) or ArtifactConflictError("artifact could not be published")) from None

    async def get_artifact(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactRecord:
        row = await self.pool.fetchrow(
            f"SELECT {_ARTIFACT_COLUMNS} FROM fs2_scientific_artifacts_versioned "  # noqa: S608
            "WHERE id=$1 AND tenant_id=$2",
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
            SELECT {_ARTIFACT_COLUMNS} FROM fs2_scientific_artifacts_versioned
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
                        f"SELECT {_ARTIFACT_COLUMNS} FROM fs2_scientific_artifacts_versioned "  # noqa: S608
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

    async def purge_keys(self, operation_id: UUID, *, tenant_id: str) -> list[ArtifactRemovalTarget]:
        rows: list[Mapping[str, Any]] = []
        while len(rows) < 500:
            claimed = await self.pool.fetch(
                "SELECT * FROM fs2_scientific_claim_artifact_removals_v2($1,$2,$3)",
                500 - len(rows),
                operation_id,
                tenant_id,
            )
            if not claimed:
                break
            rows.extend(claimed)
        return [
            ArtifactRemovalTarget(
                upload_id=row["upload_id"],
                operation_id=row["operation_id"],
                attempt_id=row["attempt_id"],
                tenant_id=row["tenant_id"],
                storage_key=row["storage_key"],
                latest_upload_capability_expires_at=row["latest_upload_capability_expires_at"],
                removal_generation=row["removal_generation"],
                verification_generation=0,
                removal_claimed_at=row["removal_claimed_at"],
                eligible_at=row["eligible_at"],
            )
            for row in rows
        ]

    async def purge_operation(self, operation_id: UUID, *, tenant_id: str, now: datetime) -> RetentionPurge:
        """Delete retired rows under the one session flag the triggers accept."""

        try:
            async with self.pool.acquire() as connection, connection.transaction():
                await connection.execute("SET LOCAL fs2.retention_purge = 'on'")
                await self._tenant_quota_lock(connection, tenant_id)
                result = await connection.fetchrow(
                    "SELECT retention_expires_at FROM fs2_scientific_run_results "
                    "WHERE operation_id=$1 AND tenant_id=$2",
                    operation_id,
                    tenant_id,
                )
                if result is None:
                    raise ArtifactNotFoundError("terminal result not found")
                pending_removals = await connection.fetchval(
                    """
                    SELECT count(*) FROM fs2_scientific_artifact_quota_reservations
                    WHERE operation_id=$1 AND tenant_id=$2 AND state<>'released'
                    """,
                    operation_id,
                    tenant_id,
                )
                if pending_removals:
                    raise ArtifactConflictError("provider removal evidence is incomplete")
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
