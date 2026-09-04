"""Shell-free artifact materializer and collector processes for staged Jobs."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import yaml
import zstandard
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from .adapters import CollectedArtifactFile, CollectionPendingError, collect_stage_output
from .adapters.production_registry import install_production_adapters
from .models import (
    ArtifactMaterialization,
    MaterializationMode,
    RuntimeArtifactAdmissionRole,
    RuntimeArtifactAdmissionSpec,
    RuntimeArtifactMount,
    StageInvocation,
    StageWorkspaceDocument,
)

install_production_adapters()

_ROOT = Path("/mnt/fs2-scientific")
_MAX_ARCHIVE_FILES = 4096
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_RUNTIME_MARKER_BYTES = 64 * 1024
RUNTIME_LOCALIZATION_SCHEMA = "fs2-serve.nebius.ai/runtime-localization-marker/v1"
RUNTIME_TREE_IDENTITY_SCHEMA = "fs2-serve.nebius.ai/scientific-localization-generation-marker/v1"
RUNTIME_TREE_IDENTITY_FILE = ".fs2-runtime-tree.json"
REFERENCE_DATA_MANIFEST_SCHEMA = "fs2-serve.nebius.ai/reference-data-manifest/v1"
REFERENCE_DATA_TREE_MARKER = ".fs2-manifest-sha256"


class CollectionDeadlineError(RuntimeError):
    """The model published no collectable output inside the stage's bound.

    The collector shares its Pod with the model container under
    ``restartPolicy: Never``, so an unbounded wait keeps the whole Job -- and
    its admitted GPUs -- alive until ``activeDeadlineSeconds``.  Failing here
    instead exits the collector non-zero, which lets Kubernetes settle the Pod
    and hand the controller an ordinary application failure.
    """


def _runtime_marker(runtime_localization_json: str) -> dict[str, Any]:
    encoded = runtime_localization_json.encode()
    if len(encoded) > _MAX_RUNTIME_MARKER_BYTES:
        raise ValueError("runtime localization marker exceeds the bound")
    try:
        marker = json.loads(encoded)
    except (UnicodeError, ValueError) as error:
        raise ValueError("runtime localization marker is invalid") from error
    if (
        not isinstance(marker, dict)
        or marker.get("schema") != RUNTIME_LOCALIZATION_SCHEMA
        or set(marker)
        != {
            "schema",
            "operation_id",
            "attempt_id",
            "tenant_id",
            "model_id",
            "variant_id",
            "stage_id",
            "artifacts",
        }
        or not isinstance(marker.get("artifacts"), list)
        or len(marker["artifacts"]) > 64
    ):
        raise ValueError("runtime localization marker fields differ")
    for artifact in marker["artifacts"]:
        if not isinstance(artifact, dict) or set(artifact) != {
            "artifact_id",
            "mount_path",
            "content_digest",
            "artifact_manifest_sha256",
            "localization_receipt_digest",
            "sub_path",
            "readiness_receipt_sha256",
            "authorization_receipt_sha256",
            "verification_receipt",
            "files",
            "aggregate_tree",
        }:
            raise ValueError("runtime localization artifact fields differ")
        if not isinstance(artifact["files"], list) or len(artifact["files"]) > 4096:
            raise ValueError("runtime localization file evidence is not bounded")
        if bool(artifact["files"]) == (artifact["aggregate_tree"] is not None):
            raise ValueError("runtime localization requires one bounded identity mode")
        reference_plane = (
            isinstance(artifact["aggregate_tree"], dict)
            and artifact["aggregate_tree"].get("storage_kind") == "reference-data-plane"
        )
        if reference_plane != isinstance(artifact["verification_receipt"], dict):
            raise ValueError("reference-data localization requires exactly one terminal verification receipt")
    return cast(dict[str, Any], marker)


def _contained(path: Path, *, root: Path | None = None) -> Path:
    root = _ROOT if root is None else root
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=False)
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise ValueError("scientific companion path escapes its workspace")
    return resolved


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _admission_identity(artifact: Mapping[str, Any], identity_field: str) -> str:
    tree = artifact.get("aggregate_tree")
    values: dict[str, object] = {"content-digest": artifact.get("content_digest")}
    if isinstance(tree, Mapping):
        values.update(
            {
                "tree-digest": tree.get("tree_digest"),
                "manifest-digest": tree.get("manifest_digest"),
                "inventory-digest": tree.get("inventory_digest"),
            }
        )
    value = values.get(identity_field)
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError("runtime artifact admission identity is unavailable")
    return value.removeprefix("sha256:")


def _runtime_admission(invocation: StageInvocation, marker: Mapping[str, Any]) -> tuple[str, bytes] | None:
    spec = invocation.runtime_admission
    if spec is None:
        return None
    artifacts = {artifact["artifact_id"]: artifact for artifact in cast(list[dict[str, Any]], marker["artifacts"])}
    trees: list[dict[str, str]] = []
    for role in spec.roles:
        artifact = artifacts.get(role.artifact_id)
        if artifact is None or artifact.get("mount_path") != role.mount_path:
            raise ValueError("runtime artifact admission differs from verified localization")
        trees.append(
            {
                "role": role.role,
                "artifact_id": role.artifact_id,
                "root": role.mount_path,
                "sha256": _admission_identity(artifact, role.identity_field),
            }
        )
    value = {
        "schema": spec.schema,
        "generation": hashlib.sha256(
            json.dumps(marker, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
        "trees": trees,
    }
    return spec.relative_path, json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def prepare_workspace(
    workspace: Path,
    *,
    runtime_localization_json: str,
    stage_invocation_json: str,
) -> None:
    """Create the invocation directory and its trusted frozen documents."""

    target = _contained(workspace)
    target.mkdir(parents=True, exist_ok=False)
    target.chmod(0o700)
    marker = _runtime_marker(runtime_localization_json)
    invocation = _invocation(stage_invocation_json)
    if invocation.stage_id != marker["stage_id"]:
        raise ValueError("stage invocation differs from runtime localization marker")
    canonical = json.dumps(marker, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    marker_directory = target / ".fs2"
    marker_directory.mkdir(mode=0o700)
    receipt_directory = marker_directory / "runtime-artifacts"
    receipts = [
        (artifact["artifact_id"], artifact["verification_receipt"])
        for artifact in marker["artifacts"]
        if artifact["verification_receipt"] is not None
    ]
    if receipts:
        receipt_directory.mkdir(mode=0o700)
        for artifact_id, receipt in receipts:
            payload = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            _write_exclusive(receipt_directory / f"{artifact_id}.receipt.json", payload)
    for document in invocation.workspace_documents:
        _write_exclusive(
            target.joinpath(*PurePosixPath(document.relative_path).parts), document.canonical_json.encode()
        )
    admission = _runtime_admission(invocation, marker)
    if admission is not None:
        path, payload = admission
        _write_exclusive(target.joinpath(*PurePosixPath(path).parts), payload)
    temporary = marker_directory / ".runtime-localization.json.tmp"
    destination = marker_directory / "runtime-localization.json"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        directory = os.open(marker_directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def verify_runtime_artifacts(*, runtime_localization_json: str) -> None:
    """Verify every mounted localization before a schedulable model container starts.

    Small artifacts are checked byte-for-byte. Large immutable trees use a
    bounded identity sidecar produced by the trusted localizer; this avoids
    re-hashing multi-gigabyte model trees on every attempt while still proving
    that the content-addressed/published mount selected by the frozen execution
    map is the tree the localizer attested.
    """

    marker = _runtime_marker(runtime_localization_json)
    for raw_artifact in marker["artifacts"]:
        artifact = cast(dict[str, Any], raw_artifact)
        mount = Path(cast(str, artifact["mount_path"]))
        if not mount.is_absolute() or mount.is_symlink() or not mount.exists():
            raise ValueError("runtime artifact mount is absent or unsafe")
        raw_tree = artifact["aggregate_tree"]
        if raw_tree is not None:
            if (
                not mount.is_dir()
                or not isinstance(raw_tree, dict)
                or set(raw_tree)
                != {
                    "tree_digest",
                    "manifest_digest",
                    "inventory_digest",
                    "manifest_algorithm",
                    "file_count",
                    "directory_count",
                    "expanded_bytes",
                    "canonical_path",
                    "storage_kind",
                    "marker_relative_path",
                }
            ):
                raise ValueError("runtime aggregate-tree evidence differs")
            if artifact["content_digest"] != raw_tree["tree_digest"]:
                raise ValueError("runtime aggregate-tree content digest differs")
            if raw_tree["storage_kind"] == "localization-generation":
                _verify_localization_generation(artifact=artifact, mount=mount, tree=raw_tree)
            elif raw_tree["storage_kind"] == "reference-data-plane":
                _verify_reference_data_plane(artifact=artifact, mount=mount, tree=raw_tree)
            else:
                raise ValueError("runtime aggregate-tree storage kind is unsupported")
            continue

        files = cast(list[dict[str, Any]], artifact["files"])
        targets: tuple[tuple[Path, dict[str, Any]], ...]
        if mount.is_file():
            if len(files) != 1:
                raise ValueError("runtime file mount has an ambiguous manifest")
            targets = ((mount, files[0]),)
        elif mount.is_dir():
            targets = tuple((mount.joinpath(*_safe_relative(item["path"]).parts), item) for item in files)
        else:
            raise ValueError("runtime artifact mount is not a regular file or directory")
        for target, expected in targets:
            if target.is_symlink() or not target.is_file():
                raise ValueError("runtime artifact file is absent or unsafe")
            size = target.stat().st_size
            if size != expected["size_bytes"]:
                raise ValueError("runtime artifact file size differs")
            digest = hashlib.sha256()
            with target.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            if f"sha256:{digest.hexdigest()}" != expected["digest"]:
                raise ValueError("runtime artifact file digest differs")


def _bounded_json(path: Path, *, label: str) -> tuple[bytes, dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is absent or unsafe")
    payload = path.read_bytes()
    if len(payload) > _MAX_RUNTIME_MARKER_BYTES:
        raise ValueError(f"{label} exceeds the bound")
    try:
        value = json.loads(payload)
    except (UnicodeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return payload, cast(dict[str, Any], value)


def _verify_localization_generation(*, artifact: dict[str, Any], mount: Path, tree: dict[str, Any]) -> None:
    if tree["marker_relative_path"] != RUNTIME_TREE_IDENTITY_FILE:
        raise ValueError("runtime localization marker path differs")
    payload, identity = _bounded_json(mount / RUNTIME_TREE_IDENTITY_FILE, label="runtime aggregate-tree marker")
    if f"sha256:{hashlib.sha256(payload).hexdigest()}" != tree["manifest_digest"]:
        raise ValueError("runtime aggregate-tree marker digest differs")
    expected = {
        "schema": RUNTIME_TREE_IDENTITY_SCHEMA,
        "artifact_id": artifact["artifact_id"],
        "generation": tree["tree_digest"].removeprefix("sha256:"),
        "inventory_algorithm": tree["manifest_algorithm"],
        "inventory_sha256": tree["inventory_digest"].removeprefix("sha256:"),
        "entry_count": tree["file_count"],
        "directory_count": tree["directory_count"],
        "total_bytes": tree["expanded_bytes"],
        "sub_path": tree["canonical_path"],
        "read_only": True,
    }
    if any(identity.get(key) != value for key, value in expected.items()):
        raise ValueError("runtime aggregate-tree marker content differs")


def _verify_reference_data_plane(*, artifact: dict[str, Any], mount: Path, tree: dict[str, Any]) -> None:
    # The renderer freezes and enforces the production container root
    # (``/reference-data``).  Verification deliberately follows the mounted
    # path in the marker so the same verifier can be exercised against an
    # isolated filesystem fixture without weakening the render-time binding.
    if tree["marker_relative_path"] != REFERENCE_DATA_TREE_MARKER:
        raise ValueError("reference-data plane marker differs")
    root = mount.resolve(strict=True)
    dataset = mount.joinpath(*_safe_relative(tree["canonical_path"]).parts)
    resolved_dataset = dataset.resolve(strict=True)
    if dataset.is_symlink() or not dataset.is_dir() or root not in resolved_dataset.parents:
        raise ValueError("reference-data dataset is absent or unsafe")
    marker_path = dataset / REFERENCE_DATA_TREE_MARKER
    if marker_path.is_symlink() or not marker_path.is_file():
        raise ValueError("reference-data dataset publication marker is absent")
    marker = marker_path.read_text(encoding="utf-8").strip()
    manifest_digest = tree["manifest_digest"].removeprefix("sha256:")
    if marker != manifest_digest:
        raise ValueError("reference-data dataset publication marker differs")
    manifest_path = mount / "manifests" / "sha256" / f"{manifest_digest}.json"
    _, manifest = _bounded_json(manifest_path, label="reference-data publication manifest")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if hashlib.sha256(canonical).hexdigest() != manifest_digest:
        raise ValueError("reference-data publication manifest digest differs")
    content = manifest.get("content")
    expected_content = {
        "tree_sha256": tree["tree_digest"].removeprefix("sha256:"),
        "inventory_sha256": tree["inventory_digest"].removeprefix("sha256:"),
        "file_count": tree["file_count"],
        "expanded_bytes": tree["expanded_bytes"],
    }
    if (
        manifest.get("schema") != REFERENCE_DATA_MANIFEST_SCHEMA
        or manifest.get("bundle_id") != artifact["artifact_id"]
        or not isinstance(content, dict)
        or any(content.get(key) != value for key, value in expected_content.items())
    ):
        raise ValueError("reference-data publication manifest content differs")
    receipt = artifact["verification_receipt"]
    if not isinstance(receipt, dict):
        raise ValueError("reference-data terminal receipt is absent")
    receipt_storage = receipt.get("storage")
    receipt_content = receipt.get("content")
    canonical_receipt = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if (
        receipt.get("schema") != "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1"
        or receipt.get("bundle_id") != artifact["artifact_id"]
        or not isinstance(receipt_storage, dict)
        or receipt_storage.get("host_root") != "/mnt/fs2-reference-data/data"
        or receipt_storage.get("mount_path") != "/reference-data"
        or receipt_storage.get("dataset_sub_path") != tree["canonical_path"]
        or receipt_storage.get("read_only") is not True
        or not isinstance(receipt_content, dict)
        or receipt_content.get("tree_sha256") != tree["tree_digest"].removeprefix("sha256:")
        or receipt_content.get("manifest_sha256") != manifest_digest
        or receipt_content.get("inventory_sha256") != tree["inventory_digest"].removeprefix("sha256:")
        or receipt_content.get("inventory_marker") != tree["marker_relative_path"]
        or receipt_content.get("file_count") != tree["file_count"]
        or receipt_content.get("expanded_bytes") != tree["expanded_bytes"]
        or "sha256:" + hashlib.sha256(canonical_receipt).hexdigest() != artifact["localization_receipt_digest"]
    ):
        raise ValueError("reference-data terminal receipt differs from the mounted publication")


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
    runtime_mounts = tuple(
        RuntimeArtifactMount(
            artifact_id=item["artifact_id"],
            mount_path=item["mount_path"],
            sub_path=item["sub_path"],
            read_only=item["read_only"],
            expected_content_sha256=item["expected_content_sha256"],
            expected_manifest_sha256=item["expected_manifest_sha256"],
            authorization_receipt_sha256=item["authorization_receipt_sha256"],
            readiness_receipt_sha256=item["readiness_receipt_sha256"],
            supplemental_groups=tuple(item["supplemental_groups"]),
        )
        for item in raw["runtime_mounts"]
    )
    workspace_documents = tuple(
        StageWorkspaceDocument(relative_path=item["relative_path"], canonical_json=item["canonical_json"])
        for item in raw.get("workspace_documents", [])
    )
    raw_admission = raw.get("runtime_admission")
    runtime_admission = (
        None
        if raw_admission is None
        else RuntimeArtifactAdmissionSpec(
            schema=raw_admission["schema"],
            relative_path=raw_admission["relative_path"],
            roles=tuple(
                RuntimeArtifactAdmissionRole(
                    role=item["role"],
                    artifact_id=item["artifact_id"],
                    mount_path=item["mount_path"],
                    identity_field=item["identity_field"],
                )
                for item in raw_admission["roles"]
            ),
        )
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
        runtime_mounts=runtime_mounts,
        workspace_documents=workspace_documents,
        runtime_admission=runtime_admission,
    )


def collect_and_commit(
    *,
    client: WorkloadArtifactHttpClient,
    collector_id: str,
    validator_id: str,
    invocation_json: str,
    workspace: Path,
    catalog_dir: Path,
    collection_deadline_seconds: float,
    poll_seconds: float = 2,
    max_artifacts: int,
    max_output_bytes: int,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if collection_deadline_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("collector deadline and poll interval must be positive")
    invocation = _invocation(invocation_json)
    if (
        collector_id != invocation.collector_id
        or validator_id != invocation.validator_id
        or max_artifacts != invocation.max_output_artifacts
        or max_output_bytes != invocation.max_output_bytes
    ):
        raise ValueError("collector arguments differ from the canonical invocation")
    deadline = monotonic() + collection_deadline_seconds
    while True:
        try:
            collected = collect_stage_output(invocation, workspace)
            break
        except CollectionPendingError:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CollectionDeadlineError(
                    "scientific collector reached its bound before the model published its output"
                ) from None
            sleep(min(poll_seconds, remaining))
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
    if validation.get("collector_id", invocation.collector_id) != invocation.collector_id:
        raise ValueError("collector evidence changed the bound collector identity")
    validation.update(
        {
            "collector_id": invocation.collector_id,
            "stage_id": invocation.stage_id,
            "shard_id": invocation.shard_id,
            "logical_output_id": invocation.produces,
        }
    )
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
        media_type="application/vnd.fs2.scientific-validation+json",
        compression=None,
    )
    if invocation.handoff_name is not None and invocation.handoff_name not in refs:
        raise ValueError("collector omitted the invocation's exact handoff entry")
    # Finalization above is the durable collection boundary. The controller
    # closes the observed attempt and commits the aggregate canonical stage
    # manifest through the artifact service after every shard succeeds.
    del manifest_ref, validation_ref
