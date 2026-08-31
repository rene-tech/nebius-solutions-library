from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from fs2_serve.auth import AuthenticationError, PepperRing, TokenService
from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import (
    AdmissionRequest,
    OperationStatus,
    Principal,
    RuntimeIdentity,
    Scope,
    TokenCreate,
)
from fs2_serve.runtime import sanitize_error_detail
from fs2_serve.store import ConflictError, StaleLeaseError

PROMPT = b"RAW_PROMPT_NEVER_PERSIST_8f58c67e"
RESPONSE = b'{"choices":[{"message":{"content":"RAW_RESPONSE_NEVER_PERSIST_39c8aa"}}]}'
BEARER_SENTINEL = "fs2_pat_00000000000000000000000000000000_RAW_BEARER_NEVER_PERSIST"


async def add_token(
    store: MemoryStore,
    *,
    token_id=None,
    principal_id: str = "user-a",
    tenant_id: str = "tenant-a",
    max_concurrency: int = 4,
) -> Principal:
    token_id = token_id or uuid4()
    await store.issue_token(
        token_id=token_id,
        prefix=f"fs2_pat_{token_id.hex[:12]}",
        pepper_key_id="pepper-v1",
        digest="test-only-digest",
        request=TokenCreate(
            principal_id=principal_id,
            tenant_id=tenant_id,
            scopes={
                Scope.CATALOG_READ,
                Scope.INFERENCE_INVOKE,
                Scope.OPERATIONS_READ,
                Scope.OPERATIONS_RESULT,
                Scope.OPERATIONS_CANCEL,
                Scope.OPERATIONS_ACKNOWLEDGE,
            },
            models={"qwen3-8b"},
            gpu_seconds_budget=1000,
            max_concurrency=max_concurrency,
        ),
        created_by="bootstrap-admin",
    )
    return Principal(
        token_id=token_id,
        token_prefix=f"fs2_pat_{token_id.hex[:12]}",
        principal_id=principal_id,
        tenant_id=tenant_id,
        scopes=frozenset(
            {
                "catalog.read",
                "inference.invoke",
                "operations.read",
                "operations.result",
                "operations.cancel",
                "operations.acknowledge",
            }
        ),
        models=frozenset({"qwen3-8b"}),
        gpu_seconds_budget=1000,
        max_concurrency=max_concurrency,
    )


def admission(principal: Principal, *, key: str = "stable-key-0001", body: bytes = PROMPT) -> AdmissionRequest:
    del principal
    return AdmissionRequest(
        model_id="qwen3-8b",
        model_revision="ignored",  # type: ignore[call-arg]
        operation="chat",
        protocol="openai-chat",
        idempotency_key=key,
        request_body=body,
        request_content_type="application/json",
    )


@pytest.mark.asyncio
async def test_encrypted_queue_and_metadata_never_persist_raw_payloads(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    principal = await add_token(store)
    accepted = await store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="qwen3-8b",
            operation="chat",
            protocol="openai-chat",
            idempotency_key="privacy-key-0001",
            request_body=PROMPT,
        ),
        model_revision="b968826d",
        reserved_gpu_seconds=10,
        max_attempts=2,
    )
    row = store.operations[accepted.id]
    assert row.request is not None and row.request.nonce and len(row.request.nonce) == 12
    assert PROMPT not in row.request.value
    assert PROMPT.decode() not in repr(store.operations)
    assert PROMPT.decode() not in json.dumps(accepted.model_dump(mode="json"))
    assert len(row.request_hmac) == 64 and row.request_hmac != PROMPT.decode()

    claimed = await store.claim_operation("worker-1", lease_seconds=30)
    assert claimed is not None
    await store.mark_ready(claimed.id, worker_id=claimed.worker_id, fencing_token=claimed.fencing_token)
    runtime = RuntimeIdentity(pod_uid="pod-1", node_uid="node-1", gpu_count=1, preemptible=True)
    await store.mark_running(
        claimed.id,
        runtime,
        worker_id=claimed.worker_id,
        fencing_token=claimed.fencing_token,
    )
    final = await store.complete_operation(
        claimed.id,
        status=OperationStatus.SUCCEEDED,
        outcome="succeeded",
        semantic_outcome="protocol_valid",
        http_status=200,
        response_body=RESPONSE,
        response_content_type="application/json",
        error_code=None,
        error_detail=f"{PROMPT.decode()} {BEARER_SENTINEL}",
        runtime=runtime,
        worker_id=claimed.worker_id,
        fencing_token=claimed.fencing_token,
    )
    assert RESPONSE.decode() not in final.model_dump_json()
    assert PROMPT.decode() not in final.model_dump_json()
    assert BEARER_SENTINEL not in final.model_dump_json()
    assert final.error_detail == "runtime operation failed"

    first = await store.get_operation_result(final.id, tenant_id=principal.tenant_id)
    second = await store.get_operation_result(final.id, tenant_id=principal.tenant_id)
    assert first.result == second.result
    metadata = await store.cancel_operation(final.id, tenant_id=principal.tenant_id, actor="user-a")
    assert RESPONSE.decode() not in metadata.model_dump_json()
    await store.purge_operation_payload(final.id, tenant_id=principal.tenant_id)
    with pytest.raises(ConflictError, match="unavailable"):
        await store.get_operation_result(final.id, tenant_id=principal.tenant_id)


