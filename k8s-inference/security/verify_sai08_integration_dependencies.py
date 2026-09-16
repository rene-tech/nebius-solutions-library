#!/usr/bin/env python3
"""Bind SAI-08 integration claims to signed authority and real Git objects."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_BYTES = 64 * 1024
SCHEMA = "fs2-serve.nebius.ai/sai-08-integration-dependencies/v2"
REJECTED_SAI10 = "1ae009b858924138de70932ac84b8e595a2656a1"
ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
            raise ValueError("dependency record must be a bounded regular file")
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
            raise ValueError("dependency record changed during its descriptor-bound read")
        return payload
    finally:
        os.close(descriptor)


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", os.fspath(ROOT), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 or len(result.stdout.encode()) > MAX_BYTES:
        raise ValueError("Git integration identity check failed")
    return result.stdout.strip()


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} is not a SHA-256 digest")
    return value


def verify(record: dict[str, Any], expected: dict[str, str]) -> dict[str, str]:
    if os.environ.get("HELM_DRIVER") != "configmap":
        raise ValueError(
            "HELM_DRIVER must be configmap for the secret-blind release identity"
        )
    if set(record) != {"schema", "status", "dependencies", "requirements"}:
        raise ValueError("dependency record fields differ")
    if record.get("schema") != SCHEMA or record.get("status") != "accepted":
        raise ValueError("SAI-08 integration dependencies are not accepted")
    dependencies = record.get("dependencies")
    if not isinstance(dependencies, dict) or set(dependencies) != set(expected):
        raise ValueError("dependency identities are incomplete")
    if dependencies != expected:
        raise ValueError("dependency identities differ from the signed provider handoff")
    if dependencies["sai_10_accepted_commit"] == REJECTED_SAI10:
        raise ValueError("rejected SAI-10 commit cannot authorize integration")
    for field in (
        "sai_10_independent_review_receipt_sha256",
        "provider_authority_manifest_sha256",
        "provider_authority_prior_head_receipt_sha256",
        "provider_project_iam_inventory_receipt_sha256",
        "kubernetes_rbac_inventory_receipt_sha256",
        "predecessor_state_custody_sha256",
        "live_predecessor_compatibility_handoff_sha256",
    ):
        _digest(dependencies[field], field)
    commit = dependencies["sai_10_accepted_commit"]
    tree = dependencies["sai_10_accepted_tree"]
    if len(commit) != 40 or len(tree) != 40:
        raise ValueError("accepted SAI-10 commit/tree identity is invalid")
    if _git("show", "-s", "--format=%T", commit) != tree:
        raise ValueError("accepted SAI-10 commit does not have the recorded tree")
    _git("merge-base", "--is-ancestor", commit, "HEAD")
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    return {
        "authorized": "true",
        "dependency_record_sha256": hashlib.sha256(canonical).hexdigest(),
        "accepted_sai10_commit": commit,
        "accepted_sai10_tree": tree,
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        if not isinstance(query, dict) or set(query) != {
            "dependency_record_path",
            "expected_dependencies_json",
        }:
            raise ValueError("integration verifier query differs")
        path = Path(query["dependency_record_path"])
        expected = json.loads(query["expected_dependencies_json"])
        record = json.loads(_read(path))
        if not isinstance(record, dict) or not isinstance(expected, dict):
            raise ValueError("integration dependency inputs are malformed")
        print(json.dumps(verify(record, expected), sort_keys=True))
        return 0
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"SAI-08 integration dependencies rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
