"""Capability-authenticated artifact port for scientific Job companions."""

from __future__ import annotations

import hmac
from dataclasses import replace
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Path, status
from fastapi.responses import Response
from pydantic import Field

from ..models import StrictModel
from ..scientific_artifact_routes import EphemeralHandleResponse
from ..scientific_artifacts import (
    ArtifactAccess,
    ArtifactAccessProfile,
    ArtifactCompression,
    ArtifactDirection,
    ArtifactNotFoundError,
    ArtifactRecord,
    BeginArtifactUpload,
    FinalizeArtifactUpload,
    OpenStageAttempt,
    ScientificArtifactControllerPort,
)
from ..scientific_run_result import ArtifactRef
from .capability import (
    CapabilityArtifact,
    ScientificWorkloadCapability,
    ScientificWorkloadCapabilityAuthority,
    capability_artifacts_digest,
)
from .models import AttemptOutcome, ExecutionMode, ScientificAttemptState, ScientificBatchState
from .native_workflows import workflow_for_binding
from .stage_descriptor import descriptor_bytes


class WorkloadBatchRepository(Protocol):
    async def get(self, operation_id: UUID, *, tenant_id: str) -> ScientificBatchState: ...


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
    artifact: ArtifactRef
    handle: EphemeralHandleResponse


class WorkloadDownloadsRequest(StrictModel):
    artifact_ids: tuple[UUID, ...] = Field(min_length=1, max_length=128)


class WorkloadUploadsRequest(StrictModel):
    uploads: tuple[WorkloadUploadRequest, ...] = Field(min_length=1, max_length=64)


class WorkloadFinalizationsRequest(StrictModel):
    upload_ids: tuple[UUID, ...] = Field(min_length=1, max_length=64)


def _bearer(value: str | None) -> str:
    if value is None or not value.startswith("Bearer ") or value.count(" ") != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="workload capability required")
    return value.removeprefix("Bearer ")


