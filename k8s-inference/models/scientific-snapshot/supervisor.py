#!/usr/bin/env python3
"""Keep PID 1 alive while a request-ready child is captured or restored.

The durable directory and the runtime command are explicit configuration. The
supervisor never restarts a captured worker: Kubernetes deletion releases the
donor GPU; a different pod acquires a GPU and restores the saved child.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
import uuid


def bind_allocated_gpu() -> None:
    """Keep a CRIU-privileged worker on the device allocated by Kubernetes.

    Privileged containers can see host device nodes outside the device-plugin
    allocation. CUDA must therefore select the assigned UUID explicitly; the
    checkpoint helper and model worker must refer to the same single GPU.
    """
    assigned = os.environ.get("NVIDIA_VISIBLE_DEVICES", "")
    if not assigned.startswith("GPU-") or "," in assigned:
        # CDI may clear this environment variable in the initial process.
        # Resolve the one device exposed by the device plugin instead. Never
        # select an arbitrary host index when multiple devices are visible.
        driver = ctypes.CDLL("libcuda.so.1")
        count = ctypes.c_int()
        if driver.cuInit(0) or driver.cuDeviceGetCount(ctypes.byref(count)) or count.value != 1:
            raise ValueError("snapshot worker requires exactly one accessible allocated GPU")
        identity = (ctypes.c_ubyte * 16)()
        if driver.cuDeviceGetUuid(ctypes.byref(identity), 0):
            raise ValueError("cannot resolve the allocated GPU UUID")
        assigned = "GPU-" + str(uuid.UUID(bytes=bytes(identity)))
    os.environ["NVIDIA_VISIBLE_DEVICES"] = assigned
    os.environ["CUDA_VISIBLE_DEVICES"] = assigned


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
        "CUEQ_TRITON_CACHE_DIR": cache_root / "cueq-triton",
        # FlashInfer does not follow XDG_CACHE_HOME; its generated-code/log
        # root would otherwise disappear with the donor container.
        "FLASHINFER_WORKSPACE_BASE": cache_root / "flashinfer-workspace",
    }.items():
        cache_directory.mkdir(exist_ok=True)
        os.environ[variable] = str(cache_directory)


def stop_restored_worker(directory: Path) -> None:
    """Release the entire restored cohort before ordinary fallback or exit.

    A partial multi-process restore can reparent children to PID 1. Killing
    only the API parent would leave its CUDA engine consuming the allocated
    GPU while the normal fallback starts. All saved PIDs belong to this
    isolated container PID namespace; PID 1 is never a worker.
    """
    marker = directory / "images" / "worker-pid"
    if not marker.is_file():
        return
    pid = int(marker.read_text())
    if pid <= 1:
        raise ValueError("saved worker must be a child process")
    cohort = [pid]
    for name in ("process-pids.json", "cuda-pids.json"):
        record = marker.with_name(name)
        if record.is_file():
            cohort = [*json.loads(record.read_text()), *cohort]
    if any(not isinstance(item, int) or item <= 1 for item in cohort):
        raise ValueError("saved worker cohort must contain only child processes")
    for item in dict.fromkeys(cohort):
        try:
            os.kill(item, signal.SIGKILL)
        except ProcessLookupError:
            pass


def prepare_restore_scratch(source: Path, directory: Path) -> None:
    """Copy tiny mutable runtime files, never the shared checkpoint pages."""
    shutil.copytree(source / "cache", directory / "cache")
    shutil.copy2(source / "worker.log", directory / "worker.log")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("donor", "restore"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--fallback", choices=("normal-load", "fail"), default="normal-load")
    parser.add_argument("--allow-device-remap", action="store_true")
    parser.add_argument("--source-directory", type=Path, help="read-only captured bundle; images mounted separately")
    parser.add_argument("--request-uid", type=int)
    parser.add_argument("--request-gid", type=int)
    parser.add_argument("--request-mode", choices=("one-shot", "server"), default="one-shot")
    parser.add_argument("--worker-url-variable", choices=(
        "FS2_ESMFOLD2_WORKER_URL", "FS2_PROTENIX_WORKER_URL", "FS2_MOSAIC_WORKER_URL",
    ), default="FS2_ESMFOLD2_WORKER_URL")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    bind_allocated_gpu()
    args.directory.mkdir(parents=True, exist_ok=True)
    # JIT launchers are file-backed executable mappings in a checkpoint. They
    # must outlive the donor container just like the checkpoint pages do.
    if (args.request_uid is None) != (args.request_gid is None):
        parser.error("request UID and GID must be supplied together")
    preparation_error = None
    if args.mode == "restore" and args.source_directory:
        try:
            prepare_restore_scratch(args.source_directory, args.directory)
        except (OSError, shutil.Error) as error:
            preparation_error = str(error)
    configure_runtime_cache(args.directory)
    if args.request_uid is not None:
        # Normal-load fallback runs as the same non-root scientific identity;
        # its JIT caches must be writable too, not only the output workspace.
        cache = args.directory / "cache"
        for path in (cache, *cache.rglob("*")):
            os.chown(path, args.request_uid, args.request_gid)
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
        result_code = 1
        if preparation_error:
            print(json.dumps({"event": "snapshot_scratch_unavailable", "error": preparation_error}), flush=True)
        else:
            result_code = subprocess.run(restore_command, check=False).returncode
        restored = result_code == 0
        if not restored:
            stop_restored_worker(args.directory)
        if not restored and (not command or args.fallback == "fail"):
            raise SystemExit(result_code)
        if command and args.request_mode == "one-shot":
            # The original scientific invocation remains the one-shot request.
            # Only its model loading is redirected into this pod's worker.
            environment = os.environ.copy()
            if restored:
                environment[args.worker_url_variable] = "http://127.0.0.1:8000"
            else:
                environment.pop(args.worker_url_variable, None)
            print(json.dumps({
                "event": "scientific_snapshot_request",
                "mechanism": "cuda-criu-restored" if restored else "normal-load-fallback",
            }), flush=True)
            child = subprocess.Popen(
                command, env=environment, start_new_session=True,
                user=args.request_uid, group=args.request_gid,
                extra_groups=[] if args.request_uid is not None else None,
            )

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
        elif args.request_mode == "server":
            print(json.dumps({
                "event": "serving_snapshot_runtime",
                "mechanism": "cuda-criu-restored" if restored else "normal-load-fallback",
            }), flush=True)
            if not restored:
                with (args.directory / "worker.log").open("a") as log:
                    child = subprocess.Popen(
                        command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        start_new_session=True, cwd="/", env=os.environ.copy(),
                    )
                (args.directory / "live-worker-pid").write_text(str(child.pid))
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
