"""Prepare scoped RDMA publication candidates, never apply or claim readiness.

Mechanics preparation deliberately writes only unbound profile/map fragments.
A genuine native proof and the parent's compatible-reader release barrier are
required before these fragments may enter a published catalog.
"""

import argparse
import copy
import importlib.util
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from qualify_rdma import CLUSTER, NODES, POOL

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "_scoped_mpi_activation", HERE.parent / "gromacs-mpinat-20261003/activate_mpi.py"
)
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)
RESOURCE = "rdma.fs2.nebius/hca"
SHAPE = "multi-node-8gpu-rdma"
QUEUE = "inference-accelerators"
VOLUMES = {
    "execution": ("scientific-batch-execution", "execution-map.json"),
    "scheduling": ("scientific-batch-scheduling", "kueue-scheduling.json"),
    "admin": ("admin-configuration", "admin-configuration.json"),
    "envelope": ("model-controller-envelope", "infrastructure-envelope.json"),
}


def one(rows, key, value):
    selected = [row for row in rows if row.get(key) == value]
    if len(selected) != 1:
        raise ValueError(f"expected one {key}={value}")
    return selected[0]


def shape_fragments(profiles, execution):
    profile = one(profiles["profiles"], "model_id", "gromacs-mpi")
    model = one(execution["models"], "model_id", "gromacs-mpi")
    shapes = profile["workload"]["stages"][0]["execution_shapes"]
    runtime_shapes = model["stages"][0]["execution_shapes"]
    if any(row["id"] == SHAPE for row in shapes + runtime_shapes):
        raise ValueError(
            "RDMA shape already present; recapture and review publication state"
        )
    shape = copy.deepcopy(one(shapes, "id", "multi-node-8gpu"))
    runtime = copy.deepcopy(one(runtime_shapes, "id", "multi-node-8gpu"))
    gpu = shape["placement"]["accelerator"]
    if (
        (shape["min_parallelism"], shape["max_parallelism"], gpu["count"]) != (2, 2, 8)
        or gpu["pool_ids"] != [POOL]
        or gpu["resource_name"] != "nvidia.com/gpu"
    ):
        raise ValueError("captured full-node shape is not the reviewed 2x8 envelope")
    binding = {"resource_name": RESOURCE, "count": 1, "gpu_cluster_id": CLUSTER}
    shape.update(id=SHAPE, rdma=binding)
    runtime.update(id=SHAPE, rdma=copy.deepcopy(binding))
    return {
        "profile_shape": shape,
        "execution_shape": runtime,
        "bound": False,
        "qualification": None,
        "warning": "Not a qualified profile: bind genuine native evidence, image and source identity before publication",
    }


