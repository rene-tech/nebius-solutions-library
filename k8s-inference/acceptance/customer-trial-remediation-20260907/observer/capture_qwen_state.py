"""Capture bounded read-only Qwen autoscaler state without inference traffic."""

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    command = [
        "kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
        "--request-timeout=15s", "-n", "fs2-models", "get",
    ]
    targets = {
        "model": ["modeldeployments.inference.fs2.nebius.ai", "qwen3-8b"],
        "hpa": ["hpa", "keda-hpa-fs2-model-qwen3-8b-b300-burst-h100-1x"],
        "scaler": ["scaledobjects.keda.sh", "fs2-model-qwen3-8b-b300-burst-h100-1x"],
        "deployments": ["deployments", "-l", "fs2-serve.nebius.ai/model-deployment=qwen3-8b"],
        "pods": ["pods", "-l", "fs2-serve.nebius.ai/model-deployment=qwen3-8b"],
    }
    result = {"captured_at": datetime.now(UTC).isoformat(), "resources": {}}
    for name, target in targets.items():
        reply = subprocess.run(  # noqa: S603 - fixed read-only targets, no shell interpolation.
            [*command, *target, "-o", "json"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        result["resources"][name] = {
            "returncode": reply.returncode,
            "data": json.loads(reply.stdout) if reply.returncode == 0 else None,
            "stderr": reply.stderr,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({
        "captured_at": result["captured_at"],
        "returncodes": {key: row["returncode"] for key, row in result["resources"].items()},
        "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
    }))


if __name__ == "__main__":
    main()
