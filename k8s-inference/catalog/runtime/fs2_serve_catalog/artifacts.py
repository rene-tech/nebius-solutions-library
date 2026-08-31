#!/usr/bin/env python3
"""Content-addressed artifact manifests shared by staging and qualification."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

from .loader import (
    CatalogError,
    MODEL_ID,
    _exact,
    _load_json,
    _positive_int,
    _text,
    canonical_content_uri,
    strong_sha256,
)


ARTIFACT_MANIFEST_SCHEMA = "fs2-serve.nebius.ai/artifact-manifest/v1"
ARTIFACT_KINDS = {"weights", "nim-cache", "snapshot"}
SAFE_URI_SCHEMES = {"sfs", "pvc", "nvme", "ngc", "hf", "oci"}
OWNER = re.compile(r"^[a-z0-9](?:[-a-z0-9./]*[a-z0-9])?$")


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogError(f"{label} must be a non-negative integer")
    return value


def canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise CatalogError("artifact value is not canonicalizable") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CatalogError(f"cannot safely open artifact file: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CatalogError(f"artifact entry is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _safe_relative_path(value: Any) -> str:
    text = _text(value, "artifact file path")
    assert text is not None
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CatalogError(f"artifact file path is unsafe: {text}")
    return path.as_posix()


def _safe_source_uri(value: Any, model_id: str, content_digest: str) -> str:
    uri = _text(value, "artifact source URI")
    assert uri is not None
    parsed = urlsplit(uri)
    if (
        parsed.scheme not in SAFE_URI_SCHEMES
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise CatalogError("artifact source URI is unsafe or not platform-approved")
    if parsed.scheme in {"sfs", "pvc", "nvme"}:
        return canonical_content_uri(
            uri, model_id=model_id, content_digest=content_digest, scheme=parsed.scheme
        )
    return uri


@dataclass(frozen=True)
class ArtifactFile:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class ArtifactManifest:
    path: Path | None
    digest: str
    model_id: str
    kind: str
    source_uri: str
    source_revision: str
    content_digest: str
    expanded_bytes: int
    files: tuple[ArtifactFile, ...]
    license_id: str
    license_state: str
    entitlement_state: str
    owner: str
    retention: str
    _value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._value))


def _validate_manifest(value: dict[str, Any], *, path: Path | None) -> ArtifactManifest:
    manifest = _exact(
        value,
        {
            "schema",
            "model_id",
            "kind",
            "source",
            "content",
            "license",
            "entitlement_state",
            "owner",
            "retention",
        },
        "artifact manifest",
    )
    if manifest["schema"] != ARTIFACT_MANIFEST_SCHEMA:
        raise CatalogError("unsupported artifact manifest schema")
    model_id = manifest["model_id"]
    if not isinstance(model_id, str) or MODEL_ID.fullmatch(model_id) is None:
        raise CatalogError("artifact model_id is not canonical")
    kind = manifest["kind"]
    if kind not in ARTIFACT_KINDS:
        raise CatalogError("artifact kind is outside the closed set")
    source = _exact(manifest["source"], {"uri", "revision"}, "artifact source")
    source_revision = _text(source["revision"], "artifact source revision")
    assert source_revision is not None
    content = _exact(
        manifest["content"], {"digest", "expanded_bytes", "files"}, "artifact content"
    )
    content_digest = content["digest"]
    strong_sha256(content_digest, "artifact content digest")
    expanded = _positive_int(content["expanded_bytes"], "artifact expanded bytes")
    assert expanded is not None
    source_uri = _safe_source_uri(source["uri"], model_id, content_digest)
    raw_files = content["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise CatalogError("artifact manifest requires at least one file")
    files: list[ArtifactFile] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_files):
        item = _exact(raw, {"path", "bytes", "sha256"}, f"artifact files[{index}]")
        file_path = _safe_relative_path(item["path"])
        file_bytes = _nonnegative_int(item["bytes"], f"artifact files[{index}].bytes")
        file_digest = item["sha256"]
        if file_path in seen:
            raise CatalogError(
                "artifact file list has a duplicate path or invalid digest"
            )
        strong_sha256(file_digest, "artifact file digest")
        seen.add(file_path)
        files.append(ArtifactFile(file_path, file_bytes, file_digest))
    if tuple(file.path for file in files) != tuple(sorted(file.path for file in files)):
        raise CatalogError("artifact files must be sorted by canonical path")
    if sum(file.bytes for file in files) != expanded:
        raise CatalogError("artifact expanded bytes do not reconcile with its files")
    calculated_content = hashlib.sha256(
        canonical_bytes(
            [
                {"path": file.path, "bytes": file.bytes, "sha256": file.sha256}
                for file in files
            ]
        )
    ).hexdigest()
    if calculated_content != content_digest:
        raise CatalogError("artifact content digest does not match the file inventory")
    license_value = _exact(manifest["license"], {"id", "state"}, "artifact license")
    license_id = _text(license_value["id"], "artifact license ID")
    license_state = license_value["state"]
    if license_state not in {"verified", "unverified", "blocked"}:
        raise CatalogError("artifact license state is invalid")
    entitlement_state = manifest["entitlement_state"]
    if entitlement_state not in {"not-required", "verified", "unverified", "blocked"}:
        raise CatalogError("artifact entitlement state is invalid")
    owner = _text(manifest["owner"], "artifact owner")
    if owner is None or OWNER.fullmatch(owner) is None:
        raise CatalogError("artifact owner is not canonical")
    retention = manifest["retention"]
    if retention not in {"retained-platform", "ephemeral-test"}:
        raise CatalogError("artifact retention is invalid")
    digest = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    return ArtifactManifest(
        path=path,
        digest=digest,
        model_id=model_id,
        kind=kind,
        source_uri=source_uri,
        source_revision=source_revision,
        content_digest=content_digest,
        expanded_bytes=expanded,
        files=tuple(files),
        license_id=license_id or "",
        license_state=license_state,
        entitlement_state=entitlement_state,
        owner=owner,
        retention=retention,
        _value=copy.deepcopy(manifest),
    )


def load_artifact_manifest(path: Path | str) -> ArtifactManifest:
    manifest_path = Path(path)
    return _validate_manifest(_load_json(manifest_path), path=manifest_path.resolve())


def artifact_manifest_from_value(value: dict[str, Any]) -> ArtifactManifest:
    """Validate an already-custodied manifest object without reopening a path."""

    return _validate_manifest(value, path=None)


def build_artifact_manifest(
    root: Path | str,
    *,
    model_id: str,
    kind: str,
    source_uri: str,
    source_revision: str,
    license_id: str,
    license_state: str,
    entitlement_state: str,
    owner: str,
    retention: str,
) -> ArtifactManifest:
    """Inventory a local tree without following symlinks and return a validated manifest."""

    artifact_root = Path(root).resolve()
    if not artifact_root.is_dir() or artifact_root.is_symlink():
        raise CatalogError("artifact root must be a non-symlink directory")
    files: list[dict[str, Any]] = []
    for candidate in sorted(artifact_root.rglob("*")):
        relative = candidate.relative_to(artifact_root).as_posix()
        info = candidate.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
            raise CatalogError(
                f"artifact tree contains a non-regular entry: {relative}"
            )
        files.append(
            {"path": relative, "bytes": info.st_size, "sha256": sha256_file(candidate)}
        )
    content_digest = hashlib.sha256(canonical_bytes(files)).hexdigest()
    value = {
        "schema": ARTIFACT_MANIFEST_SCHEMA,
        "model_id": model_id,
        "kind": kind,
        "source": {"uri": source_uri, "revision": source_revision},
        "content": {
            "digest": content_digest,
            "expanded_bytes": sum(item["bytes"] for item in files),
            "files": files,
        },
        "license": {"id": license_id, "state": license_state},
        "entitlement_state": entitlement_state,
        "owner": owner,
        "retention": retention,
    }
    return _validate_manifest(value, path=None)


def verify_artifact_tree(manifest: ArtifactManifest, root: Path | str) -> None:
    artifact_root = Path(root).resolve()
    actual_paths: list[str] = []
    for candidate in sorted(artifact_root.rglob("*")):
        info = candidate.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        relative = candidate.relative_to(artifact_root).as_posix()
        if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
            raise CatalogError(
                f"staged artifact contains a non-regular entry: {relative}"
            )
        actual_paths.append(relative)
    expected_paths = [item.path for item in manifest.files]
    if actual_paths != expected_paths:
        raise CatalogError("staged artifact file set differs from its manifest")
    for item in manifest.files:
        candidate = artifact_root / item.path
        if (
            candidate.stat().st_size != item.bytes
            or sha256_file(candidate) != item.sha256
        ):
            raise CatalogError(f"staged artifact file identity differs: {item.path}")
