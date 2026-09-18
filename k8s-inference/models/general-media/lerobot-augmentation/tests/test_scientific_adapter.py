from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOLUTION_ROOT = ROOT.parents[2]
sys.path.insert(0, str(SOLUTION_ROOT / "components" / "control-plane" / "src"))

from fs2_serve.scientific_batch.adapters import cosmos_lerobot  # noqa: E402
from fs2_serve.scientific_batch.adapters.common import ScientificAdapterError  # noqa: E402
from fs2_serve.scientific_batch.adapters.staged_workspace import (  # noqa: E402
    STAGE_COMPLETION_SCHEMA,
    unwrapped_stage_argv,
)
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer  # noqa: E402
from fs2_serve.scientific_batch.models import ScientificInputArtifact  # noqa: E402
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog  # noqa: E402
from fs2_serve.scientific_batch.adapters.primitives import ScientificParameterError  # noqa: E402


def _profile() -> dict[str, Any]:
    return json.loads((ROOT / "activation" / "workload-profile.json").read_text())["profile"]


def _request() -> dict[str, Any]:
    return json.loads((ROOT / "fixtures" / "scientific-run-request.json").read_text())


def _source_reference() -> ScientificInputArtifact:
    return ScientificInputArtifact(
        logical_artifact_id="lerobot-source",
        semantic_type="lerobot-source-reference/v1",
        artifact_id=UUID("00000000-0000-4000-8000-000000000101"),
        digest="sha256:c4a609ad88a2671d703269547fc078028e5266b7d7e064c3a2539a781afa0bbf",
        size_bytes=215,
        media_type="application/json",
        compression="none",
    )


def test_published_active_execution_binding_matches_canonical_catalog(tmp_path: Path) -> None:
    catalog_path = SOLUTION_ROOT / "catalog/runtime"
    profile = _profile()
    assert profile["route_exposed"]
    assert profile["state"] in {"active", "qualified"}
    assert profile["semantic_validation"]["state"] == profile["state"]
    candidate = json.loads((ROOT / "activation/execution-map.json").read_text())["model"]
    execution_path = tmp_path / "execution-map.json"
    execution = json.loads((catalog_path / "contracts/scientific-execution-map.json").read_text())
    assert [row for row in execution["models"] if row["model_id"] == cosmos_lerobot.MODEL_ID] == [candidate]
    execution_path.write_text(json.dumps(execution))
    catalog = ScientificProfileCatalog.load(catalog_path)
    renderer = FileScientificManifestRenderer(path=execution_path, profiles=catalog)
    assert dict(catalog.get(cosmos_lerobot.MODEL_ID).value) == profile
    assert renderer.qualification_matches(
        cosmos_lerobot.MODEL_ID, "sha256:" + profile["qualification"]["execution_map_sha256"]
    )
    assert candidate["stages"][0]["image"].split("@", 1)[1] == profile["execution_identity"]["runtime_image_digest"]
    assert (ROOT / "schema/request.schema.json").read_bytes() == (
        catalog_path / "schema/cosmos3-lerobot-augmentation-request.schema.json"
    ).read_bytes()
    assert cosmos_lerobot.MAX_BUNDLE_BYTES == cosmos_lerobot.MAX_OUTPUT_BYTES == 5 * 1024**3


def test_candidate_adapter_compiles_exact_source_reference_manifest() -> None:
    plan = cosmos_lerobot.compile_run(
        _profile(),
        _request(),
        operation_id="00000000-0000-4000-8000-000000000201",
        input_artifacts=(_source_reference(),),
    )
    invocation = plan.invocations[0]
    assert invocation.consumes == ("lerobot-source",)
    assert invocation.stage_id == "augment-dataset"
    assert invocation.collector_id == cosmos_lerobot.COLLECTOR_ID
    assert "--source-artifact" in invocation.argv
    assert not invocation.environment


def test_public_input_contract_uses_exact_compiler_roles_and_is_not_mutable() -> None:
    contract = cosmos_lerobot.public_input_contract()
    assert contract["exactly_one_entry"] is True
    bundle = contract["source_kinds"]["uploaded-bundle"]
    assert bundle == {
        "name": cosmos_lerobot.INPUT_BUNDLE_ID,
        "semantic_type": cosmos_lerobot.INPUT_BUNDLE_SEMANTIC_TYPE,
        "media_type": "application/x-tar", "compression": "zstd",
        "maximum_bytes": cosmos_lerobot.MAX_BUNDLE_BYTES,
    }
    contract["source_kinds"]["huggingface"]["name"] = "bad"
    assert cosmos_lerobot.public_input_contract()["source_kinds"]["huggingface"]["name"] == "lerobot-source"