def fabric_candidates(
    scheduling, queue, admin, envelope, nodes, *, expected_queue_drift_sha256=None
):
    selected = [node for node in nodes if node["metadata"]["name"] in NODES]
    labels = {
        "topology.fs2.nebius/scope": "gpu_cluster",
        "topology.nebius.com/gpu-cluster-id": CLUSTER,
    }
    if len(selected) != 2 or any(
        node["metadata"]["labels"].get("accelerator.fs2.nebius/pool-id") != POOL
        or any(
            node["metadata"]["labels"].get(key) != value
            for key, value in labels.items()
        )
        or node["status"]["allocatable"].get("nvidia.com/gpu") != "8"
        or node["status"]["allocatable"].get(RESOURCE) != "1"
        or not any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in node["status"]["conditions"]
        )
        for node in selected
    ):
        raise ValueError(
            "exact two Ready 8-GPU/1-RDMA nodes and GPU-cluster identity required"
        )
    desired = copy.deepcopy(scheduling)
    declared = desired["cluster_queues"][QUEUE]
    if queue["metadata"]["name"] != QUEUE:
        raise ValueError("unexpected ClusterQueue identity")
    drift = {"contract_spec": declared["spec"], "live_spec": queue["spec"]}
    drift_sha = activation.sha(activation.canonical(drift))
    if queue["spec"] != declared["spec"] and drift_sha != expected_queue_drift_sha256:
        raise ValueError(
            "live ClusterQueue differs from the mounted scheduling contract; exact reviewed drift SHA required: "
            + drift_sha
        )
    pool = desired["pools"][POOL]
    if pool["capacity"] != 16 or pool["accelerator_resource_name"] != "nvidia.com/gpu":
        raise ValueError("captured pool GPU capacity differs from reviewed 16 GPUs")
    if desired["accelerator_node_capacity"][POOL]["accelerator_count"] != 8:
        raise ValueError("captured per-node GPU capacity differs from reviewed 8 GPUs")
    if RESOURCE in desired.get("coupled_resource_capacity", {}).get(POOL, {}):
        raise ValueError(
            "RDMA scheduling already published; refuse silent second mutation"
        )
    desired_queue = copy.deepcopy(queue)
    # Preserve each document's pre-existing quotas, defaults and flavor set.
    # The reviewed drift is evidence, not permission to reconcile it here.
    for queue_spec in (declared["spec"], desired_queue["spec"]):
        groups = queue_spec["resourceGroups"]
        gpu_group = [g for g in groups if "nvidia.com/gpu" in g["coveredResources"]]
        if len(gpu_group) != 1 or any(
            RESOURCE in g["coveredResources"] for g in groups
        ):
            raise ValueError("unexpected accelerator/RDMA resource grouping")
        group = gpu_group[0]
        full = one(group["flavors"], "name", pool["resource_flavor"])
        if int(one(full["resources"], "name", "nvidia.com/gpu")["nominalQuota"]) != 16:
            raise ValueError(
                "this scoped overlay requires the captured full 16-GPU queue floor"
            )
        group["coveredResources"].append(RESOURCE)
        for flavor in group["flavors"]:
            if any(resource["name"] == RESOURCE for resource in flavor["resources"]):
                raise ValueError(
                    "RDMA resource exists outside covered resource accounting"
                )
            flavor["resources"].append(
                {"name": RESOURCE, "nominalQuota": "2" if flavor is full else "0"}
            )
    capacity = desired["accelerator_node_capacity"][POOL]
    capacity.setdefault("extended_resources", {})[RESOURCE] = 1
    capacity.setdefault("node_labels", {}).update(labels)
    desired.setdefault("coupled_resource_capacity", {})[POOL] = {RESOURCE: 2}
    updated_admin, updated_envelope = copy.deepcopy(admin), copy.deepcopy(envelope)
    for value, count_key, selector_key in (
        (updated_admin, "accelerators_per_node", "node_selector"),
        (updated_envelope, "acceleratorsPerNode", "nodeSelector"),
    ):
        target = value["pools"][POOL]
        if (
            target[count_key] != 8
            or target[selector_key].get("accelerator.fs2.nebius/pool-id") != POOL
        ):
            raise ValueError("admin/envelope target is not the exact full-node pool")
        target[selector_key]["topology.fs2.nebius/scope"] = "gpu_cluster"
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": queue["metadata"]["uid"]},
        {"op": "test", "path": "/spec", "value": queue["spec"]},
        {"op": "replace", "path": "/spec", "value": desired_queue["spec"]},
    ]
    inverse = [
        {"op": "test", "path": "/metadata/uid", "value": queue["metadata"]["uid"]},
        {"op": "test", "path": "/spec", "value": desired_queue["spec"]},
        {"op": "replace", "path": "/spec", "value": queue["spec"]},
    ]
    return {
        "scheduling": desired,
        "admin": updated_admin,
        "envelope": updated_envelope,
        "queue": desired_queue,
        "queue_patch": patch,
        "queue_inverse": inverse,
        "preserved_queue_drift_sha256": drift_sha
        if queue["spec"] != scheduling["cluster_queues"][QUEUE]["spec"]
        else None,
    }


def kubectl(*args, input=None):
    return subprocess.check_output(
        ["kubectl", "--context", activation.CONTEXT, *args], input=input, timeout=60
    )


