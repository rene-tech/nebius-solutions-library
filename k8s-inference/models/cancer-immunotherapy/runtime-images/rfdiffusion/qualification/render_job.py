#!/usr/bin/env python3
"""Render the H100 semantic qualification Job for a published RFdiffusion digest.

The accelerator is selected by class alone, through a single parameterised label.
There is no pool id, no capacity-source pin and no device-name admission check, so
the same renderer targets any GPU family the platform exposes. H100 is only the
default value of ``--accelerator-class``.

The image is always consumed by immutable digest; a mutable tag is refused.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LOCK = json.loads((HERE.parent / "image-lock.json").read_text(encoding="utf-8"))

DIGEST_REFERENCE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")

ARTIFACT_MOUNT = "/opt/fs2/artifacts"
WORKSPACE_MOUNT = "/workspace"
REQUEST_MOUNT = "/var/run/fs2"


def _image(reference: str) -> str:
    if not DIGEST_REFERENCE.match(reference):
        raise SystemExit(
            f"runtime image must be pinned by digest (repository@sha256:...), got {reference!r}"
        )
    return reference


def render(
    *,
    name: str,
    namespace: str,
    image: str,
    accelerator_class: str,
    local_queue: str,
    artifact_claim: str,
    artifact_sub_path: str,
    run_sub_path: str,
    request_config_map: str,
    cache_level: str,
    checkpoint_artifact_id: str,
    gpu_count: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    labels = {
        "app.kubernetes.io/managed-by": "fs2-serve-models",
        "app.kubernetes.io/name": "fs2-rfdiffusion-qualification",
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2.nebius.ai/model-id": "rfdiffusion",
        "fs2.nebius.ai/owner-task": LOCK["owner_task"],
    }
    if local_queue:
        labels["kueue.x-k8s.io/queue-name"] = local_queue

    annotations = {
        "fs2.nebius.ai/adapter-id": LOCK["adapter"]["adapter_id"],
        "fs2.nebius.ai/source-revision": LOCK["source"]["revision"],
        "fs2.nebius.ai/source-tag": LOCK["source"]["tag"],
        "fs2.nebius.ai/checkpoint-sha256": LOCK["external_artifacts"][0]["sha256"],
        "fs2.nebius.ai/artifact-generation": LOCK["artifact_delivery"]["generation"]["generation"],
    }

    command = [
        "python", "/opt/fs2/runtime_entrypoint.py", "run",
        "--request", f"{REQUEST_MOUNT}/request.json",
        "--input-manifest", f"{REQUEST_MOUNT}/input-manifest.json",
        "--artifact-root", ARTIFACT_MOUNT,
        "--checkpoint-artifact-id", checkpoint_artifact_id,
        "--output", f"{WORKSPACE_MOUNT}/{name}",
        "--scratch", "/tmp/fs2-rfdiffusion",
        "--cache-level", cache_level,
        "--timeout-seconds", str(timeout_seconds),
    ]

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels, "annotations": annotations},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": timeout_seconds + 1800,
            "ttlSecondsAfterFinished": 86400,
            "suspend": bool(local_queue),
            "template": {
                "metadata": {"labels": labels, "annotations": annotations},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "terminationGracePeriodSeconds": 90,
                    "nodeSelector": {"accelerator.fs2.nebius/class": accelerator_class},
                    "tolerations": [
                        {"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}
                    ],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "diffuse",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": command,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "readOnlyRootFilesystem": True,
                            },
                            "resources": {
                                "requests": {"cpu": "8", "memory": "48Gi", "nvidia.com/gpu": gpu_count},
                                "limits": {"cpu": "16", "memory": "64Gi", "nvidia.com/gpu": gpu_count},
                            },
                            "volumeMounts": [
                                {"name": "request", "mountPath": REQUEST_MOUNT, "readOnly": True},
                                {
                                    "name": "artifacts",
                                    "mountPath": ARTIFACT_MOUNT,
                                    "subPath": artifact_sub_path,
                                    "readOnly": True,
                                },
                                {"name": "artifacts", "mountPath": WORKSPACE_MOUNT, "subPath": run_sub_path},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "request", "configMap": {"name": request_config_map}},
                        # readOnly belongs on the mount, never on the claim: a read-only
                        # claim marks the whole CSI attachment read-only and the
                        # workspace mount would lose write access with it.
                        {"name": "artifacts", "persistentVolumeClaim": {"claimName": artifact_claim}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "16Gi"}},
                    ],
                },
            },
        },
    }


def main(argv: list[str]) -> int:
    generation = LOCK["artifact_delivery"]["generation"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--namespace", default="fs2-models")
    parser.add_argument("--runtime-image", required=True, help="repository@sha256:...")
    parser.add_argument("--accelerator-class", default="nvidia-h100-sxm5-80gb")
    parser.add_argument("--local-queue", default="", help="empty runs the Job unqueued")
    parser.add_argument("--artifact-claim", default=LOCK["artifact_delivery"]["transitional_claim"])
    parser.add_argument("--artifact-sub-path", default=generation["sub_path"])
    parser.add_argument("--run-sub-path", default="rfdiffusion/runs")
    parser.add_argument("--request-config-map", required=True)
    parser.add_argument("--checkpoint-artifact-id", default="artifact.rfdiffusion.base-ckpt")
    parser.add_argument("--cache-level", default="cold-registry-pull")
    parser.add_argument("--gpu-count", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)

    job = render(
        name=args.name,
        namespace=args.namespace,
        image=_image(args.runtime_image),
        accelerator_class=args.accelerator_class,
        local_queue=args.local_queue,
        artifact_claim=args.artifact_claim,
        artifact_sub_path=args.artifact_sub_path,
        run_sub_path=args.run_sub_path,
        request_config_map=args.request_config_map,
        cache_level=args.cache_level,
        checkpoint_artifact_id=args.checkpoint_artifact_id,
        gpu_count=args.gpu_count,
        timeout_seconds=args.timeout_seconds,
    )
    payload = json.dumps(job, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(payload)
    else:
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
