"""Task-owned AMBER GPU screen with explicit free-node and identity checks."""

import argparse
import json
import subprocess
from pathlib import Path

KUBE = ["kubectl", "--kubeconfig", "/home/tux/secure-handoff/cosmos-stockholm-sandbox2-20260917.kubeconfig", "--context", "fs2-remediation-sandbox2"]
NS = ["-n", "fs2-models"]
TASK = "amber-r20260923"


def get(*args):
    return json.loads(subprocess.check_output(KUBE + list(args) + ["-o", "json"]))


def owned(name):
    pod = get(*NS, "get", "pod", name)
    if pod["metadata"].get("labels", {}).get("scientific-ai.nebius.com/task") != TASK:
        raise ValueError("refusing resource outside this task")
    return pod


def create(args):
    if "@sha256:" not in args.image:
        raise ValueError("pin exact private worker image")
    node = get("get", "node", args.node)
    pool = node["metadata"]["labels"].get("accelerator.fs2.nebius/pool-id")
    if pool not in {"h100-ondemand-1x", "l40s-1x"} or not any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"]):
        raise ValueError("target must be a Ready supported single-GPU pool node")
    if args.node == "computeinstance-e00bwrmx5x05qn4bc8":
        raise ValueError("node reserved for the separate snapshot lane")
    active = [pod for pod in get("get", "pods", "-A")["items"] if pod["status"].get("phase") not in {"Succeeded", "Failed"}]
    for pod in active:
        gpu = sum(int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0)) for c in [*pod["spec"].get("containers", []), *pod["spec"].get("initContainers", [])])
        if gpu and (pod["spec"].get("nodeName") == args.node or pod["metadata"].get("labels", {}).get("scientific-ai.nebius.com/task") == TASK):
            raise ValueError("target occupied or task already owns an active GPU Pod")
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": args.pod, "labels": {"scientific-ai.nebius.com/task": TASK}}, "spec": {
        "restartPolicy": "Never", "activeDeadlineSeconds": 10800, "automountServiceAccountToken": False,
        "nodeSelector": {"kubernetes.io/hostname": args.node},
        "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
        "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{"name": "runtime", "image": args.image, "command": ["sleep", "10800"],
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "8", "memory": "24Gi", "nvidia.com/gpu": "1"}, "limits": {"cpu": "8", "memory": "24Gi", "nvidia.com/gpu": "1"}},
            "volumeMounts": [{"name": "workspace", "mountPath": "/mnt/fs2-scientific"}, {"name": "shm", "mountPath": "/dev/shm"}]}],
        "volumes": [{"name": "workspace", "emptyDir": {"sizeLimit": "32Gi"}}, {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}}]}}
    args.evidence.mkdir(parents=True, exist_ok=False)
    (args.evidence / "node-before.json").write_text(json.dumps(node, indent=2) + "\n")
    (args.evidence / "pod-manifest.json").write_text(json.dumps(pod, indent=2) + "\n")
    raw = json.dumps(pod).encode()
    subprocess.run(KUBE + NS + ["apply", "--dry-run=client", "-f", "-"], input=raw, check=True)
    subprocess.run(KUBE + NS + ["apply", "-f", "-"], input=raw, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "delete"))
    parser.add_argument("--pod", required=True)
    parser.add_argument("--node")
    parser.add_argument("--image")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.action == "create":
        create(args)
    else:
        owned(args.pod)
        subprocess.run(KUBE + NS + ["delete", "pod", args.pod, "--wait=false"], check=True)


if __name__ == "__main__":
    main()
