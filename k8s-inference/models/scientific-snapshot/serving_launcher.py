#!/usr/bin/env python3
"""Use the standard asyncio loop for a CRIU-compatible serving variant.

Current libuv event loops create io_uring mappings that pinned CRIU cannot
dump. This optional launcher changes only the CPU event-loop implementation;
the original installed server entrypoint, arguments, model, GPU kernels and
precision are retained. Normal and restored trials must both use this variant.
"""

import asyncio
import os
from pathlib import Path
import runpy
import shutil
import sys


def configure_loop():
    import uvloop

    uvloop.run = asyncio.run
    uvloop.new_event_loop = asyncio.new_event_loop
    uvloop.install = lambda: asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())


def main():
    configure_loop()
    # Spawned vLLM-Omni workers create their own loops. A scoped sitecustomize
    # propagates the same compatibility variant to those child interpreters.
    os.environ["FS2_SNAPSHOT_ASYNCIO_LOOP"] = "1"
    directory = str(Path(__file__).absolute().parent)
    os.environ["PYTHONPATH"] = directory + os.pathsep + os.environ.get("PYTHONPATH", "")
    command = sys.argv[1:]
    if command and Path(command[0]).name.startswith("python"):
        command = command[1:]
    if not command:
        raise ValueError("the exact original server entrypoint is required")
    executable = shutil.which(command[0])
    if not executable:
        raise ValueError("original server entrypoint is unavailable")
    sys.argv = [executable, *command[1:]]
    runpy.run_path(executable, run_name="__main__")


if __name__ == "__main__":
    main()
