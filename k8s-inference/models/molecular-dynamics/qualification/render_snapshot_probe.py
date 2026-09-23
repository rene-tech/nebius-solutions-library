"""Isolated one-GPU MD snapshot probe; no host mounts or public serving path."""

import argparse
import json


def render(name, node, image, tools_image, configmap, pvc, deadline=2400):
    if not all("@sha256:" in value for value in (image, tools_image)):
        raise ValueError("immutable image digests are required")
    if not 60 <= deadline <= 3600:
        raise ValueError("probe deadline must be between one minute and one hour")
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": "fs2-models", "labels": {
            "app.kubernetes.io/name": "fs2-md-snapshot-probe",
            "fs2.nebius.ai/test-only": "true", "task": "md-snapshot-r20260923",
        }},
        "spec": {
            "restartPolicy": "Never", "terminationGracePeriodSeconds": 10,
            "activeDeadlineSeconds": deadline, "automountServiceAccountToken": False,
            "nodeSelector": {"kubernetes.io/hostname": node},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
            "volumes": [
                {"name": "tools", "emptyDir": {}},
                {"name": "source", "configMap": {"name": configmap}},
                {"name": "checkpoints", "persistentVolumeClaim": {"claimName": pvc}},
                {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}},
            ],
            "initContainers": [{
                "name": "snapshot-tools", "image": tools_image,
                "command": ["/bin/sh", "-c", "cp -a /snapshot-binaries/. /tools/ && cp -L /lib/x86_64-linux-gnu/libc.so.6 /lib/x86_64-linux-gnu/libm.so.6 /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 /tools/lib/"],
                "volumeMounts": [{"name": "tools", "mountPath": "/tools"}],
            }],
            "containers": [{
                "name": "runtime", "image": image, "command": ["/bin/sleep", str(deadline)],
                "securityContext": {"privileged": True, "runAsUser": 0, "runAsGroup": 0},
                "resources": {"requests": {"cpu": "8", "memory": "16Gi", "nvidia.com/gpu": "1"},
                              "limits": {"cpu": "8", "memory": "32Gi", "nvidia.com/gpu": "1"}},
                "env": [
                    {"name": "FS2_SNAPSHOT_RUNTIME_IMAGE", "value": image},
                    {"name": "FS2_SNAPSHOT_TOOLS_IMAGE", "value": tools_image},
                    {"name": "FS2_PROBE_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
                ],
                "volumeMounts": [
                    {"name": "tools", "mountPath": "/tools"},
                    {"name": "source", "mountPath": "/snapshot-source", "readOnly": True},
                    {"name": "checkpoints", "mountPath": "/checkpoints"},
                    {"name": "shm", "mountPath": "/dev/shm"},
                ],
            }],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("name", "node", "image", "tools-image", "configmap", "pvc"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--deadline", type=int, default=2400)
    print(json.dumps(render(**vars(parser.parse_args())), indent=2))


if __name__ == "__main__":
    main()
