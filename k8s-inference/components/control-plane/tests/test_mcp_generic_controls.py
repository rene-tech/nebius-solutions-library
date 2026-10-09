"""Generic envelope regressions over the real MCP transport, with local stores.

The route matrix uses synthetic bindings and a recording stub runtime. It proves
control handling and durable dispatch, not GPU or customer-client acceptance.
"""

import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from conftest import CATALOG_ROOT
from mcp.shared.exceptions import MCPError
from test_api_mcp import bound_model_registry, build_runtime
from test_mcp_model_tools_http import _app, _connection, _data, _key

from fs2_serve.mcp_server import GATEWAY_SUBMISSION_CONTROLS, _admit, _normalize_invoke_arguments
from fs2_serve.model_input_contracts import contract_for
from fs2_serve.registry import Registry
from fs2_serve.runtime import StubRuntimeClient

CONTROL_KEY = "stockholm-generic-envelope-20260917"
NON_COSMOS_MODELS = tuple(
    model.removesuffix(".json")
    for model in json.loads((CATALOG_ROOT / "catalog.json").read_text())["model_files"]
    if not model.startswith("cosmos")
)
# This is an explicit synthetic route selection, not a "newest filename" rule.
# Historical descriptors remain in the catalog for reproducibility; retaining a
# second image must neither break this matrix nor silently change its runtime.
PUBLISHED_RUNTIME_FIXTURES = {
    "boltz2": "boltz2-portable-h100.json",
    "diffdock": "diffdock-portable-h100-http-identity-20260919.json",
    "evo2-40b": "evo2-40b-scientific-h100-20260918.json",
    "genmol": "genmol-portable-h100-20260918.json",
    "molmim": "molmim-portable-h100-20260918.json",
    "msa-search-pdb70": "msa-search-pdb70-portable-cpu.json",
    "openfold2": "openfold2-portable-h100.json",
    "openfold3": "openfold3-portable-h100.json",
    "proteinmpnn": "proteinmpnn-portable-h100-20260918.json",
}


@pytest.mark.parametrize(
    "outer,inner,expected",
    [
        ({}, {}, {}),
        ({"idempotency_key": CONTROL_KEY}, {}, {"idempotency_key": CONTROL_KEY}),
        ({}, {"idempotency_key": CONTROL_KEY}, {"idempotency_key": CONTROL_KEY}),
        ({}, {"wait_seconds": 3}, {"wait_seconds": 3}),
        ({"wait_seconds": 3.0}, {"wait_seconds": 3}, {"wait_seconds": 3}),
        ({"wait_seconds": 0}, {"wait_seconds": 0}, {"wait_seconds": 0}),
        ({"idempotency_key": None}, {"idempotency_key": None}, {"idempotency_key": None}),
        (
            {"idempotency_key": CONTROL_KEY, "wait_seconds": 3},
            {"idempotency_key": CONTROL_KEY, "wait_seconds": 3},
            {"idempotency_key": CONTROL_KEY, "wait_seconds": 3},
        ),
    ],
)
def test_normalization_preserves_presence_and_model_objects(outer, inner, expected):
    # A model input can legitimately contain these names deeper in JSON schemas.
    payload = {"schema": {"properties": {"wait_seconds": {"type": "number"}}}}
    arguments = {"model_id": "test-model", "protocol": "native", "payload": payload | inner, **outer}
    original = deepcopy(arguments)
    result = _normalize_invoke_arguments(arguments, max_wait_seconds=5)
    assert result == {"model_id": "test-model", "protocol": "native", "payload": payload, **expected}
    assert arguments == original


