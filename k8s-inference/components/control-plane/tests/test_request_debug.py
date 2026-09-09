"""Actual ASGI bytes and encrypted-debug storage boundaries; no live services."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from fs2_serve.models import Principal
from fs2_serve.request_debug import (
    DebugCaptureMiddleware,
    DebugExchange,
    InMemoryDebugStore,
    body_capture,
    credential_values,
    persist_debug_exchange,
    redact_headers,
    redact_query,
)
from fs2_serve.request_telemetry import (
    InMemoryRequestTelemetryStore,
    RequestTelemetryMiddleware,
    current_request_id,
    observe_request_metadata,
)

NOW = datetime(2026, 9, 9, 8, tzinfo=UTC)


def owner():
    return Principal(
        token_id=uuid4(),
        token_prefix="test-prefix",
        principal_id="customer",
        tenant_id="tenant-a",
        models=frozenset({"boltz2"}),
        scopes=frozenset(),
    )


def row(**updates):
    values = dict(
        id=uuid4(),
        source="public",
        request_id=uuid4(),
        operation_id=None,
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        tenant_id="tenant-a",
        principal_id="customer",
        endpoint="/v1/models/boltz2:invoke",
        method="POST",
        http_status=422,
        model_id="boltz2",
        request_body=body_capture(b'{"sequence":"ACDEFG"}', "application/json", True),
        response_body=body_capture(b'{"detail":"polymers is required"}', "application/json", True),
    )
    values.update(updates)
    return DebugExchange(**values)


async def capture(
    app,
    *,
    chunks=(),
    path="/v1/models/boltz2:invoke",
    headers=(),
    query=b"",
    state=None,
    store=None,
    resolver=None,
    telemetry_store=None,
    send_error=False,
):
    store = store or InMemoryDebugStore()
    incoming, outgoing = list(chunks), []
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": list(headers),
        "query_string": query,
        "state": state or {},
    }

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        if send_error and message["type"] == "http.response.body":
            raise OSError("connection closed")
        outgoing.append(message)

    if telemetry_store is not None:
        app = RequestTelemetryMiddleware(app, store=telemetry_store)
    await DebugCaptureMiddleware(app, store=store, principal_resolver=resolver, persist_timeout_seconds=0.05)(
        scope, receive, send
    )
    return store, outgoing, scope


@pytest.mark.parametrize(
    "raw,content_type",
    [
        (b'{ "sequence": "ACDEFG", "nested": [1,2], "max_tokens": 8 }', "application/json"),
        (b'{"sequence":"ACDEFG",', "application/json"),
        (b'data: {"choices":[{"text":"hello"}]}\n\ndata: [DONE]\n\n', "text/event-stream"),
        (b"\x00\xff\x80\x01binary", "application/octet-stream"),
        (b"z" * (1024 * 1024 + 7), "text/plain"),
    ],
)
def test_unredacted_bodies_roundtrip_exactly_without_new_size_cap(raw, content_type):
    body = body_capture(raw, content_type, True)
    decoded = body.data.encode() if body.encoding == "utf-8" else base64.b64decode(body.data)
    assert decoded == raw and body.observed_bytes == len(raw)
    assert body.complete and not body.redacted


@pytest.mark.parametrize(
    "raw",
    [
        b'{"api_key":"JSON_SECRET","sequence":"ACDEFG"}',
        b'{"api_key":"JSON_SECRET","sequence":"ACDEFG",',
        b'data: {"access_token":"JSON_SECRET","sequence":"ACDEFG"}\n\n',
    ],
)
def test_identifiable_json_credentials_redacted_even_malformed_or_sse(raw):
    body = body_capture(raw, "application/json", False)
    assert body.redacted and "JSON_SECRET" not in body.data and "ACDEFG" in body.data
    assert body.observed_bytes == len(raw) and not body.complete


def test_query_headers_and_partial_known_credentials_are_redacted_without_changing_inputs():
    headers = [
        ("Authorization", "Bearer ACCESS_SECRET"),
        ("x-auth-token", "CUSTOM_SECRET"),
        ("Set-Cookie", "session=COOKIE_SECRET; Secure"),
        ("Content-Type", "application/json"),
    ]
    known = credential_values(headers, "access_token=QUERY_SECRET", b'{"apiKey":"BODY_SECRET"}')
    assert {"ACCESS_SECRET", "CUSTOM_SECRET", "COOKIE_SECRET", "QUERY_SECRET", "BODY_SECRET"} <= set(known)
    assert redact_query("input_id=a%20b&access_token=QUERY_SECRET&count=2", known) == (
        "input_id=a%20b&access_token=[REDACTED]&count=2"
    )
    cleaned = redact_headers(headers, known)
    assert cleaned[-1] == ("Content-Type", "application/json")
    assert all(value == "[REDACTED]" for _, value in cleaned[:-1])
    assert body_capture(b"failure CUSTOM_SEC", "text/plain", False, known).data == "failure [REDACTED]"


async def test_rejected_json_complete_capture_owner_fallback_and_credential_echo():
    request = b'{"api_key":"BODY_SECRET","sequence":"ACDEFG",'
    response = b'{"detail":[{"input":"BODY_SECRET","msg":"invalid JSON"}]}'
    customer = owner()
    resolutions = []

    async def resolver(token):
        resolutions.append(token)
        return customer

    async def app(scope, receive, send):
        assert (await receive())["body"] == request  # Capture does not mutate model input.
        await send({"type": "http.response.start", "status": 422, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": response})

    store, outgoing, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": request}],
        resolver=resolver,
        headers=[(b"authorization", b"Bearer ACCESS_SECRET")],
    )
    (exchange,) = store.exchanges.values()
    assert outgoing[-1]["body"] == response and resolutions == ["ACCESS_SECRET"]
    assert exchange.request_body.complete and exchange.response_body.complete and exchange.http_status == 422
    assert exchange.tenant_id == customer.tenant_id and exchange.token_id == customer.token_id
    assert exchange.operation_id is None and exchange.model_id == "boltz2"
    assert "BODY_SECRET" not in exchange.model_dump_json() and "ACCESS_SECRET" not in exchange.model_dump_json()
    assert "ACDEFG" in exchange.request_body.data


async def test_unread_unauthorized_body_is_explicit_and_not_drained():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 401})
        await send({"type": "http.response.body", "body": b"unauthorized"})

    async def resolver(token):
        raise ValueError("invalid secret must not be logged")

    store, _, _ = await capture(app, resolver=resolver, headers=[(b"authorization", b"Bearer BAD_SECRET")])
    (exchange,) = store.exchanges.values()
    assert not exchange.request_body.complete and exchange.request_body.observed_bytes == 0
    assert exchange.response_body.complete and exchange.tenant_id is None
    assert await store.get(exchange.id, "tenant-a") is None
    assert (await store.list(tenant_id="tenant-a")).items == []
    assert len((await store.list()).items) == 1


async def test_disconnected_partial_upload_has_no_invented_status():
    async def app(scope, receive, send):
        await receive()
        await receive()

    store, _, _ = await capture(app, chunks=[{"type": "http.request", "body": b"partial", "more_body": True}])
    (exchange,) = store.exchanges.values()
    assert exchange.disconnected and exchange.http_status is None
    assert exchange.request_body.data == "partial" and not exchange.request_body.complete
    assert not exchange.response_body.complete


async def test_send_failure_preserves_observed_chunk_and_original_exception():
    store = InMemoryDebugStore()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"generated but delivery failed"})

    with pytest.raises(OSError, match="connection closed"):
        await capture(app, store=store, send_error=True)
    (exchange,) = store.exchanges.values()
    assert exchange.response_body.data == "generated but delivery failed"
    assert exchange.disconnected and not exchange.response_body.complete and exchange.error_type == "OSError"


@pytest.mark.parametrize("path", ["/admin/api/v1/keys", "/v1/tokens", "/v1/tokens/123", "/metrics"])
async def test_admin_key_issuance_and_infrastructure_not_captured(path):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"must not retain"})

    store, outgoing, _ = await capture(app, path=path)
    assert not store.exchanges and outgoing[-1]["body"] == b"must not retain"


async def test_stable_request_id_correlates_telemetry_mcp_and_operation_without_trusting_header():
    telemetry = InMemoryRequestTelemetryStore()
    operation_id, untrusted_id = uuid4(), uuid4()
    identities = []

    async def app(scope, receive, send):
        await receive()
        identities.append(current_request_id())
        observe_request_metadata(
            principal=owner(), model_id="boltz2", operation_id=operation_id, mcp_tool="invoke_model"
        )
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b'data: {"ok":true}\n\n', "more_body": True})
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n"})

    store, _, _ = await capture(
        app,
        path="/mcp",
        chunks=[{"type": "http.request", "body": b"{}"}],
        telemetry_store=telemetry,
        headers=[(b"x-request-id", str(untrusted_id).encode())],
    )
    (debug,) = store.exchanges.values()
    assert debug.request_id == telemetry.observations[0].request_id == identities[0]
    assert debug.request_id != untrusted_id and debug.operation_id == operation_id
    assert debug.response_body.data == 'data: {"ok":true}\n\ndata: [DONE]\n\n'
    assert debug.mcp_tool == "invoke_model" and debug.response_body.complete


async def test_summary_filters_cursor_and_idempotent_record_do_not_expose_details():
    store = InMemoryDebugStore()
    operation_id = uuid4()
    rows = [row(operation_id=operation_id) for _ in range(3)]
    for item in rows:
        await store.record(item)
    await store.record(rows[0])
    await store.record(row(tenant_id=None))
    await store.record(row(tenant_id="foreign"))
    page = await store.list(
        model_id="boltz2",
        operation_id=operation_id,
        tenant_id="tenant-a",
        limit=2,
        from_at=NOW,
        to_at=NOW + timedelta(seconds=1),
    )
    assert len(page.items) == 2 and page.next_cursor
    last = await store.list(
        model_id="boltz2", operation_id=operation_id, tenant_id="tenant-a", limit=2, cursor=page.next_cursor
    )
    assert len(last.items) == 1 and last.next_cursor is None
    assert {item.id for item in page.items + last.items} == {item.id for item in rows}
    assert not {"request_body", "response_body", "query_string", "request_headers", "error_detail"} & set(
        page.items[0].model_dump()
    )
    assert await store.get(rows[0].id, "foreign") is None
    assert (await store.list(to_at=NOW)).items == []
    with pytest.raises(ValueError, match="cursor"):
        await store.list(cursor="bad")


@pytest.mark.parametrize("timeout", [False, True])
async def test_persistence_failure_observable_without_customer_data_or_response_changes(caplog, timeout):
    class Broken:
        async def record(self, exchange):
            if timeout:
                await asyncio.sleep(5)
            raise RuntimeError("DB_SECRET")

    assert not await persist_debug_exchange(Broken(), row(), persist_timeout_seconds=0.01)
    assert "request debug persistence failed" in caplog.text
    assert "DB_SECRET" not in caplog.text and "ACDEFG" not in caplog.text
