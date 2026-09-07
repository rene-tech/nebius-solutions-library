#!/usr/bin/env python3
"""Matched current-image normal/restore trials with full unseen-input outputs.

The source is an actual private donor Pod receipt. Model image, argv, resource
limits, localization and adapter remain unchanged. Only the measured optional
snapshot bridge is selected. Each trial is a fresh Pod and is deleted after
its output and lifecycle receipts are retained.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from render_readonly_server_restore import render as restore
from render_serving_recapture import render as normal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("qwen3-8b", "cosmos3-nano", "nv-reason-cxr-3b"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--source-configmap", required=True)
    parser.add_argument("--pvc", required=True)
    parser.add_argument("--address-configmap", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    source = json.loads(args.source.read_bytes())
    kube = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        "k8s-inference-h100",
        "-n",
        "fs2-models",
    ]
    receipt = {
        "model": args.model,
        "cache": "existing images and localized weights; shared snapshot filesystem cache retained, no eviction or reserved-RAM guarantee",
        "status": "running",
        "runs": [],
    }
    active = None

    def call(command, *, payload=None, check=False):
        return subprocess.run(
            [*kube, *command],
            input=payload,
            text=True,
            capture_output=True,
            check=check,
            timeout=90,
        )

    def save():
        (args.directory / "receipt.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )

    try:
        for repetition in range(1, args.repetitions + 1):
            for mode in ("normal", "restore"):
                suffix = f"{args.directory.name}-{repetition}-{mode}"
                active = "fs2-snap-" + suffix
                if mode == "normal":
                    pod = normal(
                        source,
                        name=active,
                        container=args.container,
                        run=suffix,
                        source_configmap=args.source_configmap,
                        pvc=args.pvc,
                    )
                else:
                    pod = restore(
                        source,
                        name=active,
                        container=args.container,
                        pvc=args.pvc,
                        address_configmap=args.address_configmap,
                    )
                (args.directory / f"{active}.pod-private.json").write_text(
                    json.dumps(pod)
                )
                row = {
                    "repetition": repetition,
                    "mode": mode,
                    "pod": active,
                    "created_request_at": datetime.now(timezone.utc).isoformat(),
                }
                receipt["runs"].append(row)
                started = time.monotonic()
                call(["create", "-f", "-"], payload=json.dumps(pod), check=True)
                while time.monotonic() - started < 900:
                    observed = json.loads(
                        call(["get", "pod", active, "-o", "json"], check=True).stdout
                    )
                    statuses = observed["status"].get("containerStatuses", [])
                    runtime = next(
                        (item for item in statuses if item["name"] == args.container),
                        None,
                    )
                    logs = call(["logs", active, "-c", args.container, "--timestamps"])
                    (args.directory / f"{active}-lifecycle.log").write_text(
                        logs.stdout + logs.stderr
                    )
                    if runtime and "terminated" in runtime["state"]:
                        raise RuntimeError(
                            "trial runtime terminated before model readiness"
                        )
                    if (
                        mode == "normal"
                        or '"mechanism": "cuda-criu-restored"' in logs.stdout
                    ):
                        probe = call(
                            [
                                "exec",
                                active,
                                "-c",
                                args.container,
                                "--",
                                "python3",
                                "-c",
                                "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status)",
                            ]
                        )
                        if probe.returncode == 0 and probe.stdout.strip() == "200":
                            break
                    time.sleep(2)
                else:
                    raise TimeoutError("trial did not reach model readiness")
                row.update(
                    pod_uid=observed["metadata"]["uid"],
                    node=observed["spec"]["nodeName"],
                    pod_created_at=observed["metadata"]["creationTimestamp"],
                    container_started_at=runtime["state"]["running"]["startedAt"],
                    ready_observed_at=datetime.now(timezone.utc).isoformat(),
                    pod_create_request_to_ready_seconds=time.monotonic() - started,
                )
                save()
                if args.model == "qwen3-8b":
                    validation = subprocess.run(
                        [
                            *kube,
                            "exec",
                            "-i",
                            active,
                            "-c",
                            args.container,
                            "--",
                            "python3",
                            "-",
                            "--fresh-inputs",
                        ],
                        input=Path(__file__).with_name("validate_qwen.py").read_bytes(),
                        capture_output=True,
                        check=True,
                        timeout=400,
                    )
                    row["semantics"] = json.loads(validation.stdout)
                elif args.model == "nv-reason-cxr-3b":
                    validation = subprocess.run(
                        [sys.executable, str(Path(__file__).parents[1] / "medical-media/cxr_snapshot_probe.py"),
                         "validate", "--kubeconfig", args.kubeconfig, "--pod", active,
                         "--output", str(args.directory / active)],
                        capture_output=True, text=True, check=True, timeout=900,
                    )
                    row["semantics"] = json.loads(validation.stdout)
                else:
                    validation = subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).with_name("validate_cosmos.py")),
                            "--kubeconfig",
                            args.kubeconfig,
                            "--pod",
                            active,
                            "--seed-offset",
                            "10000",
                            "--output",
                            str(args.directory / active),
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=900,
                    )
                    row["semantics"] = json.loads(validation.stdout)
                row["both_full_outputs_observed_at"] = datetime.now(
                    timezone.utc
                ).isoformat()
                if not row["semantics"]["passed"]:
                    raise RuntimeError("full semantic model output failed")
                (args.directory / f"{active}-worker.log").write_text(
                    call(
                        [
                            "exec",
                            active,
                            "-c",
                            args.container,
                            "--",
                            "sh",
                            "-c",
                            'for p in /checkpoints/*/worker.log; do if [ -f "$p" ]; then tail -n 150 "$p"; fi; done',
                        ]
                    ).stdout
                )
                call(
                    ["delete", "pod", active, "--wait=true", "--timeout=60s"],
                    check=True,
                )
                row.update(status="passed", gpu_pod_deleted=True)
                active = None
                save()
                print(
                    json.dumps(
                        {
                            "model": args.model,
                            "mode": mode,
                            "repetition": repetition,
                            "ready_seconds": row["pod_create_request_to_ready_seconds"],
                            "status": "passed",
                        }
                    ),
                    flush=True,
                )
        receipt["status"] = "passed"
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        if active:
            (args.directory / "failure.log").write_text(
                call(["logs", active, "-c", args.container, "--timestamps"]).stdout
            )
            call(["delete", "pod", active, "--wait=true", "--timeout=60s"])
        save()


if __name__ == "__main__":
    main()
