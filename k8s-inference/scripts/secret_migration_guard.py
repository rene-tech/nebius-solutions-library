#!/usr/bin/env python3
"""Fail closed on destructive key migration plans and residual plaintext state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEGACY_ADDRESSES = frozenset(
    {
        "random_id.bootstrap_access_token_id",
        "random_id.scientific_access_token_id[0]",
        "random_password.admin_token",
        "random_password.bootstrap_access_token_secret",
        "random_password.scientific_access_token_secret[0]",
        'random_password.key_material["payload"]',
        'random_password.key_material["ledger"]',
        'random_password.key_material["pepper"]',
        'random_password.key_material["attestor"]',
        "kubernetes_secret_v1.admin",
        "kubernetes_secret_v1.bootstrap_access",
        "kubernetes_secret_v1.scientific_access[0]",
        "kubernetes_secret_v1.payload_keyring",
        "kubernetes_secret_v1.ledger_keyring",
        "kubernetes_secret_v1.token_pepper",
        "kubernetes_secret_v1.route_attestors",
    }
)
LEGACY_ADDRESS_PREFIXES = (
    'random_password.database["',
    'kubernetes_secret_v1.database_account["',
)
SENSITIVE_ARTIFACT_SUFFIXES = (".tfstate", ".tfplan", ".backup")
SCOPED_CREDENTIAL_PREFIXES = (
    "access-bundle",
    "admin-cookie",
    "admin",
    "general-access",
    "grafana",
    "scientific-access",
)


class GuardError(RuntimeError):
    pass


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def is_protected_address(address: Any) -> bool:
    return address in LEGACY_ADDRESSES or (
        isinstance(address, str) and address.startswith(LEGACY_ADDRESS_PREFIXES)
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


def protected_state_fingerprints(document: Any) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for resource in state_resources(document):
        address = resource.get("address")
        if not is_protected_address(address):
            continue
        values = resource.get("values")
        if values is None:
            raise GuardError(f"protected state resource lacks values: {address}")
        if address in fingerprints:
            raise GuardError(f"protected state resource is duplicated: {address}")
        fingerprints[address] = canonical_sha256(values)
    return dict(sorted(fingerprints.items()))


def plan_prior_fingerprints(document: dict[str, Any]) -> dict[str, str]:
    prior = protected_state_fingerprints(document.get("prior_state", {}))
    if prior:
        return prior
    fallback: dict[str, str] = {}
    for change in document.get("resource_changes", []):
        address = change.get("address")
        before = change.get("change", {}).get("before")
        if is_protected_address(address) and before is not None:
            fallback[address] = canonical_sha256(before)
    return dict(sorted(fallback.items()))


def write_identity_receipt(
    state_document: dict[str, Any],
    path: Path,
    *,
    source_commit: str,
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
    fingerprints = protected_state_fingerprints(state_document)
    if not fingerprints:
        raise GuardError("state contains no protected generation-1 resources")
    receipt = {
        "schema": "fs2-serve.nebius.ai/fixed-v1-identity/v1",
        "source_commit": source_commit,
        "captured_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "address_fingerprints": fingerprints,
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


def load_identity_receipt(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise GuardError("identity receipt must be a regular file")
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise GuardError("identity receipt must be owner-owned mode 0600")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("schema") != "fs2-serve.nebius.ai/fixed-v1-identity/v1":
        raise GuardError("identity receipt has the wrong schema")
    fingerprints = receipt.get("address_fingerprints")
    if not isinstance(fingerprints, dict) or not all(
        is_protected_address(address)
        and isinstance(fingerprint, str)
        and len(fingerprint) == 64
        and all(character in "0123456789abcdef" for character in fingerprint)
        for address, fingerprint in fingerprints.items()
    ):
        raise GuardError("identity receipt has malformed protected fingerprints")
    return receipt


def inspect_plan(
    document: dict[str, Any],
    *,
    identity_receipt: dict[str, Any] | None = None,
) -> dict[str, int]:
    prior_fingerprints = plan_prior_fingerprints(document)
    if prior_fingerprints:
        if identity_receipt is None:
            raise GuardError(
                "existing generation-1 resources require a fixed-v1 identity receipt"
            )
        if identity_receipt["address_fingerprints"] != prior_fingerprints:
            raise GuardError(
                "live generation-1 resource identities differ from the fixed-v1 receipt"
            )
    protected_changes = 0
    for change in document.get("resource_changes", []):
        address = change.get("address")
        actions = change.get("change", {}).get("actions", [])
        protected = is_protected_address(address)
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
    return {
        "protected_addresses": len(LEGACY_ADDRESSES),
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
    receipt = {
        "schema": "fs2-serve.nebius.ai/run-root-artifacts/v1",
        "captured_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "root_sha256": hashlib.sha256(str(root.absolute()).encode()).hexdigest(),
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
    if receipt.get("schema") != "fs2-serve.nebius.ai/run-root-artifacts/v1":
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
        and item["classification"] in {"known-sensitive", "unknown"}
        for item in artifacts
    ):
        raise GuardError("artifact manifest has malformed entries")
    return receipt


def inspect_run_root(
    root: Path,
    *,
    retired: bool,
    artifact_manifest: dict[str, Any] | None = None,
) -> dict[str, int | str]:
    inventory = inventory_run_root(root)
    known = sum(item["classification"] == "known-sensitive" for item in inventory)
    unknown = len(inventory) - known
    if retired:
        if artifact_manifest is None:
            raise GuardError("retirement requires the pre-retirement artifact manifest")
        expected_root = hashlib.sha256(str(root.absolute()).encode()).hexdigest()
        if artifact_manifest.get("root_sha256") != expected_root:
            raise GuardError("artifact manifest belongs to a different run root")
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
    capture = subparsers.add_parser("capture-state")
    capture.add_argument("state_json", type=Path)
    capture.add_argument("receipt", type=Path)
    capture.add_argument("--source-commit", required=True)
    artifacts = subparsers.add_parser("capture-run-root")
    artifacts.add_argument("path", type=Path)
    artifacts.add_argument("receipt", type=Path)
    root = subparsers.add_parser("run-root")
    root.add_argument("path", type=Path)
    root.add_argument("--retired", action="store_true")
    root.add_argument("--artifact-manifest", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command in {"plan", "capture-state"}:
        document_path = args.plan_json if args.command == "plan" else args.state_json
        encoded = (
            os.sys.stdin.read()
            if str(document_path) == "-"
            else document_path.read_text(encoding="utf-8")
        )
        document = json.loads(encoded)
        if args.command == "plan":
            result = inspect_plan(
                document,
                identity_receipt=load_identity_receipt(args.identity_receipt),
            )
        else:
            receipt = write_identity_receipt(
                document,
                args.receipt,
                source_commit=args.source_commit,
            )
            result = {
                "receipt": str(args.receipt.absolute()),
                "protected_identities": len(receipt["address_fingerprints"]),
            }
    elif args.command == "capture-run-root":
        receipt = write_artifact_manifest(args.path, args.receipt)
        result = {
            "receipt": str(args.receipt.absolute()),
            "total_artifacts": len(receipt["artifacts"]),
            "plaintext_artifacts": sum(
                item["classification"] == "known-sensitive"
                for item in receipt["artifacts"]
            ),
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
        )
    print(json.dumps({"status": "pass", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GuardError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        raise SystemExit(2) from error
