#!/usr/bin/env python3
"""Terraform external-data verifier for SAI-25 runtime-security evidence.

The authorization record supplies only paths and expected digests. Authority
comes from an Ed25519 key in a separately mounted trust-root document, never
from decision or reviewer strings in the same Terraform input map.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SHA256 = re.compile(r"^[a-f0-9]{64}$")
STRONG_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
KEY_VALUE = re.compile(r"^[A-Za-z0-9_-]{43}$")
SIGNED_SCHEMA = "fs2-serve.nebius.ai/signed-attestation/v1"
AUTHORITY_SCHEMA = "fs2-serve.nebius.ai/platform-security-authority/v1"
AUTHORITY_PATH = "/run/fs2-runtime-security/platform-security/authority.json"


class VerificationError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        + b"\n"
    )


def _read_no_follow(path_text: str, *, maximum: int, protected_authority: bool = False) -> bytes:
    path = Path(path_text)
    if not path.is_absolute() or ".." in path.parts:
        raise VerificationError("authorization evidence path must be absolute and traversal-free")
    current_fd = os.open("/", os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            if index < len(path.parts[1:]) - 1:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
            if protected_authority:
                component = os.fstat(current_fd)
                expected_kind = stat.S_ISREG if index == len(path.parts[1:]) - 1 else stat.S_ISDIR
                if (
                    not expected_kind(component.st_mode)
                    or component.st_uid != 0
                    or stat.S_IMODE(component.st_mode) & 0o022
                ):
                    raise VerificationError(
                        "platform-security authority path is not root-owned and write-protected"
                    )
        status = os.fstat(current_fd)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_size > maximum:
            raise VerificationError("authorization evidence is not one bounded regular file")
        with os.fdopen(os.dup(current_fd), "rb") as source:
            return source.read(maximum + 1)
    finally:
        os.close(current_fd)


def _document(path: str, digest: str, *, maximum: int) -> tuple[dict[str, Any], bytes]:
    if SHA256.fullmatch(digest) is None:
        raise VerificationError("authorization file digest is invalid")
    raw = _read_no_follow(path, maximum=maximum)
    if len(raw) > maximum or hashlib.sha256(raw).hexdigest() != digest:
        raise VerificationError("authorization file differs from its exact digest")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise VerificationError("authorization file must contain one JSON object")
    if raw != _canonical(value):
        raise VerificationError("authorization file bytes are not canonical JSON")
    return value, raw


def _authority_document() -> tuple[dict[str, Any], bytes]:
    raw = _read_no_follow(
        AUTHORITY_PATH,
        maximum=64 * 1024,
        protected_authority=True,
    )
    value = json.loads(raw)
    if not isinstance(value, dict) or raw != _canonical(value):
        raise VerificationError("platform-security authority is not canonical JSON")
    return value, raw


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise VerificationError(f"{label} must be whole-second UTC")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise VerificationError(f"{label} is invalid") from error
    if result.tzinfo != timezone.utc or result.microsecond:
        raise VerificationError(f"{label} must be whole-second UTC")
    return result


def _decode(value: object, *, size: int, label: str) -> bytes:
    if not isinstance(value, str):
        raise VerificationError(f"{label} is invalid")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError as error:
        raise VerificationError(f"{label} is invalid") from error
    if len(raw) != size or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise VerificationError(f"{label} is not canonical")
    return raw


def verify(query: dict[str, str]) -> dict[str, str]:
    required = {
        "authorization_id", "kind", "model_id", "subject_schema", "subject_sha256",
        "evidence_path", "evidence_sha256", "attestation_path", "attestation_sha256",
    }
    if set(query) != required or any(not isinstance(value, str) or not value for value in query.values()):
        raise VerificationError("runtime security authorization query fields differ")
    if SHA256.fullmatch(query["subject_sha256"]) is None:
        raise VerificationError("runtime security authorization subject is invalid")
    evidence, _ = _document(query["evidence_path"], query["evidence_sha256"], maximum=1024 * 1024)
    attestation, _ = _document(query["attestation_path"], query["attestation_sha256"], maximum=64 * 1024)
    authority, authority_bytes = _authority_document()
    if set(authority) != {
        "schema", "authority", "session_id", "activated_at", "expires_at", "trusted_attestors"
    } or authority["schema"] != AUTHORITY_SCHEMA or authority["authority"] != "independent-platform-security":
        raise VerificationError("platform-security authority contract fields differ")
    session_id = authority["session_id"]
    if not isinstance(session_id, str) or STRONG_SHA256.fullmatch(session_id) is None:
        raise VerificationError("platform-security authority session is invalid")
    trust_roots = authority["trusted_attestors"]
    if evidence != {
        "schema": "fs2-serve.nebius.ai/runtime-security-evidence/v1",
        "authorization_id": query["authorization_id"],
        "kind": query["kind"],
        "model_id": query["model_id"],
        "subject_schema": query["subject_schema"],
        "subject_sha256": query["subject_sha256"],
        "decision": "accepted",
        "reviewer_role": "independent-platform-security",
    }:
        raise VerificationError("runtime security evidence claims differ")
    if set(attestation) != {
        "schema", "algorithm", "key_id", "session_id", "nonce", "issued_at", "expires_at",
        "subject", "claims", "signature",
    }:
        raise VerificationError("signed authorization envelope fields differ")
    expected_subject = {
        "kind": query["kind"],
        "schema": query["subject_schema"],
        "digest": "sha256:" + query["subject_sha256"],
        "model_id": query["model_id"],
    }
    expected_claims = {
        "authorization_id": query["authorization_id"],
        "decision": "accepted",
        "reviewer_role": "independent-platform-security",
        "evidence_sha256": query["evidence_sha256"],
    }
    if (
        attestation["schema"] != SIGNED_SCHEMA
        or attestation["algorithm"] != "ed25519"
        or attestation["session_id"] != session_id
        or attestation["subject"] != expected_subject
        or attestation["claims"] != expected_claims
    ):
        raise VerificationError("signed authorization subject or claims differ")
    issued = _timestamp(attestation["issued_at"], "authorization issued_at")
    expires = _timestamp(attestation["expires_at"], "authorization expires_at")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if expires <= issued or expires - issued > timedelta(hours=24) or issued > now + timedelta(minutes=5) or expires <= now:
        raise VerificationError("signed authorization is outside its freshness window")
    authority_activated = _timestamp(authority["activated_at"], "authority activated_at")
    authority_expires = _timestamp(authority["expires_at"], "authority expires_at")
    if (
        authority_expires <= authority_activated
        or authority_expires - authority_activated > timedelta(days=397)
        or authority_activated > now + timedelta(minutes=5)
        or authority_expires <= now
        or issued < authority_activated
        or expires > authority_expires
    ):
        raise VerificationError("platform-security authority is inactive for this authorization")
    if not isinstance(trust_roots, dict) or not 1 <= len(trust_roots) <= 32:
        raise VerificationError("runtime security trust root is empty or too large")
    key_id = attestation["key_id"]
    encoded_key = trust_roots.get(key_id)
    if not isinstance(key_id, str) or not isinstance(encoded_key, str) or KEY_VALUE.fullmatch(encoded_key) is None:
        raise VerificationError("signed authorization key is not trusted")
    public_key = _decode(encoded_key, size=32, label="trusted public key")
    if key_id != "sha256:" + hashlib.sha256(public_key).hexdigest():
        raise VerificationError("trusted key ID does not bind its public key")
    unsigned = dict(attestation)
    signature = _decode(unsigned.pop("signature"), size=64, label="authorization signature")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, _canonical(unsigned))
    except InvalidSignature as error:
        raise VerificationError("signed authorization signature verification failed") from error
    return {
        "verified": "true",
        "authorization_id": query["authorization_id"],
        "key_id": key_id,
        "session_id": session_id,
        "authority_sha256": hashlib.sha256(authority_bytes).hexdigest(),
        "subject_sha256": query["subject_sha256"],
        "evidence_sha256": query["evidence_sha256"],
        "attestation_sha256": query["attestation_sha256"],
        # Terraform must project the exact bytes opened and verified above.
        # Reopening the path with file() would reintroduce a verification/use
        # race even though the expected digest is separately configured.
        "evidence_json": _canonical(evidence).decode("utf-8"),
        "attestation_json": _canonical(attestation).decode("utf-8"),
        "trusted_attestors_json": _canonical(trust_roots).decode("utf-8"),
    }


def main() -> int:
    try:
        query = json.load(sys.stdin)
        if not isinstance(query, dict):
            raise VerificationError("external verifier input must be an object")
        result = verify(query)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
