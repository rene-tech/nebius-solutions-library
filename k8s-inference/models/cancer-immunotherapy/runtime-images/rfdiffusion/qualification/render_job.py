#!/usr/bin/env python3
"""Render the H100 semantic qualification Job for a published RFdiffusion digest.

The accelerator is selected by class alone, through a single parameterised label.
There is no pool id, no capacity-source pin and no device-name admission check, so
the same renderer targets any GPU family the platform exposes. H100 is only the
default value of ``--accelerator-class``.

The image is always consumed by immutable digest; a mutable tag is refused.

Artifact delivery is mixed-plane. Each ``--plane NAME=CLAIM[:SUBPATH]`` becomes its
own read-only mount at ``<artifact-root>/NAME``, so a request may draw the checkpoint
from one plane and a target structure from another without either plane having to
host the other's bytes. Manifest paths are then relative to the artifact root and
begin with the plane name, which is what lets the same image bind a public plane and
a licence-restricted plane in one run. One shared claim mounted at a task-owned
subPath is the older single-plane shape and is not assumed here.
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


def parse_plane(spec: str) -> tuple[str, str, str]:
    """``NAME=CLAIM[:SUBPATH]`` -> (name, claim, sub_path)."""
    if "=" not in spec:
        raise SystemExit(f"--plane must be NAME=CLAIM[:SUBPATH], got {spec!r}")
    name, _, remainder = spec.partition("=")
    claim, _, sub_path = remainder.partition(":")
    if not name or not claim:
        raise SystemExit(f"--plane must name both a plane and a claim, got {spec!r}")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name):
        raise SystemExit(f"plane name must be a DNS label, got {name!r}")
    return name, claim, sub_path


def render(
    *,
    name: str,
    namespace: str,
    image: str,
    accelerator_class: str,
    local_queue: str,
    planes: list[tuple[str, str, str]],
    run_claim: str,
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
        "fs2.nebius.ai/artifact-planes": ",".join(p[0] for p in planes),
    }

    artifact_mounts = []
    plane_volumes = []
    seen_claims: dict[str, str] = {}
    for index, (plane_name, claim, sub_path) in enumerate(planes):
        volume_name = seen_claims.get(claim)
        if volume_name is None:
            volume_name = f"plane-{index}"
            seen_claims[claim] = volume_name
            plane_volumes.append(
                {"name": volume_name, "persistentVolumeClaim": {"claimName": claim}}
            )
        mount = {
            "name": volume_name,
            "mountPath": f"{ARTIFACT_MOUNT}/{plane_name}",
            "readOnly": True,
        }
        if sub_path:
            mount["subPath"] = sub_path
        artifact_mounts.append(mount)

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
                                *artifact_mounts,
                                {"name": "workspace", "mountPath": WORKSPACE_MOUNT, "subPath": run_sub_path},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "request", "configMap": {"name": request_config_map}},
                        # readOnly belongs on the mount, never on the claim: a read-only
                        # claim marks the whole CSI attachment read-only and the
                        # workspace mount would lose write access with it.
                        *plane_volumes,
                        {"name": "workspace", "persistentVolumeClaim": {"claimName": run_claim}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "16Gi"}},
                    ],
                },
            },
        },
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--namespace", default="fs2-models")
    parser.add_argument("--runtime-image", required=True, help="repository@sha256:...")
    parser.add_argument("--accelerator-class", default="nvidia-h100-sxm5-80gb")
    parser.add_argument("--local-queue", default="", help="empty runs the Job unqueued")
    parser.add_argument(
        "--plane",
        action="append",
        default=[],
        metavar="NAME=CLAIM[:SUBPATH]",
        help="read-only artifact plane mounted at <artifact-root>/NAME; repeatable",
    )
    parser.add_argument("--run-claim", default=LOCK["artifact_delivery"]["transitional_claim"])
    parser.add_argument("--run-sub-path", default="rfdiffusion/runs")
    parser.add_argument("--request-config-map", required=True)
    parser.add_argument("--checkpoint-artifact-id", default="artifact.rfdiffusion.base-ckpt")
    parser.add_argument("--cache-level", default="cold-registry-pull")
    parser.add_argument("--gpu-count", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)

    if not args.plane:
        raise SystemExit(
            "at least one --plane NAME=CLAIM[:SUBPATH] is required; artifact delivery "
            "is mixed-plane and no default single claim is assumed"
        )
    job = render(
        name=args.name,
        namespace=args.namespace,
        image=_image(args.runtime_image),
        accelerator_class=args.accelerator_class,
        local_queue=args.local_queue,
        planes=[parse_plane(spec) for spec in args.plane],
        run_claim=args.run_claim,
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
