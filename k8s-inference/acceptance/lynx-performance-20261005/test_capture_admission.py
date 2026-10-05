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