def save(path, value):
    path.write_bytes(json.dumps(value, indent=2, sort_keys=True).encode() + b"\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server-dry-run", action="store_true")
    parser.add_argument("--expected-queue-drift-sha256")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    deployment = json.loads(
        kubectl(
            "-n",
            activation.NAMESPACE,
            "get",
            "deployment",
            activation.NAME,
            "-o",
            "json",
        )
    )
    save(args.output / "deployment.before.json", deployment)
    cms, values = {}, {}
    for kind, (volume, key) in VOLUMES.items():
        name = one(deployment["spec"]["template"]["spec"]["volumes"], "name", volume)[
            "configMap"
        ]["name"]
        cm = json.loads(
            kubectl("-n", activation.NAMESPACE, "get", "cm", name, "-o", "json")
        )
        if set(cm["data"]) != {key}:
            raise ValueError("unexpected mounted ConfigMap data keys")
        cms[kind], values[kind] = cm, json.loads(cm["data"][key])
        save(args.output / f"{kind}.before.json", cm)
    api_image = one(
        deployment["spec"]["template"]["spec"]["containers"], "name", "control-plane"
    )["image"]
    rs = json.loads(kubectl("-n", activation.NAMESPACE, "get", "rs", "-o", "json"))
    pods = json.loads(kubectl("-n", activation.NAMESPACE, "get", "pods", "-o", "json"))
    pod = activation.select_api_pod(deployment, rs, pods, api_image)
    profiles = json.loads(
        kubectl(
            "-n",
            activation.NAMESPACE,
            "exec",
            pod["metadata"]["name"],
            "-c",
            "control-plane",
            "--",
            "cat",
            "/opt/fs2/catalog/contracts/scientific-workload-profiles.json",
        )
    )
    save(args.output / "profiles.before.json", profiles)
    nodes = json.loads(kubectl("get", "nodes", "-o", "json"))["items"]
    queue = json.loads(kubectl("get", "clusterqueue", QUEUE, "-o", "json"))
    save(args.output / "queue.before.json", queue)
    save(
        args.output / "nodes.before.json",
        [n for n in nodes if n["metadata"]["name"] in NODES],
    )
    candidate = fabric_candidates(
        values["scheduling"],
        queue,
        values["admin"],
        values["envelope"],
        nodes,
        expected_queue_drift_sha256=args.expected_queue_drift_sha256,
    )
    save(
        args.output / "unbound-shape-fragments.json",
        shape_fragments(profiles, values["execution"]),
    )
    for kind in ("scheduling", "admin", "envelope"):
        _, key = VOLUMES[kind]
        cm = activation.configmap(
            cms[kind],
            key,
            activation.canonical(candidate[kind]),
            "fs2.nebius.ai/rdma-candidate-sha256",
        )
        save(args.output / f"{kind}.candidate.json", cm)
        if args.server_dry_run:
            response = kubectl(
                "create",
                "--dry-run=server",
                "-f",
                "-",
                "-o",
                "json",
                input=json.dumps(cm).encode(),
            )
            save(args.output / f"{kind}.server-dry-run.json", json.loads(response))
    save(args.output / "queue.patch.json", candidate["queue_patch"])
    save(args.output / "queue.inverse.json", candidate["queue_inverse"])
    if args.server_dry_run:
        response = kubectl(
            "patch",
            "clusterqueue",
            QUEUE,
            "--type=json",
            "--dry-run=server",
            "-p",
            json.dumps(candidate["queue_patch"]),
            "-o",
            "json",
        )
        save(args.output / "queue.server-dry-run.json", json.loads(response))
    receipt = {
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "applied": False,
        "publishable": False,
        "native_qualification": None,
        "reader_barrier": None,
        "captured_api_image": api_image,
        "unrelated_models_changed": False,
        "gpu_quotas_changed": False,
        "rdma_aggregate_quota": 2,
        "preserved_queue_drift_sha256": candidate["preserved_queue_drift_sha256"],
        "remaining": [
            "native workload/host-collective proof",
            "exact source/image/proof binding",
            "all-compatible-reader barrier",
            "fresh exact activation patch and REST/MCP validation",
        ],
        "files": {
            p.name: activation.sha(p.read_bytes())
            for p in sorted(args.output.iterdir())
        },
    }
    save(args.output / "receipt.json", receipt)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "applied": False,
                "publishable": False,
                "server_dry_run": args.server_dry_run,
                "files": len(receipt["files"]),
            }
        )
    )


if __name__ == "__main__":
    main()
