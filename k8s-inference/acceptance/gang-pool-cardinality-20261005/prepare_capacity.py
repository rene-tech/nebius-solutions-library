"""Review-only correction of one proven existing pool; no provider/Kueue writes.

The parent separately verified the authoritative Terraform and provider group
both declare eight existing 1-GPU hosts. Live queue values are independent input;
they do not establish or increase a pool's configured maximum.
"""

from __future__ import annotations

import copy

from fs2_serve.scientific_batch.podset_envelope import parse_bytes, parse_cpu_millis

POOL = "h100-ondemand-1x"
FLAVOR = "inference-h100-ondemand-1x"
QUEUE = "inference-accelerators"


def reconcile(contract, live_queue, *, configured_max_nodes, configured_gpus_per_node):
    if (type(configured_max_nodes) is not int or configured_max_nodes != 8
            or type(configured_gpus_per_node) is not int or configured_gpus_per_node != 1):
        raise ValueError("the reviewed existing pool proof is exactly eight 1-GPU hosts")
    if (contract["schema"] != "fs2-serve.nebius.ai/kueue-scheduling/v1"
            or contract["pools"][POOL]["resource_flavor"] != FLAVOR
            or type(contract["pools"][POOL]["capacity"]) is not int
            or contract["pools"][POOL]["capacity"] not in {1, 8}
            or contract["accelerator_node_capacity"][POOL]["accelerator_count"] != 1
            or live_queue["metadata"]["name"] != QUEUE):
        raise ValueError("current identities or capacity differ from the reviewed correction")

    flavors = [
        flavor for group in live_queue["spec"]["resourceGroups"] for flavor in group["flavors"]
        if flavor["name"] == FLAVOR
    ]
    if len(flavors) != 1:
        raise ValueError("exact existing live flavor must occur once")
    live = {resource["name"]: resource["nominalQuota"] for resource in flavors[0]["resources"]}
    if (str(live["nvidia.com/gpu"]) != "8" or parse_cpu_millis(live["cpu"]) != 127200
            or parse_bytes(live["memory"], label="existing live memory") != 1520576 * 1024**2):
        raise ValueError("live quota differs from the independently reviewed existing values")

    updated = copy.deepcopy(contract)
    updated["pools"][POOL]["capacity"] = configured_max_nodes * configured_gpus_per_node
    embedded = [
        flavor for group in updated["cluster_queues"][QUEUE]["spec"]["resourceGroups"]
        for flavor in group["flavors"] if flavor["name"] == FLAVOR
    ]
    if len(embedded) != 1:
        raise ValueError("exact embedded flavor must occur once")
    amounts = {row["name"]: row for row in embedded[0]["resources"]}
    for resource in ("nvidia.com/gpu", "cpu", "memory"):
        amounts[resource]["nominalQuota"] = live[resource]
    core = {"cpu_millicores": 127200, "memory_mib": 1520576}
    updated["core_capacity"][POOL].update(core)
    updated["core_queue_quotas"][QUEUE][POOL].update(core)
    return updated
