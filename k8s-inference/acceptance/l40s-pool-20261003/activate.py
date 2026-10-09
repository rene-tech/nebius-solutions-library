"""Enable the existing four-L40S pool without replaying a stale Helm release.

Prepare saves an exact, reviewable additive change and rollback. Apply refuses
changed baselines. It changes only Kueue flavor membership and the scientific
scheduling contract; images, execution maps, customers and cloud bounds stay put.
Terraform's matching operator inputs are documented alongside this migration.
"""

import argparse
import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

from fs2_serve.scientific_batch.scheduling import SchedulingContractResolver

POOL = "l40s-4x"
FLAVOR = "inference-l40s-4x"
QUEUE = "inference-accelerators"
LABEL = "accelerator.fs2.nebius/pool-id"
DEPLOYMENT = "fs2-serve-control-plane"
CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def extend(contract, node):
    """Pure additive renderer. Other models and all old quotas stay identical."""
    result = copy.deepcopy(contract)
    if POOL in result["pools"]:
        raise ValueError("Pool already registered; inspect instead of overwriting")
    labels = node["metadata"]["labels"]
    if (labels[LABEL], labels["accelerator.fs2.nebius/class"], labels["kubernetes.io/arch"]) != (
        POOL, "nvidia-l40s-48gb", "amd64"
    ):
        raise ValueError("Unexpected accelerator identity")
    allocatable = node["status"]["allocatable"]
    if allocatable["nvidia.com/gpu"] != "4" or node["spec"].get("unschedulable"):
        raise ValueError("Expected the existing schedulable four-GPU node")
    if not any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"]):
        raise ValueError("Node must be Ready")
    cpu, memory = allocatable["cpu"], allocatable["memory"]
    if not cpu.endswith("m") or not memory.endswith("Ki"):
        raise ValueError("Unreviewed allocatable quantity format")
    capacity = {"cpu_millicores": int(cpu[:-1]), "memory_mib": int(memory[:-2]) // 1024}
    result["pools"][POOL] = {"accelerator_resource_name": "nvidia.com/gpu", "capacity": 4,
                              "resource_flavor": FLAVOR}
    result["pool_capacity"][POOL] = 4
    result["resource_flavor_pool_ids"][FLAVOR] = POOL
    result["accelerator_node_capacity"][POOL] = {**capacity, "accelerator_count": 4,
        "ephemeral_storage_mib": int(allocatable["ephemeral-storage"]) // 1024**2}
    result["core_capacity"][POOL] = capacity
    result["core_queue_quotas"][QUEUE][POOL] = capacity
    result["shared_pool_quota"][POOL] = 0
    result["core_shared_quota"][POOL] = {"cpu_millicores": 0, "memory_mib": 0}
    result["model_eligible_pool_ids"]["gromacs"].append(POOL)
    result["cluster_queue_pool_order"][QUEUE].insert(0, POOL)
    for policy in result["service_classes"].values():
        policy["pool_preference"].insert(0, POOL)
    quota = {"name": FLAVOR, "resources": [
        {"name": "nvidia.com/gpu", "nominalQuota": 4},
        {"name": "cpu", "nominalQuota": f'{capacity["cpu_millicores"]}m'},
        {"name": "memory", "nominalQuota": f'{capacity["memory_mib"]}Mi'}]}
    result["cluster_queues"][QUEUE]["spec"]["resourceGroups"][0]["flavors"].insert(0, quota)
    zero = {"name": FLAVOR, "resources": [{"name": r["name"], "nominalQuota": "0"}
                                            for r in quota["resources"]]}
    result["cohort"]["spec"]["resourceGroups"][0]["flavors"].append(zero)
    SchedulingContractResolver(result)
    return result, quota, zero


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", default=CONTEXT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    kube = ["kubectl", "--context", args.context, "--request-timeout=30s"]

    def run(*argv, value=None):
        return subprocess.check_output(kube + list(argv), input=None if value is None else canonical(value).encode())

    def get(kind, name, namespace=None):
        scope = [] if namespace is None else ["-n", namespace]
        return json.loads(run(*scope, "get", kind, name, "-o", "json"))

    def save(name, value):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")

    deployment = get("deployment", DEPLOYMENT, "fs2-system")
    if not args.apply:
        spec = deployment["spec"]["template"]["spec"]
        volume_index = next(i for i, v in enumerate(spec["volumes"]) if v["name"] == "scientific-batch-scheduling")
        old_name = spec["volumes"][volume_index]["configMap"]["name"]
        cm = get("configmap", old_name, "fs2-system")
        raw = cm["data"]["kueue-scheduling.json"]
        old_sha = hashlib.sha256(raw.encode()).hexdigest()
        container_index, env_index = next((i, j) for i, c in enumerate(spec["containers"])
            for j, e in enumerate(c["env"]) if e["name"] == "FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256")
        assert spec["containers"][container_index]["env"][env_index]["value"] == old_sha
        nodes = json.loads(run("get", "nodes", "-l", LABEL + "=" + POOL, "-o", "json"))["items"]
        if len(nodes) != 1:
            raise ValueError("Review the changed pool size before enabling it")
        contract, quota, zero = extend(json.loads(raw), nodes[0])
        new_raw = canonical(contract)
        sha = hashlib.sha256(new_raw.encode()).hexdigest()
        name = "fs2-scientific-scheduling-" + sha[:12]
        new_cm = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
            "metadata": {"name": name, "namespace": "fs2-system", "annotations": {
                "fs2-serve.nebius.ai/scheduling-contract-sha256": sha}},
            "data": {"kueue-scheduling.json": new_raw}}
        flavor = get("resourceflavor", "inference-l40s-1x")
        flavor["metadata"] = {"name": FLAVOR, "labels": {"app.kubernetes.io/part-of": "fs2-serve", LABEL: POOL}}
        flavor["spec"]["nodeLabels"][LABEL] = POOL
        flavor.pop("status", None)
        save("configmap.json", new_cm)
        save("flavor.json", flavor)
        save("node.json", nodes[0])
        paths = {
            f"/spec/template/spec/volumes/{volume_index}/configMap/name": (old_name, name),
            f"/spec/template/spec/containers/{container_index}/env/{env_index}/value": (old_sha, sha),
        }
        forward = [{"op": "test", "path": "/spec/template", "value": deployment["spec"]["template"]}]
        rollback = []
        for path, (old, new) in paths.items():
            forward.append({"op": "replace", "path": path, "value": new})
            rollback.extend([{"op": "test", "path": path, "value": new},
                             {"op": "replace", "path": path, "value": old}])
        save("deployment.patch.json", forward)
        save("deployment.rollback.json", rollback)
        save("deployment.before.json", deployment)
        for kind, name, entry, position in (("clusterqueue", QUEUE, quota, "0"),
                ("cohort", "inference-shared", zero, "-")):
            current = get(kind, name)
            save(kind + ".before.json", current)
            save(kind + ".patch.json", [
                {"op": "test", "path": "/spec", "value": current["spec"]},
                {"op": "add", "path": "/spec/resourceGroups/0/flavors/" + position, "value": entry}])
        save("helm-overlay.json", {"scientificBatch": {"schedulingContractConfigMapName": new_cm["metadata"]["name"],
             "schedulingContractSha256": sha}})
        save("identity.json", {"context": args.context, "pool": POOL, "new_sha256": sha,
             "old_sha256": old_sha, "old_configmap": old_name, "new_configmap": new_cm["metadata"]["name"],
             "image": spec["containers"][container_index]["image"], "model": "gromacs"})
    else:
        identity = json.loads((args.output / "identity.json").read_text())
        if identity["context"] != args.context:
            raise ValueError("Context mismatch")
    # API validation first; these dry runs do not change shared state.
    for filename in ("flavor.json", "configmap.json"):
        print(run("create", "--dry-run=server", "-f", str(args.output / filename)).decode().strip())
    targets = (("cohort", "inference-shared", []), ("clusterqueue", QUEUE, []),
               ("deployment", DEPLOYMENT, ["-n", "fs2-system"]))
    for kind, name, scope in targets:
        print(run(*scope, "patch", kind, name, "--type=json", "--dry-run=server",
                  "--patch-file", str(args.output / (kind + ".patch.json"))).decode().strip())
    if args.apply:
        for filename in ("flavor.json", "configmap.json"):
            print(run("create", "-f", str(args.output / filename)).decode().strip())
        for kind, name, scope in targets:
            print(run(*scope, "patch", kind, name, "--type=json",
                      "--patch-file", str(args.output / (kind + ".patch.json"))).decode().strip())
    print(json.dumps({"prepared": True, "applied": args.apply, "pool": POOL, "added_gpus": 4}))


if __name__ == "__main__":
    main()
