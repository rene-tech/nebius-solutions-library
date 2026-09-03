"""Authorized artifact routes with a deliberately narrow public boundary.

Every route derives the tenant from the verified bearer principal, never from
the request. Writes require ``artifacts.write``; reads require
``operations.result``. Only the two handle-issuing routes return bearer
material, and no route ever serializes a storage key, a tenant identity, or a
persistence record.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import Field

from .models import Principal, Scope, StrictModel
from .scientific_artifacts import (
    ArtifactAccess,
    ArtifactCompression,
    ArtifactConflictError,
    ArtifactContentTooLargeError,
    ArtifactDirection,
    ArtifactEvent,
    ArtifactNotFoundError,
    ArtifactPolicyError,
    ArtifactServiceError,
    ArtifactVerificationError,
    AttemptStatus,
    BeginArtifactUpload,
    CloseStageAttempt,
    CommitStageResult,
    EphemeralHandle,
    FinalizeArtifactUpload,
    KueueAdmission,
    ManifestEntryDraft,
    OpenStageAttempt,
    ResultAlreadyTerminalError,
    RunResultDraft,
    ScientificArtifactControllerPort,
    StageAttemptRecord,
    StageCommitRecord,
    StaleArtifactAttemptError,
)
from .scientific_run_result import ArtifactRef, ScientificRunResult

RawSha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
CompressionInput = ArtifactCompression | Literal["none"]

_ERROR_STATUS: tuple[tuple[type[ArtifactServiceError], int], ...] = (
    (ArtifactNotFoundError, status.HTTP_404_NOT_FOUND),
    (StaleArtifactAttemptError, status.HTTP_409_CONFLICT),
    (ResultAlreadyTerminalError, status.HTTP_409_CONFLICT),
    (ArtifactConflictError, status.HTTP_409_CONFLICT),
    (ArtifactVerificationError, status.HTTP_422_UNPROCESSABLE_ENTITY),
    (ArtifactPolicyError, status.HTTP_422_UNPROCESSABLE_ENTITY),
    (ArtifactContentTooLargeError, status.HTTP_413_CONTENT_TOO_LARGE),
)


def _http_error(error: ArtifactServiceError) -> HTTPException:
    """Map a domain failure onto a stable, payload-free public problem."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    for failure, code in _ERROR_STATUS:
        if isinstance(error, failure):
            status_code = code
            break
    return HTTPException(status_code=status_code, detail={"type": error.code, "message": str(error)})


def _compression(value: CompressionInput | None) -> ArtifactCompression | None:
    return value if isinstance(value, ArtifactCompression) else None


class KueueAdmissionInput(StrictModel):
    resolved_pool_id: str | None = Field(default=None, max_length=128)
    admitted_resource_flavor: str | None = Field(default=None, max_length=253)
    accelerator_resource_name: str | None = Field(default=None, max_length=253)
    accelerator_count: int = Field(default=0, ge=0, le=1024)
    admitted_at: datetime

    def to_internal(self) -> KueueAdmission:
        return KueueAdmission(**self.model_dump())


class OpenAttemptRequest(StrictModel):
    """Register one scheduled stage/shard attempt and its admitted identity."""

    attempt_id: UUID
    operation_id: UUID
    stage_id: str = Field(max_length=63)
    shard_id: str | None = Field(default=None, max_length=253)
    attempt_number: int = Field(ge=1, le=10)
    admission: KueueAdmissionInput | None = None
    kueue_workload_uid: str | None = Field(default=None, max_length=128)
    k8s_job_uid: str | None = Field(default=None, max_length=128)
    started_at: datetime

    def to_internal(self, principal: Principal) -> OpenStageAttempt:
        return OpenStageAttempt(
            attempt_id=self.attempt_id,
            operation_id=self.operation_id,
            tenant_id=principal.tenant_id,
            stage_id=self.stage_id,
            shard_id=self.shard_id,
            attempt_number=self.attempt_number,
            admission=self.admission.to_internal() if self.admission else None,
            kueue_workload_uid=self.kueue_workload_uid,
            k8s_job_uid=self.k8s_job_uid,
            started_at=self.started_at,
        )


