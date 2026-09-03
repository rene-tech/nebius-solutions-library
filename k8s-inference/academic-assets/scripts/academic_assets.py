#!/usr/bin/env python3
"""Fail-closed private ingestion for non-redistributable academic assets.

Two independent axes decide what an operator may do with these assets.

*Use authorization* is the platform-owner grant that activates the academic
proof-of-concept operational path: verify the pinned bytes, place them on a
tenant-private volume, and prove the runtime can consume them.

*Formal license acceptance* is the separate, licensor-required attestation by a
named representative who can bind a specific academic institution.  It is never
synthesized from placeholder institution metadata, so it stays truthfully
pending until such a representative supplies a real receipt.

The command emits only non-secret identities and readiness state.  Artifact
paths, credentials, and receipt bodies stay inside an owner-only state
directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.parser
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
from typing import Any


SCHEMA_PREFIX = "fs2-serve.nebius.ai"
TREE_MANIFEST_ALGORITHM = "fs2-tree-manifest/v1"
CONTRACT_SCHEMA = f"{SCHEMA_PREFIX}/academic-assets/v3"
READINESS_SCHEMA = f"{SCHEMA_PREFIX}/academic-assets-readiness/v3"
AUTHORIZATION_SCHEMA = f"{SCHEMA_PREFIX}/academic-use-authorization/v1"
ACCEPTANCE_SCHEMA = f"{SCHEMA_PREFIX}/academic-license-acceptance/v3"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OCI_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ASSET_ID_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
MAX_JSON_BYTES = 1024 * 1024

STRUCTURAL_VALIDATORS = frozenset({"zstd-stream", "python-wheel", "conda-v2", "none"})
DELIVERY_MODES = frozenset({"tenant-private-volume"})
INSTALL_MODES = frozenset({"none", "pip-wheel-to-volume"})
OFFLINE_VALIDATION_KINDS = frozenset(
    {"python-import", "zstd-stream-integrity", "official-parameter-loader"}
)
EXECUTE_OPERATION = "execute-scientific-prediction"
PERMITTED_OPERATIONS = frozenset(
    {
        "verify-artifact",
        "stage-tenant-private-volume",
        "install-to-tenant-private-volume",
        "validate-runtime",
        EXECUTE_OPERATION,
    }
)
FALLBACK_RELATIONSHIP = "independent-operational-alternative"

# Single source of truth for downstream evidence.  ``tests`` asserts that the
# published JSON Schema requires exactly these keys for every stage, which is
# what keeps the Python validator and the schema from drifting apart.
STAGE_IDENTITY_KEYS = frozenset(
    {"schema", "asset_id", "artifact_sha256", "observed_at", "tenant_id", "institution_id"}
)
STAGE_RECEIPT_KEYS: dict[str, frozenset[str]] = {
    "cache": STAGE_IDENTITY_KEYS
    | {
        "project_id",
        "region",
        "cluster_id",
        "filesystem_id",
        "volume_handle",
        "pvc_namespace",
        "pvc_name",
        "pvc_uid",
        "file_size_bytes",
        "file_mode",
        "directory_mode",
        "asset_gid",
        "verified",
        "runtime_mount_allowed",
        "general_shared_cache",
    },
    "install": STAGE_IDENTITY_KEYS
    | {
        "install_relative_path",
        "installed_distribution",
        "installed_distribution_version",
        "python_version",
        "file_count",
        "tree_manifest_algorithm",
        "tree_manifest_sha256",
        "tree_total_bytes",
        "file_mode",
        "directory_mode",
        "asset_gid",
        "world_readable",
        "atomic_promotion",
        "import_verified",
        "evidence_digest",
    },
    "runtime": STAGE_IDENTITY_KEYS
    | {
        "image_digest",
        "image_contains_licensed_bytes",
        "asset_delivery_mode",
        "offline_validation_kind",
        "network_disabled",
        "validation_passed",
        "python_version",
        "installed_distribution_version",
        "loaded_parameter_arrays",
        "loader_source_revision",
        "inference_performed",
        "predicted_atom_records",
        "predicted_structure_sha256",
        "evidence_digest",
    },
    "deployment": STAGE_IDENTITY_KEYS | {"model_id", "image_digest", "deployed", "resource_uid"},
    "semantic": STAGE_IDENTITY_KEYS | {"model_id", "image_digest", "passed", "validator_digest"},
}
STAGE_ORDER = ("cache", "install", "runtime", "deployment", "semantic")


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


def validate_exact_keys(value: dict[str, Any], keys: set[str] | frozenset[str], *, label: str) -> None:
    actual = set(value)
    if actual != set(keys):
        missing = sorted(set(keys) - actual)
        extra = sorted(actual - set(keys))
        raise IngestionError("InvalidInput", f"{label} keys differ (missing={missing}, extra={extra})")


def _require_bool(value: Any, expected: bool, *, label: str) -> None:
    if value is not expected:
        raise IngestionError("InvalidInput", f"{label} must be {json.dumps(expected)}")


def _require_nonempty_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IngestionError("InvalidInput", f"{label} must be a non-empty string")
    return value


def _validate_terms(terms: Any, *, label: str) -> None:
    if not isinstance(terms, list) or not terms:
        raise IngestionError("InvalidInput", f"{label} requires at least one terms document")
    for term in terms:
        if not isinstance(term, dict) or set(term) != {"document_id", "sha256"}:
            raise IngestionError("InvalidInput", f"{label} terms entry is invalid")
        _require_nonempty_string(term["document_id"], label=f"{label} terms document_id")
        if not isinstance(term["sha256"], str) or not SHA256_RE.fullmatch(term["sha256"]):
            raise IngestionError("InvalidInput", f"{label} terms SHA-256 is invalid")


def _validate_activation_policy(policy: Any) -> None:
    if not isinstance(policy, dict):
        raise IngestionError("InvalidInput", "activation_policy must be an object")
    validate_exact_keys(
        policy,
        {
            "operational_activation_requires",
            "formal_acceptance_requires",
            "formal_acceptance_state",
            "formal_acceptance_blocks_operational_poc",
            "institution_metadata_required_for_operational_poc",
            "general_shared_cache_allowed",
            "embed_licensed_bytes_in_images",
            "world_readable_licensed_bytes",
            "credentials_in_contracts_or_state",
            "license_gate_scope",
            "request_time_license_receipt_required",
            "rationale",
        },
        label="activation_policy",
    )
    expected_operational = [
        "UseAuthorizationGranted",
        "ArtifactVerified",
        "TenantCacheReady",
        "RuntimeReady",
    ]
    if policy["operational_activation_requires"] != expected_operational:
        raise IngestionError(
            "InvalidInput", "activation_policy operational stages must match the implemented state machine"
        )
    if not isinstance(policy["formal_acceptance_requires"], list) or not policy["formal_acceptance_requires"]:
        raise IngestionError("InvalidInput", "activation_policy must state what formal acceptance requires")
    if policy["formal_acceptance_state"] not in {"Pending", "Recorded"}:
        raise IngestionError("InvalidInput", "activation_policy formal_acceptance_state is invalid")
    _require_bool(policy["general_shared_cache_allowed"], False, label="general_shared_cache_allowed")
    _require_bool(policy["embed_licensed_bytes_in_images"], False, label="embed_licensed_bytes_in_images")
    _require_bool(policy["world_readable_licensed_bytes"], False, label="world_readable_licensed_bytes")
    # Licence terms bind ingestion and deployment once. Turning them into a
    # per-request gate would make an authorized model unusable in practice.
    if policy["license_gate_scope"] != "one-time-ingestion-and-deployment":
        raise IngestionError("InvalidInput", "licence gating must be scoped to ingestion and deployment")
    _require_bool(
        policy["request_time_license_receipt_required"], False, label="request_time_license_receipt_required"
    )
    _require_bool(policy["credentials_in_contracts_or_state"], False, label="credentials_in_contracts_or_state")
    for field in (
        "formal_acceptance_blocks_operational_poc",
        "institution_metadata_required_for_operational_poc",
    ):
        if not isinstance(policy[field], bool):
            raise IngestionError("InvalidInput", f"activation_policy {field} must be a boolean")
    _require_nonempty_string(policy["rationale"], label="activation_policy rationale")


def _validate_cache_target(target: Any, *, label: str, runtime_mount_allowed: bool) -> None:
    if not isinstance(target, dict):
        raise IngestionError("InvalidInput", f"{label} must be an object")
    # Deliberately no project, region, cluster or filesystem here: those are
    # deployment properties recorded as observed evidence, not contract content.
    for field in ("pvc_namespace", "pvc_name", "purpose"):
        _require_nonempty_string(target.get(field), label=f"{label}.{field}")
    for forbidden in ("project_id", "region", "cluster_id", "filesystem_id"):
        if forbidden in target:
            raise IngestionError(
                "InvalidInput", f"{label} must stay portable; {forbidden} belongs in observed evidence"
            )
    if target.get("runtime_mount_allowed") is not runtime_mount_allowed:
        raise IngestionError("InvalidInput", f"{label} runtime_mount_allowed must be {runtime_mount_allowed}")


def _validate_asset(asset_id: str, asset: dict[str, Any]) -> None:
    validate_exact_keys(
        asset,
        {
            "model_id",
            "backend_id",
            "display_name",
            "artifact",
            "license",
            "acceptance",
            "delivery",
            "runtime",
        },
        label=f"assets.{asset_id}",
    )
    for field in ("model_id", "backend_id", "display_name"):
        _require_nonempty_string(asset[field], label=f"assets.{asset_id}.{field}")
    if not ASSET_ID_RE.fullmatch(asset["model_id"]):
        raise IngestionError("InvalidInput", f"{asset_id} model_id is not a DNS label")

    artifact = asset["artifact"]
    if not isinstance(artifact, dict):
        raise IngestionError("InvalidInput", f"{asset_id} artifact must be an object")
    base_keys = {
        "identity_status",
        "filename",
        "version",
        "source_revision",
        "size_bytes",
        "sha256",
        "magic_hex",
        "structural_validation",
        "source_url",
        "source_metadata",
    }
    optional = {"wheel_expectations"}
    unexpected = set(artifact) - base_keys - optional
    if unexpected or not base_keys.issubset(set(artifact)):
        raise IngestionError(
            "InvalidInput",
            f"assets.{asset_id}.artifact keys differ "
            f"(missing={sorted(base_keys - set(artifact))}, extra={sorted(unexpected)})",
        )
    if artifact["identity_status"] != "pinned":
        raise IngestionError("InvalidInput", f"{asset_id} artifact identity must be pinned")
    filename = artifact["filename"]
    if not isinstance(filename, str) or Path(filename).name != filename or filename in {".", ".."}:
        raise IngestionError("InvalidInput", f"{asset_id} artifact filename is unsafe")
    if not isinstance(artifact["size_bytes"], int) or artifact["size_bytes"] <= 0:
        raise IngestionError("InvalidInput", f"{asset_id} artifact size is invalid")
    if not isinstance(artifact["sha256"], str) or not SHA256_RE.fullmatch(artifact["sha256"]):
        raise IngestionError("InvalidInput", f"{asset_id} artifact requires an exact SHA-256")
    if artifact["sha256"] == "0" * 64:
        raise IngestionError("InvalidInput", f"{asset_id} artifact SHA-256 must not be a placeholder")
    if not isinstance(artifact["magic_hex"], str) or not re.fullmatch(r"(?:[0-9a-f]{2})+", artifact["magic_hex"]):
        raise IngestionError("InvalidInput", f"{asset_id} artifact magic must be lowercase hex byte pairs")
    method = artifact["structural_validation"]
    if method not in STRUCTURAL_VALIDATORS:
        raise IngestionError("InvalidInput", f"{asset_id} structural validator is unsupported")
    if method == "python-wheel":
        expectations = artifact.get("wheel_expectations")
        if not isinstance(expectations, dict) or set(expectations) != {"distribution", "version", "tag"}:
            raise IngestionError("InvalidInput", f"{asset_id} wheel validation requires exact wheel_expectations")
        for field in ("distribution", "version", "tag"):
            _require_nonempty_string(expectations[field], label=f"{asset_id} wheel_expectations.{field}")
    elif "wheel_expectations" in artifact:
        raise IngestionError("InvalidInput", f"{asset_id} declares wheel_expectations without wheel validation")
    if not isinstance(artifact["source_metadata"], dict) or not artifact["source_metadata"]:
        raise IngestionError("InvalidInput", f"{asset_id} artifact source_metadata is empty")
    source_url = _require_nonempty_string(artifact["source_url"], label=f"{asset_id} artifact source_url")
    if not source_url.startswith("https://"):
        raise IngestionError("InvalidInput", f"{asset_id} artifact source_url must be https")

    licence = asset["license"]
    if not isinstance(licence, dict):
        raise IngestionError("InvalidInput", f"{asset_id} license must be an object")
    validate_exact_keys(
        licence,
        {"license_id", "allowed_users", "redistribution", "containerization", "access_procedure"},
        label=f"assets.{asset_id}.license",
    )
    for field, value in licence.items():
        _require_nonempty_string(value, label=f"assets.{asset_id}.license.{field}")

    acceptance = asset["acceptance"]
    if not isinstance(acceptance, dict):
        raise IngestionError("InvalidInput", f"{asset_id} acceptance must be an object")
    validate_exact_keys(
        acceptance,
        {"scope", "distribution_scope", "terms", "required_entitlements", "source_claims"},
        label=f"assets.{asset_id}.acceptance",
    )
    if acceptance["scope"] != "academic-noncommercial":
        raise IngestionError("InvalidInput", f"{asset_id} acceptance scope must be academic-noncommercial")
    if acceptance["distribution_scope"] != "tenant-institution-only":
        raise IngestionError("InvalidInput", f"{asset_id} acceptance must stay tenant-institution scoped")
    _validate_terms(acceptance["terms"], label=f"assets.{asset_id}.acceptance")
    entitlements = acceptance["required_entitlements"]
    if not isinstance(entitlements, list) or not entitlements:
        raise IngestionError("InvalidInput", f"{asset_id} requires at least one entitlement")
    for entry in entitlements:
        if not isinstance(entry, dict) or set(entry) != {"entitlement_id", "issuer", "evidence"}:
            raise IngestionError("InvalidInput", f"{asset_id} entitlement entry is invalid")
    if not isinstance(acceptance["source_claims"], dict) or not acceptance["source_claims"]:
        raise IngestionError("InvalidInput", f"{asset_id} acceptance source_claims is empty")

    delivery = asset["delivery"]
    if not isinstance(delivery, dict):
        raise IngestionError("InvalidInput", f"{asset_id} delivery must be an object")
    validate_exact_keys(
        delivery,
        {
            "mode",
            "embed_in_image",
            "mount_path",
            "install_mode",
            "install_relative_path",
            "asset_gid",
            "file_mode",
            "directory_mode",
            "asset_directory_mode",
            "volume_root_mode",
            "consumer_access",
            "runtime_consumption",
            "runtime_binding",
        },
        label=f"assets.{asset_id}.delivery",
    )
    # Licensed bytes are staged under a shared non-root group and read through a
    # supplemental group, so a runtime image running as its own uid can read them
    # without the bytes ever becoming world-readable.
    gid = delivery["asset_gid"]
    if not isinstance(gid, int) or gid <= 0 or gid > 65535:
        raise IngestionError("InvalidInput", f"{asset_id} delivery asset_gid must be a non-root group id")
    if delivery["consumer_access"] != "supplemental-group":
        raise IngestionError("InvalidInput", f"{asset_id} delivery consumer_access must be a supplemental group")
    # The asset directory is owner-writable so a new tree can be promoted atomically
    # without root and without relaxing the tree itself.
    asset_directory_mode = delivery["asset_directory_mode"]
    if not isinstance(asset_directory_mode, str) or not re.fullmatch(r"0[0-7]{3}", asset_directory_mode):
        raise IngestionError("InvalidInput", f"{asset_id} asset_directory_mode must be an octal mode string")
    asset_bits = int(asset_directory_mode, 8)
    if asset_bits & 0o007:
        raise IngestionError("InvalidInput", f"{asset_id} asset directory must not be world-accessible")
    if asset_bits & 0o020:
        raise IngestionError("InvalidInput", f"{asset_id} asset directory must not be group writable")
    if asset_bits & 0o750 != 0o750:
        raise IngestionError("InvalidInput", f"{asset_id} asset directory must be owner-writable and group-traversable")
    root_mode = delivery["volume_root_mode"]
    if not isinstance(root_mode, str) or not re.fullmatch(r"[0-7]{4}", root_mode):
        raise IngestionError("InvalidInput", f"{asset_id} volume_root_mode must be an octal mode string")
    if int(root_mode, 8) & 0o007:
        raise IngestionError("InvalidInput", f"{asset_id} volume root must not be world-accessible")

    # Onboarding addresses these objects by its own artifact IDs and paths. The
    # binding localizes the same verified bytes for that consumer without copying
    # or embedding them, so both contracts can name the object their own way.
    binding = delivery["runtime_binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "artifact_id",
        "source_sub_path",
        "consumer_path",
        "mechanism",
        "read_only",
        "duplicates_bytes",
        "embeds_bytes",
        "content_identity_kind",
        "content_manifest_algorithm",
        "content_digest_sha256",
        "size_bytes",
        "source_artifact",
        "note",
    }:
        raise IngestionError("InvalidInput", f"{asset_id} runtime_binding is invalid")

    # A directory is never labelled with the identity of the archive it came from.
    kind = binding["content_identity_kind"]
    if kind not in {"file-digest", "tree-manifest"}:
        raise IngestionError("InvalidInput", f"{asset_id} binding content identity kind is unsupported")
    expected_kind = "file-digest" if binding["mechanism"] == "subpath-file-mount" else "tree-manifest"
    if kind != expected_kind:
        raise IngestionError(
            "InvalidInput", f"{asset_id} binding identity kind does not match how it is mounted"
        )
    if kind == "file-digest":
        if binding["content_manifest_algorithm"] is not None:
            raise IngestionError("InvalidInput", f"{asset_id} a file binding has no manifest algorithm")
        if not SHA256_RE.fullmatch(str(binding["content_digest_sha256"])):
            raise IngestionError("InvalidInput", f"{asset_id} a file binding needs its exact file digest")
        if binding["content_digest_sha256"] != artifact["sha256"]:
            raise IngestionError("InvalidInput", f"{asset_id} binding digest is not the pinned artifact digest")
        if binding["size_bytes"] != artifact["size_bytes"]:
            raise IngestionError("InvalidInput", f"{asset_id} binding size is not the pinned artifact size")
    else:
        if binding["content_manifest_algorithm"] != TREE_MANIFEST_ALGORITHM:
            raise IngestionError("InvalidInput", f"{asset_id} tree binding needs the contracted manifest algorithm")
        # Observed at install time, so it must not be pinned or borrowed here.
        if binding["content_digest_sha256"] is not None or binding["size_bytes"] is not None:
            raise IngestionError(
                "InvalidInput",
                f"{asset_id} a tree binding must not carry a pinned digest or size; the installer observes them",
            )
    source_artifact = binding["source_artifact"]
    if not isinstance(source_artifact, dict) or set(source_artifact) != {"filename", "sha256", "size_bytes"}:
        raise IngestionError("InvalidInput", f"{asset_id} binding source_artifact is invalid")
    if (
        source_artifact["filename"] != artifact["filename"]
        or source_artifact["sha256"] != artifact["sha256"]
        or source_artifact["size_bytes"] != artifact["size_bytes"]
    ):
        raise IngestionError("InvalidInput", f"{asset_id} binding source artifact is not the pinned artifact")
    _require_nonempty_string(binding["artifact_id"], label=f"{asset_id} binding artifact_id")
    if binding["mechanism"] not in {"subpath-file-mount", "subpath-directory-mount"}:
        raise IngestionError("InvalidInput", f"{asset_id} binding must localize by subPath mount")
    _require_bool(binding["read_only"], True, label=f"{asset_id} binding read_only")
    _require_bool(binding["duplicates_bytes"], False, label=f"{asset_id} binding duplicates_bytes")
    _require_bool(binding["embeds_bytes"], False, label=f"{asset_id} binding embeds_bytes")
    source_sub_path = _require_nonempty_string(
        binding["source_sub_path"], label=f"{asset_id} binding source_sub_path"
    )
    if source_sub_path.startswith("/") or ".." in Path(source_sub_path).parts:
        raise IngestionError("InvalidInput", f"{asset_id} binding source_sub_path must be a safe relative path")
    # The subPath must name the object this asset actually staged.
    expected_sub_paths = {f"{asset_id}/{artifact['filename']}"}
    if delivery["install_relative_path"]:
        expected_sub_paths.add(f"{asset_id}/{delivery['install_relative_path']}")
    if source_sub_path not in expected_sub_paths:
        raise IngestionError(
            "InvalidInput", f"{asset_id} binding must point at this asset's staged object"
        )
    consumer_path = _require_nonempty_string(binding["consumer_path"], label=f"{asset_id} binding consumer_path")
    if not consumer_path.startswith("/") or ".." in Path(consumer_path).parts:
        raise IngestionError("InvalidInput", f"{asset_id} binding consumer_path must be an absolute safe path")
    if binding["mechanism"] == "subpath-file-mount" and Path(consumer_path).name != artifact["filename"]:
        raise IngestionError(
            "InvalidInput", f"{asset_id} file binding must expose the artifact under its own filename"
        )

    consumption = delivery["runtime_consumption"]
    if not isinstance(consumption, dict) or set(consumption) != {
        "mode",
        "pythonpath",
        "per_request_install",
        "request_time_license_receipt_required",
        "note",
    }:
        raise IngestionError("InvalidInput", f"{asset_id} runtime_consumption is invalid")
    if consumption["mode"] not in {"preinstalled-site-packages", "direct-parameter-mount"}:
        raise IngestionError("InvalidInput", f"{asset_id} runtime consumption mode is unsupported")
    if consumption["per_request_install"] is not False:
        raise IngestionError(
            "InvalidInput", f"{asset_id} must not install a licensed distribution per request"
        )
    if consumption["request_time_license_receipt_required"] is not False:
        raise IngestionError(
            "InvalidInput", f"{asset_id} must not demand a licence receipt on every inference request"
        )
    if consumption["mode"] == "preinstalled-site-packages":
        pythonpath = _require_nonempty_string(consumption["pythonpath"], label=f"{asset_id} pythonpath")
        expected = f"{delivery['mount_path']}/{delivery['install_relative_path']}"
        if pythonpath != expected:
            raise IngestionError("InvalidInput", f"{asset_id} PYTHONPATH must be the contracted installed tree")
    elif consumption["pythonpath"] is not None:
        raise IngestionError("InvalidInput", f"{asset_id} direct mount consumption must not set PYTHONPATH")

    for field, label in (("file_mode", "file"), ("directory_mode", "directory")):
        value = delivery[field]
        if not isinstance(value, str) or not re.fullmatch(r"0[0-7]{3}", value):
            raise IngestionError("InvalidInput", f"{asset_id} delivery {field} must be an octal mode string")
        bits = int(value, 8)
        if bits & 0o007:
            raise IngestionError(
                "InvalidInput", f"{asset_id} licensed {label} mode must not be world-readable"
            )
        if bits & 0o222:
            raise IngestionError("InvalidInput", f"{asset_id} licensed {label} mode must not be writable")
        if not bits & 0o040:
            raise IngestionError(
                "InvalidInput", f"{asset_id} licensed {label} mode must be group readable"
            )
        if label == "directory" and not bits & 0o010:
            raise IngestionError(
                "InvalidInput", f"{asset_id} licensed directory mode must be group executable to be traversable"
            )
    if delivery["mode"] not in DELIVERY_MODES:
        raise IngestionError("InvalidInput", f"{asset_id} delivery mode must mount rather than bake")
    _require_bool(delivery["embed_in_image"], False, label=f"assets.{asset_id}.delivery.embed_in_image")
    mount_path = _require_nonempty_string(delivery["mount_path"], label=f"assets.{asset_id}.delivery.mount_path")
    if not mount_path.startswith("/opt/fs2/academic/") or ".." in mount_path:
        raise IngestionError("InvalidInput", f"{asset_id} delivery mount_path is invalid")
    if delivery["install_mode"] not in INSTALL_MODES:
        raise IngestionError("InvalidInput", f"{asset_id} delivery install_mode is unsupported")
    relative = delivery["install_relative_path"]
    if delivery["install_mode"] == "none":
        if relative is not None:
            raise IngestionError("InvalidInput", f"{asset_id} delivery install path requires an install mode")
    else:
        relative = _require_nonempty_string(relative, label=f"assets.{asset_id}.delivery.install_relative_path")
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise IngestionError("InvalidInput", f"{asset_id} delivery install path must be a safe relative path")

    runtime = asset["runtime"]
    if not isinstance(runtime, dict):
        raise IngestionError("InvalidInput", f"{asset_id} runtime must be an object")
    runtime_keys = {
        "code_revision",
        "runtime_image_required",
        "runtime_image_contains_licensed_bytes",
        "offline_validation",
    }
    unexpected_runtime = set(runtime) - runtime_keys - {"runtime_image"}
    if unexpected_runtime or not runtime_keys.issubset(set(runtime)):
        raise IngestionError(
            "InvalidInput",
            f"assets.{asset_id}.runtime keys differ "
            f"(missing={sorted(runtime_keys - set(runtime))}, extra={sorted(unexpected_runtime)})",
        )
    _require_nonempty_string(runtime["code_revision"], label=f"assets.{asset_id}.runtime.code_revision")
    _require_bool(runtime["runtime_image_required"], True, label=f"assets.{asset_id}.runtime.runtime_image_required")
    _require_bool(
        runtime["runtime_image_contains_licensed_bytes"],
        False,
        label=f"assets.{asset_id}.runtime.runtime_image_contains_licensed_bytes",
    )
    offline = runtime["offline_validation"]
    if not isinstance(offline, dict) or offline.get("kind") not in OFFLINE_VALIDATION_KINDS:
        raise IngestionError("InvalidInput", f"{asset_id} offline validation kind is unsupported")
    if offline.get("network_disabled") is not True:
        raise IngestionError("InvalidInput", f"{asset_id} offline validation must disable egress")
    _require_nonempty_string(offline.get("description"), label=f"assets.{asset_id} offline description")
    if offline["kind"] == "python-import":
        for field in ("python_abi", "expect_distribution", "expect_version"):
            _require_nonempty_string(offline.get(field), label=f"assets.{asset_id} offline {field}")
    elif offline["kind"] == "official-parameter-loader":
        for field in ("source_revision", "source_tag", "expect_module", "expect_entrypoint", "model_dir_flag"):
            _require_nonempty_string(offline.get(field), label=f"assets.{asset_id} offline {field}")
        arrays = offline.get("expect_min_parameter_arrays")
        if not isinstance(arrays, int) or arrays <= 0:
            raise IngestionError("InvalidInput", f"{asset_id} loader validation needs a parameter-array floor")
        # Canonical mount: the runtime is invoked with an explicit model directory that
        # must equal the contracted mount path, so the two can never drift apart.
        if offline.get("model_dir") != delivery["mount_path"]:
            raise IngestionError(
                "InvalidInput", f"{asset_id} loader model_dir must equal the contracted mount path"
            )
        if runtime["code_revision"] != offline["source_revision"]:
            raise IngestionError(
                "InvalidInput", f"{asset_id} loader revision must be the runtime code revision"
            )
        image = runtime.get("runtime_image")
        required_image = {
            "repository",
            "tag",
            "source_tag",
            "contains_licensed_bytes",
            "build_context",
            "role",
            "final_wrapper",
        }
        optional_image = {
            "digest",
            "repository_note",
            "packaged_distribution_version",
            "expected_distribution_version",
            "identity_mismatch",
            "revalidation_required",
        }
        if (
            not isinstance(image, dict)
            or not required_image.issubset(set(image))
            or set(image) - required_image - optional_image
        ):
            raise IngestionError("InvalidInput", f"{asset_id} loader validation requires a pinned runtime image")
        # A packaged version that disagrees with the pinned tag must be declared, not
        # quietly tolerated, and must carry an explicit revalidation requirement.
        if image.get("packaged_distribution_version") != image.get("expected_distribution_version"):
            if not image.get("identity_mismatch") or image.get("revalidation_required") is not True:
                raise IngestionError(
                    "InvalidInput",
                    f"{asset_id} runtime image version disagreement must be explained and flagged for revalidation",
                )
        if image["contains_licensed_bytes"] is not False:
            raise IngestionError("InvalidInput", f"{asset_id} runtime image must not contain licensed bytes")
        if image["role"] not in {"historical-semantic-evidence", "final-runtime-wrapper"}:
            raise IngestionError("InvalidInput", f"{asset_id} runtime image role is unsupported")
        if not isinstance(image["final_wrapper"], bool):
            raise IngestionError("InvalidInput", f"{asset_id} runtime image final_wrapper must be a boolean")
        if image["final_wrapper"] != (image["role"] == "final-runtime-wrapper"):
            raise IngestionError("InvalidInput", f"{asset_id} runtime image role and final_wrapper disagree")
        if image["source_tag"] != offline["source_tag"]:
            raise IngestionError("InvalidInput", f"{asset_id} runtime image is not built from the pinned tag")
        if "digest" in image and not OCI_DIGEST_RE.fullmatch(str(image["digest"])):
            raise IngestionError("InvalidInput", f"{asset_id} runtime image digest is invalid")
    else:
        minimum = offline.get("expect_decompressed_min_bytes")
        if not isinstance(minimum, int) or minimum <= 0:
            raise IngestionError("InvalidInput", f"{asset_id} integrity validation needs a decompressed floor")


def _validate_platform_consumers(consumers: Any) -> None:
    if not isinstance(consumers, dict):
        raise IngestionError("InvalidInput", "platform_consumers must be an object")
    validate_exact_keys(
        consumers,
        {"catalog", "control_plane", "admin", "helm", "terraform"},
        label="platform_consumers",
    )
    catalog = consumers["catalog"]
    validate_exact_keys(
        catalog,
        {"contract_path", "schema_path", "activation_requires", "native_models"},
        label="platform_consumers.catalog",
    )
    for field in ("contract_path", "schema_path"):
        value = _require_nonempty_string(catalog[field], label=f"platform_consumers.catalog.{field}")
        if value.startswith("/") or ".." in Path(value).parts:
            raise IngestionError("InvalidInput", f"platform_consumers.catalog.{field} must be a repo-relative path")
    if catalog["activation_requires"] != [
        "UseAuthorizationGranted",
        "ArtifactVerified",
        "TenantCacheReady",
        "RuntimeReady",
    ]:
        raise IngestionError("InvalidInput", "catalog activation requirements must match the state machine")
    if sorted(catalog["native_models"]) != ["alphafold3", "bindcraft"]:
        raise IngestionError("InvalidInput", "catalog native models must be the two gated native models")

    control_plane = consumers["control_plane"]
    validate_exact_keys(
        control_plane,
        {"api_resource", "access_gate_profile", "receipt_storage"},
        label="platform_consumers.control_plane",
    )
    if not str(control_plane["api_resource"]).startswith("/admin/api/v1/"):
        raise IngestionError("InvalidInput", "control-plane resource must live under the operator BFF prefix")
    if control_plane["access_gate_profile"] != "academic":
        raise IngestionError("InvalidInput", "control-plane access gate profile must be academic")
    _require_nonempty_string(control_plane["receipt_storage"], label="control_plane.receipt_storage")

    admin = consumers["admin"]
    validate_exact_keys(admin, {"route", "show_fields"}, label="platform_consumers.admin")
    if not str(admin["route"]).startswith("/admin/"):
        raise IngestionError("InvalidInput", "admin route must live under /admin/")
    if not isinstance(admin["show_fields"], list) or not admin["show_fields"]:
        raise IngestionError("InvalidInput", "admin projection must expose fields")

    helm = consumers["helm"]
    validate_exact_keys(
        helm, {"values_key", "mounts_licensed_bytes", "embeds_licensed_bytes"}, label="platform_consumers.helm"
    )
    _require_nonempty_string(helm["values_key"], label="platform_consumers.helm.values_key")
    _require_bool(helm["mounts_licensed_bytes"], True, label="helm.mounts_licensed_bytes")
    _require_bool(helm["embeds_licensed_bytes"], False, label="helm.embeds_licensed_bytes")

    terraform = consumers["terraform"]
    validate_exact_keys(
        terraform, {"variable", "allowed_inputs", "forbidden_inputs"}, label="platform_consumers.terraform"
    )
    _require_nonempty_string(terraform["variable"], label="platform_consumers.terraform.variable")
    forbidden = set(terraform["forbidden_inputs"])
    if not {"artifact_bytes", "credentials", "signed_urls", "acceptance_receipt_body"}.issubset(forbidden):
        raise IngestionError("InvalidInput", "Terraform must forbid bytes, credentials, signed URLs and receipts")
    if forbidden & set(terraform["allowed_inputs"]):
        raise IngestionError("InvalidInput", "Terraform allowed and forbidden inputs overlap")


def _validate_quarantined_artifacts(quarantined: Any, asset_ids: set[str]) -> None:
    if not isinstance(quarantined, dict):
        raise IngestionError("InvalidInput", "quarantined_artifacts must be an object")
    for key, entry in quarantined.items():
        if not isinstance(entry, dict):
            raise IngestionError("InvalidInput", f"quarantined artifact {key} must be an object")
        validate_exact_keys(
            entry,
            {
                "asset_id",
                "filename",
                "version",
                "size_bytes",
                "sha256",
                "classification",
                "retention",
                "reason",
            },
            label=f"quarantined_artifacts.{key}",
        )
        if entry["asset_id"] not in asset_ids:
            raise IngestionError("InvalidInput", f"quarantined artifact {key} names an unknown asset")
        if not SHA256_RE.fullmatch(str(entry["sha256"])):
            raise IngestionError("InvalidInput", f"quarantined artifact {key} needs an exact SHA-256")
        if entry["classification"] != "rejected-incompatible":
            raise IngestionError("InvalidInput", f"quarantined artifact {key} classification is unsupported")
        if entry["retention"] != "retain-private-do-not-delete-do-not-activate":
            raise IngestionError("InvalidInput", f"quarantined artifact {key} retention policy is unsupported")
        _require_nonempty_string(entry["reason"], label=f"quarantined_artifacts.{key}.reason")


def load_contract(path: Path) -> dict[str, Any]:
    contract = load_json(path, label="academic asset contract")
    validate_exact_keys(
        contract,
        {
            "schema",
            "observed_at",
            "activation_policy",
            "environment_binding",
            "quarantine_cache",
            "runtime_cache",
            "assets",
            "quarantined_artifacts",
            "platform_consumers",
            "fallbacks",
        },
        label="contract",
    )
    if contract["schema"] != CONTRACT_SCHEMA:
        raise IngestionError("InvalidInput", "unsupported academic asset contract schema")
    validate_timestamp(contract["observed_at"], field="contract.observed_at")
    _validate_activation_policy(contract["activation_policy"])

    binding = contract["environment_binding"]
    if not isinstance(binding, dict):
        raise IngestionError("InvalidInput", "environment_binding must be an object")
    validate_exact_keys(binding, {"source", "path", "fields", "note"}, label="environment_binding")
    if binding["source"] != "generated-acceptance-state":
        raise IngestionError("InvalidInput", "environment identities must come from generated acceptance state")
    required_fields = {"project_id", "region", "cluster_id", "pvc_uid"}
    if not isinstance(binding["fields"], list) or not required_fields.issubset(set(binding["fields"])):
        raise IngestionError("InvalidInput", "environment_binding must name the observed deployment identities")
    path_value = _require_nonempty_string(binding["path"], label="environment_binding.path")
    if path_value.startswith("/") or ".." in Path(path_value).parts:
        raise IngestionError("InvalidInput", "environment_binding.path must be repo-relative")
    _require_nonempty_string(binding["note"], label="environment_binding.note")

    _validate_cache_target(contract["quarantine_cache"], label="quarantine_cache", runtime_mount_allowed=False)
    _validate_cache_target(contract["runtime_cache"], label="runtime_cache", runtime_mount_allowed=True)
    runtime_cache = contract["runtime_cache"]
    if runtime_cache.get("general_shared_cache") is not False:
        raise IngestionError("InvalidInput", "runtime cache must not be a general shared cache")
    quarantine = contract["quarantine_cache"]
    if (quarantine["pvc_namespace"], quarantine["pvc_name"]) == (
        runtime_cache["pvc_namespace"],
        runtime_cache["pvc_name"],
    ):
        raise IngestionError("InvalidInput", "runtime cache must be distinct from the historical quarantine")

    assets = contract["assets"]
    if not isinstance(assets, dict) or not assets:
        raise IngestionError("InvalidInput", "contract assets must be a non-empty object")
    for asset_id, asset in assets.items():
        if not ASSET_ID_RE.fullmatch(asset_id) or not isinstance(asset, dict):
            raise IngestionError("InvalidInput", "contract asset identity is invalid")
        _validate_asset(asset_id, asset)
    if len({asset["model_id"] for asset in assets.values()}) != len(assets):
        raise IngestionError("InvalidInput", "contract asset model IDs must be unique")

    _validate_quarantined_artifacts(contract["quarantined_artifacts"], set(assets))
    _validate_platform_consumers(contract["platform_consumers"])

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
            or fallback.get("relationship") != FALLBACK_RELATIONSHIP
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
        if not ENV_NAME_RE.fullmatch(env_name):
            raise IngestionError("InvalidInput", f"{label} environment variable name is invalid")
        raw = os.environ.get(env_name)
        if not raw:
            raise IngestionError("MissingArtifact", f"{label} environment reference is unset")
    return Path(raw).expanduser() if raw else None


def validate_use_authorization(asset_id: str, spec: dict[str, Any], path: Path) -> dict[str, Any]:
    """Validate the platform-owner proof-of-concept authorization.

    This grant activates the operational path.  It deliberately does not carry
    institution metadata or a representative signature, because it is not and
    must not be presented as the licensor-required institutional acceptance.
    """
    receipt = load_json(path, label=f"{asset_id} use authorization")
    validate_exact_keys(
        receipt,
        {
            "schema",
            "asset_id",
            "status",
            "granted_at",
            "granted_by_role",
            "authorization_id",
            "tenant_id",
            "scope",
            "provenance",
            "supersedes_formal_acceptance",
            "permitted_operations",
            "use_class",
            "non_exportable",
            "licence_references",
            "artifact_reference",
        },
        label=f"{asset_id} use authorization",
    )
    if receipt["schema"] != AUTHORIZATION_SCHEMA:
        raise IngestionError("UseAuthorizationMissing", f"{asset_id} authorization schema is unsupported")
    if receipt["asset_id"] != asset_id or receipt["status"] != "granted":
        raise IngestionError("UseAuthorizationMissing", f"{asset_id} authorization is not an affirmative grant")
    if receipt["granted_by_role"] != "platform-owner":
        raise IngestionError("UseAuthorizationMissing", f"{asset_id} authorization must come from the platform owner")
    validate_timestamp(receipt["granted_at"], field=f"{asset_id}.granted_at")
    if receipt["scope"] != spec["acceptance"]["scope"]:
        raise IngestionError("UseAuthorizationMissing", f"{asset_id} authorization scope does not match the contract")
    _require_bool(
        receipt["supersedes_formal_acceptance"], False, label=f"{asset_id} supersedes_formal_acceptance"
    )
    if receipt["use_class"] != "academic-non-commercial":
        raise IngestionError("UseAuthorizationMissing", f"{asset_id} authorization is not academic non-commercial")
    _require_bool(receipt["non_exportable"], True, label=f"{asset_id} non_exportable")

    # The authorization must name the exact licence documents it was granted
    # against, and the exact artifact it covers, so neither can drift silently.
    references = receipt["licence_references"]
    if not isinstance(references, list) or not references:
        raise IngestionError("UseAuthorizationMissing", f"{asset_id} authorization cites no licence documents")
    cited = set()
    for reference in references:
        if not isinstance(reference, dict):
            raise IngestionError("UseAuthorizationMissing", f"{asset_id} licence reference is invalid")
        if not str(reference.get("url", "")).startswith("https://"):
            raise IngestionError("UseAuthorizationMissing", f"{asset_id} licence reference needs an https source")
        if not SHA256_RE.fullmatch(str(reference.get("sha256", ""))):
            raise IngestionError("UseAuthorizationMissing", f"{asset_id} licence reference needs an exact digest")
        cited.add((reference.get("document_id"), reference["sha256"]))
    contracted = {
        (term["document_id"].split("@", 1)[0], term["sha256"]) for term in spec["acceptance"]["terms"]
    }
    if not contracted.issubset(cited):
        raise IngestionError(
            "UseAuthorizationMissing", f"{asset_id} authorization does not cite the contracted licence digests"
        )

    artifact_reference = receipt["artifact_reference"]
    if not isinstance(artifact_reference, dict):
        raise IngestionError("UseAuthorizationMissing", f"{asset_id} authorization cites no artifact")
    artifact = spec["artifact"]
    for field in ("filename", "version", "sha256"):
        if artifact_reference.get(field) != artifact[field]:
            raise IngestionError(
                "UseAuthorizationMissing", f"{asset_id} authorization {field} differs from the pinned artifact"
            )
    _require_nonempty_string(receipt["authorization_id"], label=f"{asset_id} authorization_id")
    _require_nonempty_string(receipt["tenant_id"], label=f"{asset_id} authorization tenant_id")
    _require_nonempty_string(receipt["provenance"], label=f"{asset_id} authorization provenance")
    operations = receipt["permitted_operations"]
    required_operations = {"verify-artifact", "stage-tenant-private-volume", "validate-runtime"}
    if not isinstance(operations, list) or not required_operations.issubset(set(operations)):
        raise IngestionError(
            "UseAuthorizationMissing", f"{asset_id} authorization must permit verification, staging and validation"
        )
    if not set(operations).issubset(PERMITTED_OPERATIONS):
        raise IngestionError(
            "UseAuthorizationMissing", f"{asset_id} authorization requests an operation that is never grantable"
        )
    return {
        "receipt_sha256": object_sha256(receipt),
        "granted_at": receipt["granted_at"],
        "granted_by_role": receipt["granted_by_role"],
        "authorization_id": receipt["authorization_id"],
        "tenant_id": receipt["tenant_id"],
        # Staging and validating an asset is not the same grant as running
        # tenant predictions with it, so the two are projected separately.
        "execution_authorized": EXECUTE_OPERATION in operations,
    }


def validate_acceptance(asset_id: str, spec: dict[str, Any], path: Path) -> dict[str, Any]:
    """Validate the licensor-required institutional acceptance receipt."""
    receipt = load_json(path, label=f"{asset_id} acceptance receipt")
    validate_exact_keys(
        receipt,
        {
            "schema",
            "asset_id",
            "status",
            "accepted_at",
            "tenant",
            "actor",
            "signature",
            "scope",
            "distribution_scope",
            "terms",
            "accepted_terms_sha256",
            "entitlements",
            "source_claims",
        },
        label=f"{asset_id} acceptance receipt",
    )
    if receipt["schema"] != ACCEPTANCE_SCHEMA:
        raise IngestionError("FormalAcceptancePending", f"{asset_id} acceptance schema is unsupported")
    if receipt["asset_id"] != asset_id or receipt["status"] != "accepted-by-authorized-representative":
        raise IngestionError("FormalAcceptancePending", f"{asset_id} receipt is not an authorized acceptance")
    validate_timestamp(receipt["accepted_at"], field=f"{asset_id}.accepted_at")
    tenant, actor, signature = receipt["tenant"], receipt["actor"], receipt["signature"]
    if not isinstance(tenant, dict) or not all(
        isinstance(tenant.get(key), str) and tenant[key] for key in ("tenant_id", "institution_id", "institution_name")
    ):
        raise IngestionError("FormalAcceptancePending", f"{asset_id} tenant/institution binding is incomplete")
    if (
        not isinstance(actor, dict)
        or actor.get("role") != "authorized-organization-representative"
        or not all(isinstance(actor.get(key), str) and actor[key] for key in ("actor_id", "display_name"))
    ):
        raise IngestionError("FormalAcceptancePending", f"{asset_id} named representative is required")
    if (
        not isinstance(signature, dict)
        or signature.get("type") not in {"detached-signature", "signed-attestation-document"}
        or not SHA256_RE.fullmatch(str(signature.get("sha256", "")))
    ):
        raise IngestionError("FormalAcceptancePending", f"{asset_id} signature evidence is invalid")
    acceptance = spec["acceptance"]
    if receipt["scope"] != acceptance["scope"] or receipt["distribution_scope"] != acceptance["distribution_scope"]:
        raise IngestionError("FormalAcceptancePending", f"{asset_id} scope is not tenant-bound")
    if sorted(receipt["terms"], key=lambda item: item.get("document_id", "")) != sorted(
        acceptance["terms"], key=lambda item: item["document_id"]
    ):
        raise IngestionError("FormalAcceptancePending", f"{asset_id} exact terms were not accepted")
    if not SHA256_RE.fullmatch(str(receipt["accepted_terms_sha256"])):
        raise IngestionError("FormalAcceptancePending", f"{asset_id} terms digest is invalid")
    expected = {entry["entitlement_id"] for entry in acceptance["required_entitlements"]}
    actual = {entry.get("entitlement_id") for entry in receipt["entitlements"]}
    if not expected.issubset(actual):
        raise IngestionError("FormalAcceptancePending", f"{asset_id} required entitlements are incomplete")
    if receipt["source_claims"] != acceptance["source_claims"]:
        raise IngestionError("FormalAcceptancePending", f"{asset_id} source claims do not match")
    return {
        "receipt_sha256": object_sha256(receipt),
        "accepted_at": receipt["accepted_at"],
        "tenant_id": tenant["tenant_id"],
        "institution_id": tenant["institution_id"],
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


def _validate_python_wheel(path: Path, expectations: dict[str, Any]) -> None:
    """Assert the wheel really declares the pinned distribution, version and ABI tag."""
    distribution = expectations["distribution"]
    version = expectations["version"]
    tag = expectations["tag"]
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise IngestionError("ArtifactInvalid", "wheel contains duplicate entries")
            dist_infos = sorted(
                {name.split("/", 1)[0] for name in names if name.split("/", 1)[0].endswith(".dist-info")}
            )
            if len(dist_infos) != 1:
                raise IngestionError("ArtifactInvalid", "wheel must contain exactly one .dist-info directory")
            dist_info = dist_infos[0]
            expected_dist_info = f"{distribution.replace('-', '_')}-{version}.dist-info"
            if dist_info != expected_dist_info:
                raise IngestionError(
                    "ArtifactInvalid", "wheel .dist-info does not match the pinned distribution and version"
                )
            for member in (f"{dist_info}/METADATA", f"{dist_info}/WHEEL", f"{dist_info}/RECORD"):
                if member not in names:
                    raise IngestionError("ArtifactInvalid", f"wheel lacks {member.rsplit('/', 1)[-1]}")
            parser = email.parser.BytesParser()
            metadata = parser.parsebytes(archive.read(f"{dist_info}/METADATA"), headersonly=True)
            if (metadata.get("Name") or "").strip().lower().replace("_", "-") != distribution.lower():
                raise IngestionError("ArtifactInvalid", "wheel METADATA declares a different distribution")
            if (metadata.get("Version") or "").strip() != version:
                raise IngestionError("ArtifactInvalid", "wheel METADATA declares a different version")
            wheel_metadata = parser.parsebytes(archive.read(f"{dist_info}/WHEEL"), headersonly=True)
            tags = [value.strip() for value in wheel_metadata.get_all("Tag") or []]
            if tag not in tags:
                raise IngestionError("ArtifactInvalid", "wheel does not declare the pinned interpreter/platform tag")
            if (wheel_metadata.get("Wheel-Version") or "").strip().split(".", 1)[0] != "1":
                raise IngestionError("ArtifactInvalid", "unsupported wheel format version")
    except (OSError, zipfile.BadZipFile) as exc:
        raise IngestionError("ArtifactInvalid", "artifact is not a readable Python wheel") from exc


def _validate_zstd_stream(path: Path) -> None:
    executable = shutil.which("zstd")
    if executable is None:
        raise IngestionError("ArtifactInvalid", "zstd is required for compressed parameter validation")
    result = subprocess.run(
        [executable, "--test", "--quiet", "--", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise IngestionError("ArtifactInvalid", "artifact failed zstd stream validation")


def _validate_structure(path: Path, spec: dict[str, Any]) -> None:
    method = spec["structural_validation"]
    if method == "none":
        return
    if method == "conda-v2":
        _validate_conda_v2(path)
        return
    if method == "zstd-stream":
        _validate_zstd_stream(path)
        return
    if method == "python-wheel":
        _validate_python_wheel(path, spec["wheel_expectations"])
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
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise IngestionError("ArtifactInvalid", "artifact changed while it was being copied")
        actual_digest = digest.hexdigest()
        if total != spec["size_bytes"]:
            raise IngestionError("ArtifactInvalid", "artifact size does not match the pinned contract")
        if prefix != magic:
            raise IngestionError("ArtifactInvalid", "artifact magic does not match the pinned format")
        if actual_digest != spec["sha256"]:
            raise IngestionError("ArtifactInvalid", "artifact SHA-256 does not match the pinned contract")
        os.replace(temporary, destination)
        _validate_structure(destination, spec)
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
    if not SHA256_RE.fullmatch(str(expected or "")) or object_sha256(unsigned) != expected:
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


def _validate_delivery_evidence(receipt: dict[str, Any], spec: dict[str, Any], *, label: str) -> None:
    """Observed ownership and modes must match the contracted delivery."""

    delivery = spec["delivery"]
    if receipt["asset_gid"] != delivery["asset_gid"]:
        raise IngestionError("InvalidEvidence", f"{label} group differs from the contracted asset group")
    if receipt["file_mode"] != delivery["file_mode"]:
        raise IngestionError("InvalidEvidence", f"{label} file mode differs from the contracted delivery")
    if receipt["directory_mode"] != delivery["directory_mode"]:
        raise IngestionError("InvalidEvidence", f"{label} directory mode differs from the contracted delivery")


def validate_stage_receipt(
    stage: str,
    receipt: dict[str, Any],
    *,
    asset_id: str,
    spec: dict[str, Any],
    contract: dict[str, Any],
    artifact_sha256: str,
    tenant_id: str,
    image_digest: str | None,
) -> None:
    if stage not in STAGE_RECEIPT_KEYS:
        raise IngestionError("InvalidInput", "unsupported readiness stage")
    validate_exact_keys(receipt, STAGE_RECEIPT_KEYS[stage], label=f"{stage} receipt")
    if receipt["schema"] != f"{SCHEMA_PREFIX}/academic-{stage}-receipt/v3":
        raise IngestionError("InvalidEvidence", f"{stage} receipt schema is unsupported")
    if receipt["asset_id"] != asset_id or receipt["artifact_sha256"] != artifact_sha256:
        raise IngestionError("InvalidEvidence", f"{stage} receipt is not bound to the active artifact")
    validate_timestamp(receipt["observed_at"], field=f"{stage}.observed_at")
    if receipt["tenant_id"] != tenant_id:
        raise IngestionError("InvalidEvidence", f"{stage} receipt is not bound to the authorized tenant")
    institution = receipt["institution_id"]
    if institution is not None and (not isinstance(institution, str) or not institution.strip()):
        raise IngestionError("InvalidEvidence", f"{stage} receipt institution_id must be a name or null")

    if stage == "cache":
        target = contract["runtime_cache"]
        for field in ("pvc_namespace", "pvc_name"):
            if receipt[field] != target[field]:
                raise IngestionError("InvalidEvidence", f"cache receipt {field} differs from the runtime target")
        quarantine = contract["quarantine_cache"]
        if (receipt["pvc_namespace"], receipt["pvc_name"]) == (
            quarantine["pvc_namespace"],
            quarantine["pvc_name"],
        ):
            raise IngestionError("InvalidEvidence", "the historical quarantine claim cannot be a runtime target")
        # Environment identity is observed, not contracted, so it is required to be
        # present and self-consistent rather than compared against the contract.
        for field in ("project_id", "region", "cluster_id", "pvc_uid", "volume_handle"):
            _require_nonempty_string(receipt[field], label=f"cache receipt {field}")
        # A dynamically provisioned claim has no shared filesystem identity, so this
        # stays nullable instead of being back-filled with an unrelated filesystem.
        filesystem_id = receipt["filesystem_id"]
        if filesystem_id is not None and (not isinstance(filesystem_id, str) or not filesystem_id.strip()):
            raise IngestionError("InvalidEvidence", "cache receipt filesystem_id must be a name or null")
        if receipt["verified"] is not True:
            raise IngestionError("InvalidEvidence", "cache receipt must record a verified on-cluster digest")
        if receipt["runtime_mount_allowed"] is not True:
            raise IngestionError("InvalidEvidence", "runtime cache evidence must permit runtime mounting")
        if receipt["general_shared_cache"] is not False:
            raise IngestionError("InvalidEvidence", "licensed bytes must never enter a general shared cache")
        if not isinstance(receipt["file_size_bytes"], int) or receipt["file_size_bytes"] != spec["artifact"]["size_bytes"]:
            raise IngestionError("InvalidEvidence", "cache receipt size does not match the pinned artifact")
        _validate_delivery_evidence(receipt, spec, label="cache receipt")
        return

    if stage == "install":
        delivery = spec["delivery"]
        if delivery["install_mode"] == "none":
            raise IngestionError("InvalidEvidence", "this asset has no contracted installed tree")
        if receipt["install_relative_path"] != delivery["install_relative_path"]:
            raise IngestionError("InvalidEvidence", "installed tree is not at the contracted path")
        offline = spec["runtime"]["offline_validation"]
        if receipt["installed_distribution"] != offline["expect_distribution"]:
            raise IngestionError("InvalidEvidence", "installed distribution is not the contracted one")
        if receipt["installed_distribution_version"] != offline["expect_version"]:
            raise IngestionError("InvalidEvidence", "installed distribution version is not the pinned version")
        if not str(receipt["python_version"]).startswith("3.10."):
            raise IngestionError("InvalidEvidence", "the pinned cp310 wheel requires a CPython 3.10 runtime")
        if not isinstance(receipt["file_count"], int) or receipt["file_count"] <= 0:
            raise IngestionError("InvalidEvidence", "an installed tree must contain files")
        # The installed tree is identified by its own manifest, never by the wheel.
        if receipt["tree_manifest_algorithm"] != delivery["runtime_binding"]["content_manifest_algorithm"]:
            raise IngestionError("InvalidEvidence", "installed tree manifest algorithm is not the contracted one")
        if not SHA256_RE.fullmatch(str(receipt["tree_manifest_sha256"])):
            raise IngestionError("InvalidEvidence", "installed tree manifest digest is invalid")
        if not isinstance(receipt["tree_total_bytes"], int) or receipt["tree_total_bytes"] <= 0:
            raise IngestionError("InvalidEvidence", "an installed tree must have a real byte total")
        if receipt["tree_manifest_sha256"] == spec["artifact"]["sha256"]:
            raise IngestionError(
                "InvalidEvidence", "the installed tree is labelled with the source archive digest"
            )
        if receipt["tree_total_bytes"] == spec["artifact"]["size_bytes"]:
            raise IngestionError(
                "InvalidEvidence", "the installed tree is labelled with the source archive size"
            )
        if receipt["atomic_promotion"] is not True:
            raise IngestionError("InvalidEvidence", "the installed tree must be promoted atomically")
        if receipt["import_verified"] is not True:
            raise IngestionError("InvalidEvidence", "the installed tree must be import-verified in place")
        if receipt["world_readable"] is not False:
            raise IngestionError("InvalidEvidence", "an installed licensed tree must not be world-readable")
        if not SHA256_RE.fullmatch(str(receipt["evidence_digest"])):
            raise IngestionError("InvalidEvidence", "install evidence digest is invalid")
        _validate_delivery_evidence(receipt, spec, label="install receipt")
        return

    if stage == "runtime":
        if not OCI_DIGEST_RE.fullmatch(str(receipt["image_digest"])):
            raise IngestionError("InvalidEvidence", "runtime validation image digest is invalid")
        declared_image = spec["runtime"].get("runtime_image") or {}
        declared_digest = declared_image.get("digest")
        if declared_digest and receipt["image_digest"] != declared_digest:
            raise IngestionError(
                "InvalidEvidence", "runtime evidence did not run against the pinned runtime image"
            )
        if receipt["image_contains_licensed_bytes"] is not False:
            raise IngestionError("InvalidEvidence", "the validating image must not contain licensed bytes")
        if receipt["asset_delivery_mode"] != spec["delivery"]["mode"]:
            raise IngestionError("InvalidEvidence", "runtime evidence contradicts the contracted delivery mode")
        if spec["delivery"]["embed_in_image"] is not False:
            raise IngestionError("InvalidEvidence", "this asset may not be embedded in a runtime image")
        if receipt["offline_validation_kind"] != spec["runtime"]["offline_validation"]["kind"]:
            raise IngestionError("InvalidEvidence", "runtime evidence used a different validation than contracted")
        if receipt["network_disabled"] is not True:
            raise IngestionError("InvalidEvidence", "runtime validation must run with egress disabled")
        if receipt["validation_passed"] is not True:
            raise IngestionError("InvalidEvidence", "runtime validation did not pass")
        _require_nonempty_string(receipt["python_version"], label="runtime receipt python_version")
        if not SHA256_RE.fullmatch(str(receipt["evidence_digest"])):
            raise IngestionError("InvalidEvidence", "runtime evidence digest is invalid")
        if not isinstance(receipt["inference_performed"], bool):
            raise IngestionError("InvalidEvidence", "runtime receipt must state whether inference ran")
        if receipt["inference_performed"]:
            atoms = receipt["predicted_atom_records"]
            if not isinstance(atoms, int) or atoms <= 0:
                raise IngestionError("InvalidEvidence", "a recorded prediction must contain atom records")
            if not SHA256_RE.fullmatch(str(receipt["predicted_structure_sha256"])):
                raise IngestionError("InvalidEvidence", "a recorded prediction needs a structure digest")
        elif receipt["predicted_atom_records"] is not None or receipt["predicted_structure_sha256"] is not None:
            raise IngestionError("InvalidEvidence", "prediction evidence recorded without a prediction")
        offline = spec["runtime"]["offline_validation"]
        installed = receipt["installed_distribution_version"]
        if offline["kind"] == "python-import":
            if installed != offline["expect_version"]:
                raise IngestionError("InvalidEvidence", "installed distribution version is not the pinned version")
            if not str(receipt["python_version"]).startswith("3.10."):
                raise IngestionError("InvalidEvidence", "the pinned cp310 wheel requires a CPython 3.10 runtime")
        elif offline["kind"] == "official-parameter-loader":
            if installed != spec["artifact"]["version"]:
                raise IngestionError("InvalidEvidence", "validated parameter identity is not the pinned version")
            arrays = receipt["loaded_parameter_arrays"]
            if not isinstance(arrays, int) or arrays < offline["expect_min_parameter_arrays"]:
                raise IngestionError(
                    "InvalidEvidence", "the official loader did not materialise the expected parameter arrays"
                )
            if receipt["loader_source_revision"] != offline["source_revision"]:
                raise IngestionError("InvalidEvidence", "parameter loader is not the pinned upstream revision")
        elif installed != spec["artifact"]["version"]:
            raise IngestionError("InvalidEvidence", "validated parameter identity is not the pinned version")
        return

    if stage == "deployment":
        if receipt["model_id"] != spec["model_id"] or receipt["image_digest"] != image_digest:
            raise IngestionError("InvalidEvidence", "deployment receipt is not bound to the active runtime")
        if receipt["deployed"] is not True:
            raise IngestionError("InvalidEvidence", "deployment receipt does not record a deployment")
        _require_nonempty_string(receipt["resource_uid"], label="deployment receipt resource_uid")
        return

    if receipt["model_id"] != spec["model_id"] or receipt["image_digest"] != image_digest:
        raise IngestionError("InvalidEvidence", "semantic receipt is not bound to the active runtime")
    if receipt["passed"] is not True:
        raise IngestionError("InvalidEvidence", "semantic receipt does not record a passing check")
    if not OCI_DIGEST_RE.fullmatch(str(receipt["validator_digest"])):
        raise IngestionError("InvalidEvidence", "semantic validator digest is invalid")


_STAGE_PROJECTION = {
    "cache": ("tenant_cache_status", "TenantCacheReady", "InvalidTenantCacheReceipt", "MissingInstall"),
    "install": ("install_status", "InstallReady", "InvalidInstallReceipt", "MissingRuntimeValidation"),
    "runtime": ("runtime_status", "RuntimeReady", "InvalidRuntimeReceipt", "MissingDeployment"),
    "deployment": ("deployment_status", "DeploymentReady", "InvalidDeploymentReceipt", "MissingSemanticReadiness"),
    "semantic": ("semantic_status", "SemanticReady", "InvalidSemanticReceipt", "Ready"),
}


def asset_readiness(
    contract: dict[str, Any], root: Path | None, generation: str | None, asset_id: str
) -> dict[str, Any]:
    spec = contract["assets"][asset_id]
    projection: dict[str, Any] = {
        "asset_id": asset_id,
        "model_id": spec["model_id"],
        "backend_id": spec["backend_id"],
        "display_name": spec["display_name"],
        "state": "UseAuthorizationMissing",
        "use_authorization_status": "Missing",
        "execution_authorization_status": "NotAuthorized",
        "formal_license_status": "FormalAcceptancePending",
        "artifact_status": "MissingArtifact",
        "tenant_cache_status": "MissingTenantCache",
        "install_status": "MissingInstall",
        "runtime_status": "MissingRuntimeValidation",
        "deployment_status": "MissingDeployment",
        "semantic_status": "MissingSemanticReadiness",
        "serving_admission": "PendingRuntimeReadiness",
        "delivery_mode": spec["delivery"]["mode"],
        "embed_in_image": spec["delivery"]["embed_in_image"],
        "artifact_sha256": None,
        "runtime_image_digest": None,
        "runtime_environment_digest": None,
        "binding_content_identity_kind": spec["delivery"]["runtime_binding"]["content_identity_kind"],
        "binding_content_digest_sha256": spec["delivery"]["runtime_binding"]["content_digest_sha256"],
        "binding_content_bytes": spec["delivery"]["runtime_binding"]["size_bytes"],
        "authorization_receipt_sha256": None,
        "acceptance_receipt_sha256": None,
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

    # The formal axis is reported independently and never gates the PoC path.
    acceptance = item.get("license_acceptance")
    if acceptance is not None:
        projection["formal_license_status"] = "FormalAcceptanceRecorded"
        projection["acceptance_receipt_sha256"] = acceptance.get("receipt_sha256")

    # Artifact presence is its own axis: a verified but unauthorized artifact is
    # reported truthfully as staged while the state stays blocked on the grant.
    artifact = item.get("artifact")
    if artifact is not None:
        projection["artifact_status"] = "ArtifactVerified"
        projection["artifact_sha256"] = artifact["sha256"]

    authorization = item.get("use_authorization")
    if authorization is None:
        return projection
    projection["use_authorization_status"] = "Granted"
    projection["execution_authorization_status"] = (
        "Authorized" if authorization.get("execution_authorized") else "NotAuthorized"
    )
    projection["authorization_receipt_sha256"] = authorization.get("receipt_sha256")
    if artifact is None:
        projection["state"] = "MissingArtifact"
        return projection
    projection["state"] = "MissingTenantCache"

    image_digest: str | None = None
    for stage in STAGE_ORDER:
        field, ready_value, invalid_state, next_state = _STAGE_PROJECTION[stage]
        # An asset with no contracted installed tree skips the install stage rather
        # than waiting forever for evidence that can never exist.
        if stage == "install" and spec["delivery"]["install_mode"] == "none":
            projection["install_status"] = "NotApplicable"
            projection["state"] = next_state
            continue
        path = receipt_path(root, generation, asset_id, stage)
        if not path.exists():
            return projection
        try:
            receipt = load_json(path, label=f"{asset_id} {stage} receipt")
            validate_stage_receipt(
                stage,
                receipt,
                asset_id=asset_id,
                spec=spec,
                contract=contract,
                artifact_sha256=artifact["sha256"],
                tenant_id=authorization["tenant_id"],
                image_digest=image_digest,
            )
        except IngestionError:
            projection[field] = "InvalidEvidence"
            projection["state"] = invalid_state
            return projection
        projection[field] = ready_value
        if stage == "install":
            # A tree binding has no pinned identity; the observed one comes from here.
            projection["binding_content_digest_sha256"] = receipt["tree_manifest_sha256"]
            projection["binding_content_bytes"] = receipt["tree_total_bytes"]
        if stage == "runtime" and projection["runtime_status"] == "RuntimeReady":
            pass
        if stage == "runtime":
            image_digest = receipt["image_digest"]
            # The image that ran the validation is not automatically a published
            # runtime image. Report it as the validation environment, and only
            # populate runtime_image_digest when the contract declares a published
            # image for this asset.
            projection["runtime_environment_digest"] = image_digest
            declared_image = spec["runtime"].get("runtime_image") or {}
            if declared_image.get("digest") and declared_image.get("final_wrapper") is True:
                projection["runtime_image_digest"] = image_digest
            # The runtime proof is real, but a runtime image whose packaged identity
            # disagrees with its pinned tag is not a finished runtime. Report the
            # passing evidence and hold short of RuntimeReady until the rebuilt image
            # repeats the test, rather than overstating readiness.
            image = spec["runtime"].get("runtime_image") or {}
            if image.get("revalidation_required") is True:
                projection["runtime_status"] = "RuntimeSemanticPassedImageRebuildPending"
                projection["state"] = "ImageRebuildPending"
                return projection
        projection["state"] = next_state
        if stage == "runtime":
            # Operationally usable: the authorized asset is mounted and proven, and
            # no caller-supplied licence receipt is required on an inference request.
            projection["serving_admission"] = "AdmittedNoPerRequestLicenseReceipt"
    return projection


def readiness_projection(contract: dict[str, Any], root: Path | None, generation: str | None) -> dict[str, Any]:
    assets = [asset_readiness(contract, root, generation, asset_id) for asset_id in sorted(contract["assets"])]
    operational = [item for item in assets if item["runtime_status"] == "RuntimeReady"]
    return {
        "schema": READINESS_SCHEMA,
        "generation": generation,
        "state": "Ready" if assets and all(item["state"] == "Ready" for item in assets) else "Blocked",
        "runtime_path_state": "Ready" if assets and len(operational) == len(assets) else "Blocked",
        "formal_license_state": (
            "Recorded"
            if assets and all(item["formal_license_status"] == "FormalAcceptanceRecorded" for item in assets)
            else "Pending"
        ),
        "assets": assets,
        "fallbacks": [
            {
                "model_id": fallback["model_id"],
                "state": "IndependentAlternative",
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
            authorization_path = resolve_env_or_path(
                getattr(args, f"{dest}_authorization", None),
                getattr(args, f"{dest}_authorization_env", None),
                label=f"{asset_id} use authorization",
            )
            acceptance_path = resolve_env_or_path(
                getattr(args, f"{dest}_acceptance", None),
                getattr(args, f"{dest}_acceptance_env", None),
                label=f"{asset_id} acceptance receipt",
            )
            item: dict[str, Any] = {"use_authorization": None, "license_acceptance": None, "artifact": None}
            if authorization_path is not None:
                item["use_authorization"] = validate_use_authorization(asset_id, spec, authorization_path)
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
            "schema": f"{SCHEMA_PREFIX}/academic-assets-generation/v2",
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
    if item.get("use_authorization") is None:
        raise IngestionError("UseAuthorizationMissing", "readiness evidence requires an authorized use grant")

    spec = contract["assets"][args.asset_id]
    index = STAGE_ORDER.index(args.stage)
    for earlier in STAGE_ORDER[:index]:
        if earlier == "install" and spec["delivery"]["install_mode"] == "none":
            continue
        if not receipt_path(root, generation, args.asset_id, earlier).exists():
            raise IngestionError("InvalidEvidence", f"{earlier} readiness must be recorded before {args.stage}")
    image_digest = None
    runtime_file = receipt_path(root, generation, args.asset_id, "runtime")
    if runtime_file.exists():
        image_digest = load_json(runtime_file, label=f"{args.asset_id} runtime receipt").get("image_digest")

    receipt = load_json(args.receipt, label=f"{args.stage} receipt")
    validate_stage_receipt(
        args.stage,
        receipt,
        asset_id=args.asset_id,
        spec=spec,
        contract=contract,
        artifact_sha256=item["artifact"]["sha256"],
        tenant_id=item["use_authorization"]["tenant_id"],
        image_digest=image_digest,
    )
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
    # Write the tombstone before destroying the payload so a crash can never
    # leave a silently missing generation with no recorded reason.
    atomic_write_json(tombstone_path, tombstone)
    shutil.rmtree(directory)
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
    spec = contract["assets"][args.asset_id]
    item = manifest["assets"].get(args.asset_id)
    if not item or not item.get("artifact"):
        raise IngestionError("MissingArtifact", "asset is not verified in this generation")
    authorization = item.get("use_authorization")
    if authorization is None:
        raise IngestionError("UseAuthorizationMissing", "artifact is quarantined pending an authorized use grant")
    if args.for_image_embedding:
        raise IngestionError(
            "InvalidInput",
            "licensed bytes are delivered by tenant-private volume mount and are never embedded in an image",
        )
    if args.for_tenant_volume and spec["delivery"]["mode"] != "tenant-private-volume":
        raise IngestionError("InvalidInput", "this asset is not contracted for tenant-private volume delivery")
    relative = Path(item["artifact"]["relative_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise IngestionError("InvalidState", "staged artifact path is unsafe")
    path = generation_path(root, generation) / relative
    if not path.is_file() or path.is_symlink():
        raise IngestionError("InvalidState", "staged artifact is missing or unsafe")
    return {
        "generation": generation,
        "asset_id": args.asset_id,
        "sha256": item["artifact"]["sha256"],
        "serving_admission": "PendingRuntimeReadiness",
        "delivery_mode": spec["delivery"]["mode"],
        "mount_path": spec["delivery"]["mount_path"],
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
        ingest_parser.add_argument(f"--{asset_id}-authorization", dest=f"{dest}_authorization")
        ingest_parser.add_argument(f"--{asset_id}-authorization-env", dest=f"{dest}_authorization_env")
        ingest_parser.add_argument(f"--{asset_id}-acceptance", dest=f"{dest}_acceptance")
        ingest_parser.add_argument(f"--{asset_id}-acceptance-env", dest=f"{dest}_acceptance_env")

    record_parser = subparsers.add_parser("record", help="record immutable downstream readiness evidence")
    add_state_arguments(record_parser)
    record_parser.add_argument("--generation")
    record_parser.add_argument("--asset-id", required=True)
    record_parser.add_argument("--stage", choices=list(STAGE_ORDER), required=True)
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
    resolve_parser.add_argument("--for-tenant-volume", action="store_true")
    resolve_parser.add_argument("--for-image-embedding", action="store_true")
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
            if not ENV_NAME_RE.fullmatch(env_name):
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
