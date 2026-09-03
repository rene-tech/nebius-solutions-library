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
SOURCE_INDEX_SCHEMA = "fs2-serve.nebius.ai/reference-data-source-index/v1"
PLACEMENT_SCHEMA = "fs2-serve.nebius.ai/reference-data-placement-contract/v1"
PLAN_SCHEMA = "fs2-serve.nebius.ai/reference-data-localization-plan/v1"
INVENTORY_SCHEMA = "fs2-serve.nebius.ai/reference-data-inventory/v1"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1"
CANONICAL_HOST_ROOT = "/mnt/fs2-reference-data/data"
# A consuming controller must be able to validate a terminal receipt without
# enumerating a reference database, so an inventory longer than this is
# published as a separate content-addressed document and referenced by digest.
MAX_INLINE_INVENTORY_FILES = 4096
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


def default_placement_path() -> Path:
    """The reviewed in-repo placement contract used when none is mounted."""
    return Path(__file__).with_name("placement-contract.json")


LABEL_KEY_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?(/[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?)?$")
RESOURCE_NAME_RE = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9][A-Za-z0-9._-]*$")
# Placement must stay portable: an accelerator generation belongs in a label
# value, never in a label key, and a node identity is never a placement input.
FORBIDDEN_SELECTOR_KEYS = frozenset({"kubernetes.io/hostname", "metadata.name"})
HARDWARE_KEY_RE = re.compile(r"(?i)(h100|h200|b200|b300|a100|l40|l4|v100|gh200|sxm|hgx)")
GPU_SELECTOR_KEYS = frozenset({"workload.fs2.nebius/gpu", "nebius.com/gpu", "accelerator.fs2.nebius/class"})
QUANTITY_SUFFIX = {"Mi": 1, "Gi": 1024}


def parse_cpu_millicores(value: str, context: str) -> int:
    text_value = str(value)
    if CPU_RE.fullmatch(text_value):
        return int(text_value) * 1000
    if re.fullmatch(r"[1-9][0-9]*m", text_value):
        return int(text_value[:-1])
    raise ContractError(f"{context} is not a valid CPU quantity")


def parse_memory_mib(value: str, context: str) -> int:
    text_value = str(value)
    if not MEMORY_RE.fullmatch(text_value):
        raise ContractError(f"{context} is not a valid Mi/Gi quantity")
    return int(text_value[:-2]) * QUANTITY_SUFFIX[text_value[-2:]]


def _validate_node_selector(value: Any, context: str) -> dict[str, str]:
    selector = _expect_object(value, context)
    if not selector:
        raise ContractError(f"{context} must select at least one stable node label")
    for key, label_value in selector.items():
        if key in FORBIDDEN_SELECTOR_KEYS:
            raise ContractError(f"{context} must not pin a node identity ({key})")
        if not LABEL_KEY_RE.fullmatch(str(key)) or HARDWARE_KEY_RE.search(str(key)):
            raise ContractError(f"{context} label key {key!r} is not a stable portable label")
        if not isinstance(label_value, str) or not label_value:
            raise ContractError(f"{context} label {key!r} must have a non-empty string value")
    return selector


