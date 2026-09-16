from __future__ import annotations

from uuid import uuid4

import pytest

from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.request_debug import PostgresDebugStore


def test_payload_rotation_reads_preexisting_operation_and_request_debug_ciphertext() -> None:
    old_key, new_key = b"o" * 32, b"n" * 32
    before = PayloadCipher(active_key_id="payload-v1", keys={"payload-v1": old_key})
    after = PayloadCipher(
        active_key_id="payload-v2",
        keys={"payload-v1": old_key, "payload-v2": new_key},
    )
    operation_id, exchange_id = uuid4(), uuid4()
    operation_aad = PayloadCipher.aad(operation_id, "tenant-a", "qwen3-8b", "request")
    debug_aad = PostgresDebugStore._aad(exchange_id, "tenant-a", "qwen3-8b")
    operation = before.encrypt(b"preexisting operation", aad=operation_aad)
    debug = before.encrypt(b"preexisting request debug", aad=debug_aad)

    assert after.decrypt(operation, aad=operation_aad) == b"preexisting operation"
    assert after.decrypt(debug, aad=debug_aad) == b"preexisting request debug"
    assert after.encrypt(b"new", aad=operation_aad).key_id == "payload-v2"


def test_payload_rotation_fails_closed_if_generation_one_is_removed() -> None:
    aad = b"fs2.user-storage/v1\0tenant-a\0principal-a"
    before = PayloadCipher(active_key_id="payload-v1", keys={"payload-v1": b"o" * 32})
    existing = before.encrypt(b"preexisting customer storage secret", aad=aad)
    with pytest.raises(ValueError, match="key id is not available"):
        PayloadCipher(active_key_id="payload-v2", keys={"payload-v2": b"n" * 32}).decrypt(existing, aad=aad)


def test_ledger_rotation_keeps_replay_identity_and_uses_new_key_for_new_rows() -> None:
    existing = b"preexisting-ledger-input"
    before = KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"o" * 32})
    old_id, old_digest = before.digest(existing, context="operation-idempotency")
    after = KeyedHasher(
        active_key_id="ledger-v2",
        keys={"ledger-v1": b"o" * 32, "ledger-v2": b"n" * 32},
    )

    assert after.digest_for(old_id, existing, context="operation-idempotency") == old_digest
    new_id, new_digest = after.digest(existing, context="operation-idempotency")
    assert new_id == "ledger-v2"
    assert new_digest != old_digest
    assert (old_id, old_digest) in after.candidate_digests(existing, context="operation-idempotency")
