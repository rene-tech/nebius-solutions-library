#!/usr/bin/env python3
"""Retain bounded private Pod logs/events for one already-running trial Job."""

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="fs2-models")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    command = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "--request-timeout=15s",
        "-n",
        args.namespace,
    ]

    def run(*parts: str) -> dict:
        result = subprocess.run(
            [*command, *parts], capture_output=True, text=True, timeout=30, check=False
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    result = {"captured_at": datetime.now(UTC).isoformat(), "job": args.job, "pods": []}
    pods = run("get", "pods", "-l", "job-name=" + args.job, "-o", "json")
    if pods["returncode"]:
        raise SystemExit("Cannot read exact Job Pods")
    for pod in json.loads(pods["stdout"])["items"]:
        name, uid = pod["metadata"]["name"], pod["metadata"]["uid"]
        row = {
            "name": name,
            "uid": uid,
            "created_at": pod["metadata"]["creationTimestamp"],
            "labels": pod["metadata"].get("labels", {}),
            "node_selector": pod["spec"].get("nodeSelector", {}),
            "resources": [
                {"name": c["name"], "resources": c.get("resources", {})}
                for c in pod["spec"]["containers"]
            ],
            "status": pod.get("status"),
            "logs": {},
            "events": run(
                "get",
                "events",
                "--field-selector",
                "involvedObject.uid=" + uid,
                "-o",
                "json",
            ),
        }
        for container in [
            *pod["spec"].get("initContainers", []),
            *pod["spec"]["containers"],
        ]:
            row["logs"][container["name"]] = run(
                "logs", name, "-c", container["name"], "--timestamps", "--tail=5000"
            )
        result["pods"].append(row)
    node_reply = run("get", "nodes", "-o", "json")
    result["node_resources"] = (
        [
            {
                "name": n["metadata"]["name"],
                "capacity": n["status"].get("capacity"),
                "allocatable": n["status"].get("allocatable"),
            }
            for n in json.loads(node_reply["stdout"])["items"]
        ]
        if node_reply["returncode"] == 0
        else node_reply
    )
    workloads = run("get", "workloads.kueue.x-k8s.io", "-o", "json")
    result["job_workloads"] = (
        [
            {
                "name": w["metadata"]["name"],
                "uid": w["metadata"]["uid"],
                "status": w.get("status", {}),
            }
            for w in json.loads(workloads["stdout"])["items"]
            if any(
                owner.get("kind") == "Job" and owner.get("name") == args.job
                for owner in w["metadata"].get("ownerReferences", [])
            )
        ]
        if workloads["returncode"] == 0
        else workloads
    )
    events = run("get", "events", "-o", "json")
    result["retained_job_and_workload_events"] = (
        [
            e
            for e in json.loads(events["stdout"])["items"]
            if args.job in e.get("involvedObject", {}).get("name", "")
        ]
        if events["returncode"] == 0
        else events
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(
        json.dumps(
            {
                "job": args.job,
                "namespace": args.namespace,
                "pod_count": len(result["pods"]),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