@pytest.mark.parametrize("field,value", [
    ("logical_artifact_id", "source-reference.json"),
    ("semantic_type", "lerobot-bundle/v1"),
    ("media_type", "application/x-tar"),
    ("compression", "zstd"),
    ("size_bytes", cosmos_lerobot.MAX_SOURCE_REFERENCE_BYTES + 1),
])
def test_wrong_owned_manifest_role_has_actionable_caller_error(field, value) -> None:
    with pytest.raises(ScientificParameterError) as caught:
        cosmos_lerobot.compile_run(
            _profile(), _request(), operation_id="00000000-0000-4000-8000-000000000201",
            input_artifacts=(replace(_source_reference(), **{field: value}),),
        )
    assert "name=lerobot-source" in caught.value.public_detail
    assert "semantic_type=lerobot-source-reference/v1" in caught.value.public_detail
    assert "No run was admitted" in caught.value.public_detail


def test_uploaded_source_must_match_verified_manifest_identity() -> None:
    request = _request()
    request["parameters"]["source"] = {
        "kind": "uploaded-bundle",
        "artifact_id": "00000000-0000-4000-8000-000000000301",
        "sha256": "a" * 64,
        "size_bytes": 1024,
        "media_type": "application/x-tar",
        "compression": "zstd",
    }
    bundle = ScientificInputArtifact(
        logical_artifact_id="lerobot-dataset",
        semantic_type="lerobot-v3-bundle/v1",
        artifact_id=UUID("00000000-0000-4000-8000-000000000301"),
        digest="sha256:" + "b" * 64,
        size_bytes=1024,
        media_type="application/x-tar",
        compression="zstd",
    )
    with pytest.raises(ScientificAdapterError, match="differs"):
        cosmos_lerobot.compile_run(
            _profile(),
            request,
            operation_id="00000000-0000-4000-8000-000000000302",
            input_artifacts=(bundle,),
        )


def test_collector_binds_reload_receipt_and_bundle_bytes(tmp_path: Path) -> None:
    operation_id = "00000000-0000-4000-8000-000000000401"
    plan = cosmos_lerobot.compile_run(
        _profile(),
        _request(),
        operation_id=operation_id,
        input_artifacts=(_source_reference(),),
    )
    invocation = plan.invocations[0]
    workspace = tmp_path / "work"
    (workspace / ".fs2").mkdir(parents=True)
    (workspace / "artifacts").mkdir()
    bundle = b"valid-worker-owned-zstd-placeholder"
    bundle_path = workspace / "artifacts" / "variant-00.tar.zst"
    bundle_path.write_bytes(bundle)
    bundle_sha = hashlib.sha256(bundle).hexdigest()
    variant = {
        "variant_index": 0,
        "seed": 20260915,
        "artifact": {
            "artifact_id": f"{operation_id}.variant-00",
            "sha256": bundle_sha,
            "size_bytes": len(bundle),
            "media_type": "application/x-tar",
            "compression": "zstd",
        },
        "validation": {
            "reader": "lerobot==0.6.1",
            "episodes": 1,
            "frames": 16,
            "decoded_video_frames": 16,
            "status": "passed",
        },
        "provenance_sha256": "a" * 64,
    }
    result = {
        "schema": cosmos_lerobot.RESULT_SCHEMA,
        "operation_id": operation_id,
        "status": "succeeded",
        "progress": {"completed_units": 1, "total_units": 1},
        "variants": [variant],
        "failures": [],
    }
    (workspace / "result.json").write_text(json.dumps(result, separators=(",", ":")))
    (workspace / "artifact-index.json").write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/cosmos3-lerobot-artifact-index/v1",
                "artifacts": [{**variant["artifact"], "path": "artifacts/variant-00.tar.zst"}],
            },
            separators=(",", ":"),
        )
    )
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
    (workspace / ".fs2" / "stage-complete.json").write_text(json.dumps(marker, separators=(",", ":")))
    collected = cosmos_lerobot.collect_companion_output(invocation, workspace)
    assert [item.name for item in collected.artifacts] == ["result", "variant-00"]
    assert collected.validation["decoded_video_frames"] == 16