@pytest.mark.asyncio
async def test_idempotency_is_token_principal_and_full_request_scoped_across_hmac_rotation(
    cipher: PayloadCipher,
) -> None:
    old = b"o" * 32
    new = b"n" * 32
    store = MemoryStore(cipher, KeyedHasher(active_key_id="old", keys={"old": old}))
    first_principal = await add_token(store, principal_id="same-user")
    second_principal = await add_token(store, principal_id="same-user")
    request = AdmissionRequest(
        model_id="qwen3-8b",
        operation="chat",
        protocol="openai-chat",
        idempotency_key="rotation-key-0001",
        request_body=PROMPT,
    )
    first = await store.append_operation(
        principal=first_principal,
        admission=request,
        model_revision="revision-a",
        reserved_gpu_seconds=10,
        max_attempts=2,
    )
    separate = await store.append_operation(
        principal=second_principal,
        admission=request,
        model_revision="revision-a",
        reserved_gpu_seconds=10,
        max_attempts=2,
    )
    assert separate.id != first.id

    store.hasher = KeyedHasher(active_key_id="new", keys={"old": old, "new": new})
    replay = await store.append_operation(
        principal=first_principal,
        admission=request,
        model_revision="revision-a",
        reserved_gpu_seconds=10,
        max_attempts=2,
    )
    assert replay.id == first.id and replay.reused

    changed = request.model_copy(update={"protocol": "native"})
    with pytest.raises(ConflictError, match="different request"):
        await store.append_operation(
            principal=first_principal,
            admission=changed,
            model_revision="revision-a",
            reserved_gpu_seconds=10,
            max_attempts=2,
        )

    store.hasher = KeyedHasher(active_key_id="new", keys={"new": new})
    with pytest.raises(ConflictError, match="key is unavailable"):
        await store.append_operation(
            principal=first_principal,
            admission=request,
            model_revision="revision-a",
            reserved_gpu_seconds=10,
            max_attempts=2,
        )


@pytest.mark.asyncio
async def test_pat_pepper_rotation_rehashes_then_old_pepper_can_retire(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    old_ring = PepperRing(active_key_id="old", keys={"old": b"o" * 32})
    old_service = TokenService(store, old_ring)
    issued = await old_service.issue(
        TokenCreate(
            principal_id="rotation-user",
            tenant_id="tenant-a",
            scopes={Scope.CATALOG_READ},
            models={"qwen3-8b"},
        ),
        created_by="bootstrap-admin",
    )
    rotating = TokenService(
        store,
        PepperRing(active_key_id="new", keys={"old": b"o" * 32, "new": b"n" * 32}),
    )
    assert (await rotating.verify(issued.token)).token_id == issued.id
    stored = await store.token_for_verification(issued.id)
    assert stored is not None and stored[0].pepper_key_id == "new"
    assert (
        await TokenService(store, PepperRing(active_key_id="new", keys={"new": b"n" * 32})).verify(issued.token)
    ).token_id == issued.id


@pytest.mark.asyncio
async def test_revocation_fences_active_work_and_releases_only_unclaimed_reservation(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    principal = await add_token(store)
    admitted = await store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="qwen3-8b",
            operation="chat",
            protocol="openai-chat",
            idempotency_key="revoke-key-0001",
            request_body=PROMPT,
        ),
        model_revision="revision-a",
        reserved_gpu_seconds=10,
        max_attempts=2,
    )
    claimed = await store.claim_operation("worker-a", lease_seconds=30)
    assert claimed is not None and claimed.id == admitted.id
    before = (await store.token_for_verification(principal.token_id))[0]  # type: ignore[index]
    assert before.gpu_seconds_used == 5 and before.gpu_seconds_reserved == 5
    revoked = await store.revoke_token(principal.token_id, actor="bootstrap-admin")
    assert revoked.gpu_seconds_used == 5 and revoked.gpu_seconds_reserved == 0
    metadata = await store.get_operation(admitted.id, tenant_id=principal.tenant_id)
    assert metadata.status == OperationStatus.CANCELLED
    with pytest.raises(StaleLeaseError):
        await store.complete_operation(
            admitted.id,
            status=OperationStatus.SUCCEEDED,
            outcome="succeeded",
            semantic_outcome="protocol_valid",
            http_status=200,
            response_body=RESPONSE,
            response_content_type="application/json",
            error_code=None,
            error_detail=None,
            runtime=RuntimeIdentity(),
            worker_id=claimed.worker_id,
            fencing_token=claimed.fencing_token,
        )
    with pytest.raises(AuthenticationError):
        # This deliberately malformed token also proves bounded, generic auth failure.
        await TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32})).verify(
            BEARER_SENTINEL
        )


