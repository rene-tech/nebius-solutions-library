#!/usr/bin/env python3
"""Render an isolated scientific CUDA/CRIU donor or restore Pod as JSON.

No production deployment or host setting is changed. Use identical runtime,
source ConfigMap, artifacts and durable directory for both pod lifecycles.
"""

from __future__ import annotations

import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("donor", "restore"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--namespace", default="fs2-models")
    parser.add_argument("--node", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--tools-image", required=True)
    parser.add_argument("--source-configmap", required=True)
    parser.add_argument("--checkpoint-pvc", required=True)
    parser.add_argument("--checkpoint-subdir", required=True)
    parser.add_argument("--reference-root", default="/mnt/fs2-reference-data/data")
    parser.add_argument("--python", default="/opt/esm/.pixi/envs/gpu/bin/python")
    parser.add_argument("--activation-script", default="/opt/fs2/activate.sh")
    parser.add_argument("--runtime-script", default="/snapshot-source/esmfold2_server.py")
    args = parser.parse_args()
    checkpoint_directory = "/checkpoints/" + args.checkpoint_subdir
    command = [
        args.python, "/snapshot-source/supervisor.py", "--directory", checkpoint_directory, args.mode,
    ]
    if args.mode == "donor":
        command += ["--", args.python, "-u", args.runtime_script]
    if args.activation_script:
        command = [
            "/bin/bash", "-c", 'source "$1"; shift; exec "$@"',
            "fs2-snapshot", args.activation_script, *command,
        ]
    artifact_paths = {
        "/models/esmfold2": "esmfold2-trunk/sha256/136a3580c01cc055ae5a1278bae056e5150a5441ddb89dfbafb9f4e88d763a0c",
        "/models/esmc-6b": "esmc-6b/sha256/8f21da30919b3e0d7af9ec6c4b9879542234d77d42ce061fef029397a4d39758",
        "/databases/esmfold2": "esmfold2-ccd/sha256/b1c2fe19204c57f7a7cca6ab4cb0cb420b99312fff424ef2e405fc8234b7616e",
    }
    pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {
            "name": args.name, "namespace": args.namespace,
            "labels": {"app.kubernetes.io/name": "fs2-scientific-snapshot-probe", "fs2.nebius.ai/test-only": "true"},
        },
        "spec": {
            "restartPolicy": "Never", "terminationGracePeriodSeconds": 10,
            "automountServiceAccountToken": False,
            "nodeSelector": {"kubernetes.io/hostname": args.node},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
            "volumes": [
                {"name": "tools", "emptyDir": {}},
                {"name": "source", "configMap": {"name": args.source_configmap}},
                {"name": "checkpoints", "persistentVolumeClaim": {"claimName": args.checkpoint_pvc}},
                {"name": "reference", "hostPath": {"path": args.reference_root, "type": "Directory"}},
                {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}},
            ],
            "initContainers": [{
                "name": "snapshot-tools", "image": args.tools_image,
                "command": ["/bin/sh", "-c", (
                    "cp -a /snapshot-binaries/. /tools/ && "
                    "cp -L /lib/x86_64-linux-gnu/libc.so.6 "
                    "/lib/x86_64-linux-gnu/libm.so.6 "
                    "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 /tools/lib/"
                )],
                "volumeMounts": [{"name": "tools", "mountPath": "/tools"}],
            }],
            "containers": [{
                "name": "runtime", "image": args.runtime_image, "command": command,
                "securityContext": {"privileged": True, "runAsUser": 0, "runAsGroup": 0},
                "resources": {
                    "requests": {"cpu": "8", "memory": "96Gi", "nvidia.com/gpu": "1"},
                    "limits": {"cpu": "16", "memory": "128Gi", "nvidia.com/gpu": "1"},
                },
                "env": [
                    {"name": "HF_HUB_OFFLINE", "value": "1"},
                    {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
                    {"name": "ESMCFOLD_CCD_PATH", "value": "/databases/esmfold2/ccd.pkl"},
                ],
                "volumeMounts": [
                    {"name": "tools", "mountPath": "/tools"},
                    {"name": "source", "mountPath": "/snapshot-source", "readOnly": True},
                    {"name": "checkpoints", "mountPath": "/checkpoints"},
                    {"name": "shm", "mountPath": "/dev/shm"},
                    *[{
                        "name": "reference", "mountPath": path,
                        "subPath": "model-artifacts/public/v1/objects/" + subpath, "readOnly": True,
                    } for path, subpath in artifact_paths.items()],
                ],
            }],
        },
    }
    print(json.dumps(pod, indent=2))


if __name__ == "__main__":
    main()
