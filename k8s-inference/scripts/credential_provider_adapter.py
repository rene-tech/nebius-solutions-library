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

import hashlib
import json
import os
import socket
import stat
import struct
import sys
import uuid
from pathlib import Path
from typing import Any

AUTHORITY_SOCKET = Path("/run/fs2-credential-authority/v1.sock")
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
READ_ONLY_OPERATIONS = frozenset(
    {
        "get",
        "inventory",
        "list-operation",
        "secret-bindings",
        "artifact-inventory",
        "consumer-readiness",
        "prove-consumers",
        "prove-zero-readers",
        "prove-absence",
        "prove-ciphertext-migration",
        "prove-customer-storage-cipher-migration",
        "prove-customer-storage-name-migration",
        "prove-auth-continuity",
        "verify-attestation",
    }
)


class AuthorityError(RuntimeError):
    pass


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


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
    nonce = uuid.uuid4().hex
    envelope = {
        "schema": "fs2-serve.nebius.ai/credential-authority-request/v1",
        "nonce": nonce,
        "request_sha256": canonical_sha256(request),
        "request": request,
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
    attestation = response.get("authority_attestation")
    if (
        not isinstance(response, dict)
        or response.get("schema")
        != "fs2-serve.nebius.ai/credential-authority-response/v1"
        or response.get("nonce") != nonce
        or response.get("request_sha256") != envelope["request_sha256"]
        or not isinstance(response.get("payload"), dict)
        or response.get("payload_sha256") != canonical_sha256(response.get("payload"))
        or not isinstance(attestation, dict)
        or attestation.get("request_sha256") != envelope["request_sha256"]
        or attestation.get("payload_sha256") != response.get("payload_sha256")
    ):
        raise AuthorityError("credential authority response binding is invalid")
    if "authorityAttestation" in response["payload"]:
        raise AuthorityError("credential authority payload uses a reserved field")
    return {**response["payload"], "authorityAttestation": attestation}


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
