"""Pure migration invariants; live API/server validation is a separate gate."""

import copy
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location("activate", Path(__file__).with_name("activate.py"))
activate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activate)


def fixtures():
    quota = {"name": "old", "resources": [{"name": "nvidia.com/gpu", "nominalQuota": 8}]}
    contract = {key: {} for key in ("pools", "pool_capacity", "resource_flavor_pool_ids",
        "accelerator_node_capacity", "core_capacity", "shared_pool_quota", "core_shared_quota")}
    contract.update(core_queue_quotas={activate.QUEUE: {}},
        model_eligible_pool_ids={"gromacs": ["old"], "namd": ["old"]},
        cluster_queue_pool_order={activate.QUEUE: ["old"]},
        service_classes={"customer-batch": {"pool_preference": ["old"]}},
        cluster_queues={activate.QUEUE: {"spec": {"resourceGroups": [{"flavors": [quota]}]}}},
        cohort={"spec": {"resourceGroups": [{"flavors": []}]}})
    node = {"metadata": {"labels": {activate.LABEL: activate.POOL,
        "accelerator.fs2.nebius/class": "nvidia-l40s-48gb", "kubernetes.io/arch": "amd64"}},
        "spec": {}, "status": {"allocatable": {"cpu": "127900m", "memory": "769159796Ki",
        "ephemeral-storage": "297467776530", "nvidia.com/gpu": "4"},
        "conditions": [{"type": "Ready", "status": "True"}]}}
    return contract, node


def test_adds_only_gromacs_eligibility_preserving_every_old_quota():
    contract, node = fixtures()
    before = copy.deepcopy(contract)
    with patch.object(activate, "SchedulingContractResolver") as validate:
        result, quota, _ = activate.extend(contract, node)
    assert contract == before
    assert result["model_eligible_pool_ids"]["namd"] == ["old"]
    assert result["model_eligible_pool_ids"]["gromacs"] == ["old", "l40s-4x"]
    assert result["cluster_queues"][activate.QUEUE]["spec"]["resourceGroups"][0]["flavors"][1:] == (
        before["cluster_queues"][activate.QUEUE]["spec"]["resourceGroups"][0]["flavors"])
    assert quota["resources"][0]["nominalQuota"] == 4
    assert result["accelerator_node_capacity"]["l40s-4x"]["memory_mib"] == 751132
    validate.assert_called_once_with(result)


@pytest.mark.parametrize("change", ["unready", "cordoned", "wrong-gpu", "already-enabled"])
def test_rejects_changed_node_or_existing_policy(change):
    contract, node = fixtures()
    if change == "unready":
        node["status"]["conditions"][0]["status"] = "False"
    elif change == "cordoned":
        node["spec"]["unschedulable"] = True
    elif change == "wrong-gpu":
        node["metadata"]["labels"]["accelerator.fs2.nebius/class"] = "nvidia-h100-sxm5-80gb"
    else:
        contract["pools"]["l40s-4x"] = {}
    with pytest.raises(ValueError):
        activate.extend(contract, node)
