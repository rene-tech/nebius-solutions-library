"""Additive full-node RDMA shape: durable identity, fit and joint quota gates."""

import copy
import json
from dataclasses import replace
from uuid import UUID

import pytest
import test_gromacs_execution_shapes as shapes

from fs2_serve.scientific_batch.codec import state_from_value, state_to_value
from fs2_serve.scientific_batch.execution import ScientificExecutionMapError
from fs2_serve.scientific_batch.models import ScientificBatchState, StageRdmaBinding, VerifiedInputManifest
from fs2_serve.scientific_batch.placement import execution_resource_envelope
from fs2_serve.scientific_batch.podset_envelope import PodSetEnvelopeError, compare_kueue_usage, envelope_from_manifest
from fs2_serve.scientific_batch.scheduling import SchedulingContractError, SchedulingContractResolver

RDMA = {"resource_name": "rdma.fs2.nebius/hca", "count": 1, "gpu_cluster_id": "computegpucluster-qualified"}


def rdma_runtime(tmp_path, monkeypatch, *, missing_map=False):
    profile = shapes.mpi_profile()
    stage = profile["workload"]["stages"][0]
    shape = copy.deepcopy(next(x for x in stage["execution_shapes"] if x["id"] == "multi-node-8gpu"))
    shape.update(id="multi-node-8gpu-rdma", rdma=RDMA)
    stage["execution_shapes"].append(shape)
    monkeypatch.setattr(shapes, "mpi_profile", lambda: copy.deepcopy(profile))

    def edit(model):
        entries = model["stages"][0]["execution_shapes"]
        selected = copy.deepcopy(next(x for x in entries if x["id"] == "multi-node-8gpu"))
        selected.update(id="multi-node-8gpu-rdma")
        if not missing_map:
            selected["rdma"] = RDMA
        entries.append(selected)

    runtime, loaded = shapes.renderer(tmp_path, edit=edit)
    plan = runtime.plan(
        loaded,
        shapes.mpi_request(2, 8),
        operation_id=UUID(shapes.OP),
        access_context=shapes.ACCESS,
        input_artifacts=(shapes.source(),),
    )
    contract = copy.deepcopy(shapes.resolver(runtime).contract)
    capacity = contract["accelerator_node_capacity"]["h100-reserved-8x"]
    capacity["extended_resources"] = {RDMA["resource_name"]: 1}
    capacity["node_labels"] = StageRdmaBinding.from_value(RDMA).node_labels
    group = contract["cluster_queues"]["inference"]["spec"]["resourceGroups"][0]
    group["coveredResources"] = ["nvidia.com/gpu", RDMA["resource_name"]]
    group["flavors"] = [
        {
            "name": "h100-reserved-8x-flavor",
            "resources": [
                {"name": "nvidia.com/gpu", "nominalQuota": 16},
                {"name": RDMA["resource_name"], "nominalQuota": 2},
            ],
        }
    ]
    return runtime, loaded, plan, contract


def freeze(runtime, profile, plan, contract):
    resolver = SchedulingContractResolver(
        contract,
        stage_shape_resources={
            key: execution_resource_envelope(value) for key, value in runtime.execution_shapes.items()
        },
    )
    return resolver.freeze(
        service_class="customer-batch",
        model_id="gromacs-mpi",
        tenant_id="tenant-a",
        profile=profile.value,
        plan=plan.controller_plan,
    )


