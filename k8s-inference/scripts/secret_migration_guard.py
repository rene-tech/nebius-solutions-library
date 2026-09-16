#!/usr/bin/env python3
"""Fail closed on destructive key migration plans and residual plaintext state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "security/durable-credential-registry.json"
SENSITIVE_ARTIFACT_SUFFIXES = (".tfstate", ".tfplan", ".backup")
SCOPED_CREDENTIAL_PREFIXES = (
    "access-bundle",
    "admin-cookie",
    "admin",
    "general-access",
    "grafana",
    "scientific-access",
)
DISPOSITION_ACTIONS = frozenset({"encrypted-rewrap", "secure-retire"})


class GuardError(RuntimeError):
    pass


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


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


def write_apply_gate_receipt(
    *,
    state_document: dict[str, Any],
    identity_receipt: dict[str, Any] | None,
    terraform_configuration: Path,
    terraform_root: str,
    source_commit: str,
    path: Path,
    ttl_seconds: int = 900,
    registry: dict[str, Any] | None = None,
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
    if fingerprints:
        if identity_receipt is None:
            raise GuardError("durable state requires an exact identity receipt")
        if identity_receipt.get("address_fingerprints") != fingerprints:
            raise GuardError("durable state differs from its identity receipt")
    now = utc_now()
    receipt = {
        "schema": "fs2-serve.nebius.ai/terraform-apply-gate/v2",
        "terraform_root": terraform_root,
        "source_commit": source_commit,
        "registry_sha256": registry_sha256(registry),
        "configuration_sha256": configuration_sha256(terraform_configuration),
        "state_fingerprints_sha256": canonical_sha256(fingerprints),
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now.replace(microsecond=0) + timedelta(seconds=ttl_seconds))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    write_private_json(path, receipt)
    return receipt


def validate_native_gate(
    query: dict[str, Any], *, registry: dict[str, Any] | None = None
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
    if receipt.get("schema") != "fs2-serve.nebius.ai/terraform-apply-gate/v2":
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


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    """Load the complete, value-free durable-credential policy."""

    if path.is_symlink() or not path.is_file():
        raise GuardError("durable credential registry is absent or unsafe")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "fs2-serve.nebius.ai/durable-credential-registry/v2":
        raise GuardError("durable credential registry has the wrong schema")
    credentials = document.get("credentials")
    resources = document.get("terraform_resource_addresses")
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
    return document


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


def is_protected_address(
    address: Any,
    *,
    registry: dict[str, Any] | None = None,
    terraform_root: str | None = None,
) -> bool:
    return isinstance(address, str) and any(
        pattern.fullmatch(address)
        for pattern in protected_patterns(registry, terraform_root=terraform_root)
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
    return dict(sorted(fingerprints.items()))


def plan_prior_fingerprints(
    document: dict[str, Any],
    *,
    registry: dict[str, Any] | None = None,
    terraform_root: str | None = None,
) -> dict[str, str]:
    prior = protected_state_fingerprints(
        document.get("prior_state", {}),
        registry=registry,
        terraform_root=terraform_root,
    )
    if prior:
        return prior
    fallback: dict[str, str] = {}
    for change in document.get("resource_changes", []):
        address = change.get("address")
        before = change.get("change", {}).get("before")
        if (
            is_protected_address(
                address, registry=registry, terraform_root=terraform_root
            )
            and before is not None
        ):
            fallback[address] = canonical_sha256(before)
    return dict(sorted(fallback.items()))


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
        and resource["address"].startswith("kubernetes_secret_v1.")
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
        data = item.get("data")
        identity = (metadata.get("namespace"), metadata.get("name"))
        if (
            not all(isinstance(value, str) and value for value in identity)
            or not isinstance(metadata.get("uid"), str)
            or not metadata["uid"]
            or not isinstance(metadata.get("resourceVersion"), str)
            or not metadata["resourceVersion"]
            or not isinstance(data, dict)
            or not data
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in data.items()
            )
        ):
            raise GuardError("live Secret inventory lacks exact identity or data")
        if identity in live:
            raise GuardError("live Secret inventory contains a duplicate identity")
        live[identity] = {
            "namespace": identity[0],
            "name": identity[1],
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "content_sha256": canonical_sha256(data),
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
        if state_uid not in (None, "", binding["uid"]) or state_rv not in (
            None,
            "",
            binding["resource_version"],
        ):
            raise GuardError(
                f"live Secret UID/resourceVersion differs from Terraform state: {address}"
            )
        bindings[address] = binding
    return dict(sorted(bindings.items()))


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
    fingerprints = protected_state_fingerprints(
        state_document, registry=registry, terraform_root=terraform_root
    )
    if not fingerprints:
        raise GuardError("state contains no protected generation-1 resources")
    secret_bindings = live_secret_bindings(
        state_document,
        live_secret_document,
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
        == {"namespace", "name", "uid", "resource_version", "content_sha256"}
        and all(isinstance(binding[key], str) and binding[key] for key in binding)
        and len(binding["content_sha256"]) == 64
        for address, binding in bindings.items()
    ):
        raise GuardError("identity receipt has malformed live Secret bindings")
    expected_secret_addresses = {
        address
        for address in fingerprints
        if address.startswith("kubernetes_secret_v1.")
    }
    if set(bindings) != expected_secret_addresses:
        raise GuardError(
            "identity receipt does not bind every protected Kubernetes Secret"
        )
    return receipt


def inspect_plan(
    document: dict[str, Any],
    *,
    identity_receipt: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
    terraform_root: str | None = None,
) -> dict[str, int]:
    registry = registry or load_registry()
    prior_fingerprints = plan_prior_fingerprints(
        document, registry=registry, terraform_root=terraform_root
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
        if previous_address is not None and not isinstance(previous_address, str):
            raise GuardError("Terraform plan contains a malformed previous_address")
        protected = is_protected_address(
            address, registry=registry, terraform_root=terraform_root
        )
        previous_protected = is_protected_address(
            previous_address, registry=registry, terraform_root=terraform_root
        )
        if previous_address is not None and (protected or previous_protected):
            raise GuardError(
                "plan would move a durable credential address: "
                f"{previous_address} -> {address}"
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
        if protected and actions != ["no-op"]:
            protected_changes += 1
    omitted = set(prior_fingerprints) - seen_protected
    if omitted:
        raise GuardError(
            "Terraform plan omitted protected prior-state addresses: "
            + ", ".join(sorted(omitted))
        )
    return {
        "protected_addresses": len(
            protected_patterns(registry, terraform_root=terraform_root)
        ),
        "verified_identities": len(prior_fingerprints),
        "protected_changes": protected_changes,
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


def inventory_run_root(root: Path) -> list[dict[str, Any]]:
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise GuardError("run root must be a real directory")
    inventory: list[dict[str, Any]] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        metadata = current.stat()
        if (
            current.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise GuardError(
                f"run-root directory is not owner-owned and owner-only: {current}"
            )
        for name in directory_names:
            if (current / name).is_symlink():
                raise GuardError(
                    f"run root contains a directory symlink: {current / name}"
                )
        for name in file_names:
            path = current / name
            if path.is_symlink():
                raise GuardError(f"run root contains a file symlink: {path}")
            inventory.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": file_sha256(path),
                    "classification": (
                        "known-sensitive" if is_plaintext_artifact(name) else "unknown"
                    ),
                }
            )
    return sorted(inventory, key=lambda item: item["path"])


def write_artifact_manifest(root: Path, path: Path) -> dict[str, Any]:
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
    artifacts = inventory_run_root(root)
    root_metadata = root.absolute().stat()
    registry = load_registry()
    receipt = {
        "schema": "fs2-serve.nebius.ai/global-state-artifacts/v2",
        "captured_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "scope_realpath_sha256": hashlib.sha256(
            str(root.absolute().resolve()).encode()
        ).hexdigest(),
        "scope_device": root_metadata.st_dev,
        "scope_inode": root_metadata.st_ino,
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
    if receipt.get("schema") != "fs2-serve.nebius.ai/global-state-artifacts/v2":
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
        != "fs2-serve.nebius.ai/global-artifact-disposition-input/v2"
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
            }
            or not all(isinstance(value, str) and value for value in evidence.values())
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


def write_disposition_receipt(
    manifest: dict[str, Any], input_document: Any, path: Path
) -> dict[str, Any]:
    entries = disposition_entries(input_document)
    expected = {(item["path"], item["sha256"]) for item in manifest["artifacts"]}
    observed = {(item["path"], item["sha256"]) for item in entries}
    if observed != expected or len(entries) != len(manifest["artifacts"]):
        raise GuardError(
            "artifact disposition input does not exactly cover the captured manifest"
        )
    receipt = {
        "schema": "fs2-serve.nebius.ai/global-artifact-dispositions/v2",
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
        != "fs2-serve.nebius.ai/global-artifact-dispositions/v2"
        or not isinstance(receipt.get("manifest_sha256"), str)
    ):
        raise GuardError("artifact disposition receipt has the wrong schema")
    disposition_entries(
        {
            "schema": "fs2-serve.nebius.ai/global-artifact-disposition-input/v2",
            "artifacts": receipt.get("artifacts"),
        }
    )
    return receipt


def inspect_run_root(
    root: Path,
    *,
    retired: bool,
    artifact_manifest: dict[str, Any] | None = None,
    disposition_receipt: dict[str, Any] | None = None,
) -> dict[str, int | str]:
    inventory = inventory_run_root(root)
    known = sum(item["classification"] == "known-sensitive" for item in inventory)
    unknown = len(inventory) - known
    if retired:
        if artifact_manifest is None:
            raise GuardError("retirement requires the pre-retirement artifact manifest")
        if disposition_receipt is None:
            raise GuardError(
                "retirement requires the exact artifact disposition receipt"
            )
        metadata = root.absolute().stat()
        expected_root = hashlib.sha256(
            str(root.absolute().resolve()).encode()
        ).hexdigest()
        if (
            artifact_manifest.get("scope_realpath_sha256") != expected_root
            or artifact_manifest.get("scope_device") != metadata.st_dev
            or artifact_manifest.get("scope_inode") != metadata.st_ino
            or artifact_manifest.get("registry_sha256")
            != registry_sha256(load_registry())
        ):
            raise GuardError("artifact manifest belongs to a different run root")
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("plan_json", type=Path)
    plan.add_argument("--identity-receipt", type=Path)
    plan.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    plan.add_argument(
        "--terraform-root",
        choices=("infrastructure", "foundation", "workloads", "reference-data"),
    )
    capture = subparsers.add_parser("capture-state")
    capture.add_argument("state_json", type=Path)
    capture.add_argument("receipt", type=Path)
    capture.add_argument("--source-commit", required=True)
    capture.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    capture.add_argument(
        "--terraform-root",
        choices=("infrastructure", "foundation", "workloads", "reference-data"),
        required=True,
    )
    capture.add_argument("--live-secrets", type=Path)
    apply_gate = subparsers.add_parser("capture-apply-gate")
    apply_gate.add_argument("state_json", type=Path)
    apply_gate.add_argument("receipt", type=Path)
    apply_gate.add_argument("--identity-receipt", type=Path)
    apply_gate.add_argument("--terraform-configuration", type=Path, required=True)
    apply_gate.add_argument(
        "--terraform-root",
        choices=("infrastructure", "foundation", "workloads", "reference-data"),
        required=True,
    )
    apply_gate.add_argument("--source-commit", required=True)
    apply_gate.add_argument("--ttl-seconds", type=int, default=900)
    apply_gate.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    native_gate = subparsers.add_parser("native-gate")
    native_gate.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    artifacts = subparsers.add_parser("capture-global-state")
    artifacts.add_argument("path", type=Path)
    artifacts.add_argument("receipt", type=Path)
    disposition = subparsers.add_parser("capture-disposition")
    disposition.add_argument("manifest", type=Path)
    disposition.add_argument("input", type=Path)
    disposition.add_argument("receipt", type=Path)
    root = subparsers.add_parser("global-state")
    root.add_argument("path", type=Path)
    root.add_argument("--retired", action="store_true")
    root.add_argument("--artifact-manifest", type=Path)
    root.add_argument("--disposition-receipt", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "native-gate":
        result = validate_native_gate(
            json.loads(os.sys.stdin.read()), registry=load_registry(args.registry)
        )
    elif args.command in {"plan", "capture-state", "capture-apply-gate"}:
        document_path = args.plan_json if args.command == "plan" else args.state_json
        encoded = (
            os.sys.stdin.read()
            if str(document_path) == "-"
            else document_path.read_text(encoding="utf-8")
        )
        document = json.loads(encoded)
        if args.command == "plan":
            registry = load_registry(args.registry)
            result = inspect_plan(
                document,
                identity_receipt=load_identity_receipt(
                    args.identity_receipt,
                    registry=registry,
                    terraform_root=args.terraform_root,
                ),
                registry=registry,
                terraform_root=args.terraform_root,
            )
        elif args.command == "capture-state":
            registry = load_registry(args.registry)
            live_document = (
                load_private_document(args.live_secrets, label="live Secret inventory")
                if args.live_secrets is not None
                else None
            )
            receipt = write_identity_receipt(
                document,
                args.receipt,
                source_commit=args.source_commit,
                registry=registry,
                terraform_root=args.terraform_root,
                live_secret_document=live_document,
            )
            result = {
                "receipt": str(args.receipt.absolute()),
                "protected_identities": len(receipt["address_fingerprints"]),
                "live_secret_bindings": len(receipt["live_secret_bindings"]),
            }
        else:
            registry = load_registry(args.registry)
            receipt = write_apply_gate_receipt(
                state_document=document,
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
            )
            result = {
                "receipt": str(args.receipt.absolute()),
                "expires_at": receipt["expires_at"],
            }
    elif args.command == "capture-global-state":
        receipt = write_artifact_manifest(args.path, args.receipt)
        result = {
            "receipt": str(args.receipt.absolute()),
            "total_artifacts": len(receipt["artifacts"]),
            "plaintext_artifacts": sum(
                item["classification"] == "known-sensitive"
                for item in receipt["artifacts"]
            ),
        }
    elif args.command == "capture-disposition":
        manifest = load_artifact_manifest(args.manifest)
        receipt = write_disposition_receipt(
            manifest,
            load_private_document(args.input, label="artifact disposition input"),
            args.receipt,
        )
        result = {
            "receipt": str(args.receipt.absolute()),
            "disposed_artifacts": len(receipt["artifacts"]),
        }
    else:
        result = inspect_run_root(
            args.path,
            retired=args.retired,
            artifact_manifest=(
                load_artifact_manifest(args.artifact_manifest)
                if args.artifact_manifest is not None
                else None
            ),
            disposition_receipt=(
                load_disposition_receipt(args.disposition_receipt)
                if args.disposition_receipt is not None
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
