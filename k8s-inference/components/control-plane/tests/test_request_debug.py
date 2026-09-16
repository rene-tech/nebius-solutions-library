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
    DebugCapturePolicy,
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
from fs2_serve.settings import Settings

NOW = datetime(2026, 9, 9, 8, tzinfo=UTC)
_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
# Default policy for middleware tests: scoped to the default boltz2 path, bounded
# by a far-future expiry. Individual tests pass their own policy when needed.
_TEST_POLICY = DebugCapturePolicy(enabled=True, models=frozenset({"boltz2"}), expires_at=_FUTURE)


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
    max_body_bytes=None,
    policy=None,
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
    await DebugCaptureMiddleware(
        app,
        store=store,
        principal_resolver=resolver,
        persist_timeout_seconds=0.05,
        max_body_bytes=max_body_bytes,
        policy=policy or _TEST_POLICY,
    )(scope, receive, send)
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


def test_body_capture_bounds_stored_size_and_flags_truncation():
    """SAI-01: a stored debug body is a bounded prefix, not the whole payload."""
    payload = b'{"sequence":"' + b"A" * 5000 + b'"}'
    capped = body_capture(payload, "application/json", complete=True, max_bytes=256)
    assert capped.truncated is True
    assert capped.observed_bytes == len(payload)
    assert len(_stored_bytes(capped)) <= 256
    # A small body under the ceiling is stored exactly and not marked truncated.
    small = b'{"sequence":"ACDEFG"}'
    kept = body_capture(small, "application/json", complete=True, max_bytes=256)
    assert kept.truncated is False
    assert kept.data == small.decode()
    # Truncation still redacts credentials found in the retained prefix.
    secret = b'{"api_key":"nvapi-' + b"z" * 40 + b'","pad":"' + b"P" * 5000 + b'"}'
    redacted = body_capture(secret, "application/json", complete=True, max_bytes=256)
    assert redacted.truncated is True
    assert b"nvapi-zzzz" not in _stored_bytes(redacted)


def test_body_capture_redacts_before_the_cap_so_no_credential_prefix_survives():
    """SAI-01: a credential straddling the cap must not leak as a truncated prefix.

    Redaction runs on the complete observed body before the stored-byte cap, so
    even the leading bytes of a known credential positioned across the boundary
    are gone from the retained prefix.
    """
    credential = b"SUPERSECRETCREDENTIAL0123456789"  # spans the cap boundary
    body = b"A" * 250 + credential + b"B" * 5000
    capped = body_capture(body, "text/plain", complete=True, known_credentials=[credential], max_bytes=256)
    assert capped.truncated is True and capped.redacted is True
    assert capped.observed_bytes == len(body)
    stored = _stored_bytes(capped)
    assert len(stored) <= 256
    # No prefix of the credential (down to 8 bytes) survives in the stored bytes.
    assert all(credential[:size] not in stored for size in range(8, len(credential) + 1))


def test_body_capture_cap_holds_even_when_redaction_expands_the_body():
    """SAI-01: redaction can grow the body, but the stored copy stays within the cap."""
    credential = b"SECRETKEY"
    body = b"X" * 250 + credential  # 259 bytes; redaction -> 260 bytes (> cap)
    # Without a cap, redaction expands past the original length, proving growth.
    grown = body_capture(body, "text/plain", complete=True, known_credentials=[credential])
    assert len(_stored_bytes(grown)) > len(body) and grown.truncated is False
    # With the cap, the stored copy is bounded and the credential is gone.
    capped = body_capture(body, "text/plain", complete=True, known_credentials=[credential], max_bytes=256)
    assert capped.truncated is True and capped.observed_bytes == len(body)
    stored = _stored_bytes(capped)
    assert len(stored) <= 256 and credential not in stored


def _stored_bytes(body):
    return body.data.encode() if body.encoding == "utf-8" else base64.b64decode(body.data)


async def test_middleware_caps_stored_body_size_but_reports_true_observed_bytes():
    big_response = b'{"result":"' + b"R" * 20000 + b'"}'

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": big_response, "more_body": False})

    big_request = b'{"input":"' + b"Q" * 20000 + b'"}'
    store, _, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": big_request, "more_body": False}],
        headers=[(b"content-type", b"application/json")],
        max_body_bytes=512,
    )
    (exchange,) = list(store.exchanges.values())
    assert exchange.request_body.truncated and exchange.response_body.truncated
    assert len(_stored_bytes(exchange.request_body)) <= 512
    assert len(_stored_bytes(exchange.response_body)) <= 512
    # observed_bytes still reports the full wire length, not the stored prefix.
    assert exchange.request_body.observed_bytes == len(big_request)
    assert exchange.response_body.observed_bytes == len(big_response)