def test_rdma_freezes_pod_resources_cluster_and_joint_kueue_count(tmp_path, monkeypatch):
    runtime, profile, plan, contract = rdma_runtime(tmp_path, monkeypatch)
    snapshot = freeze(runtime, profile, plan, contract)
    assert plan.controller_plan.stages[0].execution_shape.shape_id == "multi-node-8gpu-rdma"
    assert plan.stage_bindings[0].rdma.to_value() == RDMA
    resource = shapes.resource_for(plan, snapshot, 2)
    manifest = runtime.render(resource)
    pod = manifest["spec"]["replicatedJobs"][0]["template"]["spec"]["template"]["spec"]
    stage = pod["containers"][0]
    assert stage["resources"]["requests"][RDMA["resource_name"]] == "1"
    assert stage["resources"]["limits"]["nvidia.com/gpu"] == "8"
    assert pod["nodeSelector"]["topology.nebius.com/gpu-cluster-id"] == RDMA["gpu_cluster_id"]
    assert {x["name"]: x.get("value") for x in stage["env"]}["FS2_GROMACS_MPI_TRANSPORT"] == "ucx-rdma"
    assert not stage["securityContext"].get("privileged", False)
    assert stage["securityContext"]["allowPrivilegeEscalation"] is True
    assert stage["securityContext"]["capabilities"] == {"drop": ["ALL"], "add": ["IPC_LOCK"]}
    assert stage["securityContext"]["runAsUser"] == 10001
    for companion in pod["containers"][1:] + pod["initContainers"]:
        assert companion["securityContext"]["allowPrivilegeEscalation"] is False
        assert companion["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert not any("hostPath" in volume for volume in pod["volumes"])
    envelope = envelope_from_manifest(manifest, resource.kind)
    assert envelope.aggregate_requests.accelerator("nvidia.com/gpu") == 16
    assert envelope.aggregate_requests.accelerator(RDMA["resource_name"]) == 2
    admission = [
        {"name": envelope.names[0], "count": 2, "resourceUsage": {"nvidia.com/gpu": "16", RDMA["resource_name"]: "2"}}
    ]
    compare_kueue_usage(envelope, admission, accelerator_resource="nvidia.com/gpu")
    for amount in (None, "1", "3"):
        changed = copy.deepcopy(admission)
        if amount is None:
            changed[0]["resourceUsage"].pop(RDMA["resource_name"])
        else:
            changed[0]["resourceUsage"][RDMA["resource_name"]] = amount
        with pytest.raises(PodSetEnvelopeError):
            compare_kueue_usage(envelope, changed, accelerator_resource="nvidia.com/gpu")


@pytest.mark.parametrize("missing", ["extended_resources", "node_labels", "quota", "joint-group", "legacy"])
def test_no_hardware_or_quota_assumption_can_admit_rdma(tmp_path, monkeypatch, missing):
    runtime, profile, plan, contract = rdma_runtime(tmp_path, monkeypatch)
    if missing == "legacy":
        contract.pop("accelerator_node_capacity")
    elif missing in {"quota", "joint-group"}:
        group = contract["cluster_queues"]["inference"]["spec"]["resourceGroups"][0]
        if missing == "quota":
            group["flavors"][0]["resources"][1]["nominalQuota"] = 1
        else:
            group["coveredResources"] = ["nvidia.com/gpu"]
    else:
        contract["accelerator_node_capacity"]["h100-reserved-8x"].pop(missing)
    with pytest.raises(SchedulingContractError):
        freeze(runtime, profile, plan, contract)


def test_rdma_codec_preserves_identity_and_rejects_binding_drift(tmp_path, monkeypatch):
    runtime, profile, plan, contract = rdma_runtime(tmp_path, monkeypatch)
    snapshot = freeze(runtime, profile, plan, contract)
    artifact = UUID(shapes.request()["input_manifest"]["artifact_id"])
    state = ScientificBatchState.admit(
        operation_id=UUID(shapes.OP),
        tenant_id="tenant-a",
        plan=plan.controller_plan,
        scheduling=snapshot,
        model_id=plan.model_id,
        variant_id=plan.variant_id,
        input_artifact_id=artifact,
        execution_plan=plan,
        access_context=shapes.ACCESS,
        input_manifest=VerifiedInputManifest("input-manifest", artifact, "sha256:" + "a" * 64, (shapes.source(),)),
    )
    serialized = json.loads(json.dumps(state_to_value(state)))
    assert state_from_value(serialized) == state
    changed = copy.deepcopy(serialized)
    changed["adapter_execution"]["stage_bindings"][0].pop("rdma")
    with pytest.raises(ValueError, match="frozen execution shape"):
        state_from_value(changed)
    binding = plan.stage_bindings[0]
    with pytest.raises(ValueError, match="GPU-cluster"):
        replace(binding, required_node_labels=())
    changed_schedule = replace(snapshot, stages=(replace(snapshot.stages[0], node_selector=()),))
    with pytest.raises(ValueError, match="RDMA GPU-cluster"):
        replace(state, scheduling=changed_schedule)


def test_missing_execution_map_rdma_capability_fails_closed(tmp_path, monkeypatch):
    with pytest.raises(ScientificExecutionMapError, match="RDMA binding differs"):
        rdma_runtime(tmp_path, monkeypatch, missing_map=True)


@pytest.mark.parametrize(
    "field,value", [("count", True), ("count", 2), ("gpu_cluster_id", "other"), ("resource_name", "nvidia.com/gpu")]
)
def test_rdma_binding_rejects_unqualified_resource_or_identity(field, value):
    with pytest.raises(ValueError):
        StageRdmaBinding.from_value({**RDMA, field: value})