class CloseAttemptRequest(StrictModel):
    operation_id: UUID
    status: Literal["succeeded", "failed", "cancelled", "preempted"]
    completed_at: datetime
    admission: KueueAdmissionInput | None = None
    kueue_workload_uid: str | None = Field(default=None, max_length=128)
    k8s_job_uid: str | None = Field(default=None, max_length=128)
    pod_uids: tuple[str, ...] = Field(default=(), max_length=1024)
    node_uids: tuple[str, ...] = Field(default=(), max_length=1024)
    gpu_uuids: tuple[str, ...] = Field(default=(), max_length=1024)

    def to_internal(self, attempt_id: UUID, principal: Principal) -> CloseStageAttempt:
        return CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=self.operation_id,
            tenant_id=principal.tenant_id,
            status=AttemptStatus(self.status),
            completed_at=self.completed_at,
            admission=self.admission.to_internal() if self.admission else None,
            kueue_workload_uid=self.kueue_workload_uid,
            k8s_job_uid=self.k8s_job_uid,
            pod_uids=self.pod_uids,
            node_uids=self.node_uids,
            gpu_uuids=self.gpu_uuids,
        )


class AttemptResponse(StrictModel):
    """Attempt identity without tenant, storage or retention internals."""

    attempt_id: UUID
    operation_id: UUID
    stage_id: str
    shard_id: str | None
    attempt_number: int
    status: str
    started_at: datetime
    completed_at: datetime | None

    @classmethod
    def of(cls, record: StageAttemptRecord) -> "AttemptResponse":
        return cls(
            attempt_id=record.attempt_id,
            operation_id=record.operation_id,
            stage_id=record.stage_id,
            shard_id=record.shard_id,
            attempt_number=record.attempt_number,
            status=record.status.value,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )


class ArtifactUploadBeginRequest(StrictModel):
    """Public request fields; tenant identity comes from the bearer principal."""

    upload_id: UUID
    attempt_id: UUID
    operation_id: UUID
    direction: ArtifactDirection
    sha256: RawSha256
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=3, max_length=128)
    compression: CompressionInput | None = None
    access: ArtifactAccess = Field(default_factory=ArtifactAccess)

    def to_internal(self, principal: Principal) -> BeginArtifactUpload:
        return BeginArtifactUpload(
            upload_id=self.upload_id,
            attempt_id=self.attempt_id,
            operation_id=self.operation_id,
            tenant_id=principal.tenant_id,
            direction=self.direction,
            expected_digest=f"sha256:{self.sha256}",
            expected_size_bytes=self.size_bytes,
            media_type=self.media_type.lower(),
            compression=_compression(self.compression),
            access=self.access,
        )


class ArtifactUploadFinalizeRequest(StrictModel):
    operation_id: UUID


class EphemeralHandleResponse(StrictModel):
    """Bearer material returned only to the authorized caller, never stored."""

    method: Literal["GET", "PUT"]
    url: str
    expires_at: datetime
    write_once: bool
    headers: dict[str, str]

    @classmethod
    def of(cls, handle: EphemeralHandle) -> "EphemeralHandleResponse":
        return cls(
            method=handle.method,
            url=handle.url,
            expires_at=handle.expires_at,
            write_once=handle.write_once,
            headers=dict(handle.headers),
        )


class ArtifactUploadBeginResponse(StrictModel):
    upload_id: UUID
    handle: EphemeralHandleResponse


class ArtifactDownloadResponse(StrictModel):
    artifact: ArtifactRef
    handle: EphemeralHandleResponse


class ManifestEntryInput(StrictModel):
    name: str = Field(max_length=128)
    semantic_type: str = Field(max_length=128)
    artifact_id: UUID


class CommitStageRequest(StrictModel):
    operation_id: UUID
    stage_id: str = Field(max_length=63)
    attempt_ids: tuple[UUID, ...] = Field(min_length=1, max_length=10240)
    entries: tuple[ManifestEntryInput, ...] = Field(min_length=1, max_length=10000)
    validation_digest: RawSha256
    semantic_valid: bool
    committed_at: datetime
    validated_at: datetime

    def to_internal(self, principal: Principal) -> CommitStageResult:
        return CommitStageResult(
            operation_id=self.operation_id,
            tenant_id=principal.tenant_id,
            stage_id=self.stage_id,
            attempt_ids=self.attempt_ids,
            entries=tuple(
                ManifestEntryDraft(name=entry.name, semantic_type=entry.semantic_type, artifact_id=entry.artifact_id)
                for entry in self.entries
            ),
            validation_digest=f"sha256:{self.validation_digest}",
            semantic_valid=self.semantic_valid,
            committed_at=self.committed_at,
            validated_at=self.validated_at,
        )


