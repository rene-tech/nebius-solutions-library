"""GROMACS allocation conservation from typed input through durable state and Kueue."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from jsonschema import ValidationError
from test_gromacs_adapter import OP, ROOT, request, source
from test_scientific_batch_production import profile_catalog_for, scheduling

from fs2_serve.crypto import KeyedHasher
from fs2_serve.scientific_batch.adapters import gromacs, gromacs_mpi
from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.codec import state_from_value, state_to_value
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.models import (
    ArtifactAccessContext,
    ExecutionMode,
    ResolvedArtifactMaterialization,
    ScientificBatchState,
    ScientificStagePlan,
    ServiceClass,
    VerifiedInputManifest,
    WorkloadKind,
    WorkloadResource,
)
from fs2_serve.scientific_batch.placement import NODE_CAPACITY_SCHEMA, execution_resource_envelope
from fs2_serve.scientific_batch.podset_envelope import (
    PodSetEnvelopeError,
    compare_kueue_usage,
    envelope_from_manifest,
)
from fs2_serve.scientific_batch.scheduling import SchedulingContractError, SchedulingContractResolver

GIB = 1024**3
ACCESS = ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id="tenant-a")


def mpi_request(nodes=2, gpus=None):
    body = request()
    body["parameters"].update(schema=gromacs.MPI_PARAMETER_SCHEMA, nodes=nodes, threads=8)
    body["parameters"]["jobs"][0]["id"] = "gang"
    if gpus is not None:
        body["parameters"]["gpus_per_node"] = gpus
    return body


def mpi_profile():
    """Legacy TCP/local shape fixture, independent of additive live RDMA shapes."""
    profiles = json.loads((ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json").read_text())
    profile = next(item for item in profiles["profiles"] if item["model_id"] == "gromacs-mpi")
    for stage in profile["workload"]["stages"]:
        stage["execution_shapes"] = [shape for shape in stage["execution_shapes"] if not shape.get("rdma")]
    return profile


def renderer(tmp_path: Path, *, edit=None):
    document = json.loads((ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text())
    model = next(item for item in document["models"] if item["model_id"] == "gromacs-mpi")
    for stage in model["stages"]:
        stage["execution_shapes"] = [shape for shape in stage["execution_shapes"] if not shape.get("rdma")]
    if edit:
        edit(model)
    path = tmp_path / "mpi-execution.json"
    path.write_text(json.dumps({"schema": document["schema"], "models": [model]}))
    catalog = profile_catalog_for("gromacs-mpi", profile_document=mpi_profile())
    return FileScientificManifestRenderer(
        path=path,
        profiles=catalog,
        tools_image="registry.test/control@sha256:" + "9" * 64,
        internal_api_url="http://control.fs2.svc:8080",
        capability_authority=ScientificWorkloadCapabilityAuthority(
            KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"k" * 32})
        ),
    ), catalog.get("gromacs-mpi", runnable=False)


def resolver(runtime):
    contract = copy.deepcopy(scheduling().contract)
    capacities = {"h100-ondemand-1x": 1, "h100-reserved-8x": 8, "l40s-1x": 1, "l40s-4x": 4}
    contract["model_eligible_pool_ids"] = {"gromacs-mpi": list(capacities)}
    contract["service_classes"]["customer-batch"]["pool_preference"] = list(capacities)
    # These portable shape-conservation fixtures declare eight maximum hosts
    # in each pool, independently of the requested GPUs per host. Live pool
    # maxima are verified separately; this fixture does not change live quota.
    contract["pools"] = {
        pool: {"resource_flavor": pool + "-flavor", "accelerator_resource_name": "nvidia.com/gpu",
               "capacity": count * 8}
        for pool, count in capacities.items()
    }
    contract["cluster_queues"]["inference"]["spec"]["resourceGroups"][0]["flavors"] = [
        {"name": pool + "-flavor"} for pool in capacities
    ]
    contract["accelerator_node_capacity_schema"] = NODE_CAPACITY_SCHEMA
    contract["accelerator_node_capacity"] = {
        pool: {"cpu_millicores": count * 16000 - 100, "memory_mib": count * 65536, "accelerator_count": count}
        for pool, count in capacities.items()
    }
    return SchedulingContractResolver(
        contract,
        stage_resources={key: execution_resource_envelope(value) for key, value in runtime.executions.items()},
        stage_shape_resources={
            key: execution_resource_envelope(value) for key, value in runtime.execution_shapes.items()
        },
    )


def compile_and_freeze(tmp_path, nodes, gpus):
    runtime, profile = renderer(tmp_path)
    plan = runtime.plan(
        profile, mpi_request(nodes, gpus), operation_id=UUID(OP), access_context=ACCESS, input_artifacts=(source(),)
    )
    policy = resolver(runtime)
    snapshot = policy.freeze(
        service_class="customer-batch",
        model_id="gromacs-mpi",
        tenant_id="tenant-a",
        profile=profile.value,
        plan=plan.controller_plan,
    )
    return runtime, profile, plan, snapshot, policy


def resource_for(plan, snapshot, nodes):
    invocation = plan.invocations[0]
    entry = source()
    materialization = ResolvedArtifactMaterialization.resolve(
        invocation.materializations[0],
        artifact_id=entry.artifact_id,
        digest=entry.digest,
        size_bytes=entry.size_bytes,
        media_type=entry.media_type,
        compression=entry.compression,
    )
    return WorkloadResource(
        operation_id=UUID(OP),
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=uuid4(),
        stage_id="workflow",
        shard_id="gang" if nodes == 1 else None,
        attempt_number=1,
        tenant_id="tenant-a",
        model_id=plan.model_id,
        variant_id=plan.variant_id,
        input_artifact_id=UUID(request()["input_manifest"]["artifact_id"]),
        service_class=ServiceClass.CUSTOMER_BATCH,
        scheduling_snapshot_digest=snapshot.digest,
        namespace="fs2-models",
        name="scientific-gromacs-shape",
        kind=WorkloadKind.JOB if nodes == 1 else WorkloadKind.JOB_SET,
        gang_size=None if nodes == 1 else nodes,
        scheduling=snapshot.stages[0],
        invocation=invocation,
        materializations=(materialization,),
        access_context=ACCESS,
        execution_map_sha256=plan.execution_map_sha256,
        execution_binding=plan.stage_bindings[0],
    )


@pytest.mark.parametrize("nodes,gpus", [(1, 1), (1, 2), (1, 4), (1, 8), (2, 8), (4, 4), (8, 2), (2, 1), (8, 1)])
def test_shape_reaches_pod_and_kueue_without_losing_or_multiplying_gpus(tmp_path, nodes, gpus):
    runtime, _, plan, snapshot, _ = compile_and_freeze(tmp_path, nodes, gpus)
    stage = plan.controller_plan.stages[0]
    assert stage.mode is (ExecutionMode.FANOUT if nodes == 1 else ExecutionMode.TRUE_GANG)
    assert stage.resources.cpu_millis == 8000 * gpus
    assert stage.resources.memory_bytes == 16 * GIB * gpus
    assert stage.resources.ephemeral_storage_bytes == 64 * GIB
    assert snapshot.stages[0].accelerator_count == gpus
    assert "h100-ondemand-1x" not in snapshot.stages[0].resolved_pool_preference or gpus == 1
    assert "l40s-4x" not in snapshot.stages[0].resolved_pool_preference or gpus <= 4
    if nodes > 1 and gpus == 1:
        assert snapshot.stages[0].resolved_pool_preference == ("h100-ondemand-1x", "h100-reserved-8x")
    resource = resource_for(plan, snapshot, nodes)
    manifest = runtime.render(resource)
    pod = (
        manifest["spec"]["template"]
        if nodes == 1
        else manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]
    )
    model = pod["spec"]["containers"][0]
    assert {"name": "mpi-shared-memory", "mountPath": "/dev/shm"} in model["volumeMounts"]  # noqa: S108 - Pod tmpfs
    env = {item["name"]: item.get("value") for item in model["env"]}
    assert env["FS2_GROMACS_MPI_TOTAL_RANKS"] == str(nodes * gpus)
    assert env["FS2_GROMACS_MPI_GPUS_PER_NODE"] == env["FS2_GROMACS_MPI_RANKS_PER_NODE"] == str(gpus)
    assert env["FS2_GROMACS_MPI_NODES"] == str(nodes)
    if nodes == 1:
        assert manifest["kind"] == "Job"
        assert env["FS2_MPI_HOSTS"] == "localhost" and env["FS2_MPI_RANK"] == "0"
        assert "FS2_MPI_SSH_SEED" not in env
        assert env["FS2_GROMACS_MPI_TRANSPORT"] == "ucx-local"
    else:
        assert manifest["spec"]["replicatedJobs"][0]["replicas"] == nodes
        assert len(env["FS2_MPI_HOSTS"].split(",")) == nodes
        assert env["FS2_GROMACS_MPI_TRANSPORT"] == "tcp-host-staged"
        assert "podAntiAffinity" in pod["spec"]["affinity"]
    envelope = envelope_from_manifest(manifest, resource.kind)
    assert envelope.pod_count == nodes
    assert envelope.aggregate_requests.accelerator("nvidia.com/gpu") == nodes * gpus
    assert envelope.aggregate_requests.cpu_millis == nodes * (8000 * gpus + 100)
    assert envelope.aggregate_requests.memory_bytes == nodes * (16 * GIB * gpus + 256 * 1024**2)
    assignments = [{"name": envelope.names[0], "count": nodes, "resourceUsage": {"nvidia.com/gpu": str(nodes * gpus)}}]
    compare_kueue_usage(envelope, assignments, accelerator_resource="nvidia.com/gpu")
    assignments[0]["resourceUsage"]["nvidia.com/gpu"] = str(nodes * gpus + 1)
    with pytest.raises(PodSetEnvelopeError):
        compare_kueue_usage(envelope, assignments, accelerator_resource="nvidia.com/gpu")


@pytest.mark.parametrize("nodes", [1, 2])
def test_shape_persists_with_execution_binding_and_detects_durable_count_drift(tmp_path, nodes):
    _, _, plan, snapshot, _ = compile_and_freeze(tmp_path, nodes, 8)
    artifact = UUID(request()["input_manifest"]["artifact_id"])
    state = ScientificBatchState.admit(
        operation_id=UUID(OP),
        tenant_id="tenant-a",
        plan=plan.controller_plan,
        scheduling=snapshot,
        model_id=plan.model_id,
        variant_id=plan.variant_id,
        input_artifact_id=artifact,
        execution_plan=plan,
        access_context=ACCESS,
        input_manifest=VerifiedInputManifest("input-manifest", artifact, "sha256:" + "a" * 64, (source(),)),
    )
    document = json.loads(json.dumps(state_to_value(state)))
    reopened = state_from_value(document)
    assert reopened == state
    assert reopened.plan.stages[0].execution_shape.accelerator_count == 8
    assert dict(reopened.execution_plan.stage_bindings[0].environment)["FS2_EXECUTION_SHAPE_ID"] == (
        f"{'single-node' if nodes == 1 else 'multi-node'}-8gpu"
    )
    document["scheduling"]["stages"][0]["accelerator_count"] = 1
    with pytest.raises(ValueError, match="frozen execution shape"):
        state_from_value(document)


def test_request_shape_changes_idempotency_identity_and_legacy_requests_keep_their_envelope():
    legacy = gromacs_mpi.compile_run(mpi_profile(), mpi_request(), operation_id=OP, input_artifacts=(source(),))
    modern = gromacs_mpi.compile_run(mpi_profile(), mpi_request(2, 8), operation_id=OP, input_artifacts=(source(),))
    assert legacy.controller_plan.stages[0].execution_shape is None
    assert legacy.controller_plan.stages[0].resources.cpu_millis == 8000
    assert legacy.request_sha256 != modern.request_sha256
    assert "gpus_per_node" not in json.loads(legacy.invocations[0].workspace_documents[0].canonical_json)
    with pytest.raises(ValueError, match="gang_size >= 2"):
        ScientificStagePlan(stage_id="other", mode=ExecutionMode.TRUE_GANG, shards=("gang",), gang_size=1)


@pytest.mark.parametrize("nodes,gpus", [(3, 8), (5, 4), (0, 1), (9, 1), (1, 3), (1, True)])
def test_unbounded_or_unsupported_gpu_shapes_are_rejected(nodes, gpus):
    with pytest.raises(ValidationError):
        gromacs_mpi.compile_run(mpi_profile(), mpi_request(nodes, gpus), operation_id=OP, input_artifacts=(source(),))


def test_shape_missing_from_old_profile_or_execution_map_fails_closed(tmp_path):
    profile = mpi_profile()
    del profile["workload"]["stages"][0]["execution_shapes"]
    with pytest.raises(ValueError, match="not declared"):
        gromacs_mpi.compile_run(profile, mpi_request(2, 8), operation_id=OP, input_artifacts=(source(),))
    runtime, profile = renderer(tmp_path, edit=lambda row: row["stages"][0].pop("execution_shapes"))
    with pytest.raises(ScientificExecutionMapError, match="absent"):
        runtime.plan(profile, mpi_request(1, 8), access_context=ACCESS, input_artifacts=(source(),))


def test_profile_shape_resource_drift_and_capacity_unknown_fail_before_admission(tmp_path):
    runtime, profile, plan, _, policy = compile_and_freeze(tmp_path, 2, 8)
    changed = copy.deepcopy(dict(profile.value))
    shape = next(s for s in changed["workload"]["stages"][0]["execution_shapes"] if s["id"] == "multi-node-8gpu")
    shape["resources"]["cpu_millis"] -= 1000
    kwargs = dict(
        service_class="customer-batch", model_id="gromacs-mpi", tenant_id="tenant-a", plan=plan.controller_plan
    )
    with pytest.raises(SchedulingContractError, match="frozen execution shape"):
        policy.freeze(profile=changed, **kwargs)
    unknown = copy.deepcopy(policy.contract)
    del unknown["accelerator_node_capacity_schema"]
    del unknown["accelerator_node_capacity"]
    with pytest.raises(SchedulingContractError, match="verified per-node"):
        SchedulingContractResolver(unknown).freeze(profile=profile.value, **kwargs)
    runtime.execution_shapes.clear()
    with pytest.raises(ScientificExecutionMapError, match="absent"):
        runtime.plan(profile, mpi_request(2, 8), access_context=ACCESS, input_artifacts=(source(),))


def test_frozen_render_survives_catalog_refresh_and_refuses_gpu_allocation_drift(tmp_path):
    runtime, _, plan, snapshot, _ = compile_and_freeze(tmp_path, 2, 8)
    resource = resource_for(plan, snapshot, 2)
    expected = runtime.render(resource)
    runtime.executions.clear()
    runtime.execution_shapes.clear()
    assert runtime.render(resource) == expected
    with pytest.raises(ScientificExecutionMapError, match="frozen allocation"):
        runtime.render(replace(resource, scheduling=replace(resource.scheduling, accelerator_count=1)))


@pytest.mark.parametrize("mutation", ["gpus", "cpu"])
def test_operator_shape_must_cover_the_requested_gpu_and_thread_count(mutation):
    profile = mpi_profile()
    shape = next(s for s in profile["workload"]["stages"][0]["execution_shapes"] if s["id"] == "single-node-8gpu")
    if mutation == "gpus":
        shape["placement"]["accelerator"]["count"] = 4
    else:
        shape["resources"]["cpu_millis"] = 8000
    with pytest.raises(ValueError, match="catalog"):
        gromacs_mpi.compile_run(profile, mpi_request(1, 8), operation_id=OP, input_artifacts=(source(),))


def test_execution_map_cannot_substitute_the_default_envelope_for_a_shape(tmp_path):
    def corrupt(row):
        shape = next(s for s in row["stages"][0]["execution_shapes"] if s["id"] == "single-node-8gpu")
        shape["resources"] = copy.deepcopy(row["stages"][0]["resources"])

    with pytest.raises(ScientificExecutionMapError, match="resources differ"):
        renderer(tmp_path, edit=corrupt)
