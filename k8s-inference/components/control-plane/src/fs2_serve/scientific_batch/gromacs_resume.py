"""Explicit native GROMACS continuation with a fresh, owner-authorized budget.

The old operation remains terminal. The new run uses normal admission and
streams immutable checkpoint files to its own workspace through existing input
materialization. Large trajectories never pass through API memory or scratch.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from fs2_gromacs.contracts import canonical, normalize, relative_path
from pydantic import Field

from ..models import Principal, Scope, StrictModel
from ..scientific_artifacts import ArtifactDirection, ArtifactRecord, ScientificArtifactControllerPort
from ..scientific_input_uploads import ScientificInputUploadRequest, ScientificInputUploadService
from ..scientific_run_result import ArtifactRef
from ..store import ConflictError
from .profile_catalog import ScientificRequestError
from .service import ScientificBatchService

MAX_WALL_SECONDS = 7 * 24 * 3600
MAX_CHECKPOINT_BYTES = 16 * 1024**2
CHECKPOINT_MEDIA = "application/vnd.fs2.gromacs-checkpoint+json"


class ContinuationError(ScientificRequestError):
    """Static customer errors without request contents or storage locations."""

    def __init__(self, message: str) -> None:
        super().__init__(message, public_detail=message)


class GromacsResumeRequest(StrictModel):
    job_id: str | None = Field(default=None, max_length=63)
    max_wall_seconds: int = Field(default=MAX_WALL_SECONDS, ge=60, le=MAX_WALL_SECONDS, strict=True)


def continuation_parameters(
    parameters: dict[str, Any],
    checkpoint: dict[str, Any],
    *,
    model_id: str,
    max_wall_seconds: int,
) -> dict[str, Any]:
    """Preserve physics/TPR and skip already completed preparation/analysis."""
    if type(max_wall_seconds) is not int or not 60 <= max_wall_seconds <= MAX_WALL_SECONDS:
        raise ContinuationError("max_wall_seconds must be between 60 and 604800 (seven days)")
    value = normalize(copy.deepcopy(parameters), mpi=model_id == "gromacs-mpi")
    value.pop("continuation_files", None)
    state = checkpoint["state"]
    job = next((job for job in value["jobs"] if job["id"] == state["job_id"]), None)
    if job is None:
        raise ContinuationError("checkpoint job is absent from the original request")
    completed = state.get("completed_steps", [])
    if not isinstance(completed, list) or completed != [step["id"] for step in job["steps"]][: len(completed)]:
        raise ContinuationError("checkpoint completed commands differ from the original workflow")
    remaining = job["steps"][len(completed) :]
    if not remaining:
        raise ContinuationError("this job already completed all its commands")
    active = state.get("active_step")
    indexed = {item["path"]: item for item in checkpoint["files"]}
    if active is not None:
        step = remaining[0]
        if active["id"] != step["id"] or step["command"] != "mdrun":
            raise ContinuationError("checkpoint active simulation differs from the remaining workflow")
        checkpoint_name = f"fs2-{step['id']}.cpt"
        path = str(PurePosixPath(step["directory"]) / checkpoint_name)
        if path not in indexed:
            raise ContinuationError("committed native checkpoint is missing; refusing a fresh start")
        args = step["args"]
        tpr_name = args[args.index("-s") + 1] if "-s" in args else "topol.tpr"
        tpr = indexed.get(str(PurePosixPath(step["directory"]) / tpr_name))
        if tpr is None or tpr["sha256"] != active["tpr_sha256"]:
            raise ContinuationError("the checkpoint's immutable simulation TPR is missing or changed")
        step["restart_checkpoint"] = checkpoint_name
    job["steps"] = remaining
    value["jobs"] = [job]
    value["max_wall_seconds"] = max_wall_seconds
    return normalize(value, mpi=model_id == "gromacs-mpi")


async def _read_checkpoint(
    artifacts: ScientificArtifactControllerPort,
    principal: Principal,
    pointer: dict[str, Any],
) -> dict[str, Any]:
    if pointer["size_bytes"] > MAX_CHECKPOINT_BYTES:
        raise ContinuationError("checkpoint manifest exceeds the supported size")
    stream = await artifacts.open_content(UUID(pointer["artifact_id"]), tenant_id=principal.tenant_id)
    content = bytearray()
    async for chunk in stream.chunks:
        content.extend(chunk)
        if len(content) > MAX_CHECKPOINT_BYTES:
            raise ContinuationError("checkpoint manifest exceeds the supported size")
    if len(content) != pointer["size_bytes"] or hashlib.sha256(content).hexdigest() != pointer["sha256"]:
        raise ContinuationError("checkpoint manifest integrity verification failed")
    value: dict[str, Any] = json.loads(content)
    return value


async def checkpoint_choices(
    *,
    batches: ScientificBatchService,
    artifacts: ScientificArtifactControllerPort,
    principal: Principal,
    operation_id: UUID,
) -> dict[str, Any]:
    principal.require(Scope.OPERATIONS_RESULT)
    status = await batches.status(operation_id, principal=principal)
    model_id = status["batch"]["model_id"]
    if model_id not in {"gromacs", "gromacs-mpi"}:
        raise ContinuationError("native GROMACS continuation is unavailable for this App")
    records = await artifacts.list_artifacts(operation_id, tenant_id=principal.tenant_id, stage_id="workflow")
    latest: dict[str | None, ArtifactRecord] = {}
    for record in sorted(records, key=lambda row: row.created_at):
        if record.direction is ArtifactDirection.OUTPUT and record.media_type == CHECKPOINT_MEDIA:
            latest[record.shard_id] = record
    jobs = []
    for shard, row in latest.items():
        pointer = row.to_public_ref().model_dump(mode="json")
        # MPI gang artifacts have no shard_id. The native checkpoint retains
        # the customer job ID; the frozen execution plan uses its gang identity.
        job_id = shard
        if job_id is None:
            checkpoint = await _read_checkpoint(artifacts, principal, pointer)
            job_id = checkpoint["state"]["job_id"]
        jobs.append({"job_id": job_id, "checkpoint": pointer, "committed_at": row.created_at.isoformat()})
    return {
        "operation_id": str(operation_id),
        "model_id": model_id,
        "status": status["batch"]["status"],
        "jobs": sorted(jobs, key=lambda row: row["job_id"]),
        "resume_tool": "resume_gromacs_workflow",
        "max_wall_seconds": MAX_WALL_SECONDS,
    }


def continuation_inputs(
    checkpoint: dict[str, Any],
    records: list[ArtifactRecord],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Reference same-job native artifacts without copying or downloading them.

    Identical files share an artifact but may map to multiple paths. Native logs
    and trajectories are retained; prior wrapper logs remain in the source run
    to avoid a new segment counter overwriting historical evidence.
    """
    files = checkpoint["files"]
    names = [relative_path(item["path"]) for item in files]
    if len(names) != len(set(names)) or len(names) > 9997:
        raise ContinuationError("checkpoint has duplicate paths or too many files")
    by_id = {str(row.artifact_id): row for row in records}
    entries: dict[str, dict[str, Any]] = {}
    paths = []
    for item in sorted(files, key=lambda item: item["path"]):
        ref = item["artifact"]
        record = by_id.get(ref["artifact_id"])
        if record is None or record.to_public_ref().model_dump(mode="json") != ref:
            raise ContinuationError("checkpoint references an artifact outside its original job")
        if (ref["sha256"], ref["size_bytes"]) != (item["sha256"], item["size_bytes"]):
            raise ContinuationError("checkpoint file identity changed")
        if item["path"] == "_fs2-continuation.json" or re.fullmatch(r"fs2-.+-segment-\d+\.log", item["path"]):
            continue
        if ref["artifact_id"] not in entries:
            entries[ref["artifact_id"]] = {
                "name": f"resume-{len(entries):05d}",
                "semantic_type": "gromacs-continuation-file/v1",
                "artifact": ref,
            }
        paths.append({"input_id": entries[ref["artifact_id"]]["name"], "path": item["path"]})
    return list(entries.values()), paths


