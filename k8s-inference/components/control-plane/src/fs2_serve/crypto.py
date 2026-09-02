"""AES-256-GCM envelope for short-lived queued request and result payloads."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True)
class Ciphertext:
    key_id: str
    nonce: bytes
    value: bytes


class PayloadCipher:
    """Encrypt payloads with per-row nonces and metadata-bound AAD.

    The mounted key ring permits rotation without rewriting active queue rows.
    Key material never enters configuration values, logs, metrics, traces, or
    database columns.
    """

    def __init__(self, *, active_key_id: str, keys: dict[str, bytes]) -> None:
        if active_key_id not in keys:
            raise ValueError("active payload key is absent from key ring")
        if KEY_ID_RE.fullmatch(active_key_id) is None:
            raise ValueError("invalid active payload key id")
        if any(KEY_ID_RE.fullmatch(key_id) is None or len(key) != 32 for key_id, key in keys.items()):
            raise ValueError("payload key ids must be bounded and every decoded AES-256 key must be 32 bytes")
        self.active_key_id = active_key_id
        self._keys = dict(keys)

    @classmethod
    def from_file(cls, path: Path) -> PayloadCipher:
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read payload key ring {path}") from exc
        if not isinstance(raw, dict) or set(raw) != {"active_key_id", "keys"} or not isinstance(raw["keys"], dict):
            raise ValueError("payload key ring must contain only active_key_id and keys")
        keys: dict[str, bytes] = {}
        for key_id, encoded in raw["keys"].items():
            if not isinstance(key_id, str) or not isinstance(encoded, str):
                raise ValueError("payload key ring keys must be base64 strings")
            try:
                keys[key_id] = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("payload key ring contains invalid base64") from exc
        if not isinstance(raw["active_key_id"], str):
            raise ValueError("active_key_id must be a string")
        return cls(active_key_id=raw["active_key_id"], keys=keys)

    @staticmethod
    def aad(operation_id: UUID, tenant_id: str, model_id: str, direction: str) -> bytes:
        if direction not in {"request", "response"}:
            raise ValueError("invalid payload direction")
        return f"fs2-serve.payload/v1\0{operation_id}\0{tenant_id}\0{model_id}\0{direction}".encode()

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> Ciphertext:
        nonce = os.urandom(12)
        value = AESGCM(self._keys[self.active_key_id]).encrypt(nonce, plaintext, aad)
        return Ciphertext(key_id=self.active_key_id, nonce=nonce, value=value)

    def decrypt(self, envelope: Ciphertext, *, aad: bytes) -> bytes:
        try:
            key = self._keys[envelope.key_id]
        except KeyError as exc:
            raise ValueError("payload key id is not available") from exc
        return AESGCM(key).decrypt(envelope.nonce, envelope.value, aad)


class KeyedHasher:
    """Versioned HMAC-SHA-256 for non-public idempotency and ledger digests."""

    def __init__(self, *, active_key_id: str, keys: dict[str, bytes]) -> None:
        if active_key_id not in keys:
            raise ValueError("active ledger HMAC key is absent from key ring")
        if not 1 <= len(keys) <= 32:
            raise ValueError("ledger HMAC key ring must contain between 1 and 32 keys")
        if any(KEY_ID_RE.fullmatch(key_id) is None or len(key) < 32 for key_id, key in keys.items()):
            raise ValueError("ledger HMAC keys must be bounded and contain at least 32 bytes")
        self.active_key_id = active_key_id
        self._keys = dict(keys)

    @classmethod
    def from_file(cls, path: Path) -> KeyedHasher:
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read ledger HMAC key ring {path}") from exc
        if not isinstance(raw, dict) or set(raw) != {"active_key_id", "keys"} or not isinstance(raw["keys"], dict):
            raise ValueError("ledger HMAC key ring must contain only active_key_id and keys")
        keys: dict[str, bytes] = {}
        for key_id, encoded in raw["keys"].items():
            if not isinstance(key_id, str) or not isinstance(encoded, str):
                raise ValueError("ledger HMAC key ring values must be base64 strings")
            try:
                keys[key_id] = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("ledger HMAC key ring contains invalid base64") from exc
        if not isinstance(raw["active_key_id"], str):
            raise ValueError("ledger HMAC active_key_id must be a string")
        return cls(active_key_id=raw["active_key_id"], keys=keys)

    def digest(self, value: bytes, *, context: str) -> tuple[str, str]:
        return self.active_key_id, self.digest_for(self.active_key_id, value, context=context)

    def digest_for(self, key_id: str, value: bytes, *, context: str) -> str:
        """Digest with a retained key; missing replay keys fail closed after rotation."""

        if not context or len(context) > 128:
            raise ValueError("ledger HMAC context is invalid")
        try:
            key = self._keys[key_id]
        except KeyError as exc:
            raise ValueError("ledger HMAC replay key is unavailable") from exc
        return hmac.new(key, context.encode() + b"\0" + value, hashlib.sha256).hexdigest()

    def candidate_digests(self, value: bytes, *, context: str) -> tuple[tuple[str, str], ...]:
        """Return bounded replay identities for every retained rotation key.

        Callers persist only the selected key ID and HMAC.  A later replay can
        therefore locate receipts created before key rotation without storing
        or logging the caller-provided idempotency key.
        """

        return tuple(
            (key_id, self.digest_for(key_id, value, context=context))
            for key_id in sorted(self._keys)
        )
