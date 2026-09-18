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
from pathlib import Path
import sys
import urllib.request


def readiness(marker: Path, url: str) -> bool:
    if not marker.is_file():
        return False
    try:
        with urllib.request.urlopen(url, timeout=0.8) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def run(
    arguments, *, supervisor, filesystem, marker: Path, shared_memory=Path("/dev/shm")
):
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

    # This is the supervisor's Python event sink only. Worker stdout and the
    # frozen implementation remain unchanged; no model function is patched.
    previous_print = getattr(supervisor, "print", None)
    previous_argv = sys.argv
    supervisor.print = record_event
    sys.argv = [str(supervisor.__file__), *arguments]
    try:
        supervisor.main()
    finally:
        sys.argv = previous_argv
        if previous_print is None:
            del supervisor.print
        else:
            supervisor.print = previous_print
        log = Path("/tmp/fs2-checkpoint-work/restore.log")
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
