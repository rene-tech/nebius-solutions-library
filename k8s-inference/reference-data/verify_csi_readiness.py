#!/usr/bin/env python3
"""Verify the exact retained CSI dataset through a read-only Pod mount."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

SCHEMA = "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1"
SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ReadinessError(ValueError):
    pass


def read_regular(path: Path, *, limit: int = 1024 * 1024) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ReadinessError(f"cannot safely open {path.name}: {error.strerror}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ReadinessError(f"{path.name} is not a bounded regular file")
        payload = os.read(descriptor, metadata.st_size + 1)
        if len(payload) != metadata.st_size:
            raise ReadinessError(f"{path.name} changed while it was read")
        return payload
    finally:
        os.close(descriptor)


def validate(root: Path, receipt_relative: Path, bundle: str, revision: str, tree: str) -> dict[str, object]:
    if not SHA256.fullmatch(tree):
        raise ReadinessError("expected tree digest is malformed")
    root = root.resolve(strict=True)
    receipt_path = (root / receipt_relative).resolve(strict=True)
    if not receipt_path.is_relative_to(root):
        raise ReadinessError("receipt path escapes the CSI mount")
    try:
        receipt_payload = read_regular(receipt_path)
        receipt = json.loads(receipt_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReadinessError("terminal receipt is not valid JSON") from error
    if not isinstance(receipt, dict):
        raise ReadinessError("terminal receipt must be an object")
    storage = receipt.get("storage")
    content = receipt.get("content")
    expected_sub_path = f"datasets/{bundle}/{revision}/sha256/{tree}"
    if (
        receipt.get("schema") != SCHEMA
        or receipt.get("bundle_id") != bundle
        or receipt.get("revision") != revision
        or not isinstance(storage, dict)
        or storage.get("mount_path") != "/reference-data"
        or storage.get("dataset_sub_path") != expected_sub_path
        or storage.get("read_only") is not True
        or not isinstance(content, dict)
        or content.get("tree_sha256") != tree
        or content.get("inventory_marker") != ".fs2-manifest-sha256"
        or not SHA256.fullmatch(str(content.get("manifest_sha256", "")))
        or not SHA256.fullmatch(str(content.get("inventory_sha256", "")))
        or not isinstance(content.get("file_count"), int)
        or content["file_count"] < 1
        or not isinstance(content.get("expanded_bytes"), int)
        or content["expanded_bytes"] < 1
    ):
        raise ReadinessError("terminal receipt differs from the exact dataset contract")

    dataset = (root / expected_sub_path).resolve(strict=True)
    if not dataset.is_relative_to(root) or not dataset.is_dir():
        raise ReadinessError("dataset directory is absent or escapes the CSI mount")
    marker_path = dataset / ".fs2-manifest-sha256"
    marker = read_regular(marker_path, limit=256).decode("ascii").strip()
    if marker != content["manifest_sha256"]:
        raise ReadinessError("dataset marker differs from the terminal manifest digest")
    marker_stat = marker_path.stat(follow_symlinks=False)
    if marker_stat.st_size == 0:
        raise ReadinessError("dataset marker is empty")
    return {
        "schema": "fs2-serve.nebius.ai/reference-data-csi-readiness/v1",
        "bundle_id": bundle,
        "revision": revision,
        "tree_sha256": tree,
        "receipt_sha256": hashlib.sha256(receipt_payload).hexdigest(),
        "manifest_sha256": marker,
        "read_probe_passed": True,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--root", required=True, type=Path)
    result.add_argument("--receipt", required=True, type=Path)
    result.add_argument("--bundle", required=True)
    result.add_argument("--revision", required=True)
    result.add_argument("--tree-sha256", required=True)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        result = validate(args.root, args.receipt, args.bundle, args.revision, args.tree_sha256)
    except (OSError, ReadinessError) as error:
        print(f"reference-data CSI readiness refused: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
