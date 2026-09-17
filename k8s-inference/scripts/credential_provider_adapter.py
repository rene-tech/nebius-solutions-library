#!/usr/bin/env python3
"""Kernel-authenticated client for the production credential authority.

The operator cannot select an executable, socket, provider profile, project, or
response file.  The root-owned authority behind the fixed Unix socket performs
provider-native reads and class-specific readiness probes.  SO_PEERCRED binds
the response to uid 0; a nonce and payload digest bind it to this exact request.
No mutation operation is accepted while the program-wide irreversible-action
prohibition is in force.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from credential_evidence import (
    EvidenceVerificationError,
    canonical_sha256,
    verify_evidence_envelope,
)

AUTHORITY_SOCKET = Path("/run/fs2-credential-authority/v1.sock")
CLIENT_POLICY = Path("/etc/fs2-credential-authority/client-policy.json")
SOURCE_TRUST_POLICY = Path(
    "/usr/share/fs2-credential-authority/credential-evidence-source-trust.json"
)
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
READ_ONLY_OPERATIONS = frozenset(
    {
        "custody-snapshot",
        "planned-generation-admission",
        "artifact-inventory",
        "consumer-readiness",
        "credential-inventory",
        "rotation-readiness",
        "viewer-handoff-inventory",
        "ciphertext-migration",
        "authentication-continuity",
        "release-identity",
        "backend-custody",
    }
)


class AuthorityError(RuntimeError):
    pass


def load_client_policy() -> dict[str, Any]:
    if CLIENT_POLICY.is_symlink() or not CLIENT_POLICY.is_file():
        raise AuthorityError("credential authority client policy is absent")
    metadata = CLIENT_POLICY.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise AuthorityError(
            "credential authority client policy must be root-owned and immutable to clients"
        )
    document = json.loads(CLIENT_POLICY.read_text(encoding="utf-8"))
    required = {
        "schema",
        "trust_bundle_id",
        "evidence_public_key_sha256",
        "anchor_public_key_sha256",
    }
    if (
        not isinstance(document, dict)
        or set(document) != required
        or document.get("schema")
        != "fs2-serve.nebius.ai/credential-authority-client-policy/v1"
    ):
        raise AuthorityError("credential authority client policy is malformed")
    for field in required - {"schema"}:
        value = document.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise AuthorityError("credential authority client key pin is malformed")
    if SOURCE_TRUST_POLICY.is_symlink() or not SOURCE_TRUST_POLICY.is_file():
        raise AuthorityError("source-owned external evidence trust policy is absent")
    trust = json.loads(SOURCE_TRUST_POLICY.read_text(encoding="utf-8"))
    trust_fields = {"schema", "minimum_witnesses", "accepted_trust_bundles", "authorization_blocker"}
    if (
        not isinstance(trust, dict)
        or set(trust) != trust_fields
        or trust.get("schema")
        != "fs2-serve.nebius.ai/credential-evidence-source-trust/v2"
        or trust.get("minimum_witnesses") != 2
        or not isinstance(trust.get("accepted_trust_bundles"), list)
    ):
        raise AuthorityError(
            "external evidence trust is not source-authorized; local pins cannot authorize it"
        )
    selected = [
        item
        for item in trust["accepted_trust_bundles"]
        if isinstance(item, dict) and item.get("id") == document.get("trust_bundle_id")
    ]
    if len(selected) != 1:
        raise AuthorityError(
            "external evidence trust bundle is not accepted by checked source"
        )
    bundle = selected[0]
    bundle_fields = {
        "id",
        "log_id",
        "endpoint",
        "producer_public_key_sha256",
        "anchor_public_key_sha256",
        "anchor_key_id",
        "authority_policy_sha256",
        "trusted_checkpoint",
        "witnesses",
        "minimum_witnesses",
    }
    witnesses = bundle.get("witnesses")
    endpoint = urlsplit(bundle.get("endpoint", ""))
    if (
        set(bundle) != bundle_fields
        or not all(
            isinstance(bundle.get(field), str) and bundle[field]
            for field in ("id", "log_id", "endpoint", "anchor_key_id")
        )
        or bundle.get("minimum_witnesses") != trust["minimum_witnesses"]
        or endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or endpoint.path != "/v1/credential-evidence"
        or not isinstance(witnesses, list)
        or len(witnesses) < trust["minimum_witnesses"]
        or len({item.get("witness_id") for item in witnesses if isinstance(item, dict)})
        != len(witnesses)
    ):
        raise AuthorityError("source-owned external evidence trust bundle is malformed")
    pins = [
        bundle.get("producer_public_key_sha256"),
        bundle.get("anchor_public_key_sha256"),
        bundle.get("authority_policy_sha256"),
        *(item.get("public_key_sha256") for item in witnesses if isinstance(item, dict)),
    ]
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in pins
    ):
        raise AuthorityError("source-owned external evidence key pin is malformed")
    if any(
        not isinstance(item, dict)
        or set(item) != {"witness_id", "key_id", "public_key_sha256"}
        or not all(isinstance(item.get(field), str) and item[field] for field in ("witness_id", "key_id"))
        for item in witnesses
    ):
        raise AuthorityError("source-owned witness identity is malformed")
    prior = bundle.get("trusted_checkpoint")
    if (
        not isinstance(prior, dict)
        or set(prior) != {"tree_size", "root_sha256", "checkpoint_sha256"}
        or not isinstance(prior.get("tree_size"), int)
        or prior["tree_size"] < 1
        or any(
            not isinstance(prior.get(field), str)
            or len(prior[field]) != 64
            or any(character not in "0123456789abcdef" for character in prior[field])
            for field in ("root_sha256", "checkpoint_sha256")
        )
        or document["evidence_public_key_sha256"]
        != bundle["producer_public_key_sha256"]
        or document["anchor_public_key_sha256"]
        != bundle["anchor_public_key_sha256"]
    ):
        raise AuthorityError("source-owned trusted checkpoint is malformed")
    return {**document, "source_trust": bundle}


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise AuthorityError("credential authority closed an incomplete response")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def authority_call(request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation")
    if operation not in READ_ONLY_OPERATIONS:
        raise AuthorityError(
            "credential authority adapter is read-only; create, switch, disable, revoke and delete are prohibited"
        )
    if AUTHORITY_SOCKET.is_symlink():
        raise AuthorityError("credential authority socket must not be a symlink")
    metadata = os.stat(AUTHORITY_SOCKET, follow_symlinks=False)
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != 0:
        raise AuthorityError("credential authority must be a root-owned Unix socket")
    policy = load_client_policy()
    nonce = uuid.uuid4().hex
    bound_request = {**request, "request_nonce": nonce}
    envelope = {
        "schema": "fs2-serve.nebius.ai/credential-authority-request/v1",
        "nonce": nonce,
        "request_sha256": canonical_sha256(bound_request),
        "request": bound_request,
    }
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise AuthorityError("credential authority request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(AUTHORITY_SOCKET))
        if not hasattr(socket, "SO_PEERCRED"):
            raise AuthorityError("kernel peer credentials are unavailable")
        pid, uid, _gid = struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if uid != 0 or pid <= 0:
            raise AuthorityError("credential authority peer is not root-owned")
        connection.sendall(struct.pack("!I", len(encoded)) + encoded)
        size = struct.unpack("!I", _recv_exact(connection, 4))[0]
        if not 1 <= size <= MAX_MESSAGE_BYTES:
            raise AuthorityError("credential authority response size is invalid")
        response_bytes = _recv_exact(connection, size)
    try:
        response = json.loads(response_bytes)
    except json.JSONDecodeError as error:
        raise AuthorityError("credential authority returned invalid JSON") from error
    try:
        payload = verify_evidence_envelope(
            response,
            expected_operation=str(operation),
            expected_request_sha256=envelope["request_sha256"],
            expected_nonce=nonce,
            evidence_public_key_sha256=policy["evidence_public_key_sha256"],
            anchor_public_key_sha256=policy["anchor_public_key_sha256"],
            source_trust=policy["source_trust"],
        )
    except EvidenceVerificationError as error:
        raise AuthorityError(str(error)) from error
    if (
        payload.get("schema")
        != "fs2-serve.nebius.ai/credential-provider-observation/v2"
        or payload.get("operation") != operation
        or payload.get("complete") is not True
        or payload.get("data_fields_returned") != 0
        or not isinstance(payload.get("result"), dict)
    ):
        raise AuthorityError("credential provider observation is incomplete")
    result = payload["result"]
    if "externalEvidence" in result or "authorityObservation" in result:
        raise AuthorityError("credential authority payload uses a reserved field")
    proof = {key: value for key, value in response.items() if key != "payload"}
    observation = {key: value for key, value in payload.items() if key != "result"}
    return {
        **result,
        "authorityObservation": observation,
        "externalEvidence": proof,
    }


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise AuthorityError("credential authority request must be an object")
        print(json.dumps(authority_call(request), sort_keys=True))
        return 0
    except (AuthorityError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
