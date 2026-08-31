#!/usr/bin/env python3
"""Ed25519 signed-attestation contract for live routing evidence."""

from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .artifacts import canonical_bytes
from .loader import CatalogError, _exact, _text, strong_sha256


SIGNED_ATTESTATION_SCHEMA = "fs2-serve.nebius.ai/signed-attestation/v1"
ATTESTATION_ALGORITHM = "ed25519"
MAX_ATTESTATION_LIFETIME = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=5)


def _timestamp(value: Any, label: str) -> datetime:
    text = _text(value, label)
    assert text is not None
    if not text.endswith("Z"):
        raise CatalogError(f"{label} must be UTC with a Z suffix")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise CatalogError(f"{label} is not an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc or parsed.microsecond:
        raise CatalogError(f"{label} must use whole UTC seconds")
    return parsed


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: Any, label: str, *, expected_bytes: int) -> bytes:
    text = _text(value, label)
    assert text is not None
    try:
        decoded = base64.b64decode(
            text + "=" * (-len(text) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise CatalogError(f"{label} is not canonical base64url") from exc
    if len(decoded) != expected_bytes or _b64url_encode(decoded) != text:
        raise CatalogError(f"{label} has the wrong size or encoding")
    return decoded


def public_key_value(key: Ed25519PublicKey) -> str:
    """Return the canonical raw public-key value accepted by the verifier."""

    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64url_encode(raw)


def public_key_id(key: Ed25519PublicKey) -> str:
    """Return the immutable key ID derived from the raw Ed25519 public key."""

    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def raw_public_key(value: Any, label: str = "Ed25519 public key") -> bytes:
    """Decode one canonical raw Ed25519 public key."""

    return _b64url_decode(value, label, expected_bytes=32)


def raw_signature(value: Any, label: str = "Ed25519 signature") -> bytes:
    """Decode one canonical raw Ed25519 signature."""

    return _b64url_decode(value, label, expected_bytes=64)


def raw_public_key_id(value: Any, label: str = "Ed25519 public key") -> str:
    """Derive the immutable identity of a canonical raw public key."""

    return "sha256:" + hashlib.sha256(raw_public_key(value, label)).hexdigest()


def create_signed_attestation(
    *,
    private_key: Ed25519PrivateKey,
    session_id: str,
    nonce: str,
    issued_at: str,
    expires_at: str,
    kind: str,
    subject_schema: str,
    subject_digest: str,
    model_id: str,
    claims: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one canonical signed envelope for an already content-addressed subject."""

    strong_sha256(session_id, "attestation session ID")
    strong_sha256(nonce, "attestation nonce")
    strong_sha256(subject_digest, "attestation subject digest")
    issued = _timestamp(issued_at, "attestation issued_at")
    expires = _timestamp(expires_at, "attestation expires_at")
    if expires <= issued or expires - issued > MAX_ATTESTATION_LIFETIME:
        raise CatalogError("attestation validity interval is outside the closed bound")
    envelope: dict[str, Any] = {
        "schema": SIGNED_ATTESTATION_SCHEMA,
        "algorithm": ATTESTATION_ALGORITHM,
        "key_id": public_key_id(private_key.public_key()),
        "session_id": session_id,
        "nonce": nonce,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "subject": {
            "kind": kind,
            "schema": subject_schema,
            "digest": subject_digest,
            "model_id": model_id,
        },
        "claims": dict(claims),
    }
    envelope["signature"] = _b64url_encode(private_key.sign(canonical_bytes(envelope)))
    return envelope


def verify_signed_attestation(
    value: Any,
    *,
    trusted_attestors: Mapping[str, str],
    expected_session_id: str,
    expected_kind: str,
    expected_schema: str,
    expected_digest: str,
    expected_model_id: str,
    validation_time: datetime | None = None,
) -> dict[str, Any]:
    """Verify trust, signature, freshness, session, and the exact payload subject."""

    item = _exact(
        value,
        {
            "schema",
            "algorithm",
            "key_id",
            "session_id",
            "nonce",
            "issued_at",
            "expires_at",
            "subject",
            "claims",
            "signature",
        },
        "signed attestation",
    )
    if item["schema"] != SIGNED_ATTESTATION_SCHEMA or item["algorithm"] != ATTESTATION_ALGORITHM:
        raise CatalogError("unsupported signed-attestation contract")
    strong_sha256(item["session_id"], "attestation session ID")
    strong_sha256(item["nonce"], "attestation nonce")
    if item["session_id"] != expected_session_id:
        raise CatalogError("signed attestation belongs to another evidence session")
    subject = _exact(
        item["subject"], {"kind", "schema", "digest", "model_id"}, "attestation subject"
    )
    strong_sha256(subject["digest"], "attestation subject digest")
    expected_subject = {
        "kind": expected_kind,
        "schema": expected_schema,
        "digest": expected_digest,
        "model_id": expected_model_id,
    }
    if subject != expected_subject:
        raise CatalogError("signed attestation subject does not match the reopened evidence")
    if not isinstance(item["claims"], dict):
        raise CatalogError("signed attestation claims must be an object")
    issued = _timestamp(item["issued_at"], "attestation issued_at")
    expires = _timestamp(item["expires_at"], "attestation expires_at")
    if expires <= issued or expires - issued > MAX_ATTESTATION_LIFETIME:
        raise CatalogError("attestation validity interval is outside the closed bound")
    now = validation_time or datetime.now(timezone.utc).replace(microsecond=0)
    if now.tzinfo is None:
        raise CatalogError("attestation validation time must be timezone-aware")
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    if issued > now + MAX_CLOCK_SKEW or expires <= now:
        raise CatalogError("signed attestation is not fresh at validation time")

    key_id = _text(item["key_id"], "attestation key ID")
    assert key_id is not None
    try:
        encoded_key = trusted_attestors[key_id]
    except KeyError as exc:
        raise CatalogError("signed attestation uses an untrusted key") from exc
    raw_key = _b64url_decode(encoded_key, "trusted attestor public key", expected_bytes=32)
    if key_id != "sha256:" + hashlib.sha256(raw_key).hexdigest():
        raise CatalogError("trusted attestor key ID does not bind its public key")
    signature = _b64url_decode(item["signature"], "attestation signature", expected_bytes=64)
    unsigned = dict(item)
    unsigned.pop("signature")
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, canonical_bytes(unsigned))
    except InvalidSignature as exc:
        raise CatalogError("signed attestation signature verification failed") from exc
    return item
