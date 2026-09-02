"""Controller-owned execution and semantic tests for secondary adapters."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from fs2_serve.scientific_batch import ResourceClass, compile_adapter_run, profile_from_catalog
from fs2_serve.scientific_batch.adapters import alphafold3, esmfold2, esmfold2_fast, openfold3, protenix_v2
from fs2_serve.scientific_batch.adapters.common import ScientificAdapterError

ROOT = Path(__file__).resolve().parents[3]
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
    assert full.required_model_artifacts == ("esmfold2", "esmc-6b", "esmfold2-ccd")
    assert "--ccd-path" in full.invocations[1].argv
    assert full.invocations[1].argv[0] == "/usr/local/bin/fs2-run-esmfold2"
    assert full.invocations[0].argv[full.invocations[0].argv.index("--sequence") + 1] == (
        fixture("esmfold2", "positive-msa.json")["parameters"]["sequence"]
    )
    assert full.invocations[0].argv[full.invocations[0].argv.index("--mode") + 1] == "precomputed-msa"
    assert full.invocations[1].argv[full.invocations[1].argv.index("--variant") + 1] == "esmfold2"
    assert full.invocations[1].argv[full.invocations[1].argv.index("--num-loops") + 1] == "20"
    assert full.invocations[1].argv[full.invocations[1].argv.index("--num-sampling-steps") + 1] == "200"
    fast = esmfold2_fast.compile_run(
        profile("esmfold2-fast"), fixture("esmfold2-fast", "positive-short.json"), operation_id="op-esm-fast-01"
    )
    assert fast.required_model_artifacts == ("esmfold2-fast", "esmc-6b", "esmfold2-ccd")
    assert fast.invocations[1].argv[0] == "/usr/local/bin/fs2-run-esmfold2"
    assert fast.invocations[1].argv[fast.invocations[1].argv.index("--num-loops") + 1] == "20"
    assert fast.invocations[1].argv[fast.invocations[1].argv.index("--num-sampling-steps") + 1] == "200"
    assert fast.invocations[0].argv[fast.invocations[0].argv.index("--mode") + 1] == "single-sequence"
    assert fast.invocations[1].argv[fast.invocations[1].argv.index("--variant") + 1] == "esmfold2-fast"
    for model_id, plan in (("esmfold2", full), ("esmfold2-fast", fast)):
        requirements = {
            item["artifact_id"]: item["total_size_bytes"]
            for item in profile(model_id)["artifact_requirements"]
        }
        assert profile(model_id)["resources"]["cache_pvc"]["size_bytes"] >= sum(
            requirements[artifact_id] for artifact_id in plan.required_model_artifacts
        )
    with pytest.raises(ScientificAdapterError, match="rejects MSA"):
        esmfold2_fast.compile_run(
            profile("esmfold2-fast"),
            fixture("esmfold2-fast", "negative-msa.json"),
            operation_id="op-esm-fast-bad",
        )


def test_protenix_v2_uses_verified_mirror_and_mandatory_offline_common_data() -> None:
    request = fixture("protenix-v2", "positive-monomer.json")
    plan = protenix_v2._candidate_plan(profile("protenix-v2"), request, operation_id="op-protenix-01")
    assert plan.required_model_artifacts == ("protenix-v2",)
    assert plan.invocations[0].argv[:2] == ("/usr/local/bin/fs2-run-protenix", "prep")
    assert plan.invocations[1].argv[:2] == ("/usr/local/bin/fs2-run-protenix", "pred")
    assert plan.invocations[0].runtime_artifacts == (protenix_v2.MODEL_ARTIFACT,)
    assert plan.invocations[1].runtime_artifacts == (protenix_v2.MODEL_ARTIFACT,)
    assert plan.invocations[0].argv[plan.invocations[0].argv.index("--msa-mode") + 1] == "none"
    assert plan.invocations[1].argv[plan.invocations[1].argv.index("--msa-mode") + 1] == "none"
    inference_environment = dict(plan.invocations[1].environment)
    assert inference_environment["TRITON_CACHE_DIR"] == "/cache/protenix/triton"
    assert inference_environment["CUEQ_TRITON_CACHE_DIR"] == "/cache/protenix/cueq-triton"
    assert inference_environment["TORCH_EXTENSIONS_DIR"] == "/cache/protenix/torch-extensions"
    assert inference_environment["XDG_CACHE_HOME"] == "/cache/protenix/xdg"
    assert plan.invocations[1].materializations[0].mode.value == "extract-tar"
    assert all(
        mount.mount_path == "/models/protenix-v2"
        for invocation in plan.invocations
        for mount in invocation.runtime_mounts
    )
    reference = next(
        item
        for item in profile("protenix-v2")["artifact_requirements"]
        if item["artifact_id"] == protenix_v2.MODEL_ARTIFACT
    )
    assert set(protenix_v2.MANDATORY_COMMON_FILES) <= set(reference["required_files"])
    precomputed = fixture("protenix-v2", "positive-monomer.json")
    precomputed["parameters"]["msa_mode"] = "precomputed"
    with pytest.raises(ScientificAdapterError, match="precomputed MSA relocation"):
        protenix_v2._candidate_plan(
            profile("protenix-v2"),
            precomputed,
            operation_id="op-protenix-precomputed",
        )
    with pytest.raises(ScientificAdapterError):
        protenix_v2._candidate_plan(
            profile("protenix-v2"),
            fixture("protenix-v2", "negative-duplicate-seeds.json"),
            operation_id="op-protenix-bad",
        )


def test_native_af3_uses_exact_private_artifacts_without_per_request_license_gate() -> None:
    request = fixture("alphafold3", "positive-raw.json")
    plan = alphafold3.compile_run(
        profile("alphafold3"),
        request,
        operation_id="op-af3-01",
    )
    assert plan.required_model_artifacts == (
        "alphafold3-public-databases-v3.0",
        "alphafold3-parameters",
    )
    assert plan.invocations[0].argv[0] == "/usr/local/bin/fs2-run-alphafold3"
    assert plan.invocations[0].argv[1] == "data"
    assert plan.invocations[1].argv[1] == "inference"
    assert plan.invocations[1].argv[plan.invocations[1].argv.index("--model-dir") + 1] == "/models"
    assert plan.invocations[0].argv[plan.invocations[0].argv.index("--db-dir") + 1] == "/databases"
    assert plan.invocations[0].argv[plan.invocations[0].argv.index("--db-ready-marker") + 1] == (
        "/databases/.fs2-manifest-sha256"
    )
    assert "--db-manifest" not in plan.invocations[0].argv
    assert plan.invocations[0].argv[plan.invocations[0].argv.index("--reference-artifact-id") + 1] == (
        "alphafold3-public-databases-v3.0"
    )
    assert plan.invocations[0].argv[plan.invocations[0].argv.index("--raw-input-sha256") + 1] == (
        request["input_manifest"]["sha256"]
    )
    assert plan.invocations[1].argv[plan.invocations[1].argv.index("--num-diffusion-samples") + 1] == "2"
    assert plan.invocations[1].argv[plan.invocations[1].argv.index("--model-seeds") + 1] == "11,29"
    assert plan.invocations[1].consumes == (plan.invocations[0].produces,)
    assert plan.invocations[1].argv[plan.invocations[1].argv.index("--expected-model-seeds") + 1] == "11,29"
    assert request["input_manifest"]["artifact_id"] not in plan.invocations[1].consumes
    parameter_mount = plan.invocations[1].runtime_mounts[0]
    assert parameter_mount.read_only and parameter_mount.supplemental_groups == (65532,)
    assert parameter_mount.authorization_receipt_sha256 is None

    enriched = alphafold3.compile_run(
        profile("alphafold3"),
        fixture("alphafold3", "positive-enriched.json"),
        operation_id="op-af3-enriched",
    )
    assert tuple(stage.stage_id for stage in enriched.controller_plan.stages) == ("inference",)
    assert enriched.invocations[0].consumes == (
        fixture("alphafold3", "positive-enriched.json")["input_manifest"]["artifact_id"],
    )


def test_openfold_is_non_equivalent_and_uses_exact_offline_upstream_cli() -> None:
    plan = openfold3.compile_run(
        profile("openfold3"), fixture("openfold3", "positive-complex.json"), operation_id="op-of3-01"
    )
    assert openfold3.RELATIONSHIP == "independent-non-equivalent-alternative"
    assert plan.model_id != alphafold3.MODEL_ID
    argv = plan.invocations[1].argv
    assert argv[0] == "/usr/local/bin/fs2-run-openfold3"
    assert argv[1] == "predict"
    assert "--ccd-path" in argv
    assert argv[argv.index("--ccd-path") + 1] == "/databases/openfold3/components.bcif"
    assert argv[argv.index("--model-seeds") + 1] == "13,31"
    assert argv[argv.index("--num-model-seeds") + 1] == "2"
    assert argv[argv.index("--num-diffusion-samples") + 1] == "1"
    assert plan.required_model_artifacts == (
        openfold3.MODEL_ARTIFACT,
        openfold3.REFERENCE_ARTIFACT,
    )
    assert plan.invocations[0].runtime_artifacts == ()
    prepare = plan.invocations[0].argv
    assert "--database-dir" not in prepare
    assert prepare[prepare.index("--handoff-tar") + 1].endswith("/prepared.tar.zst")
    assert prepare[prepare.index("--output-artifact-id") + 1] == plan.invocations[0].produces
    assert prepare[prepare.index("--raw-input-sha256") + 1] == (
        fixture("openfold3", "positive-complex.json")["input_manifest"]["sha256"]
    )
    assert plan.invocations[1].materializations[0].expected_members == (
        "query.json",
        "provenance.json",
    )
    assert argv[argv.index("--input-artifact-id") + 1] == plan.invocations[0].produces
    assert argv[argv.index("--msa-mode") + 1] == "none"
    assert argv[argv.index("--runner-yaml") + 1].endswith("/runner.yaml")
    assert openfold3.BASE_RUNNER_CONFIG in plan.invocations[0].argv
    assert argv[argv.index("--use-templates") + 1] == "false"
    assert any(value.endswith("/of3-ob-2025-06-30-174k.pt") for value in argv)
    checkpoint = next(
        item
        for item in profile("openfold3")["artifact_requirements"]
        if item["artifact_id"] == openfold3.MODEL_ARTIFACT
    )
    assert checkpoint["required_files"] == ["of3-ob-2025-06-30-174k.pt"]
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


def _write_confidence_envelope(
    root: Path,
    structures: list[Path],
    *,
    runtime_id: str,
    model_revision: str,
    seeds: list[int],
    samples_per_seed: int,
) -> Path:
    confidence = root / "outputs/confidence.json"
    pairs = [(seed, sample) for seed in seeds for sample in range(samples_per_seed)]
    assert len(pairs) == len(structures)
    confidence.write_text(
        json.dumps(
            {
                "schema": "fs2.nebius.ai/structure-confidence/v1",
                "runtime_id": runtime_id,
                "model_revision": model_revision,
                "seeds": seeds,
                "samples_per_seed": samples_per_seed,
                "results": [
                    {
                        "seed": pair[0],
                        "sample_index": pair[1],
                        "upstream_summary": None,
                        "structure": {
                            "filename": structure.relative_to(root / "outputs").as_posix(),
                            "sha256": hashlib.sha256(structure.read_bytes()).hexdigest(),
                            "bytes": structure.stat().st_size,
                        },
                        "metrics": {"plddt_mean": 0.825, "ptm": 0.71},
                    }
                    for structure, pair in zip(structures, pairs, strict=True)
                ],
            },
            sort_keys=True,
        )
    )
    return confidence


@pytest.mark.parametrize("module", tuple(MODULES.values()))
def test_canonical_collectors_and_semantic_validators(module, tmp_path: Path) -> None:
    root = tmp_path / "work"
    (root / "outputs").mkdir(parents=True)
    if module is alphafold3:
        structure = root / "outputs/job/seed-1_sample-0/result_model.cif"
    elif module is openfold3:
        structure = root / "outputs/query/seed_1/query_seed_1_sample_0_model.cif"
    else:
        structure = root / "outputs/result.cif"
    structure.parent.mkdir(parents=True, exist_ok=True)
    structure.write_bytes(_mmcif())
    revision = (
        module.MODEL_REVISION
        if module in {esmfold2, esmfold2_fast}
        else module.WEIGHTS_REVISION
        if module is protenix_v2
        else module.SOURCE_REVISION
    )
    _write_confidence_envelope(
        root,
        [structure],
        runtime_id=module.MODEL_ID,
        model_revision=revision,
        seeds=[7],
        samples_per_seed=1,
    )
    collected = module.collect_output(root)
    result = module.validate_output(
        collected.manifest,
        artifact_loader=collected.blobs.__getitem__,
        **({"expected_structures": 1} if module in {alphafold3, openfold3, protenix_v2} else {}),
    )
    assert result["status"] == "passed"
    assert result["structure_count"] == 1


def test_structure_collector_binds_one_canonical_confidence_envelope(tmp_path: Path) -> None:
    root = tmp_path / "work"
    structures = []
    for sample in ("seed-1_sample-0", "seed-2_sample-0"):
        output = root / "outputs/job" / sample
        output.mkdir(parents=True)
        structure = output / "result_model.cif"
        structure.write_bytes(_mmcif())
        structures.append(structure)
    confidence = _write_confidence_envelope(
        root,
        structures,
        runtime_id=alphafold3.MODEL_ID,
        model_revision=alphafold3.SOURCE_REVISION,
        seeds=[1, 2],
        samples_per_seed=1,
    )

    collected = alphafold3.collect_output(root)
    names = tuple(entry["name"] for entry in collected.manifest["entries"])
    assert names == ("prediction.1", "prediction.2", "confidence")
    result = alphafold3.validate_output(
        collected.manifest,
        artifact_loader=collected.blobs.__getitem__,
        expected_structures=2,
    )
    assert result["confidence_document_count"] == 1

    envelope = json.loads(confidence.read_text())
    envelope["results"][1]["structure"]["sha256"] = "0" * 64
    confidence.write_text(json.dumps(envelope))
    with pytest.raises(ScientificAdapterError, match="identity does not match"):
        alphafold3.collect_output(root)

    envelope["results"] = envelope["results"][:1]
    confidence.write_text(json.dumps(envelope))
    with pytest.raises(ScientificAdapterError, match="result count does not match"):
        alphafold3.collect_output(root)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (lambda value: value.update({"unknown": True}), "unknown"),
        (
            lambda value: value["results"][0]["structure"].update({"filename": "../escape.cif"}),
            "safe relative",
        ),
        (
            lambda value: value["results"][0]["metrics"].update({"plddt_mean": 1.01}),
            "must be finite",
        ),
        (
            lambda value: value["results"][0].update({"sample_index": 1}),
            "unique and complete",
        ),
        (
            lambda value: value["results"][0].update({"upstream_summary": "/host/path.json"}),
            "safe relative",
        ),
    ),
)
def test_confidence_envelope_rejects_deep_adversaries(tmp_path: Path, mutate, message: str) -> None:
    root = tmp_path / "work"
    output = root / "outputs/job/seed-7_sample-0"
    output.mkdir(parents=True)
    structure = output / "result_model.cif"
    structure.write_bytes(_mmcif())
    confidence = _write_confidence_envelope(
        root,
        [structure],
        runtime_id=alphafold3.MODEL_ID,
        model_revision=alphafold3.SOURCE_REVISION,
        seeds=[7],
        samples_per_seed=1,
    )
    envelope = copy.deepcopy(json.loads(confidence.read_text()))
    mutate(envelope)
    confidence.write_text(json.dumps(envelope))
    with pytest.raises(ScientificAdapterError, match=message):
        alphafold3.collect_output(root)


def test_variant_id_is_checked_at_public_dispatch() -> None:
    with pytest.raises(ScientificAdapterError, match="variant_id"):
        compile_adapter_run(
            "esmfold2",
            profile("esmfold2"),
            fixture("esmfold2", "positive-sequence.json"),
            operation_id="op-wrong-variant",
            variant_id="wrong-variant",
        )
