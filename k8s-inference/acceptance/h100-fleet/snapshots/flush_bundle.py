#!/usr/bin/env python3
"""Flush only one copied snapshot bundle before declaring publication complete."""

import argparse
import json
import os
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory.resolve(strict=True)
    if not (root / "images/compatibility.json").is_file():
        parser.error("directory is not a captured bundle")
    started = time.monotonic()
    paths = list(root.rglob("*"))
    files = 0
    for path in paths:
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            files += 1
    for directory in [path for path in reversed(paths) if path.is_dir()] + [root]:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    print(json.dumps({"status": "flushed", "files": files, "seconds": time.monotonic()-started}))


if __name__ == "__main__":
    main()
