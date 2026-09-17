"""Actual ASGI bytes and encrypted-debug storage boundaries; no live services."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from fs2_serve.models import Principal
from fs2_serve.request_debug import (
    DebugBody,
    DebugCaptureMiddleware,
    DebugCapturePolicy,
    DebugExchange,
    InMemoryDebugStore,
    body_capture,
    bounded_body_capture,
    capture_store_limit,
    credential_values,
    normalize_exchange_for_read,
    persist_debug_exchange,
    redact_headers,
    redact_query,
    redact_response_headers,
    suppressed_body,
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
# Default policy for middleware tests: a tenant scope is mandatory (no cross-tenant
# capture), so the default binds tenant-a and a far-future expiry. Model is an optional
# narrowing, left unset here so the helper's server-set model_id is recorded as-is.
# Individual tests pass their own policy when they need a different scope.
_TEST_POLICY = DebugCapturePolicy(enabled=True, tenants=frozenset({"tenant-a"}), expires_at=_FUTURE)


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
        # Response is sanitized once at build time (is_response=True); the store does not
        # re-sanitize, so the fixture builds the response as it is stored.
        response_body=body_capture(b'{"detail":"polymers is required"}', "application/json", True, is_response=True),
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
    model="boltz2",
    store=None,
    tenant="tenant-a",
    principal=None,
    telemetry_store=None,
    send_error=False,
    max_body_bytes=None,
    policy=None,
):
    store = store or InMemoryDebugStore()
    incoming, outgoing = list(chunks), []
    # Shared-principal reuse: the middleware reads the auth-resolved principal from scope
    # state and NEVER re-verifies a bearer token. Seed the initial state with an authorized
    # tenant-a principal by default (matching _TEST_POLICY); a test simulating a denied or
    # unauthenticated caller passes tenant=None (no principal seeded) or its own principal=.
    scope_state = {"model_id": model} if state is None else dict(state)
    if principal is None and tenant is not None:
        principal = Principal(
            token_id=uuid4(),
            token_prefix="test-prefix",
            principal_id="customer",
            tenant_id=tenant,
            models=frozenset({"boltz2"}),
            scopes=frozenset(),
        )
    if principal is not None:
        scope_state["principal"] = principal
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": list(headers),
        "query_string": query,
        "state": scope_state,
    }

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        if send_error and message["type"] == "http.response.body":
            raise OSError("connection closed")
        outgoing.append(message)

    if telemetry_store is not None:
        app = RequestTelemetryMiddleware(app, store=telemetry_store)
    middleware = DebugCaptureMiddleware(
        app,
        store=store,
        persist_timeout_seconds=0.05,
        max_body_bytes=max_body_bytes,
        policy=policy or _TEST_POLICY,
    )
    await middleware(scope, receive, send)
    # Capture persists OFF the request path via a bounded queue; drain it here (tests only) so a
    # synchronous assertion on the store sees the capture. The middleware itself never awaits it.
    await middleware.drain()
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
    # These are wire-COMPLETE but malformed/SSE request bodies (redaction robustness on odd
    # shapes). A wire-INCOMPLETE body is instead withheld entirely — covered separately.
    body = body_capture(raw, "application/json", True)
    assert body.redacted and "JSON_SECRET" not in body.data and "ACDEFG" in body.data
    assert body.observed_bytes == len(raw) and body.complete


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
    # A wire-COMPLETE body has its full known credential redacted in place.
    assert body_capture(b"failure CUSTOM_SECRET", "text/plain", True, known).data == "failure [REDACTED]"
    # A wire-INCOMPLETE body that could end mid-credential is withheld ENTIRELY (whole-or-withhold),
    # which is strictly safer than trimming a dangling credential prefix.
    incomplete = body_capture(b"failure CUSTOM_SEC", "text/plain", False, known)
    assert incomplete.data == "[REDACTED]" and incomplete.truncated and not incomplete.complete


async def test_rejected_json_complete_capture_reuses_principal_and_redacts_credential_echo():
    request = b'{"api_key":"BODY_SECRET","sequence":"ACDEFG",'
    response = b'{"detail":[{"input":"BODY_SECRET","msg":"invalid JSON"}]}'
    customer = owner()

    async def app(scope, receive, send):
        assert (await receive())["body"] == request  # Capture does not mutate model input.
        await send({"type": "http.response.start", "status": 422, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": response})

    # The capture path reuses the auth-resolved principal from scope state (no re-verification).
    store, outgoing, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": request}],
        principal=customer,
        headers=[(b"authorization", b"Bearer ACCESS_SECRET")],
    )
    (exchange,) = store.exchanges.values()
    assert outgoing[-1]["body"] == response
    assert exchange.request_body.complete and exchange.response_body.complete and exchange.http_status == 422
    assert exchange.tenant_id == customer.tenant_id and exchange.token_id == customer.token_id
    assert exchange.operation_id is None and exchange.model_id == "boltz2"
    assert "BODY_SECRET" not in exchange.model_dump_json() and "ACCESS_SECRET" not in exchange.model_dump_json()
    assert "ACDEFG" in exchange.request_body.data


async def test_unauthorized_request_with_no_resolvable_tenant_is_not_captured():
    """SAI-01: a tenant scope is mandatory and the capture path reuses the auth-resolved
    principal (it never re-verifies the token). A request with NO resolved principal on scope
    state (unauthenticated/denied) has no tenant and is NEVER captured — no cross-tenant or
    unauthenticated capture — so a bad bearer secret never reaches the store."""

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 401})
        await send({"type": "http.response.body", "body": b"unauthorized"})

    # tenant=None seeds no principal on scope state, simulating an unauthenticated/denied caller.
    store, outgoing, _ = await capture(app, tenant=None, headers=[(b"authorization", b"Bearer BAD_SECRET")])
    # The upstream 401 is still delivered to the client, but nothing is captured and the
    # bad bearer secret never reaches the store.
    assert outgoing[0]["status"] == 401
    assert store.exchanges == {}
    assert (await store.list()).items == []


async def test_disconnected_partial_upload_has_no_invented_status():
    async def app(scope, receive, send):
        await receive()
        await receive()

    store, _, _ = await capture(app, chunks=[{"type": "http.request", "body": b"partial", "more_body": True}])
    (exchange,) = store.exchanges.values()
    assert exchange.disconnected and exchange.http_status is None
    # Whole-or-withhold: a wire-incomplete request body is withheld entirely (never stored as a
    # prefix), yet its true observed length and incomplete flag are still recorded.
    assert exchange.request_body.data == "[REDACTED]" and exchange.request_body.truncated
    assert not exchange.request_body.complete and exchange.request_body.observed_bytes == len(b"partial")
    assert not exchange.response_body.complete


async def test_send_failure_preserves_observed_chunk_and_original_exception():
    store = InMemoryDebugStore()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"partial":"generated"}'})

    with pytest.raises(OSError, match="connection closed"):
        await capture(app, store=store, send_error=True)
    (exchange,) = store.exchanges.values()
    # The response never completed on the wire, so it is withheld fail-closed; the
    # exchange is still explicitly incomplete/disconnected with the original error.
    assert _stored_bytes(exchange.response_body) == b"[REDACTED]"
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
    # An SSE stream is not a valid JSON document, so its body is withheld fail-closed;
    # correlation identity and server-observed tool attribution are still recorded.
    assert debug.response_body.data == "[REDACTED]"
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


def _clean(raw, content_type="application/json", *, complete=True):
    """body_capture with NO request-derived credentials, to prove a body is
    sanitized purely on its own key/format merits (response-only secrets)."""
    return _stored_bytes(body_capture(raw, content_type, complete))


@pytest.mark.parametrize(
    "raw,secret",
    [
        (b'{"secret_access_key":"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}', b"wJalrXUtnFEMI"),
        (b'{"access_key_id":"AKIAIOSFODNN7EXAMPLE"}', b"AKIAIOSFODNN7EXAMPLE"),
        (b'{"api_key":"nvapi-0123456789abcdef0123456789abcdef"}', b"nvapi-0123456789"),
        (b'{"password":"hunter2hunter2hunter2"}', b"hunter2hunter2"),
        (b'{"data":{"nested":{"client_secret":"topsecretvalue123"}}}', b"topsecretvalue123"),
        # Escaped sensitive key: "api_key" decodes to "api_key".
        (b'{"api\\u005fkey":"nvapi-abcdefabcdefabcdefabcdef0000"}', b"nvapi-abcdef"),
        # Truncated/malformed JSON still redacts by key on the fallback path.
        (b'{"result":"ok","password":"leakedsecretvalue"', b"leakedsecretvalue"),
        # Single-quoted repr-style object never parses as JSON.
        (b"{'password': 'leakedsecretvalue'}", b"leakedsecretvalue"),
    ],
)
def test_response_only_secret_sanitized_independent_of_request(raw, secret):
    """SAI-01: a secret appearing only in the response is removed on its own merits."""
    stored = _clean(raw)
    assert secret not in stored and b"[REDACTED]" in stored


def test_response_only_form_urlencoded_password_is_redacted():
    """SAI-01: a form-encoded credential value is removed by key name."""
    body = b"grant_type=password&username=alice&password=hunter2secretvalue&scope=read"
    stored = _clean(body, "application/x-www-form-urlencoded")
    assert b"hunter2secretvalue" not in stored
    assert b"username=alice" in stored and b"grant_type=password" in stored


def test_response_only_aws_access_key_id_format_is_redacted_without_a_key():
    """SAI-01: a bare AWS access key id in free text is removed by format alone."""
    body = b'{"detail":"key AKIAIOSFODNN7EXAMPLE is not authorized for boltz2"}'
    stored = _clean(body)
    assert b"AKIAIOSFODNN7EXAMPLE" not in stored and b"[REDACTED]" in stored


def test_invalid_utf8_body_with_sensitive_key_is_redacted_and_stored_base64():
    """SAI-01: an invalid-UTF-8 body is still key-redacted and stored losslessly."""
    body = b'{"api_key":"nvapi-abcdefabcdefabcdefabcdef0000","blob":"\xff\xfe\x80"}'
    debug = body_capture(body, "application/json", complete=True)
    stored = _stored_bytes(debug)
    assert debug.encoding == "base64"  # invalid UTF-8 -> base64 storage, no data loss
    assert b"nvapi-abcdef" not in stored and b"[REDACTED]" in stored


def test_sse_stream_passes_through_but_redacts_embedded_secret():
    """SAI-01: server-sent events keep their framing while embedded secrets go."""
    body = b'data: {"api_key":"nvapi-abcdefabcdefabcdefabcdef0000"}\n\ndata: [DONE]\n\n'
    stored = _clean(body, "text/event-stream")
    assert b"nvapi-abcdef" not in stored
    assert stored.startswith(b"data: ") and b"[DONE]" in stored  # SSE framing preserved


def test_bounded_capture_keeps_wire_complete_separate_from_truncation():
    """SAI-01: wire-completeness and buffer-completeness are independent flags, so a
    wire-complete but storage-truncated body is complete=True, truncated=True."""
    head = b'{"result":"' + b"R" * 1000 + b'"}'
    # Whole body fit in the buffer but exceeds the store cap: complete, truncated.
    body = bounded_body_capture(head, "application/json", True, (), max_bytes=256, observed_bytes=len(head))
    assert body.complete is True and body.truncated is True and body.observed_bytes == len(head)
    # Tail beyond the buffer was discarded but the wire body ended: still complete.
    discarded = bounded_body_capture(head, "application/json", True, (), max_bytes=256, observed_bytes=len(head) + 9999)
    assert discarded.complete is True and discarded.truncated is True
    assert discarded.observed_bytes == len(head) + 9999
    # Whole-or-withhold: a body cut off on the wire is withheld ENTIRELY (never a stored prefix),
    # regardless of size — including a SMALL incomplete body that fits under the cap.
    partial = bounded_body_capture(head, "application/json", False, (), max_bytes=256, observed_bytes=len(head))
    assert partial.complete is False and partial.truncated is True and _stored_bytes(partial) == b"[REDACTED]"
    small = bounded_body_capture(b'{"a":1}', "application/json", False, (), max_bytes=256, observed_bytes=7)
    assert small.complete is False and small.truncated is True and _stored_bytes(small) == b"[REDACTED]"


def test_redaction_stays_linear_on_pathological_buffer():
    """SAI-01: redacting a full buffer of malformed/unterminated JSON is bounded in
    time and memory (possessive scalars + bounded search; no catastrophic peak)."""
    import time
    import tracemalloc

    cap = 128 * 1024
    limit = capture_store_limit(cap)  # the largest body a production capture redacts
    body = (b'{"password":"' + b'x"y":"' * (limit // 6))[:limit]  # many quotes + sensitive scalar
    tracemalloc.start()
    start = time.perf_counter()
    # complete=True so the body actually reaches the redaction path under test (a wire-incomplete
    # body is now withheld before redaction, which would make this perf assertion a no-op).
    debug = body_capture(body, "application/json", complete=True, max_bytes=cap)
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert elapsed < 2.0  # catastrophic backtracking would take far longer
    assert peak < 16 * 1024 * 1024  # far below the tens-of-MiB backtracking peaks
    assert len(_stored_bytes(debug)) <= cap


def test_suppressed_body_marker_reports_true_length_and_is_flagged():
    """SAI-01: the fail-closed placeholder withholds bytes but keeps metadata truthful."""
    marker = suppressed_body("application/json", observed_bytes=1_000_000, complete=True)
    assert marker.data == "[REDACTED]" and marker.redacted is True and marker.truncated is True
    assert marker.observed_bytes == 1_000_000 and marker.complete is True


async def test_middleware_caps_stored_body_size_but_reports_true_observed_bytes():
    big_response = b'{"result":"' + b"R" * 20000 + b'"}'

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": big_response, "more_body": False})

    # The request fits inside the inspection buffer (cap + overlap), so it is fully
    # inspected and the response is captured as a bounded prefix (not suppressed).
    big_request = b'{"input":"' + b"Q" * 2000 + b'"}'
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
    # The response was truncated on the wire's terms: wire-complete but stored as a
    # bounded prefix, so complete stays True while truncated is separately True.
    assert exchange.response_body.complete is True and exchange.response_body.truncated is True


def test_capture_policy_is_scoped_and_time_bounded():
    """SAI-01: no global capture; a TENANT scope AND a bounded future expiry are mandatory,
    and capture can never span tenants (model-only is not a valid scope)."""
    now = NOW
    future = now + timedelta(hours=1)
    # Disabled: never captures.
    assert (
        DebugCapturePolicy(enabled=False, tenants=frozenset({"t"}), expires_at=future).should_capture(
            tenant_id="t", model_id="m", now=now
        )
        is False
    )
    # Enabled but unscoped: fail closed (there is no global capture switch).
    assert (
        DebugCapturePolicy(enabled=True, expires_at=future).should_capture(tenant_id="t", model_id="m", now=now)
        is False
    )
    # Enabled and tenant-scoped but no expiry: fail closed (a bounded window is mandatory).
    assert (
        DebugCapturePolicy(enabled=True, tenants=frozenset({"t"})).should_capture(tenant_id="t", model_id="m", now=now)
        is False
    )
    # Model-only is NOT a valid scope: a tenant scope is mandatory, so a model-only policy
    # captures nothing (no cross-tenant capture), even for the named model or tenant None.
    model_only = DebugCapturePolicy(enabled=True, models=frozenset({"boltz2"}), expires_at=future)
    assert model_only.should_capture(tenant_id=None, model_id="boltz2", now=now) is False
    assert model_only.should_capture(tenant_id="t1", model_id="boltz2", now=now) is False
    # Tenant-scoped: only the allowlisted tenant, and never an unauthenticated request.
    tenant = DebugCapturePolicy(enabled=True, tenants=frozenset({"t1"}), expires_at=future)
    assert tenant.should_capture(tenant_id="t1", model_id="m", now=now) is True
    assert tenant.should_capture(tenant_id="t2", model_id="m", now=now) is False
    assert tenant.should_capture(tenant_id=None, model_id="m", now=now) is False
    # Model is an OPTIONAL narrowing WITHIN the tenant scope: must match both.
    both = DebugCapturePolicy(enabled=True, tenants=frozenset({"t1"}), models=frozenset({"boltz2"}), expires_at=future)
    assert both.should_capture(tenant_id="t1", model_id="boltz2", now=now) is True
    assert both.should_capture(tenant_id="t1", model_id="qwen3-8b", now=now) is False
    assert both.should_capture(tenant_id="t2", model_id="boltz2", now=now) is False  # wrong tenant
    # Time-bounded: capture stops at expiry even while enabled and scoped.
    expiring = DebugCapturePolicy(enabled=True, tenants=frozenset({"t"}), expires_at=now)
    assert expiring.should_capture(tenant_id="t", model_id="m", now=now - timedelta(seconds=1)) is True
    assert expiring.should_capture(tenant_id="t", model_id="m", now=now) is False


def test_capture_policy_path_model_pre_gate():
    """SAI-01: the pre-buffer gate proves no-capture before any bytes are buffered."""
    now = NOW
    future = now + timedelta(hours=1)
    disabled = DebugCapturePolicy()
    assert disabled.path_model_admissible("boltz2", now) is False
    # Model-only is not a valid scope (tenant mandatory): the pre-gate rejects it outright.
    model_only = DebugCapturePolicy(enabled=True, models=frozenset({"boltz2"}), expires_at=future)
    assert model_only.path_model_admissible("boltz2", now) is False
    # Tenant + model narrowing: the pre-gate can still reject an out-of-scope path model.
    both = DebugCapturePolicy(enabled=True, tenants=frozenset({"t1"}), models=frozenset({"boltz2"}), expires_at=future)
    assert both.path_model_admissible("boltz2", now) is True
    assert both.path_model_admissible("qwen3-8b", now) is False  # out-of-scope path model
    assert both.path_model_admissible(None, now) is True  # unknown (e.g. /mcp): must buffer
    # A tenant-only policy cannot pre-gate on the path model (any model is in scope).
    tenant = DebugCapturePolicy(enabled=True, tenants=frozenset({"t1"}), expires_at=future)
    assert tenant.path_model_admissible("qwen3-8b", now) is True
    assert both.path_model_admissible("boltz2", future) is False  # expired


async def _authenticated_capture(policy, *, tenant):
    request = b'{"model":"boltz2","sequence":"ACDEFG"}'
    principal = Principal(
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

    # The capture path reuses this auth-resolved principal from scope state (no re-verification).
    store, _, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": request}],
        principal=principal,
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
        DebugCapturePolicy(enabled=True, models=frozenset({"boltz2"}), expires_at=_FUTURE),  # model-only (no tenant)
        DebugCapturePolicy(enabled=False, tenants=frozenset({"tenant-a"}), expires_at=_FUTURE),  # disabled
        DebugCapturePolicy(enabled=True, tenants=frozenset({"tenant-a"})),  # no expiry
    ):
        store = await _authenticated_capture(policy, tenant="tenant-a")
        assert store.exchanges == {}


async def test_middleware_stops_capturing_after_expiry():
    policy = DebugCapturePolicy(
        enabled=True, tenants=frozenset({"tenant-a"}), expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    store = await _authenticated_capture(policy, tenant="tenant-a")
    assert store.exchanges == {}


async def test_capture_reuses_auth_resolved_principal_without_reverifying_the_token():
    """SAI-01/SAI-17: the capture path must NOT re-verify the bearer token (which would repeat
    the Argon2 password hash the auth stack already ran — a per-request degradation). It reuses
    the principal the auth stack placed on scope state; the middleware exposes no token-
    reverification path (no principal_resolver, no _resolve_principal)."""
    import inspect

    # Regression guard: no duplicate password-hash / token-reverification path exists.
    assert "principal_resolver" not in inspect.signature(DebugCaptureMiddleware.__init__).parameters
    assert not hasattr(DebugCaptureMiddleware, "_resolve_principal")

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    # Capture happens purely from the state-provided principal (tenant-a); no resolver is used.
    store, _, _ = await capture(app, chunks=[{"type": "http.request", "body": b'{"model":"boltz2"}'}])
    (exchange,) = store.exchanges.values()
    assert exchange.tenant_id == "tenant-a"


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
    # The request far exceeded the inspection buffer, so its uninspected tail could
    # hold a credential we never saw: the response body is withheld, not stored.
    assert _stored_bytes(exchange.response_body) == b"[REDACTED]"
    assert exchange.response_body.redacted is True


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


@pytest.mark.parametrize("gap", [-4, 0, 64, 8192])
async def test_middleware_suppresses_response_for_request_tail_credential(gap):
    """SAI-01: an arbitrary credential at/after the inspection boundary in the request
    must never be echoed into a stored response. The response body is withheld when
    the request had any uninspected tail — whether the secret straddles the boundary
    (gap<0), sits on it (gap==0) or lies wholly beyond it (gap>0)."""
    cap = 512
    limit = capture_store_limit(cap)
    secret = "BOUNDARYSECRET0123456789ABCDEFGHIJKL"  # arbitrary, not a known key/format
    # Pad so the secret begins `gap` bytes past the inspection boundary (limit).
    pad = b"Q" * max(0, limit + gap)
    big_request = b'{"pad":"' + pad + b'","k":"' + secret.encode() + b'"}'
    echo = b'{"echo":"' + secret.encode() + b'"}'

    async def app(scope, receive, send):
        while (await receive()).get("more_body", False):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": echo, "more_body": False})

    store, _, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": big_request, "more_body": False}],
        headers=[(b"content-type", b"application/json")],
        max_body_bytes=cap,
    )
    (exchange,) = store.exchanges.values()
    assert secret not in exchange.model_dump_json()  # nowhere in the stored row
    assert _stored_bytes(exchange.response_body) == b"[REDACTED]"  # response withheld
    assert exchange.response_body.observed_bytes == len(echo)  # true length still reported
    assert exchange.response_body.complete is True and exchange.response_body.truncated is True


async def test_request_within_buffer_is_captured_while_response_body_is_withheld():
    """SAI-01: the request (debug target) is captured credential-redacted, while the
    response body is withheld entirely — an echoed credential never reaches storage."""
    secret = "nvapi-abcdefabcdefabcdefabcdef0000"
    request = b'{"model":"boltz2","api_key":"' + secret.encode() + b'"}'
    echo = b'{"echo":"' + secret.encode() + b'","detail":"ok"}'

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": echo, "more_body": False})

    store, _, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": request, "more_body": False}],
        headers=[(b"content-type", b"application/json")],
        max_body_bytes=64 * 1024,
    )
    (exchange,) = store.exchanges.values()
    assert secret not in exchange.model_dump_json()  # echoed credential gone everywhere
    assert _stored_bytes(exchange.request_body) != b"[REDACTED]"  # request retained (redacted)
    assert _stored_bytes(exchange.response_body) == b"[REDACTED]"  # response body withheld
    assert exchange.response_body.observed_bytes == len(echo)  # true length still reported


@pytest.mark.parametrize(
    "request_body",
    [
        b"{'api_key': 'SQSECRETVALUE0123456789'}",  # single-quoted (non-JSON)
        b"grant_type=password&api_key=SQSECRETVALUE0123456789&scope=read",  # form-urlencoded
    ],
)
async def test_single_quoted_and_form_request_credentials_are_redacted_in_response_echo(request_body):
    """SAI-01: a credential the request carries in a single-quoted or form-encoded field
    is learned and removed from a response that echoes it, not just from the request copy."""
    secret = "SQSECRETVALUE0123456789"

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
        # An arbitrary error detail echoes the credential in plaintext.
        await send({"type": "http.response.body", "body": b'{"detail":"rejected value ' + secret.encode() + b'"}'})

    store, _, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": request_body, "more_body": False}],
        headers=[(b"content-type", b"application/json")],
    )
    (exchange,) = store.exchanges.values()
    assert secret not in exchange.model_dump_json()  # gone from request copy AND response echo
    assert exchange.request_body.redacted and exchange.response_body.redacted


async def test_arbitrary_and_binary_response_bodies_are_withheld():
    """SAI-01: a response that is not structurally recognized (plain text, binary) is
    withheld rather than stored, since a non-format secret in opaque bytes can't be scrubbed."""
    for content_type, body in (
        (b"text/plain", b"opaque secret ZZZTOKEN maybe"),
        (b"application/octet-stream", b"\x00\xff\x80bin"),
    ):

        async def app(scope, receive, send, _body=body, _ct=content_type):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", _ct)]})
            await send({"type": "http.response.body", "body": _body, "more_body": False})

        store, _, _ = await capture(
            app,
            chunks=[{"type": "http.request", "body": b'{"model":"boltz2"}'}],
            headers=[(b"content-type", b"application/json")],
        )
        (exchange,) = store.exchanges.values()
        assert _stored_bytes(exchange.response_body) == b"[REDACTED]"
        assert exchange.response_body.redacted and exchange.response_body.truncated


