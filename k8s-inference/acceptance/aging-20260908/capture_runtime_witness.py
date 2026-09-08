"""Capture one live aging App's Pod/node identity without submitting inference."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess


KUBECTL = [
    "kubectl", "--kubeconfig",
    "/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig",
    "--context", "k8s-inference-h100", "--request-timeout=15s",
]


def query(*args: str) -> dict:
    result = subprocess.run(
        [*KUBECTL, *args, "-o", "json"], check=True,
        capture_output=True, text=True, timeout=25,
    )
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=("altumage", "phenoage"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.output.exists():
        parser.error("A fresh output file is required; never overwrite an earlier witness")
    pods = query(
        "-n", "fs2-models", "get", "pods", "-l",
        f"fs2-serve.nebius.ai/model-deployment={args.model}",
    )["items"]
    candidates = [
        pod for pod in pods
        if not pod["metadata"].get("deletionTimestamp")
        and pod["status"].get("phase") == "Running"
        and any(
            condition["type"] == "Ready" and condition["status"] == "True"
            for condition in pod["status"].get("conditions", [])
        )
    ]
    if len(candidates) != 1:
        raise SystemExit(f"Expected one live Ready {args.model} Pod; observed {len(candidates)}")
    pod = candidates[0]
    node = query("get", "node", pod["spec"]["nodeName"])
    uid = pod["metadata"]["uid"]
    events = query(
        "-n", "fs2-models", "get", "events", "--field-selector",
        f"involvedObject.uid={uid}",
    )
    witness = {
        "schema": "fs2-aging-live-runtime-witness/v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "model_id": args.model,
        "scope": "Read-only current public Pod/node/events; no inference or configuration change",
        "pod": pod, "node": node, "events": events,
    }
    if args.model == "altumage":
        gpu_containers = [
            container["name"] for container in pod["spec"]["containers"]
            if container.get("resources", {}).get("requests", {}).get("nvidia.com/gpu")
        ]
        if len(gpu_containers) != 1:
            raise SystemExit("Expected exactly one GPU-requesting container for hardware witness")
        gpu = subprocess.run(
            [*KUBECTL, "-n", "fs2-models", "exec", pod["metadata"]["name"],
             "-c", gpu_containers[0], "--", "nvidia-smi",
             "--query-gpu=name,uuid,driver_version,memory.total,compute_cap",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=25,
        )
        witness["nvidia_smi"] = {
            "returncode": gpu.returncode, "stdout": gpu.stdout, "stderr": gpu.stderr,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(witness, target, indent=2)
        target.write("\n")
    print(json.dumps({"model": args.model, "pod_uid": uid, "node": node["metadata"]["name"],
                      "witness_path": str(args.output)}))


if __name__ == "__main__":
    main()
