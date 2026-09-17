#!/usr/bin/env python3
"""Fail closed on destructive key migration plans and residual plaintext state."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from credential_evidence import EvidenceVerificationError, verify_evidence_envelope
from credential_provider_adapter import load_client_policy

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "security/durable-credential-registry.json"
DEFAULT_INTEGRATION_DEPENDENCIES = (
    ROOT / "security/sai-10-integration-dependencies.json"
)
DEFAULT_CONSUMER_CONTRACTS = ROOT / "security/credential-consumer-contracts.json"
PRODUCTION_AUTHORITY_COMMAND = (
    "/usr/bin/python3",
    str(ROOT / "scripts" / "credential_provider_adapter.py"),
)
PRODUCTION_TERRAFORM_COMMAND = "/snap/bin/terraform"
CONTENT_COMMITMENT_SCHEME = "sha256-canonical-json-decoded-secret-data-v1"
SENSITIVE_ARTIFACT_SUFFIXES = (".tfstate", ".tfplan", ".backup")
SCOPED_CREDENTIAL_PREFIXES = (
    "access-bundle",
    "admin-cookie",
    "admin",
    "general-access",
    "grafana",
    "scientific-access",
)
DISPOSITION_ACTIONS = frozenset({"encrypted-rewrap"})
AUTHORITATIVE_STATE_SCOPE_RELATIVE = Path(".")
AUTHORITATIVE_STATE_ROOT_NAMES = (
    ".local/state/k8s-inference-dual-acceptance",
    ".local/state/nebius-k8s-inference",
    "secure-handoff",
)
CREDENTIAL_RESOURCE_TYPES = frozenset(
    {
        "random_id",
        "random_password",
        "kubernetes_secret_v1",
        "nebius_iam_v1_access_permit",
        "nebius_iam_v1_group",
        "nebius_iam_v1_group_membership",
        "nebius_iam_v1_service_account",
        "nebius_iam_v2_access_key",
    }
)
REGISTRY_BASE_ADDRESS_PROBES = (
    "[0]",
    '["2"]',
    '["payload"]',
    '["ledger"]',
    '["pepper"]',
    '["attestor"]',
    '["storage"]',
    '["storage_name"]',
)
CONSUMER_OPERATIONS = frozenset(
    {
        "consumer-readiness",
        "rotation-readiness",
        "ciphertext-migration",
        "authentication-continuity",
    }
)


class GuardError(RuntimeError):
    pass


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_consumer_contracts(
    path: Path = DEFAULT_CONSUMER_CONTRACTS,
) -> dict[str, dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise GuardError("credential consumer contracts are absent or unsafe")
    document = json.loads(path.read_text(encoding="utf-8"))
    contracts = document.get("contracts") if isinstance(document, dict) else None
    pending = document.get("pending_contract_ids") if isinstance(document, dict) else None
    if (
        document.get("schema") != "fs2-serve.nebius.ai/credential-consumer-contracts/v1"
        or not isinstance(contracts, dict)
        or len(contracts) != 21
        or pending != []
    ):
        raise GuardError("credential consumer contract inventory is incomplete")
    for credential_class, contract in contracts.items():
        if (
            not isinstance(credential_class, str)
            or not credential_class
            or not isinstance(contract, dict)
            or set(contract)
            != {
                "adapter",
                "authority",
                "consumers",
                "readiness",
                "required_operations",
            }
            or not all(
                isinstance(contract[field], str) and contract[field]
                for field in ("adapter", "authority", "readiness")
            )
            or not isinstance(contract["consumers"], list)
            or not contract["consumers"]
            or not all(
                isinstance(consumer, str) and consumer
                for consumer in contract["consumers"]
            )
            or len(contract["consumers"]) != len(set(contract["consumers"]))
            or not isinstance(contract["required_operations"], list)
            or not contract["required_operations"]
            or len(contract["required_operations"])
            != len(set(contract["required_operations"]))
            or not set(contract["required_operations"]) <= CONSUMER_OPERATIONS
            or "consumer-readiness" not in contract["required_operations"]
            or "rotation-readiness" not in contract["required_operations"]
        ):
            raise GuardError(
                f"credential consumer contract is malformed: {credential_class}"
            )
    return dict(sorted(contracts.items()))


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise GuardError("gate expiry must be an RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GuardError("gate expiry must be a valid RFC3339 instant") from error
    if parsed.tzinfo is None:
        raise GuardError("gate expiry must include a timezone")
    return parsed.astimezone(UTC).replace(microsecond=0)


def write_private_json(path: Path, value: Any) -> None:
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise GuardError("receipt is write-once and already exists")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise GuardError("receipt parent must be a real directory")
    metadata = parent.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise GuardError("receipt parent must be owner-owned and owner-only")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if path.exists():
            path.chmod(0o600)


def configuration_sha256(root: Path) -> str:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise GuardError("Terraform root must be a real directory")
    digest = hashlib.sha256()
    terraform_files = sorted(root.glob("*.tf"))
    if not terraform_files:
        raise GuardError("Terraform root contains no configuration")
    for path in terraform_files:
        if path.is_symlink() or not path.is_file():
            raise GuardError("Terraform configuration must contain no symlinks")
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def terraform_state_identity(document: Any) -> dict[str, Any]:
    """Return the exact backend lineage required for a saved-plan apply.

    Terraform's normalized ``show -json`` output omits the backend serial and
    lineage.  The gate therefore also consumes the raw ``state pull`` document
    and rejects empty/self-invented placeholders before planning.
    """

    if not isinstance(document, dict):
        raise GuardError("raw Terraform state must be a JSON object")
    lineage = document.get("lineage")
    serial = document.get("serial")
    version = document.get("version")
    terraform_version = document.get("terraform_version")
    resources = document.get("resources")
    if (
        version != 4
        or not isinstance(lineage, str)
        or re.fullmatch(r"[0-9a-fA-F-]{16,64}", lineage) is None
        or not isinstance(serial, int)
        or serial < 1
        or not isinstance(terraform_version, str)
        or not terraform_version
        or not isinstance(resources, list)
        or not resources
    ):
        raise GuardError(
            "raw Terraform state must have a non-empty v4 lineage, positive serial and resource inventory"
        )
    return {
        "lineage": lineage,
        "serial": serial,
        "terraform_version": terraform_version,
        "raw_state_sha256": canonical_sha256(document),
    }


def reject_protected_moved_blocks(
    root: Path, *, registry: dict[str, Any], terraform_root: str
) -> None:
    for path in sorted(root.glob("*.tf")):
        source = path.read_text(encoding="utf-8")
        for block in re.finditer(r"(?s)\bmoved\s*\{(.*?)\}", source):
            body = block.group(1)
            source_match = re.search(r"(?m)^\s*from\s*=\s*([^\s#]+)", body)
            target_match = re.search(r"(?m)^\s*to\s*=\s*([^\s#]+)", body)
            if source_match is None or target_match is None:
                raise GuardError("Terraform moved block is malformed")
            previous_address = source_match.group(1)
            address = target_match.group(1)
            if is_protected_address(
                previous_address,
                registry=registry,
                terraform_root=terraform_root,
            ) or is_protected_address(
                address,
                registry=registry,
                terraform_root=terraform_root,
            ):
                raise GuardError(
                    "Terraform configuration moves a durable credential address"
                )


def greenfield_bootstrap_identity(
    *, terraform_root: str, registry: dict[str, Any]
) -> dict[str, Any]:
    """Re-observe a distinct externally anchored empty-install contract."""

    observation = authority_json(
        {
            "operation": "greenfield-bootstrap-readiness",
            "terraform_root_name": terraform_root,
        }
    )
    verify_external_evidence(observation)
    scope = observation.get("live_inventory_scope")
    expected_addresses = sorted(
        registry_resource_addresses(registry, terraform_root=terraform_root)
    )
    expected_classes = sorted(
        item["id"]
        for item in registry["credentials"]
        if item["terraform_root"] == terraform_root
    )
    inventory_sources = observation.get("inventory_sources")
    evidence = observation.get("externalEvidence")
    claim = evidence.get("claim") if isinstance(evidence, dict) else None
    if (
        observation.get("authorizedCallerPurpose") != "release-automation"
        or observation.get("terraform_root_name") != terraform_root
        or observation.get("status") != "greenfield-empty-provider-attested"
        or observation.get("initialization_mode") != "greenfield-empty"
        or observation.get("registry_sha256") != registry_sha256(registry)
        or observation.get("backend_object_present") is not False
        or observation.get("backend_object_version_ids") != []
        or observation.get("backend_lock_present") is not False
        or observation.get("terraform_managed_credential_addresses") != []
        or observation.get("kubernetes_secret_identities") != []
        or observation.get("nebius_credential_identities") != []
        or not isinstance(scope, dict)
        or scope.get("terraform_root_name") != terraform_root
        or scope.get("registry_sha256") != registry_sha256(registry)
        or scope.get("registered_credential_addresses") != expected_addresses
        or scope.get("registered_credential_classes") != expected_classes
        or scope.get("all_project_iam_credentials") is not True
        or scope.get("all_cluster_secrets") is not True
        or not isinstance(scope.get("project_id"), str)
        or not scope["project_id"]
        or not isinstance(scope.get("cluster_id"), str)
        or not scope["cluster_id"]
        or not isinstance(scope.get("namespaces"), list)
        or not scope["namespaces"]
        or scope["namespaces"] != sorted(set(scope["namespaces"]))
        or observation.get("live_inventory_scope_sha256")
        != canonical_sha256(scope)
        or not isinstance(inventory_sources, dict)
        or set(inventory_sources) != {"backend", "kubernetes", "nebius"}
        or not all(
            isinstance(value, str) and value for value in inventory_sources.values()
        )
        or not isinstance(observation.get("backend_binding_sha256"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}", observation["backend_binding_sha256"]
        )
        is None
        or not isinstance(observation.get("bootstrap_evidence_id"), str)
        or re.fullmatch(r"[0-9a-f]{64}", observation["bootstrap_evidence_id"])
        is None
        or not isinstance(claim, dict)
        or claim.get("operation") != "greenfield-bootstrap-readiness"
        or not isinstance(claim.get("evidence_id"), str)
        or not claim["evidence_id"]
    ):
        raise GuardError(
            "greenfield bootstrap lacks exact empty backend and live inventory evidence"
        )
    observed = parse_timestamp(observation.get("observed_at"))
    now = utc_now()
    if observed > now or now - observed > timedelta(minutes=5):
        raise GuardError("greenfield bootstrap observation is stale")
    return {
        "terraform_root": terraform_root,
        "registry_sha256": registry_sha256(registry),
        "backend_binding_sha256": observation["backend_binding_sha256"],
        "live_inventory_scope_sha256": observation[
            "live_inventory_scope_sha256"
        ],
        "bootstrap_evidence_id": observation["bootstrap_evidence_id"],
        "external_evidence_id": claim["evidence_id"],
        "inventory_sources": inventory_sources,
    }


def require_empty_greenfield_state(document: Any) -> None:
    """Reject bootstrap when any managed state resource already exists."""

    if not isinstance(document, dict) or any(
        resource.get("mode", "managed") == "managed"
        for resource in state_resources(document)
    ):
        raise GuardError(
            "greenfield bootstrap requires an empty managed Terraform prior state"
        )


def write_apply_gate_receipt(
    *,
    state_document: dict[str, Any],
    raw_state_document: dict[str, Any] | None,
    identity_receipt: dict[str, Any] | None,
    terraform_configuration: Path,
    terraform_root: str,
    source_commit: str,
    path: Path,
    ttl_seconds: int = 900,
    registry: dict[str, Any] | None = None,
    greenfield_bootstrap: bool = False,
) -> dict[str, Any]:
    registry = registry or load_registry()
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise GuardError("source commit must be an exact lowercase Git SHA")
    if not 60 <= ttl_seconds <= 900:
        raise GuardError("native Terraform gate TTL must be between 60 and 900 seconds")
    reject_protected_moved_blocks(
        terraform_configuration,
        registry=registry,
        terraform_root=terraform_root,
    )
    fingerprints = protected_state_fingerprints(
        state_document, registry=registry, terraform_root=terraform_root
    )
    bootstrap_identity: dict[str, Any] | None = None
    custody_evidence_sha256: str | None = None
    custody_evidence_id: str | None = None
    authority_state: dict[str, Any] | None = None
    if greenfield_bootstrap:
        require_empty_greenfield_state(state_document)
        if raw_state_document is not None or fingerprints or identity_receipt is not None:
            raise GuardError(
                "greenfield bootstrap cannot carry state or an adoption receipt"
            )
        bootstrap_identity = greenfield_bootstrap_identity(
            terraform_root=terraform_root, registry=registry
        )
        state_identity = None
        initialization_mode = "greenfield-empty"
    else:
        if not isinstance(raw_state_document, dict):
            raise GuardError("established Terraform state is absent")
        state_identity = terraform_state_identity(raw_state_document)
        custody = authority_json({"operation": "custody-snapshot"})
        verify_external_evidence(custody)
        if custody.get("registry_sha256") != registry_sha256(registry):
            raise GuardError("local durable registry differs from root authority policy")
        authority_states = custody.get("terraform_states")
        if not isinstance(authority_states, list):
            raise GuardError("credential authority omitted Terraform custody")
        matching_states = [
            item
            for item in authority_states
            if isinstance(item, dict)
            and item.get("root") == terraform_root
            and item.get("lineage") == state_identity["lineage"]
            and item.get("serial") == state_identity["serial"]
            and item.get("state_json_sha256") == state_identity["raw_state_sha256"]
            and item.get("configuration_sha256")
            == configuration_sha256(terraform_configuration)
        ]
        if len(matching_states) != 1:
            raise GuardError(
                "local Terraform state does not match the authority-owned state lineage"
            )
        authority_state = matching_states[0]
        custody_evidence_sha256 = canonical_sha256(custody["externalEvidence"])
        custody_evidence_id = custody["externalEvidence"]["claim"]["evidence_id"]
        initialization_mode = "remote-established"
    if fingerprints:
        if identity_receipt is None:
            raise GuardError("durable state requires an exact identity receipt")
        if identity_receipt.get("address_fingerprints") != fingerprints:
            raise GuardError("durable state differs from its identity receipt")
    now = utc_now()
    receipt = {
        "schema": "fs2-serve.nebius.ai/terraform-plan-gate/v4",
        "terraform_root": terraform_root,
        "source_commit": source_commit,
        "registry_sha256": registry_sha256(registry),
        "configuration_sha256": configuration_sha256(terraform_configuration),
        "state_fingerprints_sha256": canonical_sha256(fingerprints),
        "state_initialization": initialization_mode,
        "state_identity": state_identity,
        "authority_state": authority_state,
        "bootstrap_identity": bootstrap_identity,
        "custody_evidence_sha256": custody_evidence_sha256,
        "custody_evidence_id": custody_evidence_id,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now.replace(microsecond=0) + timedelta(seconds=ttl_seconds))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    write_private_json(path, receipt)
    return receipt


def validate_native_gate(
    query: dict[str, Any],
    *,
    authoritative_state_document: dict[str, Any] | None,
    registry: dict[str, Any] | None = None,
) -> dict[str, str]:
    registry = registry or load_registry()
    required = {
        "receipt_path",
        "terraform_configuration",
        "terraform_root",
        "source_commit",
    }
    if (
        not isinstance(query, dict)
        or set(query) != required
        or not all(isinstance(query[key], str) and query[key] for key in required)
    ):
        raise GuardError("native Terraform gate query is incomplete")
    receipt_path = Path(query["receipt_path"])
    receipt = load_private_document(receipt_path, label="Terraform gate receipt")
    if receipt.get("schema") != "fs2-serve.nebius.ai/terraform-plan-gate/v4":
        raise GuardError("Terraform gate receipt has the wrong schema")
    root = Path(query["terraform_configuration"])
    expected = {
        "terraform_root": query["terraform_root"],
        "source_commit": query["source_commit"],
        "registry_sha256": registry_sha256(registry),
        "configuration_sha256": configuration_sha256(root),
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise GuardError(
            "Terraform gate receipt does not bind this source and registry"
        )
    initialization_mode = receipt.get("state_initialization")
    if initialization_mode == "greenfield-empty":
        if (
            authoritative_state_document is not None
            or receipt.get("state_identity") is not None
            or receipt.get("authority_state") is not None
            or receipt.get("custody_evidence_sha256") is not None
            or receipt.get("custody_evidence_id") is not None
            or not isinstance(receipt.get("bootstrap_identity"), dict)
        ):
            raise GuardError(
                "greenfield gate is mixed with established-state or migration evidence"
            )
        current_bootstrap = greenfield_bootstrap_identity(
            terraform_root=query["terraform_root"], registry=registry
        )
        stable_bootstrap_fields = {
            "terraform_root",
            "registry_sha256",
            "backend_binding_sha256",
            "live_inventory_scope_sha256",
        }
        if any(
            current_bootstrap.get(field)
            != receipt["bootstrap_identity"].get(field)
            for field in stable_bootstrap_fields
        ):
            raise GuardError(
                "greenfield backend or live inventory changed after gate issuance"
            )
    elif initialization_mode == "remote-established":
        state_identity = receipt.get("state_identity")
        if (
            not isinstance(authoritative_state_document, dict)
            or not isinstance(state_identity, dict)
            or set(state_identity)
            != {"lineage", "serial", "terraform_version", "raw_state_sha256"}
            or not isinstance(state_identity.get("lineage"), str)
            or not isinstance(state_identity.get("serial"), int)
            or state_identity["serial"] < 1
            or not isinstance(state_identity.get("terraform_version"), str)
            or not isinstance(state_identity.get("raw_state_sha256"), str)
            or len(state_identity["raw_state_sha256"]) != 64
            or receipt.get("bootstrap_identity") is not None
        ):
            raise GuardError("Terraform gate receipt has no exact state lineage")
        if state_identity != terraform_state_identity(authoritative_state_document):
            raise GuardError(
                "Terraform gate receipt differs from the authoritative backend state"
            )
        authority_state = receipt.get("authority_state")
        if (
            not isinstance(authority_state, dict)
            or re.fullmatch(
                r"[0-9a-f]{64}", receipt.get("custody_evidence_sha256", "")
            )
            is None
            or not isinstance(receipt.get("custody_evidence_id"), str)
            or not receipt["custody_evidence_id"]
        ):
            raise GuardError("Terraform gate receipt lacks authority-owned custody")
        custody = authority_json({"operation": "custody-snapshot"})
        verify_external_evidence(custody)
        if custody.get("registry_sha256") != registry_sha256(registry):
            raise GuardError("local durable registry differs from root authority policy")
        current_matches = [
            item
            for item in custody.get("terraform_states", [])
            if isinstance(item, dict)
            and item.get("root") == query["terraform_root"]
            and item.get("lineage") == state_identity["lineage"]
            and item.get("serial") == state_identity["serial"]
            and item.get("state_json_sha256") == state_identity["raw_state_sha256"]
            and item.get("configuration_sha256")
            == expected["configuration_sha256"]
        ]
        if current_matches != [authority_state]:
            raise GuardError(
                "authority-owned Terraform state changed after gate issuance"
            )
    else:
        raise GuardError("Terraform gate receipt has an unknown initialization mode")
    issued_at = parse_timestamp(receipt.get("issued_at"))
    expires_at = parse_timestamp(receipt.get("expires_at"))
    now = utc_now()
    if (
        expires_at <= now
        or issued_at > now
        or expires_at - issued_at > timedelta(seconds=900)
    ):
        raise GuardError("Terraform gate receipt is expired or has an invalid lifetime")
    reject_protected_moved_blocks(
        root, registry=registry, terraform_root=query["terraform_root"]
    )
    return {
        "status": "pass",
        "receipt_sha256": file_sha256(receipt_path),
        "expires_at": receipt["expires_at"],
    }


def descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(descriptor, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def saved_plan_identity(
    path: Path, *, original_path: Path | None = None
) -> dict[str, Any]:
    """Identify a named plan or an inherited, descriptor-pinned plan exactly."""

    descriptor_match = re.fullmatch(r"/proc/self/fd/([0-9]+)", str(path))
    close_descriptor = False
    if descriptor_match is not None:
        descriptor = int(descriptor_match.group(1))
        if descriptor < 3 or original_path is None or not original_path.is_absolute():
            raise GuardError(
                "descriptor-pinned saved plan requires its exact absolute original path"
            )
        identity_path = original_path
    else:
        path = path.absolute()
        if (
            path.is_symlink()
            or any(parent.is_symlink() for parent in path.parents)
            or not path.is_file()
        ):
            raise GuardError("saved Terraform plan must be a real file")
        identity_path = path.resolve(strict=True)
        try:
            descriptor = os.open(
                identity_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except OSError as error:
            raise GuardError("saved Terraform plan could not be pinned") from error
        close_descriptor = True
    try:
        before = os.fstat(descriptor)
        plan_sha256 = descriptor_sha256(descriptor)
        metadata = os.fstat(descriptor)
        stability_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(before, field) != getattr(metadata, field)
            for field in stability_fields
        ):
            raise GuardError("saved Terraform plan changed while it was identified")
        if close_descriptor:
            current = identity_path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise GuardError("saved Terraform plan pathname changed while identified")
    except OSError as error:
        raise GuardError("saved Terraform plan descriptor is not open") from error
    finally:
        if close_descriptor:
            os.close(descriptor)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise GuardError("saved Terraform plan must be owner-owned and owner-only")
    if not stat.S_ISREG(metadata.st_mode):
        raise GuardError("saved Terraform plan must be a regular file")
    return {
        "realpath_sha256": hashlib.sha256(str(identity_path).encode()).hexdigest(),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "sha256": plan_sha256,
    }


def require_sealed_saved_plan(path: Path) -> None:
    """Require an inherited Linux memfd whose bytes can no longer change."""

    descriptor_match = re.fullmatch(r"/proc/self/fd/([0-9]+)", str(path))
    seal_names = ("F_SEAL_WRITE", "F_SEAL_GROW", "F_SEAL_SHRINK", "F_SEAL_SEAL")
    if descriptor_match is None or any(
        not hasattr(fcntl, name) for name in (*seal_names, "F_GET_SEALS")
    ):
        raise GuardError("runtime Terraform plan is not a sealable inherited descriptor")
    descriptor = int(descriptor_match.group(1))
    required = 0
    for name in seal_names:
        required |= int(getattr(fcntl, name))
    try:
        observed = int(fcntl.fcntl(descriptor, fcntl.F_GET_SEALS))
    except OSError as error:
        raise GuardError("runtime Terraform plan descriptor is not sealed") from error
    if observed & required != required:
        raise GuardError("runtime Terraform plan descriptor is not byte-immutable")


def write_saved_plan_gate_receipt(
    *,
    plan_document: dict[str, Any],
    raw_state_document: dict[str, Any] | None,
    saved_plan: Path,
    planning_receipt_path: Path,
    identity_receipt: dict[str, Any] | None,
    live_secret_document: dict[str, Any] | None,
    terraform_configuration: Path,
    terraform_root: str,
    source_commit: str,
    path: Path,
    ttl_seconds: int = 300,
    registry: dict[str, Any] | None = None,
    greenfield_bootstrap: bool = False,
) -> dict[str, Any]:
    """Seal the exact saved plan and live credential identities for apply."""

    registry = registry or load_registry()
    saved_plan_binding = saved_plan_identity(saved_plan)
    if live_secret_document is not None:
        raise GuardError(
            "caller-supplied live Secret inventories are forbidden; use the fixed authority"
        )
    if not 60 <= ttl_seconds <= 300:
        raise GuardError("saved-plan apply gate TTL must be between 60 and 300 seconds")
    planning_receipt = load_private_document(
        planning_receipt_path, label="Terraform planning gate receipt"
    )
    if (
        planning_receipt.get("state_initialization") == "greenfield-empty"
    ) != greenfield_bootstrap:
        raise GuardError(
            "saved-plan bootstrap mode differs from its planning receipt"
        )
    validate_native_gate(
        {
            "receipt_path": str(planning_receipt_path),
            "terraform_configuration": str(terraform_configuration),
            "terraform_root": terraform_root,
            "source_commit": source_commit,
        },
        authoritative_state_document=raw_state_document,
        registry=registry,
    )
    prior_state = plan_document.get("prior_state")
    if not isinstance(prior_state, dict):
        raise GuardError("saved plan has no authoritative prior state")
    if greenfield_bootstrap:
        require_empty_greenfield_state(prior_state)
    fingerprints = protected_state_fingerprints(
        prior_state, registry=registry, terraform_root=terraform_root
    )
    if canonical_sha256(fingerprints) != planning_receipt.get(
        "state_fingerprints_sha256"
    ):
        raise GuardError("saved plan prior state differs from the planning gate")
    if greenfield_bootstrap:
        if raw_state_document is not None or planning_receipt.get("state_identity") is not None:
            raise GuardError(
                "greenfield saved plan unexpectedly carries established state"
            )
    elif not isinstance(raw_state_document, dict) or planning_receipt.get(
        "state_identity"
    ) != terraform_state_identity(raw_state_document):
        raise GuardError("saved plan was not sealed against the current backend state")
    if fingerprints:
        if identity_receipt is None:
            raise GuardError("saved plan requires the exact durable identity receipt")
        if identity_receipt.get("address_fingerprints") != fingerprints:
            raise GuardError("saved plan differs from the durable identity receipt")
    inspect_plan(
        plan_document,
        identity_receipt=identity_receipt,
        registry=registry,
        terraform_root=terraform_root,
        greenfield_bootstrap=greenfield_bootstrap,
    )
    bindings = live_secret_bindings(
        prior_state,
        live_secret_inventory_for_state(
            prior_state, registry=registry, terraform_root=terraform_root
        ),
        registry=registry,
        terraform_root=terraform_root,
    )
    if identity_receipt is not None and bindings != identity_receipt.get(
        "live_secret_bindings"
    ):
        raise GuardError(
            "live Secret identity/content differs from its custody receipt"
        )
    commitments = planned_secret_commitments(
        plan_document,
        registry=registry,
        terraform_root=terraform_root,
        greenfield_bootstrap=greenfield_bootstrap,
    )
    if greenfield_bootstrap:
        require_greenfield_additive_plan(plan_document, commitments=commitments)
    else:
        require_staged_secret_plan(plan_document, commitments=commitments)
    phase = (
        None
        if terraform_root == "configuration"
        else plan_variable(plan_document, "credential_migration_phase")
    )
    admission_phase = (
        "greenfield-bootstrap"
        if greenfield_bootstrap and terraform_root != "configuration"
        else phase
    )
    admission: dict[str, Any] | None = None
    if admission_phase in {
        "greenfield-bootstrap",
        "secret-stage",
        "consumer-rollout",
    }:
        admission = authority_json(
            {
                "operation": "planned-generation-admission",
                "phase": admission_phase,
            }
        )
        verify_external_evidence(admission)
        authority_plans = admission.get("plans")
        if (
            admission.get("phase") != admission_phase
            or admission.get("registry_sha256") != registry_sha256(registry)
            or not isinstance(authority_plans, list)
            or saved_plan_binding["sha256"]
            not in {
                item.get("plan_sha256")
                for item in authority_plans
                if isinstance(item, dict)
            }
        ):
            raise GuardError("authority did not bind the exact staged saved plan")
        if admission_phase == "consumer-rollout" and not admission.get(
            "post_create_secret_bindings"
        ):
            raise GuardError(
                "consumer rollout lacks provider-observed post-create Secret bindings"
            )
    now = utc_now()
    receipt = {
        "schema": "fs2-serve.nebius.ai/terraform-saved-plan-apply-gate/v5",
        "terraform_root": terraform_root,
        "source_commit": source_commit,
        "registry_sha256": registry_sha256(registry),
        "configuration_sha256": configuration_sha256(terraform_configuration),
        "state_initialization": planning_receipt["state_initialization"],
        "state_identity": planning_receipt["state_identity"],
        "bootstrap_identity": planning_receipt["bootstrap_identity"],
        "address_fingerprints": fingerprints,
        "live_secret_bindings": bindings,
        "planned_secret_commitments": commitments,
        "planned_generation_admission": admission,
        "planning_receipt_sha256": file_sha256(planning_receipt_path),
        "saved_plan": saved_plan_binding,
        "plan_json_sha256": canonical_sha256(plan_document),
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(seconds=ttl_seconds))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    write_private_json(path, receipt)
    return receipt


def validate_saved_plan_gate(
    *,
    receipt_path: Path,
    plan_document: dict[str, Any],
    saved_plan: Path,
    saved_plan_original_path: Path | None = None,
    live_secret_document: dict[str, Any] | None,
    raw_state_document: dict[str, Any] | None,
    terraform_configuration: Path,
    terraform_root: str,
    source_commit: str,
    registry: dict[str, Any] | None = None,
    sealed_runtime_snapshot: bool = False,
) -> dict[str, str]:
    """Revalidate the saved plan at execution time, including receipt expiry."""

    registry = registry or load_registry()
    receipt = load_private_document(receipt_path, label="saved-plan apply receipt")
    if (
        receipt.get("schema")
        != "fs2-serve.nebius.ai/terraform-saved-plan-apply-gate/v5"
    ):
        raise GuardError("saved-plan apply receipt has the wrong schema")
    execution_plan_identity = saved_plan_identity(
        saved_plan, original_path=saved_plan_original_path
    )
    if sealed_runtime_snapshot:
        require_sealed_saved_plan(saved_plan)
        stored_plan_identity = receipt.get("saved_plan")
        if not isinstance(stored_plan_identity, dict) or any(
            execution_plan_identity.get(field) != stored_plan_identity.get(field)
            for field in ("realpath_sha256", "size", "sha256")
        ):
            raise GuardError(
                "sealed runtime plan bytes or original-path binding differ from the receipt"
            )
    elif receipt.get("saved_plan") != execution_plan_identity:
        raise GuardError(
            "saved-plan apply receipt differs from the exact execution object"
        )
    expected = {
        "terraform_root": terraform_root,
        "source_commit": source_commit,
        "registry_sha256": registry_sha256(registry),
        "configuration_sha256": configuration_sha256(terraform_configuration),
        "plan_json_sha256": canonical_sha256(plan_document),
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise GuardError(
            "saved-plan apply receipt differs from the exact execution input"
        )
    greenfield_bootstrap = receipt.get("state_initialization") == "greenfield-empty"
    if greenfield_bootstrap:
        if (
            raw_state_document is not None
            or receipt.get("state_identity") is not None
            or not isinstance(receipt.get("bootstrap_identity"), dict)
        ):
            raise GuardError(
                "greenfield saved-plan receipt is mixed with established state"
            )
        require_empty_greenfield_state(plan_document.get("prior_state"))
        fresh_bootstrap = greenfield_bootstrap_identity(
            terraform_root=terraform_root, registry=registry
        )
        for field in (
            "terraform_root",
            "registry_sha256",
            "backend_binding_sha256",
            "live_inventory_scope_sha256",
        ):
            if fresh_bootstrap.get(field) != receipt["bootstrap_identity"].get(field):
                raise GuardError(
                    "greenfield backend or live inventory changed before apply"
                )
    elif receipt.get("state_initialization") == "remote-established":
        if not isinstance(raw_state_document, dict) or receipt.get(
            "state_identity"
        ) != terraform_state_identity(raw_state_document):
            raise GuardError(
                "saved-plan apply receipt differs from the authoritative backend state"
            )
    else:
        raise GuardError("saved-plan receipt has an unknown initialization mode")
    phase = (
        None
        if terraform_root == "configuration"
        else plan_variable(plan_document, "credential_migration_phase")
    )
    admission_phase = (
        "greenfield-bootstrap"
        if greenfield_bootstrap and terraform_root != "configuration"
        else phase
    )
    stored_admission = receipt.get("planned_generation_admission")
    if admission_phase in {
        "greenfield-bootstrap",
        "secret-stage",
        "consumer-rollout",
    }:
        if not isinstance(stored_admission, dict):
            raise GuardError("saved-plan receipt lacks staged provider admission")
        verify_external_evidence(stored_admission)
        fresh_admission = authority_json(
            {
                "operation": "planned-generation-admission",
                "phase": admission_phase,
            }
        )
        verify_external_evidence(fresh_admission)
        plan_sha256 = execution_plan_identity["sha256"]
        for admission in (stored_admission, fresh_admission):
            if (
                admission.get("phase") != admission_phase
                or admission.get("registry_sha256") != registry_sha256(registry)
                or plan_sha256
                not in {
                    item.get("plan_sha256")
                    for item in admission.get("plans", [])
                    if isinstance(item, dict)
                }
            ):
                raise GuardError("staged provider admission differs from the saved plan")
        if admission_phase == "consumer-rollout" and not fresh_admission.get(
            "post_create_secret_bindings"
        ):
            raise GuardError(
                "provider did not re-observe post-create Secrets at apply time"
            )
    elif stored_admission is not None:
        raise GuardError("steady saved plan unexpectedly carries staged admission")
    issued_at = parse_timestamp(receipt.get("issued_at"))
    expires_at = parse_timestamp(receipt.get("expires_at"))
    now = utc_now()
    if (
        expires_at <= now
        or issued_at > now
        or expires_at - issued_at > timedelta(seconds=300)
    ):
        raise GuardError(
            "saved-plan apply receipt is expired or has an invalid lifetime"
        )
    identity_receipt = (
        None
        if greenfield_bootstrap
        else {
            "registry_sha256": receipt["registry_sha256"],
            "address_fingerprints": receipt.get("address_fingerprints"),
            "live_secret_bindings": receipt.get("live_secret_bindings"),
        }
    )
    inspect_plan(
        plan_document,
        identity_receipt=identity_receipt,
        registry=registry,
        terraform_root=terraform_root,
        greenfield_bootstrap=greenfield_bootstrap,
    )
    bindings = live_secret_bindings(
        plan_document["prior_state"],
        live_secret_document,
        registry=registry,
        terraform_root=terraform_root,
    )
    if bindings != receipt.get("live_secret_bindings"):
        raise GuardError(
            "live Secret identity/content changed after plan authorization"
        )
    commitments = planned_secret_commitments(
        plan_document,
        registry=registry,
        terraform_root=terraform_root,
        greenfield_bootstrap=greenfield_bootstrap,
    )
    if commitments != receipt.get("planned_secret_commitments"):
        raise GuardError("planned Secret commitments changed after plan authorization")
    if greenfield_bootstrap:
        require_greenfield_additive_plan(plan_document, commitments=commitments)
    else:
        require_staged_secret_plan(plan_document, commitments=commitments)
    return {
        "status": "pass",
        "receipt_sha256": file_sha256(receipt_path),
        "expires_at": receipt["expires_at"],
    }


def command_json(
    command: Sequence[str], *, label: str, pass_fds: tuple[int, ...] = ()
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            list(command),
            text=True,
            capture_output=True,
            check=True,
            pass_fds=pass_fds,
        )
        document = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise GuardError(
            f"{label} failed without producing authoritative JSON"
        ) from error
    if not isinstance(document, dict):
        raise GuardError(f"{label} produced malformed JSON")
    return document


def authority_json(request: dict[str, Any]) -> dict[str, Any]:
    """Use the fixed kernel-authenticated authority; never a caller-selected CLI."""

    try:
        result = subprocess.run(
            list(PRODUCTION_AUTHORITY_COMMAND),
            input=json.dumps(request, sort_keys=True),
            text=True,
            capture_output=True,
            check=True,
        )
        response = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise GuardError(
            "production credential authority failed without authoritative JSON"
        ) from error
    if not isinstance(response, dict):
        raise GuardError("production credential authority returned malformed JSON")
    return response


def verify_external_evidence(document: dict[str, Any]) -> dict[str, Any]:
    """Verify a stored observation without asking its producer to trust itself."""

    proof = document.get("externalEvidence")
    observation = document.get("authorityObservation")
    if not isinstance(proof, dict) or not isinstance(observation, dict):
        raise GuardError("authority payload lacks externally anchored evidence")
    result = {
        key: value
        for key, value in document.items()
        if key not in {"externalEvidence", "authorityObservation"}
    }
    payload = {**observation, "result": result}
    envelope = {**proof, "payload": payload}
    claim = proof.get("claim")
    if not isinstance(claim, dict):
        raise GuardError("external evidence claim is absent")
    try:
        policy = load_client_policy()
        verified = verify_evidence_envelope(
            envelope,
            expected_operation=str(claim.get("operation", "")),
            expected_request_sha256=str(claim.get("request_sha256", "")),
            expected_nonce=str(claim.get("request_nonce", "")),
            evidence_public_key_sha256=policy["evidence_public_key_sha256"],
            anchor_public_key_sha256=policy["anchor_public_key_sha256"],
            source_trust=policy["source_trust"],
        )
    except (EvidenceVerificationError, RuntimeError) as error:
        raise GuardError("external evidence verification failed") from error
    if verified != payload:
        raise GuardError("external evidence payload differs from stored observation")
    return result


def live_secret_inventory_for_receipt(
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    bindings = receipt.get("live_secret_bindings")
    if not isinstance(bindings, dict):
        raise GuardError("saved-plan apply receipt has malformed live Secret bindings")
    if not bindings:
        return None
    identities: set[tuple[str, str]] = set()
    for binding in bindings.values():
        if not isinstance(binding, dict):
            raise GuardError(
                "saved-plan apply receipt has malformed live Secret binding"
            )
        identity = (binding.get("namespace"), binding.get("name"))
        if not all(isinstance(value, str) and value for value in identity):
            raise GuardError("saved-plan apply receipt has incomplete Secret identity")
        if identity in identities:
            continue
        identities.add(identity)
    response = authority_json({"operation": "custody-snapshot"})
    verify_external_evidence(response)
    if response.get("registry_sha256") != receipt.get("registry_sha256"):
        raise GuardError("production authority registry differs from the apply receipt")
    items = response.get("kubernetes_secrets")
    if not isinstance(items, list):
        raise GuardError("production authority omitted the global Secret inventory")
    selected = [
        item
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("metadata"), dict)
        and (item["metadata"].get("namespace"), item["metadata"].get("name"))
        in identities
    ]
    if len(selected) != len(identities):
        raise GuardError("production authority omitted a live Secret binding")
    return {"items": selected}


def live_secret_inventory_for_state(
    state_document: dict[str, Any],
    *,
    registry: dict[str, Any],
    terraform_root: str,
) -> dict[str, Any] | None:
    """Ask the fixed authority for every protected Secret in exact state.

    The operator never chooses a provider executable and never receives Secret
    values.  The authority hashes the canonical decoded data map internally.
    """

    identities: set[tuple[str, str]] = set()
    for resource in state_resources(state_document):
        address = resource.get("address")
        if not (
            isinstance(address, str)
            and credential_resource_type(address) == "kubernetes_secret_v1"
            and is_protected_address(
                address, registry=registry, terraform_root=terraform_root
            )
        ):
            continue
        values = resource.get("values")
        metadata = values.get("metadata") if isinstance(values, dict) else None
        metadata = metadata[0] if isinstance(metadata, list) and metadata else metadata
        if not isinstance(metadata, dict):
            raise GuardError(f"protected Secret state lacks metadata: {address}")
        identity = (metadata.get("namespace"), metadata.get("name"))
        if not all(isinstance(value, str) and value for value in identity):
            raise GuardError(f"protected Secret state lacks identity: {address}")
        identities.add((str(identity[0]), str(identity[1])))
    if not identities:
        return None
    response = authority_json({"operation": "custody-snapshot"})
    verify_external_evidence(response)
    if response.get("registry_sha256") != registry_sha256(registry):
        raise GuardError("local durable registry differs from root authority policy")
    all_items = response.get("kubernetes_secrets")
    if not isinstance(all_items, list):
        raise GuardError("production authority omitted the global Secret inventory")
    selected = [
        item
        for item in all_items
        if isinstance(item, dict)
        and isinstance(item.get("metadata"), dict)
        and (item["metadata"].get("namespace"), item["metadata"].get("name"))
        in identities
    ]
    if len(selected) != len(identities):
        raise GuardError("production authority omitted an exact Secret binding")
    return {"items": selected}


def process_parent_pid(pid: int) -> int:
    """Read one Linux process parent without trusting process-supplied data."""

    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("PPid:"):
                return int(line.split(":", 1)[1].strip())
    except (OSError, ValueError) as error:
        raise GuardError("Terraform apply ancestry changed during gate validation") from error
    raise GuardError("Terraform apply ancestor has no kernel parent identity")


def process_start_time(pid: int) -> str:
    """Return the kernel start-time field used to detect PID reuse."""

    try:
        encoded = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = encoded[encoded.rfind(")") + 2 :].split()
        return fields[19]
    except (OSError, IndexError) as error:
        raise GuardError("Terraform apply process identity is unavailable") from error


def actual_terraform_apply_plan(terraform_configuration: Path) -> int:
    """Duplicate the exact sealed plan descriptor used by the Terraform ancestor.

    The local-exec process must descend from the fixed Terraform binary with the
    exact wrapper apply argv.  Environment paths are deliberately not consulted
    when selecting the plan: the kernel-owned ancestor argv and fd table are the
    authority for the bytes Terraform has open.
    """

    expected_executable = Path(PRODUCTION_TERRAFORM_COMMAND).resolve(strict=True)
    expected_configuration = str(terraform_configuration.resolve(strict=True))
    pid = os.getppid()
    visited: set[int] = set()
    for _ in range(32):
        if pid <= 1 or pid in visited:
            break
        visited.add(pid)
        started = process_start_time(pid)
        process_path = Path(f"/proc/{pid}")
        try:
            if process_path.stat().st_uid != os.geteuid():
                raise GuardError("Terraform apply ancestor has a different operating UID")
            executable = Path(os.readlink(process_path / "exe")).resolve(strict=True)
        except OSError as error:
            raise GuardError("Terraform apply ancestry changed during inspection") from error
        if executable == expected_executable:
            try:
                arguments = [
                    item.decode("utf-8")
                    for item in (process_path / "cmdline").read_bytes().split(b"\0")
                    if item
                ]
            except (OSError, UnicodeDecodeError) as error:
                raise GuardError("Terraform apply argv is unavailable") from error
            expected_prefix = [
                f"-chdir={expected_configuration}",
                "apply",
                "-input=false",
            ]
            if len(arguments) != 5 or arguments[1:4] != expected_prefix:
                raise GuardError(
                    "native apply gate is not running under the exact release wrapper argv"
                )
            descriptor_match = re.fullmatch(
                r"/proc/self/fd/([0-9]+)", arguments[4]
            )
            if descriptor_match is None or int(descriptor_match.group(1)) < 3:
                raise GuardError(
                    "Terraform is not applying an inherited descriptor-pinned plan"
                )
            descriptor_number = int(descriptor_match.group(1))
            try:
                duplicate = os.open(
                    process_path / "fd" / str(descriptor_number), os.O_RDONLY
                )
            except OSError as error:
                raise GuardError("Terraform's applied plan descriptor is unavailable") from error
            try:
                if process_start_time(pid) != started:
                    raise GuardError("Terraform apply PID changed during plan binding")
                require_sealed_saved_plan(Path(f"/proc/self/fd/{duplicate}"))
            except Exception:
                os.close(duplicate)
                raise
            return duplicate
        pid = process_parent_pid(pid)
    raise GuardError("native apply gate has no fixed Terraform apply ancestor")


def validate_saved_plan_gate_from_environment(
    *,
    terraform_configuration: Path,
    terraform_root: str,
    source_commit: str,
    registry: dict[str, Any] | None = None,
    wrapper_preflight: bool = False,
    wrapper_runtime_snapshot: bool = False,
) -> dict[str, str]:
    """Execution-time entrypoint used by Terraform's local apply provisioner."""

    receipt_value = os.environ.get("FS2_TERRAFORM_APPLY_GATE_RECEIPT", "")
    plan_value = os.environ.get("FS2_TERRAFORM_SAVED_PLAN", "")
    original_plan_value = os.environ.get(
        "FS2_TERRAFORM_SAVED_PLAN_ORIGINAL_PATH", ""
    )
    terraform = PRODUCTION_TERRAFORM_COMMAND
    if wrapper_preflight and wrapper_runtime_snapshot:
        raise GuardError("saved-plan validation mode is ambiguous")
    if not receipt_value or not original_plan_value:
        raise GuardError(
            "apply requires the exact receipt and original-path binding"
        )
    receipt_path = Path(receipt_value)
    original_plan = Path(original_plan_value)
    close_plan_descriptor = False
    if wrapper_preflight or wrapper_runtime_snapshot:
        descriptor_match = re.fullmatch(r"/proc/self/fd/([0-9]+)", plan_value)
        if descriptor_match is None or int(descriptor_match.group(1)) < 3:
            raise GuardError(
                "wrapper validation requires a private inherited plan descriptor"
            )
        plan_descriptor = int(descriptor_match.group(1))
        saved_plan = Path(plan_value)
    else:
        plan_descriptor = actual_terraform_apply_plan(terraform_configuration)
        close_plan_descriptor = True
        saved_plan = Path(f"/proc/self/fd/{plan_descriptor}")
    try:
        receipt = load_private_document(receipt_path, label="saved-plan apply receipt")
        plan_document = command_json(
            [
                terraform,
                f"-chdir={terraform_configuration}",
                "show",
                "-json",
                str(saved_plan),
            ],
            label="saved Terraform plan inspection",
            pass_fds=(plan_descriptor,),
        )
        raw_state_document = (
            None
            if receipt.get("state_initialization") == "greenfield-empty"
            else command_json(
                [terraform, f"-chdir={terraform_configuration}", "state", "pull"],
                label="authoritative Terraform state inspection",
            )
        )
        live_document = live_secret_inventory_for_receipt(receipt)
        return validate_saved_plan_gate(
            receipt_path=receipt_path,
            plan_document=plan_document,
            saved_plan=saved_plan,
            saved_plan_original_path=original_plan,
            live_secret_document=live_document,
            raw_state_document=raw_state_document,
            terraform_configuration=terraform_configuration,
            terraform_root=terraform_root,
            source_commit=source_commit,
            registry=registry,
            sealed_runtime_snapshot=(
                wrapper_runtime_snapshot or not wrapper_preflight
            ),
        )
    finally:
        if close_plan_descriptor:
            os.close(plan_descriptor)


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    """Load the complete, value-free durable-credential policy."""

    if path.is_symlink() or not path.is_file():
        raise GuardError("durable credential registry is absent or unsafe")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "fs2-serve.nebius.ai/durable-credential-registry/v3":
        raise GuardError("durable credential registry has the wrong schema")
    credentials = document.get("credentials")
    resources = document.get("terraform_resource_addresses")
    activation_resources = document.get("credential_activation_resource_addresses")
    if not isinstance(credentials, list) or not credentials:
        raise GuardError("durable credential registry is empty")
    if (
        not isinstance(resources, list)
        or not resources
        or not all(
            isinstance(item, dict)
            and set(item) == {"root", "address"}
            and item["root"]
            in {"infrastructure", "foundation", "workloads", "reference-data"}
            and isinstance(item["address"], str)
            and item["address"]
            for item in resources
        )
        or len({(item["root"], item["address"]) for item in resources})
        != len(resources)
    ):
        raise GuardError("durable credential Terraform resource inventory is invalid")
    if (
        not isinstance(activation_resources, list)
        or not activation_resources
        or not all(
            isinstance(item, dict)
            and set(item) == {"root", "address"}
            and item["root"] in {"infrastructure", "workloads"}
            and item["address"] == "terraform_data.credential_feature_activation"
            for item in activation_resources
        )
        or len({(item["root"], item["address"]) for item in activation_resources})
        != len(activation_resources)
    ):
        raise GuardError("credential activation state-address inventory is invalid")
    identifiers: set[str] = set()
    compiled = 0
    for item in credentials:
        if not isinstance(item, dict):
            raise GuardError("durable credential registry has a malformed entry")
        identifier = item.get("id")
        patterns = item.get("address_regexes")
        expiry = item.get("expiry")
        rotation = item.get("rotation")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in identifiers
            or item.get("terraform_root")
            not in {"infrastructure", "foundation", "workloads", "reference-data"}
            or not isinstance(patterns, list)
            or not all(isinstance(pattern, str) and pattern for pattern in patterns)
            or not isinstance(item.get("owner"), str)
            or not item.get("owner")
            or not isinstance(item.get("purpose"), str)
            or not item.get("purpose")
            or not isinstance(item.get("readers"), list)
            or not item.get("readers")
            or not all(isinstance(reader, str) and reader for reader in item["readers"])
            or not isinstance(expiry, dict)
            or not isinstance(expiry.get("required"), bool)
            or not isinstance(expiry.get("enforced_by"), str)
            or not expiry.get("enforced_by")
            or rotation
            != {
                "strategy": "dual-read-current-write",
                "disable_before_delete": True,
            }
        ):
            raise GuardError(
                f"durable credential registry entry is incomplete: {identifier!r}"
            )
        identifiers.add(identifier)
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise GuardError(
                    f"durable credential registry regex is invalid: {identifier}"
                ) from error
            compiled += 1
    if compiled == 0:
        raise GuardError("durable credential registry has no Terraform addresses")
    presence = document.get("credential_presence")
    if (
        not isinstance(presence, dict)
        or set(presence) != {"required", "feature_gated", "optional"}
        or not isinstance(presence.get("required"), list)
        or not all(isinstance(value, str) and value for value in presence["required"])
        or len(presence["required"]) != len(set(presence["required"]))
        or not isinstance(presence.get("feature_gated"), dict)
        or not isinstance(presence.get("optional"), dict)
    ):
        raise GuardError("durable credential class-presence policy is malformed")
    feature_ids = set(presence["feature_gated"])
    optional_ids = set(presence["optional"])
    required_ids = set(presence["required"])
    if (
        required_ids & feature_ids
        or required_ids & optional_ids
        or feature_ids & optional_ids
        or required_ids | feature_ids | optional_ids != identifiers
    ):
        raise GuardError("durable credential class-presence policy is not exhaustive")
    declared_resources = {
        (item["root"], item["address"]) for item in resources
    }
    for identifier, activation in presence["optional"].items():
        if (
            not isinstance(activation, dict)
            or set(activation) != {"activation", "required_addresses"}
            or activation.get("activation")
            != "all-authoritative-addresses-observed"
            or not isinstance(activation.get("required_addresses"), list)
            or not activation["required_addresses"]
            or any(
                not isinstance(address, dict)
                or set(address) != {"root", "address"}
                or (address["root"], address["address"])
                not in declared_resources
                or not any(
                    entry["id"] == identifier
                    and entry["terraform_root"] == address["root"]
                    and any(
                        re.fullmatch(pattern, address["address"])
                        for pattern in entry["address_regexes"]
                    )
                    for entry in credentials
                )
                for address in activation["required_addresses"]
            )
        ):
            raise GuardError(
                f"optional credential activation policy is malformed: {identifier}"
            )
    declared_activation_resources = {
        (item["root"], item["address"]) for item in activation_resources
    }
    used_activation_resources: set[tuple[str, str]] = set()
    for identifier, activation in presence["feature_gated"].items():
        groups = activation.get("groups") if isinstance(activation, dict) else None
        if (
            not isinstance(activation, dict)
            or set(activation) != {"activation", "groups"}
            or activation.get("activation")
            != "authoritative-state-marker-groups"
            or not isinstance(groups, dict)
            or not groups
        ):
            raise GuardError(
                f"feature credential activation policy is malformed: {identifier}"
            )
        managed_union: set[tuple[str, str]] = set()
        for group_name, group in groups.items():
            source = group.get("source") if isinstance(group, dict) else None
            required_addresses = (
                group.get("required_addresses") if isinstance(group, dict) else None
            )
            managed_addresses = (
                group.get("managed_addresses") if isinstance(group, dict) else None
            )
            if (
                not isinstance(group_name, str)
                or not group_name
                or not isinstance(group, dict)
                or set(group)
                != {"source", "required_addresses", "managed_addresses"}
                or not isinstance(source, dict)
                or set(source) != {"root", "address"}
                or (source.get("root"), source.get("address"))
                not in declared_activation_resources
                or not isinstance(required_addresses, list)
                or not required_addresses
                or not isinstance(managed_addresses, list)
                or not managed_addresses
            ):
                raise GuardError(
                    f"feature credential activation group is malformed: {identifier}:{group_name}"
                )
            required_keys = {
                (item.get("root"), item.get("address"))
                for item in required_addresses
                if isinstance(item, dict) and set(item) == {"root", "address"}
            }
            managed_keys = {
                (item.get("root"), item.get("address"))
                for item in managed_addresses
                if isinstance(item, dict) and set(item) == {"root", "address"}
            }
            if (
                len(required_keys) != len(required_addresses)
                or len(managed_keys) != len(managed_addresses)
                or not required_keys <= managed_keys
                or managed_keys & managed_union
                or any(key not in declared_resources for key in managed_keys)
                or any(
                    not any(
                        entry["id"] == identifier
                        and entry["terraform_root"] == root
                        and any(
                            re.fullmatch(pattern, address)
                            for pattern in entry["address_regexes"]
                        )
                        for entry in credentials
                    )
                    for root, address in managed_keys
                )
            ):
                raise GuardError(
                    f"feature credential address set is malformed: {identifier}:{group_name}"
                )
            managed_union.update(managed_keys)
            used_activation_resources.add((source["root"], source["address"]))
        declared_for_class = {
            (root, address)
            for root, address in declared_resources
            if any(
                entry["id"] == identifier
                and entry["terraform_root"] == root
                and any(
                    re.fullmatch(pattern, address)
                    for pattern in entry["address_regexes"]
                )
                for entry in credentials
            )
        }
        if managed_union != declared_for_class:
            raise GuardError(
                f"feature credential groups do not cover exact class addresses: {identifier}"
            )
    if used_activation_resources != declared_activation_resources:
        raise GuardError("credential activation state addresses are not exactly consumed")
    adoptions = document.get("legacy_v1_secret_adoptions")
    adoption_keys: set[tuple[str, str, str]] = set()
    if not isinstance(adoptions, list) or not adoptions:
        raise GuardError("legacy v1 Secret adoption registry is absent")
    for adoption in adoptions:
        if (
            not isinstance(adoption, dict)
            or set(adoption) != {"credential_class", "root", "address"}
            or adoption.get("credential_class") not in identifiers
            or (adoption.get("root"), adoption.get("address"))
            not in declared_resources
            or credential_resource_type(adoption.get("address"))
            != "kubernetes_secret_v1"
            or "_versioned" in str(adoption.get("address", ""))
            or not any(
                entry["id"] == adoption.get("credential_class")
                and entry["terraform_root"] == adoption.get("root")
                and any(
                    re.fullmatch(pattern, str(adoption.get("address", "")))
                    for pattern in entry["address_regexes"]
                )
                for entry in credentials
            )
        ):
            raise GuardError("legacy v1 Secret adoption entry is malformed")
        key = (
            adoption["root"],
            adoption["address"],
            adoption["credential_class"],
        )
        if key in adoption_keys:
            raise GuardError("legacy v1 Secret adoption entry is duplicated")
        adoption_keys.add(key)
    legacy_secret_addresses = {
        (root, address)
        for root, address in declared_resources
        if credential_resource_type(address) == "kubernetes_secret_v1"
        and "_versioned" not in address
    }
    adopted_secret_addresses = {(root, address) for root, address, _ in adoption_keys}
    if adopted_secret_addresses != legacy_secret_addresses:
        raise GuardError(
            "legacy v1 Secret adoption registry does not cover exact fixed addresses"
        )
    for resource in resources:
        matches = [
            entry["id"]
            for entry in credentials
            if entry["terraform_root"] == resource["root"]
            and any(
                re.fullmatch(pattern, candidate)
                for pattern in entry["address_regexes"]
                for candidate in (
                    resource["address"],
                    *(
                        resource["address"] + suffix
                        for suffix in REGISTRY_BASE_ADDRESS_PROBES
                    ),
                )
            )
        ]
        if not matches:
            raise GuardError(
                "every declared Terraform credential address must be protected by "
                f"at least one class: {resource['root']}:{resource['address']}"
            )
    return document


