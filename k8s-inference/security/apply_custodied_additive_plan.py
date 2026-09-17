#!/usr/bin/env python3
"""Apply only the exact externally approved additive Terraform plan.

This entry point deliberately has no plan-generation or cleanup mode.  An
external security owner must sign the exact saved-plan bytes plus both the
expected predecessor and successor remote-state identities.  Terraform then
executes the same descriptor-bound bytes that were verified.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_BYTES = 64 * 1024 * 1024
SAFE_ACTIONS = {(), ("no-op",), ("read",), ("create",)}
STATE_VERSION_ADAPTER = Path("/usr/libexec/fs2-security/terraform-state-version")


def _open(path: Path, *, root_owned: bool, executable: bool = False) -> tuple[int, bytes]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("custody path is invalid")
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
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    metadata = os.fstat(descriptor)
    filesystem = os.fstatvfs(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > MAX_BYTES
        or (root_owned and (metadata.st_uid != 0 or metadata.st_mode & 0o077))
        or (root_owned and not filesystem.f_flag & getattr(os, "ST_RDONLY", 1))
        or (executable and not metadata.st_mode & 0o111)
    ):
        os.close(descriptor)
        raise ValueError("custody input is not a bounded protected regular file")
    payload = b""
    while len(payload) <= MAX_BYTES:
        chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(payload)))
        if not chunk:
            break
        payload += chunk
    after = os.fstat(descriptor)
    if (
        len(payload) != metadata.st_size
        or (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        os.close(descriptor)
        raise ValueError("custody input changed during its descriptor-bound read")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor, payload


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


def _terraform(module: Path, *arguments: str, pass_fds: tuple[int, ...] = ()) -> bytes:
    result = subprocess.run(
        ["terraform", f"-chdir={module}", *arguments],
        check=False,
        capture_output=True,
        env={**os.environ, "TF_IN_AUTOMATION": "1"},
        timeout=900,
        pass_fds=pass_fds,
    )
    if result.returncode != 0 or len(result.stdout) > MAX_BYTES:
        raise ValueError("Terraform custody command failed")
    return result.stdout


def _backend(module: Path) -> dict[str, Any]:
    descriptor, payload = _open(
        module / ".terraform" / "terraform.tfstate", root_owned=False
    )
    try:
        metadata = _object(payload, "Terraform backend metadata")
    finally:
        os.close(descriptor)
    backend = metadata.get("backend")
    if not isinstance(backend, dict) or set(backend) != {"type", "config", "hash"}:
        raise ValueError("Terraform backend metadata fields differ")
    config = backend.get("config")
    if backend.get("type") != "s3" or not isinstance(config, dict):
        raise ValueError("custodied execution requires an S3 backend")
    for field in ("bucket", "key", "region", "endpoints"):
        if (
            field not in config
            or config[field] is None
            or config[field] == ""
            or config[field] == {}
        ):
            raise ValueError("Terraform backend identity is incomplete")
    if config.get("use_lockfile") is not True:
        raise ValueError("custodied execution requires native state locking")
    return {"type": backend["type"], "config": config}


def _state_version(backend: dict[str, Any], expected_adapter_sha256: str) -> str:
    descriptor, adapter = _open(STATE_VERSION_ADAPTER, root_owned=True, executable=True)
    try:
        if hashlib.sha256(adapter).hexdigest() != expected_adapter_sha256:
            raise ValueError("state-version adapter digest differs from execution approval")
        config = backend["config"]
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
        if result.returncode != 0 or len(result.stdout.encode()) > MAX_BYTES:
            raise ValueError("remote state version adapter failed")
        value = _object(result.stdout.encode(), "remote state version")
        if set(value) != {"version_id"} or not isinstance(value["version_id"], str) or not value["version_id"]:
            raise ValueError("remote state version is incomplete")
        return value["version_id"]
    finally:
        os.close(descriptor)


def _state(module: Path, expected_adapter_sha256: str) -> dict[str, Any]:
    backend = _backend(module)
    version_before = _state_version(backend, expected_adapter_sha256)
    payload = _terraform(module, "state", "pull")
    state = _object(payload, "remote state")
    addresses = sorted(
        line.strip()
        for line in _terraform(module, "state", "list").decode().splitlines()
        if line.strip()
    )
    if (
        not addresses
        or addresses != sorted(set(addresses))
        or not isinstance(state.get("lineage"), str)
        or not state["lineage"]
        or not isinstance(state.get("serial"), int)
        or state["serial"] < 1
        or not isinstance(state.get("version"), int)
        or state["version"] < 4
    ):
        raise ValueError("remote state has no canonical managed-address closure")
    version_after = _state_version(backend, expected_adapter_sha256)
    if version_before != version_after:
        raise ValueError("remote state changed during its custodied read")
    return {
        "backend_config_sha256": hashlib.sha256(_canonical(backend)).hexdigest(),
        "lineage": state.get("lineage"),
        "serial": state.get("serial"),
        "version_id": version_after,
        "snapshot_sha256": hashlib.sha256(payload).hexdigest(),
        "managed_addresses": addresses,
    }


def _source_identity(module: Path) -> tuple[str, str]:
    root = _terraform(module, "version")  # Proves the configured binary is callable before Git custody.
    if not root:
        raise ValueError("Terraform binary identity is absent")
    repository = subprocess.run(
        ["git", "-C", os.fspath(module), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if repository.returncode != 0:
        raise ValueError("module is not inside the custodied Git repository")
    repository_path = repository.stdout.strip()
    status = subprocess.run(
        ["git", "-C", repository_path, "status", "--porcelain=v1", "--untracked-files=all"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    identities = subprocess.run(
        ["git", "-C", repository_path, "rev-parse", "HEAD", "HEAD^{tree}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    values = identities.stdout.splitlines()
    if status.returncode != 0 or status.stdout or identities.returncode != 0 or len(values) != 2:
        raise ValueError("execution requires an exact clean Git commit and tree")
    return values[0], values[1]


def _verify_receipt(receipt: dict[str, Any], public_key: bytes) -> None:
    body = {key: value for key, value in receipt.items() if key not in {"payload_sha256", "signature"}}
    payload = _canonical(body)
    if receipt.get("payload_sha256") != hashlib.sha256(payload).hexdigest():
        raise ValueError("execution receipt payload digest differs")
    key = serialization.load_pem_public_key(public_key)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("execution approval key is not Ed25519")
    try:
        key.verify(base64.b64decode(receipt["signature"], validate=True), payload)
    except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
        raise ValueError("execution receipt signature is invalid") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args()
    if not args.module.is_absolute() or ".." in args.module.parts:
        parser.error("--module must be an absolute path without traversal")
    plan_fd = receipt_fd = key_fd = -1
    try:
        plan_fd, plan_bytes = _open(args.plan, root_owned=True)
        receipt_fd, receipt_bytes = _open(args.receipt, root_owned=True)
        key_fd, key_bytes = _open(args.public_key, root_owned=True)
        receipt = _object(receipt_bytes, "execution receipt")
        _verify_receipt(receipt, key_bytes)
        expected_fields = {
            "schema", "source_commit", "source_tree", "plan_sha256", "plan_json_sha256",
            "state_version_adapter_sha256",
            "prior_state", "successor_state_contract", "payload_sha256", "signature",
        }
        if set(receipt) != expected_fields or receipt.get("schema") != "fs2-serve.nebius.ai/additive-plan-execution/v1":
            raise ValueError("execution receipt fields or schema differ")
        adapter_sha256 = receipt.get("state_version_adapter_sha256")
        if (
            not isinstance(adapter_sha256, str)
            or len(adapter_sha256) != 64
            or any(character not in "0123456789abcdef" for character in adapter_sha256)
        ):
            raise ValueError("execution approval lacks the state-version adapter digest")
        if hashlib.sha256(plan_bytes).hexdigest() != receipt["plan_sha256"]:
            raise ValueError("saved-plan bytes differ from approval")
        source_commit, source_tree = _source_identity(args.module)
        if (source_commit, source_tree) != (receipt["source_commit"], receipt["source_tree"]):
            raise ValueError("Git source identity differs from execution approval")
        prior_state = _state(args.module, adapter_sha256)
        if prior_state != receipt["prior_state"]:
            raise ValueError("remote predecessor state differs from execution approval")
        plan_json = _terraform(args.module, "show", "-json", f"/proc/self/fd/{plan_fd}", pass_fds=(plan_fd,))
        if hashlib.sha256(plan_json).hexdigest() != receipt["plan_json_sha256"]:
            raise ValueError("saved-plan JSON differs from execution approval")
        plan = _object(plan_json, "saved plan")
        actions = {
            tuple(change.get("change", {}).get("actions", []))
            for change in plan.get("resource_changes", [])
        }
        if not actions <= SAFE_ACTIONS:
            raise ValueError("saved plan contains a non-additive action")
        successor_contract = receipt["successor_state_contract"]
        if (
            not isinstance(successor_contract, dict)
            or set(successor_contract) != {"backend_config_sha256", "lineage", "minimum_serial", "managed_addresses"}
            or successor_contract.get("backend_config_sha256") != prior_state["backend_config_sha256"]
            or successor_contract.get("lineage") != prior_state["lineage"]
            or successor_contract.get("minimum_serial") != prior_state["serial"] + 1
            or not isinstance(successor_contract.get("managed_addresses"), list)
            or not successor_contract["managed_addresses"]
            or successor_contract["managed_addresses"]
            != sorted(set(successor_contract["managed_addresses"]))
        ):
            raise ValueError("successor state contract is not canonical")
        _terraform(args.module, "apply", "-input=false", f"/proc/self/fd/{plan_fd}", pass_fds=(plan_fd,))
        successor_state = _state(args.module, adapter_sha256)
        if (
            successor_state["lineage"] != successor_contract["lineage"]
            or successor_state["serial"] < successor_contract["minimum_serial"]
            or successor_state["version_id"] == prior_state["version_id"]
            or successor_state["managed_addresses"]
            != successor_contract["managed_addresses"]
        ):
            raise ValueError("post-apply remote state differs from execution approval")
        print(
            json.dumps(
                {
                    "applied": True,
                    "plan_sha256": receipt["plan_sha256"],
                    "successor_state": successor_state,
                },
                sort_keys=True,
            )
        )
        return 0
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"custodied additive plan rejected: {exc}", file=sys.stderr)
        return 2
    finally:
        for descriptor in (plan_fd, receipt_fd, key_fd):
            if descriptor >= 0:
                os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
