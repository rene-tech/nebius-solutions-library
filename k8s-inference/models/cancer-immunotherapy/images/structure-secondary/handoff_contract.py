#!/usr/bin/env python3
"""Deterministic, relocatable archives for runtime-specific stage handoffs.

The provenance envelope is intentionally owned by each runtime.  A generic
four-field marker cannot bind the parameters and reference artifacts which
make a prepared scientific input safe to reuse.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _absolute(path: str | Path, label: str, *, existing: bool) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or (existing and not candidate.is_file()):
        suffix = " existing file" if existing else " path"
        raise SystemExit(f"{label} must be an absolute{suffix}: {candidate}")
    return candidate


def write_archive(handoff_tar: Path, members: dict[str, Path]) -> None:
    """Write an exact, deterministic zstd tar from canonical member names."""
    handoff_tar = _absolute(handoff_tar, "handoff-tar", existing=False)
    if not members or any(not name or Path(name).is_absolute() or ".." in Path(name).parts for name in members):
        raise SystemExit("handoff archive member names must be safe and relative")
    if any(not path.is_file() for path in members.values()):
        raise SystemExit("handoff archive members must be existing files")
    output_identity = handoff_tar.resolve(strict=False)
    member_identities = [path.resolve(strict=True) for path in members.values()]
    if output_identity in member_identities:
        raise SystemExit("handoff-tar must be distinct from every archive member")
    if len(set(member_identities)) != len(member_identities):
        raise SystemExit("handoff archive members must refer to distinct files")
    handoff_tar.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".fs2-handoff-", suffix=".tar", dir=handoff_tar.parent, delete=False
    ) as temporary:
        raw_tar = Path(temporary.name)
    with tempfile.NamedTemporaryFile(
        prefix=f".{handoff_tar.name}.", suffix=".partial", dir=handoff_tar.parent, delete=False
    ) as temporary:
        compressed_tar = Path(temporary.name)
    try:
        with tarfile.open(raw_tar, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for member_name, path in members.items():
                info = tarfile.TarInfo(member_name)
                info.size = path.stat().st_size
                info.mode = 0o444
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
        completed = subprocess.run(
            [
                "zstd",
                "-q",
                "-f",
                "--threads=1",
                "-o",
                str(compressed_tar),
                str(raw_tar),
            ],
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(completed.returncode)
        with compressed_tar.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(compressed_tar, handoff_tar)
        directory_fd = os.open(handoff_tar.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        raw_tar.unlink(missing_ok=True)
        compressed_tar.unlink(missing_ok=True)