async def test_success_response_body_is_withheld_including_numbers_and_keys():
    """SAI-01: the response body is never stored — not strings, not numeric values, not
    object keys — since any could carry an opaque secret. It is withheld entirely."""
    # Numeric value and object key that could encode/carry a secret; none must survive.
    body = (
        b'{"choices":[{"message":{"content":"the model output"}}],'
        b'"usage":{"prompt_tokens":31337},"AKIAIOSFODNN7EXAMPLE":1}'
    )

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    store, _, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": b'{"model":"boltz2"}'}],
        headers=[(b"content-type", b"application/json")],
    )
    (exchange,) = store.exchanges.values()
    stored = _stored_bytes(exchange.response_body)
    assert exchange.response_body.complete and exchange.response_body.redacted and exchange.response_body.truncated
    assert stored == b"[REDACTED]"  # whole response body withheld
    assert b"the model output" not in stored and b"31337" not in stored and b"AKIAIOSFODNN7EXAMPLE" not in stored


def test_response_headers_redact_names_and_values_except_two_structural_headers():
    """SAI-01: a response header is untrusted in BOTH its name and value. Only content-type
    (bare, server-known MIME) and content-length (name kept, value ALWAYS redacted) survive;
    an arbitrary NAME (X-OPAQUESECRET42), an arbitrary MIME subtype (application/OPAQUESECRET42),
    a numeric content-length whose digits encode data (313371337), and opaque "safe" headers
    (ETag/Content-Language/X-Request-Id/Set-Cookie) are all redacted in name and/or value."""
    pairs = [
        (b"content-type", b"application/json; charset=utf-8; secret=SMUGGLED42"),
        (b"content-length", b"313371337"),  # digits can encode data -> value redacted
        (b"x-opaquesecret42", b"1"),  # the secret is in the NAME
        (b"content-type", b"application/OPAQUESECRET42"),  # arbitrary subtype -> dropped
        (b"etag", b'W/"OPAQUE-ETAG-SECRET"'),
        (b"content-language", b"SECRET-LOCALE-TAG"),
        (b"x-request-id", b"OPAQUE-REQ-ID-SECRET"),
        (b"set-cookie", b"session=SUPERSECRETCOOKIE; HttpOnly"),
    ]
    result = redact_response_headers(pairs)
    assert ("content-type", "application/json") in result  # bare, allowlisted MIME; params dropped
    assert ("content-type", "[REDACTED]") in result  # arbitrary subtype not in allowlist -> value redacted
    assert ("content-length", "[REDACTED]") in result  # name kept, digits never stored
    # Every non-structural header has BOTH its name and value redacted.
    for name, value in result:
        if name not in {"content-type", "content-length"}:
            assert (name, value) == ("[REDACTED]", "[REDACTED]")
    rendered = repr(result)
    for secret in (
        "SMUGGLED42",
        "313371337",
        "opaquesecret42",
        "OPAQUESECRET42",
        "OPAQUE-ETAG-SECRET",
        "SECRET-LOCALE-TAG",
        "OPAQUE-REQ-ID-SECRET",
        "SUPERSECRETCOOKIE",
    ):
        assert secret not in rendered


