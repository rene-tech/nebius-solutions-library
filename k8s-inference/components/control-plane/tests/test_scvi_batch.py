from dataclasses import replace
from uuid import uuid4

import pytest
from fs2_scvi import PARAMETER_SCHEMA
from fs2_scvi.contracts import normalize, request_schema

from fs2_serve.scientific_batch.adapters.scvi_scanvi import validate_entries
from fs2_serve.scientific_batch.input_contracts import public_input_contract, validate_input_roles
from fs2_serve.scientific_batch.models import ScientificInputArtifact
from fs2_serve.scientific_batch.native_workflows import workflow_for_collector
from fs2_serve.scientific_batch.profile_catalog import ScientificRequestError


@pytest.mark.parametrize(
    "model,exit_code,reasons,retryable",
    [
        ("scvi-scanvi", 75, ["Error"], True),
        ("scvi-scanvi", 1, ["Error"], False),
        ("scvi-scanvi", 137, ["OOMKilled"], False),
        ("scvi-scanvi", 75, ["DeadlineExceeded"], False),
        ("scvi-scanvi", 75, ["MaximumExecutionTimeExceeded"], False),
        ("gromacs", 75, ["Error"], False),
    ],
)
def test_only_explicit_scvi_interruption_is_a_retry(model, exit_code, reasons, retryable):
    from fs2_serve.scientific_batch.kubernetes import _reported_failure

    status = {
        "containerStatuses": [
            {
                "name": "scientific-stage",
                "state": {
                    "terminated": {"exitCode": exit_code, "reason": reasons[0]},
                },
            }
        ]
    }
    _, kind, code = _reported_failure(reasons, [status], model_id=model)
    assert kind.retryable is retryable
    if retryable:
        assert code == "SCVI_WORKER_INTERRUPTED"


def anndata():
    return ScientificInputArtifact(
        logical_artifact_id="anndata",
        semantic_type="anndata-counts/v1",
        artifact_id=uuid4(),
        digest="sha256:" + "a" * 64,
        size_bytes=6 * 1024**3,
        media_type="application/x-hdf5",
        compression="none",
    )


def test_large_input_and_permissive_training():
    parameters = {"schema": PARAMETER_SCHEMA, "max_epochs": 400}
    validate_entries({"parameters": parameters}, (anndata(),))
    assert normalize(parameters)["scanvi_max_epochs"] == 20
    assert request_schema()["properties"]["max_epochs"].get("maximum") is None
    assert public_input_contract("scvi-scanvi")["maximum_bytes"] == 25 * 1024**3


def test_mapping_requires_reference_and_training_rejects_unexpected_input():
    reference = replace(
        anndata(),
        logical_artifact_id="reference",
        semantic_type="scvi-reference/v1",
        size_bytes=1024,
        media_type="application/x-tar",
        compression="gzip",
    )
    params = {"schema": PARAMETER_SCHEMA, "mode": "map-query"}
    validate_input_roles("scvi-scanvi", {"parameters": params}, (anndata(), reference))
    with pytest.raises(ScientificRequestError):
        validate_input_roles("scvi-scanvi", {"parameters": params}, (anndata(),))
    with pytest.raises(ScientificRequestError):
        validate_input_roles("scvi-scanvi", {"parameters": {"schema": PARAMETER_SCHEMA}}, (anndata(), reference))


def test_bad_type_is_actionable_before_admission():
    with pytest.raises(ScientificRequestError):
        validate_input_roles(
            "scvi-scanvi", {"parameters": {"schema": PARAMETER_SCHEMA}}, (replace(anndata(), media_type="text/plain"),)
        )


def test_shared_checkpoint_registration_has_no_gpu_imports():
    workflow = workflow_for_collector("scvi-scanvi-workflow-v1")
    assert workflow.engine == "scvi"
    assert workflow.state_filename == "scvi-state.json"
    assert workflow.storage_endpoint == "/internal/scientific-workloads/native/storage"
    assert workflow.normalize({"schema": PARAMETER_SCHEMA})["mode"] == "train"


@pytest.mark.parametrize("shape,ram", [("routine", 128), ("atlas", 256)])
def test_candidate_profiles_and_compilation_use_existing_resource_shapes(shape, ram):
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from fs2_serve.scientific_batch.adapters.scvi_scanvi import compile_run

    root = Path(__file__).resolve().parents[3]
    profile = json.loads((root / "models/visual-science/scvi-scanvi/activation/workload-profile.json").read_text())[
        "profile"
    ]
    Draft202012Validator(
        json.loads((root / "catalog/runtime/schema/scientific-workload-profile.schema.json").read_text())
    ).validate(profile)
    assert profile["route_exposed"] is False
    request = {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "fit-transform",
        "service_class": "customer-batch",
        "input_manifest": {
            "artifact_id": str(uuid4()),
            "sha256": "a" * 64,
            "size_bytes": 1000,
            "media_type": "application/vnd.fs2.scientific-manifest+json",
            "compression": "none",
        },
        "parameters": {"schema": PARAMETER_SCHEMA, "resource_profile": shape},
    }
    plan = compile_run(profile, request, operation_id=str(uuid4()), input_artifacts=(anndata(),))
    stage = plan.controller_plan.stages[0]
    assert stage.execution_shape.shape_id == shape
    assert stage.resources.memory_bytes == ram * 1024**3
    assert stage.execution_shape.accelerator_count == 1
    assert plan.invocations[0].materializations[0].destination.endswith("/input.h5ad")