def legacy_v1_adoption_classes(
    registry: dict[str, Any], *, terraform_root: str, address: str
) -> frozenset[str]:
    """Return source-approved classes for one exact, never-mutated predecessor.

    This is deliberately exact at the configuration-address level. Terraform
    instance keys are removed, but the owning root, complete module path, type,
    and resource name must still match. A versioned successor, moved address,
    or caller-provided alias can never enter the legacy exception.
    """

    return frozenset(
        item["credential_class"]
        for item in registry["legacy_v1_secret_adoptions"]
        if item["root"] == terraform_root
        and base_resource_address(item["address"])
        == base_resource_address(address)
        and any(
            entry["id"] == item["credential_class"]
            and entry["terraform_root"] == terraform_root
            and any(
                re.fullmatch(pattern, address)
                for pattern in entry["address_regexes"]
            )
            for entry in registry["credentials"]
        )
    )


def registry_sha256(registry: dict[str, Any]) -> str:
    return canonical_sha256(registry)


def protected_patterns(
    registry: dict[str, Any] | None = None, *, terraform_root: str | None = None
) -> tuple[re.Pattern[str], ...]:
    registry = registry or load_registry()
    return tuple(
        re.compile(pattern)
        for entry in registry["credentials"]
        if terraform_root is None or entry["terraform_root"] == terraform_root
        for pattern in entry["address_regexes"]
    )


