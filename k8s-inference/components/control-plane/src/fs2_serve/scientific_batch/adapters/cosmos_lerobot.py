"""Candidate scientific-batch adapter for the LeRobot augmentation App.

The production registry installs this compiler and collector. Route exposure
still requires an active or qualified profile and digest-pinned execution map; registration
alone does not qualify the dataset workflow for customers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import UUID

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
    strict_object,
)
from .staged_workspace import completion_marker, contained_stable_file, wrap_stage_argv
from .verified_input import verified_manifest_entry

MODEL_ID = "cosmos3-lerobot-augmentation"
VARIANT_ID = "cosmos3-nano-lerobot-v3"
SOURCE_REPOSITORY = "nvidia/Cosmos3-Nano"
SOURCE_REVISION = "7a312c868bcce8e40b3eb40861300a9d0ba3fde1"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-request/v1"
PARAMETER_SCHEMA_VALUE = "fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-request/v1"
RESULT_SCHEMA = "fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-result/v1"
SOURCE_REFERENCE_SCHEMA = "fs2-serve.nebius.ai/lerobot-source-reference/v1"
INPUT_BUNDLE_ID = "lerobot-dataset"
INPUT_REFERENCE_ID = "lerobot-source"
INPUT_BUNDLE_SEMANTIC_TYPE = "lerobot-v3-bundle/v1"
INPUT_REFERENCE_SEMANTIC_TYPE = "lerobot-source-reference/v1"
COLLECTOR_ID = "cosmos3-lerobot-v3-0-6-1"
VALIDATOR_ID = "cosmos3-lerobot-v3-0-6-1"
RUNTIME_ENTRYPOINT = "/opt/fs2/lerobot-augmentation/.venv/bin/fs2-augment-lerobot"
MAX_BUNDLE_BYTES = 5 * 1024 * 1024 * 1024
MAX_SOURCE_REFERENCE_BYTES = 64 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 5 * 1024 * 1024 * 1024
MAX_VARIANTS = 8
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ScientificAdapterError("LeRobot augmentation parameters are not canonical JSON") from error


def _parameters(value: object) -> tuple[Mapping[str, object], str, int]:
    item = strict_object(
        value,
        required=frozenset(
            {"schema", "source", "selection", "variants", "augmentation", "actions", "failure_policy"}
        ),
        label="LeRobot augmentation parameters",
    )
    if item["schema"] != PARAMETER_SCHEMA_VALUE:
        raise ScientificAdapterError("LeRobot augmentation parameter schema is invalid")
    source = strict_object(
        item["source"],
        required=frozenset({"kind"}),
        optional=frozenset(
            {
                "repo_id",
                "revision",
                "credential_binding",
                "storage_binding",
                "object_prefix",
                "manifest_sha256",
                "artifact_id",
                "sha256",
                "size_bytes",
                "media_type",
                "compression",
            }
        ),
        label="LeRobot source",
    )
    kind = source["kind"]
    if kind not in {"huggingface", "object-store", "uploaded-bundle"}:
        raise ScientificAdapterError("LeRobot source kind is unsupported")
    variants = strict_object(
        item["variants"],
        required=frozenset({"count", "seeds"}),
        label="LeRobot variants",
    )
    count = variants["count"]
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_VARIANTS:
        raise ScientificAdapterError("LeRobot variant count is outside the bound")
    return item, cast(str, kind), count


def _verified_source(
    request: Any,
    parameters: Mapping[str, object],
    kind: str,
    input_artifacts: tuple[ScientificInputArtifact, ...],
) -> ScientificInputArtifact:
    if kind == "uploaded-bundle":
        artifact = verified_manifest_entry(
            request,
            input_artifacts,
            logical_artifact_id=INPUT_BUNDLE_ID,
            semantic_type=INPUT_BUNDLE_SEMANTIC_TYPE,
            media_type="application/x-tar",
            compressions=frozenset({"zstd"}),
            maximum_bytes=MAX_BUNDLE_BYTES,
            label="LeRobot augmentation",
        )
        source = cast(Mapping[str, object], parameters["source"])
        expected = {
            "artifact_id": str(artifact.artifact_id),
            "sha256": artifact.digest.removeprefix("sha256:"),
            "size_bytes": artifact.size_bytes,
            "media_type": artifact.media_type,
            "compression": artifact.compression,
        }
        actual = {key: source.get(key) for key in expected}
        if actual != expected:
            raise ScientificAdapterError("uploaded LeRobot source differs from its verified manifest entry")
        return artifact
    return verified_manifest_entry(
        request,
        input_artifacts,
        logical_artifact_id=INPUT_REFERENCE_ID,
        semantic_type=INPUT_REFERENCE_SEMANTIC_TYPE,
        media_type="application/json",
        compressions=frozenset({None, "none"}),
        maximum_bytes=MAX_SOURCE_REFERENCE_BYTES,
        label="LeRobot augmentation",
    )


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None = None,
) -> AdapterExecutionPlan:
    """Compile a single bounded CPU coordinator around attributed Cosmos jobs."""

    request = parse_public_request(request_value, maximum_input_bytes=MAX_BUNDLE_BYTES)
    parameters, source_kind, _variant_count = _parameters(request.parameters)
    if request.operation != "augment-lerobot-dataset":
        raise ScientificAdapterError("LeRobot augmentation supports only augment-lerobot-dataset")
    source = _verified_source(request, parameters, source_kind, input_artifacts or ())
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    workspace = run_workspace(MODEL_ID, operation_id, "augment-main")
    source_path = (
        f"{workspace}/inputs/lerobot-dataset.tar.zst"
        if source_kind == "uploaded-bundle"
        else f"{workspace}/inputs/source-reference.json"
    )
    output = logical_stage_artifact(operation_id, "augment-dataset", "main")
    command = (
        RUNTIME_ENTRYPOINT,
        "--request",
        f"{workspace}/.fs2/request.json",
        "--operation-id",
        operation_id,
        "--workspace",
        workspace,
        "--source-artifact",
        source_path,
    )
    invocation = StageInvocation(
        stage_id="augment-dataset",
        shard_id="main",
        argv=wrap_stage_argv(workspace, command),
        environment=(),
        working_directory=workspace,
        consumes=(source.logical_artifact_id,),
        produces=output,
        collector_id=COLLECTOR_ID,
        validator_id=VALIDATOR_ID,
        max_output_artifacts=MAX_VARIANTS + 1,
        max_output_bytes=MAX_OUTPUT_BYTES,
        materializations=(
            ArtifactMaterialization(
                artifact_id=source.logical_artifact_id,
                destination=source_path,
                mode=MaterializationMode.COPY_FILE,
                compression=source.compression,
            ),
        ),
        workspace_documents=(StageWorkspaceDocument(".fs2/request.json", _canonical_json(parameters)),),
    )
    return build_execution_plan(
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        source_revision=SOURCE_REVISION,
        request=request,
        profile=profile,
        expansions={"augment-dataset": ScientificStageExpansion(shard_ids=("main",))},
        invocations=(invocation,),
        required_model_artifacts=(),
    )


def _argument(invocation: StageInvocation, name: str) -> str:
    if invocation.argv.count(name) != 1 or invocation.argv.index(name) + 1 >= len(invocation.argv):
        raise ScientificAdapterError(f"LeRobot invocation has no exact {name} argument")
    return invocation.argv[invocation.argv.index(name) + 1]


def _result(value: object, operation_id: str) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    item = strict_object(
        value,
        required=frozenset({"schema", "operation_id", "status", "progress", "variants", "failures"}),
        label="LeRobot augmentation result",
    )
    if item["schema"] != RESULT_SCHEMA or item["operation_id"] != operation_id:
        raise ScientificAdapterError("LeRobot result identity differs from the invocation")
    if item["status"] not in {"succeeded", "partially-succeeded"}:
        raise ScientificAdapterError("LeRobot augmentation did not publish a successful result")
    variants = item["variants"]
    if not isinstance(variants, list) or not 1 <= len(variants) <= MAX_VARIANTS:
        raise ScientificAdapterError("LeRobot result variant count is outside the bound")
    if not all(isinstance(value, Mapping) for value in variants):
        raise ScientificAdapterError("LeRobot result contains an invalid variant")
    return cast(Mapping[str, Any], item), cast(list[Mapping[str, Any]], variants)


def collect_companion_output(invocation: StageInvocation, workspace: Path) -> Any:
    """Collect only reload-validated bundle files named by the frozen result."""

    from . import CollectedArtifactFile, CollectedStageOutput

    if (
        invocation.stage_id != "augment-dataset"
        or invocation.shard_id != "main"
        or invocation.collector_id != COLLECTOR_ID
        or invocation.validator_id != VALIDATOR_ID
    ):
        raise ScientificAdapterError("LeRobot collector received another stage contract")
    completion_sha256 = completion_marker(invocation, workspace, label="LeRobot augmentation")
    result_path, result_bytes = contained_stable_file(
        workspace,
        "result.json",
        maximum_bytes=MAX_RESULT_BYTES,
        label="LeRobot result",
    )
    try:
        result_value = json.loads(result_bytes)
    except (UnicodeError, ValueError) as error:
        raise ScientificAdapterError("LeRobot result is invalid JSON") from error
    operation_id = str(UUID(_argument(invocation, "--operation-id")))
    result, variants = _result(result_value, operation_id)
    _index_path, index_bytes = contained_stable_file(
        workspace,
        "artifact-index.json",
        maximum_bytes=MAX_RESULT_BYTES,
        label="LeRobot artifact index",
    )
    try:
        index = strict_object(
            json.loads(index_bytes),
            required=frozenset({"schema", "artifacts"}),
            label="LeRobot artifact index",
        )
    except (UnicodeError, ValueError) as error:
        raise ScientificAdapterError("LeRobot artifact index is invalid JSON") from error
    raw_index = index["artifacts"]
    if index["schema"] != "fs2-serve.nebius.ai/cosmos3-lerobot-artifact-index/v1":
        raise ScientificAdapterError("LeRobot artifact index schema is invalid")
    if not isinstance(raw_index, list) or len(raw_index) != len(variants):
        raise ScientificAdapterError("LeRobot artifact index cardinality differs from the result")
    artifacts: list[CollectedArtifactFile] = [
        CollectedArtifactFile(
            name="result",
            semantic_type="lerobot-augmentation-result/v1",
            path=result_path,
            media_type="application/json",
        )
    ]
    seen: set[int] = set()
    total_frames = 0
    for variant, index_entry in zip(variants, raw_index, strict=True):
        if not isinstance(index_entry, Mapping):
            raise ScientificAdapterError("LeRobot artifact index entry is invalid")
        variant_index = variant.get("variant_index")
        if isinstance(variant_index, bool) or not isinstance(variant_index, int) or variant_index in seen:
            raise ScientificAdapterError("LeRobot variant index is invalid or duplicated")
        seen.add(variant_index)
        pointer = variant.get("artifact")
        validation = variant.get("validation")
        if not isinstance(pointer, Mapping) or not isinstance(validation, Mapping):
            raise ScientificAdapterError("LeRobot variant lacks artifact or validation evidence")
        if (
            validation.get("reader") != "lerobot==0.6.1"
            or validation.get("status") != "passed"
            or not isinstance(validation.get("decoded_video_frames"), int)
        ):
            raise ScientificAdapterError("LeRobot variant lacks pinned reader/decode validation")
        total_frames += cast(int, validation["decoded_video_frames"])
        expected_path = f"artifacts/variant-{variant_index:02d}.tar.zst"
        if index_entry.get("path") != expected_path or any(
            index_entry.get(key) != pointer.get(key)
            for key in ("artifact_id", "sha256", "size_bytes", "media_type", "compression")
        ):
            raise ScientificAdapterError("LeRobot artifact index differs from the result pointer")
        path, content = contained_stable_file(
            workspace,
            expected_path,
            maximum_bytes=MAX_OUTPUT_BYTES,
            label=f"LeRobot variant {variant_index}",
        )
        if (
            pointer.get("media_type") != "application/x-tar"
            or pointer.get("compression") != "zstd"
            or pointer.get("size_bytes") != len(content)
            or pointer.get("sha256") != hashlib.sha256(content).hexdigest()
        ):
            raise ScientificAdapterError("LeRobot variant bundle identity differs from its validated result")
        artifacts.append(
            CollectedArtifactFile(
                name=f"variant-{variant_index:02d}",
                semantic_type="lerobot-v3-augmented-bundle/v1",
                path=path,
                media_type="application/x-tar",
                compression="zstd",
            )
        )
    progress = result.get("progress")
    if not isinstance(progress, Mapping):
        raise ScientificAdapterError("LeRobot result progress is invalid")
    return CollectedStageOutput(
        artifacts=tuple(artifacts),
        validation={
            "status": "passed",
            "validator_id": invocation.validator_id,
            "collector_id": invocation.collector_id,
            "completion_sha256": completion_sha256,
            "variant_count": len(variants),
            "decoded_video_frames": total_frames,
            "completed_units": progress.get("completed_units"),
            "total_units": progress.get("total_units"),
        },
    )


__all__ = [
    "COLLECTOR_ID",
    "MODEL_ID",
    "VALIDATOR_ID",
    "VARIANT_ID",
    "collect_companion_output",
    "compile_run",
]
