#!/usr/bin/env python3
"""Independent verification for externally anchored credential evidence.

The credential authority is not a trust root.  A producer key signs the exact
request/payload claim and an independent append-only evidence service signs the
claim digest plus its immutable-log position.  Consumers verify both detached
Ed25519 signatures with pinned public keys; there is no local sign-and-verify
shortcut and no ``verify`` RPC on the producing service.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_public_key,
)

EVIDENCE_PUBLIC_KEY = Path(
    "/etc/fs2-credential-authority/evidence-producer-ed25519-public.pem"
)
ANCHOR_PUBLIC_KEY = Path(
    "/etc/fs2-credential-authority/external-anchor-ed25519-public.pem"
)
MAX_EVIDENCE_AGE = timedelta(minutes=5)
MIN_ANCHOR_RETENTION = timedelta(days=365)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class EvidenceVerificationError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def parse_time(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceVerificationError(f"{label} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceVerificationError(f"{label} must be RFC3339") from error
    if parsed.tzinfo is None:
        raise EvidenceVerificationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC).replace(microsecond=0)


def _load_public_key(path: Path, *, expected_sha256: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise EvidenceVerificationError("evidence public key must be a regular file")
    metadata = path.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise EvidenceVerificationError(
            "evidence public key must be root-owned and not group/world writable"
        )
    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != expected_sha256:
        raise EvidenceVerificationError("evidence public key digest is not pinned")
    key = load_pem_public_key(encoded)
    if key.__class__.__name__ != "Ed25519PublicKey":
        raise EvidenceVerificationError("evidence public key must be Ed25519")
    return key


def _public_key_id(key: Any) -> str:
    return hashlib.sha256(
        key.public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
    ).hexdigest()


def _signature(value: Any, *, label: str) -> bytes:
    if not isinstance(value, str):
        raise EvidenceVerificationError(f"{label} is absent")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise EvidenceVerificationError(f"{label} is malformed") from error
    if len(decoded) != 64:
        raise EvidenceVerificationError(f"{label} must be an Ed25519 signature")
    return decoded


def verify_evidence_envelope(
    envelope: Any,
    *,
    expected_operation: str,
    expected_request_sha256: str,
    expected_nonce: str,
    evidence_public_key_sha256: str,
    anchor_public_key_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify producer signature, external-log inclusion and freshness."""

    fields = {
        "schema",
        "claim",
        "producer_signature",
        "external_anchor",
        "payload",
    }
    if not isinstance(envelope, dict) or set(envelope) != fields:
        raise EvidenceVerificationError("credential evidence envelope is malformed")
    claim = envelope["claim"]
    payload = envelope["payload"]
    claim_fields = {
        "schema",
        "evidence_id",
        "operation",
        "request_sha256",
        "request_nonce",
        "payload_sha256",
        "policy_sha256",
        "producer_key_id",
        "sequence",
        "observed_at",
        "expires_at",
    }
    if (
        envelope["schema"]
        != "fs2-serve.nebius.ai/externally-anchored-credential-evidence/v1"
        or not isinstance(claim, dict)
        or set(claim) != claim_fields
        or claim.get("schema")
        != "fs2-serve.nebius.ai/credential-evidence-claim/v1"
        or claim.get("operation") != expected_operation
        or claim.get("request_sha256") != expected_request_sha256
        or claim.get("request_nonce") != expected_nonce
        or not isinstance(payload, dict)
        or claim.get("payload_sha256") != canonical_sha256(payload)
        or HEX64.fullmatch(str(claim.get("policy_sha256", ""))) is None
        or not isinstance(claim.get("producer_key_id"), str)
        or not claim["producer_key_id"]
        or not isinstance(claim.get("sequence"), int)
        or claim["sequence"] < 1
    ):
        raise EvidenceVerificationError("credential evidence claim binding is invalid")
    try:
        uuid.UUID(str(claim.get("evidence_id")))
    except (ValueError, TypeError, AttributeError) as error:
        raise EvidenceVerificationError("credential evidence ID is invalid") from error

    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    observed = parse_time(claim.get("observed_at"), label="evidence observation")
    expires = parse_time(claim.get("expires_at"), label="evidence expiry")
    if (
        observed > current
        or current - observed > MAX_EVIDENCE_AGE
        or expires <= current
        or expires - observed > MAX_EVIDENCE_AGE
    ):
        raise EvidenceVerificationError("credential evidence is stale or future-dated")

    producer_key = _load_public_key(
        EVIDENCE_PUBLIC_KEY, expected_sha256=evidence_public_key_sha256
    )
    if claim["producer_key_id"] != _public_key_id(producer_key):
        raise EvidenceVerificationError("producer key ID differs from the pinned key")
    producer_signature = envelope["producer_signature"]
    try:
        producer_key.verify(
            _signature(producer_signature, label="producer signature"),
            canonical_bytes(claim),
        )
    except InvalidSignature as error:
        raise EvidenceVerificationError("producer signature is invalid") from error

    anchor = envelope["external_anchor"]
    anchor_fields = {
        "schema",
        "log_id",
        "checkpoint_id",
        "entry_index",
        "claim_sha256",
        "record_sha256",
        "anchored_at",
        "retention_until",
        "anchor_key_id",
        "signature",
    }
    if (
        not isinstance(anchor, dict)
        or set(anchor) != anchor_fields
        or anchor.get("schema")
        != "fs2-serve.nebius.ai/external-evidence-anchor/v1"
        or not all(
            isinstance(anchor.get(field), str) and anchor[field]
            for field in ("log_id", "checkpoint_id", "anchor_key_id")
        )
        or not isinstance(anchor.get("entry_index"), int)
        or anchor["entry_index"] < 0
        or claim["sequence"] != anchor["entry_index"] + 1
        or anchor.get("claim_sha256") != canonical_sha256(claim)
        or anchor.get("record_sha256")
        != canonical_sha256(
            {
                "schema": "fs2-serve.nebius.ai/credential-evidence-record/v1",
                "claim_sha256": canonical_sha256(claim),
                "producer_signature": producer_signature,
            }
        )
    ):
        raise EvidenceVerificationError("external evidence anchor is malformed")
    anchored = parse_time(anchor.get("anchored_at"), label="anchor time")
    retention = parse_time(anchor.get("retention_until"), label="anchor retention")
    if anchored < observed or anchored > current or retention < current + MIN_ANCHOR_RETENTION:
        raise EvidenceVerificationError("external evidence anchor is stale or not durable")
    unsigned_anchor = {
        key: value for key, value in anchor.items() if key != "signature"
    }
    anchor_key = _load_public_key(
        ANCHOR_PUBLIC_KEY, expected_sha256=anchor_public_key_sha256
    )
    if anchor["anchor_key_id"] != _public_key_id(anchor_key):
        raise EvidenceVerificationError("anchor key ID differs from the pinned key")
    try:
        anchor_key.verify(
            _signature(anchor["signature"], label="anchor signature"),
            canonical_bytes(unsigned_anchor),
        )
    except InvalidSignature as error:
        raise EvidenceVerificationError("external anchor signature is invalid") from error
    return payload


def public_key_pin_from_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if HEX64.fullmatch(value) is None:
        raise EvidenceVerificationError(f"{name} must pin a lowercase SHA-256")
    return value