def registry_resource_addresses(
    registry: dict[str, Any], *, terraform_root: str
) -> frozenset[str]:
    return frozenset(
        item["address"]
        for item in registry["terraform_resource_addresses"]
        if item["root"] == terraform_root
    )


def base_resource_address(address: Any) -> str | None:
    """Return the declared address with module path but without instance keys."""

    if not isinstance(address, str) or not address:
        return None
    return re.sub(r"\[[^\]]+\]", "", address)


def generation_from_address(address: str) -> int:
    """Parse numeric and composite Terraform instance generations."""

    match = re.search(r"\[([^\]]+)\]$", address)
    if match is None:
        return 1
    try:
        key = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise GuardError(f"Terraform resource has an invalid instance key: {address}") from error
    if isinstance(key, int) and not isinstance(key, bool) and key >= 1:
        return key
    if isinstance(key, str):
        leading = key.partition(":")[0]
        if leading.isdecimal() and int(leading) >= 1:
            return int(leading)
    return 1


def credential_resource_type(address: Any) -> str | None:
    base = base_resource_address(address)
    if base is None:
        return None
    resource = base.rsplit(".", 2)[-2:]
    if len(resource) != 2 or resource[0] not in CREDENTIAL_RESOURCE_TYPES:
        return None
    return resource[0]


