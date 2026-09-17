#!/usr/bin/env python3
"""Bind the boundary root to the signed workloads backend and state lineage."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_BYTES = 1024 * 1024
PRIOR_HEAD_PATH = Path(
    "/var/lib/fs2-security-checkpoints-ro/customer-storage-egress-prior-head.json"
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


def _remote_object_version(config: dict[str, Any], expected_sha256: str) -> str:
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
        adapter = os.read(descriptor, before.st_size + 1)
        if (
            len(adapter) != before.st_size
            or hashlib.sha256(adapter).hexdigest() != expected_sha256
        ):
            raise ValueError("state-version adapter custody differs")
        result = subprocess.run(
            [
                f"/proc/self/fd/{descriptor}",
                "--bucket", str(config["bucket"]),
                "--key", str(config["key"]),
                "--region", str(config["region"]),
                "--endpoints-json", json.dumps(config["endpoints"], sort_keys=True),
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
        value = _object(result.stdout.encode(), "remote state object version")
        if set(value) != {"version_id"} or not isinstance(value["version_id"], str) or not value["version_id"]:
            raise ValueError("remote state object version is incomplete")
        return value["version_id"]
    finally:
        os.close(descriptor)


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
    version_before = _remote_object_version(
        config, str(custody["state_version_adapter_sha256"])
    )
    state_payload = _terraform(module, "state", "pull")
    state = _object(state_payload, "remote Terraform state")
    addresses = sorted(
        line.strip()
        for line in _terraform(module, "state", "list").decode().splitlines()
        if line.strip()
    )
    version_after = _remote_object_version(
        config, str(custody["state_version_adapter_sha256"])
    )
    backend_lineage = hashlib.sha256(
        _canonical({"backend_config_sha256": backend_sha256, "state_lineage": state.get("lineage")})
    ).hexdigest()
    if (
        version_before != version_after
        or not addresses
        or addresses != sorted(set(addresses))
        or backend_lineage != custody.get("backend_lineage")
        or state.get("lineage") != custody.get("state_lineage")
        or state.get("serial") != custody.get("state_serial")
        or not isinstance(state.get("serial"), int)
        or state["serial"] < 1
        or version_after != custody.get("state_version_id")
        or hashlib.sha256(state_payload).hexdigest()
        != custody.get("state_snapshot_sha256")
        or addresses != custody.get("managed_addresses")
    ):
        raise ValueError("live remote boundary state differs from signed custody")
    return {
        "authorized": "true",
        "backend_config_sha256": backend_sha256,
        "backend_lineage": backend_lineage,
        "state_lineage": str(state["lineage"]),
        "state_serial": str(state["serial"]),
        "state_version_id": version_after,
        "state_snapshot_sha256": hashlib.sha256(state_payload).hexdigest(),
        "managed_addresses_sha256": hashlib.sha256(
            _canonical(addresses)
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
        print(f"customer-storage workloads backend rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
