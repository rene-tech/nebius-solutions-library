"""Two-node native-runtime qualification, separate from hosted acceptance.

Uses a task-owned JobSet's real DNS and distinct GPU nodes. Keeps Pods available
for output collection; they are explicitly removed by the caller after testing.
No platform or customer-storage credential is injected into runtime processes.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import secrets
import subprocess
import time

from run_cluster_workflow import KUBE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suffix", required=True)
    parser.add_argument(
        "--pool",
        choices=("h100-ondemand-1x", "h100-reserved-8x"),
        default="h100-ondemand-1x",
    )
    args = parser.parse_args()
    if "@sha256:" not in args.image:
        raise ValueError("Use an immutable worker image")
    args.output.mkdir(parents=True, exist_ok=False)
    name = "fs2-gromacs-mpi-probe-" + args.suffix
    peers = [f"{name}-gang-{i}-0.{name}" for i in range(2)]
    remote = "/mnt/fs2-scientific/probe"
    request = json.loads((args.input / "request.json").read_text())
    if request["jobs"][0]["id"] != "gang" or request.get("nodes", 2) != 2:
        raise ValueError(
            "This qualification requires an explicit two-node gang fixture"
        )
    seed = secrets.token_hex(32)
    template = {
        "metadata": {"labels": {"scientific-ai.nebius.com/task": "gromacs-r20260923"}},
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "nodeSelector": {"accelerator.fs2.nebius/pool-id": args.pool},
            "tolerations": [
                {
                    "key": "dedicated",
                    "operator": "Equal",
                    "value": "fs2-inference",
                    "effect": "NoSchedule",
                }
            ],
            "affinity": {
                "podAntiAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "labelSelector": {
                                "matchLabels": {"jobset.sigs.k8s.io/jobset-name": name}
                            },
                            "topologyKey": "kubernetes.io/hostname",
                        }
                    ]
                }
            },
            "securityContext": {
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
            },
            "containers": [
                {
                    "name": "runtime",
                    "image": args.image,
                    "command": ["sleep", "7200"],
                    "env": [
                        {"name": "FS2_MPI_HOSTS", "value": ",".join(peers)},
                        {"name": "FS2_MPI_SSH_SEED", "value": seed},
                        {
                            "name": "FS2_MPI_RANK",
                            "valueFrom": {
                                "fieldRef": {
                                    "fieldPath": "metadata.labels['jobset.sigs.k8s.io/job-index']"
                                }
                            },
                        },
                    ],
                    "volumeMounts": [
                        {"name": "workspace", "mountPath": "/mnt/fs2-scientific"}
                    ],
                    "resources": {
                        "requests": {
                            "cpu": "8",
                            "memory": "16Gi",
                            "nvidia.com/gpu": "1",
                        },
                        "limits": {"cpu": "8", "memory": "16Gi", "nvidia.com/gpu": "1"},
                    },
                }
            ],
            "volumes": [{"name": "workspace", "emptyDir": {"sizeLimit": "64Gi"}}],
        },
    }
    manifest = {
        "apiVersion": "jobset.x-k8s.io/v1alpha2",
        "kind": "JobSet",
        "metadata": {
            "name": name,
            "namespace": "fs2-models",
            "labels": {"scientific-ai.nebius.com/task": "gromacs-r20260923"},
        },
        "spec": {
            "network": {"enableDNSHostnames": True, "publishNotReadyAddresses": True},
            "failurePolicy": {"maxRestarts": 0},
            "replicatedJobs": [
                {
                    "name": "gang",
                    "replicas": 2,
                    "template": {
                        "spec": {
                            "parallelism": 1,
                            "completions": 1,
                            "backoffLimit": 0,
                            "activeDeadlineSeconds": 7200,
                            "template": template,
                        }
                    },
                }
            ],
        },
    }
    subprocess.run(
        KUBE + ["create", "-f", "-"], input=json.dumps(manifest).encode(), check=True
    )
    started = time.monotonic()
    pods = []
    while time.monotonic() - started < 600:
        value = json.loads(
            subprocess.check_output(
                KUBE
                + [
                    "get",
                    "pods",
                    "-l",
                    "jobset.sigs.k8s.io/jobset-name=" + name,
                    "-o",
                    "json",
                ]
            )
        )
        if len(value["items"]) == 2 and all(
            p["status"].get("phase") == "Running" for p in value["items"]
        ):
            pods = sorted(
                value["items"],
                key=lambda p: p["metadata"]["labels"]["jobset.sigs.k8s.io/job-index"],
            )
            break
        time.sleep(2)
    if not pods:
        raise TimeoutError("The two-node qualification gang did not become ready")
    if len({p["spec"]["nodeName"] for p in pods}) != 2:
        raise ValueError("Both ranks landed on the same node")

    def run(pod):
        pod_name = pod["metadata"]["name"]
        index = pod["metadata"]["labels"]["jobset.sigs.k8s.io/job-index"]
        subprocess.run(
            KUBE + ["cp", "--no-preserve", str(args.input), pod_name + ":" + remote],
            check=True,
        )
        with (args.output / f"rank-{index}.log").open("wb") as log:
            result = subprocess.run(
                KUBE
                + [
                    "exec",
                    pod_name,
                    "--",
                    "python3",
                    "-m",
                    "fs2_gromacs.mpi",
                    "--workspace",
                    remote,
                    "--request",
                    remote + "/request.json",
                    "--job-id",
                    "gang",
                    "--operation-id",
                    "9eb1af68-cee7-46ea-9c3c-270e039ba923",
                    "--checkpoint-mode",
                    "local",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        # Copy runtime evidence only; never copy the peer authentication seed/keys.
        target = args.output / ("workspace" if index == "0" else f"peer-{index}")
        target.mkdir()
        for relative in (
            ["data", "result.json", ".fs2/mpi-topology.json", ".fs2/engine.json"]
            if index == "0"
            else [".fs2/mpi/sshd.log"]
        ):
            subprocess.run(
                KUBE
                + [
                    "cp",
                    pod_name + ":" + remote + "/" + relative,
                    str(target / Path(relative).name),
                ],
                check=False,
            )
        return {
            "rank": int(index),
            "pod": pod_name,
            "node": pod["spec"]["nodeName"],
            "exit_code": result.returncode,
        }

    running = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, pods))
    record = {
        "image": args.image,
        "jobset": name,
        "pool": args.pool,
        "ranks": results,
        "runtime_wall_seconds": time.monotonic() - running,
        "gpu_count": 2,
        "cpus_per_rank": 8,
        "worker_exit_code": max(p["exit_code"] for p in results),
        "customer_path_tested": False,
        "checkpoint_transport": "local-only",
    }
    (args.output / "qualification.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
