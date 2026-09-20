"""Attempt-bound Cosmos calls using the parent customer's current policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field

from ..admission import AdmissionService
from ..models import AdmissionRequest, OperationView, Principal, Scope, StrictModel
from ..scientific_artifacts import ScientificArtifactControllerPort
from ..scientific_input_uploads import (
    ScientificInputUploadFinalizeRequest,
    ScientificInputUploadRequest,
    ScientificInputUploadService,
)
from ..scientific_run_result import ArtifactRef
from ..store import NotFoundError, Store
from .capability import ScientificWorkloadCapability, ScientificWorkloadCapabilityAuthority
from .workload_routes import WorkloadBatchRepository, authorize_workload_capability

PREFIX = "/internal/scientific-workloads/cosmos"
MODEL_ID = "cosmos3-nano"
PARENT_MODEL_ID = "cosmos3-lerobot-augmentation"
PARENT_CONTRACTS = frozenset({
    (PARENT_MODEL_ID, "augment-dataset", "main", "cosmos3-lerobot-v3-0-6-1"),
    ("physical-ai-video-augmentation", "augment-videos", "main", "paidf-video-v1"),
})


class ChildInvocation(StrictModel):
    operation: Literal["generate-media"]
    payload: dict[str, Any] = Field(max_length=64)


def scientific_child_router(
    *,
    authority: ScientificWorkloadCapabilityAuthority,
    batches: WorkloadBatchRepository,
    store: Store,
    admission: AdmissionService,
    uploads: ScientificInputUploadService,
    artifacts: ScientificArtifactControllerPort,
    principal_policy: Callable[[Principal], Awaitable[Principal]] | None = None,
) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["scientific-workloads-internal"])

    async def authorized(authorization: str | None) -> tuple[ScientificWorkloadCapability, Principal, OperationView]:
        capability, _, _ = await authorize_workload_capability(authority, batches, authorization)
        if (capability.model_id, capability.stage_id, capability.shard_id, capability.collector_id) not in PARENT_CONTRACTS:
            raise HTTPException(403, "workload cannot delegate Cosmos operations")
        parent = await store.get_operation(capability.operation_id, tenant_id=capability.tenant_id)
        token = await store.get_token(parent.token_id)
        now = datetime.now(UTC)
        if (
            parent.model_id != capability.model_id
            or parent.protocol != "scientific-batch-v1"
            or parent.parent_operation_id is not None
            or parent.status.terminal
            or (parent.deadline_at is not None and parent.deadline_at <= now)
            or token.revoked_at is not None
            or (token.expires_at is not None and token.expires_at <= now)
            or (token.tenant_id, token.principal_id) != (parent.tenant_id, parent.principal_id)
        ):
            raise HTTPException(409, "scientific parent delegation is no longer active")
        identity = Principal(
            token_id=token.id,
            token_prefix=token.prefix,
            principal_id=token.principal_id,
            tenant_id=token.tenant_id,
            scopes=frozenset(token.scopes),
            models=frozenset(token.models),
            expires_at=token.expires_at,
            request_budget=token.request_budget,
            gpu_seconds_budget=token.gpu_seconds_budget,
            max_concurrency=token.max_concurrency,
        )
        if principal_policy is not None:
            identity = await principal_policy(identity)
        identity.require(Scope.INFERENCE_INVOKE, MODEL_ID)
        return capability, identity, parent

    async def child(operation_id: UUID, capability: ScientificWorkloadCapability) -> OperationView:
        operation = await store.get_operation(operation_id, tenant_id=capability.tenant_id)
        if (
            operation.parent_operation_id != capability.operation_id
            or operation.parent_attempt_id != capability.attempt_id
            or operation.model_id != MODEL_ID
        ):
            raise NotFoundError("child operation not found")
        return operation

    def key(capability: ScientificWorkloadCapability, value: str | None) -> str:
        if value is None or not 8 <= len(value) <= 200:
            raise HTTPException(400, "Idempotency-Key must contain 8 to 200 characters")
        # A retried parent attempt cannot adopt an earlier attempt's child.
        return "delegated-" + hashlib.sha256(f"{capability.attempt_id}/{value}".encode()).hexdigest()

    @router.post("/v1/scientific-artifacts/uploads", status_code=201)
    async def begin_upload(
        request: ScientificInputUploadRequest,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        capability, identity, _ = await authorized(authorization)
        if (request.model_id, request.media_type) != (MODEL_ID, "video/mp4") or request.size_bytes > 512 * 1024**2:
            raise HTTPException(422, "Cosmos delegation accepts bounded MP4 episode inputs only")
        result = await uploads.begin(
            principal=identity,
            request=request,
            idempotency_key=key(capability, idempotency_key),
            parent_operation_id=capability.operation_id,
            parent_attempt_id=capability.attempt_id,
        )
        value = result.model_dump(mode="json")
        value["content_path"] = PREFIX + result.content_path
        return JSONResponse(value, status_code=201, headers={"cache-control": "no-store"})

    @router.put("/v1/scientific-artifacts/uploads/{upload_id}/content")
    async def upload_content(
        upload_id: UUID,
        request: Request,
        operation_id: Annotated[UUID, Query()],
        authorization: Annotated[str | None, Header()] = None,
        content_type: Annotated[str | None, Header()] = None,
        content_length: Annotated[int | None, Header(ge=0)] = None,
    ) -> Any:
        capability, identity, _ = await authorized(authorization)
        await child(operation_id, capability)
        maximum = min(uploads.max_content_bytes, 512 * 1024**2)
        if content_length is not None and content_length > maximum:
            raise HTTPException(413, "episode input exceeds the upload bound")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > maximum:
                raise HTTPException(413, "episode input exceeds the upload bound")
            content.extend(chunk)
        return await uploads.store_content(
            principal=identity,
            operation_id=operation_id,
            upload_id=upload_id,
            content=bytes(content),
            declared_media_type=content_type,
            declared_size_bytes=content_length,
        )

    @router.post("/v1/scientific-artifacts/uploads/{upload_id}:finalize")
    async def finalize_upload(
        upload_id: UUID,
        request: ScientificInputUploadFinalizeRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> ArtifactRef:
        capability, identity, _ = await authorized(authorization)
        await child(request.operation_id, capability)
        return await uploads.finalize(principal=identity, operation_id=request.operation_id, upload_id=upload_id)

    @router.post("/v1/models/cosmos3-nano:invoke", status_code=202)
    async def invoke(
        request: ChildInvocation,
        authorization: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> OperationView:
        capability, identity, parent = await authorized(authorization)
        payload = request.payload
        if (
            payload.get("mode") not in {"video-to-video", "transfer-video"}
            or payload.get("output_delivery") != "artifact"
        ):
            raise HTTPException(422, "delegation accepts artifact-backed video-to-video and transfer-video only")
        try:
            reference = ArtifactRef.model_validate(payload.get("input_reference"))
            source_id = UUID(reference.artifact_id)
        except ValueError:
            raise HTTPException(422, "episode input_reference must be a finalized artifact pointer") from None
        source = (await artifacts.download(source_id, tenant_id=identity.tenant_id)).artifact
        source_operation = await child(source.operation_id, capability)
        if source_operation.protocol != "scientific-artifact-upload-v1" or source.to_public_ref() != reference:
            raise HTTPException(403, "episode artifact is outside the parent attempt")
        return await admission.admit(
            identity,
            AdmissionRequest(
                model_id=MODEL_ID,
                operation="generate-media",
                protocol="native",
                idempotency_key=key(capability, idempotency_key),
                request_body=json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
                traceparent=parent.traceparent,
                deadline_at=parent.deadline_at,
                parent_operation_id=parent.id,
                parent_attempt_id=capability.attempt_id,
            ),
        )

    @router.get("/v1/operations/{operation_id}")
    async def operation_status(
        operation_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> OperationView:
        capability, identity, _ = await authorized(authorization)
        return await child(operation_id, capability)

    @router.get("/v1/operations/{operation_id}/result")
    async def operation_result(
        operation_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        capability, identity, _ = await authorized(authorization)
        await child(operation_id, capability)
        result = await store.get_operation_result(operation_id, tenant_id=identity.tenant_id)
        return JSONResponse(result.result, headers={"cache-control": "no-store"})

    @router.post("/v1/operations/{operation_id}:cancel")
    async def operation_cancel(
        operation_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> OperationView:
        capability, identity, _ = await authorized(authorization)
        await child(operation_id, capability)
        return await store.cancel_operation(operation_id, tenant_id=identity.tenant_id, actor=identity.principal_id)

    @router.get("/v1/artifacts/{artifact_id}/content")
    async def artifact_content(
        artifact_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        capability, identity, _ = await authorized(authorization)
        identity.require(Scope.OPERATIONS_RESULT)
        record = (await artifacts.download(artifact_id, tenant_id=identity.tenant_id)).artifact
        operation = await child(record.operation_id, capability)
        if operation.protocol != "native" or operation.status.value != "succeeded":
            raise NotFoundError("successful child artifact not found")
        stream = await artifacts.open_content(artifact_id, tenant_id=identity.tenant_id)
        return StreamingResponse(
            stream.chunks,
            media_type=record.media_type,
            headers={
                "cache-control": "no-store",
                "content-length": str(record.size_bytes),
                "x-fs2-artifact-sha256": record.digest.removeprefix("sha256:"),
            },
        )

    return router
