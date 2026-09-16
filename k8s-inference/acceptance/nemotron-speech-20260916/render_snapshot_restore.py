"""Retain an exact live speech donor and render a separate restore-only Pod."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "h100-fleet/snapshots"))
from render_readonly_server_restore import render  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--donor", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--source-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = subprocess.check_output([
        "kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
        "-n", "fs2-models", "get", "pod", args.donor, "-o", "json", "--request-timeout=20s",
    ])
    source = json.loads(raw)
    if source["metadata"]["labels"].get("workload.fs2.nebius/owner") != "nemotron-speech-20260916":
        raise ValueError("not a task-owned speech donor")
    pod = render(source, name=args.name, container="speech",
                 network_configmap="fs2-fleet-snapshot-net-tools-v1",
                 address_configmap="fs2-fleet-snapshot-address-v1", address_python="/opt/conda/bin/python3")
    args.source_output.write_text(json.dumps(source, indent=2) + "\n")
    args.output.write_text(json.dumps(pod, indent=2) + "\n")


if __name__ == "__main__":
    main()
