import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "admission_plan", Path(__file__).with_name("plan.py")
)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


@pytest.fixture
def sample():
    settings = json.loads(
        (ROOT / "examples/scheduling-lynx-whitelab.tfvars.json").read_text()
    )["deployment"]["scheduling"]
    pool_ids = list(reversed(settings["default_queue_pool_order"]))
    pools = {
        p: {
            "resource_flavor": "inference-" + p,
            "capacity": 4,
            "accelerator_resource_name": "nvidia.com/gpu",
        }
        for p in pool_ids
    }
    groups = [
        {
            "coveredResources": list(settings["fair_share_resource_weights"]),
            "flavors": [
                {
                    "name": "inference-" + p,
                    "resources": [{"name": "nvidia.com/gpu", "nominalQuota": "4"}],
                }
                for p in pool_ids + ["unrelated-h200"]
            ],
        }
    ]
    queue = {
        "metadata": {"name": "inference-accelerators"},
        "spec": {
            "resourceGroups": groups,
            "preemption": {"withinClusterQueue": "LowerPriority"},
            "admissionScope": {"admissionMode": "UsageBasedAdmissionFairSharing"},
        },
    }
    route = {
        "cluster_queue": "inference-accelerators",
        "namespace": "fs2-models",
        "tenant_ids": [],
        "model_ids": [],
        "service_classes": [],
    }
    contract = {
        "pools": pools,
        "pool_capacity": {"stale": 1},
        "cluster_queues": {"inference-accelerators": copy.deepcopy(queue)},
        "cluster_queue_namespaces": {
            "inference-accelerators": ["fs2-models", "fs2-academic-poc"]
        },
        "cluster_queue_pool_order": {"inference-accelerators": pool_ids},
        "local_queue_routes": {"inference-models": route},
        "local_queues": {},
        "namespace_bound_models": {"bindcraft": "fs2-academic-poc"},
        "service_classes": {
            "customer-batch": {
                "default_local_queue": "inference-models",
                "pool_preference": pool_ids,
            }
        },
        "model_eligible_pool_ids": {
            "scvi-scanvi": ["h100-reserved-8x"],
            "gromacs": pool_ids,
        },
        "cpu_classes": {"general-cpu": {"namespace": "fs2-models"}},
        "accelerator_node_capacity": {"h100-reserved-8x": {"memory_mib": 1572748}},
    }
    config = {
        "admissionFairSharing": {"usageHalfLifeTime": "168h"},
        "waitForPodsReady": {"timeout": "2h"},
    }
    return contract, queue, config, settings


def test_preserves_resource_quota_and_unrelated_gpu(sample):
    contract, queue, config, settings = sample
    original = copy.deepcopy(sample)
    result, cq, cfg, lanes, cm, digest = planner.build(*sample)
    assert sample == original  # planning is pure
    old = {f["name"]: f for f in queue["spec"]["resourceGroups"][0]["flavors"]}
    new = {f["name"]: f for f in cq["spec"]["resourceGroups"][0]["flavors"]}
    assert old == new
    assert cq["spec"]["preemption"] == queue["spec"]["preemption"]
    assert (
        cq["spec"]["resourceGroups"][0]["flavors"][-1]["name"]
        == "inference-unrelated-h200"
    )
    for key in (
        "pools",
        "model_eligible_pool_ids",
        "accelerator_node_capacity",
        "cpu_classes",
        "namespace_bound_models",
    ):
        assert result[key] == contract[key]
    assert cfg["waitForPodsReady"] == config["waitForPodsReady"]
    assert cfg["admissionFairSharing"]["resourceWeights"]["memory"] == 0
    assert result["local_queue_routes"]["lynx-md"]["tenant_ids"] == ["lynx"]
    assert result["local_queue_routes"]["whitelab-single-cell"]["tenant_ids"] == [
        "whitelab"
    ]
    assert len(lanes) == 2
    assert cm["immutable"] is True
    assert (
        planner.hashlib.sha256(cm["data"]["kueue-scheduling.json"].encode()).hexdigest()
        == digest
    )
    assert (
        result["service_classes"]["customer-batch"]["pool_preference"]
        == settings["default_queue_pool_order"]
    )


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_pool",
        "missing_pool",
        "missing_memory_weight",
        "missing_rdma_weight",
        "different_cq",
        "wrong_namespace",
        "namespace_bound_model",
        "zero_weight",
        "missing_ack",
        "wildcard_tenant",
        "conflicting_route",
        "queue_rebinding",
    ],
)
def test_refuses_ambiguous_or_unscoped_updates(sample, change):
    contract, queue, config, settings = sample
    lane = settings["local_queues"]["lynx-md"]
    if change == "duplicate_pool":
        settings["default_queue_pool_order"].append("l40s-1x")
    if change == "missing_pool":
        settings["default_queue_pool_order"].pop()
    if change == "missing_memory_weight":
        del settings["fair_share_resource_weights"]["memory"]
    if change == "missing_rdma_weight":
        del settings["fair_share_resource_weights"]["rdma.fs2.nebius/hca"]
    if change == "different_cq":
        lane["cluster_queue"] = "another"
    if change == "wrong_namespace":
        lane["namespace"] = "customer-owned"
    if change == "namespace_bound_model":
        lane["model_ids"].append("bindcraft")
    if change == "zero_weight":
        lane["fair_sharing_weight"] = 0
    if change == "missing_ack":
        settings["fair_share_precedence_acknowledged"] = False
    if change == "wildcard_tenant":
        lane["tenant_ids"] = []
    if change == "conflicting_route":
        contract["local_queue_routes"]["collision"] = copy.deepcopy(lane)
    if change == "queue_rebinding":
        contract["local_queue_routes"]["lynx-md"] = {"cluster_queue": "different"}
    with pytest.raises(ValueError):
        planner.build(*sample)


def test_root_fair_share_weights_include_the_managed_rdma_budget():
    source = (ROOT / "locals.tf").read_text()
    budget = source.split("root_budgeted_resource_names =", 1)[1].split(")))", 1)[0]
    assert 'length(var.managed_rdma_pools) > 0 ? ["rdma.fs2.nebius/hca"] : []' in budget