def terraform_address_is_data_source(address: Any) -> bool:
    """Return whether an address has Terraform's data-source address shape."""

    base = base_resource_address(address)
    if base is None:
        return False
    parts = base.split(".")
    offset = 0
    while offset + 1 < len(parts) and parts[offset] == "module":
        offset += 2
    return offset < len(parts) and parts[offset] == "data"


def configuration_resource_addresses(document: Any) -> frozenset[str]:
    """Collect managed addresses from the exact configuration embedded in a plan.

    Terraform places managed resources and read-only data sources in the same
    ``configuration.*.resources`` arrays.  A data source can therefore have the
    same terminal type as a durable resource (for example
    ``data.kubernetes_secret_v1.database_ca``).  Only ``mode=managed`` entries
    are part of the durable-resource registry; data sources remain visible to
    Terraform but can never be treated as managed credential custody.
    """

    if not isinstance(document, dict):
        raise GuardError("Terraform plan has no embedded configuration")
    root = document.get("root_module")
    if not isinstance(root, dict):
        raise GuardError("Terraform plan has no embedded root configuration")
    addresses: set[str] = set()
    observed_addresses: set[str] = set()
    pending = [(root, "")]
    while pending:
        module, module_prefix = pending.pop()
        resources = module.get("resources", [])
        if not isinstance(resources, list):
            raise GuardError("Terraform plan configuration has malformed resources")
        for resource in resources:
            if not isinstance(resource, dict) or not isinstance(
                resource.get("address"), str
            ):
                raise GuardError(
                    "Terraform plan configuration has a malformed resource"
                )
            address = base_resource_address(resource["address"])
            if address is None or address in observed_addresses:
                raise GuardError("Terraform plan configuration duplicates a resource")
            observed_addresses.add(address)
            mode = resource.get("mode", "managed")
            if mode not in {"managed", "data"}:
                raise GuardError(
                    "Terraform plan configuration has an unknown resource mode"
                )
            if (
                (module_prefix and not address.startswith(module_prefix))
                or (not module_prefix and address.startswith("module."))
            ):
                raise GuardError(
                    "Terraform resource address differs from its configuration module path"
                )
            if (mode == "data") != terraform_address_is_data_source(address):
                raise GuardError(
                    "Terraform plan configuration resource mode differs from its address"
                )
            if mode == "data":
                continue
            addresses.add(address)
        calls = module.get("module_calls", {})
        if not isinstance(calls, dict):
            raise GuardError("Terraform plan configuration has malformed module calls")
        for call_name, call in calls.items():
            if (
                not isinstance(call_name, str)
                or re.fullmatch(r"[A-Za-z0-9_-]+", call_name) is None
            ):
                raise GuardError(
                    "Terraform plan configuration has a malformed module call name"
                )
            nested = call.get("module") if isinstance(call, dict) else None
            if isinstance(nested, dict):
                pending.append((nested, f"{module_prefix}module.{call_name}."))
    return frozenset(addresses)


def enforce_registry_resource_inventory(
    document: dict[str, Any], *, registry: dict[str, Any], terraform_root: str
) -> frozenset[str]:
    """Make the reviewed durable-address inventory normative, not documentary."""

    declared = registry_resource_addresses(registry, terraform_root=terraform_root)
    declared_activation = frozenset(
        item["address"]
        for item in registry["credential_activation_resource_addresses"]
        if item["root"] == terraform_root
    )
    configured = configuration_resource_addresses(document.get("configuration"))
    configured_credentials = frozenset(
        address
        for address in configured
        if credential_resource_type(address) is not None
    )
    missing = declared - configured_credentials
    unregistered = configured_credentials - declared
    configured_activation = frozenset(
        address
        for address in configured
        if address.endswith("terraform_data.credential_feature_activation")
    )
    missing_activation = declared_activation - configured_activation
    unregistered_activation = configured_activation - declared_activation
    if missing or unregistered or missing_activation or unregistered_activation:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(sorted(missing)))
        if unregistered:
            details.append("unregistered=" + ",".join(sorted(unregistered)))
        if missing_activation:
            details.append(
                "missing-activation=" + ",".join(sorted(missing_activation))
            )
        if unregistered_activation:
            details.append(
                "unregistered-activation="
                + ",".join(sorted(unregistered_activation))
            )
        raise GuardError(
            "Terraform credential/activation address inventory differs from the reviewed registry: "
            + "; ".join(details)
        )
    unprotected = {
        address
        for address in configured_credentials
        if not is_protected_address(
            address, registry=registry, terraform_root=terraform_root
        )
    }
    if unprotected:
        raise GuardError(
            "reviewed credential addresses lack exact class protection: "
            + ",".join(sorted(unprotected))
        )
    return declared


def is_protected_address(
    address: Any,
    *,
    registry: dict[str, Any] | None = None,
    terraform_root: str | None = None,
) -> bool:
    if not isinstance(address, str):
        return False
    candidates = (
        (address,)
        if "[" in address
        else (
            address,
            *(address + suffix for suffix in REGISTRY_BASE_ADDRESS_PROBES),
        )
    )
    return any(
        pattern.fullmatch(candidate)
        for pattern in protected_patterns(registry, terraform_root=terraform_root)
        for candidate in candidates
    )


