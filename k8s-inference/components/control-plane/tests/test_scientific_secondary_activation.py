"""Activation inputs for the four non-AF3 secondary structure models.

The model-owned fragments under ``models/structure/batch-adapters/<id>/activation``
must compile real requests through the controller's dispatcher exactly as the
shared aggregate would, without any edit to that aggregate.  These tests bind
the fragments to the adapter modules, the model-owned contracts, the published
r4 image handoff, the parameter schemas and the artifact catalog.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from fs2_serve.scientific_batch import CheckpointMode, PreemptionMode, ResourceClass, ScientificAdapterError
from fs2_serve.scientific_batch.adapters import common as adapter_common
from fs2_serve.scientific_batch.adapters import compile_adapter_run, esmfold2, esmfold2_fast, openfold3, protenix_v2
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog, ScientificWorkloadProfile

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters"
CATALOG_ROOT = SOLUTION_ROOT / "catalog/runtime"
SCHEMA_ROOT = CATALOG_ROOT / "schema"
IMAGE_HANDOFF = ADAPTER_ROOT / "secondary-r4-image-handoff.json"
RENDERER = ADAPTER_ROOT / "render_activation_fragments.py"
MODULES: dict[str, ModuleType] = {
    "esmfold2": esmfold2,
    "esmfold2-fast": esmfold2_fast,
    "protenix-v2": protenix_v2,
    "openfold3": openfold3,
}
POSITIVE_FIXTURES = {
    "esmfold2": ("positive-sequence", "positive-msa"),
    "esmfold2-fast": ("positive-short", "positive-recycles"),
    "protenix-v2": ("positive-complex", "positive-monomer"),
    "openfold3": ("positive-complex", "positive-monomer"),
}
NEGATIVE_FIXTURES = {
    "esmfold2": "negative-invalid-sequence",
    "esmfold2-fast": "negative-msa",
    "protenix-v2": "negative-duplicate-seeds",
    "openfold3": "negative-alphafold-alias",
}
PARAMETER_SCHEMAS = {
    "esmfold2": "esmfold2-parameters.schema.json",
    "esmfold2-fast": "esmfold2-fast-parameters.schema.json",
    "protenix-v2": "protenix-v2-parameters.schema.json",
    "openfold3": "openfold3-parameters.schema.json",
}


def load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def fragment(model_id: str, name: str) -> dict[str, object]:
    return load(ADAPTER_ROOT / model_id / "activation" / name)


def profile(model_id: str) -> dict[str, object]:
    value = fragment(model_id, "workload-profile.json")["profile"]
    assert isinstance(value, dict)
    return value


def fixture(model_id: str, name: str) -> dict[str, object]:
    return load(ADAPTER_ROOT / model_id / "fixtures" / f"{name}.json")


def contract(model_id: str) -> dict[str, object]:
    return load(ADAPTER_ROOT / model_id / "contract.json")


def renderer_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("render_activation_fragments", RENDERER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def profile_validator() -> Draft202012Validator:
    return Draft202012Validator(
        load(SCHEMA_ROOT / "scientific-workload-profile.schema.json"),
        format_checker=FormatChecker(),
    )


@pytest.mark.parametrize("model_id", sorted(MODULES))
def test_fragment_profiles_are_schema_valid_unrouted_candidates(
    model_id: str, profile_validator: Draft202012Validator
) -> None:
    wrapper = fragment(model_id, "workload-profile.json")
    assert wrapper["schema"] == "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1"
    assert wrapper["merge_target"] == "catalog/runtime/contracts/scientific-workload-profiles.json"
    candidate = profile(model_id)
    assert list(profile_validator.iter_errors(candidate)) == []
    assert candidate["state"] == "candidate-unqualified"
    assert candidate["route_exposed"] is False
    assert candidate["interface"]["mcp"] == {  # type: ignore[index]
        "discoverable": True,
        "invocable": False,
        "tool_name": candidate["interface"]["mcp"]["tool_name"],  # type: ignore[index]
        "description": candidate["interface"]["mcp"]["description"],  # type: ignore[index]
    }
    # A candidate never reaches the runnable catalog and never carries qualification receipts.
    assert ScientificWorkloadProfile(candidate).runnable is False  # type: ignore[arg-type]
    assert "qualification" not in candidate
    assert "runtime_artifacts" not in candidate, "no localization exists yet, so no identity may be claimed"


@pytest.mark.parametrize("model_id", sorted(MODULES))
def test_fragment_identities_join_the_adapter_contract_and_the_published_r4_handoff(model_id: str) -> None:
    module = MODULES[model_id]
    candidate = profile(model_id)
    value = contract(model_id)
    images = {item["model_id"]: item for item in load(IMAGE_HANDOFF)["images"]}  # type: ignore[union-attr]
    assert candidate["model_id"] == module.MODEL_ID
    assert candidate["source"]["repository"] == module.SOURCE_REPOSITORY  # type: ignore[index]
    assert candidate["source"]["revision"] == module.SOURCE_REVISION  # type: ignore[index]
    identity = candidate["execution_identity"]
    assert identity["model_revision"] == module.SOURCE_REVISION  # type: ignore[index]
    assert identity["runtime_image_digest"] == images[model_id]["digest"] == value["runtime_image"]["digest"]  # type: ignore[index]
    assert identity["artifact_manifest_digest"] is None  # type: ignore[index]
    assert identity["execution_identity_sha256"] is None  # type: ignore[index]
    interface = candidate["interface"]
    assert interface["parameter_schema"] == module.PARAMETER_SCHEMA == value["interface"]["parameter_schema"]  # type: ignore[index]
    assert interface["operations"] == value["interface"]["operations"]  # type: ignore[index]
    assert candidate["semantic_validation"]["validator_id"] == module.VALIDATOR_ID  # type: ignore[index]
    stages = {stage["id"]: stage for stage in candidate["workload"]["stages"]}  # type: ignore[index]
    assert list(stages) == list(module.STAGE_EXECUTION_CONTRACTS)
    assert [stage["resource_class"] for stage in stages.values()] == ["cpu", "gpu"]
    contract_stages = {stage["stage_id"]: stage for stage in value["stages"]}  # type: ignore[union-attr]
    for stage_id, stage in stages.items():
        assert stage["max_parallelism"] == contract_stages[stage_id]["max_parallelism"]
        assert "placement" not in stage, "secondary CPU stages take the general-cpu default until a class is assigned"
    workload_digest = hashlib.sha256(
        json.dumps(candidate["workload"], separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert identity["workload_recipe_sha256"] == workload_digest  # type: ignore[index]
    recipe = fragment(model_id, "integration-recipe.json")
    assert identity["runtime_recipe_sha256"] == recipe["runtime_recipe_sha256_at_render"]  # type: ignore[index]
    assert set(recipe["recipe_paths"]) & set(adapter_common._RECIPE_SHARED_PATHS) == set()  # type: ignore[arg-type]  # noqa: SLF001
    for relative in recipe["recipe_paths"]:  # type: ignore[union-attr]
        assert (SOLUTION_ROOT / relative).is_file(), relative


@pytest.mark.parametrize("model_id", sorted(MODULES))
def test_positive_fixtures_compile_through_the_controller_dispatcher(model_id: str) -> None:
    module = MODULES[model_id]
    candidate = profile(model_id)
    for name in POSITIVE_FIXTURES[model_id]:
        plan = compile_adapter_run(model_id, candidate, fixture(model_id, name), operation_id=f"op-{model_id}-{name}")
        assert plan.model_id == module.MODEL_ID
        assert plan.variant_id == module.VARIANT_ID
        assert plan.source_revision == module.SOURCE_REVISION
        assert [stage.stage_id for stage in plan.controller_plan.stages] == list(module.STAGE_EXECUTION_CONTRACTS)
        assert [stage.resource_class for stage in plan.controller_plan.stages] == [ResourceClass.CPU, ResourceClass.GPU]
        assert all(stage.placement_class is None and stage.resources is None for stage in plan.controller_plan.stages)
        gpu_stage = plan.controller_plan.stages[1]
        assert gpu_stage.checkpoint_mode is CheckpointMode.RESTART
        assert gpu_stage.preemption_mode is PreemptionMode.RESTARTABLE
        assert len(plan.invocations) == 2
        for invocation in plan.invocations:
            assert invocation.argv[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"}
            expected = module.STAGE_EXECUTION_CONTRACTS[invocation.stage_id]
            assert tuple(invocation.runtime_artifacts) == tuple(expected["runtime_artifacts"])
        assert set(plan.required_model_artifacts) == {
            artifact for stage in module.STAGE_EXECUTION_CONTRACTS.values() for artifact in stage["runtime_artifacts"]
        }
    with pytest.raises(ScientificAdapterError):
        compile_adapter_run(model_id, candidate, fixture(model_id, NEGATIVE_FIXTURES[model_id]), operation_id="op-neg")


@pytest.mark.parametrize("model_id", sorted(MODULES))
def test_parameter_schemas_mirror_the_adapter_parsers_and_are_registered_by_id(model_id: str) -> None:
    module = MODULES[model_id]
    schema = load(SCHEMA_ROOT / PARAMETER_SCHEMAS[model_id])
    validator = Draft202012Validator(schema)
    for name in POSITIVE_FIXTURES[model_id]:
        assert list(validator.iter_errors(fixture(model_id, name)["parameters"])) == []
    assert list(validator.iter_errors(fixture(model_id, NEGATIVE_FIXTURES[model_id])["parameters"]))
    # The catalog registers parameter schemas by their $id slug; a profile whose
    # parameter_schema has no registered validator fails at submit time.
    catalog = ScientificProfileCatalog.load(CATALOG_ROOT)
    assert module.PARAMETER_SCHEMA in catalog._validators  # type: ignore[attr-defined]  # noqa: SLF001
    assert schema["$id"].endswith("/" + module.PARAMETER_SCHEMA.removeprefix("fs2-serve.nebius.ai/"))


@pytest.mark.parametrize("model_id", sorted(MODULES))
def test_execution_map_fragments_bind_stage_identities_and_leave_localization_honest(model_id: str) -> None:
    module = MODULES[model_id]
    wrapper = fragment(model_id, "execution-map-fragment.json")
    assert wrapper["execution_map_schema"] == "fs2-serve.nebius.ai/scientific-execution-map/v3"
    assert wrapper["state"] == "stages-bound-runtime-artifacts-pending-localization"
    model = wrapper["model"]
    assert model["model_id"] == module.MODEL_ID  # type: ignore[index]
    assert model["variant_id"] == module.VARIANT_ID  # type: ignore[index]
    assert model["workload_namespace"] == "fs2-models"  # type: ignore[index]
    assert model["access_profile"] == "public"  # type: ignore[index]
    assert model["plan_adapter"] == {  # type: ignore[index]
        "module": "fs2_serve.scientific_batch.adapters",
        "function": "compile_adapter_run",
    }
    image_digest = profile(model_id)["execution_identity"]["runtime_image_digest"]  # type: ignore[index]
    stages = {stage["stage_id"]: stage for stage in model["stages"]}  # type: ignore[index]
    assert list(stages) == list(module.STAGE_EXECUTION_CONTRACTS)
    for stage_id, stage in stages.items():
        expected = module.STAGE_EXECUTION_CONTRACTS[stage_id]
        assert stage["image"].endswith(f"@{image_digest}")
        assert stage["collector_id"] == expected["collector_id"]
        assert stage["validator_id"] == expected["validator_id"]
        assert sum(mount["kind"] == "artifact-workspace" for mount in stage["mounts"]) == 1
        if expected["runtime_artifacts"]:
            assert any(
                mount["kind"] == "reference" and mount["mount_path"] == "/reference-data" for mount in stage["mounts"]
            )
            assert stage["required_node_labels"]["storage.fs2.nebius/reference-data"] == "true"
    contract_mounts = {item["artifact_id"]: item["mount_path"] for item in contract(model_id)["runtime_artifacts"]}  # type: ignore[union-attr]
    localizations = {item["artifact_id"]: item for item in model["runtime_artifacts"]}  # type: ignore[index]
    assert set(localizations) == set(contract_mounts)
    for artifact_id, localization in localizations.items():
        manifest = load(SOLUTION_ROOT / "model-artifacts" / f"manifest-{artifact_id}.json")
        assert localization["mount_path"] == contract_mounts[artifact_id]
        assert localization["content_digest"] == f"sha256:{manifest['content']['digest']}"  # type: ignore[index]
        assert localization["localization_receipt_digest"] is None, "no receipt exists, so none may be claimed"
        assert len(localization["file_manifest"]) == len(manifest["content"]["files"])  # type: ignore[index]
    gates = wrapper["activation_gates"]
    assert isinstance(gates, list) and gates
    assert any(gate.startswith("semantic-h100:") for gate in gates)
    assert any(gate.startswith("artifact-localization:") for gate in gates)


def test_contract_and_catalog_agree_except_for_the_recorded_protenix_identity_gap() -> None:
    disagreements: dict[str, tuple[str, str]] = {}
    for model_id in MODULES:
        for item in contract(model_id)["runtime_artifacts"]:  # type: ignore[union-attr]
            manifest = load(SOLUTION_ROOT / "model-artifacts" / f"manifest-{item['artifact_id']}.json")
            catalog_digest = manifest["content"]["digest"]  # type: ignore[index]
            if item["content_sha256"] != catalog_digest:
                disagreements[item["artifact_id"]] = (item["content_sha256"], catalog_digest)
    # The Protenix composite is the one known identity gap; it is named as an
    # activation limitation rather than silently rebound to either side.
    assert set(disagreements) == {"protenix-v2"}
    limitations = profile("protenix-v2")["policy"]["limitations"]  # type: ignore[index]
    assert any("5e1c3b54" in item and "8e14bb80" in item for item in limitations)


def test_openfold3_stays_an_explicit_non_equivalent_alternative() -> None:
    candidate = profile("openfold3")
    assert contract("openfold3")["relationship"] == "independent-non-equivalent-alternative-to-alphafold3"
    assert any("non-equivalent" in item for item in candidate["policy"]["limitations"])  # type: ignore[index]
    assert any("collides" in item for item in candidate["policy"]["limitations"])  # type: ignore[index]
    with pytest.raises(ScientificAdapterError):
        compile_adapter_run("openfold3", candidate, fixture("openfold3", "negative-alphafold-alias"), operation_id="op")


def test_committed_fragments_match_the_renderer() -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository-owned script
        [sys.executable, str(RENDERER), "--check"],
        check=False,
        capture_output=True,
        text=True,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    module = renderer_module()
    for model_id in MODULES:
        rendered = module.render_profile(module.SPECS[model_id])
        assert rendered == fragment(model_id, "workload-profile.json")
