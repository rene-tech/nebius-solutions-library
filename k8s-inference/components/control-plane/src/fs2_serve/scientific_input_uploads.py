"""Customer-owned immutable input uploads for scientific batch submission.

One customer input object is one durable Operation. The bootstrap reserves the
content address, the inline byte write fills it through the public gateway, and
finalization independently verifies the stored object before the immutable
pointer exists. Every step derives the tenant from the verified bearer
principal, so an upload can only ever be advanced by the tenant that opened it.
"""

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
from .scientific_batch.profile_catalog import ScientificProfileCatalog
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
    """Everything a client needs to finish the upload with no other endpoint.

    ``handle`` is the direct object-store path for a caller that can reach it.
    ``content_path`` is the equivalent gateway-only path, so a customer behind
    nothing but the public API can still write the exact same bytes.
    """

    operation_id: UUID
    upload_id: UUID
    handle: UploadHandle
    content_path: str
    max_content_bytes: int


class ScientificInputUploadFinalizeRequest(StrictModel):
    operation_id: UUID


class ScientificInputUploadReceipt(StrictModel):
    """Confirmation that the reserved content address now holds exact bytes."""

    operation_id: UUID
    upload_id: UUID
    sha256: str
    size_bytes: int
    media_type: str
    finalized: bool


def _identity(operation_id: UUID, suffix: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"fs2-serve/{UPLOAD_PROTOCOL}/{operation_id}/{suffix}")


def content_path(operation_id: UUID, upload_id: UUID) -> str:
    """The one public path that accepts the bytes for a reserved upload."""

    return f"/v1/scientific-artifacts/uploads/{upload_id}/content?operation_id={operation_id}"


class ScientificInputUploadService:
    """Reserve, verify and terminalize one customer input object per operation."""

    def __init__(
        self,
        *,
        store: Store,
        artifacts: ScientificArtifactControllerPort,
        profiles: ScientificProfileCatalog,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.profiles = profiles

    @property
    def max_content_bytes(self) -> int:
        return self.artifacts.max_inline_content_bytes

    async def begin(
        self,
        *,
        principal: Principal,
        request: ScientificInputUploadRequest,
        idempotency_key: str,
    ) -> ScientificInputUpload:
        principal.require(Scope.INFERENCE_INVOKE, model_id=request.model_id)
        # An input may be staged for a profile whose runtime is not qualified
        # yet, but never for a model this deployment does not declare at all.
        self.profiles.get(request.model_id, runnable=False)
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
            content_path=content_path(operation.id, upload_id),
            max_content_bytes=self.max_content_bytes,
            handle=UploadHandle(
                method="PUT",
                url=result.handle.url,
                expires_at=result.handle.expires_at,
                write_once=True,
                headers=dict(result.handle.headers),
            ),
        )

    async def _authorize(self, principal: Principal, operation_id: UUID, upload_id: UUID) -> UUID:
        """Resolve one upload the caller's own tenant is allowed to advance.

        A tenant mismatch, a foreign protocol and an upload identity that does
        not belong to the operation all collapse onto the same not-found
        answer, so nothing about another tenant's operations is observable.
        """

        operation = await self.store.get_operation(operation_id, tenant_id=principal.tenant_id)
        require_operation_access(principal, operation)
        if operation.protocol != UPLOAD_PROTOCOL:
            raise NotFoundError("scientific artifact upload operation not found")
        principal.require(Scope.INFERENCE_INVOKE, model_id=operation.model_id)
        if upload_id != _identity(operation.id, "upload"):
            raise NotFoundError("scientific artifact upload operation not found")
        return operation.id

    async def store_content(
        self,
        *,
        principal: Principal,
        operation_id: UUID,
        upload_id: UUID,
        content: bytes,
        declared_media_type: str | None = None,
        declared_size_bytes: int | None = None,
    ) -> ScientificInputUploadReceipt:
        """Accept the customer's exact bytes for an already-reserved upload."""

        resolved = await self._authorize(principal, operation_id, upload_id)
        receipt = await self.artifacts.store_upload_content(
            FinalizeArtifactUpload(
                upload_id=upload_id,
                operation_id=resolved,
                tenant_id=principal.tenant_id,
            ),
            content=content,
            declared_media_type=declared_media_type,
            declared_size_bytes=declared_size_bytes,
        )
        return ScientificInputUploadReceipt(
            operation_id=resolved,
            upload_id=upload_id,
            sha256=receipt.stored.digest.removeprefix("sha256:"),
            size_bytes=receipt.stored.size_bytes,
            media_type=receipt.stored.media_type,
            finalized=False,
        )

    async def finalize(
        self,
        *,
        principal: Principal,
        operation_id: UUID,
        upload_id: UUID,
    ) -> ArtifactRef:
        operation_uuid = await self._authorize(principal, operation_id, upload_id)
        artifact = await self.artifacts.finalize_upload(
            FinalizeArtifactUpload(
                upload_id=upload_id,
                operation_id=operation_uuid,
                tenant_id=principal.tenant_id,
            )
        )
        await self.store.complete_scientific_artifact_upload(
            operation_uuid,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
        )
        return artifact.to_public_ref()


__all__ = [
    "content_path",
    "ScientificInputUpload",
    "ScientificInputUploadFinalizeRequest",
    "ScientificInputUploadReceipt",
    "ScientificInputUploadRequest",
    "ScientificInputUploadService",
    "UPLOAD_PROTOCOL",
    "UploadHandle",
]
