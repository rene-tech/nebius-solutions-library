"""Controller-facing artifact routes with a deliberately narrow public boundary."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import Field

from .models import Principal, Scope, StrictModel
from .scientific_artifacts import (
    ArtifactAccess,
    ArtifactCompression,
    ArtifactConflictError,
    ArtifactDirection,
    ArtifactNotFoundError,
    ArtifactPolicyError,
    ArtifactRecord,
    ArtifactRefProjection,
    ArtifactServiceError,
    ArtifactVerificationError,
    BeginArtifactUpload,
    BeginUploadResult,
    EphemeralHandle,
    FinalizeArtifactUpload,
    ScientificArtifactControllerPort,
    StaleArtifactAttemptError,
)

RawSha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
CompressionInput = ArtifactCompression | Literal["none"]


class ArtifactUploadBeginRequest(StrictModel):
    """Internal controller fields; tenant identity comes from the bearer principal."""

    upload_id: UUID
    operation_id: UUID
    attempt: int = Field(ge=0, le=10)
    direction: ArtifactDirection
    sha256: RawSha256
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=3, max_length=127)
    compression: CompressionInput | None = None
    access: ArtifactAccess = Field(default_factory=ArtifactAccess)

    def to_internal(self, principal: Principal) -> BeginArtifactUpload:
        compression = self.compression if isinstance(self.compression, ArtifactCompression) else None
        return BeginArtifactUpload(
            upload_id=self.upload_id,
            operation_id=self.operation_id,
            tenant_id=principal.tenant_id,
            attempt=self.attempt,
            direction=self.direction,
            expected_digest=f"sha256:{self.sha256}",
            expected_size_bytes=self.size_bytes,
            media_type=self.media_type.lower(),
            compression=compression,
            access=self.access,
        )


class ArtifactUploadFinalizeRequest(StrictModel):
    operation_id: UUID
    attempt: int = Field(ge=0, le=10)


class EphemeralHandleResponse(StrictModel):
    """Bearer material is returned only by the internal controller route, never a projection."""

    method: Literal["GET", "PUT"]
    url: str
    expires_at: datetime
    write_once: bool
    headers: dict[str, str]

    @classmethod
    def from_handle(cls, handle: EphemeralHandle) -> "EphemeralHandleResponse":
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
    artifact: ArtifactRefProjection
    handle: EphemeralHandleResponse


def _http_error(error: ArtifactServiceError) -> HTTPException:
    if isinstance(error, ArtifactNotFoundError):
        code, status_code = "artifact_not_found", status.HTTP_404_NOT_FOUND
    elif isinstance(error, StaleArtifactAttemptError):
        code, status_code = "stale_artifact_attempt", status.HTTP_409_CONFLICT
    elif isinstance(error, ArtifactConflictError):
        code, status_code = "artifact_conflict", status.HTTP_409_CONFLICT
    elif isinstance(error, ArtifactVerificationError):
        code, status_code = "artifact_verification_failed", status.HTTP_422_UNPROCESSABLE_ENTITY
    elif isinstance(error, ArtifactPolicyError):
        code, status_code = "artifact_policy_rejected", status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code, status_code = "artifact_service_error", status.HTTP_503_SERVICE_UNAVAILABLE
    return HTTPException(status_code=status_code, detail={"type": code, "message": str(error)})


def scientific_artifact_router(
    *,
    service: ScientificArtifactControllerPort,
    principal_dependency: Callable[..., Awaitable[Principal]],
) -> APIRouter:
    """Mount internal controller routes; no route serializes ``ArtifactRecord``."""

    router = APIRouter(prefix="/internal/scientific-artifacts", tags=["scientific-artifacts"])

    @router.post("/uploads", response_model=ArtifactUploadBeginResponse, status_code=status.HTTP_201_CREATED)
    async def begin_upload(
        request: ArtifactUploadBeginRequest,
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> ArtifactUploadBeginResponse:
        principal.require(Scope.INFERENCE_INVOKE)
        try:
            result: BeginUploadResult = await service.begin_upload(request.to_internal(principal))
        except ArtifactServiceError as error:
            raise _http_error(error) from None
        return ArtifactUploadBeginResponse(
            upload_id=result.upload.upload_id,
            handle=EphemeralHandleResponse.from_handle(result.handle),
        )

    @router.post(
        "/uploads/{upload_id}:finalize",
        response_model=ArtifactRefProjection,
        response_model_exclude_none=True,
    )
    async def finalize_upload(
        request: ArtifactUploadFinalizeRequest,
        upload_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> ArtifactRefProjection:
        principal.require(Scope.INFERENCE_INVOKE)
        try:
            artifact: ArtifactRecord = await service.finalize_upload(
                FinalizeArtifactUpload(
                    upload_id=upload_id,
                    operation_id=request.operation_id,
                    tenant_id=principal.tenant_id,
                    attempt=request.attempt,
                )
            )
        except ArtifactServiceError as error:
            raise _http_error(error) from None
        return artifact.to_public_ref()

    @router.get(
        "/{artifact_id}:download",
        response_model=ArtifactDownloadResponse,
        response_model_exclude_none=True,
    )
    async def download(
        artifact_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(principal_dependency)],
    ) -> ArtifactDownloadResponse:
        principal.require(Scope.INFERENCE_INVOKE)
        try:
            result = await service.download(artifact_id, tenant_id=principal.tenant_id)
        except ArtifactServiceError as error:
            raise _http_error(error) from None
        return ArtifactDownloadResponse(
            artifact=result.artifact.to_public_ref(),
            handle=EphemeralHandleResponse.from_handle(result.handle),
        )

    return router
