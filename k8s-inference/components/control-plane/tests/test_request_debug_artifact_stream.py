"""Artifact transfer logging must not buffer trajectories in API memory."""

import hashlib
import tracemalloc
from uuid import uuid4

import pytest

from fs2_serve.request_debug import DebugCaptureMiddleware, InMemoryDebugStore


async def transfer(*, chunks, expected=None, status=200, path=None, fail_send=False, complete=True):
    artifact_id = uuid4()
    digest = hashlib.sha256()
    size = 0
    for chunk in chunks:
        digest.update(chunk)
        size += len(chunk)
    expected = expected or (digest.hexdigest(), size)
    store = InMemoryDebugStore()
    scope = {
        "type": "http", "method": "GET", "path": path or f"/v1/artifacts/{artifact_id}/content",
        "headers": [], "query_string": b"", "state": {},
    }
    sent_digest = hashlib.sha256()
    sent_bytes = 0

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        nonlocal sent_bytes
        if message["type"] == "http.response.body":
            if fail_send:
                raise OSError("synthetic disconnected customer")
            sent_digest.update(message.get("body", b""))
            sent_bytes += len(message.get("body", b""))

    async def app(scope, receive, send):
        await receive()
        await send({
            "type": "http.response.start", "status": status,
            "headers": [
                (b"content-type", b"application/octet-stream"),
                (b"content-length", str(expected[1]).encode()),
                (b"x-fs2-artifact-id", str(artifact_id).encode()),
                (b"x-fs2-artifact-sha256", expected[0].encode()),
            ],
        })
        for chunk in chunks:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        if complete:
            await send({"type": "http.response.body", "body": b""})
        else:
            raise OSError("synthetic upstream interrupted")

    error = None
    try:
        await DebugCaptureMiddleware(app, store=store)(scope, receive, send)
    except OSError as caught:
        error = caught
    (exchange,) = store.exchanges.values()
    return exchange, sent_digest.hexdigest(), sent_bytes, error


async def test_half_gib_artifact_is_logged_with_bounded_memory_and_exact_stream_digest():
    # Reuse one chunk; neither the test sink nor middleware may accumulate it.
    chunk = b"\x00\xffnative-trajectory" * 4096
    chunks = (chunk,) * 8192
    tracemalloc.start()
    try:
        exchange, digest, sent, error = await transfer(chunks=chunks)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    body = exchange.response_body
    assert error is None and body.capture_mode == "artifact_reference"
    assert sent == len(chunk) * len(chunks) == body.observed_bytes
    assert body.complete and not body.redacted
    assert body.artifact_reference.verified
    assert body.artifact_reference.sha256 == body.artifact_reference.observed_sha256 == digest
    assert body.artifact_reference.delivered_bytes == sent
    assert len(body.data) < 256 and len(exchange.model_dump_json()) < 4096
    assert peak < 8 * 1024 * 1024, f"artifact debug capture allocated {peak} bytes"


@pytest.mark.parametrize("expected", [("0" * 64, 3), (hashlib.sha256(b"abc").hexdigest(), 4)])
async def test_mismatch_is_retained_as_unverified_not_fabricated_success(expected):
    exchange, _, _, error = await transfer(chunks=(b"abc",), expected=expected)
    body = exchange.response_body
    assert error is None and body.complete and body.observed_bytes == 3
    assert body.artifact_reference.verified is False


@pytest.mark.parametrize("kwargs,delivered", [({"fail_send": True}, 0), ({"complete": False}, 3)])
async def test_disconnect_preserves_observed_and_delivered_distinction(kwargs, delivered):
    exchange, _, sent, error = await transfer(chunks=(b"abc",), **kwargs)
    body = exchange.response_body
    assert isinstance(error, OSError) and exchange.disconnected
    assert body.observed_bytes == 3 and not body.complete
    assert body.artifact_reference.delivered_bytes == sent == delivered
    assert body.artifact_reference.verified is False


@pytest.mark.parametrize("kwargs", [
    {"status": 403}, {"status": 404}, {"status": 500},
    {"path": "/v1/models/lammps:invoke"}, {"path": "/v1/artifacts/not-a-uuid/content"},
    {"expected": ("not-a-digest", 3)},
])
async def test_errors_other_routes_and_invalid_metadata_keep_full_inline_debug_capture(kwargs):
    exchange, _, _, error = await transfer(chunks=(b"abc",), **kwargs)
    body = exchange.response_body
    assert error is None and body.capture_mode == "inline"
    assert body.artifact_reference is None and body.data == "abc" and body.complete


async def test_empty_artifact_and_legacy_body_defaults():
    exchange, _, sent, error = await transfer(chunks=())
    assert error is None and sent == 0
    assert exchange.response_body.artifact_reference.verified
    assert exchange.request_body.capture_mode == "inline"
    assert exchange.request_body.artifact_reference is None
