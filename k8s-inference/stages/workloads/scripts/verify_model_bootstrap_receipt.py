#!/usr/bin/env python3
"""Verify one public, durable model-bootstrap retention receipt for Terraform."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from datetime import datetime
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

RECEIPT_TYPE = "fs2-model-bootstrap-retention+jws"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/model-bootstrap-retention-receipt/v1"
GENERATION = re.compile(r"^[a-f0-9]{32}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


def fail(message: str) -> None:
    raise ValueError(message)


def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            fail("document contains duplicate fields")
        value[key] = item
    return value


def json_object(raw: str | bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is malformed") from error
    if not isinstance(value, dict):
        fail(f"{label} is not an object")
    return value


def decode_segment(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        fail("receipt is malformed")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (TypeError, ValueError) as error:
        raise ValueError("receipt is malformed") from error


def aware_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or value.endswith("Z") is False:
        fail(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    if parsed.utcoffset() is None:
        fail(f"{label} is not timezone-aware")
    return parsed


def exact_string(
    query: dict[str, Any], key: str, pattern: re.Pattern[str] | None = None
) -> str:
    value = query.get(key)
    if not isinstance(value, str) or not value or (pattern is not None and pattern.fullmatch(value) is None):
        fail(f"query field {key} is invalid")
    return value


def exact_uuid(query: dict[str, Any], key: str) -> str:
    value = exact_string(query, key)
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError(f"query field {key} is not a canonical UUID") from error
    if str(parsed) != value:
        fail(f"query field {key} is not a canonical UUID")
    return value


def main() -> None:
    query = json_object(sys.stdin.buffer.read(), "query")
    expected_query = {
        "receipt_jws",
        "trust_json",
        "generation",
        "phase",
        "identity_sha256",
        "config_map_uid",
        "config_map_object_sha256",
        "job_uid",
        "job_object_sha256",
    }
    if set(query) != expected_query:
        fail("query shape is invalid")

    generation = exact_string(query, "generation", GENERATION)
    phase = exact_string(query, "phase")
    if phase not in {"configmap", "terminal"}:
        fail("receipt phase is invalid")
    identity_sha256 = exact_string(query, "identity_sha256", DIGEST)
    config_map_uid = exact_uuid(query, "config_map_uid")
    config_map_object_sha256 = exact_string(query, "config_map_object_sha256", DIGEST)
    job_uid = query["job_uid"]
    job_object_sha256 = query["job_object_sha256"]
    if phase == "configmap":
        if job_uid != "" or job_object_sha256 != "":
            fail("configmap receipt cannot carry a Job identity")
    elif (
        not isinstance(job_uid, str)
        or not isinstance(job_object_sha256, str)
        or DIGEST.fullmatch(job_object_sha256) is None
    ):
        fail("terminal receipt requires the exact Job identity")
    if phase == "terminal":
        exact_uuid(query, "job_uid")

    trust = json_object(exact_string(query, "trust_json"), "trust policy")
    if (
        set(trust) != {"schema", "issuers"}
        or trust.get("schema") != "fs2-serve.nebius.ai/release-identity-trust/v1"
    ):
        fail("trust policy shape is invalid")
    issuers = trust.get("issuers")
    if not isinstance(issuers, list) or not 1 <= len(issuers) <= 8:
        fail("trust policy issuers are invalid")

    compact = exact_string(query, "receipt_jws")
    if len(compact) > 8192:
        fail("receipt exceeds the size bound")
    parts = compact.split(".")
    if len(parts) != 3:
        fail("receipt is malformed")
    protected = json_object(decode_segment(parts[0]), "protected header")
    claims = json_object(decode_segment(parts[1]), "receipt claims")
    if (
        set(protected) != {"alg", "kid", "typ"}
        or protected.get("alg") != "EdDSA"
        or protected.get("typ") != RECEIPT_TYPE
    ):
        fail("receipt protected header is invalid")
    key_id = protected.get("kid")
    if not isinstance(key_id, str) or not key_id:
        fail("receipt signing key id is invalid")

    required_claims = {
        "schema",
        "issuer",
        "subject",
        "audience",
        "authorization_closure_sha256",
        "issued_at",
        "generation",
        "phase",
        "identity_sha256",
        "config_map_uid",
        "config_map_object_sha256",
        "job_uid",
        "job_object_sha256",
        "release_assertion_id",
        "release_assertion_fingerprint",
        "release_receipt_consumed_at",
    }
    if set(claims) != required_claims or claims.get("schema") != RECEIPT_SCHEMA:
        fail("receipt claim shape is invalid")
    if claims.get("audience") != "fs2-admin-release":
        fail("receipt audience is invalid")
    if (
        not isinstance(claims.get("issuer"), str)
        or not 1 <= len(claims["issuer"]) <= 200
        or not isinstance(claims.get("subject"), str)
        or not 1 <= len(claims["subject"]) <= 200
        or not isinstance(claims.get("authorization_closure_sha256"), str)
        or DIGEST.fullmatch(claims["authorization_closure_sha256"]) is None
    ):
        fail("receipt authority claims are invalid")

    trusted_issuer: dict[str, Any] | None = None
    for candidate in issuers:
        if isinstance(candidate, dict) and candidate.get("issuer") == claims.get("issuer"):
            if trusted_issuer is not None:
                fail("receipt issuer is ambiguous")
            trusted_issuer = candidate
    if trusted_issuer is None:
        fail("receipt issuer is not trusted")
    capabilities = trusted_issuer.get("capabilities")
    keys = trusted_issuer.get("keys")
    if set(trusted_issuer) != {
        "issuer",
        "workload_subject",
        "audience",
        "authorization_closure_sha256",
        "capabilities",
        "keys",
    } or (
        not isinstance(capabilities, list)
        or not 1 <= len(capabilities) <= 6
        or len(set(capabilities)) != len(capabilities)
        or any(not isinstance(capability, str) for capability in capabilities)
        or "models.bootstrap" not in capabilities
        or not isinstance(keys, list)
        or not 1 <= len(keys) <= 8
    ):
        fail("receipt authority lacks the exact bootstrap capability")
    if (
        claims.get("subject") != trusted_issuer.get("workload_subject")
        or claims.get("audience") != trusted_issuer.get("audience")
        or claims.get("authorization_closure_sha256")
        != trusted_issuer.get("authorization_closure_sha256")
    ):
        fail("receipt authority does not match the trust policy")

    public_key: Ed25519PublicKey | None = None
    for candidate in keys:
        if isinstance(candidate, dict) and candidate.get("key_id") == key_id:
            if public_key is not None:
                fail("receipt signing key is ambiguous")
            if (
                set(candidate) != {"key_id", "public_key_base64"}
                or len(key_id) > 64
                or KEY_ID.fullmatch(key_id) is None
            ):
                fail("receipt signing key is invalid")
            encoded = candidate.get("public_key_base64")
            if not isinstance(encoded, str) or not 43 <= len(encoded) <= 64:
                fail("receipt signing key is invalid")
            raw = decode_segment(encoded)
            if len(raw) != 32:
                fail("receipt signing key length is invalid")
            public_key = Ed25519PublicKey.from_public_bytes(raw)
    if public_key is None:
        fail("receipt signing key is not trusted")
    try:
        public_key.verify(decode_segment(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii"))
    except InvalidSignature as error:
        raise ValueError("receipt signature is invalid") from error

    expected_claims = {
        "generation": generation,
        "phase": phase,
        "identity_sha256": identity_sha256,
        "config_map_uid": config_map_uid,
        "config_map_object_sha256": config_map_object_sha256,
        "job_uid": None if phase == "configmap" else job_uid,
        "job_object_sha256": None if phase == "configmap" else job_object_sha256,
    }
    for key, expected in expected_claims.items():
        if claims.get(key) != expected:
            fail(f"receipt claim {key} does not match the observed object")
    try:
        assertion_id = UUID(str(claims.get("release_assertion_id")))
    except ValueError as error:
        raise ValueError("receipt assertion id is invalid") from error
    if str(assertion_id) != claims.get("release_assertion_id"):
        fail("receipt assertion id is not canonical")
    if not isinstance(claims.get("release_assertion_fingerprint"), str) or DIGEST.fullmatch(
        claims["release_assertion_fingerprint"]
    ) is None:
        fail("receipt assertion fingerprint is invalid")
    issued_at = aware_timestamp(claims.get("issued_at"), "retention receipt time")
    if phase == "terminal":
        consumed_at = aware_timestamp(claims.get("release_receipt_consumed_at"), "release receipt time")
        if issued_at < consumed_at:
            fail("retention receipt predates assertion consumption")
    elif claims.get("release_receipt_consumed_at") is not None:
        fail("a pre-Job ConfigMap receipt cannot claim assertion consumption")

    sys.stdout.write(
        json.dumps(
            {
                "valid": "true",
                "generation": generation,
                "phase": phase,
                "receipt_sha256": hashlib.sha256(compact.encode("ascii")).hexdigest(),
                "issuer": str(claims["issuer"]),
                "config_map_uid": config_map_uid,
                "job_uid": "" if phase == "configmap" else job_uid,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, TypeError, ValueError) as error:
        sys.stderr.write(f"model bootstrap retention receipt refused: {error}\n")
        raise SystemExit(1) from None
