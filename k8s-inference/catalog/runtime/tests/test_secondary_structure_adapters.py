"""Cross-contract tests for controller-owned secondary structure adapters."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
CONTROL_PLANE_SOURCE = ROOT / "components/control-plane/src"
sys.path.insert(0, str(CONTROL_PLANE_SOURCE))

from fs2_serve.scientific_batch import (  # noqa: E402
    ArtifactAccessContext,
    ResourceClass,
    ScientificInputArtifact,
    compile_adapter_run,
)
from fs2_serve.scientific_batch.adapters import (  # noqa: E402
    alphafold3,
    esmfold2,
    esmfold2_fast,
    openfold3,
    protenix_v2,
)
from fs2_serve.scientific_batch.adapters.common import (  # noqa: E402
    ScientificAdapterError,
    profile_from_catalog,
    runtime_recipe_sha256,
)

FIXTURE_ROOT = ROOT / "models/structure/runtime"
WRAPPER_ROOT = Path(
    os.environ.get(
        "FS2_SECONDARY_WRAPPER_ROOT",
        ROOT / "models/cancer-immunotherapy/images/structure-secondary",
    )
)
PROFILES = json.loads(
    (ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json").read_text()
)
REQUEST_SCHEMA = json.loads(
    (ROOT / "catalog/runtime/schema/scientific-run-request.schema.json").read_text()
)
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
    candidate = deepcopy(profile_from_catalog(PROFILES, model_id))
    if model_id == "alphafold3":
        reference = next(
            item
            for item in candidate["artifact_requirements"]
            if item["artifact_id"] == alphafold3.REFERENCE_ARTIFACT
        )
        reference["content_digest_sha256"] = "c" * 64
        reference["localization_manifest_sha256"] = "d" * 64
    return candidate


def fixture(model_id: str, filename: str):
    return json.loads(
        (FIXTURE_ROOT / DIRECTORIES[model_id] / "fixtures" / filename).read_text()
    )


def input_artifact(*, enriched: bool = False) -> ScientificInputArtifact:
    return ScientificInputArtifact(
        logical_artifact_id="model-input",
        semantic_type="processed-input/v1" if enriched else "request/v1",
        artifact_id=uuid4(),
        digest="sha256:" + "a" * 64,
        size_bytes=1024,
        media_type="application/x-tar" if enriched else "application/json",
        compression="zstd" if enriched else None,
    )


def ordinary_request_access() -> ArtifactAccessContext:
    """The request input is independent of operator-owned model licensing."""

    return ArtifactAccessContext(profile="public", receipt_digest=None)


def compile_fixture(model_id: str, filename: str):
    request = fixture(model_id, filename)
    enriched = (
        model_id == "alphafold3" and request["parameters"]["input_mode"] == "enriched"
    )
    return compile_adapter_run(
        model_id,
        profile(model_id),
        request,
        operation_id=f"op-{model_id}-01",
        variant_id=MODULES[model_id].VARIANT_ID,
        access_context=ordinary_request_access(),
        input_artifacts=(input_artifact(enriched=enriched),),
    )


@pytest.mark.parametrize("model_id", MODULES)
def test_two_positive_and_one_negative_fixture_per_model(model_id: str) -> None:
    candidate = profile(model_id)
    positives = sorted(
        (FIXTURE_ROOT / DIRECTORIES[model_id] / "fixtures").glob("positive-*.json")
    )
    negatives = sorted(
        (FIXTURE_ROOT / DIRECTORIES[model_id] / "fixtures").glob("negative-*.json")
    )
    assert len(positives) == 2
    assert negatives
    for path in positives:
        request = json.loads(path.read_text())
        Draft202012Validator(REQUEST_SCHEMA).validate(request)
        Draft202012Validator(
            candidate["interface"]["parameter_schema_definition"]
        ).validate(request["parameters"])
        compile_fixture(model_id, path.name)
    if model_id == "alphafold3":
        denial = json.loads(negatives[0].read_text())
        assert denial["state"] == "LicenseAcceptancePending"
        assert denial["activation_allowed"] is False
        blocked = deepcopy(candidate)
        parameters = next(
            item
            for item in blocked["artifact_requirements"]
            if item["artifact_id"] == alphafold3.PARAMETERS_ARTIFACT
        )
        parameters["supply_state"] = "unresolved"
        with pytest.raises(ScientificAdapterError, match="LicenseAcceptancePending"):
            request = fixture(model_id, "positive-raw.json")
            compile_adapter_run(
                model_id,
                blocked,
                request,
                operation_id="op-af3-denied",
                variant_id=alphafold3.VARIANT_ID,
                access_context=ordinary_request_access(),
                input_artifacts=(input_artifact(),),
            )
    else:
        request = fixture(model_id, negatives[0].name)
        with pytest.raises((ScientificAdapterError, ValueError)):
            compile_adapter_run(
                model_id,
                candidate,
                request,
                operation_id=f"op-{model_id}-negative",
                variant_id=MODULES[model_id].VARIANT_ID,
                access_context=ordinary_request_access(),
                input_artifacts=(input_artifact(),),
            )


@pytest.mark.parametrize("model_id", MODULES)
def test_profile_recipe_and_hot_first_burst_placement_are_compiler_owned(
    model_id: str,
) -> None:
    candidate = profile(model_id)
    assert candidate["execution_identity"]["runtime_recipe_sha256"] == (
        runtime_recipe_sha256(ROOT, model_id)
    )
    assert candidate["resources"]["compatible_pool_ids"] == [
        "h100-reserved-8x",
        "h100-1x",
    ]
    for stage in candidate["workload"]["stages"]:
        expected = (
            ["h100-reserved-8x", "h100-1x"] if stage["resource_class"] == "gpu" else []
        )
        assert stage["placement"]["compatible_pool_ids"] == expected


@pytest.mark.parametrize(
    "model_id,fixture_name",
    (
        ("esmfold2", "positive-sequence.json"),
        ("esmfold2-fast", "positive-short.json"),
        ("alphafold3", "positive-raw.json"),
        ("openfold3", "positive-monomer.json"),
        ("protenix-v2", "positive-monomer.json"),
    ),
)
def test_missing_runtime_artifact_fails_during_preflight_before_gpu_admission(
    model_id: str, fixture_name: str
) -> None:
    candidate = profile(model_id)
    candidate["artifact_requirements"] = candidate["artifact_requirements"][1:]
    with pytest.raises(ScientificAdapterError, match="artifact"):
        compile_adapter_run(
            model_id,
            candidate,
            fixture(model_id, fixture_name),
            operation_id=f"op-{model_id}-missing-artifact",
            variant_id=MODULES[model_id].VARIANT_ID,
            access_context=ordinary_request_access(),
            input_artifacts=(input_artifact(),),
        )


def test_checked_in_af3_profile_stays_blocked_until_reference_manifest_promotion() -> (
    None
):
    candidate = deepcopy(profile_from_catalog(PROFILES, "alphafold3"))
    with pytest.raises(
        ScientificAdapterError, match="reference bundle manifest is not promoted"
    ):
        compile_adapter_run(
            "alphafold3",
            candidate,
            fixture("alphafold3", "positive-raw.json"),
            operation_id="op-af3-unpromoted-reference",
            variant_id=alphafold3.VARIANT_ID,
            access_context=ordinary_request_access(),
            input_artifacts=(input_artifact(),),
        )


def test_exact_stage_commands_artifacts_and_cpu_before_gpu() -> None:
    cases = {
        "esmfold2": ("positive-sequence.json", ("prepare-input", "fold")),
        "esmfold2-fast": ("positive-short.json", ("prepare-input", "fold")),
        "alphafold3": ("positive-raw.json", ("data", "inference")),
        "openfold3": ("positive-complex.json", ("prepare", "predict")),
        "protenix-v2": ("positive-monomer.json", ("prep", "pred")),
    }
    for model_id, (filename, commands) in cases.items():
        plan = compile_fixture(model_id, filename)
        assert tuple(item.argv[1] for item in plan.invocations) == commands
        assert tuple(stage.resource_class for stage in plan.controller_plan.stages) == (
            ResourceClass.CPU,
            ResourceClass.GPU,
        )
        assert plan.invocations[1].consumes == (plan.invocations[0].produces,)
        assert all(
            item.argv[0].startswith("/usr/local/bin/fs2-run-")
            for item in plan.invocations
        )
        assert all(
            "s3://" not in arg and "file://" not in arg
            for item in plan.invocations
            for arg in item.argv
        )


def test_esm_typed_prepare_production_defaults_and_complete_cache() -> None:
    for model_id, filename, expected_model_path in (
        ("esmfold2", "positive-sequence.json", "/models/esmfold2"),
        ("esmfold2-fast", "positive-short.json", "/models/esmfold2-fast"),
    ):
        plan = compile_fixture(model_id, filename)
        prepare, fold = plan.invocations
        assert {"--input-manifest", "--sequence", "--mode", "--seed"} <= set(
            prepare.argv
        )
        assert fold.argv[fold.argv.index("--model-dir") + 1] == expected_model_path
        assert fold.argv[fold.argv.index("--esmc-dir") + 1] == "/models/esmc-6b"
        assert (
            fold.argv[fold.argv.index("--ccd-path") + 1]
            == "/databases/esmfold2/ccd.pkl"
        )
        assert fold.argv[fold.argv.index("--num-loops") + 1] == "20"
        assert fold.argv[fold.argv.index("--num-sampling-steps") + 1] == "200"
        assert "--smoke" not in fold.argv
        mounts = {item.artifact_id: item for item in fold.runtime_mounts}
        expected_trunk = (
            "esmfold2-trunk" if model_id == "esmfold2" else "esmfold2-fast-trunk"
        )
        assert mounts[expected_trunk].mount_path == expected_model_path
        assert mounts["esmc-6b"].mount_path == "/models/esmc-6b"
        assert mounts["esmfold2-ccd"].mount_path == "/databases/esmfold2"
        assert all(
            item.read_only and item.readiness_receipt_sha256 is None
            for item in mounts.values()
        )
        environment = dict(fold.environment)
        assert environment["FS2_MODEL_DIR"] == expected_model_path
        assert environment["FS2_ESMC_MODEL_DIR"] == "/models/esmc-6b"
        assert environment["ESMCFOLD_CCD_PATH"] == "/databases/esmfold2/ccd.pkl"
        assert fold.argv[fold.argv.index("--runtime-localization-marker") + 1] == (
            f"{fold.working_directory}/.fs2/runtime-localization.json"
        )
        requirements = {
            item["artifact_id"]: item["total_size_bytes"]
            for item in profile(model_id)["artifact_requirements"]
        }
        assert profile(model_id)["resources"]["cache_pvc"]["size_bytes"] >= sum(
            requirements[item] for item in plan.required_model_artifacts
        )


def test_af3_processed_handoff_and_exact_private_paths() -> None:
    raw = compile_fixture("alphafold3", "positive-raw.json")
    data, inference = raw.invocations
    assert data.argv[:2] == ("/usr/local/bin/fs2-run-alphafold3", "data")
    assert data.argv[data.argv.index("--db-dir") + 1] == "/databases"
    assert (
        data.argv[data.argv.index("--reference-artifact-id") + 1]
        == alphafold3.REFERENCE_ARTIFACT
    )
    assert data.argv[data.argv.index("--expected-db-content-sha256") + 1] == "c" * 64
    assert data.argv[data.argv.index("--expected-db-manifest-sha256") + 1] == "d" * 64
    assert inference.argv[:2] == ("/usr/local/bin/fs2-run-alphafold3", "inference")
    assert inference.argv[inference.argv.index("--processed-json") + 1].endswith(
        "/input/processed.json"
    )
    assert inference.argv[inference.argv.index("--provenance-marker") + 1].endswith(
        "/input/provenance.json"
    )
    assert inference.argv[inference.argv.index("--model-dir") + 1] == "/models"
    assert (
        inference.argv[inference.argv.index("--expected-reference-content-sha256") + 1]
        == "c" * 64
    )
    assert (
        inference.argv[inference.argv.index("--expected-reference-manifest-sha256") + 1]
        == "d" * 64
    )
    assert data.runtime_mounts[0].mount_path == "/databases"
    assert data.runtime_mounts[0].expected_content_sha256 == "c" * 64
    assert data.runtime_mounts[0].artifact_manifest_sha256 == "d" * 64
    assert inference.runtime_mounts[0].mount_path == "/models/af3.bin.zst"
    assert inference.runtime_mounts[0].sub_path == "alphafold3/af3.bin.zst"
    assert (
        inference.runtime_mounts[0].expected_content_sha256
        == alphafold3.PARAMETERS_SHA256
    )
    assert (
        inference.runtime_mounts[0].artifact_manifest_sha256
        == alphafold3.PARAMETERS_SHA256
    )
    assert all(
        item.authorization_receipt_sha256 is None
        for stage in raw.invocations
        for item in stage.runtime_mounts
    )
    assert raw.required_model_artifacts == (
        "alphafold3-parameters",
        "alphafold3-public-databases-v3.0",
    )
    enriched = compile_fixture("alphafold3", "positive-enriched.json")
    assert len(enriched.invocations) == 1
    assert enriched.invocations[0].argv[1] == "inference"


def test_af3_authorized_poc_execution_needs_no_request_receipt_or_tenant() -> None:
    candidate = profile("alphafold3")
    deployment = candidate["access"]
    assert deployment["operational_activation"] == "user-authorized-academic-poc"
    assert deployment["materialization"] == "restricted-quarantine-poc-authorized"
    assert deployment["receipt_digest"] is None

    plan = compile_adapter_run(
        "alphafold3",
        candidate,
        fixture("alphafold3", "positive-raw.json"),
        operation_id="ordinary-poc-no-receipt",
        variant_id=alphafold3.VARIANT_ID,
        access_context=ordinary_request_access(),
        input_artifacts=(input_artifact(),),
    )

    assert len(plan.invocations) == 2
    assert plan.required_model_artifacts == (
        alphafold3.PARAMETERS_ARTIFACT,
        alphafold3.REFERENCE_ARTIFACT,
    )
    assert all(
        mount.authorization_receipt_sha256 is None
        for invocation in plan.invocations
        for mount in invocation.runtime_mounts
    )
    generated = json.dumps(
        [
            {"argv": invocation.argv, "environment": invocation.environment}
            for invocation in plan.invocations
        ]
    )
    assert "academic-poc" not in generated
    assert "receipt_digest" not in generated


def test_af3_reference_content_and_localization_manifest_cannot_be_equated() -> None:
    candidate = profile("alphafold3")
    reference = next(
        item
        for item in candidate["artifact_requirements"]
        if item["artifact_id"] == alphafold3.REFERENCE_ARTIFACT
    )
    reference["localization_manifest_sha256"] = reference["content_digest_sha256"]
    with pytest.raises(
        ScientificAdapterError, match="reference bundle manifest is not promoted"
    ):
        compile_adapter_run(
            "alphafold3",
            candidate,
            fixture("alphafold3", "positive-raw.json"),
            operation_id="af3-equal-reference-identities",
            variant_id=alphafold3.VARIANT_ID,
            access_context=ordinary_request_access(),
            input_artifacts=(input_artifact(),),
        )


def test_protenix_single_composite_root_and_offline_typed_semantics() -> None:
    plan = compile_fixture("protenix-v2", "positive-monomer.json")
    assert plan.required_model_artifacts == ("protenix-v2",)
    prep, pred = plan.invocations
    assert prep.runtime_artifacts == pred.runtime_artifacts == ("protenix-v2",)
    assert prep.runtime_mounts == pred.runtime_mounts
    assert prep.runtime_mounts[0].mount_path == "/models/protenix-v2"
    assert (
        prep.runtime_mounts[0].expected_content_sha256
        == protenix_v2.COMPOSITE_MANIFEST_SHA256
    )
    assert (
        prep.runtime_mounts[0].artifact_manifest_sha256
        == protenix_v2.LOCALIZATION_MANIFEST_SHA256
    )
    assert prep.argv[prep.argv.index("--reference-root") + 1] == "/models/protenix-v2"
    assert (
        pred.argv[pred.argv.index("--checkpoint") + 1]
        == "/models/protenix-v2/checkpoint/protenix-v2.pt"
    )
    assert (
        pred.argv[pred.argv.index("--common-dir") + 1] == "/models/protenix-v2/common"
    )
    assert pred.argv[pred.argv.index("--seeds") + 1] == "7,19"
    assert {"--disable-templates", "--disable-rna-msa"} <= set(pred.argv)
    requirement = profile("protenix-v2")["artifact_requirements"][0]
    assert set(protenix_v2.MANDATORY_FILES) == set(requirement["required_files"])
    assert requirement["content_digest_sha256"] == protenix_v2.COMPOSITE_MANIFEST_SHA256


def test_openfold_non_equivalent_no_msa_exact_seed_runner() -> None:
    plan = compile_fixture("openfold3", "positive-complex.json")
    prepare, predict = plan.invocations
    assert openfold3.RELATIONSHIP == "independent-non-equivalent-alternative"
    assert "--database-dir" not in prepare.argv
    assert (
        prepare.argv[prepare.argv.index("--base-runner-yaml") + 1]
        == "/opt/fs2/runtime/openfold3/runner-base.yaml"
    )
    assert predict.argv[predict.argv.index("--model-seeds") + 1] == "13,31"
    assert predict.argv[predict.argv.index("--num-model-seeds") + 1] == "2"
    assert predict.argv[predict.argv.index("--use-templates") + 1] == "false"
    mounts = {item.artifact_id: item for item in predict.runtime_mounts}
    assert mounts["openfold3-openbind-0"].mount_path == "/models/openfold3"
    assert mounts["openfold3-components-bcif"].mount_path == "/databases/openfold3"
    assert plan.required_model_artifacts == (
        "openfold3-openbind-0",
        "openfold3-components-bcif",
    )


@pytest.mark.parametrize(
    "model_id,fixture_name",
    (
        ("esmfold2", "positive-sequence.json"),
        ("esmfold2-fast", "positive-short.json"),
        ("protenix-v2", "positive-monomer.json"),
        ("openfold3", "positive-monomer.json"),
    ),
)
def test_model_parameters_reject_protected_argv_overrides(
    model_id: str, fixture_name: str
) -> None:
    request = fixture(model_id, fixture_name)
    request["parameters"]["args"] = ["--model-dir=/tmp/attacker"]
    with pytest.raises(ScientificAdapterError, match="unknown"):
        compile_adapter_run(
            model_id,
            profile(model_id),
            request,
            operation_id="op-override",
            variant_id=MODULES[model_id].VARIANT_ID,
            access_context=ordinary_request_access(),
            input_artifacts=(input_artifact(),),
        )


def _load_wrapper(filename: str):
    path = WRAPPER_ROOT / filename
    if not path.is_file():
        pytest.skip(
            "structure-secondary image successor is not present in this adapter worktree"
        )
    name = f"fs2_wrapper_test_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(WRAPPER_ROOT))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


@pytest.mark.parametrize(
    "model_id,fixture_name,wrapper,handlers",
    (
        (
            "esmfold2",
            "positive-sequence.json",
            "run_esmfold2.py",
            ("_prepare", "_fold"),
        ),
        (
            "esmfold2-fast",
            "positive-short.json",
            "run_esmfold2.py",
            ("_prepare", "_fold"),
        ),
        (
            "alphafold3",
            "positive-raw.json",
            "run_alphafold3.py",
            ("_data", "_inference"),
        ),
        (
            "openfold3",
            "positive-complex.json",
            "run_openfold3.py",
            ("_prepare", "_predict"),
        ),
        ("protenix-v2", "positive-monomer.json", "run_protenix.py", ("_prep", "_pred")),
    ),
)
def test_generated_argv_executes_through_actual_wrapper_parser(
    model_id: str,
    fixture_name: str,
    wrapper: str,
    handlers: tuple[str, ...],
    monkeypatch,
) -> None:
    module = _load_wrapper(wrapper)
    plan = compile_fixture(model_id, fixture_name)
    for invocation, handler_name in zip(plan.invocations, handlers, strict=True):
        source = (WRAPPER_ROOT / wrapper).read_text(encoding="utf-8")
        missing_flags = sorted(
            flag
            for flag in invocation.argv
            if flag.startswith("--") and f'"{flag}"' not in source
        )
        if not hasattr(module, handler_name) or missing_flags:
            pytest.skip(
                f"published image wrapper successor is pending: handler={handler_name}, flags={missing_flags}"
            )
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            module,
            handler_name,
            lambda args, target=captured: target.update(vars(args)),
        )
        argv = list(invocation.argv[1:])
        if len(inspect.signature(module.main).parameters) == 1:
            module.main(argv)
        else:
            monkeypatch.setattr(sys, "argv", [wrapper, *argv])
            module.main()
        assert captured


def _mmcif(atom_count: int = 10) -> bytes:
    rows = "\n".join(
        f"ATOM {index} A {index}.0 0.0 0.0" for index in range(1, atom_count + 1)
    )
    return (
        "data_result\nloop_\n_atom_site.group_PDB\n_atom_site.id\n"
        "_atom_site.label_asym_id\n_atom_site.Cartn_x\n_atom_site.Cartn_y\n"
        f"_atom_site.Cartn_z\n{rows}\n#\n"
    ).encode()


def test_canonical_collector_accepts_exactly_one_bound_confidence_envelope(
    tmp_path: Path,
) -> None:
    plan = compile_fixture("openfold3", "positive-complex.json")
    invocation = plan.invocations[-1]
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    results = []
    for seed in (13, 31):
        filename = f"seed_{seed}/sample_1.cif"
        path = outputs / filename
        path.parent.mkdir()
        content = _mmcif()
        path.write_bytes(content)
        results.append(
            {
                "seed": seed,
                "sample_index": 0,
                "upstream_summary": None,
                "structure": {
                    "filename": filename,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                },
                "metrics": {"plddt": 82.5, "ptm": 0.7},
            }
        )
    (outputs / "confidence.json").write_text(
        json.dumps(
            {
                "schema": "fs2.nebius.ai/structure-confidence/v1",
                "runtime_id": "openfold3",
                "model_revision": openfold3.SOURCE_REVISION,
                "seeds": [13, 31],
                "samples_per_seed": 1,
                "results": results,
            }
        )
    )
    collected = openfold3.collect_result(invocation, tmp_path)
    assert collected.validation["status"] == "passed"
    assert collected.validation["structure_count"] == 2
    assert [item.name for item in collected.artifacts] == [
        "prediction.13.0",
        "prediction.31.0",
        "confidence",
    ]
    envelope = json.loads((outputs / "confidence.json").read_text())
    envelope["model_revision"] = "wrong-revision"
    (outputs / "confidence.json").write_text(json.dumps(envelope))
    with pytest.raises(ScientificAdapterError, match="identity or cardinality"):
        openfold3.collect_result(invocation, tmp_path)