def test_response_header_allowed_names_are_stored_canonically_not_as_caller_spelling():
    """SAI-01: the header-name gate (`_name`) is lossy — it drops separators and case — so the
    RAW name is itself an attacker channel even for an allowed header. An obfuscated/weird-cased
    spelling that still normalizes to content-type/content-length must be stored under the
    CANONICAL name, never the caller's bytes; no separator/casing pattern survives."""
    pairs = [
        (b"C_O_N_T_E_N_T_L_E_N_G_T_H", b"42"),  # underscore-obfuscated -> normalizes to contentlength
        (b"cOnTeNt.TyPe", b"application/json"),  # weird case + dot -> normalizes to contenttype
        (b"c-o-n-t-e-n-t-t-y-p-e", b"text/plain; charset=utf-8"),  # hyphen-spread
    ]
    result = redact_response_headers(pairs)
    assert result == [
        ("content-length", "[REDACTED]"),
        ("content-type", "application/json"),
        ("content-type", "text/plain"),
    ]
    rendered = repr(result)
    # None of the caller's obfuscated spellings (which could carry attacker bytes) survive.
    for spelling in ("C_O_N_T_E_N_T", "cOnTeNt", "TyPe", "c-o-n-t-e-n-t"):
        assert spelling not in rendered


def test_response_content_type_arbitrary_subtype_is_dropped_not_persisted():
    """SAI-01: a MIME-shaped but arbitrary/unknown subtype is NOT in the server-known
    allowlist, so it is dropped from both the header value and the withheld-body content_type
    — the subtype cannot smuggle a secret. Known types (with params) survive as bare MIME."""
    from fs2_serve.request_debug import _safe_content_type

    assert _safe_content_type("application/OPAQUESECRET42") is None
    assert _safe_content_type("application/json; secret=X") == "application/json"
    assert _safe_content_type("TEXT/Event-Stream") == "text/event-stream"  # case-insensitive
    # The withheld response body's content_type is reduced through the same allowlist.
    assert suppressed_body("application/OPAQUESECRET42", 10, True).content_type is None
    assert suppressed_body("application/json; x=y", 10, True).content_type == "application/json"