@pytest.mark.parametrize(
    "outer,inner,field",
    [
        ({"wait_seconds": 0}, {"wait_seconds": 1}, "wait_seconds"),
        ({"wait_seconds": 1}, {"wait_seconds": 0}, "wait_seconds"),
        ({"idempotency_key": None}, {"idempotency_key": CONTROL_KEY}, "idempotency_key"),
        ({"idempotency_key": CONTROL_KEY}, {"idempotency_key": None}, "idempotency_key"),
        ({"idempotency_key": CONTROL_KEY}, {"idempotency_key": "different-control-key"}, "idempotency_key"),
    ],
)
def test_duplicate_conflicts_are_structured_and_do_not_reflect_values(outer, inner, field):
    with pytest.raises(MCPError) as failure:
        _normalize_invoke_arguments({"payload": inner, **outer}, max_wait_seconds=5)
    assert failure.value.error.code == -32602
    assert failure.value.error.data == {
        "type": "gateway_control_validation",
        "durable_admission": False,
        "issues": [{"field": f"/payload/{field}", "rule": "conflict"}],
    }
    assert CONTROL_KEY not in str(failure.value)
    assert "different-control-key" not in str(failure.value)


@pytest.mark.parametrize("location", ["outer", "nested"])
@pytest.mark.parametrize(
    "control,value,rule",
    [
        ("idempotency_key", True, "type"),
        ("idempotency_key", 123, "type"),
        ("idempotency_key", {}, "type"),
        ("idempotency_key", "", "length"),
        ("idempotency_key", "x" * 201, "length"),
        ("wait_seconds", None, "type"),
        ("wait_seconds", True, "type"),
        ("wait_seconds", "0", "type"),
        ("wait_seconds", [], "type"),
        ("wait_seconds", -1, "bound"),
        ("wait_seconds", 6, "bound"),
        ("wait_seconds", float("nan"), "bound"),
        ("wait_seconds", float("inf"), "bound"),
        ("wait_seconds", 10**1000, "bound"),
    ],
)
def test_both_control_locations_are_validated_without_coercion(location, control, value, rule):
    arguments = {"payload": {control: value}} if location == "nested" else {"payload": {}, control: value}
    with pytest.raises(MCPError) as failure:
        _normalize_invoke_arguments(arguments, max_wait_seconds=5)
    assert failure.value.error.data["issues"] == [
        {"field": f"{'/payload' if location == 'nested' else ''}/{control}", "rule": rule}
    ]


def _routable(registry, model_id):
    selected = registry if model_id == "qwen3-8b" else bound_model_registry(registry, model_id)
    model = selected.get(model_id)
    # Synthetic publication only; no live grant, binding or runtime is changed.
    if declaration_name := PUBLISHED_RUNTIME_FIXTURES.get(model_id):
        declaration_path = CATALOG_ROOT / "deployment-runtimes" / declaration_name
        declaration = json.loads(declaration_path.read_text())
        record = declaration["record"]
        assert declaration["model_id"] == record["model"]["id"] == model_id
        assert declaration["qualification"]["active_runtime"]["runtime_image_digest"] == (
            record["runtime"]["image"]["digest"]
        )
        model = replace(
            model,
            variant_id=declaration["variant_id"],
            gateway=replace(
                model.gateway,
                runtime_kind=record["runtime"]["kind"],
                runtime_image_digest=record["runtime"]["image"]["digest"],
                gpu_class=record["resources"]["gpu"]["class"],
                qualification=None,
                binding=replace(
                    model.binding,
                    backend_runtime_image_digest=record["runtime"]["image"]["digest"],
                    backend_gpu_class=record["resources"]["gpu"]["class"],
                ),
            ),
        )
    return Registry(
        selected.catalog,
        {model_id: replace(model, gateway=replace(model.gateway, mcp_invocable=True, mcp_discoverable=True))},
    )


@pytest.mark.parametrize("model_id", PUBLISHED_RUNTIME_FIXTURES)
def test_route_fixture_uses_explicit_published_identity(registry, model_id):
    declaration = json.loads(
        (CATALOG_ROOT / "deployment-runtimes" / PUBLISHED_RUNTIME_FIXTURES[model_id]).read_text()
    )
    variant = json.loads((CATALOG_ROOT / "contracts/model-variants.json").read_text())["variants"][
        declaration["variant_id"]
    ]
    record = declaration["record"]
    assert variant["base_model_id"] == variant["exposed_model_id"] == model_id
    for field in ("kind", "repository", "revision"):
        assert record["model"]["source"][field] == variant["source"][field]

    model = _routable(registry, model_id).get(model_id)
    assert model.variant_id == declaration["variant_id"]
    assert model.gateway.runtime_image_digest == model.binding.backend_runtime_image_digest == (
        record["runtime"]["image"]["digest"]
    )
    assert model.gateway.gpu_class == model.binding.backend_gpu_class == record["resources"]["gpu"]["class"]


