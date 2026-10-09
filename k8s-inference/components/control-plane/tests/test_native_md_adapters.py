import copy
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest
from fs2_gromacs.contracts import canonical
from fs2_gromacs.files import atomic_json, inventory
from jsonschema import Draft202012Validator

from fs2_serve.scientific_batch.adapters import amber, lammps, namd
from fs2_serve.scientific_batch.adapters.staged_workspace import STAGE_COMPLETION_SCHEMA, unwrapped_stage_argv
from fs2_serve.scientific_batch.input_contracts import public_input_contract, validate_input_roles
from fs2_serve.scientific_batch.models import MaterializationMode, ScientificInputArtifact
from fs2_serve.scientific_batch.profile_catalog import ScientificRequestError

ROOT = Path(__file__).resolve().parents[3]
OP = "9eb1af68-cee7-46ea-9c3c-270e039ba923"


@pytest.fixture(params=[lammps, namd, amber], ids=["lammps", "namd", "amber"])
def engine(request):
    return request.param


def profile(engine):
    return json.loads(
        (ROOT / f"models/molecular-dynamics/{engine.MODEL_ID}/activation/workload-profile.json").read_text()
    )["profile"]


def request_body(engine):
    step = (
        {"id": "production", "input": "production.in"}
        if engine is lammps
        else {
            "id": "production",
            "config": "production.namd",
            "mode": "dynamics",
            "steps": 10000,
            "output_prefix": "production",
        }
    )
    if engine is amber:
        step = {
            "id": "production", "input": "production.mdin", "topology": "system.prmtop",
            "coordinates": "equilibrated.rst7", "expected_nsteps": 10000,
        }
    return {
        "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
        "operation": "run-workflow",
        "service_class": "customer-batch",
        "input_manifest": {
            "artifact_id": "d4923b81-1470-4c57-ac35-8f389ca68e57",
            "sha256": "a" * 64,
            "size_bytes": 1000,
            "media_type": "application/vnd.fs2.scientific-manifest+json",
            "compression": "none",
        },
        "parameters": {"schema": engine.PARAMETER_SCHEMA, "jobs": [{"id": "replica-1", "steps": [step]}]},
    }


def source(engine):
    return ScientificInputArtifact(
        logical_artifact_id=engine.INPUT_ID,
        semantic_type=f"{engine.MODEL_ID}-input-bundle/v1",
        artifact_id=UUID("cf9c5eea-005f-4aa4-bc74-6fb9728d6fbc"),
        digest="sha256:" + "b" * 64,
        size_bytes=1000,
        media_type="application/x-tar",
        compression="gzip",
    )


def test_profiles_and_schemas_are_typed_but_not_falsely_published(engine):
    value = profile(engine)
    Draft202012Validator(
        json.loads((ROOT / "catalog/runtime/schema/scientific-workload-profile.schema.json").read_text())
    ).validate(value)
    assert value["source"]["revision"] == engine.SOURCE_REVISION
    assert value["route_exposed"] is value["interface"]["mcp"]["invocable"] is False
    assert (ROOT / f"catalog/runtime/schema/{engine.MODEL_ID}-workflow-request.schema.json").read_bytes() == (
        ROOT / f"components/control-plane/src/fs2_serve/model_input_schemas/{engine.MODEL_ID}-workflow.json"
    ).read_bytes()


def test_amber_preserves_private_academic_provenance_without_calling_it_a_nim():
    value = profile(amber)
    assert value["source"]["repository"] == "fs2-platform/amber26-engine"
    assert value["source"]["review_url"] == "https://ambermd.org/GetAmber.php"
    assert value["policy"]["commercial_use"] == "license-dependent"
    assert "academic" in value["policy"]["limitations"][1]


