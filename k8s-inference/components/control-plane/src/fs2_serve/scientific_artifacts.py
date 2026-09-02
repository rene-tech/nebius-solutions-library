"""Durable, payload-free scientific artifact and result-manifest service.

The module deliberately has no HTTP, MCP, Kubernetes, or model-adapter wiring.
Object-store implementations expose only independently measured metadata to
``finalize_upload``; biological bytes and signed handles never cross the
repository boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
import httpx
from pydantic import AwareDatetime, ConfigDict, Field, StringConstraints, model_validator

from .models import StrictModel

ARTIFACT_RECORD_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-record/v1"
SCIENTIFIC_RUN_RESULT_SCHEMA = "fs2-serve.nebius.ai/scientific-run-result/v1"
SCIENTIFIC_ARTIFACT_MIGRATION = "0014_scientific_artifact_results.sql"
MAX_ARTIFACT_BYTES = 1 << 40
MAX_HANDLE_TTL = timedelta(minutes=15)

SHA256_PATTERN = r"^sha256:[a-f0-9]{64}$"
TENANT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
SAFE_ID_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9_.:@/+\-]*[A-Za-z0-9])?$"
MEDIA_TYPE_PATTERN = r"^[a-z0-9][a-z0-9.+\-]*/[A-Za-z0-9][A-Za-z0-9.+_\-]*$"

TenantId = Annotated[str, StringConstraints(min_length=1, max_length=120, pattern=TENANT_PATTERN)]
SafeId = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=SAFE_ID_PATTERN)]
Sha256Digest = Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]
MediaType = Annotated[str, StringConstraints(min_length=3, max_length=128, pattern=MEDIA_TYPE_PATTERN)]


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


class ArtifactDirection(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class ArtifactCompression(StrEnum):
    GZIP = "gzip"
    ZSTD = "zstd"


class ArtifactAccessProfile(StrEnum):
    PUBLIC = "public"
    RESTRICTED = "restricted"
    ACADEMIC = "academic"


class ArtifactEventType(StrEnum):
    UPLOAD_BEGUN = "upload_begun"
    ARTIFACT_FINALIZED = "artifact_finalized"
    RESULT_COMMITTED = "result_committed"


class TerminalResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PREEMPTED = "preempted"
    EXPIRED = "expired"


class SemanticValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


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


class BeginArtifactUpload(ScientificArtifactModel):
    """Idempotent upload intent; callers reuse ``upload_id`` after timeouts."""

    upload_id: UUID
    operation_id: UUID
    tenant_id: TenantId
    attempt: int = Field(ge=0, le=10)
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
    attempt: int = Field(ge=0, le=10)


class VerifiedStoredObject(ScientificArtifactModel):
    """Metadata independently measured by the trusted object-store adapter."""

    storage_key: str = Field(min_length=1, max_length=1024)
    digest: Sha256Digest
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    media_type: MediaType
    compression: ArtifactCompression | None = None


class ArtifactRefProjection(ScientificArtifactModel):
    """Schema-neutral value matching the shared public ArtifactRef fields."""

    artifact_id: UUID
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    media_type: MediaType
    compression: ArtifactCompression | None = None


class ArtifactRecord(ScientificArtifactModel):
    """Internal persisted content address and attempt-scoped storage identity."""

    schema_version: Literal["fs2-serve.nebius.ai/scientific-artifact-record/v1"] = (
        "fs2-serve.nebius.ai/scientific-artifact-record/v1"
    )
    artifact_id: UUID
    operation_id: UUID
    tenant_id: TenantId
    attempt: int = Field(ge=0, le=10)
    direction: ArtifactDirection
    digest: Sha256Digest
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    media_type: MediaType
    compression: ArtifactCompression | None = None
    storage_key: str = Field(min_length=1, max_length=1024)
    access: ArtifactAccess
    created_at: AwareDatetime

    @model_validator(mode="after")
    def content_address_matches_scope(self) -> ArtifactRecord:
        expected = artifact_storage_key(
            tenant_id=self.tenant_id,
            operation_id=self.operation_id,
            attempt=self.attempt,
            direction=self.direction,
            digest=self.digest,
        )
        if self.storage_key != expected:
            raise ValueError("artifact storage key is not the canonical attempt-scoped content address")
        return self

    def to_public_ref(self) -> ArtifactRefProjection:
        """Drop every persistence/fencing/location field at the public boundary."""

        return ArtifactRefProjection(
            artifact_id=self.artifact_id,
            sha256=self.digest.removeprefix("sha256:"),
            size_bytes=self.size_bytes,
            media_type=self.media_type,
            compression=self.compression,
        )


class UploadIntent(ScientificArtifactModel):
    upload_id: UUID
    operation_id: UUID
    tenant_id: TenantId
    attempt: int = Field(ge=0, le=10)
    direction: ArtifactDirection
    expected_digest: Sha256Digest
    expected_size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    media_type: MediaType
    compression: ArtifactCompression | None = None
    storage_key: str = Field(min_length=1, max_length=1024)
    access: ArtifactAccess
    begun_at: AwareDatetime
    finalized_at: AwareDatetime | None = None
    artifact: ArtifactRecord | None = None

    @model_validator(mode="after")
    def state_and_scope_are_consistent(self) -> UploadIntent:
        expected = artifact_storage_key(
            tenant_id=self.tenant_id,
            operation_id=self.operation_id,
            attempt=self.attempt,
            direction=self.direction,
            digest=self.expected_digest,
        )
        if self.storage_key != expected:
            raise ValueError("upload storage key is not canonical")
        if (self.finalized_at is None) != (self.artifact is None):
            raise ValueError("upload finalization state is incomplete")
        if self.artifact is not None:
            artifact = self.artifact
            if (
                artifact.operation_id != self.operation_id
                or artifact.tenant_id != self.tenant_id
                or artifact.attempt != self.attempt
                or artifact.direction is not self.direction
                or artifact.digest != self.expected_digest
                or artifact.size_bytes != self.expected_size_bytes
                or artifact.media_type != self.media_type
                or artifact.compression != self.compression
                or artifact.storage_key != self.storage_key
                or artifact.access != self.access
            ):
                raise ValueError("finalized artifact differs from its upload intent")
        return self


class ExecutionProvenance(ScientificArtifactModel):
    """Bounded execution identity; payloads, argv, environment, and credentials are absent."""

    model_id: SafeId
    model_revision: SafeId
    runtime_image_digest: Sha256Digest
    workload_spec_digest: Sha256Digest
    scheduling_snapshot_digest: Sha256Digest
    job_uid: SafeId
    pod_uids: tuple[SafeId, ...] = Field(min_length=1, max_length=64)
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def execution_is_consistent(self) -> ExecutionProvenance:
        if self.completed_at < self.started_at:
            raise ValueError("execution completion precedes its start")
        if len(self.pod_uids) != len(set(self.pod_uids)):
            raise ValueError("execution Pod identities must be unique")
        return self


class SemanticValidation(ScientificArtifactModel):
    validator_id: SafeId
    validator_revision: Sha256Digest
    status: SemanticValidationStatus
    evidence_artifact: ArtifactRecord | None = None

    @model_validator(mode="after")
    def evidence_matches_status(self) -> SemanticValidation:
        if self.status is SemanticValidationStatus.PASSED and self.evidence_artifact is None:
            raise ValueError("passed semantic validation requires a content-addressed evidence artifact")
        if self.status is SemanticValidationStatus.NOT_RUN and self.evidence_artifact is not None:
            raise ValueError("semantic validation that did not run cannot have evidence")
        return self


class TerminalResultDraft(ScientificArtifactModel):
    operation_id: UUID
    tenant_id: TenantId
    attempt: int = Field(ge=0, le=10)
    status: TerminalResultStatus
    input_artifacts: tuple[ArtifactRecord, ...] = Field(default=(), max_length=256)
    output_artifacts: tuple[ArtifactRecord, ...] = Field(default=(), max_length=256)
    provenance: ExecutionProvenance
    validation: SemanticValidation
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def references_are_scoped_and_validated(self) -> TerminalResultDraft:
        all_artifacts = (*self.input_artifacts, *self.output_artifacts)
        if len({item.artifact_id for item in all_artifacts}) != len(all_artifacts):
            raise ValueError("terminal result contains duplicate artifact references")
        for expected_direction, artifacts in (
            (ArtifactDirection.INPUT, self.input_artifacts),
            (ArtifactDirection.OUTPUT, self.output_artifacts),
        ):
            for artifact in artifacts:
                if (
                    artifact.operation_id != self.operation_id
                    or artifact.tenant_id != self.tenant_id
                    or artifact.attempt != self.attempt
                    or artifact.direction is not expected_direction
                ):
                    raise ValueError("terminal result contains an artifact outside its fenced attempt")
        evidence = self.validation.evidence_artifact
        if evidence is not None and evidence not in self.output_artifacts:
            raise ValueError("semantic evidence must be one of the committed output artifacts")
        if self.completed_at != self.provenance.completed_at:
            raise ValueError("result and execution completion times differ")
        if self.status is TerminalResultStatus.SUCCEEDED:
            if not self.output_artifacts:
                raise ValueError("successful result requires at least one output artifact")
            if self.validation.status is not SemanticValidationStatus.PASSED:
                raise ValueError("successful result requires passed semantic validation")
        return self


class TerminalResultManifest(TerminalResultDraft):
    """Internal persisted projection of canonical scientific-run-result/v1."""
    schema: Literal["fs2-serve.nebius.ai/scientific-run-result/v1"] = SCIENTIFIC_RUN_RESULT_SCHEMA
    manifest_digest: Sha256Digest
    committed_at: AwareDatetime

    @property
    def schema_version(self) -> str:
        """Backward-compatible accessor; only canonical ``schema`` is persisted."""
        return self.schema

    @model_validator(mode="after")
    def digest_matches_manifest(self) -> TerminalResultManifest:
        if self.committed_at < self.completed_at:
            raise ValueError("terminal result was committed before it completed")
        if self.manifest_digest != result_manifest_digest(self):
            raise ValueError("terminal result manifest digest does not match its canonical body")
        return self


class ArtifactEvent(ScientificArtifactModel):
    """Closed, payload-free durable event; there is intentionally no detail map."""

    event_id: int = Field(ge=1)
    event_type: ArtifactEventType
    operation_id: UUID
    tenant_id: TenantId
    attempt: int = Field(ge=0, le=10)
    upload_id: UUID | None = None
    artifact_id: UUID | None = None
    manifest_digest: Sha256Digest | None = None
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def identity_matches_event(self) -> ArtifactEvent:
        if self.event_type is ArtifactEventType.UPLOAD_BEGUN:
            valid = self.upload_id is not None and self.artifact_id is None and self.manifest_digest is None
        elif self.event_type is ArtifactEventType.ARTIFACT_FINALIZED:
            valid = self.upload_id is not None and self.artifact_id is not None and self.manifest_digest is None
        else:
            valid = self.upload_id is None and self.artifact_id is None and self.manifest_digest is not None
        if not valid:
            raise ValueError("artifact event has an invalid identity shape")
        return self


class ArtifactObjectStore(Protocol):
    async def inspect(self, storage_key: str) -> VerifiedStoredObject:
        """Hash and size the stored bytes without returning them to this service."""


class ArtifactHandleSigner(Protocol):
    async def issue_upload(
        self,
        *,
        storage_key: str,
        media_type: str,
        compression: ArtifactCompression | None,
        expires_at: datetime,
    ) -> EphemeralHandle: ...

    async def issue_download(self, *, storage_key: str, expires_at: datetime) -> EphemeralHandle: ...


class S3CompatibleArtifactObjectStore:
    """Same-region S3-compatible store adapter used by the live runtime."""
    def __init__(self, *, endpoint: str, bucket: str, region: str, access_key: str, secret_key: str) -> None:
        self.endpoint, self.bucket, self.region = endpoint.rstrip("/"), bucket, region
        self.access_key, self.secret_key = access_key, secret_key

    async def inspect(self, storage_key: str) -> VerifiedStoredObject:
        url = f"{self.endpoint}/{self.bucket}/{storage_key}"
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.content
        return VerifiedStoredObject(
            digest="sha256:" + hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            media_type=response.headers.get("content-type", "application/octet-stream").split(";", 1)[0],
            compression=None,
        )


class S3CompatibleArtifactHandleSigner:
    """Short-lived signed-handle provider; credentials stay process-local."""
    def __init__(self, *, endpoint: str, bucket: str, region: str, access_key: str, secret_key: str) -> None:
        self.endpoint, self.bucket, self.region = endpoint.rstrip("/"), bucket, region
        self.access_key, self.secret_key = access_key, secret_key

    def _handle(self, method: Literal["GET", "PUT"], storage_key: str, expires_at: datetime, media_type: str | None = None) -> EphemeralHandle:
        # The object-store gateway validates these short-lived query credentials.
        expiry = int(expires_at.timestamp())
        url = f"{self.endpoint}/{self.bucket}/{storage_key}?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential={self.access_key}%2F{self.region}&X-Amz-Expires={max(1, expiry-int(datetime.now(UTC).timestamp()))}"
        return EphemeralHandle(method=method, url=url, expires_at=expires_at, write_once=method == "PUT", headers={"content-type": media_type} if media_type else {})

    async def issue_upload(self, *, storage_key: str, media_type: str, compression: ArtifactCompression | None, expires_at: datetime) -> EphemeralHandle:
        return self._handle("PUT", storage_key, expires_at, media_type)

    async def issue_download(self, *, storage_key: str, expires_at: datetime) -> EphemeralHandle:
        return self._handle("GET", storage_key, expires_at)


class ArtifactRepository(Protocol):
    async def begin_upload(self, request: BeginArtifactUpload, storage_key: str) -> UploadIntent: ...

    async def get_upload(self, request: FinalizeArtifactUpload) -> UploadIntent: ...

    async def finalize_upload(
        self,
        request: FinalizeArtifactUpload,
        verified: VerifiedStoredObject,
        *,
        artifact_id: UUID,
    ) -> ArtifactRecord: ...

    async def get_artifact(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactRecord: ...

    async def commit_terminal_result(self, manifest: TerminalResultManifest) -> TerminalResultManifest: ...

    async def get_terminal_result(self, operation_id: UUID, *, tenant_id: str) -> TerminalResultManifest: ...

    async def list_events(self, operation_id: UUID, *, tenant_id: str) -> list[ArtifactEvent]: ...


class ScientificArtifactControllerPort(Protocol):
    """Stable controller port; implementations never expose persistence records publicly."""

    async def begin_upload(
        self,
        request: BeginArtifactUpload,
        *,
        handle_ttl: timedelta | None = None,
    ) -> BeginUploadResult: ...

    async def finalize_upload(self, request: FinalizeArtifactUpload) -> ArtifactRecord: ...

    async def commit_terminal_result(self, draft: TerminalResultDraft) -> TerminalResultManifest: ...

    async def download(
        self,
        artifact_id: UUID,
        *,
        tenant_id: str,
        handle_ttl: timedelta | None = None,
    ) -> ArtifactDownload: ...


@dataclass(frozen=True, slots=True)
class EphemeralHandle:
    """Short-lived secret returned to a caller and excluded from object repr."""

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


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _manifest_body(value: TerminalResultDraft | TerminalResultManifest) -> dict[str, Any]:
    body = value.model_dump(mode="json", exclude={"manifest_digest", "committed_at"})
    body.setdefault("schema", SCIENTIFIC_RUN_RESULT_SCHEMA)
    return body


def result_manifest_digest(value: TerminalResultDraft | TerminalResultManifest) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(_manifest_body(value))).hexdigest()


def build_terminal_manifest(
    draft: TerminalResultDraft,
    *,
    committed_at: datetime,
) -> TerminalResultManifest:
    body = draft.model_dump()
    return TerminalResultManifest(
        **body,
        manifest_digest=result_manifest_digest(draft),
        committed_at=committed_at,
    )


def artifact_storage_key(
    *,
    tenant_id: str,
    operation_id: UUID,
    attempt: int,
    direction: ArtifactDirection,
    digest: str,
) -> str:
    """Return the only accepted attempt-scoped, content-addressed object key."""

    if re.fullmatch(TENANT_PATTERN, tenant_id) is None or len(tenant_id) > 120:
        raise ValueError("tenant identity is not canonical")
    if not 0 <= attempt <= 10:
        raise ValueError("artifact attempt is outside the supported range")
    if re.fullmatch(SHA256_PATTERN, digest) is None:
        raise ValueError("artifact digest is not canonical")
    return (
        f"scientific/v1/tenants/{tenant_id}/operations/{operation_id}/attempts/{attempt}/"
        f"{direction.value}/sha256/{digest.removeprefix('sha256:')}"
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_handle(
    handle: EphemeralHandle,
    *,
    method: Literal["GET", "PUT"],
    now: datetime,
    deadline: datetime,
) -> None:
    parsed = urlsplit(handle.url)
    if (
        handle.method != method
        or (method == "PUT" and not handle.write_once)
        or (method == "GET" and handle.write_once)
        or handle.expires_at.tzinfo is None
        or not now < handle.expires_at <= deadline
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(
            not isinstance(key, str) or not key or not isinstance(value, str) for key, value in handle.headers.items()
        )
    ):
        raise ArtifactPolicyError("artifact handle violates the short-lived HTTPS policy")


def _same_upload_request(intent: UploadIntent, request: BeginArtifactUpload, storage_key: str) -> bool:
    return (
        intent.upload_id == request.upload_id
        and intent.operation_id == request.operation_id
        and intent.tenant_id == request.tenant_id
        and intent.attempt == request.attempt
        and intent.direction is request.direction
        and intent.expected_digest == request.expected_digest
        and intent.expected_size_bytes == request.expected_size_bytes
        and intent.media_type == request.media_type
        and intent.compression == request.compression
        and intent.storage_key == storage_key
        and intent.access == request.access
    )


def _verify_object(intent: UploadIntent, verified: VerifiedStoredObject) -> None:
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


class ScientificArtifactService:
    """Coordinates verified storage, ephemeral handles, and durable metadata."""

    def __init__(
        self,
        *,
        repository: ArtifactRepository,
        object_store: ArtifactObjectStore,
        signer: ArtifactHandleSigner,
        allowed_media_types: set[str] | frozenset[str],
        max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
        max_handle_ttl: timedelta = MAX_HANDLE_TTL,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not allowed_media_types or any(
            re.fullmatch(MEDIA_TYPE_PATTERN, item) is None for item in allowed_media_types
        ):
            raise ValueError("allowed media types must be a non-empty exact allowlist")
        if not 0 <= max_artifact_bytes <= MAX_ARTIFACT_BYTES:
            raise ValueError("maximum artifact size is outside the supported range")
        if not timedelta(seconds=1) <= max_handle_ttl <= MAX_HANDLE_TTL:
            raise ValueError("maximum handle TTL is outside the supported range")
        self.repository = repository
        self.object_store = object_store
        self.signer = signer
        self.allowed_media_types = frozenset(allowed_media_types)
        self.max_artifact_bytes = max_artifact_bytes
        self.max_handle_ttl = max_handle_ttl
        self.clock = clock

    def _check_policy(self, media_type: str, size_bytes: int) -> None:
        if media_type not in self.allowed_media_types:
            raise ArtifactPolicyError("artifact media type is not allowlisted")
        if size_bytes > self.max_artifact_bytes:
            raise ArtifactPolicyError("artifact exceeds the configured size bound")

    def _deadline(self, ttl: timedelta | None) -> tuple[datetime, datetime]:
        now = self.clock()
        requested = ttl or self.max_handle_ttl
        if now.tzinfo is None or not timedelta(seconds=1) <= requested <= self.max_handle_ttl:
            raise ArtifactPolicyError("artifact handle TTL is outside the configured range")
        return now, now + requested

    async def begin_upload(
        self,
        request: BeginArtifactUpload,
        *,
        handle_ttl: timedelta | None = None,
    ) -> BeginUploadResult:
        self._check_policy(request.media_type, request.expected_size_bytes)
        storage_key = artifact_storage_key(
            tenant_id=request.tenant_id,
            operation_id=request.operation_id,
            attempt=request.attempt,
            direction=request.direction,
            digest=request.expected_digest,
        )
        upload = await self.repository.begin_upload(request, storage_key)
        if upload.artifact is not None:
            raise ArtifactConflictError("upload is already finalized")
        now, deadline = self._deadline(handle_ttl)
        try:
            handle = await self.signer.issue_upload(
                storage_key=upload.storage_key,
                media_type=upload.media_type,
                compression=upload.compression,
                expires_at=deadline,
            )
        except Exception:
            raise ArtifactPolicyError("artifact handle generation failed") from None
        _validate_handle(handle, method="PUT", now=now, deadline=deadline)
        return BeginUploadResult(upload=upload, handle=handle)

    async def finalize_upload(self, request: FinalizeArtifactUpload) -> ArtifactRecord:
        upload = await self.repository.get_upload(request)
        self._check_policy(upload.media_type, upload.expected_size_bytes)
        try:
            verified = await self.object_store.inspect(upload.storage_key)
        except Exception:
            raise ArtifactVerificationError("stored object inspection failed") from None
        if not isinstance(verified, VerifiedStoredObject):
            raise ArtifactVerificationError("stored object inspection returned invalid metadata")
        _verify_object(upload, verified)
        return await self.repository.finalize_upload(request, verified, artifact_id=uuid4())

    async def download(
        self,
        artifact_id: UUID,
        *,
        tenant_id: str,
        handle_ttl: timedelta | None = None,
    ) -> ArtifactDownload:
        artifact = await self.repository.get_artifact(artifact_id, tenant_id=tenant_id)
        now, deadline = self._deadline(handle_ttl)
        try:
            handle = await self.signer.issue_download(storage_key=artifact.storage_key, expires_at=deadline)
        except Exception:
            raise ArtifactPolicyError("artifact handle generation failed") from None
        _validate_handle(handle, method="GET", now=now, deadline=deadline)
        return ArtifactDownload(artifact=artifact, handle=handle)

    async def commit_terminal_result(self, draft: TerminalResultDraft) -> TerminalResultManifest:
        manifest = build_terminal_manifest(draft, committed_at=self.clock())
        return await self.repository.commit_terminal_result(manifest)

    async def get_terminal_result(self, operation_id: UUID, *, tenant_id: str) -> TerminalResultManifest:
        return await self.repository.get_terminal_result(operation_id, tenant_id=tenant_id)


@dataclass(slots=True)
class _MemoryOperation:
    tenant_id: str
    attempt: int


class MemoryArtifactRepository:
    """Deterministic repository used for adapter and concurrency tests."""

    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._operations: dict[UUID, _MemoryOperation] = {}
        self._uploads: dict[UUID, UploadIntent] = {}
        self._uploads_by_key: dict[tuple[UUID, int, str], UUID] = {}
        self._artifacts: dict[UUID, ArtifactRecord] = {}
        self._artifacts_by_key: dict[tuple[UUID, int, str], UUID] = {}
        self._manifests: dict[UUID, TerminalResultManifest] = {}
        self._events: list[ArtifactEvent] = []

    async def register_operation(self, operation_id: UUID, *, tenant_id: str, attempt: int = 0) -> None:
        if re.fullmatch(TENANT_PATTERN, tenant_id) is None or not 1 <= len(tenant_id) <= 120:
            raise ValueError("tenant identity is not canonical")
        if not 0 <= attempt <= 10:
            raise ValueError("operation attempt is outside the supported range")
        async with self._lock:
            if operation_id in self._operations:
                raise ArtifactConflictError("operation is already registered")
            self._operations[operation_id] = _MemoryOperation(tenant_id=tenant_id, attempt=attempt)

    async def advance_attempt(self, operation_id: UUID, *, attempt: int) -> None:
        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ArtifactNotFoundError("operation does not exist")
            if attempt <= operation.attempt or attempt > 10:
                raise ValueError("operation attempt must advance monotonically")
            operation.attempt = attempt

    def _assert_current(self, operation_id: UUID, tenant_id: str, attempt: int) -> None:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise ArtifactNotFoundError("operation does not exist")
        if operation.tenant_id != tenant_id:
            raise ArtifactNotFoundError("operation does not exist")
        if operation.attempt != attempt:
            raise StaleArtifactAttemptError()

    def _append_event(
        self,
        event_type: ArtifactEventType,
        *,
        operation_id: UUID,
        tenant_id: str,
        attempt: int,
        upload_id: UUID | None = None,
        artifact_id: UUID | None = None,
        manifest_digest: str | None = None,
    ) -> None:
        self._events.append(
            ArtifactEvent(
                event_id=len(self._events) + 1,
                event_type=event_type,
                operation_id=operation_id,
                tenant_id=tenant_id,
                attempt=attempt,
                upload_id=upload_id,
                artifact_id=artifact_id,
                manifest_digest=manifest_digest,
                occurred_at=self._clock(),
            )
        )

    async def begin_upload(self, request: BeginArtifactUpload, storage_key: str) -> UploadIntent:
        async with self._lock:
            self._assert_current(request.operation_id, request.tenant_id, request.attempt)
            current = self._uploads.get(request.upload_id)
            if current is not None:
                if not _same_upload_request(current, request, storage_key):
                    raise ArtifactConflictError("upload ID was already used for a different intent")
                return current.model_copy(deep=True)
            key = (request.operation_id, request.attempt, storage_key)
            if key in self._uploads_by_key:
                raise ArtifactConflictError("content address already has a different upload ID")
            intent = UploadIntent(
                **request.model_dump(),
                storage_key=storage_key,
                begun_at=self._clock(),
            )
            self._uploads[request.upload_id] = intent
            self._uploads_by_key[key] = request.upload_id
            self._append_event(
                ArtifactEventType.UPLOAD_BEGUN,
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
                attempt=request.attempt,
                upload_id=request.upload_id,
            )
            return intent.model_copy(deep=True)

    async def get_upload(self, request: FinalizeArtifactUpload) -> UploadIntent:
        async with self._lock:
            self._assert_current(request.operation_id, request.tenant_id, request.attempt)
            intent = self._uploads.get(request.upload_id)
            if intent is None:
                raise ArtifactNotFoundError("upload does not exist")
            if (
                intent.operation_id != request.operation_id
                or intent.tenant_id != request.tenant_id
                or intent.attempt != request.attempt
            ):
                raise ArtifactNotFoundError("upload does not exist")
            return intent.model_copy(deep=True)

    async def finalize_upload(
        self,
        request: FinalizeArtifactUpload,
        verified: VerifiedStoredObject,
        *,
        artifact_id: UUID,
    ) -> ArtifactRecord:
        async with self._lock:
            self._assert_current(request.operation_id, request.tenant_id, request.attempt)
            intent = self._uploads.get(request.upload_id)
            if intent is None or (
                intent.operation_id != request.operation_id
                or intent.tenant_id != request.tenant_id
                or intent.attempt != request.attempt
            ):
                raise ArtifactNotFoundError("upload does not exist")
            _verify_object(intent, verified)
            if intent.artifact is not None:
                return intent.artifact.model_copy(deep=True)
            artifact_key = (intent.operation_id, intent.attempt, intent.storage_key)
            existing_id = self._artifacts_by_key.get(artifact_key)
            if existing_id is None:
                artifact = ArtifactRecord(
                    artifact_id=artifact_id,
                    operation_id=intent.operation_id,
                    tenant_id=intent.tenant_id,
                    attempt=intent.attempt,
                    direction=intent.direction,
                    digest=verified.digest,
                    size_bytes=verified.size_bytes,
                    media_type=verified.media_type,
                    compression=verified.compression,
                    storage_key=verified.storage_key,
                    access=intent.access,
                    created_at=self._clock(),
                )
                self._artifacts[artifact.artifact_id] = artifact
                self._artifacts_by_key[artifact_key] = artifact.artifact_id
            else:
                artifact = self._artifacts[existing_id]
                if (
                    artifact.direction is not intent.direction
                    or artifact.digest != verified.digest
                    or artifact.size_bytes != verified.size_bytes
                    or artifact.media_type != verified.media_type
                    or artifact.compression != verified.compression
                    or artifact.access != intent.access
                ):
                    raise ArtifactConflictError("content address already has different immutable metadata")
            finalized_at = self._clock()
            self._uploads[request.upload_id] = intent.model_copy(
                update={"artifact": artifact, "finalized_at": finalized_at},
                deep=True,
            )
            self._append_event(
                ArtifactEventType.ARTIFACT_FINALIZED,
                operation_id=intent.operation_id,
                tenant_id=intent.tenant_id,
                attempt=intent.attempt,
                upload_id=intent.upload_id,
                artifact_id=artifact.artifact_id,
            )
            return artifact.model_copy(deep=True)

    async def get_artifact(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactRecord:
        async with self._lock:
            artifact = self._artifacts.get(artifact_id)
            if artifact is None or artifact.tenant_id != tenant_id:
                raise ArtifactNotFoundError("artifact does not exist")
            return artifact.model_copy(deep=True)

    def _validate_manifest_artifacts(self, manifest: TerminalResultManifest) -> None:
        refs = [*manifest.input_artifacts, *manifest.output_artifacts]
        for reference in refs:
            stored = self._artifacts.get(reference.artifact_id)
            if stored is None or stored != reference:
                raise ArtifactConflictError("result manifest references an unknown or changed artifact")

    async def commit_terminal_result(self, manifest: TerminalResultManifest) -> TerminalResultManifest:
        async with self._lock:
            self._assert_current(manifest.operation_id, manifest.tenant_id, manifest.attempt)
            self._validate_manifest_artifacts(manifest)
            current = self._manifests.get(manifest.operation_id)
            if current is not None:
                if current.manifest_digest != manifest.manifest_digest:
                    raise ArtifactConflictError("operation already has a different terminal result")
                return current.model_copy(deep=True)
            self._manifests[manifest.operation_id] = manifest
            self._append_event(
                ArtifactEventType.RESULT_COMMITTED,
                operation_id=manifest.operation_id,
                tenant_id=manifest.tenant_id,
                attempt=manifest.attempt,
                manifest_digest=manifest.manifest_digest,
            )
            return manifest.model_copy(deep=True)

    async def get_terminal_result(self, operation_id: UUID, *, tenant_id: str) -> TerminalResultManifest:
        async with self._lock:
            operation = self._operations.get(operation_id)
            manifest = self._manifests.get(operation_id)
            if operation is None or operation.tenant_id != tenant_id or manifest is None:
                raise ArtifactNotFoundError("terminal result does not exist")
            return manifest.model_copy(deep=True)

    async def list_events(self, operation_id: UUID, *, tenant_id: str) -> list[ArtifactEvent]:
        async with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None or operation.tenant_id != tenant_id:
                raise ArtifactNotFoundError("operation does not exist")
            return [
                event.model_copy(deep=True)
                for event in self._events
                if event.operation_id == operation_id and event.tenant_id == tenant_id
            ]


def _decode_json_object(value: object, *, label: str, maximum_bytes: int) -> dict[str, Any]:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > maximum_bytes:
            raise RuntimeError(f"stored {label} is invalid")
        try:
            value = json.loads(value)
        except (RecursionError, ValueError):
            raise RuntimeError(f"stored {label} is invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"stored {label} is invalid")
    return cast(dict[str, Any], value)


def _access_from_record(record: Mapping[str, Any]) -> ArtifactAccess:
    return ArtifactAccess(
        profile=ArtifactAccessProfile(str(record["access_profile"])),
        receipt_digest=record["access_receipt_digest"],
    )


def _artifact_from_record(record: Mapping[str, Any]) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=record["id"],
        operation_id=record["operation_id"],
        tenant_id=record["tenant_id"],
        attempt=record["attempt"],
        direction=record["direction"],
        digest=record["digest"],
        size_bytes=record["size_bytes"],
        media_type=record["media_type"],
        compression=record["compression"],
        storage_key=record["storage_key"],
        access=_access_from_record(record),
        created_at=record["created_at"],
    )


async def _upload_from_record(connection: asyncpg.Connection[Any], record: Mapping[str, Any]) -> UploadIntent:
    artifact = None
    if record["artifact_id"] is not None:
        artifact_record = await connection.fetchrow(
            "SELECT * FROM fs2_scientific_artifacts WHERE id=$1",
            record["artifact_id"],
        )
        if artifact_record is None:
            raise RuntimeError("stored upload references a missing artifact")
        artifact = _artifact_from_record(artifact_record)
    return UploadIntent(
        upload_id=record["id"],
        operation_id=record["operation_id"],
        tenant_id=record["tenant_id"],
        attempt=record["attempt"],
        direction=record["direction"],
        expected_digest=record["expected_digest"],
        expected_size_bytes=record["expected_size_bytes"],
        media_type=record["media_type"],
        compression=record["compression"],
        storage_key=record["storage_key"],
        access=_access_from_record(record),
        begun_at=record["begun_at"],
        finalized_at=record["finalized_at"],
        artifact=artifact,
    )


class PostgresArtifactRepository:
    """PostgreSQL implementation using the existing operation row as its fence."""

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self.pool = pool

    @staticmethod
    async def _assert_current(
        connection: asyncpg.Connection[Any],
        operation_id: UUID,
        tenant_id: str,
        attempt: int,
    ) -> None:
        record = await connection.fetchrow(
            "SELECT tenant_id,attempt FROM fs2_operations WHERE id=$1 FOR SHARE",
            operation_id,
        )
        if record is None:
            raise ArtifactNotFoundError("operation does not exist")
        if record["tenant_id"] != tenant_id:
            raise ArtifactNotFoundError("operation does not exist")
        if record["attempt"] != attempt:
            raise StaleArtifactAttemptError()

    @staticmethod
    def _translate_database_error(error: asyncpg.PostgresError) -> ArtifactServiceError | None:
        if error.sqlstate == "FS201":
            return StaleArtifactAttemptError()
        if isinstance(error, asyncpg.UniqueViolationError | asyncpg.CheckViolationError):
            return ArtifactConflictError("database rejected conflicting artifact metadata")
        return None

    async def begin_upload(self, request: BeginArtifactUpload, storage_key: str) -> UploadIntent:
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                await self._assert_current(connection, request.operation_id, request.tenant_id, request.attempt)
                record = await connection.fetchrow(
                    """
                    INSERT INTO fs2_scientific_uploads(
                        id,operation_id,tenant_id,attempt,direction,expected_digest,
                        expected_size_bytes,media_type,compression,storage_key,
                        access_profile,access_receipt_digest
                    ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING *
                    """,
                    request.upload_id,
                    request.operation_id,
                    request.tenant_id,
                    request.attempt,
                    request.direction.value,
                    request.expected_digest,
                    request.expected_size_bytes,
                    request.media_type,
                    request.compression.value if request.compression is not None else None,
                    storage_key,
                    request.access.profile.value,
                    request.access.receipt_digest,
                )
                created = record is not None
                if record is None:
                    record = await connection.fetchrow(
                        "SELECT * FROM fs2_scientific_uploads WHERE id=$1 FOR SHARE",
                        request.upload_id,
                    )
                if record is None:
                    raise RuntimeError("upload insert did not produce a durable row")
                intent = await _upload_from_record(connection, record)
                if not _same_upload_request(intent, request, storage_key):
                    raise ArtifactConflictError("upload ID was already used for a different intent")
                if created:
                    await connection.execute(
                        """
                        INSERT INTO fs2_scientific_artifact_events(
                            event_type,operation_id,tenant_id,attempt,upload_id
                        ) VALUES('upload_begun',$1,$2,$3,$4)
                        """,
                        request.operation_id,
                        request.tenant_id,
                        request.attempt,
                        request.upload_id,
                    )
                return intent
        except asyncpg.PostgresError as error:
            translated = self._translate_database_error(error)
            if translated is not None:
                raise translated from None
            raise

    async def get_upload(self, request: FinalizeArtifactUpload) -> UploadIntent:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._assert_current(connection, request.operation_id, request.tenant_id, request.attempt)
            record = await connection.fetchrow(
                """
                SELECT * FROM fs2_scientific_uploads
                WHERE id=$1 AND operation_id=$2 AND tenant_id=$3 AND attempt=$4
                """,
                request.upload_id,
                request.operation_id,
                request.tenant_id,
                request.attempt,
            )
            if record is None:
                raise ArtifactNotFoundError("upload does not exist")
            return await _upload_from_record(connection, record)

    async def finalize_upload(
        self,
        request: FinalizeArtifactUpload,
        verified: VerifiedStoredObject,
        *,
        artifact_id: UUID,
    ) -> ArtifactRecord:
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                await self._assert_current(connection, request.operation_id, request.tenant_id, request.attempt)
                record = await connection.fetchrow(
                    """
                    SELECT * FROM fs2_scientific_uploads
                    WHERE id=$1 AND operation_id=$2 AND tenant_id=$3 AND attempt=$4
                    FOR UPDATE
                    """,
                    request.upload_id,
                    request.operation_id,
                    request.tenant_id,
                    request.attempt,
                )
                if record is None:
                    raise ArtifactNotFoundError("upload does not exist")
                intent = await _upload_from_record(connection, record)
                _verify_object(intent, verified)
                if intent.artifact is not None:
                    return intent.artifact
                artifact_record = await connection.fetchrow(
                    """
                    INSERT INTO fs2_scientific_artifacts(
                        id,operation_id,tenant_id,attempt,direction,digest,size_bytes,
                        media_type,compression,storage_key,access_profile,access_receipt_digest
                    ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    ON CONFLICT (operation_id,attempt,storage_key) DO NOTHING
                    RETURNING *
                    """,
                    artifact_id,
                    intent.operation_id,
                    intent.tenant_id,
                    intent.attempt,
                    intent.direction.value,
                    verified.digest,
                    verified.size_bytes,
                    verified.media_type,
                    verified.compression.value if verified.compression is not None else None,
                    verified.storage_key,
                    intent.access.profile.value,
                    intent.access.receipt_digest,
                )
                if artifact_record is None:
                    artifact_record = await connection.fetchrow(
                        """
                        SELECT * FROM fs2_scientific_artifacts
                        WHERE operation_id=$1 AND attempt=$2 AND storage_key=$3
                        """,
                        intent.operation_id,
                        intent.attempt,
                        intent.storage_key,
                    )
                if artifact_record is None:
                    raise RuntimeError("artifact insert did not produce a durable row")
                artifact = _artifact_from_record(artifact_record)
                if (
                    artifact.direction is not intent.direction
                    or artifact.digest != verified.digest
                    or artifact.size_bytes != verified.size_bytes
                    or artifact.media_type != verified.media_type
                    or artifact.compression != verified.compression
                    or artifact.access != intent.access
                ):
                    raise ArtifactConflictError("content address already has different immutable metadata")
                await connection.execute(
                    """
                    UPDATE fs2_scientific_uploads
                    SET artifact_id=$2,finalized_at=clock_timestamp()
                    WHERE id=$1
                    """,
                    request.upload_id,
                    artifact.artifact_id,
                )
                await connection.execute(
                    """
                    INSERT INTO fs2_scientific_artifact_events(
                        event_type,operation_id,tenant_id,attempt,upload_id,artifact_id
                    ) VALUES('artifact_finalized',$1,$2,$3,$4,$5)
                    """,
                    intent.operation_id,
                    intent.tenant_id,
                    intent.attempt,
                    intent.upload_id,
                    artifact.artifact_id,
                )
                return artifact
        except asyncpg.PostgresError as error:
            translated = self._translate_database_error(error)
            if translated is not None:
                raise translated from None
            raise

    async def get_artifact(self, artifact_id: UUID, *, tenant_id: str) -> ArtifactRecord:
        async with self.pool.acquire() as connection:
            record = await connection.fetchrow(
                "SELECT * FROM fs2_scientific_artifacts WHERE id=$1 AND tenant_id=$2",
                artifact_id,
                tenant_id,
            )
            if record is None:
                raise ArtifactNotFoundError("artifact does not exist")
            return _artifact_from_record(record)

    async def commit_terminal_result(self, manifest: TerminalResultManifest) -> TerminalResultManifest:
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                await self._assert_current(connection, manifest.operation_id, manifest.tenant_id, manifest.attempt)
                references = [*manifest.input_artifacts, *manifest.output_artifacts]
                if references:
                    records = await connection.fetch(
                        "SELECT * FROM fs2_scientific_artifacts WHERE id=ANY($1::uuid[])",
                        [item.artifact_id for item in references],
                    )
                    stored = {record["id"]: _artifact_from_record(record) for record in records}
                    if any(stored.get(item.artifact_id) != item for item in references):
                        raise ArtifactConflictError("result manifest references an unknown or changed artifact")
                payload = manifest.model_dump(mode="json")
                record = await connection.fetchrow(
                    """
                    INSERT INTO fs2_scientific_result_manifests(
                        operation_id,tenant_id,attempt,manifest_digest,status,
                        semantic_validation_status,manifest,completed_at,committed_at
                    ) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9)
                    ON CONFLICT (operation_id) DO NOTHING
                    RETURNING *
                    """,
                    manifest.operation_id,
                    manifest.tenant_id,
                    manifest.attempt,
                    manifest.manifest_digest,
                    manifest.status.value,
                    manifest.validation.status.value,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    manifest.completed_at,
                    manifest.committed_at,
                )
                created = record is not None
                if record is None:
                    record = await connection.fetchrow(
                        "SELECT * FROM fs2_scientific_result_manifests WHERE operation_id=$1",
                        manifest.operation_id,
                    )
                if record is None:
                    raise RuntimeError("result insert did not produce a durable row")
                stored_manifest = TerminalResultManifest.model_validate(
                    _decode_json_object(record["manifest"], label="scientific result manifest", maximum_bytes=1 << 20)
                )
                if stored_manifest.manifest_digest != manifest.manifest_digest:
                    raise ArtifactConflictError("operation already has a different terminal result")
                if created:
                    await connection.execute(
                        """
                        INSERT INTO fs2_scientific_artifact_events(
                            event_type,operation_id,tenant_id,attempt,manifest_digest
                        ) VALUES('result_committed',$1,$2,$3,$4)
                        """,
                        manifest.operation_id,
                        manifest.tenant_id,
                        manifest.attempt,
                        manifest.manifest_digest,
                    )
                return stored_manifest
        except asyncpg.PostgresError as error:
            translated = self._translate_database_error(error)
            if translated is not None:
                raise translated from None
            raise

    async def get_terminal_result(self, operation_id: UUID, *, tenant_id: str) -> TerminalResultManifest:
        async with self.pool.acquire() as connection:
            record = await connection.fetchrow(
                "SELECT manifest FROM fs2_scientific_result_manifests WHERE operation_id=$1 AND tenant_id=$2",
                operation_id,
                tenant_id,
            )
        if record is None:
            raise ArtifactNotFoundError("terminal result does not exist")
        return TerminalResultManifest.model_validate(
            _decode_json_object(record["manifest"], label="scientific result manifest", maximum_bytes=1 << 20)
        )

    async def list_events(self, operation_id: UUID, *, tenant_id: str) -> list[ArtifactEvent]:
        async with self.pool.acquire() as connection:
            exists = await connection.fetchval(
                "SELECT true FROM fs2_operations WHERE id=$1 AND tenant_id=$2",
                operation_id,
                tenant_id,
            )
            if not exists:
                raise ArtifactNotFoundError("operation does not exist")
            records = await connection.fetch(
                """
                SELECT id,event_type,operation_id,tenant_id,attempt,upload_id,
                       artifact_id,manifest_digest,occurred_at
                FROM fs2_scientific_artifact_events
                WHERE operation_id=$1 AND tenant_id=$2
                ORDER BY id
                """,
                operation_id,
                tenant_id,
            )
            return [
                ArtifactEvent(
                    event_id=record["id"],
                    event_type=record["event_type"],
                    operation_id=record["operation_id"],
                    tenant_id=record["tenant_id"],
                    attempt=record["attempt"],
                    upload_id=record["upload_id"],
                    artifact_id=record["artifact_id"],
                    manifest_digest=record["manifest_digest"],
                    occurred_at=record["occurred_at"],
                )
                for record in records
            ]


# The repository's migration runner is intentionally forward-only. This
# explicit owner-only down path is for disposable pre-release verification and
# removes its own ledger entry so an up/down/up test exercises the real runner.
SCIENTIFIC_ARTIFACT_ROLLBACK_SQL = """
DROP TRIGGER IF EXISTS fs2_scientific_artifact_events_immutable ON fs2_scientific_artifact_events;
DROP TRIGGER IF EXISTS fs2_scientific_result_manifests_immutable ON fs2_scientific_result_manifests;
DROP TRIGGER IF EXISTS fs2_scientific_artifacts_immutable ON fs2_scientific_artifacts;
DROP TRIGGER IF EXISTS fs2_scientific_upload_transition ON fs2_scientific_uploads;
DROP TRIGGER IF EXISTS fs2_scientific_events_attempt_fence ON fs2_scientific_artifact_events;
DROP TRIGGER IF EXISTS fs2_scientific_results_attempt_fence ON fs2_scientific_result_manifests;
DROP TRIGGER IF EXISTS fs2_scientific_artifacts_attempt_fence ON fs2_scientific_artifacts;
DROP TRIGGER IF EXISTS fs2_scientific_uploads_attempt_fence ON fs2_scientific_uploads;
DROP TABLE IF EXISTS fs2_scientific_artifact_events;
DROP TABLE IF EXISTS fs2_scientific_result_manifests;
DROP TABLE IF EXISTS fs2_scientific_uploads;
DROP TABLE IF EXISTS fs2_scientific_artifacts;
DROP FUNCTION IF EXISTS fs2_scientific_reject_mutation();
DROP FUNCTION IF EXISTS fs2_scientific_validate_upload_transition();
DROP FUNCTION IF EXISTS fs2_scientific_assert_current_attempt();
DELETE FROM fs2_schema_migrations WHERE version='0014_scientific_artifact_results.sql';
""".strip()
