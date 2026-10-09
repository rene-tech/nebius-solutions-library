"""Declared gang feasibility is distinct from current nodes and GPU quota."""

import copy

import pytest
from test_gromacs_execution_shapes import ACCESS, mpi_request, renderer, resolver, source

from fs2_serve.scientific_batch.scheduling import SchedulingContractError, SchedulingContractResolver


def freeze(tmp_path, *, nodes, gpus, capacities, minimum_nodes=0, queue_gpu=1):
    runtime, profile = renderer(tmp_path)
    base = resolver(runtime)
    contract = copy.deepcopy(base.contract)
    # Try the small L40S pool first, as the current deployment does.
    contract["service_classes"]["customer-batch"]["pool_preference"] = [
        "l40s-4x", "h100-ondemand-1x", "h100-reserved-8x", "l40s-1x",
    ]
    for pool_id in contract["pools"]:
        contract["pools"][pool_id]["capacity"] = capacities.get(pool_id, 0)
        contract["pools"][pool_id]["min_nodes"] = minimum_nodes
    for flavor in contract["cluster_queues"]["inference"]["spec"]["resourceGroups"][0]["flavors"]:
        flavor["resources"] = [{"name": "nvidia.com/gpu", "nominalQuota": str(queue_gpu)}]
    policy = SchedulingContractResolver(
        contract, stage_resources=base.stage_resources, stage_shape_resources=base.stage_shape_resources,
    )
    plan = runtime.plan(profile, mpi_request(nodes, gpus), access_context=ACCESS, input_artifacts=(source(),))
    snapshot = policy.freeze(
        service_class="customer-batch", model_id="gromacs-mpi", tenant_id="tenant-a",
        profile=profile.value, plan=plan.controller_plan,
    )
    return snapshot.stages[0].resolved_pool_preference


@pytest.mark.parametrize("nodes,expected", [
    (1, ("l40s-4x", "h100-reserved-8x")),
    (2, ("h100-reserved-8x",)),
])
def test_one_four_gpu_host_fits_one_pod_but_not_two_distinct_hosts(tmp_path, nodes, expected):
    assert freeze(tmp_path, nodes=nodes, gpus=2, capacities={"l40s-4x": 4, "h100-reserved-8x": 16}) == expected


def test_node_maximum_uses_physical_gpus_per_host_not_requested_gpus(tmp_path):
    # Both pools have16GPUs and the request uses only8. The full-H100 pool
    # nevertheless has only2hosts, whereas L40S has4hosts, so only L40S fits.
    assert freeze(
        tmp_path, nodes=4, gpus=2, capacities={"l40s-4x": 16, "h100-reserved-8x": 16},
    ) == ("l40s-4x",)


@pytest.mark.parametrize("floor,queue_gpu", [(0, 0), (0, 2), (2, 1000)])
def test_scale_zero_and_queue_budget_do_not_rewrite_configured_maximum(tmp_path, floor, queue_gpu):
    # Static feasibility permits waiting for autoscaling/queue capacity. It
    # does not promise current free hosts, and quota never invents hosts.
    assert freeze(
        tmp_path, nodes=2, gpus=2, capacities={"l40s-4x": 8}, minimum_nodes=floor, queue_gpu=queue_gpu,
    ) == ("l40s-4x",)


def test_stale_single_gpu_pool_capacity_is_not_invented_from_larger_queue(tmp_path):
    assert freeze(
        tmp_path, nodes=2, gpus=1, capacities={"h100-ondemand-1x": 1, "h100-reserved-8x": 16}, queue_gpu=8,
    ) == ("h100-reserved-8x",)


def test_corrected_eight_single_gpu_hosts_allow_eight_host_gang(tmp_path):
    assert freeze(
        tmp_path, nodes=8, gpus=1, capacities={"h100-ondemand-1x": 8, "h100-reserved-8x": 16},
    ) == ("h100-ondemand-1x",)


def test_impossible_gang_is_rejected_before_operation_admission(tmp_path):
    with pytest.raises(SchedulingContractError, match="requires 2 distinct hosts"):
        freeze(tmp_path, nodes=2, gpus=2, capacities={"l40s-4x": 4})


@pytest.mark.parametrize("capacity", [True, -1, 4.0, "4", 5, None])
def test_unknown_or_inconsistent_maximum_is_not_guessed(tmp_path, capacity):
    with pytest.raises(SchedulingContractError, match="invalid configured maximum"):
        freeze(tmp_path, nodes=2, gpus=2, capacities={"l40s-4x": capacity, "h100-reserved-8x": 16})