def test_capture_policy_is_scoped_and_time_bounded():
    """SAI-01: no global capture; a scope AND a bounded future expiry are mandatory."""
    now = NOW
    future = now + timedelta(hours=1)
    # Disabled: never captures.
    assert DebugCapturePolicy(enabled=False, models=frozenset({"m"}), expires_at=future).should_capture(
        tenant_id="t", model_id="m", now=now
    ) is False
    # Enabled but unscoped: fail closed (there is no global capture switch).
    assert DebugCapturePolicy(enabled=True, expires_at=future).should_capture(
        tenant_id="t", model_id="m", now=now
    ) is False
    # Enabled and scoped but no expiry: fail closed (a bounded window is mandatory).
    assert DebugCapturePolicy(enabled=True, models=frozenset({"m"})).should_capture(
        tenant_id="t", model_id="m", now=now
    ) is False
    # Tenant-scoped: only the allowlisted tenant, and never an unauthenticated request.
    tenant = DebugCapturePolicy(enabled=True, tenants=frozenset({"t1"}), expires_at=future)
    assert tenant.should_capture(tenant_id="t1", model_id="m", now=now) is True
    assert tenant.should_capture(tenant_id="t2", model_id="m", now=now) is False
    assert tenant.should_capture(tenant_id=None, model_id="m", now=now) is False
    # Model/App-scoped: all tenants for that App, including pre-admission (tenant None).
    model = DebugCapturePolicy(enabled=True, models=frozenset({"boltz2"}), expires_at=future)
    assert model.should_capture(tenant_id=None, model_id="boltz2", now=now) is True
    assert model.should_capture(tenant_id="t1", model_id="qwen3-8b", now=now) is False
    # Both set: must match both.
    both = DebugCapturePolicy(enabled=True, tenants=frozenset({"t1"}), models=frozenset({"boltz2"}), expires_at=future)
    assert both.should_capture(tenant_id="t1", model_id="boltz2", now=now) is True
    assert both.should_capture(tenant_id="t1", model_id="qwen3-8b", now=now) is False
    # Time-bounded: capture stops at expiry even while enabled and scoped.
    expiring = DebugCapturePolicy(enabled=True, models=frozenset({"m"}), expires_at=now)
    assert expiring.should_capture(tenant_id="t", model_id="m", now=now - timedelta(seconds=1)) is True
    assert expiring.should_capture(tenant_id="t", model_id="m", now=now) is False


def test_capture_policy_path_model_pre_gate():
    """SAI-01: the pre-buffer gate proves no-capture before any bytes are buffered."""
    now = NOW
    future = now + timedelta(hours=1)
    disabled = DebugCapturePolicy()
    assert disabled.path_model_admissible("boltz2", now) is False
    model = DebugCapturePolicy(enabled=True, models=frozenset({"boltz2"}), expires_at=future)
    assert model.path_model_admissible("boltz2", now) is True
    assert model.path_model_admissible("qwen3-8b", now) is False  # out-of-scope path model
    assert model.path_model_admissible(None, now) is True  # unknown (e.g. /mcp): must buffer
    # A tenant-scoped policy cannot pre-gate on the path model (tenant unknown yet).
    tenant = DebugCapturePolicy(enabled=True, tenants=frozenset({"t1"}), expires_at=future)
    assert tenant.path_model_admissible("qwen3-8b", now) is True
    assert model.path_model_admissible("boltz2", future) is False  # expired


async def _authenticated_capture(policy, *, tenant):
    request = b'{"model":"boltz2","sequence":"ACDEFG"}'

    async def resolver(token):
        return Principal(
            token_id=uuid4(),
            token_prefix="p",
            principal_id="researcher",
            tenant_id=tenant,
            models=frozenset({"boltz2"}),
            scopes=frozenset(),
        )

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    store, _, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": request}],
        resolver=resolver,
        headers=[(b"authorization", b"Bearer T"), (b"content-type", b"application/json")],
        policy=policy,
    )
    return store


async def test_middleware_captures_only_the_allowlisted_tenant():
    """SAI-01: capture is tenant-scoped; other tenants are not recorded."""
    policy = DebugCapturePolicy(enabled=True, tenants=frozenset({"tenant-a"}), expires_at=_FUTURE)
    allowed = await _authenticated_capture(policy, tenant="tenant-a")
    assert len(allowed.exchanges) == 1
    denied = await _authenticated_capture(policy, tenant="tenant-b")
    assert denied.exchanges == {}


