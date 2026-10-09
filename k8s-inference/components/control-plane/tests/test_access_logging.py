"""Actual ASGI disconnect and streaming boundaries; no fabricated HTTP replies."""

import asyncio
import json
import logging

import pytest
from starlette.middleware.base import BaseHTTPMiddleware

from fs2_serve.access_logging import AccessLogMiddleware
from fs2_serve.request_debug import DebugCaptureMiddleware, InMemoryDebugStore
from fs2_serve.request_telemetry import InMemoryRequestTelemetryStore, RequestTelemetryMiddleware


async def exchange(app, *, messages=(), scope_type="http", send_error=False):
    incoming, outgoing = list(messages), []
    scope = {
        "type": scope_type,
        "method": "POST",
        "path": "/mcp",
        "state": {},
        "headers": [(b"authorization", b"Bearer SECRET")],
        "query_string": b"secret=PRIVATE",
    }

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        if send_error:
            raise OSError("PRIVATE_TRANSPORT_DETAIL")
        outgoing.append(message)

    await app(scope, receive, send)
    return outgoing


def access_record(caplog):
    return json.loads(next(record.message for record in caplog.records if record.name == "fs2_serve.access"))


async def test_old_base_http_logger_reproduces_no_response_returned_after_disconnect():
    async def app(scope, receive, send):
        assert (await receive())["type"] == "http.disconnect"

    async def old_dispatch(request, call_next):
        return await call_next(request)

    with pytest.raises(RuntimeError, match="No response returned"):
        await exchange(BaseHTTPMiddleware(app, dispatch=old_dispatch))


async def test_disconnected_mounted_app_preserves_debug_telemetry_and_no_invented_response(caplog):
    caplog.set_level(logging.INFO)
    debug, telemetry = InMemoryDebugStore(), InMemoryRequestTelemetryStore()

    async def app(scope, receive, send):
        assert (await receive())["type"] == "http.disconnect"

    wrapped = AccessLogMiddleware(DebugCaptureMiddleware(RequestTelemetryMiddleware(app, store=telemetry), store=debug))
    assert await exchange(wrapped) == []
    row = access_record(caplog)
    assert row["status"] is None and row["disconnected"] and not row["response_complete"]
    assert row["error_type"] is None and row["request_id"]
    (capture,) = debug.exchanges.values()
    assert capture.http_status is None and capture.disconnected
    assert telemetry.observations[0].semantic_outcome == "cancelled"
    assert "SECRET" not in caplog.text and "PRIVATE" not in caplog.text


@pytest.mark.parametrize("status", [200, 202, 400, 503])
async def test_preserves_status_headers_chunks_and_first_audio_or_sse_before_completion(caplog, status):
    caplog.set_level(logging.INFO)
    received = []

    async def app(scope, receive, send):
        received.append(await receive())
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"cache-control", b"private"), (b"set-cookie", b"a"), (b"set-cookie", b"b")],
            }
        )
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await send({"type": "http.response.body", "body": b"second", "more_body": False})

    message = {"type": "http.request", "body": b"PRIVATE_INPUT", "more_body": False}
    outgoing = await exchange(AccessLogMiddleware(app), messages=[message])
    assert received == [message]
    assert outgoing[0]["status"] == status
    assert outgoing[0]["headers"] == [
        (b"cache-control", b"private"),
        (b"set-cookie", b"a"),
        (b"set-cookie", b"b"),
        (b"x-content-type-options", b"nosniff"),
    ]
    assert [m["body"] for m in outgoing[1:]] == [b"first", b"second"]
    assert access_record(caplog)["response_complete"]
    assert "PRIVATE_INPUT" not in caplog.text


@pytest.mark.parametrize("error", [ValueError("PRIVATE_DETAIL"), asyncio.CancelledError()])
async def test_real_application_errors_and_cancellation_propagate_unchanged(caplog, error):
    caplog.set_level(logging.INFO)

    async def app(scope, receive, send):
        raise error

    with pytest.raises(type(error)) as caught:
        await exchange(AccessLogMiddleware(app))
    assert caught.value is error
    record = access_record(caplog)
    assert record["status"] is None and record["error_type"] == type(error).__name__
    assert "PRIVATE_DETAIL" not in caplog.text


async def test_send_failure_does_not_invent_observed_status(caplog):
    caplog.set_level(logging.INFO)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    with pytest.raises(OSError):
        await exchange(AccessLogMiddleware(app), send_error=True)
    record = access_record(caplog)
    assert record["status"] is None and record["disconnected"]
    assert "PRIVATE_TRANSPORT_DETAIL" not in caplog.text


async def test_non_http_is_not_wrapped_or_logged(caplog):
    caplog.set_level(logging.INFO)

    async def app(scope, receive, send):
        await send({"type": "websocket.close", "code": 1000})

    assert await exchange(AccessLogMiddleware(app), scope_type="websocket") == [
        {"type": "websocket.close", "code": 1000}
    ]
    assert not caplog.records


async def test_stream_chunks_reach_client_before_app_finishes_without_reading_extra_body():
    first_seen, allow_finish = asyncio.Event(), asyncio.Event()
    received, outgoing = [], []

    async def receive():
        received.append(True)
        return {"type": "http.disconnect"}

    async def send(message):
        outgoing.append(message)
        if message.get("body") == b"first":
            first_seen.set()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await allow_finish.wait()
        await send({"type": "http.response.body", "body": b"second", "more_body": False})

    scope = {"type": "http", "method": "GET", "path": "/mcp", "state": {}}
    task = asyncio.create_task(AccessLogMiddleware(app)(scope, receive, send))
    try:
        await asyncio.wait_for(first_seen.wait(), 1)
        assert not task.done() and outgoing[-1]["body"] == b"first"
        assert not received  # No middleware body drain or disconnect polling.
    finally:
        allow_finish.set()
        await task
    assert outgoing[-1]["body"] == b"second"
