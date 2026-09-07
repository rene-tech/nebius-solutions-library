#!/usr/bin/env python3
"""Fsync and hash one already-captured shared-filesystem snapshot bundle."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess


SAFE_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for value in ("name", "run", "pvc", "node", "image", "python"):
        parser.add_argument("--" + value, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", default="fs2-h100-fleet-bionemo-structure-r20260907")
    args = parser.parse_args()
    if any(SAFE_NAME.fullmatch(value) is None for value in (args.name, args.run, args.pvc, args.task)):
        parser.error("name, run, PVC and task must be DNS labels")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": args.name,
            "namespace": "fs2-models",
            "labels": {
                "fs2.nebius.ai/test-only": "true",
                "fs2.nebius.ai/task": args.task,
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 1800,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "nodeSelector": {"kubernetes.io/hostname": args.node},
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
                    "name": "publish",
                    "image": args.image,
                    "command": [args.python, "-c", "import time; time.sleep(1700)"],
                    "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
                    "securityContext": {
                        "runAsUser": 0,
                        "runAsGroup": 0,
                        "runAsNonRoot": False,
                    },
                    "resources": {
                        "requests": {"cpu": "1", "memory": "1Gi"},
                        "limits": {"cpu": "4", "memory": "8Gi"},
                    },
                    "volumeMounts": [{"name": "bundle", "mountPath": "/publication"}],
                }
            ],
            "volumes": [
                {
                    "name": "bundle",
                    "persistentVolumeClaim": {"claimName": args.pvc},
                }
            ],
        },
    }
    (args.output / "pod-private.json").write_text(json.dumps(pod, indent=2) + "\n")
    kube = [
        "kubectl",
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        "k8s-inference-h100",
        "-n",
        "fs2-models",
    ]

    def call(command, *, payload=None, timeout=900):
        return subprocess.run(
            [*kube, *command],
            input=payload,
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
        ).stdout

    receipt = {"status": "running", "run": args.run, "pvc": args.pvc}
    helpers = Path(__file__).resolve().parent
    try:
        call(["create", "--dry-run=client", "-f", "-"], payload=json.dumps(pod))
        call(["create", "-f", "-"], payload=json.dumps(pod))
        call(["wait", "--for=condition=Ready", "pod/" + args.name, "--timeout=300s"])
        directory = "/publication/" + args.run
        flushed = call(
            ["exec", "-i", args.name, "--", args.python, "-", directory],
            payload=(helpers / "flush_bundle.py").read_text(),
        )
        hashed = call(
            ["exec", "-i", args.name, "--", args.python, "-", directory],
            payload=(helpers / "hash_bundle.py").read_text(),
        )
        flush_receipt = json.loads(flushed)
        hash_receipt = json.loads(hashed)
        publication = {
            "status": "passed",
            "bundle_sha256": hash_receipt["bundle_sha256"],
            "bytes": hash_receipt["bytes"],
            "file_count": hash_receipt["file_count"],
            "files": hash_receipt["manifest"]["files"],
            "compatibility": hash_receipt["compatibility"],
            "flush": flush_receipt,
        }
        (args.output / "publication.json").write_text(
            json.dumps(publication, indent=2) + "\n"
        )
        receipt.update(
            status="passed", finished_at=datetime.now(timezone.utc).isoformat()
        )
    finally:
        try:
            (args.output / "pod-final-private.json").write_text(
                call(["get", "pod", args.name, "-o", "json"], timeout=60)
            )
            call(["delete", "pod", args.name, "--wait=true", "--timeout=60s"], timeout=70)
            receipt["pod_deleted"] = True
        finally:
            (args.output / "receipt.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
