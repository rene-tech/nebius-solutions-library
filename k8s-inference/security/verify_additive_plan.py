#!/usr/bin/env python3
"""Reject Terraform plan JSON containing any destructive or in-place action."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

MAX_BYTES = 32 * 1024 * 1024
SAFE_ACTIONS = {("create",), ("no-op",), ("read",)}


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
            raise ValueError("plan JSON must be a bounded non-empty regular file")
        chunks: list[bytes] = []
        remaining = MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) > MAX_BYTES
            or len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("plan JSON changed during its descriptor-bound read")
        return payload
    finally:
        os.close(descriptor)


def verify(value: object) -> dict[str, str | int]:
    if not isinstance(value, dict) or not isinstance(value.get("resource_changes"), list):
        raise ValueError("Terraform plan JSON is malformed")
    creates = 0
    for item in value["resource_changes"]:
        if not isinstance(item, dict) or not isinstance(item.get("change"), dict):
            raise ValueError("Terraform resource change is malformed")
        address = item.get("address")
        actions = item["change"].get("actions")
        if not isinstance(address, str) or not isinstance(actions, list):
            raise ValueError("Terraform resource change identity is malformed")
        action_tuple = tuple(actions)
        if action_tuple in SAFE_ACTIONS:
            creates += action_tuple == ("create",)
            continue
        raise ValueError(f"non-additive Terraform action is forbidden at {address}")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {
        "authorized": "true",
        "plan_sha256": hashlib.sha256(canonical).hexdigest(),
        "create_count": creates,
        "forget_count": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan_json", type=Path)
    args = parser.parse_args()
    try:
        payload = _read(args.plan_json)
        value: Any = json.loads(payload)
        print(json.dumps(verify(value), sort_keys=True))
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"additive Terraform plan rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
