"""Create the immutable scientific-cache writer lock before fence activation.

This is a deliberately separate, non-migrating preparation phase. It creates
one root-owned mode-0444 inode with O_EXCL, fsyncs its canonical bytes, and
prints only the non-secret identity an independent observer must bind into the
subsequent quiescence record. It never replaces, truncates, or removes an
existing inode.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any


CACHE_ROOT = Path("/cache")
CONTRACT_ENV = "FS2_SCIENTIFIC_RUNTIME_CACHE_WRITER_LOCK_JSON"
CONTRACT_SCHEMA = "fs2-serve.nebius.ai/scientific-runtime-cache-writer-lock/v1"
LOCK_NAME = ".fs2-cache-writer-admission.lock"


class WriterLockError(ValueError):
    """The pre-migration lock contract or existing inode is unsafe."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def initialize(contract: object, *, root: Path = CACHE_ROOT) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise WriterLockError("writer-lock initialization requires its dedicated root identity")
    if not isinstance(contract, dict) or set(contract) != {
        "schema",
        "lease_name",
        "lease_uid",
        "activation_id",
    }:
        raise WriterLockError("writer-lock initialization fields differ")
    if (
        contract["schema"] != CONTRACT_SCHEMA
        or contract["lease_name"] != LOCK_NAME
        or not isinstance(contract["lease_uid"], str)
        or re.fullmatch(r"[a-f0-9]{64}", contract["lease_uid"]) is None
        or not isinstance(contract["activation_id"], str)
        or re.fullmatch(r"[a-f0-9]{64}", contract["activation_id"]) is None
    ):
        raise WriterLockError("writer-lock initialization identity is invalid")
    expected = _canonical(contract)
    root_fd = os.open(root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                LOCK_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o444,
                dir_fd=root_fd,
            )
            written = 0
            while written < len(expected):
                count = os.write(descriptor, expected[written:])
                if count <= 0:
                    raise WriterLockError("writer-lock initialization made no progress")
                written += count
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
            os.fsync(root_fd)
        except FileExistsError:
            descriptor = os.open(
                LOCK_NAME,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            actual = os.read(descriptor, len(expected) + 1)
            if actual != expected or os.read(descriptor, 1):
                raise WriterLockError("existing writer-lock bytes differ; replacement is forbidden")
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid != 0
            or status.st_gid != 0
            or stat.S_IMODE(status.st_mode) != 0o444
        ):
            raise WriterLockError("writer lock is not one root-owned immutable-mode inode")
        return {
            "schema": "fs2-serve.nebius.ai/scientific-runtime-cache-writer-lock-receipt/v1",
            "lease_name": LOCK_NAME,
            "lease_uid": contract["lease_uid"],
            "activation_id": contract["activation_id"],
            "lock_device": status.st_dev,
            "lock_inode": status.st_ino,
            "lock_content_sha256": hashlib.sha256(expected).hexdigest(),
        }
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(root_fd)


if __name__ == "__main__":
    raw = os.environ.get(CONTRACT_ENV)
    if raw is None:
        raise SystemExit(f"{CONTRACT_ENV} is required")
    receipt = initialize(json.loads(raw))
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
