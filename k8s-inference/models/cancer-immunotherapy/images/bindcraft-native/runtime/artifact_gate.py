#!/usr/bin/env python3
"""Fail-closed verification for model assets mounted outside runtime images."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any


SHA256 = re.compile(r"^[0-9a-f]{64}$")
SCHEMA = "fs2.nebius.ai/external-model-artifact-manifest/v1"


class ArtifactGateError(RuntimeError):
    """An external artifact set failed its immutable admission contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_child(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise ArtifactGateError("artifact manifest contains an unsafe relative path")
    candidate = root.joinpath(*pure.parts)
    if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents if parent != root.parent):
        raise ArtifactGateError("artifact manifest may not admit symlinks")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise ArtifactGateError("artifact manifest references a missing or escaped file") from exc
    if not candidate.is_file():
        raise ArtifactGateError("artifact manifest entry is not a regular file")
    return candidate


def verify_manifest(
    root: Path,
    manifest_path: Path,
    *,
    expected_kind: str,
    expected_source_revision: str,
) -> dict[str, Any]:
    """Verify every immutable file before the model process is allowed to start."""

    if not root.is_dir() or root.is_symlink():
        raise ArtifactGateError("external artifact root is unavailable")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ArtifactGateError("external artifact manifest is unavailable")
    try:
        raw = manifest_path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactGateError("external artifact manifest is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ArtifactGateError("external artifact manifest schema is unsupported")
    if value.get("artifact_kind") != expected_kind:
        raise ArtifactGateError("external artifact kind does not match this runtime")
    if value.get("source_revision") != expected_source_revision:
        raise ArtifactGateError("external artifact source revision does not match this runtime")
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise ArtifactGateError("external artifact manifest has no files")
    seen: set[str] = set()
    verified_bytes = 0
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size_bytes"}:
            raise ArtifactGateError("external artifact manifest entry is malformed")
        relative = entry["path"]
        expected_sha = entry["sha256"]
        expected_size = entry["size_bytes"]
        if not isinstance(relative, str) or relative in seen:
            raise ArtifactGateError("external artifact manifest paths must be unique strings")
        if not isinstance(expected_sha, str) or SHA256.fullmatch(expected_sha) is None:
            raise ArtifactGateError("external artifact manifest has an invalid SHA-256")
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 1:
            raise ArtifactGateError("external artifact manifest has an invalid size")
        path = _safe_child(root, relative)
        if path.stat().st_size != expected_size or sha256_file(path) != expected_sha:
            raise ArtifactGateError("external artifact content does not match its manifest")
        seen.add(relative)
        verified_bytes += expected_size
    return {
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "files": len(files),
        "bytes": verified_bytes,
    }


def verify_from_environment() -> dict[str, Any]:
    required = {
        "FS2_ARTIFACT_ROOT": os.environ.get("FS2_ARTIFACT_ROOT"),
        "FS2_ARTIFACT_MANIFEST": os.environ.get("FS2_ARTIFACT_MANIFEST"),
        "FS2_ARTIFACT_KIND": os.environ.get("FS2_ARTIFACT_KIND"),
        "FS2_SOURCE_REVISION": os.environ.get("FS2_SOURCE_REVISION"),
    }
    if any(not value for value in required.values()):
        raise ArtifactGateError("external artifact gate configuration is incomplete")
    return verify_manifest(
        Path(required["FS2_ARTIFACT_ROOT"] or ""),
        Path(required["FS2_ARTIFACT_MANIFEST"] or ""),
        expected_kind=required["FS2_ARTIFACT_KIND"] or "",
        expected_source_revision=required["FS2_SOURCE_REVISION"] or "",
    )
