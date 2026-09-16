from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag

from fs2_serve.auth import AuthenticationError, PepperRing, TokenService
from fs2_serve.crypto import CustomerStorageCrypto, KeyedHasher, PayloadCipher
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
    aad = PayloadCipher.customer_storage_aad("tenant-a", "principal-a")
    before = PayloadCipher(active_key_id="payload-v1", keys={"payload-v1": b"o" * 32})
    existing = before.encrypt(b"preexisting customer storage secret", aad=aad)
    with pytest.raises(ValueError, match="key id is not available"):
        PayloadCipher(active_key_id="payload-v2", keys={"payload-v2": b"n" * 32}).decrypt(existing, aad=aad)


def test_storage_rotation_reads_preexisting_ciphertext_writes_current_and_rolls_back() -> None:
    aad = PayloadCipher.customer_storage_aad("tenant-a", "principal-a")
    keys = {
        "payload-v1": b"p" * 32,
        "storage-v1": b"o" * 32,
        "storage-v2": b"n" * 32,
    }
    legacy = PayloadCipher(active_key_id="payload-v1", keys={"payload-v1": keys["payload-v1"]})
    preexisting = legacy.encrypt(b"preexisting customer storage secret", aad=aad)
    generation_one = PayloadCipher(
        active_key_id="storage-v1",
        keys={key_id: keys[key_id] for key_id in ("payload-v1", "storage-v1")},
    )
    assert generation_one.decrypt(preexisting, aad=aad) == b"preexisting customer storage secret"
    existing = generation_one.encrypt(b"generation one secret", aad=aad)
    rotating = PayloadCipher(
        active_key_id="storage-v2",
        keys=keys,
    )

    assert rotating.decrypt(preexisting, aad=aad) == b"preexisting customer storage secret"
    assert rotating.decrypt(existing, aad=aad) == b"generation one secret"
    replacement = rotating.encrypt(b"replacement", aad=aad)
    assert replacement.key_id == "storage-v2"

    # Application rollback changes the current writer only. It retains both
    # generations so rows written before and after rotation remain readable.
    rolled_back = PayloadCipher(active_key_id="storage-v1", keys=keys)
    assert rolled_back.decrypt(preexisting, aad=aad) == b"preexisting customer storage secret"
    assert rolled_back.decrypt(existing, aad=aad) == b"generation one secret"
    assert rolled_back.decrypt(replacement, aad=aad) == b"replacement"
    rollback_write = rolled_back.encrypt(b"rollback write", aad=aad)
    assert rollback_write.key_id == "storage-v1"

    rolled_forward = PayloadCipher(active_key_id="storage-v2", keys=keys)
    assert rolled_forward.decrypt(rollback_write, aad=aad) == b"rollback write"


def test_runtime_customer_storage_boundary_uses_canonical_aad_for_old_new_and_rollback() -> None:
    keys = {"storage-v1": b"o" * 32, "storage-v2": b"n" * 32}
    names = {"storage-name-v1": b"a" * 32, "storage-name-v2": b"b" * 32}
    before = CustomerStorageCrypto(
        PayloadCipher(active_key_id="storage-v1", keys={"storage-v1": keys["storage-v1"]}),
        KeyedHasher(
            active_key_id="storage-name-v1",
            keys={"storage-name-v1": names["storage-name-v1"]},
        ),
    )
    existing = before.encrypt_secret(
        b"preexisting customer credential",
        tenant_id="tenant-a",
        principal_id="principal-a",
    )
    rotating = CustomerStorageCrypto(
        PayloadCipher(active_key_id="storage-v2", keys=keys),
        KeyedHasher(active_key_id="storage-name-v2", keys=names),
    )
    assert rotating.decrypt_secret(
        existing, tenant_id="tenant-a", principal_id="principal-a"
    ) == b"preexisting customer credential"
    migrated = rotating.migrate_secret(
        existing, tenant_id="tenant-a", principal_id="principal-a"
    )
    assert migrated.key_id == "storage-v2"
    rollback = CustomerStorageCrypto(
        PayloadCipher(active_key_id="storage-v1", keys=keys),
        KeyedHasher(active_key_id="storage-name-v1", keys=names),
    )
    assert rollback.decrypt_secret(
        migrated, tenant_id="tenant-a", principal_id="principal-a"
    ) == b"preexisting customer credential"
    with pytest.raises(InvalidTag):
        rotating.decrypt_secret(
            existing, tenant_id="tenant-b", principal_id="principal-a"
        )


def test_customer_storage_aad_is_exact_stable_and_identity_bound() -> None:
    aad = PayloadCipher.customer_storage_aad("tenant-a", "principal-a")
    assert aad == b"fs2.user-storage/v1\0tenant-a\0principal-a"

    cipher = PayloadCipher(active_key_id="payload-v1", keys={"payload-v1": b"o" * 32})
    envelope = cipher.encrypt(b"customer storage secret", aad=aad)
    assert cipher.decrypt(envelope, aad=aad) == b"customer storage secret"
    for other_aad in (
        PayloadCipher.customer_storage_aad("tenant-b", "principal-a"),
        PayloadCipher.customer_storage_aad("tenant-a", "principal-b"),
        PayloadCipher.customer_storage_aad("principal-a", "tenant-a"),
    ):
        with pytest.raises(InvalidTag):
            cipher.decrypt(envelope, aad=other_aad)


@pytest.mark.parametrize(
    ("tenant_id", "principal_id"),
    [
        ("", "principal-a"),
        ("tenant-a", ""),
        ("tenant-a\0other", "principal-a"),
        ("tenant-a", "principal-a\0other"),
        (None, "principal-a"),
        ("tenant-a", None),
    ],
)
def test_customer_storage_aad_rejects_ambiguous_identities(tenant_id: str | None, principal_id: str | None) -> None:
    with pytest.raises(ValueError, match="non-empty and NUL-free"):
        PayloadCipher.customer_storage_aad(tenant_id, principal_id)  # type: ignore[arg-type]


def test_customer_storage_aad_domain_is_declared_only_by_the_reviewed_helper() -> None:
    source_root = Path(__file__).parents[1] / "src" / "fs2_serve"
    duplicate_domains = [
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if path.name != "crypto.py" and "fs2.user-storage/v1" in path.read_text(encoding="utf-8")
    ]
    assert duplicate_domains == []


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


def test_storage_name_rotation_keeps_old_names_and_supports_forward_only_rollback() -> None:
    identity = b"project-a\0tenant-a\0principal-a"
    context = "fs2.user-storage-bucket/v1"
    keys = {"storage-name-v1": b"o" * 32, "storage-name-v2": b"n" * 32}
    generation_one = KeyedHasher(active_key_id="storage-name-v1", keys={"storage-name-v1": keys["storage-name-v1"]})
    old_id, old_digest = generation_one.digest(identity, context=context)

    generation_two = KeyedHasher(active_key_id="storage-name-v2", keys=keys)
    new_id, new_digest = generation_two.digest(identity, context=context)
    assert new_id == "storage-name-v2"
    assert new_digest != old_digest
    assert (old_id, old_digest) in generation_two.candidate_digests(identity, context=context)

    rollback_bundle = KeyedHasher(active_key_id="storage-name-v1", keys=keys)
    assert rollback_bundle.digest(identity, context=context) == (old_id, old_digest)
    assert (new_id, new_digest) in rollback_bundle.candidate_digests(identity, context=context)


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
