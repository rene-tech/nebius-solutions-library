"""Controller-owned execution and semantic tests for secondary adapters."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE_SRC = ROOT / "components/control-plane/src"
sys.path.insert(0, str(CONTROL_PLANE_SRC))

from fs2_serve.scientific_batch import ResourceClass, compile_adapter_run, profile_from_catalog  # noqa: E402
from fs2_serve.scientific_batch.adapters import (  # noqa: E402
    alphafold3,
    esmfold2,
    esmfold2_fast,
    openfold3,
    protenix_v2,
)
from fs2_serve.scientific_batch.adapters.common import ScientificAdapterError  # noqa: E402

FIXTURE_ROOT = ROOT / "models/structure/runtime"
PROFILES = json.loads((ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json").read_text())
REQUEST_SCHEMA = json.loads((ROOT / "catalog/runtime/schema/scientific-run-request.schema.json").read_text())
DIRECTORIES = {
    "esmfold2": "esmfold2",
    "esmfold2-fast": "esmfold2_fast",
    "protenix-v2": "protenix_v2",
    "alphafold3": "alphafold3",
    "openfold3": "openfold3",
}
MODULES = {
    module.MODEL_ID: module
    for module in (esmfold2, esmfold2_fast, protenix_v2, alphafold3, openfold3)
}


def profile(model_id: str):
    return profile_from_catalog(PROFILES, model_id)


def fixture(model_id: str, filename: str):
    return json.loads((FIXTURE_ROOT / DIRECTORIES[model_id] / "fixtures" / filename).read_text())


def positive_fixtures(model_id: str):
    root = FIXTURE_ROOT / DIRECTORIES[model_id] / "fixtures"
    return [json.loads(path.read_text()) for path in sorted(root.glob("positive-*.json"))]


@pytest.mark.parametrize("model_id", MODULES)
def test_two_positive_fixtures_use_generated_canonical_contract(model_id: str) -> None:
    candidate = profile(model_id)
    assert candidate["variant_id"] == MODULES[model_id].VARIANT_ID
    assert candidate["interface"]["request_schema"] == "fs2-serve.nebius.ai/scientific-run-request/v1"
    assert candidate["resources"]["compatible_pool_ids"] == ["h100-reserved-8x", "h100-1x"]
    requests = positive_fixtures(model_id)
    assert len(requests) == 2
    parameter_validator = Draft202012Validator(candidate["interface"]["parameter_schema_definition"])
    for request in requests:
        Draft202012Validator(REQUEST_SCHEMA).validate(request)
        parameter_validator.validate(request["parameters"])


def test_open_adapters_compile_exact_shell_free_cpu_then_gpu_invocations() -> None:
    cases = (
        ("esmfold2", "positive-sequence.json", ("prepare-input", "fold")),
        ("esmfold2-fast", "positive-short.json", ("prepare-input", "fold")),
        ("openfold3", "positive-monomer.json", ("data-pipeline", "inference")),
    )
    for model_id, filename, stage_ids in cases:
        plan = compile_adapter_run(
            model_id,
            profile(model_id),
            fixture(model_id, filename),
            operation_id=f"op-{model_id}-01",
            variant_id=MODULES[model_id].VARIANT_ID,
        )
        assert plan.model_id == model_id
        assert plan.variant_id == MODULES[model_id].VARIANT_ID
        assert tuple(stage.stage_id for stage in plan.controller_plan.stages) == stage_ids
        assert tuple(stage.resource_class for stage in plan.controller_plan.stages) == (
            ResourceClass.CPU,
            ResourceClass.GPU,
        )
        assert all(item.argv[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"} for item in plan.invocations)
        assert all(item.working_directory.startswith("/mnt/fs2-scientific/work/") for item in plan.invocations)
        assert all(item.materializations for item in plan.invocations)
        assert plan.invocations[1].consumes == (plan.invocations[0].produces,)
        assert all("s3://" not in value and "file://" not in value for item in plan.invocations for value in item.argv)


def test_esm_exact_artifacts_and_fast_capability_difference() -> None:
    full = esmfold2.compile_run(
        profile("esmfold2"), fixture("esmfold2", "positive-msa.json"), operation_id="op-esm-full-01"
    )
    assert full.required_model_artifacts == ("esmfold2", "esmc-6b")
    assert "--ccd-path" in full.invocations[1].argv
    fast = esmfold2_fast.compile_run(
        profile("esmfold2-fast"), fixture("esmfold2-fast", "positive-short.json"), operation_id="op-esm-fast-01"
    )
    assert fast.required_model_artifacts == ("esmfold2-fast", "esmc-6b", "esmfold2-ccd")
    assert "--single-sequence" in fast.invocations[1].argv
    with pytest.raises(ScientificAdapterError, match="rejects MSA"):
        esmfold2_fast.compile_run(
            profile("esmfold2-fast"),
            fixture("esmfold2-fast", "negative-msa.json"),
            operation_id="op-esm-fast-bad",
        )


def test_protenix_v2_exact_plan_binds_verified_v2_checkpoint() -> None:
    request = fixture("protenix-v2", "positive-monomer.json")
    plan = protenix_v2._candidate_plan(profile("protenix-v2"), request, operation_id="op-protenix-01")
    assert plan.required_model_artifacts == (
        "protenix-v2",
        "protenix-v2-inference-data-2026-01-29",
    )
    assert plan.invocations[0].argv[:2] == ("protenix", "prep")
    assert plan.invocations[1].argv[:2] == ("protenix", "pred")
    checkpoint_mount = next(
        mount for mount in plan.invocations[1].runtime_mounts if mount.artifact_id == "protenix-v2"
    )
    assert checkpoint_mount.sub_path == "checkpoint"
    assert checkpoint_mount.expected_content_sha256 == protenix_v2.WEIGHTS_SHA256
    assert compile_adapter_run(
        "protenix-v2",
        profile("protenix-v2"),
        request,
        operation_id="op-protenix-01",
        variant_id=protenix_v2.VARIANT_ID,
    ) == plan
    with pytest.raises(ScientificAdapterError):
        protenix_v2._candidate_plan(
            profile("protenix-v2"),
            fixture("protenix-v2", "negative-duplicate-seeds.json"),
            operation_id="op-protenix-bad",
        )


def test_native_af3_uses_exact_private_artifacts_and_scoped_technical_receipt() -> None:
    request = fixture("alphafold3", "positive-raw.json")
    receipt = {
        "schema": "fs2-serve.nebius.ai/academic-asset-access-receipt/v1",
        "tenant_id": "academic-poc",
        "scope": "technical-poc",
        "parameters_sha256": alphafold3.PARAMETERS_SHA256,
        "authorization_id": "poc-technical-authorization-01",
    }
    plan = alphafold3.compile_run(
        profile("alphafold3"),
        request,
        operation_id="op-af3-01",
        tenant_id="academic-poc",
        access_receipt=receipt,
    )
    assert plan.required_model_artifacts == (
        "alphafold3-parameters",
        "alphafold3-reference-databases",
    )
    assert "--run_data_pipeline" in plan.invocations[0].argv
    assert "--norun_inference" in plan.invocations[0].argv
    assert "--norun_data_pipeline" in plan.invocations[1].argv
    assert "--run_inference" in plan.invocations[1].argv
    assert any(
        mount.mount_path == "/opt/fs2/academic/alphafold3"
        and mount.expected_content_sha256 == alphafold3.PARAMETERS_SHA256
        for mount in plan.invocations[1].runtime_mounts
    )
    with pytest.raises(ScientificAdapterError, match="LicenseAcceptancePending"):
        alphafold3.compile_run(profile("alphafold3"), request, operation_id="op-af3-no-receipt")
    wrong = dict(receipt, tenant_id="another-tenant")
    with pytest.raises(ScientificAdapterError, match="LicenseAcceptancePending"):
        alphafold3.compile_run(
            profile("alphafold3"),
            request,
            operation_id="op-af3-wrong-tenant",
            tenant_id="academic-poc",
            access_receipt=wrong,
        )


def test_openfold_is_non_equivalent_and_uses_exact_offline_upstream_cli() -> None:
    plan = openfold3.compile_run(
        profile("openfold3"), fixture("openfold3", "positive-complex.json"), operation_id="op-of3-01"
    )
    assert openfold3.RELATIONSHIP == "independent-non-equivalent-alternative"
    assert plan.model_id != alphafold3.MODEL_ID
    argv = plan.invocations[1].argv
    assert argv[0] == "/usr/local/bin/fs2-run-openfold3"
    assert "--query-json" in argv
    assert "--runner-yaml" in argv
    assert any(value.endswith("/of3-ob-2025-06-30-174k.pt") for value in argv)
    with pytest.raises(ScientificAdapterError):
        openfold3.compile_run(
            profile("openfold3"),
            fixture("openfold3", "negative-alphafold-alias.json"),
            operation_id="op-of3-bad",
        )


def _mmcif() -> bytes:
    return b"""data_result
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.label_asym_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
ATOM 1 A 0 0 0
ATOM 2 A 1 0 0
ATOM 3 A 1 1 0
ATOM 4 A 1 1 1
#
"""


@pytest.mark.parametrize("module", tuple(MODULES.values()))
def test_canonical_collectors_and_semantic_validators(module, tmp_path: Path) -> None:
    root = tmp_path / "work"
    (root / "outputs").mkdir(parents=True)
    if module is alphafold3:
        structure = root / "outputs/job/seed-1_sample-0/result_model.cif"
        confidence = root / "outputs/job/summary_confidences.json"
    elif module is openfold3:
        structure = root / "outputs/query/seed_1/query_seed_1_sample_0_model.cif"
        confidence = root / "outputs/query/seed_1/query_seed_1_confidences_aggregated.json"
    else:
        structure = root / "outputs/result.cif"
        confidence = root / "outputs/confidence.json"
    structure.parent.mkdir(parents=True, exist_ok=True)
    structure.write_bytes(_mmcif())
    confidence.write_text('{"plddt_mean":82.5,"ptm":0.71}')
    collected = module.collect_output(root)
    result = module.validate_output(
        collected.manifest,
        artifact_loader=collected.blobs.__getitem__,
        **({"expected_structures": 1} if module in {alphafold3, openfold3, protenix_v2} else {}),
    )
    assert result["status"] == "passed"
    assert result["structure_count"] == 1


def test_variant_id_is_checked_at_public_dispatch() -> None:
    with pytest.raises(ScientificAdapterError, match="variant_id"):
        compile_adapter_run(
            "esmfold2",
            profile("esmfold2"),
            fixture("esmfold2", "positive-sequence.json"),
            operation_id="op-wrong-variant",
            variant_id="wrong-variant",
        )
