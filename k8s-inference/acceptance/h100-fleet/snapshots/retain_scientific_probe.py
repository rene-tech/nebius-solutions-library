#!/usr/bin/env python3
"""Retain private probe state/logs before deleting an exact task-owned Pod."""

import argparse
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True)
    kube = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", "k8s-inference-h100", "-n", "fs2-models"]
    state = subprocess.check_output([*kube, "get", "pod", args.pod, "-o", "json"])
    pod = json.loads(state)
    assert pod["metadata"]["labels"]["snapshot.fs2.nebius/task"] == "fs2-h100-fleet-snapshot-options-r20260907"
    (args.directory / "pod-final-private.json").write_bytes(state)
    for row in pod["spec"].get("initContainers", []) + pod["spec"]["containers"]:
        result = subprocess.run([*kube, "logs", args.pod, "-c", row["name"], "--timestamps"], capture_output=True)
        (args.directory / (row["name"] + ".log")).write_bytes(result.stdout + result.stderr)
    result = subprocess.run([*kube, "exec", args.pod, "-c", "scientific-stage", "--", "tail", "-n", "1000",
                             "/tmp/fs2-checkpoint-work/restore.log"], capture_output=True)
    (args.directory / "criu-restore.log").write_bytes(result.stdout + result.stderr)
    if args.delete:
        subprocess.run([*kube, "delete", "pod", args.pod, "--wait=true", "--timeout=90s"], check=True)


if __name__ == "__main__":
    main()
