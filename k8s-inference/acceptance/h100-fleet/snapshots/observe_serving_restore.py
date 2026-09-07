#!/usr/bin/env python3
"""Observe a fresh task Pod and validate unseen inputs after CUDA restore.

HTTP health alone is insufficient while the CPU API process is being restored.
Require the supervisor's completed CUDA+CRIU event first, then application
health and two complete model outputs. All raw Pod/log receipts stay private.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        choices=("qwen3-8b", "cosmos3-nano", "nv-reason-cxr-3b", "genmol"),
        required=True,
    )
    parser.add_argument("--pod", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--expect-filesystem-fallback", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    kube = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        "k8s-inference-h100",
        "-n",
        "fs2-models",
    ]
    receipt = {"model": args.model, "pod": args.pod, "status": "running"}

    def call(command):
        return subprocess.run(
            [*kube, *command], text=True, capture_output=True, check=False, timeout=30
        )

    try:
        deadline = time.monotonic() + 750
        while time.monotonic() < deadline:
            current = call(["get", "pod", args.pod, "-o", "json"])
            if current.returncode:
                raise RuntimeError("restore Pod is unavailable")
            pod = json.loads(current.stdout)
            (args.directory / "pod-private.json").write_text(current.stdout)
            logs = call(["logs", args.pod, "-c", args.container, "--timestamps"])
            (args.directory / "lifecycle.log").write_text(logs.stdout + logs.stderr)
            runtime = next(
                (
                    item
                    for item in pod["status"].get("containerStatuses", [])
                    if item["name"] == args.container
                ),
                None,
            )
            if runtime and "terminated" in runtime["state"]:
                raise RuntimeError(
                    "snapshot runtime terminated before qualified readiness"
                )
            expected_event = (
                '"event": "snapshot_filesystem_unavailable"'
                if args.expect_filesystem_fallback
                else '"mechanism": "cuda-criu-restored"'
            )
            if expected_event in logs.stdout:
                health_path = (
                    "/v1/health/ready" if args.model == "genmol" else "/health"
                )
                probe = call(
                    [
                        "exec",
                        args.pod,
                        "-c",
                        args.container,
                        "--",
                        "python3",
                        "-c",
                        "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000"
                        + health_path
                        + "',timeout=2).status)",
                    ]
                )
                if probe.returncode == 0 and probe.stdout.strip() == "200":
                    break
            time.sleep(2)
        else:
            raise TimeoutError(
                "snapshot restore did not reach completed CUDA+application readiness"
            )
        now = datetime.now(timezone.utc)
        started = runtime["state"]["running"]["startedAt"]
        created = pod["metadata"]["creationTimestamp"]
        receipt.update(
            mechanism="normal-load-filesystem-fallback"
            if args.expect_filesystem_fallback
            else "cuda-criu-restored",
            pod_uid=pod["metadata"]["uid"],
            node=pod["spec"]["nodeName"],
            ready_observed_at=now.isoformat(),
            container_started_at=started,
            pod_created_at=created,
            container_to_ready_seconds=(
                now - datetime.fromisoformat(started.replace("Z", "+00:00"))
            ).total_seconds(),
            pod_to_ready_seconds=(
                now - datetime.fromisoformat(created.replace("Z", "+00:00"))
            ).total_seconds(),
        )
        if args.model == "qwen3-8b":
            validator = Path(__file__).with_name("validate_qwen.py")
            result = subprocess.run(
                [
                    *kube,
                    "exec",
                    "-i",
                    args.pod,
                    "-c",
                    args.container,
                    "--",
                    "python3",
                    "-",
                    "--fresh-inputs",
                ],
                input=validator.read_bytes(),
                capture_output=True,
                check=True,
                timeout=400,
            )
            (args.directory / "semantics.json").write_bytes(result.stdout)
        elif args.model == "nv-reason-cxr-3b":
            result = subprocess.run(
                [sys.executable, str(Path(__file__).parents[1] / "medical-media/cxr_snapshot_probe.py"),
                 "validate", "--kubeconfig", args.kubeconfig, "--pod", args.pod,
                 "--output", str(args.directory / "semantics")],
                capture_output=True, text=True, check=True, timeout=900,
            )
            (args.directory / "semantics.log").write_text(result.stdout + result.stderr)
        elif args.model == "genmol":
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("validate_genmol.py")),
                    "--kubeconfig",
                    args.kubeconfig,
                    "--pod",
                    args.pod,
                    "--container",
                    args.container,
                    "--output",
                    str(args.directory / "semantics"),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=1300,
            )
            (args.directory / "semantics.log").write_text(result.stdout + result.stderr)
        else:
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("validate_cosmos.py")),
                    "--kubeconfig",
                    args.kubeconfig,
                    "--pod",
                    args.pod,
                    "--seed-offset",
                    "10000",
                    "--output",
                    str(args.directory / "semantics"),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=900,
            )
            (args.directory / "semantics.log").write_text(result.stdout + result.stderr)
        receipt["status"] = "passed"
        receipt[
            "original_inputs_passed"
            if args.model in ("nv-reason-cxr-3b", "genmol")
            else "unseen_inputs_passed"
        ] = 2
    except Exception as error:
        receipt["status"] = "failed"
        receipt["error"] = str(error)
    finally:
        (args.directory / "receipt.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
    print(json.dumps(receipt), flush=True)
    raise SystemExit(0 if receipt["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