@pytest.mark.parametrize("model_id", ["diffdock", "genmol", "molmim", "proteinmpnn"])
def test_retained_runtime_history_does_not_ambiguously_select_fixture(registry, monkeypatch, model_id):
    directory = CATALOG_ROOT / "deployment-runtimes"
    historical = json.loads((directory / f"{model_id}-portable-h100.json").read_text())
    published = json.loads((directory / PUBLISHED_RUNTIME_FIXTURES[model_id]).read_text())
    assert historical["record"]["runtime"]["image"]["digest"] != published["record"]["runtime"]["image"]["digest"]

    original_glob = type(directory).glob

    def unordered_candidates_are_not_identity(path, *args, **kwargs):
        if path == directory:
            pytest.fail("Synthetic route selection must not depend on a descriptor glob")
        return original_glob(path, *args, **kwargs)

    monkeypatch.setattr(type(directory), "glob", unordered_candidates_are_not_identity)
    assert _routable(registry, model_id).get(model_id).gateway.runtime_image_digest == (
        published["record"]["runtime"]["image"]["digest"]
    )


class RecordingRuntime(StubRuntimeClient):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def invoke(self, model, operation, request_body):
        self.requests.append((model.id, operation.protocol, json.loads(request_body)))
        return await super().invoke(model, operation, request_body)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["qwen3-8b", "openfold2", "boltz2"])
async def test_http_named_generic_legacy_and_equal_duplicates_replay_one_operation(registry, cipher, hasher, model_id):
    selected = _routable(registry, model_id)
    model = selected.get(model_id)
    (protocol,) = model.gateway.protocols
    payload = deepcopy(contract_for(model, protocol).examples[0])
    payload.pop("model", None)
    runtime = build_runtime(selected, cipher, hasher)
    app = _app(runtime)
    key = await _key(runtime, models=(model_id,), max_concurrency=1)
    controls = {"idempotency_key": CONTROL_KEY, "wait_seconds": 0}
    envelope = {"model_id": model_id, "protocol": protocol}
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        named_tool = f"{model.binding.mcp_tool_name}_{protocol.replace('-', '_')}"
        admitted = _data(await client.call_tool(named_tool, payload | controls))
        for arguments in (
            {**envelope, "payload": payload, **controls},
            {**envelope, "payload": payload | controls},
            {**envelope, "payload": payload | controls, **controls},
        ):
            replay = _data(await client.call_tool("invoke_model", arguments))
            assert replay["id"] == admitted["id"] and replay["reused"] is True
        assert len(runtime.store.operations) == 1
        claimed = await runtime.store.claim_operation("envelope-test", lease_seconds=30)
        assert claimed is not None
        persisted = json.loads(
            await runtime.store.read_request_payload(
                claimed.id, worker_id=claimed.worker_id, fencing_token=claimed.fencing_token
            )
        )
        assert not GATEWAY_SUBMISSION_CONTROLS.intersection(persisted)
        expected = payload | ({"model": model_id} if protocol.startswith("openai-") else {})
        assert persisted == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outer,inner,rule",
    [
        ({"idempotency_key": CONTROL_KEY}, {"idempotency_key": "different-control-key"}, "conflict"),
        ({"idempotency_key": None}, {"idempotency_key": CONTROL_KEY}, "conflict"),
        ({"wait_seconds": 0}, {"wait_seconds": 1}, "conflict"),
        ({}, {"wait_seconds": "0"}, "type"),
        ({}, {"idempotency_key": ""}, "length"),
        ({}, {"wait_seconds": 9999}, "bound"),
    ],
)
async def test_http_invalid_controls_never_reach_admission(registry, cipher, hasher, monkeypatch, outer, inner, rule):
    runtime = build_runtime(registry, cipher, hasher)
    admission = AsyncMock(wraps=runtime.admission.admit)
    monkeypatch.setattr(runtime.admission, "admit", admission)
    app = _app(runtime)
    key = await _key(runtime)
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        with pytest.raises(MCPError) as failure:
            await client.call_tool(
                "invoke_model",
                {"model_id": "qwen3-8b", "protocol": "openai-chat", "payload": inner, **outer},
            )
        assert failure.value.error.data["type"] == "gateway_control_validation"
        assert failure.value.error.data["durable_admission"] is False
        assert failure.value.error.data["issues"][0]["rule"] == rule
    admission.assert_not_awaited()
    assert not runtime.store.operations


