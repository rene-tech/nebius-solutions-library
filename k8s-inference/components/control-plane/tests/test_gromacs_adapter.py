import copy
import json
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from fs2_serve.scientific_batch.adapters import gromacs
from fs2_serve.scientific_batch.input_contracts import public_input_contract, validate_input_roles
from fs2_serve.scientific_batch.models import MaterializationMode, ScientificInputArtifact
from fs2_serve.scientific_batch.profile_catalog import ScientificRequestError

ROOT = Path(__file__).resolve().parents[3]
OP = "9eb1af68-cee7-46ea-9c3c-270e039ba923"


def profile():
    return json.loads((ROOT / "models/molecular-dynamics/gromacs/activation/workload-profile.json").read_text())[
        "profile"
    ]


def request():
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
        "parameters": {
            "schema": gromacs.PARAMETER_SCHEMA,
            "jobs": [
                {
                    "id": "replica-1",
                    "steps": [
                        {"id": "production", "command": "mdrun", "args": ["-s", "production.tpr"]},
                    ],
                }
            ],
        },
    }


def source():
    return ScientificInputArtifact(
        logical_artifact_id=gromacs.INPUT_ID,
        semantic_type="gromacs-input-bundle/v1",
        artifact_id=UUID("cf9c5eea-005f-4aa4-bc74-6fb9728d6fbc"),
        digest="sha256:" + "b" * 64,
        size_bytes=1000,
        media_type="application/x-tar",
        compression="gzip",
    )


def test_schema_and_oci_identity_are_unrouted_until_qualification():
    value = profile()
    Draft202012Validator(
        json.loads((ROOT / "catalog/runtime/schema/scientific-workload-profile.schema.json").read_text())
    ).validate(value)
    assert value["source"]["kind"] == "oci" and len(value["source"]["revision"]) == 64
    assert value["route_exposed"] is value["interface"]["mcp"]["invocable"] is False
    assert (ROOT / "catalog/runtime/schema/gromacs-workflow-request.schema.json").read_bytes() == (
        ROOT / "components/control-plane/src/fs2_serve/model_input_schemas/gromacs-workflow.json"
    ).read_bytes()


def test_two_replicas_compile_into_separate_gpu_shards_not_another_queue():
    body = request()
    replica = copy.deepcopy(body["parameters"]["jobs"][0])
    replica["id"] = "replica-2"
    body["parameters"]["jobs"].append(replica)
    plan = gromacs.compile_run(profile(), body, operation_id=OP, input_artifacts=(source(),))
    assert [item.shard_id for item in plan.invocations] == ["replica-1", "replica-2"]
    assert len({item.working_directory for item in plan.invocations}) == 2
    for item in plan.invocations:
        assert item.materializations[0].mode == MaterializationMode.COPY_FILE
        assert item.collector_id == "gromacs-workflow-v1"
        assert item.argv[-1] == "companion"
    assert plan.controller_plan.stages[0].checkpoint_mode.value == "resume"


def test_bundle_contract_is_discoverable_and_verified_before_admission():
    assert public_input_contract("gromacs")["entry"]["name"] == "gromacs-inputs"
    validate_input_roles("gromacs", request(), (source(),))
    with pytest.raises(ScientificRequestError):
        validate_input_roles("gromacs", request(), ())
    with pytest.raises(ValueError, match="verified"):
        gromacs.compile_run(profile(), request(), operation_id=OP, input_artifacts=())
