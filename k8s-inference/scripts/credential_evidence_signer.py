#!/usr/bin/env python3
"""Minimal Ed25519 producer signer for credential observations.

The private key path is fixed and cannot be supplied by the caller.  This
process signs only the strict credential-evidence claim schema and emits no key
material.  External anchoring is performed by a separately configured service.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key,
)

KEY_PATH = Path("/etc/fs2-credential-authority/evidence-producer-ed25519-private.pem")


class SignerError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def load_key() -> tuple[Any, str]:
    if KEY_PATH.is_symlink() or not KEY_PATH.is_file():
        raise SignerError("producer private key is absent")
    metadata = KEY_PATH.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SignerError("producer private key must be root-owned mode 0600")
    encoded = KEY_PATH.read_bytes()
    key = load_pem_private_key(encoded, password=None)
    if key.__class__.__name__ != "Ed25519PrivateKey":
        raise SignerError("producer private key must be Ed25519")
    public = key.public_key().public_bytes(
        encoding=Encoding.Raw, format=PublicFormat.Raw
    )
    return key, hashlib.sha256(public).hexdigest()


def main() -> int:
    request = json.loads(sys.stdin.read())
    if (
        not isinstance(request, dict)
        or set(request) != {"schema", "claim"}
        or request.get("schema")
        != "fs2-serve.nebius.ai/credential-evidence-sign-request/v1"
        or not isinstance(request.get("claim"), dict)
        or request["claim"].get("schema")
        != "fs2-serve.nebius.ai/credential-evidence-claim/v1"
    ):
        raise SignerError("credential evidence signing request is malformed")
    key, key_id = load_key()
    if request["claim"].get("producer_key_id") != key_id:
        raise SignerError("credential evidence claim names another producer key")
    signature = base64.b64encode(key.sign(canonical_bytes(request["claim"]))).decode()
    print(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/credential-evidence-signature/v1",
                "producer_key_id": key_id,
                "signature": signature,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SignerError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