@pytest.mark.asyncio
async def test_http_nested_wait_controls_actual_wait_duration(registry, cipher, hasher, monkeypatch):
    runtime = build_runtime(registry, cipher, hasher)

    async def wait(operation_id, *, tenant_id, seconds):
        assert seconds == 0.125
        return await runtime.store.get_operation(operation_id, tenant_id=tenant_id)

    waiting = AsyncMock(side_effect=wait)
    monkeypatch.setattr(runtime.admission, "wait", waiting)
    app = _app(runtime)
    key = await _key(runtime)
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        _data(
            await client.call_tool(
                "invoke_model",
                {
                    "model_id": "qwen3-8b",
                    "protocol": "openai-chat",
                    "payload": {
                        "messages": [{"role": "user", "content": "synthetic wait fixture"}],
                        "idempotency_key": CONTROL_KEY,
                        "wait_seconds": 0.125,
                    },
                },
            )
        )
    waiting.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", NON_COSMOS_MODELS)
async def test_http_non_cosmos_route_matrix_does_not_persist_or_dispatch_controls(registry, cipher, hasher, model_id):
    selected = _routable(registry, model_id)
    model = selected.get(model_id)
    (protocol,) = model.gateway.protocols
    runtime = build_runtime(selected, cipher, hasher)
    recorder = RecordingRuntime()
    runtime.admission.runtime = recorder
    app = _app(runtime)
    key = await _key(runtime, models=(model_id,), max_concurrency=5)
    # Deliberately tests envelopes only. Scientific input and semantic results
    # are validated by their own contract/runtime suites, not this stub matrix.
    payload = {"messages": [{"role": "user", "content": "synthetic envelope fixture"}]}
    async with app.router.lifespan_context(app), _connection(runtime, app, key) as client:
        result = _data(
            await client.call_tool(
                "invoke_model",
                {
                    "model_id": model_id,
                    "protocol": protocol,
                    "payload": payload | {"idempotency_key": CONTROL_KEY, "wait_seconds": 0},
                },
            )
        )
        claimed = await runtime.store.claim_operation("envelope-matrix", lease_seconds=30)
        assert claimed is not None
        await runtime.admission._execute(claimed)
        assert recorder.requests == [
            (model_id, protocol, payload | ({"model": model_id} if protocol.startswith("openai-") else {}))
        ]
        status = _data(await client.call_tool("get_operation", {"operation_id": result["id"]}))
        assert status["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["native", "openai-chat"])
@pytest.mark.parametrize("control", sorted(GATEWAY_SUBMISSION_CONTROLS))
async def test_shared_admission_boundary_rejects_controls_if_adapter_is_bypassed(
    registry, cipher, hasher, monkeypatch, protocol, control
):
    runtime = build_runtime(registry, cipher, hasher)
    monkeypatch.setattr("fs2_serve.mcp_server._principal", lambda: None)
    admission = AsyncMock(wraps=runtime.admission.admit)
    monkeypatch.setattr(runtime.admission, "admit", admission)
    with pytest.raises(MCPError) as failure:
        await _admit(
            runtime,
            model_id="qwen3-8b",
            protocol=protocol,
            operation="chat",
            payload={control: "must-not-leak"},
            idempotency_key=CONTROL_KEY,
            wait_seconds=0,
            traceparent=None,
        )
    assert failure.value.error.data["issues"] == [{"field": f"/payload/{control}", "rule": "reserved"}]
    admission.assert_not_awaited()
