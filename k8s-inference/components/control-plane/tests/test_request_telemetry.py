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
