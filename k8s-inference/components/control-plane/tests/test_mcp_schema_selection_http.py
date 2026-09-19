"""Bounded discovery must preserve exact typed contracts and caller grants."""

import json
from dataclasses import replace

import pytest
from mcp.shared.exceptions import MCPError
from test_api_mcp import bound_model_registry, build_runtime
from test_mcp_model_tools_http import _app, _connection, _data, _key
from test_scientific_batch_production import scientific_runtime

from fs2_serve.registry import Registry


@pytest.mark.asyncio
async def test_cosmos_compact_discovery_selects_exact_unchanged_contract(registry, cipher, hasher):
    native = bound_model_registry(registry, "cosmos3-nano")
    model = native.get("cosmos3-nano")
    native = Registry(native.catalog, {"cosmos3-nano": replace(
        model, gateway=replace(model.gateway, mcp_discoverable=True, mcp_invocable=True),
    )})
    runtime = build_runtime(native, cipher, hasher)
    app = _app(runtime)
    key = await _key(runtime, models=("cosmos3-nano",))
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        full = _data(await client.call_tool("get_model_schema", {"model_id": "cosmos3-nano"}))
        summary = _data(await client.call_tool("get_model_schema", {
            "model_id": "cosmos3-nano", "summary_only": True,
        }))
        assert summary["contracts"] == [
            {k: c[k] for k in ("tool_name", "protocol", "model_ref")} for c in full["contracts"]
        ]
        assert len(json.dumps(summary)) < len(json.dumps(full)) / 10
        for expected in full["contracts"]:
            selected = _data(await client.call_tool("get_model_schema", {
                "model_id": "cosmos3-nano", "tool_name": expected["tool_name"],
            }))
            assert selected == {**full, "contracts": [expected]}
        for name in ("invented", "cosmos3_nano_transfer_video_mcp_bionemo-models", ""):
            with pytest.raises(MCPError, match="No matching tool contract"):
                await client.call_tool("get_model_schema", {"model_id": "cosmos3-nano", "tool_name": name})
        assert not runtime.store.operations


@pytest.mark.asyncio
async def test_scientific_selection_preserves_artifact_contract_and_rejects_wrong_name(registry, cipher, hasher):
    runtime, *_ = scientific_runtime(registry, cipher, hasher)
    app = _app(runtime)
    key = await _key(runtime, models=("protein-design",))
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        full = _data(await client.call_tool("get_model_schema", {"model_id": "protein-design"}))
        selected = _data(await client.call_tool("get_model_schema", {
            "model_id": "protein-design", "tool_name": "submit_protein_design", "protocol": "scientific-batch-v1",
        }))
        assert selected == full
        summary = _data(await client.call_tool("get_model_schema", {
            "model_id": "protein-design", "summary_only": True,
        }))
        assert set(summary) == {"model_id", "contracts"}
        assert set(summary["contracts"][0]) == {"tool_name", "protocol", "model_ref"}
        with pytest.raises(MCPError, match="No matching tool contract"):
            await client.call_tool("get_model_schema", {
                "model_id": "protein-design", "tool_name": "cosmos3_nano_transfer_video",
            })


@pytest.mark.asyncio
@pytest.mark.parametrize("selectors", [
    {"summary_only": True}, {"tool_name": "qwen3_8b_openai_chat"},
])
async def test_schema_selectors_do_not_bypass_existing_catalog_grants(registry, cipher, hasher, selectors):
    runtime = build_runtime(registry, cipher, hasher)
    app = _app(runtime)
    key = await _key(runtime, models=("outside-this-catalog",))
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        with pytest.raises(MCPError, match="outside token policy"):
            await client.call_tool("get_model_schema", {"model_id": "qwen3-8b", **selectors})
        assert not runtime.store.operations
