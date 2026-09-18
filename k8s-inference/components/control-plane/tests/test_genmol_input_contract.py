"""GenMol discovery explains existing mask semantics without changing bounds."""

from __future__ import annotations

import ast
import asyncio
import logging
import re
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import SOLUTION_ROOT
from fastapi import HTTPException
from jsonschema import Draft202012Validator
from test_api_mcp import bound_model_registry, build_runtime
from test_mcp_model_tools_http import _app, _connection, _data, _key
from test_model_input_contracts import selected, source_models

from fs2_serve.model_input_contracts import _resource, contract_for
from fs2_serve.registry import Registry


def assert_mask_description(description):
    assert "SAFE mask tokens" in description
    assert "floor((minimum + maximum) / 2)" in description
    assert "minimum_mask_tokens=15" in description
    assert "Neither endpoint is a heavy-atom bound" in description
    assert "maximum is not a maximum token or molecular-size limit" in description


def test_genmol_contract_describes_mask_mapping_and_preserves_limits(registry):
    contract = contract_for(selected(registry, "genmol"), "native")
    assert_mask_description(contract.input_schema["description"])
    assert_mask_description(contract.input_schema["properties"]["smiles"]["description"])
    assert any("add09fc83b7255bd09c797e527c0f4b51f5fb7c1" in ref for ref in contract.source_refs)
    schema = contract.input_schema
    for mask in ("[*{10-20}]", "[*{1-512}]"):
        Draft202012Validator(schema).validate({"smiles": mask, "num_molecules": 16})
    assert schema["properties"]["num_molecules"]["maximum"] == 16
    assert schema["properties"]["step_size"]["maximum"] == 100
    assert "heavy_atoms" not in schema["properties"]
    source = _resource("runtime-pydantic.json")["genmol"]
    assert (
        source_models(SOLUTION_ROOT.parent / source["source"])["GenerateRequest"].model_json_schema()
        == source["schema"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("minimum,maximum,expected", [(10, 20, 15), (10, 21, 15), (1, 512, 256)])
async def test_actual_runtime_endpoint_maps_mask_midpoint_without_maximum_size_filter(minimum, maximum, expected):
    """Execute the real endpoint AST with a CPU stand-in at the GPU boundary."""
    path = SOLUTION_ROOT / "models/bionemo/genmol/server.py"
    endpoint = next(
        node
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "generate"
    )
    endpoint.decorator_list = []
    request_model = source_models(path)["GenerateRequest"]
    runtime = SimpleNamespace(ready=True, sampler=object(), requests=0, failures=0, generated=0, lock=asyncio.Lock())
    calls = []

    def generate(sampler, **kwargs):
        calls.append((sampler, kwargs))
        return [{"smiles": "C" * 30, "score": 0.1}], {"minimum_mask_tokens": kwargs["token_length"]}

    namespace = {
        "Any": Any,
        "GenerateRequest": request_model,
        "RUNTIME": runtime,
        "asyncio": asyncio,
        "HTTPException": HTTPException,
        "MASK_RANGE": re.compile(r"^\[\*\{(?P<minimum>[0-9]+)-(?P<maximum>[0-9]+)\}\]$"),
        "_finite": lambda value, _name: float(value),
        "generate_valid_molecules": generate,
        "time": time,
        "SOURCE_REVISION": "test-source",
        "MODEL_REVISION": "test-model",
        "GenerationExhausted": RuntimeError,
        "LOGGER": logging.getLogger(__name__),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[endpoint], type_ignores=[])), str(path), "exec"), namespace)  # noqa: S102 - trusted endpoint only
    result = await namespace["generate"](request_model(smiles=f"[*{{{minimum}-{maximum}}}]"))
    assert calls[0][0] is runtime.sampler
    assert calls[0][1]["token_length"] == result["metrics"]["minimum_mask_tokens"] == expected
    assert result["molecules"][0]["smiles"] == "C" * 30
    assert runtime.generated == 1


@pytest.mark.asyncio
async def test_mcp_tool_and_get_model_schema_both_expose_mask_semantics(registry, cipher, hasher):
    native = bound_model_registry(registry, "genmol")
    model = native.get("genmol")
    model = replace(
        model,
        variant_id="nv-genmol-89m-v2-portable",
        gateway=replace(
            model.gateway,
            runtime_kind="custom",
            mcp_discoverable=True,
            mcp_invocable=True,
            qualification=None,
        ),
    )
    runtime = build_runtime(Registry(native.catalog, {"genmol": model}), cipher, hasher)
    app = _app(runtime)
    key = await _key(runtime, models=("genmol",))
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        tool = next(item for item in (await client.list_tools()).tools if item.name == "genmol_native")
        assert_mask_description(tool.description)
        assert_mask_description(tool.input_schema["properties"]["smiles"]["description"])
        discovered = _data(await client.call_tool("get_model_schema", {"model_id": "genmol"}))
        schema = discovered["contracts"][0]["input_schema"]
        assert schema == tool.input_schema
        assert_mask_description(schema["description"])
    assert not runtime.store.operations
