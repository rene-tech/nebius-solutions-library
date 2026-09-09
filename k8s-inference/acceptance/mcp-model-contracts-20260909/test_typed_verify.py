"""Offline only: typed discovery, examples, fixed fixtures and exact failures."""

import asyncio
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from mcp.shared.exceptions import MCPError

from fs2_serve import model_input_contracts

spec = importlib.util.spec_from_file_location("fs2_typed_mcp_live_verify", Path(__file__).with_name("verify.py"))
assert spec and spec.loader
v = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = v
spec.loader.exec_module(v)


def tool_contract(model="phenoage", protocol="native", field="samples"):
    schema = {
        "type": "object",
        "properties": {
            field: {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Explicit synthetic inputs for the selected runtime.",
            },
            "wait_seconds": {"type": "number", "minimum": 0, "description": "Wait briefly for a durable result."},
            "idempotency_key": {"type": "string", "description": "Unique original logical request identity."},
        },
        "required": [field],
        "additionalProperties": False,
    }
    tool = {
        "name": model + "_" + protocol,
        "description": "Submit concrete inputs to this selected runtime and retain the operation ID.",
        "inputSchema": schema,
        "_meta": {"fs2_model_id": model, "fs2_protocol": protocol, "fs2_input_contract": "typed-model-v1"},
    }
    contract = {
        "tool_name": tool["name"],
        "protocol": protocol,
        "model_ref": model,
        "input_schema": deepcopy(schema),
        "examples": [{field: ["synthetic"]}],
        "source_refs": ["repository:model-local/runtime.py"],
    }
    return tool, contract