def state_resources(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        return []
    values = document.get("values", document)
    if not isinstance(values, dict):
        return []
    root = values.get("root_module")
    if not isinstance(root, dict):
        return []
    resources: list[dict[str, Any]] = []
    pending = [root]
    while pending:
        module = pending.pop()
        raw_resources = module.get("resources", [])
        if not isinstance(raw_resources, list):
            raise GuardError("Terraform state contains a malformed resource list")
        for resource in raw_resources:
            if not isinstance(resource, dict):
                raise GuardError("Terraform state contains a malformed resource")
            resources.append(resource)
        children = module.get("child_modules", [])
        if not isinstance(children, list) or not all(
            isinstance(child, dict) for child in children
        ):
            raise GuardError("Terraform state contains malformed child modules")
        pending.extend(children)
    return resources


def protected_state_fingerprints(
    document: Any,
    *,
    registry: dict[str, Any] | None = None,
    terraform_root: str | None = None,
) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for resource in state_resources(document):
        address = resource.get("address")
        if not is_protected_address(
            address, registry=registry, terraform_root=terraform_root
        ):
            continue
        values = resource.get("values")
        if values is None:
            raise GuardError(f"protected state resource lacks values: {address}")
        if address in fingerprints:
            raise GuardError(f"protected state resource is duplicated: {address}")
        fingerprints[address] = canonical_sha256(values)
    # The source configuration must declare every registry address, but state
    # legitimately omits disabled optional/count/for_each instances and
    # dependency-owned resources that have not landed.  Existing protected
    # instances remain fingerprinted exactly; plan inspection separately
    # rejects moves, deletes, replacement, and undeclared configuration.
    return dict(sorted(fingerprints.items()))


def plan_prior_fingerprints(
    document: dict[str, Any],
    *,
    registry: dict[str, Any] | None = None,
    terraform_root: str | None = None,
) -> dict[str, str]:
    if not isinstance(document.get("prior_state"), dict):
        raise GuardError("Terraform plan omits its authoritative prior state")
    prior = protected_state_fingerprints(
        document["prior_state"],
        registry=registry,
        terraform_root=terraform_root,
    )
    return prior


def live_secret_bindings(
    state_document: dict[str, Any],
    live_document: dict[str, Any] | None,
    *,
    registry: dict[str, Any],
    terraform_root: str,
) -> dict[str, dict[str, str]]:
    """Bind Terraform Secret addresses to exact provider-observed live objects.

    Only hashes of Secret data are persisted. UID and resourceVersion prevent a
    receipt for an older object incarnation from authorizing a later apply.
    """

    secret_resources = [
        resource
        for resource in state_resources(state_document)
        if isinstance(resource.get("address"), str)
        and credential_resource_type(resource["address"])
        == "kubernetes_secret_v1"
        and is_protected_address(
            resource["address"],
            registry=registry,
            terraform_root=terraform_root,
        )
    ]
    if not secret_resources:
        return {}
    if not isinstance(live_document, dict) or not isinstance(
        live_document.get("items"), list
    ):
        raise GuardError(
            "protected Kubernetes Secrets require a complete live Secret inventory"
        )
    live: dict[tuple[str, str], dict[str, str]] = {}
    for item in live_document["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
            raise GuardError("live Secret inventory contains a malformed object")
        metadata = item["metadata"]
        annotations = metadata.get("annotations")
        identity = (metadata.get("namespace"), metadata.get("name"))
        if (
            not all(isinstance(value, str) and value for value in identity)
            or not isinstance(metadata.get("uid"), str)
            or not metadata["uid"]
            or not isinstance(metadata.get("resourceVersion"), str)
            or not metadata["resourceVersion"]
        ):
            raise GuardError("live Secret inventory lacks exact identity")
        declared_content = (
            annotations.get("fs2.nebius.ai/content-sha256")
            if isinstance(annotations, dict)
            else None
        )
        authority_content = item.get("authorityContentSha256")
        authority_evidence_id = item.get("authorityEvidenceId")
        authority_observed_at = item.get("authorityObservedAt")
        if item.get("data") is not None or item.get("stringData") is not None:
            raise GuardError("live Secret authority returned forbidden Secret values")
        if (
            not isinstance(authority_content, str)
            or re.fullmatch(r"[0-9a-f]{64}", authority_content) is None
            or not isinstance(authority_evidence_id, str)
            or not authority_evidence_id
            or not isinstance(authority_observed_at, str)
            or not authority_observed_at
        ):
            raise GuardError("live Secret lacks an authoritative content binding")
        parse_timestamp(authority_observed_at)
        if declared_content is not None and declared_content != authority_content:
            raise GuardError(
                "live Secret payload differs from its declared content commitment"
            )
        if identity in live:
            raise GuardError("live Secret inventory contains a duplicate identity")
        live[identity] = {
            "namespace": identity[0],
            "name": identity[1],
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "content_sha256": authority_content,
            "annotation_content": declared_content,
            "authority_evidence_id": authority_evidence_id,
            "authority_observed_at": authority_observed_at,
            "credential_class": (
                annotations.get("fs2.nebius.ai/credential-class")
                if isinstance(annotations, dict)
                else None
            ),
            "generation": (
                annotations.get("fs2.nebius.ai/credential-generation")
                if isinstance(annotations, dict)
                else None
            ),
            "immutable": "true" if item.get("immutable") is True else "false",
        }

    bindings: dict[str, dict[str, str]] = {}
    for resource in secret_resources:
        address = resource["address"]
        values = resource.get("values")
        metadata = values.get("metadata") if isinstance(values, dict) else None
        metadata = metadata[0] if isinstance(metadata, list) and metadata else None
        if not isinstance(metadata, dict):
            raise GuardError(f"protected Secret state lacks metadata: {address}")
        identity = (metadata.get("namespace"), metadata.get("name"))
        binding = live.get(identity)
        if binding is None:
            raise GuardError(
                f"protected Secret is absent from live inventory: {address}"
            )
        state_uid = metadata.get("uid")
        state_rv = metadata.get("resource_version")
        state_annotations = metadata.get("annotations")
        declared_content = binding["annotation_content"]
        matching_classes = {
            entry["id"]
            for entry in registry["credentials"]
            if entry["terraform_root"] == terraform_root
            and any(
                re.fullmatch(pattern, address)
                for pattern in entry["address_regexes"]
            )
        }
        if len(matching_classes) != 1:
            raise GuardError(
                f"protected Secret does not map to one credential class: {address}"
            )
        expected_class = next(iter(matching_classes))
        expected_generation = str(generation_from_address(address))
        legacy_adoption = (
            expected_generation == "1"
            and expected_class
            in legacy_v1_adoption_classes(
                registry, terraform_root=terraform_root, address=address
            )
        )
        state_class = (
            state_annotations.get("fs2.nebius.ai/credential-class")
            if isinstance(state_annotations, dict)
            else None
        )
        state_generation = (
            state_annotations.get("fs2.nebius.ai/credential-generation")
            if isinstance(state_annotations, dict)
            else None
        )
        state_content = (
            state_annotations.get("fs2.nebius.ai/content-sha256")
            if isinstance(state_annotations, dict)
            else None
        )
        complete_annotations = all(
            isinstance(value, str) and value
            for value in (
                binding["credential_class"],
                binding["generation"],
                declared_content,
            )
        )
        legacy_annotations_nonconflicting = (
            legacy_adoption
            and state_class in {None, expected_class}
            and state_generation in {None, "1"}
            and state_content in {None, binding["content_sha256"]}
            and binding["credential_class"] in {None, expected_class}
            and binding["generation"] in {None, "1"}
            and declared_content in {None, binding["content_sha256"]}
        )
        state_is_immutable = values.get("immutable") is True
        live_is_immutable = binding["immutable"] == "true"
        if (
            state_uid != binding["uid"]
            or str(state_rv) != binding["resource_version"]
            or state_is_immutable != live_is_immutable
            or (
                complete_annotations
                and (
                    binding["credential_class"] != expected_class
                    or binding["generation"] != expected_generation
                    or declared_content != binding["content_sha256"]
                    or state_class != binding["credential_class"]
                    or state_generation != binding["generation"]
                    or state_content != declared_content
                )
            )
            or (
                not complete_annotations
                and not legacy_annotations_nonconflicting
            )
            or (not legacy_adoption and not live_is_immutable)
        ):
            raise GuardError(
                f"live Secret UID/RV/class/generation/content/immutable binding differs from Terraform state: {address}"
            )
        bindings[address] = {
            **{
                key: value
                for key, value in binding.items()
                if key != "annotation_content"
            },
            "credential_class": expected_class,
            "generation": expected_generation,
        }
    return dict(sorted(bindings.items()))


def _planned_secret_metadata(
    after: Any, *, address: str, minimum_generation: int = 2
) -> dict[str, Any]:
    if not isinstance(after, dict):
        raise GuardError(f"planned Secret has no after-state: {address}")
    metadata = after.get("metadata")
    metadata = metadata[0] if isinstance(metadata, list) and metadata else metadata
    if not isinstance(metadata, dict):
        raise GuardError(f"planned Secret has no metadata: {address}")
    annotations = metadata.get("annotations")
    if not isinstance(annotations, dict):
        raise GuardError(f"planned Secret has no custody annotations: {address}")
    required = {
        "fs2.nebius.ai/credential-generation",
        "fs2.nebius.ai/content-sha256",
        "fs2.nebius.ai/credential-class",
    }
    if not required.issubset(annotations):
        raise GuardError(f"planned Secret lacks exact custody annotations: {address}")
    generation = annotations["fs2.nebius.ai/credential-generation"]
    content_sha256 = annotations["fs2.nebius.ai/content-sha256"]
    credential_class = annotations["fs2.nebius.ai/credential-class"]
    if (
        not isinstance(generation, str)
        or not generation.isdigit()
        or int(generation) < minimum_generation
        or not isinstance(content_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
        or not isinstance(credential_class, str)
        or not credential_class
        or after.get("immutable") is not True
        or not isinstance(after.get("data_wo_revision"), int)
        or after["data_wo_revision"] < 1
        or not isinstance(metadata.get("name"), str)
        or not metadata["name"]
        or not isinstance(metadata.get("namespace"), str)
        or not metadata["namespace"]
    ):
        raise GuardError(f"planned Secret custody commitment is malformed: {address}")
    return {
        "namespace": metadata["namespace"],
        "name": metadata["name"],
        "generation": generation,
        "credential_class": credential_class,
        "content_sha256": content_sha256,
    }


def planned_secret_commitments(
    document: dict[str, Any],
    *,
    registry: dict[str, Any],
    terraform_root: str,
    greenfield_bootstrap: bool = False,
) -> dict[str, dict[str, Any]]:
    """Bind every new immutable Secret before a Secret-only phase-one apply."""

    commitments: dict[str, dict[str, Any]] = {}
    for change in document.get("resource_changes", []):
        if not isinstance(change, dict):
            continue
        address = change.get("address")
        actions = change.get("change", {}).get("actions")
        if (
            actions == ["create"]
            and isinstance(address, str)
            and credential_resource_type(address) == "kubernetes_secret_v1"
            and is_protected_address(
                address, registry=registry, terraform_root=terraform_root
            )
        ):
            commitments[address] = _planned_secret_metadata(
                change.get("change", {}).get("after"),
                address=address,
                minimum_generation=1 if greenfield_bootstrap else 2,
            )
    return dict(sorted(commitments.items()))


def require_staged_secret_plan(
    document: dict[str, Any], *, commitments: dict[str, dict[str, Any]]
) -> None:
    if not commitments:
        return
    variables = document.get("variables")
    phase = (
        variables.get("credential_migration_phase", {}).get("value")
        if isinstance(variables, dict)
        and isinstance(variables.get("credential_migration_phase"), dict)
        else None
    )
    if phase != "secret-stage":
        raise GuardError(
            "new credential Secrets require credential_migration_phase=secret-stage"
        )
    allowed = set(commitments)
    for change in document.get("resource_changes", []):
        address = change.get("address") if isinstance(change, dict) else None
        actions = (
            change.get("change", {}).get("actions")
            if isinstance(change, dict)
            else None
        )
        if actions not in (["no-op"], ["read"], ["create"]):
            raise GuardError("Secret staging plan contains a non-additive action")
        if (
            actions == ["create"]
            and address not in allowed
            and not (
                isinstance(address, str)
                and address.startswith(
                    "terraform_data.credential_apply_gate_generation["
                )
            )
        ):
            raise GuardError(
                "Secret staging plan must contain only reviewed immutable Secret creates"
            )


def require_greenfield_additive_plan(
    document: dict[str, Any], *, commitments: dict[str, dict[str, Any]]
) -> None:
    """Permit a complete first install while forbidding all mutation semantics."""

    for change in document.get("resource_changes", []):
        if not isinstance(change, dict):
            raise GuardError("greenfield plan contains a malformed change")
        mode = change.get("mode", "managed")
        actions = change.get("change", {}).get("actions")
        address = change.get("address")
        if mode == "data":
            if actions not in (["read"], ["no-op"]):
                raise GuardError("greenfield data source has a mutating action")
            continue
        if actions not in (["create"], ["read"], ["no-op"]):
            raise GuardError("greenfield plan contains a non-additive action")
        if change.get("previous_address") is not None:
            raise GuardError("greenfield plan may not move an existing address")
        if (
            actions == ["create"]
            and credential_resource_type(address) == "kubernetes_secret_v1"
            and address not in commitments
        ):
            raise GuardError(
                "greenfield credential Secret lacks an immutable write-only commitment"
            )


def plan_variable(document: dict[str, Any], name: str) -> Any:
    variables = document.get("variables")
    if not isinstance(variables, dict):
        raise GuardError("Terraform plan has no exact input variable inventory")
    item = variables.get(name)
    if not isinstance(item, dict) or "value" not in item:
        raise GuardError(f"Terraform plan omits required variable {name}")
    return item["value"]


def require_accepted_integration_dependencies(
    path: Path = DEFAULT_INTEGRATION_DEPENDENCIES,
) -> None:
    if path.is_symlink() or not path.is_file():
        raise GuardError("SAI integration dependency ledger is absent or unsafe")
    document = json.loads(path.read_text(encoding="utf-8"))
    dependencies = document.get("dependencies")
    if document.get(
        "schema"
    ) != "fs2-serve.nebius.ai/sai-10-integration-dependencies/v1" or set(
        dependencies or {}
    ) != {"SAI-05", "SAI-06", "SAI-08", "SAI-09"}:
        raise GuardError("SAI integration dependency ledger is malformed")
    for ticket, value in dependencies.items():
        expected_fields = {"status", "commit", "tree"}
        if ticket == "SAI-06":
            expected_fields.add("required_credential_addresses")
        if ticket == "SAI-08":
            expected_fields.add("required_semantics")
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise GuardError(f"SAI integration dependency is malformed: {ticket}")
        if value["status"] not in {
            "accepted-source-ancestor",
            "static-source-go-integration-live-unaccepted",
            "blocked-pending-independent-acceptance",
        }:
            raise GuardError(f"SAI integration dependency has invalid status: {ticket}")
        if value["status"] in {
            "accepted-source-ancestor",
            "static-source-go-integration-live-unaccepted",
        } and not all(
            isinstance(value[field], str)
            and re.fullmatch(r"[0-9a-f]{40}", value[field]) is not None
            for field in ("commit", "tree")
        ):
            raise GuardError(
                f"accepted SAI dependency lacks exact Git identity: {ticket}"
            )
        if ticket == "SAI-06":
            addresses = value["required_credential_addresses"]
            if (
                not isinstance(addresses, dict)
                or set(addresses) != {"infrastructure", "workloads"}
                or any(
                    not isinstance(items, list)
                    or not items
                    or len(items) != len(set(items))
                    or not all(isinstance(item, str) and item for item in items)
                    for items in addresses.values()
                )
            ):
                raise GuardError("SAI-06 pending credential surface is malformed")
        if ticket == "SAI-08":
            semantics = value["required_semantics"]
            if (
                not isinstance(semantics, list)
                or len(semantics) < 5
                or len(semantics) != len(set(semantics))
                or not all(isinstance(item, str) and item for item in semantics)
            ):
                raise GuardError("SAI-08 semantic integration contract is malformed")
    if document.get("integration_authorized") is not True or any(
        value["status"] != "accepted-source-ancestor" for value in dependencies.values()
    ):
        raise GuardError(
            "consumer rollout is blocked: SAI-06 has static SOURCE GO only, "
            "SAI-08/09 lack accepted source successors, and semantic integration "
            "authorization is not recorded"
        )
    for ticket, value in dependencies.items():
        try:
            ancestry = subprocess.run(
                [
                    "git",
                    "-C",
                    str(ROOT),
                    "merge-base",
                    "--is-ancestor",
                    value["commit"],
                    "HEAD",
                ],
                capture_output=True,
                check=False,
            )
            tree = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", f"{value['commit']}^{{tree}}"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise GuardError(
                f"cannot verify accepted SAI dependency: {ticket}"
            ) from error
        if ancestry.returncode != 0 or tree != value["tree"]:
            raise GuardError(
                f"current source does not contain the exact accepted SAI dependency: {ticket}"
            )


def require_consumer_rollout_binding(
    document: dict[str, Any],
    *,
    identity_receipt: dict[str, Any] | None,
    terraform_root: str,
) -> None:
    phase = plan_variable(document, "credential_migration_phase")
    if terraform_root != "workloads":
        if phase == "consumer-rollout":
            raise GuardError("consumer-rollout phase is owned by the workloads root")
        return
    bindings = plan_variable(document, "credential_consumer_bindings")
    binding_sha256 = plan_variable(document, "credential_consumer_binding_sha256")
    readiness_sha256 = plan_variable(
        document, "credential_consumer_readiness_receipt_sha256"
    )
    readiness_path = plan_variable(
        document, "credential_consumer_readiness_receipt_path"
    )
    rollout_step = plan_variable(document, "credential_consumer_rollout_step")
    if phase != "consumer-rollout":
        if (
            bindings not in ({}, None)
            or binding_sha256 not in ("", None)
            or readiness_sha256 not in ("", None)
            or readiness_path not in ("", None)
            or rollout_step not in ("", None)
        ):
            raise GuardError(
                "Secret bindings and readiness receipt are allowed only in consumer-rollout phase"
            )
        return
    require_accepted_integration_dependencies()
    if identity_receipt is None:
        raise GuardError("consumer rollout requires the exact durable identity receipt")
    expected = identity_receipt.get("live_secret_bindings")
    if not isinstance(expected, dict) or not expected:
        raise GuardError(
            "consumer rollout identity receipt has no live Secret bindings"
        )
    if bindings != expected or binding_sha256 != canonical_sha256(expected):
        raise GuardError(
            "consumer rollout inputs differ from exact live Secret UID/resourceVersion/content bindings"
        )
    if (
        not isinstance(readiness_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", readiness_sha256) is None
    ):
        raise GuardError("consumer rollout lacks a class-specific readiness receipt")
    if rollout_step not in {"dual-read", "current-write"}:
        raise GuardError("consumer rollout step must be dual-read or current-write")
    if not isinstance(readiness_path, str) or not readiness_path:
        raise GuardError("consumer rollout lacks its exact readiness receipt path")
    validate_consumer_readiness_receipt(
        Path(readiness_path),
        expected_sha256=readiness_sha256,
        expected_bindings_sha256=binding_sha256,
        expected_phase=(
            "predecessor-ready" if rollout_step == "dual-read" else "dual-read-ready"
        ),
    )
    changed_consumers = 0
    for change in document.get("resource_changes", []):
        if not isinstance(change, dict):
            continue
        address = change.get("address")
        actions = change.get("change", {}).get("actions")
        if credential_resource_type(
            address
        ) == "kubernetes_secret_v1" and actions not in (
            ["no-op"],
            ["read"],
        ):
            raise GuardError(
                "consumer rollout may not create or mutate credential Secrets"
            )
        if (
            isinstance(address, str)
            and (
                address.startswith("helm_release.")
                or address.startswith("kubernetes_deployment_v1.")
                or address.startswith("kubernetes_stateful_set_v1.")
            )
            and actions not in (["no-op"], ["read"])
        ):
            changed_consumers += 1
    if changed_consumers == 0:
        raise GuardError("consumer-rollout plan changes no governed consumer")


def validate_consumer_readiness_payload(
    payload: Any,
    *,
    expected_bindings_sha256: str,
    expected_phase: str,
) -> dict[str, Any]:
    contracts = load_consumer_contracts()
    registry = load_registry()
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "schema",
            "phase",
            "contracts_sha256",
            "bindings",
            "bindings_sha256",
            "inventory",
            "feature_gated_classes",
            "enabled_classes",
            "absent_feature_classes",
            "absent_optional_classes",
            "sources",
            "classes",
        }
        or payload.get("schema")
        != "fs2-serve.nebius.ai/credential-consumer-readiness/v3"
        or payload.get("phase") != expected_phase
        or payload.get("contracts_sha256") != canonical_sha256(contracts)
        or payload.get("bindings_sha256") != expected_bindings_sha256
        or not isinstance(payload.get("bindings"), dict)
        or canonical_sha256(payload["bindings"]) != expected_bindings_sha256
        or not isinstance(payload.get("inventory"), dict)
        or not isinstance(payload.get("feature_gated_classes"), list)
        or not isinstance(payload.get("enabled_classes"), list)
        or not isinstance(payload.get("absent_feature_classes"), list)
        or not isinstance(payload.get("absent_optional_classes"), list)
        or not all(
            isinstance(value, str) and value
            for value in (
                payload["feature_gated_classes"]
                + payload["enabled_classes"]
                + payload["absent_feature_classes"]
                + payload["absent_optional_classes"]
            )
        )
        or not isinstance(payload.get("classes"), dict)
        or not isinstance(payload.get("sources"), dict)
    ):
        raise GuardError(
            "consumer readiness authority returned an incomplete inventory"
        )
    verify_external_evidence(payload["inventory"])
    required = set(registry["credential_presence"]["required"])
    feature_gated = set(registry["credential_presence"]["feature_gated"])
    optional = set(registry["credential_presence"]["optional"])
    payload_feature_gated = set(payload["feature_gated_classes"])
    enabled = set(payload["enabled_classes"])
    absent_feature = set(payload["absent_feature_classes"])
    absent_optional = set(payload["absent_optional_classes"])
    if (
        len(payload["feature_gated_classes"]) != len(payload_feature_gated)
        or len(payload["enabled_classes"]) != len(enabled)
        or len(payload["absent_feature_classes"]) != len(absent_feature)
        or len(payload["absent_optional_classes"]) != len(absent_optional)
        or payload_feature_gated != feature_gated
        or enabled & absent_feature
        or enabled & absent_optional
        or absent_feature & absent_optional
        or enabled | absent_feature | absent_optional != set(contracts)
        or not required <= enabled
        or not absent_feature <= feature_gated
        or not absent_optional <= optional
        or set(payload["classes"]) != enabled
        or set(payload["sources"]) != enabled
        or payload["inventory"].get("enabled_classes")
        != sorted(enabled)
        or payload["inventory"].get("feature_gated_classes")
        != sorted(feature_gated)
        or payload["inventory"].get("absent_feature_classes")
        != sorted(absent_feature)
        or payload["inventory"].get("absent_optional_classes")
        != sorted(absent_optional)
        or payload["inventory"].get("required_classes")
        != sorted(required)
    ):
        raise GuardError(
            "consumer readiness class activation differs from authoritative inventory"
        )
    inventory_classes = payload["inventory"].get("classes")
    if not isinstance(inventory_classes, dict) or set(inventory_classes) != set(
        contracts
    ):
        raise GuardError("consumer readiness inventory omits registered classes")
    for credential_class in absent_feature | absent_optional:
        class_entry = inventory_classes[credential_class]
        expected_presence = (
            "feature-gated"
            if credential_class in absent_feature
            else "optional"
        )
        if (
            not isinstance(class_entry, dict)
            or class_entry.get("availability") != "disabled-absent"
            or class_entry.get("presence") != expected_presence
            or class_entry.get("current_generation") is not None
            or class_entry.get("retained_generations") != []
            or class_entry.get("source_trust") is not None
            or class_entry.get("secret_bindings") != {}
        ):
            raise GuardError(
                f"credential absence is not authoritative: {credential_class}"
            )
    now = utc_now()
    evidence_ids: set[str] = set()
    assigned_addresses: set[str] = set()
    for credential_class in sorted(enabled):
        contract = contracts[credential_class]
        item = payload["classes"][credential_class]
        source = payload["sources"][credential_class]
        if (
            not isinstance(source, dict)
            or set(source) != {"source_trust", "credential_bindings"}
            or not isinstance(source.get("source_trust"), dict)
            or not isinstance(source.get("credential_bindings"), dict)
            or any(
                address in assigned_addresses
                for address in source["credential_bindings"]
            )
        ):
            raise GuardError(
                f"consumer readiness source binding is malformed: {credential_class}"
            )
        source_trust = source["source_trust"]
        if (
            set(source_trust)
            != {
                "credential_class",
                "generation",
                "retained_generations",
                "registry_sha256",
                "credential_identities_sha256",
                "terraform_bindings_sha256",
                "secret_bindings_sha256",
            }
            or source_trust.get("credential_class") != credential_class
            or not isinstance(source_trust.get("generation"), int)
            or source_trust.get("retained_generations")
            != list(range(1, source_trust["generation"] + 1))
            or any(
                re.fullmatch(r"[0-9a-f]{64}", str(source_trust.get(field, "")))
                is None
                for field in (
                    "registry_sha256",
                    "credential_identities_sha256",
                    "terraform_bindings_sha256",
                    "secret_bindings_sha256",
                )
            )
            or source_trust.get("secret_bindings_sha256")
            != canonical_sha256(source["credential_bindings"])
        ):
            raise GuardError(
                f"consumer readiness source trust is malformed: {credential_class}"
            )
        assigned_addresses.update(source["credential_bindings"])
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("externalEvidence"), dict)
            or not isinstance(item.get("authorityObservation"), dict)
            or item.get("adapter") != contract["adapter"]
            or item.get("authority") != contract["authority"]
            or item.get("consumers") != contract["consumers"]
            or item.get("readiness") != contract["readiness"]
            or item.get("ready") is not True
            or not isinstance(item.get("generation"), int)
            or item["generation"] < 1
            or item["generation"] != source_trust["generation"]
            or not isinstance(item.get("consumer_bindings"), list)
            or item.get("source_trust") != source["source_trust"]
            or item.get("credential_bindings")
            != source["credential_bindings"]
            or item.get("credential_bindings_sha256")
            != canonical_sha256(source["credential_bindings"])
            or not isinstance(item.get("observed_at"), str)
        ):
            raise GuardError(
                f"consumer readiness is missing a class-specific adapter proof: {credential_class}"
            )
        verify_external_evidence(item)
        evidence_id = item["externalEvidence"].get("claim", {}).get("evidence_id")
        if not isinstance(evidence_id, str) or evidence_id in evidence_ids:
            raise GuardError("consumer readiness reuses or omits external evidence")
        evidence_ids.add(evidence_id)
        bindings = item["consumer_bindings"]
        source_identity = canonical_sha256(source)
        if (
            len(bindings) != len(contract["consumers"])
            or {binding.get("consumer") for binding in bindings if isinstance(binding, dict)}
            != set(contract["consumers"])
        ):
            raise GuardError(
                f"consumer readiness omits an exact consumer: {credential_class}"
            )
        for binding in bindings:
            if (
                not isinstance(binding, dict)
                or set(binding)
                != {
                    "consumer",
                    "consumer_identity",
                    "credential_identity",
                    "generation",
                    "ready",
                    "observed_at",
                    "readiness_evidence_sha256",
                }
                or binding.get("generation") != item["generation"]
                or binding.get("ready") is not True
                or binding.get("credential_identity")
                != source_identity
                or not all(
                    isinstance(binding.get(field), str) and binding[field]
                    for field in (
                        "consumer_identity",
                        "credential_identity",
                        "observed_at",
                        "readiness_evidence_sha256",
                    )
                )
                or re.fullmatch(
                    r"[0-9a-f]{64}", binding["readiness_evidence_sha256"]
                )
                is None
            ):
                raise GuardError(
                    f"consumer readiness binding is incomplete: {credential_class}"
                )
            binding_time = parse_timestamp(binding["observed_at"])
            if binding_time > now or now - binding_time > timedelta(minutes=5):
                raise GuardError(
                    f"consumer readiness binding is stale: {credential_class}"
                )
        item_observed_at = parse_timestamp(item["observed_at"])
        if item_observed_at > now or now - item_observed_at > timedelta(minutes=15):
            raise GuardError(
                f"consumer readiness class evidence is stale: {credential_class}"
            )
    if assigned_addresses != set(payload["bindings"]):
        raise GuardError(
            "consumer readiness class sources do not partition the exact Secret bindings"
        )
    return payload


