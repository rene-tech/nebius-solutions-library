#!/usr/bin/env python3
"""Stage and verify immutable scientific reference data and private MSA jobs.

The module intentionally uses only the Python standard library. Large source
objects are downloaded resumably, verified against the catalog transport
identity, hashed with SHA-256, materialized in a temporary directory, and
atomically promoted only after every expanded file has been hashed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, BinaryIO, Mapping, Sequence
from urllib import error, parse, request
import zipfile


CATALOG_SCHEMA = "fs2-serve.nebius.ai/reference-data-source-catalog/v1"
MANIFEST_SCHEMA = "fs2-serve.nebius.ai/reference-data-manifest/v1"
ACCESS_SCHEMA = "fs2-serve.nebius.ai/reference-data-access-receipt/v1"
REQUEST_SCHEMA = "fs2-serve.nebius.ai/private-preprocess-request/v1"
RESULT_SCHEMA = "fs2-serve.nebius.ai/private-preprocess-result/v1"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[a-f0-9]{64}$")
REQUEST_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
CPU_RE = re.compile(r"^[1-9][0-9]*$")
MEMORY_RE = re.compile(r"^[1-9][0-9]*(?:Mi|Gi)$")
BUFFER_BYTES = 4 * 1024 * 1024


class ContractError(RuntimeError):
    """A fail-closed contract or integrity violation."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read valid JSON from {path}") from exc


