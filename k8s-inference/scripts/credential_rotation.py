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
CONSUMER_OPERATIONS = frozenset(
    {
        "consumer-readiness",
        "rotation-readiness",
        "ciphertext-migration",
        "authentication-continuity",
    }
)
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
    if document.get("schema") != "fs2-serve.nebius.ai/durable-credential-registry/v3":
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
    if (
        document.get("pending_contract_ids") != []
        or not isinstance(contracts, dict)
        or set(contracts) != expected
    ):
        raise RotationError(
            "credential consumer contracts must cover every registered class exactly"
        )
    for credential_class, contract in contracts.items():
        if (
            not isinstance(contract, dict)
            or set(contract)
            != {
                "adapter",
                "authority",
                "consumers",
                "readiness",
                "required_operations",
            }
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
            or not isinstance(contract.get("required_operations"), list)
            or not contract["required_operations"]
            or len(contract["required_operations"])
            != len(set(contract["required_operations"]))
            or not set(contract["required_operations"]) <= CONSUMER_OPERATIONS
            or "consumer-readiness" not in contract["required_operations"]
            or "rotation-readiness" not in contract["required_operations"]
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
    source_trust = response.get("source_trust")
    credential_bindings = response.get("credential_bindings")
    if (
        not isinstance(source_trust, dict)
        or set(source_trust)
        != {
            "credential_class",
            "generation",
            "retained_generations",
            "registry_sha256",
            "credential_identities_sha256",
            "terraform_bindings_sha256",
            "secret_bindings_sha256",
        }
        or source_trust.get("credential_class") != journal["credential_class"]
        or source_trust.get("generation") != successor["generation"]
        or source_trust.get("retained_generations")
        != list(range(1, successor["generation"] + 1))
        or any(
            not isinstance(source_trust.get(field), str)
            or len(source_trust[field]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in source_trust[field]
            )
            for field in (
                "registry_sha256",
                "credential_identities_sha256",
                "terraform_bindings_sha256",
                "secret_bindings_sha256",
            )
        )
        or not isinstance(credential_bindings, dict)
        or source_trust.get("secret_bindings_sha256")
        != canonical_sha256(credential_bindings)
        or response.get("credential_bindings_sha256")
        != canonical_sha256(credential_bindings)
    ):
        raise RotationError(
            "consumer readiness lacks exact class-specific source bindings"
        )
    source_identity = canonical_sha256(
        {
            "source_trust": source_trust,
            "credential_bindings": credential_bindings,
        }
    )
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
            or binding["credential_identity"] != source_identity
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
            source_trust=policy["source_trust"],
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
        if parsed_expiry.astimezone(UTC) <= datetime.now(UTC):
            raise RotationError("provider credential expiry is not future-valid")
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
    if expiry["required"]:
        expires_at = identity["expires_at"]
        if expires_at is None:
            raise RotationError("provider credential lacks the required expiry")
        parsed_expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if parsed_expiry.astimezone(UTC) <= datetime.now(UTC):
            raise RotationError("provider credential expiry is not future-valid")


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


def require_source_binding(response: dict[str, Any], journal: dict[str, Any]) -> None:
    """Require the adapter result to retain the authority's exact source binding."""

    source_trust = response.get("source_trust")
    bindings = response.get("credential_bindings")
    if (
        not isinstance(source_trust, dict)
        or source_trust.get("credential_class") != journal["credential_class"]
        or source_trust.get("generation") != journal["successor_generation"]
        or source_trust.get("retained_generations")
        != list(range(1, journal["successor_generation"] + 1))
        or not isinstance(bindings, dict)
        or source_trust.get("secret_bindings_sha256") != canonical_sha256(bindings)
        or response.get("credential_bindings_sha256")
        != canonical_sha256(bindings)
    ):
        raise RotationError("class observation lacks exact all-generation source custody")


def require_rotation_readiness(
    journal: dict[str, Any],
    response: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accept lifecycle state only from the class's provider-native adapter."""

    predecessor = exact_identity(response.get("predecessor"))
    successor = exact_identity(response.get("successor"))
    expected = {
        "schema": "fs2-serve.nebius.ai/credential-rotation-readiness/v1",
        "credential_class": journal["credential_class"],
        "predecessor_id": journal["predecessor_id"],
        "successor_id": journal["successor_id"],
        "ready": True,
        "mutation_performed": False,
    }
    if any(response.get(key) != value for key, value in expected.items()):
        raise RotationError("provider did not attest the exact read-only rotation lineage")
    require_source_binding(response, journal)
    for identity, generation, fingerprint in (
        (
            predecessor,
            journal["predecessor_generation"],
            journal.get("predecessor_fingerprint"),
        ),
        (successor, journal["successor_generation"], journal["successor_fingerprint"]),
    ):
        require_lineage(
            identity,
            credential_class=journal["credential_class"],
            owner_id=journal["owner_id"],
            project_id=journal["project_id"],
            purpose=journal["purpose"],
            generation=generation,
            fingerprint=fingerprint,
            statuses={"active"},
        )
        require_inventory_policy(identity, policy)
    if (
        predecessor["id"] == successor["id"]
        or predecessor["fingerprint"] == successor["fingerprint"]
    ):
        raise RotationError("provider reused the predecessor identity for the successor")
    return predecessor, successor


def require_authentication_continuity(
    journal: dict[str, Any], response: dict[str, Any]
) -> None:
    expected = {
        "schema": "fs2-serve.nebius.ai/credential-authentication-continuity/v1",
        "credential_class": journal["credential_class"],
        "predecessor_id": journal["predecessor"]["id"],
        "successor_id": journal["successor"]["id"],
        "predecessor_authenticates": True,
        "successor_authenticates": True,
        "predecessor_disabled": False,
        "ready": True,
        "mutation_performed": False,
    }
    if any(response.get(key) != value for key, value in expected.items()):
        raise RotationError("authentication continuity is not proven for both generations")
    require_source_binding(response, journal)


def require_ciphertext_migration(
    journal: dict[str, Any], response: dict[str, Any]
) -> None:
    expected = {
        "schema": "fs2-serve.nebius.ai/credential-ciphertext-migration/v1",
        "credential_class": journal["credential_class"],
        "from": journal["predecessor"]["generation"],
        "to": journal["successor_generation"],
        "preexisting_predecessor_read_verified": True,
        "preexisting_successor_read_verified": True,
        "successor_write_read_verified": True,
        "rollback_read_verified": True,
        "tenant_principal_binding_verified": True,
        "retirement_allowed": False,
        "ready": True,
        "mutation_performed": False,
    }
    if any(response.get(key) != value for key, value in expected.items()):
        raise RotationError(
            "ciphertext migration does not preserve old, new, rollback and binding semantics"
        )
    require_source_binding(response, journal)


def required_semantic_observations(
    journal: dict[str, Any], command: Sequence[str]
) -> dict[str, str]:
    """Run every non-consumer operation declared by this exact class contract."""

    digests: dict[str, str] = {}
    required = set(journal["consumer_contract"]["required_operations"])
    if "authentication-continuity" in required:
        response = class_operation_observation(
            journal, command, "authentication-continuity"
        )
        require_authentication_continuity(journal, response)
        digests["authentication-continuity"] = canonical_sha256(response)
    if "ciphertext-migration" in required:
        response = class_operation_observation(journal, command, "ciphertext-migration")
        require_ciphertext_migration(journal, response)
        digests["ciphertext-migration"] = canonical_sha256(response)
    if required != {"consumer-readiness", "rotation-readiness", *digests}:
        raise RotationError("credential workflow did not execute every required operation")
    return dict(sorted(digests.items()))


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
    if operation in CONSUMER_OPERATIONS:
        request = {
            "operation": operation,
            "credential_class": journal["credential_class"],
            "source_trust": extra["source_trust"],
            "bindings_sha256": canonical_sha256(extra["credential_bindings"]),
            "credential_bindings": extra["credential_bindings"],
        }
        if operation == "consumer-readiness":
            request.update(
                {
                    "generation": journal["successor_generation"],
                    "phase": extra["phase"],
                }
            )
        elif operation == "rotation-readiness":
            request.update(
                {
                    "predecessor_id": extra["predecessor_id"],
                    "successor_id": extra["successor_id"],
                }
            )
        elif operation == "ciphertext-migration":
            request.update(
                {
                    "from": journal["predecessor"]["generation"],
                    "to": journal["successor_generation"],
                }
            )
        else:
            request.update(
                {
                    "predecessor_id": journal["predecessor"]["id"],
                    "successor_id": journal["successor"]["id"],
                }
            )
        return request
    raise RotationError("rotation requested an unreviewed authority operation")


def class_source_material(
    journal: dict[str, Any], command: Sequence[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return exact all-generation source custody for one credential class."""

    inventory = provider_call(command, {"operation": "credential-inventory"})
    items = inventory.get("items")
    classes = inventory.get("classes")
    if not isinstance(items, list) or not isinstance(classes, dict):
        raise RotationError("credential inventory omitted source bindings")
    class_source = classes.get(journal["credential_class"])
    if (
        not isinstance(class_source, dict)
        or class_source.get("current_generation")
        != journal["successor_generation"]
    ):
        raise RotationError("successor is not the class current-write generation")
    source_trust = class_source.get("source_trust")
    credential_bindings = class_source.get("secret_bindings")
    if not isinstance(source_trust, dict) or not isinstance(
        credential_bindings, dict
    ):
        raise RotationError("successor source binding is incomplete")
    source_ids = {
        item.get("id")
        for item in items
        if isinstance(item, dict)
        and item.get("credential_class") == journal["credential_class"]
    }
    expected_ids = {journal["predecessor_id"]}
    successor = journal.get("successor")
    if isinstance(successor, dict):
        expected_ids.add(successor["id"])
    elif isinstance(journal.get("successor_id"), str):
        expected_ids.add(journal["successor_id"])
    if not expected_ids <= source_ids:
        raise RotationError("rotation lineage is absent from exact source custody")
    return source_trust, credential_bindings


def class_operation_observation(
    journal: dict[str, Any], command: Sequence[str], operation: str, **extra: Any
) -> dict[str, Any]:
    """Invoke one contract-declared read-only operation with exact source custody."""

    if operation not in journal["consumer_contract"]["required_operations"]:
        raise RotationError(
            f"credential contract does not declare required operation {operation}"
        )
    source_trust, credential_bindings = class_source_material(journal, command)
    return provider_call(
        command,
        operation_request(
            journal,
            operation,
            source_trust=source_trust,
            credential_bindings=credential_bindings,
            **extra,
        ),
    )


def consumer_readiness_observation(
    journal: dict[str, Any], command: Sequence[str], *, phase: str
) -> dict[str, Any]:
    """Bind a readiness request to one externally verified class generation."""

    return class_operation_observation(
        journal, command, "consumer-readiness", phase=phase
    )


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
                statuses={"source-observed"},
            )
        except RotationError:
            continue
        matches.append(identity)
    if len(matches) > 1:
        raise RotationError(
            "provider reconciliation found duplicate successor credentials"
        )
    if not matches:
        return None
    journal["successor_id"] = matches[0]["id"]
    readiness = class_operation_observation(
        journal,
        command,
        "rotation-readiness",
        predecessor_id=journal["predecessor_id"],
        successor_id=journal["successor_id"],
    )
    policy = {
        "readers": journal["readers"],
        "expiry": journal["expiry_policy"],
    }
    predecessor, successor = require_rotation_readiness(journal, readiness, policy)
    if predecessor != journal["predecessor"]:
        raise RotationError("provider predecessor differs from the durable journal")
    journal["rotation_readiness_evidence_sha256"] = canonical_sha256(readiness)
    return successor


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
    observed_successor = reconcile_created(journal, command)
    if observed_successor != successor:
        raise RotationError(
            "pending transition lineage is no longer active in provider readiness"
        )
    phase = journal.get("phase")
    if phase == "dual-read-pending":
        response = consumer_readiness_observation(
            journal, command, phase="dual-read-ready"
        )
        require_consumer_readiness(
            journal,
            successor,
            response,
            expected_write_id=journal["predecessor"]["id"],
        )
        journal["dual_read_semantic_evidence"] = required_semantic_observations(
            journal, command
        )
        journal["dual_read_evidence_sha256"] = canonical_sha256(response)
        return "dual-read"
    if phase == "switch-write-pending":
        response = consumer_readiness_observation(
            journal, command, phase="current-write-ready"
        )
        require_consumer_readiness(
            journal, successor, response, expected_write_id=successor["id"]
        )
        journal["current_write_semantic_evidence"] = required_semantic_observations(
            journal, command
        )
        journal["current_write_evidence_sha256"] = canonical_sha256(response)
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
        source_items = [
            exact_identity(item)
            for item in inventory.get("items", [])
            if isinstance(item, dict)
        ]
        predecessor_matches = [
            item for item in source_items if item.get("id") == args.predecessor_id
        ]
        if len(predecessor_matches) != 1:
            raise RotationError(
                "authoritative inventory did not return one exact predecessor"
            )
        predecessor_source = predecessor_matches[0]
        require_lineage(
            predecessor_source,
            credential_class=args.credential_class,
            owner_id=args.owner_id,
            project_id=args.project_id,
            purpose=policy["purpose"],
            generation=args.predecessor_generation,
            statuses={"source-observed"},
        )
        if len(args.successor_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in args.successor_fingerprint
        ):
            raise RotationError("successor fingerprint must be lowercase SHA-256")
        if args.successor_fingerprint == predecessor_source["fingerprint"]:
            raise RotationError(
                "successor fingerprint must differ from the retained predecessor"
            )
        successor_sources = [
            item
            for item in source_items
            if item["credential_class"] == args.credential_class
            and item["owner_id"] == args.owner_id
            and item["project_id"] == args.project_id
            and item["purpose"] == policy["purpose"]
            and item["generation"] == args.predecessor_generation + 1
            and item["fingerprint"] == args.successor_fingerprint
            and item["status"] == "source-observed"
        ]
        if len(successor_sources) != 1:
            raise RotationError(
                "authoritative source custody did not return one exact staged successor"
            )
        journal = {
            "schema": "fs2-serve.nebius.ai/credential-rotation/v3",
            "operation_id": str(uuid.uuid4()),
            "credential_class": args.credential_class,
            "owner_id": args.owner_id,
            "project_id": args.project_id,
            "purpose": policy["purpose"],
            "readers": policy["readers"],
            "expiry_policy": policy["expiry"],
            "registry_sha256": canonical_sha256(registry),
            "consumer_contracts_sha256": canonical_sha256(
                {
                    "schema": "fs2-serve.nebius.ai/credential-consumer-contracts/v1",
                    "contracts": contracts,
                }
            ),
            "consumer_contract": contracts[args.credential_class],
            "provider_command": provider_identity,
            "predecessor_id": predecessor_source["id"],
            "predecessor_generation": predecessor_source["generation"],
            "predecessor_fingerprint": predecessor_source["fingerprint"],
            "predecessor": None,
            "successor_generation": predecessor_source["generation"] + 1,
            "successor_fingerprint": args.successor_fingerprint,
            "successor_id": successor_sources[0]["id"],
            "successor": None,
            "phase": "adoption-pending",
            "created_at": utc_timestamp(),
            "events": [],
        }
        readiness = class_operation_observation(
            journal,
            args.provider_command,
            "rotation-readiness",
            predecessor_id=journal["predecessor_id"],
            successor_id=journal["successor_id"],
        )
        predecessor, created = require_rotation_readiness(journal, readiness, policy)
        journal["predecessor"] = predecessor
        journal["rotation_readiness_evidence_sha256"] = canonical_sha256(readiness)
        append_journal_state(journal_path, journal)
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
        observed_successor = reconcile_created(journal, args.provider_command)
        if observed_successor != successor:
            raise RotationError(
                "rotation lineage is no longer active in provider-native readiness"
            )
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
            response = consumer_readiness_observation(
                journal, args.provider_command, phase="dual-read-ready"
            )
            require_consumer_readiness(
                journal,
                successor,
                response,
                expected_write_id=journal["predecessor"]["id"],
            )
            journal["dual_read_evidence_sha256"] = canonical_sha256(response)
            journal["dual_read_semantic_evidence"] = required_semantic_observations(
                journal, args.provider_command
            )
            record_phase(journal_path, journal, "dual-read", "prove-dual-read")
        elif args.command == "prove-current-write":
            if journal["phase"] != "dual-read":
                raise RotationError("write cutover requires dual-read proof")
            record_phase(
                journal_path, journal, "switch-write-pending", "switch-write-intent"
            )
            response = consumer_readiness_observation(
                journal, args.provider_command, phase="current-write-ready"
            )
            require_consumer_readiness(
                journal, successor, response, expected_write_id=successor["id"]
            )
            journal["current_write_evidence_sha256"] = canonical_sha256(response)
            journal["current_write_semantic_evidence"] = required_semantic_observations(
                journal, args.provider_command
            )
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
        enabled_classes = response.get("enabled_classes")
        feature_gated_classes = response.get("feature_gated_classes")
        absent_feature_classes = response.get("absent_feature_classes")
        absent_optional_classes = response.get("absent_optional_classes")
        required_classes = response.get("required_classes")
        presence = registry.get("credential_presence")
        if (
            not isinstance(raw_items, list)
            or not isinstance(enabled_classes, list)
            or not isinstance(feature_gated_classes, list)
            or not isinstance(absent_feature_classes, list)
            or not isinstance(absent_optional_classes, list)
            or not isinstance(required_classes, list)
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
            or not isinstance(presence, dict)
            or set(required_classes) != set(presence.get("required", []))
            or set(feature_gated_classes)
            != set(presence.get("feature_gated", {}))
            or set(enabled_classes)
            | set(absent_feature_classes)
            | set(absent_optional_classes)
            != {item["id"] for item in registry["credentials"]}
            or set(enabled_classes) & set(absent_feature_classes)
            or set(enabled_classes) & set(absent_optional_classes)
            or set(absent_feature_classes) & set(absent_optional_classes)
            or not set(absent_feature_classes)
            <= set(presence.get("feature_gated", {}))
            or not set(absent_optional_classes)
            <= set(presence.get("optional", {}))
        ):
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
            if credential_class in (
                set(absent_feature_classes) | set(absent_optional_classes)
            ):
                if matches:
                    raise RotationError(
                        f"disabled credential class has observed identities: {credential_class}"
                    )
                classes[credential_class] = []
                continue
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
                or any(item["status"] != "source-observed" for item in matches)
            ):
                raise RotationError(
                    f"provider inventory has invalid lineage for {credential_class}"
                )
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
            "enabled_classes": sorted(enabled_classes),
            "feature_gated_classes": sorted(feature_gated_classes),
            "absent_feature_classes": sorted(absent_feature_classes),
            "absent_optional_classes": sorted(absent_optional_classes),
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
    create_parser.add_argument("--owner-id", required=True)
    create_parser.add_argument("--project-id", required=True)
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
