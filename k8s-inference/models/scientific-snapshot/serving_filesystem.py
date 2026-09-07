#!/usr/bin/env python3
"""Preserve named POSIX shared-memory backing files for a stopped server.

CRIU restores process mappings, but named /dev/shm files also have a filesystem
identity. This is container-local shared memory, never host /dev/shm. Capture
is called only after the entire worker cohort has been successfully dumped and
stopped; restore verifies original contents before recreating any process.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat


def copy_sparse(source: Path, target: Path) -> None:
    """Keep large zero-filled shm extents sparse within the original shm size.

    vLLM creates logical 240MiB files while touching only a few MiB. A dense
    copy would exhaust the original 64MiB container shm even though the
    captured workload fitted. Seeking zero blocks retains identical bytes;
    it does not enlarge the Pod's shared-memory or memory limit.
    """
    with source.open("rb") as incoming, target.open("xb") as output:
        while chunk := incoming.read(1024 * 1024):
            if chunk.count(0) == len(chunk):
                output.seek(len(chunk), os.SEEK_CUR)
            else:
                output.write(chunk)
        output.truncate(source.stat().st_size)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def capture(bundle: Path, shared_memory: Path = Path("/dev/shm")) -> dict:
    destination = bundle / "filesystem" / "dev-shm"
    destination.mkdir(parents=True, exist_ok=False)
    files = []
    for path in sorted(shared_memory.iterdir()):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"unsupported shared-memory entry: {path.name}")
        target = destination / path.name
        copy_sparse(path, target)
        os.chmod(target, stat.S_IMODE(metadata.st_mode))
        os.chown(target, metadata.st_uid, metadata.st_gid)
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        files.append({"name": path.name, "bytes": metadata.st_size, "sha256": digest(target),
                      "uid": metadata.st_uid, "gid": metadata.st_gid,
                      "mode": stat.S_IMODE(metadata.st_mode)})
    manifest = {"schema": "fs2-serve.nebius.ai/serving-filesystem/v1", "files": files}
    marker = destination.parent / "manifest.json"
    marker.write_text(json.dumps(manifest, sort_keys=True))
    with marker.open("rb") as stream:
        os.fsync(stream.fileno())
    return manifest


def restore(bundle: Path, shared_memory: Path = Path("/dev/shm")) -> None:
    marker = bundle / "filesystem" / "manifest.json"
    manifest = json.loads(marker.read_bytes())
    if manifest["schema"] != "fs2-serve.nebius.ai/serving-filesystem/v1":
        raise ValueError("serving filesystem manifest differs")
    for record in manifest["files"]:
        name = record["name"]
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError("shared-memory backing file must have a contained name")
        source = marker.parent / "dev-shm" / name
        if source.is_symlink() or source.stat().st_size != record["bytes"] or digest(source) != record["sha256"]:
            raise ValueError("captured shared-memory backing content differs")
        target = shared_memory / name
        copy_sparse(source, target)
        os.chown(target, record["uid"], record["gid"])
        os.chmod(target, record["mode"])
