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
