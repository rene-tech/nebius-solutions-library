"""Real pinned-SDK registration and dispatch for source-backed JSON contracts."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError

from fs2_serve.mcp_input_contracts import apply_tool_input_contract, tool_input_schema

SCHEMA = {
    "type": "object",
    "properties": {
        "sequence": {"type": "string", "minLength": 1},
        "samples": {"type": "integer", "minimum": 1, "default": 3},
        "alignment": {"$ref": "#/$defs/Alignment"},
    },
    "required": ["sequence"],
    "$defs": {
        "Alignment": {
            "type": "object",
            "properties": {"a3m": {"type": "string"}},
            "required": ["a3m"],
            "additionalProperties": False,
        }
    },
    "additionalProperties": False,
}


def server_for(*, scientific: bool = False, openai: bool = False) -> tuple[MCPServer, list[dict[str, Any]]]:
    server = MCPServer("typed-contract-test")
    observed: list[dict[str, Any]] = []

    async def native(
        payload: dict[str, Any], ctx: Context, idempotency_key: str | None = None, wait_seconds: float = 0
    ) -> dict[str, Any]:
        result = {"payload": payload, "idempotency_key": idempotency_key, "wait_seconds": wait_seconds}
        observed.append(result)
        return result

    async def batch(request: dict[str, Any], ctx: Context, idempotency_key: str | None = None) -> dict[str, Any]:
        result = {"request": request, "idempotency_key": idempotency_key}
        observed.append(result)
        return result

    server.add_tool(batch if scientific else native, name="model_tool")
    apply_tool_input_contract(
        server,
        "model_tool",
        model_id="fixture-model",
        payload_schema=SCHEMA,
        scientific=scientific,
        max_wait_seconds=10,
        openai=openai,
    )
    return server, observed


async def call(server: MCPServer, arguments: dict[str, Any]) -> Any:
    return await server._tool_manager.call_tool(
        "model_tool", arguments, context=Context(mcp_server=server, subscriptions={})
    )


async def test_list_and_call_share_flat_nested_schema_without_coercion_or_default_injection() -> None:
    server, observed = server_for()
    tools = await server.list_tools()
    schema = tools[0].input_schema
    assert "sequence" in schema["properties"]
    assert "payload" not in schema["properties"]
    assert schema["properties"]["alignment"]["$ref"] == "#/$defs/Alignment"
    source = {"sequence": "ACDEFGHIK", "alignment": {"a3m": ">query\nACDEFGHIK\n"}, "wait_seconds": 2}
    before = deepcopy(source)
    await call(server, source)
    assert source == before
    assert observed == [
        {
            "payload": {k: v for k, v in source.items() if k != "wait_seconds"},
            "idempotency_key": None,
            "wait_seconds": 2,
        }
    ]
    assert "samples" not in observed[0]["payload"]


@pytest.mark.parametrize("scientific", [False, True])
async def test_legacy_wrappers_validate_against_the_same_concrete_contract(scientific: bool) -> None:
    server, observed = server_for(scientific=scientific)
    wrapper = "request" if scientific else "payload"
    await call(server, {wrapper: {"sequence": "ACDEFGHIK"}, "idempotency_key": "legacy-contract-0001"})
    assert observed[0][wrapper] == {"sequence": "ACDEFGHIK"}
    assert observed[0]["idempotency_key"] == "legacy-contract-0001"
    schema = (await server.list_tools())[0].input_schema
    assert wrapper not in schema["properties"]
    assert ("wait_seconds" not in schema["properties"]) is scientific


async def test_openai_legacy_model_is_not_a_route_override() -> None:
    server, observed = server_for(openai=True)
    await call(server, {"payload": {"sequence": "ACDEFGHIK", "model": "legacy-ignored-selector"}})
    assert observed[0]["payload"] == {"sequence": "ACDEFGHIK"}


@pytest.mark.parametrize(
    ("arguments", "rule"),
    [
        ({}, "required"),
        ({"sequence": "ACDE", "unknown": "PRIVATE_INVALID_VALUE"}, "additionalProperties"),
        ({"sequence": "ACDE", "samples": "PRIVATE_INVALID_VALUE"}, "type"),
        ({"sequence": "ACDE", "samples": 0}, "minimum"),
        ({"sequence": "ACDE", "alignment": {"unknown": "PRIVATE_INVALID_VALUE"}}, "required"),
        ({"sequence": "ACDE", "wait_seconds": 11}, "maximum"),
        ({"sequence": "ACDE", "wait_seconds": float("nan")}, "finite"),
        ({"sequence": "ACDE", "idempotency_key": "short"}, "minLength"),
        ({"payload": {"sequence": "ACDE", "unknown": "PRIVATE_INVALID_VALUE"}}, "additionalProperties"),
    ],
)
async def test_invalid_inputs_return_actionable_field_issues_before_dispatch(
    arguments: dict[str, Any], rule: str
) -> None:
    server, observed = server_for()
    with pytest.raises(MCPError) as caught:
        await call(server, arguments)
    error = caught.value.error
    assert error.code == -32602
    assert error.data["type"] == "model_input_validation"
    assert rule in {item["rule"] for item in error.data["issues"]}
    assert "PRIVATE_INVALID_VALUE" not in json.dumps(error.model_dump())
    assert not observed


async def test_submission_controls_cannot_be_hidden_inside_legacy_payload() -> None:
    server, observed = server_for()
    with pytest.raises(MCPError, match="Submission controls"):
        await call(server, {"payload": {"sequence": "ACDE", "wait_seconds": 2}})
    assert not observed


def test_composition_keeps_original_contract_unchanged_and_rejects_reserved_names() -> None:
    source = deepcopy(SCHEMA)
    result = tool_input_schema(source, scientific=False, max_wait_seconds=10)
    assert source == SCHEMA
    assert result["properties"]["wait_seconds"]["maximum"] == 10
    source["properties"]["wait_seconds"] = {"type": "number"}
    with pytest.raises(ValueError, match="collide"):
        tool_input_schema(source, scientific=False, max_wait_seconds=10)
