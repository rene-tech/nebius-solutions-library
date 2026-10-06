"""Plan a narrow update of existing Terraform scheduling settings.

Read-only against Kubernetes. The input is the deployment.scheduling section
from tfvars, not a second scheduler or tenant database. Preserve live drift in
unrelated pools, runtime bindings, quotas and namespaces. Emit test-and-replace
patches, an immutable scheduling ConfigMap, and rollback patches for review.
The operator must persist the same settings in the deployment's real tfvars.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

import yaml


def patch(path, before, after):
    return [
        {"op": "test", "path": path, "value": before},
        {"op": "replace", "path": path, "value": after},
    ]


def build(contract, live_queue, controller_config, settings):
    result = copy.deepcopy(contract)
    queue = copy.deepcopy(live_queue)
    config = copy.deepcopy(controller_config)
    name = queue["metadata"]["name"]
    if name not in result["cluster_queues"]:
        raise ValueError("ClusterQueue is outside the captured scheduling contract")
    if settings.get("fair_share_precedence_acknowledged") is not True:
        raise ValueError("Multi-lane historical fairness must be acknowledged")
    if (
        queue["spec"].get("admissionScope", {}).get("admissionMode")
        != "UsageBasedAdmissionFairSharing"
    ):
        raise ValueError(
            "Existing ClusterQueue must already enable admission fair sharing"
        )
    order = settings["default_queue_pool_order"]
    if len(order) != len(set(order)) or set(order) != set(result["pools"]):
        raise ValueError(
            "Pool order must name every existing qualified pool exactly once"
        )
    weights = settings["fair_share_resource_weights"]
    resources = {
        r
        for group in queue["spec"]["resourceGroups"]
        for r in group["coveredResources"]
    }
    if (
        set(weights) != resources
        or any(v < 0 for v in weights.values())
        or not any(weights.values())
    ):
        raise ValueError(
            "Explicit weights must cover all budgeted resources, including RDMA"
        )
    flavor_order = [result["pools"][p]["resource_flavor"] for p in order]
    for group in queue["spec"]["resourceGroups"]:
        previous = group["flavors"]
        known = {f["name"]: f for f in previous}
        group["flavors"] = [known[f] for f in flavor_order if f in known] + [
            f for f in previous if f["name"] not in flavor_order
        ]
    result["cluster_queues"][name]["spec"] = copy.deepcopy(queue["spec"])
    result["cluster_queue_pool_order"][name] = list(order)
    for service in result["service_classes"].values():
        route = result["local_queue_routes"][service["default_local_queue"]]
        if route["cluster_queue"] == name:
            service["pool_preference"] = list(order)
    manifests = []
    for lane, policy in settings["local_queues"].items():
        if (
            policy["cluster_queue"] != name
            or policy["namespace"] not in result["cluster_queue_namespaces"][name]
        ):
            raise ValueError(
                "New lane must use the existing ClusterQueue and an admitted namespace"
            )
        if (
            not policy["tenant_ids"]
            or not policy["model_ids"]
            or not policy["service_classes"]
        ):
            raise ValueError(
                "New lanes need explicit tenant, model and service-class selectors"
            )
        if any(m in result["namespace_bound_models"] for m in policy["model_ids"]):
            raise ValueError("This update cannot reroute namespace-bound models")
        if policy["fair_sharing_weight"] <= 1e-9:
            raise ValueError("LocalQueue weight must satisfy the Kueue minimum")
        route = {
            k: copy.deepcopy(policy[k])
            for k in (
                "namespace",
                "cluster_queue",
                "tenant_ids",
                "model_ids",
                "service_classes",
            )
        }
        if (
            lane in result["local_queue_routes"]
            and result["local_queue_routes"][lane] != route
        ):
            raise ValueError("Refusing to rebind or repurpose an existing LocalQueue")
        for other, binding in result["local_queue_routes"].items():
            if other != lane and all(
                set(binding.get(k, [])) & set(route[k])
                for k in ("tenant_ids", "model_ids", "service_classes")
            ):
                raise ValueError("Tenant/model/class route collides with another lane")
        manifest = {
            "apiVersion": "kueue.x-k8s.io/v1beta2",
            "kind": "LocalQueue",
            "metadata": {
                "name": lane,
                "namespace": policy["namespace"],
                "labels": {"app.kubernetes.io/part-of": "fs2-serve"},
            },
            "spec": {
                "clusterQueue": name,
                "fairSharing": {"weight": str(policy["fair_sharing_weight"])},
            },
        }
        result["local_queue_routes"][lane] = route
        result["local_queues"][lane] = manifest
        manifests.append(manifest)
    config.setdefault("admissionFairSharing", {})["resourceWeights"] = weights
    # Reconcile a stale duplicate capacity field, without changing actual quota.
    result["pool_capacity"] = {
        p: facts["capacity"] for p, facts in result["pools"].items()
    }
    raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "namespace": "fs2-system",
            "name": "fs2-scientific-scheduling-" + digest[:12],
        },
        "immutable": True,
        "data": {"kueue-scheduling.json": raw},
    }
    return result, queue, config, manifests, cm, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--queue", default="inference-accelerators")
    parser.add_argument("--kueue-release", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", args.context, "--request-timeout=30s"]

    def get(kind, name, namespace=None):
        command = [*kube, "get", kind, name, "-o", "json"]
        if namespace:
            command += ["-n", namespace]
        return json.loads(subprocess.check_output(command))

    def save(name, data):
        (args.output / name).write_text(json.dumps(data, indent=2) + "\n")

    deployment = get("deployment", "fs2-serve-control-plane", "fs2-system")
    before = deployment["spec"]["template"]
    volume = next(
        v
        for v in before["spec"]["volumes"]
        if v["name"] == "scientific-batch-scheduling"
    )
    baseline_cm = get("configmap", volume["configMap"]["name"], "fs2-system")
    contract = json.loads(baseline_cm["data"]["kueue-scheduling.json"])
    cq = get("clusterqueue", args.queue)
    manager_name = args.kueue_release + "-manager-config"
    manager = get("configmap", manager_name, "kueue-system")
    config_key = "controller_manager_config.yaml"
    config_raw = manager["data"][config_key]
    settings = json.loads(args.settings.read_text())["deployment"]["scheduling"]
    candidate, new_cq, config, lanes, cm, digest = build(
        contract, cq, yaml.safe_load(config_raw), settings
    )
    # Verify exact runtime route resolution separately before applying these patches.
    save("baseline.scheduling.configmap.json", baseline_cm)
    save("baseline.clusterqueue.json", cq)
    save("scheduling.configmap.json", cm)
    save("scheduling.json", candidate)
    save("lanes.json", {"apiVersion": "v1", "kind": "List", "items": lanes})
    for lane in lanes:
        check = subprocess.run(
            [
                *kube,
                "get",
                "localqueue",
                lane["metadata"]["name"],
                "-n",
                lane["metadata"]["namespace"],
                "--ignore-not-found",
                "-o",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if check.stdout.strip():
            existing = json.loads(check.stdout)
            if existing["spec"]["clusterQueue"] != lane["spec"]["clusterQueue"]:
                raise ValueError("Live LocalQueue has a different immutable binding")
            save(lane["metadata"]["name"] + ".baseline.json", existing)
    after = copy.deepcopy(before)
    for v in after["spec"]["volumes"]:
        if v["name"] == "scientific-batch-scheduling":
            v["configMap"]["name"] = cm["metadata"]["name"]
    found = 0
    for container in after["spec"]["containers"]:
        for env in container.get("env", []):
            if env["name"] == "FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256":
                env["value"] = digest
                found += 1
    if found != 1:
        raise ValueError("Expected one API scheduling digest binding")
    new_config_raw = yaml.safe_dump(config, sort_keys=False)
    for label, path, old, new in [
        ("api", "/spec/template", before, after),
        (
            "clusterqueue",
            "/spec/resourceGroups",
            cq["spec"]["resourceGroups"],
            new_cq["spec"]["resourceGroups"],
        ),
        ("kueue-config", "/data/" + config_key, config_raw, new_config_raw),
    ]:
        save(label + ".patch.json", patch(path, old, new))
        save(label + ".rollback.json", patch(path, new, old))
    save(
        "receipt.json",
        {
            "context": args.context,
            "scheduling_sha256": digest,
            "previous_scheduling_configmap": baseline_cm["metadata"]["name"],
            "configmap": cm["metadata"]["name"],
            "lanes": [m["metadata"]["name"] for m in lanes],
            "controller_configmap": manager_name,
            "applied": False,
        },
    )
    print(json.dumps({"planned": True, "output": str(args.output), "sha256": digest}))


if __name__ == "__main__":
    main()
