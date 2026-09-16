#!/usr/bin/env python3
"""Verify the root-owned approval and signed append-only provider boundary."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REGISTRY_PATH = Path("/etc/fs2-security/customer-storage-egress-authority.json")
REGISTRY_SCHEMA = "fs2-serve.nebius.ai/customer-storage-egress-authority-registry/v1"
MANIFEST_SCHEMA = "fs2-serve.nebius.ai/customer-storage-egress-authority-ledger/v1"
MAX_BYTES = 1024 * 1024


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def safe_root_read(path: Path) -> bytes:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("authority registry path is invalid")
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_mode & 0o077
            or before.st_size <= 0
            or before.st_size > MAX_BYTES
        ):
            raise ValueError("authority registry must be a bounded root-owned private regular file")
        payload = b""
        while len(payload) <= MAX_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        if (
            len(payload) > MAX_BYTES
            or len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("authority registry changed during its descriptor-bound read")
        return payload
    finally:
        os.close(descriptor)


def strict_json(payload: bytes | str, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"{label} contains a duplicate field")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def host_routes(values: object, label: str, *, maximum: int = 64) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        raise ValueError(f"{label} must be a bounded non-empty list")
    normalized: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError(f"{label} entries must be strings")
        network = ipaddress.ip_network(raw, strict=True)
        if network.prefixlen != network.max_prefixlen:
            raise ValueError(f"{label} accepts only exact host routes")
        normalized.append(network.with_prefixlen)
    if len(normalized) != len(set(normalized)) or normalized != sorted(normalized):
        raise ValueError(f"{label} must be unique and sorted")
    return normalized


def private_routes(values: object) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        raise ValueError("private CIDRs must be a bounded non-empty list")
    normalized: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError("private CIDRs must be strings")
        network = ipaddress.ip_network(raw, strict=True)
        if not network.is_private:
            raise ValueError("private CIDRs must be non-public networks")
        normalized.append(network.with_prefixlen)
    if len(normalized) != len(set(normalized)) or normalized != sorted(normalized):
        raise ValueError("private CIDRs must be unique and sorted")
    return normalized


def verify(manifest_json: str) -> dict[str, str]:
    registry = strict_json(safe_root_read(REGISTRY_PATH), "authority registry")
    expected_registry_fields = {
        "schema",
        "authority_project_id",
        "authority_service_account_id",
        "authority_group_id",
        "authority_access_permits",
        "authority_auth_public_keys",
        "workloads_service_account_id",
        "release_service_account_ids",
        "human_principal_ids",
        "kubernetes_security_owner_username",
        "kubernetes_workloads_username",
        "kubernetes_non_owner_subjects",
        "provider_project_iam_inventory_receipt_sha256",
        "approved_manifest_sha256",
        "public_key_pem",
    }
    if set(registry) != expected_registry_fields or registry.get("schema") != REGISTRY_SCHEMA:
        raise ValueError("authority registry fields or schema differ")
    for field in (
        "authority_project_id",
        "authority_service_account_id",
        "authority_group_id",
        "workloads_service_account_id",
        "kubernetes_security_owner_username",
        "kubernetes_workloads_username",
        "provider_project_iam_inventory_receipt_sha256",
        "approved_manifest_sha256",
        "public_key_pem",
    ):
        if not isinstance(registry[field], str) or not registry[field]:
            raise ValueError("authority registry identity is incomplete")
    if (
        len(registry["provider_project_iam_inventory_receipt_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in registry["provider_project_iam_inventory_receipt_sha256"]
        )
    ):
        raise ValueError("provider project IAM inventory receipt digest is invalid")
    for field in ("release_service_account_ids", "human_principal_ids"):
        values = registry[field]
        if not isinstance(values, list) or not values or any(not isinstance(item, str) or not item for item in values):
            raise ValueError(f"authority registry {field} must enumerate every in-scope identity")
        if len(values) != len(set(values)):
            raise ValueError(f"authority registry {field} contains duplicates")
    for field, expected_fields in (
        ("authority_access_permits", {"id", "role", "resource_id"}),
        ("authority_auth_public_keys", {"id", "expires_at"}),
    ):
        values = registry[field]
        if (
            not isinstance(values, list)
            or not values
            or any(
                not isinstance(item, dict)
                or set(item) != expected_fields
                or any(not isinstance(value, str) or not value for value in item.values())
                for item in values
            )
        ):
            raise ValueError(f"authority registry {field} is incomplete")
        if len({item["id"] for item in values}) != len(values):
            raise ValueError(f"authority registry {field} contains duplicate IDs")
    kubernetes_subjects = registry["kubernetes_non_owner_subjects"]
    if (
        not isinstance(kubernetes_subjects, list)
        or not kubernetes_subjects
        or any(
            not isinstance(item, dict)
            or set(item) != {"category", "username"}
            or item.get("category") not in {"release", "human", "break-glass", "other"}
            or not isinstance(item.get("username"), str)
            or not item["username"]
            for item in kubernetes_subjects
        )
        or {item["category"] for item in kubernetes_subjects}
        != {"release", "human", "break-glass", "other"}
        or len({item["username"] for item in kubernetes_subjects})
        != len(kubernetes_subjects)
    ):
        raise ValueError("authority registry Kubernetes non-owner inventory is incomplete")
    all_kubernetes_subjects = {
        registry["kubernetes_security_owner_username"],
        registry["kubernetes_workloads_username"],
        *(item["username"] for item in kubernetes_subjects),
    }
    if len(all_kubernetes_subjects) != len(kubernetes_subjects) + 2:
        raise ValueError("authority registry Kubernetes subjects are not disjoint")
    all_subjects = {
        registry["authority_service_account_id"],
        registry["workloads_service_account_id"],
        *registry["release_service_account_ids"],
        *registry["human_principal_ids"],
    }
    expected_subject_count = 2 + len(registry["release_service_account_ids"]) + len(registry["human_principal_ids"])
    if len(all_subjects) != expected_subject_count:
        raise ValueError("authority, workloads, release and human identities must be disjoint")

    manifest = strict_json(manifest_json, "authority manifest")
    expected_manifest_fields = {
        "schema",
        "authority_project_id",
        "cluster_id",
        "network_id",
        "subnet_id",
        "node_service_account_id",
        "kubernetes_version",
        "generations",
        "current_generation",
        "payload_sha256",
        "signature",
    }
    if set(manifest) != expected_manifest_fields or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("authority manifest fields or schema differ")
    body = {key: value for key, value in manifest.items() if key not in {"payload_sha256", "signature"}}
    payload = canonical(body)
    digest = hashlib.sha256(payload).hexdigest()
    if manifest.get("payload_sha256") != digest or registry["approved_manifest_sha256"] != digest:
        raise ValueError("authority manifest is not the exact externally approved ledger")
    if manifest.get("authority_project_id") != registry["authority_project_id"]:
        raise ValueError("authority manifest project differs from the external registry")
    try:
        key = serialization.load_pem_public_key(registry["public_key_pem"].encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("authority registry key must be Ed25519")
        signature = base64.b64decode(manifest["signature"], validate=True)
        key.verify(signature, payload)
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise ValueError("authority manifest signature is invalid") from exc

    generations = manifest.get("generations")
    if not isinstance(generations, list) or not generations:
        raise ValueError("authority manifest needs at least one generation")
    normalized: dict[str, dict[str, Any]] = {}
    predecessor: str | None = None
    for entry in generations:
        fields = {
            "generation",
            "predecessor_sha256",
            "contract_sha256",
            "predecessor_compatibility_sha256",
            "provider_api_cidrs",
            "kubernetes_api_cidrs",
            "bootstrap_https_cidrs",
            "private_cidrs",
            "platform",
            "preset",
            "boot_disk_type",
            "boot_disk_gib",
        }
        if not isinstance(entry, dict) or set(entry) != fields:
            raise ValueError("authority generation fields differ")
        content = {key: value for key, value in entry.items() if key != "generation"}
        content_digest = hashlib.sha256(canonical(content)).hexdigest()
        generation = entry.get("generation")
        if (
            not isinstance(generation, str)
            or not generation.startswith("g")
            or len(generation) != 28
            or generation[-12:] != content_digest[:12]
            or generation in normalized
        ):
            raise ValueError("authority generation is not content-bound and unique")
        if entry.get("predecessor_sha256") != predecessor:
            raise ValueError("authority generation chain is incomplete or reordered")
        for field in ("contract_sha256", "predecessor_compatibility_sha256"):
            value = entry.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"authority generation {field} is invalid")
        host_routes(entry.get("provider_api_cidrs"), "provider API CIDRs")
        host_routes(entry.get("kubernetes_api_cidrs"), "Kubernetes API CIDRs")
        host_routes(entry.get("bootstrap_https_cidrs"), "bootstrap HTTPS CIDRs")
        private_routes(entry.get("private_cidrs"))
        if (
            not isinstance(entry.get("platform"), str)
            or not entry["platform"]
            or not isinstance(entry.get("preset"), str)
            or not entry["preset"]
            or entry.get("boot_disk_type") not in {"NETWORK_SSD", "NETWORK_SSD_NON_REPLICATED"}
            or not isinstance(entry.get("boot_disk_gib"), int)
            or not 32 <= entry["boot_disk_gib"] <= 256
        ):
            raise ValueError("authority generation node shape is invalid")
        normalized[generation] = entry
        predecessor = content_digest
    if manifest.get("current_generation") not in normalized:
        raise ValueError("current authority generation is absent from the signed ledger")

    return {
        "authorized": "true",
        "manifest_sha256": digest,
        "manifest_json": json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        "generations_json": json.dumps(normalized, sort_keys=True, separators=(",", ":")),
        "authority_project_id": registry["authority_project_id"],
        "authority_service_account_id": registry["authority_service_account_id"],
        "authority_group_id": registry["authority_group_id"],
        "workloads_service_account_id": registry["workloads_service_account_id"],
        "release_service_account_ids_sha256": hashlib.sha256(canonical(registry["release_service_account_ids"])).hexdigest(),
        "human_principal_ids_sha256": hashlib.sha256(canonical(registry["human_principal_ids"])).hexdigest(),
        "kubernetes_security_owner_sha256": hashlib.sha256(registry["kubernetes_security_owner_username"].encode()).hexdigest(),
        "kubernetes_workloads_sha256": hashlib.sha256(registry["kubernetes_workloads_username"].encode()).hexdigest(),
        "kubernetes_non_owner_subjects_sha256": hashlib.sha256(
            canonical(sorted(kubernetes_subjects, key=lambda item: (item["category"], item["username"])))
        ).hexdigest(),
        "provider_project_iam_inventory_receipt_sha256": registry["provider_project_iam_inventory_receipt_sha256"],
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        if not isinstance(query, dict) or set(query) != {"authority_manifest_json"}:
            raise ValueError("authority verifier query differs")
        manifest_json = query["authority_manifest_json"]
        if not isinstance(manifest_json, str) or not manifest_json:
            raise ValueError("authority manifest is absent")
        print(json.dumps(verify(manifest_json), sort_keys=True))
        return 0
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"customer-storage provider authority rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
