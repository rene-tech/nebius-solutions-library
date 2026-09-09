"""Typed contracts over real MCP HTTP/ASGI, without external or GPU traffic."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID

import httpx2
import pytest
from conftest import CATALOG_ROOT
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from test_api_mcp import bound_model_registry, build_runtime
from test_scientific_batch_production import scientific_runtime

from fs2_serve.api import _model_view, create_app
from fs2_serve.mcp_server import CORE_TOOLS, MCP_HTTP_PATH, mount_mcp
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.registry import Registry
from fs2_serve.request_debug import InMemoryDebugStore


async def _key(runtime, *, tenant="tenant-a", models=("qwen3-8b",), catalog=True):
    scopes = {Scope.MCP_INVOKE, Scope.INFERENCE_INVOKE, Scope.OPERATIONS_READ, Scope.OPERATIONS_RESULT}
    if catalog:
        scopes.add(Scope.CATALOG_READ)
    return await runtime.tokens.issue(
        TokenCreate(
            name="typed-http-local-test",
            principal_id=f"researcher-{tenant}",
            tenant_id=tenant,
            scopes=scopes,
            models=set(models),
            max_concurrency=4,
        ),
        created_by="offline-test",
    )


@asynccontextmanager
async def _connection(runtime, app, key):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        headers={"authorization": f"Bearer {key.token}", "origin": runtime.settings.public_origin()},
        base_url=runtime.settings.public_origin(),
        trust_env=False,
    ) as http:
        async with Client(
            streamable_http_client(runtime.settings.public_origin() + MCP_HTTP_PATH, http_client=http),
            mode="auto",
        ) as client:
            yield client


def _data(result):
    assert result.is_error is False
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def _app(runtime):
    runtime.settings.max_request_bytes = 16_384
    app = create_app(runtime)
    mount_mcp(app, runtime)
    return app


@pytest.mark.asyncio
async def test_http_discovery_schema_flat_submission_and_legacy_replay(registry, cipher, hasher):
    runtime = build_runtime(registry, cipher, hasher)
    app = _app(runtime)
    key = await _key(runtime)
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        listing = await client.list_tools()
        tool = next(item for item in listing.tools if item.name == "qwen3_8b_openai_chat")
        assert "messages" in tool.input_schema["properties"]
        assert "payload" not in tool.input_schema["properties"]
        assert "request" not in tool.input_schema["properties"]
        contract = _data(
            await client.call_tool("get_model_schema", {"model_id": "qwen3-8b", "protocol": "openai-chat"})
        )
        assert contract["model_id"] == "qwen3-8b"
        assert contract["active_runtime"] == _model_view(runtime.registry.get("qwen3-8b"))["active_runtime"]
        (published,) = contract["contracts"]
        assert published["tool_name"] == tool.name and published["protocol"] == "openai-chat"
        assert published["input_schema"] == tool.input_schema
        assert published["examples"] and published["source_refs"]
        for example in published["examples"]:
            Draft202012Validator(tool.input_schema).validate(example)

        payload = {"messages": [{"role": "user", "content": "synthetic protocol check"}], "max_tokens": 2}
        controls = {"idempotency_key": "typed-http-flat-replay-20260909", "wait_seconds": 0}
        flat = _data(await client.call_tool(tool.name, payload | controls))
        wrapped = _data(await client.call_tool(tool.name, {"payload": payload, **controls}))
        assert flat["id"] == wrapped["id"] and wrapped["reused"]
        assert len(runtime.store.operations) == 1
        status = _data(await client.call_tool("get_operation", {"operation_id": flat["id"]}))
        assert status["id"] == flat["id"] and status["model_id"] == "qwen3-8b"


@pytest.mark.asyncio
async def test_http_all_core_descriptions_and_named_model_fields(registry, cipher, hasher):
    runtime = build_runtime(registry, cipher, hasher)
    app = _app(runtime)
    key = await _key(runtime)
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        tools = {item.name: item for item in (await client.list_tools()).tools}
        assert CORE_TOOLS <= tools.keys()
        assert tools.keys() - CORE_TOOLS, "at least one authorized named model tool must be published"
        for name in CORE_TOOLS:
            description = tools[name].description or ""
            assert len(description.strip()) >= 45, f"{name} needs a useful description of its operation"
            assert description.strip() != name.replace("_", " ")
            undocumented = [
                field
                for field, schema in tools[name].input_schema.get("properties", {}).items()
                if not schema.get("description")
            ]
            assert not undocumented, f"{name} has arguments without descriptions: {undocumented}"
        for name, tool in tools.items():
            if name in CORE_TOOLS:
                continue
            properties = tool.input_schema["properties"]
            assert set(properties) - {"idempotency_key", "wait_seconds", "payload", "request"}
            assert "payload" not in properties and "request" not in properties
            assert tool.description and len(tool.description) >= 45
            missing = [field for field, schema in properties.items() if not schema.get("description")]
            assert not missing, f"{name} has fields without descriptions: {missing}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_id,tool_name,invalid,field",
    [
        (
            "boltz2",
            "boltz2_native",
            {"polymers": [{"id": "A", "molecule_type": "protein", "sequence": "ACDEFGHIKLMNPQRSTVWY"}]},
            "/polymers/0",
        ),
        (
            "openfold2",
            "openfold2_native",
            {
                "input_id": "shape-test",
                "sequence": "ACDEFGHIKLMNPQRSTVWY",
                "selected_models": [1],
                "relax_prediction": False,
                "alignments": {},
            },
            "/",
        ),
    ],
)
async def test_http_native_validation_has_field_issues_no_run_and_debug_owner(
    registry, cipher, hasher, model_id, tool_name, invalid, field
):
    native = bound_model_registry(registry, model_id)
    model = native.get(model_id)
    # Bind the actual selected portable adapter, not the archival NIM schema.
    # This is only a local protocol fixture; it does not qualify a live route.
    declaration = json.loads((CATALOG_ROOT / "deployment-runtimes" / f"{model_id}-portable-h100.json").read_text())
    record = declaration["record"]
    image_digest = record["runtime"]["image"]["digest"]
    gpu_class = record["resources"]["gpu"]["class"]
    native = Registry(
        native.catalog,
        {
            model_id: replace(
                model,
                variant_id=declaration["variant_id"],
                gateway=replace(
                    model.gateway,
                    mcp_discoverable=True,
                    mcp_invocable=True,
                    runtime_kind=record["runtime"]["kind"],
                    runtime_image_digest=image_digest,
                    gpu_class=gpu_class,
                    qualification=None,
                    binding=replace(
                        model.binding, backend_runtime_image_digest=image_digest, backend_gpu_class=gpu_class
                    ),
                ),
            )
        },
    )
    runtime = build_runtime(native, cipher, hasher)
    runtime.settings.request_debug_enabled = True
    debug = InMemoryDebugStore()
    runtime.request_debug_store = debug
    app = _app(runtime)
    key = await _key(runtime, models=(model_id,))
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        with pytest.raises(MCPError) as failure:
            await client.call_tool(tool_name, invalid)
        assert failure.value.code == -32602
        assert isinstance(failure.value.data, dict), repr(failure.value.error)
        assert failure.value.data["type"] == "model_input_validation"
        assert failure.value.data["model_id"] == model_id
        issues = failure.value.data["issues"]
        assert any(issue["field"] == field for issue in issues)
        assert any("missing_fields" in issue or "allowed_fields" in issue for issue in issues)
        assert "ACDEFGHIKLMNPQRSTVWY" not in str(failure.value)
        assert not runtime.store.operations
    calls = [row for row in (await debug.list(model_id=model_id)).items if row.mcp_tool == tool_name]
    assert len(calls) == 1
    assert calls[0].tenant_id == "tenant-a" and calls[0].principal_id == "researcher-tenant-a"
    assert calls[0].token_id == key.id and calls[0].operation_id is None
    detail = await debug.get(calls[0].id, "tenant-a")
    assert detail.request_body.complete and "ACDEFGHIKLMNPQRSTVWY" in detail.request_body.data
    assert key.token not in detail.model_dump_json()


@pytest.mark.asyncio
async def test_http_shared_model_access_does_not_share_operation_history(registry, cipher, hasher):
    runtime = build_runtime(registry, cipher, hasher)
    app = _app(runtime)
    first = await _key(runtime)
    second = await _key(runtime, tenant="tenant-b")
    restricted = await _key(runtime, tenant="tenant-b", models=("openfold2",))
    async with app.router.lifespan_context(app):
        async with _connection(runtime, app, first) as owner:
            accepted = _data(
                await owner.call_tool(
                    "qwen3_8b_openai_chat",
                    {
                        "messages": [{"role": "user", "content": "synthetic ownership"}],
                        "idempotency_key": "typed-http-owner-isolation-20260909",
                    },
                )
            )
            assert UUID(accepted["id"])
        async with _connection(runtime, app, second) as other:
            assert _data(await other.call_tool("get_model_schema", {"model_id": "qwen3-8b"}))["model_id"] == "qwen3-8b"
            with pytest.raises(MCPError, match="operation not found"):
                await other.call_tool("get_operation", {"operation_id": accepted["id"]})
            with pytest.raises(MCPError):
                await other.call_tool("get_operation_result", {"operation_id": accepted["id"]})
        async with _connection(runtime, app, restricted) as denied:
            assert "qwen3_8b_openai_chat" not in {tool.name for tool in (await denied.list_tools()).tools}
            with pytest.raises(MCPError, match="outside token policy"):
                await denied.call_tool("get_model_schema", {"model_id": "qwen3-8b"})
        assert len(runtime.store.operations) == 1


@pytest.mark.asyncio
async def test_http_scientific_flat_manifest_and_legacy_wrapper_share_one_run(registry, cipher, hasher):
    runtime, _, repository, _, pointer = scientific_runtime(registry, cipher, hasher)
    app = _app(runtime)
    key = await _key(runtime, models=("protein-design",))
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        schema = _data(await client.call_tool("get_model_schema", {"model_id": "protein-design"}))
        (contract,) = schema["contracts"]
        assert contract["protocol"] == "scientific-batch-v1"
        assert contract["tool_name"] == "submit_protein_design"
        fields = contract["input_schema"]["properties"]
        assert "input_manifest" in fields and "parameters" in fields
        assert "request" not in fields and "wait_seconds" not in fields
        request = {
            "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "operation": "design",
            "service_class": "customer-batch",
            "input_manifest": pointer,
            "parameters": {},
        }
        idempotency_key = "typed-http-scientific-replay-20260909"
        submitted = _data(await client.call_tool(contract["tool_name"], request | {"idempotency_key": idempotency_key}))
        wrapped = _data(
            await client.call_tool(
                contract["tool_name"],
                {
                    "request": request,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        assert wrapped["operation"]["id"] == submitted["operation"]["id"]
        assert len(repository.records) == 1
        status = _data(await client.call_tool("get_scientific_status", {"operation_id": submitted["operation"]["id"]}))
        assert status["batch"]["status"] == "queued"
