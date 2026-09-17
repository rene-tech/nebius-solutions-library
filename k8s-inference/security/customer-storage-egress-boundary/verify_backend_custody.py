#!/usr/bin/env python3
"""Bind the boundary root to the signed workloads backend and state lineage."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

MAX_BYTES = 1024 * 1024
PRIOR_HEAD_PATH = Path(
    "/var/lib/fs2-security-checkpoints-ro/customer-storage-egress-prior-head.json"
)


def _read(path: Path, *, root_owned: bool) -> bytes:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("backend custody path is invalid")
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
        )
    finally:
        os.close(directory_fd)
    try:
        before = os.fstat(descriptor)
        filesystem = os.fstatvfs(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > MAX_BYTES
            or (root_owned and (before.st_uid != 0 or before.st_mode & 0o077))
            or (root_owned and not filesystem.f_flag & getattr(os, "ST_RDONLY", 1))
        ):
            raise ValueError("backend custody input is not a bounded private regular file")
        payload = b""
        while len(payload) <= MAX_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if (
            len(payload) > MAX_BYTES
            or len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("backend custody input changed during its read")
        return payload
    finally:
        os.close(descriptor)


def _object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def verify(module_path: str) -> dict[str, str]:
    module = Path(module_path)
    if not module.is_absolute() or ".." in module.parts:
        raise ValueError("module path must be absolute without traversal")
    prior = _object(_read(PRIOR_HEAD_PATH, root_owned=True), "signed prior head")
    custody = prior.get("boundary_state_custody")
    if not isinstance(custody, dict):
        raise ValueError("signed boundary state custody is absent")
    metadata = _object(
        _read(module / ".terraform" / "terraform.tfstate", root_owned=False),
        "Terraform backend metadata",
    )
    backend = metadata.get("backend")
    if not isinstance(backend, dict) or set(backend) != {"type", "config", "hash"}:
        raise ValueError("Terraform backend metadata fields differ")
    config = backend.get("config")
    if backend.get("type") != "s3" or not isinstance(config, dict):
        raise ValueError("canonical workloads state requires the S3 backend")
    for field in ("bucket", "key", "region", "endpoints"):
        if (
            field not in config
            or config[field] is None
            or config[field] == ""
            or config[field] == {}
        ):
            raise ValueError("canonical workloads backend identity is incomplete")
    if config.get("use_lockfile") is not True:
        raise ValueError("canonical workloads backend must use native state locking")
    backend_sha256 = hashlib.sha256(
        _canonical({"type": backend["type"], "config": config})
    ).hexdigest()
    if backend_sha256 != custody.get("backend_config_sha256"):
        raise ValueError("loaded Terraform backend differs from signed boundary custody")
    return {
        "authorized": "true",
        "backend_config_sha256": backend_sha256,
        "backend_lineage": str(custody["backend_lineage"]),
        "state_lineage": str(custody["state_lineage"]),
        "state_serial": str(custody["state_serial"]),
        "managed_addresses_sha256": hashlib.sha256(
            _canonical(custody["managed_addresses"])
        ).hexdigest(),
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        if not isinstance(query, dict) or set(query) != {"module_path"}:
            raise ValueError("backend custody query differs")
        print(json.dumps(verify(query["module_path"]), sort_keys=True))
        return 0
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"customer-storage workloads backend rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