def write_consumer_readiness_receipt(
    *, identity_receipt_path: Path, phase: str, path: Path
) -> dict[str, Any]:
    if phase not in {"predecessor-ready", "dual-read-ready", "current-write-ready"}:
        raise GuardError("consumer readiness phase is invalid")
    identity = load_identity_receipt(identity_receipt_path)
    if identity is None:
        raise GuardError("consumer readiness requires an exact identity receipt")
    bindings = identity["live_secret_bindings"]
    bindings_sha256 = canonical_sha256(bindings)
    contracts = load_consumer_contracts()
    inventory = authority_json({"operation": "credential-inventory"})
    verify_external_evidence(inventory)
    if inventory.get("registry_sha256") != identity.get("registry_sha256"):
        raise GuardError("consumer inventory registry differs from custody identity")
    generations = inventory.get("classes")
    inventory_items = inventory.get("items")
    required_classes = inventory.get("required_classes")
    feature_gated_classes = inventory.get("feature_gated_classes")
    enabled_classes = inventory.get("enabled_classes")
    absent_feature_classes = inventory.get("absent_feature_classes")
    absent_optional_classes = inventory.get("absent_optional_classes")
    if (
        not isinstance(generations, dict)
        or set(generations) != set(contracts)
        or not isinstance(inventory_items, list)
        or not isinstance(required_classes, list)
        or not isinstance(feature_gated_classes, list)
        or not isinstance(enabled_classes, list)
        or not isinstance(absent_feature_classes, list)
        or not isinstance(absent_optional_classes, list)
        or not all(
            isinstance(value, str) and value
            for value in (
                required_classes
                + feature_gated_classes
                + enabled_classes
                + absent_feature_classes
                + absent_optional_classes
            )
        )
        or set(required_classes)
        != set(load_registry()["credential_presence"]["required"])
        or set(feature_gated_classes)
        != set(load_registry()["credential_presence"]["feature_gated"])
        or set(enabled_classes)
        | set(absent_feature_classes)
        | set(absent_optional_classes)
        != set(contracts)
        or set(enabled_classes) & set(absent_feature_classes)
        or set(enabled_classes) & set(absent_optional_classes)
        or set(absent_feature_classes) & set(absent_optional_classes)
    ):
        raise GuardError("credential inventory omits a registered class")
    classes: dict[str, Any] = {}
    sources: dict[str, Any] = {}
    for credential_class in sorted(enabled_classes):
        class_source = generations[credential_class]
        generation = (
            class_source.get("current_generation")
            if isinstance(class_source, dict)
            else None
        )
        if not isinstance(generation, int) or generation < 1:
            raise GuardError(f"credential generation is absent: {credential_class}")
        matches = [
            item
            for item in inventory_items
            if isinstance(item, dict)
            and item.get("credential_class") == credential_class
            and item.get("generation") == generation
        ]
        if len(matches) != 1:
            raise GuardError(
                f"credential source is absent or ambiguous: {credential_class}"
            )
        source_trust = class_source.get("source_trust")
        class_bindings = class_source.get("secret_bindings")
        if (
            not isinstance(source_trust, dict)
            or not isinstance(class_bindings, dict)
            or any(
                not isinstance(bindings.get(address), dict)
                or {
                    key: bindings[address].get(key)
                    for key in binding
                }
                != binding
                for address, binding in class_bindings.items()
            )
        ):
            raise GuardError(
                f"credential source differs from the exact custody receipt: {credential_class}"
            )
        sources[credential_class] = {
            "source_trust": source_trust,
            "credential_bindings": class_bindings,
        }
        classes[credential_class] = authority_json(
            {
                "operation": "consumer-readiness",
                "credential_class": credential_class,
                "generation": generation,
                "phase": phase,
                "source_trust": source_trust,
                "bindings_sha256": canonical_sha256(class_bindings),
                "credential_bindings": class_bindings,
            }
        )
    payload = {
        "schema": "fs2-serve.nebius.ai/credential-consumer-readiness/v3",
        "phase": phase,
        "contracts_sha256": canonical_sha256(contracts),
        "bindings": bindings,
        "bindings_sha256": bindings_sha256,
        "inventory": inventory,
        "feature_gated_classes": sorted(feature_gated_classes),
        "enabled_classes": sorted(enabled_classes),
        "absent_feature_classes": sorted(absent_feature_classes),
        "absent_optional_classes": sorted(absent_optional_classes),
        "sources": sources,
        "classes": classes,
    }
    validate_consumer_readiness_payload(
        payload,
        expected_bindings_sha256=bindings_sha256,
        expected_phase=phase,
    )
    receipt = {
        "schema": "fs2-serve.nebius.ai/credential-consumer-readiness-receipt/v1",
        "captured_at": utc_now().isoformat().replace("+00:00", "Z"),
        "identity_receipt_sha256": file_sha256(identity_receipt_path),
        "payload": payload,
    }
    write_private_json(path, receipt)
    return receipt