def atomic_json(path: Path, value: Any, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True).encode("utf-8"))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, value: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _expect_keys(value: Mapping[str, Any], required: set[str], allowed: set[str], context: str) -> None:
    missing = required - set(value)
    unknown = set(value) - allowed
    if missing:
        raise ContractError(f"{context} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ContractError(f"{context} has unknown fields: {', '.join(sorted(unknown))}")


def _expect_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{context} must be an object")
    return value


def _expect_datetime(value: Any, context: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ContractError(f"{context} must be an RFC 3339 UTC timestamp")
    try:
        dt.datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{context} must be an RFC 3339 UTC timestamp") from exc


def _safe_relative(value: str, context: str, *, allow_dot: bool = False) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or (path == Path(".") and not allow_dot):
        raise ContractError(f"{context} must be a safe relative path")
    return path


def validate_catalog(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ContractError("catalog must be an object")
    _expect_keys(document, {"schema", "generated_at", "bundles"}, {"schema", "generated_at", "bundles"}, "catalog")
    if document["schema"] != CATALOG_SCHEMA:
        raise ContractError(f"catalog schema must be {CATALOG_SCHEMA}")
    _expect_datetime(document["generated_at"], "catalog generated_at")
    bundles = document["bundles"]
    if not isinstance(bundles, dict) or not bundles:
        raise ContractError("catalog bundles must be a non-empty object")
    for bundle_id, bundle in bundles.items():
        if not isinstance(bundle, dict) or bundle.get("id") != bundle_id or not ID_RE.fullmatch(bundle_id):
            raise ContractError(f"catalog bundle key/id is invalid: {bundle_id!r}")
        required = {"id", "revision", "description", "upstream", "access", "sizing", "update_policy", "objects"}
        _expect_keys(bundle, required, required, f"bundle {bundle_id}")
        if not isinstance(bundle["revision"], str) or not 1 <= len(bundle["revision"]) <= 160:
            raise ContractError(f"bundle {bundle_id} revision is invalid")
        if not isinstance(bundle["description"], str) or not bundle["description"]:
            raise ContractError(f"bundle {bundle_id} description is invalid")
        upstream = _expect_object(bundle["upstream"], f"bundle {bundle_id} upstream")
        upstream_fields = {"project", "revision", "source_url", "source_sha256"}
        _expect_keys(upstream, upstream_fields, upstream_fields, f"bundle {bundle_id} upstream")
        if not re.fullmatch(r"[a-f0-9]{40}", str(upstream.get("revision", ""))):
            raise ContractError(f"bundle {bundle_id} must pin a 40-character upstream revision")
        if not SHA256_RE.fullmatch(str(upstream.get("source_sha256", ""))):
            raise ContractError(f"bundle {bundle_id} must pin the upstream source file SHA-256")
        if not isinstance(upstream["project"], str) or not upstream["project"]:
            raise ContractError(f"bundle {bundle_id} upstream project is invalid")
        if parse.urlparse(str(upstream["source_url"])).scheme != "https":
            raise ContractError(f"bundle {bundle_id} upstream source URL must use HTTPS")
        access = _expect_object(bundle["access"], f"bundle {bundle_id} access")
        access_fields = {"state", "redistribution", "staging_policy", "terms"}
        _expect_keys(access, access_fields, access_fields, f"bundle {bundle_id} access")
        if access["state"] not in {"public", "academic-gated", "commercial-gated", "unresolved"}:
            raise ContractError(f"bundle {bundle_id} access state is invalid")
        if access["redistribution"] not in {
            "allowed-with-attribution", "component-terms", "prohibited", "review-required"
        }:
            raise ContractError(f"bundle {bundle_id} redistribution state is invalid")
        if access.get("staging_policy") not in {
            "automatic-public", "terms-receipt-required", "entitlement-receipt-required"
        }:
            raise ContractError(f"bundle {bundle_id} has an invalid staging policy")
        if not isinstance(access.get("terms"), list) or not access["terms"]:
            raise ContractError(f"bundle {bundle_id} must record source terms")
        for index, term_value in enumerate(access["terms"]):
            term = _expect_object(term_value, f"bundle {bundle_id} term {index}")
            term_fields = {"component", "license", "url", "verification"}
            _expect_keys(term, term_fields, term_fields, f"bundle {bundle_id} term {index}")
            if not all(isinstance(term[field], str) and term[field] for field in ("component", "license")):
                raise ContractError(f"bundle {bundle_id} term {index} text is invalid")
            if parse.urlparse(str(term["url"])).scheme != "https":
                raise ContractError(f"bundle {bundle_id} term {index} URL must use HTTPS")
            if term["verification"] not in {"primary-source-verified", "upstream-terms-review-required"}:
                raise ContractError(f"bundle {bundle_id} term {index} verification is invalid")
        sizing = _expect_object(bundle["sizing"], f"bundle {bundle_id} sizing")
        sizing_fields = {"compressed_bytes", "expanded_bytes", "expanded_bytes_kind"}
        _expect_keys(sizing, sizing_fields, sizing_fields, f"bundle {bundle_id} sizing")
        if (
            not isinstance(sizing["compressed_bytes"], int)
            or isinstance(sizing["compressed_bytes"], bool)
            or sizing["compressed_bytes"] < 1
        ):
            raise ContractError(f"bundle {bundle_id} compressed size is invalid")
        if sizing["expanded_bytes"] is not None and (
            not isinstance(sizing["expanded_bytes"], int)
            or isinstance(sizing["expanded_bytes"], bool)
            or sizing["expanded_bytes"] < 1
        ):
            raise ContractError(f"bundle {bundle_id} expanded size is invalid")
        if sizing["expanded_bytes_kind"] not in {"exact", "upstream-estimate", "not-published"}:
            raise ContractError(f"bundle {bundle_id} expanded size kind is invalid")
        if (sizing["expanded_bytes"] is None) != (sizing["expanded_bytes_kind"] == "not-published"):
            raise ContractError(f"bundle {bundle_id} expanded size and kind are inconsistent")
        objects = bundle["objects"]
        if not isinstance(objects, list) or not objects:
            raise ContractError(f"bundle {bundle_id} must contain source objects")
        ids: set[str] = set()
        total = 0
        for item in objects:
            if not isinstance(item, dict):
                raise ContractError(f"bundle {bundle_id} source object must be an object")
            required_object = {
                "id", "source", "target", "transform", "source_bytes", "source_integrity", "license_component"
            }
            _expect_keys(item, required_object, required_object | {"overwrite"}, f"bundle {bundle_id} source object")
            object_id = item["id"]
            if not isinstance(object_id, str) or not ID_RE.fullmatch(object_id) or object_id in ids:
                raise ContractError(f"bundle {bundle_id} has invalid or duplicate object id {object_id!r}")
            ids.add(object_id)
            source = item["source"]
            if not isinstance(source, dict) or set(source) not in ({"url"}, {"url_env"}):
                raise ContractError(f"bundle {bundle_id}/{object_id} source must contain exactly url or url_env")
            if "url" in source and parse.urlparse(str(source["url"])).scheme not in {"https", "file"}:
                raise ContractError(f"bundle {bundle_id}/{object_id} source URL is invalid")
            if "url_env" in source and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", str(source["url_env"])):
                raise ContractError(f"bundle {bundle_id}/{object_id} source URL environment name is invalid")
            _safe_relative(str(item["target"]), f"bundle {bundle_id}/{object_id} target", allow_dot=True)
            if item["transform"] not in {"none", "gzip", "zstd", "tar.gz", "tar.zst", "zip"}:
                raise ContractError(f"bundle {bundle_id}/{object_id} has an unsupported transform")
            if (
                not isinstance(item["source_bytes"], int)
                or isinstance(item["source_bytes"], bool)
                or item["source_bytes"] < 1
            ):
                raise ContractError(f"bundle {bundle_id}/{object_id} has invalid source_bytes")
            total += item["source_bytes"]
            integrity = item["source_integrity"]
            integrity = _expect_object(integrity, f"bundle {bundle_id}/{object_id} integrity")
            integrity_fields = {"algorithm", "digest", "cryptographic"}
            _expect_keys(
                integrity,
                integrity_fields,
                integrity_fields,
                f"bundle {bundle_id}/{object_id} integrity",
            )
            if integrity.get("algorithm") not in {"sha256", "md5", "etag", "s3-multipart-etag"}:
                raise ContractError(f"bundle {bundle_id}/{object_id} has invalid source integrity")
            digest = str(integrity["digest"])
            algorithm = integrity["algorithm"]
            if (
                algorithm == "sha256" and not SHA256_RE.fullmatch(digest)
                or algorithm == "md5" and not re.fullmatch(r"[a-f0-9]{32}", digest)
                or algorithm in {"etag", "s3-multipart-etag"}
                and not re.fullmatch(r"[A-Fa-f0-9-]{8,160}", digest)
            ):
                raise ContractError(f"bundle {bundle_id}/{object_id} has invalid source integrity digest")
            if not isinstance(integrity["cryptographic"], bool):
                raise ContractError(f"bundle {bundle_id}/{object_id} integrity classification is invalid")
            if integrity["cryptographic"] != (algorithm == "sha256"):
                raise ContractError(f"bundle {bundle_id}/{object_id} integrity classification is inconsistent")
            if not isinstance(item["license_component"], str) or not item["license_component"]:
                raise ContractError(f"bundle {bundle_id}/{object_id} license component is invalid")
            if "overwrite" in item and not isinstance(item["overwrite"], bool):
                raise ContractError(f"bundle {bundle_id}/{object_id} overwrite flag is invalid")
        if total != sizing.get("compressed_bytes"):
            raise ContractError(f"bundle {bundle_id} compressed_bytes does not equal its source object sum")
        update = _expect_object(bundle["update_policy"], f"bundle {bundle_id} update policy")
        update_fields = {"cadence", "mutable_aliases_allowed", "promotion"}
        _expect_keys(update, update_fields, update_fields, f"bundle {bundle_id} update policy")
        if not isinstance(update["cadence"], str) or not update["cadence"]:
            raise ContractError(f"bundle {bundle_id} update cadence is invalid")
        if update.get("mutable_aliases_allowed") is not False or update.get("promotion") != "new-revision-after-offline-validation":
            raise ContractError(f"bundle {bundle_id} update policy permits mutable publication")
    return document


def validate_access_receipt(document: Any, bundle: Mapping[str, Any]) -> str:
    if not isinstance(document, dict):
        raise ContractError("access receipt must be an object")
    required = {"schema", "bundle_id", "revision", "terms_sha256", "approved_at", "approved_by", "scope"}
    _expect_keys(document, required, required, "access receipt")
    if document["schema"] != ACCESS_SCHEMA:
        raise ContractError(f"access receipt schema must be {ACCESS_SCHEMA}")
    if document["bundle_id"] != bundle["id"] or document["revision"] != bundle["revision"]:
        raise ContractError("access receipt does not bind the selected bundle revision")
    if not SHA256_RE.fullmatch(str(document["terms_sha256"])):
        raise ContractError("access receipt terms_sha256 is invalid")
    expected_terms = sha256_bytes(canonical_json(bundle["access"]["terms"]))
    if document["terms_sha256"] != expected_terms:
        raise ContractError("access receipt terms_sha256 does not bind the catalog terms")
    if document["scope"] not in {"academic-research", "commercial", "internal-evaluation"}:
        raise ContractError("access receipt scope is invalid")
    if not isinstance(document["approved_by"], str) or not 1 <= len(document["approved_by"]) <= 200:
        raise ContractError("access receipt approved_by is invalid")
    _expect_datetime(document["approved_at"], "access receipt approved_at")
    return sha256_bytes(canonical_json(document))


def _source_url(item: Mapping[str, Any]) -> str:
    source = item["source"]
    if "url" in source:
        value = source["url"]
    else:
        variable = source["url_env"]
        value = os.environ.get(variable, "")
        if not value:
            raise ContractError(f"required gated source URL environment reference {variable} is unset")
    parsed = parse.urlparse(value)
    if parsed.scheme not in {"https", "file"}:
        raise ContractError("source URL must use https or file")
    return value


def _http_identity(url: str) -> tuple[int | None, str | None]:
    try:
        with request.urlopen(request.Request(url, method="HEAD"), timeout=60) as response:  # noqa: S310
            length = response.headers.get("Content-Length")
            etag = response.headers.get("ETag")
            return (int(length) if length is not None else None, etag.strip('"') if etag else None)
    except (error.URLError, ValueError) as exc:
        raise ContractError("source identity probe failed") from exc


def _copy_stream(source: BinaryIO, destination: BinaryIO) -> None:
    while True:
        block = source.read(BUFFER_BYTES)
        if not block:
            return
        destination.write(block)


def _download_file_url(url: str, partial: Path, expected_bytes: int) -> None:
    source = Path(parse.unquote(parse.urlparse(url).path))
    if not source.is_file() or source.is_symlink():
        raise ContractError("file source must be a regular non-symlink file")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > expected_bytes:
        partial.unlink()
        offset = 0
    with source.open("rb") as input_handle, partial.open("ab" if offset else "wb") as output_handle:
        input_handle.seek(offset)
        _copy_stream(input_handle, output_handle)
        output_handle.flush()
        os.fsync(output_handle.fileno())


def _download_https(url: str, partial: Path, expected_bytes: int) -> None:
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > expected_bytes:
        partial.unlink()
        offset = 0
    headers = {"User-Agent": "fs2-reference-data/1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    try:
        with request.urlopen(request.Request(url, headers=headers), timeout=300) as response:  # noqa: S310
            append = offset > 0 and getattr(response, "status", 200) == 206
            with partial.open("ab" if append else "wb") as output_handle:
                _copy_stream(response, output_handle)
                output_handle.flush()
                os.fsync(output_handle.fileno())
    except error.URLError as exc:
        raise ContractError("source download failed; partial file was retained for resume") from exc


def _hash_file(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        while block := handle.read(BUFFER_BYTES):
            digest.update(block)
    return digest.hexdigest()


def download_source(item: Mapping[str, Any], downloads: Path) -> tuple[Path, str]:
    downloads.mkdir(parents=True, exist_ok=True)
    partial = downloads / f"{item['id']}.part"
    url = _source_url(item)
    expected_bytes = item["source_bytes"]
    parsed = parse.urlparse(url)
    integrity = item["source_integrity"]
    if parsed.scheme == "https" and integrity["algorithm"] in {"etag", "s3-multipart-etag"}:
        remote_bytes, remote_etag = _http_identity(url)
        if remote_bytes is not None and remote_bytes != expected_bytes:
            raise ContractError(f"source {item['id']} Content-Length changed")
        if remote_etag != integrity["digest"]:
            raise ContractError(f"source {item['id']} ETag changed")
    if not partial.exists() or partial.stat().st_size != expected_bytes:
        if parsed.scheme == "file":
            _download_file_url(url, partial, expected_bytes)
        else:
            _download_https(url, partial, expected_bytes)
    if partial.stat().st_size != expected_bytes:
        raise ContractError(f"source {item['id']} byte count does not match catalog")
    source_sha256 = _hash_file(partial)
    if integrity["algorithm"] in {"sha256", "md5"}:
        observed = source_sha256 if integrity["algorithm"] == "sha256" else _hash_file(partial, "md5")
        if observed != integrity["digest"].lower():
            raise ContractError(f"source {item['id']} checksum does not match catalog")
    return partial, source_sha256


def _safe_member_path(root: Path, name: str) -> Path:
    relative = _safe_relative(name, "archive member")
    destination = (root / relative).resolve()
    if os.path.commonpath((str(root.resolve()), str(destination))) != str(root.resolve()):
        raise ContractError("archive member escapes the destination")
    return destination


def _extract_tar(archive: Path, target: Path, *, overwrite: bool) -> None:
    with tarfile.open(archive, "r:*") as handle:
        members = handle.getmembers()
        file_destinations: set[Path] = set()
        for member in members:
            if not (member.isfile() or member.isdir()):
                raise ContractError(f"archive contains unsupported entry type: {member.name}")
            destination = _safe_member_path(target, member.name)
            if member.isfile():
                if (destination.exists() or destination in file_destinations) and not overwrite:
                    raise ContractError(f"archive would overwrite an existing path: {member.name}")
                file_destinations.add(destination)
        for member in members:
            destination = _safe_member_path(target, member.name)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_handle = handle.extractfile(member)
            if source_handle is None:
                raise ContractError(f"archive file cannot be read: {member.name}")
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
            temporary = Path(temporary_name)
            try:
                with source_handle, os.fdopen(descriptor, "wb") as output_handle:
                    _copy_stream(source_handle, output_handle)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)


def _decompress_zstd(source: Path, destination: Path) -> None:
    executable = shutil.which("zstd")
    if executable is None:
        raise ContractError("zstd is required to materialize zstd sources")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output_handle:
            result = subprocess.run(
                [executable, "--decompress", "--stdout", str(source)],
                stdout=output_handle,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if result.returncode != 0:
            raise ContractError("zstd decompression failed")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _extract_zip(archive: Path, target: Path, *, overwrite: bool) -> None:
    with zipfile.ZipFile(archive) as handle:
        members = handle.infolist()
        file_destinations: set[Path] = set()
        for member in members:
            destination = _safe_member_path(target, member.filename)
            unix_mode = member.external_attr >> 16
            if unix_mode and (unix_mode & 0o170000) not in {0, 0o040000, 0o100000}:
                raise ContractError(f"zip contains unsupported entry type: {member.filename}")
            if not member.is_dir():
                if (destination.exists() or destination in file_destinations) and not overwrite:
                    raise ContractError(f"zip would overwrite an existing path: {member.filename}")
                file_destinations.add(destination)
        for member in members:
            destination = _safe_member_path(target, member.filename)
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
            temporary = Path(temporary_name)
            try:
                with handle.open(member) as source_handle, os.fdopen(descriptor, "wb") as output_handle:
                    _copy_stream(source_handle, output_handle)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)


def materialize(source: Path, item: Mapping[str, Any], root: Path) -> None:
    relative = _safe_relative(item["target"], f"source {item['id']} target", allow_dot=True)
    target = root if relative == Path(".") else root / relative
    transform = item["transform"]
    overwrite = bool(item.get("overwrite", False))
    if transform == "none":
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            raise ContractError(f"source {item['id']} would overwrite {relative}")
        shutil.copyfile(source, target)
    elif transform == "gzip":
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            raise ContractError(f"source {item['id']} would overwrite {relative}")
        with gzip.open(source, "rb") as input_handle, target.open("wb") as output_handle:
            _copy_stream(input_handle, output_handle)
    elif transform == "zstd":
        if target.exists() and not overwrite:
            raise ContractError(f"source {item['id']} would overwrite {relative}")
        _decompress_zstd(source, target)
    elif transform == "tar.gz":
        target.mkdir(parents=True, exist_ok=True)
        _extract_tar(source, target, overwrite=overwrite)
    elif transform == "tar.zst":
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="fs2-tar-", dir=root.parent) as temporary_directory:
            expanded_tar = Path(temporary_directory) / "archive.tar"
            _decompress_zstd(source, expanded_tar)
            _extract_tar(expanded_tar, target, overwrite=overwrite)
    elif transform == "zip":
        target.mkdir(parents=True, exist_ok=True)
        _extract_zip(source, target, overwrite=overwrite)
    else:  # pragma: no cover - validate_catalog already rejects it
        raise ContractError(f"unsupported transform {transform}")


def tree_inventory(root: Path, *, exclude: frozenset[str] = frozenset()) -> tuple[list[dict[str, Any]], str, int]:
    files: list[dict[str, Any]] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ContractError(f"published tree contains a symlink: {path.relative_to(root)}")
        if not path.is_file() or path.name.startswith(".fs2-"):
            continue
        relative = path.relative_to(root).as_posix()
        if relative in exclude:
            continue
        size = path.stat().st_size
        files.append({"path": relative, "bytes": size, "sha256": _hash_file(path)})
        total += size
    if not files:
        raise ContractError("materialized reference-data tree is empty")
    tree_sha256 = sha256_bytes(canonical_json(files))
    return files, tree_sha256, total


def _readonly_tree(root: Path, *, include_root: bool = True) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    if include_root:
        root.chmod(0o555)


def _remove_tree(root: Path) -> None:
    """Remove a failed read-only staging tree without widening other paths."""
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o700)
        else:
            path.chmod(0o600)
    root.chmod(0o700)
    shutil.rmtree(root)


def _aws_copy(arguments: Sequence[str]) -> None:
    executable = shutil.which("aws")
    if executable is None:
        raise ContractError("aws CLI is required for object-store publication")
    result = subprocess.run(
        [executable, *arguments, "--only-show-errors"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise ContractError("private object-store copy failed")


def _write_stage_telemetry(root: Path, bundle: Mapping[str, Any], manifest_sha256: str, duration: float, expanded: int) -> None:
    labels = f'bundle="{bundle["id"]}",revision="{bundle["revision"]}"'
    metrics = (
        "# HELP fs2_reference_data_ready Immutable reference-data revision readiness.\n"
        "# TYPE fs2_reference_data_ready gauge\n"
        f"fs2_reference_data_ready{{{labels}}} 1\n"
        "# HELP fs2_reference_data_expanded_bytes Expanded bytes in the immutable revision.\n"
        "# TYPE fs2_reference_data_expanded_bytes gauge\n"
        f"fs2_reference_data_expanded_bytes{{{labels}}} {expanded}\n"
        "# HELP fs2_reference_data_stage_duration_seconds Last successful staging duration.\n"
        "# TYPE fs2_reference_data_stage_duration_seconds gauge\n"
        f"fs2_reference_data_stage_duration_seconds{{{labels}}} {duration:.6f}\n"
        f"# fs2_reference_data_manifest_sha256 {manifest_sha256}\n"
    )
    atomic_text(root / "telemetry" / f"{bundle['id']}.prom", metrics)


def stage_bundle(
    catalog_path: Path,
    bundle_id: str,
    root: Path,
    *,
    access_receipt_path: Path | None = None,
    object_store_prefix: str | None = None,
) -> tuple[dict[str, Any], str]:
    started = time.monotonic()
    catalog = validate_catalog(load_json(catalog_path))
    try:
        bundle = catalog["bundles"][bundle_id]
    except KeyError as exc:
        raise ContractError(f"unknown bundle id {bundle_id}") from exc
    policy = bundle["access"]["staging_policy"]
    access_sha256: str | None = None
    if policy != "automatic-public":
        if access_receipt_path is None:
            raise ContractError(f"bundle {bundle_id} requires a non-secret access/terms receipt")
        access_sha256 = validate_access_receipt(load_json(access_receipt_path), bundle)
    elif access_receipt_path is not None:
        access_sha256 = validate_access_receipt(load_json(access_receipt_path), bundle)

    root.mkdir(parents=True, exist_ok=True)
    catalog_sha256 = sha256_bytes(canonical_json(catalog))
    object_prefix = object_store_prefix.rstrip("/") if object_store_prefix else None
    expected_object_manifest_prefix = f"{object_prefix}/manifests/sha256" if object_prefix else None
    lock_path = root / "locks" / f"{bundle_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        status_path = root / "status" / f"{bundle_id}.json"
        if status_path.is_file():
            status = _expect_object(load_json(status_path), "existing publication status")
            if status.get("revision") == bundle["revision"]:
                if status.get("bundle_id") != bundle_id or status.get("ready") is not True:
                    raise ContractError("existing publication status is inconsistent")
                manifest_sha256 = str(status.get("manifest_sha256", ""))
                if not SHA256_RE.fullmatch(manifest_sha256):
                    raise ContractError("existing publication status has an invalid manifest digest")
                manifest_path = root / "manifests" / "sha256" / f"{manifest_sha256}.json"
                manifest = _expect_object(load_json(manifest_path), "existing publication manifest")
                storage = _expect_object(manifest.get("storage"), "existing publication storage")
                if (
                    manifest.get("source_catalog_sha256") != catalog_sha256
                    or manifest.get("access_receipt_sha256") != access_sha256
                    or storage.get("object_manifest_prefix") != expected_object_manifest_prefix
                ):
                    raise ContractError("published revision provenance changed; create a new immutable revision")
                verify_manifest(manifest_path)
                _write_stage_telemetry(
                    root,
                    bundle,
                    manifest_sha256,
                    time.monotonic() - started,
                    manifest["content"]["expanded_bytes"],
                )
                return manifest, manifest_sha256
        downloads = root / "downloads" / bundle_id / bundle["revision"]
        source_objects: list[dict[str, Any]] = []
        blobs: list[tuple[Mapping[str, Any], Path]] = []
        for item in bundle["objects"]:
            partial, source_sha256 = download_source(item, downloads)
            blob = root / "blobs" / "sha256" / source_sha256[:2] / source_sha256
            blob.parent.mkdir(parents=True, exist_ok=True)
            if blob.exists():
                if blob.stat().st_size != item["source_bytes"] or _hash_file(blob) != source_sha256:
                    raise ContractError(f"existing content-addressed blob is corrupt: {item['id']}")
                partial.unlink(missing_ok=True)
            else:
                os.replace(partial, blob)
                blob.chmod(0o444)
            source_objects.append({
                "id": item["id"],
                "source_bytes": item["source_bytes"],
                "source_sha256": source_sha256,
                "target": item["target"],
                "transform": item["transform"],
            })
            blobs.append((item, blob))

        staging_parent = root / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{bundle_id}.", dir=staging_parent))
        try:
            for item, blob in blobs:
                materialize(blob, item, staging)
            files, tree_sha256, expanded_bytes = tree_inventory(staging)
            final = root / "datasets" / bundle_id / bundle["revision"] / "sha256" / tree_sha256
            stable_manifest: dict[str, Any] = {
                "schema": MANIFEST_SCHEMA,
                "bundle_id": bundle_id,
                "revision": bundle["revision"],
                "source_catalog_sha256": catalog_sha256,
                "access_receipt_sha256": access_sha256,
                "source_objects": source_objects,
                "content": {
                    "tree_sha256": tree_sha256,
                    "expanded_bytes": expanded_bytes,
                    "file_count": len(files),
                    "files": files,
                },
                "storage": {
                    "shared_filesystem_uri": final.resolve().as_uri(),
                    "object_manifest_prefix": expected_object_manifest_prefix,
                },
            }
            marker = final / ".fs2-manifest-sha256"
            if final.exists():
                manifest_sha256 = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
                if not SHA256_RE.fullmatch(manifest_sha256):
                    raise ContractError("existing immutable tree has no valid publication marker")
                manifest_path = root / "manifests" / "sha256" / f"{manifest_sha256}.json"
                manifest = load_json(manifest_path)
                if sha256_bytes(canonical_json(manifest)) != manifest_sha256:
                    raise ContractError("existing immutable manifest content is corrupt")
                existing_stable = {key: value for key, value in manifest.items() if key != "created_at"}
                if existing_stable != stable_manifest:
                    raise ContractError("existing immutable tree has a different publication manifest")
                _remove_tree(staging)
                verify_manifest(manifest_path)
            else:
                manifest = {**stable_manifest, "created_at": _utc_now()}
                manifest_sha256 = sha256_bytes(canonical_json(manifest))
                manifest_path = root / "manifests" / "sha256" / f"{manifest_sha256}.json"
                if manifest_path.exists():
                    if sha256_bytes(canonical_json(load_json(manifest_path))) != manifest_sha256:
                        raise ContractError("existing immutable manifest content is corrupt")
                else:
                    atomic_json(manifest_path, manifest)
                    manifest_path.chmod(0o444)
                atomic_text(staging / ".fs2-manifest-sha256", manifest_sha256)
                final.parent.mkdir(parents=True, exist_ok=True)
                # Some shared filesystems refuse to rename a read-only source
                # directory, so make descendants immutable first and close the
                # root directory immediately after the atomic rename.
                _readonly_tree(staging, include_root=False)
                os.replace(staging, final)
                final.chmod(0o555)
            if manifest_path.exists():
                if sha256_bytes(canonical_json(load_json(manifest_path))) != manifest_sha256:
                    raise ContractError("existing immutable manifest content is corrupt")
            else:
                atomic_json(manifest_path, manifest)
                manifest_path.chmod(0o444)
            if object_prefix:
                for _item, blob in blobs:
                    _aws_copy(["s3", "cp", str(blob), f"{object_prefix}/blobs/sha256/{blob.name}"])
                _aws_copy(["s3", "cp", str(manifest_path), f"{object_prefix}/manifests/sha256/{manifest_sha256}.json"])
            status = {
                "schema": "fs2-serve.nebius.ai/reference-data-status/v1",
                "bundle_id": bundle_id,
                "revision": bundle["revision"],
                "ready": True,
                "manifest_sha256": manifest_sha256,
                "tree_sha256": tree_sha256,
                "expanded_bytes": expanded_bytes,
                "file_count": len(files),
                "updated_at": _utc_now(),
            }
            atomic_json(root / "status" / f"{bundle_id}.json", status)
            _write_stage_telemetry(root, bundle, manifest_sha256, time.monotonic() - started, expanded_bytes)
            return manifest, manifest_sha256
        finally:
            if staging.exists():
                _remove_tree(staging)


def verify_manifest(manifest_path: Path) -> tuple[dict[str, Any], str]:
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ContractError("published manifest schema is invalid")
    manifest_sha256 = sha256_bytes(canonical_json(manifest))
    stem = manifest_path.stem
    if SHA256_RE.fullmatch(stem) and stem != manifest_sha256:
        raise ContractError("manifest filename does not equal its canonical SHA-256")
    uri = manifest.get("storage", {}).get("shared_filesystem_uri", "")
    parsed = parse.urlparse(uri)
    if parsed.scheme != "file":
        raise ContractError("offline tree verification requires a file:/// shared-filesystem URI")
    root = Path(parse.unquote(parsed.path))
    expected_files = manifest.get("content", {}).get("files")
    if not isinstance(expected_files, list):
        raise ContractError("manifest file inventory is invalid")
    observed_files, tree_sha256, expanded_bytes = tree_inventory(root)
    if observed_files != expected_files:
        raise ContractError("published tree file inventory or checksums changed")
    if tree_sha256 != manifest["content"]["tree_sha256"] or expanded_bytes != manifest["content"]["expanded_bytes"]:
        raise ContractError("published tree aggregate identity changed")
    marker = root / ".fs2-manifest-sha256"
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != manifest_sha256:
        raise ContractError("published tree readiness marker is missing or mismatched")
    return manifest, manifest_sha256


def validate_preprocess_request(document: Any, *, allow_public_msa: bool = False) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schema") != REQUEST_SCHEMA:
        raise ContractError(f"preprocess request schema must be {REQUEST_SCHEMA}")
    required = {
        "schema", "request_id", "tenant_id", "workload_id", "input",
        "reference_data", "backend", "privacy", "output", "execution",
    }
    _expect_keys(document, required, required, "preprocess request")
    if not REQUEST_ID_RE.fullmatch(str(document["request_id"])):
        raise ContractError("preprocess request_id is invalid")
    for field in ("tenant_id", "workload_id"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(document[field])):
            raise ContractError(f"preprocess {field} is invalid")
    input_object = _expect_object(document["input"], "preprocess input")
    input_fields = {"uri", "sha256", "bytes", "media_type"}
    _expect_keys(input_object, input_fields, input_fields, "preprocess input")
    digest = input_object.get("sha256", "")
    input_uri = str(input_object.get("uri", ""))
    input_path = parse.urlparse(input_uri).path
    if (
        not SHA256_RE.fullmatch(str(digest))
        or not re.search(rf"/(?:sha256/)?{digest}(?:\.[A-Za-z0-9._-]+)?$", input_path)
    ):
        raise ContractError("preprocess input URI must contain its exact SHA-256")
    if (
        not isinstance(input_object.get("bytes"), int)
        or isinstance(input_object["bytes"], bool)
        or input_object["bytes"] < 1
    ):
        raise ContractError("preprocess input byte count is invalid")
    if input_object["media_type"] not in {
        "text/x-fasta", "application/json", "chemical/x-pdb", "chemical/x-mmcif"
    }:
        raise ContractError("preprocess input media type is invalid")
    if parse.urlparse(input_uri).scheme not in {"file", "s3"}:
        raise ContractError("preprocess input URI must use file or s3")
    reference = _expect_object(document["reference_data"], "preprocess reference_data")
    reference_fields = {"bundle_id", "revision", "manifest_uri", "manifest_sha256"}
    _expect_keys(reference, reference_fields, reference_fields, "preprocess reference_data")
    if not ID_RE.fullmatch(str(reference["bundle_id"])) or not isinstance(reference["revision"], str):
        raise ContractError("preprocess reference-data bundle/revision is invalid")
    manifest_digest = reference.get("manifest_sha256", "")
    manifest_uri = str(reference.get("manifest_uri", ""))
    if (
        not SHA256_RE.fullmatch(str(manifest_digest))
        or not parse.urlparse(manifest_uri).path.endswith(f"/{manifest_digest}.json")
    ):
        raise ContractError("reference manifest URI must contain its exact SHA-256")
    if parse.urlparse(manifest_uri).scheme not in {"file", "s3"}:
        raise ContractError("preprocess reference manifest URI must use file or s3")
    privacy = _expect_object(document["privacy"], "preprocess privacy")
    privacy_fields = {"network_mode", "public_msa_opt_in", "log_sequence_content"}
    _expect_keys(privacy, privacy_fields, privacy_fields, "preprocess privacy")
    if privacy["network_mode"] not in {"private-only", "public-opt-in"}:
        raise ContractError("preprocess privacy network mode is invalid")
    if not isinstance(privacy["public_msa_opt_in"], bool):
        raise ContractError("preprocess public MSA opt-in must be a boolean")
    public = privacy.get("public_msa_opt_in") is True or privacy.get("network_mode") == "public-opt-in"
    if privacy.get("log_sequence_content") is not False:
        raise ContractError("customer sequence logging must be disabled")
    if public != (privacy.get("public_msa_opt_in") is True and privacy.get("network_mode") == "public-opt-in"):
        raise ContractError("public MSA opt-in and network mode must agree")
    if public and not allow_public_msa:
        raise ContractError("public MSA service use requires explicit renderer/executor opt-in")
    execution = _expect_object(document["execution"], "preprocess execution")
    execution_fields = {
        "image", "cpu", "memory", "ephemeral_storage",
        "active_deadline_seconds", "backoff_limit",
    }
    _expect_keys(execution, execution_fields, execution_fields, "preprocess execution")
    image = execution.get("image", "")
    if not IMAGE_RE.fullmatch(str(image)):
        raise ContractError("preprocess image must be digest-pinned")
    if not CPU_RE.fullmatch(str(execution["cpu"])):
        raise ContractError("preprocess CPU request is invalid")
    for field in ("memory", "ephemeral_storage"):
        if not MEMORY_RE.fullmatch(str(execution[field])):
            raise ContractError(f"preprocess {field} request is invalid")
    if (
        not isinstance(execution["active_deadline_seconds"], int)
        or isinstance(execution["active_deadline_seconds"], bool)
        or not 60 <= execution["active_deadline_seconds"] <= 604800
        or not isinstance(execution["backoff_limit"], int)
        or isinstance(execution["backoff_limit"], bool)
        or not 0 <= execution["backoff_limit"] <= 10
    ):
        raise ContractError("preprocess deadline or retry limit is invalid")
    backend = _expect_object(document["backend"], "preprocess backend")
    backend_fields = {"kind", "database_root", "output_format", "threads"}
    _expect_keys(backend, backend_fields, backend_fields, "preprocess backend")
    if not isinstance(backend["threads"], int) or not 1 <= backend["threads"] <= 128:
        raise ContractError("preprocess backend thread count is invalid")
    database_root = str(backend.get("database_root", ""))
    if not database_root.startswith("/reference-data/") or ".." in Path(database_root).parts:
        raise ContractError("database_root must be inside the read-only /reference-data mount")
    backend_kind = backend.get("kind")
    expected_formats = {
        "colabfold-search": "a3m",
        "protenix-inputprep": "protenix-json",
        "alphafold3-data": "alphafold3-json",
    }
    if backend_kind not in expected_formats:
        raise ContractError("preprocess backend is unsupported")
    if backend.get("output_format") != expected_formats[backend_kind]:
        raise ContractError("preprocess output format does not match its backend")
    database_parts = Path(database_root).parts
    if (
        len(database_parts) < 6
        or database_parts[-4] != reference["bundle_id"]
        or database_parts[-3] != reference["revision"]
        or database_parts[-2] != "sha256"
        or not SHA256_RE.fullmatch(database_parts[-1])
    ):
        raise ContractError("database_root must identify the requested immutable bundle revision and tree SHA-256")
    output = _expect_object(document["output"], "preprocess output")
    output_fields = {"prefix_uri", "retention_days"}
    _expect_keys(output, output_fields, output_fields, "preprocess output")
    if parse.urlparse(str(output["prefix_uri"])).scheme not in {"file", "s3"}:
        raise ContractError("preprocess output prefix must use file or s3")
    if not isinstance(output["retention_days"], int) or not 1 <= output["retention_days"] <= 3650:
        raise ContractError("preprocess output retention is invalid")
    return document


def _copy_object_to_file(uri: str, destination: Path) -> None:
    parsed = parse.urlparse(uri)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if parsed.scheme == "file":
        source = Path(parse.unquote(parsed.path))
        if not source.is_file() or source.is_symlink():
            raise ContractError("input file object is absent or unsafe")
        shutil.copyfile(source, destination)
    elif parsed.scheme == "s3":
        _aws_copy(["s3", "cp", uri, str(destination)])
    else:
        raise ContractError("immutable object URI must use file or s3")


def _publish_preprocess_output(source: Path, prefix_uri: str, request_sha256: str) -> str:
    parsed = parse.urlparse(prefix_uri)
    if parsed.scheme == "file":
        prefix = Path(parse.unquote(parsed.path))
        destination = prefix / "sha256" / request_sha256
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ContractError("preprocess output already exists; immutable outputs are never overwritten")
        _readonly_tree(source, include_root=False)
        os.replace(source, destination)
        destination.chmod(0o555)
        return destination.resolve().as_uri()
    if parsed.scheme == "s3":
        marker = source / ".fs2-ready"
        result_manifest_sha256 = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
        if not SHA256_RE.fullmatch(result_manifest_sha256):
            raise ContractError("preprocess output has no valid immutable readiness marker")
        destination = (
            f"{prefix_uri.rstrip('/')}/requests/sha256/{request_sha256}"
            f"/results/sha256/{result_manifest_sha256}"
        )
        for path in sorted(source.rglob("*")):
            if path.is_file() and path != marker:
                relative = path.relative_to(source).as_posix()
                _aws_copy(["s3", "cp", str(path), f"{destination}/{relative}"])
        # Publish readiness last. A versioned bucket retains history even if a
        # retry encounters the same content-addressed result.
        _aws_copy(["s3", "cp", str(marker), f"{destination}/.fs2-ready"])
        return destination
    raise ContractError("preprocess output prefix must use file or s3")


def validate_preprocess_result(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ContractError("preprocess result must be an object")
    required = {
        "schema", "request_sha256", "input_sha256", "reference_manifest_sha256",
        "backend", "privacy_mode", "created_at", "duration_seconds", "content",
    }
    _expect_keys(document, required, required, "preprocess result")
    if document["schema"] != RESULT_SCHEMA:
        raise ContractError(f"preprocess result schema must be {RESULT_SCHEMA}")
    for field in ("request_sha256", "input_sha256", "reference_manifest_sha256"):
        if not SHA256_RE.fullmatch(str(document[field])):
            raise ContractError(f"preprocess result {field} is invalid")
    if document["backend"] not in {"colabfold-search", "protenix-inputprep", "alphafold3-data"}:
        raise ContractError("preprocess result backend is invalid")
    if document["privacy_mode"] not in {"private-only", "public-opt-in"}:
        raise ContractError("preprocess result privacy mode is invalid")
    if not isinstance(document["duration_seconds"], (int, float)) or document["duration_seconds"] < 0:
        raise ContractError("preprocess result duration is invalid")
    content = document["content"]
    if not isinstance(content, dict) or not SHA256_RE.fullmatch(str(content.get("tree_sha256", ""))):
        raise ContractError("preprocess result content identity is invalid")
    if not isinstance(content.get("files"), list) or not content["files"]:
        raise ContractError("preprocess result content inventory is empty")
    if content.get("file_count") != len(content["files"]):
        raise ContractError("preprocess result file count does not match its inventory")
    return document


def _cached_preprocess_result(prefix_uri: str, request_sha256: str) -> dict[str, Any] | None:
    parsed = parse.urlparse(prefix_uri)
    if parsed.scheme != "file":
        return None
    destination = Path(parse.unquote(parsed.path)) / "sha256" / request_sha256
    if not destination.exists():
        return None
    if not destination.is_dir() or destination.is_symlink():
        raise ContractError("preprocess cache path is not a safe immutable directory")
    result_path = destination / "result-manifest.json"
    result_manifest = validate_preprocess_result(load_json(result_path))
    if result_manifest.get("request_sha256") != request_sha256:
        raise ContractError("preprocess cache does not bind the request SHA-256")
    result_sha256 = sha256_bytes(canonical_json(result_manifest))
    marker = destination / ".fs2-ready"
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != result_sha256:
        raise ContractError("preprocess cache readiness marker is missing or corrupt")
    files, tree_sha256, expanded_bytes = tree_inventory(
        destination,
        exclude=frozenset({"result-manifest.json"}),
    )
    content = result_manifest.get("content", {})
    if (
        files != content.get("files")
        or tree_sha256 != content.get("tree_sha256")
        or expanded_bytes != content.get("expanded_bytes")
    ):
        raise ContractError("preprocess cache content failed checksum validation")
    return {
        "request_sha256": request_sha256,
        "result_manifest_sha256": result_sha256,
        "output_uri": destination.resolve().as_uri(),
        "output_bytes": content["expanded_bytes"],
        "output_file_count": content["file_count"],
        "cache_hit": True,
    }


def _write_preprocess_observation(
    telemetry_root: Path | None,
    document: Mapping[str, Any],
    request_sha256: str,
    *,
    outcome: str,
    cache_hit: bool,
    duration_seconds: float,
    error_code: str | None = None,
    result: Mapping[str, Any] | None = None,
) -> None:
    if telemetry_root is None:
        return
    observation = {
        "schema": "fs2-serve.nebius.ai/private-preprocess-observation/v1",
        "request_id": document["request_id"],
        "request_sha256": request_sha256,
        "tenant_id": document["tenant_id"],
        "workload_id": document["workload_id"],
        "backend": document["backend"]["kind"],
        "privacy_mode": document["privacy"]["network_mode"],
        "outcome": outcome,
        "cache_hit": cache_hit,
        "duration_seconds": round(duration_seconds, 6),
        "error_code": error_code,
        "output_bytes": result.get("output_bytes") if result else None,
        "output_file_count": result.get("output_file_count") if result else None,
        "retention_days": document["output"]["retention_days"],
        "observed_at": _utc_now(),
    }
    atomic_json(telemetry_root / "preprocessing" / "status" / f"{request_sha256}.json", observation)


def _execute_preprocess(
    document: Mapping[str, Any],
    request_sha256: str,
    *,
    allow_public_msa: bool,
    started: float,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="fs2-private-msa-") as temporary_directory:
        work = Path(temporary_directory)
        input_path = work / "input"
        _copy_object_to_file(document["input"]["uri"], input_path)
        if (
            input_path.stat().st_size != document["input"]["bytes"]
            or _hash_file(input_path) != document["input"]["sha256"]
        ):
            raise ContractError("preprocess input object failed immutable checksum validation")
        manifest_path = work / "reference-manifest.json"
        _copy_object_to_file(document["reference_data"]["manifest_uri"], manifest_path)
        reference_manifest = load_json(manifest_path)
        if sha256_bytes(canonical_json(reference_manifest)) != document["reference_data"]["manifest_sha256"]:
            raise ContractError("reference-data manifest failed immutable checksum validation")
        if (
            reference_manifest.get("bundle_id") != document["reference_data"]["bundle_id"]
            or reference_manifest.get("revision") != document["reference_data"]["revision"]
        ):
            raise ContractError("reference-data manifest does not bind the requested bundle revision")
        database_root = Path(document["backend"]["database_root"])
        if database_root.name != reference_manifest.get("content", {}).get("tree_sha256"):
            raise ContractError("database_root tree SHA-256 does not match the reference-data manifest")
        marker = database_root / ".fs2-manifest-sha256"
        if (
            not marker.is_file()
            or marker.read_text(encoding="utf-8").strip()
            != document["reference_data"]["manifest_sha256"]
        ):
            raise ContractError("mounted reference-data tree is not ready for the requested manifest")
        output = work / "output"
        output.mkdir()
        backend = document["backend"]
        environment = dict(os.environ)
        environment["FS2_PUBLIC_MSA_OPT_IN"] = "true" if allow_public_msa else "false"
        if backend["kind"] == "colabfold-search":
            command = [
                "colabfold_search",
                str(input_path),
                str(database_root),
                str(output),
                "--threads",
                str(backend["threads"]),
            ]
        elif backend["kind"] == "protenix-inputprep":
            environment["PROTENIX_ROOT_DIR"] = str(database_root)
            command = ["protenix", "prep", "--input", str(input_path), "--out_dir", str(output)]
        else:
            command = [
                "python", "/opt/alphafold3/run_alphafold.py", f"--json_path={input_path}",
                f"--db_dir={database_root}", f"--output_dir={output}", "--norun_inference",
            ]
        if shutil.which(command[0]) is None:
            raise ContractError(f"preprocess image does not contain required executable {command[0]}")
        result = subprocess.run(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode != 0:
            raise ContractError(f"private preprocessing backend failed with exit code {result.returncode}")
        files, tree_sha256, expanded_bytes = tree_inventory(output)
        result_manifest = {
            "schema": RESULT_SCHEMA,
            "request_sha256": request_sha256,
            "input_sha256": document["input"]["sha256"],
            "reference_manifest_sha256": document["reference_data"]["manifest_sha256"],
            "backend": backend["kind"],
            "privacy_mode": document["privacy"]["network_mode"],
            "created_at": _utc_now(),
            "duration_seconds": round(time.monotonic() - started, 6),
            "content": {"tree_sha256": tree_sha256, "expanded_bytes": expanded_bytes, "file_count": len(files), "files": files},
        }
        validate_preprocess_result(result_manifest)
        atomic_json(output / "result-manifest.json", result_manifest)
        atomic_text(output / ".fs2-ready", sha256_bytes(canonical_json(result_manifest)) + "\n")
        published_uri = _publish_preprocess_output(output, document["output"]["prefix_uri"], request_sha256)
        return {
            "request_sha256": request_sha256,
            "result_manifest_sha256": sha256_bytes(canonical_json(result_manifest)),
            "output_uri": published_uri,
            "output_bytes": expanded_bytes,
            "output_file_count": len(files),
            "cache_hit": False,
        }


def run_preprocess(
    request_path: Path,
    *,
    allow_public_msa: bool = False,
    telemetry_root: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    document = validate_preprocess_request(load_json(request_path), allow_public_msa=allow_public_msa)
    request_sha256 = sha256_bytes(canonical_json(document))
    try:
        cached = _cached_preprocess_result(document["output"]["prefix_uri"], request_sha256)
        if cached is not None:
            _write_preprocess_observation(
                telemetry_root,
                document,
                request_sha256,
                outcome="success",
                cache_hit=True,
                duration_seconds=time.monotonic() - started,
                result=cached,
            )
            return cached
        result = _execute_preprocess(
            document,
            request_sha256,
            allow_public_msa=allow_public_msa,
            started=started,
        )
        _write_preprocess_observation(
            telemetry_root,
            document,
            request_sha256,
            outcome="success",
            cache_hit=False,
            duration_seconds=time.monotonic() - started,
            result=result,
        )
        return result
    except Exception as exc:
        _write_preprocess_observation(
            telemetry_root,
            document,
            request_sha256,
            outcome="error",
            cache_hit=False,
            duration_seconds=time.monotonic() - started,
            error_code=type(exc).__name__,
        )
        raise


def _preprocess_metrics(root: Path) -> bytes:
    observations = [
        load_json(path)
        for path in sorted((root / "telemetry" / "preprocessing" / "status").glob("*.json"))
    ]
    aggregates: dict[tuple[str, str, str], dict[str, float]] = {}
    for observation in observations:
        key = (
            str(observation.get("backend", "unknown")),
            str(observation.get("privacy_mode", "unknown")),
            str(observation.get("outcome", "unknown")),
        )
        aggregate = aggregates.setdefault(
            key,
            {"count": 0, "duration": 0, "cache_hits": 0, "output_bytes": 0},
        )
        aggregate["count"] += 1
        aggregate["duration"] += float(observation.get("duration_seconds", 0))
        aggregate["cache_hits"] += int(observation.get("cache_hit") is True)
        aggregate["output_bytes"] += int(observation.get("output_bytes") or 0)
    lines = [
        "# HELP fs2_reference_preprocess_observations Current retained preprocessing observations.",
        "# TYPE fs2_reference_preprocess_observations gauge",
        "# HELP fs2_reference_preprocess_duration_seconds_sum Duration sum across retained preprocessing observations.",
        "# TYPE fs2_reference_preprocess_duration_seconds_sum gauge",
        "# HELP fs2_reference_preprocess_cache_hits Current retained preprocessing cache-hit observations.",
        "# TYPE fs2_reference_preprocess_cache_hits gauge",
        "# HELP fs2_reference_preprocess_output_bytes Output bytes across retained preprocessing observations.",
        "# TYPE fs2_reference_preprocess_output_bytes gauge",
    ]
    for (backend, privacy, outcome), aggregate in sorted(aggregates.items()):
        labels = f'backend="{backend}",privacy="{privacy}",outcome="{outcome}"'
        lines.append(f"fs2_reference_preprocess_observations{{{labels}}} {aggregate['count']}")
        lines.append(f"fs2_reference_preprocess_duration_seconds_sum{{{labels}}} {aggregate['duration']:.6f}")
        lines.append(f"fs2_reference_preprocess_cache_hits{{{labels}}} {aggregate['cache_hits']}")
        lines.append(f"fs2_reference_preprocess_output_bytes{{{labels}}} {aggregate['output_bytes']}")
    return ("\n".join(lines) + "\n").encode()


class StatusHandler(http.server.BaseHTTPRequestHandler):
    root: Path

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if self.path == "/readyz":
            ready = any((self.root / "status").glob("*.json"))
            self._send(200 if ready else 503, b"ready\n" if ready else b"not ready\n", "text/plain; charset=utf-8")
            return
        if self.path == "/metrics":
            metrics = b"".join(path.read_bytes() for path in sorted((self.root / "telemetry").glob("*.prom")))
            metrics += _preprocess_metrics(self.root)
            self._send(200, metrics, "text/plain; version=0.0.4; charset=utf-8")
            return
        if self.path == "/v1/status":
            statuses = [load_json(path) for path in sorted((self.root / "status").glob("*.json"))]
            body = {
                "schema": "fs2-serve.nebius.ai/reference-data-status-list/v1",
                "items": statuses,
            }
            self._send(
                200,
                json.dumps(body, sort_keys=True).encode() + b"\n",
                "application/json",
            )
            return
        if self.path == "/v1/preprocessing":
            observations = [
                load_json(path)
                for path in sorted((self.root / "telemetry" / "preprocessing" / "status").glob("*.json"))
            ]
            body = {
                "schema": "fs2-serve.nebius.ai/private-preprocess-observation-list/v1",
                "items": observations,
            }
            self._send(200, json.dumps(body, sort_keys=True).encode() + b"\n", "application/json")
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def serve_status(root: Path, host: str, port: int) -> None:
    handler = type("BoundStatusHandler", (StatusHandler,), {"root": root})
    server = http.server.ThreadingHTTPServer((host, port), handler)
    server.serve_forever()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-catalog")
    validate.add_argument("--catalog", type=Path, required=True)
    stage = subparsers.add_parser("stage")
    stage.add_argument("--catalog", type=Path, required=True)
    stage.add_argument("--bundle", required=True)
    stage.add_argument("--root", type=Path, required=True)
    stage.add_argument("--access-receipt", type=Path)
    stage.add_argument("--object-store-prefix")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    validate_request = subparsers.add_parser("validate-request")
    validate_request.add_argument("--request", type=Path, required=True)
    validate_request.add_argument("--allow-public-msa", action="store_true")
    preprocess = subparsers.add_parser("preprocess")
    preprocess.add_argument("--request", type=Path, required=True)
    preprocess.add_argument("--allow-public-msa", action="store_true")
    preprocess.add_argument("--telemetry-root", type=Path)
    serve = subparsers.add_parser("serve-status")
    serve.add_argument("--root", type=Path, required=True)
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    return result


def main(arguments: Sequence[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    try:
        if args.command == "validate-catalog":
            catalog = validate_catalog(load_json(args.catalog))
            print(json.dumps({
                "valid": True,
                "catalog_sha256": sha256_bytes(canonical_json(catalog)),
                "bundle_count": len(catalog["bundles"]),
            }, sort_keys=True))
        elif args.command == "stage":
            _manifest, digest = stage_bundle(
                args.catalog,
                args.bundle,
                args.root,
                access_receipt_path=args.access_receipt,
                object_store_prefix=args.object_store_prefix,
            )
            print(json.dumps({"ready": True, "manifest_sha256": digest}, sort_keys=True))
        elif args.command == "verify":
            _manifest, digest = verify_manifest(args.manifest)
            print(json.dumps({"valid": True, "manifest_sha256": digest}, sort_keys=True))
        elif args.command == "validate-request":
            document = validate_preprocess_request(load_json(args.request), allow_public_msa=args.allow_public_msa)
            print(json.dumps({"valid": True, "request_sha256": sha256_bytes(canonical_json(document))}, sort_keys=True))
        elif args.command == "preprocess":
            print(json.dumps(run_preprocess(
                args.request,
                allow_public_msa=args.allow_public_msa,
                telemetry_root=args.telemetry_root,
            ), sort_keys=True))
        elif args.command == "serve-status":
            serve_status(args.root, args.host, args.port)
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
