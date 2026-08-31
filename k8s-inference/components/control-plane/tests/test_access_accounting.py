from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fs2_serve.access import AdminAccessService
from fs2_serve.access_models import (
    BOOTSTRAP_OPERATOR_PRINCIPAL_ID,
    OperatorPrincipalCreate,
    OperatorPrincipalPatch,
    OperatorRole,
    PrincipalKind,
)
from fs2_serve.auth import AuthenticationError, OperatorSessionService, PepperRing, TokenService
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import (
    AdmissionRequest,
    ModalityUsage,
    OperationStatus,
    ReportedUsage,
    RuntimeIdentity,
    Scope,
    TokenCreate,
    UsageDirection,
)
from fs2_serve.store import NotFoundError, RateLimitExceededError


def token_request(*, name: str = "agent key", rate_limit: int | None = None) -> TokenCreate:
    return TokenCreate(
        principal_id="agent-user",
        tenant_id="tenant-a",
        scopes={Scope.CATALOG_READ, Scope.INFERENCE_INVOKE, Scope.MCP_INVOKE},
        models={"qwen3-8b"},
        request_budget=20,
        gpu_seconds_budget=100,
        max_concurrency=4,
        name=name,
        rate_limit_requests=rate_limit,
        rate_window_seconds=60 if rate_limit is not None else None,
    )


def admission(key: str) -> AdmissionRequest:
    return AdmissionRequest(
        model_id="qwen3-8b",
        operation="chat",
        protocol="openai-chat",
        idempotency_key=key,
        request_body=b'{"messages":[]}',
    )


@pytest.mark.asyncio
async def test_operator_principal_roles_and_tenant_projection_are_bounded(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tenant_principal = await store.create_operator_principal(
        principal_id=uuid4(),
        request=OperatorPrincipalCreate(
            subject="human@example.test",
            display_name="Human operator",
            kind=PrincipalKind.HUMAN,
            role=OperatorRole.OPERATOR,
            tenant_id="tenant-a",
        ),
        actor="bootstrap-admin",
    )
    other = await store.create_operator_principal(
        principal_id=uuid4(),
        request=OperatorPrincipalCreate(
            subject="other@example.test",
            display_name="Other viewer",
            kind=PrincipalKind.HUMAN,
            role=OperatorRole.VIEWER,
            tenant_id="tenant-b",
        ),
        actor="bootstrap-admin",
    )

    tenant_principal.require(OperatorRole.VIEWER, tenant_id="tenant-a")
    tenant_principal.require(OperatorRole.OPERATOR, tenant_id="tenant-a")
    with pytest.raises(PermissionError):
        tenant_principal.require(OperatorRole.ADMIN, tenant_id="tenant-a")
    with pytest.raises(PermissionError):
        tenant_principal.require(OperatorRole.VIEWER, tenant_id="tenant-b")

    visible = await store.list_operator_principals(tenant_id="tenant-a", include_global=False, limit=20)
    assert [item.id for item in visible] == [tenant_principal.id]
    assert other.id not in {item.id for item in visible}
    with_global = await store.list_operator_principals(tenant_id="tenant-a", include_global=True, limit=20)
    assert {item.id for item in with_global} == {tenant_principal.id, BOOTSTRAP_OPERATOR_PRINCIPAL_ID}

    disabled = await store.update_operator_principal(
        tenant_principal.id,
        request=OperatorPrincipalPatch(enabled=False),
        actor="bootstrap-admin",
    )
    assert not disabled.enabled and disabled.disabled_at is not None
    with pytest.raises(ValidationError):
        OperatorPrincipalPatch()


@pytest.mark.asyncio
async def test_operator_session_is_domain_separated_opaque_and_replay_fenced(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    peppers = PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32})
    with pytest.raises(ValueError, match="TTL is outside"):
        OperatorSessionService(store, peppers, ttl_seconds=299)
    assert OperatorSessionService(store, peppers, ttl_seconds=300).ttl_seconds == 300
    sessions = OperatorSessionService(
        store,
        peppers,
        ttl_seconds=600,
    )
    attacker_cookie = "fs2_admin_00000000000000000000000000000000_fixed-session-value-must-not-survive"
    first = await sessions.issue_bootstrap()
    second = await sessions.issue_bootstrap()
    assert first.cookie_value != second.cookie_value != attacker_cookie
    assert first.cookie_value not in repr(store.operator_sessions)
    stored = store.operator_sessions[first.session.id]
    assert len(stored.digest) == 64 and stored.digest not in first.cookie_value
    assert stored.pepper_key_id == "pepper-v1"
    assert (await sessions.verify(first.cookie_value)).principal.role is OperatorRole.ADMIN

    tampered = first.cookie_value[:-1] + ("A" if first.cookie_value[-1] != "A" else "B")
    with pytest.raises(AuthenticationError, match="invalid operator session"):
        await sessions.verify(tampered)
    replacement = await sessions.replace(first.cookie_value)
    with pytest.raises(AuthenticationError, match="invalid operator session"):
        await sessions.verify(first.cookie_value)
    assert (await sessions.verify(replacement.cookie_value)).principal.role is OperatorRole.ADMIN
    with pytest.raises(NotFoundError, match="operator principal not found"):
        await sessions.replace(replacement.cookie_value, principal_id=uuid4())
    assert (await sessions.verify(replacement.cookie_value)).principal.role is OperatorRole.ADMIN
    await sessions.revoke(replacement.cookie_value, actor="bootstrap-admin")

    expired_record = store.operator_sessions[second.session.id]
    store.operator_sessions[second.session.id] = expired_record.model_copy(
        update={
            "session": expired_record.session.model_copy(
                update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
            )
        }
    )
    with pytest.raises(AuthenticationError, match="invalid operator session"):
        await sessions.verify(second.cookie_value)