async def _upload_json(
    uploads: ScientificInputUploadService,
    principal: Principal,
    *,
    content: bytes,
    model_id: str,
    key: str,
    media_type: str,
) -> ArtifactRef:
    reservation = await uploads.begin(
        principal=principal,
        idempotency_key=key,
        request=ScientificInputUploadRequest(
            model_id=model_id,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type=media_type,
        ),
    )
    await uploads.store_content(
        principal=principal, operation_id=reservation.operation_id, upload_id=reservation.upload_id, content=content
    )
    return await uploads.finalize(
        principal=principal, operation_id=reservation.operation_id, upload_id=reservation.upload_id
    )


async def resume_gromacs(
    *,
    batches: ScientificBatchService,
    artifacts: ScientificArtifactControllerPort,
    uploads: ScientificInputUploadService,
    principal: Principal,
    operation_id: UUID,
    request: GromacsResumeRequest,
    idempotency_key: str,
    require_mcp_invocable: bool = False,
) -> dict[str, Any]:
    choices = await checkpoint_choices(
        batches=batches, artifacts=artifacts, principal=principal, operation_id=operation_id
    )
    principal.require(Scope.INFERENCE_INVOKE, choices["model_id"])
    if choices["status"] not in {"failed", "cancelled"}:
        raise ConflictError("only a terminal failed or cancelled operation can be continued")
    selected = [job for job in choices["jobs"] if request.job_id is None or job["job_id"] == request.job_id]
    if len(selected) != 1:
        raise ContinuationError("select exactly one job_id with a committed checkpoint")
    choice = selected[0]
    checkpoint = await _read_checkpoint(artifacts, principal, choice["checkpoint"])
    state = checkpoint.get("state", {})
    if (state.get("schema"), state.get("operation_id"), state.get("job_id")) != (
        "fs2-serve.nebius.ai/gromacs-checkpoint/v1",
        str(operation_id),
        choice["job_id"],
    ):
        raise ContinuationError("checkpoint identity differs from the source operation and job")
    source = await batches.repository.get(operation_id, tenant_id=principal.tenant_id)
    if source.execution_plan is None:
        raise ContinuationError("original execution plan is unavailable")
    all_records = await artifacts.list_artifacts(operation_id, tenant_id=principal.tenant_id, stage_id="workflow")
    record = next(row for row in all_records if str(row.artifact_id) == choice["checkpoint"]["artifact_id"])
    invocation = source.execution_plan.invocation("workflow", record.shard_id)
    original = json.loads(
        next(
            document.canonical_json
            for document in invocation.workspace_documents
            if document.relative_path == ".fs2/request.json"
        )
    )
    parameters = continuation_parameters(
        original, checkpoint, model_id=choices["model_id"], max_wall_seconds=request.max_wall_seconds
    )
    records = [
        row for row in all_records if row.shard_id == record.shard_id and row.direction is ArtifactDirection.OUTPUT
    ]
    entries, paths = continuation_inputs(checkpoint, records)
    lineage = {
        "source_operation_id": str(operation_id),
        "source_job_id": choice["job_id"],
        "checkpoint": choice["checkpoint"],
        "generation": state["generation"],
        "previous_elapsed_seconds": state.get("elapsed_seconds"),
        "previous_completed_commands": state.get("completed_steps", []),
        "original_parameters_sha256": hashlib.sha256(canonical(original)).hexdigest(),
    }
    identity = hashlib.sha256(f"{operation_id}/{choice['job_id']}/{idempotency_key}".encode()).hexdigest()
    provenance = await _upload_json(
        uploads,
        principal,
        content=canonical(lineage),
        model_id=choices["model_id"],
        key=f"resume-provenance-{identity}",
        media_type="application/json",
    )
    name = f"resume-{len(entries):05d}"
    entries.append(
        {"name": name, "semantic_type": "gromacs-continuation-file/v1", "artifact": provenance.model_dump(mode="json")}
    )
    paths.append({"input_id": name, "path": "_fs2-continuation.json"})
    parameters["continuation_files"] = paths
    pointer = await _upload_json(
        uploads,
        principal,
        model_id=choices["model_id"],
        key=f"resume-manifest-{identity}",
        media_type="application/vnd.fs2.scientific-manifest+json",
        content=canonical(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                "manifest_id": f"resume-{identity[:32]}",
                "entries": entries,
            }
        ),
    )
    return await batches.submit(
        principal=principal,
        model_id=choices["model_id"],
        idempotency_key=f"resume-run-{identity}",
        require_mcp_invocable=require_mcp_invocable,
        request={
            "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "operation": "run-workflow",
            "service_class": source.scheduling.service_class,
            "input_manifest": pointer.model_dump(mode="json"),
            "parameters": parameters,
            "client_context": {
                "correlation_id": str(operation_id),
                "display_name": f"Continue {choice['job_id']} from checkpoint {state['generation']}",
            },
        },
    )
