#!/usr/bin/env python3
"""Verify externally signed, append-only storage-reconciler cutover state."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from daemonset_fence_runtime import _object, _safe_read
from storage_reconciler_cutover_policy import canonical, digest

REGISTRY_PATH = Path(
    "/var/lib/fs2-security-checkpoints-ro/storage-reconciler-cutover-runtime.json"
)
RECEIPT_AUTHORITY_REGISTRY_PATH = Path(
    "/var/lib/fs2-security-checkpoints-ro/storage-reconciler-receipt-authorities.json"
)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
UID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
PHASES = {
    "PREPARED",
    "DRAIN_PREDECESSOR",
    "PREDECESSOR_DRAINED",
    "QUIESCE_PREDECESSOR",
    "PREDECESSOR_QUIESCED",
    "ACTIVATE_SUCCESSOR",
    "COMPLETED",
    "ROLLBACK_QUIESCE",
    "ROLLBACK_SUCCESSOR_QUIESCED",
    "ROLLBACK_ACTIVATE",
    "ROLLED_BACK",
}
PHASE_SUCCESSORS = {
    "PREPARED": {"DRAIN_PREDECESSOR"},
    # A drain which cannot obtain exact zero-provider/zero-action evidence is
    # reopened only by a new signed epoch for the same retained predecessor.
    # The old drain row and failed attempt remain immutable evidence.
    "DRAIN_PREDECESSOR": {"PREDECESSOR_DRAINED", "PREPARED"},
    "PREDECESSOR_DRAINED": {"QUIESCE_PREDECESSOR"},
    "QUIESCE_PREDECESSOR": {"PREDECESSOR_QUIESCED"},
    "PREDECESSOR_QUIESCED": {"ACTIVATE_SUCCESSOR"},
    "ACTIVATE_SUCCESSOR": {"COMPLETED"},
    "COMPLETED": {"PREPARED", "ROLLBACK_QUIESCE"},
    "ROLLBACK_QUIESCE": {"ROLLBACK_SUCCESSOR_QUIESCED"},
    "ROLLBACK_SUCCESSOR_QUIESCED": {"ROLLBACK_ACTIVATE"},
    "ROLLBACK_ACTIVATE": {"ROLLED_BACK"},
    "ROLLED_BACK": {"PREPARED"},
}
RECEIPT_CONTRACTS = {
    "provider-drain": (
        "fs2-serve.nebius.ai/storage-reconciler-provider-drain-attestation/v1",
        "fs2-serve.nebius.ai/storage-reconciler-provider-drain-raw-observation/v1",
    ),
    "rollback-zero-inflight": (
        "fs2-serve.nebius.ai/storage-reconciler-zero-inflight-observation/v1",
        "fs2-serve.nebius.ai/storage-reconciler-zero-inflight-raw-observation/v1",
    ),
    "rollback-schema-compatibility": (
        "fs2-serve.nebius.ai/storage-reconciler-schema-compatibility/v1",
        "fs2-serve.nebius.ai/storage-reconciler-schema-compatibility-raw-observation/v1",
    ),
    "rollback-provider-continuity": (
        "fs2-serve.nebius.ai/storage-reconciler-provider-continuity/v1",
        "fs2-serve.nebius.ai/storage-reconciler-provider-continuity-raw-observation/v1",
    ),
}


def _identity(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"username", "uid", "groups"}
        and isinstance(value.get("username"), str)
        and value["username"]
        and isinstance(value.get("uid"), str)
        and value["uid"]
        and isinstance(value.get("groups"), list)
        and value["groups"] == sorted(set(value["groups"]))
    )


def _bounded_window(
    observed: object, valid_until: object, *, maximum_seconds: int = 900
) -> tuple[datetime, datetime] | None:
    try:
        start = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
    except ValueError:
        return None
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or end <= start
        or (end - start).total_seconds() > maximum_seconds
    ):
        return None
    return start.astimezone(UTC), end.astimezone(UTC)


def _currently_valid(window: tuple[datetime, datetime] | None) -> bool:
    if window is None:
        return False
    now = datetime.now(UTC)
    return window[0] <= now < window[1]


def _epoch_identity(value: object, *, activation_epoch: int) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "username",
        "uid",
        "groups",
        "credential_id",
        "valid_from",
        "valid_until",
    }:
        return False
    if not _identity({key: value[key] for key in ("username", "uid", "groups")}):
        return False
    if not re.fullmatch(r"[a-f0-9]{64}", str(value.get("credential_id", ""))):
        return False
    if f":epoch-{activation_epoch}:" not in value["username"]:
        return False
    # Historical epoch credentials must remain verifiable after expiry, but
    # only the latest epoch may authenticate a mutation (checked after the
    # complete chain is loaded). This preserves the append-only audit chain
    # without granting retained RoleBindings a reusable principal.
    return _bounded_window(value["valid_from"], value["valid_until"]) is not None


def _receipt_authority_registry(path: Path) -> tuple[dict[str, Any], str]:
    registry = _object(_safe_read(path), "storage reconciler receipt authority registry")
    if (
        set(registry) != {"schema", "registry_id", "authorities"}
        or registry.get("schema")
        != "fs2-serve.nebius.ai/storage-reconciler-receipt-authority-registry/v1"
        or not isinstance(registry.get("authorities"), dict)
        or set(registry["authorities"]) != set(RECEIPT_CONTRACTS)
        or registry.get("registry_id")
        != digest({key: item for key, item in registry.items() if key != "registry_id"})
    ):
        raise ValueError("storage receipt authority registry fields differ")
    authority_ids: set[str] = set()
    public_key_digests: set[str] = set()
    for purpose, authority in registry["authorities"].items():
        receipt_schema, raw_schema = RECEIPT_CONTRACTS[purpose]
        if (
            not isinstance(authority, dict)
            or set(authority)
            != {
                "authority_id",
                "purpose",
                "public_key_pem",
                "public_key_sha256",
                "receipt_schema",
                "raw_observation_schema",
                "observation_adapter_sha256",
            }
            or authority.get("purpose") != purpose
            or authority.get("receipt_schema") != receipt_schema
            or authority.get("raw_observation_schema") != raw_schema
            or not re.fullmatch(
                r"fs2-storage-receipt-[a-z0-9-]+:[a-f0-9]{12}",
                str(authority.get("authority_id", "")),
            )
            or not str(authority.get("public_key_pem", "")).startswith(
                "-----BEGIN PUBLIC KEY-----"
            )
            or authority.get("public_key_sha256")
            != hashlib.sha256(str(authority.get("public_key_pem", "")).encode()).hexdigest()
            or not SHA256_RE.fullmatch(
                str(authority.get("observation_adapter_sha256", ""))
            )
        ):
            raise ValueError("storage receipt authority is not purpose-bound")
        key = serialization.load_pem_public_key(authority["public_key_pem"].encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("storage receipt authority key is not Ed25519")
        authority_ids.add(authority["authority_id"])
        public_key_digests.add(authority["public_key_sha256"])
    if len(authority_ids) != len(RECEIPT_CONTRACTS) or len(public_key_digests) != len(
        RECEIPT_CONTRACTS
    ):
        raise ValueError("storage receipt purposes do not have independent authorities")
    return registry, digest(registry)


def _independently_signed_receipt(
    value: object,
    *,
    purpose: str,
    authorities: dict[str, Any],
) -> dict[str, Any] | None:
    authority = authorities.get(purpose)
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema", "purpose", "authority_id", "body", "payload_sha256", "signature"}
        or value.get("schema")
        != "fs2-serve.nebius.ai/storage-reconciler-signed-receipt/v1"
        or not isinstance(authority, dict)
        or value.get("purpose") != purpose
        or value.get("authority_id") != authority.get("authority_id")
        or not isinstance(value.get("body"), dict)
        or value.get("payload_sha256") != digest(value["body"])
    ):
        return None
    signed_payload = {key: item for key, item in value.items() if key != "signature"}
    try:
        key = serialization.load_pem_public_key(authority["public_key_pem"].encode())
        if not isinstance(key, Ed25519PublicKey):
            return None
        key.verify(
            base64.b64decode(str(value["signature"]), validate=True),
            canonical(signed_payload),
        )
    except (InvalidSignature, TypeError, ValueError):
        return None
    body = value["body"]
    raw_observation = body.get("raw_observation")
    if (
        body.get("schema") != authority.get("receipt_schema")
        or body.get("purpose") != purpose
        or body.get("authority_id") != authority.get("authority_id")
        or body.get("observation_adapter_sha256")
        != authority.get("observation_adapter_sha256")
        or not isinstance(raw_observation, dict)
        or raw_observation.get("schema") != authority.get("raw_observation_schema")
        or body.get("raw_observation_sha256") != digest(raw_observation)
    ):
        return None
    return body


def _content_receipt(
    value: object,
    *,
    purpose: str,
    schema: str,
    cluster_id: str,
    subject_generations: set[str],
    forbidden_issuer: dict[str, Any],
    authorities: dict[str, Any],
) -> dict[str, Any] | None:
    body = _independently_signed_receipt(
        value,
        purpose=purpose,
        authorities=authorities,
    )
    if not isinstance(body, dict) or set(body) != {
        "schema",
        "receipt_id",
        "purpose",
        "authority_id",
        "issuer",
        "observed_at",
        "valid_until",
        "cluster_id",
        "subject_generations",
        "object_identities",
        "observation_source",
        "observation_adapter_sha256",
        "raw_observation",
        "raw_observation_sha256",
        "outcome",
        "detail",
    }:
        return None
    receipt_id_body = {key: item for key, item in body.items() if key != "receipt_id"}
    issuer = body.get("issuer")
    raw_observation = body.get("raw_observation")
    if (
        body.get("schema") != schema
        or body.get("receipt_id") != digest(receipt_id_body)
        or not _identity(issuer)
        or issuer == {key: forbidden_issuer.get(key) for key in ("username", "uid", "groups")}
        or body.get("cluster_id") != cluster_id
        or set(body.get("subject_generations") or []) != subject_generations
        or body.get("subject_generations") != sorted(subject_generations)
        or not isinstance(body.get("object_identities"), list)
        or not body["object_identities"]
        or any(
            not isinstance(item, dict)
            or set(item) != {"kind", "id", "resource_version", "sha256"}
            or not item["kind"]
            or not item["id"]
            or not item["resource_version"]
            or not SHA256_RE.fullmatch(str(item["sha256"]))
            for item in body["object_identities"]
        )
        or body["object_identities"]
        != sorted(body["object_identities"], key=lambda item: (item["kind"], item["id"]))
        or len({(item["kind"], item["id"]) for item in body["object_identities"]})
        != len(body["object_identities"])
        or body.get("outcome") != "PASS"
        or not isinstance(body.get("detail"), dict)
        or raw_observation
        != {
            "schema": RECEIPT_CONTRACTS[purpose][1],
            "cluster_id": cluster_id,
            "observed_at": body.get("observed_at"),
            "subject_generations": sorted(subject_generations),
            "object_identities": body.get("object_identities"),
            "facts": body.get("detail"),
        }
    ):
        return None
    if _bounded_window(body["observed_at"], body["valid_until"]) is None:
        return None
    return body


def _provider_drain_intent(value: object, transition: dict[str, Any]) -> bool:
    window = (
        _bounded_window(
            value.get("requested_at") if isinstance(value, dict) else None,
            value.get("deadline_at") if isinstance(value, dict) else None,
            maximum_seconds=600,
        )
        if isinstance(value, dict)
        else None
    )
    return bool(
        isinstance(value, dict)
        and set(value)
        == {
            "schema",
            "drain_id",
            "reconciler_generation",
            "requested_at",
            "deadline_at",
            "max_provider_action_seconds",
            "termination_grace_seconds",
        }
        and value.get("schema")
        == "fs2-serve.nebius.ai/storage-reconciler-provider-drain-intent/v1"
        and UID_RE.fullmatch(str(value.get("drain_id", "")))
        and value.get("reconciler_generation") == transition["predecessor_generation"]
        and value.get("max_provider_action_seconds") == 120
        and isinstance(value.get("termination_grace_seconds"), int)
        and value["termination_grace_seconds"] > value["max_provider_action_seconds"]
        and value["termination_grace_seconds"] <= 600
        and window is not None
        and (window[1] - window[0]).total_seconds()
        == value["termination_grace_seconds"]
    )


def _database_drain_receipt(value: object, intent: dict[str, Any]) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "drain_id",
        "reconciler_generation",
        "activation_epoch",
        "activation_state_head_sha256",
        "transition_id",
        "operation_cutoff_at",
        "nonterminal_provider_operations",
        "queued_user_actions",
        "queued_audit_actions",
        "operations",
        "postgres_jsonb_receipt_sha256",
    }:
        return False
    operations = value.get("operations")
    if (
        value.get("schema")
        != "fs2-serve.nebius.ai/storage-reconciler-provider-drain-receipt/v1"
        or value.get("drain_id") != intent.get("drain_id")
        or value.get("reconciler_generation") != intent.get("reconciler_generation")
        or not isinstance(value.get("activation_epoch"), int)
        or value["activation_epoch"] < 1
        or not isinstance(value.get("operation_cutoff_at"), str)
        or not value["operation_cutoff_at"]
        or value.get("nonterminal_provider_operations") != 0
        or not isinstance(value.get("queued_user_actions"), int)
        or value["queued_user_actions"] < 0
        or not isinstance(value.get("queued_audit_actions"), int)
        or value["queued_audit_actions"] < 0
        or not SHA256_RE.fullmatch(
            str(value.get("activation_state_head_sha256", ""))
        )
        or not SHA256_RE.fullmatch(str(value.get("transition_id", "")))
        or not SHA256_RE.fullmatch(
            str(value.get("postgres_jsonb_receipt_sha256", ""))
        )
        or not isinstance(operations, list)
    ):
        return False
    operation_ids: list[str] = []
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) != {
            "operation_id",
            "provider_idempotency_id",
            "operation_kind",
            "target_identity_sha256",
            "status",
            "superseded_by",
            "provider_operations",
        }:
            return False
        attempts = operation.get("provider_operations")
        if (
            not UID_RE.fullmatch(str(operation.get("operation_id", "")))
            or not UID_RE.fullmatch(str(operation.get("provider_idempotency_id", "")))
            or not isinstance(operation.get("operation_kind"), str)
            or not operation["operation_kind"]
            or not SHA256_RE.fullmatch(
                str(operation.get("target_identity_sha256", ""))
            )
            or operation.get("status")
            not in {"succeeded", "failed_terminal", "superseded"}
            or (
                operation.get("status") == "superseded"
                and not UID_RE.fullmatch(str(operation.get("superseded_by", "")))
            )
            or (
                operation.get("status") != "superseded"
                and operation.get("superseded_by") is not None
            )
            or not isinstance(attempts, list)
            or any(
                not isinstance(attempt, dict)
                or set(attempt)
                != {"provider_operation_id", "status", "provider_code"}
                or not attempt.get("provider_operation_id")
                or attempt.get("status")
                not in {"succeeded", "failed_terminal", "superseded_indeterminate"}
                for attempt in attempts
            )
        ):
            return False
        operation_ids.append(str(operation["operation_id"]))
    return operation_ids == sorted(set(operation_ids))


def _provider_drain_receipt(
    value: object,
    intent: dict[str, Any],
    *,
    cluster_id: str,
    forbidden_issuer: dict[str, Any],
    authorities: dict[str, Any],
) -> dict[str, Any] | None:
    body = _independently_signed_receipt(
        value,
        purpose="provider-drain",
        authorities=authorities,
    )
    window = (
        _bounded_window(
            body.get("observed_at") if isinstance(body, dict) else None,
            body.get("valid_until") if isinstance(body, dict) else None,
        )
        if isinstance(body, dict)
        else None
    )
    operation_ids = body.get("provider_operation_ids") if isinstance(body, dict) else None
    valid = bool(
        isinstance(body, dict)
        and set(body)
        == {
            "schema",
            "receipt_id",
            "purpose",
            "authority_id",
            "issuer",
            "observed_at",
            "valid_until",
            "cluster_id",
            "drain_id",
            "reconciler_generation",
            "activation_epoch",
            "activation_state_head_sha256",
            "transition_id",
            "database_receipt",
            "database_receipt_sha256",
            "provider_operation_ledger_head_sha256",
            "provider_operation_ids",
            "nonterminal_provider_operations",
            "observation_adapter_sha256",
            "raw_observation",
            "raw_observation_sha256",
        }
        and body.get("schema")
        == "fs2-serve.nebius.ai/storage-reconciler-provider-drain-attestation/v1"
        and body.get("receipt_id")
        == digest({key: item for key, item in body.items() if key != "receipt_id"})
        and _identity(body.get("issuer"))
        and body.get("issuer")
        != {key: forbidden_issuer.get(key) for key in ("username", "uid", "groups")}
        and body.get("cluster_id") == cluster_id
        and body.get("drain_id") == intent.get("drain_id")
        and body.get("reconciler_generation") == intent.get("reconciler_generation")
        and isinstance(body.get("activation_epoch"), int)
        and body["activation_epoch"] > 0
        and SHA256_RE.fullmatch(
            str(body.get("activation_state_head_sha256", ""))
        )
        and SHA256_RE.fullmatch(str(body.get("transition_id", "")))
        and _database_drain_receipt(body.get("database_receipt"), intent)
        and body.get("database_receipt_sha256")
        == digest(body["database_receipt"])
        and body["database_receipt"].get("activation_epoch")
        == body.get("activation_epoch")
        and body["database_receipt"].get("activation_state_head_sha256")
        == body.get("activation_state_head_sha256")
        and body["database_receipt"].get("transition_id")
        == body.get("transition_id")
        and body.get("provider_operation_ledger_head_sha256")
        == digest(body["database_receipt"]["operations"])
        and SHA256_RE.fullmatch(
            str(body.get("provider_operation_ledger_head_sha256", ""))
        )
        and isinstance(body.get("provider_operation_ids"), list)
        and operation_ids == sorted(set(operation_ids))
        and all(UID_RE.fullmatch(str(item)) for item in operation_ids)
        and operation_ids
        == sorted(
            str(item["operation_id"])
            for item in body["database_receipt"]["operations"]
        )
        and body.get("nonterminal_provider_operations") == 0
        and body.get("raw_observation")
        == {
            "schema": RECEIPT_CONTRACTS["provider-drain"][1],
            "cluster_id": cluster_id,
            "drain_id": body.get("drain_id"),
            "reconciler_generation": body.get("reconciler_generation"),
            "activation_epoch": body.get("activation_epoch"),
            "activation_state_head_sha256": body.get(
                "activation_state_head_sha256"
            ),
            "transition_id": body.get("transition_id"),
            "observed_at": body.get("observed_at"),
            "database_receipt": body.get("database_receipt"),
            "provider_operation_ids": body.get("provider_operation_ids"),
        }
        and window is not None
    )
    return body if valid else None


def _verify_envelope(envelope: object, key: Ed25519PublicKey) -> dict[str, Any]:
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"body", "payload_sha256", "signature"}
        or not isinstance(envelope.get("body"), dict)
        or envelope.get("payload_sha256") != digest(envelope["body"])
    ):
        raise ValueError("storage cutover envelope fields differ")
    try:
        key.verify(
            base64.b64decode(str(envelope["signature"]), validate=True),
            canonical(envelope["body"]),
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise ValueError("storage cutover signature is invalid") from exc
    return envelope["body"]


def _receipt(value: object, expected_phase: str) -> bool:
    expected_replicas = {
        "QUIESCE_PREDECESSOR": (0, 0),
        "ACTIVATE_SUCCESSOR": (0, 1),
        "ROLLBACK_QUIESCE": (0, 0),
        "ROLLBACK_ACTIVATE": (1, 0),
    }
    predecessor_replicas, successor_replicas = expected_replicas[expected_phase]
    predecessor = value.get("predecessor") if isinstance(value, dict) else None
    successor = value.get("successor") if isinstance(value, dict) else None
    return bool(
        isinstance(value, dict)
        and value.get("schema")
        == "fs2-serve.nebius.ai/storage-reconciler-cutover-observation/v1"
        and value.get("phase") == expected_phase
        and SHA256_RE.fullmatch(str(value.get("observation_sha256", "")))
        and value.get("observation_sha256")
        == digest({key: item for key, item in value.items() if key != "observation_sha256"})
        and isinstance(value.get("provider_drain_receipt"), dict)
        and value.get("provider_drain_receipt_sha256")
        == digest(value["provider_drain_receipt"])
        and value["provider_drain_receipt"].get("nonterminal_provider_operations") == 0
        and isinstance(predecessor, dict)
        and isinstance(successor, dict)
        and predecessor.get("replicas") == predecessor_replicas
        and successor.get("replicas") == successor_replicas
        and (
            predecessor_replicas != 0
            or all(
                predecessor.get(field) == 0
                for field in ("ready_replicas", "available_replicas", "unavailable_replicas")
            )
        )
        and (
            successor_replicas != 0
            or all(
                successor.get(field) == 0
                for field in ("ready_replicas", "available_replicas", "unavailable_replicas")
            )
        )
        and (
            successor_replicas != 1
            or (
                successor.get("ready_replicas") == 1
                and successor.get("available_replicas") == 1
                and successor.get("unavailable_replicas") == 0
            )
        )
        and (
            predecessor_replicas != 1
            or (
                predecessor.get("ready_replicas") == 1
                and predecessor.get("available_replicas") == 1
                and predecessor.get("unavailable_replicas") == 0
            )
        )
    )


def _transition(
    value: object,
    deployments: dict[str, Any],
    previous: dict[str, Any] | None,
    previous_state_head: str,
    *,
    activation_epoch: int,
    cluster_id: str,
    security_owner_identity: dict[str, Any],
    receipt_authorities: dict[str, Any],
    advance_phase: bool,
) -> dict[str, Any]:
    fields = {
        "schema",
        "transition_id",
        "phase",
        "predecessor_generation",
        "successor_generation",
        "predecessor_transition_sha256",
        "provider_drain_intent",
        "provider_drain_intent_sha256",
        "provider_drain_receipt",
        "provider_drain_receipt_sha256",
        "predecessor_quiescence",
        "predecessor_quiescence_sha256",
        "successor_readiness",
        "successor_readiness_sha256",
        "rollback",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema")
        != "fs2-serve.nebius.ai/storage-reconciler-cutover-transition/v1"
        or value.get("phase") not in PHASES
        or value.get("predecessor_generation") not in deployments
        or value.get("successor_generation") not in deployments
        or value.get("predecessor_generation") == value.get("successor_generation")
    ):
        raise ValueError("storage cutover transition fields differ")
    body = {key: item for key, item in value.items() if key != "transition_id"}
    if value.get("transition_id") != digest(body):
        raise ValueError("storage cutover transition is not content-bound")
    phase = value["phase"]
    prior_id = value.get("predecessor_transition_sha256")
    if previous is None:
        if not advance_phase or phase != "PREPARED" or prior_id is not None:
            raise ValueError("first storage cutover transition is not PREPARED")
    elif not advance_phase:
        if value != previous:
            raise ValueError("AUTH_REFRESH changed storage cutover semantics")
    else:
        if (
            phase not in PHASE_SUCCESSORS[previous["phase"]]
            or prior_id != previous["transition_id"]
        ):
            raise ValueError("storage cutover phase does not extend its predecessor")
        if phase != "PREPARED" and (
            value["predecessor_generation"] != previous["predecessor_generation"]
            or value["successor_generation"] != previous["successor_generation"]
        ):
            raise ValueError("storage cutover changed generations mid-transition")
        if phase == "PREPARED" and value["predecessor_generation"] not in {
            previous["predecessor_generation"],
            previous["successor_generation"],
        }:
            raise ValueError("next storage cutover does not start from a retained generation")
    drain_intent = value.get("provider_drain_intent")
    drain_phases = PHASES - {"PREPARED"}
    if phase in drain_phases:
        if (
            not _provider_drain_intent(drain_intent, value)
            or value.get("provider_drain_intent_sha256") != digest(drain_intent)
        ):
            raise ValueError("storage cutover provider-drain intent differs")
        if phase != "DRAIN_PREDECESSOR" and previous is not None and (
            drain_intent != previous.get("provider_drain_intent")
        ):
            raise ValueError("storage cutover changed its provider-drain intent")
    elif drain_intent is not None or value.get("provider_drain_intent_sha256") is not None:
        raise ValueError("storage cutover carries a premature provider-drain intent")
    drain_receipt = value.get("provider_drain_receipt")
    drained_phases = PHASES - {"PREPARED", "DRAIN_PREDECESSOR"}
    if phase in drained_phases:
        drain_receipt_body = _provider_drain_receipt(
            drain_receipt,
            drain_intent,
            cluster_id=cluster_id,
            forbidden_issuer=security_owner_identity,
            authorities=receipt_authorities,
        )
        if (
            drain_receipt_body is None
            or value.get("provider_drain_receipt_sha256") != digest(drain_receipt)
        ):
            raise ValueError("storage cutover lacks an exact zero-terminal provider receipt")
        if phase == "PREDECESSOR_DRAINED" and advance_phase:
            if (
                previous is None
                or drain_receipt_body.get("transition_id") != previous["transition_id"]
                or drain_receipt_body.get("activation_state_head_sha256")
                != previous_state_head
                or drain_receipt_body.get("activation_epoch") != activation_epoch - 1
            ):
                raise ValueError("provider drain receipt is not bound to the draining epoch")
        elif advance_phase and previous is not None and drain_receipt != previous.get(
            "provider_drain_receipt"
        ):
            raise ValueError("storage cutover changed provider-drain evidence")
    elif drain_receipt is not None or value.get("provider_drain_receipt_sha256") is not None:
        raise ValueError("storage cutover carries a premature provider-drain receipt")
    quiescence = value.get("predecessor_quiescence")
    if phase in {
        "PREDECESSOR_QUIESCED",
        "ACTIVATE_SUCCESSOR",
        "COMPLETED",
        "ROLLBACK_QUIESCE",
        "ROLLBACK_SUCCESSOR_QUIESCED",
        "ROLLBACK_ACTIVATE",
        "ROLLED_BACK",
    }:
        if (
            not _receipt(quiescence, "QUIESCE_PREDECESSOR")
            or value.get("predecessor_quiescence_sha256") != digest(quiescence)
        ):
            raise ValueError("storage predecessor quiescence receipt differs")
        if phase == "PREDECESSOR_QUIESCED" and advance_phase:
            if (
                previous is None
                or quiescence.get("transition_id") != previous["transition_id"]
                or quiescence.get("state_head_sha256") != previous_state_head
            ):
                raise ValueError("storage predecessor quiescence is not bound to the executed epoch")
        elif advance_phase and previous is not None and quiescence != previous.get(
            "predecessor_quiescence"
        ):
            raise ValueError("storage predecessor quiescence changed after admission")
    elif quiescence is not None or value.get("predecessor_quiescence_sha256") is not None:
        raise ValueError("storage cutover carries premature predecessor quiescence")
    readiness = value.get("successor_readiness")
    if phase in {
        "COMPLETED",
        "ROLLBACK_QUIESCE",
        "ROLLBACK_SUCCESSOR_QUIESCED",
        "ROLLBACK_ACTIVATE",
        "ROLLED_BACK",
    }:
        if (
            not _receipt(readiness, "ACTIVATE_SUCCESSOR")
            or value.get("successor_readiness_sha256") != digest(readiness)
        ):
            raise ValueError("storage successor readiness receipt differs")
        if phase == "COMPLETED" and advance_phase:
            if (
                previous is None
                or readiness.get("transition_id") != previous["transition_id"]
                or readiness.get("state_head_sha256") != previous_state_head
            ):
                raise ValueError("storage successor readiness is not bound to the activation epoch")
        elif advance_phase and previous is not None and readiness != previous.get(
            "successor_readiness"
        ):
            raise ValueError("storage successor readiness changed after completion")
    elif readiness is not None or value.get("successor_readiness_sha256") is not None:
        raise ValueError("storage cutover carries premature successor readiness")
    rollback = value.get("rollback")
    if phase.startswith("ROLLBACK_") or phase == "ROLLED_BACK":
        if (
            not isinstance(rollback, dict)
            or set(rollback)
            != {
                "from_epoch",
                "successor_quiesced",
                "successor_quiescence",
                "successor_quiescence_sha256",
                "zero_inflight_actions_receipt",
                "zero_inflight_actions_receipt_sha256",
                "schema_compatibility_receipt",
                "schema_compatibility_receipt_sha256",
                "provider_continuity_receipt",
                "provider_continuity_receipt_sha256",
            }
            or not isinstance(rollback.get("from_epoch"), int)
            or rollback["from_epoch"] < 1
        ):
            raise ValueError("storage rollback criteria are incomplete")
        if (
            phase == "ROLLBACK_QUIESCE"
            and advance_phase
            and rollback.get("from_epoch") != activation_epoch - 1
        ):
            raise ValueError("storage rollback is not bound to its originating epoch")
        subject_generations = {
            value["predecessor_generation"],
            value["successor_generation"],
        }
        receipt_contracts = (
            (
                "zero_inflight_actions_receipt",
                "rollback-zero-inflight",
                "fs2-serve.nebius.ai/storage-reconciler-zero-inflight-observation/v1",
                "zero_inflight_actions_receipt_sha256",
            ),
            (
                "schema_compatibility_receipt",
                "rollback-schema-compatibility",
                "fs2-serve.nebius.ai/storage-reconciler-schema-compatibility/v1",
                "schema_compatibility_receipt_sha256",
            ),
            (
                "provider_continuity_receipt",
                "rollback-provider-continuity",
                "fs2-serve.nebius.ai/storage-reconciler-provider-continuity/v1",
                "provider_continuity_receipt_sha256",
            ),
        )
        verified_rollback_receipts = {
            receipt_field: _content_receipt(
                rollback.get(receipt_field),
                purpose=purpose,
                schema=schema,
                cluster_id=cluster_id,
                subject_generations=subject_generations,
                forbidden_issuer=security_owner_identity,
                authorities=receipt_authorities,
            )
            for receipt_field, purpose, schema, _ in receipt_contracts
        }
        if any(
            verified_rollback_receipts[receipt_field] is None
            or rollback.get(hash_field) != digest(rollback[receipt_field])
            for receipt_field, _, _, hash_field in receipt_contracts
        ):
            raise ValueError("storage rollback receipt content is absent or unauthoritative")
        if (
            verified_rollback_receipts["zero_inflight_actions_receipt"].get(
                "detail", {}
            ).get(
                "nonterminal_provider_operations"
            )
            != 0
            or verified_rollback_receipts["schema_compatibility_receipt"].get(
                "detail", {}
            ).get(
                "compatible"
            )
            is not True
            or verified_rollback_receipts["provider_continuity_receipt"].get(
                "detail", {}
            ).get(
                "continuous"
            )
            is not True
        ):
            raise ValueError("storage rollback receipt outcome is not safe")
        if phase == "ROLLBACK_QUIESCE":
            if (
                rollback.get("successor_quiesced") is not False
                or rollback.get("successor_quiescence") is not None
                or rollback.get("successor_quiescence_sha256") is not None
            ):
                raise ValueError("rollback quiescence cannot be asserted before execution")
        else:
            successor_quiescence = rollback.get("successor_quiescence")
            if (
                rollback.get("successor_quiesced") is not True
                or not _receipt(successor_quiescence, "ROLLBACK_QUIESCE")
                or rollback.get("successor_quiescence_sha256")
                != digest(successor_quiescence)
            ):
                raise ValueError("rollback successor quiescence receipt differs")
            if phase == "ROLLBACK_SUCCESSOR_QUIESCED" and advance_phase:
                if (
                    previous is None
                    or successor_quiescence.get("transition_id")
                    != previous["transition_id"]
                    or successor_quiescence.get("state_head_sha256")
                    != previous_state_head
                ):
                    raise ValueError("rollback quiescence is not bound to the executed epoch")
            elif advance_phase and previous is not None and (
                previous.get("rollback") or {}
            ).get("successor_quiescence") != successor_quiescence:
                raise ValueError("rollback quiescence changed after admission")
    elif rollback is not None:
        raise ValueError("forward storage cutover carries rollback authority")
    return value


def _deployment(generation: str, deployment: object) -> dict[str, Any]:
    if (
        not re.fullmatch(r"r[0-9]{14}-[a-f0-9]{12}", generation)
        or not isinstance(deployment, dict)
        or set(deployment)
        != {
            "namespace",
            "name",
            "uid",
            "image_digest",
            "passive_spec",
            "passive_spec_sha256",
            "pod_template",
            "release_identity",
        }
        or not isinstance(deployment.get("namespace"), str)
        or not deployment["namespace"]
        or not isinstance(deployment.get("name"), str)
        or not deployment["name"]
        or not UID_RE.fullmatch(str(deployment.get("uid", "")))
        or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", str(deployment.get("image_digest", ""))
        )
        or (deployment.get("passive_spec") or {}).get("replicas") != 0
        or deployment.get("passive_spec_sha256")
        != digest(deployment.get("passive_spec"))
        or deployment.get("pod_template")
        != (deployment.get("passive_spec") or {}).get("template")
        or not _identity(deployment.get("release_identity"))
    ):
        raise ValueError("storage cutover Deployment contract differs")
    labels = ((deployment["pod_template"].get("metadata") or {}).get("labels") or {})
    containers = (deployment["pod_template"].get("spec") or {}).get("containers") or []
    if (
        labels.get("fs2.nebius.ai/storage-rollout-generation") != generation
        or len(containers) != 1
        or containers[0].get("image", "").split("@")[-1] != deployment["image_digest"]
    ):
        raise ValueError("storage cutover Deployment generation or image differs")
    return deployment


def load_verified_state(
    path: Path = REGISTRY_PATH,
    receipt_authority_path: Path = RECEIPT_AUTHORITY_REGISTRY_PATH,
) -> dict[str, Any]:
    receipt_authority_registry, receipt_authority_registry_sha256 = (
        _receipt_authority_registry(receipt_authority_path)
    )
    receipt_authorities = receipt_authority_registry["authorities"]
    registry = _object(_safe_read(path), "storage reconciler cutover registry")
    if (
        set(registry)
        != {
            "schema",
            "checkpoint_public_key_pem",
            "anchor_sha256",
            "generations",
            "head_sha256",
            "activation_envelope",
            "source_bundle_sha256",
            "enforcer_image_digest",
            "receipt_authority_registry_sha256",
        }
        or registry.get("schema")
        != "fs2-serve.nebius.ai/storage-reconciler-cutover-registry/v1"
        or not SHA256_RE.fullmatch(str(registry.get("anchor_sha256", "")))
        or not isinstance(registry.get("generations"), list)
        or not registry["generations"]
        or not SHA256_RE.fullmatch(str(registry.get("source_bundle_sha256", "")))
        or not re.fullmatch(
            r"[^@]+@sha256:[a-f0-9]{64}",
            str(registry.get("enforcer_image_digest", "")),
        )
        or registry.get("receipt_authority_registry_sha256")
        != receipt_authority_registry_sha256
    ):
        raise ValueError("storage cutover registry fields differ")
    public_key = serialization.load_pem_public_key(
        str(registry["checkpoint_public_key_pem"]).encode()
    )
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("storage cutover key is not Ed25519")
    checkpoint_key_sha256 = hashlib.sha256(
        str(registry["checkpoint_public_key_pem"]).encode()
    ).hexdigest()
    if checkpoint_key_sha256 in {
        authority["public_key_sha256"]
        for authority in receipt_authorities.values()
    }:
        raise ValueError("cutover signer cannot also issue independent observations")
    predecessor = registry["anchor_sha256"]
    previous_epoch = 0
    latest: dict[str, Any] | None = None
    retained_deployments: dict[str, Any] = {}
    previous_transition: dict[str, Any] | None = None
    immutable_state: dict[str, Any] | None = None
    used_security_owner_credentials: set[tuple[str, str, str]] = set()
    for envelope in registry["generations"]:
        body = _verify_envelope(envelope, public_key)
        fields = {
            "schema",
            "cluster_id",
            "activation_epoch",
            "epoch_kind",
            "predecessor_head_sha256",
            "authority_manifest_sha256",
            "receipt_authority_registry_sha256",
            "active_generation",
            "deployments",
            "security_owner_identity",
            "deployment_controller_identity",
            "replicaset_controller_identity",
            "transition",
            "valid_from",
            "valid_until",
        }
        if (
            set(body) != fields
            or body.get("schema")
            != "fs2-serve.nebius.ai/storage-reconciler-cutover-state/v1"
            or body.get("predecessor_head_sha256") != predecessor
            or body.get("activation_epoch") != previous_epoch + 1
            or body.get("epoch_kind") not in {"TRANSITION", "AUTH_REFRESH"}
            or (previous_epoch == 0 and body.get("epoch_kind") != "TRANSITION")
            or (
                body.get("epoch_kind") == "AUTH_REFRESH"
                and body.get("deployments") != retained_deployments
            )
            or body.get("receipt_authority_registry_sha256")
            != receipt_authority_registry_sha256
            or not isinstance(body.get("deployments"), dict)
            or not body["deployments"]
            or not set(retained_deployments).issubset(body["deployments"])
            or any(
                generation in retained_deployments
                and body["deployments"][generation] != retained_deployments[generation]
                for generation in retained_deployments
            )
        ):
            raise ValueError("storage cutover generation is forked or omits custody")
        deployments = {
            generation: _deployment(generation, deployment)
            for generation, deployment in sorted(body["deployments"].items())
        }
        owner_identity = body.get("security_owner_identity")
        owner_key = (
            str((owner_identity or {}).get("username", "")),
            str((owner_identity or {}).get("uid", "")),
            str((owner_identity or {}).get("credential_id", "")),
        )
        if (
            not SHA256_RE.fullmatch(str(body.get("authority_manifest_sha256", "")))
            or not _epoch_identity(owner_identity, activation_epoch=body["activation_epoch"])
            or _bounded_window(body.get("valid_from"), body.get("valid_until"))
            is None
            or body.get("valid_from") != (owner_identity or {}).get("valid_from")
            or body.get("valid_until") != (owner_identity or {}).get("valid_until")
            or owner_key in used_security_owner_credentials
            or not all(
                _identity(body.get(field))
                for field in (
                    "deployment_controller_identity",
                    "replicaset_controller_identity",
                )
            )
        ):
            raise ValueError("storage cutover authority identity differs")
        used_security_owner_credentials.add(owner_key)
        stable = {
            field: body[field]
            for field in (
                "cluster_id",
                "authority_manifest_sha256",
                "receipt_authority_registry_sha256",
                "deployment_controller_identity",
                "replicaset_controller_identity",
            )
        }
        if immutable_state is not None and stable != immutable_state:
            raise ValueError("storage cutover changed its cluster or authenticated identities")
        immutable_state = stable
        transition = _transition(
            body.get("transition"),
            deployments,
            previous_transition,
            predecessor,
            activation_epoch=body["activation_epoch"],
            cluster_id=str(body.get("cluster_id", "")),
            security_owner_identity=body.get("security_owner_identity") or {},
            receipt_authorities=receipt_authorities,
            advance_phase=body["epoch_kind"] == "TRANSITION",
        )
        phase = transition["phase"]
        expected_active = (
            transition["successor_generation"]
            if phase in {"ACTIVATE_SUCCESSOR", "COMPLETED"}
            else transition["predecessor_generation"]
            if phase
            in {"PREPARED", "DRAIN_PREDECESSOR", "ROLLBACK_ACTIVATE", "ROLLED_BACK"}
            else None
        )
        if body.get("active_generation") != expected_active:
            raise ValueError("storage cutover active generation differs from its phase")
        predecessor = envelope["payload_sha256"]
        previous_epoch = body["activation_epoch"]
        retained_deployments = deployments
        previous_transition = transition
        latest = body
    if registry.get("head_sha256") != predecessor or latest is None:
        raise ValueError("storage cutover head differs")
    try:
        valid_from = datetime.fromisoformat(
            str(latest["valid_from"]).replace("Z", "+00:00")
        )
        valid_until = datetime.fromisoformat(
            str(latest["valid_until"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("storage cutover validity is not RFC3339") from exc
    now = datetime.now(UTC)
    if (
        valid_from.tzinfo is None
        or valid_until.tzinfo is None
        or valid_from.astimezone(UTC) > now
        or valid_until.astimezone(UTC) <= now
        or _bounded_window(latest["valid_from"], latest["valid_until"]) is None
        or latest["valid_from"] != latest["security_owner_identity"]["valid_from"]
        or latest["valid_until"] != latest["security_owner_identity"]["valid_until"]
    ):
        raise ValueError("storage cutover state is stale or future-dated")
    if not _currently_valid(
        _bounded_window(
            latest["security_owner_identity"]["valid_from"],
            latest["security_owner_identity"]["valid_until"],
        )
    ):
        raise ValueError("latest storage cutover credential epoch is expired")
    latest_transition = latest["transition"]
    latest_drain_intent = latest_transition.get("provider_drain_intent")
    if latest_transition["phase"] == "DRAIN_PREDECESSOR" and not _currently_valid(
        _bounded_window(
            latest_drain_intent.get("requested_at")
            if isinstance(latest_drain_intent, dict)
            else None,
            latest_drain_intent.get("deadline_at")
            if isinstance(latest_drain_intent, dict)
            else None,
            maximum_seconds=600,
        )
    ):
        raise ValueError("current provider-drain intent is stale")
    latest_drain_envelope = latest_transition.get("provider_drain_receipt")
    latest_drain_receipt = (
        _independently_signed_receipt(
            latest_drain_envelope,
            purpose="provider-drain",
            authorities=receipt_authorities,
        )
        if latest_drain_envelope is not None
        else None
    )
    if latest_transition["phase"] in {
        "PREDECESSOR_DRAINED",
        "QUIESCE_PREDECESSOR",
        "PREDECESSOR_QUIESCED",
        "ACTIVATE_SUCCESSOR",
    } and not _currently_valid(
        _bounded_window(
            latest_drain_receipt.get("observed_at")
            if isinstance(latest_drain_receipt, dict)
            else None,
            latest_drain_receipt.get("valid_until")
            if isinstance(latest_drain_receipt, dict)
            else None,
        )
    ):
        raise ValueError("current provider-drain attestation is stale")
    latest_rollback = latest_transition.get("rollback")
    if isinstance(latest_rollback, dict) and latest_transition["phase"] in {
        "ROLLBACK_QUIESCE",
        "ROLLBACK_SUCCESSOR_QUIESCED",
        "ROLLBACK_ACTIVATE",
    }:
        for receipt_field, purpose in (
            ("zero_inflight_actions_receipt", "rollback-zero-inflight"),
            ("schema_compatibility_receipt", "rollback-schema-compatibility"),
            ("provider_continuity_receipt", "rollback-provider-continuity"),
        ):
            receipt = _independently_signed_receipt(
                latest_rollback.get(receipt_field),
                purpose=purpose,
                authorities=receipt_authorities,
            )
            if not isinstance(receipt, dict) or not _currently_valid(
                _bounded_window(receipt.get("observed_at"), receipt.get("valid_until"))
            ):
                raise ValueError("current storage rollback receipt is stale")
    activation = _verify_envelope(registry["activation_envelope"], public_key)
    activation_fields = {
        "schema",
        "cluster_id",
        "activation_epoch",
        "epoch_kind",
        "transition_phase",
        "predecessor_epoch",
        "target_generation",
        "target_image_digest",
        "authority_manifest_sha256",
        "cutover_receipt_sha256",
        "phase",
        "valid_from",
        "valid_until",
        "rollback",
        "predecessor_head_sha256",
        "ledger_anchor_sha256",
        "state_head_sha256",
        "source_bundle_sha256",
        "enforcer_image_digest",
        "receipt_authority_registry_sha256",
        "provider_drain_intent",
        "provider_drain_intent_sha256",
        "provider_drain_receipt",
        "provider_drain_receipt_sha256",
    }
    active_generation = latest["active_generation"]
    active_image = (
        latest["deployments"][active_generation]["image_digest"]
        if active_generation is not None
        else None
    )
    transition = latest["transition"]
    core_drain_intent = transition.get("provider_drain_intent")
    core_drain_receipt_envelope = transition.get("provider_drain_receipt")
    core_drain_receipt = (
        _independently_signed_receipt(
            core_drain_receipt_envelope,
            purpose="provider-drain",
            authorities=receipt_authorities,
        )
        if core_drain_receipt_envelope is not None
        else None
    )
    activation_drain_intent = (
        {
            **core_drain_intent,
            "activation_epoch": (
                core_drain_receipt["activation_epoch"]
                if isinstance(core_drain_receipt, dict)
                else latest["activation_epoch"]
            ),
            "activation_state_head_sha256": (
                core_drain_receipt["activation_state_head_sha256"]
                if isinstance(core_drain_receipt, dict)
                else registry["head_sha256"]
            ),
            "transition_id": (
                core_drain_receipt["transition_id"]
                if isinstance(core_drain_receipt, dict)
                else transition["transition_id"]
            ),
        }
        if core_drain_intent is not None
        else None
    )
    expected_activation_phase = (
        "DRAINING"
        if transition["phase"] == "DRAIN_PREDECESSOR"
        else "ACTIVE"
        if active_generation is not None
        else "QUIESCED"
    )
    if (
        set(activation) != activation_fields
        or activation.get("schema")
        != "fs2-serve.nebius.ai/storage-reconciler-activation/v1"
        or activation.get("activation_epoch") != latest["activation_epoch"]
        or activation.get("epoch_kind") != latest["epoch_kind"]
        or activation.get("transition_phase") != latest["transition"]["phase"]
        or activation.get("predecessor_epoch") != latest["activation_epoch"] - 1
        or activation.get("cluster_id") != latest["cluster_id"]
        or activation.get("target_generation") != latest["active_generation"]
        or activation.get("target_image_digest") != active_image
        or activation.get("valid_from") != latest["valid_from"]
        or activation.get("valid_until") != latest["valid_until"]
        or activation.get("phase") != expected_activation_phase
        or activation.get("authority_manifest_sha256")
        != latest["authority_manifest_sha256"]
        or activation.get("cutover_receipt_sha256") != latest["transition"]["transition_id"]
        or not SHA256_RE.fullmatch(str(activation.get("predecessor_head_sha256", "")))
        or activation.get("ledger_anchor_sha256") != registry["anchor_sha256"]
        or activation.get("state_head_sha256") != registry["head_sha256"]
        or activation.get("source_bundle_sha256")
        != registry["source_bundle_sha256"]
        or activation.get("enforcer_image_digest")
        != registry["enforcer_image_digest"]
        or activation.get("receipt_authority_registry_sha256")
        != receipt_authority_registry_sha256
        or activation.get("rollback") != latest["transition"].get("rollback")
        or activation.get("provider_drain_intent") != activation_drain_intent
        or activation.get("provider_drain_intent_sha256")
        != (digest(activation_drain_intent) if activation_drain_intent is not None else None)
        or activation.get("provider_drain_receipt")
        != core_drain_receipt_envelope
        or activation.get("provider_drain_receipt_sha256")
        != transition.get("provider_drain_receipt_sha256")
        or (
            activation.get("rollback") is not None
            and latest.get("epoch_kind") == "TRANSITION"
            and transition.get("phase") == "ROLLBACK_QUIESCE"
            and activation["rollback"].get("from_epoch")
            != activation.get("predecessor_epoch")
        )
    ):
        raise ValueError("runtime activation projection differs from cutover state")
    try:
        activation_from = datetime.fromisoformat(
            str(activation["valid_from"]).replace("Z", "+00:00")
        )
        activation_until = datetime.fromisoformat(
            str(activation["valid_until"]).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("runtime activation validity is not RFC3339") from exc
    if (
        activation_from.tzinfo is None
        or activation_until.tzinfo is None
        or activation_from.astimezone(UTC) > now
        or activation_until.astimezone(UTC) <= now
    ):
        raise ValueError("runtime activation projection is stale or future-dated")
    return {
        **latest,
        "verified": True,
        "head_sha256": predecessor,
        "registry_anchor_sha256": registry["anchor_sha256"],
        "activation_envelope": registry["activation_envelope"],
        "source_bundle_sha256": registry["source_bundle_sha256"],
        "enforcer_image_digest": registry["enforcer_image_digest"],
        "receipt_authority_registry_sha256": receipt_authority_registry_sha256,
    }
