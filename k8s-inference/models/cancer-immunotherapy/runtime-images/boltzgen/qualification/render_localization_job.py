#!/usr/bin/env python3
"""Render the task-owned resumable checkpoint-localization ConfigMap and Job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TASK = "fs2-boltzgen-h100-codex-successor-r20260903"
NAME = "fs2-boltzgen-localize-checkpoints-codex-r20260903"
PYTHON_IMAGE = (
    "python:3.12-slim@"
    "sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
)


def render(node_name: str | None) -> dict[str, object]:
    labels = {
        "fs2.nebius.ai/task": TASK,
        "fs2.nebius.ai/model": "boltzgen",
        "fs2.nebius.ai/purpose": "checkpoint-localization",
    }
    pod_spec: dict[str, object] = {
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "fsGroup": 1000,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "nodeSelector": {
            "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
            "storage.fs2.nebius/reference-data": "true",
        },
        "tolerations": [
            {
                "key": "dedicated",
                "operator": "Equal",
                "value": "fs2-inference",
                "effect": "NoSchedule",
            }
        ],
        "containers": [
            {
                "name": "localize",
                "image": PYTHON_IMAGE,
                "imagePullPolicy": "IfNotPresent",
                "command": [
                    "python",
                    "/opt/fs2/code/localize_checkpoints.py",
                    "--lock",
                    "/opt/fs2/code/image-lock.json",
                    "--host-root",
                    "/reference-data",
                    "--physical-host-root",
                    "/mnt/fs2-reference-data/data",
                    "--staging-name",
                    "fs2-boltzgen-boltzgen-checkpoints-1",
                ],
                "env": [
                    {"name": "PYTHONUNBUFFERED", "value": "1"},
                    {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                ],
                "resources": {
                    "requests": {"cpu": "1", "memory": "2Gi"},
                    "limits": {"cpu": "4", "memory": "6Gi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [
                    {"name": "code", "mountPath": "/opt/fs2/code", "readOnly": True},
                    {"name": "reference-data", "mountPath": "/reference-data"},
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
            }
        ],
        "volumes": [
            {"name": "code", "configMap": {"name": NAME}},
            {
                "name": "reference-data",
                "hostPath": {
                    "path": "/mnt/fs2-reference-data/data",
                    "type": "Directory",
                },
            },
            {"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}},
        ],
    }
    if node_name:
        pod_spec["nodeSelector"]["kubernetes.io/hostname"] = node_name
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": NAME, "namespace": "fs2-models", "labels": labels},
                "data": {
                    "localize_checkpoints.py": (HERE / "localize_checkpoints.py").read_text(),
                    "image-lock.json": (ROOT / "image-lock.json").read_text(),
                },
            },
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": NAME, "namespace": "fs2-models", "labels": labels},
                "spec": {
                    "backoffLimit": 0,
                    "activeDeadlineSeconds": 7200,
                    "template": {"metadata": {"labels": labels}, "spec": pod_spec},
                },
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-name")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    payload = json.dumps(render(arguments.node_name), indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