def test_offline_default_never_reads_credentials_or_creates_client(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline mode attempted private/network access")

    monkeypatch.setattr(v.debug, "read_key", forbidden)
    monkeypatch.setattr(v.httpx, "Client", forbidden)
    assert v.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["expected_original_operations"] == 4
    assert plan["expected_preadmission_errors"] == 2
    assert plan["scientific_submissions"] == plan["client_replays"] == 0


def test_positive_payloads_preserve_known_fixture_hashes_and_small_qwen():
    payloads = v.fixtures()
    assert v.shared.digest(payloads["phenoage"]) == "b15ebdc61742dff7d3293e35a09b2a3207fd39d6e05ef7f881d900b4172b7842"
    assert v.shared.digest(payloads["boltz2"]) == "03debd5292c6df0b8ef532d0e57d0237d73b001970ee4e2e3e43b08b500a1ca7"
    assert v.shared.digest(payloads["openfold2"]) == "f44d71c6d1224bead72645377e725acadf19b462978457d80890ab77f86ec589"
    qwen = payloads["qwen3-8b"]
    assert qwen["max_tokens"] == 32 and qwen["chat_template_kwargs"] == {"enable_thinking": False}
    assert "model" not in qwen and "payload" not in qwen


def test_negative_fixtures_change_only_declared_unsupported_inputs():
    original, bad = v.fixtures(), v.invalid_fixtures()
    assert "msa" not in bad["boltz2"]["polymers"][0]
    bad["boltz2"]["polymers"][0]["msa"] = original["boltz2"]["polymers"][0]["msa"]
    assert bad["boltz2"] == original["boltz2"]
    assert bad["openfold2"].pop("alignments") == {}
    assert bad["openfold2"] == original["openfold2"]


@pytest.mark.parametrize("model", v.CALLS)
def test_original_live_fixture_matches_current_selected_runtime_contract(model):
    if model == "qwen3-8b":
        schema = model_input_contracts._chat(model)[0]
    elif model == "openfold2":
        schema = model_input_contracts._openfold2()
    else:
        schema = model_input_contracts._pydantic_contract(model)[0]
    validator = Draft202012Validator(schema)
    assert validator.is_valid(v.fixtures()[model])
    if model in {"boltz2", "openfold2"}:
        assert not validator.is_valid(v.invalid_fixtures()[model])


def test_real_examples_are_validated_not_merely_counted():
    tool, contract = tool_contract()
    result = v.inspect_contract(tool, contract)
    assert result["example_count"] == 1 and result["examples_state"] == "validated"
    assert result["fields"] == ["samples"]
    contract["examples"] = [{"samples": []}]
    with pytest.raises(v.debug.CheckError, match="published_example_invalid"):
        v.inspect_contract(tool, contract)


def test_asset_examples_absent_are_not_manufactured():
    tool, contract = tool_contract()
    contract["examples"] = []
    result = v.inspect_contract(tool, contract)
    assert result["example_count"] == 0
    assert result["examples_state"] == "not_provided_asset_or_geometry_required"


@pytest.mark.parametrize(
    "mutation", ["payload", "request", "no_description", "untyped", "no_fields", "external_ref", "unbounded"]
)
def test_opaque_or_incomplete_tool_never_passes(mutation):
    tool, _ = tool_contract()
    if mutation in {"payload", "request"}:
        tool["inputSchema"]["properties"][mutation] = {"type": "object", "description": "Opaque wrapper"}
    elif mutation == "no_description":
        tool["description"] = "Call model"
    elif mutation == "untyped":
        tool["inputSchema"]["type"] = "string"
    elif mutation == "no_fields":
        tool["inputSchema"]["properties"] = {}
    elif mutation == "external_ref":
        tool["inputSchema"]["properties"]["samples"]["items"] = {"$ref": "https://example.invalid/private"}
    else:
        tool["inputSchema"]["additionalProperties"] = True
    with pytest.raises(v.debug.CheckError):
        v.inspect_tool(tool)


@pytest.mark.parametrize(
    "model,issues",
    [
        ("boltz2", [{"field": "/polymers/0", "rule": "required", "missing_fields": ["msa"]}]),
        ("openfold2", [{"field": "/", "rule": "additionalProperties", "allowed_fields": ["sequence"]}]),
    ],
)
def test_exact_invalid_params_requires_model_and_actionable_issue(model, issues):
    error = MCPError(
        code=-32602,
        message="Invalid model inputs",
        data={"type": "model_input_validation", "model_id": model, "issues": issues},
    )
    assert v.expected_issue(model, error)["code"] == -32602
    wrong = MCPError(code=-32603, message="Different failure", data=error.data)
    with pytest.raises(v.debug.CheckError):
        v.expected_issue(model, wrong)
    with pytest.raises(v.debug.CheckError):
        v.expected_issue("other-model", error)


def test_qwen_nonempty_content_is_required():
    v.validate_result("qwen3-8b", {}, {"choices": [{"message": {"content": "OK"}}]})
    with pytest.raises(v.debug.CheckError, match="useful_output"):
        v.validate_result("qwen3-8b", {}, {"choices": [{"message": {"content": ""}}]})


def test_failed_exchange_metadata_retains_correlation_without_payload():
    cases = [
        {
            "tool": "model_native",
            "probe": "synthetic-probe",
            "started_at": "synthetic-clock",
            "endpoint": "/mcp",
            "request": b'{"synthetic":"input"}',
            "response": b"partial",
        }
    ]
    row = v.exchange_events(cases)[0]
    assert row["http_status"] is None and row["operation_id"] is None
    assert row["client_observed_response_bytes"] == 7
    assert row["probe"] == "synthetic-probe"
    assert "partial" not in json.dumps(row) and "input" not in json.dumps(row)


@pytest.mark.parametrize("missing", [False, True])
def test_all_authorized_serving_scientific_and_clone_tools_are_covered(tmp_path, monkeypatch, missing):
    (tmp_path / "raw").mkdir()
    pairs = [(model, "native") for model in v.CALLS] + [
        ("independent-clone", "native"),
        ("scientific-app", "scientific-batch-v1"),
    ]
    records = [tool_contract(model, protocol) for model, protocol in pairs]
    tools = [tool for tool, _ in records]
    if missing:
        tools.pop()

    class Tool:
        def __init__(self, value):
            self.value = value

        def model_dump(self, **kwargs):
            return self.value

    class Client:
        async def list_tools(self):
            return SimpleNamespace(tools=[Tool(tool) for tool in tools], next_cursor=None)

    async def tool_data(client, name, arguments):
        if name == "list_models":
            return {
                "data": [
                    {"id": model, "capabilities": [protocol]}
                    for model, protocol in pairs
                    if protocol != "scientific-batch-v1"
                ]
            }
        if name == "list_scientific_models":
            return {"data": [{"model_id": "scientific-app"}]}
        return {
            "model_id": arguments["model_id"],
            "contracts": [
                contract for tool, contract in records if tool["_meta"]["fs2_model_id"] == arguments["model_id"]
            ],
        }

    monkeypatch.setattr(v, "tool_data", tool_data)
    if missing:
        with pytest.raises(v.debug.CheckError, match="coverage_mismatch"):
            asyncio.run(v.inventory(Client(), tmp_path, ()))
    else:
        report, contracts = asyncio.run(v.inventory(Client(), tmp_path, ()))
        assert len(report) == len(pairs) == len(contracts)
        assert {row["model_id"] for row in report} >= {"independent-clone", "scientific-app"}
