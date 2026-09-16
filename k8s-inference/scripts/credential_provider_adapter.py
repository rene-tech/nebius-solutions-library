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

from credential_evidence import (
    EvidenceVerificationError,
    canonical_sha256,
    verify_evidence_envelope,
)

AUTHORITY_SOCKET = Path("/run/fs2-credential-authority/v1.sock")
CLIENT_POLICY = Path("/etc/fs2-credential-authority/client-policy.json")
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
    }
)


class AuthorityError(RuntimeError):
    pass


def load_client_policy() -> dict[str, str]:
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
    return document


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
