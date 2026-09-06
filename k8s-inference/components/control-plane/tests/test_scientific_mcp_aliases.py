from __future__ import annotations

from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from test_scientific_batch_production import scientific_runtime

from fs2_serve.api import create_app
from fs2_serve.live_acceptance import _mcp_result
from fs2_serve.mcp_server import mount_mcp
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.scientific_batch.catalog_adapter import CatalogProfileAdapterError


@asynccontextmanager
async def scientific_client(runtime, *, models=None, invoke=True, tenant="tenant-a"):
    scopes = {Scope.MCP_INVOKE, Scope.CATALOG_READ, Scope.OPERATIONS_READ}
    if invoke:
        scopes.add(Scope.INFERENCE_INVOKE)
    issued = await runtime.tokens.issue(
        TokenCreate(
            principal_id="scientific-mcp-test",
            tenant_id=tenant,
            scopes=scopes,
            models=models or {"protein-design"},
            max_concurrency=4,
        ),
        created_by="test",
    )
    app = create_app(runtime)
    mount_mcp(app, runtime)
    async with app.router.lifespan_context(app), httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        headers={"authorization": f"Bearer {issued.token}", "origin": runtime.settings.public_origin()},
        trust_env=False,
    ) as http:
        async with Client(
            streamable_http_client(runtime.settings.public_origin() + "/mcp", http_client=http),
            mode="2026-07-28",
        ) as client:
            yield client


async def assert_alias_rejected(client, arguments):
    try:
        result = await client.call_tool("submit_protein_design", arguments)
    except MCPError:
        return
    assert result.is_error


@pytest.mark.asyncio
async def test_scientific_alias_is_discoverable_and_reuses_generic_idempotent_submission(registry, cipher, hasher):
    runtime, _, repository, _, pointer = scientific_runtime(registry, cipher, hasher)
    arguments = {
        "request": {
            "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "operation": "design",
            "service_class": "customer-batch",
            "input_manifest": pointer,
            "parameters": {},
        },
        "idempotency_key": "scientific-alias-shared-0001",
    }
    async with scientific_client(runtime) as client:
        tools = await client.list_tools()
        assert tools.ttl_ms == 0 and tools.cache_scope == "private"
        discovered = _mcp_result(await client.call_tool("list_scientific_models", {}))
        advertised = {profile["mcp_tool_name"] for profile in discovered["data"]}
        assert advertised == {"submit_protein_design"}
        assert advertised <= {tool.name for tool in tools.tools}
        submitted = _mcp_result(await client.call_tool("submit_protein_design", arguments))
        replayed = _mcp_result(await client.call_tool(
            "submit_scientific_run", {**arguments, "model_id": "protein-design"}
        ))
        assert replayed["operation"]["id"] == submitted["operation"]["id"]
        assert len(repository.records) == 1
        status = _mcp_result(await client.call_tool(
            "get_scientific_status", {"operation_id": submitted["operation"]["id"]}
        ))
        assert status["batch"]["status"] == "queued"

        # Already registered names cannot continue invoking a withdrawn service.
        runtime.scientific_batches = None
        assert "submit_protein_design" not in {tool.name for tool in (await client.list_tools()).tools}
        await assert_alias_rejected(client, arguments)
        assert len(repository.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["model", "scope", "license"])
async def test_scientific_alias_discovery_and_calls_retain_admission_authorization(
    registry, cipher, hasher, monkeypatch, restriction
):
    runtime, _, repository, _, _ = scientific_runtime(registry, cipher, hasher)
    assert runtime.scientific_batches is not None
    if restriction == "license":
        def unavailable_access(profile, *, tenant_id):
            raise CatalogProfileAdapterError("academic deployment authorization unavailable")

        monkeypatch.setattr(runtime.scientific_batches.execution_binding, "access_context", unavailable_access)
    async with scientific_client(
        runtime,
        models={"other-model"} if restriction == "model" else {"protein-design"},
        invoke=restriction != "scope",
        tenant="other-tenant" if restriction == "license" else "tenant-a",
    ) as client:
        assert "submit_protein_design" not in {tool.name for tool in (await client.list_tools()).tools}
        discovered = _mcp_result(await client.call_tool("list_scientific_models", {}))
        assert discovered["data"] == []
        await assert_alias_rejected(client, {"request": {}, "idempotency_key": "scientific-denied-0001"})
        assert not repository.records
