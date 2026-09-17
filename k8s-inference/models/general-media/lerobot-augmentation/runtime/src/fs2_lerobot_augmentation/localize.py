"""Resolve admitted dataset references into a run-local directory."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, cast

from .contracts import HuggingFaceSource, ObjectStoreSource, UploadedBundleSource
from .dataset import DatasetError, extract_uploaded_bundle, sha256_file, write_bundle_manifest

SOURCE_REFERENCE_SCHEMA = "fs2-serve.nebius.ai/lerobot-source-reference/v1"
MAX_SOURCE_REFERENCE_BYTES = 64 * 1024


def _bindings(name: str) -> Mapping[str, Any]:
    raw = os.getenv(name, "{}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise DatasetError(f"operator credential binding map {name} is invalid") from error
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise DatasetError(f"operator credential binding map {name} is invalid")
    return cast(Mapping[str, Any], value)


def _new_destination(path: Path) -> None:
    if path.exists():
        raise DatasetError("localized dataset destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True)


def verify_source_reference(
    path: Path,
    source: HuggingFaceSource | ObjectStoreSource,
) -> None:
    """Bind a non-artifact source parameter to its admitted manifest entry."""

    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_SOURCE_REFERENCE_BYTES:
        raise DatasetError("dataset source-reference artifact is unavailable or oversized")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise DatasetError("dataset source-reference artifact is invalid JSON") from error
    expected = {"schema": SOURCE_REFERENCE_SCHEMA, "source": asdict(source)}
    if value != expected:
        raise DatasetError("dataset source-reference artifact differs from the submitted source")


def localize_huggingface(source: HuggingFaceSource, destination: Path) -> Path:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise DatasetError("huggingface-hub is required for a Hugging Face dataset source") from error
    token: str | bool | None = False
    if source.credential_binding is not None:
        binding = _bindings("FS2_HF_CREDENTIAL_BINDINGS_JSON").get(source.credential_binding)
        if not isinstance(binding, Mapping) or set(binding) != {"token_env"}:
            raise DatasetError("authorized Hugging Face credential binding is unavailable")
        token_env = binding["token_env"]
        if not isinstance(token_env, str) or not token_env.startswith("FS2_SECRET_"):
            raise DatasetError(
                "Hugging Face credential binding does not reference an allowed secret environment variable"
            )
        token_value = os.getenv(token_env)
        if not token_value:
            raise DatasetError("Hugging Face credential binding secret is unavailable")
        token = token_value
    _new_destination(destination)
    try:
        snapshot_download(
            repo_id=source.repo_id,
            repo_type="dataset",
            revision=source.revision,
            local_dir=destination,
            token=token,
        )
        # An immutable Hub revision proves the remote source identity. Materialize
        # the complete inventory required of all source forms before validation.
        write_bundle_manifest(destination)
    except Exception as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise DatasetError("Hugging Face dataset localization failed") from error
    return destination


def localize_object_store(source: ObjectStoreSource, destination: Path) -> Path:
    try:
        import boto3
    except ImportError as error:
        raise DatasetError("boto3 is required for an object-store dataset source") from error
    binding = _bindings("FS2_OBJECT_STORE_BINDINGS_JSON").get(source.storage_binding)
    if not isinstance(binding, Mapping):
        raise DatasetError("authorized object-store binding is unavailable")
    allowed = {"bucket", "endpoint_url", "region_name", "access_key_env", "secret_key_env", "session_token_env"}
    if set(binding) - allowed or not isinstance(binding.get("bucket"), str):
        raise DatasetError("authorized object-store binding is invalid")
    credentials: dict[str, str] = {}
    for binding_key, argument in (
        ("access_key_env", "aws_access_key_id"),
        ("secret_key_env", "aws_secret_access_key"),
        ("session_token_env", "aws_session_token"),
    ):
        environment_name = binding.get(binding_key)
        if environment_name is not None:
            if not isinstance(environment_name, str) or not environment_name.startswith("FS2_SECRET_"):
                raise DatasetError("object-store binding references an invalid secret environment variable")
            value = os.getenv(environment_name)
            if not value:
                raise DatasetError("object-store binding secret is unavailable")
            credentials[argument] = value
    _new_destination(destination)
    destination.mkdir(parents=True)
    try:
        client = boto3.client(
            "s3",
            endpoint_url=binding.get("endpoint_url"),
            region_name=binding.get("region_name"),
            **credentials,
        )
        prefix = source.object_prefix.rstrip("/") + "/"
        paginator = client.get_paginator("list_objects_v2")
        count = 0
        for page in paginator.paginate(Bucket=binding["bucket"], Prefix=prefix):
            for item in page.get("Contents", []):
                key = item.get("Key")
                if not isinstance(key, str) or not key.startswith(prefix) or key.endswith("/"):
                    continue
                relative = PurePosixPath(key[len(prefix) :])
                if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                    raise DatasetError("object-store source contains an unsafe object key")
                count += 1
                if count > 100_000:
                    raise DatasetError("object-store source exceeds the file-count bound")
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(binding["bucket"], key, str(target))
        if count == 0:
            raise DatasetError("object-store source prefix is empty")
    except DatasetError:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    except Exception as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise DatasetError("object-store dataset localization failed") from error
    manifest = destination / "fs2-bundle-manifest.json"
    if not manifest.is_file() or sha256_file(manifest) != source.manifest_sha256:
        shutil.rmtree(destination, ignore_errors=True)
        raise DatasetError("object-store dataset manifest is missing or differs from admission")
    return destination


def localize_uploaded(source: UploadedBundleSource, destination: Path, artifact: Path) -> Path:
    if not artifact.is_file() or artifact.is_symlink():
        raise DatasetError("uploaded dataset artifact is unavailable or unsafe")
    if artifact.stat().st_size != source.size_bytes:
        raise DatasetError("uploaded dataset artifact size differs from admission")
    extract_uploaded_bundle(artifact, destination, expected_sha256=source.sha256)
    return destination
