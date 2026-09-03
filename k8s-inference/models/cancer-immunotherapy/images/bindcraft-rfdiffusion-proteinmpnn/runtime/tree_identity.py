#!/usr/bin/env python3
"""Immutable identity for the model trees this runtime mounts from outside the image.

The BindCraft image deliberately ships no weights and no PyRosetta, so four
directories arrive at run time from a shared filesystem: the licensed PyRosetta
installed tree, the AlphaFold2 parameter set, and the vanilla and soluble
ColabDesign MPNN weights. A directory has no natural digest, so one has to be
defined rather than borrowed, and it has to be the *same* definition the
component that published the tree used. Two are needed because two different
publishers exist:

``fs2-tree-manifest/v1``
    Every regular file contributes its POSIX-relative path, size and SHA-256;
    every symlink contributes its path and target. Kept byte-identical to
    ``academic-assets/scripts/install_tree.py``, which is what installs the
    PyRosetta tree - a digest only this file could reproduce would prove nothing
    about the tree that installer actually published.

``fs2-flat-tree-inventory/v1``
    A path-sorted inventory of a flat directory's regular files carrying size
    and CRC-32. Kept byte-identical to the scientific-localization staging
    receipts, which is what publishes the AlphaFold2 and ColabDesign trees.

Both read every byte. That is affordable here and measured: on the eu-north1
shared filesystem the 3.29 GB PyRosetta tree hashes in about 15 s and the
5.59 GB AlphaFold2 tree in about 6 s, against trajectories that run for
minutes. Reading the bytes is therefore the cheap way to be honest, and no
metadata-only shortcut is offered - a size-and-shape check would pass on a tree
whose contents had been swapped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zlib
from pathlib import Path
from typing import Any


TREE_MANIFEST_ALGORITHM = "fs2-tree-manifest/v1"
FLAT_INVENTORY_ALGORITHM = "fs2-flat-tree-inventory/v1"

SHA256 = re.compile(r"^[0-9a-f]{64}$")
FLAT_ENTRY_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,254}$")

READ_CHUNK = 1 << 20


class TreeIdentityError(RuntimeError):
    """A mounted tree failed its immutable identity contract."""


def _require_directory(root: Path, label: str) -> None:
    if not root.is_dir() or root.is_symlink():
        raise TreeIdentityError(f"{label} is not an available directory")


def tree_manifest(root: Path) -> dict[str, Any]:
    """Identify a nested tree by the full content of every file it holds."""

    _require_directory(root, "tree")
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(READ_CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
        entries.append({"path": relative, "kind": "file", "size_bytes": size, "sha256": digest.hexdigest()})
        total_bytes += size

    payload = {"algorithm": TREE_MANIFEST_ALGORITHM, "entries": entries}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return {
        "tree_manifest_algorithm": TREE_MANIFEST_ALGORITHM,
        "tree_manifest_sha256": hashlib.sha256(canonical).hexdigest(),
        "tree_total_bytes": total_bytes,
        "file_count": sum(1 for entry in entries if entry["kind"] == "file"),
        "symlink_count": sum(1 for entry in entries if entry["kind"] == "symlink"),
        "entries": entries,
    }


def flat_tree_inventory(root: Path) -> dict[str, Any]:
    """Identify a flat directory of regular files by size and CRC-32."""

    _require_directory(root, "flat tree")
    rows: list[dict[str, Any]] = []
    total_bytes = 0
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_file():
            raise TreeIdentityError("flat tree may contain only regular files")
        if FLAT_ENTRY_NAME.fullmatch(child.name) is None:
            raise TreeIdentityError("flat tree entries must be safe flat-root names")
        checksum = 0
        size = 0
        with child.open("rb") as handle:
            for chunk in iter(lambda: handle.read(READ_CHUNK), b""):
                checksum = zlib.crc32(chunk, checksum)
                size += len(chunk)
        rows.append({"bytes": size, "crc32": f"{checksum & 0xFFFFFFFF:08x}", "path": child.name})
        total_bytes += size
    if not rows:
        raise TreeIdentityError("flat tree is empty")
    rows.sort(key=lambda row: str(row["path"]))
    serialized = (json.dumps(rows, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    return {
        "inventory_algorithm": FLAT_INVENTORY_ALGORITHM,
        "inventory_sha256": hashlib.sha256(serialized).hexdigest(),
        "entry_count": len(rows),
        "total_bytes": total_bytes,
        "rows": rows,
    }


def verify_tree(root: Path, *, artifact_id: str, expected_tree_manifest_sha256: str) -> dict[str, Any]:
    """Admit a nested tree only if its full content is the pinned identity."""

    if SHA256.fullmatch(expected_tree_manifest_sha256) is None:
        raise TreeIdentityError("expected tree manifest digest must be a lowercase SHA-256")
    observed = tree_manifest(root)
    if observed["tree_manifest_sha256"] != expected_tree_manifest_sha256:
        raise TreeIdentityError(
            f"mounted tree {artifact_id!r} is not the immutable tree this runtime is pinned to"
        )
    return {
        "artifact_id": artifact_id,
        "verification": "full-content-tree-manifest",
        "tree_manifest_algorithm": observed["tree_manifest_algorithm"],
        "tree_manifest_sha256": observed["tree_manifest_sha256"],
        "file_count": observed["file_count"],
        "symlink_count": observed["symlink_count"],
        "total_bytes": observed["tree_total_bytes"],
        "entries": observed["entries"],
    }


def verify_flat_tree(root: Path, *, artifact_id: str, expected_inventory_sha256: str) -> dict[str, Any]:
    """Admit a flat tree only if its full content is the pinned identity."""

    if SHA256.fullmatch(expected_inventory_sha256) is None:
        raise TreeIdentityError("expected inventory digest must be a lowercase SHA-256")
    observed = flat_tree_inventory(root)
    if observed["inventory_sha256"] != expected_inventory_sha256:
        raise TreeIdentityError(
            f"mounted tree {artifact_id!r} is not the immutable tree this runtime is pinned to"
        )
    return {
        "artifact_id": artifact_id,
        "verification": "full-content-flat-inventory",
        "inventory_algorithm": observed["inventory_algorithm"],
        "inventory_sha256": observed["inventory_sha256"],
        "entry_count": observed["entry_count"],
        "total_bytes": observed["total_bytes"],
    }
