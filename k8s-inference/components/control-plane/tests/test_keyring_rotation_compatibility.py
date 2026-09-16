from __future__ import annotations

from uuid import uuid4

import pytest

from fs2_serve.auth import AuthenticationError, PepperRing, TokenService
from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.request_debug import PostgresDebugStore

OLD_BOOTSTRAP_PAT = "fs2_pat_1234567890abcdef1234567890abcdef_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUV"
NEW_BOOTSTRAP_PAT = "fs2_pat_fedcba0987654321fedcba0987654321_ZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvutsrqponmlkjihgf"


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


@pytest.mark.asyncio
async def test_bootstrap_pat_overlap_preserves_new_token_after_old_revocation(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)

    def request(principal: str) -> TokenCreate:
        return TokenCreate(
            principal_id=principal,
            tenant_id="tenant-a",
            scopes={Scope.CATALOG_READ, Scope.INFERENCE_INVOKE, Scope.MCP_INVOKE},
            models={"qwen3-8b"},
        )

    old_service = TokenService(
        store,
        PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"o" * 32}),
    )
    old = await old_service.ensure_provisioned(
        OLD_BOOTSTRAP_PAT,
        request("terraform-bootstrap-v1"),
        created_by="terraform-bootstrap",
    )
    rotating = TokenService(
        store,
        PepperRing(
            active_key_id="pepper-v2",
            keys={"pepper-v1": b"o" * 32, "pepper-v2": b"n" * 32},
        ),
    )
    new = await rotating.ensure_provisioned(
        NEW_BOOTSTRAP_PAT,
        request("terraform-bootstrap-v2"),
        created_by="terraform-bootstrap",
    )

    assert (await rotating.verify(OLD_BOOTSTRAP_PAT)).token_id == old.id
    assert (await rotating.verify(NEW_BOOTSTRAP_PAT)).token_id == new.id
    await store.revoke_token(old.id, actor="terraform-bootstrap-rotation")
    with pytest.raises(AuthenticationError):
        await rotating.verify(OLD_BOOTSTRAP_PAT)
    assert (await rotating.verify(NEW_BOOTSTRAP_PAT)).token_id == new.id
