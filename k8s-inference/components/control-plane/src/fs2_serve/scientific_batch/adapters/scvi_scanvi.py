"""Single-cell training on the existing scientific queue/checkpoint transport."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from fs2_gromacs.files import inventory, media_type
from fs2_scvi import MODEL_ID, PARAMETER_SCHEMA, RESULT_SCHEMA
from fs2_scvi.contracts import MAX_INPUT_BYTES, canonical, normalize

from ..catalog_adapter import ScientificStageExpansion
from ..models import (
    ArtifactMaterialization,
    MaterializationMode,
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
from .staged_workspace import completion_marker, contained_stable_file, wrap_stage_argv

VARIANT_ID = "scvi-tools-1-5-batch-v1"
SOURCE_REPOSITORY = "scverse/scvi-tools"
SOURCE_REVISION = "56520c713eb1d2b245c72a0f60bc393b74198c91"
COLLECTOR_ID = VALIDATOR_ID = "scvi-scanvi-workflow-v1"
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True


def public_input_contract():
    return {
        "exactly_one_entry": False,
        "minimum_entries": 1,
        "maximum_entries": 2,
        "entry_name_rule": "anndata: raw-count .h5ad (application/x-hdf5, no outer compression); "
        "map-query additionally needs reference: a platform-produced reference.tar.gz (application/x-tar, gzip).",
        "maximum_bytes": MAX_INPUT_BYTES,
        "roles": {
            "anndata": {
                "semantic_type": "anndata-counts/v1",
                "media_type": "application/x-hdf5",
                "compression": "none",
            },
            "reference": {
                "semantic_type": "scvi-reference/v1",
                "media_type": "application/x-tar",
                "compression": "gzip",
            },
        },
    }


def validate_entries(request, entries):
    value = normalize(request["parameters"])
    expected = {"anndata", "reference"} if value["mode"] == "map-query" else {"anndata"}
    if len(entries) != len(expected) or {e.logical_artifact_id for e in entries} != expected:
        raise ScientificAdapterError("scVI input manifest must contain anndata and, only for map-query, reference")
    for entry in entries:
        spec = public_input_contract()["roles"][entry.logical_artifact_id]
        maximum = MAX_INPUT_BYTES if entry.logical_artifact_id == "anndata" else 1024**3
        if (entry.semantic_type, entry.media_type, entry.compression or "none") != (
            spec["semantic_type"],
            spec["media_type"],
            spec["compression"],
        ) or not 1 <= entry.size_bytes <= maximum:
            raise ScientificAdapterError("scVI input role, media type, compression or file size is invalid")


def compile_run(profile: Mapping[str, object], request_value: object, *, operation_id: str, input_artifacts=None):
    request = parse_public_request(request_value, maximum_input_bytes=MAX_INPUT_BYTES)
    value = normalize(request.parameters)
    if request.operation != "fit-transform":
        raise ScientificAdapterError("scVI/scANVI scientific operation must be fit-transform")
    entries = input_artifacts or ()
    validate_entries(request_value, entries)
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    workspace = run_workspace(MODEL_ID, operation_id, "main")
    command = (
        "python",
        "-m",
        "fs2_scvi.worker",
        "--request",
        f"{workspace}/.fs2/request.json",
        "--workspace",
        workspace,
        "--operation-id",
        operation_id,
        "--checkpoint-mode",
        "companion",
    )
    invocation = StageInvocation(
        stage_id="workflow",
        shard_id="main",
        argv=wrap_stage_argv(workspace, command),
        environment=(),
        working_directory=workspace,
        consumes=tuple(entry.logical_artifact_id for entry in entries),
        produces=logical_stage_artifact(operation_id, "workflow", "main"),
        collector_id=COLLECTOR_ID,
        validator_id=VALIDATOR_ID,
        max_output_artifacts=9998,
        max_output_bytes=value["max_output_bytes"] + 16 * 1024**2,
        materializations=tuple(
            ArtifactMaterialization(
                artifact_id=entry.logical_artifact_id,
                destination=f"{workspace}/"
                + ("input.h5ad" if entry.logical_artifact_id == "anndata" else "reference.tar.gz"),
                mode=MaterializationMode.COPY_FILE,
                compression=entry.compression,
            )
            for entry in entries
        ),
        workspace_documents=(StageWorkspaceDocument(".fs2/request.json", canonical(value).decode()),),
    )
    return build_execution_plan(
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        source_revision=SOURCE_REVISION,
        request=request,
        profile=profile,
        expansions={"workflow": ScientificStageExpansion(shard_ids=("main",))},
        invocations=(invocation,),
        required_model_artifacts=(),
    )


def collect_companion_output(invocation: StageInvocation, workspace: Path):
    from . import CollectedArtifactFile, CollectedStageOutput

    if (invocation.stage_id, invocation.shard_id, invocation.collector_id) != ("workflow", "main", COLLECTOR_ID):
        raise ScientificAdapterError("scVI collector received a different invocation")
    completion_marker(invocation, workspace, label="scVI/scANVI")
    path, raw = contained_stable_file(workspace, "result.json", maximum_bytes=16 * 1024**2, label="scVI result")
    _, request_raw = contained_stable_file(workspace, ".fs2/request.json", maximum_bytes=1024**2, label="scVI request")
    result, parameters = json.loads(raw), normalize(json.loads(request_raw))
    operation = invocation.argv[invocation.argv.index("--operation-id") + 1]
    if (result.get("schema"), result.get("operation_id"), result.get("status"), result.get("parameters")) != (
        RESULT_SCHEMA,
        operation,
        "succeeded",
        parameters,
    ):
        raise ScientificAdapterError("scVI result does not match the completed operation and frozen parameters")
    files = inventory(workspace / "data", max_bytes=parameters["max_output_bytes"])
    if files != result.get("files") or result.get("cells", 0) < 2 or result.get("latent_dimensions", 0) < 2:
        raise ScientificAdapterError("scVI artifact inventory or output dimensions are invalid")
    expected = {"latent_embeddings.csv", "reference.tar.gz", "preflight.json"}
    if parameters["method"] == "scanvi":
        expected |= {"predicted_labels.csv", "label_probabilities.csv"}
    if not expected <= {item["path"] for item in files}:
        raise ScientificAdapterError("scVI result is missing required semantic artifacts")
    artifacts = [CollectedArtifactFile("result", "scvi-workflow-result/v1", path, "application/json")]
    artifacts.extend(
        CollectedArtifactFile(
            f"file-{index:05d}", "scvi-file/v1", workspace / "data" / item["path"], media_type(item["path"])
        )
        for index, item in enumerate(files)
    )
    return CollectedStageOutput(
        tuple(artifacts),
        {
            "status": "passed",
            "validator_id": VALIDATOR_ID,
            "cells": result["cells"],
            "scientific_convergence_claimed": False,
        },
    )