class StageCommitResponse(StrictModel):
    """The exact commit identity the batch controller compares against."""

    operation_id: UUID
    stage_id: str
    attempt_ids: tuple[UUID, ...]
    manifest_digest: str
    validation_digest: str
    semantic_valid: bool
    committed_at: datetime
    validated_at: datetime
    manifest: dict[str, Any]

    @classmethod
    def of(cls, record: StageCommitRecord) -> "StageCommitResponse":
        return cls(
            operation_id=record.operation_id,
            stage_id=record.stage_id,
            attempt_ids=record.attempt_ids,
            manifest_digest=record.manifest_digest,
            validation_digest=record.validation_digest,
            semantic_valid=record.semantic_valid,
            committed_at=record.committed_at,
            validated_at=record.validated_at,
            manifest=record.manifest.to_document(),
        )


class CommitRunResultRequest(StrictModel):
    """Publish the canonical terminal result for one scientific operation."""

    terminal_status: Literal["succeeded", "failed", "cancelled"]
    submitted_at: datetime
    completed_at: datetime
    execution_identity: dict[str, Any]
    access: ArtifactAccess = Field(default_factory=ArtifactAccess)
    scheduling_snapshot: dict[str, Any]
    input_manifest_artifact_id: UUID
    output_manifest_artifact_id: UUID | None = None
    validator_id: str = Field(max_length=253)
    validation_status: Literal["passed", "failed", "not-run"]
    validation_receipt_sha256: RawSha256 | None = None
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=2000)
    error_retryable: bool | None = None

    def to_internal(self, operation_id: UUID, principal: Principal) -> RunResultDraft:
        return RunResultDraft(
            operation_id=operation_id,
            tenant_id=principal.tenant_id,
            terminal_status=self.terminal_status,
            submitted_at=self.submitted_at,
            completed_at=self.completed_at,
            execution_identity=self.execution_identity,
            access=self.access,
            scheduling_snapshot=self.scheduling_snapshot,
            input_manifest_artifact_id=self.input_manifest_artifact_id,
            output_manifest_artifact_id=self.output_manifest_artifact_id,
            validator_id=self.validator_id,
            validation_status=self.validation_status,
            validation_receipt_digest=(
                f"sha256:{self.validation_receipt_sha256}" if self.validation_receipt_sha256 else None
            ),
            error_code=self.error_code,
            error_message=self.error_message,
            error_retryable=self.error_retryable,
        )


class RunResultResponse(StrictModel):
    """The canonical document plus the digest that identifies it immutably."""

    result_digest: str
    committed_at: datetime
    result: dict[str, Any]

    @classmethod
    def of(cls, digest: str, committed_at: datetime, result: ScientificRunResult) -> "RunResultResponse":
        return cls(result_digest=digest, committed_at=committed_at, result=result.to_document())


class ArtifactEventResponse(StrictModel):
    event_id: int
    event_type: str
    operation_id: UUID
    stage_id: str | None
    attempt_id: UUID | None
    upload_id: UUID | None
    artifact_id: UUID | None
    manifest_digest: str | None
    occurred_at: datetime

    @classmethod
    def of(cls, event: ArtifactEvent) -> "ArtifactEventResponse":
        return cls(
            event_id=event.event_id,
            event_type=event.event_type.value,
            operation_id=event.operation_id,
            stage_id=event.stage_id,
            attempt_id=event.attempt_id,
            upload_id=event.upload_id,
            artifact_id=event.artifact_id,
            manifest_digest=event.manifest_digest,
            occurred_at=event.occurred_at,
        )


class ArtifactEventPage(StrictModel):
    events: tuple[ArtifactEventResponse, ...]
    next_after_id: int


