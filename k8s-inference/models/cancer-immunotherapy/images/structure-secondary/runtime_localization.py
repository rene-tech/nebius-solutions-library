#!/usr/bin/env python3
"""Strict validation for controller-issued runtime artifact localization markers."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from uuid import UUID


RUNTIME_LOCALIZATION_SCHEMA = "fs2-serve.nebius.ai/runtime-localization-marker/v1"
MAX_MARKER_BYTES = 64 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOGICAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DNS_ID = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class RuntimeArtifactExpectation:
    artifact_id: str
    mount_path: str
    content_sha256: str
    expected_manifest_sha256: str | None = None
    sub_path: str | None = None

    def __post_init__(self) -> None:
        if _LOGICAL_ID.fullmatch(self.artifact_id) is None:
            raise ValueError("runtime artifact expectation has an invalid artifact ID")
        mount = Path(self.mount_path)
        if (
            not mount.is_absolute()
            or ".." in mount.parts
            or mount.as_posix() != self.mount_path
        ):
            raise ValueError("runtime artifact expectation has an invalid mount path")
        if _SHA256.fullmatch(self.content_sha256) is None:
            raise ValueError("runtime artifact expectation has an invalid content SHA-256")
        if (
            self.expected_manifest_sha256 is not None
            and _SHA256.fullmatch(self.expected_manifest_sha256) is None
        ):
            raise ValueError("runtime artifact expectation has an invalid manifest SHA-256")
        if self.sub_path is not None:
            relative = Path(self.sub_path)
            if (
                relative.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.parts)
                or not self.sub_path
                or relative.as_posix() != self.sub_path
            ):
                raise ValueError("runtime artifact expectation has an invalid subpath")


def _bounded_identity(value: object, label: str, *, dns: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise SystemExit(f"runtime localization marker {label} is invalid")
    if dns and _DNS_ID.fullmatch(value) is None:
        raise SystemExit(f"runtime localization marker {label} is invalid")
    return value


def _optional_environment_match(marker: dict[str, object], field: str, env_name: str) -> None:
    expected = os.environ.get(env_name)
    if expected and marker[field] != expected:
        raise SystemExit(
            f"runtime localization marker {field} does not match {env_name}"
        )


def validate_runtime_localization(
    marker_path: str | Path,
    *,
    model_id: str,
    variant_id: str,
    stage_id: str,
    artifacts: tuple[RuntimeArtifactExpectation, ...],
) -> dict[str, object]:
    """Validate one small controller marker against the image's immutable contract."""

    path = Path(marker_path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise SystemExit(
            f"runtime-localization-marker must be an existing absolute regular file: {path}"
        )
    if path.stat().st_size < 2 or path.stat().st_size > MAX_MARKER_BYTES:
        raise SystemExit("runtime localization marker size is outside the bound")
    configured_path = os.environ.get("FS2_RUNTIME_LOCALIZATION_MARKER")
    if configured_path and Path(configured_path) != path:
        raise SystemExit(
            "runtime-localization-marker does not match FS2_RUNTIME_LOCALIZATION_MARKER"
        )
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"runtime localization marker is invalid JSON: {exc}") from exc
    required = {
        "schema",
        "operation_id",
        "attempt_id",
        "tenant_id",
        "model_id",
        "variant_id",
        "stage_id",
        "artifacts",
    }
    if not isinstance(marker, dict) or set(marker) != required:
        raise SystemExit("runtime localization marker fields differ from the v1 contract")
    if marker["schema"] != RUNTIME_LOCALIZATION_SCHEMA:
        raise SystemExit("runtime localization marker schema is unsupported")
    for field in ("operation_id", "attempt_id"):
        value = _bounded_identity(marker[field], field)
        try:
            UUID(value)
        except ValueError as exc:
            raise SystemExit(f"runtime localization marker {field} is not a UUID") from exc
    _bounded_identity(marker["tenant_id"], "tenant_id")
    if marker["model_id"] != model_id:
        raise SystemExit("runtime localization marker model_id differs from the image contract")
    if marker["variant_id"] != variant_id:
        raise SystemExit("runtime localization marker variant_id differs from the image contract")
    if marker["stage_id"] != stage_id:
        raise SystemExit("runtime localization marker stage_id differs from the image contract")
    _bounded_identity(marker["model_id"], "model_id", dns=True)
    _bounded_identity(marker["variant_id"], "variant_id", dns=True)
    _bounded_identity(marker["stage_id"], "stage_id", dns=True)
    for field, env_name in (
        ("operation_id", "FS2_OPERATION_ID"),
        ("attempt_id", "FS2_ATTEMPT_ID"),
        ("tenant_id", "FS2_TENANT_ID"),
        ("variant_id", "FS2_VARIANT_ID"),
        ("stage_id", "FS2_STAGE_ID"),
    ):
        _optional_environment_match(marker, field, env_name)

    expected_by_id = {item.artifact_id: item for item in artifacts}
    if not artifacts or len(expected_by_id) != len(artifacts):
        raise ValueError("runtime artifact expectations must be non-empty and unique")
    raw_artifacts = marker["artifacts"]
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) != len(artifacts):
        raise SystemExit("runtime localization marker artifact cardinality differs")
    observed: set[str] = set()
    legacy_artifact_fields = {
        "artifact_id",
        "mount_path",
        "content_digest",
        "localization_receipt_digest",
        "sub_path",
        "expected_manifest_sha256",
        "readiness_receipt_sha256",
        "authorization_receipt_sha256",
    }
    current_artifact_fields = (
        legacy_artifact_fields
        - {"expected_manifest_sha256"}
        | {
            "artifact_manifest_sha256",
            "verification_receipt",
            "files",
            "aggregate_tree",
        }
    )
    access_receipt = os.environ.get("FS2_ARTIFACT_ACCESS_RECEIPT_DIGEST", "")
    if access_receipt:
        if _DIGEST.fullmatch(access_receipt) is None:
            raise SystemExit("FS2_ARTIFACT_ACCESS_RECEIPT_DIGEST is invalid")
        expected_authorization = access_receipt.removeprefix("sha256:")
    else:
        expected_authorization = None
    for raw in raw_artifacts:
        if not isinstance(raw, dict) or set(raw) not in {
            frozenset(legacy_artifact_fields),
            frozenset(current_artifact_fields),
        }:
            raise SystemExit("runtime localization marker artifact fields differ")
        current_marker = "artifact_manifest_sha256" in raw
        manifest_sha256 = raw.get(
            "artifact_manifest_sha256"
            if current_marker
            else "expected_manifest_sha256"
        )
        artifact_id = raw.get("artifact_id")
        if not isinstance(artifact_id, str) or artifact_id in observed:
            raise SystemExit("runtime localization marker artifact IDs are invalid or duplicated")
        observed.add(artifact_id)
        expected = expected_by_id.get(artifact_id)
        if expected is None:
            raise SystemExit(f"runtime localization marker contains unexpected artifact {artifact_id}")
        localization_receipt = raw.get("localization_receipt_digest")
        readiness_receipt = raw.get("readiness_receipt_sha256")
        expected_manifest = expected.expected_manifest_sha256
        if current_marker and expected_manifest is None:
            expected_manifest = expected.content_sha256
        if current_marker:
            files = raw.get("files")
            aggregate_tree = raw.get("aggregate_tree")
            verification_receipt = raw.get("verification_receipt")
            if (
                not isinstance(files, list)
                or len(files) > 4096
                or bool(files) == (aggregate_tree is not None)
            ):
                raise SystemExit(
                    f"runtime localization marker evidence mode differs for {artifact_id}"
                )
            if files:
                if verification_receipt is not None:
                    raise SystemExit(
                        f"runtime localization marker verification receipt differs for {artifact_id}"
                    )
                paths: set[str] = set()
                for file in files:
                    if not isinstance(file, dict) or set(file) != {
                        "path",
                        "digest",
                        "size_bytes",
                    }:
                        raise SystemExit(
                            f"runtime localization marker file evidence differs for {artifact_id}"
                        )
                    file_path = file.get("path")
                    relative = Path(file_path) if isinstance(file_path, str) else None
                    if (
                        relative is None
                        or relative.is_absolute()
                        or any(part in {"", ".", ".."} for part in relative.parts)
                        or relative.as_posix() != file_path
                        or "\\" in file_path
                        or file_path in paths
                        or not isinstance(file.get("digest"), str)
                        or _DIGEST.fullmatch(file["digest"]) is None
                        or not isinstance(file.get("size_bytes"), int)
                        or isinstance(file["size_bytes"], bool)
                        or file["size_bytes"] < 0
                    ):
                        raise SystemExit(
                            f"runtime localization marker file evidence differs for {artifact_id}"
                        )
                    paths.add(file_path)
            elif (
                not isinstance(aggregate_tree, dict)
                or aggregate_tree.get("tree_digest") != raw.get("content_digest")
                or (
                    aggregate_tree.get("storage_kind") == "reference-data-plane"
                )
                != isinstance(verification_receipt, dict)
            ):
                raise SystemExit(
                    f"runtime localization marker aggregate evidence differs for {artifact_id}"
                )
        if (
            raw.get("mount_path") != expected.mount_path
            or raw.get("content_digest") != f"sha256:{expected.content_sha256}"
            or raw.get("sub_path") != expected.sub_path
            or manifest_sha256 != expected_manifest
            or (
                current_marker
                and (
                    not isinstance(manifest_sha256, str)
                    or _SHA256.fullmatch(manifest_sha256) is None
                )
            )
            or not isinstance(localization_receipt, str)
            or _DIGEST.fullmatch(localization_receipt) is None
            or readiness_receipt != localization_receipt.removeprefix("sha256:")
            or raw.get("authorization_receipt_sha256") != expected_authorization
        ):
            raise SystemExit(
                f"runtime localization marker does not bind exact artifact {artifact_id}"
            )
    if observed != set(expected_by_id):
        raise SystemExit("runtime localization marker omits a required artifact")
    return marker
