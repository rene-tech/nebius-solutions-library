#!/usr/bin/env python3
"""Append-only, authority-reconciled rotation evidence for durable credentials.

The controller uses one fixed kernel-authenticated read-only authority client.
It records provider IDs, fingerprints, exact consumer proofs, and durable
authority attestations; it never handles credential values or mutates a
provider credential.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from credential_evidence import EvidenceVerificationError, verify_evidence_envelope
from credential_provider_adapter import load_client_policy

from scripts.append_only_evidence import (
    EvidenceError,
    append_event,
    latest_state,
    load_stream,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "security/durable-credential-registry.json"
DEFAULT_CONSUMER_CONTRACTS = ROOT / "security/credential-consumer-contracts.json"
PRODUCTION_PROVIDER_COMMAND = (
    "/usr/bin/python3",
    str(ROOT / "scripts" / "credential_provider_adapter.py"),
)


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


def append_journal_state(path: Path, value: Any) -> None:
    """Append one full state snapshot without replacing or deleting evidence."""

    event = value.get("events", [{}])[-1].get("event", value.get("phase", "state"))
    try:
        append_event(path, stream="credential-rotation", event=event, state=value)
    except EvidenceError as error:
        raise RotationError(str(error)) from error


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


def load_consumer_contracts(
    path: Path, registry: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise RotationError("credential consumer contracts are absent or unsafe")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "fs2-serve.nebius.ai/credential-consumer-contracts/v1":
        raise RotationError("credential consumer contracts have the wrong schema")
    contracts = document.get("contracts")
    expected = {item["id"] for item in registry["credentials"]}
    if not isinstance(contracts, dict) or set(contracts) != expected:
        raise RotationError(
            "credential consumer contracts must cover every registered class exactly"
        )
    for credential_class, contract in contracts.items():
        if (
            not isinstance(contract, dict)
            or set(contract) != {"adapter", "authority", "consumers", "readiness"}
            or not all(
                isinstance(contract.get(field), str) and contract[field]
                for field in ("adapter", "authority", "readiness")
            )
            or not isinstance(contract.get("consumers"), list)
            or not contract["consumers"]
            or not all(
                isinstance(consumer, str) and consumer
                for consumer in contract["consumers"]
            )
            or len(contract["consumers"]) != len(set(contract["consumers"]))
        ):
            raise RotationError(
                f"credential consumer contract is malformed for {credential_class}"
            )
    return contracts


def require_consumer_readiness(
    journal: dict[str, Any],
    successor: dict[str, Any],
    response: dict[str, Any],
    *,
    expected_write_id: str,
) -> None:
    """Verify one class-specific, provider-derived readiness statement."""

    contract = journal["consumer_contract"]
    expected = {
        "schema": "fs2-serve.nebius.ai/credential-consumer-readiness/v2",
        "credential_class": journal["credential_class"],
        "adapter": contract["adapter"],
        "authority": contract["authority"],
        "consumers": contract["consumers"],
        "readiness": contract["readiness"],
        "predecessor_id": journal["predecessor"]["id"],
        "successor_id": successor["id"],
        "read_ids": [journal["predecessor"]["id"], successor["id"]],
        "current_write_id": expected_write_id,
        "ready": True,
    }
    if any(response.get(key) != value for key, value in expected.items()):
        raise RotationError(
            "provider did not prove the exact class-specific consumer contract"
        )
    if not isinstance(response.get("observed_at"), str) or not response["observed_at"]:
        raise RotationError("consumer readiness lacks authoritative evidence identity")
    bindings = response.get("consumer_bindings")
    if not isinstance(bindings, list):
        raise RotationError("consumer readiness lacks exact consumer bindings")
    bound_consumers: set[str] = set()
    for binding in bindings:
        required = {
            "consumer",
            "consumer_identity",
            "credential_identity",
            "generation",
            "ready",
            "observed_at",
            "readiness_evidence_sha256",
        }
        if (
            not isinstance(binding, dict)
            or set(binding) != required
            or binding["consumer"] not in contract["consumers"]
            or not all(
                isinstance(binding[field], str) and binding[field]
                for field in (
                    "consumer",
                    "consumer_identity",
                    "credential_identity",
                    "observed_at",
                    "readiness_evidence_sha256",
                )
            )
            or len(binding["readiness_evidence_sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in binding["readiness_evidence_sha256"]
            )
            or binding["generation"] != successor["generation"]
            or binding["ready"] is not True
        ):
            raise RotationError("consumer readiness has an invalid live binding")
        bound_consumers.add(binding["consumer"])
    if bound_consumers != set(contract["consumers"]):
        raise RotationError("consumer readiness omits a declared exact consumer")


def provider_call(command: Sequence[str], request: dict[str, Any]) -> dict[str, Any]:
    if not command:
        raise RotationError("provider adapter command is empty")
    try:
        result = subprocess.run(
            list(command),
            input=json.dumps(request, sort_keys=True),
            text=True,
            capture_output=True,
            check=True,
        )
        response = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise RotationError("credential provider operation failed") from error
    if not isinstance(response, dict):
        raise RotationError("credential provider returned a malformed response")
    proof = response.get("externalEvidence")
    observation = response.get("authorityObservation")
    if not isinstance(proof, dict) or not isinstance(observation, dict):
        raise RotationError("credential provider response lacks external evidence")
    payload = {
        **observation,
        "result": {
            key: value
            for key, value in response.items()
            if key not in {"externalEvidence", "authorityObservation"}
        },
    }
    claim = proof.get("claim")
    if not isinstance(claim, dict):
        raise RotationError("credential provider external claim is absent")
    try:
        policy = load_client_policy()
        verify_evidence_envelope(
            {**proof, "payload": payload},
            expected_operation=str(request.get("operation", "")),
            expected_request_sha256=str(claim.get("request_sha256", "")),
            expected_nonce=str(claim.get("request_nonce", "")),
            evidence_public_key_sha256=policy["evidence_public_key_sha256"],
            anchor_public_key_sha256=policy["anchor_public_key_sha256"],
        )
    except (EvidenceVerificationError, RuntimeError) as error:
        raise RotationError("credential provider external evidence failed") from error
    return response


def provider_command_identity(command: Sequence[str]) -> dict[str, Any]:
    """Bind every phase to one exact, non-writable provider adapter binary."""

    if tuple(command) != PRODUCTION_PROVIDER_COMMAND:
        raise RotationError("provider adapter must be the fixed production command")
    if (
        len(command) != 2
        or not all(Path(value).is_absolute() for value in command)
        or any(value.startswith("-") for value in command)
    ):
        raise RotationError("provider adapter may not use relative or inline arguments")
    resolved = shutil.which(command[0])
    if resolved is None:
        raise RotationError("provider adapter executable cannot be resolved")
    path = Path(resolved).resolve()
    if path.is_symlink() or not path.is_file():
        raise RotationError("provider adapter executable must be a real file")
    metadata = path.stat()
    if metadata.st_mode & 0o022:
        raise RotationError(
            "provider adapter executable must not be group/world writable"
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    argument_files: list[dict[str, Any]] = []
    for index, argument in enumerate(command[1:], start=1):
        candidate = Path(argument)
        if not candidate.exists():
            continue
        resolved_argument = candidate.resolve()
        if candidate.is_symlink() or not resolved_argument.is_file():
            raise RotationError("provider adapter file arguments must be real files")
        argument_metadata = resolved_argument.stat()
        if argument_metadata.st_mode & 0o022:
            raise RotationError(
                "provider adapter file arguments must not be group/world writable"
            )
        argument_digest = hashlib.sha256()
        with resolved_argument.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                argument_digest.update(chunk)
        argument_files.append(
            {
                "index": index,
                "realpath": str(resolved_argument),
                "device": argument_metadata.st_dev,
                "inode": argument_metadata.st_ino,
                "uid": argument_metadata.st_uid,
                "sha256": argument_digest.hexdigest(),
            }
        )
    return {
        "realpath": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "uid": metadata.st_uid,
        "sha256": digest.hexdigest(),
        "arguments_sha256": canonical_sha256(list(command[1:])),
        "argument_files": argument_files,
    }


def require_provider_command(journal: dict[str, Any], command: Sequence[str]) -> None:
    if journal.get("provider_command") != provider_command_identity(command):
        raise RotationError("provider adapter differs from the journaled authority")


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
        "provider_version",
        "expires_at",
        "readers",
    }
    if not isinstance(document, dict) or not required.issubset(document):
        raise RotationError("provider credential identity is incomplete")
    if (
        not all(
            isinstance(document[key], str) and document[key]
            for key in required - {"generation", "expires_at", "readers"}
        )
        or not isinstance(document["generation"], int)
        or document["generation"] < 1
        or len(document["fingerprint"]) != 64
        or any(
            character not in "0123456789abcdef" for character in document["fingerprint"]
        )
    ):
        raise RotationError("provider credential identity is malformed")
    expires_at = document["expires_at"]
    readers = document["readers"]
    if expires_at is not None:
        if not isinstance(expires_at, str) or not expires_at:
            raise RotationError("provider credential expiry is malformed")
        try:
            parsed_expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise RotationError("provider credential expiry is malformed") from error
        if parsed_expiry.tzinfo is None:
            raise RotationError("provider credential expiry must include a timezone")
    if (
        not isinstance(readers, list)
        or not readers
        or not all(isinstance(reader, str) and reader for reader in readers)
        or len(readers) != len(set(readers))
    ):
        raise RotationError("provider credential readers are incomplete")
    return {key: document[key] for key in sorted(required)}


def require_inventory_policy(identity: dict[str, Any], policy: dict[str, Any]) -> None:
    """Bind provider inventory to the reviewed owner/readers/expiry policy."""

    if sorted(identity["readers"]) != sorted(policy["readers"]):
        raise RotationError("provider credential readers differ from the registry")
    expiry = policy.get("expiry")
    if not isinstance(expiry, dict) or not isinstance(expiry.get("required"), bool):
        raise RotationError("credential registry has a malformed expiry policy")
    if expiry["required"] and identity["expires_at"] is None:
        raise RotationError("provider credential lacks the required expiry")


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
    try:
        journal = latest_state(path, stream="credential-rotation")
    except EvidenceError as error:
        raise RotationError(str(error)) from error
    if journal.get("schema") != "fs2-serve.nebius.ai/credential-rotation/v3":
        raise RotationError("rotation journal has the wrong schema")
    return journal


def operation_request(
    journal: dict[str, Any], operation: str, **extra: Any
) -> dict[str, Any]:
    if operation == "credential-inventory":
        return {"operation": operation}
    if operation == "consumer-readiness":
        return {
            "operation": operation,
            "credential_class": journal["credential_class"],
            "generation": journal["successor_generation"],
            "phase": extra["phase"],
        }
    raise RotationError("rotation requested an unreviewed authority operation")


def reconcile_created(
    journal: dict[str, Any], command: Sequence[str]
) -> dict[str, Any] | None:
    response = provider_call(command, operation_request(journal, "credential-inventory"))
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


def record_phase(
    journal_path: Path,
    journal: dict[str, Any],
    phase: str,
    event: str,
) -> None:
    """Durably record intent or completion before crossing another side effect."""

    journal["phase"] = phase
    journal["events"].append({"event": event, "recorded_at": utc_timestamp()})
    append_journal_state(journal_path, journal)


def reconcile_pending_transition(
    journal: dict[str, Any], command: Sequence[str]
) -> str | None:
    """Recover a read-only proof phase after interruption."""

    successor = journal.get("successor")
    if not isinstance(successor, dict):
        raise RotationError("pending transition has no provider-reconciled successor")
    phase = journal.get("phase")
    if phase == "dual-read-pending":
        response = provider_call(
            command,
            operation_request(
                journal,
                "consumer-readiness",
                phase="dual-read-ready",
            ),
        )
        require_consumer_readiness(
            journal,
            successor,
            response,
            expected_write_id=journal["predecessor"]["id"],
        )
        return "dual-read"
    if phase == "switch-write-pending":
        response = provider_call(
            command,
            operation_request(
                journal,
                "consumer-readiness",
                phase="current-write-ready",
            ),
        )
        require_consumer_readiness(
            journal, successor, response, expected_write_id=successor["id"]
        )
        return "current-write"
    return None


def adopt_successor(args: argparse.Namespace) -> dict[str, Any]:
    """Adopt a separately staged provider credential; never create one here."""
    directory = private_directory(args.directory)
    journal_path = directory / "rotation.events"
    registry = load_registry(args.registry)
    policy = credential_policy(registry, args.credential_class)
    contracts = load_consumer_contracts(args.consumer_contracts, registry)
    provider_identity = provider_command_identity(args.provider_command)
    with locked(directory):
        if journal_path.exists() and load_stream(
            journal_path, stream="credential-rotation"
        ):
            raise RotationError("rotation journal already exists; reconcile it first")
        inventory = provider_call(
            args.provider_command, {"operation": "credential-inventory"}
        )
        predecessor_matches = [
            exact_identity(item)
            for item in inventory.get("items", [])
            if isinstance(item, dict) and item.get("id") == args.predecessor_id
        ]
        if len(predecessor_matches) != 1:
            raise RotationError(
                "authoritative inventory did not return one exact predecessor"
            )
        predecessor = predecessor_matches[0]
        require_lineage(
            predecessor,
            credential_class=args.credential_class,
            owner_id=predecessor["owner_id"],
            project_id=predecessor["project_id"],
            purpose=policy["purpose"],
            generation=args.predecessor_generation,
            statuses={"active"},
        )
        require_inventory_policy(predecessor, policy)
        if len(args.successor_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in args.successor_fingerprint
        ):
            raise RotationError("successor fingerprint must be lowercase SHA-256")
        if args.successor_fingerprint == predecessor["fingerprint"]:
            raise RotationError(
                "successor fingerprint must differ from the retained predecessor"
            )
        journal = {
            "schema": "fs2-serve.nebius.ai/credential-rotation/v3",
            "operation_id": str(uuid.uuid4()),
            "credential_class": args.credential_class,
            "owner_id": predecessor["owner_id"],
            "project_id": predecessor["project_id"],
            "purpose": policy["purpose"],
            "readers": policy["readers"],
            "registry_sha256": canonical_sha256(registry),
            "consumer_contracts_sha256": canonical_sha256(
                {
                    "schema": "fs2-serve.nebius.ai/credential-consumer-contracts/v1",
                    "contracts": contracts,
                }
            ),
            "consumer_contract": contracts[args.credential_class],
            "provider_command": provider_identity,
            "predecessor": predecessor,
            "successor_generation": predecessor["generation"] + 1,
            "successor_fingerprint": args.successor_fingerprint,
            "successor": None,
            "phase": "adoption-pending",
            "created_at": utc_timestamp(),
            "events": [],
        }
        append_journal_state(journal_path, journal)
        created = reconcile_created(journal, args.provider_command)
        if created is None:
            raise RotationError(
                "the separately staged successor is absent from authoritative provider inventory"
            )
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
        require_inventory_policy(created, policy)
        reconciled = reconcile_created(journal, args.provider_command)
        if reconciled != created:
            raise RotationError(
                "created successor differs from provider reconciliation"
            )
        journal["successor"] = created
        journal["phase"] = "successor-active"
        journal["events"].append(
            {"event": "successor-adopted", "recorded_at": utc_timestamp()}
        )
        append_journal_state(journal_path, journal)
        return {"status": journal["phase"], "credential_id": created["id"]}


def reconcile(args: argparse.Namespace) -> dict[str, Any]:
    directory = private_directory(args.directory)
    journal_path = directory / "rotation.events"
    with locked(directory):
        journal = load_journal(journal_path)
        require_provider_command(journal, args.provider_command)
        if journal.get("phase") in {"dual-read-pending", "switch-write-pending"}:
            recovered = reconcile_pending_transition(journal, args.provider_command)
            if recovered is None:
                return {"status": journal["phase"]}
            record_phase(
                journal_path,
                journal,
                recovered,
                f"{journal['phase']}-reconciled",
            )
            return {
                "status": recovered,
                "credential_id": journal["successor"]["id"],
            }
        successor = reconcile_created(journal, args.provider_command)
        if successor is None:
            if journal["phase"] not in {"adoption-pending"}:
                raise RotationError("recorded successor is absent from the provider")
            return {"status": "no-successor-created"}
        if journal.get("successor") not in (None, successor):
            raise RotationError("provider successor differs from the durable journal")
        journal["successor"] = successor
        if journal["phase"] in {"adoption-pending"}:
            journal["phase"] = "successor-active"
        journal["events"].append(
            {"event": "provider-reconciled", "recorded_at": utc_timestamp()}
        )
        append_journal_state(journal_path, journal)
        return {"status": journal["phase"], "credential_id": successor["id"]}


def transition(args: argparse.Namespace) -> dict[str, Any]:
    directory = private_directory(args.directory)
    journal_path = directory / "rotation.events"
    with locked(directory):
        journal = load_journal(journal_path)
        require_provider_command(journal, args.provider_command)
        successor = journal.get("successor")
        if not isinstance(successor, dict):
            raise RotationError("a provider-reconciled successor is required")
        pending_for_command = {
            "prove-dual-read": "dual-read-pending",
            "prove-current-write": "switch-write-pending",
        }
        expected_pending = pending_for_command.get(args.command)
        if journal.get("phase") == expected_pending:
            recovered = reconcile_pending_transition(journal, args.provider_command)
            if recovered is not None:
                record_phase(
                    journal_path,
                    journal,
                    recovered,
                    f"{expected_pending}-reconciled",
                )
                return {"status": recovered, "credential_id": successor["id"]}
            retry_phases = {
                "prove-current-write": "dual-read",
            }
            if args.command not in retry_phases:
                raise RotationError("pending transition could not be reconciled")
            journal["phase"] = retry_phases[args.command]
        if args.command == "prove-dual-read":
            if journal["phase"] != "successor-active":
                raise RotationError("dual-read proof is out of order")
            record_phase(journal_path, journal, "dual-read-pending", "dual-read-intent")
            response = provider_call(
                args.provider_command,
                operation_request(
                    journal,
                    "consumer-readiness",
                    phase="dual-read-ready",
                ),
            )
            require_consumer_readiness(
                journal,
                successor,
                response,
                expected_write_id=journal["predecessor"]["id"],
            )
            journal["dual_read_evidence_sha256"] = canonical_sha256(response)
            record_phase(journal_path, journal, "dual-read", "prove-dual-read")
        elif args.command == "prove-current-write":
            if journal["phase"] != "dual-read":
                raise RotationError("write cutover requires dual-read proof")
            record_phase(
                journal_path, journal, "switch-write-pending", "switch-write-intent"
            )
            response = provider_call(
                args.provider_command,
                operation_request(
                    journal,
                    "consumer-readiness",
                    phase="current-write-ready",
                ),
            )
            require_consumer_readiness(
                journal, successor, response, expected_write_id=successor["id"]
            )
            journal["current_write_evidence_sha256"] = canonical_sha256(response)
            record_phase(journal_path, journal, "current-write", "prove-current-write")
        else:
            raise RotationError("unsupported rotation transition")
        journal["events"].append(
            {"event": args.command, "recorded_at": utc_timestamp()}
        )
        append_journal_state(journal_path, journal)
        return {"status": journal["phase"], "credential_id": successor["id"]}


def audit_inventory(args: argparse.Namespace) -> dict[str, Any]:
    """Seal a provider-derived, value-free inventory for every credential class."""

    directory = private_directory(args.directory)
    receipt_path = directory / "credential-inventory.events"
    registry = load_registry(args.registry)
    contracts = load_consumer_contracts(args.consumer_contracts, registry)
    provider_identity = provider_command_identity(args.provider_command)
    with locked(directory):
        if receipt_path.exists() and load_stream(
            receipt_path, stream="credential-inventory"
        ):
            raise RotationError("credential inventory receipt is write-once")
        response = provider_call(
            args.provider_command,
            {"operation": "credential-inventory"},
        )
        raw_items = response.get("items")
        if not isinstance(raw_items, list):
            raise RotationError("provider returned no complete credential inventory")
        items = [exact_identity(item) for item in raw_items]
        ids = [item["id"] for item in items]
        fingerprints = [item["fingerprint"] for item in items]
        if len(ids) != len(set(ids)) or len(fingerprints) != len(set(fingerprints)):
            raise RotationError("provider inventory reuses an ID or fingerprint")

        projects = {item["project_id"] for item in items}
        if len(projects) != 1:
            raise RotationError("provider inventory spans multiple projects")
        project_id = next(iter(projects))
        classes: dict[str, list[dict[str, Any]]] = {}
        for policy in registry["credentials"]:
            credential_class = policy["id"]
            matches = [
                item for item in items if item["credential_class"] == credential_class
            ]
            if not matches:
                raise RotationError(
                    f"provider inventory omits credential class {credential_class}"
                )
            owners = {item["owner_id"] for item in matches}
            generations = sorted(item["generation"] for item in matches)
            if (
                any(item["project_id"] != project_id for item in matches)
                or any(item["purpose"] != policy["purpose"] for item in matches)
                or len(owners) != 1
                or generations != list(range(1, max(generations) + 1))
                or not any(item["status"] == "active" for item in matches)
            ):
                raise RotationError(
                    f"provider inventory has invalid lineage for {credential_class}"
                )
            for item in matches:
                require_inventory_policy(item, policy)
            classes[credential_class] = sorted(
                matches, key=lambda item: item["generation"]
            )
        unknown = sorted({item["credential_class"] for item in items} - set(classes))
        if unknown:
            raise RotationError(
                "provider inventory contains unregistered credential classes: "
                + ", ".join(unknown)
            )
        receipt = {
            "schema": "fs2-serve.nebius.ai/credential-inventory/v1",
            "project_id": project_id,
            "registry_sha256": canonical_sha256(registry),
            "consumer_contracts_sha256": canonical_sha256(
                {
                    "schema": "fs2-serve.nebius.ai/credential-consumer-contracts/v1",
                    "contracts": contracts,
                }
            ),
            "provider_command": provider_identity,
            "external_evidence": response["externalEvidence"],
            "authority_observation": response["authorityObservation"],
            "audited_at": utc_timestamp(),
            "classes": dict(sorted(classes.items())),
        }
        try:
            appended = append_event(
                receipt_path,
                stream="credential-inventory",
                event="provider-inventory-audited",
                state=receipt,
            )
        except EvidenceError as error:
            raise RotationError(str(error)) from error
        return {
            "status": "inventory-audited",
            "credential_classes": len(classes),
            "credentials": len(items),
            "receipt": str(receipt_path),
            "receipt_sha256": appended["sha256"],
        }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--consumer-contracts", type=Path, default=DEFAULT_CONSUMER_CONTRACTS
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("adopt-successor")
    create_parser.add_argument("--credential-class", required=True)
    create_parser.add_argument("--predecessor-id", required=True)
    create_parser.add_argument("--predecessor-generation", type=int, required=True)
    create_parser.add_argument("--successor-fingerprint", required=True)
    subparsers.add_parser("reconcile")
    subparsers.add_parser("audit-inventory")
    subparsers.add_parser("prove-dual-read")
    subparsers.add_parser("prove-current-write")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(argv)
    args.provider_command = list(PRODUCTION_PROVIDER_COMMAND)
    handler = {
        "adopt-successor": adopt_successor,
        "reconcile": reconcile,
        "audit-inventory": audit_inventory,
    }.get(args.command, transition)
    print(json.dumps(handler(args), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RotationError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        raise SystemExit(2) from error
