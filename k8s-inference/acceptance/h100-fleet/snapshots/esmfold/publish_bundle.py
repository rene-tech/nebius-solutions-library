#!/usr/bin/env python3
"""Copy a completed ESM snapshot to a fresh shared-FS path, then verify it.

The task CPU Pod has no fsGroup: kubelet must not change immutable checkpoint
metadata. Its source claim is read-only. Captured bytes and prior bundles are
never overwritten; publication finishes with byte/metadata verification and
fsync before a fresh GPU restore can consume the shared bundle.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("donor", "directory", "kubeconfig"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("node", "run", "name", "destination-pvc"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if Path(args.run).name != args.run or not args.run.startswith("esmfold2"):
        parser.error("an exact ESM snapshot subdirectory is required")
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    donor = json.loads(args.donor.read_bytes())
    runtime = next(c for c in donor["spec"]["containers"] if c["name"] == "scientific-stage")
    source_claim = next(v["persistentVolumeClaim"]["claimName"] for v in donor["spec"]["volumes"]
                        if v["name"] == "snapshot-checkpoints")
    assert source_claim != args.destination_pvc
    python = "/opt/esm/.pixi/envs/gpu/bin/python"
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": args.name, "namespace": "fs2-models",
        "labels": {"snapshot.fs2.nebius/task": "fs2-h100-fleet-snapshot-options-r20260907"}},
        "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": 3600,
            "automountServiceAccountToken": False, "enableServiceLinks": False,
            "nodeSelector": {"kubernetes.io/hostname": args.node},
            "tolerations": donor["spec"].get("tolerations", []),
            "containers": [{"name": "copy", "image": runtime["image"],
                "command": [python, "-c", "import time;time.sleep(3500)"],
                "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
                "securityContext": {"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False},
                "resources": {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "4", "memory": "8Gi"}},
                "volumeMounts": [{"name": "source", "mountPath": "/capture", "readOnly": True},
                                 {"name": "destination", "mountPath": "/publication"}]}],
            "volumes": [{"name": "source", "persistentVolumeClaim": {"claimName": source_claim, "readOnly": True}},
                        {"name": "destination", "persistentVolumeClaim": {"claimName": args.destination_pvc}}]}}
    kube = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", "k8s-inference-h100", "-n", "fs2-models"]

    def call(arguments, data=None, timeout=900):
        return subprocess.run([*kube, *arguments], input=data, text=True, capture_output=True,
                              timeout=timeout, check=True).stdout

    (args.directory / "copy-pod-private.json").write_text(json.dumps(pod))
    call(["create", "--dry-run=client", "-f", "-"], json.dumps(pod))
    call(["create", "-f", "-"], json.dumps(pod))
    receipt = {"status": "running", "source_pvc": source_claim, "destination_pvc": args.destination_pvc,
               "subpath": args.run, "started_at": datetime.now(timezone.utc).isoformat()}
    helpers = Path(__file__).resolve().parent.parent
    try:
        call(["wait", "--for=condition=Ready", "pod/" + args.name, "--timeout=300s"], timeout=310)
        code = ("from pathlib import Path;import subprocess;s=Path('/capture')/" + repr(args.run)
                + ";d=Path('/publication')/" + repr(args.run)
                + ";assert (s/'images/compatibility.json').is_file();assert not d.exists();"
                + "subprocess.run(['cp','-a','--sparse=always',str(s),str(d)],check=True)")
        before = time.monotonic()
        call(["exec", args.name, "--", python, "-c", code])
        receipt["copy_seconds"] = time.monotonic() - before
        for helper, name, paths in (
            ("verify_bundle_copy.py", "copy-verification.json", ["/capture/" + args.run, "/publication/" + args.run]),
            ("flush_bundle.py", "flush.json", ["/publication/" + args.run]),
            ("hash_bundle.py", "bundle-manifest.json", ["/publication/" + args.run]),
        ):
            output = call(["exec", "-i", args.name, "--", python, "-", *paths], (helpers / helper).read_text())
            json.loads(output)
            (args.directory / name).write_text(output)
        receipt.update(status="passed", finished_at=datetime.now(timezone.utc).isoformat())
    finally:
        call(["delete", "pod", args.name, "--wait=true", "--timeout=60s"], timeout=70)
        receipt["copy_pod_deleted"] = True
        (args.directory / "receipt.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
