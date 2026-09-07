#!/usr/bin/env python3
"""CPU-only exact CXR bundle copy from task block disk to retained shared FS."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
SNAPSHOTS = HERE.parent / "snapshots"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--source-pvc", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", default="computeinstance-e00p3acr87k9k4mckj")
    args = parser.parse_args()
    if not args.run.startswith("cxr-r") or Path(args.run).name != args.run:
        raise ValueError("An exact CXR capture subdirectory is required")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    bundle = json.loads((SNAPSHOTS / "qwen3-8b-bundle.json").read_text())
    name = "fs2-mm-cxr-snapshot-copy-" + args.run
    kube = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", "k8s-inference-h100", "-n", "fs2-models"]

    def call(command, *, data=None, timeout=60):
        return subprocess.run([*kube, *command], input=data, text=True, capture_output=True, check=True, timeout=timeout).stdout

    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "fs2-models",
        "labels": {"fs2.nebius/task": "fs2-h100-fleet-medical-media-r20260907"}},
        "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": 3600,
        "automountServiceAccountToken": False, "enableServiceLinks": False,
        "nodeSelector": {"kubernetes.io/hostname": args.node},
        "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
        "containers": [{"name": "copy", "image": bundle["runtime_image"],
            "command": ["python3", "-c", "import time; time.sleep(3500)"],
            "env": [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}],
            "securityContext": {"runAsUser": 0, "runAsGroup": 0, "runAsNonRoot": False},
            "resources": {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "4", "memory": "8Gi"}},
            "volumeMounts": [{"name": "source", "mountPath": "/capture", "readOnly": True},
                             {"name": "destination", "mountPath": "/publication"}]}],
        "volumes": [{"name": "source", "persistentVolumeClaim": {"claimName": args.source_pvc, "readOnly": True}},
                    {"name": "destination", "persistentVolumeClaim": {"claimName": bundle["pvc"]}}]}}
    (args.output / "pod-manifest.json").write_text(json.dumps(pod, indent=2) + "\n")
    call(["create", "--dry-run=client", "-f", "-"], data=json.dumps(pod))
    call(["create", "-f", "-"], data=json.dumps(pod))
    call(["wait", "--for=condition=Ready", "pod/" + name, "--timeout=300s"], timeout=310)
    before = dt.datetime.now(dt.timezone.utc).isoformat()
    # Fresh task subpath only; never overwrite an existing published capture.
    code = "from pathlib import Path; import subprocess; s=Path('/capture')/" + repr(args.run) + "; d=Path('/publication')/" + repr(args.run) + "; assert (s/'images/compatibility.json').is_file(); assert (s/'filesystem/manifest.json').is_file(); assert not d.exists(); subprocess.run(['cp','-a','--sparse=always',str(s),str(d)],check=True)"
    call(["exec", name, "--", "python3", "-c", code], timeout=900)
    copied = dt.datetime.now(dt.timezone.utc).isoformat()
    verified = call(["exec", "-i", name, "--", "python3", "-", "/capture/" + args.run, "/publication/" + args.run],
                    data=(SNAPSHOTS / "verify_bundle_copy.py").read_text(), timeout=900)
    (args.output / "publication.json").write_text(verified)
    flushed = call(["exec", "-i", name, "--", "python3", "-", "/publication/" + args.run],
                   data=(SNAPSHOTS / "flush_bundle.py").read_text(), timeout=900)
    (args.output / "flush.json").write_text(flushed)
    receipt = {"status": "passed", "source_pvc": args.source_pvc, "destination_pvc": bundle["pvc"], "subpath": args.run,
               "copy_started_at": before, "copy_finished_at": copied, "verified_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    (args.output / "pod-final.json").write_text(call(["get", "pod", name, "-o", "json"]))
    call(["delete", "pod", name, "--wait=true", "--timeout=60s"], timeout=70)
    receipt["copy_pod_deleted"] = True
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