@pytest.mark.parametrize("signal,expected", [(True, True), (False, False), (None, None)])
async def test_mcp_tool_error_signal_is_carried_without_storing_the_response_body(signal, expected):
    """SAI-01: an MCP tool error inside an HTTP 200 stays distinguishable from success via the
    server-authoritative mcp_is_error signal (read from dispatch state), even though the
    response body is withheld — restoring the debug signal with no response-content channel."""
    body = b'{"jsonrpc":"2.0","result":{"content":[{"text":"secret tool output"}],"isError":true}}'

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    state: dict[str, object] = {"model_id": "boltz2"}
    if signal is not None:
        state["mcp_is_error"] = signal
    store, _, _ = await capture(
        app, path="/mcp", chunks=[{"type": "http.request", "body": b'{"method":"tools/call"}'}], state=state
    )
    (exchange,) = store.exchanges.values()
    assert exchange.mcp_is_error is expected
    # The signal is never derived from the (withheld) response body.
    assert _stored_bytes(exchange.response_body) == b"[REDACTED]"
    assert "secret tool output" not in exchange.model_dump_json()


async def test_mcp_failure_category_and_code_are_carried_from_dispatch_state_not_the_body():
    """SAI-01/blocker-1: the fixed server-origin MCP failure classification (category enum +
    bucketed code) set by the dispatch path is stored on the DETAIL exchange, so an
    operationless HTTP-200 tool error is classifiable without the withheld response body."""
    body = b'{"jsonrpc":"2.0","result":{"content":[{"text":"SEKRIT tool output"}],"isError":true}}'

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    state: dict[str, object] = {
        "model_id": "boltz2",
        "mcp_is_error": True,
        "mcp_failure_category": "tool_execution_failure",
        "mcp_error_code": "tool",  # coarse bucket, never a raw/verbatim code
    }
    store, _, _ = await capture(
        app, path="/mcp", chunks=[{"type": "http.request", "body": b'{"method":"tools/call"}'}], state=state
    )
    (exchange,) = store.exchanges.values()
    assert exchange.mcp_is_error is True
    assert exchange.mcp_failure_category == "tool_execution_failure"
    assert exchange.mcp_error_code == "tool"
    # Still classified without any response-content channel.
    assert _stored_bytes(exchange.response_body) == b"[REDACTED]"
    assert "SEKRIT tool output" not in exchange.model_dump_json()


