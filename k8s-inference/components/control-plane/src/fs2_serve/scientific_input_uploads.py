"""Customer-owned immutable input uploads for scientific batch submission."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field

from .auth import require_operation_access
from .models import AdmissionRequest, ModelId, Principal, Scope, StrictModel
from .scientific_artifacts import (
    ArtifactAccess,
    ArtifactCompression,
    ArtifactDirection,
    BeginArtifactUpload,
    FinalizeArtifactUpload,
    OpenStageAttempt,
    ScientificArtifactControllerPort,
)
from .scientific_run_result import ArtifactRef
from .store import NotFoundError, Store

RAW_SHA256 = r"^[a-f0-9]{64}$"
UPLOAD_PROTOCOL = "scientific-artifact-upload-v1"
UPLOAD_MODEL_REVISION = "scientific-input-artifact-v1"
CompressionInput = ArtifactCompression | Literal["none"]


class ScientificInputUploadRequest(StrictModel):
    """Immutable object identity; tenant and principal come from authentication."""

    model_id: ModelId
    sha256: Annotated[str, Field(pattern=RAW_SHA256)]
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=3, max_length=128)
    compression: CompressionInput | None = None

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()


class UploadHandle(StrictModel):
    method: Literal["PUT"]
    url: str
    expires_at: datetime
    write_once: Literal[True]
    headers: dict[str, str]


class ScientificInputUpload(StrictModel):
    operation_id: UUID
    upload_id: UUID
    handle: UploadHandle


class ScientificInputUploadFinalizeRequest(StrictModel):
    operation_id: UUID


def _identity(operation_id: UUID, suffix: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"fs2-serve/{UPLOAD_PROTOCOL}/{operation_id}/{suffix}")


class ScientificInputUploadService:
    """Reserve, verify and terminalize one customer input object per operation."""

    def __init__(self, *, store: Store, artifacts: ScientificArtifactControllerPort) -> None:
        self.store = store
        self.artifacts = artifacts

    async def begin(
        self,
        *,
        principal: Principal,
        request: ScientificInputUploadRequest,
        idempotency_key: str,
    ) -> ScientificInputUpload:
        principal.require(Scope.INFERENCE_INVOKE, model_id=request.model_id)
        operation = await self.store.append_operation(
            principal=principal,
            admission=AdmissionRequest(
                model_id=request.model_id,
                operation="upload",
                protocol=UPLOAD_PROTOCOL,
                idempotency_key=idempotency_key,
                request_body=request.canonical_bytes(),
                request_content_type="application/json",
            ),
            model_revision=UPLOAD_MODEL_REVISION,
            reserved_gpu_seconds=0,
            max_attempts=1,
        )
        attempt_id = _identity(operation.id, "attempt")
        upload_id = _identity(operation.id, "upload")
        await self.artifacts.open_attempt(
            OpenStageAttempt(
                attempt_id=attempt_id,
                operation_id=operation.id,
                tenant_id=principal.tenant_id,
                stage_id="input-upload",
                attempt_number=1,
                started_at=operation.accepted_at,
            )
        )
        result = await self.artifacts.begin_upload(
            BeginArtifactUpload(
                upload_id=upload_id,
                attempt_id=attempt_id,
                operation_id=operation.id,
                tenant_id=principal.tenant_id,
                direction=ArtifactDirection.INPUT,
                expected_digest=f"sha256:{request.sha256}",
                expected_size_bytes=request.size_bytes,
                media_type=request.media_type.lower(),
                compression=request.compression if isinstance(request.compression, ArtifactCompression) else None,
                access=ArtifactAccess(),
            )
        )
        return ScientificInputUpload(
            operation_id=operation.id,
            upload_id=upload_id,
            handle=UploadHandle(
                method="PUT",
                url=result.handle.url,
                expires_at=result.handle.expires_at,
                write_once=True,
                headers=dict(result.handle.headers),
            ),
        )

    async def finalize(
        self,
        *,
        principal: Principal,
        operation_id: UUID,
        upload_id: UUID,
    ) -> ArtifactRef:
        operation = await self.store.get_operation(operation_id, tenant_id=principal.tenant_id)
        require_operation_access(principal, operation)
        if operation.protocol != UPLOAD_PROTOCOL:
            raise NotFoundError("scientific artifact upload operation not found")
        principal.require(Scope.INFERENCE_INVOKE, model_id=operation.model_id)
        expected_upload_id = _identity(operation.id, "upload")
        if upload_id != expected_upload_id:
            raise NotFoundError("scientific artifact upload operation not found")
        artifact = await self.artifacts.finalize_upload(
            FinalizeArtifactUpload(
                upload_id=upload_id,
                operation_id=operation.id,
                tenant_id=principal.tenant_id,
            )
        )
        await self.store.complete_scientific_artifact_upload(
            operation.id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
        )
        return artifact.to_public_ref()


__all__ = [
    "ScientificInputUpload",
    "ScientificInputUploadFinalizeRequest",
    "ScientificInputUploadRequest",
    "ScientificInputUploadService",
    "UPLOAD_PROTOCOL",
    "UploadHandle",
]
