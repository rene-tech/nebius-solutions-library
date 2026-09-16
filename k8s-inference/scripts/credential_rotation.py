#!/usr/bin/env python3
"""Crash-safe, provider-reconciled rotation for every durable credential class.

The provider adapter is an executable that accepts one JSON request on stdin
and returns one JSON document on stdout. Secret material is supplied directly
to that adapter (for example through its process environment); this controller
records only provider IDs and SHA-256 fingerprints.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "security/durable-credential-registry.json"


class RotationError(RuntimeError):
    pass


def utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def private_directory(path: Path) -> Path:
    path = path.absolute()
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise RotationError("rotation directory must be a real directory")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise RotationError("rotation directory path must contain no symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RotationError("rotation directory must be owner-owned mode 0700")
    return path


def atomic_private_json(path: Path, value: Any) -> None:
    if path.is_symlink():
        raise RotationError("rotation journal path must not be a symlink")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(directory: Path) -> Iterator[None]:
    path = directory / "rotation.lock"
    descriptor = os.open(
        path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def load_registry(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RotationError("durable credential registry is absent or unsafe")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "fs2-serve.nebius.ai/durable-credential-registry/v2":
        raise RotationError("durable credential registry has the wrong schema")
    credentials = document.get("credentials")
    if not isinstance(credentials, list):
        raise RotationError("durable credential registry has no entries")
    return document


def credential_policy(
    registry: dict[str, Any], credential_class: str
) -> dict[str, Any]:
    matches = [
        item
        for item in registry["credentials"]
        if isinstance(item, dict) and item.get("id") == credential_class
    ]
    if len(matches) != 1:
        raise RotationError("credential class is absent or duplicated in the registry")
    policy = matches[0]
    if policy.get("rotation") != {
        "strategy": "dual-read-current-write",
        "disable_before_delete": True,
    }:
        raise RotationError("credential class lacks the mandatory rotation policy")
    return policy


def provider_call(command: Sequence[str], request: dict[str, Any]) -> dict[str, Any]:
    if not command:
        raise RotationError("provider adapter command is empty")
    try:
        result = subprocess.run(
            list(command),
            input=json.dumps(request, sort_keys=True),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        response = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise RotationError("credential provider operation failed") from error
    if not isinstance(response, dict):
        raise RotationError("credential provider returned a malformed response")
    return response


def exact_identity(document: Any) -> dict[str, Any]:
    required = {
        "id",
        "credential_class",
        "owner_id",
        "project_id",
        "purpose",
        "generation",
        "fingerprint",
        "status",
    }
    if not isinstance(document, dict) or not required.issubset(document):
        raise RotationError("provider credential identity is incomplete")
    if (
        not all(
            isinstance(document[key], str) and document[key]
            for key in required - {"generation"}
        )
        or not isinstance(document["generation"], int)
        or document["generation"] < 1
        or len(document["fingerprint"]) != 64
        or any(
            character not in "0123456789abcdef" for character in document["fingerprint"]
        )
    ):
        raise RotationError("provider credential identity is malformed")
    return {key: document[key] for key in sorted(required)}


def require_lineage(
    identity: dict[str, Any],
    *,
    credential_class: str,
    owner_id: str,
    project_id: str,
    purpose: str,
    generation: int,
    fingerprint: str | None = None,
    statuses: set[str],
) -> None:
    expected = {
        "credential_class": credential_class,
        "owner_id": owner_id,
        "project_id": project_id,
        "purpose": purpose,
        "generation": generation,
    }
    if any(identity.get(key) != value for key, value in expected.items()):
        raise RotationError("provider credential is outside the exact rotation lineage")
    if fingerprint is not None and identity.get("fingerprint") != fingerprint:
        raise RotationError("provider credential fingerprint differs from the journal")
    if identity.get("status") not in statuses:
        raise RotationError("provider credential has an invalid lifecycle status")


def load_journal(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RotationError("rotation journal is absent or unsafe")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RotationError("rotation journal must be owner-owned mode 0600")
    journal = json.loads(path.read_text(encoding="utf-8"))
    if journal.get("schema") != "fs2-serve.nebius.ai/credential-rotation/v2":
        raise RotationError("rotation journal has the wrong schema")
    return journal


def operation_request(
    journal: dict[str, Any], operation: str, **extra: Any
) -> dict[str, Any]:
    return {
        "schema": "fs2-serve.nebius.ai/credential-provider-operation/v1",
        "operation": operation,
        "operation_id": journal["operation_id"],
        "credential_class": journal["credential_class"],
        "owner_id": journal["owner_id"],
        "project_id": journal["project_id"],
        "purpose": journal["purpose"],
        **extra,
    }


def reconcile_created(
    journal: dict[str, Any], command: Sequence[str]
) -> dict[str, Any] | None:
    response = provider_call(command, operation_request(journal, "list-operation"))
    items = response.get("items")
    if not isinstance(items, list):
        raise RotationError("provider reconciliation returned no complete inventory")
    matches: list[dict[str, Any]] = []
    for raw in items:
        identity = exact_identity(raw)
        try:
            require_lineage(
                identity,
                credential_class=journal["credential_class"],
                owner_id=journal["owner_id"],
                project_id=journal["project_id"],
                purpose=journal["purpose"],
                generation=journal["successor_generation"],
                fingerprint=journal["successor_fingerprint"],
                statuses={"active", "disabled"},
            )
        except RotationError:
            continue
        matches.append(identity)
    if len(matches) > 1:
        raise RotationError(
            "provider reconciliation found duplicate successor credentials"
        )
    return matches[0] if matches else None


def create(args: argparse.Namespace) -> dict[str, Any]:
    directory = private_directory(args.directory)
    journal_path = directory / "journal.json"
    registry = load_registry(args.registry)
    policy = credential_policy(registry, args.credential_class)
    with locked(directory):
        if journal_path.exists():
            raise RotationError("rotation journal already exists; reconcile it first")
        predecessor = exact_identity(
            provider_call(
                args.provider_command,
                {
                    "schema": "fs2-serve.nebius.ai/credential-provider-operation/v1",
                    "operation": "get",
                    "credential_id": args.predecessor_id,
                },
            )
        )
        require_lineage(
            predecessor,
            credential_class=args.credential_class,
            owner_id=args.owner_id,
            project_id=args.project_id,
            purpose=policy["purpose"],
            generation=args.predecessor_generation,
            statuses={"active"},
        )
        if len(args.successor_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in args.successor_fingerprint
        ):
            raise RotationError("successor fingerprint must be lowercase SHA-256")
        journal = {
            "schema": "fs2-serve.nebius.ai/credential-rotation/v2",
            "operation_id": str(uuid.uuid4()),
            "credential_class": args.credential_class,
            "owner_id": args.owner_id,
            "project_id": args.project_id,
            "purpose": policy["purpose"],
            "readers": policy["readers"],
            "registry_sha256": canonical_sha256(registry),
            "predecessor": predecessor,
            "successor_generation": predecessor["generation"] + 1,
            "successor_fingerprint": args.successor_fingerprint,
            "successor": None,
            "phase": "create-pending",
            "created_at": utc_timestamp(),
            "events": [],
        }
        atomic_private_json(journal_path, journal)
        try:
            created = exact_identity(
                provider_call(
                    args.provider_command,
                    operation_request(
                        journal,
                        "create",
                        generation=journal["successor_generation"],
                        fingerprint=journal["successor_fingerprint"],
                    ),
                )
            )
        except BaseException:
            journal["phase"] = "create-uncertain"
            journal["events"].append(
                {"event": "create-interrupted", "recorded_at": utc_timestamp()}
            )
            atomic_private_json(journal_path, journal)
            reconciled = reconcile_created(journal, args.provider_command)
            if reconciled is not None:
                journal["successor"] = reconciled
                journal["phase"] = "successor-active"
                journal["events"].append(
                    {"event": "create-reconciled", "recorded_at": utc_timestamp()}
                )
                atomic_private_json(journal_path, journal)
            raise
        require_lineage(
            created,
            credential_class=args.credential_class,
            owner_id=args.owner_id,
            project_id=args.project_id,
            purpose=policy["purpose"],
            generation=journal["successor_generation"],
            fingerprint=journal["successor_fingerprint"],
            statuses={"active"},
        )
        reconciled = reconcile_created(journal, args.provider_command)
        if reconciled != created:
            raise RotationError(
                "created successor differs from provider reconciliation"
            )
        journal["successor"] = created
        journal["phase"] = "successor-active"
        journal["events"].append(
            {"event": "successor-created", "recorded_at": utc_timestamp()}
        )
        atomic_private_json(journal_path, journal)
        return {"status": journal["phase"], "credential_id": created["id"]}


def reconcile(args: argparse.Namespace) -> dict[str, Any]:
    directory = private_directory(args.directory)
    journal_path = directory / "journal.json"
    with locked(directory):
        journal = load_journal(journal_path)
        successor = reconcile_created(journal, args.provider_command)
        if successor is None:
            if journal["phase"] not in {"create-pending", "create-uncertain"}:
                raise RotationError("recorded successor is absent from the provider")
            return {"status": "no-successor-created"}
        if journal.get("successor") not in (None, successor):
            raise RotationError("provider successor differs from the durable journal")
        journal["successor"] = successor
        if journal["phase"] in {"create-pending", "create-uncertain"}:
            journal["phase"] = "successor-active"
        journal["events"].append(
            {"event": "provider-reconciled", "recorded_at": utc_timestamp()}
        )
        atomic_private_json(journal_path, journal)
        return {"status": journal["phase"], "credential_id": successor["id"]}


def transition(args: argparse.Namespace) -> dict[str, Any]:
    directory = private_directory(args.directory)
    journal_path = directory / "journal.json"
    with locked(directory):
        journal = load_journal(journal_path)
        successor = journal.get("successor")
        if not isinstance(successor, dict):
            raise RotationError("a provider-reconciled successor is required")
        if args.command == "prove-dual-read":
            if journal["phase"] != "successor-active":
                raise RotationError("dual-read proof is out of order")
            response = provider_call(
                args.provider_command,
                operation_request(
                    journal,
                    "prove-consumers",
                    predecessor_id=journal["predecessor"]["id"],
                    successor_id=successor["id"],
                    expected_read_ids=[journal["predecessor"]["id"], successor["id"]],
                    expected_write_id=journal["predecessor"]["id"],
                ),
            )
            if response != {
                "dual_read": True,
                "current_write_id": journal["predecessor"]["id"],
            }:
                raise RotationError(
                    "provider did not prove dual-read/predecessor-write"
                )
            journal["phase"] = "dual-read"
        elif args.command == "switch-write":
            if journal["phase"] != "dual-read":
                raise RotationError("write cutover requires dual-read proof")
            response = provider_call(
                args.provider_command,
                operation_request(
                    journal,
                    "switch-write",
                    predecessor_id=journal["predecessor"]["id"],
                    successor_id=successor["id"],
                ),
            )
            if response != {"dual_read": True, "current_write_id": successor["id"]}:
                raise RotationError("provider did not prove successor-only writes")
            journal["phase"] = "current-write"
        elif args.command == "disable-old":
            if journal["phase"] != "current-write":
                raise RotationError(
                    "predecessor disable requires successor write proof"
                )
            if journal["credential_class"] == "payload-keyring":
                migration = provider_call(
                    args.provider_command,
                    operation_request(
                        journal,
                        "prove-ciphertext-migration",
                        predecessor_id=journal["predecessor"]["id"],
                        successor_id=successor["id"],
                        aad_contract="fs2-serve.nebius.ai/payload-aad/v1",
                    ),
                )
                required_counts = (
                    "operation_ciphertexts",
                    "request_debug_ciphertexts",
                    "customer_storage_ciphertexts",
                )
                if (
                    migration.get("aad_contract")
                    != "fs2-serve.nebius.ai/payload-aad/v1"
                    or migration.get("old_key_references") != 0
                    or migration.get("current_write_id") != successor["id"]
                    or not all(
                        isinstance(migration.get(key), int) and migration[key] >= 1
                        for key in required_counts
                    )
                ):
                    raise RotationError(
                        "payload predecessor cannot be disabled before deployed operation, request-debug and customer-storage migration proof"
                    )
                journal["payload_migration_evidence_sha256"] = canonical_sha256(
                    migration
                )
            if journal["credential_class"] in {
                "pat-bootstrap",
                "pat-scientific",
                "pat-website",
                "admin-token",
            }:
                continuity = provider_call(
                    args.provider_command,
                    operation_request(
                        journal,
                        "prove-auth-continuity",
                        predecessor_id=journal["predecessor"]["id"],
                        successor_id=successor["id"],
                    ),
                )
                if continuity != {
                    "predecessor_valid": True,
                    "successor_valid": True,
                    "current_write_id": successor["id"],
                }:
                    raise RotationError(
                        "authentication predecessor cannot be disabled without overlap continuity proof"
                    )
                journal["auth_continuity_evidence_sha256"] = canonical_sha256(
                    continuity
                )
            response = provider_call(
                args.provider_command,
                operation_request(
                    journal,
                    "disable",
                    credential_id=journal["predecessor"]["id"],
                    successor_id=successor["id"],
                ),
            )
            identity = exact_identity(response)
            require_lineage(
                identity,
                credential_class=journal["credential_class"],
                owner_id=journal["owner_id"],
                project_id=journal["project_id"],
                purpose=journal["purpose"],
                generation=journal["predecessor"]["generation"],
                fingerprint=journal["predecessor"]["fingerprint"],
                statuses={"disabled"},
            )
            journal["phase"] = "predecessor-disabled"
        elif args.command == "delete-old":
            if journal["phase"] != "predecessor-disabled":
                raise RotationError("predecessor must be disabled before deletion")
            if args.confirm_predecessor_id != journal["predecessor"]["id"]:
                raise RotationError("deletion confirmation differs from predecessor")
            proof = provider_call(
                args.provider_command,
                operation_request(
                    journal,
                    "prove-zero-readers",
                    credential_id=journal["predecessor"]["id"],
                    successor_id=successor["id"],
                ),
            )
            if proof != {"reader_references": 0, "current_write_id": successor["id"]}:
                raise RotationError("provider did not prove zero predecessor readers")
            provider_call(
                args.provider_command,
                operation_request(
                    journal,
                    "delete",
                    credential_id=journal["predecessor"]["id"],
                ),
            )
            absence = provider_call(
                args.provider_command,
                operation_request(
                    journal,
                    "prove-absence",
                    credential_id=journal["predecessor"]["id"],
                    successor_id=successor["id"],
                ),
            )
            if absence != {
                "get_absent": True,
                "list_absent": True,
                "successor_active": True,
            }:
                raise RotationError("provider did not prove predecessor deletion")
            journal["phase"] = "predecessor-deleted"
        else:
            raise RotationError("unsupported rotation transition")
        journal["events"].append(
            {"event": args.command, "recorded_at": utc_timestamp()}
        )
        atomic_private_json(journal_path, journal)
        return {"status": journal["phase"], "credential_id": successor["id"]}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--provider-command", action="append", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--credential-class", required=True)
    create_parser.add_argument("--owner-id", required=True)
    create_parser.add_argument("--project-id", required=True)
    create_parser.add_argument("--predecessor-id", required=True)
    create_parser.add_argument("--predecessor-generation", type=int, required=True)
    create_parser.add_argument("--successor-fingerprint", required=True)
    subparsers.add_parser("reconcile")
    subparsers.add_parser("prove-dual-read")
    subparsers.add_parser("switch-write")
    subparsers.add_parser("disable-old")
    delete_parser = subparsers.add_parser("delete-old")
    delete_parser.add_argument("--confirm-predecessor-id", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(argv)
    handler = (
        create
        if args.command == "create"
        else reconcile
        if args.command == "reconcile"
        else transition
    )
    print(json.dumps(handler(args), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RotationError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        raise SystemExit(2) from error