def scientific_artifact_router(
    *,
    service: ScientificArtifactControllerPort,
    principal_dependency: Callable[..., Awaitable[Principal]],
) -> APIRouter:
    """Mount the authorized artifact surface for adapters and the controller."""

    router = APIRouter(prefix="/internal/scientific-artifacts", tags=["scientific-artifacts"])

    @router.post("/attempts", response_model=AttemptResponse, status_code=status.HTTP_201_CREATED)
    async def open_attempt(
        request: OpenAttemptRequest,
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> "AttemptResponse":
        principal.require(Scope.ARTIFACTS_WRITE)
        try:
            return AttemptResponse.of(await service.open_attempt(request.to_internal(principal)))
        except ArtifactServiceError as error:
            raise _http_error(error) from None

    @router.post("/attempts/{attempt_id}:close", response_model=AttemptResponse)
    async def close_attempt(
        request: CloseAttemptRequest,
        attempt_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> "AttemptResponse":
        principal.require(Scope.ARTIFACTS_WRITE)
        try:
            return AttemptResponse.of(await service.close_attempt(request.to_internal(attempt_id, principal)))
        except ArtifactServiceError as error:
            raise _http_error(error) from None

    @router.post("/uploads", response_model=ArtifactUploadBeginResponse, status_code=status.HTTP_201_CREATED)
    async def begin_upload(
        request: ArtifactUploadBeginRequest,
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> ArtifactUploadBeginResponse:
        principal.require(Scope.ARTIFACTS_WRITE)
        try:
            result = await service.begin_upload(request.to_internal(principal))
        except ArtifactServiceError as error:
            raise _http_error(error) from None
        return ArtifactUploadBeginResponse(
            upload_id=result.upload.upload_id, handle=EphemeralHandleResponse.of(result.handle)
        )

    @router.post("/uploads/{upload_id}:finalize", response_model=ArtifactRef)
    async def finalize_upload(
        request: ArtifactUploadFinalizeRequest,
        upload_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> ArtifactRef:
        principal.require(Scope.ARTIFACTS_WRITE)
        try:
            artifact = await service.finalize_upload(
                FinalizeArtifactUpload(
                    upload_id=upload_id,
                    operation_id=request.operation_id,
                    tenant_id=principal.tenant_id,
                )
            )
        except ArtifactServiceError as error:
            raise _http_error(error) from None
        return artifact.to_public_ref()

    @router.get("/{artifact_id}:download", response_model=ArtifactDownloadResponse)
    async def download(
        artifact_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> ArtifactDownloadResponse:
        principal.require(Scope.OPERATIONS_RESULT)
        try:
            result = await service.download(artifact_id, tenant_id=principal.tenant_id)
        except ArtifactServiceError as error:
            raise _http_error(error) from None
        return ArtifactDownloadResponse(
            artifact=result.artifact.to_public_ref(), handle=EphemeralHandleResponse.of(result.handle)
        )

    @router.post("/stages:commit", response_model=StageCommitResponse, status_code=status.HTTP_201_CREATED)
    async def commit_stage(
        request: CommitStageRequest,
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> "StageCommitResponse":
        principal.require(Scope.ARTIFACTS_WRITE)
        try:
            return StageCommitResponse.of(await service.commit_stage(request.to_internal(principal)))
        except ArtifactServiceError as error:
            raise _http_error(error) from None

    @router.get("/operations/{operation_id}/stages/{stage_id}:commit", response_model=StageCommitResponse)
    async def read_stage_commit(
        operation_id: Annotated[UUID, Path()],
        stage_id: Annotated[str, Path(max_length=63)],
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> "StageCommitResponse":
        principal.require(Scope.OPERATIONS_RESULT)
        commit = await service.stage_commit(operation_id, stage_id=stage_id, tenant_id=principal.tenant_id)
        if commit is None:
            raise _http_error(ArtifactNotFoundError("stage commit not found"))
        return StageCommitResponse.of(commit)

    @router.post(
        "/operations/{operation_id}:result",
        response_model=RunResultResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def commit_run_result(
        request: CommitRunResultRequest,
        operation_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> "RunResultResponse":
        principal.require(Scope.ARTIFACTS_WRITE)
        try:
            record = await service.commit_run_result(request.to_internal(operation_id, principal))
        except ArtifactServiceError as error:
            raise _http_error(error) from None
        return RunResultResponse.of(record.result_digest, record.committed_at, record.result)

    @router.get("/operations/{operation_id}:result", response_model=RunResultResponse)
    async def read_run_result(
        operation_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> "RunResultResponse":
        principal.require(Scope.OPERATIONS_RESULT)
        try:
            record = await service.get_run_result(operation_id, tenant_id=principal.tenant_id)
        except ArtifactServiceError as error:
            raise _http_error(error) from None
        return RunResultResponse.of(record.result_digest, record.committed_at, record.result)

    @router.get("/operations/{operation_id}/events", response_model=ArtifactEventPage)
    async def read_events(
        operation_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(principal_dependency)],
        after_id: Annotated[int, Query(ge=0, le=2**62)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> ArtifactEventPage:
        principal.require(Scope.OPERATIONS_RESULT)
        try:
            events = await service.list_events(
                operation_id, tenant_id=principal.tenant_id, after_id=after_id, limit=limit
            )
        except ArtifactServiceError as error:
            raise _http_error(error) from None
        rendered = tuple(ArtifactEventResponse.of(event) for event in events)
        return ArtifactEventPage(events=rendered, next_after_id=rendered[-1].event_id if rendered else after_id)

    return router
