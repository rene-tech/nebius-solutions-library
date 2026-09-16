#!/usr/bin/env python3
"""Render or launch the retained BioIR batch probe through a CSI claim.

The historical launcher mounted the node filesystem directly. This successor
requires one Bound, namespace-local RWX claim populated from the retained
reference-data tree. A live launch verifies that exact PVC and waits for the
Pod before recording a read-only mount probe receipt. ``--render-only`` is the
offline review path and never contacts a cluster.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")
DNS_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
RETAINED_STORAGE_CLASS = "fs2-reference-data-retained-sc"


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def run(base: list[str], *args: str, input_value: dict[str, Any] | None = None) -> str:
    completed = subprocess.run(
        [*base, *args],
        input=None if input_value is None else json.dumps(input_value),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def execution_model(model_id: str) -> dict[str, Any]:
    path = ROOT / "inventory" / "fs2-r927c465c6d-scientific-execution-88c83daa474b.json"
    payload = json.loads(json.loads(path.read_text(encoding="utf-8"))["execution-map.json"])
    return next(model for model in payload["models"] if model["model_id"] == model_id)


def render(model_id: str, node: str, namespace: str, reference_pvc: str) -> tuple[dict[str, Any], dict[str, Any]]:
    stage = execution_model(model_id)["stages"][0]
    if not IMAGE_RE.fullmatch(stage["image"]):
        raise ValueError("the retained BioIR runtime image must be digest pinned")
    for value, label in ((node, "node"), (namespace, "namespace"), (reference_pvc, "reference PVC")):
        if not DNS_RE.fullmatch(value):
            raise ValueError(f"{label} must be one DNS label")

    mounts = [
        {"name": "work", "mountPath": "/work"},
        {"name": "evaluation-client", "mountPath": "/eval", "readOnly": True},
    ]
    reference_mounts: list[dict[str, str | bool]] = []
    for item in stage["mounts"]:
        if item["kind"] != "reference":
            continue
        mount = {
            "name": "reference-data",
            "mountPath": item["mount_path"],
            "subPath": item["sub_path"],
            "readOnly": True,
        }
        mounts.append(mount)
        reference_mounts.append(mount)
    if not reference_mounts:
        raise ValueError("the retained BioIR model has no reference-data mounts")

    resources = {
        boundary: {
            key.replace("ephemeral_storage", "ephemeral-storage"): value
            for key, value in values.items()
        }
        for boundary, values in stage["resources"].items()
    }
    for values in resources.values():
        values["nvidia.com/gpu"] = "1"

    labels = {
        "evaluation": "fs2-bioir-20260915",
        "lane": "coverage",
        "benchmark-model": model_id,
        "fs2-serve.nebius.ai/network-profile": "mounted-content",
    }
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "fs2-bioir-coverage-batch", "namespace": namespace, "labels": labels},
        "immutable": True,
        "data": {"batch_client.py": (ROOT / "batch_client.py").read_text(encoding="utf-8")},
    }
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": f"fs2-bioir-coverage-{model_id}", "namespace": namespace, "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 14400,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "nodeSelector": {"kubernetes.io/hostname": node},
            "tolerations": [
                {"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}
            ],
            "securityContext": {
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "runAsNonRoot": True,
                "fsGroup": 10001,
                "supplementalGroups": [1000],
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "runtime",
                    "image": stage["image"],
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["python", "-c", "import time; time.sleep(14400)"],
                    "resources": resources,
                    "volumeMounts": mounts,
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "readOnlyRootFilesystem": True,
                        "runAsNonRoot": True,
                    },
                }
            ],
            "volumes": [
                {"name": "work", "emptyDir": {"sizeLimit": "32Gi"}},
                {"name": "evaluation-client", "configMap": {"name": "fs2-bioir-coverage-batch"}},
                {
                    "name": "reference-data",
                    "persistentVolumeClaim": {"claimName": reference_pvc, "readOnly": True},
                },
            ],
        },
    }
    return config_map, pod


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=("rfdiffusion", "proteina-complexa"))
    parser.add_argument("--node", required=True)
    parser.add_argument("--namespace", default="fs2-bioir-coverage")
    parser.add_argument("--reference-pvc", required=True)
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context")
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()

    config_map, pod = render(args.model, args.node, args.namespace, args.reference_pvc)
    if args.render_only:
        print(json.dumps({"config_map": config_map, "pod": pod}, indent=2))
        return 0
    if args.kubeconfig is None or not args.context:
        parser.error("a live launch requires --kubeconfig and --context")

    base = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context]
    pvc = json.loads(run(base, "-n", args.namespace, "get", "pvc", args.reference_pvc, "-o", "json"))
    if (
        pvc.get("status", {}).get("phase") != "Bound"
        or pvc.get("spec", {}).get("storageClassName") != RETAINED_STORAGE_CLASS
        or not pvc.get("metadata", {}).get("uid")
        or not pvc.get("metadata", {}).get("resourceVersion")
    ):
        raise RuntimeError("reference-data PVC is not the exact Bound retained CSI contract")

    run(base, "apply", "--server-side", "--field-manager=fs2-bioir-csi-launcher", "-f", "-", input_value=config_map)
    run(base, "apply", "--server-side", "--field-manager=fs2-bioir-csi-launcher", "-f", "-", input_value=pod)
    run(base, "-n", args.namespace, "wait", "--for=condition=Ready", f"pod/{pod['metadata']['name']}", "--timeout=15m")
    for mount in pod["spec"]["containers"][0]["volumeMounts"]:
        if mount["name"] == "reference-data":
            run(base, "-n", args.namespace, "exec", pod["metadata"]["name"], "--", "test", "-r", mount["mountPath"])
    live = json.loads(run(base, "-n", args.namespace, "get", "pod", pod["metadata"]["name"], "-o", "json"))
    receipt = {
        "schema": "fs2-serve.nebius.ai/bioir-csi-launch/v1",
        "captured_at_unix": time.time(),
        "namespace": args.namespace,
        "pod": {
            "name": live["metadata"]["name"],
            "uid": live["metadata"]["uid"],
            "resource_version": live["metadata"]["resourceVersion"],
            "ready": any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in live.get("status", {}).get("conditions", [])
            ),
        },
        "pvc": {
            "name": args.reference_pvc,
            "uid": pvc["metadata"]["uid"],
            "resource_version": pvc["metadata"]["resourceVersion"],
            "storage_class": pvc["spec"]["storageClassName"],
        },
        "reference_mounts": [
            mount for mount in pod["spec"]["containers"][0]["volumeMounts"] if mount["name"] == "reference-data"
        ],
    }
    print(canonical(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
