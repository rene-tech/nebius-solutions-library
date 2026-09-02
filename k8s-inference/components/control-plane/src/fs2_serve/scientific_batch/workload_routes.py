"""Capability-authenticated artifact port for scientific Job companions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Path, status
from pydantic import Field

from ..models import StrictModel
from ..scientific_artifact_routes import EphemeralHandleResponse
from ..scientific_artifacts import (
    ArtifactAccess,
    ArtifactAccessProfile,
    ArtifactCompression,
    ArtifactDirection,
    ArtifactRefProjection,
    BeginArtifactUpload,
    FinalizeArtifactUpload,
    ScientificArtifactControllerPort,
)
from .capability import ScientificWorkloadCapability, ScientificWorkloadCapabilityAuthority
from .models import ArtifactCommit, AttemptOutcome, ScientificBatchState
from .protocols import BatchRepositoryConflictError

_OPERATION_ARTIFACT_ATTEMPT = 0


class WorkloadBatchRepository(Protocol):
    async def get(self, operation_id: UUID, *, tenant_id: str) -> ScientificBatchState: ...

    async def record_artifact_commit(
        self,
        commit: ArtifactCommit,
        *,
        tenant_id: str,
        manifest_artifact_id: UUID,
        validation_artifact_id: UUID,
    ) -> ArtifactCommit: ...


class WorkloadUploadRequest(StrictModel):
    upload_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=3, max_length=128)
    compression: ArtifactCompression | Literal["none"] | None = None


class WorkloadUploadResponse(StrictModel):
    upload_id: UUID
    handle: EphemeralHandleResponse


class WorkloadDownloadResponse(StrictModel):
    artifact: ArtifactRefProjection
    handle: EphemeralHandleResponse


class WorkloadCommitRequest(StrictModel):
    handoff_artifact_id: UUID
    handoff_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    handoff_size_bytes: int = Field(ge=0)
    handoff_media_type: str = Field(min_length=3, max_length=128)
    handoff_compression: ArtifactCompression | Literal["none"] | None = None
    manifest_artifact_id: UUID
    validation_artifact_id: UUID
    manifest_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    validation_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    semantic_valid: Literal[True]


def _bearer(value: str | None) -> str:
    if value is None or not value.startswith("Bearer ") or value.count(" ") != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="workload capability required")
    return value.removeprefix("Bearer ")


def scientific_workload_artifact_router(
    *,
    authority: ScientificWorkloadCapabilityAuthority,
    artifacts: ScientificArtifactControllerPort,
    batches: WorkloadBatchRepository,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/internal/scientific-workloads", tags=["scientific-workloads-internal"])

    async def authorized(authorization: str | None) -> tuple[ScientificWorkloadCapability, ScientificBatchState]:
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
        return capability, state

    @router.get("/artifacts/{artifact_id}:download", response_model=WorkloadDownloadResponse)
    async def download(
        artifact_id: Annotated[UUID, Path()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkloadDownloadResponse:
        capability, _ = await authorized(authorization)
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
            handle=EphemeralHandleResponse.from_handle(result.handle),
        )

    @router.post("/uploads", response_model=WorkloadUploadResponse, status_code=status.HTTP_201_CREATED)
    async def begin_upload(
        request: WorkloadUploadRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkloadUploadResponse:
        capability, _ = await authorized(authorization)
        compression = request.compression if isinstance(request.compression, ArtifactCompression) else None
        result = await artifacts.begin_upload(
            BeginArtifactUpload(
                upload_id=request.upload_id,
                operation_id=capability.operation_id,
                tenant_id=capability.tenant_id,
                # Stage retries are capability-fenced by their UUID. Artifact
                # rows use the parent Operation's controller-owned attempt,
                # which scientific submissions deliberately keep at zero.
                attempt=_OPERATION_ARTIFACT_ATTEMPT,
                direction=ArtifactDirection.OUTPUT,
                expected_digest=f"sha256:{request.sha256}",
                expected_size_bytes=request.size_bytes,
                media_type=request.media_type,
                compression=compression,
                access=ArtifactAccess(
                    profile=ArtifactAccessProfile(capability.access_profile),
                    receipt_digest=capability.access_receipt_digest,
                ),
            )
        )
        return WorkloadUploadResponse(
            upload_id=result.upload.upload_id,
            handle=EphemeralHandleResponse.from_handle(result.handle),
        )

    @router.post("/uploads/{upload_id}:finalize", response_model=ArtifactRefProjection)
    async def finalize_upload(
        upload_id: Annotated[UUID, Path()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> ArtifactRefProjection:
        capability, _ = await authorized(authorization)
        record = await artifacts.finalize_upload(
            FinalizeArtifactUpload(
                upload_id=upload_id,
                operation_id=capability.operation_id,
                tenant_id=capability.tenant_id,
                attempt=_OPERATION_ARTIFACT_ATTEMPT,
            )
        )
        return record.to_public_ref()

    @router.post("/commit")
    async def commit(
        request: WorkloadCommitRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        capability, _ = await authorized(authorization)
        now = clock()
        commit_value = ArtifactCommit(
            operation_id=capability.operation_id,
            stage_id=capability.stage_id,
            attempt_ids=(capability.attempt_id,),
            logical_artifact_id=capability.logical_output_id,
            handoff_artifact_id=request.handoff_artifact_id,
            handoff_digest=request.handoff_digest,
            handoff_size_bytes=request.handoff_size_bytes,
            handoff_media_type=request.handoff_media_type,
            handoff_compression=(
                request.handoff_compression.value
                if isinstance(request.handoff_compression, ArtifactCompression)
                else None
            ),
            manifest_artifact_id=request.manifest_artifact_id,
            validation_artifact_id=request.validation_artifact_id,
            manifest_digest=request.manifest_digest,
            validation_digest=request.validation_digest,
            committed_at=now,
            validated_at=now,
            semantic_valid=request.semantic_valid,
            collector_id=capability.collector_id,
            validator_id=capability.validator_id,
        )
        try:
            stored = await batches.record_artifact_commit(
                commit_value,
                tenant_id=capability.tenant_id,
                manifest_artifact_id=request.manifest_artifact_id,
                validation_artifact_id=request.validation_artifact_id,
            )
        except BatchRepositoryConflictError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None
        return {
            "operation_id": str(stored.operation_id),
            "stage_id": stored.stage_id,
            "attempt_id": str(stored.attempt_ids[0]),
            "logical_artifact_id": stored.logical_artifact_id,
            "semantic_valid": stored.semantic_valid,
        }

    return router