async def test_response_content_type_parameters_do_not_reach_the_stored_exchange():
    """SAI-01: a response Content-Type carrying an injected parameter is reduced to a bare
    MIME type before it is stored, so a secret smuggled as a Content-Type parameter never
    lands in the persisted exchange (headers or the withheld-body marker's content_type)."""

    async def app(scope, receive, send):
        await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json; boundary=SMUGGLEDCTPARAM"),
                    (b"x-request-id", b"OPAQUEREQIDSECRET"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}', "more_body": False})

    store, _, _ = await capture(
        app,
        chunks=[{"type": "http.request", "body": b'{"model":"boltz2"}'}],
        headers=[(b"content-type", b"application/json")],
    )
    (exchange,) = store.exchanges.values()
    assert exchange.response_body.content_type == "application/json"  # params dropped
    dumped = exchange.model_dump_json()
    assert "SMUGGLEDCTPARAM" not in dumped and "OPAQUEREQIDSECRET" not in dumped


@pytest.mark.parametrize("state", [{}, {"model_id": "qwen3-8b"}, {"model_id": "boltz2"}])
async def test_mcp_scope_uses_server_model_not_caller_declared_body(state):
    """SAI-01: /mcp capture scope is decided from server-authoritative dispatch state
    (state["model_id"]), never the caller-declared model in the request body. A caller
    declaring an in-scope model it is not authorized for must not force capture."""
    spoof = b'{"method":"tools/call","params":{"name":"unrelated","arguments":{"model":"boltz2"}}}'

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    # Tenant scope is mandatory; model narrows within it. The helper resolves tenant-a.
    policy = DebugCapturePolicy(
        enabled=True, tenants=frozenset({"tenant-a"}), models=frozenset({"boltz2"}), expires_at=_FUTURE
    )
    store, _, _ = await capture(
        app, path="/mcp", chunks=[{"type": "http.request", "body": spoof}], policy=policy, state=dict(state)
    )
    if state.get("model_id") == "boltz2":
        (exchange,) = store.exchanges.values()
        assert exchange.model_id == "boltz2"  # only a server-resolved in-scope model captures
    else:
        assert store.exchanges == {}  # spoofed body model, or an unrelated server model, does not


