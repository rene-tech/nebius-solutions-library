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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REGISTRY_PATH = Path("/etc/fs2-security-ro/authority/customer-storage-egress-authority.json")
PRIOR_HEAD_PATH = Path(
    "/var/lib/fs2-security-checkpoints-ro/customer-storage-egress-prior-head.json"
)
REGISTRY_SCHEMA = "fs2-serve.nebius.ai/customer-storage-egress-authority-registry/v2"
PRIOR_HEAD_SCHEMA = "fs2-serve.nebius.ai/customer-storage-egress-prior-head/v1"
MANIFEST_SCHEMA = "fs2-serve.nebius.ai/customer-storage-egress-authority-ledger/v2"
MAX_BYTES = 1024 * 1024
NEBIUS_TERRAFORM_PROVIDER_VERSION = "0.5.232"


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def safe_root_read_with_identity(path: Path) -> tuple[bytes, tuple[int, int]]:
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
        filesystem = os.fstatvfs(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_mode & 0o077
            or before.st_size <= 0
            or before.st_size > MAX_BYTES
            or not filesystem.f_flag & getattr(os, "ST_RDONLY", 1)
        ):
            raise ValueError(
                "authority input must be a bounded root-owned private file on a read-only filesystem"
            )
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
        return payload, (before.st_dev, before.st_ino)
    finally:
        os.close(descriptor)


def safe_root_read(path: Path) -> bytes:
    return safe_root_read_with_identity(path)[0]


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


def digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} is not a SHA-256 digest")
    return value


def require_fresh_timestamp(value: object, label: str) -> None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} timestamp is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp has no timezone")
    parsed = parsed.astimezone(UTC)
    now = datetime.now(UTC)
    if parsed > now or now - parsed > timedelta(hours=24):
        raise ValueError(f"{label} receipt is future-dated or stale")


def verify_signed_object(
    value: dict[str, Any],
    *,
    public_key_pem: str,
    digest_field: str = "payload_sha256",
) -> str:
    body = {key: item for key, item in value.items() if key not in {digest_field, "signature"}}
    payload = canonical(body)
    payload_digest = hashlib.sha256(payload).hexdigest()
    if value.get(digest_field) != payload_digest:
        raise ValueError("signed authority object payload digest differs")
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("authority key must be Ed25519")
        signature = base64.b64decode(value["signature"], validate=True)
        key.verify(signature, payload)
    except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
        raise ValueError("signed authority object signature is invalid") from exc
    return payload_digest


