from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from fs2_serve.scientific_batch import (
    CatalogProfileAdapterError,
    ExecutionMode,
    PreemptionMode,
    ResourceClass,
    ScientificStageExpansion,
    scientific_plan_from_catalog_profile,
)

INFERENCE_ROOT = Path(__file__).resolve().parents[3]
ONBOARDING_COMPILER = INFERENCE_ROOT / "model-onboarding/compile_model.py"
BATCH_DECLARATION = INFERENCE_ROOT / "model-onboarding/examples/scientific-batch-git.json"


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
