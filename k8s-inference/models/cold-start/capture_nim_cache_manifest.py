#!/usr/bin/env python3
"""Double-capture one populated NIM cache into an exact artifact manifest.

The script is standard-library-only so it can be streamed to ``python3 -`` in
an existing NIM container. It performs no writes below the cache root and emits
canonical JSON on stdout only after two identical, mutation-checked hash passes.
The caller must capture stdout directly into a new mode-0600 private file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any


SHA256 = re.compile(r"^[0-9a-f]{64}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
DNS = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
UID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
NGC_REPOSITORY = re.compile(r"^nvcr\.io/nim/[a-z0-9][a-z0-9._/-]*$")
OWNER = re.compile(r"^[a-z0-9](?:[-a-z0-9./]*[a-z0-9])?$")
MAX_FILE_COUNT = 1_000_000


class CacheManifestError(ValueError):
    """The candidate tree or immutable identity binding is invalid."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError):
        raise CacheManifestError("value_not_canonicalizable") from None


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_root(value: Path) -> Path:
    if not value.is_absolute():
        raise CacheManifestError("cache_root_not_absolute")
    try:
        metadata = value.lstat()
    except OSError:
        raise CacheManifestError("cache_root_unavailable") from None
    if value.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise CacheManifestError("cache_root_not_plain_directory")
    return value


