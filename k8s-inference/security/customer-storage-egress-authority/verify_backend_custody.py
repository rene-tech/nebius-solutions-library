#!/usr/bin/env python3
"""Bind this invocation to the externally custodied provider-state backend."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
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

STATE_VERSION_ADAPTER = Path("/usr/libexec/fs2-security/terraform-state-version")


def _open_fixed_adapter(path: Path) -> int:
    parts = path.parts[1:]
    if (
        not path.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("state-version adapter path is invalid")
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
        return os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
        )
    finally:
        os.close(directory_fd)


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


def _terraform(module: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["terraform", f"-chdir={module}", *arguments],
        check=False,
        capture_output=True,
        env={**os.environ, "TF_IN_AUTOMATION": "1"},
        timeout=60,
    )
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 64 * MAX_BYTES:
        raise ValueError("canonical remote Terraform state could not be read")
    return result.stdout


def _remote_state(module: Path) -> tuple[dict[str, Any], list[str], bytes]:
    payload = _terraform(module, "state", "pull")
    state = strict_json(payload, "remote Terraform state")
    addresses = sorted(
        line.strip()
        for line in _terraform(module, "state", "list").decode().splitlines()
        if line.strip()
    )
    if (
        not addresses
        or addresses != sorted(set(addresses))
        or not isinstance(state.get("serial"), int)
        or state["serial"] < 1
        or not isinstance(state.get("lineage"), str)
        or not state["lineage"]
        or not isinstance(state.get("version"), int)
        or state["version"] < 4
    ):
        raise ValueError("canonical remote Terraform state is empty or malformed")
    return state, addresses, payload


def _remote_object_version(backend: dict[str, Any], expected_adapter_sha256: str) -> str:
    descriptor = _open_fixed_adapter(STATE_VERSION_ADAPTER)
    try:
        before = os.fstat(descriptor)
        filesystem = os.fstatvfs(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_mode & 0o022
            or not before.st_mode & 0o111
            or before.st_size <= 0
            or before.st_size > MAX_BYTES
            or not filesystem.f_flag & getattr(os, "ST_RDONLY", 1)
        ):
            raise ValueError("state-version adapter custody differs")
        payload = os.read(descriptor, before.st_size + 1)
        if (
            len(payload) != before.st_size
            or hashlib.sha256(payload).hexdigest() != expected_adapter_sha256
        ):
            raise ValueError("state-version adapter custody differs")
        result = subprocess.run(
            [
                f"/proc/self/fd/{descriptor}",
                "--bucket", str(backend["config"]["bucket"]),
                "--key", str(backend["config"]["key"]),
                "--region", str(backend["config"]["region"]),
                "--endpoints-json", json.dumps(backend["config"]["endpoints"], sort_keys=True),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            pass_fds=(descriptor,),
        )
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or result.returncode != 0
            or len(result.stdout.encode()) > MAX_BYTES
        ):
            raise ValueError("remote state object version could not be read")
        value = strict_json(result.stdout.encode(), "remote state object version")
        if set(value) != {"version_id"} or not isinstance(value["version_id"], str) or not value["version_id"]:
            raise ValueError("remote state object version is incomplete")
        return value["version_id"]
    finally:
        os.close(descriptor)


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
    version_before = _remote_object_version(
        backend, str(custody["state_version_adapter_sha256"])
    )
    state, addresses, state_payload = _remote_state(module)
    backend_lineage = hashlib.sha256(
        canonical({"backend_config_sha256": backend_sha256, "state_lineage": state["lineage"]})
    ).hexdigest()
    version_after = _remote_object_version(
        backend, str(custody["state_version_adapter_sha256"])
    )
    if (
        version_before != version_after
        or backend_lineage != custody.get("backend_lineage")
        or state["lineage"] != custody.get("state_lineage")
        or state["serial"] != custody.get("state_serial")
        or version_after != custody.get("state_version_id")
        or hashlib.sha256(state_payload).hexdigest()
        != custody.get("state_snapshot_sha256")
        or addresses != custody.get("managed_addresses")
    ):
        raise ValueError("live remote provider state differs from signed custody")
    return {
        "authorized": "true",
        "backend_config_sha256": backend_sha256,
        "backend_lineage": backend_lineage,
        "state_lineage": state["lineage"],
        "state_serial": str(state["serial"]),
        "state_version_id": version_after,
        "state_snapshot_sha256": hashlib.sha256(state_payload).hexdigest(),
        "managed_addresses_sha256": hashlib.sha256(
            canonical(addresses)
        ).hexdigest(),
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        if not isinstance(query, dict) or set(query) != {"module_path"}:
            raise ValueError("backend custody query differs")
        print(json.dumps(verify(query["module_path"]), sort_keys=True))
        return 0
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"customer-storage provider backend rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
