"""Bounded GPU admission test: eight MD-shaped and eight single-cell-shaped jobs.

Uses system-owned test queues, no API/customer credentials or customer usage.
Checks device visibility, simultaneous allocation, backlog and automatic queue
draining. This is NOT a scientific-result or GPU-throughput benchmark.
"""

import argparse
import copy
import json
import os
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if (
        not args.run.startswith("fs2-admission-qa-")
        or not args.run.replace("-", "").isalnum()
    ):
        raise ValueError("Use a task-owned, label-safe QA run name")
    if "@sha256:" not in args.image:
        raise ValueError("Pin the already qualified worker image")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--context", args.context, "--request-timeout=30s"]
    labels = {
        "fs2.nebius.ai/acceptance-run": args.run,
        "fs2.nebius.ai/tenant-id": "system",
    }

    def run(*parts, obj=None):
        return subprocess.run(
            [*kube, *parts],
            input=None if obj is None else json.dumps(obj),
            text=True,
            check=True,
            capture_output=True,
        ).stdout

    def get(kind, name=None, namespace="fs2-models"):
        if kind == "workloads" and name is None:
            value = json.loads(run("get", kind, "-n", namespace, "-o", "json"))
            value["items"] = [
                w
                for w in value["items"]
                if w["spec"].get("queueName", "").startswith(args.run + "-")
            ]
            return value
        return json.loads(
            run(
                "get",
                kind,
                *(
                    [name]
                    if name
                    else ["-l", "fs2.nebius.ai/acceptance-run=" + args.run]
                ),
                "-n",
                namespace,
                "-o",
                "json",
            )
        )

    def save(name, value):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")

    baseline = get("clusterqueue", "inference-accelerators")
    cq = {
        "apiVersion": "kueue.x-k8s.io/v1beta2",
        "kind": "ClusterQueue",
        "metadata": {"name": args.run, "labels": labels},
        "spec": copy.deepcopy(baseline["spec"]),
    }
    cq["spec"].pop("cohortName", None)
    cq["spec"]["preemption"] = {
        "reclaimWithinCohort": "Never",
        "withinClusterQueue": "Never",
        "borrowWithinCohort": {"policy": "Never"},
    }
    cq["spec"]["namespaceSelector"] = {
        "matchLabels": {"kubernetes.io/metadata.name": "fs2-models"}
    }
    capacities = {
        "inference-l40s-4x": 4,
        "inference-l40s-1x": 4,
        "inference-h100-ondemand-1x": 8,
    }
    for group in cq["spec"]["resourceGroups"]:
        group["flavors"] = [f for f in group["flavors"] if f["name"] in capacities]
        for flavor in group["flavors"]:
            for resource in flavor["resources"]:
                # Kueue disallows borrowing/lending limits without a cohort.
                resource.pop("borrowingLimit", None)
                resource.pop("lendingLimit", None)
                if resource["name"] == "nvidia.com/gpu":
                    resource["nominalQuota"] = str(capacities[flavor["name"]])
    objects = [
        cq,
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": args.run, "namespace": "fs2-models", "labels": labels},
            "data": {"waiting": "true"},
        },
    ]
    for lane, weight in (("md", "2"), ("single-cell", "1")):
        objects.append(
            {
                "apiVersion": "kueue.x-k8s.io/v1beta2",
                "kind": "LocalQueue",
                "metadata": {
                    "name": args.run + "-" + lane,
                    "namespace": "fs2-models",
                    "labels": labels,
                },
                "spec": {"clusterQueue": args.run, "fairSharing": {"weight": weight}},
            }
        )
    jobs = []
    for lane in ("md", "single-cell"):
        for index in range(9):
            pools = ["l40s-4x", "l40s-1x"] if lane == "md" else ["h100-ondemand-1x"]
            requests = {
                "cpu": "8100m",
                "memory": "16640Mi" if lane == "md" else "131328Mi",
                "nvidia.com/gpu": "1",
                "ephemeral-storage": "64Gi" if lane == "md" else "128Gi",
            }
            job_labels = {
                **labels,
                "kueue.x-k8s.io/queue-name": args.run + "-" + lane,
                "kueue.x-k8s.io/priority-class": "batch",
                "fs2.nebius.ai/qa-lane": lane,
            }
            jobs.append(
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {
                        "name": f"{args.run}-{lane}-{index}",
                        "namespace": "fs2-models",
                        "labels": job_labels,
                    },
                    "spec": {
                        "suspend": True,
                        "backoffLimit": 0,
                        "activeDeadlineSeconds": 600,
                        "template": {
                            "metadata": {"labels": job_labels},
                            "spec": {
                                "restartPolicy": "Never",
                                "automountServiceAccountToken": False,
                                "tolerations": [
                                    {
                                        "key": "dedicated",
                                        "operator": "Equal",
                                        "value": "fs2-inference",
                                        "effect": "NoSchedule",
                                    }
                                ],
                                "affinity": {
                                    "nodeAffinity": {
                                        "requiredDuringSchedulingIgnoredDuringExecution": {
                                            "nodeSelectorTerms": [
                                                {
                                                    "matchExpressions": [
                                                        {
                                                            "key": "accelerator.fs2.nebius/pool-id",
                                                            "operator": "In",
                                                            "values": pools,
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    }
                                },
                                "securityContext": {
                                    "runAsUser": 10001,
                                    "runAsGroup": 10001,
                                    "runAsNonRoot": True,
                                    "seccompProfile": {"type": "RuntimeDefault"},
                                },
                                "volumes": [
                                    {"name": "gate", "configMap": {"name": args.run}}
                                ],
                                "containers": [
                                    {
                                        "name": "gpu-admission",
                                        "image": args.image,
                                        "command": [
                                            "/bin/sh",
                                            "-ec",
                                            "nvidia-smi --query-gpu=name,uuid --format=csv,noheader; echo device-ready; while [ ! -f /gate/release ]; do sleep 2; done; echo completed",
                                        ],
                                        "resources": {
                                            "requests": requests,
                                            "limits": requests,
                                        },
                                        "volumeMounts": [
                                            {
                                                "name": "gate",
                                                "mountPath": "/gate",
                                                "readOnly": True,
                                            }
                                        ],
                                        "securityContext": {
                                            "allowPrivilegeEscalation": False,
                                            "capabilities": {"drop": ["ALL"]},
                                        },
                                    }
                                ],
                            },
                        },
                    },
                }
            )
    save("objects.json", {"apiVersion": "v1", "kind": "List", "items": objects})
    save("jobs.json", {"apiVersion": "v1", "kind": "List", "items": jobs})
    if not args.apply:
        print(json.dumps({"planned_jobs": 18, "simultaneous_target": 16}))
        return
    receipt = {
        "scope": "GPU admission/device visibility, not model semantics or API concurrency",
        "run": args.run,
        "context": args.context,
        "image": args.image,
        "passed": False,
        "samples": [],
    }
    created = []
    try:
        for obj in objects:
            run("create", "--dry-run=server", "-f", "-", obj=obj)
            run("create", "-f", "-", obj=obj)
            created.append(obj)
        first = [j for j in jobs if not j["metadata"]["name"].endswith("-8")]
        for job in first:
            run("create", "-f", "-", obj=job)
            created.append(job)
        deadline = time.monotonic() + 480
        while time.monotonic() < deadline:
            pods = get("pods")["items"]
            running = [p for p in pods if p["status"]["phase"] == "Running"]
            receipt["samples"].append({"time": time.time(), "running": len(running)})
            if len(running) == 16:
                assert all(
                    "device-ready"
                    in run("logs", p["metadata"]["name"], "-n", "fs2-models")
                    for p in running
                )
                save("sixteen-pods.json", pods)
                save("sixteen-workloads.json", get("workloads"))
                receipt["simultaneous"] = {
                    lane: sum(
                        p["metadata"]["labels"]["fs2.nebius.ai/qa-lane"] == lane
                        for p in running
                    )
                    for lane in ("md", "single-cell")
                }
                break
            if any(p["status"]["phase"] == "Failed" for p in pods):
                raise RuntimeError("GPU admission test Pod failed")
            time.sleep(5)
        else:
            raise TimeoutError(
                "Sixteen GPU-shaped jobs did not become ready in eight minutes"
            )
        for job in jobs:
            if job not in first:
                run("create", "-f", "-", obj=job)
                created.append(job)
        time.sleep(15)
        waiting = get("clusterqueue", args.run)["status"]
        receipt["backlog"] = {
            k: waiting.get(k) for k in ("admittedWorkloads", "pendingWorkloads")
        }
        assert receipt["backlog"] == {"admittedWorkloads": 16, "pendingWorkloads": 2}, (
            receipt["backlog"]
        )
        run(
            "patch",
            "configmap",
            args.run,
            "-n",
            "fs2-models",
            "--type=merge",
            "-p",
            '{"data":{"release":"true"}}',
        )
        for _ in range(60):
            statuses = get("jobs")["items"]
            if len(statuses) == 18 and all(
                j["status"].get("succeeded") == 1 for j in statuses
            ):
                save("completed-jobs.json", statuses)
                receipt["completed"] = 18
                receipt["passed"] = True
                break
            if any(j["status"].get("failed", 0) for j in statuses):
                raise RuntimeError("Queued job failed after capacity release")
            time.sleep(5)
        if not receipt["passed"]:
            raise TimeoutError("Queue did not drain")
    except Exception as error:
        receipt["error"] = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            receipt["command_error"] = error.stderr
        raise
    finally:
        save("receipt.json", receipt)
        save("final-pods.json", get("pods"))
        for obj in reversed(created):
            name = obj["metadata"]["name"]
            if not name.startswith(args.run):
                raise ValueError("Unowned cleanup target")
            run(
                "delete",
                obj["kind"],
                name,
                "-n",
                "fs2-models",
                "--ignore-not-found",
                "--wait=false",
            )
        receipt["cleanup_requested"] = len(created)
        save("receipt.json", receipt)
    print(json.dumps({k: v for k, v in receipt.items() if k != "samples"}))


if __name__ == "__main__":
    main()
