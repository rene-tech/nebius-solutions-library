"""Register only an exact, already-running pool with Kueue; create no nodes."""

import argparse
import copy
import json
import subprocess
from pathlib import Path

from remove_hot_floors import kubectl


def main(args):
    declaration = json.loads(args.declaration.read_text())
    if args.context != declaration["context"]:
        raise ValueError("pool receipt belongs to another cluster")
    pool = declaration["envelope"]
    nodes = json.loads(kubectl(args.context, "get", "nodes", *declaration["nodes"], "-o", "json"))
    for node in nodes["items"]:
        labels, available = node["metadata"]["labels"], node["status"]["allocatable"]
        if labels["nebius.com/node-group-id"] != declaration["node_group_id"]:
            raise ValueError("unexpected provider node group")
        if any(labels.get(k) != v for k, v in pool["nodeSelector"].items()):
            raise ValueError("live node differs from declared pool selector")
        if available["nvidia.com/gpu"] != "1" or available["cpu"] != "15900m":
            raise ValueError("unexpected measured node resources")
        if int(available["memory"].removesuffix("Ki")) * 1024 < pool["allocatableMemoryBytes"]:
            raise ValueError("declared memory exceeds live allocatable")
    if len(nodes["items"]) != pool["maxNodes"] or pool["maxNodes"] != pool["minNodes"]:
        raise ValueError("registration must equal existing fixed node count")
    before = json.loads(kubectl(args.context, "get", "clusterqueue", declaration["cluster_queue"], "-o", "json"))
    groups = before["spec"]["resourceGroups"]
    index = next(i for i, group in enumerate(groups)
                 if set(group["coveredResources"]) == {"nvidia.com/gpu", "cpu", "memory"})
    if any(f["name"] == declaration["flavor_quota"]["name"] for group in groups for f in group["flavors"]):
        raise ValueError("pool already registered; inspect before retry")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = json.loads(kubectl(args.context, "get", "workloads.kueue.x-k8s.io", "-A", "-o", "json"))
    receipt = {"nodes": nodes, "cluster_queue_before": before,
               "workloads_before": [{"namespace": w["metadata"]["namespace"], "name": w["metadata"]["name"],
                                      "uid": w["metadata"]["uid"], "status": w.get("status", {})}
                                     for w in work["items"]]}
    (args.output / "before.json").write_text(json.dumps(receipt, indent=2) + "\n")
    flavor = args.output / "resource-flavor.json"
    flavor.write_text(json.dumps(declaration["resource_flavor"], indent=2) + "\n")
    command = ["kubectl", "--context", args.context, "apply", "--server-side",
               "--field-manager=fs2-idle-registration", "-f", str(flavor)]
    subprocess.run(command + ["--dry-run=server"], check=True)
    if not args.apply:
        print(json.dumps({"preview_only": True, "existing_nodes": len(nodes["items"]),
                          "unrelated_quota_changes": 0, "new_provider_resources": 0}))
        return
    subprocess.run(command, check=True)
    patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": before["metadata"]["resourceVersion"]},
             {"op": "add", "path": f"/spec/resourceGroups/{index}/flavors/-", "value": declaration["flavor_quota"]}]
    kubectl(args.context, "patch", "clusterqueue", declaration["cluster_queue"], "--type=json", "-p", json.dumps(patch))
    after = json.loads(kubectl(args.context, "get", "clusterqueue", declaration["cluster_queue"], "-o", "json"))
    expected = copy.deepcopy(before["spec"])
    expected["resourceGroups"][index]["flavors"].append(declaration["flavor_quota"])
    if expected != after["spec"]:
        raise RuntimeError("unrelated Kueue policy changed")
    result = {"pool_id": pool["poolId"], "existing_nodes": declaration["nodes"],
              "new_provider_resources": 0, "unrelated_quota_changes": 0,
              "patch": patch, "after": after}
    (args.output / "applied.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in {"patch", "after"}}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    main(parser.parse_args())
