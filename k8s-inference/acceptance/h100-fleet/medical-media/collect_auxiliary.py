#!/usr/bin/env python3
"""Retain bounded staging/kernel-preflight receipts and delete completed probes."""
import argparse
import datetime
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--kubeconfig", required=True)
parser.add_argument("--pod", required=True, choices=("fs2-mm-evo2-stage-20260907", "fs2-mm-evo2-preflight-20260907", "fs2-mm-evo2-cache-copy-20260907"))
parser.add_argument("--output", required=True, type=Path)
parser.add_argument("--delete-completed", action="store_true")
args = parser.parse_args()
k = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", "k8s-inference-h100", "-n", "fs2-models"]


def kube(*words):
    return subprocess.run(k + list(words), text=True, capture_output=True, check=True).stdout


args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
pod = json.loads(kube("get", "pod", args.pod, "-o", "json"))
if pod["metadata"]["labels"].get("fs2.nebius/task") != "fs2-h100-fleet-medical-media-r20260907":
    raise RuntimeError("pod ownership mismatch")
(args.output / "pod-latest.json").write_text(json.dumps(pod, indent=2) + "\n")
(args.output / "events.json").write_text(kube("get", "events", "--field-selector", "involvedObject.uid=" + pod["metadata"]["uid"], "-o", "json"))
if pod["spec"].get("nodeName"):
    (args.output / "node.json").write_text(kube("get", "node", pod["spec"]["nodeName"], "-o", "json"))
for volume in pod["spec"].get("volumes", []):
    if "persistentVolumeClaim" in volume:
        claim = json.loads(kube("get", "pvc", volume["persistentVolumeClaim"]["claimName"], "-o", "json"))
        (args.output / (volume["name"] + "-pvc.json")).write_text(json.dumps(claim, indent=2) + "\n")
        if claim["spec"].get("volumeName"):
            (args.output / (volume["name"] + "-pv.json")).write_text(kube("get", "pv", claim["spec"]["volumeName"], "-o", "json"))
for container in pod.get("status", {}).get("containerStatuses", []):
    if "running" in container["state"] or "terminated" in container["state"]:
        (args.output / (container["name"] + ".log")).write_text(kube("logs", args.pod, "-c", container["name"], "--timestamps"))
print(json.dumps({"pod": args.pod, "phase": pod["status"]["phase"], "container_states": {c["name"]: c["state"] for c in pod.get("status", {}).get("containerStatuses", [])}}))
if args.delete_completed:
    if pod["status"]["phase"] not in ("Succeeded", "Failed"):
        raise RuntimeError("refusing to delete a running auxiliary probe")
    result = kube("delete", "pod", args.pod, "--grace-period=5", "--wait=true")
    (args.output / "deleted.json").write_text(json.dumps({"deleted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "result": result}) + "\n")
