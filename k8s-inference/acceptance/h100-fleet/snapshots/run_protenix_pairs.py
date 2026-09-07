#!/usr/bin/env python3
"""Three matched fresh normal-worker/read-only-snapshot trials, real inputs.

The same immutable image, loader bridge, weights, original request wrappers and
parameters are used on both sides. Existing image/filesystem caches are kept;
none of these measurements claims disk-cold or reserved-RAM readiness.
"""

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from render_scientific_restore import scientific_restore
from render_serving_probe import render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "config", "directory", "kubeconfig"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--case", type=Path, action="append", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--resume", action="store_true", help="Keep completed trials and retain failed attempts separately")
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=args.resume)
    source = json.loads(args.source.read_bytes())
    config = json.loads(args.config.read_bytes())
    kube = [
        "kubectl",
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        "k8s-inference-h100",
        "-n",
        "fs2-models",
    ]
    receipt = {
        "cache": "existing image and shared filesystem cache; no global or file cache eviction",
        "runs": [],
        "status": "running",
    }
    if args.resume:
        receipt = json.loads((args.directory / "receipt.json").read_bytes())
        unfinished = [row for row in receipt["runs"] if row.get("status") != "passed"]
        for row in unfinished:
            row["status"] = "failed"
        receipt.setdefault("failed_attempts", []).extend(unfinished)
        receipt["runs"] = [row for row in receipt["runs"] if row.get("status") == "passed"]
        receipt["status"] = "running"
    active = None

    def call(command, *, payload=None, check=True):
        return subprocess.run(
            [*kube, *command],
            input=payload,
            text=True,
            capture_output=True,
            timeout=900,
            check=check,
        )

    def save():
        (args.directory / "receipt.json").write_text(json.dumps(receipt, indent=2))

    try:
        for repetition in range(1, args.repetitions + 1):
            for mode in ("normal", "restore"):
                if any(row["repetition"] == repetition and row["mode"] == mode
                       for row in receipt["runs"]):
                    continue
                active = f"fs2-snap-{config['model_id']}-{args.directory.name}-{repetition}-{mode}"
                attempts = sum(row["repetition"] == repetition and row["mode"] == mode
                               for row in receipt.get("failed_attempts", []))
                if attempts:
                    active += f"-a{attempts + 1}"
                local = {**config, "name": active}
                pod = scientific_restore(source, local)
                runtime = next(
                    c
                    for c in pod["spec"]["containers"]
                    if c["name"] == "scientific-stage"
                )
                if mode == "normal":
                    options = argparse.Namespace(
                        container="scientific-stage",
                        entrypoint_json="[]",
                        asyncio_loop=False,
                        python=config["python"],
                        run=f"{config['model_id']}-{args.directory.name}-normal-{repetition}",
                        fallback="fail",
                        mode="donor",
                        request_uid=10001,
                        allow_device_remap=True,
                        tools_image=config["tools_image"],
                        model_revision=config["model_revision"],
                        model_id=config["model_id"],
                        source_configmap=config["source_configmap"],
                        pvc=config["pvc"],
                        name=active,
                        node=config["node"],
                    )
                    normal = render(source, options)
                    normal["spec"]["volumes"].append(
                        next(
                            copy.deepcopy(v)
                            for v in pod["spec"]["volumes"]
                            if v["name"] == "snapshot-cli"
                        )
                    )
                    target = next(
                        c
                        for c in normal["spec"]["containers"]
                        if c["name"] == "scientific-stage"
                    )
                    target["volumeMounts"].append(
                        next(
                            copy.deepcopy(m)
                            for m in runtime["volumeMounts"]
                            if m["name"] == "snapshot-cli"
                        )
                    )
                    pod = normal
                else:
                    offset = runtime["command"].index("restore")
                    runtime["command"][offset:offset] = ["--request-mode", "server"]
                    runtime["command"][runtime["command"].index("--fallback") + 1] = (
                        "fail"
                    )
                (args.directory / f"{active}.pod-private.json").write_text(
                    json.dumps(pod)
                )
                started = time.monotonic()
                row = {
                    "repetition": repetition,
                    "mode": mode,
                    "pod": active,
                    "created_request_at": datetime.now(timezone.utc).isoformat(),
                }
                receipt["runs"].append(row)
                call(["create", "-f", "-"], payload=json.dumps(pod))
                print(json.dumps({"event": "trial_created", "model_id": config["model_id"],
                                  "repetition": repetition, "mode": mode, "pod": active}), flush=True)
                health = "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=1).read().decode())"
                while time.monotonic() - started < 900:
                    observed = json.loads(
                        call(["get", "pod", active, "-o", "json"]).stdout
                    )
                    if observed["status"]["phase"] == "Failed":
                        raise RuntimeError("trial Pod failed before worker readiness")
                    result = call(
                        [
                            "exec",
                            active,
                            "-c",
                            "scientific-stage",
                            "--",
                            config["python"],
                            "-c",
                            health,
                        ],
                        check=False,
                    )
                    if result.returncode == 0:
                        row["ready"] = json.loads(result.stdout)
                        break
                    time.sleep(1)
                else:
                    raise TimeoutError("trial did not become model-ready")
                row.update(
                    ready_observed_at=datetime.now(timezone.utc).isoformat(),
                    creation_to_ready_observed_seconds=time.monotonic() - started,
                    pod_uid=observed["metadata"]["uid"],
                    node=observed["spec"]["nodeName"],
                    pod_created_at=observed["metadata"]["creationTimestamp"],
                )
                container = next(
                    c
                    for c in observed["status"]["containerStatuses"]
                    if c["name"] == "scientific-stage"
                )
                row["container_started_at"] = container["state"]["running"]["startedAt"]
                row["cases"] = []
                save()
                print(json.dumps({"event": "model_ready", "model_id": config["model_id"],
                                  "repetition": repetition, "mode": mode,
                                  "creation_to_ready_observed_seconds": row["creation_to_ready_observed_seconds"]}), flush=True)
                for case in args.case:
                    archive = case / "prepared-workspace.tar"
                    subprocess.run(
                        [
                            *kube,
                            "exec",
                            "-i",
                            active,
                            "-c",
                            "scientific-stage",
                            "--",
                            "tar",
                            "-C",
                            "/mnt/fs2-scientific",
                            "-xpf",
                            "-",
                        ],
                        input=archive.read_bytes(),
                        check=True,
                    )
                    command = json.loads((case / "original-command.json").read_bytes())
                    output = command[command.index("--output-dir") + 1]
                    validation = subprocess.run(
                        [
                            sys.executable,
                            str(Path(__file__).with_name("validate_scientific.py")),
                            "--kubeconfig",
                            str(args.kubeconfig),
                            "--pod",
                            active,
                            "--python",
                            config["python"],
                            "--command",
                            str(case / "original-command.json"),
                            "--worker-variable",
                            config["worker_variable"],
                            "--output",
                            output,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=900,
                        check=False,
                    )
                    (args.directory / f"{active}-{case.name}.log").write_text(
                        validation.stdout + validation.stderr
                    )
                    if validation.returncode:
                        raise RuntimeError(
                            "original scientific wrapper/semantic validation failed"
                        )
                    result = json.loads(
                        next(
                            line.removeprefix("FS2_SNAPSHOT_SEMANTIC ")
                            for line in validation.stdout.splitlines()
                            if line.startswith("FS2_SNAPSHOT_SEMANTIC ")
                        )
                    )
                    row["cases"].append(result)
                    copied = subprocess.check_output(
                        [
                            *kube,
                            "exec",
                            active,
                            "-c",
                            "scientific-stage",
                            "--",
                            "tar",
                            "-C",
                            output,
                            "-cf",
                            "-",
                            ".",
                        ]
                    )
                    (args.directory / f"{active}-{case.name}-outputs.tar").write_bytes(
                        copied
                    )
                logs = call(
                    ["logs", active, "-c", "scientific-stage", "--timestamps"]
                ).stdout
                (args.directory / f"{active}-lifecycle.log").write_text(logs)
                row["status"] = "passed"
                call(["delete", "pod", active, "--wait=true", "--timeout=60s"])
                row["gpu_pod_deleted"] = True
                active = None
                save()
                print(
                    json.dumps(
                        {
                            "repetition": repetition,
                            "mode": mode,
                            "status": "passed",
                            "ready_observed_seconds": row[
                                "creation_to_ready_observed_seconds"
                            ],
                        }
                    ),
                    flush=True,
                )
        receipt["status"] = "passed"
    except BaseException as error:
        receipt["status"] = "failed"
        receipt["error"] = str(error)
        raise
    finally:
        if active:
            state = call(["get", "pod", active, "-o", "json"], check=False)
            (args.directory / f"{active}-failure-pod-private.json").write_text(state.stdout)
            try:
                failed_pod = json.loads(state.stdout)
            except json.JSONDecodeError:
                failed_pod = {}
            for container in failed_pod.get("spec", {}).get("initContainers", []):
                init_log = call(["logs", active, "-c", container["name"], "--timestamps"], check=False)
                (args.directory / f"{active}-{container['name']}-failure.log").write_text(init_log.stdout + init_log.stderr)
            logs = call(
                ["logs", active, "-c", "scientific-stage", "--timestamps"], check=False
            )
            (args.directory / "failure.log").write_text(logs.stdout + logs.stderr)
            call(
                [
                    "delete",
                    "pod",
                    active,
                    "--ignore-not-found=true",
                    "--wait=true",
                    "--timeout=60s",
                ],
                check=False,
            )
        save()


if __name__ == "__main__":
    main()
