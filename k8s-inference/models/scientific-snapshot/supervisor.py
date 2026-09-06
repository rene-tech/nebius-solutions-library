#!/usr/bin/env python3
"""Keep PID 1 alive while a request-ready child is captured or restored.

The durable directory and the runtime command are explicit configuration. The
supervisor never restarts a captured worker: Kubernetes deletion releases the
donor GPU; a different pod acquires a GPU and restores the saved child.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def configure_runtime_cache(directory: Path) -> None:
    """Bind executable runtime caches to storage that survives the donor pod."""
    cache_root = directory / "cache"
    cache_root.mkdir(exist_ok=True)
    for variable, cache_directory in {
        "XDG_CACHE_HOME": cache_root,
        "TORCHINDUCTOR_CACHE_DIR": cache_root / "torchinductor",
        "TRITON_CACHE_DIR": cache_root / "triton",
        "TORCH_EXTENSIONS_DIR": cache_root / "torch-extensions",
        "CUDA_CACHE_PATH": cache_root / "cuda",
    }.items():
        cache_directory.mkdir(exist_ok=True)
        os.environ[variable] = str(cache_directory)


def stop_restored_worker(directory: Path) -> None:
    marker = directory / "images" / "worker-pid"
    if not marker.is_file():
        return
    pid = int(marker.read_text())
    if pid <= 1:
        raise ValueError("saved worker must be a child process")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("donor", "restore"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--fallback", choices=("normal-load", "fail"), default="normal-load")
    parser.add_argument("--allow-device-remap", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    # JIT launchers are file-backed executable mappings in a checkpoint. They
    # must outlive the donor container just like the checkpoint pages do.
    configure_runtime_cache(args.directory)
    Path("/tmp/empty-criu-plugins").mkdir(exist_ok=True)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.mode == "donor":
        if not command:
            parser.error("donor requires the runtime command after --")
        # Reserve a modest PID range so the fresh restore supervisor/tooling
        # does not occupy the saved worker PID before CRIU recreates it.
        for _ in range(128):
            subprocess.run(["/bin/true"], check=True)
        with (args.directory / "worker.log").open("a") as log:
            child = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, cwd="/", env=os.environ.copy(),
            )
        (args.directory / "live-worker-pid").write_text(str(child.pid))
        print(json.dumps({"event": "worker_started", "pid": child.pid}), flush=True)
    else:
        helper = Path(__file__).with_name("process_checkpoint.py")
        restore_command = [
            sys.executable, str(helper), "restore", "--directory", str(args.directory / "images"),
        ]
        if args.allow_device_remap:
            restore_command.append("--allow-device-remap")
        result = subprocess.run(restore_command, check=False)
        restored = result.returncode == 0
        if not restored:
            stop_restored_worker(args.directory)
        if not restored and (not command or args.fallback == "fail"):
            raise SystemExit(result.returncode)
        if command:
            # The original scientific invocation remains the one-shot request.
            # Only its model loading is redirected into this pod's worker.
            environment = os.environ.copy()
            if restored:
                environment["FS2_ESMFOLD2_WORKER_URL"] = "http://127.0.0.1:8000"
            else:
                environment.pop("FS2_ESMFOLD2_WORKER_URL", None)
            print(json.dumps({
                "event": "scientific_snapshot_request",
                "mechanism": "cuda-criu-restored" if restored else "normal-load-fallback",
            }), flush=True)
            child = subprocess.Popen(command, env=environment, start_new_session=True)

            def forward_signal(signum, _frame):
                try:
                    os.killpg(child.pid, signum)
                except ProcessLookupError:
                    pass
                raise SystemExit(128 + signum)

            signal.signal(signal.SIGTERM, forward_signal)
            signal.signal(signal.SIGINT, forward_signal)
            try:
                raise SystemExit(child.wait())
            finally:
                if restored:
                    stop_restored_worker(args.directory)
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    while True:
        # Reap captured/terminated descendants; restored workers can be
        # reparented to this PID-1 supervisor after CRIU detaches.
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        time.sleep(1)


if __name__ == "__main__":
    main()
