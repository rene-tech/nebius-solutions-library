#!/usr/bin/env python3
"""Verify controller identities against the fixed live audit-evidence adapter."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

AUDIT_REGISTRY_PATH = Path(
    "/var/lib/fs2-security-checkpoints-ro/kubernetes-controller-audit.json"
)
AUDIT_ADAPTER_PATH = Path("/usr/libexec/fs2-security/kubernetes-audit-evidence")
MAX_BYTES = 1024 * 1024


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _safe_read(path: Path) -> bytes:
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        before = os.fstat(descriptor)
        filesystem = os.fstatvfs(descriptor)
        payload = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_mode & 0o077
            or before.st_size <= 0
            or before.st_size > MAX_BYTES
            or not filesystem.f_flag & getattr(os, "ST_RDONLY", 1)
            or len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("controller audit registry is not immutable and bounded")
        return payload
    finally:
        os.close(descriptor)


def _object(payload: bytes | str, label: str) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _open_adapter(expected_sha256: str) -> tuple[int, os.stat_result]:
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in AUDIT_ADAPTER_PATH.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            AUDIT_ADAPTER_PATH.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory,
        )
    finally:
        os.close(directory)
    metadata = os.fstat(descriptor)
    filesystem = os.fstatvfs(descriptor)
    payload = os.read(descriptor, metadata.st_size + 1)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or not metadata.st_mode & 0o111
        or metadata.st_size <= 0
        or metadata.st_size > MAX_BYTES
        or not filesystem.f_flag & getattr(os, "ST_RDONLY", 1)
        or len(payload) != metadata.st_size
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        os.close(descriptor)
        raise ValueError("controller audit adapter differs from pinned custody")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return descriptor, metadata


def verify_live_controller_audit(cluster_id: str) -> tuple[dict[str, Any], str]:
    registry = _object(_safe_read(AUDIT_REGISTRY_PATH), "controller audit registry")
    if (
        set(registry)
        != {
            "schema",
            "checkpoint_public_key_pem",
            "audit_adapter_sha256",
            "controller_audit_receipt",
        }
        or registry.get("schema")
        != "fs2-serve.nebius.ai/kubernetes-controller-audit-registry/v2"
        or not isinstance(registry.get("audit_adapter_sha256"), str)
        or len(registry["audit_adapter_sha256"]) != 64
    ):
        raise ValueError("controller audit registry fields differ")
    receipt = registry.get("controller_audit_receipt")
    if not isinstance(receipt, dict) or receipt.get("cluster_id") != cluster_id:
        raise ValueError("controller audit receipt belongs to another cluster")
    body = {
        key: value
        for key, value in receipt.items()
        if key not in {"payload_sha256", "signature"}
    }
    body_sha256 = hashlib.sha256(canonical(body)).hexdigest()
    if receipt.get("payload_sha256") != body_sha256:
        raise ValueError("controller audit receipt digest differs")
    key = serialization.load_pem_public_key(
        str(registry["checkpoint_public_key_pem"]).encode()
    )
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("controller audit checkpoint key is not Ed25519")
    try:
        key.verify(
            base64.b64decode(str(receipt["signature"]), validate=True),
            canonical(body),
        )
    except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
        raise ValueError("controller audit receipt signature is invalid") from exc

    descriptor, before = _open_adapter(registry["audit_adapter_sha256"])
    request = {
        "schema": "fs2-serve.nebius.ai/kubernetes-controller-audit-query/v1",
        "cluster_id": cluster_id,
        "receipt_body_sha256": body_sha256,
        "controller_audit_ids": {
            role: event["audit_id"]
            for role, event in body["controller_events"].items()
        },
        "daemonset_audit_ids": {
            name: maintainer["event"]["audit_id"]
            for name, maintainer in body["daemonset_maintainers"].items()
        },
    }
    try:
        result = subprocess.run(
            [f"/proc/self/fd/{descriptor}"],
            input=json.dumps(request, sort_keys=True, separators=(",", ":")),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            pass_fds=(descriptor,),
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        result.returncode != 0
        or not result.stdout
        or len(result.stdout.encode()) > 8 * MAX_BYTES
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError("controller audit adapter failed or changed during verification")
    live = _object(result.stdout, "controller audit adapter response")
    if set(live) != {"schema", "receipt_body"} or live.get("schema") != (
        "fs2-serve.nebius.ai/kubernetes-controller-audit-evidence/v1"
    ) or live.get("receipt_body") != body:
        raise ValueError("live authenticated audit evidence differs from signed receipt")
    return receipt, body_sha256