def validate_consumer_readiness_receipt(
    path: Path,
    *,
    expected_sha256: str,
    expected_bindings_sha256: str,
    expected_phase: str,
) -> dict[str, Any]:
    if file_sha256(path) != expected_sha256:
        raise GuardError("consumer readiness receipt hash differs from the plan")
    receipt = load_private_document(path, label="consumer readiness receipt")
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {"schema", "captured_at", "identity_receipt_sha256", "payload"}
        or receipt.get("schema")
        != "fs2-serve.nebius.ai/credential-consumer-readiness-receipt/v1"
        or not isinstance(receipt.get("identity_receipt_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt["identity_receipt_sha256"]) is None
    ):
        raise GuardError("consumer readiness receipt is malformed")
    parse_timestamp(receipt.get("captured_at"))
    validate_consumer_readiness_payload(
        receipt.get("payload"),
        expected_bindings_sha256=expected_bindings_sha256,
        expected_phase=expected_phase,
    )
    return receipt


def require_additive_apply_gate_generation(document: dict[str, Any]) -> None:
    """Require one new permanent gate instance in every saved plan.

    This makes the execution-time provisioner unavoidable for direct saved-plan
    apply without replacing or deleting an earlier gate generation.
    """

    current = plan_variable(document, "credential_migration_gate_receipt_sha256")
    history = plan_variable(document, "credential_migration_gate_history")
    if not isinstance(current, str) or re.fullmatch(r"[0-9a-f]{64}", current) is None:
        raise GuardError("plan lacks the exact current apply-gate receipt hash")
    if (
        not isinstance(history, list)
        or len(history) != len(set(history))
        or any(
            not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for digest in history
        )
        or current in history
    ):
        raise GuardError(
            "apply-gate history is malformed or reuses the current receipt"
        )
    pattern = re.compile(
        r'^terraform_data\.credential_apply_gate_generation\["([0-9a-f]{64})"\]$'
    )
    observed: dict[str, list[str]] = {}
    for change in document.get("resource_changes", []):
        if not isinstance(change, dict):
            continue
        address = change.get("address")
        if not isinstance(address, str) or not address.startswith(
            "terraform_data.credential_apply_gate_generation"
        ):
            continue
        match = pattern.fullmatch(address)
        actions = change.get("change", {}).get("actions")
        if (
            match is None
            or change.get("previous_address") is not None
            or actions not in (["no-op"], ["read"], ["create"])
        ):
            raise GuardError(
                "apply-gate generation would move, update, replace or delete"
            )
        observed[match.group(1)] = actions
    expected = set(history) | {current}
    if set(observed) != expected or observed.get(current) != ["create"]:
        raise GuardError(
            "saved plan must retain every prior gate and create exactly its current gate"
        )
    if any(observed[digest] not in (["no-op"], ["read"]) for digest in history):
        raise GuardError("saved plan would mutate a prior apply-gate generation")


def write_identity_receipt(
    state_document: dict[str, Any],
    path: Path,
    *,
    source_commit: str,
    registry: dict[str, Any] | None = None,
    terraform_root: str = "workloads",
    live_secret_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise GuardError("identity receipt is write-once and already exists")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise GuardError("identity receipt parent must be a real directory")
    if (
        parent.stat().st_uid != os.geteuid()
        or stat.S_IMODE(parent.stat().st_mode) & 0o077
    ):
        raise GuardError("identity receipt parent must be owner-owned and owner-only")
    if len(source_commit) != 40 or any(
        character not in "0123456789abcdef" for character in source_commit
    ):
        raise GuardError("source commit must be an exact lowercase Git SHA")
    registry = registry or load_registry()
    if live_secret_document is not None:
        raise GuardError(
            "caller-supplied live Secret inventories are forbidden; use the fixed authority"
        )
    fingerprints = protected_state_fingerprints(
        state_document, registry=registry, terraform_root=terraform_root
    )
    if not fingerprints:
        raise GuardError("state contains no protected generation-1 resources")
    secret_bindings = live_secret_bindings(
        state_document,
        live_secret_inventory_for_state(
            state_document, registry=registry, terraform_root=terraform_root
        ),
        registry=registry,
        terraform_root=terraform_root,
    )
    receipt = {
        "schema": "fs2-serve.nebius.ai/durable-identity/v2",
        "source_commit": source_commit,
        "terraform_root": terraform_root,
        "registry_sha256": registry_sha256(registry),
        "captured_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "address_fingerprints": fingerprints,
        "live_secret_bindings": secret_bindings,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if path.exists():
            path.chmod(0o600)
    return receipt


def load_identity_receipt(
    path: Path | None,
    *,
    registry: dict[str, Any] | None = None,
    terraform_root: str | None = None,
) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise GuardError("identity receipt must be a regular file")
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise GuardError("identity receipt must be owner-owned mode 0600")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("schema") != "fs2-serve.nebius.ai/durable-identity/v2":
        raise GuardError("identity receipt has the wrong schema")
    registry = registry or load_registry()
    if receipt.get("registry_sha256") != registry_sha256(registry):
        raise GuardError("identity receipt is not bound to the current registry")
    if terraform_root is not None and receipt.get("terraform_root") != terraform_root:
        raise GuardError("identity receipt is bound to a different Terraform root")
    fingerprints = receipt.get("address_fingerprints")
    if not isinstance(fingerprints, dict) or not all(
        is_protected_address(
            address,
            registry=registry,
            terraform_root=receipt.get("terraform_root"),
        )
        and isinstance(fingerprint, str)
        and len(fingerprint) == 64
        and all(character in "0123456789abcdef" for character in fingerprint)
        for address, fingerprint in fingerprints.items()
    ):
        raise GuardError("identity receipt has malformed protected fingerprints")
    bindings = receipt.get("live_secret_bindings")
    if not isinstance(bindings, dict) or not all(
        isinstance(address, str)
        and is_protected_address(
            address,
            registry=registry,
            terraform_root=receipt.get("terraform_root"),
        )
        and isinstance(binding, dict)
        and set(binding)
        == {
            "namespace",
            "name",
            "uid",
            "resource_version",
            "content_sha256",
            "authority_evidence_id",
            "authority_observed_at",
            "credential_class",
            "generation",
            "immutable",
        }
        and all(isinstance(binding[key], str) and binding[key] for key in binding)
        and len(binding["content_sha256"]) == 64
        and binding["immutable"] in {"true", "false"}
        for address, binding in bindings.items()
    ):
        raise GuardError("identity receipt has malformed live Secret bindings")
    expected_secret_addresses = {
        address
        for address in fingerprints
        if credential_resource_type(address) == "kubernetes_secret_v1"
    }
    if set(bindings) != expected_secret_addresses:
        raise GuardError(
            "identity receipt does not bind every protected Kubernetes Secret"
        )
    return receipt


def validate_identity_receipt_state(
    state_document: dict[str, Any],
    path: Path,
    *,
    registry: dict[str, Any] | None = None,
    terraform_root: str,
) -> dict[str, Any]:
    """Re-observe state/live bindings for an existing write-once receipt."""

    registry = registry or load_registry()
    receipt = load_identity_receipt(
        path, registry=registry, terraform_root=terraform_root
    )
    if receipt is None:
        raise GuardError("durable identity receipt is absent")
    fingerprints = protected_state_fingerprints(
        state_document, registry=registry, terraform_root=terraform_root
    )
    bindings = live_secret_bindings(
        state_document,
        live_secret_inventory_for_state(
            state_document, registry=registry, terraform_root=terraform_root
        ),
        registry=registry,
        terraform_root=terraform_root,
    )
    if (
        receipt.get("address_fingerprints") != fingerprints
        or receipt.get("live_secret_bindings") != bindings
    ):
        raise GuardError(
            "durable identity receipt differs from authoritative state or live Secrets"
        )
    return {
        "status": "pass",
        "receipt_sha256": file_sha256(path),
        "protected_identities": len(fingerprints),
        "live_secret_bindings": len(bindings),
    }


def inspect_plan(
    document: dict[str, Any],
    *,
    identity_receipt: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
    terraform_root: str | None = None,
    greenfield_bootstrap: bool = False,
) -> dict[str, int]:
    registry = registry or load_registry()
    if terraform_root is None:
        raise GuardError("Terraform root is required for credential plan inspection")
    if terraform_root == "configuration":
        if identity_receipt is not None:
            raise GuardError(
                "credential-free configuration root cannot consume a durable identity receipt"
            )
        return inspect_configuration_plan(
            document, greenfield_bootstrap=greenfield_bootstrap
        )
    required_addresses = enforce_registry_resource_inventory(
        document, registry=registry, terraform_root=terraform_root
    )
    prior_fingerprints = plan_prior_fingerprints(
        document, registry=registry, terraform_root=terraform_root
    )
    if greenfield_bootstrap:
        require_empty_greenfield_state(document.get("prior_state"))
        if prior_fingerprints or identity_receipt is not None:
            raise GuardError(
                "greenfield bootstrap cannot use prior credential state or adoption evidence"
            )
    if prior_fingerprints:
        if identity_receipt is None:
            raise GuardError(
                "existing generation-1 resources require a fixed-v1 identity receipt"
            )
        if identity_receipt.get("registry_sha256") != registry_sha256(registry):
            raise GuardError("identity receipt is not bound to the current registry")
        if identity_receipt["address_fingerprints"] != prior_fingerprints:
            raise GuardError(
                "live generation-1 resource identities differ from the fixed-v1 receipt"
            )
    raw_changes = document.get("resource_changes", [])
    if not isinstance(raw_changes, list):
        raise GuardError("Terraform plan contains no resource change inventory")
    if prior_fingerprints and "resource_changes" not in document:
        raise GuardError("Terraform plan contains no resource change inventory")
    protected_changes = 0
    seen_addresses: set[str] = set()
    seen_protected: set[str] = set()
    for change in raw_changes:
        if not isinstance(change, dict):
            raise GuardError("Terraform plan contains a malformed resource change")
        address = change.get("address")
        previous_address = change.get("previous_address")
        actions = change.get("change", {}).get("actions", [])
        if not isinstance(address, str) or address in seen_addresses:
            raise GuardError("Terraform plan contains a missing or duplicate address")
        seen_addresses.add(address)
        mode = change.get("mode", "managed")
        if mode not in {"managed", "data"}:
            raise GuardError("Terraform plan contains an unknown resource mode")
        if (mode == "data") != terraform_address_is_data_source(address):
            raise GuardError("Terraform plan resource mode differs from its address")
        if previous_address is not None and not isinstance(previous_address, str):
            raise GuardError("Terraform plan contains a malformed previous_address")
        if mode == "data":
            if previous_address is not None or actions not in (["read"], ["no-op"]):
                raise GuardError(
                    "Terraform data source has a managed-resource change shape"
                )
            continue
        protected = is_protected_address(
            address, registry=registry, terraform_root=terraform_root
        )
        previous_protected = is_protected_address(
            previous_address, registry=registry, terraform_root=terraform_root
        )
        base = base_resource_address(address)
        previous_base = base_resource_address(previous_address)
        credential_shaped = credential_resource_type(address) is not None
        previous_credential_shaped = (
            credential_resource_type(previous_address) is not None
        )
        if credential_shaped and base not in required_addresses:
            raise GuardError(
                f"plan contains an unregistered credential resource address: {address}"
            )
        if previous_credential_shaped and previous_base not in required_addresses:
            raise GuardError(
                "plan contains a credential moved from or deleted through an "
                f"unregistered address: {previous_address}"
            )
        if previous_address is not None and (protected or previous_protected):
            raise GuardError(
                "plan would move a durable credential address: "
                f"{previous_address} -> {address}"
            )
        if previous_address is not None and (
            credential_shaped or previous_credential_shaped
        ):
            raise GuardError(
                "plan moves a credential resource regardless of its current registry match"
            )
        if credential_shaped and not protected:
            raise GuardError(
                f"registry regexes do not protect declared credential address {address}"
            )
        if protected:
            seen_protected.add(address)
        if protected and actions not in (["no-op"], ["read"], ["create"]):
            raise GuardError(
                f"plan would update, replace, or delete protected legacy address {address}"
            )
        if protected and actions == ["create"] and address in prior_fingerprints:
            raise GuardError(
                f"plan would recreate protected legacy address with recorded prior state {address}"
            )
        if (
            protected
            and actions == ["create"]
            and not greenfield_bootstrap
            and not (
                isinstance(base, str)
                and base.endswith("_versioned")
                and address != base
            )
        ):
            raise GuardError(
                "only a new instance of a registered append-only *_versioned resource "
                f"may be created; fixed address create refused: {address}"
            )
        if protected and actions != ["no-op"]:
            protected_changes += 1
    omitted = set(prior_fingerprints) - seen_protected
    if omitted:
        raise GuardError(
            "Terraform plan omitted protected prior-state addresses: "
            + ", ".join(sorted(omitted))
        )
    commitments = planned_secret_commitments(
        document,
        registry=registry,
        terraform_root=terraform_root,
        greenfield_bootstrap=greenfield_bootstrap,
    )
    if greenfield_bootstrap:
        require_greenfield_additive_plan(document, commitments=commitments)
    else:
        require_staged_secret_plan(document, commitments=commitments)
    require_consumer_rollout_binding(
        document,
        identity_receipt=identity_receipt,
        terraform_root=terraform_root,
    )
    require_additive_apply_gate_generation(document)
    return {
        "protected_addresses": len(required_addresses),
        "verified_identities": len(prior_fingerprints),
        "protected_changes": protected_changes,
        "planned_secret_commitments": len(commitments),
    }


def inspect_configuration_plan(
    document: dict[str, Any], *, greenfield_bootstrap: bool
) -> dict[str, int]:
    """Prove the deployment-contract root cannot carry durable credentials.

    The top-level configuration root intentionally has no durable credential.
    It still carries the same append-only native apply-gate generation as each
    stage: any missing/stale gate, managed credential-shaped address, moved
    address, unknown mode, or data-source mutation fails closed.
    """

    configured = configuration_resource_addresses(document.get("configuration"))
    credential_addresses = sorted(
        address
        for address in configured
        if credential_resource_type(address) is not None
    )
    if credential_addresses:
        raise GuardError(
            "configuration root contains a durable credential resource: "
            + ",".join(credential_addresses)
        )
    if greenfield_bootstrap:
        require_empty_greenfield_state(document.get("prior_state"))
    changes = document.get("resource_changes", [])
    if not isinstance(changes, list):
        raise GuardError("configuration plan contains no resource change inventory")
    for change in changes:
        if not isinstance(change, dict):
            raise GuardError("configuration plan contains a malformed change")
        address = change.get("address")
        mode = change.get("mode", "managed")
        actions = change.get("change", {}).get("actions", [])
        if not isinstance(address, str) or mode not in {"managed", "data"}:
            raise GuardError("configuration plan resource identity is malformed")
        if (mode == "data") != terraform_address_is_data_source(address):
            raise GuardError(
                "configuration plan resource mode differs from its address"
            )
        if credential_resource_type(address) is not None:
            raise GuardError(
                f"configuration plan contains a credential-shaped change: {address}"
            )
        if change.get("previous_address") is not None:
            raise GuardError("configuration plan may not move a resource address")
        if mode == "data" and actions not in (["read"], ["no-op"]):
            raise GuardError("configuration data source has a mutating action")
        if greenfield_bootstrap and mode == "managed" and actions not in (
            ["create"],
            ["read"],
            ["no-op"],
        ):
            raise GuardError("greenfield configuration plan is not additive")
    require_additive_apply_gate_generation(document)
    return {
        "protected_addresses": 0,
        "verified_identities": 0,
        "protected_changes": 0,
        "planned_secret_commitments": 0,
    }


def is_plaintext_artifact(name: str) -> bool:
    """Classify legacy state, plans, cookies, and scoped credential exports."""

    normalized = name.lower()
    return (
        normalized.endswith(SENSITIVE_ARTIFACT_SUFFIXES)
        or ".tfstate." in normalized
        or ".tfplan." in normalized
        or normalized.endswith(".plan.json")
        or any(
            normalized == f"{prefix}.json"
            or normalized.startswith((f"{prefix}.", f"{prefix}-"))
            for prefix in SCOPED_CREDENTIAL_PREFIXES
        )
    )


def file_sha256(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise GuardError(f"run-root entry is not a regular file: {path}")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise GuardError(f"run-root file is not owner-owned and owner-only: {path}")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _descriptor_sha256(descriptor: int, *, display_path: Path) -> str:
    """Hash one stable, owner-only regular-file descriptor."""

    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise GuardError(f"run-root entry is not a regular file: {display_path}")
    if before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o077:
        raise GuardError(
            f"run-root file is not owner-owned and owner-only: {display_path}"
        )
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    after = os.fstat(descriptor)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise GuardError(f"run-root file changed while inventoried: {display_path}")
    return digest.hexdigest()


def _inventory_run_root(root: Path) -> tuple[list[dict[str, Any]], os.stat_result]:
    """Inventory from stable directory/file descriptors, rejecting all symlinks."""

    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise GuardError("run root must be a real directory")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    root_descriptor = os.open(root, directory_flags)
    inventory: list[dict[str, Any]] = []
    try:
        root_metadata = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(root_metadata.st_mode) & 0o077
        ):
            raise GuardError("run root must be an owner-owned, owner-only directory")
        for directory, directory_names, file_names, directory_descriptor in os.fwalk(
            ".", topdown=True, follow_symlinks=False, dir_fd=root_descriptor
        ):
            current_relative = Path(directory)
            current_metadata = os.fstat(directory_descriptor)
            if (
                not stat.S_ISDIR(current_metadata.st_mode)
                or current_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(current_metadata.st_mode) & 0o077
            ):
                raise GuardError(
                    "run-root directory is not owner-owned and owner-only: "
                    f"{root / current_relative}"
                )
            for name in directory_names:
                child = os.stat(
                    name, dir_fd=directory_descriptor, follow_symlinks=False
                )
                if stat.S_ISLNK(child.st_mode):
                    raise GuardError(
                        "run root contains a directory symlink: "
                        f"{root / current_relative / name}"
                    )
                if not stat.S_ISDIR(child.st_mode):
                    raise GuardError(
                        "run-root traversal encountered a non-directory: "
                        f"{root / current_relative / name}"
                    )
            for name in file_names:
                relative_path = (current_relative / name).relative_to(".")
                display_path = root / relative_path
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
                except OSError as error:
                    raise GuardError(
                        f"cannot safely open run-root entry: {display_path}"
                    ) from error
                try:
                    digest = _descriptor_sha256(descriptor, display_path=display_path)
                finally:
                    os.close(descriptor)
                inventory.append(
                    {
                        "path": relative_path.as_posix(),
                        "sha256": digest,
                        "classification": (
                            "known-sensitive"
                            if is_plaintext_artifact(name)
                            else "unknown"
                        ),
                    }
                )
        path_metadata = os.stat(root, follow_symlinks=False)
        if (path_metadata.st_dev, path_metadata.st_ino) != (
            root_metadata.st_dev,
            root_metadata.st_ino,
        ):
            raise GuardError("run root changed while it was inventoried")
        return sorted(inventory, key=lambda item: item["path"]), root_metadata
    finally:
        os.close(root_descriptor)


def inventory_run_root(root: Path) -> list[dict[str, Any]]:
    inventory, _ = _inventory_run_root(root)
    return inventory


def authoritative_operator_state_root() -> Path:
    """Return the non-overridable home scope for every product state family."""

    try:
        owner_home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError as error:
        raise GuardError("effective OS account has no authoritative home") from error
    if not owner_home.is_absolute():
        raise GuardError("effective OS account has no absolute authoritative home")
    return (owner_home / AUTHORITATIVE_STATE_SCOPE_RELATIVE).resolve()


def require_authoritative_scope(root: Path) -> str:
    """Require the fixed platform-wide owner scope, never a caller-selected root."""

    configured_path = authoritative_operator_state_root()
    if configured_path.is_symlink() or not configured_path.is_dir():
        raise GuardError("authoritative owner scope must be a real directory")
    if configured_path.resolve() != root.absolute().resolve():
        raise GuardError("requested run root is not the authoritative owner scope")
    metadata = configured_path.stat()
    if metadata.st_uid != os.geteuid():
        raise GuardError(
            "authoritative owner scope must be owned by the effective account"
        )
    return hashlib.sha256(str(configured_path.resolve()).encode()).hexdigest()


def _inventory_authoritative_scope(
    scope: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], os.stat_result]:
    """Inventory every code-defined product state family below the fixed scope.

    The caller supplies only the fixed parent returned by the OS account
    database.  It cannot select one run directory and omit a sibling family.
    Missing families are recorded so a newly created family also invalidates a
    pre-retirement manifest.
    """

    scope = scope.absolute()
    scope_metadata = os.stat(scope, follow_symlinks=False)
    artifacts: list[dict[str, Any]] = []
    roots: list[dict[str, Any]] = []
    for name in AUTHORITATIVE_STATE_ROOT_NAMES:
        child = scope / name
        try:
            child_metadata = os.stat(child, follow_symlinks=False)
        except FileNotFoundError:
            roots.append(
                {"path": name, "present": False, "device": None, "inode": None}
            )
            continue
        if stat.S_ISLNK(child_metadata.st_mode) or not stat.S_ISDIR(
            child_metadata.st_mode
        ):
            raise GuardError(
                f"authoritative state family is not a real directory: {name}"
            )
        child_artifacts, stable_metadata = _inventory_run_root(child)
        roots.append(
            {
                "path": name,
                "present": True,
                "device": stable_metadata.st_dev,
                "inode": stable_metadata.st_ino,
            }
        )
        artifacts.extend(
            {
                **item,
                "path": (Path(name) / item["path"]).as_posix(),
            }
            for item in child_artifacts
        )
    final_scope_metadata = os.stat(scope, follow_symlinks=False)
    if (scope_metadata.st_dev, scope_metadata.st_ino) != (
        final_scope_metadata.st_dev,
        final_scope_metadata.st_ino,
    ):
        raise GuardError("authoritative state scope changed while inventoried")
    return sorted(artifacts, key=lambda item: item["path"]), roots, scope_metadata


def _validate_scope_roots(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(AUTHORITATIVE_STATE_ROOT_NAMES):
        raise GuardError("artifact manifest has malformed authoritative roots")
    if [item.get("path") for item in value if isinstance(item, dict)] != list(
        AUTHORITATIVE_STATE_ROOT_NAMES
    ):
        raise GuardError("artifact manifest omits an authoritative state family")
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "present",
            "device",
            "inode",
        }:
            raise GuardError("artifact manifest has malformed authoritative roots")
        if not isinstance(item["present"], bool):
            raise GuardError("artifact manifest has malformed authoritative roots")
        if item["present"]:
            if not all(
                isinstance(item[field], int) and item[field] >= 0
                for field in ("device", "inode")
            ):
                raise GuardError("artifact manifest has malformed authoritative roots")
        elif item["device"] is not None or item["inode"] is not None:
            raise GuardError("artifact manifest has malformed authoritative roots")
    return value


def write_artifact_manifest(root: Path, path: Path) -> dict[str, Any]:
    authority_sha256 = require_authoritative_scope(root)
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise GuardError("artifact manifest is write-once and already exists")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise GuardError("artifact manifest parent must be a real directory")
    if (
        parent.stat().st_uid != os.geteuid()
        or stat.S_IMODE(parent.stat().st_mode) & 0o077
    ):
        raise GuardError("artifact manifest parent must be owner-owned and owner-only")
    if path.is_relative_to(root.absolute()):
        raise GuardError("artifact manifest must be stored outside the retirement root")
    artifacts, scope_roots, root_metadata = _inventory_authoritative_scope(root)
    registry = load_registry()
    receipt = {
        "schema": "fs2-serve.nebius.ai/global-state-artifacts/v3",
        "captured_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "scope_realpath_sha256": hashlib.sha256(
            str(root.absolute().resolve()).encode()
        ).hexdigest(),
        "scope_authority_sha256": authority_sha256,
        "scope_device": root_metadata.st_dev,
        "scope_inode": root_metadata.st_ino,
        "scope_roots": scope_roots,
        "registry_sha256": registry_sha256(registry),
        "artifacts": artifacts,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if path.exists():
            path.chmod(0o600)
    return receipt


def load_artifact_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise GuardError("artifact manifest must be a regular file")
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise GuardError("artifact manifest must be owner-owned mode 0600")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("schema") != "fs2-serve.nebius.ai/global-state-artifacts/v3":
        raise GuardError("artifact manifest has the wrong schema")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not all(
        isinstance(item, dict)
        and set(item) == {"path", "sha256", "classification"}
        and isinstance(item["path"], str)
        and item["path"]
        and not Path(item["path"]).is_absolute()
        and ".." not in Path(item["path"]).parts
        and isinstance(item["sha256"], str)
        and len(item["sha256"]) == 64
        and all(character in "0123456789abcdef" for character in item["sha256"])
        and item["classification"] in {"known-sensitive", "unknown"}
        and item["classification"]
        == (
            "known-sensitive"
            if is_plaintext_artifact(Path(item["path"]).name)
            else "unknown"
        )
        for item in artifacts
    ):
        raise GuardError("artifact manifest has malformed entries")
    paths = [item["path"] for item in artifacts]
    if len(paths) != len(set(paths)):
        raise GuardError("artifact manifest contains duplicate paths")
    root_sha256 = receipt.get("scope_realpath_sha256")
    if not (
        isinstance(root_sha256, str)
        and len(root_sha256) == 64
        and all(character in "0123456789abcdef" for character in root_sha256)
    ):
        raise GuardError("artifact manifest has a malformed root identity")
    if receipt.get("scope_authority_sha256") != root_sha256:
        raise GuardError("artifact manifest is not bound to its scope authority")
    _validate_scope_roots(receipt.get("scope_roots"))
    if not all(
        isinstance(receipt.get(key), int) and receipt[key] >= 0
        for key in ("scope_device", "scope_inode")
    ):
        raise GuardError("artifact manifest has a malformed filesystem identity")
    if receipt.get("registry_sha256") != registry_sha256(load_registry()):
        raise GuardError("artifact manifest is not bound to the current registry")
    return receipt


def load_private_document(path: Path, *, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise GuardError(f"{label} must be a regular file")
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise GuardError(f"{label} must be owner-owned mode 0600")
    return json.loads(path.read_text(encoding="utf-8"))


def disposition_entries(document: Any) -> list[dict[str, Any]]:
    if (
        not isinstance(document, dict)
        or document.get("schema")
        != "fs2-serve.nebius.ai/global-artifact-disposition-input/v3"
    ):
        raise GuardError("artifact disposition input has the wrong schema")
    entries = document.get("artifacts")
    if not isinstance(entries, list) or not entries:
        raise GuardError("artifact disposition input must enumerate every artifact")
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "sha256",
            "action",
            "evidence",
        }:
            raise GuardError("artifact disposition input has malformed entries")
        path = entry["path"]
        digest = entry["sha256"]
        action = entry["action"]
        evidence = entry["evidence"]
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or action not in DISPOSITION_ACTIONS
            or not isinstance(evidence, dict)
            or set(evidence)
            != {
                "provider",
                "object_id",
                "version_id",
                "audit_event_id",
                "verified_at",
                "verifier",
                "receipt_sha256",
                "receipt_path",
            }
            or not all(isinstance(value, str) and value for value in evidence.values())
            or not Path(evidence["receipt_path"]).is_absolute()
            or len(evidence["receipt_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in evidence["receipt_sha256"]
            )
        ):
            raise GuardError("artifact disposition input has invalid evidence")
        parse_timestamp(evidence["verified_at"])
        normalized.append(
            {
                "path": path,
                "sha256": digest,
                "action": action,
                "evidence": evidence,
            }
        )
    paths = [entry["path"] for entry in normalized]
    if len(paths) != len(set(paths)):
        raise GuardError("artifact disposition input contains duplicate paths")
    return sorted(normalized, key=lambda item: item["path"])


def verify_disposition_provider_receipt(entry: dict[str, Any]) -> None:
    """Verify a separately captured provider/eraser receipt for one artifact."""

    evidence = entry["evidence"]
    receipt_path = Path(evidence["receipt_path"]).absolute()
    document = load_private_document(
        receipt_path, label="artifact disposition provider receipt"
    )
    if file_sha256(receipt_path) != evidence["receipt_sha256"]:
        raise GuardError("artifact disposition provider receipt hash differs")
    required = {
        "schema",
        "provider",
        "action",
        "source_sha256",
        "object_id",
        "version_id",
        "audit_event_id",
        "verified_at",
        "verifier",
        "status",
    }
    expected_status = {"encrypted-rewrap": "rewrapped-and-verified"}[entry["action"]]
    if not isinstance(document, dict) or set(document) != required:
        raise GuardError("artifact disposition provider receipt is malformed")
    expected = {
        "schema": "fs2-serve.nebius.ai/artifact-disposition-provider-receipt/v1",
        "provider": evidence["provider"],
        "action": entry["action"],
        "source_sha256": entry["sha256"],
        "object_id": evidence["object_id"],
        "version_id": evidence["version_id"],
        "audit_event_id": evidence["audit_event_id"],
        "verified_at": evidence["verified_at"],
        "verifier": evidence["verifier"],
        "status": expected_status,
    }
    if document != expected:
        raise GuardError(
            "artifact disposition provider receipt differs from the exact artifact"
        )


def write_disposition_receipt(
    manifest: dict[str, Any], input_document: Any, path: Path
) -> dict[str, Any]:
    entries = disposition_entries(input_document)
    for entry in entries:
        verify_disposition_provider_receipt(entry)
    expected = {(item["path"], item["sha256"]) for item in manifest["artifacts"]}
    observed = {(item["path"], item["sha256"]) for item in entries}
    if observed != expected or len(entries) != len(manifest["artifacts"]):
        raise GuardError(
            "artifact disposition input does not exactly cover the captured manifest"
        )
    receipt = {
        "schema": "fs2-serve.nebius.ai/global-artifact-dispositions/v3",
        "recorded_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "manifest_sha256": canonical_sha256(manifest),
        "artifacts": entries,
    }
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise GuardError(
            "artifact disposition receipt is write-once and already exists"
        )
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise GuardError("artifact disposition receipt parent must be a real directory")
    if (
        parent.stat().st_uid != os.geteuid()
        or stat.S_IMODE(parent.stat().st_mode) & 0o077
    ):
        raise GuardError(
            "artifact disposition receipt parent must be owner-owned and owner-only"
        )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if path.exists():
            path.chmod(0o600)
    return receipt


def load_disposition_receipt(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise GuardError("artifact disposition receipt must be a regular file")
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise GuardError("artifact disposition receipt must be owner-owned mode 0600")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema")
        != "fs2-serve.nebius.ai/global-artifact-dispositions/v3"
        or not isinstance(receipt.get("manifest_sha256"), str)
    ):
        raise GuardError("artifact disposition receipt has the wrong schema")
    entries = disposition_entries(
        {
            "schema": "fs2-serve.nebius.ai/global-artifact-disposition-input/v3",
            "artifacts": receipt.get("artifacts"),
        }
    )
    for entry in entries:
        verify_disposition_provider_receipt(entry)
    return receipt


def inspect_run_root(
    root: Path,
    *,
    retired: bool,
    artifact_manifest: dict[str, Any] | None = None,
    disposition_receipt: dict[str, Any] | None = None,
) -> dict[str, int | str]:
    if retired:
        raise GuardError(
            "local filesystem inventory is containment evidence only and cannot authorize retirement"
        )
    authority_sha256 = require_authoritative_scope(root)
    inventory, scope_roots, metadata = _inventory_authoritative_scope(root)
    known = sum(item["classification"] == "known-sensitive" for item in inventory)
    unknown = len(inventory) - known
    if retired:
        if artifact_manifest is None:
            raise GuardError("retirement requires the pre-retirement artifact manifest")
        if disposition_receipt is None:
            raise GuardError(
                "retirement requires the exact artifact disposition receipt"
            )
        expected_root = hashlib.sha256(
            str(root.absolute().resolve()).encode()
        ).hexdigest()
        if (
            artifact_manifest.get("scope_realpath_sha256") != expected_root
            or artifact_manifest.get("scope_authority_sha256") != authority_sha256
            or artifact_manifest.get("scope_device") != metadata.st_dev
            or artifact_manifest.get("scope_inode") != metadata.st_ino
            or artifact_manifest.get("registry_sha256")
            != registry_sha256(load_registry())
        ):
            raise GuardError("artifact manifest belongs to a different run root")
        manifested_roots = _validate_scope_roots(artifact_manifest.get("scope_roots"))
        for manifested, current in zip(manifested_roots, scope_roots, strict=True):
            if not manifested["present"] and current["present"]:
                raise GuardError(
                    "authoritative state family appeared after the artifact manifest"
                )
            if (
                manifested["present"]
                and current["present"]
                and (
                    manifested["device"],
                    manifested["inode"],
                )
                != (current["device"], current["inode"])
            ):
                raise GuardError(
                    "authoritative state family changed after the artifact manifest"
                )
        if disposition_receipt.get("manifest_sha256") != canonical_sha256(
            artifact_manifest
        ):
            raise GuardError("artifact disposition receipt belongs to another manifest")
        expected_artifacts = {
            (item["path"], item["sha256"]) for item in artifact_manifest["artifacts"]
        }
        disposed_artifacts = {
            (item["path"], item["sha256"]) for item in disposition_receipt["artifacts"]
        }
        if disposed_artifacts != expected_artifacts or len(
            disposition_receipt["artifacts"]
        ) != len(artifact_manifest["artifacts"]):
            raise GuardError(
                "artifact disposition receipt does not exactly cover the manifest"
            )
        if inventory:
            raise GuardError(
                "run root is not retired; manifested or unknown local artifacts remain"
            )
    return {
        "phase": "retired" if retired else "migration",
        "plaintext_artifacts": known,
        "unknown_artifacts": unknown,
        "total_artifacts": len(inventory),
    }


def authoritative_artifact_inventory() -> dict[str, Any]:
    """Read the production authority's complete configured artifact inventory."""

    document = authority_json({"operation": "artifact-inventory"})
    required = {
        "schema",
        "scope",
        "configuration_sha256",
        "scope_registry_sha256",
        "scope_count",
        "evidence_id",
        "observed_at",
        "artifacts",
        "authorityObservation",
        "externalEvidence",
    }
    if (
        set(document) != required
        or document.get("schema")
        != "fs2-serve.nebius.ai/authoritative-artifact-inventory/v1"
        or document.get("scope") != "all-configured-product-operator-state"
        or not all(
            isinstance(document.get(field), str) and document[field]
            for field in ("configuration_sha256", "evidence_id", "observed_at")
        )
        or re.fullmatch(r"[0-9a-f]{64}", document["configuration_sha256"]) is None
        or re.fullmatch(r"[0-9a-f]{64}", document.get("scope_registry_sha256", ""))
        is None
        or not isinstance(document.get("scope_count"), int)
        or document["scope_count"] < 1
        or not isinstance(document.get("artifacts"), list)
    ):
        raise GuardError("production authority returned a malformed artifact inventory")
    verify_external_evidence(document)
    parse_timestamp(document["observed_at"])
    artifact_fields = {
        "artifact_id",
        "path_sha256",
        "sha256",
        "classification",
        "owner",
        "purpose",
        "expires_at",
        "readers",
        "storage",
        "local_present",
        "disposition",
        "provider_version",
        "audit_event_id",
    }
    identifiers: set[str] = set()
    for item in document["artifacts"]:
        if (
            not isinstance(item, dict)
            or set(item) != artifact_fields
            or not all(
                isinstance(item.get(field), str) and item[field]
                for field in (
                    "artifact_id",
                    "owner",
                    "purpose",
                    "storage",
                    "provider_version",
                    "audit_event_id",
                )
            )
            or any(
                not isinstance(item.get(field), str)
                or re.fullmatch(r"[0-9a-f]{64}", item[field]) is None
                for field in ("path_sha256", "sha256")
            )
            or item.get("classification")
            not in {
                "terraform-state",
                "terraform-plan",
                "session-cookie",
                "scoped-credential",
                "unknown-sensitive",
            }
            or item.get("disposition")
            not in {"retained-for-migration", "encrypted-rewrap"}
            or not isinstance(item.get("local_present"), bool)
            or not isinstance(item.get("readers"), list)
            or not item["readers"]
            or not all(isinstance(reader, str) and reader for reader in item["readers"])
            or len(item["readers"]) != len(set(item["readers"]))
            or item["artifact_id"] in identifiers
        ):
            raise GuardError("production artifact inventory has a malformed entry")
        if not isinstance(item["expires_at"], str):
            raise GuardError(
                "production artifact inventory must give every artifact a bounded expiry"
            )
        parse_timestamp(item["expires_at"])
        identifiers.add(item["artifact_id"])
    return document


def validate_stored_authoritative_artifact_inventory(document: Any) -> dict[str, Any]:
    """Revalidate a stored authority payload without trusting its container."""

    required = {
        "schema",
        "scope",
        "configuration_sha256",
        "scope_registry_sha256",
        "scope_count",
        "evidence_id",
        "observed_at",
        "artifacts",
        "authorityObservation",
        "externalEvidence",
    }
    if (
        not isinstance(document, dict)
        or set(document) != required
        or document.get("schema")
        != "fs2-serve.nebius.ai/authoritative-artifact-inventory/v1"
        or document.get("scope") != "all-configured-product-operator-state"
        or re.fullmatch(r"[0-9a-f]{64}", document.get("configuration_sha256", ""))
        is None
        or re.fullmatch(r"[0-9a-f]{64}", document.get("scope_registry_sha256", ""))
        is None
        or not isinstance(document.get("scope_count"), int)
        or document["scope_count"] < 1
        or not isinstance(document.get("evidence_id"), str)
        or not document["evidence_id"]
        or not isinstance(document.get("observed_at"), str)
        or not isinstance(document.get("artifacts"), list)
    ):
        raise GuardError("stored authoritative artifact inventory is malformed")
    verify_external_evidence(document)
    parse_timestamp(document["observed_at"])
    fields = {
        "artifact_id",
        "path_sha256",
        "sha256",
        "classification",
        "owner",
        "purpose",
        "expires_at",
        "readers",
        "storage",
        "local_present",
        "disposition",
        "provider_version",
        "audit_event_id",
    }
    identifiers: set[str] = set()
    for item in document["artifacts"]:
        if (
            not isinstance(item, dict)
            or set(item) != fields
            or not all(
                isinstance(item.get(field), str) and item[field]
                for field in (
                    "artifact_id",
                    "owner",
                    "purpose",
                    "expires_at",
                    "storage",
                    "provider_version",
                    "audit_event_id",
                )
            )
            or any(
                re.fullmatch(r"[0-9a-f]{64}", item.get(field, "")) is None
                for field in ("path_sha256", "sha256")
            )
            or item.get("classification")
            not in {
                "terraform-state",
                "terraform-plan",
                "session-cookie",
                "scoped-credential",
                "unknown-sensitive",
            }
            or item.get("disposition")
            not in {"retained-for-migration", "encrypted-rewrap"}
            or not isinstance(item.get("local_present"), bool)
            or not isinstance(item.get("readers"), list)
            or not item["readers"]
            or not all(isinstance(reader, str) and reader for reader in item["readers"])
            or len(item["readers"]) != len(set(item["readers"]))
            or item["artifact_id"] in identifiers
        ):
            raise GuardError("stored authoritative artifact entry is malformed")
        parse_timestamp(item["expires_at"])
        identifiers.add(item["artifact_id"])
    return document


def write_authoritative_artifact_manifest(path: Path) -> dict[str, Any]:
    inventory = authoritative_artifact_inventory()
    receipt = {
        "schema": "fs2-serve.nebius.ai/authoritative-artifact-manifest/v1",
        "captured_at": utc_now().isoformat().replace("+00:00", "Z"),
        "inventory": inventory,
    }
    write_private_json(path, receipt)
    return receipt


def load_authoritative_artifact_manifest(path: Path) -> dict[str, Any]:
    document = load_private_document(path, label="authoritative artifact manifest")
    if (
        not isinstance(document, dict)
        or set(document) != {"schema", "captured_at", "inventory"}
        or document.get("schema")
        != "fs2-serve.nebius.ai/authoritative-artifact-manifest/v1"
    ):
        raise GuardError("authoritative artifact manifest has the wrong schema")
    parse_timestamp(document["captured_at"])
    validate_stored_authoritative_artifact_inventory(document["inventory"])
    return document


def inspect_authoritative_artifacts(
    *, retired: bool, manifest: dict[str, Any] | None = None
) -> dict[str, int | str]:
    current = authoritative_artifact_inventory()
    retained = [
        item
        for item in current["artifacts"]
        if item["local_present"] or item["disposition"] != "encrypted-rewrap"
    ]
    if retired:
        if manifest is None:
            raise GuardError(
                "retirement requires an authoritative pre-migration manifest"
            )
        previous = manifest.get("inventory")
        if (
            not isinstance(previous, dict)
            or previous.get("scope") != current["scope"]
            or previous.get("configuration_sha256") != current["configuration_sha256"]
            or previous.get("scope_registry_sha256") != current["scope_registry_sha256"]
            or previous.get("scope_count") != current["scope_count"]
        ):
            raise GuardError("artifact authority configuration changed after manifest")
        previous_ids = {item["artifact_id"] for item in previous.get("artifacts", [])}
        current_ids = {item["artifact_id"] for item in current["artifacts"]}
        if previous_ids != current_ids:
            raise GuardError("authoritative artifact inventory changed after manifest")
        if retained:
            raise GuardError(
                "legacy plaintext retirement is blocked until every artifact is provider-verified encrypted-rewrap and absent locally"
            )
    return {
        "phase": "retired" if retired else "migration",
        "total_artifacts": len(current["artifacts"]),
        "retained_artifacts": len(retained),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("plan_json", type=Path)
    plan.add_argument("--identity-receipt", type=Path)
    plan.add_argument("--greenfield-bootstrap", action="store_true")
    plan.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    plan.add_argument(
        "--terraform-root",
        choices=(
            "configuration",
            "infrastructure",
            "foundation",
            "workloads",
            "reference-data",
        ),
        required=True,
    )
    capture = subparsers.add_parser("capture-state")
    capture.add_argument("state_json", type=Path)
    capture.add_argument("receipt", type=Path)
    capture.add_argument("--source-commit", required=True)
    capture.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    capture.add_argument(
        "--terraform-root",
        choices=("configuration", "infrastructure", "foundation", "workloads", "reference-data"),
        required=True,
    )
    validate_state = subparsers.add_parser("validate-state")
    validate_state.add_argument("state_json", type=Path)
    validate_state.add_argument("receipt", type=Path)
    validate_state.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    validate_state.add_argument(
        "--terraform-root",
        choices=("configuration", "infrastructure", "foundation", "workloads", "reference-data"),
        required=True,
    )
    apply_gate = subparsers.add_parser("capture-apply-gate")
    apply_gate.add_argument("state_json", type=Path)
    apply_gate.add_argument("receipt", type=Path)
    apply_gate.add_argument(
        "--raw-state",
        type=Path,
        help="Legacy owner-only input; omit to send state_document/raw_state_document as one stdin envelope.",
    )
    apply_gate.add_argument("--identity-receipt", type=Path)
    apply_gate.add_argument("--greenfield-bootstrap", action="store_true")
    apply_gate.add_argument("--terraform-configuration", type=Path, required=True)
    apply_gate.add_argument(
        "--terraform-root",
        choices=("configuration", "infrastructure", "foundation", "workloads", "reference-data"),
        required=True,
    )
    apply_gate.add_argument("--source-commit", required=True)
    apply_gate.add_argument("--ttl-seconds", type=int, default=900)
    apply_gate.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    native_gate = subparsers.add_parser("native-gate")
    native_gate.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    saved_gate = subparsers.add_parser("capture-saved-plan-gate")
    saved_gate.add_argument("plan_json", type=Path)
    saved_gate.add_argument("saved_plan", type=Path)
    saved_gate.add_argument("receipt", type=Path)
    saved_gate.add_argument("--planning-receipt", type=Path, required=True)
    saved_gate.add_argument(
        "--raw-state",
        type=Path,
        help="Legacy owner-only input; omit to send plan_document/raw_state_document as one stdin envelope.",
    )
    saved_gate.add_argument("--identity-receipt", type=Path)
    saved_gate.add_argument("--greenfield-bootstrap", action="store_true")
    saved_gate.add_argument("--terraform-configuration", type=Path, required=True)
    saved_gate.add_argument(
        "--terraform-root",
        choices=("configuration", "infrastructure", "foundation", "workloads", "reference-data"),
        required=True,
    )
    saved_gate.add_argument("--source-commit", required=True)
    saved_gate.add_argument("--ttl-seconds", type=int, default=300)
    saved_gate.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    execution_gate = subparsers.add_parser("apply-saved-plan-gate")
    execution_gate.add_argument("--terraform-configuration", type=Path, required=True)
    execution_gate.add_argument(
        "--terraform-root",
        choices=("configuration", "infrastructure", "foundation", "workloads", "reference-data"),
        required=True,
    )
    execution_gate.add_argument("--source-commit", required=True)
    execution_gate.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    execution_mode = execution_gate.add_mutually_exclusive_group()
    execution_mode.add_argument("--wrapper-preflight", action="store_true")
    execution_mode.add_argument("--wrapper-runtime-snapshot", action="store_true")
    artifacts = subparsers.add_parser("capture-global-state")
    artifacts.add_argument("receipt", type=Path)
    root = subparsers.add_parser("global-state")
    root.add_argument("--retired", action="store_true")
    root.add_argument("--artifact-manifest", type=Path)
    readiness = subparsers.add_parser("capture-consumer-readiness")
    readiness.add_argument("identity_receipt", type=Path)
    readiness.add_argument("receipt", type=Path)
    readiness.add_argument(
        "--phase",
        choices=("predecessor-ready", "dual-read-ready", "current-write-ready"),
        required=True,
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "native-gate":
        query = json.loads(os.sys.stdin.read())
        terraform_configuration = Path(query.get("terraform_configuration", ""))
        gate_receipt = load_private_document(
            Path(query.get("receipt_path", "")), label="Terraform gate receipt"
        )
        result = validate_native_gate(
            query,
            authoritative_state_document=(
                None
                if gate_receipt.get("state_initialization") == "greenfield-empty"
                else command_json(
                    [
                        PRODUCTION_TERRAFORM_COMMAND,
                        f"-chdir={terraform_configuration}",
                        "state",
                        "pull",
                    ],
                    label="authoritative Terraform state inspection",
                )
            ),
            registry=load_registry(args.registry),
        )
    elif args.command == "apply-saved-plan-gate":
        result = validate_saved_plan_gate_from_environment(
            terraform_configuration=args.terraform_configuration,
            terraform_root=args.terraform_root,
            source_commit=args.source_commit,
            registry=load_registry(args.registry),
            wrapper_preflight=args.wrapper_preflight,
            wrapper_runtime_snapshot=args.wrapper_runtime_snapshot,
        )
    elif args.command == "capture-saved-plan-gate":
        registry = load_registry(args.registry)
        encoded = (
            os.sys.stdin.read()
            if str(args.plan_json) == "-"
            else args.plan_json.read_text(encoding="utf-8")
        )
        supplied = json.loads(encoded)
        if args.raw_state is None:
            if not isinstance(supplied, dict) or set(supplied) != {
                "plan_document",
                "raw_state_document",
            }:
                raise GuardError(
                    "saved-plan gate stdin envelope must contain exact plan_document and raw_state_document fields"
                )
            plan_document = supplied["plan_document"]
            raw_state_document = supplied["raw_state_document"]
        else:
            plan_document = supplied
            raw_state_document = load_private_document(
                args.raw_state, label="raw Terraform state"
            )
        if not isinstance(plan_document, dict) or not (
            isinstance(raw_state_document, dict)
            or (args.greenfield_bootstrap and raw_state_document is None)
        ):
            raise GuardError("saved-plan gate documents must be JSON objects")
        receipt = write_saved_plan_gate_receipt(
            plan_document=plan_document,
            raw_state_document=raw_state_document,
            saved_plan=args.saved_plan,
            planning_receipt_path=args.planning_receipt,
            identity_receipt=load_identity_receipt(
                args.identity_receipt,
                registry=registry,
                terraform_root=args.terraform_root,
            ),
            live_secret_document=None,
            terraform_configuration=args.terraform_configuration,
            terraform_root=args.terraform_root,
            source_commit=args.source_commit,
            path=args.receipt,
            ttl_seconds=args.ttl_seconds,
            registry=registry,
            greenfield_bootstrap=args.greenfield_bootstrap,
        )
        result = {
            "receipt": str(args.receipt.absolute()),
            "expires_at": receipt["expires_at"],
            "plan_sha256": receipt["saved_plan"]["sha256"],
        }
    elif args.command in {
        "plan",
        "capture-state",
        "validate-state",
        "capture-apply-gate",
    }:
        document_path = args.plan_json if args.command == "plan" else args.state_json
        encoded = (
            os.sys.stdin.read()
            if str(document_path) == "-"
            else document_path.read_text(encoding="utf-8")
        )
        supplied = json.loads(encoded)
        if args.command == "capture-apply-gate" and args.raw_state is None:
            if not isinstance(supplied, dict) or set(supplied) != {
                "state_document",
                "raw_state_document",
            }:
                raise GuardError(
                    "planning gate stdin envelope must contain exact state_document and raw_state_document fields"
                )
            document = supplied["state_document"]
            raw_state_document = supplied["raw_state_document"]
        else:
            document = supplied
            raw_state_document = (
                load_private_document(args.raw_state, label="raw Terraform state")
                if args.command == "capture-apply-gate"
                else None
            )
        if not isinstance(document, dict):
            raise GuardError("guard input document must be a JSON object")
        if args.command == "plan":
            registry = load_registry(args.registry)
            if args.greenfield_bootstrap:
                greenfield_bootstrap_identity(
                    terraform_root=args.terraform_root, registry=registry
                )
            result = inspect_plan(
                document,
                identity_receipt=load_identity_receipt(
                    args.identity_receipt,
                    registry=registry,
                    terraform_root=args.terraform_root,
                ),
                registry=registry,
                terraform_root=args.terraform_root,
                greenfield_bootstrap=args.greenfield_bootstrap,
            )
        elif args.command == "capture-state":
            registry = load_registry(args.registry)
            receipt = write_identity_receipt(
                document,
                args.receipt,
                source_commit=args.source_commit,
                registry=registry,
                terraform_root=args.terraform_root,
                live_secret_document=None,
            )
            result = {
                "receipt": str(args.receipt.absolute()),
                "protected_identities": len(receipt["address_fingerprints"]),
                "live_secret_bindings": len(receipt["live_secret_bindings"]),
            }
        elif args.command == "validate-state":
            result = validate_identity_receipt_state(
                document,
                args.receipt,
                registry=load_registry(args.registry),
                terraform_root=args.terraform_root,
            )
        else:
            registry = load_registry(args.registry)
            receipt = write_apply_gate_receipt(
                state_document=document,
                raw_state_document=raw_state_document,
                identity_receipt=load_identity_receipt(
                    args.identity_receipt,
                    registry=registry,
                    terraform_root=args.terraform_root,
                ),
                terraform_configuration=args.terraform_configuration,
                terraform_root=args.terraform_root,
                source_commit=args.source_commit,
                path=args.receipt,
                ttl_seconds=args.ttl_seconds,
                registry=registry,
                greenfield_bootstrap=args.greenfield_bootstrap,
            )
            result = {
                "receipt": str(args.receipt.absolute()),
                "expires_at": receipt["expires_at"],
            }
    elif args.command == "capture-consumer-readiness":
        receipt = write_consumer_readiness_receipt(
            identity_receipt_path=args.identity_receipt,
            phase=args.phase,
            path=args.receipt,
        )
        result = {
            "receipt": str(args.receipt.absolute()),
            "receipt_sha256": file_sha256(args.receipt),
            "credential_classes": len(receipt["payload"]["classes"]),
            "authority_evidence_id": receipt["payload"]["evidence_id"],
        }
    elif args.command == "capture-global-state":
        receipt = write_authoritative_artifact_manifest(args.receipt)
        result = {
            "receipt": str(args.receipt.absolute()),
            "total_artifacts": len(receipt["inventory"]["artifacts"]),
            "authority_evidence_id": receipt["inventory"]["evidence_id"],
        }
    else:
        result = inspect_authoritative_artifacts(
            retired=args.retired,
            manifest=(
                load_authoritative_artifact_manifest(args.artifact_manifest)
                if args.artifact_manifest is not None
                else None
            ),
        )
    print(json.dumps({"status": "pass", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GuardError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        raise SystemExit(2) from error