def _validate_tolerations(value: Any, context: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{context} must declare at least one toleration")
    tolerations: list[dict[str, str]] = []
    for index, item in enumerate(value):
        toleration = _expect_object(item, f"{context}[{index}]")
        fields = {"key", "operator", "value", "effect"}
        _expect_keys(toleration, fields, fields, f"{context}[{index}]")
        if not LABEL_KEY_RE.fullmatch(str(toleration["key"])):
            raise ContractError(f"{context}[{index}] key is not a valid taint key")
        if toleration["operator"] != "Equal":
            raise ContractError(f"{context}[{index}] must use the Equal operator")
        if not isinstance(toleration["value"], str) or not toleration["value"]:
            raise ContractError(f"{context}[{index}] value must be a non-empty string")
        if toleration["effect"] not in {"NoSchedule", "NoExecute", "PreferNoSchedule"}:
            raise ContractError(f"{context}[{index}] effect is invalid")
        tolerations.append({key: str(toleration[key]) for key in sorted(fields)})
    return tolerations


def validate_placement_contract(document: Any) -> dict[str, Any]:
    """Validate the tfvars-derived placement and sizing contract.

    The contract is the only source of node placement and resource sizing for
    reference-data and preprocessing stages, so nothing downstream needs a
    literal node name, accelerator generation or hand-written pod field.
    """
    contract = _expect_object(document, "placement contract")
    required = {"schema", "generated_at", "pools", "stages"}
    _expect_keys(contract, required, required, "placement contract")
    if contract["schema"] != PLACEMENT_SCHEMA:
        raise ContractError(f"placement contract schema must be {PLACEMENT_SCHEMA}")
    _expect_datetime(contract["generated_at"], "placement contract generated_at")
    pools = _expect_object(contract["pools"], "placement contract pools")
    if not pools:
        raise ContractError("placement contract must declare at least one pool")
    for pool_id, pool_value in pools.items():
        context = f"placement pool {pool_id}"
        if not ID_RE.fullmatch(str(pool_id)):
            raise ContractError(f"{context} id is invalid")
        pool = _expect_object(pool_value, context)
        pool_fields = {"resource_class", "node_selector", "tolerations", "schedulable_capacity", "queue"}
        optional = {"accelerator"}
        _expect_keys(pool, pool_fields, pool_fields | optional, context)
        if pool["resource_class"] not in {"cpu", "gpu"}:
            raise ContractError(f"{context} resource class must be cpu or gpu")
        selector = _validate_node_selector(pool["node_selector"], f"{context} node selector")
        _validate_tolerations(pool["tolerations"], f"{context} tolerations")
        capacity = _expect_object(pool["schedulable_capacity"], f"{context} schedulable capacity")
        capacity_fields = {"cpu_millicores", "memory_mib", "ephemeral_storage_mib"}
        _expect_keys(capacity, capacity_fields, capacity_fields, f"{context} schedulable capacity")
        for field in sorted(capacity_fields):
            if not isinstance(capacity[field], int) or isinstance(capacity[field], bool) or capacity[field] < 1:
                raise ContractError(f"{context} schedulable {field} must be a positive integer")
        queue = _expect_object(pool["queue"], f"{context} queue")
        queue_fields = {"local_queue", "cluster_queue", "nominal_cpu", "nominal_memory", "nominal_accelerator"}
        _expect_keys(queue, queue_fields, queue_fields, f"{context} queue")
        for field in ("local_queue", "cluster_queue"):
            if not ID_RE.fullmatch(str(queue[field])):
                raise ContractError(f"{context} {field} is invalid")
        # A ClusterQueue need not cover every resource. A null nominal quota
        # means the resource is bounded only by schedulable node capacity.
        if queue["nominal_cpu"] is not None:
            parse_cpu_millicores(queue["nominal_cpu"], f"{context} nominal CPU")
        if queue["nominal_memory"] is not None:
            parse_memory_mib(queue["nominal_memory"], f"{context} nominal memory")
        if queue["nominal_accelerator"] is not None and (
            not isinstance(queue["nominal_accelerator"], int)
            or isinstance(queue["nominal_accelerator"], bool)
            or queue["nominal_accelerator"] < 1
        ):
            raise ContractError(f"{context} nominal accelerator quota is invalid")
        if pool["resource_class"] == "cpu":
            if "accelerator" in pool:
                raise ContractError(f"{context} is a CPU pool and must not reserve accelerators")
            if GPU_SELECTOR_KEYS & set(selector):
                raise ContractError(f"{context} is a CPU pool and must not select an accelerator node")
            if not any(item["effect"] == "NoSchedule" for item in pool["tolerations"]):
                raise ContractError(f"{context} must tolerate its dedicated NoSchedule taint")
        else:
            accelerator = _expect_object(pool.get("accelerator"), f"{context} accelerator")
            accelerator_fields = {"resource_name", "count"}
            _expect_keys(accelerator, accelerator_fields, accelerator_fields, f"{context} accelerator")
            if not RESOURCE_NAME_RE.fullmatch(str(accelerator["resource_name"])):
                raise ContractError(f"{context} accelerator resource name is invalid")
            if (
                not isinstance(accelerator["count"], int)
                or isinstance(accelerator["count"], bool)
                or accelerator["count"] < 1
            ):
                raise ContractError(f"{context} accelerator count must be a positive integer")
            if selector.get("capacity.fs2.nebius/pool") == "reference-data":
                raise ContractError(f"{context} must not route accelerator work to the reference-data pool")
    stages = _expect_object(contract["stages"], "placement contract stages")
    for stage_id in ("staging", "raw-input", "inference"):
        if stage_id not in stages:
            raise ContractError(f"placement contract must declare the {stage_id} stage")
    for stage_id, stage_value in stages.items():
        context = f"placement stage {stage_id}"
        stage = _expect_object(stage_value, context)
        stage_fields = {"pool", "defaults"}
        _expect_keys(stage, stage_fields, stage_fields, context)
        pool_id = str(stage["pool"])
        if pool_id not in pools:
            raise ContractError(f"{context} references unknown pool {pool_id}")
        expected_class = "gpu" if stage_id == "inference" else "cpu"
        if pools[pool_id]["resource_class"] != expected_class:
            raise ContractError(f"{context} must bind a {expected_class} pool")
        defaults = _expect_object(stage["defaults"], f"{context} defaults")
        default_fields = {"cpu", "memory", "ephemeral_storage", "active_deadline_seconds", "backoff_limit", "threads"}
        _expect_keys(defaults, default_fields, default_fields, f"{context} defaults")
        if not CPU_RE.fullmatch(str(defaults["cpu"])):
            raise ContractError(f"{context} default CPU request is invalid")
        for field in ("memory", "ephemeral_storage"):
            parse_memory_mib(defaults[field], f"{context} default {field}")
        if (
            not isinstance(defaults["active_deadline_seconds"], int)
            or isinstance(defaults["active_deadline_seconds"], bool)
            or not 60 <= defaults["active_deadline_seconds"] <= 604800
        ):
            raise ContractError(f"{context} default deadline is invalid")
        if (
            not isinstance(defaults["backoff_limit"], int)
            or isinstance(defaults["backoff_limit"], bool)
            or not 0 <= defaults["backoff_limit"] <= 10
        ):
            raise ContractError(f"{context} default retry limit is invalid")
        if (
            not isinstance(defaults["threads"], int)
            or isinstance(defaults["threads"], bool)
            or not 1 <= defaults["threads"] <= 128
        ):
            raise ContractError(f"{context} default thread count is invalid")
        if defaults["threads"] > int(defaults["cpu"]):
            raise ContractError(f"{context} default thread count exceeds its CPU request")
    return contract


def model_preprocessing_capacity(model_id: str, requirements_path: Path | None = None) -> dict[str, Any] | None:
    """The declared minimum a model's preprocessing stage needs to be runnable.

    A stage sized below this is not slow, it is not runnable, so a request
    below it is refused rather than left to fail late on a node that could
    never have run it.
    """
    path = requirements_path or Path(__file__).with_name("model-requirements.json")
    document = _expect_object(load_json(path), "model reference-data requirements")
    model = _expect_object(document.get("models", {}), "model requirements").get(model_id)
    if not isinstance(model, dict) or "preprocessing_capacity" not in model:
        return None
    capacity = _expect_object(model["preprocessing_capacity"], f"{model_id} preprocessing capacity")
    for field in ("cpu", "memory", "ephemeral_storage"):
        if field not in capacity:
            raise ContractError(f"{model_id} preprocessing capacity is missing {field}")
    parse_cpu_millicores(capacity["cpu"], f"{model_id} preprocessing CPU")
    for field in ("memory", "ephemeral_storage"):
        parse_memory_mib(capacity[field], f"{model_id} preprocessing {field}")
    return capacity


def check_model_preprocessing_capacity(execution: Mapping[str, Any], model_id: str) -> None:
    """Refuse a preprocessing request below its model's declared minimum."""
    capacity = model_preprocessing_capacity(model_id)
    if capacity is None:
        return
    checks = (
        ("CPU", parse_cpu_millicores(execution["cpu"], "request CPU"),
         parse_cpu_millicores(capacity["cpu"], "required CPU"), capacity["cpu"]),
        ("memory", parse_memory_mib(execution["memory"], "request memory"),
         parse_memory_mib(capacity["memory"], "required memory"), capacity["memory"]),
        ("ephemeral storage", parse_memory_mib(execution["ephemeral_storage"], "request ephemeral storage"),
         parse_memory_mib(capacity["ephemeral_storage"], "required ephemeral storage"),
         capacity["ephemeral_storage"]),
    )
    for resource, requested, required, declared in checks:
        if requested < required:
            raise ContractError(
                f"{model_id} preprocessing requires at least {declared} {resource} and this "
                f"request declares less; the reference-data pool that stages the bulk databases "
                "is not sized for the data pipeline, so provision a fitting CPU class in root "
                "terraform.tfvars rather than advertising a lane that cannot run"
            )


def stage_admissibility(contract: Mapping[str, Any], stage_id: str) -> dict[str, Any]:
    """Report whether a stage's declared sizing can actually be admitted."""
    placement = resolve_stage_placement(contract, stage_id)
    reasons: list[str] = []
    try:
        check_execution_fits(placement["defaults"], contract, stage_id)
    except ContractError as exc:
        reasons.append(str(exc))
    return {
        "stage": stage_id,
        "pool": placement["pool"],
        "resource_class": placement["resource_class"],
        "requested": {
            field: placement["defaults"][field]
            for field in ("cpu", "memory", "ephemeral_storage")
        },
        "pool_schedulable_capacity": placement["schedulable_capacity"],
        "queue": placement["queue"],
        "runnable": not reasons,
        "reasons": reasons,
    }


def load_placement_contract(path: Path | None = None) -> dict[str, Any]:
    """Load the placement contract, letting a mounted document override pools.

    Terraform renders only the CPU pools and stages it owns, so the
    reference-data plane never names an accelerator resource. Any pool or
    stage the mounted document declares replaces the reviewed default whole.
    """
    default = _expect_object(load_json(default_placement_path()), "default placement contract")
    if path is None:
        return validate_placement_contract(default)
    override = _expect_object(load_json(path), "placement override")
    if override.get("schema") != PLACEMENT_SCHEMA:
        raise ContractError(f"placement override schema must be {PLACEMENT_SCHEMA}")
    unknown = set(override) - {"schema", "generated_at", "pools", "stages"}
    if unknown:
        raise ContractError(f"placement override has unknown fields: {', '.join(sorted(unknown))}")
    merged = {
        "schema": PLACEMENT_SCHEMA,
        "generated_at": override.get("generated_at", default["generated_at"]),
        "pools": {
            **default["pools"],
            **_expect_object(override.get("pools", {}), "placement override pools"),
        },
        "stages": {
            **default["stages"],
            **_expect_object(override.get("stages", {}), "placement override stages"),
        },
    }
    return validate_placement_contract(merged)


def resolve_stage_placement(contract: Mapping[str, Any], stage_id: str) -> dict[str, Any]:
    """Return the resolved pool placement and sizing defaults for one stage."""
    try:
        stage = contract["stages"][stage_id]
    except (KeyError, TypeError) as exc:
        raise ContractError(f"placement contract has no {stage_id} stage") from exc
    pool = contract["pools"][stage["pool"]]
    resolved: dict[str, Any] = {
        "stage": stage_id,
        "pool": stage["pool"],
        "resource_class": pool["resource_class"],
        "node_selector": dict(pool["node_selector"]),
        "tolerations": [dict(item) for item in pool["tolerations"]],
        "queue": dict(pool["queue"]),
        "schedulable_capacity": dict(pool["schedulable_capacity"]),
        "defaults": dict(stage["defaults"]),
    }
    if "accelerator" in pool:
        resolved["accelerator"] = dict(pool["accelerator"])
    return resolved


def check_execution_fits(execution: Mapping[str, Any], contract: Mapping[str, Any], stage_id: str) -> None:
    """Fail closed when a stage request cannot be admitted by its own pool.

    Kueue leaves an over-sized Job pending forever rather than rejecting it, so
    the request is compared against both the dedicated pool's schedulable
    capacity and the ClusterQueue nominal quota before a Job is ever created.
    """
    placement = resolve_stage_placement(contract, stage_id)
    capacity = placement["schedulable_capacity"]
    queue = placement["queue"]
    requested_cpu = parse_cpu_millicores(execution["cpu"], f"{stage_id} CPU request")
    requested_memory = parse_memory_mib(execution["memory"], f"{stage_id} memory request")
    requested_storage = parse_memory_mib(execution["ephemeral_storage"], f"{stage_id} ephemeral storage request")
    limits = [
        ("CPU", requested_cpu, capacity["cpu_millicores"], "schedulable capacity"),
        ("memory", requested_memory, capacity["memory_mib"], "schedulable capacity"),
        ("ephemeral storage", requested_storage, capacity["ephemeral_storage_mib"], "schedulable capacity"),
    ]
    if queue["nominal_cpu"] is not None:
        limits.append((
            "CPU", requested_cpu,
            parse_cpu_millicores(queue["nominal_cpu"], "nominal CPU"), "Kueue nominal quota",
        ))
    if queue["nominal_memory"] is not None:
        limits.append((
            "memory", requested_memory,
            parse_memory_mib(queue["nominal_memory"], "nominal memory"), "Kueue nominal quota",
        ))
    accelerator = placement.get("accelerator")
    if accelerator is not None and queue["nominal_accelerator"] is not None:
        limits.append((
            f"{accelerator['resource_name']} count", accelerator["count"],
            queue["nominal_accelerator"], "Kueue nominal quota",
        ))
    for resource, requested, available, source in limits:
        if requested > available:
            raise ContractError(
                f"{stage_id} {resource} request exceeds the {source} of the dedicated "
                f"{placement['pool']} pool ({requested} > {available}); raise the pool in root "
                "terraform.tfvars instead of leaving the Job unschedulable"
            )


# Field names a consumer draft has used instead of the published contract.
HANDOFF_ALIASES = {
    "published_manifest_sha256": "content.manifest_sha256",
    "source_sub_path": "storage.dataset_sub_path",
    "published_tree_sha256": "content.tree_sha256",
    "manifest_digest": "content.manifest_sha256",
}


def _reject_handoff_aliases(*documents: Mapping[str, Any]) -> None:
    """Fail closed when a consumer sends an invented handoff field name.

    There is exactly one published handoff contract. Silently ignoring a
    near-miss field name would let a runtime bind a digest nobody produced.
    """
    for document in documents:
        for alias, actual in HANDOFF_ALIASES.items():
            if alias in document:
                raise ContractError(
                    f"handoff field {alias!r} does not exist; use {actual!r} from the "
                    f"{RECEIPT_SCHEMA} contract"
                )


def validate_terminal_receipt(document: Any) -> dict[str, Any]:
    """Validate a bounded, content-addressed terminal reference-data receipt.

    A consumer binds a mount and a dataset sub-path from this document alone.
    It therefore carries the aggregate tree digest, an independent manifest
    digest and an inventory digest with counts, never a file list.
    """
    receipt = _expect_object(document, "terminal receipt")
    required = {"schema", "bundle_id", "revision", "created_at", "storage", "content", "placement"}
    _expect_keys(receipt, required, required, "terminal receipt")
    if receipt["schema"] != RECEIPT_SCHEMA:
        raise ContractError(f"terminal receipt schema must be {RECEIPT_SCHEMA}")
    _expect_datetime(receipt["created_at"], "terminal receipt created_at")
    if not ID_RE.fullmatch(str(receipt["bundle_id"])):
        raise ContractError("terminal receipt bundle id is invalid")
    revision = str(receipt["revision"])
    if not revision or len(revision) > 160:
        raise ContractError("terminal receipt revision is invalid")
    storage = _expect_object(receipt["storage"], "terminal receipt storage")
    _reject_handoff_aliases(storage)
    storage_fields = {"host_root", "mount_path", "dataset_sub_path", "read_only"}
    _expect_keys(storage, storage_fields, storage_fields, "terminal receipt storage")
    for field in ("host_root", "mount_path"):
        value = str(storage[field])
        if not value.startswith("/") or ".." in Path(value).parts:
            raise ContractError(f"terminal receipt {field} must be an absolute path without traversal")
    if storage["read_only"] is not True:
        raise ContractError("terminal receipt must mount the published tree read-only")
    content = _expect_object(receipt["content"], "terminal receipt content")
    _reject_handoff_aliases(receipt, content)
    content_fields = {
        "tree_sha256", "manifest_sha256", "inventory_sha256",
        "inventory_marker", "file_count", "expanded_bytes", "inline_inventory",
    }
    _expect_keys(content, content_fields, content_fields, "terminal receipt content")
    for field in ("tree_sha256", "manifest_sha256", "inventory_sha256"):
        if not SHA256_RE.fullmatch(str(content[field])):
            raise ContractError(f"terminal receipt {field} must be a SHA-256 digest")
    if content["manifest_sha256"] == content["tree_sha256"]:
        raise ContractError("terminal receipt manifest digest must be independent of the tree digest")
    if content["inventory_marker"] != ".fs2-manifest-sha256":
        raise ContractError("terminal receipt inventory marker is invalid")
    for field in ("file_count", "expanded_bytes"):
        if not isinstance(content[field], int) or isinstance(content[field], bool) or content[field] < 1:
            raise ContractError(f"terminal receipt {field} must be a positive integer")
    if not isinstance(content["inline_inventory"], bool):
        raise ContractError("terminal receipt inline_inventory must be a boolean")
    if content["inline_inventory"] != (content["file_count"] <= MAX_INLINE_INVENTORY_FILES):
        raise ContractError(
            "terminal receipt inline_inventory must agree with the "
            f"{MAX_INLINE_INVENTORY_FILES}-file bound a consumer can validate"
        )
    expected = f"datasets/{receipt['bundle_id']}/{revision}/sha256/{content['tree_sha256']}"
    if str(storage["dataset_sub_path"]) != expected:
        raise ContractError(
            "terminal receipt dataset sub-path must bind its /sha256/<tree> component "
            "to the exact aggregate tree digest"
        )
    placement = _expect_object(receipt["placement"], "terminal receipt placement")
    if placement.get("resource_class") != "cpu":
        raise ContractError("the reference-data stage placement must stay CPU-only")
    if "accelerator" in placement:
        raise ContractError("the reference-data stage must not reserve an accelerator")
    selector = _validate_node_selector(placement.get("node_selector"), "terminal receipt node selector")
    if GPU_SELECTOR_KEYS & set(selector):
        raise ContractError("the reference-data stage must not declare an accelerator node selector")
    _validate_tolerations(placement.get("tolerations"), "terminal receipt tolerations")
    return receipt


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
    return _hash_file_multi(path, (algorithm,))[algorithm]


def _hash_file_multi(path: Path, algorithms: Sequence[str]) -> dict[str, str]:
    """Hash a file once under several algorithms.

    Reference-data blobs are tens of gigabytes each, so the catalog transport
    digest and the content-addressed SHA-256 are computed in a single pass.
    """
    digests = {name: hashlib.new(name) for name in dict.fromkeys(algorithms)}
    with path.open("rb") as handle:
        while block := handle.read(BUFFER_BYTES):
            for digest in digests.values():
                digest.update(block)
    return {name: digest.hexdigest() for name, digest in digests.items()}


def _blob_path(root: Path, source_sha256: str) -> Path:
    if not SHA256_RE.fullmatch(source_sha256):
        raise ContractError("content-addressed blob name must be a SHA-256 digest")
    return root / "blobs" / "sha256" / source_sha256[:2] / source_sha256


def _source_index_path(root: Path, bundle: Mapping[str, Any], object_id: str) -> Path:
    return root / "sources" / bundle["id"] / bundle["revision"] / f"{object_id}.json"


def _catalog_identity(item: Mapping[str, Any]) -> dict[str, Any]:
    """The immutable catalog identity a localized blob must keep matching."""
    integrity = item["source_integrity"]
    return {
        "source_bytes": item["source_bytes"],
        "integrity_algorithm": integrity["algorithm"],
        "integrity_digest": str(integrity["digest"]).lower(),
    }


def _blob_catalog_match(blob: Path, item: Mapping[str, Any]) -> str | None:
    """Return the blob's SHA-256 when it is this catalog object, else None.

    Two catalog objects may legitimately have the same byte count, so a
    candidate that fails the catalog digest is simply a different object.
    A blob whose content does not hash to its own content-addressed name is
    store corruption and is always fatal.
    """
    if blob.stat().st_size != item["source_bytes"]:
        return None
    integrity = item["source_integrity"]
    algorithm = integrity["algorithm"]
    wanted = {"sha256"}
    if algorithm in {"sha256", "md5"}:
        wanted.add(algorithm)
    observed = _hash_file_multi(blob, sorted(wanted))
    if SHA256_RE.fullmatch(blob.name) and observed["sha256"] != blob.name:
        raise ContractError(f"existing content-addressed blob is corrupt: {blob.name}")
    if algorithm in {"sha256", "md5"} and observed[algorithm] != str(integrity["digest"]).lower():
        return None
    return observed["sha256"]


def verify_blob_identity(blob: Path, item: Mapping[str, Any]) -> str:
    """Return a blob's SHA-256 after proving it is the exact catalog object."""
    if blob.stat().st_size != item["source_bytes"]:
        raise ContractError(f"source {item['id']} byte count does not match catalog")
    matched = _blob_catalog_match(blob, item)
    if matched is None:
        raise ContractError(f"source {item['id']} checksum does not match catalog")
    return matched


def _write_source_index(root: Path, bundle: Mapping[str, Any], item: Mapping[str, Any], source_sha256: str) -> None:
    blob = _blob_path(root, source_sha256)
    atomic_json(
        _source_index_path(root, bundle, item["id"]),
        {
            "schema": SOURCE_INDEX_SCHEMA,
            "bundle_id": bundle["id"],
            "revision": bundle["revision"],
            "object_id": item["id"],
            "source_sha256": source_sha256,
            "blob_path": blob.relative_to(root).as_posix(),
            "recorded_at": _utc_now(),
            **_catalog_identity(item),
        },
    )


def read_source_index(root: Path, bundle: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a validated localization record, or None when none is recorded.

    A record whose catalog identity no longer matches is a fail-closed error:
    silently re-downloading would let a mutated catalog reuse a revision.
    """
    index_path = _source_index_path(root, bundle, item["id"])
    if not index_path.is_file():
        return None
    entry = _expect_object(load_json(index_path), f"source index for {item['id']}")
    required = {
        "schema", "bundle_id", "revision", "object_id", "source_sha256",
        "blob_path", "recorded_at", "source_bytes", "integrity_algorithm", "integrity_digest",
    }
    _expect_keys(entry, required, required, f"source index for {item['id']}")
    if entry["schema"] != SOURCE_INDEX_SCHEMA:
        raise ContractError(f"source index for {item['id']} has an unsupported schema")
    if (
        entry["bundle_id"] != bundle["id"]
        or entry["revision"] != bundle["revision"]
        or entry["object_id"] != item["id"]
    ):
        raise ContractError(f"source index for {item['id']} belongs to a different bundle revision")
    if not SHA256_RE.fullmatch(str(entry["source_sha256"])):
        raise ContractError(f"source index for {item['id']} has an invalid content digest")
    identity = _catalog_identity(item)
    if {key: entry[key] for key in identity} != identity:
        raise ContractError(
            f"recorded source identity for {item['id']} differs from the catalog; "
            "publish a new immutable revision instead of reusing this one"
        )
    if entry["blob_path"] != _blob_path(root, str(entry["source_sha256"])).relative_to(root).as_posix():
        raise ContractError(f"source index for {item['id']} does not point at its content-addressed blob")
    return entry


def _claimed_digests(root: Path, bundle: Mapping[str, Any]) -> set[str]:
    """SHA-256 values this bundle revision has already recorded."""
    index_dir = root / "sources" / bundle["id"] / bundle["revision"]
    if not index_dir.is_dir():
        return set()
    claimed: set[str] = set()
    for path in index_dir.glob("*.json"):
        try:
            entry = load_json(path)
        except ContractError:
            continue
        digest = entry.get("source_sha256") if isinstance(entry, dict) else None
        if isinstance(digest, str) and SHA256_RE.fullmatch(digest):
            claimed.add(digest)
    return claimed


def _blob_sizes(root: Path) -> dict[str, int]:
    """Content-addressed blob digests and byte counts, without hashing."""
    blobs = root / "blobs" / "sha256"
    if not blobs.is_dir():
        return {}
    return {
        candidate.name: candidate.stat().st_size
        for candidate in blobs.glob("*/*")
        if candidate.is_file() and not candidate.is_symlink() and SHA256_RE.fullmatch(candidate.name)
    }


def _adopt_existing_blob(root: Path, bundle: Mapping[str, Any], item: Mapping[str, Any]) -> str | None:
    """Adopt an already-downloaded blob that has no localization record.

    Staging interrupted before this index existed leaves verified blobs
    behind, and re-downloading them would discard hours of transfer. Only
    blobs this revision has not already recorded are candidates: otherwise a
    bundle whose objects share a byte count would re-hash every sibling for
    every object.
    """
    blobs = root / "blobs" / "sha256"
    if not blobs.is_dir():
        return None
    claimed = _claimed_digests(root, bundle)
    for candidate in sorted(blobs.glob("*/*")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if not SHA256_RE.fullmatch(candidate.name) or candidate.name in claimed:
            continue
        if candidate.stat().st_size != item["source_bytes"]:
            continue
        source_sha256 = _blob_catalog_match(candidate, item)
        if source_sha256 is not None:
            return source_sha256
    return None


def localize_source(
    root: Path,
    bundle: Mapping[str, Any],
    item: Mapping[str, Any],
    downloads: Path,
    *,
    verify_existing_blobs: bool = False,
) -> tuple[Path, str, str]:
    """Make one catalog object available as an immutable blob.

    Returns the blob path, its SHA-256 and how it was obtained. Already
    localized objects are never re-downloaded, so an interrupted bundle
    resumes from the bytes it already has.
    """
    entry = read_source_index(root, bundle, item)
    if entry is not None:
        source_sha256 = str(entry["source_sha256"])
        blob = _blob_path(root, source_sha256)
        if blob.is_file() and blob.stat().st_size == item["source_bytes"]:
            if verify_existing_blobs and verify_blob_identity(blob, item) != source_sha256:
                raise ContractError(f"existing content-addressed blob is corrupt: {item['id']}")
            return blob, source_sha256, "reused"
    adopted = _adopt_existing_blob(root, bundle, item)
    if adopted is not None:
        _write_source_index(root, bundle, item, adopted)
        return _blob_path(root, adopted), adopted, "adopted"
    partial, source_sha256 = download_source(item, downloads)
    blob = _blob_path(root, source_sha256)
    blob.parent.mkdir(parents=True, exist_ok=True)
    if blob.exists():
        if verify_blob_identity(blob, item) != source_sha256:
            raise ContractError(f"existing content-addressed blob is corrupt: {item['id']}")
        partial.unlink(missing_ok=True)
    else:
        os.replace(partial, blob)
        blob.chmod(0o444)
    _write_source_index(root, bundle, item, source_sha256)
    return blob, source_sha256, "downloaded"


def localization_plan(catalog_path: Path, bundle_id: str, root: Path) -> dict[str, Any]:
    """Report, without mutating anything, how much of a bundle is localized."""
    catalog = validate_catalog(load_json(catalog_path))
    try:
        bundle = catalog["bundles"][bundle_id]
    except KeyError as exc:
        raise ContractError(f"unknown bundle id {bundle_id}") from exc
    downloads = root / "downloads" / bundle_id / bundle["revision"]
    objects: list[dict[str, Any]] = []
    localized_bytes = 0
    adoptable_bytes = 0
    partial_bytes = 0
    claimed = _claimed_digests(root, bundle)
    sized_blobs = _blob_sizes(root)
    for item in bundle["objects"]:
        entry = read_source_index(root, bundle, item)
        blob: Path | None = None
        if entry is not None:
            candidate = _blob_path(root, str(entry["source_sha256"]))
            if candidate.is_file() and candidate.stat().st_size == item["source_bytes"]:
                blob = candidate
        partial = downloads / f"{item['id']}.part"
        partial_size = partial.stat().st_size if partial.is_file() else 0
        adoptable = None
        if blob is None:
            adoptable = next(
                (
                    digest for digest, size in sorted(sized_blobs.items())
                    if size == item["source_bytes"] and digest not in claimed
                ),
                None,
            )
        if blob is not None:
            state = "localized"
            localized_bytes += item["source_bytes"]
        elif adoptable is not None:
            # Size-only evidence. Staging re-verifies the catalog transport
            # digest before adopting, so this is a plan, not a guarantee.
            state = "adoptable"
            adoptable_bytes += item["source_bytes"]
        elif partial_size:
            state = "partial"
            partial_bytes += min(partial_size, item["source_bytes"])
        else:
            state = "missing"
        objects.append({
            "id": item["id"],
            "state": state,
            "source_bytes": item["source_bytes"],
            "partial_bytes": partial_size,
            "source_sha256": str(entry["source_sha256"]) if entry is not None and blob is not None else None,
            "adoptable_blob_sha256": adoptable,
            "digest_verified": blob is not None,
            "indexed": entry is not None,
        })
    source_bytes = sum(item["source_bytes"] for item in bundle["objects"])
    status_path = root / "status" / f"{bundle_id}.json"
    published: dict[str, Any] = {"ready": False, "manifest_sha256": None, "tree_sha256": None, "revision": None}
    if status_path.is_file():
        status = _expect_object(load_json(status_path), "publication status")
        published = {
            "ready": status.get("ready") is True,
            "manifest_sha256": status.get("manifest_sha256"),
            "tree_sha256": status.get("tree_sha256"),
            "revision": status.get("revision"),
        }
    return {
        "schema": PLAN_SCHEMA,
        "bundle_id": bundle_id,
        "revision": bundle["revision"],
        "generated_at": _utc_now(),
        "objects": objects,
        "totals": {
            "object_count": len(objects),
            "localized_objects": sum(1 for item in objects if item["state"] == "localized"),
            "adoptable_objects": sum(1 for item in objects if item["state"] == "adoptable"),
            "source_bytes": source_bytes,
            "localized_bytes": localized_bytes,
            "adoptable_bytes": adoptable_bytes,
            "partial_bytes": partial_bytes,
            "remaining_bytes": source_bytes - localized_bytes - adoptable_bytes - partial_bytes,
        },
        "published": published,
    }


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


def _inventory_sort_key(entry: Mapping[str, Any]) -> str:
    """The single ordering rule every inventory producer must use.

    ``Path`` comparison is a plain comparison of the whole path string, so
    sorting tree-relative POSIX paths as strings reproduces the order of a
    recursive walk exactly. Per-object expansion and a whole-tree walk
    therefore agree on the aggregate digest.
    """
    return str(entry["path"])


def aggregate_tree_identity(files: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str, int]:
    """Return the canonical inventory, aggregate tree digest and total bytes."""
    ordered = sorted((dict(entry) for entry in files), key=_inventory_sort_key)
    paths = [entry["path"] for entry in ordered]
    if len(set(paths)) != len(paths):
        duplicate = next(path for path in paths if paths.count(path) > 1)
        raise ContractError(f"reference-data tree has a duplicate path: {duplicate}")
    if not ordered:
        raise ContractError("materialized reference-data tree is empty")
    return ordered, sha256_bytes(canonical_json(ordered)), sum(int(entry["bytes"]) for entry in ordered)


def walk_tree_files(root: Path, *, exclude: frozenset[str] = frozenset()) -> list[tuple[Path, str, int]]:
    """List the regular files of a tree with their tree-relative paths."""
    found: list[tuple[Path, str, int]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ContractError(f"published tree contains a symlink: {path.relative_to(root)}")
        if not path.is_file() or path.name.startswith(".fs2-"):
            continue
        relative = path.relative_to(root).as_posix()
        if relative in exclude:
            continue
        found.append((path, relative, path.stat().st_size))
    return found


def tree_stat_summary(root: Path, *, exclude: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Report every tree file's path and size without hashing its content.

    Structural drift in a reference database is caught here in seconds; the
    per-file digests were already computed while each object was expanded.
    """
    return sorted(
        ({"path": relative, "bytes": size} for _path, relative, size in walk_tree_files(root, exclude=exclude)),
        key=_inventory_sort_key,
    )


def tree_inventory(root: Path, *, exclude: frozenset[str] = frozenset()) -> tuple[list[dict[str, Any]], str, int]:
    files = [
        {"path": relative, "bytes": size, "sha256": _hash_file(path)}
        for path, relative, size in walk_tree_files(root, exclude=exclude)
    ]
    return aggregate_tree_identity(files)


EXPANSION_SCHEMA = "fs2-serve.nebius.ai/reference-data-object-expansion/v1"


def _object_lock_path(root: Path, bundle: Mapping[str, Any], object_id: str) -> Path:
    return root / "locks" / bundle["id"] / bundle["revision"] / f"{object_id}.lock"


def _expansion_receipt_path(root: Path, bundle: Mapping[str, Any], object_id: str) -> Path:
    return root / "expansions" / bundle["id"] / bundle["revision"] / f"{object_id}.json"


def _expanded_root(root: Path, bundle: Mapping[str, Any], object_id: str, source_sha256: str) -> Path:
    return root / "expanded" / bundle["id"] / bundle["revision"] / object_id / "sha256" / source_sha256


def _object_target_paths(item: Mapping[str, Any], expanded: Path) -> list[tuple[Path, str, int]]:
    """Map one object's expanded files onto their final tree-relative paths."""
    relative = _safe_relative(item["target"], f"source {item['id']} target", allow_dot=True)
    prefix = "" if relative == Path(".") else relative.as_posix()
    if expanded.is_file():
        if not prefix:
            raise ContractError(f"source {item['id']} expanded to a file but targets the tree root")
        return [(expanded, prefix, expanded.stat().st_size)]
    return [
        (path, path_relative if not prefix else f"{prefix}/{path_relative}", size)
        for path, path_relative, size in walk_tree_files(expanded)
    ]


def read_expansion_receipt(
    root: Path,
    bundle: Mapping[str, Any],
    item: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return a usable per-object expansion record, or None to redo the work."""
    path = _expansion_receipt_path(root, bundle, item["id"])
    if not path.is_file():
        return None
    receipt = _expect_object(load_json(path), f"expansion receipt for {item['id']}")
    required = {
        "schema", "bundle_id", "revision", "object_id", "source_sha256", "target", "transform",
        "expanded_path", "file_count", "expanded_bytes", "inventory_sha256", "recorded_at",
    }
    _expect_keys(receipt, required, required, f"expansion receipt for {item['id']}")
    if receipt["schema"] != EXPANSION_SCHEMA:
        raise ContractError(f"expansion receipt for {item['id']} has an unsupported schema")
    if (
        receipt["bundle_id"] != bundle["id"]
        or receipt["revision"] != bundle["revision"]
        or receipt["object_id"] != item["id"]
        or receipt["target"] != item["target"]
        or receipt["transform"] != item["transform"]
    ):
        raise ContractError(f"expansion receipt for {item['id']} belongs to a different bundle revision")
    for field in ("source_sha256", "inventory_sha256"):
        if not SHA256_RE.fullmatch(str(receipt[field])):
            raise ContractError(f"expansion receipt for {item['id']} has an invalid {field}")
    expanded = root / str(receipt["expanded_path"])
    if not expanded.exists():
        return None
    inventory_path = root / "inventories" / "sha256" / f"{receipt['inventory_sha256']}.json"
    if not inventory_path.is_file():
        return None
    return receipt


def _write_object_inventory(root: Path, document: Mapping[str, Any]) -> str:
    digest = sha256_bytes(canonical_json(document))
    path = root / "inventories" / "sha256" / f"{digest}.json"
    if path.exists():
        if sha256_bytes(canonical_json(load_json(path))) != digest:
            raise ContractError("existing reference-data inventory content is corrupt")
    else:
        atomic_json(path, document)
        path.chmod(0o444)
    return digest


def expand_object(
    root: Path,
    bundle: Mapping[str, Any],
    item: Mapping[str, Any],
    downloads: Path,
    *,
    verify_existing_blobs: bool = False,
) -> tuple[dict[str, Any], str]:
    """Localize and expand one catalog object into its own immutable tree.

    Every object owns a separate content-addressed expansion, so independent
    objects are downloaded, verified and decompressed concurrently and only the
    small aggregate promotion needs the whole-bundle lock.
    """
    blob, source_sha256, disposition = localize_source(
        root,
        bundle,
        item,
        downloads,
        verify_existing_blobs=verify_existing_blobs,
    )
    expanded = _expanded_root(root, bundle, item["id"], source_sha256)
    if not expanded.exists():
        staging_parent = root / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f"{item['id']}.", dir=staging_parent))
        try:
            materialize(blob, item, temporary)
            candidates = list(temporary.iterdir())
            relative = _safe_relative(item["target"], f"source {item['id']} target", allow_dot=True)
            promoted = temporary if relative == Path(".") else temporary / relative
            if not promoted.exists() or (relative != Path(".") and not candidates):
                raise ContractError(f"source {item['id']} produced no expanded content")
            expanded.parent.mkdir(parents=True, exist_ok=True)
            os.replace(promoted, expanded)
        finally:
            if temporary.exists():
                _remove_tree(temporary)
    entries = _object_target_paths(item, expanded)
    if not entries:
        raise ContractError(f"source {item['id']} produced no expanded files")
    files = [
        {"path": tree_relative, "bytes": size, "sha256": _hash_file(path)}
        for path, tree_relative, size in entries
    ]
    ordered, _digest, expanded_bytes = aggregate_tree_identity(files)
    inventory_sha256 = _write_object_inventory(root, {
        "schema": INVENTORY_SCHEMA,
        "bundle_id": bundle["id"],
        "revision": bundle["revision"],
        "object_id": item["id"],
        "source_sha256": source_sha256,
        "file_count": len(ordered),
        "expanded_bytes": expanded_bytes,
        "files": ordered,
    })
    receipt = {
        "schema": EXPANSION_SCHEMA,
        "bundle_id": bundle["id"],
        "revision": bundle["revision"],
        "object_id": item["id"],
        "source_sha256": source_sha256,
        "target": item["target"],
        "transform": item["transform"],
        "expanded_path": expanded.relative_to(root).as_posix(),
        "file_count": len(ordered),
        "expanded_bytes": expanded_bytes,
        "inventory_sha256": inventory_sha256,
        "recorded_at": _utc_now(),
    }
    atomic_json(_expansion_receipt_path(root, bundle, item["id"]), receipt)
    return receipt, disposition


def localize_and_expand_bundle(
    root: Path,
    bundle: Mapping[str, Any],
    downloads: Path,
    *,
    verify_existing_blobs: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Bring every object of a bundle to an expanded state, sharing the work.

    Objects are claimed with non-blocking per-object locks, so several workers
    on different nodes make progress at the same time and a worker never waits
    on an object a peer already owns.
    """
    receipts: dict[str, dict[str, Any]] = {}
    dispositions: dict[str, str] = {}
    pending = {item["id"]: item for item in bundle["objects"]}

    def claim(object_id: str, *, blocking: bool) -> bool:
        item = pending[object_id]
        lock_path = _object_lock_path(root, bundle, object_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            existing = read_expansion_receipt(root, bundle, item)
            if existing is not None:
                receipts[object_id] = existing
                dispositions[object_id] = "expanded"
            else:
                receipt, disposition = expand_object(
                    root,
                    bundle,
                    item,
                    downloads,
                    verify_existing_blobs=verify_existing_blobs,
                )
                receipts[object_id] = receipt
                dispositions[object_id] = disposition
        del pending[object_id]
        return True

    while pending:
        progressed = False
        for object_id in list(pending):
            existing = read_expansion_receipt(root, bundle, pending[object_id])
            if existing is not None:
                receipts[object_id] = existing
                dispositions[object_id] = "expanded"
                del pending[object_id]
                progressed = True
                continue
            if claim(object_id, blocking=False):
                progressed = True
        if pending and not progressed:
            # Every remaining object belongs to a peer. Wait on that peer's
            # lock rather than on a timer, so this worker resumes the instant
            # the object is published or the peer dies holding it.
            claim(next(iter(pending)), blocking=True)
    return receipts, dispositions


def _link_or_move(source: Path, destination: Path) -> None:
    """Place expanded content into the aggregate tree without copying bytes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        destination.mkdir(exist_ok=True)
        for child in sorted(source.iterdir()):
            _link_or_move(child, destination / child.name)
        return
    try:
        os.link(source, destination)
    except OSError:
        os.replace(source, destination)


def assemble_bundle_tree(
    root: Path,
    bundle: Mapping[str, Any],
    receipts: Mapping[str, Mapping[str, Any]],
    staging: Path,
) -> tuple[list[dict[str, Any]], str, int]:
    """Assemble the aggregate tree and prove its exact aggregate identity."""
    merged: list[dict[str, Any]] = []
    for item in bundle["objects"]:
        receipt = receipts[item["id"]]
        inventory = _expect_object(
            load_json(root / "inventories" / "sha256" / f"{receipt['inventory_sha256']}.json"),
            f"object inventory for {item['id']}",
        )
        if sha256_bytes(canonical_json(inventory)) != receipt["inventory_sha256"]:
            raise ContractError(f"object inventory for {item['id']} does not match its recorded digest")
        merged.extend(inventory["files"])
        expanded = root / str(receipt["expanded_path"])
        relative = _safe_relative(item["target"], f"source {item['id']} target", allow_dot=True)
        destination = staging if relative == Path(".") else staging / relative
        if relative != Path(".") and destination.exists() and not bool(item.get("overwrite", False)):
            raise ContractError(f"source {item['id']} would overwrite {relative}")
        _link_or_move(expanded, destination)
    files, tree_sha256, expanded_bytes = aggregate_tree_identity(merged)
    observed = tree_stat_summary(staging)
    if observed != [{"path": entry["path"], "bytes": entry["bytes"]} for entry in files]:
        raise ContractError("assembled reference-data tree does not match the per-object inventories")
    return files, tree_sha256, expanded_bytes


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


def _write_stage_telemetry(
    root: Path,
    bundle: Mapping[str, Any],
    manifest_sha256: str,
    duration: float,
    expanded: int,
    localization: Mapping[str, str] | None = None,
) -> None:
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
    )
    if localization is not None:
        counts = {state: 0 for state in ("downloaded", "adopted", "reused")}
        for state in localization.values():
            counts[state] = counts.get(state, 0) + 1
        metrics += (
            "# HELP fs2_reference_data_source_objects Catalog objects by how staging obtained them.\n"
            "# TYPE fs2_reference_data_source_objects gauge\n"
        )
        for state in sorted(counts):
            metrics += f'fs2_reference_data_source_objects{{{labels},disposition="{state}"}} {counts[state]}\n'
        atomic_json(
            root / "telemetry" / f"{bundle['id']}.localization.json",
            {
                "schema": PLAN_SCHEMA,
                "bundle_id": bundle["id"],
                "revision": bundle["revision"],
                "manifest_sha256": manifest_sha256,
                "recorded_at": _utc_now(),
                "dispositions": dict(sorted(localization.items())),
            },
        )
    metrics += f"# fs2_reference_data_manifest_sha256 {manifest_sha256}\n"
    atomic_text(root / "telemetry" / f"{bundle['id']}.prom", metrics)


def _published_revision(
    root: Path,
    bundle: Mapping[str, Any],
    *,
    catalog_sha256: str,
    access_sha256: str | None,
    expected_object_manifest_prefix: str | None,
    host_root: str,
    placement: Mapping[str, Any],
    verify_tree: bool,
) -> tuple[dict[str, Any], str] | None:
    """Return an already published, verified revision when one exists."""
    bundle_id = bundle["id"]
    status_path = root / "status" / f"{bundle_id}.json"
    if not status_path.is_file():
        return None
    status = _expect_object(load_json(status_path), "existing publication status")
    if status.get("revision") != bundle["revision"]:
        return None
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
    content = _expect_object(manifest.get("content"), "existing publication content")
    if "inventory_sha256" not in content or "dataset_sub_path" not in storage:
        raise ContractError(
            "this revision was published before the bounded handoff contract and has no "
            "inventory digest or dataset sub-path; run `upgrade-publication` to republish "
            "the existing immutable tree under the bounded contract without re-staging it"
        )
    expected_sub_path = (
        f"datasets/{bundle_id}/{bundle['revision']}/sha256/{manifest['content']['tree_sha256']}"
    )
    if storage.get("host_root") != host_root or storage.get("dataset_sub_path") != expected_sub_path:
        raise ContractError(
            "published manifest storage identity no longer matches the requested host root or "
            f"dataset sub-path (manifest {storage.get('host_root')!r}:"
            f"{storage.get('dataset_sub_path')!r} vs requested {host_root!r}:{expected_sub_path!r}); "
            "publish a new immutable revision instead of returning a stale handoff"
        )
    verify_manifest(manifest_path, verify_tree=verify_tree)
    expected_receipt = _terminal_receipt(
        bundle,
        manifest,
        manifest_sha256,
        host_root=host_root,
        placement=placement,
    )
    receipt_path = root / "receipts" / bundle_id / f"{bundle['revision']}.json"
    if receipt_path.is_file():
        receipt = validate_terminal_receipt(load_json(receipt_path))
        if receipt["content"]["manifest_sha256"] != manifest_sha256:
            raise ContractError("published terminal receipt does not bind the published manifest")
        # created_at is the only field allowed to differ between publications;
        # a changed host root or tfvars placement must never be served stale.
        stable = {key: value for key, value in receipt.items() if key != "created_at"}
        if stable != {key: value for key, value in expected_receipt.items() if key != "created_at"}:
            atomic_json(receipt_path, {**expected_receipt, "created_at": receipt["created_at"]})
    else:
        atomic_json(receipt_path, expected_receipt)
    return manifest, manifest_sha256


def _enter_shared_bundle_lock(lock: BinaryIO) -> None:
    """Take the bundle lock in shared mode, excluding any legacy worker.

    A worker from before per-object locking existed holds this lock
    exclusively for its whole run. Probing exclusively first proves no such
    worker is active before this worker starts claiming individual objects.
    """
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        return
    fcntl.flock(lock.fileno(), fcntl.LOCK_SH)


def stage_bundle(
    catalog_path: Path,
    bundle_id: str,
    root: Path,
    *,
    access_receipt_path: Path | None = None,
    object_store_prefix: str | None = None,
    verify_existing_blobs: bool = False,
    placement_path: Path | None = None,
    host_root: str = CANONICAL_HOST_ROOT,
) -> tuple[dict[str, Any], str]:
    started = time.monotonic()
    catalog = validate_catalog(load_json(catalog_path))
    try:
        bundle = catalog["bundles"][bundle_id]
    except KeyError as exc:
        raise ContractError(f"unknown bundle id {bundle_id}") from exc
    placement = load_placement_contract(placement_path)
    placement_stage = resolve_stage_placement(placement, "staging")
    placement_stage = {
        "resource_class": placement_stage["resource_class"],
        "pool": placement_stage["pool"],
        "node_selector": placement_stage["node_selector"],
        "tolerations": placement_stage["tolerations"],
    }
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
    published_arguments = {
        "catalog_sha256": catalog_sha256,
        "access_sha256": access_sha256,
        "expected_object_manifest_prefix": expected_object_manifest_prefix,
        "host_root": host_root,
        "placement": placement_stage,
        "verify_tree": verify_existing_blobs,
    }
    lock_path = root / "locks" / f"{bundle_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    downloads = root / "downloads" / bundle_id / bundle["revision"]
    with lock_path.open("a+b") as lock:
        # Phase 1 shares the bundle lock so peers claim individual objects
        # concurrently; only the aggregate promotion needs exclusivity.
        _enter_shared_bundle_lock(lock)
        already = _published_revision(root, bundle, **published_arguments)
        if already is not None:
            manifest, manifest_sha256 = already
            _write_stage_telemetry(
                root, bundle, manifest_sha256, time.monotonic() - started,
                manifest["content"]["expanded_bytes"],
            )
            return manifest, manifest_sha256
        expansions, localization = localize_and_expand_bundle(
            root,
            bundle,
            downloads,
            verify_existing_blobs=verify_existing_blobs,
        )
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        already = _published_revision(root, bundle, **published_arguments)
        if already is not None:
            manifest, manifest_sha256 = already
            _write_stage_telemetry(
                root, bundle, manifest_sha256, time.monotonic() - started,
                manifest["content"]["expanded_bytes"],
            )
            return manifest, manifest_sha256
        source_objects = [
            {
                "id": item["id"],
                "source_bytes": item["source_bytes"],
                "source_sha256": expansions[item["id"]]["source_sha256"],
                "target": item["target"],
                "transform": item["transform"],
            }
            for item in bundle["objects"]
        ]
        staging_parent = root / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{bundle_id}.", dir=staging_parent))
        try:
            files, tree_sha256, expanded_bytes = assemble_bundle_tree(root, bundle, expansions, staging)
            final = root / "datasets" / bundle_id / bundle["revision"] / "sha256" / tree_sha256
            dataset_sub_path = final.relative_to(root).as_posix()
            inventory = {
                "schema": INVENTORY_SCHEMA,
                "bundle_id": bundle_id,
                "revision": bundle["revision"],
                "tree_sha256": tree_sha256,
                "expanded_bytes": expanded_bytes,
                "file_count": len(files),
                "files": files,
            }
            inventory_sha256 = sha256_bytes(canonical_json(inventory))
            inventory_path = root / "inventories" / "sha256" / f"{inventory_sha256}.json"
            content: dict[str, Any] = {
                "tree_sha256": tree_sha256,
                "expanded_bytes": expanded_bytes,
                "file_count": len(files),
                "inventory_sha256": inventory_sha256,
            }
            if len(files) <= MAX_INLINE_INVENTORY_FILES:
                content["files"] = files
            stable_manifest: dict[str, Any] = {
                "schema": MANIFEST_SCHEMA,
                "bundle_id": bundle_id,
                "revision": bundle["revision"],
                "source_catalog_sha256": catalog_sha256,
                "access_receipt_sha256": access_sha256,
                "source_objects": source_objects,
                "content": content,
                "storage": {
                    "shared_filesystem_uri": final.resolve().as_uri(),
                    "object_manifest_prefix": expected_object_manifest_prefix,
                    "host_root": host_root,
                    "dataset_sub_path": dataset_sub_path,
                    "inventory_uri": inventory_path.resolve().as_uri(),
                },
            }
            if not inventory_path.exists():
                atomic_json(inventory_path, inventory)
                inventory_path.chmod(0o444)
            elif sha256_bytes(canonical_json(load_json(inventory_path))) != inventory_sha256:
                raise ContractError("existing reference-data inventory content is corrupt")
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
                for item in bundle["objects"]:
                    blob = _blob_path(root, expansions[item["id"]]["source_sha256"])
                    _aws_copy(["s3", "cp", str(blob), f"{object_prefix}/blobs/sha256/{blob.name}"])
                _aws_copy([
                    "s3", "cp", str(inventory_path),
                    f"{object_prefix}/inventories/sha256/{inventory_sha256}.json",
                ])
                _aws_copy(["s3", "cp", str(manifest_path), f"{object_prefix}/manifests/sha256/{manifest_sha256}.json"])
            receipt = _terminal_receipt(
                bundle,
                manifest,
                manifest_sha256,
                host_root=host_root,
                placement=placement_stage,
            )
            receipt_path = root / "receipts" / bundle_id / f"{bundle['revision']}.json"
            atomic_json(receipt_path, receipt)
            status = {
                "schema": "fs2-serve.nebius.ai/reference-data-status/v1",
                "bundle_id": bundle_id,
                "revision": bundle["revision"],
                "ready": True,
                "manifest_sha256": manifest_sha256,
                "tree_sha256": tree_sha256,
                "inventory_sha256": inventory_sha256,
                "receipt_sha256": sha256_bytes(canonical_json(receipt)),
                "dataset_sub_path": dataset_sub_path,
                "expanded_bytes": expanded_bytes,
                "file_count": len(files),
                "updated_at": _utc_now(),
            }
            atomic_json(root / "status" / f"{bundle_id}.json", status)
            _write_stage_telemetry(
                root,
                bundle,
                manifest_sha256,
                time.monotonic() - started,
                expanded_bytes,
                localization,
            )
            # The published tree now holds the same inodes as the per-object
            # expansions, so the working copies are redundant, not a second copy.
            expanded_revision = root / "expanded" / bundle_id / bundle["revision"]
            if expanded_revision.exists():
                _remove_tree(expanded_revision)
            return manifest, manifest_sha256
        finally:
            if staging.exists():
                _remove_tree(staging)


def _resolve_manifest_inventory(manifest: Mapping[str, Any], manifest_path: Path) -> list[dict[str, Any]]:
    """Return the full file inventory a manifest commits to.

    Small bundles keep the inventory inline. Reference databases exceed what a
    consumer may enumerate, so their inventory is a separate content-addressed
    document referenced by digest and resolved only for a full tree audit.
    """
    content = _expect_object(manifest.get("content"), "manifest content")
    inline = content.get("files")
    if isinstance(inline, list):
        return inline
    digest = str(content.get("inventory_sha256", ""))
    if not SHA256_RE.fullmatch(digest):
        raise ContractError("manifest file inventory is invalid")
    uri = str(manifest.get("storage", {}).get("inventory_uri", ""))
    parsed = parse.urlparse(uri)
    if parsed.scheme == "file":
        inventory_path = Path(parse.unquote(parsed.path))
    else:
        inventory_path = manifest_path.parents[2] / "inventories" / "sha256" / f"{digest}.json"
    inventory = _expect_object(load_json(inventory_path), "published inventory")
    if inventory.get("schema") != INVENTORY_SCHEMA:
        raise ContractError("published inventory schema is invalid")
    if sha256_bytes(canonical_json(inventory)) != digest:
        raise ContractError("published inventory does not match its recorded digest")
    if (
        inventory.get("tree_sha256") != content.get("tree_sha256")
        or inventory.get("file_count") != content.get("file_count")
        or inventory.get("expanded_bytes") != content.get("expanded_bytes")
    ):
        raise ContractError("published inventory does not bind the manifest tree identity")
    files = inventory.get("files")
    if not isinstance(files, list):
        raise ContractError("published inventory file list is invalid")
    return files


DEFAULT_RECEIPT_PLACEMENT: dict[str, Any] = {
    "resource_class": "cpu",
    "pool": "reference-cpu",
    "node_selector": {
        "capacity.fs2.nebius/pool": "reference-data",
        "capacity.fs2.nebius/type": "regular",
        "storage.fs2.nebius/reference-data": "true",
        "workload.fs2.nebius/reference-data": "true",
    },
    "tolerations": [{
        "effect": "NoSchedule",
        "key": "workload.fs2.nebius/reference-data",
        "operator": "Equal",
        "value": "true",
    }],
}


def build_terminal_receipt(
    *,
    bundle_id: str,
    revision: str,
    tree_sha256: str,
    manifest_sha256: str,
    inventory_sha256: str,
    file_count: int,
    expanded_bytes: int,
    created_at: str | None = None,
    host_root: str = CANONICAL_HOST_ROOT,
    mount_path: str = "/reference-data",
    placement: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and validate a terminal handoff receipt.

    Both the publisher and any consumer-side fixture go through this one
    function, so a consumer integration test can never drift from the shape
    the publisher actually writes.
    """
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "bundle_id": bundle_id,
        "revision": revision,
        "created_at": created_at or _utc_now(),
        "storage": {
            "host_root": host_root,
            "mount_path": mount_path,
            "dataset_sub_path": f"datasets/{bundle_id}/{revision}/sha256/{tree_sha256}",
            "read_only": True,
        },
        "content": {
            "tree_sha256": tree_sha256,
            "manifest_sha256": manifest_sha256,
            "inventory_sha256": inventory_sha256,
            "inventory_marker": ".fs2-manifest-sha256",
            "file_count": file_count,
            "expanded_bytes": expanded_bytes,
            "inline_inventory": file_count <= MAX_INLINE_INVENTORY_FILES,
        },
        "placement": dict(placement if placement is not None else DEFAULT_RECEIPT_PLACEMENT),
    }
    return validate_terminal_receipt(receipt)


def _terminal_receipt(
    bundle: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    *,
    host_root: str,
    placement: Mapping[str, Any],
) -> dict[str, Any]:
    content = _expect_object(manifest.get("content"), "manifest content")
    return build_terminal_receipt(
        bundle_id=bundle["id"],
        revision=bundle["revision"],
        tree_sha256=content["tree_sha256"],
        manifest_sha256=manifest_sha256,
        inventory_sha256=content["inventory_sha256"],
        file_count=content["file_count"],
        expanded_bytes=content["expanded_bytes"],
        host_root=host_root,
        placement=placement,
    )


def derive_database_root(receipt: Mapping[str, Any]) -> str:
    """The read-only in-container path a consumer mounts for this revision.

    Derived only from published receipt fields, so the mounted dataset path
    and its ``/sha256/<tree>`` component always agree with the tree digest.
    """
    validated = validate_terminal_receipt(receipt)
    storage = validated["storage"]
    return f"{str(storage['mount_path']).rstrip('/')}/{storage['dataset_sub_path']}"


def derive_preprocess_reference_data(
    receipt: Mapping[str, Any],
    *,
    manifest_uri: str,
) -> dict[str, Any]:
    """Transform a producer receipt into a preprocess request reference block.

    This is the consumer-side transform. The publisher never invents a
    manifest location, so the caller supplies the URI it published the
    manifest to and it must name that manifest's exact digest.
    """
    validated = validate_terminal_receipt(receipt)
    manifest_sha256 = validated["content"]["manifest_sha256"]
    if parse.urlparse(manifest_uri).scheme not in {"file", "s3"}:
        raise ContractError("reference manifest URI must use file or s3")
    if not parse.urlparse(manifest_uri).path.endswith(f"/{manifest_sha256}.json"):
        raise ContractError(
            "reference manifest URI must name the published manifest digest "
            f"{manifest_sha256}"
        )
    return {
        "bundle_id": validated["bundle_id"],
        "revision": validated["revision"],
        "manifest_uri": manifest_uri,
        "manifest_sha256": manifest_sha256,
    }


def upgrade_publication(
    catalog_path: Path,
    bundle_id: str,
    root: Path,
    *,
    access_receipt_path: Path | None = None,
    object_store_prefix: str | None = None,
    placement_path: Path | None = None,
    host_root: str = CANONICAL_HOST_ROOT,
) -> tuple[dict[str, Any], str]:
    """Republish an existing immutable tree under the bounded handoff contract.

    A revision staged before the bounded contract carries its whole file
    inventory inside the manifest and names no host root or dataset sub-path.
    The tree itself is still correct, so it is re-verified in place and only
    the manifest, inventory, marker, receipt and status are rewritten. Nothing
    is re-downloaded and nothing is re-materialized.
    """
    catalog = validate_catalog(load_json(catalog_path))
    try:
        bundle = catalog["bundles"][bundle_id]
    except KeyError as exc:
        raise ContractError(f"unknown bundle id {bundle_id}") from exc
    placement = load_placement_contract(placement_path)
    stage_placement = resolve_stage_placement(placement, "staging")
    placement_stage = {
        "resource_class": stage_placement["resource_class"],
        "pool": stage_placement["pool"],
        "node_selector": stage_placement["node_selector"],
        "tolerations": stage_placement["tolerations"],
    }
    policy = bundle["access"]["staging_policy"]
    access_sha256: str | None = None
    if access_receipt_path is not None:
        access_sha256 = validate_access_receipt(load_json(access_receipt_path), bundle)
    elif policy != "automatic-public":
        raise ContractError(f"bundle {bundle_id} requires a non-secret access/terms receipt")
    catalog_sha256 = sha256_bytes(canonical_json(catalog))
    object_prefix = object_store_prefix.rstrip("/") if object_store_prefix else None
    expected_object_manifest_prefix = f"{object_prefix}/manifests/sha256" if object_prefix else None

    lock_path = root / "locks" / f"{bundle_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        status_path = root / "status" / f"{bundle_id}.json"
        if not status_path.is_file():
            raise ContractError("there is no published revision to upgrade")
        status = _expect_object(load_json(status_path), "existing publication status")
        if status.get("revision") != bundle["revision"] or status.get("ready") is not True:
            raise ContractError("the published revision does not match this catalog revision")
        previous_sha256 = str(status.get("manifest_sha256", ""))
        if not SHA256_RE.fullmatch(previous_sha256):
            raise ContractError("existing publication status has an invalid manifest digest")
        previous_path = root / "manifests" / "sha256" / f"{previous_sha256}.json"
        previous = _expect_object(load_json(previous_path), "existing publication manifest")
        previous_storage = _expect_object(previous.get("storage"), "existing publication storage")
        if (
            previous.get("source_catalog_sha256") != catalog_sha256
            or previous.get("access_receipt_sha256") != access_sha256
        ):
            raise ContractError("published revision provenance changed; create a new immutable revision")

        parsed = parse.urlparse(str(previous_storage.get("shared_filesystem_uri", "")))
        if parsed.scheme != "file":
            raise ContractError("the published tree must be addressable as a file:/// URI")
        final = Path(parse.unquote(parsed.path))
        previous_content = _expect_object(previous.get("content"), "existing publication content")
        if not final.is_dir() or final.name != previous_content.get("tree_sha256"):
            raise ContractError("the published tree is missing or does not bind its aggregate digest")

        # Re-verify the tree in place: the upgrade must never assume content.
        files, tree_sha256, expanded_bytes = tree_inventory(final)
        if (
            tree_sha256 != previous_content["tree_sha256"]
            or expanded_bytes != previous_content["expanded_bytes"]
            or len(files) != previous_content["file_count"]
        ):
            raise ContractError("published tree aggregate identity changed; refusing to upgrade")

        dataset_sub_path = final.relative_to(root).as_posix()
        inventory = {
            "schema": INVENTORY_SCHEMA,
            "bundle_id": bundle_id,
            "revision": bundle["revision"],
            "tree_sha256": tree_sha256,
            "expanded_bytes": expanded_bytes,
            "file_count": len(files),
            "files": files,
        }
        inventory_sha256 = _write_object_inventory(root, inventory)
        inventory_path = root / "inventories" / "sha256" / f"{inventory_sha256}.json"
        content: dict[str, Any] = {
            "tree_sha256": tree_sha256,
            "expanded_bytes": expanded_bytes,
            "file_count": len(files),
            "inventory_sha256": inventory_sha256,
        }
        if len(files) <= MAX_INLINE_INVENTORY_FILES:
            content["files"] = files
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "bundle_id": bundle_id,
            "revision": bundle["revision"],
            "source_catalog_sha256": catalog_sha256,
            "access_receipt_sha256": access_sha256,
            "source_objects": previous["source_objects"],
            "content": content,
            "storage": {
                "shared_filesystem_uri": final.resolve().as_uri(),
                "object_manifest_prefix": expected_object_manifest_prefix,
                "host_root": host_root,
                "dataset_sub_path": dataset_sub_path,
                "inventory_uri": inventory_path.resolve().as_uri(),
            },
            # The publication instant is a fact about the tree, not about this
            # upgrade, so it is carried forward unchanged.
            "created_at": previous["created_at"],
        }
        manifest_sha256 = sha256_bytes(canonical_json(manifest))
        manifest_path = root / "manifests" / "sha256" / f"{manifest_sha256}.json"
        if not manifest_path.exists():
            atomic_json(manifest_path, manifest)
            manifest_path.chmod(0o444)

        marker = final / ".fs2-manifest-sha256"
        if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != manifest_sha256:
            directory_mode = final.stat().st_mode & 0o7777
            final.chmod(0o755)
            try:
                marker.unlink(missing_ok=True)
                atomic_text(marker, manifest_sha256 + "\n")
                marker.chmod(0o444)
            finally:
                final.chmod(directory_mode)
        verify_manifest(manifest_path, verify_tree=False)

        if object_prefix:
            _aws_copy([
                "s3", "cp", str(inventory_path),
                f"{object_prefix}/inventories/sha256/{inventory_sha256}.json",
            ])
            _aws_copy([
                "s3", "cp", str(manifest_path),
                f"{object_prefix}/manifests/sha256/{manifest_sha256}.json",
            ])

        receipt = _terminal_receipt(
            bundle, manifest, manifest_sha256, host_root=host_root, placement=placement_stage
        )
        atomic_json(root / "receipts" / bundle_id / f"{bundle['revision']}.json", receipt)
        atomic_json(status_path, {
            "schema": "fs2-serve.nebius.ai/reference-data-status/v1",
            "bundle_id": bundle_id,
            "revision": bundle["revision"],
            "ready": True,
            "manifest_sha256": manifest_sha256,
            "tree_sha256": tree_sha256,
            "inventory_sha256": inventory_sha256,
            "receipt_sha256": sha256_bytes(canonical_json(receipt)),
            "dataset_sub_path": dataset_sub_path,
            "expanded_bytes": expanded_bytes,
            "file_count": len(files),
            "updated_at": _utc_now(),
        })
        return manifest, manifest_sha256


def verify_manifest(manifest_path: Path, *, verify_tree: bool = True) -> tuple[dict[str, Any], str]:
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
    content = _expect_object(manifest.get("content"), "manifest content")
    for field in ("tree_sha256", "expanded_bytes", "file_count"):
        if field not in content:
            raise ContractError("manifest content identity is incomplete")
    marker = root / ".fs2-manifest-sha256"
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != manifest_sha256:
        raise ContractError("published tree readiness marker is missing or mismatched")
    if root.name != content["tree_sha256"]:
        raise ContractError("published tree path does not bind its aggregate tree digest")
    if not verify_tree:
        _resolve_manifest_inventory(manifest, manifest_path)
        return manifest, manifest_sha256
    expected_files = _resolve_manifest_inventory(manifest, manifest_path)
    observed_files, tree_sha256, expanded_bytes = tree_inventory(root)
    if observed_files != expected_files:
        raise ContractError("published tree file inventory or checksums changed")
    if tree_sha256 != content["tree_sha256"] or expanded_bytes != content["expanded_bytes"]:
        raise ContractError("published tree aggregate identity changed")
    return manifest, manifest_sha256


DEFAULT_ALPHAFOLD3_ENTRYPOINT = {
    "interpreter": "/usr/bin/python3",
    "script": "/opt/alphafold3/run_alphafold.py",
}


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
    _expect_keys(backend, backend_fields, backend_fields | {"entrypoint"}, "preprocess backend")
    if not isinstance(backend["threads"], int) or not 1 <= backend["threads"] <= 128:
        raise ContractError("preprocess backend thread count is invalid")
    if backend["threads"] > int(execution["cpu"]):
        raise ContractError(
            f"preprocess backend requests {backend['threads']} threads but only "
            f"{execution['cpu']} CPU; the search tools would oversubscribe the pool"
        )
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
    if backend_kind == "alphafold3-data":
        check_model_preprocessing_capacity(execution, "alphafold3")
    if "entrypoint" in backend:
        if backend_kind != "alphafold3-data":
            raise ContractError(
                "only the alphafold3-data backend takes an explicit entrypoint; other "
                "backends resolve their own executable from the image"
            )
        entrypoint = _expect_object(backend["entrypoint"], "preprocess backend entrypoint")
        entrypoint_fields = {"interpreter", "script"}
        _expect_keys(entrypoint, entrypoint_fields, entrypoint_fields, "preprocess backend entrypoint")
        for field in sorted(entrypoint_fields):
            value = str(entrypoint[field])
            if not value.startswith("/") or ".." in Path(value).parts:
                raise ContractError(
                    f"preprocess backend entrypoint {field} must be an absolute path without traversal"
                )
            if not re.fullmatch(r"[A-Za-z0-9._/+-]+", value):
                raise ContractError(f"preprocess backend entrypoint {field} has unsupported characters")
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
            # Runtime images place the interpreter and entrypoint differently,
            # so the request declares them rather than the executor assuming
            # one image layout. AlphaFold 3 defaults both search tools to eight
            # threads, which oversubscribes a smaller CPU pool, so the declared
            # budget is passed explicitly and inference stays on the
            # accelerator stage.
            entrypoint = backend.get("entrypoint", DEFAULT_ALPHAFOLD3_ENTRYPOINT)
            command = [
                entrypoint["interpreter"], entrypoint["script"], f"--json_path={input_path}",
                f"--db_dir={database_root}", f"--output_dir={output}", "--norun_inference",
                f"--jackhmmer_n_cpu={backend['threads']}",
                f"--nhmmer_n_cpu={backend['threads']}",
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


def _dataset_status(root: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    invalid_items = 0
    scan_errors = 0
    try:
        paths = sorted((root / "status").glob("*.json"))
    except OSError:
        paths = []
        scan_errors = 1
    for path in paths:
        try:
            document = load_json(path)
        except (OSError, json.JSONDecodeError):
            invalid_items += 1
            continue
        if not isinstance(document, dict):
            invalid_items += 1
            continue
        items.append(document)
    ready_items = sum(item.get("ready") is True for item in items)
    return {
        "ready": ready_items > 0 and scan_errors == 0,
        "ready_items": ready_items,
        "not_ready_items": len(items) - ready_items,
        "invalid_items": invalid_items,
        "scan_errors": scan_errors,
        "items": items,
    }


def _dataset_status_metrics(status: Mapping[str, Any]) -> bytes:
    lines = [
        "# HELP fs2_reference_data_dataset_ready Whether at least one immutable dataset revision is ready.",
        "# TYPE fs2_reference_data_dataset_ready gauge",
        f"fs2_reference_data_dataset_ready {int(status['ready'])}",
        "# HELP fs2_reference_data_dataset_status_items Dataset status documents by validation state.",
        "# TYPE fs2_reference_data_dataset_status_items gauge",
        f'fs2_reference_data_dataset_status_items{{state="ready"}} {status["ready_items"]}',
        f'fs2_reference_data_dataset_status_items{{state="not_ready"}} {status["not_ready_items"]}',
        f'fs2_reference_data_dataset_status_items{{state="invalid"}} {status["invalid_items"]}',
        "# HELP fs2_reference_data_dataset_scan_errors Errors while scanning the dataset status directory.",
        "# TYPE fs2_reference_data_dataset_scan_errors gauge",
        f"fs2_reference_data_dataset_scan_errors {status['scan_errors']}",
    ]
    return ("\n".join(lines) + "\n").encode()


class StatusHandler(http.server.BaseHTTPRequestHandler):
    root: Path

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if self.path == "/readyz":
            ready = _dataset_status(self.root)["ready"]
            self._send(200 if ready else 503, b"ready\n" if ready else b"not ready\n", "text/plain; charset=utf-8")
            return
        if self.path == "/metrics":
            metrics = b"".join(path.read_bytes() for path in sorted((self.root / "telemetry").glob("*.prom")))
            metrics += _preprocess_metrics(self.root)
            metrics += _dataset_status_metrics(_dataset_status(self.root))
            self._send(200, metrics, "text/plain; version=0.0.4; charset=utf-8")
            return
        if self.path == "/v1/status":
            status = _dataset_status(self.root)
            body = {
                "schema": "fs2-serve.nebius.ai/reference-data-status-list/v1",
                **status,
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
    stage.add_argument("--placement", type=Path)
    stage.add_argument("--host-root", default=CANONICAL_HOST_ROOT)
    stage.add_argument("--verify-existing-blobs", action="store_true")
    upgrade = subparsers.add_parser("upgrade-publication")
    upgrade.add_argument("--catalog", type=Path, required=True)
    upgrade.add_argument("--bundle", required=True)
    upgrade.add_argument("--root", type=Path, required=True)
    upgrade.add_argument("--access-receipt", type=Path)
    upgrade.add_argument("--object-store-prefix")
    upgrade.add_argument("--placement", type=Path)
    upgrade.add_argument("--host-root", default=CANONICAL_HOST_ROOT)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--catalog", type=Path, required=True)
    plan.add_argument("--bundle", required=True)
    plan.add_argument("--root", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--verify-tree", action="store_true")
    handoff = subparsers.add_parser("handoff")
    handoff.add_argument("--root", type=Path, required=True)
    handoff.add_argument("--bundle", required=True)
    handoff.add_argument("--revision", required=True)
    validate_handoff = subparsers.add_parser("validate-handoff")
    validate_handoff.add_argument("--receipt", type=Path, required=True)
    validate_placement = subparsers.add_parser("validate-placement")
    validate_placement.add_argument("--placement", type=Path)
    capacity = subparsers.add_parser("capacity-requirements")
    capacity.add_argument("--placement", type=Path)
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
            manifest, digest = stage_bundle(
                args.catalog,
                args.bundle,
                args.root,
                access_receipt_path=args.access_receipt,
                object_store_prefix=args.object_store_prefix,
                verify_existing_blobs=args.verify_existing_blobs,
                placement_path=args.placement,
                host_root=args.host_root,
            )
            content = manifest["content"]
            print(json.dumps({
                "ready": True,
                "manifest_sha256": digest,
                "tree_sha256": content["tree_sha256"],
                "inventory_sha256": content["inventory_sha256"],
                "file_count": content["file_count"],
                "expanded_bytes": content["expanded_bytes"],
                "dataset_sub_path": manifest["storage"]["dataset_sub_path"],
            }, sort_keys=True))
        elif args.command == "upgrade-publication":
            manifest, digest = upgrade_publication(
                args.catalog,
                args.bundle,
                args.root,
                access_receipt_path=args.access_receipt,
                object_store_prefix=args.object_store_prefix,
                placement_path=args.placement,
                host_root=args.host_root,
            )
            content = manifest["content"]
            print(json.dumps({
                "upgraded": True,
                "manifest_sha256": digest,
                "tree_sha256": content["tree_sha256"],
                "inventory_sha256": content["inventory_sha256"],
                "file_count": content["file_count"],
                "expanded_bytes": content["expanded_bytes"],
                "dataset_sub_path": manifest["storage"]["dataset_sub_path"],
            }, sort_keys=True))
        elif args.command == "plan":
            print(json.dumps(localization_plan(args.catalog, args.bundle, args.root), indent=2, sort_keys=True))
        elif args.command == "verify":
            _manifest, digest = verify_manifest(args.manifest, verify_tree=args.verify_tree)
            print(json.dumps({"valid": True, "manifest_sha256": digest}, sort_keys=True))
        elif args.command == "handoff":
            receipt_path = args.root / "receipts" / args.bundle / f"{args.revision}.json"
            print(json.dumps(validate_terminal_receipt(load_json(receipt_path)), indent=2, sort_keys=True))
        elif args.command == "validate-handoff":
            receipt = validate_terminal_receipt(load_json(args.receipt))
            print(json.dumps({
                "valid": True,
                "receipt_sha256": sha256_bytes(canonical_json(receipt)),
                "tree_sha256": receipt["content"]["tree_sha256"],
                "manifest_sha256": receipt["content"]["manifest_sha256"],
            }, sort_keys=True))
        elif args.command == "capacity-requirements":
            contract = load_placement_contract(args.placement)
            print(json.dumps({
                "schema": "fs2-serve.nebius.ai/reference-data-capacity-requirements/v1",
                "model_preprocessing_capacity": {
                    "alphafold3": model_preprocessing_capacity("alphafold3"),
                },
                "stages": [
                    stage_admissibility(contract, stage_id)
                    for stage_id in sorted(contract["stages"])
                ],
            }, indent=2, sort_keys=True))
        elif args.command == "validate-placement":
            contract = load_placement_contract(args.placement)
            print(json.dumps({
                "valid": True,
                "placement_sha256": sha256_bytes(canonical_json(contract)),
                "stages": sorted(contract["stages"]),
                "pools": sorted(contract["pools"]),
            }, sort_keys=True))
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
