#!/usr/bin/env python3
"""Turn the canonical rendered plan into applied H100 qualification Jobs.

The container argv, image, labels, annotations, resources, security context and
Kueue suspension come straight from the canonical adapter plan and are never
rewritten.  Only the surrounding cluster wiring is adapted, because the
scientific batch plane the adapter targets is not deployed on this cluster yet.
Every adaptation is recorded in the emitted receipt so the deviation is visible
rather than silent.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent

# Cluster wiring the canonical scientific batch plane has not provisioned yet.
DEVIATIONS = [
    {
        "id": "artifact-plane-mount-absent-from-canonical-job",
        "kind": "contract-defect",
        "detail": (
            "The canonical _job renderer mounts only the request ConfigMap, /workspace and "
            "/tmp. The runtime resolves every external checkpoint under FS2_ARTIFACT_ROOT, "
            "which stays inside the read-only root filesystem, so a strictly canonical Job "
            "can never see Boltz-2 or ProteinMPNN. One read-only artifact-plane mount is added."
        ),
    },
    {
        "id": "aggregate-image-digest-env-absent-from-canonical-job",
        "kind": "contract-defect",
        "detail": (
            "validate_output_manifest requires aggregate.runtime_image_digest to equal the "
            "admitted image digest, but the canonical Job carries no env and no argv flag "
            "that could supply it. FS2_RUNTIME_IMAGE_DIGEST is injected on the aggregate stage."
        ),
    },
    {
        "id": "batch-plane-not-deployed",
        "kind": "environment-gap",
        "detail": (
            "serviceAccount fs2-batch, imagePullSecret fs2-runtime-registry, PVC fs2-cache and "
            "LocalQueue fs2-models-async do not exist on this cluster. The service account and "
            "pull secret are dropped (the pod already disables token automount and the registry "
            "is reachable without a secret), the workspace claim is redirected to the preserved "
            "qualification PVC, and the plan was rendered against the live LocalQueue."
        ),
    },
    {
        "id": "accelerator-placement-not-rendered",
        "kind": "environment-gap",
        "detail": (
            "The canonical plan carries no nodeSelector or toleration, so the GPU stage would "
            "not land on the H100 pool. The live accelerator selector and taint toleration are added."
        ),
    },
]

NODE_SELECTOR = {
    "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
    "accelerator.fs2.nebius/pool-id": "h100-1x",
}
TOLERATIONS = [
    {"effect": "NoSchedule", "key": "dedicated", "operator": "Equal", "value": "fs2-inference"}
]


def _kubectl(context: str, namespace: str, *arguments: str, stdin: str | None = None) -> str:
    command = ["kubectl", "--context", context, "--namespace", namespace, *arguments]
    print("+ " + " ".join(command), flush=True)
    result = subprocess.run(
        command, input=stdin, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    if result.returncode != 0:
        raise SystemExit(result.stdout)
    return result.stdout


def adapt(job: dict[str, Any], *, claim: str, digest: str, gpu: bool) -> dict[str, Any]:
    adapted = copy.deepcopy(job)
    spec = adapted["spec"]["template"]["spec"]
    spec.pop("serviceAccountName", None)
    spec.pop("imagePullSecrets", None)
    for volume in spec["volumes"]:
        if volume["name"] == "workspace":
            volume["persistentVolumeClaim"]["claimName"] = claim
    # The artifact plane and the workspace are two mounts of one claim while the
    # canonical fs2-cache claim is undeployed. readOnly must therefore be set on
    # the mount, never on the claim: a read-only claim marks the whole CSI
    # attachment read-only and the workspace mount loses write access with it.
    spec["volumes"].append(
        {"name": "artifacts", "persistentVolumeClaim": {"claimName": claim}}
    )
    if gpu:
        spec["nodeSelector"] = dict(NODE_SELECTOR)
        spec["tolerations"] = copy.deepcopy(TOLERATIONS)
    container = spec["containers"][0]
    container["volumeMounts"].append(
        {"name": "artifacts", "mountPath": "/opt/fs2/artifacts", "readOnly": True}
    )
    environment = [{"name": "FS2_ARTIFACT_ROOT", "value": "/opt/fs2/artifacts"}]
    if not gpu:
        environment.append({"name": "FS2_RUNTIME_IMAGE_DIGEST", "value": digest})
    container["env"] = environment
    return adapted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=HERE / "generated-plan.json")
    parser.add_argument("--context", default="k8s-inference-h100")
    parser.add_argument("--claim", default="fs2-runtime-qualification-artifacts-r20260902")
    parser.add_argument("--stage", choices=["design", "aggregate"], required=True)
    parser.add_argument("--receipt", type=Path, default=HERE / "submitted-jobs.json")
    arguments = parser.parse_args()

    summary = json.loads(arguments.plan.read_text(encoding="utf-8"))
    plan = summary["plan"]
    namespace = plan["nodes"][0]["job"]["metadata"]["namespace"]
    digest = plan["runtime_image_digest"]

    if arguments.stage == "design":
        request = json.loads((HERE / "mosaic-request.json").read_text(encoding="utf-8"))
        manifest = json.loads((HERE / "mosaic-input-manifest.json").read_text(encoding="utf-8"))
        config_name = plan["nodes"][0]["job"]["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"]
        config_map = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": config_name,
                "namespace": namespace,
                "labels": {"fs2.nebius.ai/task": "fs2-cancer-images-mosaic-runtime-qualification-r20260903"},
            },
            "data": {
                "request.json": json.dumps(request, indent=2) + "\n",
                "input-manifest.json": json.dumps(manifest, indent=2) + "\n",
            },
        }
        _kubectl(arguments.context, namespace, "apply", "-f", "-", stdin=json.dumps(config_map))

    selected = [
        node for node in plan["nodes"]
        if (node["stage_id"] == "design") == (arguments.stage == "design")
    ]
    applied = []
    for node in selected:
        job = adapt(
            node["job"], claim=arguments.claim, digest=digest, gpu=node["stage_id"] == "design"
        )
        job["metadata"].setdefault("labels", {})["fs2.nebius.ai/task"] = (
            "fs2-cancer-images-mosaic-runtime-qualification-r20260903"
        )
        _kubectl(arguments.context, namespace, "apply", "-f", "-", stdin=json.dumps(job))
        applied.append({"node": node["id"], "job": job["metadata"]["name"], "gpu": node["stage_id"] == "design"})

    receipt = {}
    if arguments.receipt.is_file():
        receipt = json.loads(arguments.receipt.read_text(encoding="utf-8"))
    receipt.setdefault("namespace", namespace)
    receipt.setdefault("context", arguments.context)
    receipt.setdefault("runtime_image_digest", digest)
    receipt.setdefault("workspace_claim", arguments.claim)
    receipt["deviations_from_canonical_job"] = DEVIATIONS
    receipt.setdefault("applied", []).extend(applied)
    arguments.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"applied": applied}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