def verify(manifest_json: str) -> dict[str, str]:
    registry_payload, registry_identity = safe_root_read_with_identity(REGISTRY_PATH)
    prior_payload, prior_identity = safe_root_read_with_identity(PRIOR_HEAD_PATH)
    if registry_identity[0] == prior_identity[0]:
        raise ValueError("authority approval and prior head must use distinct read-only filesystems")
    registry = strict_json(registry_payload, "authority registry")
    prior_head = strict_json(prior_payload, "authority prior head")
    expected_registry_fields = {
        "schema",
        "authority_project_id",
        "authority_service_account_id",
        "authority_group_id",
        "authority_access_permits",
        "authority_auth_public_keys",
        "workloads_service_account_id",
        "kubernetes_identity_inventory",
        "kubernetes_rbac_inventory_receipt",
        "provider_project_iam_inventory_receipt",
        "accepted_custody",
        "approved_manifest_sha256",
        "manifest_public_key_pem",
        "checkpoint_public_key_pem",
    }
    if set(registry) != expected_registry_fields or registry.get("schema") != REGISTRY_SCHEMA:
        raise ValueError("authority registry fields or schema differ")
    for field in (
        "authority_project_id",
        "authority_service_account_id",
        "authority_group_id",
        "workloads_service_account_id",
        "approved_manifest_sha256",
        "manifest_public_key_pem",
        "checkpoint_public_key_pem",
    ):
        if not isinstance(registry[field], str) or not registry[field]:
            raise ValueError("authority registry identity is incomplete")
    digest(registry["approved_manifest_sha256"], "approved manifest digest")
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
    kubernetes_subjects = registry["kubernetes_identity_inventory"]
    if (
        not isinstance(kubernetes_subjects, list)
        or len(kubernetes_subjects) < 6
        or any(
            not isinstance(item, dict)
            or set(item)
            != {
                "name",
                "category",
                "username",
                "credential_sha256",
                "provider_principal_id",
            }
            or item.get("category")
            not in {"owner", "workloads", "release", "human", "break-glass", "other"}
            or any(
                not isinstance(item.get(field), str) or not item[field]
                for field in ("name", "username", "provider_principal_id")
            )
            or not isinstance(item.get("credential_sha256"), str)
            or len(item["credential_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in item["credential_sha256"])
            for item in kubernetes_subjects
        )
        or {item["category"] for item in kubernetes_subjects}
        != {"owner", "workloads", "release", "human", "break-glass", "other"}
        or sum(item["category"] == "owner" for item in kubernetes_subjects) != 1
        or sum(item["category"] == "workloads" for item in kubernetes_subjects) != 1
        or len({item["name"] for item in kubernetes_subjects}) != len(kubernetes_subjects)
        or len({item["username"] for item in kubernetes_subjects})
        != len(kubernetes_subjects)
        or len({item["credential_sha256"] for item in kubernetes_subjects})
        != len(kubernetes_subjects)
        or len({item["provider_principal_id"] for item in kubernetes_subjects})
        != len(kubernetes_subjects)
    ):
        raise ValueError("authority registry Kubernetes identity inventory is incomplete")
    owner_identity = next(item for item in kubernetes_subjects if item["category"] == "owner")
    workloads_identity = next(
        item for item in kubernetes_subjects if item["category"] == "workloads"
    )
    if owner_identity["provider_principal_id"] != registry["authority_service_account_id"]:
        raise ValueError("Kubernetes owner is not bound to the provider authority principal")
    if workloads_identity["provider_principal_id"] != registry["workloads_service_account_id"]:
        raise ValueError("Kubernetes workloads identity is not bound to its provider principal")

    iam_receipt = registry["provider_project_iam_inventory_receipt"]
    if not isinstance(iam_receipt, dict) or set(iam_receipt) != {
        "schema",
        "project_id",
        "inventory",
        "cluster_access_principal_ids",
        "mutating_principal_ids",
        "observed_at",
        "payload_sha256",
        "signature",
    }:
        raise ValueError("provider project IAM inventory receipt is absent")
    iam_receipt_sha256 = verify_signed_object(
        iam_receipt, public_key_pem=registry["checkpoint_public_key_pem"]
    )
    if iam_receipt.get("schema") != "fs2-serve.nebius.ai/provider-project-iam-inventory/v1":
        raise ValueError("provider project IAM inventory receipt schema differs")
    if iam_receipt.get("project_id") != registry["authority_project_id"]:
        raise ValueError("provider project IAM inventory receipt project differs")
    require_fresh_timestamp(iam_receipt.get("observed_at"), "provider IAM inventory")
    if not isinstance(iam_receipt.get("inventory"), dict):
        raise ValueError("provider project IAM inventory receipt has no exact inventory")
    inventory = iam_receipt["inventory"]
    if set(inventory) != {"groups", "service_accounts", "access_permits"}:
        raise ValueError("provider project IAM inventory fields differ")
    provider_inventory_ids = {
        str(item.get("id"))
        for field in ("groups", "service_accounts")
        for item in inventory[field]
        if isinstance(item, dict) and item.get("id")
    }
    provider_inventory_ids.update(
        str(member)
        for group in inventory["groups"]
        if isinstance(group, dict)
        for member in group.get("members", [])
    )
    if not {item["provider_principal_id"] for item in kubernetes_subjects}.issubset(
        provider_inventory_ids
    ):
        raise ValueError("Kubernetes identity inventory contains a principal absent from provider IAM")
    declared_provider_principals = sorted(
        item["provider_principal_id"] for item in kubernetes_subjects
    )
    if iam_receipt.get("cluster_access_principal_ids") != declared_provider_principals:
        raise ValueError(
            "Kubernetes identity inventory is not the exact provider-approved cluster access set"
        )
    if iam_receipt.get("mutating_principal_ids") != [registry["authority_group_id"]]:
        raise ValueError(
            "provider IAM receipt does not confine mutations to the external authority group"
        )

    rbac_receipt = registry["kubernetes_rbac_inventory_receipt"]
    if not isinstance(rbac_receipt, dict):
        raise ValueError("Kubernetes RBAC inventory receipt is absent")
    rbac_receipt_sha256 = verify_signed_object(
        rbac_receipt, public_key_pem=registry["checkpoint_public_key_pem"]
    )
    if set(rbac_receipt) != {
        "schema",
        "cluster_id",
        "inventory_sha256",
        "observed_at",
        "payload_sha256",
        "signature",
    } or rbac_receipt.get("schema") != (
        "fs2-serve.nebius.ai/kubernetes-rbac-inventory/v1"
    ):
        raise ValueError("Kubernetes RBAC inventory receipt fields or schema differ")
    digest(rbac_receipt.get("inventory_sha256"), "Kubernetes RBAC inventory")
    require_fresh_timestamp(rbac_receipt.get("observed_at"), "Kubernetes RBAC inventory")

    custody = registry["accepted_custody"]
    if not isinstance(custody, dict) or set(custody) != {
        "sai10_commit",
        "sai10_tree",
        "independent_review_receipt_sha256",
    }:
        raise ValueError("accepted SAI-10 custody is incomplete")
    for field in ("sai10_commit", "sai10_tree"):
        if not isinstance(custody[field], str) or len(custody[field]) != 40:
            raise ValueError("accepted SAI-10 Git identity is invalid")
    digest(
        custody["independent_review_receipt_sha256"],
        "accepted SAI-10 review receipt",
    )
    if custody["sai10_commit"].startswith("1ae009b85"):
        raise ValueError("rejected SAI-10 candidate cannot authorize customer storage")

    expected_prior_fields = {
        "schema",
        "authority_project_id",
        "head_manifest_sha256",
        "live_custody",
        "live_custody_sha256",
        "observed_at",
        "payload_sha256",
        "signature",
    }
    if set(prior_head) != expected_prior_fields or prior_head.get("schema") != PRIOR_HEAD_SCHEMA:
        raise ValueError("authority prior-head fields or schema differ")
    prior_head_sha256 = verify_signed_object(
        prior_head, public_key_pem=registry["checkpoint_public_key_pem"]
    )
    if prior_head.get("authority_project_id") != registry["authority_project_id"]:
        raise ValueError("authority prior head project differs")
    digest(prior_head.get("head_manifest_sha256"), "prior manifest head")
    digest(prior_head.get("live_custody_sha256"), "prior live custody")
    live_custody = prior_head.get("live_custody")
    expected_custody_fields = {
        "workloads_backend_id_sha256",
        "workloads_state_lineage",
        "workloads_state_serial",
        "managed_addresses",
        "predecessor_compatibility_sha256",
    }
    if (
        not isinstance(live_custody, dict)
        or set(live_custody) != expected_custody_fields
        or not isinstance(live_custody.get("workloads_state_lineage"), str)
        or not live_custody["workloads_state_lineage"]
        or not isinstance(live_custody.get("workloads_state_serial"), int)
        or live_custody["workloads_state_serial"] < 1
        or live_custody.get("managed_addresses")
        != [
            "kubernetes_config_map_v1.customer_storage_egress_contract[0]",
            "kubernetes_manifest.customer_storage_egress_admission_binding[0]",
            "kubernetes_manifest.customer_storage_egress_admission_policy[0]",
        ]
    ):
        raise ValueError("prior live custody does not retain the exact workloads state lineage")
    digest(live_custody["workloads_backend_id_sha256"], "workloads backend identity")
    digest(
        live_custody["predecessor_compatibility_sha256"],
        "custodied predecessor compatibility",
    )
    if hashlib.sha256(canonical(live_custody)).hexdigest() != prior_head["live_custody_sha256"]:
        raise ValueError("prior live custody digest differs from its signed content")
    try:
        observed_at = datetime.fromisoformat(
            str(prior_head["observed_at"]).replace("Z", "+00:00")
        ).astimezone(UTC)
    except ValueError as exc:
        raise ValueError("authority prior-head timestamp is not RFC3339") from exc
    if observed_at > datetime.now(UTC):
        raise ValueError("authority prior-head timestamp is in the future")

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
        "parent_manifest_sha256",
        "prior_live_custody_sha256",
        "nebius_terraform_provider_version",
        "accepted_custody",
        "provider_project_iam_inventory_receipt_sha256",
        "kubernetes_rbac_inventory_receipt_sha256",
        "payload_sha256",
        "signature",
    }
    if set(manifest) != expected_manifest_fields or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("authority manifest fields or schema differ")
    manifest_digest = verify_signed_object(
        manifest, public_key_pem=registry["manifest_public_key_pem"]
    )
    if registry["approved_manifest_sha256"] != manifest_digest:
        raise ValueError("authority manifest is not the exact externally approved ledger")
    if manifest.get("authority_project_id") != registry["authority_project_id"]:
        raise ValueError("authority manifest project differs from the external registry")
    if not isinstance(manifest.get("cluster_id"), str) or not manifest["cluster_id"]:
        raise ValueError("customer-storage authority requires the exact target cluster")
    if manifest.get("parent_manifest_sha256") != prior_head["head_manifest_sha256"]:
        raise ValueError("authority manifest does not extend the separately anchored prior head")
    if manifest.get("prior_live_custody_sha256") != prior_head["live_custody_sha256"]:
        raise ValueError("authority manifest does not retain the prior live custody")
    if manifest.get("nebius_terraform_provider_version") != NEBIUS_TERRAFORM_PROVIDER_VERSION:
        raise ValueError("authority manifest uses a different Terraform provider version")
    if manifest.get("accepted_custody") != custody:
        raise ValueError("authority manifest does not bind the accepted SAI-10 custody")
    if manifest.get("provider_project_iam_inventory_receipt_sha256") != iam_receipt_sha256:
        raise ValueError("authority manifest does not bind the exact provider IAM receipt")
    if (
        manifest.get("kubernetes_rbac_inventory_receipt_sha256")
        != rbac_receipt_sha256
        or rbac_receipt.get("cluster_id") != manifest["cluster_id"]
    ):
        raise ValueError("authority manifest does not bind the target-cluster RBAC receipt")

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
            "boundary_policy_sha256",
            "release_values_sha256",
            "provider_iam_receipt_sha256",
            "kubernetes_rbac_receipt_sha256",
            "prior_live_custody_sha256",
            "accepted_sai10_commit",
            "accepted_sai10_tree",
            "accepted_sai10_review_sha256",
            "cluster_id",
            "network_id",
            "subnet_id",
            "node_service_account_id",
            "kubernetes_version",
            "nebius_terraform_provider_version",
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
        for field in (
            "contract_sha256",
            "predecessor_compatibility_sha256",
            "boundary_policy_sha256",
            "release_values_sha256",
            "provider_iam_receipt_sha256",
            "kubernetes_rbac_receipt_sha256",
            "prior_live_custody_sha256",
            "accepted_sai10_review_sha256",
        ):
            value = entry.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"authority generation {field} is invalid")
        if (
            entry.get("provider_iam_receipt_sha256") != iam_receipt_sha256
            or entry.get("kubernetes_rbac_receipt_sha256") != rbac_receipt_sha256
            or entry.get("prior_live_custody_sha256")
            != prior_head["live_custody_sha256"]
            or entry.get("accepted_sai10_commit") != custody["sai10_commit"]
            or entry.get("accepted_sai10_tree") != custody["sai10_tree"]
            or entry.get("accepted_sai10_review_sha256")
            != custody["independent_review_receipt_sha256"]
            or entry.get("cluster_id") != manifest["cluster_id"]
            or entry.get("network_id") != manifest["network_id"]
            or entry.get("subnet_id") != manifest["subnet_id"]
            or entry.get("node_service_account_id")
            != manifest["node_service_account_id"]
            or entry.get("kubernetes_version") != manifest["kubernetes_version"]
            or entry.get("nebius_terraform_provider_version")
            != NEBIUS_TERRAFORM_PROVIDER_VERSION
        ):
            raise ValueError(
                "authority generation is not content-bound to custody and provider parents"
            )
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
    if manifest.get("current_generation") != list(normalized)[-1]:
        raise ValueError("current authority generation must be the final signed generation")

    return {
        "authorized": "true",
        "manifest_sha256": manifest_digest,
        "prior_head_receipt_sha256": prior_head_sha256,
        "prior_live_custody_sha256": prior_head["live_custody_sha256"],
        "prior_live_predecessor_compatibility_sha256": live_custody[
            "predecessor_compatibility_sha256"
        ],
        "manifest_json": json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        "generations_json": json.dumps(normalized, sort_keys=True, separators=(",", ":")),
        "authority_project_id": registry["authority_project_id"],
        "authority_service_account_id": registry["authority_service_account_id"],
        "authority_group_id": registry["authority_group_id"],
        "workloads_service_account_id": registry["workloads_service_account_id"],
        "kubernetes_identity_inventory_sha256": hashlib.sha256(
            canonical(sorted(kubernetes_subjects, key=lambda item: item["name"]))
        ).hexdigest(),
        "provider_project_iam_inventory_receipt_sha256": iam_receipt_sha256,
        "kubernetes_rbac_inventory_receipt_sha256": rbac_receipt_sha256,
        "kubernetes_rbac_inventory_sha256": rbac_receipt["inventory_sha256"],
        "accepted_sai10_commit": custody["sai10_commit"],
        "accepted_sai10_tree": custody["sai10_tree"],
        "sai10_independent_review_receipt_sha256": custody[
            "independent_review_receipt_sha256"
        ],
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
