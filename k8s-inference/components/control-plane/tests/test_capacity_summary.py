from types import SimpleNamespace

from fs2_serve.admin_adapters import KubernetesCapacityConfig
from fs2_serve.capacity_summary import loaded_idle_gpu_count, project_pools


def node(name, *, ready=True, cordoned=False, pool="h100", resource="nvidia.com/gpu", gpus=8):
    return {
        "metadata": {
            "name": name,
            "labels": {"accelerator.fs2.nebius/pool-id": pool, "accelerator.fs2.nebius/class": "H100"},
        },
        "spec": {"unschedulable": cordoned},
        "status": {
            "capacity": {resource: str(gpus)},
            "allocatable": {resource: str(gpus)},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def pod(name, node_name, *, gpus=1, ready=True, model="qwen", phase="Running", deleting=False, job=False):
    return {
        "metadata": {
            "name": name,
            "labels": {"fs2.nebius.ai/model-id": model},
            "deletionTimestamp": "now" if deleting else None,
            "ownerReferences": [{"kind": "Job" if job else "ReplicaSet"}],
        },
        "spec": {"nodeName": node_name, "containers": [{"resources": {"requests": {"nvidia.com/gpu": str(gpus)}}}]},
        "status": {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def test_free_gpu_subtraction_is_node_local_and_excludes_unready_cordoned():
    nodes = [node("one"), node("two", ready=False), node("three", cordoned=True)]
    pods = [
        pod("worker", "one", gpus=2),
        pod("failed", "one", gpus=5, phase="Failed"),
        pod("deleting", "one", gpus=1, deleting=True),
    ]
    contract = SimpleNamespace(pool_id="h100", min_nodes=1, max_nodes=5)
    row = project_pools(nodes, pods, KubernetesCapacityConfig(), [contract])[0]
    assert row.total_gpus.value == 24 and row.allocated_gpus.value == 3
    assert row.schedulable_free_gpus.value == 5
    assert row.ready_nodes == 2 and row.cordoned_nodes == 1
    assert row.ready_workers.value == 1
    assert row.configured_additional_nodes.value == 2
    assert "not a provider" in row.configured_additional_nodes.reason


def test_partial_namespace_inventory_never_claims_free_or_idle():
    row = project_pools([node("one")], [], KubernetesCapacityConfig(), complete=False)[0]
    assert row.total_gpus.value == 8
    assert row.allocated_gpus.value is None and row.schedulable_free_gpus.value is None
    assert row.starting_workers.value is None


def test_zero_node_pool_retains_configured_headroom_without_available_supply_claim():
    contract = SimpleNamespace(
        pool_id="h200",
        min_nodes=0,
        max_nodes=3,
        accelerator_class="H200",
        capacity_type="preemptible",
        resource_name="nvidia.com/gpu",
    )
    row = project_pools([], [], KubernetesCapacityConfig(), [contract])[0]
    assert row.nodes == 0 and row.total_gpus.value == 0
    assert row.configured_additional_nodes.value == 3
    assert row.schedulable_free_gpus.value == 0


def test_heterogeneous_resource_units_are_not_summed_into_full_gpus():
    rows = project_pools(
        [node("h100"), node("mig", pool="mig-pool", resource="nvidia.com/mig-1g.10gb", gpus=7)],
        [],
        KubernetesCapacityConfig(),
    )
    assert {row.resource_name for row in rows} == {"nvidia.com/gpu", "nvidia.com/mig-1g.10gb"}


def test_loaded_idle_requires_ready_serving_without_current_model_work():
    pods = [
        pod("hot", "one", gpus=2),
        pod("busy", "one", model="active"),
        pod("starting", "one", ready=False),
        pod("job", "one", job=True),
        pod("drain", "one", deleting=True),
        pod("completed", "one", phase="Succeeded"),
    ]
    assert loaded_idle_gpu_count(pods, {"active"}) == 2
    assert loaded_idle_gpu_count(pods, {"active", "qwen"}) == 0