@pytest.mark.asyncio
async def test_operation_idempotency_and_pat_rows_have_bounded_deletion(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    principal = await add_token(store)
    accepted = await store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="qwen3-8b",
            operation="chat",
            protocol="openai-chat",
            idempotency_key="retention-key-0001",
            request_body=PROMPT,
            deadline_at=datetime.now(UTC) - timedelta(seconds=1),
        ),
        model_revision="revision-a",
        reserved_gpu_seconds=10,
        max_attempts=2,
    )
    assert await store.expire_deadline_operations() == 1
    assert (await store.get_operation(accepted.id)).error_code == "deadline_exceeded"
    assert (await store.list_tokens(tenant_id=principal.tenant_id))[0].gpu_seconds_reserved == 0
    store.operations[accepted.id].view = store.operations[accepted.id].view.model_copy(
        update={"completed_at": datetime.now(UTC) - timedelta(seconds=2)}
    )
    await store.revoke_token(principal.token_id, actor="bootstrap-admin")
    store.tokens[principal.token_id].view = store.tokens[principal.token_id].view.model_copy(
        update={"revoked_at": datetime.now(UTC) - timedelta(seconds=2)}
    )
    deleted = await store.delete_expired_rows(
        operation_retention_seconds=1,
        token_retention_seconds=1,
    )
    assert deleted == {"operations": 1, "tokens": 1, "audit": 0, "usage": 0}
    assert not store.operations and not store.idempotency and not store.tokens


@pytest.mark.asyncio
async def test_last_attempt_release_is_terminal_and_preserves_conservative_charges(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    principal = await add_token(store)
    accepted = await store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="qwen3-8b",
            operation="chat",
            protocol="openai-chat",
            idempotency_key="memory-last-attempt-0001",
            request_body=PROMPT,
        ),
        model_revision="revision-a",
        reserved_gpu_seconds=10,
        max_attempts=2,
    )
    first = await store.claim_operation("rolling-pod-old", lease_seconds=30)
    assert first is not None and first.id == accepted.id and first.attempt == 1
    assert (
        await store.release_operation(
            first.id,
            worker_id=first.worker_id,
            fencing_token=first.fencing_token,
        )
    ).status == OperationStatus.QUEUED

    last = await store.claim_operation("rolling-pod-new", lease_seconds=30)
    assert last is not None and last.attempt == last.max_attempts == 2
    terminal = await store.release_operation(
        last.id,
        worker_id=last.worker_id,
        fencing_token=last.fencing_token,
    )
    assert terminal.status == OperationStatus.EXPIRED
    assert terminal.error_code == "attempts_exhausted"
    assert terminal.estimated_gpu_seconds == 10 and terminal.reserved_gpu_seconds == 0
    assert await store.claim_operation("forbidden-n-plus-one", lease_seconds=30) is None
    token = (await store.token_for_verification(principal.token_id))[0]  # type: ignore[index]
    assert token.gpu_seconds_used == 10 and token.gpu_seconds_reserved == 0


def test_error_sanitizer_never_preserves_untrusted_text() -> None:
    detail = sanitize_error_detail(f"{PROMPT.decode()} {RESPONSE.decode()} {BEARER_SENTINEL}")
    assert detail == "runtime operation failed"
    assert all(value not in detail for value in (PROMPT.decode(), RESPONSE.decode(), BEARER_SENTINEL))
