#!/usr/bin/env python3
"""Atomic, single-writer SFS-to-local-NVMe artifact localization."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import stat
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .artifacts import ArtifactFile, ArtifactManifest, canonical_bytes, sha256_file, verify_artifact_tree
from .loader import CatalogError, canonical_content_uri


STAGING_RECEIPT_SCHEMA = "fs2-serve.nebius.ai/staging-receipt/v3"
LOCALIZER_OWNER = "fs2-serve-localizer"
K8S_UID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27,}$")


def _copy_one(source_root: Path, target_root: Path, item: ArtifactFile) -> None:
    source = source_root / item.path
    target = target_root / item.path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise CatalogError("staging destination contains a symlinked directory")
    source_info = source.lstat()
    if not stat.S_ISREG(source_info.st_mode) or source.is_symlink():
        raise CatalogError(f"source artifact entry is not regular: {item.path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(target, flags, 0o440)
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb", closefd=False) as output:
            shutil.copyfileobj(input_stream, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)
    if target.stat().st_size != item.bytes or sha256_file(target) != item.sha256:
        raise CatalogError(f"localized artifact entry failed verification: {item.path}")


def stage_artifact(
    manifest: ArtifactManifest,
    source_root: Path | str,
    model_cache_root: Path | str,
    *,
    controller_owner: str,
    serving_node_name: str,
    serving_node_uid: str,
    serving_node_provider_id_sha256: str,
    reserve_bytes: int = 8 * 1024**3,
    max_concurrent_files: int = 4,
) -> dict[str, Any]:
    """Verify, copy, fsync, and atomically publish one immutable artifact version."""

    if controller_owner != LOCALIZER_OWNER:
        raise CatalogError("the fs2 localizer may write only its explicitly owned cache paths")
    if manifest.kind == "nim-cache":
        raise CatalogError("NIM cache content is owned exclusively by NIM Operator NIMCache")
    if (
        not isinstance(serving_node_name, str)
        or not serving_node_name
        or len(serving_node_name) > 253
        or not isinstance(serving_node_uid, str)
        or K8S_UID.fullmatch(serving_node_uid) is None
        or not isinstance(serving_node_provider_id_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", serving_node_provider_id_sha256) is None
    ):
        raise CatalogError("staging requires the exact serving node identity")
    if isinstance(reserve_bytes, bool) or not isinstance(reserve_bytes, int) or reserve_bytes < 0:
        raise CatalogError("staging reserve bytes must be a nonnegative integer")
    if isinstance(max_concurrent_files, bool) or not 1 <= max_concurrent_files <= 16:
        raise CatalogError("staging file concurrency must be between one and sixteen")
    source = Path(source_root).resolve()
    cache_root = Path(model_cache_root).resolve()
    if source == cache_root or source in cache_root.parents or cache_root in source.parents:
        raise CatalogError("staging source and destination roots must not overlap")
    verify_artifact_tree(manifest, source)
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o750)
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise CatalogError("model cache root must be a non-symlink directory")
    # The Job mounts exactly model_cache_root. Keep the lock under that writable
    # mount so read-only root filesystems cannot silently disable single-writer
    # protection.
    lock_root = cache_root / ".locks"
    lock_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    lock_path = lock_root / f"{manifest.model_id}.lock"
    lock_descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o640,
    )
    operation_id = str(uuid.uuid4())
    destination = cache_root / "sha256" / manifest.content_digest
    temporary = cache_root / f".stage-{operation_id}"
    free_before = shutil.disk_usage(cache_root).free
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CatalogError("another cache controller currently owns this model path") from exc
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise CatalogError("existing content address is not a regular directory")
            verify_artifact_tree(manifest, destination)
            outcome = "already-present"
        else:
            required = manifest.expanded_bytes + reserve_bytes
            if free_before < required:
                raise CatalogError(
                    f"insufficient free space: required={required} available={free_before}"
                )
            temporary.mkdir(mode=0o750)
            try:
                with ThreadPoolExecutor(max_workers=max_concurrent_files) as executor:
                    list(executor.map(lambda item: _copy_one(source, temporary, item), manifest.files))
                verify_artifact_tree(manifest, temporary)
                (cache_root / "sha256").mkdir(mode=0o750, exist_ok=True)
                os.replace(temporary, destination)
            except Exception:
                if temporary.exists() and temporary.parent == cache_root:
                    shutil.rmtree(temporary)
                raise
            outcome = "staged"
        free_after = shutil.disk_usage(cache_root).free
        receipt = {
            "schema": STAGING_RECEIPT_SCHEMA,
            "operation_id": operation_id,
            "model_id": manifest.model_id,
            "artifact_manifest_digest": manifest.digest,
            "content_digest": manifest.content_digest,
            "source_path": str(source),
            "source_uri": canonical_content_uri(
                "sfs://fs2-cache"
                f"/mnt/fs2-serve-cache/models/{manifest.model_id}/sha256/{manifest.content_digest}",
                model_id=manifest.model_id,
                content_digest=manifest.content_digest,
                scheme="sfs",
            ),
            "destination_path": str(destination),
            "content_uri": canonical_content_uri(
                "nvme://localhost"
                f"/var/lib/fs2-serve/cache/models/{manifest.model_id}/sha256/{manifest.content_digest}",
                model_id=manifest.model_id,
                content_digest=manifest.content_digest,
                scheme="nvme",
            ),
            "controller_owner": controller_owner,
            "lock_path": str(lock_path),
            "serving_node": {
                "name": serving_node_name,
                "uid": serving_node_uid,
                "provider_id_sha256": serving_node_provider_id_sha256,
            },
            "max_concurrent_files": max_concurrent_files,
            "expanded_bytes": manifest.expanded_bytes,
            "reserve_bytes": reserve_bytes,
            "free_bytes_before": free_before,
            "free_bytes_after": free_after,
            "outcome": outcome,
            "cleanup": {"temporary_path_absent": not temporary.exists()},
        }
        receipt["receipt_digest"] = hashlib.sha256(canonical_bytes(receipt)).hexdigest()
        return receipt
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)
