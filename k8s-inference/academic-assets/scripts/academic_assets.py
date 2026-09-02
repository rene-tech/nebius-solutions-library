#!/usr/bin/env python3
"""Fail-closed private ingestion for non-redistributable academic assets.

The command intentionally emits only non-secret identities and readiness state.
Artifact paths and license receipts remain inside an owner-only state directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO


SCHEMA_PREFIX = "fs2-serve.nebius.ai"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ASSET_ID_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
MAX_JSON_BYTES = 1024 * 1024


class IngestionError(Exception):
    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state
        self.message = message


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise IngestionError("MissingArtifact", f"{label} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise IngestionError("InvalidInput", f"{label} must be a regular, non-symlink file")
    if info.st_size > MAX_JSON_BYTES:
        raise IngestionError("InvalidInput", f"{label} exceeds the JSON size limit")
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise IngestionError("InvalidInput", f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise IngestionError("InvalidInput", f"{label} must be a JSON object")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def object_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_timestamp(value: Any, *, field: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IngestionError("InvalidInput", f"{field} must be a UTC RFC3339 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestionError("InvalidInput", f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise IngestionError("InvalidInput", f"{field} must include a timezone")


def validate_exact_keys(value: dict[str, Any], keys: set[str], *, label: str) -> None:
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise IngestionError("InvalidInput", f"{label} keys differ (missing={missing}, extra={extra})")


def load_contract(path: Path) -> dict[str, Any]:
    contract = load_json(path, label="academic asset contract")
    required = {"schema", "observed_at", "private_registry", "private_cache", "assets", "fallbacks"}
    validate_exact_keys(contract, required, label="contract")
    if contract["schema"] != f"{SCHEMA_PREFIX}/academic-assets/v1":
        raise IngestionError("InvalidInput", "unsupported academic asset contract schema")
    validate_timestamp(contract["observed_at"], field="contract.observed_at")
    registry = contract["private_registry"]
    if not isinstance(registry, dict):
        raise IngestionError("InvalidInput", "private_registry must be an object")
    validate_exact_keys(
        registry,
        {"project_id", "region", "registry_id", "repository_prefix"},
        label="private_registry",
    )
    prefix = registry["repository_prefix"]
    if not isinstance(prefix, str) or not prefix.endswith("/") or "@" in prefix:
        raise IngestionError("InvalidInput", "private registry repository_prefix is invalid")
    private_cache = contract["private_cache"]
    if not isinstance(private_cache, dict):
        raise IngestionError("InvalidInput", "private_cache must be an object")
    validate_exact_keys(
        private_cache,
        {
            "project_id",
            "region",
            "cluster_id",
            "filesystem_id",
            "pvc_namespace",
            "pvc_name",
            "distribution_scope",
        },
        label="private_cache",
    )
    if private_cache["project_id"] != registry["project_id"] or private_cache["region"] != registry["region"]:
        raise IngestionError("InvalidInput", "private cache and registry must share project and region")
    if private_cache["distribution_scope"] != "organization-internal":
        raise IngestionError("InvalidInput", "shared private cache must be organization-internal")
    assets = contract["assets"]
    if not isinstance(assets, dict) or not assets:
        raise IngestionError("InvalidInput", "contract assets must be a non-empty object")
    for asset_id, asset in assets.items():
        if not ASSET_ID_RE.fullmatch(asset_id) or not isinstance(asset, dict):
            raise IngestionError("InvalidInput", "contract asset identity is invalid")
        validate_exact_keys(
            asset,
            {
                "model_id",
                "display_name",
                "artifact",
                "license",
                "acceptance",
                "private_layer",
            },
            label=f"assets.{asset_id}",
        )
        artifact = asset["artifact"]
        validate_exact_keys(
            artifact,
            {
                "filename",
                "version",
                "source_revision",
                "size_bytes",
                "sha256",
                "magic_hex",
                "structural_validation",
                "source_url",
                "source_metadata",
            },
            label=f"assets.{asset_id}.artifact",
        )
        if not isinstance(artifact["filename"], str) or Path(artifact["filename"]).name != artifact["filename"]:
            raise IngestionError("InvalidInput", f"{asset_id} artifact filename is unsafe")
        if not isinstance(artifact["size_bytes"], int) or artifact["size_bytes"] <= 0:
            raise IngestionError("InvalidInput", f"{asset_id} artifact size is invalid")
        if not isinstance(artifact["sha256"], str) or not SHA256_RE.fullmatch(artifact["sha256"]):
            raise IngestionError("InvalidInput", f"{asset_id} artifact requires an exact SHA-256")
        if artifact["structural_validation"] not in {"conda-v2", "zstd-test", "none"}:
            raise IngestionError("InvalidInput", f"{asset_id} structural validator is unsupported")
        acceptance = asset["acceptance"]
        validate_exact_keys(
            acceptance,
            {"scope", "accepted_by_roles", "distribution_scopes", "terms", "source_claims"},
            label=f"assets.{asset_id}.acceptance",
        )
        if not acceptance["terms"] or not isinstance(acceptance["terms"], list):
            raise IngestionError("InvalidInput", f"{asset_id} requires at least one terms document")
        for term in acceptance["terms"]:
            if not isinstance(term, dict) or set(term) != {"document_id", "sha256"}:
                raise IngestionError("InvalidInput", f"{asset_id} terms entry is invalid")
            if not SHA256_RE.fullmatch(term["sha256"]):
                raise IngestionError("InvalidInput", f"{asset_id} terms SHA-256 is invalid")
        private_layer = asset["private_layer"]
        validate_exact_keys(
            private_layer,
            {"allowed", "destination", "redistributable"},
            label=f"assets.{asset_id}.private_layer",
        )
        if private_layer["redistributable"] is not False or not isinstance(private_layer["allowed"], bool):
            raise IngestionError("InvalidInput", f"{asset_id} private layer policy is invalid")
        if not isinstance(private_layer["destination"], str) or not private_layer["destination"].startswith("/opt/fs2/"):
            raise IngestionError("InvalidInput", f"{asset_id} private layer destination is invalid")
    fallbacks = contract["fallbacks"]
    required_fallbacks = {"openfold3": "alphafold3", "open-binder": "bindcraft"}
    if not isinstance(fallbacks, dict) or set(fallbacks) != set(required_fallbacks):
        raise IngestionError("InvalidInput", "OpenFold3 and open binder must remain explicit alternatives")
    for fallback_id, native_id in required_fallbacks.items():
        fallback = fallbacks[fallback_id]
        if (
            not isinstance(fallback, dict)
            or set(fallback) != {"model_id", "relationship", "aliases", "does_not_satisfy"}
            or fallback.get("model_id") != fallback_id
            or fallback.get("relationship") != "independent-operational-fallback"
            or fallback.get("aliases") != []
            or fallback.get("does_not_satisfy") != [native_id]
        ):
            raise IngestionError("InvalidInput", f"{fallback_id} must not alias or satisfy {native_id}")
    return contract


def resolve_env_or_path(direct: str | None, env_name: str | None, *, label: str) -> Path | None:
    if direct and env_name:
        raise IngestionError("InvalidInput", f"choose either a path or environment reference for {label}")
    raw = direct
    if env_name:
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name):
            raise IngestionError("InvalidInput", f"{label} environment variable name is invalid")
        raw = os.environ.get(env_name)
        if not raw:
            raise IngestionError("MissingArtifact", f"{label} environment reference is unset")
    return Path(raw).expanduser() if raw else None


def validate_acceptance(asset_id: str, spec: dict[str, Any], path: Path) -> dict[str, Any]:
    receipt = load_json(path, label=f"{asset_id} acceptance receipt")
    keys = {
        "schema",
        "asset_id",
        "accepted",
        "accepted_at",
        "accepted_by_role",
        "scope",
        "distribution_scope",
        "terms",
        "source_claims",
    }
    validate_exact_keys(receipt, keys, label=f"{asset_id} acceptance receipt")
    if receipt["schema"] != f"{SCHEMA_PREFIX}/academic-license-acceptance/v1":
        raise IngestionError("MissingLicenseAcceptance", f"{asset_id} acceptance schema is unsupported")
    if receipt["asset_id"] != asset_id or receipt["accepted"] is not True:
        raise IngestionError("MissingLicenseAcceptance", f"{asset_id} terms were not affirmatively accepted")
    validate_timestamp(receipt["accepted_at"], field=f"{asset_id}.accepted_at")
    acceptance = spec["acceptance"]
    if receipt["accepted_by_role"] not in acceptance["accepted_by_roles"]:
        raise IngestionError("MissingLicenseAcceptance", f"{asset_id} accepting role is not authorized")
    if receipt["scope"] != acceptance["scope"]:
        raise IngestionError("MissingLicenseAcceptance", f"{asset_id} use scope does not match the contract")
    if receipt["distribution_scope"] not in acceptance["distribution_scopes"]:
        raise IngestionError("MissingLicenseAcceptance", f"{asset_id} distribution scope is not authorized")
    if receipt["distribution_scope"] == "organization-internal" and (
        receipt["accepted_by_role"] != "authorized-organization-representative"
    ):
        raise IngestionError(
            "MissingLicenseAcceptance", f"{asset_id} organization scope requires an authorized representative"
        )
    expected_terms = sorted(acceptance["terms"], key=lambda item: item["document_id"])
    actual_terms = receipt["terms"]
    if not isinstance(actual_terms, list) or any(not isinstance(item, dict) for item in actual_terms):
        raise IngestionError("MissingLicenseAcceptance", f"{asset_id} terms set is invalid")
    if sorted(actual_terms, key=lambda item: item.get("document_id", "")) != expected_terms:
        raise IngestionError("MissingLicenseAcceptance", f"{asset_id} exact terms digests were not accepted")
    if receipt["source_claims"] != acceptance["source_claims"]:
        raise IngestionError("MissingLicenseAcceptance", f"{asset_id} approved source claims do not match")
    return {
        "receipt_sha256": object_sha256(receipt),
        "accepted_at": receipt["accepted_at"],
        "accepted_by_role": receipt["accepted_by_role"],
        "distribution_scope": receipt["distribution_scope"],
    }


def prepare_state_root(path: Path) -> Path:
    root = path.expanduser().resolve(strict=False)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise IngestionError("InvalidState", "state root must be a real directory")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise IngestionError("InvalidState", "state root must not be accessible by group or others")
    return root


def read_state_root(path: Path) -> Path | None:
    root = path.expanduser().resolve(strict=False)
    if not root.exists():
        return None
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise IngestionError("InvalidState", "state root must be an owner-only real directory")
    return root


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_conda_v2(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or archive.testzip() is not None:
                raise IngestionError("ArtifactInvalid", "Conda archive failed integrity validation")
            if "metadata.json" not in names:
                raise IngestionError("ArtifactInvalid", "Conda v2 archive lacks metadata.json")
            if len([name for name in names if name.startswith("info-") and name.endswith(".tar.zst")]) != 1:
                raise IngestionError("ArtifactInvalid", "Conda v2 archive lacks one info payload")
            if len([name for name in names if name.startswith("pkg-") and name.endswith(".tar.zst")]) != 1:
                raise IngestionError("ArtifactInvalid", "Conda v2 archive lacks one package payload")
    except (OSError, zipfile.BadZipFile) as exc:
        raise IngestionError("ArtifactInvalid", "artifact is not a valid Conda v2 archive") from exc


def _validate_structure(path: Path, method: str) -> None:
    if method == "none":
        return
    if method == "conda-v2":
        _validate_conda_v2(path)
        return
    if method == "zstd-test":
        executable = shutil.which("zstd")
        if executable is None:
            raise IngestionError("ArtifactInvalid", "zstd is required for AlphaFold3 stream validation")
        result = subprocess.run(
            [executable, "--test", "--quiet", "--", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            raise IngestionError("ArtifactInvalid", "artifact failed zstd stream validation")
        return
    raise IngestionError("InvalidInput", "unsupported structural validation method")


def copy_and_validate_artifact(source: Path, destination: Path, spec: dict[str, Any]) -> dict[str, Any]:
    if source.name != spec["filename"]:
        raise IngestionError("ArtifactInvalid", "artifact filename does not match the pinned contract")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except (FileNotFoundError, OSError) as exc:
        raise IngestionError("MissingArtifact", "artifact could not be opened safely") from exc
    temporary = destination.with_name(f".{destination.name}.copying")
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise IngestionError("ArtifactInvalid", "artifact must be a regular file")
        if before.st_nlink != 1:
            raise IngestionError("ArtifactInvalid", "artifact must have exactly one filesystem link")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        digest = hashlib.sha256()
        total = 0
        magic = bytes.fromhex(spec["magic_hex"])
        prefix = b""
        destination_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            with os.fdopen(destination_fd, "wb", closefd=True) as output:
                with os.fdopen(os.dup(source_fd), "rb", closefd=True) as input_file:
                    while True:
                        chunk = input_file.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        if len(prefix) < len(magic):
                            prefix += chunk[: len(magic) - len(prefix)]
                        digest.update(chunk)
                        total += len(chunk)
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            if temporary.exists():
                temporary.unlink()
            raise
        after = os.fstat(source_fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise IngestionError("ArtifactInvalid", "artifact changed while it was being copied")
        actual_digest = digest.hexdigest()
        if total != spec["size_bytes"]:
            raise IngestionError("ArtifactInvalid", "artifact size does not match the pinned contract")
        if prefix != magic:
            raise IngestionError("ArtifactInvalid", "artifact magic does not match the pinned format")
        if actual_digest != spec["sha256"]:
            raise IngestionError("ArtifactInvalid", "artifact SHA-256 does not match the pinned contract")
        os.replace(temporary, destination)
        _validate_structure(destination, spec["structural_validation"])
        return {"sha256": actual_digest, "size_bytes": total}
    finally:
        os.close(source_fd)
        if temporary.exists():
            temporary.unlink()


def enforce_private_pin(root: Path, asset_id: str, spec: dict[str, Any], digest: str) -> None:
    pins = root / "pins"
    pins.mkdir(mode=0o700, exist_ok=True)
    revision_key = hashlib.sha256(spec["source_revision"].encode()).hexdigest()[:24]
    pin_path = pins / f"{asset_id}-{revision_key}.json"
    pin = {
        "schema": f"{SCHEMA_PREFIX}/private-artifact-pin/v1",
        "asset_id": asset_id,
        "source_revision": spec["source_revision"],
        "sha256": digest,
        "size_bytes": spec["size_bytes"],
    }
    if pin_path.exists():
        existing = load_json(pin_path, label=f"{asset_id} private content pin")
        if existing != pin:
            raise IngestionError("ArtifactInvalid", "artifact differs from the private pin for this source revision")
    else:
        atomic_write_json(pin_path, pin)


def generation_path(root: Path, generation: str) -> Path:
    if not GENERATION_RE.fullmatch(generation):
        raise IngestionError("InvalidInput", "generation ID is invalid")
    return root / "generations" / generation


def load_generation(root: Path, generation: str) -> dict[str, Any]:
    directory = generation_path(root, generation)
    manifest = load_json(directory / "generation.json", label=f"generation {generation} manifest")
    expected = manifest.get("manifest_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    if not SHA256_RE.fullmatch(expected or "") or object_sha256(unsigned) != expected:
        raise IngestionError("InvalidState", f"generation {generation} manifest digest is invalid")
    if manifest.get("generation") != generation:
        raise IngestionError("InvalidState", "generation manifest identity mismatch")
    return manifest


def active_generation(root: Path) -> str | None:
    pointer_path = root / "active.json"
    if not pointer_path.exists():
        return None
    pointer = load_json(pointer_path, label="active generation pointer")
    validate_exact_keys(pointer, {"schema", "generation", "manifest_sha256", "activated_at"}, label="active pointer")
    if pointer["schema"] != f"{SCHEMA_PREFIX}/academic-assets-active/v1":
        raise IngestionError("InvalidState", "active generation pointer schema is unsupported")
    manifest = load_generation(root, pointer["generation"])
    if manifest["manifest_sha256"] != pointer["manifest_sha256"]:
        raise IngestionError("InvalidState", "active generation pointer digest mismatch")
    return pointer["generation"]


def activate_generation(root: Path, manifest: dict[str, Any]) -> None:
    atomic_write_json(
        root / "active.json",
        {
            "schema": f"{SCHEMA_PREFIX}/academic-assets-active/v1",
            "generation": manifest["generation"],
            "manifest_sha256": manifest["manifest_sha256"],
            "activated_at": utc_now(),
        },
    )


def receipt_path(root: Path, generation: str, asset_id: str, stage: str) -> Path:
    return generation_path(root, generation) / "receipts" / asset_id / f"{stage}.json"


def validate_stage_receipt(
    stage: str,
    receipt: dict[str, Any],
    *,
    asset_id: str,
    model_id: str,
    artifact_sha256: str,
    registry_prefix: str,
    private_cache: dict[str, Any],
    private_layer_allowed: bool,
    image_digest: str | None,
) -> None:
    common = {"schema", "asset_id", "artifact_sha256", "observed_at"}
    expected_schema = f"{SCHEMA_PREFIX}/academic-{stage}-receipt/v1"
    if receipt.get("schema") != expected_schema:
        raise IngestionError("InvalidEvidence", f"{stage} receipt schema is unsupported")
    if receipt.get("asset_id") != asset_id or receipt.get("artifact_sha256") != artifact_sha256:
        raise IngestionError("InvalidEvidence", f"{stage} receipt is not bound to the active artifact")
    validate_timestamp(receipt.get("observed_at"), field=f"{stage}.observed_at")
    if stage == "cache":
        validate_exact_keys(
            receipt,
            common
            | {
                "project_id",
                "region",
                "cluster_id",
                "filesystem_id",
                "pvc_namespace",
                "pvc_name",
                "file_size_bytes",
                "verified",
            },
            label="cache receipt",
        )
        for field in ("project_id", "region", "cluster_id", "filesystem_id", "pvc_namespace", "pvc_name"):
            if receipt[field] != private_cache[field]:
                raise IngestionError("InvalidEvidence", f"cache receipt {field} differs from the target contract")
        if receipt["verified"] is not True or not isinstance(receipt["file_size_bytes"], int) or receipt["file_size_bytes"] <= 0:
            raise IngestionError("InvalidEvidence", "cache receipt lacks verified file identity")
    elif stage == "image":
        validate_exact_keys(
            receipt,
            common
            | {
                "repository",
                "image_digest",
                "visibility",
                "redistributable",
                "asset_delivery_mode",
                "contains_licensed_asset",
                "builder",
            },
            label="image receipt",
        )
        repository = receipt["repository"]
        if not isinstance(repository, str) or not repository.startswith(registry_prefix) or "@" in repository:
            raise IngestionError("InvalidEvidence", "runtime image is not in the contracted private registry")
        if not OCI_DIGEST_RE.fullmatch(receipt["image_digest"]):
            raise IngestionError("InvalidEvidence", "runtime image digest is invalid")
        if receipt["visibility"] != "private" or receipt["redistributable"] is not False:
            raise IngestionError("InvalidEvidence", "academic runtime release must remain private and non-redistributable")
        delivery_mode = receipt["asset_delivery_mode"]
        contains_asset = receipt["contains_licensed_asset"]
        if delivery_mode not in {"external-private-cache", "embedded-private-layer"} or not isinstance(
            contains_asset, bool
        ):
            raise IngestionError("InvalidEvidence", "runtime image asset-delivery evidence is invalid")
        if delivery_mode == "external-private-cache" and contains_asset is not False:
            raise IngestionError("InvalidEvidence", "external-cache runtime image must not contain licensed bytes")
        if delivery_mode == "embedded-private-layer" and (
            private_layer_allowed is not True or contains_asset is not True
        ):
            raise IngestionError("InvalidEvidence", "this asset cannot be embedded in a runtime layer")
    elif stage == "deployment":
        validate_exact_keys(
            receipt,
            common | {"model_id", "image_digest", "deployed", "resource_uid"},
            label="deployment receipt",
        )
        if receipt["model_id"] != model_id or receipt["image_digest"] != image_digest or receipt["deployed"] is not True:
            raise IngestionError("InvalidEvidence", "deployment receipt is not bound to the active runtime")
        if not isinstance(receipt["resource_uid"], str) or not receipt["resource_uid"]:
            raise IngestionError("InvalidEvidence", "deployment receipt lacks a resource UID")
    elif stage == "semantic":
        validate_exact_keys(
            receipt,
            common | {"model_id", "image_digest", "passed", "validator_digest"},
            label="semantic receipt",
        )
        if receipt["model_id"] != model_id or receipt["image_digest"] != image_digest or receipt["passed"] is not True:
            raise IngestionError("InvalidEvidence", "semantic receipt is not bound to the active runtime")
        if not OCI_DIGEST_RE.fullmatch(receipt["validator_digest"]):
            raise IngestionError("InvalidEvidence", "semantic validator digest is invalid")
    else:
        raise IngestionError("InvalidInput", "unsupported readiness stage")


def asset_readiness(
    contract: dict[str, Any], root: Path | None, generation: str | None, asset_id: str
) -> dict[str, Any]:
    spec = contract["assets"][asset_id]
    stages = {
        "license_acceptance": "missing",
        "artifact": "missing",
        "private_cache": "missing",
        "private_image": "missing",
        "deployment": "missing",
        "semantic_readiness": "missing",
    }
    projection: dict[str, Any] = {
        "asset_id": asset_id,
        "model_id": spec["model_id"],
        "display_name": spec["display_name"],
        "state": "MissingLicenseAcceptance",
        "stages": stages,
        "artifact_sha256": None,
        "runtime_image_digest": None,
    }
    if root is None or generation is None:
        return projection
    manifest = load_generation(root, generation)
    if manifest.get("contract_sha256") != object_sha256(contract):
        projection["state"] = "InvalidContract"
        return projection
    item = manifest["assets"].get(asset_id)
    if not item:
        return projection
    artifact = item.get("artifact")
    if artifact is not None:
        stages["artifact"] = "verified"
        projection["artifact_sha256"] = artifact["sha256"]
    if item.get("license_acceptance") is None:
        return projection
    stages["license_acceptance"] = "accepted"
    projection["state"] = "MissingArtifact"
    if artifact is None:
        return projection
    projection["state"] = "MissingCache"
    cache_file = receipt_path(root, generation, asset_id, "cache")
    if not cache_file.exists():
        return projection
    acceptance = item["license_acceptance"]
    if (
        acceptance["accepted_by_role"] != "authorized-organization-representative"
        or acceptance["distribution_scope"] != contract["private_cache"]["distribution_scope"]
    ):
        stages["private_cache"] = "invalid"
        projection["state"] = "InvalidCacheReceipt"
        return projection
    try:
        cache = load_json(cache_file, label=f"{asset_id} cache receipt")
        validate_stage_receipt(
            "cache",
            cache,
            asset_id=asset_id,
            model_id=spec["model_id"],
            artifact_sha256=artifact["sha256"],
            registry_prefix=contract["private_registry"]["repository_prefix"],
            private_cache=contract["private_cache"],
            private_layer_allowed=spec["private_layer"]["allowed"],
            image_digest=None,
        )
    except IngestionError:
        stages["private_cache"] = "invalid"
        projection["state"] = "InvalidCacheReceipt"
        return projection
    if cache["file_size_bytes"] != artifact["size_bytes"]:
        stages["private_cache"] = "invalid"
        projection["state"] = "InvalidCacheReceipt"
        return projection
    stages["private_cache"] = "ready"
    projection["state"] = "MissingImage"
    image_file = receipt_path(root, generation, asset_id, "image")
    if not image_file.exists():
        return projection
    try:
        image = load_json(image_file, label=f"{asset_id} image receipt")
        validate_stage_receipt(
            "image",
            image,
            asset_id=asset_id,
            model_id=spec["model_id"],
            artifact_sha256=artifact["sha256"],
            registry_prefix=contract["private_registry"]["repository_prefix"],
            private_cache=contract["private_cache"],
            private_layer_allowed=spec["private_layer"]["allowed"],
            image_digest=None,
        )
    except IngestionError:
        stages["private_image"] = "invalid"
        projection["state"] = "InvalidImageReceipt"
        return projection
    stages["private_image"] = "ready"
    projection["runtime_image_digest"] = image["image_digest"]
    projection["state"] = "MissingDeployment"
    deployment_file = receipt_path(root, generation, asset_id, "deployment")
    if not deployment_file.exists():
        return projection
    try:
        deployment = load_json(deployment_file, label=f"{asset_id} deployment receipt")
        validate_stage_receipt(
            "deployment",
            deployment,
            asset_id=asset_id,
            model_id=spec["model_id"],
            artifact_sha256=artifact["sha256"],
            registry_prefix=contract["private_registry"]["repository_prefix"],
            private_cache=contract["private_cache"],
            private_layer_allowed=spec["private_layer"]["allowed"],
            image_digest=image["image_digest"],
        )
    except IngestionError:
        stages["deployment"] = "invalid"
        projection["state"] = "InvalidDeploymentReceipt"
        return projection
    stages["deployment"] = "ready"
    projection["state"] = "MissingSemanticReadiness"
    semantic_file = receipt_path(root, generation, asset_id, "semantic")
    if not semantic_file.exists():
        return projection
    try:
        semantic = load_json(semantic_file, label=f"{asset_id} semantic receipt")
        validate_stage_receipt(
            "semantic",
            semantic,
            asset_id=asset_id,
            model_id=spec["model_id"],
            artifact_sha256=artifact["sha256"],
            registry_prefix=contract["private_registry"]["repository_prefix"],
            private_cache=contract["private_cache"],
            private_layer_allowed=spec["private_layer"]["allowed"],
            image_digest=image["image_digest"],
        )
    except IngestionError:
        stages["semantic_readiness"] = "invalid"
        projection["state"] = "InvalidSemanticReceipt"
        return projection
    stages["semantic_readiness"] = "ready"
    projection["state"] = "Ready"
    return projection


def readiness_projection(contract: dict[str, Any], root: Path | None, generation: str | None) -> dict[str, Any]:
    assets = [asset_readiness(contract, root, generation, asset_id) for asset_id in sorted(contract["assets"])]
    return {
        "schema": f"{SCHEMA_PREFIX}/academic-assets-readiness/v1",
        "generation": generation,
        "state": "Ready" if assets and all(item["state"] == "Ready" for item in assets) else "Blocked",
        "assets": assets,
        "fallbacks": [
            {
                "model_id": fallback["model_id"],
                "state": "IndependentFallback",
                "aliases": fallback["aliases"],
                "does_not_satisfy": fallback["does_not_satisfy"],
            }
            for _, fallback in sorted(contract["fallbacks"].items())
        ],
    }


def ingest(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    root = prepare_state_root(args.state_dir)
    final = generation_path(root, args.generation)
    if final.exists():
        raise IngestionError("InvalidState", "generation already exists; use a new generation ID")
    generations = root / "generations"
    generations.mkdir(mode=0o700, exist_ok=True)
    staging = generations / f".{args.generation}.staging-{os.getpid()}"
    staging.mkdir(mode=0o700)
    try:
        manifest_assets: dict[str, Any] = {}
        for asset_id in sorted(contract["assets"]):
            spec = contract["assets"][asset_id]
            dest = asset_id.replace("-", "_")
            source = resolve_env_or_path(
                getattr(args, f"{dest}_path", None),
                getattr(args, f"{dest}_path_env", None),
                label=f"{asset_id} artifact",
            )
            acceptance_path = resolve_env_or_path(
                getattr(args, f"{dest}_acceptance", None),
                getattr(args, f"{dest}_acceptance_env", None),
                label=f"{asset_id} acceptance receipt",
            )
            item: dict[str, Any] = {"license_acceptance": None, "artifact": None}
            if acceptance_path is not None:
                item["license_acceptance"] = validate_acceptance(asset_id, spec, acceptance_path)
            if source is not None:
                destination = staging / "assets" / asset_id / spec["artifact"]["filename"]
                evidence = copy_and_validate_artifact(source, destination, spec["artifact"])
                enforce_private_pin(root, asset_id, spec["artifact"], evidence["sha256"])
                item["artifact"] = {
                    "filename": spec["artifact"]["filename"],
                    "version": spec["artifact"]["version"],
                    "source_revision": spec["artifact"]["source_revision"],
                    "sha256": evidence["sha256"],
                    "size_bytes": evidence["size_bytes"],
                    "relative_path": f"assets/{asset_id}/{spec['artifact']['filename']}",
                }
            manifest_assets[asset_id] = item
        unsigned = {
            "schema": f"{SCHEMA_PREFIX}/academic-assets-generation/v1",
            "generation": args.generation,
            "created_at": utc_now(),
            "contract_sha256": object_sha256(contract),
            "assets": manifest_assets,
        }
        manifest = {**unsigned, "manifest_sha256": object_sha256(unsigned)}
        atomic_write_json(staging / "generation.json", manifest)
        os.replace(staging, final)
        activate_generation(root, manifest)
        projection = readiness_projection(contract, root, args.generation)
        atomic_write_json(final / "readiness.json", projection)
        return projection
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def record_stage(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    root = read_state_root(args.state_dir)
    if root is None:
        raise IngestionError("InvalidState", "private state directory does not exist")
    generation = args.generation or active_generation(root)
    if generation is None:
        raise IngestionError("InvalidState", "no active generation exists")
    manifest = load_generation(root, generation)
    if manifest.get("contract_sha256") != object_sha256(contract):
        raise IngestionError("InvalidState", "generation is bound to a different contract revision")
    if args.asset_id not in contract["assets"]:
        raise IngestionError("InvalidInput", "asset ID is not in the contract")
    item = manifest["assets"].get(args.asset_id)
    if not item or not item.get("artifact"):
        raise IngestionError("MissingArtifact", "asset must be verified before readiness evidence")
    if item.get("license_acceptance") is None:
        raise IngestionError("MissingLicenseAcceptance", "readiness evidence requires accepted exact terms")
    artifact_sha256 = item["artifact"]["sha256"]
    spec = contract["assets"][args.asset_id]
    image_digest = None
    image_file = receipt_path(root, generation, args.asset_id, "image")
    if image_file.exists():
        image_digest = load_json(image_file, label=f"{args.asset_id} image receipt").get("image_digest")
    receipt = load_json(args.receipt, label=f"{args.stage} receipt")
    validate_stage_receipt(
        args.stage,
        receipt,
        asset_id=args.asset_id,
        model_id=spec["model_id"],
        artifact_sha256=artifact_sha256,
        registry_prefix=contract["private_registry"]["repository_prefix"],
        private_cache=contract["private_cache"],
        private_layer_allowed=spec["private_layer"]["allowed"],
        image_digest=image_digest,
    )
    if args.stage == "image" and not receipt_path(root, generation, args.asset_id, "cache").exists():
        raise IngestionError("InvalidEvidence", "private-cache readiness must be recorded before image readiness")
    if args.stage == "cache" and (
        item["license_acceptance"]["accepted_by_role"] != "authorized-organization-representative"
        or item["license_acceptance"]["distribution_scope"] != contract["private_cache"]["distribution_scope"]
    ):
        raise IngestionError(
            "MissingLicenseAcceptance", "shared private cache requires organization-level acceptance"
        )
    if args.stage in {"deployment", "semantic"} and image_digest is None:
        raise IngestionError("InvalidEvidence", "image readiness must be recorded first")
    if args.stage == "semantic" and not receipt_path(root, generation, args.asset_id, "deployment").exists():
        raise IngestionError("InvalidEvidence", "deployment readiness must be recorded before semantic readiness")
    destination = receipt_path(root, generation, args.asset_id, args.stage)
    if destination.exists() and load_json(destination, label=f"existing {args.stage} receipt") != receipt:
        raise IngestionError("InvalidState", "readiness receipt is immutable; rotate the generation")
    atomic_write_json(destination, receipt)
    projection = readiness_projection(contract, root, generation)
    atomic_write_json(generation_path(root, generation) / "readiness.json", projection)
    return projection


def rollback(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    root = read_state_root(args.state_dir)
    if root is None:
        raise IngestionError("InvalidState", "private state directory does not exist")
    manifest = load_generation(root, args.to_generation)
    if manifest.get("contract_sha256") != object_sha256(contract):
        raise IngestionError("InvalidState", "rollback generation is bound to a different contract revision")
    activate_generation(root, manifest)
    projection = readiness_projection(contract, root, args.to_generation)
    atomic_write_json(generation_path(root, args.to_generation) / "readiness.json", projection)
    return projection


def revoke_generation(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    root = read_state_root(args.state_dir)
    if root is None:
        raise IngestionError("InvalidState", "private state directory does not exist")
    if active_generation(root) == args.generation:
        raise IngestionError("InvalidState", "activate a replacement generation before revocation")
    directory = generation_path(root, args.generation)
    manifest = load_generation(root, args.generation)
    tombstone_path = root / "revocations" / f"{args.generation}.json"
    if tombstone_path.exists():
        raise IngestionError("InvalidState", "generation already has a revocation tombstone")
    tombstone = {
        "schema": f"{SCHEMA_PREFIX}/academic-assets-revocation/v1",
        "generation": args.generation,
        "manifest_sha256": manifest["manifest_sha256"],
        "revoked_at": utc_now(),
        "reason": args.reason,
    }
    shutil.rmtree(directory)
    atomic_write_json(tombstone_path, tombstone)
    return tombstone


def resolve_asset(args: argparse.Namespace, contract: dict[str, Any]) -> dict[str, Any]:
    root = read_state_root(args.state_dir)
    if root is None:
        raise IngestionError("InvalidState", "private state directory does not exist")
    generation = args.generation or active_generation(root)
    if generation is None:
        raise IngestionError("InvalidState", "no active generation exists")
    manifest = load_generation(root, generation)
    if manifest.get("contract_sha256") != object_sha256(contract):
        raise IngestionError("InvalidState", "generation is bound to a different contract revision")
    if args.asset_id not in contract["assets"]:
        raise IngestionError("InvalidInput", "asset ID is not in the contract")
    item = manifest["assets"].get(args.asset_id)
    if not item or not item.get("artifact"):
        raise IngestionError("MissingArtifact", "asset is not verified in this generation")
    acceptance = item["license_acceptance"]
    if acceptance is None:
        raise IngestionError("MissingLicenseAcceptance", "artifact is quarantined pending exact terms acceptance")
    if args.for_shared_cache:
        if acceptance["accepted_by_role"] != "authorized-organization-representative":
            raise IngestionError("MissingLicenseAcceptance", "shared private cache requires organization acceptance")
        if acceptance["distribution_scope"] != contract["private_cache"]["distribution_scope"]:
            raise IngestionError("MissingLicenseAcceptance", "acceptance does not authorize shared private cache")
    if args.for_private_layer:
        if contract["assets"][args.asset_id]["private_layer"]["allowed"] is not True:
            raise IngestionError("InvalidInput", "this asset must remain an external private-volume artifact")
        if acceptance["accepted_by_role"] != "authorized-organization-representative":
            raise IngestionError("MissingLicenseAcceptance", "shared private layers require organization-level acceptance")
        if acceptance["distribution_scope"] != "organization-internal":
            raise IngestionError("MissingLicenseAcceptance", "shared private layers require organization-internal scope")
    relative = Path(item["artifact"]["relative_path"])
    path = generation_path(root, generation) / relative
    if not path.is_file() or path.is_symlink():
        raise IngestionError("InvalidState", "staged artifact is missing or unsafe")
    return {
        "generation": generation,
        "asset_id": args.asset_id,
        "sha256": item["artifact"]["sha256"],
        "path": str(path),
    }


def build_parser(contract: dict[str, Any]) -> argparse.ArgumentParser:
    def add_state_arguments(command_parser: argparse.ArgumentParser) -> None:
        group = command_parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--state-dir", type=Path)
        group.add_argument("--state-dir-env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="print a non-secret readiness projection")
    add_state_arguments(status_parser)
    status_parser.add_argument("--generation")

    ingest_parser = subparsers.add_parser("ingest", help="validate and atomically stage approved artifacts")
    add_state_arguments(ingest_parser)
    ingest_parser.add_argument("--generation", required=True)
    for asset_id in sorted(contract["assets"]):
        dest = asset_id.replace("-", "_")
        ingest_parser.add_argument(f"--{asset_id}-path", dest=f"{dest}_path")
        ingest_parser.add_argument(f"--{asset_id}-path-env", dest=f"{dest}_path_env")
        ingest_parser.add_argument(f"--{asset_id}-acceptance", dest=f"{dest}_acceptance")
        ingest_parser.add_argument(f"--{asset_id}-acceptance-env", dest=f"{dest}_acceptance_env")

    record_parser = subparsers.add_parser("record", help="record immutable image/deployment/semantic evidence")
    add_state_arguments(record_parser)
    record_parser.add_argument("--generation")
    record_parser.add_argument("--asset-id", required=True)
    record_parser.add_argument("--stage", choices=["cache", "image", "deployment", "semantic"], required=True)
    record_parser.add_argument("--receipt", type=Path, required=True)

    rollback_parser = subparsers.add_parser("rollback", help="atomically reactivate an existing generation")
    add_state_arguments(rollback_parser)
    rollback_parser.add_argument("--to-generation", required=True)

    revoke_parser = subparsers.add_parser("revoke", help="remove an inactive invalid generation and tombstone it")
    add_state_arguments(revoke_parser)
    revoke_parser.add_argument("--generation", required=True)
    revoke_parser.add_argument(
        "--reason",
        choices=["invalid-license-acceptance-attribution", "license-terminated"],
        required=True,
    )

    resolve_parser = subparsers.add_parser("resolve", help="resolve a verified private artifact for automation")
    add_state_arguments(resolve_parser)
    resolve_parser.add_argument("--generation")
    resolve_parser.add_argument("--asset-id", required=True)
    resolve_parser.add_argument("--for-shared-cache", action="store_true")
    resolve_parser.add_argument("--for-private-layer", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--contract", type=Path, required=True)
    try:
        bootstrap_args, _ = bootstrap.parse_known_args(argv)
        contract = load_contract(bootstrap_args.contract)
        parser = build_parser(contract)
        args = parser.parse_args(argv)
        if getattr(args, "state_dir_env", None):
            env_name = args.state_dir_env
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name):
                raise IngestionError("InvalidInput", "state directory environment variable name is invalid")
            state_dir = os.environ.get(env_name)
            if not state_dir:
                raise IngestionError("InvalidState", "state directory environment reference is unset")
            args.state_dir = Path(state_dir)
        if args.command == "status":
            root = read_state_root(args.state_dir)
            generation = args.generation
            if root is not None and generation is None:
                generation = active_generation(root)
            result = readiness_projection(contract, root, generation)
        elif args.command == "ingest":
            result = ingest(args, contract)
        elif args.command == "record":
            result = record_stage(args, contract)
        elif args.command == "rollback":
            result = rollback(args, contract)
        elif args.command == "revoke":
            result = revoke_generation(args, contract)
        elif args.command == "resolve":
            result = resolve_asset(args, contract)
        else:
            raise IngestionError("InvalidInput", "unsupported command")
        json.dump(result, sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except IngestionError as exc:
        json.dump(
            {
                "schema": f"{SCHEMA_PREFIX}/academic-assets-error/v1",
                "state": exc.state,
                "message": exc.message,
            },
            sys.stdout,
            sort_keys=True,
            separators=(",", ":"),
        )
        sys.stdout.write("\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
