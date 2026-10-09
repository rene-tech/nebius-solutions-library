import copy

import pytest
from prepare_capacity import FLAVOR, POOL, QUEUE, reconcile


def fixture():
    flavor = {"name": FLAVOR, "resources": [
        {"name": "nvidia.com/gpu", "nominalQuota": "1", "borrowingLimit": "0"},
        {"name": "cpu", "nominalQuota": "15900m"},
        {"name": "memory", "nominalQuota": "190072Mi"},
    ]}
    other = {"name": "unrelated", "resources": [{"name": "nvidia.com/gpu", "nominalQuota": "4"}]}
    queue = {"metadata": {"name": QUEUE}, "spec": {"resourceGroups": [{"flavors": [flavor, other]}]}}
    live = copy.deepcopy(queue)
    for row, amount in zip(live["spec"]["resourceGroups"][0]["flavors"][0]["resources"],
                           (8, "127200m", "1520576Mi"), strict=True):
        row["nominalQuota"] = amount
    core = {"cpu_millicores": 15900, "memory_mib": 190072}
    contract = {
        "schema": "fs2-serve.nebius.ai/kueue-scheduling/v1",
        "pools": {POOL: {"resource_flavor": FLAVOR, "capacity": 1}, "unrelated": {"capacity": 4}},
        "accelerator_node_capacity": {POOL: {"accelerator_count": 1}},
        "cluster_queues": {QUEUE: queue}, "core_capacity": {POOL: copy.deepcopy(core)},
        "core_queue_quotas": {QUEUE: {POOL: copy.deepcopy(core)}},
        "core_shared_quota": {POOL: {"cpu_millicores": 0}}, "default_and_wan": {"unchanged": True},
    }
    return contract, live


def test_correction_is_exact_idempotent_and_does_not_modify_source_or_live_queue():
    original, live = fixture()
    before, live_before = copy.deepcopy(original), copy.deepcopy(live)
    arguments = dict(configured_max_nodes=8, configured_gpus_per_node=1)
    updated = reconcile(original, live, **arguments)
    expected = copy.deepcopy(original)
    expected["pools"][POOL]["capacity"] = 8
    for row, amount in zip(expected["cluster_queues"][QUEUE]["spec"]["resourceGroups"][0]["flavors"][0]["resources"],
                           (8, "127200m", "1520576Mi"), strict=True):
        row["nominalQuota"] = amount
    expected["core_capacity"][POOL] = {"cpu_millicores": 127200, "memory_mib": 1520576}
    expected["core_queue_quotas"][QUEUE][POOL] = copy.deepcopy(expected["core_capacity"][POOL])
    assert updated == expected
    assert reconcile(updated, live, **arguments) == updated
    assert original == before and live == live_before


@pytest.mark.parametrize("mutation", ["quota", "maximum", "node-size", "identity", "duplicate"])
def test_rejects_unreviewed_change_instead_of_inventing_capacity(mutation):
    contract, live = fixture()
    maximum, gpus = 8, 1
    if mutation == "quota":
        live["spec"]["resourceGroups"][0]["flavors"][0]["resources"][0]["nominalQuota"] = "16"
    elif mutation == "maximum":
        maximum = 16
    elif mutation == "node-size":
        gpus = 8
    elif mutation == "identity":
        contract["pools"][POOL]["resource_flavor"] = "other"
    else:
        live["spec"]["resourceGroups"][0]["flavors"].append(live["spec"]["resourceGroups"][0]["flavors"][0])
    with pytest.raises(ValueError):
        reconcile(contract, live, configured_max_nodes=maximum, configured_gpus_per_node=gpus)
