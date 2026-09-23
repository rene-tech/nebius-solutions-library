"""Bounded, streaming workspace inventories; never load a trajectory in RAM."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
from pathlib import Path
from typing import Any

from .contracts import relative_path


def media_type(path: str) -> str:
    # Native filenames are recorded separately in the result/manifest. One
    # byte-identical object can have several names (including different suffixes),
    # so its immutable content address must not acquire conflicting MIME types.
    return "application/octet-stream"


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def inventory(root: Path, *, max_bytes: int) -> list[dict[str, Any]]:
    total = 0
    files = []
    for path in sorted(root.rglob("*")):
        meta = path.lstat()
        if stat.S_ISDIR(meta.st_mode):
            continue
        if not stat.S_ISREG(meta.st_mode):
            raise ValueError("workflow outputs must be regular files, not links or devices")
        total += meta.st_size
        if total > max_bytes or len(files) >= 9998:
            raise ValueError("workflow exceeds the workspace file/byte budget")
        sha = digest_file(path)
        after = path.lstat()
        if (meta.st_ino, meta.st_size, meta.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("workflow file changed during its checkpoint inventory")
        files.append({"path": str(path.relative_to(root)), "size_bytes": meta.st_size, "sha256": sha})
    return files


def extract_inputs(archive: Path, destination: Path, *, max_bytes: int) -> None:
    """Extract regular bundle members without materializing the archive in RAM."""
    destination.mkdir(parents=True, exist_ok=False)
    total, count, seen = 0, 0, set()
    with tarfile.open(archive, "r|gz") as source:
        for member in source:
            name = relative_path(member.name.rstrip("/"))
            if name in seen or not (member.isdir() or member.isfile()):
                raise ValueError("input bundle has a duplicate, link or non-regular member")
            seen.add(name)
            total += member.size
            count += 1
            if total > max_bytes or count > 9998:
                raise ValueError("expanded input bundle exceeds the execution workspace budget")
            target = destination / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = source.extractfile(member)
            if handle is None:
                raise ValueError("input archive regular file has no content")
            with handle, target.open("xb") as output:
                remaining = member.size
                while remaining:
                    block = handle.read(min(4 * 1024 * 1024, remaining))
                    if not block:
                        raise ValueError("input archive member is truncated")
                    output.write(block)
                    remaining -= len(block)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
