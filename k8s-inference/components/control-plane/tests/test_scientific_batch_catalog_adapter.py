from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from jsonschema import ValidationError

from fs2_serve.scientific_batch import (
    CatalogProfileAdapterError,
    ExecutionMode,
    PreemptionMode,
    ResourceClass,
    ScientificStageExpansion,
    scientific_plan_from_catalog_profile,
    validate_scientific_run_request,
)

INFERENCE_ROOT = Path(__file__).resolve().parents[3]
ONBOARDING_COMPILER = INFERENCE_ROOT / "model-onboarding/compile_model.py"
BATCH_DECLARATION = INFERENCE_ROOT / "model-onboarding/examples/scientific-batch-git.json"
PROFILE_SET = INFERENCE_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
REQUEST_SCHEMA = INFERENCE_ROOT / "catalog/runtime/schema/scientific-run-request.schema.json"


def compiled_catalog_profile() -> dict[str, object]:
    """Compile and schema-validate the repository's canonical batch example."""

    spec = importlib.util.spec_from_file_location("scientific_batch_adapter_onboarding", ONBOARDING_COMPILER)
    assert spec is not None and spec.loader is not None
    compiler = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = compiler
    spec.loader.exec_module(compiler)
    declaration = compiler.load_declaration(BATCH_DECLARATION)
    artifacts = compiler.compile_artifacts(declaration, INFERENCE_ROOT)
    artifact = next(item for item in artifacts if item.path == "projections/scientific-workload-profile.json")
    projection = json.loads(artifact.payload)
    profile = projection["profile"]
    assert isinstance(profile, dict)
    return profile


def candidate_profile(model_id: str) -> dict[str, object]:
    profiles = json.loads(PROFILE_SET.read_text(encoding="utf-8"))["profiles"]
    return next(profile for profile in profiles if profile["model_id"] == model_id)


def test_validated_catalog_profile_projects_to_distinct_internal_plan() -> None:
    internal = scientific_plan_from_catalog_profile(
        compiled_catalog_profile(),
        expansions={
            "generate": ScientificStageExpansion(shard_ids=("input-0001", "input-0002")),
            "score": ScientificStageExpansion(shard_ids=("candidate-0001", "candidate-0002")),
        },
    )

    generate, score = internal.stages
    assert generate.stage_id == "generate"
    assert generate.mode is ExecutionMode.FANOUT
    assert generate.resource_class is ResourceClass.GPU
    assert generate.shards == ("input-0001", "input-0002")
    assert generate.max_attempts == 3
    assert score.depends_on == ("generate",)
    assert score.mode is ExecutionMode.FANOUT
    assert score.preemption_mode is PreemptionMode.RESTARTABLE


def test_catalog_projection_preserves_stage_resources_and_storage() -> None:
    internal = scientific_plan_from_catalog_profile(
        candidate_profile("alphafold3"),
        expansions={
            "data-pipeline": ScientificStageExpansion(),
            "inference": ScientificStageExpansion(),
        },
    )

    data, inference = internal.stages
    assert data.resources is not None and data.resources.gpu_count == 0
    assert inference.resources is not None and inference.resources.gpu_count == 1
    reference = next(item for item in internal.storage if item.purpose == "reference-data")
    assert reference.minimum_bytes >= 1024**4
    assert reference.access_mode.value == "ReadWriteMany"
    assert reference.read_only is True
    assert reference.stages == ("data-pipeline",)


def test_request_validation_is_schema_driven_and_fail_closed() -> None:
    profile = candidate_profile("esmfold2-fast")
    request_schema = json.loads(REQUEST_SCHEMA.read_text(encoding="utf-8"))
    request: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "predict-protein-structure",
        "service_class": "interactive",
        "input_manifest": {
            "artifact_id": "protein.input.01",
            "sha256": "a" * 64,
            "size_bytes": 20,
            "media_type": "application/json",
        },
        "parameters": {"mode": "single-sequence", "sequence": "ACDEFGHIK"},
    }
    validate_scientific_run_request(profile, request, request_schema)

    unknown_envelope = {**request, "runtime_image": "caller-controlled"}
    with pytest.raises(ValidationError):
        validate_scientific_run_request(profile, unknown_envelope, request_schema)

    unknown_parameter = json.loads(json.dumps(request))
    unknown_parameter["parameters"]["msa"] = "caller-controlled"
    with pytest.raises(ValidationError):
        validate_scientific_run_request(profile, unknown_parameter, request_schema)

    invalid_mode = json.loads(json.dumps(request))
    invalid_mode["parameters"]["mode"] = "precomputed-msa"
    with pytest.raises(ValidationError):
        validate_scientific_run_request(profile, invalid_mode, request_schema)


def test_catalog_projection_rejects_unbounded_or_unknown_run_expansion() -> None:
    with pytest.raises(CatalogProfileAdapterError, match="outside the catalog profile bounds"):
        scientific_plan_from_catalog_profile(
            compiled_catalog_profile(),
            expansions={
                "generate": ScientificStageExpansion(shard_ids=tuple(f"input-{index}" for index in range(65))),
                "score": ScientificStageExpansion(),
            },
        )

    with pytest.raises(CatalogProfileAdapterError, match="unknown catalog stages"):
        scientific_plan_from_catalog_profile(
            compiled_catalog_profile(),
            expansions={
                "generate": ScientificStageExpansion(),
                "score": ScientificStageExpansion(),
                "caller-image-or-argv": ScientificStageExpansion(),
            },
        )