def _regular_files(root: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            raise CacheManifestError("cache_tree_scan_failed") from None
        for entry in entries:
            candidate = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                raise CacheManifestError("cache_entry_stat_failed") from None
            relative = candidate.relative_to(root).as_posix()
            safe = PurePosixPath(relative)
            if (
                safe.is_absolute()
                or not safe.parts
                or any(part in {"", ".", ".."} for part in safe.parts)
            ):
                raise CacheManifestError("cache_relative_path_unsafe")
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(candidate)
                continue
            if not stat.S_ISREG(metadata.st_mode) or entry.is_symlink():
                raise CacheManifestError("cache_tree_has_non_regular_entry")
            files.append((relative, candidate))
            if len(files) > MAX_FILE_COUNT:
                raise CacheManifestError("cache_tree_file_count_exceeded")
    return sorted(files, key=lambda item: item[0])


def _hash_stable_file(path: Path) -> tuple[int, str, tuple[int, ...]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise CacheManifestError("cache_file_open_failed") from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CacheManifestError("cache_file_identity_invalid")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        after = os.fstat(descriptor)
    except OSError:
        raise CacheManifestError("cache_file_read_failed") from None
    finally:
        os.close(descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise CacheManifestError("cache_file_mutated_during_hash")
    return before.st_size, digest.hexdigest(), identity_after


def capture_pass(root: Path, capacity_bound_bytes: int) -> dict[str, Any]:
    root_before = root.stat()
    started_at = utc_now()
    files: list[dict[str, Any]] = []
    metadata_identities: list[dict[str, Any]] = []
    expanded_bytes = 0
    for relative, path in _regular_files(root):
        size, sha256, identity = _hash_stable_file(path)
        expanded_bytes += size
        if expanded_bytes > capacity_bound_bytes:
            raise CacheManifestError("cache_tree_exceeds_capacity_bound")
        files.append({"path": relative, "bytes": size, "sha256": sha256})
        metadata_identities.append({"path": relative, "identity": list(identity)})
    if not files:
        raise CacheManifestError("cache_tree_empty")
    if expanded_bytes <= 0:
        raise CacheManifestError("cache_tree_has_no_positive_payload")
    if [item[0] for item in _regular_files(root)] != [item["path"] for item in files]:
        raise CacheManifestError("cache_file_set_mutated_during_hash")
    root_after = root.stat()
    root_identity_before = (
        root_before.st_dev,
        root_before.st_ino,
        root_before.st_mtime_ns,
        root_before.st_ctime_ns,
    )
    root_identity_after = (
        root_after.st_dev,
        root_after.st_ino,
        root_after.st_mtime_ns,
        root_after.st_ctime_ns,
    )
    if root_identity_before != root_identity_after:
        raise CacheManifestError("cache_root_mutated_during_hash")
    return {
        "started_at": started_at,
        "completed_at": utc_now(),
        "file_count": len(files),
        "expanded_bytes": expanded_bytes,
        "content_digest": digest_value(files),
        "metadata_digest": digest_value(metadata_identities),
        "files": files,
    }


def build_capture(args: argparse.Namespace) -> dict[str, Any]:
    root = _safe_root(args.root)
    for value, expression, code in (
        (args.model_id, DNS, "model_id_invalid"),
        (args.namespace, DNS, "namespace_invalid"),
        (args.pvc_name, DNS, "pvc_name_invalid"),
        (args.container, DNS, "container_invalid"),
        (args.namespace_uid, UID, "namespace_uid_invalid"),
        (args.pvc_uid, UID, "pvc_uid_invalid"),
        (args.pod_uid, UID, "pod_uid_invalid"),
        (args.source_commit, GIT_SHA, "source_commit_invalid"),
        (args.source_tree, GIT_SHA, "source_tree_invalid"),
        (args.source_repository, NGC_REPOSITORY, "source_repository_invalid"),
        (args.source_revision, DIGEST, "source_revision_invalid"),
        (args.runtime_image_digest, DIGEST, "runtime_image_digest_invalid"),
        (args.owner, OWNER, "owner_invalid"),
    ):
        if expression.fullmatch(value) is None:
            raise CacheManifestError(code)
    if args.runtime_image_digest != args.source_revision:
        raise CacheManifestError("runtime_source_digest_mismatch")
    if (
        isinstance(args.capacity_bound_bytes, bool)
        or not isinstance(args.capacity_bound_bytes, int)
        or args.capacity_bound_bytes < 1
    ):
        raise CacheManifestError("capacity_bound_invalid")

    first = capture_pass(root, args.capacity_bound_bytes)
    time.sleep(args.stability_delay_seconds)
    second = capture_pass(root, args.capacity_bound_bytes)
    for field in (
        "file_count",
        "expanded_bytes",
        "content_digest",
        "metadata_digest",
        "files",
    ):
        if first[field] != second[field]:
            raise CacheManifestError("cache_double_capture_mismatch")

    manifest = {
        "schema": "fs2-serve.nebius.ai/artifact-manifest/v1",
        "model_id": args.model_id,
        "kind": "nim-cache",
        "source": {
            "uri": f"ngc://{args.source_repository}",
            "revision": args.source_revision,
        },
        "content": {
            "digest": second["content_digest"],
            "expanded_bytes": second["expanded_bytes"],
            "files": second["files"],
        },
        "license": {"id": args.license_id, "state": args.license_state},
        "entitlement_state": args.entitlement_state,
        "owner": args.owner,
        "retention": args.retention,
    }
    receipt: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/nim-cache-manifest-capture/v1",
        "source": {"commit": args.source_commit, "tree": args.source_tree},
        "subject": {
            "model_id": args.model_id,
            "namespace": args.namespace,
            "namespace_uid": args.namespace_uid,
            "pod_uid": args.pod_uid,
            "container": args.container,
            "pvc_name": args.pvc_name,
            "pvc_uid": args.pvc_uid,
            "cache_root": str(root),
            "runtime_image_digest": args.runtime_image_digest,
            "capacity_bound_bytes": args.capacity_bound_bytes,
        },
        "stability": {
            "method": "two-complete-sha256-passes-with-descriptor-stat-guards",
            "delay_seconds": args.stability_delay_seconds,
            "first": {key: first[key] for key in first if key != "files"},
            "second": {key: second[key] for key in second if key != "files"},
            "stable": True,
        },
        "artifact_manifest": manifest,
        "artifact_manifest_digest": digest_value(manifest),
        "captured_at": utc_now(),
    }
    receipt["receipt_digest"] = digest_value(receipt)
    return receipt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--namespace-uid", required=True)
    parser.add_argument("--pod-uid", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--pvc-name", required=True)
    parser.add_argument("--pvc-uid", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--source-repository", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--runtime-image-digest", required=True)
    parser.add_argument("--capacity-bound-bytes", type=int, required=True)
    parser.add_argument("--license-id", required=True)
    parser.add_argument(
        "--license-state", choices=("verified", "unverified", "blocked"), required=True
    )
    parser.add_argument(
        "--entitlement-state",
        choices=("not-required", "verified", "unverified", "blocked"),
        required=True,
    )
    parser.add_argument("--owner", default="nim-operator-nimcache")
    parser.add_argument(
        "--retention",
        choices=("retained-platform", "ephemeral-test"),
        default="ephemeral-test",
    )
    parser.add_argument("--stability-delay-seconds", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.stability_delay_seconds <= 60:
        parser.error("--stability-delay-seconds must be from 1 through 60")
    return args


def main() -> int:
    try:
        sys.stdout.buffer.write(canonical_bytes(build_capture(parse_args())))
        sys.stdout.buffer.flush()
    except CacheManifestError:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
