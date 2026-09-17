"""Verify one-use assertions minted for provider-attested release workloads."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field

from .access_models import ReleaseIdentityAssertion, ReleaseIdentityCapability, ReleaseIdentityPurpose
from .models import StrictModel

MAX_RELEASE_ASSERTION_LENGTH = 8192
RELEASE_ASSERTION_TYPE = "fs2-release-identity+jws"


class ReleaseIdentityError(PermissionError):
    pass


class ReleaseIdentityKey(StrictModel):
    key_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    public_key_base64: str = Field(min_length=43, max_length=64)


class ReleaseIdentityIssuer(StrictModel):
    issuer: str = Field(min_length=1, max_length=200)
    workload_subject: str = Field(min_length=1, max_length=200)
    audience: str = Field(pattern=r"^fs2-admin-release$")
    authorization_closure_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    capabilities: frozenset[ReleaseIdentityCapability] = Field(min_length=1, max_length=6)
    keys: tuple[ReleaseIdentityKey, ...] = Field(min_length=1, max_length=8)


class ReleaseIdentityTrust(StrictModel):
    schema: str = Field(pattern=r"^fs2-serve\.nebius\.ai/release-identity-trust/v1$")
    issuers: tuple[ReleaseIdentityIssuer, ...] = Field(min_length=1, max_length=8)


@dataclass(frozen=True)
class VerifiedReleaseIdentity:
    assertion: ReleaseIdentityAssertion
    fingerprint: str

    @property
    def actor(self) -> str:
        return f"release:{self.assertion.subject}"


def _decode_segment(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise ReleaseIdentityError("release identity assertion is malformed")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as error:
        raise ReleaseIdentityError("release identity assertion is malformed") from error


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ReleaseIdentityError("release identity assertion contains duplicate fields")
        value[key] = item
    return value


def _json_object(value: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(value, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseIdentityError("release identity assertion is malformed") from error
    if not isinstance(decoded, dict):
        raise ReleaseIdentityError("release identity assertion is malformed")
    return decoded


class ReleaseIdentityVerifier:
    """Pin issuer keys and the exact non-human authorization closure."""

    def __init__(self, trust: ReleaseIdentityTrust, *, clock_skew_seconds: int = 15) -> None:
        if not 0 <= clock_skew_seconds <= 30:
            raise ValueError("release identity clock skew is outside the bound")
        issuers: dict[str, tuple[ReleaseIdentityIssuer, dict[str, Ed25519PublicKey]]] = {}
        seen_key_ids: set[tuple[str, str]] = set()
        for issuer in trust.issuers:
            if issuer.issuer in issuers:
                raise ValueError("release identity issuers must be unique")
            keys: dict[str, Ed25519PublicKey] = {}
            for item in issuer.keys:
                if (issuer.issuer, item.key_id) in seen_key_ids:
                    raise ValueError("release identity key ids must be unique per issuer")
                seen_key_ids.add((issuer.issuer, item.key_id))
                raw = _decode_segment(item.public_key_base64)
                if len(raw) != 32:
                    raise ValueError("release identity Ed25519 public keys must contain 32 bytes")
                keys[item.key_id] = Ed25519PublicKey.from_public_bytes(raw)
            issuers[issuer.issuer] = (issuer, keys)
        self._issuers = issuers
        self._clock_skew = timedelta(seconds=clock_skew_seconds)

    @classmethod
    def from_file(cls, path: Path) -> ReleaseIdentityVerifier:
        try:
            raw = _json_object(path.read_bytes())
            trust = ReleaseIdentityTrust.model_validate(raw)
            return cls(trust)
        except OSError as error:
            raise ValueError(f"cannot read release identity trust policy {path}") from error
        except (ReleaseIdentityError, ValueError) as error:
            raise ValueError(f"release identity trust policy {path} is malformed") from error

    def verify(
        self,
        compact: str,
        *,
        capability: ReleaseIdentityCapability,
        purpose: ReleaseIdentityPurpose,
        now: datetime | None = None,
    ) -> VerifiedReleaseIdentity:
        if len(compact) > MAX_RELEASE_ASSERTION_LENGTH:
            raise ReleaseIdentityError("release identity assertion is invalid")
        parts = compact.split(".")
        if len(parts) != 3:
            raise ReleaseIdentityError("release identity assertion is invalid")
        protected_raw = _decode_segment(parts[0])
        payload_raw = _decode_segment(parts[1])
        signature = _decode_segment(parts[2])
        protected = _json_object(protected_raw)
        if set(protected) != {"alg", "kid", "typ"} or protected.get("alg") != "EdDSA" or protected.get("typ") != RELEASE_ASSERTION_TYPE:
            raise ReleaseIdentityError("release identity protected header is invalid")
        key_id = protected.get("kid")
        if not isinstance(key_id, str):
            raise ReleaseIdentityError("release identity protected header is invalid")
        try:
            assertion = ReleaseIdentityAssertion.model_validate(_json_object(payload_raw))
        except ValueError as error:
            raise ReleaseIdentityError("release identity claims are invalid") from error
        trusted = self._issuers.get(assertion.issuer)
        if trusted is None:
            raise ReleaseIdentityError("release identity issuer is not trusted")
        issuer, keys = trusted
        key = keys.get(key_id)
        if key is None:
            raise ReleaseIdentityError("release identity signing key is not trusted")
        try:
            key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"))
        except InvalidSignature as error:
            raise ReleaseIdentityError("release identity signature is invalid") from error
        observed = now or datetime.now(UTC)
        if observed.tzinfo is None:
            raise ValueError("release identity verification time must be timezone-aware")
        if (
            assertion.audience != issuer.audience
            or assertion.subject != issuer.workload_subject
            or assertion.authorization_closure_sha256 != issuer.authorization_closure_sha256
            or not assertion.capabilities.issubset(issuer.capabilities)
            or assertion.capabilities != frozenset({capability})
            or assertion.purpose is not purpose
            or assertion.issued_at > observed + self._clock_skew
            or assertion.not_before > observed + self._clock_skew
            or assertion.expires_at <= observed
            or assertion.session_issued_at > observed + self._clock_skew
            or assertion.session_expires_at <= observed
        ):
            raise ReleaseIdentityError("release identity assertion is outside policy")
        return VerifiedReleaseIdentity(
            assertion=assertion,
            fingerprint=hashlib.sha256(compact.encode("ascii")).hexdigest(),
        )
