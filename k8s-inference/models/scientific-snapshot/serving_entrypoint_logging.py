#!/usr/bin/env python3
"""Production serving boundary around an immutable captured source bundle.

Mount separately from /snapshot-source: the captured Python bytes remain exact.
The small readiness marker is emitted only after CUDA restore completes (or the
ordinary fallback is selected), then the original HTTP health must also pass.
Filesystem preparation errors obey the same Prefer/Require fallback policy as
the CUDA/CRIU supervisor. No model arguments or generation settings are changed.
"""

from __future__ import annotations

import builtins
import json
import os
import sys
import threading
import urllib.request
from pathlib import Path


def readiness(marker: Path, url: str) -> bool:
    if not marker.is_file():
        return False
    try:
        with urllib.request.urlopen(url, timeout=0.8) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


class WorkerLogBridge:
    """Follow appended worker bytes without reprinting the captured donor log.

    This lives outside the captured process. It does not modify its file
    descriptor, model code or checkpoint. Polling also handles replacement and
    truncation; shutdown drains a final partial line rather than losing errors.
    """

    def __init__(self, path: Path, *, historical_bytes: int = 0, interval=0.1):
        self.path = path
        self.offset = historical_bytes
        self.identity = None
        self.interval = interval
        self.pending = b""
        self.stop_event = threading.Event()
        self.thread = None

    def emit(self, raw):
        builtins.print(
            json.dumps({"event": "snapshot_worker_log", "message": raw.decode("utf-8", errors="replace")}),
            flush=True,
        )

    def drain(self, *, final=False):
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if self.identity is not None and (identity != self.identity or stat.st_size < self.offset):
                    if self.pending:
                        self.emit(self.pending)
                    self.pending = b""
                    self.offset = 0
                self.identity = identity
                stream.seek(self.offset)
                while chunk := stream.read(65536):
                    self.offset += len(chunk)
                    lines = (self.pending + chunk).split(b"\n")
                    self.pending = lines.pop()
                    for line in lines:
                        self.emit(line)
                    # Preserve unusually long messages in bounded chunks.
                    if len(self.pending) >= 65536:
                        self.emit(self.pending)
                        self.pending = b""
        except FileNotFoundError:
            pass
        if final and self.pending:
            self.emit(self.pending)
            self.pending = b""

    def start(self):
        def follow():
            while not self.stop_event.wait(self.interval):
                self.drain()
            self.drain(final=True)

        self.thread = threading.Thread(target=follow, name="snapshot-worker-log", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        else:
            self.drain(final=True)


def worker_log_bridge(arguments):
    if "--directory" not in arguments:
        return None
    path = Path(arguments[arguments.index("--directory") + 1]) / "worker.log"
    historical = path.stat().st_size if path.is_file() else 0
    if "restore" in arguments and "--source-directory" in arguments:
        source = Path(arguments[arguments.index("--source-directory") + 1]) / "worker.log"
        if source.is_file():
            historical = source.stat().st_size
    return WorkerLogBridge(path, historical_bytes=historical)


def run(arguments, *, supervisor, filesystem, marker: Path, shared_memory=Path("/dev/shm")):
    arguments = list(arguments)
    marker.unlink(missing_ok=True)
    if "restore" in arguments:
        before = set(shared_memory.iterdir())
        try:
            source = Path(arguments[arguments.index("--source-directory") + 1])
            filesystem.restore(source, shared_memory)
        except (OSError, ValueError, KeyError) as error:
            fallback = arguments[arguments.index("--fallback") + 1]
            if fallback != "normal-load":
                raise
            # No model process exists yet. Remove only backing files created
            # by this attempted preparation in the container's own shm mount.
            for path in set(shared_memory.iterdir()) - before:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            builtins.print(
                json.dumps(
                    {
                        "event": "snapshot_filesystem_unavailable",
                        "fallback": "normal-load",
                        "error": str(error),
                    }
                ),
                flush=True,
            )
            arguments[arguments.index("restore")] = "donor"

    bridge = worker_log_bridge(arguments)

    def record_event(*values, **kwargs):
        builtins.print(*values, **kwargs)
        if len(values) != 1 or not isinstance(values[0], str):
            return
        try:
            event = json.loads(values[0])
        except ValueError:
            return
        if event.get("event") in {"serving_snapshot_runtime", "worker_started"}:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(event))
            # Scratch copying has completed before these supervisor events.
            # Starting here prevents seeing a partially copied donor log as a
            # truncation while retaining all appended worker startup messages.
            if bridge is not None and bridge.thread is None:
                if event.get("event") == "worker_started":
                    # Donor/normal-load did not copy a captured log.
                    bridge.offset = min(bridge.offset, bridge.path.stat().st_size) if bridge.path.is_file() else 0
                bridge.start()

    # This is the supervisor's Python event sink only. Worker stdout and the
    # frozen implementation remain unchanged; no model function is patched.
    previous_print = getattr(supervisor, "print", None)
    previous_argv = sys.argv
    supervisor.print = record_event
    sys.argv = [str(supervisor.__file__), *arguments]
    try:
        supervisor.main()
    finally:
        if bridge is not None:
            bridge.stop()
        sys.argv = previous_argv
        if previous_print is None:
            del supervisor.print
        else:
            supervisor.print = previous_print
        directory = (
            Path(arguments[arguments.index("--directory") + 1])
            if "--directory" in arguments
            else Path("/tmp/fs2-checkpoint-work")
        )
        log = directory / "images" / "restore.log"
        if log.is_file():
            for line in log.read_text(errors="replace").splitlines():
                if "Error" in line:
                    builtins.print("CRIU_RESTORE_ERROR " + line, flush=True)


def main():
    marker = Path(os.environ["FS2_SNAPSHOT_READY_FILE"])
    if sys.argv[1:] == ["ready"]:
        raise SystemExit(0 if readiness(marker, "http://127.0.0.1:8000/health") else 1)
    sys.path.insert(0, "/snapshot-source")
    import serving_filesystem
    import supervisor

    run(
        sys.argv[1:],
        supervisor=supervisor,
        filesystem=serving_filesystem,
        marker=marker,
    )


if __name__ == "__main__":
    main()
