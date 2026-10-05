import copy
import json

import prepare_rdma as prepare
import pytest


def fixtures():
    queue = {
        "metadata": {"name": prepare.QUEUE, "uid": "exact-queue"},
        "spec": {
            "resourceGroups": [
                {
                    "coveredResources": ["nvidia.com/gpu", "cpu", "memory"],
                    "flavors": [
                        {
                            "name": "full",
                            "resources": [
                                {"name": "nvidia.com/gpu", "nominalQuota": "16"},
                                {"name": "cpu", "nominalQuota": "256"},
                            ],
                        },
                        {
                            "name": "other",
                            "resources": [
                                {"name": "nvidia.com/gpu", "nominalQuota": "4"}
                            ],
                        },
                    ],
                }
            ]
        },
    }
    scheduling = {
        "cluster_queues": {prepare.QUEUE: copy.deepcopy(queue)},
        "pools": {
            prepare.POOL: {
                "capacity": 16,
                "accelerator_resource_name": "nvidia.com/gpu",
                "resource_flavor": "full",
            }
        },
        "accelerator_node_capacity": {
            prepare.POOL: {"accelerator_count": 8, "cpu_millicores": 127900}
        },
    }
    admin = {
        "models": {"untouched": {"value": "original"}},
        "pools": {
            prepare.POOL: {
                "accelerators_per_node": 8,
                "node_selector": {
                    "accelerator.fs2.nebius/pool-id": prepare.POOL,
                    "topology.fs2.nebius/scope": "standalone",
                },
            },
            "other": {"unchanged": True},
        },
    }
    envelope = {
        "pools": {
            prepare.POOL: {
                "acceleratorsPerNode": 8,
                "nodeSelector": {
                    "accelerator.fs2.nebius/pool-id": prepare.POOL,
                    "topology.fs2.nebius/scope": "standalone",
                },
            },
            "other": {"unchanged": True},
        }
    }
    nodes = [
        {
            "metadata": {
                "name": name,
                "labels": {
                    "accelerator.fs2.nebius/pool-id": prepare.POOL,
                    "topology.fs2.nebius/scope": "gpu_cluster",
                    "topology.nebius.com/gpu-cluster-id": prepare.CLUSTER,
                },
            },
            "status": {
                "allocatable": {"nvidia.com/gpu": "8", prepare.RESOURCE: "1"},
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }
        for name in prepare.NODES
    ]
    return scheduling, queue, admin, envelope, nodes


def test_scoped_quota_candidates_preserve_all_old_gpu_and_core_values():
    source = fixtures()
    before = copy.deepcopy(source)
    value = prepare.fabric_candidates(*source)
    assert source == before
    group = value["queue"]["spec"]["resourceGroups"][0]
    assert group["coveredResources"] == [
        "nvidia.com/gpu",
        "cpu",
        "memory",
        prepare.RESOURCE,
    ]
    assert [flavor["resources"][-1]["nominalQuota"] for flavor in group["flavors"]] == [
        "2",
        "0",
    ]
    for old, new in zip(
        source[1]["spec"]["resourceGroups"][0]["flavors"], group["flavors"], strict=True
    ):
        assert old["resources"] == new["resources"][:-1]
    assert value["admin"]["models"] == source[2]["models"]
    assert value["admin"]["pools"]["other"] == source[2]["pools"]["other"]
    assert value["envelope"]["pools"]["other"] == source[3]["pools"]["other"]
    assert value["queue_patch"][:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "exact-queue"},
        {"op": "test", "path": "/spec", "value": source[1]["spec"]},
    ]
    assert value["queue_inverse"][-1]["value"] == source[1]["spec"]


@pytest.mark.parametrize(
    "drift", ["cluster", "device", "queue", "gpu-quota", "node-not-ready"]
)
def test_candidate_stops_on_hardware_or_admission_drift(drift):
    values = fixtures()
    scheduling, queue, _, _, nodes = values
    if drift == "cluster":
        nodes[0]["metadata"]["labels"]["topology.nebius.com/gpu-cluster-id"] = (
            "computegpucluster-other"
        )
    elif drift == "device":
        del nodes[0]["status"]["allocatable"][prepare.RESOURCE]
    elif drift == "queue":
        queue["spec"]["unreviewed"] = True
    elif drift == "gpu-quota":
        scheduling["pools"][prepare.POOL]["capacity"] = 32
    else:
        nodes[0]["status"]["conditions"][0]["status"] = "False"
    with pytest.raises(ValueError):
        prepare.fabric_candidates(*values)


def test_shape_draft_does_not_invent_qualification_or_mutate_legacy_catalog():
    contracts = prepare.HERE.parents[1] / "catalog/runtime/contracts"
    profiles = json.loads((contracts / "scientific-workload-profiles.json").read_text())
    execution = json.loads((contracts / "scientific-execution-map.json").read_text())
    before = copy.deepcopy((profiles, execution))
    value = prepare.shape_fragments(profiles, execution)
    assert (profiles, execution) == before
    assert value["bound"] is False and value["qualification"] is None
    assert value["profile_shape"]["placement"]["accelerator"]["count"] == 8
    assert value["profile_shape"]["rdma"]["count"] == 1
    assert value["execution_shape"]["rdma"] == value["profile_shape"]["rdma"]


def test_exact_reviewed_drift_is_preserved_not_reconciled():
    values = fixtures()
    scheduling, queue, _, _, _ = values
    queue["spec"]["resourceGroups"][0]["flavors"].append(
        {
            "name": "wan",
            "resources": [
                {"name": "nvidia.com/gpu", "nominalQuota": "2", "borrowingLimit": "0"}
            ],
        }
    )
    before = copy.deepcopy(queue)
    sha = prepare.activation.sha(
        prepare.activation.canonical(
            {
                "contract_spec": scheduling["cluster_queues"][prepare.QUEUE]["spec"],
                "live_spec": queue["spec"],
            }
        )
    )
    with pytest.raises(ValueError, match="reviewed drift"):
        prepare.fabric_candidates(*values, expected_queue_drift_sha256="0" * 64)
    result = prepare.fabric_candidates(*values, expected_queue_drift_sha256=sha)
    assert result["preserved_queue_drift_sha256"] == sha
    assert (
        result["queue"]["spec"]["resourceGroups"][0]["flavors"][-1]["resources"][:-1]
        == before["spec"]["resourceGroups"][0]["flavors"][-1]["resources"]
    )
    assert (
        len(
            result["scheduling"]["cluster_queues"][prepare.QUEUE]["spec"][
                "resourceGroups"
            ][0]["flavors"]
        )
        == 2
    )
