from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from fs2_serve import request_telemetry as telemetry
from fs2_serve.models import Principal
from fs2_serve.request_telemetry import (
    InMemoryRequestTelemetryStore,
    RequestTelemetryMiddleware,
    classify_public_outcome,
    observe_mcp_result,
    observe_request_metadata,
    request_telemetry_context,
)


def principal():
    return Principal(
        token_id=uuid4(),
        token_prefix="visible-prefix",
        principal_id="actual-owner",
        tenant_id="tenant-a",
        scopes=frozenset(),
        models=frozenset({"qwen3-8b"}),
    )


async def exchange(app, *, path="/v1/models/qwen3-8b/invoke", chunks=(), store=None, state=None):
    store = store or InMemoryRequestTelemetryStore()
    incoming = list(chunks)
    outgoing = []
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"token=QUERY_SECRET",
        "headers": [(b"authorization", b"Bearer BEARER_SECRET")],
        "state": state or {},
    }

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        outgoing.append(message)

    await RequestTelemetryMiddleware(app, store=store, persist_timeout_seconds=0.01)(scope, receive, send)
    return store, outgoing


async def test_exact_streamed_bytes_actual_owner_and_final_chunk_clock(monkeypatch):
    clock = SimpleNamespace(value=10.0)
    monkeypatch.setattr(telemetry, "time", SimpleNamespace(monotonic=lambda: clock.value))
    owner = principal()
    operation_id = uuid4()

    async def app(scope, receive, send):
        assert (await receive())["body"] == b"PRIVATE_REQUEST"
        assert (await receive())["body"] == "é".encode()
        scope["state"]["principal"] = owner
        scope["state"]["model_id"] = "qwen3-8b"
        clock.value = 11
        await send(
            {
                "type": "http.response.start",
                "status": 202,
                "headers": [(b"x-fs2-operation-id", str(operation_id).encode())],
            }
        )
        clock.value = 12
        await send({"type": "http.response.body", "body": b"PRIVATE_RESULT", "more_body": True})
        clock.value = 13
        await send({"type": "http.response.body", "body": "好".encode(), "more_body": False})
        clock.value = 99  # Post-response background work is not response latency.

    store, outgoing = await exchange(
        app,
        chunks=[
            {"type": "http.request", "body": b"PRIVATE_REQUEST", "more_body": True},
            {"type": "http.request", "body": "é".encode(), "more_body": False},
        ],
    )
    (row,) = store.observations
    assert row.request_bytes == len(b"PRIVATE_REQUEST") + 2
    assert row.response_bytes == len(b"PRIVATE_RESULT") + 3
    assert row.response_duration_seconds == 3
    assert row.request_complete and row.response_complete
    assert row.operation_id == operation_id and row.model_id == "qwen3-8b"
    assert (row.tenant_id, row.principal_id, row.token_id) == (owner.tenant_id, owner.principal_id, owner.token_id)
    assert row.http_status == 202 and row.transport == "http"
    encoded = row.model_dump_json()
    assert not any(secret in encoded for secret in ("PRIVATE", "SECRET", "Bearer", "visible-prefix"))
    assert len(outgoing) == 3


async def test_unconsumed_request_is_unknown_not_zero_and_route_validation_failure_has_model():
    async def app(scope, receive, send):
        scope["path_params"] = {"model_id": "qwen3-8b"}
        await send({"type": "http.response.start", "status": 422})
        await send({"type": "http.response.body", "body": b"invalid"})

    store, _ = await exchange(app)
    (row,) = store.observations
    assert row.request_bytes is None and row.request_bytes_observed == 0
    assert not row.request_complete
    assert row.response_bytes == 7 and row.model_id == "qwen3-8b"
    assert row.principal_id is None and row.operation_id is None


async def test_disconnect_preserves_partial_bytes_and_does_not_invent_status():
    async def app(scope, receive, send):
        await receive()
        await receive()

    store, _ = await exchange(app, chunks=[{"type": "http.request", "body": b"abc", "more_body": True}])
    (row,) = store.observations
    assert row.disconnected and row.request_bytes_observed == 3
    assert row.request_bytes is None and row.response_bytes is None and row.http_status is None