async def test_middleware_records_nothing_when_policy_is_unscoped_or_disabled():
    for policy in (
        DebugCapturePolicy(enabled=True, expires_at=_FUTURE),  # enabled but unscoped
        DebugCapturePolicy(enabled=False, models=frozenset({"boltz2"}), expires_at=_FUTURE),  # disabled
        DebugCapturePolicy(enabled=True, models=frozenset({"boltz2"})),  # no expiry
    ):
        store = await _authenticated_capture(policy, tenant="tenant-a")
        assert store.exchanges == {}


async def test_middleware_stops_capturing_after_expiry():
    policy = DebugCapturePolicy(
        enabled=True, models=frozenset({"boltz2"}), expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    store = await _authenticated_capture(policy, tenant="tenant-a")
    assert store.exchanges == {}


async def test_middleware_bounds_memory_for_matched_large_streaming_bodies():
    """SAI-01: a matched large request/response is bounded near the cap, not fully buffered."""
    big_request = b'{"model":"boltz2","blob":"' + b"Q" * (20 * 1024 * 1024) + b'"}'
    big_response = b'{"result":"' + b"R" * (20 * 1024 * 1024) + b'"}'

    async def app(scope, receive, send):
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        for offset in range(0, len(big_response), 1 << 16):
            chunk = big_response[offset : offset + (1 << 16)]
            more = offset + len(chunk) < len(big_response)
            await send({"type": "http.response.body", "body": chunk, "more_body": more})

    cap = 64 * 1024
    store, _, _ = await capture(
        app,
        chunks=[
            {"type": "http.request", "body": big_request[: 10 * 1024 * 1024], "more_body": True},
            {"type": "http.request", "body": big_request[10 * 1024 * 1024 :], "more_body": False},
        ],
        headers=[(b"content-type", b"application/json")],
        max_body_bytes=cap,
    )
    (exchange,) = store.exchanges.values()
    # Observed counts are the full wire length; stored bodies are bounded and truncated.
    assert exchange.request_body.observed_bytes == len(big_request)
    assert exchange.response_body.observed_bytes == len(big_response)
    assert exchange.request_body.truncated and exchange.response_body.truncated
    assert len(_stored_bytes(exchange.request_body)) <= cap
    assert len(_stored_bytes(exchange.response_body)) <= cap


async def test_middleware_does_not_buffer_unmatched_large_response():
    """SAI-01: an out-of-scope path is gated before buffering and never captured."""
    big = b"Z" * (20 * 1024 * 1024)

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": big})

    policy = DebugCapturePolicy(enabled=True, models=frozenset({"boltz2"}), expires_at=_FUTURE)
    store, outgoing, _ = await capture(
        app,
        path="/v1/models/qwen3-8b:invoke",  # out of the model scope -> pre-gated, not buffered
        chunks=[{"type": "http.request", "body": b"{}"}],
        policy=policy,
        max_body_bytes=64 * 1024,
    )
    assert store.exchanges == {}
    assert outgoing[-1]["body"] == big  # response still streamed through unchanged


async def test_middleware_redacts_credential_split_across_request_chunks():
    """SAI-01: a credential straddling a chunk boundary is still redacted."""
    secret = "NGCSPLITSECRETVALUE0123456789"
    first = b'{"model":"boltz2","api_key":"' + secret[:10].encode()
    second = secret[10:].encode() + b'","sequence":"ACDEFG"}'

    async def app(scope, receive, send):
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    store, _, _ = await capture(
        app,
        chunks=[
            {"type": "http.request", "body": first, "more_body": True},
            {"type": "http.request", "body": second, "more_body": False},
        ],
        headers=[(b"content-type", b"application/json")],
    )
    (exchange,) = store.exchanges.values()
    assert secret not in exchange.model_dump_json() and "ACDEFG" in exchange.request_body.data


async def test_unterminated_sensitive_scalar_is_redacted_in_request_and_echoed_response():
    """SAI-01: a credential in an unterminated JSON scalar is redacted in both directions."""
    secret = "UNTERMINATEDPASSWORDSECRET123"
    request = b'{"model":"boltz2","password":"' + secret.encode()  # no closing quote/brace

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
        # The error echoes the offending value back to the caller.
        await send({"type": "http.response.body", "body": b'{"detail":"invalid value ' + secret.encode() + b'"}'})

    store, outgoing, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": request}],
        headers=[(b"content-type", b"application/json")],
    )
    (exchange,) = store.exchanges.values()
    assert secret not in exchange.request_body.data
    assert secret not in exchange.response_body.data
    assert secret not in exchange.model_dump_json()


async def test_storage_credentials_endpoint_is_never_captured():
    """SAI-01/SAI-08: the storage-credentials disclosure is excluded from capture."""
    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"access_key_id":"AKIA","secret_access_key":"SECRET"}'})

    store, outgoing, _ = await capture(
        app,
        path="/v1/storage/credentials",
        chunks=[{"type": "http.request", "body": b"{}"}],
    )
    assert store.exchanges == {}
    assert b"SECRET" in outgoing[-1]["body"]  # response still returned to the caller


