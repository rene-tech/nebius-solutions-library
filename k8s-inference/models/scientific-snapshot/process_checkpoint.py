#!/usr/bin/env python3
"""Single-process CUDA + CRIU lifecycle using the existing pinned FS2 tools.

Run in the isolated donor/restore pod, with identical runtime/artifact mounts
and a durable checkpoint directory. The worker process must be quiescent.
Kubernetes owns GPU allocation: delete the donor pod after capture, and create
a fresh GPU pod before restore. This helper does not bypass the scheduler.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "restore"))
    parser.add_argument("--pid", type=int)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--tools", type=Path, default=Path("/tools"))
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
        if args.action == "capture":
            if args.pid is None or args.pid <= 1:
                parser.error("capture requires a child worker PID")
            args.directory.mkdir(parents=True, exist_ok=False)
            for action in ("lock", "checkpoint"):
                run([str(args.tools / "cuda-checkpoint"), "--action", action, "--pid", str(args.pid)])
                cuda_state = "locked" if action == "lock" else "checkpointed"
            (args.directory / "worker-pid").write_text(str(args.pid))
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
            for path in args.directory.iterdir():
                if path.is_file():
                    with path.open("rb") as checkpoint:
                        os.fsync(checkpoint.fileno())
            directory_fd = os.open(args.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            receipt["checkpoint_flush_seconds"] = time.monotonic() - started
        else:
            run([
                *criu, "restore", "--images-dir", str(args.directory),
                "--shell-job", "--restore-detached", "--log-file", "restore.log", "-v4",
                "--file-locks", "--tcp-established", "--manage-cgroups=ignore",
                "--libdir", "/tmp/empty-criu-plugins",
            ])
            pid = int((args.directory / "worker-pid").read_text())
            for action in ("restore", "unlock"):
                run([str(args.tools / "cuda-checkpoint"), "--action", action, "--pid", str(pid)])
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
