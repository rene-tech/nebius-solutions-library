"""Shell-free artifact materializer and collector processes for staged Jobs."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import yaml
import zstandard
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from .adapters import CollectedArtifactFile, CollectionPendingError, collect_stage_output
from .models import ArtifactMaterialization, MaterializationMode, StageInvocation

_ROOT = Path("/mnt/fs2-scientific")
_MAX_ARCHIVE_FILES = 4096
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


def _contained(path: Path, *, root: Path | None = None) -> Path:
    root = _ROOT if root is None else root
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=False)
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise ValueError("scientific companion path escapes its workspace")
    return resolved


def prepare_workspace(workspace: Path) -> None:
    """Create only the invocation directory beneath the controller-owned root."""

    target = _contained(workspace)
    target.mkdir(parents=True, exist_ok=False)
    target.chmod(0o700)


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or "\\" in value:
        raise ValueError("scientific archive contains an unsafe path")
    return path


def _extract_tar(payload: bytes, destination: Path, *, compression: str | None, overlay: bool) -> None:
    if len(payload) > _MAX_ARCHIVE_BYTES:
        raise ValueError("scientific input archive exceeds its compressed bound")
    destination = _contained(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if not overlay and any(destination.iterdir()):
        raise ValueError("scientific input archive requires an empty destination")
    if compression == "zstd":
        try:
            payload = zstandard.ZstdDecompressor().decompress(payload, max_output_size=_MAX_ARCHIVE_BYTES)
        except zstandard.ZstdError as error:
            raise ValueError("scientific input is not valid zstd data") from error
    mode: Literal["r:gz", "r:"] = "r:gz" if compression == "gzip" else "r:"
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode=mode) as archive:
            members = archive.getmembers()
            if not 1 <= len(members) <= _MAX_ARCHIVE_FILES:
                raise ValueError("scientific input archive member count is outside the bound")
            total = 0
            seen: set[PurePosixPath] = set()
            for member in members:
                relative = _safe_relative(member.name.rstrip("/"))
                if relative in seen or not (member.isdir() or member.isfile()):
                    raise ValueError("scientific input archive contains duplicate or unsupported entries")
                seen.add(relative)
                total += member.size
                if member.size < 0 or total > _MAX_ARCHIVE_BYTES:
                    raise ValueError("scientific input archive exceeds its extracted bound")
                target = _contained(destination.joinpath(*relative.parts))
                if target.exists() and not overlay:
                    raise ValueError("scientific input archive would overwrite a workspace entry")
                if any(item.is_symlink() for item in (target, *target.parents) if item != _ROOT):
                    raise ValueError("scientific input archive encountered a symbolic link")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=overlay)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("scientific input archive member has no payload")
                content = source.read(_MAX_ARCHIVE_BYTES + 1)
                if len(content) != member.size:
                    raise ValueError("scientific input archive member size differs")
                target.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(target, flags, 0o400)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
    except tarfile.TarError as error:
        raise ValueError("scientific input is not a valid tar archive") from error


def _rewrite_paths(value: object, root: Path, *, depth: int = 0) -> object:
    if depth > 32:
        raise ValueError("BoltzGen YAML nesting exceeds the bound")
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or key in result:
                raise ValueError("BoltzGen YAML contains invalid mapping keys")
            if key == "path" and item is not None:
                if not isinstance(item, str):
                    raise ValueError("BoltzGen YAML path must be a string")
                path = _contained(root.joinpath(*_safe_relative(item).parts), root=root)
                if not path.is_file() or path.is_symlink():
                    raise ValueError("BoltzGen YAML path does not resolve to a localized file")
                result[key] = str(path)
            else:
                result[key] = _rewrite_paths(item, root, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_rewrite_paths(item, root, depth=depth + 1) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise ValueError("BoltzGen YAML contains an unsupported value")


class WorkloadArtifactHttpClient:
    def __init__(self, *, base_url: str, capability: str, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=60, follow_redirects=False)
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {capability}"}

    def download(
        self,
        artifact_id: UUID,
        *,
        expected_digest: str,
        expected_size_bytes: int,
        expected_media_type: str,
    ) -> bytes:
        response = self.client.get(
            f"{self.base_url}/internal/scientific-workloads/artifacts/{artifact_id}:download",
            headers=self.headers,
        )
        response.raise_for_status()
        value = response.json()
        artifact = cast(dict[str, Any], value["artifact"])
        handle = cast(dict[str, Any], value["handle"])
        if (
            f"sha256:{artifact.get('sha256')}" != expected_digest
            or artifact.get("size_bytes") != expected_size_bytes
            or artifact.get("media_type") != expected_media_type
        ):
            raise ValueError("artifact service pointer differs from the frozen materialization")
        if handle.get("method") != "GET":
            raise ValueError("artifact service returned a non-download handle")
        stored = self.client.get(handle["url"], headers=handle.get("headers", {}))
        stored.raise_for_status()
        content = stored.content
        if len(content) != artifact["size_bytes"] or hashlib.sha256(content).hexdigest() != artifact["sha256"]:
            raise ValueError("downloaded artifact differs from its immutable pointer")
        return content

    def upload(
        self,
        *,
        identity: str,
        content: bytes,
        media_type: str,
        compression: str | None,
    ) -> dict[str, Any]:
        digest = hashlib.sha256(content).hexdigest()
        upload_id = uuid5(NAMESPACE_URL, f"fs2-scientific-upload:{identity}:{digest}")
        request: dict[str, Any] = {
            "upload_id": str(upload_id),
            "sha256": digest,
            "size_bytes": len(content),
            "media_type": media_type,
            "compression": compression,
        }
        begun = self.client.post(
            f"{self.base_url}/internal/scientific-workloads/uploads",
            headers=self.headers,
            json=request,
        )
        begun.raise_for_status()
        handle = begun.json()["handle"]
        if handle.get("method") != "PUT":
            raise ValueError("artifact service returned a non-upload handle")
        stored = self.client.put(handle["url"], headers=handle.get("headers", {}), content=content)
        stored.raise_for_status()
        finalized = self.client.post(
            f"{self.base_url}/internal/scientific-workloads/uploads/{upload_id}:finalize",
            headers=self.headers,
        )
        finalized.raise_for_status()
        return cast(dict[str, Any], finalized.json())

    def commit(
        self,
        *,
        handoff_artifact_id: str,
        handoff_ref: Mapping[str, Any],
        manifest_ref: Mapping[str, Any],
        validation_ref: Mapping[str, Any],
        semantic_valid: bool,
    ) -> None:
        response = self.client.post(
            f"{self.base_url}/internal/scientific-workloads/commit",
            headers=self.headers,
            json={
                "handoff_artifact_id": handoff_artifact_id,
                "handoff_digest": f"sha256:{handoff_ref['sha256']}",
                "handoff_size_bytes": handoff_ref["size_bytes"],
                "handoff_media_type": handoff_ref["media_type"],
                "handoff_compression": handoff_ref.get("compression"),
                "manifest_artifact_id": manifest_ref["artifact_id"],
                "validation_artifact_id": validation_ref["artifact_id"],
                "manifest_digest": f"sha256:{manifest_ref['sha256']}",
                "validation_digest": f"sha256:{validation_ref['sha256']}",
                "semantic_valid": semantic_valid,
            },
        )
        response.raise_for_status()


def materialize_artifact(
    *,
    client: WorkloadArtifactHttpClient,
    artifact_id: UUID,
    destination: Path,
    mode: MaterializationMode,
    compression: str | None,
    yaml_name: str | None,
    reuse_prefix: str | None,
    expected_digest: str,
    expected_size_bytes: int,
    expected_media_type: str,
) -> None:
    payload = client.download(
        artifact_id,
        expected_digest=expected_digest,
        expected_size_bytes=expected_size_bytes,
        expected_media_type=expected_media_type,
    )
    destination = _contained(destination)
    if mode is MaterializationMode.COPY_FILE:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
        return
    _extract_tar(
        payload,
        destination,
        compression=compression,
        overlay=mode in {MaterializationMode.OVERLAY_TAR, MaterializationMode.BOLTZGEN_INPUT},
    )
    if mode is MaterializationMode.BOLTZGEN_INPUT:
        if yaml_name is None:
            raise ValueError("BoltzGen input has no design YAML")
        root = (destination / "inputs").resolve(strict=True)
        yaml_path = _contained(root.joinpath(*_safe_relative(yaml_name).parts), root=root)
        raw = yaml_path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("BoltzGen YAML exceeds the bound")
        value = yaml.safe_load(raw)
        yaml_path.chmod(0o600)
        yaml_path.write_text(yaml.safe_dump(_rewrite_paths(value, root), sort_keys=False), encoding="utf-8")
        yaml_path.chmod(0o400)
        if reuse_prefix is not None:
            reuse = _contained(root.joinpath(*_safe_relative(reuse_prefix).parts), root=root)
            if not reuse.is_dir() or reuse.is_symlink():
                raise ValueError("BoltzGen reuse prefix is invalid")


def _invocation(value: str) -> StageInvocation:
    raw = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError("stage invocation is invalid")
    materializations = tuple(
        ArtifactMaterialization(
            artifact_id=item["artifact_id"],
            destination=item["destination"],
            mode=MaterializationMode(item["mode"]),
            compression=item["compression"],
            yaml_name=item["yaml_name"],
            reuse_prefix=item["reuse_prefix"],
        )
        for item in raw["materializations"]
    )
    return StageInvocation(
        stage_id=raw["stage_id"],
        shard_id=raw["shard_id"],
        argv=tuple(raw["argv"]),
        environment=tuple((item[0], item[1]) for item in raw["environment"]),
        working_directory=raw["working_directory"],
        consumes=tuple(raw["consumes"]),
        produces=raw["produces"],
        collector_id=raw["collector_id"],
        validator_id=raw["validator_id"],
        handoff_name=raw["handoff_name"],
        max_output_artifacts=raw["max_output_artifacts"],
        max_output_bytes=raw["max_output_bytes"],
        materializations=materializations,
        runtime_artifacts=tuple(raw["runtime_artifacts"]),
    )


def collect_and_commit(
    *,
    client: WorkloadArtifactHttpClient,
    collector_id: str,
    validator_id: str,
    invocation_json: str,
    workspace: Path,
    catalog_dir: Path,
    poll_seconds: float = 2,
    max_artifacts: int,
    max_output_bytes: int,
) -> None:
    invocation = _invocation(invocation_json)
    if (
        collector_id != invocation.collector_id
        or validator_id != invocation.validator_id
        or max_artifacts != invocation.max_output_artifacts
        or max_output_bytes != invocation.max_output_bytes
    ):
        raise ValueError("collector arguments differ from the canonical invocation")
    while True:
        try:
            collected = collect_stage_output(invocation, workspace)
            break
        except CollectionPendingError:
            time.sleep(poll_seconds)
    refs: dict[str, dict[str, Any]] = {}
    root = workspace.resolve(strict=True)
    if not 1 <= len(collected.artifacts) <= invocation.max_output_artifacts:
        raise ValueError("collector artifact count is outside the invocation bound")
    total_output_bytes = 0
    for item in collected.artifacts:
        if not isinstance(item, CollectedArtifactFile):
            raise TypeError("collector artifact has another controller type")
        path = item.path.resolve(strict=True)
        if root not in path.parents or not path.is_file() or path.is_symlink():
            raise ValueError("collector artifact is outside its contained workspace")
        content = path.read_bytes()
        total_output_bytes += len(content)
        if total_output_bytes > invocation.max_output_bytes:
            raise ValueError("collector output exceeds the invocation byte bound")
        refs[item.name] = client.upload(
            identity=f"{invocation.produces}:{item.name}",
            content=content,
            media_type=item.media_type,
            compression=item.compression,
        )
    if not refs:
        raise ValueError("collector produced no artifacts")
    manifest = {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
        "manifest_id": invocation.produces,
        "entries": [
            {
                "name": item.name,
                "semantic_type": item.semantic_type,
                "artifact": refs[item.name],
            }
            for item in collected.artifacts
        ],
    }
    schema = json.loads((catalog_dir / "schema/scientific-artifact-manifest.schema.json").read_text())
    Draft202012Validator(schema).validate(manifest)
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    validation = dict(collected.validation)
    semantic_valid = validation.get("status") == "passed" and validation.get("validator_id") == invocation.validator_id
    if not semantic_valid:
        raise ValueError("collector semantic validation did not pass the bound validator")
    validation_bytes = json.dumps(validation, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    manifest_ref = client.upload(
        identity=f"{invocation.produces}:manifest",
        content=manifest_bytes,
        media_type="application/vnd.fs2.scientific-manifest+json",
        compression=None,
    )
    validation_ref = client.upload(
        identity=f"{invocation.produces}:validation",
        content=validation_bytes,
        media_type="application/json",
        compression=None,
    )
    if invocation.handoff_name is not None and invocation.handoff_name not in refs:
        raise ValueError("collector omitted the invocation's exact handoff entry")
    handoff = manifest_ref if invocation.handoff_name is None else refs[invocation.handoff_name]
    client.commit(
        handoff_artifact_id=handoff["artifact_id"],
        handoff_ref=handoff,
        manifest_ref=manifest_ref,
        validation_ref=validation_ref,
        semantic_valid=semantic_valid,
    )