async def test_concurrent_near_cap_captures_stay_within_a_bounded_memory_budget():
    """SAI-01: many concurrent captures of near-cap malformed bodies stay bounded in
    time and aggregate memory (per-capture sanitizer input is capped; >cap is withheld)."""
    import time
    import tracemalloc

    cap = 64 * 1024
    body = (b'{"password":"' + b'x"y":"' * cap)[:cap]  # exactly the cap, dense scalars

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}', "more_body": False})

    async def one():
        store, _, _ = await capture(
            app,
            chunks=[{"type": "http.request", "body": body}],
            headers=[(b"content-type", b"application/json")],
            max_body_bytes=cap,
        )
        # Under overload some captures are shed (offload_capture returns None), leaving
        # the per-call store empty; that is expected load-shedding, not an error.
        return next(iter(store.exchanges.values()), None)

    tracemalloc.start()
    start = time.perf_counter()
    exchanges = await asyncio.gather(*(one() for _ in range(16)))
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert elapsed < 5.0  # no catastrophic backtracking across the concurrent set
    assert peak < 64 * 1024 * 1024  # aggregate stays far below a per-body blowup
    # Every capture that survived load-shedding stored a bounded body (dense-secret
    # bodies whose redaction expands past the cap are withheld, not stored).
    for exchange in filter(None, exchanges):
        assert len(_stored_bytes(exchange.request_body)) <= cap


@pytest.mark.parametrize(
    "raw,secret",
    [
        (b'{"detail":"opaque OPAQUESECRET42XYZ here"}', b"OPAQUESECRET42XYZ"),  # response-only opaque in detail
        (b'{"note":"AKIAIOSFODNN7EXAMPLE"}', b"AKIAIOSFODNN7EXAMPLE"),  # AKIA under a non-sensitive key
        (b'{"choices":[{"content":"model said SEKRIT"}]}', b"SEKRIT"),  # success content
    ],
)
def test_response_only_opaque_secret_is_redacted_not_stored(raw, secret):
    """SAI-01: a valid-JSON response never stores any string value verbatim; every string
    (even an opaque, non-format secret, under any key) is redacted — and never hashed
    reversibly. The response shape (keys, numbers) is kept."""
    stored = _stored_bytes(body_capture(raw, "application/json", complete=True, is_response=True))
    assert secret not in stored and b"[sha256:" not in stored and b"[REDACTED]" in stored


@pytest.mark.parametrize(
    "raw,content_type,complete",
    [
        (b'{"detail": "trunc', "application/json", True),  # malformed application/json
        (b'{"detail":"ok"}', "application/json", False),  # incomplete (wire-cut) though parseable
        (b"AKIA", "application/json", False),  # incomplete 4-byte AKIA tail
        (b"\x00\xffbinary", "application/octet-stream", True),  # binary / unknown
        (b"plain text detail", "text/plain", True),  # unknown text content
        (b'data: {"x":1}\n\n', "text/event-stream", True),  # streaming (not a JSON document)
    ],
)
def test_unknown_malformed_or_incomplete_response_is_withheld(raw, content_type, complete):
    """SAI-01: only a COMPLETE, valid JSON document is retained; anything unknown,
    malformed, incomplete, streaming or binary response body is withheld fail-closed."""
    body = body_capture(raw, content_type, complete=complete, is_response=True)
    assert _stored_bytes(body) == b"[REDACTED]" and body.truncated and body.redacted


async def test_capture_sanitizer_does_not_freeze_the_event_loop():
    """SAI-01: sanitizing a near-cap body runs off the event loop (bounded-concurrency
    offload), so a concurrent heartbeat keeps ticking instead of stalling."""
    cap = 256 * 1024
    heavy_request = (b'{"a":"' + b'x"b":"' * cap)[:cap]  # near-cap, scan-heavy request body

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}', "more_body": False})

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        await capture(
            app,
            chunks=[{"type": "http.request", "body": heavy_request}],
            headers=[(b"content-type", b"application/json")],
            max_body_bytes=cap,
        )
    finally:
        beat.cancel()
    # Had the sanitizer run inline on the loop, the heartbeat would have been frozen for
    # the whole ~0.2s scan (~0 ticks). Offloaded, the loop stays responsive.
    assert ticks >= 3


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
    # The value is unterminated (no closing quote), so it is learned only as a
    # prefix; the stored request copy is additionally capped to max_body_bytes.
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
    """SAI-01: an enabled but unscoped/unbounded capture config must not start; a TENANT
    scope is mandatory and a MODEL-only config (which would span tenants) is rejected."""
    import pytest as _pytest
    from pydantic import ValidationError

    # Enabled with no scope at all.
    with _pytest.raises(ValidationError, match="request_debug"):
        Settings(request_debug_enabled=True, request_debug_expires_at=_future_iso(1))
    # Enabled with a MODEL scope but no TENANT scope: model-only is not a valid scope
    # (it would capture every tenant on a shared App), so it must be rejected.
    with _pytest.raises(ValidationError, match="request_debug_tenants"):
        Settings(
            request_debug_enabled=True,
            request_debug_models="boltz2",
            request_debug_expires_at=_future_iso(1),
        )
    # Enabled and tenant-scoped but no expiry.
    with _pytest.raises(ValidationError, match="expires_at"):
        Settings(request_debug_enabled=True, request_debug_tenants="tenant-a")
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
        request_debug_tenants="tenant-a",
        request_debug_expires_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )  # does not raise
    policy = settings.debug_capture_policy()
    assert policy.enabled is True and policy.expires_at is not None
    # An in-scope tenant still captures nothing because the window has elapsed.
    assert policy.should_capture(tenant_id="tenant-a", model_id="boltz2", now=datetime.now(UTC)) is False


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


def test_settings_activation_window_is_seven_days_distinct_from_ninety_day_retention():
    """SAI-01/owner policy: the capture ACTIVATION window defaults to and is capped at 7 days
    (604,800s) — how long a policy may be enabled — which is DISTINCT from the 90-day RECORD
    retention TTL (DEBUG_RETENTION_SECONDS) used by the preflight/purge. Enabling for longer
    than 7 days is rejected; the two constants are not conflated."""
    from pydantic import ValidationError

    from fs2_serve.request_debug import DEBUG_RETENTION_SECONDS

    assert Settings().request_debug_max_window_seconds == 604_800  # 7-day activation window
    assert DEBUG_RETENTION_SECONDS == 7_776_000  # 90-day record retention, tracked separately
    assert Settings().request_debug_max_window_seconds != DEBUG_RETENTION_SECONDS
    # An activation expiry beyond 7 days is rejected; one inside the window is accepted.
    with pytest.raises(ValidationError, match="max_window"):
        Settings(
            request_debug_enabled=True,
            request_debug_tenants="tenant-a",
            request_debug_expires_at=_future_iso(24 * 8),
        )
    ok = Settings(
        request_debug_enabled=True,
        request_debug_tenants="tenant-a",
        request_debug_expires_at=_future_iso(24 * 6),
    )
    assert ok.debug_capture_policy().enabled is True


