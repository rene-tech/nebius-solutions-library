#!/usr/bin/env python3
"""Copy the qualified Evo2 checkpoint to a release-owned RWX claim once.

The existing RWO source is mounted read-only on its current node. This tool
creates only one labelled CPU Pod; it does not create/change/delete claims.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path

from probe import REGISTRY, TASK

SOURCE = "fs2-mm-evo2-40b-cache-20260907"
DESTINATION = "evo2-40b-cache-rwx-ecc3e914"
POD = "fs2-mm-evo2-cache-copy-20260907"
CHECKPOINT_SHA256 = "dd299612b1c1cdded0dfdcaf4d16f98fc97458261d80f4d662429f0ccb316bc3"
COPY_SCRIPT = """set -eu
test -r /source/evo2_40b.pt
test -r /source/evo2_40b.materialization.json
test ! -e /destination/evo2_40b.pt
test ! -e /destination/.fs2-mm-evo2-copy-20260907.partial
test ! -e /destination/evo2_40b.materialization.json
test ! -e /destination/.fs2
date -u '+COPY_BEGIN %Y-%m-%dT%H:%M:%SZ'
cp -p /source/evo2_40b.pt /destination/.fs2-mm-evo2-copy-20260907.partial
date -u '+COPY_BYTES_COMPLETE %Y-%m-%dT%H:%M:%SZ'
printf '%s  %s\\n' 'CHECKPOINT_SHA256' '/destination/.fs2-mm-evo2-copy-20260907.partial' | sha256sum -c -
date -u '+DESTINATION_HASH_VERIFIED %Y-%m-%dT%H:%M:%SZ'
mv /destination/.fs2-mm-evo2-copy-20260907.partial /destination/evo2_40b.pt
cp -p /source/evo2_40b.materialization.json /destination/evo2_40b.materialization.json
cp -a /source/.fs2 /destination/.fs2
stat -c '%n %s bytes mode=%a' /destination/evo2_40b.pt /destination/evo2_40b.materialization.json
date -u '+COPY_COMPLETE %Y-%m-%dT%H:%M:%SZ'
""".replace("CHECKPOINT_SHA256", CHECKPOINT_SHA256)


def manifest(node: str) -> dict:
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": POD, "namespace": "fs2-models", "labels": {"fs2.nebius/task": TASK},
                     "annotations": {"fs2.nebius/cache-cohort": "source-readonly-to-RWX-copy-and-full-destination-sha256"}},
        "spec": {
            "restartPolicy": "Never", "activeDeadlineSeconds": 7200,
            "automountServiceAccountToken": False, "enableServiceLinks": False,
            "nodeSelector": {"kubernetes.io/hostname": node},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
                                "fsGroup": 65532, "fsGroupChangePolicy": "OnRootMismatch"},
            "containers": [{
                "name": "copy", "image": REGISTRY + "/vllm-openai@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635",
                "command": ["/bin/sh", "-ec", COPY_SCRIPT],
                "resources": {"requests": {"cpu": "2", "memory": "1Gi"}, "limits": {"cpu": "4", "memory": "2Gi"}},
                "volumeMounts": [{"name": "source", "mountPath": "/source", "readOnly": True},
                                 {"name": "destination", "mountPath": "/destination"}],
            }],
            "volumes": [{"name": "source", "persistentVolumeClaim": {"claimName": SOURCE, "readOnly": True}},
                        {"name": "destination", "persistentVolumeClaim": {"claimName": DESTINATION}}],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", default="k8s-inference-h100")
    parser.add_argument("--node", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    k = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", "fs2-models"]

    def kube(*words, data=None):
        return subprocess.run(k + list(words), input=data, capture_output=True, text=True, check=True).stdout

    claims = {name: json.loads(kube("get", "pvc", name, "-o", "json")) for name in (SOURCE, DESTINATION)}
    if any(pvc.get("status", {}).get("phase") != "Bound" for pvc in claims.values()):
        raise ValueError("Both claims must already be Bound")
    if claims[SOURCE]["spec"]["volumeName"] == claims[DESTINATION]["spec"]["volumeName"]:
        raise ValueError("Source and destination must identify distinct PVs")
    if claims[DESTINATION]["spec"]["storageClassName"] != "csi-mounted-fs-path-sc":
        raise ValueError("Destination must be the intended shared-filesystem claim")
    args.output.mkdir(parents=True, mode=0o700, exist_ok=True)
    pod = manifest(args.node)
    (args.output / "pod-manifest.json").write_text(json.dumps(pod, indent=2) + "\n")
    (args.output / "claims-before.json").write_text(json.dumps(claims, indent=2) + "\n")
    kube("create", "--dry-run=client", "-f", "-", data=json.dumps(pod))
    receipt = {"requested_at": dt.datetime.now(dt.timezone.utc).isoformat(), "output": kube("create", "-f", "-", data=json.dumps(pod))}
    (args.output / "created.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
