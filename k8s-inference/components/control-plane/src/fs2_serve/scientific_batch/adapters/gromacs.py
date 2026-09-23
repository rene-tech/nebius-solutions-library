"""NVIDIA GROMACS workflows use the existing durable scientific-batch queue."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fs2_gromacs import MODEL_ID, MPI_ENGINE, MPI_PARAMETER_SCHEMA, NVIDIA_IMAGE, PARAMETER_SCHEMA, RESULT_SCHEMA
from fs2_gromacs.contracts import canonical, normalize
from fs2_gromacs.files import digest_file, inventory, media_type

from ..catalog_adapter import ScientificStageExpansion
from ..models import (
    AdapterExecutionPlan,
    ArtifactMaterialization,
    MaterializationMode,
    ScientificInputArtifact,
    StageInvocation,
    StageWorkspaceDocument,
)
from .common import (
    ScientificAdapterError,
    assert_profile_identity,
    build_execution_plan,
    logical_stage_artifact,
    parse_public_request,
    run_workspace,
)
from .staged_workspace import completion_marker, contained_stable_file

if TYPE_CHECKING:
    from . import CollectedStageOutput

VARIANT_ID = "nvidia-2026-2-single-gpu-v1"
SOURCE_REPOSITORY = "nvidia/gromacs"
SOURCE_REVISION = NVIDIA_IMAGE.rsplit(":", 1)[1]
COLLECTOR_ID = VALIDATOR_ID = "gromacs-workflow-v1"
INPUT_ID = "gromacs-inputs"
MAX_INPUT_BYTES = 4 * 1024**3
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True


def public_input_contract() -> dict[str, Any]:
    return {
        "exactly_one_entry": True,
        "manifest_media_type": "application/vnd.fs2.scientific-manifest+json",
        "manifest_compression": "none",
        "entry_name_rule": "Exactly one gzip tar bundle containing all relative-path GROMACS inputs and includes.",
        "entry": {
            "name": INPUT_ID,
            "semantic_type": "gromacs-input-bundle/v1",
            "media_type": "application/x-tar",
            "compression": "gzip",
            "allowed_compressions": ["gzip"],
            "maximum_bytes": MAX_INPUT_BYTES,
        },
    }


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None = None,
    mpi: bool = False,
) -> AdapterExecutionPlan:
    request = parse_public_request(request_value, maximum_input_bytes=MAX_INPUT_BYTES)
    value = normalize(request.parameters, mpi=mpi)
    model_id = "gromacs-mpi" if mpi else MODEL_ID
    variant_id = "upstream-2026-2-mpi-v1" if mpi else VARIANT_ID
    repository = "gromacs/gromacs" if mpi else SOURCE_REPOSITORY
    source_revision = MPI_ENGINE.split("@")[1] if mpi else SOURCE_REVISION
    collector_id = "gromacs-mpi-workflow-v1" if mpi else COLLECTOR_ID
    assert_profile_identity(
        profile,
        model_id=model_id,
        repository=repository,
        revision=source_revision,
        parameter_schema=MPI_PARAMETER_SCHEMA if mpi else PARAMETER_SCHEMA,
        request=request,
        gpu_topology="multi-node" if mpi else "single-gpu",
    )
    entries = input_artifacts or ()
    if len(entries) != 1:
        raise ScientificAdapterError("GROMACS needs one verified gromacs-inputs bundle")
    entry = entries[0]
    if (entry.logical_artifact_id, entry.semantic_type, entry.media_type, entry.compression) != (
        INPUT_ID,
        "gromacs-input-bundle/v1",
        "application/x-tar",
        "gzip",
    ) or not 1 <= entry.size_bytes <= MAX_INPUT_BYTES:
        raise ScientificAdapterError("GROMACS bundle metadata differs from its typed input contract")
    invocations = []
    for job in value["jobs"]:
        workspace = run_workspace(model_id, operation_id, job["id"])
        command = (
            "python3",
            "-m",
            "fs2_gromacs.mpi" if mpi else "fs2_gromacs.worker",
            "--request",
            f"{workspace}/.fs2/request.json",
            "--workspace",
            workspace,
            "--operation-id",
            operation_id,
            "--job-id",
            job["id"],
            "--checkpoint-mode",
            "companion",
        )
        from .staged_workspace import wrap_stage_argv

        invocations.append(
            StageInvocation(
                stage_id="workflow",
                shard_id=job["id"],
                argv=wrap_stage_argv(workspace, command),
                environment=(),
                working_directory=workspace,
                consumes=(INPUT_ID,),
                produces=logical_stage_artifact(operation_id, "workflow", job["id"]),
                collector_id=collector_id,
                validator_id=collector_id,
                max_output_artifacts=10000,
                max_output_bytes=value["max_output_bytes"] + 16 * 1024**2,
                materializations=(
                    ArtifactMaterialization(
                        INPUT_ID, f"{workspace}/input.tar.gz", MaterializationMode.COPY_FILE, compression="gzip"
                    ),
                ),
                workspace_documents=(StageWorkspaceDocument(".fs2/request.json", canonical(value).decode()),),
            )
        )
    return build_execution_plan(
        model_id=model_id,
        variant_id=variant_id,
        source_revision=source_revision,
        request=request,
        profile=profile,
        expansions={
            "workflow": ScientificStageExpansion(
                shard_ids=tuple(job["id"] for job in value["jobs"]), gang_size=value["nodes"] if mpi else None
            )
        },
        invocations=tuple(invocations),
        required_model_artifacts=(),
    )


def collect_companion_output(
    invocation: StageInvocation, workspace: Path, *, mpi: bool = False
) -> CollectedStageOutput:
    from . import CollectedArtifactFile, CollectedStageOutput

    collector_id = "gromacs-mpi-workflow-v1" if mpi else COLLECTOR_ID
    if invocation.stage_id != "workflow" or invocation.collector_id != collector_id:
        raise ScientificAdapterError("GROMACS collector received another stage contract")
    completion_marker(invocation, workspace, label="GROMACS workflow")
    result_path, raw = contained_stable_file(
        workspace, "result.json", maximum_bytes=16 * 1024**2, label="GROMACS result"
    )
    _, request_raw = contained_stable_file(
        workspace, ".fs2/request.json", maximum_bytes=1024**2, label="GROMACS request"
    )
    result, request = json.loads(raw), normalize(json.loads(request_raw), mpi=mpi)
    operation = invocation.argv[invocation.argv.index("--operation-id") + 1]
    job = next(job for job in request["jobs"] if job["id"] == invocation.shard_id)
    recipe = hashlib.sha256(
        canonical({"request": request, "job": job["id"], "image": MPI_ENGINE if mpi else NVIDIA_IMAGE})
    ).hexdigest()
    if (
        result.get("schema"),
        result.get("operation_id"),
        result.get("job_id"),
        result.get("status"),
        result.get("recipe_sha256"),
    ) != (
        RESULT_SCHEMA,
        operation,
        invocation.shard_id,
        "succeeded",
        recipe,
    ) or result.get("completed_steps") != [step["id"] for step in job["steps"]]:
        raise ScientificAdapterError("GROMACS did not complete the exact frozen workflow")
    actual = inventory(workspace / "data", max_bytes=request["max_output_bytes"])
    if actual != result.get("files"):
        raise ScientificAdapterError("GROMACS output files differ from the completed inventory")
    files = [CollectedArtifactFile("result", "gromacs-workflow-result/v1", result_path, "application/json")]
    # Stable index names preserve arbitrary native filenames in result.json,
    # without flattening or constraining scientific filenames to manifest IDs.
    for index, item in enumerate(actual):
        path = workspace / "data" / item["path"]
        media = media_type(item["path"])
        files.append(CollectedArtifactFile(f"file-{index:05d}", "gromacs-file/v1", path, media))
    return CollectedStageOutput(
        tuple(files),
        {
            "validator_id": collector_id,
            "status": "passed",
            "command_count": len(result["commands"]),
            "completed_steps": result["completed_steps"],
            "file_count": len(actual),
            "result_sha256": digest_file(result_path),
            "scientific_convergence_claimed": False,
        },
    )
