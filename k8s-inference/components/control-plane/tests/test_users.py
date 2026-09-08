from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from fs2_serve.access import AdminAccessService
from fs2_serve.access_models import AdminApiKeyCreate, OperatorPrincipal, OperatorRole, PrincipalKind
from fs2_serve.admin_models import AdminContext
from fs2_serve.auth import PepperRing, TokenService
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import OperationStatus, OperationView, Scope, TokenCreate
from fs2_serve.store import ConflictError, NotFoundError
from fs2_serve.user_models import UserAppChoice, UserCreate, UserPatch, owner_id
from fs2_serve.user_repository import MemoryUserRepository, usage_from_counts
from fs2_serve.users import UserService


@pytest.fixture
def user_env(cipher, hasher):
    store = MemoryStore(cipher, hasher)
    tokens = TokenService(store, PepperRing(active_key_id="test", keys={"test": b"t" * 32}))
    access = AdminAccessService(store, tokens)
    repository = MemoryUserRepository(store)
    apps = [
        UserAppChoice(app_id=uuid4(), display_name="App A", public_model_id="qwen3-8b", academic_required=False),
        UserAppChoice(
            app_id=uuid4(), display_name="Independent Qwen", public_model_id="app-second", academic_required=True
        ),
    ]

    async def catalog():
        return apps

    users = UserService(repository, access, app_catalog=catalog)
    tokens.principal_policy = users.constrain_principal
    now = datetime.now(UTC)
    operator = OperatorPrincipal(
        id=uuid4(),
        subject="key-issuer",
        display_name="Operator",
        kind=PrincipalKind.HUMAN,
        role=OperatorRole.ADMIN,
        enabled=True,
        created_at=now,
        created_by="bootstrap",
        updated_at=now,
    )
    context = AdminContext(from_at=now - timedelta(hours=1), to_at=now + timedelta(hours=1), timezone="UTC")
    return SimpleNamespace(
        store=store, tokens=tokens, repository=repository, users=users, operator=operator, context=context, apps=apps
    )


def key_request(principal="researcher", tenant="tenant-a", models=None):
    return TokenCreate(
        principal_id=principal,
        tenant_id=tenant,
        models=models or {"*"},
        scopes={Scope.INFERENCE_INVOKE, Scope.MCP_INVOKE, Scope.OPERATIONS_READ, Scope.OPERATIONS_RESULT},
    )


@pytest.mark.asyncio
async def test_legacy_owner_discovery_is_read_only_and_preserves_policy(user_env):
    env = user_env
    issued = await env.tokens.issue(key_request(), created_by="key-issuer")
    principal = await env.tokens.verify(issued.token)
    result = await env.users.list(env.operator, env.context)
    row = result.items[0]
    assert row.id == owner_id("tenant-a", "researcher")
    assert row.principal_id == "researcher" and row.principal_id != "key-issuer"
    assert row.kind is None and row.academic_eligible is None and row.app_ids is None
    assert row.key_count == 1 and row.active_key_count == 1
    assert not env.repository.users
    assert principal.models == frozenset({"*"})


@pytest.mark.asyncio
async def test_user_disable_applies_to_all_keys_but_not_existing_result_reads(user_env):
    env = user_env
    first = await env.tokens.issue(key_request(), created_by="key-issuer")
    second = await env.tokens.issue(key_request(), created_by="another-issuer")
    uid = owner_id("tenant-a", "researcher")
    await env.users.update(env.operator, uid, UserPatch(enabled=False))
    for issued in (first, second):
        principal = await env.tokens.verify(issued.token)
        with pytest.raises(PermissionError):
            principal.require(Scope.INFERENCE_INVOKE, "qwen3-8b")
        with pytest.raises(PermissionError):
            principal.require(Scope.MCP_INVOKE, "qwen3-8b")
        principal.require(Scope.OPERATIONS_RESULT)
    await env.users.update(env.operator, uid, UserPatch(enabled=True))
    (await env.tokens.verify(first.token)).require(Scope.INFERENCE_INVOKE, "qwen3-8b")


@pytest.mark.asyncio
async def test_app_allowlist_is_independent_route_intersection_and_academic_is_setting(user_env):
    env = user_env
    issued = await env.tokens.issue(key_request(), created_by="operator")
    uid = owner_id("tenant-a", "researcher")
    await env.users.update(env.operator, uid, UserPatch(app_ids=[env.apps[0].app_id]))
    principal = await env.tokens.verify(issued.token)
    assert principal.models == frozenset({"qwen3-8b"})
    with pytest.raises(PermissionError):
        await env.users.require_app(principal, env.apps[1].app_id, True)
    await env.users.update(env.operator, uid, UserPatch(app_ids=None, academic_eligible=False))
    principal = await env.tokens.verify(issued.token)
    assert principal.models == frozenset({"qwen3-8b"})
    await env.users.update(env.operator, uid, UserPatch(academic_eligible=True))
    assert (await env.tokens.verify(issued.token)).models == frozenset({"*"})
    restricted = await env.tokens.issue(key_request(models={"qwen3-8b"}), created_by="operator")
    await env.users.update(env.operator, uid, UserPatch(app_ids=[env.apps[1].app_id]))
    assert not (await env.tokens.verify(restricted.token)).models


