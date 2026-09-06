#!/usr/bin/env python3
"""Single-process CUDA + CRIU lifecycle using the existing pinned FS2 tools.

Run in the isolated donor/restore pod, with identical runtime/artifact mounts
and a durable checkpoint directory. The worker process must be quiescent.
Kubernetes owns GPU allocation: delete the donor pod after capture, and create
a fresh GPU pod before restore. This helper does not bypass the scheduler.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def runtime_identity() -> dict:
    image = os.environ.get("FS2_SNAPSHOT_RUNTIME_IMAGE", "")
    tools_image = os.environ.get("FS2_SNAPSHOT_TOOLS_IMAGE", "")
    if "@sha256:" not in image or "@sha256:" not in tools_image:
        raise ValueError("snapshot capture/restore requires immutable runtime and tools image identities")
    command = ["nvidia-smi", "--query-gpu=uuid,name,driver_version,compute_cap", "--format=csv,noheader,nounits"]
    visible = os.environ.get("NVIDIA_VISIBLE_DEVICES", "")
    if visible.startswith("GPU-"):
        command.extend(("--id", visible))
    rows = subprocess.check_output(command, text=True).strip().splitlines()
    if len(rows) != 1:
        raise ValueError("this snapshot lane requires exactly one assigned GPU")
    uuid, name, driver, capability = (value.strip() for value in rows[0].split(","))
    return {
        "runtime_image": image, "tools_image": tools_image, "kernel_release": os.uname().release,
        "runtime_id": os.environ.get("FS2_RUNTIME_ID", ""),
        "model_revision": os.environ.get("FS2_MODEL_REVISION", ""),
        "gpu_uuid": uuid, "gpu_name": name, "driver_version": driver, "compute_capability": capability,
    }


def generated_cache_manifest(directory: Path) -> list[dict]:
    root = directory.parent / "cache"
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            records.append({"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": digest})
    return records


def validate_generated_cache(directory: Path, expected: list[dict]) -> None:
    """Require captured executable files unchanged; permit new request kernels."""
    observed = {record["path"]: record for record in generated_cache_manifest(directory)}
    if any(observed.get(record["path"]) != record for record in expected):
        raise ValueError("snapshot executable cache is missing or differs")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "restore"))
    parser.add_argument("--pid", type=int)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--tools", type=Path, default=Path("/tools"))
    parser.add_argument("--restore-work-directory", type=Path, default=Path("/tmp/fs2-checkpoint-work"))
    parser.add_argument("--allow-device-remap", action="store_true", help="opt in only after cross-GPU qualification")
    args = parser.parse_args()
    environment = os.environ.copy()
    # The reused CRIU tools are Ubuntu 24.04; scientific images can be 22.04.
    # Run CRIU under its own loader/libc, without changing the model process's
    # libraries or installing anything on the host.
    criu = [
        str(args.tools / "lib" / "ld-linux-x86-64.so.2"),
        "--library-path", str(args.tools / "lib"), str(args.tools / "criu"),
    ]
    records = []

    def run(command: list[str]) -> None:
        started = time.monotonic()
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=600)
        records.append({
            "command": command, "seconds": time.monotonic() - started,
            "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
        })
        if result.returncode:
            raise RuntimeError(f"command failed: {command[0]} {command[1]}")

    receipt = {"action": args.action, "records": records, "status": "running"}
    cuda_state = "running"
    try:
        identity = runtime_identity()
        receipt["runtime_identity"] = identity
        if args.action == "capture":
            if args.pid is None or args.pid <= 1:
                parser.error("capture requires a child worker PID")
            args.directory.mkdir(parents=True, exist_ok=False)
            for action in ("lock", "checkpoint"):
                run([str(args.tools / "cuda-checkpoint"), "--action", action, "--pid", str(args.pid)])
                cuda_state = "locked" if action == "lock" else "checkpointed"
            (args.directory / "worker-pid").write_text(str(args.pid))
            (args.directory / "compatibility.json").write_text(json.dumps({
                "schema": "fs2-serve.nebius.ai/scientific-process-checkpoint/v1",
                "runtime_identity": identity, "generated_cache": generated_cache_manifest(args.directory),
            }, sort_keys=True))
            run([
                *criu, "dump", "--tree", str(args.pid),
                "--images-dir", str(args.directory), "--shell-job", "--log-file", "dump.log",
                "-v4", "--file-locks", "--tcp-established",
                "--manage-cgroups=ignore", "--libdir", "/tmp/empty-criu-plugins",
            ])
            # CRIU closes files without waiting for every dirty page. Complete
            # this checkpoint's writes before deleting the last PVC consumer;
            # otherwise CSI unmount flush time is charged to the next restore,
            # and a preemption could occur before the checkpoint is durable.
            started = time.monotonic()
            paths = [*args.directory.iterdir(), *(args.directory.parent / "cache").rglob("*")]
            for path in paths:
                if path.is_file():
                    with path.open("rb") as checkpoint:
                        os.fsync(checkpoint.fileno())
            directories = [path for path in paths if path.is_dir()]
            directories.extend((args.directory, args.directory.parent / "cache", args.directory.parent))
            for directory in directories:
                if not directory.is_dir():
                    continue
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            receipt["checkpoint_flush_seconds"] = time.monotonic() - started
        else:
            compatibility = json.loads((args.directory / "compatibility.json").read_text())
            if compatibility.get("schema") != "fs2-serve.nebius.ai/scientific-process-checkpoint/v1":
                raise ValueError("snapshot compatibility manifest version is unsupported")
            saved = compatibility["runtime_identity"]
            if {key: value for key, value in saved.items() if key != "gpu_uuid"} != {
                key: value for key, value in identity.items() if key != "gpu_uuid"
            }:
                raise ValueError("snapshot runtime, tools, model, driver, kernel or GPU type differs")
            remap = saved["gpu_uuid"] != identity["gpu_uuid"]
            if remap and not args.allow_device_remap:
                raise ValueError("snapshot requires another GPU UUID; device remapping is not qualified/enabled")
            validate_generated_cache(args.directory, compatibility["generated_cache"])
            args.restore_work_directory.mkdir(parents=True, exist_ok=True)
            run([
                *criu, "restore", "--images-dir", str(args.directory),
                "--work-dir", str(args.restore_work_directory),
                "--shell-job", "--restore-detached", "--log-file", "restore.log", "-v4",
                "--file-locks", "--tcp-established", "--manage-cgroups=ignore",
                "--libdir", "/tmp/empty-criu-plugins",
            ])
            pid = int((args.directory / "worker-pid").read_text())
            for action in ("restore", "unlock"):
                command = [str(args.tools / "cuda-checkpoint"), "--action", action, "--pid", str(pid)]
                if action == "restore" and remap:
                    command.extend(("--device-map", f"{saved['gpu_uuid']}={identity['gpu_uuid']}"))
                run(command)
        receipt["status"] = "passed"
    except Exception as error:
        receipt["status"] = "failed"
        receipt["error"] = str(error)
        # CRIU normally leaves a failed dump's process alive. Resume our own
        # donor where possible, so a filesystem error does not strand a locked
        # CUDA context until the operator deletes the disposable pod.
        if args.action == "capture" and cuda_state != "running":
            try:
                recovery = ("restore", "unlock") if cuda_state == "checkpointed" else ("unlock",)
                for action in recovery:
                    run([str(args.tools / "cuda-checkpoint"), "--action", action, "--pid", str(args.pid)])
                receipt["donor_recovered"] = True
            except Exception as recovery_error:
                receipt["donor_recovered"] = False
                receipt["recovery_error"] = str(recovery_error)
    print(json.dumps(receipt, indent=2))
    if receipt["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
