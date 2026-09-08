"""One operator deployment, model-scoped customer keys, separate request owners."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError
from test_api_mcp import TestClient, _mcp_payload, build_runtime, issue
from test_dynamic_routes import _cosmos_revision, _principal, _revision
from test_model_deployment_publication import status_view
from test_postgres_integration import postgres_store as _postgres_store_fixture

from fs2_serve.access import AdminAccessService
from fs2_serve.admin_models import AdminContext
from fs2_serve.api import create_app
from fs2_serve.auth import require_operation_access
from fs2_serve.mcp_server import PATTokenVerifier, build_mcp_server
from fs2_serve.model_deployment import Visibility, spec_digest
from fs2_serve.model_deployment_publication import project_dynamic_publications
from fs2_serve.model_deployment_records import ModelDeploymentAppendRequest, ModelDeploymentRevisionAction
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.runtime import StubRuntimeClient
from fs2_serve.store import NotFoundError
from fs2_serve.user_models import InferenceUser, owner_id
from fs2_serve.user_repository import MemoryUserRepository, PostgresUserRepository
from fs2_serve.users import UserService

postgres_store = _postgres_store_fixture


class RecordingRuntime(StubRuntimeClient):
    def __init__(self):
        super().__init__()
        self.dispatched = []

    async def invoke(self, model, operation, request_body):
        self.dispatched.append((model, operation))
        return await super().invoke(model, operation, request_body)


async def publish(runtime, model_id="qwen3-8b"):
    original = _cosmos_revision(runtime.registry) if model_id == "cosmos3-nano" else _revision(runtime.registry)
    appended = await runtime.store.model_deployment_append_revision(
        ModelDeploymentAppendRequest(
            namespace=original.namespace,
            name=original.name,
            expected_etag=None,
            spec=original.spec,
            action=ModelDeploymentRevisionAction.CREATE,
            actor_id=uuid4(),
            actor="platform-operator",
            idempotency_key=f"operator-deployment-{uuid4()}",
        )
    )
    revision = appended.value
    snapshot = project_dynamic_publications([revision], {(revision.namespace, revision.name): status_view(revision)})
    assert runtime.registry.set_dynamic_publications(snapshot, valid_until=datetime.now(UTC) + timedelta(minutes=5))
    recorder = RecordingRuntime()
    runtime.admission.runtime = recorder
    return revision, recorder


def invocation(model_id):
    if model_id == "cosmos3-nano":
        return f"/v1/models/{model_id}:invoke", {"operation": "generate-media", "payload": {"prompt": "fixture"}}
    return "/v1/chat/completions", {"model": model_id, "messages": [{"role": "user", "content": "fixture"}]}


@pytest.mark.parametrize("model_id", ["qwen3-8b", "cosmos3-nano"])
def test_shared_http_and_native_dispatch_keep_customer_ownership_and_key_denials(registry, cipher, hasher, model_id):
    runtime = build_runtime(registry, cipher, hasher, run_workers=True)
    revision, recorder = asyncio.run(publish(runtime, model_id))
    path, payload = invocation(model_id)
    customers = ("customer-a", "customer-b")
    tokens = []
    operation_ids = []
    with TestClient(create_app(runtime)) as client:
        for tenant in customers:
            token = issue(
                client,
                principal="same-user-name",
                tenant=tenant,
                scopes=["catalog.read", "inference.invoke"],
                models=[model_id],
            )
            tokens.append(token)
            headers = {
                "authorization": f"Bearer {token}",
                "idempotency-key": "same-customer-local-key",
                "x-fs2-wait-seconds": "2",
            }
            listing = client.get("/v1/models", headers=headers)
            assert listing.status_code == 200
            assert [item["id"] for item in listing.json()["data"]] == [model_id]
            response = client.post(path, headers=headers, json=payload)
            assert response.status_code == 200, response.text
            operation_ids.append(UUID(response.headers["x-fs2-operation-id"]))
            replay = client.post(path, headers=headers, json=payload)
            assert replay.status_code == 200
            assert UUID(replay.headers["x-fs2-operation-id"]) == operation_ids[-1]

        assert len(set(operation_ids)) == 2
        for index, token in enumerate(tokens):
            headers = {"authorization": f"Bearer {token}"}
            assert client.get(f"/v1/operations/{operation_ids[index]}/result", headers=headers).status_code == 200
            foreign = operation_ids[1 - index]
            assert client.get(f"/v1/operations/{foreign}", headers=headers).status_code == 404
            assert client.get(f"/v1/operations/{foreign}/result", headers=headers).status_code == 404
            assert client.post(f"/v1/operations/{foreign}:cancel", headers=headers).status_code == 404

        denied = issue(
            client,
            principal="denied",
            tenant="customer-a",
            scopes=["catalog.read", "inference.invoke"],
            models=["glm-5-2-fp8"],
        )
        denied_headers = {"authorization": f"Bearer {denied}", "idempotency-key": "restricted-customer-key"}
        assert client.get("/v1/models", headers=denied_headers).json()["data"] == []
        assert client.post(path, headers=denied_headers, json=payload).status_code == 403

        missing_scope = issue(
            client, principal="read-only", tenant="customer-a", scopes=["catalog.read"], models=[model_id]
        )
        assert client.post(path, headers={"authorization": f"Bearer {missing_scope}"}, json=payload).status_code == 403

        # Disabling a configured owner still stops new invocations, while that
        # owner's existing result remains readable through the same key.
        repository = MemoryUserRepository(runtime.store)
        users = UserService(repository, AdminAccessService(runtime.store, runtime.tokens))
        now = datetime.now(UTC)
        asyncio.run(
            repository.save(
                InferenceUser(
                    id=owner_id("customer-a", "same-user-name"),
                    tenant_id="customer-a",
                    principal_id="same-user-name",
                    display_name="Disabled customer",
                    enabled=False,
                    source="configured",
                    created_at=now,
                    updated_at=now,
                ),
                create=True,
            )
        )
        runtime.tokens.principal_policy = users.constrain_principal
        headers = {"authorization": f"Bearer {tokens[0]}", "idempotency-key": "disabled-customer-key"}
        assert client.post(path, headers=headers, json=payload).status_code == 403
        assert client.get(f"/v1/operations/{operation_ids[0]}/result", headers=headers).status_code == 200

    assert len(recorder.dispatched) == 2
    assert {op.tenant_id for _, op in recorder.dispatched} == set(customers)
    assert {op.principal_id for _, op in recorder.dispatched} == {"same-user-name"}
    assert {op.model_revision for _, op in recorder.dispatched} == {f"dynamic:{revision.etag}"}
    assert {model.binding.service_origin for model, _ in recorder.dispatched} == {
        registry.get(model_id).binding.service_origin
    }
    assert all(op.tenant_id != revision.tenant_id for _, op in recorder.dispatched)
    assert len({op.token_id for _, op in recorder.dispatched}) == 2
    context = AdminContext(from_at=now - timedelta(minutes=5), to_at=now + timedelta(minutes=5), timezone="UTC")
    for tenant in customers:
        usage = asyncio.run(repository.usage(tenant, "same-user-name", context))
        assert usage.requests == usage.succeeded == 1


@pytest.mark.parametrize("visibility", [Visibility.PRIVATE, Visibility.TENANT])
@pytest.mark.parametrize("surface", ["catalog", "openai", "mcp", "native"])
def test_explicit_principal_restrictions_remain_tenant_qualified(registry, visibility, surface):
    original = _revision(registry, visibility=visibility)
    spec = original.spec.model_copy(
        update={"policy": original.spec.policy.model_copy(update={"allowed_principal_ids": ["private-user"]})}
    )
    revision = original.model_copy(update={"spec": spec, "etag": spec_digest(spec)})
    registry.set_dynamic_publications(
        project_dynamic_publications([revision], {(revision.namespace, revision.name): status_view(revision)}),
        valid_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    permitted = _principal(tenant=revision.tenant_id, principal_id="private-user")
    assert [model.id for model in registry.allowed_for_principal(permitted, surface=surface)] == ["qwen3-8b"]
    for denied in (_principal(), _principal(tenant="other-customer", principal_id="private-user")):
        assert registry.allowed_for_principal(denied, surface=surface) == []
        with pytest.raises(PermissionError, match="dynamic route policy"):
            registry.authorize_principal(
                registry.get("qwen3-8b"), denied, requested_model_id="qwen3-8b", surface=surface
            )


async def exercise_mcp_customers(runtime, model_id):
    revision, recorder = await publish(runtime, model_id)
    server = build_mcp_server(runtime)
    context = Context(mcp_server=server, subscriptions=server._subscriptions)
    protocol = "native" if model_id == "cosmos3-nano" else "openai-chat"
    payload = (
        {"prompt": "fixture"}
        if protocol == "native"
        else {"model": model_id, "messages": [{"role": "user", "content": "fixture"}]}
    )
    ids = []
    principals = []
    await runtime.admission.start()
    try:
        for tenant in ("customer-a", "customer-b"):
            key = await runtime.tokens.issue(
                TokenCreate(
                    principal_id="same-user-name",
                    tenant_id=tenant,
                    models={model_id},
                    scopes={Scope.CATALOG_READ, Scope.MCP_INVOKE},
                    max_concurrency=4,
                ),
                created_by="platform-operator",
            )
            principals.append(await runtime.tokens.verify(key.token))
            access = await PATTokenVerifier(runtime).verify_token(key.token)
            assert access is not None
            auth = auth_context_var.set(AuthenticatedUser(access))
            try:
                listing = await server._tool_manager.call_tool("list_models", {}, context, convert_result=False)
                assert [row["id"] for row in listing["data"]] == [model_id]
                arguments = {
                    "model_id": model_id,
                    "protocol": protocol,
                    "payload": payload,
                    "idempotency_key": "same-mcp-customer-key",
                    "wait_seconds": 2,
                }
                operation = _mcp_payload(
                    await server._tool_manager.call_tool("invoke_model", arguments, context, convert_result=False)
                )
                assert operation["status"] == "succeeded"
                ids.append(UUID(operation["id"]))
                replay = _mcp_payload(
                    await server._tool_manager.call_tool("invoke_model", arguments, context, convert_result=False)
                )
                # The wait path returns current operation metadata; exact ID
                # and the dispatch count below prove no second logical run.
                assert replay["id"] == operation["id"]
                result = await server._tool_manager.call_tool(
                    "get_operation_result", {"operation_id": str(ids[-1])}, context, convert_result=False
                )
                assert result["result"]
                if len(ids) == 2:
                    with pytest.raises(MCPError, match="operation not found"):
                        await server._tool_manager.call_tool(
                            "get_operation_result", {"operation_id": str(ids[0])}, context, convert_result=False
                        )
            finally:
                auth_context_var.reset(auth)

        denied = await runtime.tokens.issue(
            TokenCreate(
                principal_id="denied",
                tenant_id="customer-a",
                models={"glm-5-2-fp8"},
                scopes={Scope.CATALOG_READ, Scope.MCP_INVOKE},
            ),
            created_by="platform-operator",
        )
        denied_access = await PATTokenVerifier(runtime).verify_token(denied.token)
        assert denied_access is not None
        auth = auth_context_var.set(AuthenticatedUser(denied_access))
        try:
            listing = await server._tool_manager.call_tool("list_models", {}, context, convert_result=False)
            assert listing["data"] == []
            with pytest.raises(MCPError, match="outside token policy"):
                await server._tool_manager.call_tool("invoke_model", arguments, context, convert_result=False)
        finally:
            auth_context_var.reset(auth)
    finally:
        await runtime.admission.close()

    assert len(recorder.dispatched) == len(set(ids)) == 2
    for index, (model, operation) in enumerate(recorder.dispatched):
        assert operation.tenant_id == principals[index].tenant_id != revision.tenant_id
        assert operation.token_id == principals[index].token_id
        assert operation.principal_id == "same-user-name"
        assert operation.model_revision == f"dynamic:{revision.etag}"
        assert model.dynamic_policy.publication.tenant_id == revision.tenant_id
        with pytest.raises(NotFoundError):
            require_operation_access(principals[1 - index], operation)
    return principals


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["qwen3-8b", "cosmos3-nano"])
async def test_shared_mcp_dispatch_and_result_isolation(registry, cipher, hasher, model_id):
    await exercise_mcp_customers(build_runtime(registry, cipher, hasher), model_id)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["qwen3-8b", "cosmos3-nano"])
async def test_real_postgres_shared_dispatch_preserves_customer_usage(
    postgres_store, registry, cipher, hasher, model_id
):
    runtime = build_runtime(registry, cipher, hasher, store=postgres_store)
    principals = await exercise_mcp_customers(runtime, model_id)
    repository = PostgresUserRepository(postgres_store.pool)
    now = datetime.now(UTC)
    context = AdminContext(from_at=now - timedelta(minutes=5), to_at=now + timedelta(minutes=5), timezone="UTC")
    for principal in principals:
        usage = await repository.usage(principal.tenant_id, principal.principal_id, context)
        assert usage.requests == usage.succeeded == 1
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM fs2_model_deployments") == 1
