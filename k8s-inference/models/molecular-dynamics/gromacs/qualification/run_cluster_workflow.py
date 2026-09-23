"""Exercise an exact candidate worker on one idle GPU; retain full evidence.

This is a runtime qualification, not a hosted REST/MCP/customer acceptance test.
It only creates a task-owned Pod. Results never overwrite an earlier run.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

KUBE = ["kubectl", "--kubeconfig",
        "/home/tux/secure-handoff/cosmos-stockholm-sandbox2-20260917.kubeconfig",
        "--context", "fs2-remediation-sandbox2", "-n", "fs2-models"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", choices=("h100-ondemand-1x", "l40s-1x"), required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suffix", default="v1")
    parser.add_argument("--job-id", default="lysozyme")
    parser.add_argument("--existing-pod", help="Reuse only an existing task-owned qualification Pod with the same image and pool.")
    args = parser.parse_args()
    if "@sha256:" not in args.image:
        raise ValueError("pin the candidate image digest")
    args.output.mkdir(parents=True, exist_ok=False)
    name = "fs2-gromacs-r20260923-" + args.pool.split("-")[0] + "-" + args.suffix
    if args.existing_pod:
        name = args.existing_pod
        existing = json.loads(subprocess.check_output(KUBE + ["get", "pod", name, "-o", "json"]))
        if (existing["metadata"].get("labels", {}).get("scientific-ai.nebius.com/task") != "gromacs-r20260923"
                or existing["spec"]["containers"][0]["image"] != args.image
                or existing["spec"]["nodeSelector"]["accelerator.fs2.nebius/pool-id"] != args.pool):
            raise ValueError("reuse is restricted to the matching task-owned qualification Pod")
    remote = "/mnt/fs2-scientific/" + args.output.name
    pod = {
        "apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "fs2-models",
            "labels": {"scientific-ai.nebius.com/task": "gromacs-r20260923"}},
        "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never", "activeDeadlineSeconds": 7200,
            "nodeSelector": {"accelerator.fs2.nebius/pool-id": args.pool},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
            "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
            "containers": [{"name": "runtime", "image": args.image, "command": ["sleep", "7200"],
                "volumeMounts": [{"name": "workspace", "mountPath": "/mnt/fs2-scientific"}],
                "resources": {"requests": {"cpu": "8", "memory": "16Gi", "nvidia.com/gpu": "1"},
                              "limits": {"cpu": "8", "memory": "16Gi", "nvidia.com/gpu": "1"}}}],
            "volumes": [{"name": "workspace", "emptyDir": {"sizeLimit": "64Gi"}}],
        },
    }
    start = time.monotonic()
    if not args.existing_pod:
        subprocess.run(KUBE + ["apply", "-f", "-"], input=json.dumps(pod).encode(), check=True)
    subprocess.run(KUBE + ["wait", "--for=condition=Ready", "pod/" + name, "--timeout=600s"], check=True)
    ready_seconds = time.monotonic() - start
    subprocess.run(KUBE + ["cp", "--no-preserve", str(args.input), name + ":" + remote], check=True)
    (args.output / "environment.txt").write_bytes(subprocess.check_output(KUBE + ["exec", name, "--",
        "nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv"]))
    running = time.monotonic()
    with (args.output / "worker.log").open("wb") as log:
        result = subprocess.run(KUBE + ["exec", name, "--", "python3", "-m", "fs2_gromacs.worker",
            "--workspace", remote, "--request", remote + "/request.json",
            "--job-id", args.job_id, "--operation-id", "9eb1af68-cee7-46ea-9c3c-270e039ba923",
            "--checkpoint-mode", "local"], stdout=log, stderr=subprocess.STDOUT)
    execution_seconds = time.monotonic() - running
    subprocess.run(KUBE + ["cp", name + ":" + remote, str(args.output / "workspace")], check=True)
    pod_state = json.loads(subprocess.check_output(KUBE + ["get", "pod", name, "-o", "json"]))
    record = {"image": args.image, "pod": name, "node": pod_state["spec"]["nodeName"], "pool": args.pool,
              "pod_ready_seconds": ready_seconds, "runtime_wall_seconds": execution_seconds,
              "worker_exit_code": result.returncode, "customer_path_tested": False,
              "checkpoint_transport": "local-only", "cpus": 8, "gpu_count": 1, "pod_reused": bool(args.existing_pod)}
    (args.output / "qualification.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
