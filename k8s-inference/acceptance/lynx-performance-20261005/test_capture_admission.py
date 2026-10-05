import pytest
from capture_admission import allocation, resources


def test_only_fixed_nonsecret_shape_transport_environment_is_retained():
    result = resources([{"name": "runtime", "env": [
        {"name": "FS2_GROMACS_MPI_TRANSPORT", "value": "tcp-host-staged"},
        {"name": "FS2_GROMACS_MPI_TOTAL_RANKS", "value": "16"},
        {"name": "SCIENTIFIC_MODELS_API_KEY", "value": "never-retain"},
        {"name": "FS2_MPI_SSH_KEY", "value": "never-retain"}]}])
    assert result[0]["shape_environment"] == {"FS2_GROMACS_MPI_TRANSPORT": "tcp-host-staged",
                                              "FS2_GROMACS_MPI_TOTAL_RANKS": "16"}


@pytest.mark.parametrize("count,gpus", [(1, 1), (1, 4), (1, 8), (2, 8)])
def test_frozen_shape_matches_actual_pods_and_kueue(count, gpus):
    plan = {"tenant_id": "system", "model_id": "gromacs-mpi", "operation_id": "test",
            "plan": {"stages": [{"mode": "gang-jobset" if count > 1 else "independent-jobs",
                                  "gang_size": count if count > 1 else None,
                                  "execution_shape": {"accelerator_count": gpus}}]}}
    containers = [{"name": "scientific-stage", "resources": {"requests": {"nvidia.com/gpu": str(gpus)}}}]
    pods = [{"metadata": {"name": str(i), "uid": str(i), "labels": {
        "fs2.nebius.ai/operation-id": "test", "fs2.nebius.ai/tenant-id": "system"}},
        "spec": {"containers": containers}, "status": {"phase": "Running"}} for i in range(count)]
    workload = {"metadata": {"name": "w", "uid": "w"}, "spec": {"podSets": [
        {"name": "main", "count": count, "template": {"spec": {"containers": containers}}}]},
        "status": {"admission": {"podSetAssignments": [{"resourceUsage": {"nvidia.com/gpu": str(count * gpus)}}]}}}
    result = allocation(plan, pods, [workload])
    assert result["expected_total_gpus"] == count * gpus
    assert result["pods_match_frozen_shape"] and result["kueue_matches_frozen_shape"]
    workload["status"] = {}
    assert not allocation(plan, pods, [workload])["kueue_matches_frozen_shape"]
    assert not allocation(plan, [], [workload])["pods_match_frozen_shape"]
    pods[0]["metadata"]["labels"]["fs2.nebius.ai/tenant-id"] = "other"
    with pytest.raises(ValueError, match="unrelated"):
        allocation(plan, pods, [])


def test_rdma_requires_actual_joint_quota_nodes_and_exact_pod_bundles():
    resource = "rdma.fs2.nebius/hca"
    binding = {"resource_name": resource, "count": 1, "gpu_cluster_id": "computegpucluster-exact"}
    labels = {"topology.fs2.nebius/scope": "gpu_cluster",
              "topology.nebius.com/gpu-cluster-id": binding["gpu_cluster_id"]}
    claims = {"nvidia.com/gpu": "8", resource: "1"}
    containers = [{"name": "scientific-stage", "resources": {"requests": claims, "limits": claims}}]
    plan = {"tenant_id": "system", "model_id": "gromacs-mpi", "operation_id": "test",
            "plan": {"stages": [{"mode": "gang-jobset", "gang_size": 2,
                                  "execution_shape": {"accelerator_count": 8, "rdma": binding}}]}}
    pods = [{"metadata": {"name": str(i), "uid": str(i), "labels": {
        "fs2.nebius.ai/operation-id": "test", "fs2.nebius.ai/tenant-id": "system"}},
        "spec": {"containers": containers, "nodeName": f"node{i}", "nodeSelector": dict(labels)},
        "status": {"phase": "Running"}} for i in range(2)]
    nodes = [{"metadata": {"name": f"node{i}", "labels": dict(labels)},
              "status": {"allocatable": dict(claims)}} for i in range(2)]
    assignment = {"resourceUsage": {"nvidia.com/gpu": "16", resource: "2"},
                  "flavors": {"nvidia.com/gpu": "reserved-h100", resource: "reserved-h100"}}
    workload = {"metadata": {"name": "w", "uid": "w"}, "spec": {"podSets": [
        {"name": "main", "count": 2, "template": {"spec": {"containers": containers}}}]},
        "status": {"admission": {"podSetAssignments": [assignment]}}}
    result = allocation(plan, pods, [workload], nodes=nodes)
    assert result["pods_match_frozen_shape"] and result["kueue_matches_frozen_shape"]
    assert result["rdma"]["pods_match_frozen_binding"]
    assert result["rdma"]["nodes_match_frozen_binding"]
    assert result["rdma"]["kueue_matches_frozen_binding"]
    workload["spec"]["podSets"][0]["count"] = 1
    assert not allocation(plan, pods, [workload], nodes=nodes)["rdma"]["kueue_matches_frozen_binding"]
    workload["spec"]["podSets"][0]["count"] = 2
    assignment["flavors"][resource] = "different-pool"
    assert not allocation(plan, pods, [workload], nodes=nodes)["rdma"]["kueue_matches_frozen_binding"]
    assignment["flavors"][resource] = "reserved-h100"
    assignment["resourceUsage"][resource] = "1"
    assert not allocation(plan, pods, [workload], nodes=nodes)["rdma"]["kueue_matches_frozen_binding"]
    nodes[1]["metadata"]["labels"]["topology.nebius.com/gpu-cluster-id"] = "wrong"
    assert not allocation(plan, pods, [workload], nodes=nodes)["rdma"]["nodes_match_frozen_binding"]
    pods[1]["spec"]["nodeName"] = "node0"
    assert not allocation(plan, pods, [workload], nodes=nodes)["rdma"]["pods_match_frozen_binding"]
