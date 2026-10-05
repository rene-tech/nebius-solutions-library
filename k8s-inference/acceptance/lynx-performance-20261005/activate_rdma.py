"""Prepare the additive RDMA publication after a compatible-reader release.

Read-only except for a new local proposal directory and Kubernetes server dry
runs. Reuse the ordinary GROMACS identity binder and scoped fabric preparation;
this does not reconcile unrelated Terraform/queue drift or apply a release.
"""

import argparse
import copy
import json
import os
import re
from pathlib import Path

import prepare_rdma as fabric

A = fabric.activation
CONTROLLER = A.NAME + "-model-controller"


def barrier(deployments, replicasets, pods, image):
    """Every potential shared reader, including terminating Pods, must be new."""
    observed = []
    for key, container_name in (
        ("api", "control-plane"),
        ("controller", "model-controller"),
    ):
        deployment = deployments[key]
        desired = deployment["spec"]["replicas"]
        if (
            desired < 1
            or deployment.get("status", {}).get("observedGeneration", 0)
            < deployment["metadata"]["generation"]
        ):
            raise ValueError("Compatible-reader Deployment is not observed")
        owners = {
            row["metadata"]["uid"]
            for row in replicasets
            if any(
                owner.get("uid") == deployment["metadata"]["uid"]
                and owner.get("controller")
                for owner in row["metadata"].get("ownerReferences", [])
            )
        }
        readers = [
            pod
            for pod in pods
            if any(
                owner.get("uid") in owners and owner.get("controller")
                for owner in pod["metadata"].get("ownerReferences", [])
            )
        ]
        if len(readers) != desired:
            raise ValueError(
                "Compatible-reader barrier requires all old Pods to be absent"
            )
        for pod in readers:
            container = A.named(pod["spec"]["containers"], container_name, "reader")
            if (
                pod["metadata"].get("deletionTimestamp")
                or container["image"] != image
                or not any(
                    c["type"] == "Ready" and c["status"] == "True"
                    for c in pod.get("status", {}).get("conditions", [])
                )
            ):
                raise ValueError(
                    "Not every potential reader is Ready on the compatible image"
                )
            if key == "api" and (
                A.named(pod["spec"]["initContainers"], "wait-schema", "schema init")[
                    "image"
                ]
                != image
                or A.named(
                    container["env"], "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE", "tools image"
                ).get("value")
                != image
            ):
                raise ValueError("Compatible API/init/tools identities differ")
            observed.append(
                {
                    "role": key,
                    "name": pod["metadata"]["name"],
                    "uid": pod["metadata"]["uid"],
                    "image": image,
                }
            )
    return observed