@pytest.mark.asyncio
async def test_key_fingerprint_last_use_rate_window_and_atomic_rotation(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tokens = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32}))
    issued = await tokens.issue(token_request(rate_limit=2), created_by="operator-a")
    assert issued.name == "agent key"
    assert issued.fingerprint is not None and len(issued.fingerprint) == 64
    assert issued.token not in repr(store.tokens)

    principal = await tokens.verify(issued.token)
    verified = await store.token_for_verification(issued.id)
    assert verified is not None and verified[0].last_used_at is None

    first = await store.append_operation(
        principal=principal,
        admission=admission("rate-key-0001"),
        model_revision="revision-a",
        reserved_gpu_seconds=1,
        max_attempts=2,
    )
    admitted = await store.token_for_verification(issued.id)
    assert admitted is not None and admitted[0].last_used_at is not None
    replay = await store.append_operation(
        principal=principal,
        admission=admission("rate-key-0001"),
        model_revision="revision-a",
        reserved_gpu_seconds=1,
        max_attempts=2,
    )
    assert replay.id == first.id and replay.reused
    await store.append_operation(
        principal=principal,
        admission=admission("rate-key-0002"),
        model_revision="revision-a",
        reserved_gpu_seconds=1,
        max_attempts=2,
    )
    with pytest.raises(RateLimitExceededError):
        await store.append_operation(
            principal=principal,
            admission=admission("rate-key-0003"),
            model_revision="revision-a",
            reserved_gpu_seconds=1,
            max_attempts=2,
        )

    with pytest.raises(ValueError, match="future"):
        await tokens.rotate(
            issued.id,
            actor="operator-a",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    still_active = await store.token_for_verification(issued.id)
    assert still_active is not None and still_active[0].revoked_at is None

    rotated = await tokens.rotate(issued.id, actor="operator-a", name="rotated agent key")
    predecessor = await store.token_for_verification(issued.id)
    assert predecessor is not None and predecessor[0].revoked_at is not None
    assert predecessor[0].rotated_at is not None
    assert rotated.rotation_parent_id == issued.id
    assert rotated.name == "rotated agent key"
    assert rotated.requests_used == 2 and rotated.rate_window_requests == 2
    assert all(store.operations[item].view.status is OperationStatus.CANCELLED for item in (first.id, replay.id))
    with pytest.raises(AuthenticationError):
        await tokens.verify(issued.token)
    assert (await tokens.verify(rotated.token)).token_id == rotated.id

    audits = await store.list_audit(tenant_id="tenant-a", limit=100)
    rotation = next(item for item in audits if item.action == "token.rotate")
    assert issued.token not in rotation.model_dump_json() and rotated.token not in rotation.model_dump_json()


@pytest.mark.asyncio
async def test_runtime_reported_units_preserve_zero_and_unavailable(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tokens = TokenService(store, PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32}))
    issued = await tokens.issue(token_request(), created_by="operator-a")
    principal = await tokens.verify(issued.token)
    accepted = await store.append_operation(
        principal=principal,
        admission=admission("usage-key-0001"),
        model_revision="revision-a",
        reserved_gpu_seconds=1,
        max_attempts=2,
    )
    claimed = await store.claim_operation("worker-a", lease_seconds=30)
    assert claimed is not None and claimed.id == accepted.id
    await store.mark_running(
        claimed.id,
        RuntimeIdentity(gpu_count=1),
        worker_id=claimed.worker_id,
        fencing_token=claimed.fencing_token,
    )
    usage = ReportedUsage(
        input_tokens=0,
        output_tokens=7,
        modalities=[
            ModalityUsage(
                modality="image",
                direction=UsageDirection.INPUT,
                unit="image",
                amount=1,
            )
        ],
    )
    completed = await store.complete_operation(
        claimed.id,
        status=OperationStatus.SUCCEEDED,
        outcome="succeeded",
        semantic_outcome="protocol_valid",
        http_status=200,
        response_body=b"{}",
        response_content_type="application/json",
        error_code=None,
        error_detail=None,
        runtime=RuntimeIdentity(gpu_count=1),
        worker_id=claimed.worker_id,
        fencing_token=claimed.fencing_token,
        usage=usage,
    )
    assert completed.input_tokens == 0
    assert completed.output_tokens == 7
    assert completed.modality_usage == usage.modalities

    second = await store.append_operation(
        principal=principal,
        admission=admission("usage-key-0002"),
        model_revision="revision-a",
        reserved_gpu_seconds=1,
        max_attempts=2,
    )
    assert second.input_tokens is None and second.output_tokens is None and second.modality_usage == []
    second_claim = await store.claim_operation("worker-b", lease_seconds=30)
    assert second_claim is not None and second_claim.id == second.id
    await store.mark_running(
        second_claim.id,
        RuntimeIdentity(gpu_count=1),
        worker_id=second_claim.worker_id,
        fencing_token=second_claim.fencing_token,
    )
    token_only = await store.complete_operation(
        second_claim.id,
        status=OperationStatus.SUCCEEDED,
        outcome="succeeded",
        semantic_outcome="protocol_valid",
        http_status=200,
        response_body=b"{}",
        response_content_type="application/json",
        error_code=None,
        error_detail=None,
        runtime=RuntimeIdentity(gpu_count=1),
        worker_id=second_claim.worker_id,
        fencing_token=second_claim.fencing_token,
        usage=ReportedUsage(input_tokens=2, output_tokens=3),
    )
    assert token_only.modality_usage == []
    assert token_only.modality_usage_reported is False
    usage_rows = await store.admin_key_usage((issued.id,), tenant_id="tenant-a")
    assert usage_rows[0].terminal_operations == 2
    assert usage_rows[0].modality_reported_operations == 1
    bootstrap = await store.get_operator_principal(BOOTSTRAP_OPERATOR_PRINCIPAL_ID)
    projected = await AdminAccessService(store, tokens).list_keys(
        bootstrap,
        tenant_id="tenant-a",
        limit=20,
    )
    assert projected.items[0].usage.modality_state.value == "unavailable"
    assert projected.items[0].usage.modality_units == []
    with pytest.raises(ValidationError):
        ReportedUsage(
            modalities=[
                ModalityUsage(modality="image", direction=UsageDirection.INPUT, unit="image", amount=1),
                ModalityUsage(modality="image", direction=UsageDirection.INPUT, unit="image", amount=2),
            ]
        )


@pytest.mark.asyncio
async def test_failed_action_can_commit_redacted_audit_after_rollback(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    rejected_secret = "fs2_pat_rejected_secret_must_not_persist"
    with pytest.raises(NotFoundError):
        await store.rotate_token(
            uuid4(),
            token_id=uuid4(),
            prefix="fs2_pat_missing",
            pepper_key_id="pepper-v1",
            digest="not-a-raw-secret",
            fingerprint="a" * 64,
            name="missing",
            expires_at=None,
            actor="operator-a",
        )
    await store.append_audit_event(
        actor="operator-a",
        tenant_id="tenant-a",
        token_id=None,
        action="token.rotate",
        target_type="token",
        target_id="unresolved",
        outcome="failed",
        detail={"reason": "not_found"},
    )
    audits = await store.list_audit(tenant_id="tenant-a", limit=20)
    assert audits[0].outcome == "failed"
    assert rejected_secret not in audits[0].model_dump_json()
