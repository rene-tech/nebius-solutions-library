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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("donor", "restore"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    (args.directory / "cache").mkdir(exist_ok=True)
    os.environ.setdefault("XDG_CACHE_HOME", str(args.directory / "cache"))
    os.environ.setdefault("TORCHINDUCTOR_CACHE_DIR", str(args.directory / "cache" / "torchinductor"))
    Path("/tmp/empty-criu-plugins").mkdir(exist_ok=True)
    if args.mode == "donor":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
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
        result = subprocess.run([
            sys.executable, str(helper), "restore", "--directory", str(args.directory / "images"),
        ], check=False)
        if result.returncode:
            raise SystemExit(result.returncode)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
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
