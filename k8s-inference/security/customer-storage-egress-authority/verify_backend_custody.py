#!/usr/bin/env python3
"""Bind this invocation to the externally custodied provider-state backend."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from verify_authority_ledger import (
    MAX_BYTES,
    PRIOR_HEAD_PATH,
    canonical,
    safe_root_read,
    strict_json,
)


def _read_backend_metadata(module_path: Path) -> dict[str, Any]:
    metadata_path = module_path / ".terraform" / "terraform.tfstate"
    parts = metadata_path.parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Terraform backend metadata path is invalid")
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
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > MAX_BYTES
        ):
            raise ValueError("Terraform backend metadata is not a bounded regular file")
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
            raise ValueError("Terraform backend metadata changed during its read")
    finally:
        os.close(descriptor)
    metadata = strict_json(payload, "Terraform backend metadata")
    backend = metadata.get("backend")
    if not isinstance(backend, dict) or set(backend) != {"type", "config", "hash"}:
        raise ValueError("Terraform backend metadata fields differ")
    config = backend.get("config")
    if backend.get("type") != "s3" or not isinstance(config, dict):
        raise ValueError("canonical provider state requires the S3 backend")
    for field in ("bucket", "key", "region", "endpoints"):
        if (
            field not in config
            or config[field] is None
            or config[field] == ""
            or config[field] == {}
        ):
            raise ValueError("canonical provider backend identity is incomplete")
    if config.get("use_lockfile") is not True:
        raise ValueError("canonical provider backend must use native state locking")
    return {"type": backend["type"], "config": config}


def verify(module_path: str) -> dict[str, str]:
    module = Path(module_path)
    if not module.is_absolute() or ".." in module.parts:
        raise ValueError("module path must be absolute without traversal")
    prior = strict_json(safe_root_read(PRIOR_HEAD_PATH), "authority prior head")
    custody = prior.get("provider_state_custody")
    if not isinstance(custody, dict):
        raise ValueError("signed provider state custody is absent")
    backend = _read_backend_metadata(module)
    backend_sha256 = hashlib.sha256(canonical(backend)).hexdigest()
    if backend_sha256 != custody.get("backend_config_sha256"):
        raise ValueError("loaded Terraform backend differs from signed provider custody")
    return {
        "authorized": "true",
        "backend_config_sha256": backend_sha256,
        "backend_lineage": str(custody["backend_lineage"]),
        "state_lineage": str(custody["state_lineage"]),
        "state_serial": str(custody["state_serial"]),
        "managed_addresses_sha256": hashlib.sha256(
            canonical(custody["managed_addresses"])
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
        print(f"customer-storage provider backend rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
