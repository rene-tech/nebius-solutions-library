"""Read-only post-rollout preservation checks; no credentials in the receipt."""

import argparse
import copy
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from activate import CONTEXT, DEPLOYMENT, FLAVOR, POOL, QUEUE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("activation", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    def read(name):
        return json.loads((args.activation / name).read_text())
    identity = read("identity.json")
    assert identity["context"] == CONTEXT

    def get(kind, name, namespace=None):
        scope = [] if namespace is None else ["-n", namespace]
        return json.loads(subprocess.check_output(
            ["kubectl", "--context", CONTEXT, "--request-timeout=30s", *scope, "get", kind, name, "-o", "json"]))

    before = read("deployment.before.json")
    live = get("deployment", DEPLOYMENT, "fs2-system")
    expected = copy.deepcopy(before["spec"])
    spec = expected["template"]["spec"]
    for volume in spec["volumes"]:
        if volume["name"] == "scientific-batch-scheduling":
            volume["configMap"]["name"] = identity["new_configmap"]
    for container in spec["containers"]:
        for env in container.get("env", []):
            if env["name"] == "FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256":
                env["value"] = identity["new_sha256"]
    assert live["spec"] == expected, "Unrelated Deployment spec changed"
    assert live["status"]["readyReplicas"] == live["status"]["updatedReplicas"] == expected["replicas"] == 3
    expected_cm = read("configmap.json")
    cm = get("configmap", identity["new_configmap"], "fs2-system")
    assert cm["data"] == expected_cm["data"] and cm["immutable"] is True
    cq = get("clusterqueue", QUEUE)
    expected_cq = copy.deepcopy(read("clusterqueue.before.json")["spec"])
    expected_cq["resourceGroups"][0]["flavors"].insert(0, read("clusterqueue.patch.json")[-1]["value"])
    assert cq["spec"] == expected_cq
    assert any(c["type"] == "Active" and c["status"] == "True" for c in cq["status"]["conditions"])
    cohort = get("cohort", "inference-shared")
    expected_cohort = copy.deepcopy(read("cohort.before.json")["spec"])
    expected_cohort["resourceGroups"][0]["flavors"].append(read("cohort.patch.json")[-1]["value"])
    assert cohort["spec"] == expected_cohort
    assert get("resourceflavor", FLAVOR)["spec"] == read("flavor.json")["spec"]
    receipt = {"captured_at": datetime.now(timezone.utc).isoformat(), **identity,
        "activation_source_commit": "e85e00674eff99f3183cf61c298b29b4efdd32ca",
        "api_replicas_ready": 3, "unrelated_deployment_spec_unchanged": True,
        "all_prior_queue_quotas_unchanged": True, "clusterqueue_active": True,
        "immutable_contract_verified": True, "pool": POOL,
        "resource_flavor": FLAVOR, "cloud_quota_or_node_limit_changed": False}
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
