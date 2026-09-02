"""Bounded archive extraction and BoltzGen design-YAML localization.

The public API supplies only logical artifact identities. The controller calls
these helpers after resolving an artifact into bytes and before starting model
argv. Nothing from the uploaded archive can select a host path.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import tarfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import cast

import yaml

from ..models import ArtifactMaterialization, MaterializationMode
from .common import ScientificAdapterError

HANDOFF_SCHEMA = "fs2-serve.nebius.ai/relocatable-stage-handoff/v1"

MAX_ARCHIVE_FILES = 4_096
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_YAML_BYTES = 1 * 1024 * 1024
MAX_YAML_DEPTH = 32


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.Node, deep: bool = False) -> object:
    result: dict[object, object] = {}
    for key_node, value_node in cast(yaml.MappingNode, node).value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ScientificAdapterError("BoltzGen YAML mapping keys must be strings")
        if key in result:
            raise ScientificAdapterError(f"BoltzGen YAML contains duplicate field {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _safe_relative_path(value: str, *, label: str) -> PurePosixPath:
    if (
        not value
        or "\\" in value
        or any(character == "\x7f" or ord(character) < 32 for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ScientificAdapterError(f"{label} is not a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ScientificAdapterError(f"{label} is not a safe relative POSIX path")
    return path


def _tar_stream(payload: bytes, compression: str | None) -> io.BytesIO:
    if compression == "zstd":
        try:
            import zstandard
        except ImportError as error:  # pragma: no cover - packaging guards this branch
            raise ScientificAdapterError("zstd artifact support is not installed") from error
        try:
            payload = zstandard.ZstdDecompressor().decompress(payload, max_output_size=MAX_ARCHIVE_BYTES * 2)
        except zstandard.ZstdError as error:
            raise ScientificAdapterError("input artifact is not valid zstd data") from error
    elif compression not in {None, "none", "gzip"}:
        raise ScientificAdapterError("input artifact compression is unsupported")
    return io.BytesIO(payload)


def safe_extract_tar(
    payload: bytes,
    destination: Path,
    *,
    compression: str | None = None,
    maximum_files: int = MAX_ARCHIVE_FILES,
    maximum_bytes: int = MAX_ARCHIVE_BYTES,
    expected_members: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    """Extract regular files/directories without tarfile path or link behavior."""

    if not isinstance(payload, bytes) or len(payload) > maximum_bytes:
        raise ScientificAdapterError("input archive exceeds its compressed byte bound")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve(strict=True)
    extracted: list[Path] = []
    seen: set[PurePosixPath] = set()
    total = 0
    try:
        stream = _tar_stream(payload, compression)
        archive_context = (
            tarfile.open(fileobj=stream, mode="r:gz")
            if compression == "gzip"
            else tarfile.open(fileobj=stream, mode="r:")
        )
        with archive_context as archive:
            members = archive.getmembers()
            if not 1 <= len(members) <= maximum_files:
                raise ScientificAdapterError("input archive member count is outside the bound")
            for member in members:
                relative = _safe_relative_path(member.name.rstrip("/"), label="archive member")
                if relative in seen:
                    raise ScientificAdapterError("input archive contains duplicate normalized paths")
                seen.add(relative)
                if not (member.isdir() or member.isfile()):
                    raise ScientificAdapterError("input archive may contain only regular files and directories")
                total += member.size
                if member.size < 0 or total > maximum_bytes:
                    raise ScientificAdapterError("input archive exceeds its extracted byte bound")
                target = root.joinpath(*relative.parts)
                if root not in target.resolve(strict=False).parents:
                    raise ScientificAdapterError("archive member escapes the localization root")
                target.parent.mkdir(parents=True, exist_ok=True)
                if any(parent.is_symlink() for parent in (target, *target.parents) if parent != root):
                    raise ScientificAdapterError("archive extraction encountered a symbolic link")
                if member.isdir():
                    target.mkdir(exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ScientificAdapterError("archive regular file has no payload")
                data = source.read(maximum_bytes + 1)
                if len(data) != member.size:
                    raise ScientificAdapterError("archive member size does not match its payload")
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(target, flags, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                extracted.append(target)
    except (tarfile.TarError, OSError) as error:
        if isinstance(error, ScientificAdapterError):
            raise
        raise ScientificAdapterError("input artifact is not a safe tar archive") from error
    result = tuple(extracted)
    verify_expected_members(result, destination, expected_members)
    return result


def _rewrite_yaml_paths(value: object, *, root: Path, depth: int = 0) -> object:
    if depth > MAX_YAML_DEPTH:
        raise ScientificAdapterError("BoltzGen YAML nesting exceeds the bound")
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise ScientificAdapterError("BoltzGen YAML mapping keys must be strings")
            if raw_key == "path" and raw_value is not None:
                if not isinstance(raw_value, str):
                    raise ScientificAdapterError("BoltzGen YAML path values must be strings or null")
                relative = _safe_relative_path(raw_value, label="BoltzGen YAML path")
                resolved = root.joinpath(*relative.parts).resolve(strict=True)
                if root not in resolved.parents or not resolved.is_file() or resolved.is_symlink():
                    raise ScientificAdapterError("BoltzGen YAML path does not resolve to a localized regular file")
                result[raw_key] = str(resolved)
            else:
                result[raw_key] = _rewrite_yaml_paths(raw_value, root=root, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_rewrite_yaml_paths(item, root=root, depth=depth + 1) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ScientificAdapterError("BoltzGen YAML contains a non-finite number")
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise ScientificAdapterError("BoltzGen YAML contains an unsupported value")


def rewrite_boltzgen_yaml(localization_root: Path, yaml_name: str) -> Path:
    """Rewrite every nested ``path`` value to a contained controller path."""

    root = localization_root.resolve(strict=True)
    relative = _safe_relative_path(yaml_name, label="BoltzGen design YAML")
    yaml_path = root.joinpath(*relative.parts).resolve(strict=True)
    if root not in yaml_path.parents or not yaml_path.is_file() or yaml_path.is_symlink():
        raise ScientificAdapterError("BoltzGen design YAML is not a localized regular file")
    payload = yaml_path.read_bytes()
    if len(payload) > MAX_YAML_BYTES:
        raise ScientificAdapterError("BoltzGen design YAML exceeds the byte bound")
    try:
        value = yaml.load(payload, Loader=_UniqueKeyLoader)  # noqa: S506 - subclass of SafeLoader
    except (yaml.YAMLError, UnicodeError) as error:
        raise ScientificAdapterError("BoltzGen design YAML is invalid") from error
    rewritten = _rewrite_yaml_paths(value, root=root)
    yaml_path.write_text(yaml.safe_dump(rewritten, sort_keys=False), encoding="utf-8")
    return yaml_path


def materialize_boltzgen_input(
    payload: bytes,
    destination: Path,
    *,
    yaml_name: str,
    compression: str | None,
    reuse_prefix: str | None = None,
) -> Path:
    input_root = destination / "inputs"
    safe_extract_tar(payload, input_root, compression=compression)
    rewritten = rewrite_boltzgen_yaml(input_root, yaml_name)
    if reuse_prefix is not None:
        relative = _safe_relative_path(reuse_prefix, label="BoltzGen reuse prefix")
        source = input_root.joinpath(*relative.parts).resolve(strict=True)
        localized = input_root.resolve(strict=True)
        target_root = destination.resolve(strict=True)
        if localized not in source.parents or not source.is_dir() or source.is_symlink():
            raise ScientificAdapterError("BoltzGen reuse prefix is not a localized directory")
        for candidate in sorted(source.rglob("*")):
            if candidate.is_symlink() or not (candidate.is_dir() or candidate.is_file()):
                raise ScientificAdapterError("BoltzGen reuse tree contains an unsafe entry")
            target = target_root / candidate.relative_to(source)
            if target_root not in target.resolve(strict=False).parents:
                raise ScientificAdapterError("BoltzGen reuse entry escapes the campaign workspace")
            if candidate.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(candidate.read_bytes())
    return rewritten


def materialize_stage_input(payload: bytes, specification: ArtifactMaterialization) -> tuple[Path, ...]:
    """Execute the bounded controller localization encoded in a StageInvocation."""

    destination = Path(specification.destination)
    if specification.mode is MaterializationMode.COPY_FILE:
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise ScientificAdapterError("input artifact exceeds its byte bound")
        destination.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(destination, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
        except OSError as error:
            raise ScientificAdapterError("input artifact cannot be copied safely") from error
        return (destination,)
    if specification.mode in {MaterializationMode.EXTRACT_TAR, MaterializationMode.OVERLAY_TAR}:
        return safe_extract_tar(
            payload,
            destination,
            compression=specification.compression,
            expected_members=specification.expected_members,
        )
    if specification.mode is MaterializationMode.BOLTZGEN_INPUT:
        rewritten = materialize_boltzgen_input(
            payload,
            destination,
            yaml_name=specification.yaml_name or "",
            compression=specification.compression,
            reuse_prefix=specification.reuse_prefix,
        )
        return (rewritten,)
    raise ScientificAdapterError("materialization mode is not implemented")


def validate_relocatable_handoff(root: Path, *, artifact_id: str) -> Path:
    """Validate a relocated processed payload without trusting producer paths."""

    localized = root.resolve(strict=True)
    marker_path = localized / "provenance.json"
    processed_path = localized / "processed.json"
    if marker_path.is_symlink() or processed_path.is_symlink():
        raise ScientificAdapterError("stage handoff members cannot be symbolic links")
    try:
        marker_raw = marker_path.read_bytes()
        processed_sha256 = hashlib.sha256(processed_path.read_bytes()).hexdigest()
        marker = json.loads(marker_raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScientificAdapterError("stage handoff is missing valid processed data or provenance") from error
    expected_keys = {"schema", "artifact_id", "member", "sha256"}
    if (
        not isinstance(marker, dict)
        or set(marker) != expected_keys
        or marker.get("schema") != HANDOFF_SCHEMA
        or marker.get("artifact_id") != artifact_id
        or marker.get("member") != "processed.json"
        or marker.get("sha256") != processed_sha256
    ):
        raise ScientificAdapterError("stage handoff provenance does not bind the relocated processed artifact")
    return processed_path


def verify_expected_members(paths: tuple[Path, ...], root: Path, expected: tuple[str, ...]) -> None:
    """Fail closed unless an extracted handoff has exactly its declared files."""

    if not expected:
        return
    localized = root.resolve(strict=True)
    observed = {
        str(path.resolve(strict=True).relative_to(localized))
        for path in paths
        if path.is_file()
    }
    if observed != set(expected):
        raise ScientificAdapterError("localized stage artifact does not have the exact expected members")