async def authorize_workload_capability(
    authority: ScientificWorkloadCapabilityAuthority,
    batches: WorkloadBatchRepository,
    authorization: str | None,
) -> tuple[ScientificWorkloadCapability, ScientificBatchState, ScientificAttemptState]:
    try:
        capability = authority.verify(_bearer(authorization))
        state = await batches.get(capability.operation_id, tenant_id=capability.tenant_id)
        stage = state.stage(capability.stage_id)
        # "gang" is also a valid independent job ID, including single-node MPI.
        # Only a true JobSet has the historical None attempt-shard identity.
        # Resolve against the immutable admitted plan, never the current catalog.
        is_gang = state.plan.stage(capability.stage_id).mode is ExecutionMode.TRUE_GANG
        attempt = stage.latest_attempt(None if is_gang and capability.shard_id == "gang" else capability.shard_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="workload capability rejected",
        ) from None
    if (
        state.cancel_requested
        or state.status.value not in {"queued", "running"}
        or state.batch_id != capability.batch_id
        or state.workload_id != capability.workload_id
        or state.model_id != capability.model_id
        or state.variant_id != capability.variant_id
        or attempt is None
        or attempt.attempt_id != capability.attempt_id
        or attempt.attempt_number != capability.attempt_number
        or attempt.outcome is not AttemptOutcome.ACTIVE
        or attempt.resource_released
        or attempt.deletion_requested
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
    if capability.artifacts_digest is not None:
        try:
            if state.input_manifest is None:
                raise ValueError("missing immutable input manifest")
            sources = {item.logical_artifact_id: item for item in state.input_manifest.entries}
            bindings = tuple(
                CapabilityArtifact(
                    logical_artifact_id=item.artifact_id,
                    artifact_id=(source := sources[item.artifact_id]).artifact_id,
                    digest=source.digest,
                    size_bytes=source.size_bytes,
                    media_type=source.media_type,
                    compression=source.compression,
                )
                for item in invocation.materializations
            )
            if not hmac.compare_digest(capability_artifacts_digest(bindings), capability.artifacts_digest):
                raise ValueError("immutable input identities changed")
        except (KeyError, ValueError):
            raise HTTPException(status_code=409, detail="workload capability input binding changed") from None
        capability = replace(capability, artifacts=bindings)
    return capability, state, attempt


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
        return await authorize_workload_capability(authority, batches, authorization)

    async def checkpoint_records(
        capability: ScientificWorkloadCapability, attempt: ScientificAttemptState
    ) -> list[ArtifactRecord]:
        # Recovery never widens a worker to another customer's, operation's,
        # stage's, or replica's files. Unrelated Apps retain the old boundary.
        if workflow_for_binding(capability.model_id, capability.stage_id, capability.collector_id) is None:
            raise HTTPException(status_code=403, detail="this workload has no native checkpoint contract")
        records = await artifacts.list_artifacts(
            capability.operation_id, tenant_id=capability.tenant_id, stage_id=capability.stage_id
        )
        return [
            record
            for record in records
            if record.shard_id == attempt.shard_id and record.direction is ArtifactDirection.OUTPUT
        ]

    @router.get("/stage-descriptor")
    async def stage_descriptor(authorization: Annotated[str | None, Header()] = None) -> Response:
        capability, state, attempt = await authorized(authorization)
        if capability.model_id not in {"gromacs", "gromacs-mpi"} or state.execution_plan is None:
            raise HTTPException(status_code=403, detail="this workload has no remote stage descriptor")
        invocation = state.execution_plan.invocation(capability.stage_id, attempt.shard_id)
        return Response(
            content=descriptor_bytes(invocation, capability.artifacts),
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/artifacts:download", response_model=list[WorkloadDownloadResponse])
    async def download_inputs(
        request: WorkloadDownloadsRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> list[WorkloadDownloadResponse]:
        capability, _, attempt = await authorized(authorization)
        if capability.model_id not in {"gromacs", "gromacs-mpi"}:
            raise HTTPException(status_code=403, detail="this workload has no bulk input contract")
        bindings = {item.artifact_id: item for item in capability.artifacts}
        if len(set(request.artifact_ids)) != len(request.artifact_ids):
            raise HTTPException(status_code=403, detail="artifact is outside workload capability")
        try:
            results = await artifacts.downloads(request.artifact_ids, tenant_id=capability.tenant_id)
        except ArtifactNotFoundError:
            raise HTTPException(status_code=403, detail="artifact is outside workload capability") from None
        responses = []
        for artifact_id, result in zip(request.artifact_ids, results, strict=True):
            binding: CapabilityArtifact | ArtifactRecord | None = bindings.get(artifact_id)
            if binding is None:
                record = result.artifact
                # The same boundary as checkpoint_records + the single-file
                # route, without rereading every historical row per128 files.
                # Never return the generated handle until scope is verified.
                if (
                    workflow_for_binding(capability.model_id, capability.stage_id, capability.collector_id) is None
                    or record.operation_id != capability.operation_id
                    or record.stage_id != capability.stage_id
                    or record.shard_id != attempt.shard_id
                    or record.direction is not ArtifactDirection.OUTPUT
                ):
                    raise HTTPException(status_code=403, detail="artifact is outside workload capability")
                binding = record
            if (
                result.artifact.digest != binding.digest
                or result.artifact.size_bytes != binding.size_bytes
                or result.artifact.media_type != binding.media_type
                or (None if result.artifact.compression is None else result.artifact.compression.value)
                != binding.compression
            ):
                raise HTTPException(status_code=409, detail="artifact metadata changed")
            responses.append(
                WorkloadDownloadResponse(
                    artifact=result.artifact.to_public_ref(), handle=EphemeralHandleResponse.of(result.handle)
                )
            )

        return responses

    @router.get("/checkpoints/latest")
    async def latest_checkpoint(authorization: Annotated[str | None, Header()] = None) -> dict[str, ArtifactRef | None]:
        capability, _, attempt = await authorized(authorization)
        workflow = workflow_for_binding(capability.model_id, capability.stage_id, capability.collector_id)
        if workflow is None:
            raise HTTPException(status_code=403, detail="this workload has no native checkpoint contract")
        records = [
            record
            for record in await checkpoint_records(capability, attempt)
            if record.media_type == workflow.checkpoint_media_type
        ]
        latest = max(records, key=lambda record: record.created_at) if records else None
        return {"checkpoint": latest.to_public_ref() if latest else None}

    @router.get("/artifacts/{artifact_id}:download", response_model=WorkloadDownloadResponse)
    async def download(
        artifact_id: Annotated[UUID, Path()],
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkloadDownloadResponse:
        capability, _, attempt = await authorized(authorization)
        binding: CapabilityArtifact | ArtifactRecord | None = next(
            (item for item in capability.artifacts if item.artifact_id == artifact_id), None
        )
        if binding is None:
            binding = next(
                (
                    record
                    for record in await checkpoint_records(capability, attempt)
                    if record.artifact_id == artifact_id
                ),
                None,
            )
            if binding is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="artifact is outside workload capability"
                )
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
        return await reserve_upload(request, capability)

    async def reserve_upload(
        request: WorkloadUploadRequest, capability: ScientificWorkloadCapability
    ) -> WorkloadUploadResponse:
        result = await artifacts.begin_upload(upload_request(request, capability))
        return WorkloadUploadResponse(
            upload_id=result.upload.upload_id,
            handle=EphemeralHandleResponse.of(result.handle),
        )

    def upload_request(request: WorkloadUploadRequest, capability: ScientificWorkloadCapability) -> BeginArtifactUpload:
        compression = request.compression if isinstance(request.compression, ArtifactCompression) else None
        return BeginArtifactUpload(
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
        )

    @router.post("/uploads:batch", response_model=list[WorkloadUploadResponse], status_code=status.HTTP_201_CREATED)
    async def begin_uploads(
        request: WorkloadUploadsRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> list[WorkloadUploadResponse]:
        capability, state, attempt = await authorized(authorization)
        if capability.model_id not in {"gromacs", "gromacs-mpi"}:
            raise HTTPException(status_code=403, detail="this workload has no bulk upload contract")
        if len({item.upload_id for item in request.uploads}) != len(request.uploads):
            raise HTTPException(status_code=422, detail="upload identities must be distinct")
        await artifacts.open_attempt(
            OpenStageAttempt(
                attempt_id=capability.attempt_id,
                operation_id=capability.operation_id,
                tenant_id=capability.tenant_id,
                stage_id=capability.stage_id,
                shard_id=attempt.shard_id,
                attempt_number=capability.attempt_number,
                started_at=attempt.started_at or state.scheduling.captured_at,
            )
        )
        results = await artifacts.begin_uploads(tuple(upload_request(item, capability) for item in request.uploads))
        return [
            WorkloadUploadResponse(upload_id=result.upload.upload_id, handle=EphemeralHandleResponse.of(result.handle))
            for result in results
        ]

    @router.post("/uploads:finalize", response_model=list[ArtifactRef])
    async def finalize_uploads(
        request: WorkloadFinalizationsRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> list[ArtifactRef]:
        capability, _, _ = await authorized(authorization)
        if capability.model_id not in {"gromacs", "gromacs-mpi"}:
            raise HTTPException(status_code=403, detail="this workload has no bulk upload contract")
        if len(set(request.upload_ids)) != len(request.upload_ids):
            raise HTTPException(status_code=422, detail="upload identities must be distinct")
        results = await artifacts.finalize_uploads(
            tuple(
                FinalizeArtifactUpload(
                    upload_id=upload_id,
                    operation_id=capability.operation_id,
                    tenant_id=capability.tenant_id,
                )
                for upload_id in request.upload_ids
            )
        )
        return [record.to_public_ref() for record in results]

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