async def test_response_failure_preserves_partial_result_without_replacing_original_exception():
    store = InMemoryRequestTelemetryStore()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("PRIVATE_EXCEPTION_CONTENT")

    with pytest.raises(RuntimeError, match="PRIVATE_EXCEPTION_CONTENT"):
        await exchange(app, store=store)
    (row,) = store.observations
    assert row.response_bytes is None and row.response_bytes_observed == 7
    assert row.error_type == "RuntimeError" and "PRIVATE" not in row.model_dump_json()


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_persistence_failure_does_not_replace_success_or_log_secrets(failure, caplog):
    class BrokenStore:
        async def record(self, observation):
            if failure == "timeout":
                await asyncio.sleep(10)
            raise RuntimeError("DATABASE_PASSWORD_MUST_NOT_LEAK")

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})

    _, outgoing = await exchange(app, store=BrokenStore())
    assert outgoing[-1]["body"] == b"ok"
    assert "request telemetry persistence failed" in caplog.text
    assert "DATABASE_PASSWORD" not in caplog.text


@pytest.mark.parametrize("path", ["/admin/api/v1/apps", "/metrics", "/health", "/.well-known/openid-configuration"])
async def test_admin_and_infrastructure_paths_are_not_transport_usage(path):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b""})

    store, _ = await exchange(app, path=path)
    assert not store.observations


async def test_mcp_actual_request_context_crosses_sdk_task_and_http200_tool_failure_is_distinct():
    owner = principal()

    async def app(scope, receive, send):
        async def sdk_handler():
            with request_telemetry_context(Request(scope)):
                observe_request_metadata(principal=owner, model_id="qwen3-8b", mcp_tool="invoke_model")
                observe_mcp_result({"isError": True})

        await asyncio.create_task(sdk_handler())
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"tool error"})

    store, _ = await exchange(app, path="/mcp")
    (row,) = store.observations
    assert row.transport == "mcp" and row.http_status == 200 and row.mcp_is_error is True
    assert row.mcp_tool == "invoke_model" and row.principal_id == "actual-owner"
    assert row.semantic_outcome == "failed" and row.admission_stage == "pre_admission"
    assert row.semantic_error_type == "mcp_tool_error"


async def test_replay_and_polling_are_three_requests_for_one_operation_with_isolated_auth_contexts():
    operation_id = uuid4()
    owner = principal()
    store = InMemoryRequestTelemetryStore()

    async def app(scope, receive, send):
        observe_request_metadata(principal=owner, model_id="qwen3-8b", operation_id=operation_id)
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b""})

    await asyncio.gather(*(exchange(app, store=store) for _ in range(3)))
    assert len(store.observations) == 3
    assert len({row.request_id for row in store.observations}) == 3
    assert {row.operation_id for row in store.observations} == {operation_id}

    async def unauthenticated(scope, receive, send):
        await send({"type": "http.response.start", "status": 401})
        await send({"type": "http.response.body", "body": b""})

    await exchange(unauthenticated, store=store)
    assert store.observations[-1].principal_id is None
    assert store.observations[-1].operation_id is None


def test_invalid_result_identity_and_non_http_context_do_not_raise_or_capture_payload():
    scope = {"type": "http", "state": {}}
    with request_telemetry_context(Request(scope)):
        observe_mcp_result({"operation": {"id": "not-an-id", "model_id": "bad\nmodel"}, "result": "PRIVATE"})
    assert scope["state"] == {}
    with request_telemetry_context(None):
        observe_mcp_result(json.loads('{"id":"not-a-uuid"}'))


def classify(body, **updates):
    values = dict(
        path="/mcp",
        http_status=200,
        response_body=json.dumps(body).encode() if isinstance(body, dict) else body,
        response_complete=True,
        disconnected=False,
        process_error_type=None,
        state={"mcp_tool": "cosmos3_nano_generate_media_native", "model_id": "cosmos3-nano"},
        operation_id=None,
    )
    values.update(updates)
    return classify_public_outcome(**values)


