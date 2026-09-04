#!/usr/bin/env python3
"""Resumable, checksum-first publication of public model artifacts.

Only catalog-declared HTTPS objects are accepted. Bytes first land in a stable
``.staging`` path on the destination filesystem so retries can issue Range
requests. Publication is a same-filesystem rename and occurs only after the
canonical artifact manifest has been verified in full.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import fcntl
import hashlib
import json
import mmap
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
from typing import Any, Mapping, Sequence
from urllib import error, parse, request
import zipfile


CATALOG_SCHEMA = "fs2-serve.nebius.ai/public-artifact-catalog/v1"
MANIFEST_SCHEMA = "fs2-serve.nebius.ai/artifact-manifest/v1"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/public-artifact-cache-receipt/v1"
READINESS_SCHEMA = "fs2-serve.nebius.ai/public-artifact-readiness/v1"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ID_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
COMMIT_RE = re.compile(r"^[a-f0-9]{8,64}$")
BUFFER_BYTES = 4 * 1024 * 1024
DEFAULT_DOWNLOAD_CONCURRENCY = 4
MAX_DOWNLOAD_CONCURRENCY = 8


class ContractError(RuntimeError):
    """A fail-closed catalog, provenance, or integrity violation."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8") + b"\n"


def pretty_json(value: Any) -> bytes:
    """Return the exact bytes used for generated human-readable JSON files."""
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def protenix_localization_document(
    payload_files: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the sole canonical runtime-visible Protenix v2 manifest."""
    return {
        "schema": "fs2.nebius.ai/protenix-v2-composite-artifact/v1",
        "artifact_id": "protenix-v2",
        "revision": (
            "code-2475421477ab414b571149ad4a875c390ff8a35d_"
            "checkpoint-653edab28103133512575365130916e3fd23ecc3_"
            "common-2026-01-29"
        ),
        "sources": {
            "code": {"revision": "2475421477ab414b571149ad4a875c390ff8a35d"},
            "checkpoint": {
                "revision": (
                    "TMF001/protenix-v2-weights@"
                    "653edab28103133512575365130916e3fd23ecc3"
                ),
                "bytes": 1859785497,
                "sha256": "8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599",
                "md5": "49016ebf4775bf6b629bc4dc77b6673e",
                "parameter_count": 464442431,
                "verification": "third-party-mirror-verified-not-publisher-byte-compared",
            },
            "common": {
                "revision": "tos-common-2026-01-29",
                "archive_url": "https://protenix.tos-cn-beijing.volces.com/common.tar.gz",
                "archive_bytes": 475085654,
                "archive_sha256": "08ea594f429df35494c062e3dfcacaf48fa761e4ea4a8bcb6d5107d211e64dbd",
            },
        },
        "files": [dict(item) for item in payload_files],
    }


def protenix_localized_inventory(
    payload_files: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str, list[dict[str, Any]], str]:
    """Derive the generated files and full seven-file localized-tree identity."""
    document = protenix_localization_document(payload_files)
    manifest_digest = sha256_bytes(canonical_json(document))
    manifest_bytes = pretty_json(document)
    marker_bytes = f"{manifest_digest}\n".encode("ascii")
    files = sorted(
        [
            *(dict(item) for item in payload_files),
            {
                "path": "manifest.json",
                "bytes": len(manifest_bytes),
                "sha256": sha256_bytes(manifest_bytes),
            },
            {
                "path": ".fs2-manifest-sha256",
                "bytes": len(marker_bytes),
                "sha256": sha256_bytes(marker_bytes),
            },
        ],
        key=lambda item: item["path"],
    )
    content_digest = sha256_bytes(canonical_json(files))
    return document, manifest_digest, files, content_digest


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read valid JSON from {path}") from exc


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True).encode("utf-8"))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o444)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{context} must be an object")
    return value


def _exact(value: Any, fields: set[str], context: str) -> dict[str, Any]:
    result = _object(value, context)
    missing = fields - set(result)
    unknown = set(result) - fields
    if missing or unknown:
        raise ContractError(
            f"{context} fields mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    return result


def _safe_relative(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{context} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ContractError(f"{context} is unsafe")
    return path.as_posix()


def _https_url(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{context} must be an HTTPS URL")
    parsed = parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ContractError(f"{context} must be a credential-free immutable HTTPS URL")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContractError(f"cannot safely open artifact file: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ContractError(f"artifact entry is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(BUFFER_BYTES), b""):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def validate_manifest(value: Any) -> dict[str, Any]:
    fields = {
        "schema", "model_id", "kind", "source", "content", "license",
        "entitlement_state", "owner", "retention",
    }
    manifest = _exact(value, fields, "artifact manifest")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise ContractError("artifact manifest schema is unsupported")
    if not isinstance(manifest["model_id"], str) or not ID_RE.fullmatch(manifest["model_id"]):
        raise ContractError("artifact manifest model_id is not canonical")
    if manifest["kind"] not in {"weights", "snapshot"}:
        raise ContractError("public ingestion accepts weights and immutable snapshot manifests only")
    source = _exact(manifest["source"], {"uri", "revision"}, "artifact source")
    uri = source["uri"]
    if not isinstance(uri, str) or parse.urlsplit(uri).scheme not in {"hf", "https"}:
        raise ContractError("public artifact source must be immutable hf:// or https://")
    if not isinstance(source["revision"], str) or not source["revision"]:
        raise ContractError("artifact source revision is required")
    content = _exact(manifest["content"], {"digest", "expanded_bytes", "files"}, "artifact content")
    if not SHA256_RE.fullmatch(str(content["digest"])):
        raise ContractError("artifact content digest is invalid")
    files = content["files"]
    if not isinstance(files, list) or not files:
        raise ContractError("artifact manifest files must be a non-empty array")
    canonical_files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(files):
        item = _exact(raw, {"path", "bytes", "sha256"}, f"artifact file {index}")
        path = _safe_relative(item["path"], f"artifact file {index} path")
        if path in seen:
            raise ContractError(f"duplicate artifact path: {path}")
        seen.add(path)
        if isinstance(item["bytes"], bool) or not isinstance(item["bytes"], int) or item["bytes"] < 1:
            raise ContractError(f"artifact file {path} byte size is invalid")
        if not SHA256_RE.fullmatch(str(item["sha256"])):
            raise ContractError(f"artifact file {path} SHA-256 is invalid")
        canonical_files.append({"path": path, "bytes": item["bytes"], "sha256": item["sha256"]})
    if canonical_files != sorted(canonical_files, key=lambda item: item["path"]):
        raise ContractError("artifact files must be sorted by path")
    if sum(item["bytes"] for item in canonical_files) != content["expanded_bytes"]:
        raise ContractError("artifact expanded_bytes does not reconcile")
    if sha256_bytes(canonical_json(canonical_files)) != content["digest"]:
        raise ContractError("artifact content digest does not match the inventory")
    license_value = _exact(manifest["license"], {"id", "state"}, "artifact license")
    if not isinstance(license_value["id"], str) or not license_value["id"]:
        raise ContractError("artifact license id is required")
    if license_value["state"] != "verified" or manifest["entitlement_state"] != "not-required":
        raise ContractError("only verified, ungated public artifacts may be ingested")
    if manifest["retention"] != "retained-platform":
        raise ContractError("public cache artifacts must be retained-platform")
    return manifest


def validate_mirror_provenance(
    value: Any,
    *,
    artifact_id: str,
    manifest: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]],
    catalog_path: Path,
) -> dict[str, Any]:
    """Validate a mirror without implying publisher byte equivalence."""
    provenance = _exact(
        value,
        {"state", "canonical_source", "acquisition_source", "verification"},
        f"artifact {artifact_id} provenance",
    )
    if provenance["state"] != "mirror-verified-not-publisher-byte-compared":
        raise ContractError(f"artifact {artifact_id} mirror provenance state is invalid")
    canonical = _exact(
        provenance["canonical_source"],
        {
            "publisher", "uri", "source_revision", "publisher_bytes_reachable",
            "publisher_digest_available",
        },
        f"artifact {artifact_id} canonical source",
    )
    if not isinstance(canonical["publisher"], str) or not canonical["publisher"]:
        raise ContractError(f"artifact {artifact_id} canonical publisher is invalid")
    _https_url(canonical["uri"], f"artifact {artifact_id} canonical source URL")
    if not re.fullmatch(r"[a-f0-9]{40}", str(canonical["source_revision"])):
        raise ContractError(f"artifact {artifact_id} source revision is not an immutable commit")
    if canonical["publisher_bytes_reachable"] is not False:
        raise ContractError(f"artifact {artifact_id} cannot claim an unreachable publisher object is reachable")
    if canonical["publisher_digest_available"] is not False:
        raise ContractError(f"artifact {artifact_id} cannot claim an unavailable publisher digest")

    mirror = _exact(
        provenance["acquisition_source"],
        {"relationship", "repository", "repository_revision", "url", "lfs_oid_sha256", "bytes"},
        f"artifact {artifact_id} mirror source",
    )
    if mirror["relationship"] != "third-party-mirror":
        raise ContractError(f"artifact {artifact_id} acquisition source is not explicitly a third-party mirror")
    if not isinstance(mirror["repository"], str) or not mirror["repository"]:
        raise ContractError(f"artifact {artifact_id} mirror repository is invalid")
    if not re.fullmatch(r"[a-f0-9]{40}", str(mirror["repository_revision"])):
        raise ContractError(f"artifact {artifact_id} mirror revision is not an immutable commit")
    _https_url(mirror["url"], f"artifact {artifact_id} mirror URL")
    manifest_source = manifest["source"]
    if (
        manifest_source["uri"] == canonical["uri"]
        or manifest_source["uri"] in {source["url"] for source in sources}
        or canonical["source_revision"] not in manifest_source["revision"]
        or mirror["repository_revision"] not in manifest_source["revision"]
    ):
        raise ContractError(
            f"artifact {artifact_id} manifest must identify the composite declaration, "
            "not an unavailable publisher or individual acquisition object"
        )
    mirror_sources = [
        source
        for source in sources
        if mirror["url"] == source["url"]
        and mirror["bytes"] == source["bytes"]
        and mirror["lfs_oid_sha256"] == source["sha256"]
    ]
    if len(mirror_sources) != 1:
        raise ContractError(f"artifact {artifact_id} mirror metadata does not match the acquisition source")
    source = mirror_sources[0]

    verification = _exact(
        provenance["verification"],
        {
            "evidence", "evidence_sha256", "sha256", "md5", "safe_torch_load",
            "root_type", "top_level_key", "checkpoint_key_count", "checkpoint_tensor_count",
            "checkpoint_tensor_dtypes", "source_state_key_count", "checkpoint_parameter_count",
            "checkpoint_element_count", "source_parameter_count", "key_shape_inventory_sha256",
            "inspection_image_digest", "inspection_torch_version", "strict_key_shape_match",
            "publisher_byte_compared",
        },
        f"artifact {artifact_id} mirror verification",
    )
    for field in ("evidence_sha256", "sha256", "key_shape_inventory_sha256"):
        if not SHA256_RE.fullmatch(str(verification[field])):
            raise ContractError(f"artifact {artifact_id} verification {field} is invalid")
    if not re.fullmatch(r"[a-f0-9]{32}", str(verification["md5"])):
        raise ContractError(f"artifact {artifact_id} verification MD5 is invalid")
    if verification["sha256"] != source["sha256"]:
        raise ContractError(f"artifact {artifact_id} verified SHA-256 does not match its source")
    if verification["safe_torch_load"] != "weights-only-mmap-cpu":
        raise ContractError(f"artifact {artifact_id} verification did not use safe offline torch loading")
    if verification["root_type"] != "dict":
        raise ContractError(f"artifact {artifact_id} checkpoint root type is not an exact dict")
    if verification["top_level_key"] != "model":
        raise ContractError(f"artifact {artifact_id} checkpoint top-level model key is not verified")
    for field in (
        "checkpoint_key_count", "checkpoint_tensor_count", "source_state_key_count",
        "checkpoint_parameter_count", "checkpoint_element_count", "source_parameter_count",
    ):
        if isinstance(verification[field], bool) or not isinstance(verification[field], int) or verification[field] < 1:
            raise ContractError(f"artifact {artifact_id} verification {field} is invalid")
    if verification["checkpoint_key_count"] != verification["source_state_key_count"]:
        raise ContractError(f"artifact {artifact_id} checkpoint/source key counts differ")
    if verification["checkpoint_tensor_count"] != verification["checkpoint_key_count"]:
        raise ContractError(f"artifact {artifact_id} checkpoint key/tensor counts differ")
    if verification["checkpoint_tensor_dtypes"] != {
        "torch.float32": verification["checkpoint_tensor_count"]
    }:
        raise ContractError(f"artifact {artifact_id} checkpoint is not entirely float32 tensors")
    if verification["checkpoint_parameter_count"] != verification["source_parameter_count"]:
        raise ContractError(f"artifact {artifact_id} checkpoint/source parameter counts differ")
    if verification["checkpoint_element_count"] != verification["checkpoint_parameter_count"]:
        raise ContractError(f"artifact {artifact_id} checkpoint element/parameter counts differ")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", str(verification["inspection_image_digest"])):
        raise ContractError(f"artifact {artifact_id} pinned inspection image digest is invalid")
    if not isinstance(verification["inspection_torch_version"], str) or not verification["inspection_torch_version"]:
        raise ContractError(f"artifact {artifact_id} pinned inspection Torch version is invalid")
    if verification["strict_key_shape_match"] is not True:
        raise ContractError(f"artifact {artifact_id} checkpoint did not strictly match source architecture")
    if verification["publisher_byte_compared"] is not False:
        raise ContractError(f"artifact {artifact_id} must not claim publisher-byte comparison")
    evidence_path = (
        catalog_path.parent / _safe_relative(verification["evidence"], "mirror evidence path")
    ).resolve()
    if catalog_path.parent.resolve() not in evidence_path.parents or not evidence_path.is_file():
        raise ContractError(f"artifact {artifact_id} mirror verification evidence is absent")
    if sha256_file(evidence_path) != verification["evidence_sha256"]:
        raise ContractError(f"artifact {artifact_id} mirror verification evidence digest mismatch")
    evidence = _object(load_json(evidence_path), f"artifact {artifact_id} mirror verification evidence")
    if (
        evidence.get("schema") != "fs2-serve.nebius.ai/third-party-model-mirror-verification/v1"
        or evidence.get("artifact_id") != artifact_id
        or evidence.get("conclusion") != provenance["state"]
    ):
        raise ContractError(f"artifact {artifact_id} mirror verification evidence identity is invalid")
    byte_evidence = _object(evidence.get("byte_verification"), f"artifact {artifact_id} byte evidence")
    checkpoint_evidence = _object(
        evidence.get("checkpoint_inspection"), f"artifact {artifact_id} checkpoint evidence"
    )
    image_evidence = _object(
        evidence.get("pinned_runtime_image_inspection"),
        f"artifact {artifact_id} pinned image checkpoint evidence",
    )
    architecture_evidence = _object(
        evidence.get("source_architecture"), f"artifact {artifact_id} architecture evidence"
    )
    comparison_evidence = _object(
        evidence.get("comparison"), f"artifact {artifact_id} comparison evidence"
    )
    canonical_evidence = _object(
        evidence.get("canonical_source"), f"artifact {artifact_id} canonical evidence"
    )
    mirror_evidence = _object(evidence.get("mirror"), f"artifact {artifact_id} mirror evidence")
    if (
        byte_evidence.get("bytes") != source["bytes"]
        or byte_evidence.get("sha256") != verification["sha256"]
        or byte_evidence.get("md5") != verification["md5"]
        or byte_evidence.get("all_expected_values_match") is not True
        or checkpoint_evidence.get("load_mode") != verification["safe_torch_load"]
        or checkpoint_evidence.get("root_type") != verification["root_type"]
        or checkpoint_evidence.get("state_key") != verification["top_level_key"]
        or checkpoint_evidence.get("state_key_count") != verification["checkpoint_key_count"]
        or checkpoint_evidence.get("tensor_count") != verification["checkpoint_tensor_count"]
        or checkpoint_evidence.get("tensor_dtype_counts") != verification["checkpoint_tensor_dtypes"]
        or checkpoint_evidence.get("parameter_count") != verification["checkpoint_parameter_count"]
        or checkpoint_evidence.get("element_count") != verification["checkpoint_element_count"]
        or checkpoint_evidence.get("key_shape_inventory_sha256")
        != verification["key_shape_inventory_sha256"]
        or image_evidence.get("image_digest") != verification["inspection_image_digest"]
        or image_evidence.get("image_qualification_state") != "unqualified-inspection-only"
        or image_evidence.get("network_mode") != "none"
        or image_evidence.get("torch_version") != verification["inspection_torch_version"]
        or image_evidence.get("load_mode") != verification["safe_torch_load"]
        or image_evidence.get("root_type") != verification["root_type"]
        or image_evidence.get("top_level_keys") != [verification["top_level_key"]]
        or image_evidence.get("tensor_count") != verification["checkpoint_tensor_count"]
        or image_evidence.get("tensor_dtype_counts") != verification["checkpoint_tensor_dtypes"]
        or image_evidence.get("element_count") != verification["checkpoint_element_count"]
        or architecture_evidence.get("state_key_count") != verification["source_state_key_count"]
        or architecture_evidence.get("parameter_count") != verification["source_parameter_count"]
        or architecture_evidence.get("key_shape_inventory_sha256")
        != verification["key_shape_inventory_sha256"]
        or comparison_evidence.get("missing_key_count") != 0
        or comparison_evidence.get("unexpected_key_count") != 0
        or comparison_evidence.get("shape_mismatch_count") != 0
        or comparison_evidence.get("strict_key_shape_match") is not True
        or canonical_evidence.get("official_uri") != canonical["uri"]
        or canonical_evidence.get("source_revision") != canonical["source_revision"]
        or canonical_evidence.get("publisher_byte_compared") is not False
        or mirror_evidence.get("relationship") != mirror["relationship"]
        or mirror_evidence.get("repository") != mirror["repository"]
        or mirror_evidence.get("repository_revision") != mirror["repository_revision"]
        or mirror_evidence.get("lfs_oid_sha256") != mirror["lfs_oid_sha256"]
        or mirror_evidence.get("lfs_size") != mirror["bytes"]
    ):
        raise ContractError(f"artifact {artifact_id} mirror verification evidence conflicts with the catalog")
    return provenance


def validate_catalog(value: Any, catalog_path: Path) -> dict[str, Any]:
    catalog = _exact(
        value,
        {
            "schema", "generated_at", "licenses", "artifacts", "consumers",
            "consumer_layouts", "private_layouts", "reference_layouts",
            "runtime_constraints", "runtime_handoffs",
        },
        "artifact catalog",
    )
    if catalog["schema"] != CATALOG_SCHEMA:
        raise ContractError("artifact catalog schema is unsupported")
    try:
        dt.datetime.fromisoformat(str(catalog["generated_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("catalog generated_at is not RFC 3339") from exc
    artifacts = _object(catalog["artifacts"], "catalog artifacts")
    licenses = _object(catalog["licenses"], "catalog licenses")
    for license_id, raw_license in licenses.items():
        license_value = _exact(raw_license, {"id", "url", "commercial_use", "redistribution"}, f"license {license_id}")
        if license_value["id"] != license_id:
            raise ContractError(f"license identity mismatch: {license_id}")
        _https_url(license_value["url"], f"license {license_id} URL")
        if not isinstance(license_value["commercial_use"], str) or not isinstance(license_value["redistribution"], str):
            raise ContractError(f"license {license_id} use statements are invalid")
    if not artifacts:
        raise ContractError("catalog artifacts cannot be empty")
    for artifact_id, raw in artifacts.items():
        if not ID_RE.fullmatch(artifact_id):
            raise ContractError(f"artifact id is not canonical: {artifact_id}")
        entry = _object(raw, f"artifact {artifact_id}")
        state = entry.get("state")
        common = {"id", "family", "state", "reason", "consumers"}
        if state == "available":
            allowed = common | {"manifest", "sources", "offline_smoke"}
            if "provenance" in entry:
                allowed.add("provenance")
            if "localization" in entry:
                allowed.add("localization")
            entry = _exact(entry, allowed, f"artifact {artifact_id}")
            if entry["reason"] is not None:
                raise ContractError(f"available artifact {artifact_id} cannot have a reason")
            manifest_path = (catalog_path.parent / _safe_relative(entry["manifest"], "manifest path")).resolve()
            if catalog_path.parent.resolve() not in manifest_path.parents:
                raise ContractError("manifest escapes the catalog directory")
            manifest = validate_manifest(load_json(manifest_path))
            if manifest["license"]["id"] not in licenses:
                raise ContractError(f"artifact {artifact_id} references an undeclared license")
            sources = entry["sources"]
            if not isinstance(sources, list) or not sources:
                raise ContractError(f"available artifact {artifact_id} has no sources")
            normalized: list[dict[str, Any]] = []
            for index, raw_source in enumerate(sources):
                source = _exact(raw_source, {"path", "url", "bytes", "sha256"}, f"source {artifact_id}/{index}")
                path = _safe_relative(source["path"], "source path")
                _https_url(source["url"], "source URL")
                if isinstance(source["bytes"], bool) or not isinstance(source["bytes"], int) or source["bytes"] < 1:
                    raise ContractError(f"source {artifact_id}/{path} byte size is invalid")
                if not SHA256_RE.fullmatch(str(source["sha256"])):
                    raise ContractError(f"source {artifact_id}/{path} SHA-256 is invalid")
                normalized.append({"path": path, "bytes": source["bytes"], "sha256": source["sha256"]})
            expected = manifest["content"]["files"]
            if sorted(normalized, key=lambda item: item["path"]) != expected:
                raise ContractError(f"sources for {artifact_id} do not exactly match its manifest")
            if "localization" in entry:
                localization = _exact(
                    entry["localization"],
                    {"receipt_schema", "transform", "archive_sha256", "mount_paths", "tree"},
                    f"artifact {artifact_id} localization",
                )
                if localization["receipt_schema"] != "fs2-serve.nebius.ai/scientific-localization-receipt/v1":
                    raise ContractError(f"artifact {artifact_id} localization receipt schema is unsupported")
                if localization["transform"] not in {
                    "safe-extract-tar", "safe-extract-tar-gz", "safe-extract-zip"
                }:
                    raise ContractError(f"artifact {artifact_id} localization transform is unsupported")
                if len(sources) != 1 or localization["archive_sha256"] != sources[0]["sha256"]:
                    raise ContractError(f"artifact {artifact_id} localization archive identity conflicts with its source")
                mount_paths = localization["mount_paths"]
                if (
                    not isinstance(mount_paths, list)
                    or not mount_paths
                    or len(set(mount_paths)) != len(mount_paths)
                    or any(
                        not isinstance(path, str)
                        or not PurePosixPath(path).is_absolute()
                        or any(part in {".", ".."} for part in PurePosixPath(path).parts)
                        for path in mount_paths
                    )
                ):
                    raise ContractError(f"artifact {artifact_id} localization mount paths are invalid")
                tree = _exact(
                    localization["tree"],
                    {"entry_count", "total_bytes", "inventory_algorithm", "inventory_sha256"},
                    f"artifact {artifact_id} localization tree",
                )
                if (
                    isinstance(tree["entry_count"], bool)
                    or not isinstance(tree["entry_count"], int)
                    or tree["entry_count"] < 1
                    or isinstance(tree["total_bytes"], bool)
                    or not isinstance(tree["total_bytes"], int)
                    or tree["total_bytes"] < 1
                    or tree["inventory_algorithm"] != "fs2-flat-tree-inventory/v1"
                    or not SHA256_RE.fullmatch(str(tree["inventory_sha256"]))
                    or tree["inventory_sha256"] == localization["archive_sha256"]
                ):
                    raise ContractError(f"artifact {artifact_id} localization tree identity is invalid")
            if "provenance" in entry:
                validate_mirror_provenance(
                    entry["provenance"], artifact_id=artifact_id, manifest=manifest,
                    sources=sources, catalog_path=catalog_path,
                )
            entry["_manifest"] = manifest
            entry["_manifest_path"] = str(manifest_path)
            entry["_manifest_digest"] = sha256_bytes(canonical_json(manifest))
        elif state in {"unavailable", "excluded-private"}:
            entry = _exact(entry, common, f"artifact {artifact_id}")
            if not isinstance(entry["reason"], str) or not entry["reason"]:
                raise ContractError(f"blocked artifact {artifact_id} requires an exact reason")
        else:
            raise ContractError(f"artifact {artifact_id} has invalid state")
        if entry["id"] != artifact_id or not isinstance(entry["family"], str):
            raise ContractError(f"artifact {artifact_id} identity/family is invalid")
        if not isinstance(entry["consumers"], list) or not entry["consumers"]:
            raise ContractError(f"artifact {artifact_id} must name consumers")
    consumers = _object(catalog["consumers"], "catalog consumers")
    known = set(artifacts)
    for consumer_id, requirements in consumers.items():
        if not ID_RE.fullmatch(consumer_id) or not isinstance(requirements, list) or not requirements:
            raise ContractError(f"consumer {consumer_id} requirements are invalid")
        if len(set(requirements)) != len(requirements) or not set(requirements) <= known:
            raise ContractError(f"consumer {consumer_id} references unknown or duplicate artifacts")
        for artifact_id in requirements:
            if consumer_id not in artifacts[artifact_id]["consumers"]:
                raise ContractError(f"consumer mapping is not bidirectional for {consumer_id}/{artifact_id}")
    layouts = _object(catalog["consumer_layouts"], "catalog consumer layouts")
    if not set(layouts) <= set(consumers):
        raise ContractError("consumer layouts reference an unknown consumer")
    for consumer_id, requirements in consumers.items():
        expected = sorted(
            artifact_id
            for artifact_id in requirements
            if artifacts[artifact_id]["state"] == "available"
        )
        if not expected:
            if consumer_id in layouts:
                raise ContractError(f"private-only consumer {consumer_id} must not have a public cache layout")
            continue
        if consumer_id not in layouts:
            raise ContractError(f"consumer {consumer_id} has no public cache layout")
        layout_fields = {"mount_root", "bindings"}
        if "runtime_paths" in _object(layouts[consumer_id], f"consumer layout {consumer_id}"):
            layout_fields.add("runtime_paths")
        layout = _exact(layouts[consumer_id], layout_fields, f"consumer layout {consumer_id}")
        if layout["mount_root"] != "/models":
            raise ContractError(f"consumer {consumer_id} mount root must be /models")
        raw_bindings = layout["bindings"]
        if not isinstance(raw_bindings, list) or not raw_bindings:
            raise ContractError(f"consumer {consumer_id} bindings must be a non-empty array")
        bound_ids: list[str] = []
        runtime_files: set[str] = set()
        runtime_mounts: set[str] = set()
        for index, raw_binding in enumerate(raw_bindings):
            binding_fields = {"artifact_id", "mount_path", "read_only"}
            if "mount_root" in _object(
                raw_binding, f"consumer layout {consumer_id} binding {index}"
            ):
                binding_fields.add("mount_root")
            binding = _exact(
                raw_binding, binding_fields,
                f"consumer layout {consumer_id} binding {index}",
            )
            artifact_id = binding["artifact_id"]
            if artifact_id not in expected:
                raise ContractError(f"consumer {consumer_id} binds an unavailable or unrequired artifact")
            mount_root = binding.get("mount_root", layout["mount_root"])
            if mount_root not in {
                "/models",
                "/databases",
                "/opt/fs2/artifacts",
                "/opt/conda/lib/python3.10/site-packages",
            }:
                raise ContractError(
                    f"consumer {consumer_id}/{artifact_id} mount root is unsupported"
                )
            mount_path = binding["mount_path"]
            if not isinstance(mount_path, str):
                raise ContractError(
                    f"consumer {consumer_id}/{artifact_id} mount path must be absolute"
                )
            pure_mount = PurePosixPath(mount_path)
            pure_root = PurePosixPath(mount_root)
            if (
                not pure_mount.is_absolute()
                or any(part in {".", ".."} for part in pure_mount.parts)
                or pure_root not in pure_mount.parents
            ):
                raise ContractError(
                    f"consumer {consumer_id}/{artifact_id} mount path must be below {mount_root}"
                )
            if binding["read_only"] is not True:
                raise ContractError(f"consumer {consumer_id}/{artifact_id} binding must be read-only")
            bound_ids.append(artifact_id)
            runtime_mounts.add(mount_path.rstrip("/"))
            for source in artifacts[artifact_id]["_manifest"]["content"]["files"]:
                runtime_files.add(f"{mount_path.rstrip('/')}/{source['path']}")
        if bound_ids != expected:
            raise ContractError(f"consumer {consumer_id} bindings do not exactly cover available requirements")
        if "runtime_paths" in layout:
            runtime_paths = _object(
                layout["runtime_paths"], f"consumer layout {consumer_id} runtime paths"
            )
            if not runtime_paths:
                raise ContractError(f"consumer {consumer_id} runtime paths cannot be empty")
            for name, runtime_path in runtime_paths.items():
                if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                    raise ContractError(f"consumer {consumer_id} runtime path name is invalid")
                if (
                    not isinstance(runtime_path, str)
                    or runtime_path not in runtime_files | runtime_mounts
                ):
                    raise ContractError(
                        f"consumer {consumer_id} runtime path {name} does not resolve to a declared artifact file"
                    )

    for artifact_id, artifact in artifacts.items():
        localization = artifact.get("localization")
        if localization is None:
            continue
        bound_mounts = {
            binding["mount_path"]
            for consumer_id in artifact["consumers"]
            for binding in layouts[consumer_id]["bindings"]
            if binding["artifact_id"] == artifact_id
        }
        if bound_mounts != set(localization["mount_paths"]):
            raise ContractError(
                f"artifact {artifact_id} localization paths conflict with consumer bindings"
            )

    runtime_handoffs = _object(catalog["runtime_handoffs"], "catalog runtime handoffs")
    if not set(runtime_handoffs) <= set(layouts):
        raise ContractError("runtime handoffs reference an unknown public consumer layout")
    if runtime_handoffs and set(runtime_handoffs) != {"protenix-v2"}:
        raise ContractError("runtime handoffs currently support exactly the Protenix v2 contract")
    for consumer_id, raw_handoff in runtime_handoffs.items():
        handoff = _exact(
            raw_handoff,
            {
                "schema", "model_id", "variant_id", "source", "checkpoint",
                "localization", "image", "adapter", "semantic_smoke",
            },
            f"runtime handoff {consumer_id}",
        )
        if (
            handoff["schema"] != "fs2-serve.nebius.ai/public-artifact-runtime-handoff/v1"
            or handoff["model_id"] != consumer_id
            or handoff["variant_id"] != "upstream-v2-0-0"
        ):
            raise ContractError("Protenix v2 runtime handoff identity is invalid")
        source_contract = _exact(
            handoff["source"], {"repository", "revision", "tag", "model_name"},
            "Protenix v2 runtime source",
        )
        if source_contract != {
            "repository": "https://github.com/bytedance/Protenix",
            "revision": "2475421477ab414b571149ad4a875c390ff8a35d",
            "tag": "v2.0.0",
            "model_name": "protenix-v2",
        }:
            raise ContractError("Protenix v2 runtime handoff must retain the exact v2.0.0 source")
        checkpoint = _exact(
            handoff["checkpoint"],
            {
                "artifact_id", "source_path", "mount_path", "runtime_path", "bytes",
                "sha256", "md5", "provenance_state", "mirror_repository",
                "mirror_revision",
            },
            "Protenix v2 runtime checkpoint",
        )
        artifact = artifacts.get(checkpoint["artifact_id"])
        if artifact is None or artifact["state"] != "available":
            raise ContractError("Protenix v2 composite artifact is unavailable")
        artifact_sources = {
            source["path"]: source for source in artifact["sources"]
        }
        artifact_source = artifact_sources.get(checkpoint["source_path"])
        provenance = artifact.get("provenance")
        layout = layouts[consumer_id]
        binding = next(
            (
                item for item in layout["bindings"]
                if item["artifact_id"] == checkpoint["artifact_id"]
            ),
            None,
        )
        expected_runtime_path = (
            f"{checkpoint['mount_path'].rstrip('/')}/{checkpoint['source_path']}"
        )
        if (
            artifact_source is None
            or artifact_source["path"] != checkpoint["source_path"]
            or artifact_source["bytes"] != checkpoint["bytes"]
            or artifact_source["sha256"] != checkpoint["sha256"]
            or not isinstance(provenance, dict)
            or provenance["state"] != checkpoint["provenance_state"]
            or provenance["verification"]["md5"] != checkpoint["md5"]
            or provenance["acquisition_source"]["repository"] != checkpoint["mirror_repository"]
            or provenance["acquisition_source"]["repository_revision"] != checkpoint["mirror_revision"]
            or binding is None
            or binding["mount_path"] != checkpoint["mount_path"]
            or checkpoint["runtime_path"] != expected_runtime_path
            or layout.get("runtime_paths", {}).get("checkpoint") != checkpoint["runtime_path"]
        ):
            raise ContractError("Protenix v2 runtime handoff checkpoint conflicts with catalog bytes or layout")
        payload_files = artifact["_manifest"]["content"]["files"]
        (
            localization_manifest,
            localization_digest,
            localized_files,
            localized_content_digest,
        ) = protenix_localized_inventory(payload_files)
        localization = _exact(
            handoff["localization"],
            {
                "artifact_id", "mount_path", "manifest_path", "manifest_schema",
                "manifest_sha256", "ready_marker_path", "source_content_digest_sha256",
                "content_digest_sha256", "files", "required_files",
            },
            "Protenix v2 localization contract",
        )
        required_files = [item["path"] for item in localized_files]
        if localization != {
            "artifact_id": "protenix-v2",
            "mount_path": "/models/protenix-v2",
            "manifest_path": "/models/protenix-v2/manifest.json",
            "manifest_schema": localization_manifest["schema"],
            "manifest_sha256": localization_digest,
            "ready_marker_path": "/models/protenix-v2/.fs2-manifest-sha256",
            "source_content_digest_sha256": artifact["_manifest"]["content"]["digest"],
            "content_digest_sha256": localized_content_digest,
            "files": localized_files,
            "required_files": required_files,
        }:
            raise ContractError("Protenix v2 must use one exact composite localization contract")
        image = _exact(
            handoff["image"],
            {
                "runtime_id", "source_revision", "candidate_tag", "runtime_base_image",
                "entrypoint", "required_checkpoint_path", "required_manifest_path",
                "required_manifest_sha256", "required_ready_marker_path",
                "required_content_sha256", "digest_required", "known_unqualified_digests",
            },
            "Protenix v2 image handoff",
        )
        if image != {
            "runtime_id": "protenix-v2",
            "source_revision": source_contract["revision"],
            "candidate_tag": f"{source_contract['revision']}-h100-r2",
            "runtime_base_image": (
                "pytorch/pytorch:2.7.1-cuda12.6-cudnn9-runtime@sha256:"
                "2b59b1b91885677814f78be1f8df48a25d5dc952eb6580eaecfefca510f9afd3"
            ),
            "entrypoint": "/usr/local/bin/fs2-run-protenix",
            "required_checkpoint_path": checkpoint["runtime_path"],
            "required_manifest_path": localization["manifest_path"],
            "required_manifest_sha256": localization_digest,
            "required_ready_marker_path": localization["ready_marker_path"],
            "required_content_sha256": localized_content_digest,
            "digest_required": True,
            "known_unqualified_digests": [
                "sha256:ad2a55f1740f49296ec730e9ff4f1d06ad391a87354f03b2921f960fe0f6d240"
            ],
        }:
            raise ContractError("Protenix v2 image handoff is not pinned to the corrected H100 contract")
        adapter = _exact(
            handoff["adapter"],
            {
                "model_id", "variant_id", "artifact_id", "required_files", "mount_path",
                "expected_content_sha256", "expected_manifest_sha256",
                "forbidden_artifact_ids",
            },
            "Protenix v2 adapter handoff",
        )
        if adapter != {
            "model_id": consumer_id,
            "variant_id": handoff["variant_id"],
            "artifact_id": checkpoint["artifact_id"],
            "required_files": required_files,
            "mount_path": "/models/protenix-v2",
            "expected_content_sha256": localized_content_digest,
            "expected_manifest_sha256": localization_digest,
            "forbidden_artifact_ids": ["protenix-v1-substitute"],
        }:
            raise ContractError("Protenix v2 adapter handoff permits path drift or a v1 substitute")
        smoke = _exact(
            handoff["semantic_smoke"],
            {
                "state", "target", "fixture", "network_mode", "preprocessing", "stages",
                "required_evidence",
            },
            "Protenix v2 semantic smoke",
        )
        if smoke["state"] != "required-not-yet-qualified" or smoke["network_mode"] != "offline":
            raise ContractError("Protenix v2 semantic smoke cannot claim qualification before live evidence")
        target = _exact(
            smoke["target"],
            {"project_id", "region", "cluster_context", "accelerator_product", "compute_capability"},
            "Protenix v2 semantic smoke target",
        )
        if target != {
            "project_id": "${PROJECT_ID}",
            "region": "eu-north1",
            "cluster_context": "k8s-inference-h100",
            "accelerator_product": "NVIDIA-H100-80GB-HBM3",
            "compute_capability": "9.0",
        }:
            raise ContractError(
                "Protenix v2 semantic smoke target must retain provider-neutral project input "
                "and the exact H100 accelerator contract"
            )
        fixture = _exact(smoke["fixture"], {"path", "sha256"}, "Protenix v2 smoke fixture")
        fixture_path = (
            catalog_path.parent / _safe_relative(fixture["path"], "Protenix v2 smoke fixture path")
        ).resolve()
        if (
            catalog_path.parent.resolve() not in fixture_path.parents
            or not fixture_path.is_file()
            or sha256_file(fixture_path) != fixture["sha256"]
        ):
            raise ContractError("Protenix v2 semantic smoke fixture is absent or changed")
        preprocessing = _exact(
            smoke["preprocessing"],
            {"namespace", "local_queue", "pool", "artifact_id", "mount_path"},
            "Protenix v2 semantic smoke preprocessing",
        )
        if preprocessing != {
            "namespace": "fs2-reference-data",
            "local_queue": "reference-data",
            "pool": "reference-data",
            "artifact_id": "protenix-v2",
            "mount_path": "/models/protenix-v2",
        }:
            raise ContractError("Protenix v2 smoke must use the Terraform-owned reference-data plane")
        stages = smoke["stages"]
        if not isinstance(stages, list) or [stage.get("id") for stage in stages if isinstance(stage, dict)] != [
            "prepare-data", "sample-structure"
        ]:
            raise ContractError("Protenix v2 smoke must retain the CPU-prep to H100 stage order")
        for index, stage in enumerate(stages):
            item = _exact(stage, {"id", "compute", "argv"}, f"Protenix v2 smoke stage {index}")
            if (
                not isinstance(item["argv"], list)
                or not item["argv"]
                or item["argv"][0] != image["entrypoint"]
                or any(not isinstance(argument, str) or not argument for argument in item["argv"])
            ):
                raise ContractError("Protenix v2 semantic smoke stages require direct pinned-image argv")
        if [stage["compute"] for stage in stages] != ["dedicated-cpu-preprocessing", "h100-sm90"]:
            raise ContractError("Protenix v2 semantic smoke compute placement drifted")
        required_evidence = smoke["required_evidence"]
        if not isinstance(required_evidence, list) or set(required_evidence) != {
            "corrected-image-repository-digest", "public-cache-receipt",
            "composite-localization-receipt",
            "observed-h100-sm90", "offline-egress-enforcement", "parseable-mmcif",
            "finite-confidence-json", "semantic-validator-pass",
        }:
            raise ContractError("Protenix v2 semantic smoke evidence gate is incomplete")

    reference_layouts = _object(catalog["reference_layouts"], "catalog reference layouts")
    if not set(reference_layouts) <= set(consumers):
        raise ContractError("reference layouts reference an unknown consumer")
    for consumer_id, raw_layout in reference_layouts.items():
        layout = _exact(
            raw_layout,
            {
                "bundle_id", "bundle_revision", "source_plane", "source_revision",
                "mount_path", "read_only", "runtime_argument",
            },
            f"reference layout {consumer_id}",
        )
        if not re.fullmatch(r"[a-z0-9](?:[-.a-z0-9]*[a-z0-9])?", str(layout["bundle_id"])):
            raise ContractError(f"reference layout {consumer_id} bundle id is invalid")
        if not isinstance(layout["bundle_revision"], str) or not layout["bundle_revision"]:
            raise ContractError(f"reference layout {consumer_id} bundle revision is required")
        if layout["source_plane"] != "reference-data":
            raise ContractError(f"reference layout {consumer_id} source plane must be reference-data")
        if not re.fullmatch(r"[a-f0-9]{40}", str(layout["source_revision"])):
            raise ContractError(f"reference layout {consumer_id} source revision is invalid")
        if layout["mount_path"] != "/databases" or layout["read_only"] is not True:
            raise ContractError(
                f"reference layout {consumer_id} must mount the public database tree read-only at /databases"
            )
        if layout["runtime_argument"] != "--db_dir=/databases":
            raise ContractError(
                f"reference layout {consumer_id} runtime argument must be --db_dir=/databases"
            )

    evidence_relative = "evidence/esm-af3-external-runtime-contract-20260902.json"
    evidence_path = (catalog_path.parent / evidence_relative).resolve()
    if catalog_path.parent.resolve() not in evidence_path.parents:
        raise ContractError("external runtime evidence escapes the catalog directory")

    runtime_constraints = _object(
        catalog["runtime_constraints"], "catalog runtime constraints"
    )
    esm_consumers = {"esmfold2", "esmfold2-fast"}
    if runtime_constraints and set(runtime_constraints) != esm_consumers:
        raise ContractError(
            "ESM runtime constraints must cover exactly esmfold2 and esmfold2-fast"
        )
    expected_binary_evidence = {
        "component": "flash-attn",
        "version": "2.7.4.post1",
        "wheel": "flash_attn-2.7.4.post1-cp312-cp312-linux_x86_64.whl",
        "wheel_url": "https://github.com/evolutionaryscale/wheels/releases/download/py312-pt211-cu13-sm80-90/flash_attn-2.7.4.post1-cp312-cp312-linux_x86_64.whl",
        "source_revision": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
        "native_cubins": ["sm80", "sm90"],
        "ptx_present": False,
        "inspection": "manager-provided-frozen-wheel-binary-inspection",
    }
    expected_runtime_constraint = {
        "binary_compatibility": "candidate-hopper-sm90-cubin-no-ptx",
        "candidate_accelerator_families": ["Hopper"],
        "candidate_cuda_architectures": ["sm90"],
        "qualification_state": "pending-exact-image-h100-semantic-test",
        "blackwell_state": "blocked",
        "blackwell_unblock_requires_one_of": [
            "qualified-sdpa-fallback-image",
            "target-aware-blackwell-image",
        ],
        "external_immutable_artifacts": ["esmc-6b", "esmfold2-ccd"],
        "binary_evidence": expected_binary_evidence,
        "evidence": evidence_relative,
    }
    runtime_constraint_fields = set(expected_runtime_constraint)
    for consumer_id, raw_constraint in runtime_constraints.items():
        constraint = _exact(
            raw_constraint,
            runtime_constraint_fields,
            f"runtime constraint {consumer_id}",
        )
        if constraint != expected_runtime_constraint:
            raise ContractError(
                f"runtime constraint {consumer_id} must retain the exact binary-candidate contract"
            )
        _https_url(
            constraint["binary_evidence"]["wheel_url"],
            f"runtime constraint {consumer_id} wheel URL",
        )
        for artifact_id in constraint["external_immutable_artifacts"]:
            if (
                artifact_id not in consumers[consumer_id]
                or artifacts[artifact_id]["state"] != "available"
            ):
                raise ContractError(
                    f"runtime constraint {consumer_id} references an unavailable external artifact"
                )
        binding_ids = {
            binding["artifact_id"] for binding in layouts[consumer_id]["bindings"]
        }
        if not set(constraint["external_immutable_artifacts"]) <= binding_ids:
            raise ContractError(
                f"runtime constraint {consumer_id} external artifacts are not mounted read-only"
            )

    private_layouts = _object(catalog["private_layouts"], "catalog private layouts")
    if private_layouts and set(private_layouts) != {"alphafold3"}:
        raise ContractError("private layouts may expose only the AlphaFold3 private delivery contract")
    expected_private_layout = {
        "artifact_id": "alphafold3-private",
        "source_plane": "academic-assets",
        "cache_scope": "tenant-private",
        "general_shared_cache_allowed": False,
        "embed_in_image": False,
        "source_url": "https://storage.googleapis.com/alphafold3/af3.bin.zst",
        "source_revision": "gs://alphafold3/af3.bin.zst#1780568696389861",
        "generation": "1780568696389861",
        "last_modified": "2026-06-04T10:24:56Z",
        "filename": "af3.bin.zst",
        "bytes": 1020545840,
        "sha256": "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff",
        "mount_path": "/models",
        "file_path": "/models/af3.bin.zst",
        "read_only": True,
        "runtime_argument": "--model_dir=/models",
        "evidence": evidence_relative,
    }
    for consumer_id, raw_layout in private_layouts.items():
        private_layout = _exact(
            raw_layout, set(expected_private_layout), f"private layout {consumer_id}"
        )
        if private_layout != expected_private_layout:
            raise ContractError(
                "private layout alphafold3 must retain the exact private identity and /models path"
            )
        _https_url(private_layout["source_url"], "private layout alphafold3 source URL")
        if not SHA256_RE.fullmatch(str(private_layout["sha256"])):
            raise ContractError("private layout alphafold3 SHA-256 is invalid")
        artifact_id = private_layout["artifact_id"]
        if (
            artifact_id not in consumers[consumer_id]
            or artifacts[artifact_id]["state"] != "excluded-private"
        ):
            raise ContractError(
                "private layout alphafold3 must reference its excluded-private artifact"
            )
        if consumer_id in layouts:
            raise ContractError("private layout alphafold3 must not have a public cache layout")

    if runtime_constraints or private_layouts:
        evidence = _exact(
            load_json(evidence_path),
            {"schema", "observed_at", "esm_current_image", "alphafold3_private_parameters", "limitations"},
            "external runtime evidence",
        )
        if evidence["schema"] != "fs2-serve.nebius.ai/external-runtime-contract-evidence/v1":
            raise ContractError("external runtime evidence schema is unsupported")
        try:
            dt.datetime.fromisoformat(str(evidence["observed_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("external runtime evidence observed_at is not RFC 3339") from exc
        if not isinstance(evidence["limitations"], list) or not evidence["limitations"]:
            raise ContractError("external runtime evidence must retain explicit limitations")
        expected_esm_evidence = {
            key: expected_runtime_constraint[key]
            for key in expected_runtime_constraint
            if key != "evidence"
        }
        expected_esm_evidence["consumers"] = ["esmfold2", "esmfold2-fast"]
        if evidence["esm_current_image"] != expected_esm_evidence:
            raise ContractError("external runtime evidence conflicts with the ESM constraints")
        expected_af3_evidence = {
            key: expected_private_layout[key]
            for key in expected_private_layout
            if key != "evidence"
        }
        if evidence["alphafold3_private_parameters"] != expected_af3_evidence:
            raise ContractError("external runtime evidence conflicts with the AlphaFold3 private layout")
    return catalog


def _download(source: Mapping[str, Any], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    expected_bytes = source["bytes"]
    if target.exists():
        actual_bytes = target.stat().st_size
        if actual_bytes == expected_bytes:
            if sha256_file(target) == source["sha256"]:
                return
            target.unlink()
        elif actual_bytes > expected_bytes:
            target.unlink()
    offset = target.stat().st_size if target.exists() else 0
    headers = {"User-Agent": "fs2-public-artifact-ingester/1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    try:
        response = request.urlopen(request.Request(source["url"], headers=headers), timeout=120)  # noqa: S310
    except error.HTTPError as exc:
        if offset and exc.code == 416:
            target.unlink(missing_ok=True)
            return _download(source, target)
        raise ContractError(f"download failed for {source['path']}: HTTP {exc.code}") from exc
    with response:
        status = getattr(response, "status", response.getcode())
        if offset and status != 206:
            offset = 0
        content_range = response.headers.get("Content-Range", "")
        if offset and not content_range.startswith(f"bytes {offset}-"):
            raise ContractError(f"source returned an invalid resume range for {source['path']}")
        mode = "ab" if offset else "wb"
        with target.open(mode) as handle:
            for chunk in iter(lambda: response.read(BUFFER_BYTES), b""):
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    actual_bytes = target.stat().st_size
    if actual_bytes != expected_bytes:
        raise ContractError(
            f"size mismatch for {source['path']}: expected {expected_bytes}, got {actual_bytes}"
        )
    actual_sha = sha256_file(target)
    if actual_sha != source["sha256"]:
        raise ContractError(
            f"SHA-256 mismatch for {source['path']}: expected {source['sha256']}, got {actual_sha}"
        )


def verify_tree(root: Path, manifest: Mapping[str, Any]) -> None:
    expected = {item["path"]: item for item in manifest["content"]["files"]}
    actual: set[str] = set()
    if not root.is_dir() or root.is_symlink():
        raise ContractError(f"artifact root is missing or unsafe: {root}")
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ContractError(f"symlink is forbidden in artifact tree: {relative}")
        if path.is_file():
            actual.add(relative)
            if relative not in expected:
                raise ContractError(f"undeclared artifact file: {relative}")
            item = expected[relative]
            if path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
                raise ContractError(f"artifact verification failed: {relative}")
        elif not path.is_dir():
            raise ContractError(f"non-file artifact entry is forbidden: {relative}")
    if actual != set(expected):
        raise ContractError(f"artifact tree is incomplete: missing={sorted(set(expected) - actual)}")


def _safe_archive_name(value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"..", ""} for part in path.parts):
        raise ContractError(f"archive contains an unsafe path: {value}")


def offline_smoke(root: Path, smoke: str) -> list[str]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    checks: list[str] = []
    if smoke == "binarycif-ccd":
        relative_files = [path.relative_to(root).as_posix() for path in files]
        if relative_files != ["components.bcif"]:
            raise ContractError("OpenFold3 CCD artifact must contain exactly components.bcif")
        path = files[0]
        required_markers = (
            b"_chem_comp", b"_chem_comp_atom", b"_chem_comp_bond",
            b"components", b"biotite", b"0.3.0",
        )
        with path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            if data[:12] != b"\x83\xaadataBlocks":
                raise ContractError("OpenFold3 components.bcif is not the expected BinaryCIF envelope")
            missing = [marker.decode("ascii") for marker in required_markers if data.find(marker) < 0]
        if missing:
            raise ContractError(
                f"OpenFold3 components.bcif lacks required BinaryCIF CCD markers: {missing}"
            )
        checks.extend([
            "openfold3-components-exact-file-set",
            "binarycif-envelope",
            "binarycif-ccd-required-categories",
            "binarycif-components-block",
            "binarycif-biotite-0.3.0",
        ])
    if smoke in {"hf-sharded-snapshot", "esmc-6b-snapshot"}:
        configs = [path for path in files if path.name == "config.json"]
        indexes = [path for path in files if path.name.endswith(".safetensors.index.json")]
        tokenizers = [path for path in files if path.name == "tokenizer.json"]
        if len(configs) != 1 or len(indexes) != 1 or len(tokenizers) != 1:
            raise ContractError("offline HF snapshot lacks config, tokenizer, or shard index")
        index = load_json(indexes[0])
        weight_map = _object(index.get("weight_map"), "safetensors weight_map")
        referenced = {path.name for path in files}
        missing = sorted(set(weight_map.values()) - referenced)
        if missing:
            raise ContractError(f"HF shard index references absent files: {missing}")
        checks.extend(["config-json", "tokenizer-json", "safetensors-index-all-shards"])
        if smoke == "esmc-6b-snapshot":
            expected_shards = {
                f"model-{index:05d}-of-00006.safetensors" for index in range(1, 7)
            }
            actual_shards = {path.name for path in files if path.suffix == ".safetensors"}
            support_files = {"config.json", "model.safetensors.index.json", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json"}
            actual_names = {path.name for path in files}
            if actual_shards != expected_shards:
                raise ContractError("ESMC-6B snapshot must contain exactly all six declared shards")
            expected_files = expected_shards | support_files
            if actual_names != expected_files:
                missing_files = sorted(expected_files - actual_names)
                extra_files = sorted(actual_names - expected_files)
                raise ContractError(
                    "ESMC-6B snapshot does not exactly match its six-shard support-file set: "
                    f"missing={missing_files}, extra={extra_files}"
                )
            if set(weight_map.values()) != expected_shards:
                raise ContractError("ESMC-6B shard index does not bind exactly all six shards")
            checks.extend([
                "esmc-6b-exact-file-set",
                "esmc-6b-six-shards",
                "esmc-6b-index-binds-all-six-shards",
                "esmc-6b-tokenizer-support-files",
            ])
    if smoke == "esmfold2-model-snapshot":
        relative_files = {path.relative_to(root).as_posix() for path in files}
        expected_files = {"config.json", "model.safetensors"}
        if relative_files != expected_files:
            raise ContractError(
                "ESMFold2 model artifact must contain exactly config.json and model.safetensors"
            )
        checks.append("esmfold2-model-files-complete")
    if smoke == "ccd-pickle":
        relative_files = [path.relative_to(root).as_posix() for path in files]
        if relative_files != ["ccd.pkl"]:
            raise ContractError("ESMFold2 CCD artifact must contain exactly ccd.pkl")
        checks.append("esmfold2-ccd-complete")
    if smoke == "protenix-v2-bundle":
        expected = {
            "checkpoint/protenix-v2.pt",
            "common/clusters-by-entity-40.txt",
            "common/components.cif",
            "common/components.cif.rdkit_mol.pkl",
            "common/obsolete_release_date.csv",
        }
        actual = {path.relative_to(root).as_posix() for path in files}
        if actual != expected:
            raise ContractError("Protenix v2 source tree must contain exactly checkpoint plus four common files")
        components = root / "common/components.cif"
        obsolete = root / "common/obsolete_release_date.csv"
        clusters = root / "common/clusters-by-entity-40.txt"
        with components.open("rb") as handle:
            components_header = handle.read(64)
        if not components_header.lstrip().startswith(b"data_"):
            raise ContractError("Protenix components.cif is not a CIF data document")
        with obsolete.open("rb") as handle:
            obsolete_header = handle.read(4096)
        if b"," not in obsolete_header:
            raise ContractError("Protenix obsolete_release_date.csv lacks a CSV header")
        if clusters.stat().st_size < 1:
            raise ContractError("Protenix cluster identity file is empty")
        checks.extend([
            "protenix-v2-exact-source-file-set",
            "protenix-v2-common-cif",
            "protenix-v2-obsolete-release-csv",
            "protenix-v2-cluster-identities",
        ])
    if smoke == "boltzgen-molecules-zip":
        relative_files = [path.relative_to(root).as_posix() for path in files]
        if relative_files != ["mols.zip"]:
            raise ContractError("BoltzGen molecule artifact must contain exactly mols.zip")
        with zipfile.ZipFile(root / "mols.zip") as archive:
            names = [member.filename for member in archive.infolist()]
        if (
            len(names) != 45227
            or len(set(names)) != 45227
            or any("/" in name or re.fullmatch(r"[A-Z0-9]{1,5}\.pkl", name) is None for name in names)
        ):
            raise ContractError(
                "BoltzGen mols.zip must contain exactly 45,227 unique flat-root one-to-five-character PKL files"
            )
        checks.extend([
            "boltzgen-molecules-exact-archive",
            "boltzgen-molecules-45227-flat-root-pkl-files",
        ])
    for path in files:
        if path.suffix == ".json":
            load_json(path)
            checks.append(f"json:{path.relative_to(root)}")
        elif path.suffix == ".safetensors":
            with path.open("rb") as handle:
                raw = handle.read(8)
                if len(raw) != 8:
                    raise ContractError(f"invalid safetensors header: {path}")
                header_size = struct.unpack("<Q", raw)[0]
                if header_size <= 2 or header_size > min(path.stat().st_size - 8, 512 * 1024 * 1024):
                    raise ContractError(f"invalid safetensors header size: {path}")
                json.loads(handle.read(header_size))
            checks.append(f"safetensors-header:{path.relative_to(root)}")
        elif path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    _safe_archive_name(member.filename)
                if archive.testzip() is not None:
                    raise ContractError(f"ZIP CRC smoke failed: {path}")
            checks.append(f"zip-crc:{path.relative_to(root)}")
        elif path.suffix == ".tar" or path.name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(path, "r:*") as archive:
                for member in archive.getmembers():
                    _safe_archive_name(member.name)
                    if member.issym() or member.islnk() or member.isdev():
                        raise ContractError(f"unsafe tar member in {path}: {member.name}")
            checks.append(f"tar-index:{path.relative_to(root)}")
        elif path.suffix in {".pt", ".pth", ".ckpt", ".pkl"}:
            with path.open("rb") as handle:
                prefix = handle.read(4)
            if prefix not in {b"PK\x03\x04", b"\x80\x02cc", b"\x80\x03cc", b"\x80\x04\x95", b"\x80\x05\x95"} and not prefix.startswith(b"\x80"):
                raise ContractError(f"checkpoint container is not recognizable: {path}")
            checks.append(f"checkpoint-container:{path.relative_to(root)}")
    if not checks:
        raise ContractError(f"offline smoke mode {smoke} performed no checks")
    return checks


def artifact_destination(cache_root: Path, manifest: Mapping[str, Any]) -> Path:
    return cache_root / "objects" / manifest["model_id"] / "sha256" / manifest["content"]["digest"]


def artifact_consumer_bindings(catalog: Mapping[str, Any], artifact_id: str) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    for consumer_id, layout in sorted(catalog["consumer_layouts"].items()):
        for binding in layout["bindings"]:
            if binding["artifact_id"] == artifact_id:
                bindings.append(
                    {
                        "consumer_id": consumer_id,
                        "mount_root": binding.get("mount_root", layout["mount_root"]),
                        "mount_path": binding["mount_path"],
                        "read_only": True,
                    }
                )
    if not bindings:
        raise ContractError(f"available artifact {artifact_id} has no consumer binding")
    return bindings


def artifact_runtime_handoffs(catalog: Mapping[str, Any], artifact_id: str) -> list[dict[str, Any]]:
    """Project exact runtime paths and composite localization into a receipt."""
    handoffs: list[dict[str, Any]] = []
    for consumer_id, handoff in sorted(catalog["runtime_handoffs"].items()):
        checkpoint = handoff["checkpoint"]
        if checkpoint["artifact_id"] != artifact_id:
            continue
        localization = handoff["localization"]
        handoffs.append(
            {
                "consumer_id": consumer_id,
                "source_path": checkpoint["source_path"],
                "mount_path": checkpoint["mount_path"],
                "runtime_path": checkpoint["runtime_path"],
                "localization": dict(localization),
            }
        )
    return handoffs


def verify_protenix_localization(
    root: Path,
    payload_manifest: Mapping[str, Any],
    localization: Mapping[str, Any],
) -> str:
    """Verify the exact seven-path Protenix runtime tree and ready marker."""
    if not root.is_dir() or root.is_symlink():
        raise ContractError(f"localized Protenix root is missing or unsafe: {root}")
    expected_payload = {
        item["path"]: item for item in payload_manifest["content"]["files"]
    }
    (
        expected_document,
        expected_digest,
        localized_files,
        localized_content_digest,
    ) = protenix_localized_inventory(payload_manifest["content"]["files"])
    if expected_digest != localization["manifest_sha256"]:
        raise ContractError("Protenix localization manifest digest conflicts with the catalog")
    if localization["source_content_digest_sha256"] != payload_manifest["content"]["digest"]:
        raise ContractError("Protenix localization source digest conflicts with the cache object")
    if localization["files"] != localized_files:
        raise ContractError("Protenix localized file inventory conflicts with the catalog")
    if localization["content_digest_sha256"] != localized_content_digest:
        raise ContractError("Protenix localized tree digest conflicts with the catalog")
    expected_paths = {item["path"] for item in localized_files}
    actual_paths: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ContractError(f"symlink is forbidden in localized Protenix tree: {relative}")
        if path.is_file():
            actual_paths.add(relative)
        elif not path.is_dir():
            raise ContractError(f"unsafe entry in localized Protenix tree: {relative}")
    if actual_paths != expected_paths:
        raise ContractError(
            "localized Protenix tree is incomplete or contains extras: "
            f"missing={sorted(expected_paths - actual_paths)}, "
            f"extra={sorted(actual_paths - expected_paths)}"
        )
    for relative, item in expected_payload.items():
        path = root / relative
        if path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
            raise ContractError(f"localized Protenix payload identity changed: {relative}")
    if load_json(root / "manifest.json") != expected_document:
        raise ContractError("localized Protenix manifest content conflicts with the catalog")
    marker = (root / ".fs2-manifest-sha256").read_text(encoding="utf-8").strip()
    if marker != expected_digest:
        raise ContractError("localized Protenix ready marker does not match the manifest")
    for item in localized_files:
        if item["path"] in expected_payload:
            continue
        path = root / item["path"]
        if path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
            raise ContractError(f"localized Protenix generated identity changed: {item['path']}")
    return localized_content_digest


def _validate_materialization_destination(destination: Path) -> None:
    if not destination.is_absolute() or ".." in destination.parts or len(destination.parts) < 3:
        raise ContractError("materialization destination must be a specific absolute path")
    if destination.exists() and destination.is_symlink():
        raise ContractError("materialization destination cannot be a symlink")
    for parent in destination.parents:
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise ContractError("materialization destination parent is unsafe")


def materialize_protenix_source(
    source_root: Path,
    destination: Path,
    payload_manifest: Mapping[str, Any],
    localization: Mapping[str, Any],
) -> str:
    """Atomically create the sole runtime Protenix tree from verified source bytes."""
    verify_tree(source_root, payload_manifest)
    _validate_materialization_destination(destination)
    if destination.exists():
        return verify_protenix_localization(destination, payload_manifest, localization)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.localizing-", dir=destination.parent)
    )
    try:
        for item in payload_manifest["content"]["files"]:
            source = source_root / item["path"]
            target = staging / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
        document = protenix_localization_document(payload_manifest["content"]["files"])
        atomic_json(staging / "manifest.json", document)
        manifest_digest = sha256_bytes(canonical_json(document))
        marker = staging / ".fs2-manifest-sha256"
        marker.write_text(f"{manifest_digest}\n", encoding="utf-8")
        marker.chmod(0o444)
        verify_protenix_localization(staging, payload_manifest, localization)
        try:
            os.replace(staging, destination)
        except OSError:
            if not destination.exists():
                raise
            verify_protenix_localization(destination, payload_manifest, localization)
        for path in sorted(destination.rglob("*"), reverse=True):
            path.chmod(0o555 if path.is_dir() else 0o444)
        destination.chmod(0o555)
        return localization["content_digest_sha256"]
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def materialize_artifact(
    catalog_path: Path,
    artifact_id: str,
    cache_root: Path,
    destination: Path,
) -> dict[str, Any]:
    """Create a runtime view only from an existing verified cache receipt."""
    catalog = validate_catalog(load_json(catalog_path), catalog_path)
    if artifact_id != "protenix-v2":
        raise ContractError("only Protenix v2 requires a composite localization transform")
    entry = catalog["artifacts"][artifact_id]
    receipt_path = cache_root / "receipts" / artifact_id / f"{entry['_manifest_digest']}.json"
    if not receipt_path.is_file():
        raise ContractError("Protenix materialization requires its verified cache receipt")
    receipt = load_json(receipt_path)
    unsigned = dict(receipt)
    receipt_digest = unsigned.pop("receipt_digest", None)
    if (
        receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("artifact_id") != artifact_id
        or receipt.get("manifest_digest") != entry["_manifest_digest"]
        or receipt_digest != sha256_bytes(canonical_json(unsigned))
    ):
        raise ContractError("Protenix cache receipt is invalid")
    source_root = artifact_destination(cache_root, entry["_manifest"])
    localization = catalog["runtime_handoffs"][artifact_id]["localization"]
    content_digest = materialize_protenix_source(
        source_root, destination, entry["_manifest"], localization
    )
    result = {
        "schema": "fs2-serve.nebius.ai/public-artifact-localization-receipt/v1",
        "artifact_id": artifact_id,
        "source_receipt_digest": receipt_digest,
        "source_content_digest": entry["_manifest"]["content"]["digest"],
        "content_digest": content_digest,
        "manifest_sha256": localization["manifest_sha256"],
        "destination": str(destination),
        "required_files": localization["required_files"],
        "verified_at": utc_now(),
    }
    result["receipt_digest"] = sha256_bytes(canonical_json(result))
    receipt_output = (
        cache_root / "localization-receipts" / artifact_id / f"{content_digest}.json"
    )
    atomic_json(receipt_output, result)
    return result


def validate_storage_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "project_id", "region", "cluster", "filesystem_id", "filesystem_size_gib",
        "namespace", "local_queue", "cpu_pool_id", "cpu_pool_name", "cpu_pool_label",
        "shared_filesystem_host_path",
        "cache_subpath", "reference_plane_source_commit", "source_commit",
    }
    if set(metadata) != required:
        raise ContractError(f"storage metadata fields must be exactly {sorted(required)}")
    text_fields = required - {"filesystem_size_gib"}
    if any(not isinstance(metadata[key], str) or not metadata[key] for key in text_fields):
        raise ContractError("all non-secret storage provenance fields are required")
    if not re.fullmatch(r"computefilesystem-[a-z0-9]+", metadata["filesystem_id"]):
        raise ContractError("filesystem_id must identify a managed filesystem")
    size = metadata["filesystem_size_gib"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 2048:
        raise ContractError("the integrated regional reference filesystem must be at least 2048 GiB")
    if metadata["namespace"] != "fs2-reference-data":
        raise ContractError("artifact ingestion requires the isolated fs2-reference-data namespace")
    if metadata["cpu_pool_label"] != "reference-data":
        raise ContractError("artifact ingestion requires the integrated reference-data CPU pool")
    host_path = metadata["shared_filesystem_host_path"]
    if not host_path.startswith("/mnt/") or ".." in Path(host_path).parts:
        raise ContractError("shared_filesystem_host_path must be a safe absolute path below /mnt")
    cache_subpath = metadata["cache_subpath"]
    if cache_subpath.startswith("/") or not Path(cache_subpath).parts or ".." in Path(cache_subpath).parts:
        raise ContractError("cache_subpath must be a safe relative path")
    if not COMMIT_RE.fullmatch(metadata["source_commit"]) or not COMMIT_RE.fullmatch(metadata["reference_plane_source_commit"]):
        raise ContractError("source commits must be immutable hexadecimal commits")
    return dict(metadata)


def stage_artifact(
    catalog_path: Path,
    artifact_id: str,
    cache_root: Path,
    metadata: Mapping[str, Any],
    download_concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY,
) -> dict[str, Any]:
    if (
        isinstance(download_concurrency, bool)
        or not isinstance(download_concurrency, int)
        or not 1 <= download_concurrency <= MAX_DOWNLOAD_CONCURRENCY
    ):
        raise ContractError(
            f"download concurrency must be an integer between 1 and {MAX_DOWNLOAD_CONCURRENCY}"
        )
    storage = validate_storage_metadata(metadata)
    catalog = validate_catalog(load_json(catalog_path), catalog_path)
    if artifact_id not in catalog["artifacts"]:
        raise ContractError(f"artifact is not declared: {artifact_id}")
    entry = catalog["artifacts"][artifact_id]
    if entry["state"] != "available":
        raise ContractError(f"artifact {artifact_id} is {entry['state']}: {entry['reason']}")
    manifest = entry["_manifest"]
    manifest_digest = entry["_manifest_digest"]
    destination = artifact_destination(cache_root, manifest)
    staging = cache_root / ".staging" / artifact_id / manifest_digest
    lock_path = cache_root / ".locks" / f"{artifact_id}-{manifest_digest}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if destination.exists():
            verify_tree(destination, manifest)
        else:
            payload = staging / "payload"
            payload.mkdir(parents=True, exist_ok=True)

            def stage_source(source: Mapping[str, Any]) -> None:
                final_path = payload / source["path"]
                if final_path.exists():
                    if final_path.stat().st_size == source["bytes"] and sha256_file(final_path) == source["sha256"]:
                        return
                    final_path.unlink()
                partial = staging / "downloads" / f"{source['path']}.part"
                _download(source, partial)
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(partial, final_path)

            sources = entry["sources"]
            workers = min(download_concurrency, len(sources))
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix=f"artifact-{artifact_id[:24]}",
            ) as executor:
                # Resolve in catalog order so the reported first failure is deterministic.
                futures = [executor.submit(stage_source, source) for source in sources]
                for future in futures:
                    future.result()
            verify_tree(payload, manifest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(payload, destination)
            except OSError:
                if not destination.exists():
                    raise
                verify_tree(destination, manifest)
            for path in sorted(destination.rglob("*"), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            destination.chmod(0o555)
        checks = offline_smoke(destination, entry["offline_smoke"])
        receipt: dict[str, Any] = {
            "schema": RECEIPT_SCHEMA,
            "artifact_id": artifact_id,
            "family": entry["family"],
            "state": "verified",
            "catalog_digest": sha256_bytes(canonical_json(load_json(catalog_path))),
            "manifest_digest": manifest_digest,
            "content_digest": manifest["content"]["digest"],
            "source": manifest["source"],
            "license": manifest["license"],
            "files": manifest["content"]["files"],
            "expanded_bytes": manifest["content"]["expanded_bytes"],
            "cache_uri": (
                f"nebius-sharedfs://{storage['filesystem_id']}/"
                f"{storage['cache_subpath'].strip('/')}/"
                f"{destination.relative_to(cache_root).as_posix()}"
            ),
            "storage": storage,
            "consumer_bindings": artifact_consumer_bindings(catalog, artifact_id),
            "runtime_handoffs": artifact_runtime_handoffs(catalog, artifact_id),
            "offline_checks": checks,
            "started_at": started_at,
            "verified_at": utc_now(),
        }
        if "provenance" in entry:
            receipt["provenance"] = entry["provenance"]
        receipt["receipt_digest"] = sha256_bytes(canonical_json(receipt))
        receipt_path = cache_root / "receipts" / artifact_id / f"{manifest_digest}.json"
        atomic_json(receipt_path, receipt)
        return receipt


def validate_scientific_localization_receipt(
    receipt: Any,
    *,
    artifact_id: str,
    localization: Mapping[str, Any],
) -> tuple[bool, str | None]:
    """Check the canonical primary-localizer receipt without trusting its path."""
    if not isinstance(receipt, Mapping):
        return False, None
    archive = receipt.get("archive_provenance")
    tree = receipt.get("tree_identity")
    if not isinstance(archive, Mapping) or not isinstance(tree, Mapping):
        return False, None
    expected_tree = localization["tree"]
    valid = (
        receipt.get("schema") == localization["receipt_schema"]
        and receipt.get("artifact_id") == artifact_id
        and receipt.get("state") == "verified"
        and receipt.get("mount_path") in localization["mount_paths"]
        and receipt.get("rejection_reason") is None
        and archive.get("sha256") == localization["archive_sha256"]
        and archive.get("present_in_mount") is False
        and tree.get("entry_count") == expected_tree["entry_count"]
        and tree.get("total_bytes") == expected_tree["total_bytes"]
        and tree.get("inventory_algorithm") == expected_tree["inventory_algorithm"]
        and tree.get("inventory_sha256") == expected_tree["inventory_sha256"]
    )
    return valid, sha256_bytes(canonical_json(receipt)) if valid else None


def readiness(catalog_path: Path, cache_root: Path) -> dict[str, Any]:
    catalog = validate_catalog(load_json(catalog_path), catalog_path)
    consumers: dict[str, Any] = {}
    for consumer_id, required_ids in catalog["consumers"].items():
        artifacts: list[dict[str, Any]] = []
        ready = True
        for artifact_id in required_ids:
            entry = catalog["artifacts"][artifact_id]
            item: dict[str, Any] = {"artifact_id": artifact_id, "catalog_state": entry["state"]}
            if entry["state"] != "available":
                item.update({"ready": False, "reason": entry["reason"]})
                ready = False
            else:
                receipt_path = cache_root / "receipts" / artifact_id / f"{entry['_manifest_digest']}.json"
                if not receipt_path.is_file():
                    item.update({"ready": False, "reason": "verified immutable cache receipt is absent"})
                    ready = False
                else:
                    receipt = load_json(receipt_path)
                    unsigned = dict(receipt)
                    digest = unsigned.pop("receipt_digest", None)
                    valid = (
                        receipt.get("schema") == RECEIPT_SCHEMA
                        and receipt.get("manifest_digest") == entry["_manifest_digest"]
                        and receipt.get("provenance") == entry.get("provenance")
                        and receipt.get("consumer_bindings")
                        == artifact_consumer_bindings(catalog, artifact_id)
                        and receipt.get("runtime_handoffs")
                        == artifact_runtime_handoffs(catalog, artifact_id)
                        and digest == sha256_bytes(canonical_json(unsigned))
                    )
                    item.update(
                        {
                            "ready": valid,
                            "reason": None if valid else "cache receipt failed provenance validation",
                            "receipt_digest": digest,
                            "cache_uri": receipt.get("cache_uri"),
                        }
                    )
                    if valid and "localization" in entry:
                        localization = entry["localization"]
                        localization_path = (
                            cache_root
                            / "localization-receipts"
                            / artifact_id
                            / f"{localization['tree']['inventory_sha256']}.json"
                        )
                        if not localization_path.is_file():
                            valid = False
                            item.update(
                                {
                                    "ready": False,
                                    "reason": "verified runtime-tree localization receipt is absent",
                                }
                            )
                        else:
                            localized_valid, localized_digest = (
                                validate_scientific_localization_receipt(
                                    load_json(localization_path),
                                    artifact_id=artifact_id,
                                    localization=localization,
                                )
                            )
                            valid = localized_valid
                            item.update(
                                {
                                    "ready": localized_valid,
                                    "reason": (
                                        None
                                        if localized_valid
                                        else "runtime-tree localization receipt failed identity validation"
                                    ),
                                    "localization_receipt_digest": localized_digest,
                                }
                            )
                    if valid and artifact_id == "protenix-v2":
                        localization = catalog["runtime_handoffs"]["protenix-v2"]["localization"]
                        localization_path = (
                            cache_root
                            / "localization-receipts"
                            / artifact_id
                            / f"{localization['content_digest_sha256']}.json"
                        )
                        if not localization_path.is_file():
                            valid = False
                            item.update(
                                {
                                    "ready": False,
                                    "reason": "verified composite localization receipt is absent",
                                }
                            )
                        else:
                            localized = load_json(localization_path)
                            localized_unsigned = dict(localized)
                            localized_digest = localized_unsigned.pop("receipt_digest", None)
                            localized_valid = (
                                localized.get("schema")
                                == "fs2-serve.nebius.ai/public-artifact-localization-receipt/v1"
                                and localized.get("artifact_id") == artifact_id
                                and localized.get("source_receipt_digest") == digest
                                and localized.get("source_content_digest")
                                == localization["source_content_digest_sha256"]
                                and localized.get("content_digest")
                                == localization["content_digest_sha256"]
                                and localized.get("manifest_sha256")
                                == localization["manifest_sha256"]
                                and localized.get("required_files")
                                == localization["required_files"]
                                and localized_digest
                                == sha256_bytes(canonical_json(localized_unsigned))
                            )
                            destination = localized.get("destination")
                            if localized_valid and isinstance(destination, str):
                                try:
                                    verify_protenix_localization(
                                        Path(destination), entry["_manifest"], localization
                                    )
                                except ContractError:
                                    localized_valid = False
                            else:
                                localized_valid = False
                            valid = localized_valid
                            item.update(
                                {
                                    "ready": localized_valid,
                                    "reason": (
                                        None
                                        if localized_valid
                                        else "composite localization receipt or tree failed validation"
                                    ),
                                    "localization_receipt_digest": localized_digest,
                                }
                            )
                    ready = ready and valid
            artifacts.append(item)
        consumer_readiness: dict[str, Any] = {"ready": ready, "artifacts": artifacts}
        for catalog_field, readiness_field in (
            ("runtime_constraints", "runtime_constraint"),
            ("runtime_handoffs", "runtime_handoff"),
            ("private_layouts", "private_layout"),
            ("reference_layouts", "reference_layout"),
        ):
            if consumer_id in catalog[catalog_field]:
                consumer_readiness[readiness_field] = catalog[catalog_field][consumer_id]
        consumers[consumer_id] = consumer_readiness
    result: dict[str, Any] = {
        "schema": READINESS_SCHEMA,
        "catalog_digest": sha256_bytes(canonical_json(load_json(catalog_path))),
        "generated_at": utc_now(),
        "consumers": consumers,
    }
    result["readiness_digest"] = sha256_bytes(canonical_json(result))
    return result


def _metadata(args: argparse.Namespace) -> dict[str, Any]:
    values = {
        "project_id": args.project_id,
        "region": args.region,
        "cluster": args.cluster,
        "filesystem_id": args.filesystem_id,
        "namespace": args.namespace,
        "local_queue": args.local_queue,
        "cpu_pool_id": args.cpu_pool_id,
        "cpu_pool_name": args.cpu_pool_name,
        "cpu_pool_label": args.cpu_pool_label,
        "shared_filesystem_host_path": args.shared_filesystem_host_path,
        "cache_subpath": args.cache_subpath,
        "reference_plane_source_commit": args.reference_plane_source_commit,
        "source_commit": args.source_commit,
    }
    values["filesystem_size_gib"] = args.filesystem_size_gib
    return validate_storage_metadata(values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--catalog", type=Path, required=True)
    stage = subparsers.add_parser("stage")
    stage.add_argument("--catalog", type=Path, required=True)
    stage.add_argument("--artifact", required=True)
    stage.add_argument("--cache-root", type=Path, required=True)
    for name in (
        "project-id", "region", "cluster", "filesystem-id", "namespace",
        "local-queue", "cpu-pool-id", "cpu-pool-name", "cpu-pool-label",
        "shared-filesystem-host-path", "cache-subpath",
        "reference-plane-source-commit", "source-commit",
    ):
        stage.add_argument(f"--{name}", required=True)
    stage.add_argument("--filesystem-size-gib", type=int, required=True)
    stage.add_argument(
        "--download-concurrency",
        type=int,
        default=DEFAULT_DOWNLOAD_CONCURRENCY,
        help=(
            "number of source objects downloaded concurrently "
            f"(1-{MAX_DOWNLOAD_CONCURRENCY}; default: {DEFAULT_DOWNLOAD_CONCURRENCY})"
        ),
    )
    verify = subparsers.add_parser("verify")
    verify.add_argument("--catalog", type=Path, required=True)
    verify.add_argument("--artifact", required=True)
    verify.add_argument("--cache-root", type=Path, required=True)
    materialize = subparsers.add_parser("materialize")
    materialize.add_argument("--catalog", type=Path, required=True)
    materialize.add_argument("--artifact", required=True)
    materialize.add_argument("--cache-root", type=Path, required=True)
    materialize.add_argument("--destination", type=Path, required=True)
    ready = subparsers.add_parser("readiness")
    ready.add_argument("--catalog", type=Path, required=True)
    ready.add_argument("--cache-root", type=Path, required=True)
    ready.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            catalog = validate_catalog(load_json(args.catalog), args.catalog)
            result: Any = {
                "valid": True,
                "catalog_digest": sha256_bytes(canonical_json(load_json(args.catalog))),
                "available": sum(entry["state"] == "available" for entry in catalog["artifacts"].values()),
            }
        elif args.command == "stage":
            result = stage_artifact(
                args.catalog,
                args.artifact,
                args.cache_root,
                _metadata(args),
                download_concurrency=args.download_concurrency,
            )
        elif args.command == "verify":
            catalog = validate_catalog(load_json(args.catalog), args.catalog)
            entry = catalog["artifacts"].get(args.artifact)
            if entry is None or entry["state"] != "available":
                raise ContractError("verify requires an available catalog artifact")
            root = artifact_destination(args.cache_root, entry["_manifest"])
            verify_tree(root, entry["_manifest"])
            result = {"valid": True, "artifact_id": args.artifact, "content_digest": entry["_manifest"]["content"]["digest"]}
        elif args.command == "materialize":
            result = materialize_artifact(
                args.catalog, args.artifact, args.cache_root, args.destination
            )
        else:
            result = readiness(args.catalog, args.cache_root)
            if args.output:
                atomic_json(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ContractError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
