"""Bounded, streaming workspace inventories; never load a trajectory in RAM."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any

from .contracts import relative_path

MAX_WORKSPACE_FILES = 32766


def file_signature(meta: os.stat_result) -> tuple[int, ...]:
    """Include ctime: replacing bytes then restoring mtime must invalidate."""
    return (meta.st_dev, meta.st_ino, meta.st_mode, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)


class FileDigestCache:
    """Process-local hashes of unchanged closed files, never trusted after restart.

    Long simulations retain thousands of immutable segment files. Re-reading all
    earlier trajectory bytes at every five-minute checkpoint is quadratic I/O.
    Every use still checks file identity/metadata; changed files are fully hashed.
    The worker and companion own independent caches and verify independently.
    """

    def __init__(self, max_entries: int = MAX_WORKSPACE_FILES) -> None:
        if max_entries < 1:
            raise ValueError("digest cache requires a positive entry bound")
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[tuple[int, ...], str]] = OrderedDict()
        self._lock = Lock()

    def digest(self, path: Path) -> str:
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode):
            raise ValueError("workflow outputs must be regular files, not links or devices")
        signature = file_signature(meta)
        key = str(path.absolute())
        with self._lock:
            previous = self._entries.get(key)
        sha = previous[1] if previous is not None and previous[0] == signature else digest_file(path)
        if signature != file_signature(path.lstat()):
            raise ValueError("workflow file changed during its checkpoint inventory")
        with self._lock:
            # Filesystems may round ctime/mtime to one clock tick. A new file
            # rewritten in that tick can retain every stat field. Do not memoize
            # recently modified files; rehash them until the timestamp is old.
            if time.time_ns() - max(meta.st_ctime_ns, meta.st_mtime_ns) >= 1_000_000_000:
                self._entries[key] = (signature, sha)
                self._entries.move_to_end(key)
            else:
                self._entries.pop(key, None)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return sha


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


def inventory(
    root: Path, *, max_bytes: int, digest_cache: FileDigestCache | None = None
) -> list[dict[str, Any]]:
    total = 0
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        meta = path.lstat()
        if stat.S_ISDIR(meta.st_mode):
            continue
        if not stat.S_ISREG(meta.st_mode):
            raise ValueError("workflow outputs must be regular files, not links or devices")
        total += meta.st_size
        if total > max_bytes or len(files) >= MAX_WORKSPACE_FILES:
            raise ValueError("workflow exceeds the workspace file/byte budget")
        sha = digest_cache.digest(path) if digest_cache is not None else digest_file(path)
        after = path.lstat()
        if file_signature(meta) != file_signature(after):
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
            if total > max_bytes or count > MAX_WORKSPACE_FILES:
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
