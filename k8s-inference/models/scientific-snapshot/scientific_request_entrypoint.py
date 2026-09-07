#!/usr/bin/env python3
"""Versioned one-shot bridge for additional scientific snapshot adapters.

The immutable captured supervisor/checkpoint code is imported, never rewritten.
This separate entrypoint only adds an explicit model-specific worker environment
name and keeps the original normal-loading invocation as the fallback.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import supervisor as lifecycle


def request_environment(environment, variable, restored):
    result = dict(environment)
    if restored:
        result[variable] = "http://127.0.0.1:8000"
    else:
        result.pop(variable, None)
    return result


def observed_startup(restored, *, bundle_id, manifest_sha256):
    return {
        "backend": "cuda-criu" if restored else "normal-load",
        "bundle_id": bundle_id,
        "manifest_sha256": manifest_sha256,
    }


def run_request(command, environment, *, uid, gid, directory, restored):
    child = subprocess.Popen(  # noqa: S603 - original admitted argv, no shell
        command,
        env=environment,
        start_new_session=True,
        user=uid,
        group=gid,
        extra_groups=[] if uid is not None else None,
    )
    old_handlers = {
        number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)
    }

    def forward(number, _frame):
        try:
            os.killpg(child.pid, number)
        except ProcessLookupError:
            pass
        raise SystemExit(128 + number)

    for number in old_handlers:
        signal.signal(number, forward)
    try:
        return child.wait()
    finally:
        if restored:
            lifecycle.stop_restored_worker(directory)
        for number, previous in old_handlers.items():
            signal.signal(number, previous)


def restore_runtime(args):
    lifecycle.bind_allocated_gpu()
    args.directory.mkdir(parents=True, exist_ok=True)
    preparation_error = None
    try:
        lifecycle.prepare_restore_scratch(args.source_directory, args.directory)
    except (OSError, shutil.Error) as error:
        preparation_error = str(error)
    lifecycle.configure_runtime_cache(args.directory)
    cache = args.directory / "cache"
    for path in (cache, *cache.rglob("*")):
        os.chown(path, args.request_uid, args.request_gid)
    Path("/tmp/empty-criu-plugins").mkdir(exist_ok=True)  # noqa: S108 - private per-Pod captured tmp mount
    result_code = 1
    if preparation_error:
        print(
            json.dumps(
                {"event": "snapshot_scratch_unavailable", "error": preparation_error}
            ),
            flush=True,
        )
    else:
        command = [
            sys.executable,
            str(Path(lifecycle.__file__).with_name("process_checkpoint.py")),
            "restore",
            "--directory",
            str(args.directory / "images"),
        ]
        if args.allow_device_remap:
            command.append("--allow-device-remap")
        result_code = subprocess.run(command, check=False).returncode  # noqa: S603 - exact frozen checkpoint helper
    if result_code:
        lifecycle.stop_restored_worker(args.directory)
    return result_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("restore",))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument(
        "--fallback", choices=("normal-load", "fail"), default="normal-load"
    )
    parser.add_argument("--allow-device-remap", action="store_true")
    parser.add_argument("--request-uid", type=int, required=True)
    parser.add_argument("--request-gid", type=int, required=True)
    parser.add_argument("--worker-url-variable", required=True)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not re.fullmatch(r"FS2_[A-Z0-9_]+_WORKER_URL", args.worker_url_variable):
        parser.error("worker environment name must identify an FS2 model worker")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("the original scientific invocation is required after --")
    restore_code = restore_runtime(args)
    restored = restore_code == 0
    if not restored and args.fallback == "fail":
        raise SystemExit(restore_code)
    print(
        json.dumps(
            {
                "event": "scientific_snapshot_request",
                "mechanism": "cuda-criu-restored"
                if restored
                else "normal-load-fallback",
            }
        ),
        flush=True,
    )
    environment = request_environment(os.environ, args.worker_url_variable, restored)
    # This is the completed restore result, never the selected policy. The
    # optional CLI overlay records it only after native output validation.
    environment["FS2_SCIENTIFIC_STARTUP_OBSERVATION"] = json.dumps(
        observed_startup(
            restored,
            bundle_id=args.bundle_id,
            manifest_sha256=args.manifest_sha256,
        )
    )
    raise SystemExit(
        run_request(
            command,
            environment,
            uid=args.request_uid,
            gid=args.request_gid,
            directory=args.directory,
            restored=restored,
        )
    )


if __name__ == "__main__":
    main()