async def test_known_credential_prefix_at_cap_boundary_is_not_stored():
    """SAI-01: a known credential longer than the head, echoed in a body larger than
    the buffer, must not leave a prefix in the stored (truncated) capture."""
    secret = "K" + "".join(f"{i % 10}" for i in range(240))  # 241-char known credential
    body = (
        b'{"model":"boltz2","pad":"' + b"P" * 20 + b'","echo":"' + secret.encode() + b'","tail":"' + b"Z" * 4000 + b'"}'
    )

    async def app(scope, receive, send):
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    store, _, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": body, "more_body": False}],
        headers=[(b"x-api-key", secret.encode()), (b"content-type", b"application/json")],
        max_body_bytes=64,
    )
    (exchange,) = store.exchanges.values()
    stored = _stored_bytes(exchange.request_body)
    assert exchange.request_body.observed_bytes == len(body)
    assert exchange.request_body.truncated
    # No prefix of the credential (down to 8 bytes) survives in the stored bytes.
    assert all(secret[:size].encode() not in stored for size in range(8, len(secret) + 1))


async def test_unterminated_password_learned_as_prefix_redacts_full_echoed_value():
    """SAI-01: when only a prefix of an unterminated password is buffered, a response
    echoing the full value must be fully redacted, not just its learned prefix."""
    secret = "PW" + "".join(f"{i % 10}" for i in range(90))  # 92-char secret
    # Request buffer is bounded (store_limit = 2*cap), so only a prefix is learned.
    request = b'{"model":"boltz2","password":"' + secret.encode()  # unterminated

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"detail":"bad ' + secret.encode() + b'"}'})

    store, outgoing, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": request}],
        headers=[(b"content-type", b"application/json")],
        max_body_bytes=64,
    )
    (exchange,) = store.exchanges.values()
    # Neither the request copy nor the echoed response retains any run of the secret.
    for body in (exchange.request_body, exchange.response_body):
        stored = _stored_bytes(body)
        assert all(secret[:size].encode() not in stored for size in range(8, len(secret) + 1))
    assert secret not in exchange.model_dump_json()


def _future_iso(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


def test_settings_reject_enabled_capture_without_scope_or_bounded_expiry():
    """SAI-01: an enabled but unscoped/unbounded capture config must not start."""
    import pytest as _pytest
    from pydantic import ValidationError

    # Enabled with no scope at all.
    with _pytest.raises(ValidationError, match="request_debug"):
        Settings(request_debug_enabled=True, request_debug_expires_at=_future_iso(1))
    # Enabled and scoped but no expiry.
    with _pytest.raises(ValidationError, match="expires_at"):
        Settings(request_debug_enabled=True, request_debug_models="boltz2")
    # Enabled, scoped, but expiry beyond the strict maximum window.
    with _pytest.raises(ValidationError, match="max_window"):
        Settings(
            request_debug_enabled=True,
            request_debug_tenants="tenant-a",
            request_debug_max_window_seconds=3600,
            request_debug_expires_at=_future_iso(48),
        )


def test_settings_expired_window_is_service_safe_not_a_crash():
    """SAI-01: a stale (past) expiry must NOT crash the control plane; it normalizes
    to capture-off. Startup succeeds and the built policy captures nothing."""
    settings = Settings(
        request_debug_enabled=True,
        request_debug_models="boltz2",
        request_debug_expires_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )  # does not raise
    policy = settings.debug_capture_policy()
    assert policy.enabled is True and policy.expires_at is not None
    assert policy.should_capture(tenant_id="t", model_id="boltz2", now=datetime.now(UTC)) is False


def test_settings_build_scoped_bounded_capture_policy():
    """SAI-01: a valid scoped+bounded config yields an enabled policy; default is off."""
    assert Settings().debug_capture_policy().enabled is False
    settings = Settings(
        request_debug_enabled=True,
        request_debug_tenants="tenant-a, tenant-b",
        request_debug_models="boltz2",
        request_debug_expires_at=_future_iso(2),
    )
    policy = settings.debug_capture_policy()
    assert policy.enabled is True
    assert policy.tenants == frozenset({"tenant-a", "tenant-b"})
    assert policy.models == frozenset({"boltz2"})
    assert policy.expires_at is not None
    # The built policy actually admits an in-scope exchange and rejects others.
    now = datetime.now(UTC)
    assert policy.should_capture(tenant_id="tenant-a", model_id="boltz2", now=now) is True
    assert policy.should_capture(tenant_id="other", model_id="qwen3-8b", now=now) is False