def guarded_patch(obj, desired):
    return [
        {"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]},
        {"op": "test", "path": "/spec/template", "value": obj["spec"]["template"]},
        {"op": "replace", "path": "/spec/template", "value": desired},
    ]


def prepare(
    deployments,
    cms,
    live_profiles,
    source_map,
    source_profiles,
    queue,
    nodes,
    *,
    image,
    compatible_image,
    catalog_digest,
    expected_queue_drift,
):
    if re.fullmatch(r"sha256:[a-f0-9]{64}", catalog_digest) is None:
        raise ValueError("Require the exact packaged catalog digest")
    values = {
        kind: json.loads(cm["data"][fabric.VOLUMES[kind][1]])
        for kind, cm in cms.items()
    }
    for kind, (volume, key) in fabric.VOLUMES.items():
        cm = cms[kind]
        if (
            cm.get("immutable") is not True
            or set(cm["data"]) != {key}
            or cm["metadata"]["namespace"] != A.NAMESPACE
            or A.named(
                deployments["api"]["spec"]["template"]["spec"]["volumes"],
                volume,
                "volume",
            )["configMap"]["name"]
            != cm["metadata"]["name"]
        ):
            raise ValueError("Captured immutable ConfigMap/mount changed")
    bound = A.prepare(
        deployments["api"],
        cms["execution"],
        cms["scheduling"],
        live_profiles,
        source_map,
        source_profiles,
        image=image,
        tools_image=image,
        expected_api=compatible_image,
        expected_tools=compatible_image,
        expected_execution=cms["execution"]["metadata"]["name"],
        expected_scheduling=cms["scheduling"]["metadata"]["name"],
    )
    candidate = fabric.fabric_candidates(
        values["scheduling"],
        queue,
        values["admin"],
        values["envelope"],
        nodes,
        expected_queue_drift_sha256=expected_queue_drift,
    )
    maps = {"execution": bound["configmaps"][0]}
    for kind in ("scheduling", "admin", "envelope"):
        maps[kind] = A.configmap(
            cms[kind],
            fabric.VOLUMES[kind][1],
            A.canonical(candidate[kind]),
            "fs2.nebius.ai/rdma-candidate-sha256",
        )
    api = bound["patch"][-1]["value"]
    init = A.named(api["spec"]["initContainers"], "wait-schema", "schema init")
    if init["image"] != compatible_image:
        raise ValueError("API schema init differs from compatible reader baseline")
    init["image"] = image
    schedule_digest = A.sha(
        maps["scheduling"]["data"]["kueue-scheduling.json"].encode()
    )
    maps["scheduling"]["metadata"]["annotations"][
        "fs2-serve.nebius.ai/scheduling-contract-sha256"
    ] = schedule_digest
    main = A.named(api["spec"]["containers"], "control-plane", "API")
    A.named(
        main["env"], "FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256", "environment"
    )["value"] = schedule_digest
    for kind, (volume, _) in fabric.VOLUMES.items():
        A.named(api["spec"]["volumes"], volume, "volume")["configMap"]["name"] = maps[
            kind
        ]["metadata"]["name"]
    api["metadata"]["annotations"]["fs2.nebius.ai/scientific-scheduling-sha256"] = (
        schedule_digest
    )
    api["metadata"]["annotations"]["fs2.nebius.ai/catalog-rollout-digest"] = (
        catalog_digest
    )
    controller = copy.deepcopy(deployments["controller"]["spec"]["template"])
    main = A.named(controller["spec"]["containers"], "model-controller", "controller")
    envelope = A.named(
        controller["spec"]["volumes"], "infrastructure-envelope", "controller envelope"
    )
    if (
        main["image"] != compatible_image
        or envelope["configMap"]["name"] != cms["envelope"]["metadata"]["name"]
    ):
        raise ValueError("Controller image/envelope baseline changed")
    main["image"] = image
    envelope["configMap"]["name"] = maps["envelope"]["metadata"]["name"]
    annotations = controller.setdefault("metadata", {}).setdefault("annotations", {})
    annotations["fs2.nebius.ai/image-digest"] = image.rsplit("@", 1)[1]
    annotations["fs2.nebius.ai/catalog-rollout-digest"] = catalog_digest
    bound["helm_overlay"]["catalog"] = {"rolloutDigest": catalog_digest}
    bound["helm_overlay"]["scientificBatch"].update(
        schedulingContractConfigMapName=maps["scheduling"]["metadata"]["name"],
        schedulingContractSha256=schedule_digest,
    )
    bound["helm_overlay"]["podAnnotations"][
        "fs2.nebius.ai/scientific-scheduling-sha256"
    ] = schedule_digest
    return {
        "configmaps": maps,
        "api_patch": guarded_patch(deployments["api"], api),
        "controller_patch": guarded_patch(deployments["controller"], controller),
        "queue_patch": candidate["queue_patch"],
        "queue_inverse": candidate["queue_inverse"],
        "helm_overlay": bound["helm_overlay"],
        "preserved_queue_drift_sha256": candidate["preserved_queue_drift_sha256"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--compatible-reader-image", required=True)
    parser.add_argument("--catalog-digest", required=True)
    parser.add_argument("--expected-queue-drift-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)

    def read(*parts):
        return json.loads(fabric.kubectl(*parts))

    def save(name, value):
        fabric.save(args.output / name, value)

    deployments = {
        key: read("-n", A.NAMESPACE, "get", "deployment", name, "-o", "json")
        for key, name in (("api", A.NAME), ("controller", CONTROLLER))
    }
    rs = read("-n", A.NAMESPACE, "get", "rs", "-o", "json")["items"]
    pods = read("-n", A.NAMESPACE, "get", "pods", "-o", "json")["items"]
    readers = barrier(deployments, rs, pods, args.compatible_reader_image)
    save("reader-barrier.json", readers)
    for kind, deployment in deployments.items():
        save(kind + ".before.json", deployment)
    cms = {}
    for kind, (volume, _) in fabric.VOLUMES.items():
        name = A.named(
            deployments["api"]["spec"]["template"]["spec"]["volumes"], volume, "volume"
        )["configMap"]["name"]
        cms[kind] = read("-n", A.NAMESPACE, "get", "cm", name, "-o", "json")
        save(kind + ".before.json", cms[kind])
    pod = next(row["name"] for row in readers if row["role"] == "api")
    profiles = read(
        "-n",
        A.NAMESPACE,
        "exec",
        pod,
        "-c",
        "control-plane",
        "--",
        "cat",
        "/opt/fs2/catalog/contracts/scientific-workload-profiles.json",
    )
    queue = read("get", "clusterqueue", fabric.QUEUE, "-o", "json")
    nodes = read("get", "nodes", "-o", "json")["items"]
    save("profiles.before.json", profiles)
    save("queue.before.json", queue)
    save(
        "nodes.before.json", [n for n in nodes if n["metadata"]["name"] in fabric.NODES]
    )
    contracts = A.ROOT / "catalog/runtime/contracts"
    result = prepare(
        deployments,
        cms,
        profiles,
        json.loads((contracts / "scientific-execution-map.json").read_bytes()),
        json.loads((contracts / "scientific-workload-profiles.json").read_bytes()),
        queue,
        nodes,
        image=args.image,
        compatible_image=args.compatible_reader_image,
        catalog_digest=args.catalog_digest,
        expected_queue_drift=args.expected_queue_drift_sha256,
    )
    for kind, cm in result["configmaps"].items():
        save(kind + ".candidate.json", cm)
        checked = json.loads(
            fabric.kubectl(
                "create",
                "--dry-run=server",
                "-f",
                "-",
                "-o",
                "json",
                input=json.dumps(cm).encode(),
            )
        )
        if checked["data"] != cm["data"] or checked.get("immutable") is not True:
            raise ValueError("Server changed candidate ConfigMap bytes")
        save(kind + ".server-dry-run.json", checked)
    for kind, resource, name in (
        ("api", "deployment", A.NAME),
        ("controller", "deployment", CONTROLLER),
        ("queue", "clusterqueue", fabric.QUEUE),
    ):
        save(kind + ".patch.json", result[kind + "_patch"])
        command = ["-n", A.NAMESPACE] if resource == "deployment" else []
        checked = json.loads(
            fabric.kubectl(
                *command,
                "patch",
                resource,
                name,
                "--type=json",
                "--dry-run=server",
                "-p",
                json.dumps(result[kind + "_patch"]),
                "-o",
                "json",
            )
        )
        expected = result[kind + "_patch"][-1]["value"]
        actual = (
            checked["spec"]["template"] if resource == "deployment" else checked["spec"]
        )
        if actual != expected:
            raise ValueError("Server changed the proposed patch")
        save(kind + ".server-dry-run.json", checked)
    save("helm-overlay.json", result["helm_overlay"])
    save("queue.inverse-before-rdma-admissions-only.json", result["queue_inverse"])
    save(
        "receipt.json",
        {
            "applied": False,
            "customer_path_qualified": False,
            "image": args.image,
            "compatible_reader_image": args.compatible_reader_image,
            "preserved_queue_drift_sha256": result["preserved_queue_drift_sha256"],
            "warning": "Never remove HCA accounting while an RDMA workload is admitted.",
            "files": {
                p.name: A.sha(p.read_bytes()) for p in sorted(args.output.iterdir())
            },
        },
    )
    print(
        json.dumps(
            {"output": str(args.output), "applied": False, "server_dry_run": "passed"}
        )
    )


if __name__ == "__main__":
    main()
