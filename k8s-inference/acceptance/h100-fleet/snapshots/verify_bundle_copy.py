#!/usr/bin/env python3
"""Verify a task-owned snapshot copy and preserve captured filesystem metadata.

CRIU checks file modes as well as contents. Some cross-filesystem copies can
leave restrictive creation modes behind despite ``cp -a``. This publication
step compares every byte identity first, restores the captured mode/ownership,
then verifies the destination. It never changes the captured source bundle.
Run before hashing or publishing an immutable bundle, not against a live one.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import time


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(source, destination):
    source, destination = source.resolve(strict=True), destination.resolve(strict=True)
    if (
        source == destination
        or source in destination.parents
        or destination in source.parents
    ):
        raise ValueError("capture and copy must be separate bundle directories")
    if not (source / "images/compatibility.json").is_file():
        raise ValueError("source must be a captured snapshot bundle")
    original = {path.relative_to(source): path for path in source.rglob("*")}
    copied = {path.relative_to(destination): path for path in destination.rglob("*")}
    if original.keys() != copied.keys():
        raise ValueError("snapshot copy file set differs from capture")
    records, corrected = [], []
    # Read/compare every file before applying metadata changes.
    for relative, path in sorted(original.items()):
        before, after = path.lstat(), copied[relative].lstat()
        if stat.S_IFMT(before.st_mode) != stat.S_IFMT(after.st_mode):
            raise ValueError(f"snapshot entry type differs: {relative}")
        if not (
            stat.S_ISDIR(before.st_mode)
            or stat.S_ISREG(before.st_mode)
            or stat.S_ISSOCK(before.st_mode)
            or stat.S_ISFIFO(before.st_mode)
        ):
            raise ValueError(f"unsupported snapshot entry type: {relative}")
        row = {
            "path": str(relative),
            "mode": stat.S_IMODE(before.st_mode),
            "uid": before.st_uid,
            "gid": before.st_gid,
        }
        if stat.S_ISREG(before.st_mode):
            if before.st_size != after.st_size:
                raise ValueError(f"snapshot file size differs: {relative}")
            row.update(bytes=before.st_size, sha256=digest(path))
            if digest(copied[relative]) != row["sha256"]:
                raise ValueError(f"snapshot file content differs: {relative}")
        records.append(row)
    for row in records:
        path = destination / row["path"]
        before = path.stat()
        expected = (row["mode"], row["uid"], row["gid"])
        if (stat.S_IMODE(before.st_mode), before.st_uid, before.st_gid) != expected:
            os.chown(path, row["uid"], row["gid"])
            os.chmod(path, row["mode"])
            after = path.stat()
            if (stat.S_IMODE(after.st_mode), after.st_uid, after.st_gid) != expected:
                raise ValueError(
                    f"captured snapshot metadata was not preserved: {row['path']}"
                )
            corrected.append(row["path"])
    return {
        "status": "passed",
        "entries": len(records),
        "metadata_corrected": corrected,
        "files": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    result = verify(args.source, args.destination)
    result["seconds"] = time.monotonic() - started
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
