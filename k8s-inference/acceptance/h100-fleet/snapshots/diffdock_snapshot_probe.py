#!/usr/bin/env python3
"""Create, validate, capture, and remove an isolated DiffDock snapshot donor."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from render_serving_probe import render as initial
from render_serving_recapture import render as recapture
from validate_diffdock import validate

HERE = Path(__file__).resolve().parent
TASK = "fs2-h100-fleet-bionemo-structure-r20260907"
MODEL_REVISION = "85c49b60d3e0b0182a59ee43a34a6d7036981284"


def utc() -> str:
    return datetime.now(UTC).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("create", "observe", "validate", "capture", "delete")
    )
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--pod", default="fs2-diffdock-snapshot-donor-r01")
    parser.add_argument("--run", default="diffdock-r01")
    parser.add_argument("--node", default="computeinstance-e00m0hsph76ajt9sdb")
    parser.add_argument(
        "--source-configmap", default="fs2-fleet-snapshot-serving-diffdock-v1"
    )
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    kube = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        "k8s-inference-h100",
        "-n",
        "fs2-models",
    ]

    def call(command, *, payload=None, timeout=60, check=True):
        return subprocess.run(
            [*kube, *command],
            input=payload,
            text=True,
            capture_output=True,
            check=check,
            timeout=timeout,
        )

    def save(name, value):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")

    if args.action == "create":
        if args.source is None:
            raise ValueError("--source is required for create")
        bundle = json.loads((HERE / "qwen3-8b-bundle.json").read_bytes())
        config = argparse.Namespace(
            container="runtime",
            entrypoint_json=json.dumps(
                ["python3", "/opt/fs2/runtime/common/server.py"]
            ),
            asyncio_loop=False,
            python="python3",
            run=args.run,
            fallback="fail",
            request_uid=10001,
            allow_device_remap=True,
            mode="donor",
            tools_image=bundle["tools_image"],
            model_revision=MODEL_REVISION,
            model_id="diffdock",
            source_configmap=args.source_configmap,
            pvc="fs2-fleet-snapshots-rwx-r20260907",
            node=args.node,
            name=args.pod,
        )
        pod = initial(json.loads(args.source.read_bytes()), config)
        pod = recapture(
            pod,
            name=args.pod,
            container="runtime",
            run=args.run,
            source_configmap=config.source_configmap,
            network_configmap="fs2-fleet-snapshot-net-tools-v1",
        )
        runtime = next(
            item for item in pod["spec"]["containers"] if item["name"] == "runtime"
        )
        network_path = next(item for item in runtime["env"] if item["name"] == "PATH")
        network_path["value"] = (
            "/tools/usr/sbin:/usr/local/nvidia/bin:/usr/local/cuda/bin:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        separator = runtime["command"].index("--") + 1
        runtime["command"][separator:separator] = [
            "python3",
            "/snapshot-source/working_directory_launcher.py",
            "--directory",
            "/opt/fs2/model/upstream",
            "--uid",
            "10001",
            "--gid",
            "10001",
            "--",
        ]
        pod["metadata"]["labels"]["fs2.nebius/task"] = TASK
        pod["spec"]["activeDeadlineSeconds"] = 3600
        save("donor-manifest.json", pod)
        created = call(["create", "-f", "-"], payload=json.dumps(pod))
        save("created.json", {"requested_at": utc(), "result": created.stdout})
        print(json.dumps({"pod": args.pod, "status": "created"}), flush=True)
        return

    pod = json.loads(call(["get", "pod", args.pod, "-o", "json"]).stdout)
    if pod["metadata"].get("labels", {}).get("fs2.nebius/task") != TASK:
        raise ValueError("task Pod ownership mismatch")
    if args.action == "delete":
        result = call(["delete", "pod", args.pod, "--wait=true", "--timeout=60s"])
        save(
            "deleted.json",
            {
                "uid": pod["metadata"]["uid"],
                "completed_at": utc(),
                "result": result.stdout,
            },
        )
        return
    if args.action == "validate":
        print(
            json.dumps(
                validate(
                    args.kubeconfig,
                    args.pod,
                    args.output / "semantics",
                )
            ),
            flush=True,
        )
        return
    if args.action == "observe":
        for _ in range(450):
            pod = json.loads(call(["get", "pod", args.pod, "-o", "json"]).stdout)
            save("donor-live.json", pod)
            for container in pod.get("status", {}).get("containerStatuses", []):
                if "terminated" in container["state"]:
                    raise RuntimeError(
                        "snapshot donor exited before application readiness"
                    )
            worker = call(
                [
                    "exec",
                    args.pod,
                    "-c",
                    "runtime",
                    "--",
                    "tail",
                    "-n",
                    "250",
                    f"/checkpoints/{args.run}/worker.log",
                ],
                check=False,
            )
            (args.output / "worker.log").write_text(worker.stdout + worker.stderr)
            alive = call(
                [
                    "exec",
                    args.pod,
                    "-c",
                    "runtime",
                    "--",
                    "sh",
                    "-c",
                    (
                        f"test ! -f /checkpoints/{args.run}/live-worker-pid || "
                        f'kill -0 "$(cat /checkpoints/{args.run}/live-worker-pid)"'
                    ),
                ],
                check=False,
            )
            if alive.returncode:
                raise RuntimeError(
                    "snapshot donor worker exited before application readiness"
                )
            probe = call(
                [
                    "exec",
                    args.pod,
                    "-c",
                    "runtime",
                    "--",
                    "python3",
                    "-c",
                    (
                        "import urllib.request; print(urllib.request.urlopen("
                        "'http://127.0.0.1:8000/readyz',timeout=2).status)"
                    ),
                ],
                check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "200":
                save("ready.json", {"pod": args.pod, "observed_at": utc()})
                print(
                    json.dumps(
                        {"pod": args.pod, "status": "HTTP-ready", "observed_at": utc()}
                    ),
                    flush=True,
                )
                return
            time.sleep(2)
        raise TimeoutError("donor did not reach application readiness")

    save("donor-completed.json", pod)
    code = (
        "from pathlib import Path; import subprocess; "
        f"d=Path('/checkpoints/{args.run}'); "
        "subprocess.run(['python3','/snapshot-source/serving_checkpoint.py','capture',"
        "'--directory',str(d/'images'),'--pid',(d/'live-worker-pid').read_text().strip(),"
        "'--process-tree'],check=True)"
    )
    result = call(
        ["exec", args.pod, "-c", "runtime", "--", "python3", "-c", code],
        timeout=900,
        check=False,
    )
    (args.output / "capture.log").write_text(result.stdout + result.stderr)
    if result.stdout.lstrip().startswith("{"):
        save("capture.json", json.JSONDecoder().raw_decode(result.stdout.lstrip())[0])
    for filename in ("compatibility.json", "dump.log", "filesystem.json"):
        captured = call(
            [
                "exec",
                args.pod,
                "-c",
                "runtime",
                "--",
                "cat",
                f"/checkpoints/{args.run}/images/{filename}",
            ],
            check=False,
        )
        (args.output / filename).write_text(captured.stdout + captured.stderr)
    result.check_returncode()
    print(
        json.dumps({"pod": args.pod, "status": "captured", "completed_at": utc()}),
        flush=True,
    )


if __name__ == "__main__":
    main()