def test_single_cell_failure_is_not_interpreted_as_an_md_job(tmp_path):
    import hashlib
    import json

    from fs2_gromacs.files import atomic_json

    from fs2_serve.scientific_batch import native_failures
    from fs2_serve.scientific_batch.adapters.staged_workspace import wrap_stage_argv
    from fs2_serve.scientific_batch.models import StageInvocation

    operation = str(uuid4())
    command = ("python", "-m", "fs2_scvi.worker", "--operation-id", operation)
    invocation = StageInvocation(
        stage_id="workflow",
        shard_id="main",
        argv=wrap_stage_argv("/mnt/fs2-scientific/test/main", command),
        environment=(),
        working_directory="/mnt/fs2-scientific/test/main",
        consumes=(),
        produces="run.test.workflow.main",
        collector_id="scvi-scanvi-workflow-v1",
        validator_id="scvi-scanvi-workflow-v1",
        max_output_artifacts=100,
        max_output_bytes=1024**2,
    )
    parameters = normalize({"schema": PARAMETER_SCHEMA})
    atomic_json(tmp_path / ".fs2/request.json", parameters)
    atomic_json(
        tmp_path / "result.json",
        {
            "schema": "fs2-serve.nebius.ai/scvi-workflow-result/v1",
            "operation_id": operation,
            "parameters": parameters,
            "status": "failed",
            "error_type": "ValueError",
        },
    )
    atomic_json(
        tmp_path / ".fs2/stage-failed.json",
        {
            "schema": native_failures.FAILURE_SCHEMA,
            "status": "failed",
            "exit_code": 1,
            "stage_id": "workflow",
            "shard_id": "main",
            "logical_output_id": invocation.produces,
            "collector_id": invocation.collector_id,
            "validator_id": invocation.validator_id,
            "argv_sha256": hashlib.sha256(json.dumps(command, separators=(",", ":")).encode()).hexdigest(),
        },
    )
    output = native_failures.collect_failed_diagnostics(
        invocation, tmp_path, workflow_for_collector(invocation.collector_id)
    )
    assert output.validation["status"] == "failed"
    assert output.validation["scientific_validation_passed"] is False
    assert [item.name for item in output.artifacts] == ["failed-result"]


def test_activation_keeps_existing_apps_and_projects_both_memory_shapes():
    import copy
    import hashlib
    import importlib.util
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    module_path = root / "models/molecular-dynamics/gromacs/activation/prepare.py"
    spec = importlib.util.spec_from_file_location("scvi_release_test", module_path)
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    before = json.loads((root / "catalog/runtime/contracts/scientific-execution-map.json").read_text())
    # Exercise first onboarding even after the source catalog includes scVI.
    # This fixture has no prior profile claims; hash its actual retained rows.
    before["models"] = [row for row in before["models"] if row["model_id"] != "scvi-scanvi"]
    before["qualification_baselines"] = {}
    candidate = json.loads((root / "models/visual-science/scvi-scanvi/activation/workload-profile.json").read_text())[
        "profile"
    ]
    scheduling = release.canonical(
        {
            "pools": {pool: {} for pool in candidate["resources"]["compatible_pool_ids"]},
            "model_eligible_pool_ids": {"gromacs": ["l40s-1x"]},
            "quotas": {"keep": True},
        }
    )
    values = {
        "scientificBatch": {
            "executionMap": copy.deepcopy(before),
            "schedulingContractSha256": hashlib.sha256(scheduling).hexdigest(),
            "schedulingContractConfigMapName": "fixture-abc",
            "schedulingContractNamespace": "fs2-system",
            "schedulingContractKey": "scheduling.json",
        },
        "scientificArtifacts": {"mediaTypes": ["application/json"]},
    }
    image = "registry.test/scvi@sha256:" + "a" * 64
    profile, row, overlay, config = release.prepare(
        values,
        scheduling,
        candidate,
        image,
        {"runtime_image": image, "tests": [{"fixture": True}], "recorded_at": "2026-10-06T00:00:00Z"},
        "b" * 64,
    )
    assert overlay["scientificBatch"]["executionMap"]["models"][:-1] == before["models"]
    assert json.loads(config["data"]["scheduling.json"])["quotas"] == {"keep": True}
    assert profile["qualification"]["public_completion_receipt_sha256"] is None
    shapes = row["stages"][0]["execution_shapes"]
    assert [(s["id"], s["resources"]["requests"]["memory"]) for s in shapes] == [
        ("routine", "128Gi"),
        ("atlas", "256Gi"),
    ]
    assert "application/x-hdf5" in overlay["scientificArtifacts"]["mediaTypes"]
    assert "application/vnd.fs2.scvi-checkpoint+json" in overlay["scientificArtifacts"]["mediaTypes"]


def test_published_single_cell_map_is_accepted_by_production_renderer():
    from pathlib import Path

    from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
    from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog

    root = Path(__file__).resolve().parents[3] / "catalog/runtime"
    renderer = FileScientificManifestRenderer(
        path=root / "contracts/scientific-execution-map.json",
        profiles=ScientificProfileCatalog.load(root),
    )
    assert renderer.variant_id("scvi-scanvi") == "scvi-tools-1-5-batch-v1"
