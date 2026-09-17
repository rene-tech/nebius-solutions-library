"""Capability-authenticated artifact port for scientific Job companions."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Path, status
from pydantic import Field, model_validator

from ..models import StrictModel
from ..scientific_artifact_routes import EphemeralHandleResponse
from ..scientific_artifacts import (
    ArtifactAccess,
    ArtifactAccessProfile,
    ArtifactCompression,
    ArtifactDirection,
    AuthorizeArtifactUploadPart,
    BeginArtifactUpload,
    FinalizeArtifactUpload,
    MULTIPART_FIRST_PART_DESCRIPTION,
    OpenStageAttempt,
    ScientificArtifactControllerPort,
)
from ..scientific_run_result import ArtifactRef
from .capability import ScientificWorkloadCapability, ScientificWorkloadCapabilityAuthority
from .models import AttemptOutcome, ScientificAttemptState, ScientificBatchState


class WorkloadBatchRepository(Protocol):
    async def get(self, operation_id: UUID, *, tenant_id: str) -> ScientificBatchState: ...


class WorkloadUploadRequest(StrictModel):
    upload_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=3, max_length=128)
    compression: ArtifactCompression | Literal["none"] | None = None
    upload_protocol: Literal["single-put-v1", "multipart-v2"] = "single-put-v1"
    first_part_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
        description=MULTIPART_FIRST_PART_DESCRIPTION,
    )

    @model_validator(mode="after")
    def protocol_fields_match(self) -> "WorkloadUploadRequest":
        if self.upload_protocol == "multipart-v2" and self.first_part_sha256 is None:
            raise ValueError("multipart-v2 requires first_part_sha256")
        if self.upload_protocol == "single-put-v1" and self.first_part_sha256 is not None:
            raise ValueError("single-put-v1 does not accept first_part_sha256")
        return self


class WorkloadUploadResponse(StrictModel):
    upload_id: UUID
    upload_protocol: Literal["single-put-v1", "multipart-v2"]
    session_generation: int
    part_size_bytes: int
    part_count: int
    handle: EphemeralHandleResponse


class WorkloadUploadPartRequest(StrictModel):
    session_generation: int = Field(ge=1, le=1_000_000)
    size_bytes: int = Field(ge=0, le=5 * 1024 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkloadDownloadResponse(StrictModel):
    artifact: ArtifactRef
    handle: EphemeralHandleResponse


def _bearer(value: str | None) -> str:
    if value is None or not value.startswith("Bearer ") or value.count(" ") != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="workload capability required")
    return value.removeprefix("Bearer ")


def scientific_workload_artifact_router(
    *,
    authority: ScientificWorkloadCapabilityAuthority,
    artifacts: ScientificArtifactControllerPort,
    batches: WorkloadBatchRepository,
) -> APIRouter:
    router = APIRouter(prefix="/internal/scientific-workloads", tags=["scientific-workloads-internal"])

    async def authorized(
        authorization: str | None,
    ) -> tuple[ScientificWorkloadCapability, ScientificBatchState, ScientificAttemptState]:
        try:
            capability = authority.verify(_bearer(authorization))
            state = await batches.get(capability.operation_id, tenant_id=capability.tenant_id)
            stage = state.stage(capability.stage_id)
            attempt = stage.latest_attempt(None if capability.shard_id == "gang" else capability.shard_id)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="workload capability rejected",
            ) from None
        if (
            state.batch_id != capability.batch_id
            or state.workload_id != capability.workload_id
            or state.model_id != capability.model_id
            or state.variant_id != capability.variant_id
            or attempt is None
            or attempt.attempt_id != capability.attempt_id
            or attempt.attempt_number != capability.attempt_number
            or attempt.outcome is not AttemptOutcome.ACTIVE
            or attempt.resource_released
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="workload capability is stale")
        if state.execution_plan is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="workload execution is unavailable")
        invocation = state.execution_plan.invocation(capability.stage_id, attempt.shard_id)
        if (
            invocation.collector_id != capability.collector_id
            or invocation.validator_id != capability.validator_id
            or invocation.produces != capability.logical_output_id
            or state.access_context.profile != capability.access_profile
            or state.access_context.receipt_digest != capability.access_receipt_digest
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="workload capability binding changed")
        return capability, state, attempt

    @router.get("/artifacts/{artifact_id}:download", response_model=WorkloadDownloadResponse)
    async def download(
        artifact_id: Annotated[UUID, Path()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkloadDownloadResponse:
        capability, _, _ = await authorized(authorization)
        binding = next((item for item in capability.artifacts if item.artifact_id == artifact_id), None)
        if binding is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="artifact is outside workload capability")
        result = await artifacts.download(artifact_id, tenant_id=capability.tenant_id)
        if (
            result.artifact.digest != binding.digest
            or result.artifact.size_bytes != binding.size_bytes
            or result.artifact.media_type != binding.media_type
            or (None if result.artifact.compression is None else result.artifact.compression.value)
            != binding.compression
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="artifact metadata changed")
        return WorkloadDownloadResponse(
            artifact=result.artifact.to_public_ref(),
            handle=EphemeralHandleResponse.of(result.handle),
        )

    @router.post("/uploads", response_model=WorkloadUploadResponse, status_code=status.HTTP_201_CREATED)
    async def begin_upload(
        request: WorkloadUploadRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkloadUploadResponse:
        capability, state, attempt = await authorized(authorization)
        await artifacts.open_attempt(
            OpenStageAttempt(
                attempt_id=capability.attempt_id,
                operation_id=capability.operation_id,
                tenant_id=capability.tenant_id,
                stage_id=capability.stage_id,
                shard_id=attempt.shard_id,
                attempt_number=attempt.attempt_number,
                started_at=attempt.started_at or state.scheduling.captured_at,
            )
        )
        compression = request.compression if isinstance(request.compression, ArtifactCompression) else None
        result = await artifacts.begin_upload(
            BeginArtifactUpload(
                upload_id=request.upload_id,
                attempt_id=capability.attempt_id,
                operation_id=capability.operation_id,
                tenant_id=capability.tenant_id,
                direction=ArtifactDirection.OUTPUT,
                expected_digest=f"sha256:{request.sha256}",
                expected_size_bytes=request.size_bytes,
                media_type=request.media_type,
                compression=compression,
                access=ArtifactAccess(
                    profile=ArtifactAccessProfile(capability.access_profile),
                    receipt_digest=capability.access_receipt_digest,
                ),
                upload_protocol=request.upload_protocol,
                first_part_checksum=(
                    f"sha256:{request.first_part_sha256}"
                    if request.first_part_sha256 is not None
                    else None
                ),
            )
        )
        return WorkloadUploadResponse(
            upload_id=result.upload.upload_id,
            upload_protocol=request.upload_protocol,
            session_generation=result.session.session_generation,
            part_size_bytes=result.session.part_size_bytes,
            part_count=result.session.part_count,
            handle=EphemeralHandleResponse.of(result.handle),
        )

    @router.post(
        "/uploads/{upload_id}/parts/{part_number}:authorize",
        response_model=EphemeralHandleResponse,
    )
    async def authorize_upload_part(
        request: WorkloadUploadPartRequest,
        upload_id: Annotated[UUID, Path()],
        part_number: Annotated[int, Path(ge=1, le=10_000)],
        authorization: Annotated[str | None, Header()] = None,
    ) -> EphemeralHandleResponse:
        capability, _, _ = await authorized(authorization)
        result = await artifacts.authorize_upload_part(
            AuthorizeArtifactUploadPart(
                upload_id=upload_id,
                operation_id=capability.operation_id,
                tenant_id=capability.tenant_id,
                session_generation=request.session_generation,
                part_number=part_number,
                size_bytes=request.size_bytes,
                checksum=f"sha256:{request.sha256}",
            )
        )
        return EphemeralHandleResponse.of(result.handle)

    @router.post("/uploads/{upload_id}:finalize", response_model=ArtifactRef)
    async def finalize_upload(
        upload_id: Annotated[UUID, Path()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> ArtifactRef:
        capability, _, _ = await authorized(authorization)
        record = await artifacts.finalize_upload(
            FinalizeArtifactUpload(
                upload_id=upload_id,
                operation_id=capability.operation_id,
                tenant_id=capability.tenant_id,
            )
        )
        return record.to_public_ref()

    return router
