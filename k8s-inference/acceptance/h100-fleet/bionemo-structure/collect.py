#!/usr/bin/env python3
"""Collect raw task Job/Pod evidence without embedding cluster credentials."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any


TASK = "fs2-h100-fleet-bionemo-structure-r20260907"


def kubectl(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", "k8s-inference-h100", "-n", "fs2-models", *args],
        check=check,
        text=True,
        capture_output=True,
    )


def write_text(path: Path, value: str) -> None:
    path.write_text(value)
    path.chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--pod",
        action="append",
        default=[],
        help="collect only a named task Pod (repeatable)",
    )
    args = parser.parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output_root.chmod(0o700)

    jobs_raw = kubectl(
        ["get", "jobs", "-l", f"fs2.nebius.ai/task={TASK}", "-o", "json"]
    ).stdout
    pods_raw = kubectl(
        ["get", "pods", "-l", f"fs2.nebius.ai/task={TASK}", "-o", "json"]
    ).stdout
    write_text(args.output_root / "jobs.json", jobs_raw)
    write_text(args.output_root / "pods.json", pods_raw)

    pods: dict[str, Any] = json.loads(pods_raw)
    selected = set(args.pod)
    pod_items = [
        pod
        for pod in pods.get("items", [])
        if not selected or pod["metadata"]["name"] in selected
    ]
    missing = selected - {pod["metadata"]["name"] for pod in pod_items}
    if missing:
        parser.error(f"unknown task Pod(s): {', '.join(sorted(missing))}")
    for pod in pod_items:
        name = pod["metadata"]["name"]
        pod_root = args.output_root / name
        pod_root.mkdir(exist_ok=True, mode=0o700)
        pod_root.chmod(0o700)
        for container in ("materialize-validator", "runtime", "validator"):
            result = kubectl(["logs", name, "-c", container], check=False)
            write_text(pod_root / f"{container}.log", result.stdout + result.stderr)
        # EmptyDir evidence survives until the completed Pod is removed. Copy
        # it when the validator container is still available; logs remain the
        # authoritative fallback because each validator emits its full receipt.
        receipt = kubectl(
            ["exec", name, "-c", "validator", "--", "find", "/evidence", "-type", "f", "-maxdepth", "3", "-print", "-exec", "cat", "{}", ";"],
            check=False,
        )
        write_text(pod_root / "evidence-files.txt", receipt.stdout + receipt.stderr)

    manifest = {
        "schema_version": 1,
        "cluster_context": "k8s-inference-h100",
        "namespace": "fs2-models",
        "task": TASK,
        "kubeconfig_path": os.environ.get("KUBECONFIG", ""),
        "job_count": len(json.loads(jobs_raw).get("items", [])),
        "pod_count": len(pod_items),
    }
    write_text(args.output_root / "collection.json", json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