async def test_retention_preflight_is_payload_free_and_preserves_within_ttl():
    """SAI-01/owner TTL: the retention preflight reports payload-free aggregates (oldest
    started_at, total, count over the 90-day cutoff) and DELETES NOTHING — it is the proof
    produced before any (separately owned, separately authorized) purge may run. Records
    within 90 days are counted as preserved; only rows older than 90 days are counted expired."""
    from fs2_serve.request_debug import DEBUG_RETENTION_SECONDS

    store = InMemoryDebugStore()
    now = datetime(2026, 9, 16, tzinfo=UTC)
    old = now - timedelta(seconds=DEBUG_RETENTION_SECONDS + 3600)  # older than 90d
    recent = now - timedelta(days=1)  # within 90d
    await store.record(row(id=uuid4(), started_at=old))
    await store.record(row(id=uuid4(), started_at=recent))
    await store.record(row(id=uuid4(), started_at=now))
    preflight = await store.retention_preflight(now=now)
    assert preflight.total == 3
    assert preflight.expired == 1  # only the >90d row is eligible for deletion
    assert preflight.within == 2  # <=90d rows are preserved
    assert preflight.oldest_started_at == old
    assert preflight.max_age_seconds == DEBUG_RETENTION_SECONDS
    assert len(store.exchanges) == 3  # producing the proof deletes nothing
    # Payload-free: the serialized snapshot carries no body/header/payload content.
    dumped = preflight.model_dump_json()
    assert "sequence" not in dumped and "REDACTED" not in dumped and "response" not in dumped


async def test_persist_queue_is_bounded_nonblocking_and_drops_on_overload():
    """SAI-01: debug persistence is OFF the request path via a bounded, non-blocking queue.
    A reserved capture's submit never blocks; when the bounded QUEUE is full the capture is DROPPED
    (overload shed, counted) and its reservation slot is released on context exit, so a burst cannot
    grow memory without bound. There is no public unreserved enqueue path. drain()/aclose()
    (tests/shutdown only) then persist exactly the accepted captures."""
    from fs2_serve.request_debug import DebugPersistQueue

    store = InMemoryDebugStore()
    # max_inflight high so the QUEUE-DEPTH bound (maxsize) is what sheds here, not the admission
    # bound — this exercises the defensive enqueue-full drop specifically.
    queue = DebugPersistQueue(store, maxsize=2, max_inflight=100)

    def builder() -> DebugExchange:
        return row(id=uuid4())

    # Submit a burst WITHOUT yielding to the loop, so the worker cannot drain between submits:
    # the bounded queue accepts maxsize and drops the rest (non-blocking, never raises). Enqueue is
    # only reachable through a reservation handle; a dropped submit releases its slot on with-exit.
    accepted = 0
    for _ in range(5):
        reservation = queue.reserve()
        assert reservation is not None
        with reservation:
            if reservation.submit(builder):
                accepted += 1
    assert accepted == 2 and queue.dropped == 3
    await queue.drain()  # tests/shutdown only
    assert len(store.exchanges) == 2  # only the accepted (non-dropped) captures persisted
    await queue.aclose()


async def test_persist_queue_reserves_before_buffering_and_bounds_inflight():
    """SAI-01: total in-flight capture memory is bounded by a NON-BLOCKING reservation taken
    BEFORE any buffer is allocated — not just by enqueued-item count. reserve() hands out a
    context-managed slot up to the bound then sheds (returns None, counted); EXITING the context
    releases the slot unless submit committed it to the worker, and the worker frees a committed
    slot after it persists — so the bound is leak-proof against exceptions/cancellation/submit
    failure and recovers on its own."""
    from fs2_serve.request_debug import DebugPersistQueue

    store = InMemoryDebugStore()
    queue = DebugPersistQueue(store, maxsize=8, max_inflight=2)
    # Admit up to the in-flight bound, then shed the next reservation (bypass; count the drop).
    r1, r2 = queue.reserve(), queue.reserve()
    assert r1 is not None and r2 is not None
    assert queue.reserve() is None and queue.dropped == 1
    # Exiting a reservation context WITHOUT submitting releases the slot (e.g. an exception path).
    with r1:
        pass
    r3 = queue.reserve()
    assert r3 is not None  # slot reclaimed on context exit
    # A committed reservation's slot is freed by the worker after it persists (drain waits for it).
    with r3:
        assert r3.submit(lambda: row(id=uuid4())) is True
    await queue.drain()
    assert len(store.exchanges) == 1
    # r2 still holds its slot; exiting its context releases it, so both slots are free again.
    with r2:
        pass
    a, b = queue.reserve(), queue.reserve()
    assert a is not None and b is not None
    await queue.aclose()


async def test_persist_queue_ownership_is_server_side_forgery_replay_double_safe():
    """SAI-01: reservation ownership is enforced SERVER-SIDE by the queue's token registry, not by a
    handle flag. A fabricated/directly-constructed handle, a replayed/double commit, or a double
    release can neither enqueue unreserved work nor free another reservation's slot. Authored
    regression for forged/replay/double; not executed here."""
    from fs2_serve.request_debug import DebugPersistQueue, _CaptureReservation

    store = InMemoryDebugStore()
    queue = DebugPersistQueue(store, maxsize=8, max_inflight=3)

    # FORGERY: a directly-constructed handle carrying a token the queue never issued cannot enqueue
    # and cannot free a slot.
    forged = _CaptureReservation(queue, object())
    assert forged.submit(lambda: row(id=uuid4())) is False
    assert queue._inflight() == 0
    with forged:  # __exit__ release of an unknown token is a no-op
        pass
    assert queue._inflight() == 0

    # A committed slot stays counted until the worker frees it, and a REPLAY/DOUBLE commit or a
    # post-commit context exit cannot double-free or undercount.
    reservation = queue.reserve()
    assert reservation is not None and queue._inflight() == 1
    assert reservation.submit(lambda: row(id=uuid4())) is True
    assert reservation.submit(lambda: row(id=uuid4())) is False  # replay/double commit rejected
    assert queue._inflight() == 1
    with reservation:  # post-commit exit must NOT free the worker-owned slot
        pass
    assert queue._inflight() == 1
    await queue.drain()  # worker frees exactly its token
    assert queue._inflight() == 0 and len(store.exchanges) == 1

    # DOUBLE RELEASE of a reserved (un-committed) token decrements exactly once.
    r2 = queue.reserve()
    assert r2 is not None and queue._inflight() == 1
    queue._release(r2._token)
    queue._release(r2._token)  # idempotent
    assert queue._inflight() == 0
    with r2:  # context exit now a no-op
        pass
    assert queue._inflight() == 0
    await queue.aclose()


async def test_persist_queue_commit_enqueue_failure_leaves_token_reserved_not_orphaned():
    """SAI-01 regression: a commit whose enqueue fails (queue full, or an allocation error building
    the queue item) must leave the token RESERVED and releasable — never queued-but-unrecorded, which
    would undercount the bound and let it be exceeded. The reserved->committed flip happens only after
    a clean enqueue and is a non-allocating dict-value update. Authored; not executed here."""
    from fs2_serve.request_debug import DebugPersistQueue

    store = InMemoryDebugStore()
    queue = DebugPersistQueue(store, maxsize=1, max_inflight=5)  # depth 1, admission looser
    a, b = queue.reserve(), queue.reserve()
    assert a is not None and b is not None and queue._inflight() == 2
    assert a.submit(lambda: row(id=uuid4())) is True  # fills the depth-1 queue
    assert b.submit(lambda: row(id=uuid4())) is False  # enqueue drop: b stays RESERVED, counted
    assert queue.dropped == 1 and queue._inflight() == 2
    with b:  # b still reserved -> exit frees it exactly once (no undercount, no orphan)
        pass
    assert queue._inflight() == 1  # only a remains, committed/worker-owned
    await queue.drain()
    assert queue._inflight() == 0 and len(store.exchanges) == 1
    await queue.aclose()


