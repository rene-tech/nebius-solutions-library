"""The new App cannot widen delegated identities or publish an unverified clip."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from fs2_serve.scientific_batch.adapters import video_augmentation as adapter
from fs2_serve.scientific_batch.adapters.common import ScientificAdapterError
from fs2_serve.scientific_batch.adapters.staged_workspace import STAGE_COMPLETION_SCHEMA, unwrapped_stage_argv
from fs2_serve.scientific_batch.child_routes import PARENT_CONTRACTS
from fs2_serve.scientific_batch.input_contracts import public_input_contract
from fs2_serve.scientific_batch.models import ScientificInputArtifact

ROOT = Path(__file__).resolve().parents[3]
MODEL = ROOT / "models/general-media/video-augmentation"
OPERATION = "00000000-0000-4000-8000-000000000222"


def request():
    return {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "augment-videos",
        "service_class": "customer-batch",
        "input_manifest": {
            "artifact_id": "00000000-0000-4000-8000-000000000111",
            "sha256": "a" * 64,
            "size_bytes": 1000,
            "media_type": "application/vnd.fs2.scientific-manifest+json",
            "compression": "none",
        },
        "parameters": {
            "schema": adapter.PARAMETER_SCHEMA,
            "recipe": {"weather": "overcast"},
            "items": [{"id": "video-0000", "source_name": "folder/clip.mp4", "sha256": "b" * 64}],
        },
    }


def source():
    return ScientificInputArtifact(
        logical_artifact_id="video-0000",
        semantic_type="source-video/v1",
        artifact_id=UUID("00000000-0000-4000-8000-000000000333"),
        digest="sha256:" + "b" * 64,
        size_bytes=1000,
        media_type="video/mp4",
        compression="none",
    )


def profile():
    return json.loads((MODEL / "activation/workload-profile.json").read_text())["profile"]


def compile_one():
    return adapter.compile_run(profile(), request(), operation_id=OPERATION, input_artifacts=(source(),))


def test_candidate_is_unrouted_and_schema_matches_runtime_projection():
    value = profile()
    assert value["route_exposed"] is False and value["interface"]["mcp"]["invocable"] is False
    Draft202012Validator(
        json.loads((ROOT / "catalog/runtime/schema/scientific-workload-profile.schema.json").read_text())
    ).validate(value)
    assert (ROOT / "catalog/runtime/schema/video-augmentation-request.schema.json").read_bytes() == (
        ROOT / "components/control-plane/src/fs2_serve/model_input_schemas/video-augmentation.json"
    ).read_bytes()


def test_compiles_verified_manifest_and_reports_public_contract():
    plan = compile_one()
    invocation = plan.invocations[0]
    assert invocation.stage_id == "augment-videos" and invocation.consumes == ("video-0000",)
    assert invocation.max_output_artifacts == 2
    assert public_input_contract(adapter.MODEL_ID)["maximum_entries"] == 64
    assert (adapter.MODEL_ID, "augment-videos", "main", adapter.COLLECTOR_ID) in PARENT_CONTRACTS
    assert (adapter.MODEL_ID, "augment-dataset", "main", adapter.COLLECTOR_ID) not in PARENT_CONTRACTS


@pytest.mark.parametrize("width,height,frames,fps", [(640, 480, 16, 1), (1280, 720, 400, 30)])
def test_exact_child_payload_matches_real_cosmos_contract(monkeypatch, width, height, frames, fps):
    monkeypatch.syspath_prepend(str(MODEL / "runtime"))
    from fs2_video.bridge import transfer_request
    from fs2_video.contracts import Recipe
    from test_model_input_contracts import cosmos_adapter_request

    from fs2_serve.model_input_contracts import _cosmos

    reference = {
        "artifact_id": str(source().artifact_id),
        "sha256": "b" * 64,
        "size_bytes": 1000,
        "media_type": "video/mp4",
        "compression": "none",
    }
    body = transfer_request(
        {"width": width, "height": height, "frames": frames, "fps": fps},
        reference,
        "Overcast scene, preserving the recorded motion.",
        Recipe(weather="overcast").model_dump(),
    )
    Draft202012Validator(_cosmos()).validate(body)
    # Public/delegated requests carry immutable tenant artifacts. The existing
    # gateway resolves those before calling the GPU adapter's string transport.
    parsed = cosmos_adapter_request().validate_python(
        {**body, "input_reference": "https://media.example.test/materialized-video.mp4"}
    )
    assert parsed.num_frames == parsed.num_video_frames_per_chunk == frames
    assert parsed.controls[0].control_type == "edge"


@pytest.mark.parametrize(
    "changes",
    [
        {"digest": "sha256:" + "c" * 64},
        {"media_type": "application/json"},
        {"logical_artifact_id": "wrong-video"},
        {"semantic_type": "other/v1"},
        {"size_bytes": adapter.MAX_VIDEO_BYTES + 1},
    ],
)
def test_rejects_mismatched_payload(changes):
    with pytest.raises(ScientificAdapterError):
        adapter.compile_run(
            profile(), request(), operation_id=OPERATION, input_artifacts=(replace(source(), **changes),)
        )


def test_batch_requires_approval_and_no_extra_manifest_entries():
    value = request()
    value["parameters"]["items"].append({"id": "video-0001", "source_name": "second.mp4", "sha256": "b" * 64})
    with pytest.raises(ScientificAdapterError, match="approved"):
        adapter.parameters(value["parameters"])
    with pytest.raises(ScientificAdapterError, match="exactly once"):
        adapter.validate_entries(request(), (source(), source()))


def collector_fixture(tmp_path):
    invocation = compile_one().invocations[0]
    (tmp_path / ".fs2").mkdir()
    (tmp_path / ".fs2/request.json").write_text(json.dumps(request()["parameters"]))
    command = unwrapped_stage_argv(invocation, label="test")
    marker = {
        "schema": STAGE_COMPLETION_SCHEMA,
        "status": "passed",
        "stage_id": invocation.stage_id,
        "shard_id": invocation.shard_id,
        "logical_output_id": invocation.produces,
        "collector_id": invocation.collector_id,
        "validator_id": invocation.validator_id,
        "argv_sha256": hashlib.sha256(json.dumps(command, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }
    (tmp_path / ".fs2/stage-complete.json").write_text(json.dumps(marker))
    recipe = {"parameters": {"weather": "overcast"}, "paidf_revision": adapter.SOURCE_REVISION}
    recipe["sha256"] = hashlib.sha256(
        (json.dumps(recipe, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    result = {
        "schema": adapter.RESULT_SCHEMA,
        "operation_id": OPERATION,
        "recipe": recipe,
        "items": [
            {
                "id": "video-0000",
                "source_name": "folder/clip.mp4",
                "source_sha256": "b" * 64,
                "recipe_sha256": recipe["sha256"],
                "status": "failed",
                "error_code": "VIDEO_AUGMENTATION_FAILED",
            }
        ],
        "counts": {"accepted": 0, "rejected": 0, "failed": 1},
    }
    (tmp_path / "result.json").write_text(json.dumps(result))
    return invocation, result


def test_failed_clips_publish_honest_report_not_successful_video(tmp_path):
    invocation, _ = collector_fixture(tmp_path)
    collected = adapter.collect_companion_output(invocation, tmp_path)
    assert len(collected.artifacts) == 1
    assert collected.validation["clip_counts"]["failed"] == 1


def test_collector_rejects_invented_pass_or_source(tmp_path):
    invocation, result = collector_fixture(tmp_path)
    result["items"][0]["status"] = "accepted"
    result["counts"] = {"accepted": 1, "rejected": 0, "failed": 0}
    (tmp_path / "result.json").write_text(json.dumps(result))
    with pytest.raises(ScientificAdapterError, match="quality checks"):
        adapter.collect_companion_output(invocation, tmp_path)
    result["items"][0]["source_sha256"] = "c" * 64
    (tmp_path / "result.json").write_text(json.dumps(result))
    with pytest.raises(ScientificAdapterError, match="source identity"):
        adapter.collect_companion_output(invocation, tmp_path)


@pytest.mark.parametrize("missing", [None, "width", "height", "frames", "fps", "sha256", "size_bytes"])
def test_collector_requires_complete_matching_alignment_receipt(tmp_path, missing):
    """Header-only bytes are a collector fixture, never a generated-video claim."""
    invocation, result = collector_fixture(tmp_path)
    data = b"\x00\x00\x00\x18ftypmp42" + b"fixture" * 10
    path = tmp_path / "outputs/video-0000/video.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    pointer = {
        "path": "outputs/video-0000/video.mp4",
        "media_type": "video/mp4",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    geometry = {"width": 640, "height": 480, "fps": 16, "frames": 32}
    item = result["items"][0]
    item.update(
        status="accepted",
        artifact=pointer,
        checks={"hallucination_check": {"passed": True}, "attribute_verification": {"passed": True}},
        input={**geometry, "sha256": item["source_sha256"]},
        output={**geometry, "sha256": pointer["sha256"], "size_bytes": len(data)},
    )
    if missing:
        item["output"].pop(missing)
    result["counts"] = {"accepted": 1, "rejected": 0, "failed": 0}
    (tmp_path / "result.json").write_text(json.dumps(result))
    if missing:
        with pytest.raises(ScientificAdapterError, match="alignment"):
            adapter.collect_companion_output(invocation, tmp_path)
    else:
        collected = adapter.collect_companion_output(invocation, tmp_path)
        assert len(collected.artifacts) == 2
