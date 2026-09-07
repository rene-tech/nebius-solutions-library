#!/usr/bin/env python3
"""Capture exact server processes plus their named container-local shm files."""

import json
from pathlib import Path
import subprocess
import sys

from serving_filesystem import capture


def main():
    arguments = sys.argv[1:]
    if not arguments or arguments[0] != "capture":
        raise ValueError("serving_checkpoint only captures a quiescent donor")
    directory = Path(arguments[arguments.index("--directory") + 1])
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("process_checkpoint.py")),
                             *arguments], check=False)
    if result.returncode:
        raise SystemExit(result.returncode)
    manifest = capture(directory.parent)
    print(json.dumps({"event": "serving_filesystem_captured", "files": manifest["files"]}), flush=True)


if __name__ == "__main__":
    main()