def test_independent_scientific_jobs_reuse_the_durable_gpu_queue(engine):
    body = request_body(engine)
    replica = copy.deepcopy(body["parameters"]["jobs"][0])
    replica["id"] = "replica-2"
    body["parameters"]["jobs"].append(replica)
    plan = engine.compile_run(profile(engine), body, operation_id=OP, input_artifacts=(source(engine),))
    assert [item.shard_id for item in plan.invocations] == ["replica-1", "replica-2"]
    assert len({item.working_directory for item in plan.invocations}) == 2
    for item in plan.invocations:
        # NVIDIA HPC images install python3, not necessarily a python alias.
        assert item.argv[0] == "python3"
        assert item.materializations[0].mode == MaterializationMode.COPY_FILE
        assert item.collector_id == engine.COLLECTOR_ID
        assert f"fs2_{engine.MODEL_ID}.worker" in item.argv
        assert item.argv[-1] == "companion"
    assert plan.controller_plan.stages[0].checkpoint_mode.value == "resume"


def test_input_contract_is_discoverable_and_mismatches_fail_before_gpu_admission(engine):
    assert public_input_contract(engine.MODEL_ID)["entry"]["name"] == engine.INPUT_ID
    validate_input_roles(engine.MODEL_ID, request_body(engine), (source(engine),))
    with pytest.raises(ScientificRequestError):
        validate_input_roles(engine.MODEL_ID, request_body(engine), ())
    with pytest.raises(ValueError, match="verified"):
        engine.compile_run(profile(engine), request_body(engine), operation_id=OP, input_artifacts=())
    wrong = source(namd if engine is lammps else lammps)
    with pytest.raises(ValueError, match="metadata"):
        engine.compile_run(profile(engine), request_body(engine), operation_id=OP, input_artifacts=(wrong,))


def completed(engine, workspace):
    plan = engine.compile_run(profile(engine), request_body(engine), operation_id=OP, input_artifacts=(source(engine),))
    invocation = plan.invocations[0]
    request = engine.ADAPTER.contracts().normalize(request_body(engine)["parameters"])
    (workspace / "data").mkdir()
    (workspace / "data/output.dat").write_bytes(b"native finite output\n")
    atomic_json(workspace / ".fs2/request.json", request)
    command = unwrapped_stage_argv(invocation, label=engine.MODEL_ID)
    atomic_json(
        workspace / ".fs2/stage-complete.json",
        {
            "schema": STAGE_COMPLETION_SCHEMA,
            "status": "passed",
            "stage_id": "workflow",
            "shard_id": "replica-1",
            "logical_output_id": invocation.produces,
            "collector_id": engine.COLLECTOR_ID,
            "validator_id": engine.VALIDATOR_ID,
            "argv_sha256": hashlib.sha256(canonical(command)).hexdigest(),
        },
    )
    result = {
        "schema": engine.RESULT_SCHEMA,
        "operation_id": OP,
        "job_id": "replica-1",
        "status": "succeeded",
        "recipe_sha256": hashlib.sha256(
            canonical(
                {
                    "request": request,
                    "job": "replica-1",
                    "image": engine.ENGINE_ID,
                }
            )
        ).hexdigest(),
        "completed_steps": ["production"],
        "commands": [{"exit_code": 0}],
        "files": inventory(workspace / "data", max_bytes=request["max_output_bytes"]),
    }
    atomic_json(workspace / "result.json", result)
    return invocation, result


def test_collector_requires_exact_recipe_complete_steps_and_real_inventory(engine, tmp_path):
    invocation, result = completed(engine, tmp_path)
    output = engine.collect_companion_output(invocation, tmp_path)
    assert [item.name for item in output.artifacts] == ["result", "file-00000"]
    assert output.validation["status"] == "passed"
    assert output.validation["scientific_convergence_claimed"] is False
    for field, value in [("status", "failed"), ("recipe_sha256", "0" * 64), ("completed_steps", [])]:
        invalid = {**result, field: value}
        atomic_json(tmp_path / "result.json", invalid)
        with pytest.raises(ValueError, match="exact frozen"):
            engine.collect_companion_output(invocation, tmp_path)
    atomic_json(tmp_path / "result.json", result)
    (tmp_path / "data/output.dat").write_bytes(b"uncommitted changed bytes\n")
    with pytest.raises(ValueError, match="inventory"):
        engine.collect_companion_output(invocation, tmp_path)
