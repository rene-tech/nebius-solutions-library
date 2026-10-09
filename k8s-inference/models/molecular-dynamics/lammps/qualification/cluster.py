"""Bounded task-owned GPU qualification; no production App or catalog mutation."""

import argparse
import json
import subprocess
import time
from pathlib import Path

from mirror_runtime import KUBE

TASK = "lammps-r20260923"


def owned(name):
    pod = json.loads(subprocess.check_output(KUBE + ["get", "pod", name, "-o", "json"]))
    if pod["metadata"].get("labels", {}).get("scientific-ai.nebius.com/task") != TASK:
        raise ValueError("refusing a resource not owned by this task")
    return pod


def create(args):
    if "@sha256:" not in args.image:
        raise ValueError("pin the candidate image digest")
    pods = json.loads(subprocess.check_output(KUBE + ["get", "pod", "-l", "scientific-ai.nebius.com/task=" + TASK, "-o", "json"]))
    active = sum(p["status"].get("phase") not in ("Succeeded", "Failed") and any(c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu") for c in p["spec"]["containers"]) for p in pods["items"])
    if active >= args.max_active_gpu_pods:
        raise ValueError("task-owned GPU Pod concurrency limit reached")
    node = json.loads(subprocess.check_output(KUBE + ["get", "node", args.node, "-o", "json"]))
    if not any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"]):
        raise ValueError("target node is not Ready")
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": args.pod, "labels": {"scientific-ai.nebius.com/task": TASK}}, "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never", "activeDeadlineSeconds": 14400, "nodeSelector": {"kubernetes.io/hostname": args.node}, "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}], "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}}, "containers": [{"name": "runtime", "image": args.image, "command": ["sleep", "14400"], "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}, "resources": {"requests": {"cpu": "8", "memory": "24Gi", "nvidia.com/gpu": "1"}, "limits": {"cpu": "8", "memory": "24Gi", "nvidia.com/gpu": "1"}}, "volumeMounts": [{"name": "workspace", "mountPath": "/mnt/fs2-scientific"}, {"name": "shm", "mountPath": "/dev/shm"}]}], "volumes": [{"name": "workspace", "emptyDir": {"sizeLimit": "48Gi"}}, {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}}]}}
    raw = json.dumps(pod).encode()
    subprocess.run(KUBE + ["apply", "--dry-run=client", "-f", "-"], input=raw, check=True)
    subprocess.run(KUBE + ["apply", "-f", "-"], input=raw, check=True)


def run(args):
    pod = owned(args.pod)
    args.output.mkdir(parents=True, exist_ok=False)
    remote = "/mnt/fs2-scientific/" + args.output.name
    before = time.monotonic()
    subprocess.run(KUBE + ["cp", "--no-preserve", str(args.input), args.pod + ":" + remote], check=True)
    input_seconds = time.monotonic() - before
    subprocess.run(KUBE + ["cp", "--no-preserve", str(Path(__file__).with_name("run_case.py")), args.pod + ":/mnt/fs2-scientific/run_case.py"], check=True)
    body = json.loads((args.input / "request.json").read_text())
    job = args.job or body["jobs"][0]["id"]
    before = time.monotonic()
    with (args.output / "client.log").open("wb") as log:
        result = subprocess.run(KUBE + ["exec", args.pod, "--", "python3", "/mnt/fs2-scientific/run_case.py", "--workspace", remote, "--job", job], stdout=log, stderr=subprocess.STDOUT)
    elapsed = time.monotonic() - before
    before = time.monotonic()
    subprocess.run(KUBE + ["cp", "--retries=3", args.pod + ":" + remote, str(args.output / "workspace")], check=True)
    output_seconds = time.monotonic() - before
    receipt = {"pod": args.pod, "pod_uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"], "image": pod["spec"]["containers"][0]["image"], "image_id": pod["status"]["containerStatuses"][0]["imageID"], "created_at": pod["metadata"]["creationTimestamp"], "worker_exit": result.returncode, "job": job, "input_copy_seconds": input_seconds, "runtime_wall_seconds": elapsed, "output_copy_seconds": output_seconds, "customer_path_tested": False, "checkpoint_transport": "local-only", "cpu_limit": 8, "gpu_count": 1, "gpu_process_snapshot": "not-tested"}
    (args.output / "qualification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "run", "delete"))
    parser.add_argument("--pod", required=True)
    parser.add_argument("--image")
    parser.add_argument("--node")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--job")
    parser.add_argument("--max-active-gpu-pods", type=int, choices=(1, 2), default=1, help="Use 2 only after parent explicitly approves simultaneous pool qualification")
    args = parser.parse_args()
    if args.action == "create":
        create(args)
    elif args.action == "run":
        run(args)
    else:
        owned(args.pod)
        subprocess.run(KUBE + ["delete", "pod", args.pod, "--dry-run=client"], check=True)
        subprocess.run(KUBE + ["delete", "pod", args.pod, "--wait=false"], check=True)


if __name__ == "__main__":
    main()
