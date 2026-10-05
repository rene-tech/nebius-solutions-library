import copy
import json
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, ValidationError

from fs2_serve.scientific_batch.adapters import gromacs, gromacs_mpi
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


def test_direct_continuation_materializes_large_files_once_and_keeps_aliases():
    from dataclasses import replace

    body = request()
    body["parameters"]["max_output_bytes"] = 12 * 1024**3
    body["parameters"]["continuation_files"] = [
        {"input_id": "resume-00000", "path": "md.part0001.xtc"},
        {"input_id": "resume-00000", "path": "retained/md.part0001.xtc"},
    ]
    entry = replace(
        source(),
        logical_artifact_id="resume-00000",
        semantic_type="gromacs-continuation-file/v1",
        media_type="application/octet-stream",
        compression=None,
        size_bytes=5 * 1024**3,
    )
    validate_input_roles("gromacs", body, (entry,))
    plan = gromacs.compile_run(profile(), body, operation_id=OP, input_artifacts=(entry,))
    invocation = plan.invocations[0]
    assert invocation.consumes == ("resume-00000",)
    assert len(invocation.materializations) == 1
    assert invocation.materializations[0].destination.endswith("/data/md.part0001.xtc")
    assert "retained/md.part0001.xtc" in invocation.workspace_documents[0].canonical_json


@pytest.mark.parametrize("count", [65, 305, 1000])
def test_many_file_continuation_survives_durable_execution_plan_roundtrip(count):
    from dataclasses import replace
    from uuid import uuid4

    from test_scientific_batch_execution_handoff import scheduling

    from fs2_serve.scientific_batch.codec import state_from_value, state_to_value
    from fs2_serve.scientific_batch.models import ArtifactAccessContext, ScientificBatchState, VerifiedInputManifest

    body = request()
    body["parameters"]["continuation_files"] = [
        {"input_id": f"resume-{i:05d}", "path": f"md.part{i:05d}.xtc"} for i in range(count)
    ]
    entries = tuple(
        replace(
            source(),
            logical_artifact_id=f"resume-{i:05d}",
            artifact_id=uuid4(),
            semantic_type="gromacs-continuation-file/v1",
            media_type="application/octet-stream",
            compression=None,
        )
        for i in range(count)
    )
    plan = gromacs.compile_run(profile(), body, operation_id=OP, input_artifacts=entries)
    manifest = VerifiedInputManifest(
        manifest_id="native-parts", manifest_artifact_id=uuid4(), manifest_digest="sha256:" + "a" * 64, entries=entries
    )
    snapshot = scheduling(plan.controller_plan)
    snapshot = replace(
        snapshot,
        model_lane="gromacs",
        stages=tuple(
            replace(stage, placement_class=plan.controller_plan.stage(stage.stage_id).placement_class)
            for stage in snapshot.stages
        ),
    )
    state = ScientificBatchState.admit(
        operation_id=UUID(OP),
        tenant_id="system",
        model_id=plan.model_id,
        variant_id=plan.variant_id,
        input_artifact_id=manifest.manifest_artifact_id,
        plan=plan.controller_plan,
        scheduling=snapshot,
        execution_plan=plan,
        input_manifest=manifest,
        access_context=ArtifactAccessContext(profile="public", tenant_id="system", receipt_digest=None),
    )
    decoded = state_from_value(state_to_value(state))
    assert decoded == state
    assert len(decoded.execution_plan.invocations[0].materializations) == count
    assert len(decoded.execution_plan.invocations[0].consumes) == count


@pytest.mark.parametrize("path", ["../escape.cpt", "/absolute.cpt", ".fs2/state.json"])
def test_continuation_paths_cannot_escape_native_workspace(path):
    body = request()
    body["parameters"]["continuation_files"] = [{"input_id": "resume-00000", "path": path}]
    with pytest.raises(ValueError):
        gromacs.compile_run(profile(), body, operation_id=OP, input_artifacts=(source(),))


def test_bundle_contract_is_discoverable_and_verified_before_admission():
    assert public_input_contract("gromacs")["entry"]["name"] == "gromacs-inputs"
    validate_input_roles("gromacs", request(), (source(),))
    with pytest.raises(ScientificRequestError):
        validate_input_roles("gromacs", request(), ())
    with pytest.raises(ValueError, match="verified"):
        gromacs.compile_run(profile(), request(), operation_id=OP, input_artifacts=())


@pytest.mark.parametrize("nodes", [2, 4, 8])
def test_mpi_compiles_one_gang_with_one_gpu_per_rank(nodes):
    candidate = json.loads(
        (ROOT / "models/molecular-dynamics/gromacs/activation/mpi-workload-profile.json").read_text()
    )["profile"]
    Draft202012Validator(
        json.loads((ROOT / "catalog/runtime/schema/scientific-workload-profile.schema.json").read_text())
    ).validate(candidate)
    body = request()
    body["parameters"].update(schema=gromacs.MPI_PARAMETER_SCHEMA, nodes=nodes)
    body["parameters"]["jobs"][0]["id"] = "gang"
    plan = gromacs_mpi.compile_run(candidate, body, operation_id=OP, input_artifacts=(source(),))
    assert len(plan.invocations) == 1
    assert plan.invocations[0].collector_id == "gromacs-mpi-workflow-v1"
    assert "fs2_gromacs.mpi" in plan.invocations[0].argv
    stage = plan.controller_plan.stages[0]
    assert stage.gang_size == nodes
    assert stage.shards == ("gang",)
    assert stage.checkpoint_mode.value == "resume"


def test_mpi_rejects_ambiguous_shards_and_out_of_range_nodes():
    candidate = json.loads(
        (ROOT / "models/molecular-dynamics/gromacs/activation/mpi-workload-profile.json").read_text()
    )["profile"]
    for nodes, job_id in [(0, "gang"), (9, "gang"), (2, "replica-1")]:
        body = request()
        body["parameters"].update(schema=gromacs.MPI_PARAMETER_SCHEMA, nodes=nodes)
        body["parameters"]["jobs"][0]["id"] = job_id
        with pytest.raises(ValidationError):
            gromacs_mpi.compile_run(candidate, body, operation_id=OP, input_artifacts=(source(),))


@pytest.mark.asyncio
async def test_gromacs_oci_source_is_in_the_operator_inventory_without_false_qualification(registry):
    from fs2_serve.scientific_admin_catalog import ScientificCatalogFileAdapter

    catalog = ROOT / "catalog/runtime"
    receipts = json.loads((catalog / "contracts/scientific-source-candidate-receipts.json").read_text())
    Draft202012Validator(
        json.loads((catalog / "schema/scientific-source-candidate-receipts.schema.json").read_text())
    ).validate(receipts)
    snapshot = await ScientificCatalogFileAdapter(
        registry=registry,
        receipts_file=catalog / "contracts/scientific-source-candidate-receipts.json",
    ).list_models()
    model = next(item for item in snapshot.data.items if item.model_id == "gromacs")
    assert model.execution_mode == "scientific-batch"
    assert model.readiness == "candidate"
    assert "source-identity-agreement" not in model.missing_evidence
    assert model.qualification.state != "identity-mismatch"
    assert model.backend.source_revision == profile()["source"]["revision"]