async def test_read_withholds_preserved_legacy_response_and_incomplete_request_without_deleting():
    """SAI-01: redact-on-read. A preserved (possibly legacy) row whose payload still holds a response
    body or a wire-INCOMPLETE request body must NEVER disclose them on read — the store applies the
    current withhold / whole-or-withhold contract on the way out, without rewriting or deleting the
    stored row. Authored; not executed here."""
    store = InMemoryDebugStore()
    # A legacy-shaped row: response body stored verbatim, request body a stored partial prefix — the
    # pre-remediation contract. (Directly constructed to simulate what an old capture left behind.)
    legacy = row(
        request_body=DebugBody(
            encoding="utf-8",
            data="partial-legacy-INPUT",
            content_type="application/json",
            observed_bytes=64,
            complete=False,
            redacted=False,
            truncated=False,
        ),
        response_body=DebugBody(
            encoding="utf-8",
            data='{"secret":"LEGACY-RESPONSE-LEAK"}',
            content_type="application/json",
            observed_bytes=33,
            complete=True,
            redacted=False,
            truncated=False,
        ),
        # Legacy disclosure fields the old contract retained more broadly / scrubbed narrowly.
        error_detail="upstream raised ValueError: secret token sk-LEGACYDETAILLEAK in prompt",
        query_string="authorization=QUERYLEAK&ok=1",
        request_headers=[("authorization", "Bearer REQHEADERLEAK"), ("content-type", "application/json")],
        response_headers=[("x-trace", "RESPHEADERLEAK"), ("content-type", "application/json")],
    )
    await store.record(legacy)
    got = await store.get(legacy.id)
    assert got is not None
    # Response body is served withheld regardless of what was stored.
    assert got.response_body.truncated and got.response_body.redacted
    assert "LEGACY-RESPONSE-LEAK" not in got.response_body.data and got.response_body.data == "[REDACTED]"
    # Wire-incomplete request body is served withheld (whole-or-withhold), true length preserved.
    assert got.request_body.truncated and "partial-legacy-INPUT" not in got.request_body.data
    assert got.request_body.data == "[REDACTED]" and got.request_body.observed_bytes == 64
    # error_detail is failed closed to a generic marker (never the raw stored free-text).
    assert got.error_detail == "[detail withheld on read]" and "LEGACYDETAILLEAK" not in (got.error_detail or "")
    # query + request-auth headers are re-scrubbed under current rules; response headers are structural-only.
    assert "QUERYLEAK" not in got.query_string
    assert ["authorization", "[REDACTED]"] in [list(pair) for pair in got.request_headers]
    dumped = got.model_dump_json()
    assert "REQHEADERLEAK" not in dumped and "RESPHEADERLEAK" not in dumped
    # The stored row itself is NOT rewritten or deleted (a separately owned purge handles TTL).
    assert len(store.exchanges) == 1
    stored = store.exchanges[legacy.id]
    assert stored.response_body.data == '{"secret":"LEGACY-RESPONSE-LEAK"}' and not stored.response_body.truncated
    assert "LEGACYDETAILLEAK" in (stored.error_detail or "")  # stored ciphertext content is preserved
    # A wire-COMPLETE request body (the debugging target, redacted at capture) is served as stored.
    fresh = row()
    await store.record(fresh)
    got_fresh = await store.get(fresh.id)
    assert got_fresh is not None and got_fresh.request_body.data == fresh.request_body.data
    # normalize_exchange_for_read is idempotent on an already-normalized exchange.
    assert normalize_exchange_for_read(got) == got


async def test_read_and_list_withhold_legacy_over_cap_request_and_normalize_flags():
    """SAI-01 egress: a preserved legacy request body that is wire-complete but OVER the CURRENT cap
    is withheld on read (a smaller cap applies now), and list() summary flags are normalized to match
    (response always redacted; request redacted when it will be withheld). Authored; not executed."""
    cap = 16
    store = InMemoryDebugStore(max_body_bytes=cap)
    over = row(
        request_body=DebugBody(
            encoding="utf-8",
            data='{"input":"X"}',
            content_type="application/json",
            observed_bytes=4096,  # far over the current cap, even though the stored prefix is small
            complete=True,
            redacted=False,
            truncated=False,
        ),
        response_body=DebugBody(
            encoding="utf-8",
            data='{"r":"LEGACY-RAW"}',
            content_type="application/json",
            observed_bytes=18,
            complete=True,
            redacted=False,  # legacy stored a raw, non-redacted response
            truncated=False,
        ),
    )
    await store.record(over)
    got = await store.get(over.id)
    assert got is not None
    # Over the current cap -> withheld on read, even though wire-complete.
    assert got.request_body.truncated and got.request_body.data == "[REDACTED]"
    assert got.request_body.observed_bytes == 4096
    assert got.response_body.truncated and "LEGACY-RAW" not in got.response_body.data
    # list() flags reflect the withheld reality even for a legacy row stored with redacted=False.
    listing = await store.list()
    (summary,) = listing.items
    assert summary.response_redacted is True and summary.request_redacted is True


async def test_commit_post_insert_exception_keeps_count_matching_queue_contents():
    """SAI-01 regression: asyncio.Queue.put_nowait appends the item BEFORE its bookkeeping/wakeup, so
    a post-insert exception can leave work queued. With commit-before-insert behind a not-full guard,
    the token is already committed when such an exception fires, so the count matches the queue
    contents (no undercount) and the worker is the sole releaser. Simulated; not executed here."""
    from fs2_serve.request_debug import DebugPersistQueue

    store = InMemoryDebugStore()
    queue = DebugPersistQueue(store, maxsize=4, max_inflight=4)
    reservation = queue.reserve()
    assert reservation is not None and queue._inflight() == 1

    # Simulate a post-insert failure: the real put_nowait fully inserts + bookkeeps (queue stays
    # consistent), then a later step "raises". The item IS queued when the exception propagates.
    real_put = queue._queue.put_nowait

    def put_then_raise(item: object) -> None:
        real_put(item)
        raise MemoryError("simulated post-insert failure")

    queue._queue.put_nowait = put_then_raise  # type: ignore[method-assign]
    try:
        assert reservation.submit(lambda: row(id=uuid4())) is True  # committed despite the raise
    finally:
        queue._queue.put_nowait = real_put  # type: ignore[method-assign]
    with reservation:  # context exit must NOT free the committed (worker-owned) slot
        pass
    assert queue._inflight() == 1  # count matches the one queued item (no undercount)
    await queue.drain()
    assert queue._inflight() == 0 and len(store.exchanges) == 1
    await queue.aclose()
