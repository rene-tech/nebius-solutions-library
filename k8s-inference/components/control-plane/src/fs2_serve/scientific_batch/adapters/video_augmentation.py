"""Verified multi-video NVIDIA PAIDF App compiler and bounded result collector."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from jsonschema import Draft202012Validator

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
from .staged_workspace import completion_marker, contained_stable_file, wrap_stage_argv

MODEL_ID = "physical-ai-video-augmentation"
VARIANT_ID = "paidf-cosmos3-nano-v1"
SOURCE_REPOSITORY = "NVIDIA/paidf-augmentation"
SOURCE_REVISION = "bc5719362492a1e3b40bd7d33b43c46dd89efad5"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/video-augmentation-request/v1"
RESULT_SCHEMA = "fs2-serve.nebius.ai/video-augmentation-result/v1"
COLLECTOR_ID = VALIDATOR_ID = "paidf-video-v1"
MAX_VIDEO_BYTES = 128 * 1024**2
MAX_TOTAL_BYTES = 2 * 1024**3
REQUIRES_VERIFIED_INPUT_ARTIFACTS = True


def public_input_contract():
    return {
        "exactly_one_entry": False,
        "minimum_entries": 1,
        "maximum_entries": 64,
        "name_pattern": "^video-[0-9]{4}$",
        "semantic_type": "source-video/v1",
        "media_type": "video/mp4",
        "compression": "none",
        "maximum_bytes": MAX_VIDEO_BYTES,
        "maximum_total_bytes": MAX_TOTAL_BYTES,
        "entry_name_rule": "One verified MP4 per parameters.items entry; name equals item.id "
        "and SHA-256 equals item.sha256. No extra entries.",
    }


def parameters(value):
    schema = json.loads((Path(__file__).parents[2] / "model_input_schemas/video-augmentation.json").read_text())
    try:
        Draft202012Validator(schema).validate(value)
    except Exception as error:
        raise ScientificAdapterError("video augmentation parameters violate the closed public schema") from error
    ids = [item["id"] for item in value["items"]]
    names = [item["source_name"] for item in value["items"]]
    if (
        len(set(ids)) != len(ids)
        or len(set(names)) != len(names)
        or any(
            name.startswith("/")
            or "\\" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or any(ord(c) < 32 for c in name)
            for name in names
        )
    ):
        raise ScientificAdapterError("video IDs/source names must be unique safe relative names")
    if len(ids) > 1 and not value.get("approved_recipe_sha256"):
        raise ScientificAdapterError("multi-video batches require an approved preview recipe hash")
    return value


def validate_entries(request, entries):
    items = parameters(request["parameters"])["items"]
    by_id = {entry.logical_artifact_id: entry for entry in entries}
    if len(by_id) != len(entries) or set(by_id) != {item["id"] for item in items}:
        raise ScientificAdapterError("input manifest must match every requested video exactly once")
    if sum(entry.size_bytes for entry in entries) > MAX_TOTAL_BYTES:
        raise ScientificAdapterError("video batch exceeds 2 GiB input bound")
    for item in items:
        entry = by_id[item["id"]]
        if (
            entry.semantic_type != "source-video/v1"
            or entry.media_type != "video/mp4"
            or entry.compression not in {None, "none"}
            or not 16 <= entry.size_bytes <= MAX_VIDEO_BYTES
            or entry.digest.removeprefix("sha256:") != item["sha256"]
            or str(entry.artifact_id) == request["input_manifest"]["artifact_id"]
        ):
            raise ScientificAdapterError("video input metadata differs from its immutable manifest identity")


def compile_run(
    profile: Mapping[str, object],
    request_value: object,
    *,
    operation_id: str,
    input_artifacts: tuple[ScientificInputArtifact, ...] | None = None,
) -> AdapterExecutionPlan:
    request = parse_public_request(request_value, maximum_input_bytes=MAX_TOTAL_BYTES)
    value = parameters(request.parameters)
    if request.operation != "augment-videos":
        raise ScientificAdapterError("video augmentation supports only augment-videos")
    if (
        request.input_manifest.media_type != "application/vnd.fs2.scientific-manifest+json"
        or request.input_manifest.compression not in {None, "none"}
    ):
        raise ScientificAdapterError("input_manifest must be an uncompressed scientific manifest")
    validate_entries(request_value, input_artifacts or ())
    assert_profile_identity(
        profile,
        model_id=MODEL_ID,
        repository=SOURCE_REPOSITORY,
        revision=SOURCE_REVISION,
        parameter_schema=PARAMETER_SCHEMA,
        request=request,
    )
    workspace = run_workspace(MODEL_ID, operation_id, "augment-main")
    command = (
        "/opt/paidf/.venv/bin/python",
        "-m",
        "fs2_video.worker",
        "--request",
        f"{workspace}/.fs2/request.json",
        "--operation-id",
        operation_id,
        "--workspace",
        workspace,
    )
    entries = input_artifacts or ()
    invocation = StageInvocation(
        stage_id="augment-videos",
        shard_id="main",
        argv=wrap_stage_argv(workspace, command),
        environment=(),
        working_directory=workspace,
        consumes=tuple(entry.logical_artifact_id for entry in entries),
        produces=logical_stage_artifact(operation_id, "augment-videos", "main"),
        collector_id=COLLECTOR_ID,
        validator_id=VALIDATOR_ID,
        max_output_artifacts=len(entries) + 1,
        max_output_bytes=MAX_TOTAL_BYTES + 2 * 1024**2,
        materializations=tuple(
            ArtifactMaterialization(
                artifact_id=entry.logical_artifact_id,
                destination=f"{workspace}/inputs/{entry.logical_artifact_id}.mp4",
                mode=MaterializationMode.COPY_FILE,
                compression=entry.compression,
            )
            for entry in entries
        ),
        workspace_documents=(
            StageWorkspaceDocument(
                ".fs2/request.json", json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            ),
        ),
    )
    return build_execution_plan(
        model_id=MODEL_ID,
        variant_id=VARIANT_ID,
        source_revision=SOURCE_REVISION,
        request=request,
        profile=profile,
        expansions={"augment-videos": ScientificStageExpansion(shard_ids=("main",))},
        invocations=(invocation,),
        required_model_artifacts=(),
    )


def collect_companion_output(invocation: StageInvocation, workspace: Path):
    from . import CollectedArtifactFile, CollectedStageOutput

    if (invocation.stage_id, invocation.shard_id, invocation.collector_id, invocation.validator_id) != (
        "augment-videos",
        "main",
        COLLECTOR_ID,
        VALIDATOR_ID,
    ):
        raise ScientificAdapterError("video collector received another stage contract")
    completion = completion_marker(invocation, workspace, label="video augmentation")
    result_path, raw = contained_stable_file(workspace, "result.json", maximum_bytes=2 * 1024**2, label="video result")
    _, request_raw = contained_stable_file(
        workspace, ".fs2/request.json", maximum_bytes=128 * 1024, label="video request"
    )
    result = json.loads(raw)
    requested = parameters(json.loads(request_raw))
    operation = str(UUID(invocation.argv[invocation.argv.index("--operation-id") + 1]))
    if result.get("schema") != RESULT_SCHEMA or result.get("operation_id") != operation:
        raise ScientificAdapterError("video result identity differs from invocation")
    items = result.get("items", [])
    if not isinstance(items, list) or len(items) != len(requested["items"]):
        raise ScientificAdapterError("video result cardinality differs from frozen request")
    recipe = dict(result.get("recipe", {}))
    recipe_hash = recipe.pop("sha256", None)
    canonical = (json.dumps(recipe, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if recipe_hash != hashlib.sha256(canonical).hexdigest() or recipe.get("paidf_revision") != SOURCE_REVISION:
        raise ScientificAdapterError("video recipe identity is invalid")
    if requested.get("approved_recipe_sha256") and recipe_hash != requested["approved_recipe_sha256"]:
        raise ScientificAdapterError("batch recipe differs from approved preview")
    actual_params = recipe.get("parameters", {})
    if any(actual_params.get(key) != value for key, value in requested["recipe"].items()):
        raise ScientificAdapterError("result recipe parameters differ from request")
    artifacts = [
        CollectedArtifactFile(
            name="result", semantic_type="video-augmentation-result/v1", path=result_path, media_type="application/json"
        )
    ]
    counts = {status: 0 for status in ("accepted", "rejected", "failed")}
    total = len(raw)
    for item, source in zip(items, requested["items"], strict=True):
        if (item.get("id"), item.get("source_name"), item.get("source_sha256"), item.get("recipe_sha256")) != (
            source["id"],
            source["source_name"],
            source["sha256"],
            recipe_hash,
        ):
            raise ScientificAdapterError("video result source identity changed")
        status = item.get("status")
        if status not in counts:
            raise ScientificAdapterError("video result has an invalid status")
        counts[status] += 1
        if status == "failed":
            if item.get("artifact"):
                raise ScientificAdapterError("failed item must not publish a video")
            continue
        if status == "accepted" and any(
            item.get("checks", {}).get(name, {}).get("passed") is not True
            for name in ("hallucination_check", "attribute_verification")
        ):
            raise ScientificAdapterError("accepted video lacks both quality checks")
        pointer = item.get("artifact", {})
        expected = f"outputs/{source['id']}/video.mp4"
        if pointer.get("path") != expected or pointer.get("media_type") != "video/mp4":
            raise ScientificAdapterError("video artifact path or media type is invalid")
        path, data = contained_stable_file(workspace, expected, maximum_bytes=MAX_VIDEO_BYTES, label="augmented video")
        if (
            pointer.get("size_bytes") != len(data)
            or pointer.get("sha256") != hashlib.sha256(data).hexdigest()
            or b"ftyp" not in data[:32]
        ):
            raise ScientificAdapterError("video artifact bytes differ from validated pointer")
        before, after = item.get("input", {}), item.get("output", {})
        dimensions = (before.get("width"), before.get("height"))
        if (
            dimensions not in {(640, 480), (1280, 720)}
            or type(before.get("frames")) is not int
            or not 16 <= before["frames"] <= 400
            or type(before.get("fps")) is not int
            or not 1 <= before["fps"] <= 30
            or before.get("sha256") != source["sha256"]
            or after.get("sha256") != pointer["sha256"]
            or after.get("size_bytes") != len(data)
            or any(
                type(after.get(key)) is not int or before.get(key) != after.get(key)
                for key in ("width", "height", "frames", "fps")
            )
        ):
            raise ScientificAdapterError("video output alignment evidence differs from source")
        total += len(data)
        artifacts.append(
            CollectedArtifactFile(
                name=source["id"], semantic_type=f"{status}-augmented-video/v1", path=path, media_type="video/mp4"
            )
        )
    if counts != result.get("counts") or total > invocation.max_output_bytes:
        raise ScientificAdapterError("video result counts or byte budget is invalid")
    return CollectedStageOutput(
        artifacts=tuple(artifacts),
        validation={
            "status": "passed",
            "validator_id": VALIDATOR_ID,
            "collector_id": COLLECTOR_ID,
            "completion_sha256": completion,
            "clip_counts": counts,
            "quality_scope": "Structural publication validation; accepted clips passed automated checks, "
            "not human or training qualification.",
        },
    )