def test_jsonrpc_http200_schema_rejection_is_failed_pre_admission_without_usage_identity():
    result = classify(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "error": {
                "code": -32602,
                "message": "Invalid request parameters",
                "data": {"type": "model_input_validation", "private_input": "must-not-be-retained"},
            },
        }
    )
    assert result == {
        "semantic_outcome": "failed",
        "jsonrpc_error_code": -32602,
        "semantic_error_type": "model_input_validation",
        "admission_stage": "pre_admission",
        "mcp_is_error": True,
        "operation_id": None,
        "model_id": "cosmos3-nano",
    }
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    ("status", "expected", "error_type"),
    [
        ("queued", "accepted", None),
        ("running", "accepted", None),
        ("succeeded", "succeeded", None),
        ("failed", "failed", "upstream_runtime_failure"),
        ("cancelled", "cancelled", "customer_cancelled"),
        ("expired", "timed_out", "execution_deadline_exceeded"),
    ],
)
def test_mcp_operation_terminal_semantics_are_not_inferred_from_http(status, expected, error_type):
    operation_id = uuid4()
    operation = {"id": str(operation_id), "model_id": "cosmos3-nano", "status": status}
    if error_type:
        operation["error_class"] = error_type
    result = classify(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "result": {"isError": False, "structuredContent": {"operation": operation}},
        }
    )
    assert result["semantic_outcome"] == expected
    assert result["admission_stage"] == "admitted" and result["operation_id"] == operation_id
    assert result["semantic_error_type"] == error_type


def test_structured_tool_error_auth_timeout_disconnect_and_malformed_are_distinct():
    tool_error = classify(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "result": {
                "isError": True,
                "structuredContent": {"error": {"type": "route_unavailable", "retryable": True}},
            },
        }
    )
    assert (tool_error["semantic_outcome"], tool_error["semantic_error_type"]) == (
        "failed",
        "route_unavailable",
    )
    authentication = classify(b"unauthorized", http_status=401)
    assert authentication["semantic_error_type"] == "authentication_failed"
    timeout = classify(b"", response_complete=False, process_error_type="TimeoutError")
    assert timeout["semantic_outcome"] == "timed_out" and timeout["semantic_error_type"] == "request_timeout"
    disconnected = classify(b"", response_complete=False, disconnected=True)
    assert disconnected["semantic_outcome"] == "cancelled"
    malformed = classify(b"not-json")
    assert malformed["semantic_outcome"] == "failed"
    assert malformed["semantic_error_type"] == "malformed_mcp_response"


def test_sse_jsonrpc_error_is_classified_without_persisting_response_payload():
    body = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"PRIVATE"}}\n\n'
    result = classify(body)
    assert result["jsonrpc_error_code"] == -32603
    assert result["semantic_error_type"] == "jsonrpc_internal_error"
    assert "PRIVATE" not in str(result)


def test_protocol_or_handler_failure_cannot_be_erased_by_a_later_success_event():
    operation_id = uuid4()
    success = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "structuredContent": {
                    "operation": {
                        "id": str(operation_id),
                        "model_id": "cosmos3-nano",
                        "status": "succeeded",
                    }
                }
            },
        }
    ).encode()
    protocol_error = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32602}}'
    mixed = b"data: " + protocol_error + b"\n\ndata: " + success + b"\n\n"
    result = classify(mixed)
    assert result["semantic_outcome"] == "failed"
    assert result["jsonrpc_error_code"] == -32602

    trusted = classify(
        {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {}}},
        state={
            "mcp_tool": "cosmos3_nano_video_to_video",
            "model_id": "cosmos3-nano",
            "semantic_outcome": "failed",
            "semantic_error_type": "upstream_runtime_failure",
            "admission_stage": "admitted",
            "mcp_is_error": True,
        },
    )
    assert trusted["semantic_outcome"] == "failed"
    assert trusted["semantic_error_type"] == "upstream_runtime_failure"