@pytest.mark.asyncio
async def test_tenants_same_principal_are_distinct_and_roles_are_not_inferred(user_env):
    env = user_env
    await env.tokens.issue(key_request(), created_by="operator")
    await env.tokens.issue(key_request(tenant="tenant-b"), created_by="operator")
    viewer = env.operator.model_copy(update={"tenant_id": "tenant-a", "role": OperatorRole.VIEWER})
    rows = (await env.users.list(viewer, env.context)).items
    assert len(rows) == 1 and rows[0].tenant_id == "tenant-a"
    with pytest.raises(NotFoundError):
        await env.users.detail(viewer, owner_id("tenant-b", "researcher"), env.context)
    with pytest.raises(PermissionError):
        await env.users.update(viewer, rows[0].id, UserPatch(enabled=False))
    assert owner_id("tenant-a", "researcher") != owner_id("tenant-b", "researcher")


@pytest.mark.asyncio
async def test_create_and_key_ownership_are_exact(user_env):
    env = user_env
    user = await env.users.create(
        env.operator, UserCreate(tenant_id="tenant-a", principal_id="new", display_name="New", kind="service")
    )
    with pytest.raises(ConflictError):
        await env.users.create(
            env.operator, UserCreate(tenant_id="tenant-a", principal_id="new", display_name="Duplicate", kind="human")
        )
    with pytest.raises(ValueError):
        await env.users.issue_key(
            env.operator, user.id, AdminApiKeyCreate(**{**key_request().model_dump(), "name": "Wrong owner"})
        )
    issued = await env.users.issue_key(
        env.operator, user.id, AdminApiKeyCreate(**{**key_request(principal="new").model_dump(), "name": "New key"})
    )
    assert issued.key.principal_id == "new" and issued.key.created_by == "key-issuer"


@pytest.mark.asyncio
async def test_usage_deduplicates_operations_across_keys_rotation_and_issuers(user_env):
    env = user_env
    first = await env.tokens.issue(key_request(), created_by="issuer-one")
    rotated = await env.tokens.rotate(first.id, actor="issuer-two", name=None, expires_at=None)
    now = datetime.now(UTC)
    for token, protocol in (
        (first, "openai-chat"),
        (rotated, "scientific-batch-v1"),
        (first, "scientific-artifact-upload-v1"),
        (rotated, "scientific-artifact-upload-v1"),
    ):
        op = OperationView(
            id=uuid4(),
            tenant_id="tenant-a",
            principal_id="researcher",
            token_id=token.id,
            model_id="qwen3-8b",
            model_revision="test",
            protocol=protocol,
            operation="chat",
            idempotency_key=f"run-{uuid4()}",
            status=OperationStatus.SUCCEEDED,
            accepted_at=now,
            available_at=now,
            completed_at=now,
            input_tokens=2,
            output_tokens=3,
        )
        env.store.operations[op.id] = SimpleNamespace(view=op)
    detail = await env.users.detail(env.operator, owner_id("tenant-a", "researcher"), env.context)
    assert detail.user.usage.requests == 2 and detail.user.usage.scientific_requests == 1
    assert detail.user.usage.input_tokens.value == 4 and detail.user.usage.output_tokens.value == 6
    assert sum(point.requests for point in detail.user.usage.request_series) == 2
    assert len(env.store.operations) == 4  # Upload history remains durable and accessible.
    assert detail.user.key_count == 2 and detail.user.active_key_count == 1
    assert detail.user.usage.scheduler_occupied_gpu_seconds.value is None
    later = env.context.model_copy(update={"from_at": now + timedelta(minutes=1)})
    assert (await env.users.detail(env.operator, detail.user.id, later)).user.usage.requests == 0


def test_lifecycle_coverage_never_fabricates_shared_idle():
    missing = usage_from_counts({"requests": 2, "lifecycle_subjects": 1, "lifecycle_complete": False, "occupied": 20})
    assert missing.scheduler_occupied_gpu_seconds.value is None
    complete = usage_from_counts(
        {"requests": 2, "lifecycle_subjects": 2, "lifecycle_complete": True, "occupied": 20, "active": 12, "idle": 8}
    )
    assert complete.scheduler_occupied_gpu_seconds.value == 20
    assert complete.occupied_idle_gpu_seconds.value == 8
    assert "shared serving" in complete.occupied_idle_gpu_seconds.reason
