#!/usr/bin/env python3
"""Validate the immutable execution capsule used by SAI-24 release gates.

This module is deliberately standard-library-only.  A protected integration
job publishes one reviewed lock whose digest is pinned by the source trust
policy.  Release code never resolves a security tool through PATH and never
accepts an unmeasured Python import tree.
"""

from __future__ import annotations

import hashlib
import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


HEX_SHA256 = __import__("re").compile(r"^[0-9a-f]{64}$")


class ToolchainError(ValueError):
    """Raised when the protected execution capsule is incomplete or mutable."""


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolchainError(f"{path}: unreadable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolchainError(f"{path}: JSON root must be an object")
    return value


def _load_fd(descriptor: int, label: str) -> dict[str, Any]:
    """Parse JSON from the retained exact descriptor without reopening a path."""

    offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    os.lseek(descriptor, offset, os.SEEK_SET)
    try:
        value = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolchainError(f"{label}: unreadable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolchainError(f"{label}: JSON root must be an object")
    return value


def _hash_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = os.lseek(descriptor, 0, os.SEEK_CUR)
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, offset, os.SEEK_SET)
    return digest.hexdigest()


def open_verified_file(path: Path, expected_sha256: str) -> int:
    """Open one immutable capsule file once and hash the retained descriptor."""

    if not HEX_SHA256.fullmatch(expected_sha256):
        raise ToolchainError(f"{path}: expected SHA-256 is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ToolchainError(f"{path}: cannot open exact object: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ToolchainError(f"{path}: exact object is not a regular file")
        if metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise ToolchainError(f"{path}: exact capsule object has a write bit")
        if _hash_fd(descriptor) != expected_sha256:
            raise ToolchainError(f"{path}: exact object SHA-256 differs")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _tree_manifest(path: Path) -> str:
    if path.is_symlink() or not path.is_dir():
        raise ToolchainError(f"{path}: runtime root must be a real directory")
    entries: list[dict[str, str]] = []
    for candidate in sorted(path.rglob("*")):
        if candidate.is_symlink():
            raise ToolchainError(f"{candidate}: runtime symlinks are not accepted")
        if candidate.is_file():
            descriptor = os.open(
                candidate,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                entries.append(
                    {
                        "path": candidate.relative_to(path).as_posix(),
                        "sha256": _hash_fd(descriptor),
                    }
                )
            finally:
                os.close(descriptor)
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _anchored_lock(lock_path: Path, trust_path: Path) -> dict[str, Any]:
    external_value = os.environ.get("FS2_EXTERNAL_CAPSULE_TRUST", "")
    if not external_value or not Path(external_value).is_absolute():
        raise ToolchainError("external capsule trust path is mandatory")
    external_path = Path(external_value)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        external_fd = os.open(external_path, flags)
    except OSError as exc:
        raise ToolchainError("external capsule trust cannot be opened exactly") from exc
    try:
        metadata = os.fstat(external_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ToolchainError(
                "external capsule trust must be root-owned and read-only"
            )
        external = _load_fd(external_fd, str(external_path))
    finally:
        os.close(external_fd)
    for parent in (external_path.parent, *external_path.parents):
        parent_metadata = parent.stat()
        if parent_metadata.st_uid != 0 or parent_metadata.st_mode & (
            stat.S_IWGRP | stat.S_IWOTH
        ):
            raise ToolchainError(
                "external capsule trust ancestry must be root-owned and not group/world writable"
            )
        if parent == Path("/"):
            break
    if (
        external.get("schema") != "fs2-serve.nebius.ai/external-capsule-trust/v1"
        or external.get("state") != "trusted"
    ):
        raise ToolchainError("external capsule trust is not active")
    repository_binding = external.get("repository_trust")
    lock_binding = external.get("toolchain_lock")
    if not isinstance(repository_binding, dict) or not isinstance(lock_binding, dict):
        raise ToolchainError("external capsule bindings are incomplete")
    def open_binding(
        actual: Path, binding: dict[str, Any], label: str
    ) -> tuple[int, str]:
        bound_path = binding.get("path")
        bound_sha256 = binding.get("sha256")
        if (
            not isinstance(bound_path, str)
            or not Path(bound_path).is_absolute()
            or actual != Path(bound_path).resolve()
            or not isinstance(bound_sha256, str)
            or not HEX_SHA256.fullmatch(bound_sha256)
        ):
            raise ToolchainError(f"external {label} binding differs")
        return open_verified_file(actual, bound_sha256), bound_sha256

    trust_fd, repository_trust_sha256 = open_binding(
        trust_path.resolve(), repository_binding, "repository trust"
    )
    lock_fd, external_lock_sha256 = open_binding(
        lock_path.resolve(), lock_binding, "toolchain lock"
    )
    try:
        trust = _load_fd(trust_fd, str(trust_path))
        lock = _load_fd(lock_fd, str(lock_path))
    finally:
        os.close(lock_fd)
        os.close(trust_fd)
    anchor = trust.get("execution_toolchain")
    if trust.get("state") != "trusted" or not isinstance(anchor, dict):
        raise ToolchainError(f"{trust_path}: execution toolchain trust is not active")
    expected_path = anchor.get("lock_path")
    expected_sha256 = anchor.get("lock_sha256")
    if (
        not isinstance(expected_path, str)
        or not expected_path
        or Path(expected_path).is_absolute()
        or not isinstance(expected_sha256, str)
        or not HEX_SHA256.fullmatch(expected_sha256)
    ):
        raise ToolchainError(f"{trust_path}: execution toolchain anchor is incomplete")
    protected_path = (trust_path.parent / expected_path).resolve()
    if lock_path.resolve() != protected_path:
        raise ToolchainError("caller-selected execution toolchain is not trusted")
    if expected_sha256 != external_lock_sha256:
        raise ToolchainError(
            "repository and external authority bind different toolchain bytes"
        )
    if repository_binding.get("sha256") != repository_trust_sha256:
        raise ToolchainError("repository trust descriptor binding changed")
    if (
        lock.get("schema")
        != "fs2-serve.nebius.ai/execution-toolchain-lock/v1"
        or lock.get("state") != "trusted"
    ):
        raise ToolchainError(f"{protected_path}: execution toolchain is not accepted")
    source_root = os.environ.get("FS2_CAPSULE_SOURCE_ROOT", "")
    source_binding = external.get("source_root")
    source_tree_sha256 = (
        source_binding.get("tree_sha256") if isinstance(source_binding, dict) else None
    )
    if (
        not source_root
        or not Path(source_root).is_absolute()
        or not isinstance(source_binding, dict)
        or source_binding.get("path") != str(Path(source_root).resolve())
        or not isinstance(source_tree_sha256, str)
        or not HEX_SHA256.fullmatch(source_tree_sha256)
        or source_binding.get("transport")
        not in {"fs-verity-read-only-mount", "sealed-read-only-private-mount"}
        or _tree_manifest(Path(source_root)) != source_tree_sha256
    ):
        raise ToolchainError("capsule source is not the externally bound read-only tree")
    tool_directory = os.environ.get("FS2_CAPSULE_TOOL_DIR", "")
    tool_binding = external.get("tool_dispatch_directory")
    tool_tree_sha256 = (
        tool_binding.get("tree_sha256") if isinstance(tool_binding, dict) else None
    )
    if (
        not tool_directory
        or not Path(tool_directory).is_absolute()
        or not isinstance(tool_binding, dict)
        or tool_binding.get("path") != str(Path(tool_directory).resolve())
        or not isinstance(tool_tree_sha256, str)
        or not HEX_SHA256.fullmatch(tool_tree_sha256)
        or tool_binding.get("transport") != "sealed-read-only-private-mount"
        or _tree_manifest(Path(tool_directory)) != tool_tree_sha256
    ):
        raise ToolchainError("capsule tools are not the externally bound read-only tree")
    return lock


def validated_tool(
    name: str, *, lock_path: Path, trust_path: Path
) -> tuple[Path, str]:
    lock = _anchored_lock(lock_path, trust_path)
    tools = lock.get("tools")
    binding = tools.get(name) if isinstance(tools, dict) else None
    path_value = binding.get("path") if isinstance(binding, dict) else None
    expected = binding.get("sha256") if isinstance(binding, dict) else None
    if (
        not isinstance(path_value, str)
        or not Path(path_value).is_absolute()
        or not isinstance(expected, str)
    ):
        raise ToolchainError(f"execution tool {name!r} is not exactly bound")
    path = Path(path_value)
    tool_directory = Path(os.environ["FS2_CAPSULE_TOOL_DIR"]).resolve()
    if path.resolve().parent != tool_directory:
        raise ToolchainError(f"execution tool {name!r} is outside capsule tool directory")
    descriptor = open_verified_file(path, expected)
    os.close(descriptor)
    return path, expected


def validate_source_file(
    relative: str, *, source_root: Path, lock_path: Path, trust_path: Path
) -> str:
    lock = _anchored_lock(lock_path, trust_path)
    source_files = lock.get("source_files")
    expected = source_files.get(relative) if isinstance(source_files, dict) else None
    if not isinstance(expected, str):
        raise ToolchainError(f"execution source {relative!r} is not measured")
    path = (source_root / relative).resolve()
    try:
        path.relative_to(source_root.resolve())
    except ValueError as exc:
        raise ToolchainError(f"execution source {relative!r} escapes source root") from exc
    descriptor = open_verified_file(path, expected)
    os.close(descriptor)
    return expected


def validate_current_python(*, lock_path: Path, trust_path: Path) -> None:
    lock = _anchored_lock(lock_path, trust_path)
    runtime = lock.get("python")
    if not isinstance(runtime, dict):
        raise ToolchainError("Python execution runtime is not bound")
    executable = runtime.get("executable")
    expected = runtime.get("sha256")
    if (
        not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or not isinstance(expected, str)
        or Path(sys.executable).resolve() != Path(executable).resolve()
    ):
        raise ToolchainError("current Python executable differs from the reviewed runtime")
    descriptor = open_verified_file(Path(executable), expected)
    os.close(descriptor)
    if runtime.get("version") != sys.version or runtime.get("implementation") != "CPython":
        raise ToolchainError("current Python version differs from the reviewed runtime")
    if not (sys.flags.isolated and sys.flags.ignore_environment and sys.flags.no_user_site):
        raise ToolchainError("Python release gates require -I isolated execution")
    expected_path = runtime.get("sys_path")
    if not isinstance(expected_path, list) or expected_path != list(sys.path):
        raise ToolchainError("Python import path differs from the reviewed runtime")
    roots = runtime.get("distribution_roots")
    if not isinstance(roots, list) or not roots:
        raise ToolchainError("Python distribution roots are not bound")
    for entry in roots:
        if not isinstance(entry, dict):
            raise ToolchainError("Python distribution binding is malformed")
        path_value = entry.get("path")
        digest = entry.get("tree_sha256")
        if (
            not isinstance(path_value, str)
            or not Path(path_value).is_absolute()
            or not isinstance(digest, str)
            or not HEX_SHA256.fullmatch(digest)
            or _tree_manifest(Path(path_value)) != digest
        ):
            raise ToolchainError("Python distribution tree differs from reviewed runtime")


def environment_toolchain() -> tuple[Path, Path]:
    lock = os.environ.get("FS2_IMAGE_GATE_TOOLCHAIN", "")
    trust = os.environ.get("FS2_IMAGE_GATE_TRUST", "")
    if not lock or not trust:
        raise ToolchainError("image gate toolchain and trust environment are mandatory")
    return Path(lock), Path(trust)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-python")
    validate.add_argument("--trust", required=True, type=Path)
    validate.add_argument("--toolchain", required=True, type=Path)
    args = parser.parse_args()
    try:
        validate_current_python(
            lock_path=args.toolchain.resolve(), trust_path=args.trust.resolve()
        )
    except ToolchainError as exc:
        print(f"execution toolchain: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
