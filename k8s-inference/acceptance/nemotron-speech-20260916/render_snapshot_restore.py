"""Retain an exact live speech donor and render a separate restore-only Pod."""

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "h100-fleet/snapshots"))
from render_readonly_server_restore import render  # noqa: E402


def render_restore(source, *, name, node=None):
    if source["metadata"]["labels"].get("workload.fs2.nebius/owner") != "nemotron-speech-20260916":
        raise ValueError("not a task-owned speech donor")
    source = copy.deepcopy(source)
    if node:
        source["spec"].setdefault("nodeSelector", {})["kubernetes.io/hostname"] = node
    pod = render(source, name=name, container="speech",
                 network_configmap="fs2-fleet-snapshot-net-tools-v1",
                 address_configmap="fs2-fleet-snapshot-address-v1", address_python="/opt/conda/bin/python3")
    pod["spec"]["activeDeadlineSeconds"] = 900
    return pod


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--donor")
    sources.add_argument("--source", type=Path, help="Retained live donor receipt after the clean donor was removed")
    parser.add_argument("--node", help="Different existing H100 node for a device-remapping qualification")
    parser.add_argument("--name", required=True)
    parser.add_argument("--source-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.source.read_bytes() if args.source else subprocess.check_output([
        "kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
        "-n", "fs2-models", "get", "pod", args.donor, "-o", "json", "--request-timeout=20s",
    ])
    source = json.loads(raw)
    pod = render_restore(source, name=args.name, node=args.node)
    args.source_output.write_text(json.dumps(source, indent=2) + "\n")
    args.output.write_text(json.dumps(pod, indent=2) + "\n")


if __name__ == "__main__":
    main()
