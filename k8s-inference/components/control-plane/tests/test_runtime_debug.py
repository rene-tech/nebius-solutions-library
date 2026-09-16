from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from test_federation import _operation, _router
from test_runtime_and_schema import claimed

from fs2_serve.request_debug import DebugCapturePolicy, DebugExchange, InMemoryDebugStore
from fs2_serve.runtime import PreemptedError, RuntimeClient, RuntimeProtocolError, RuntimeTransportError

# The test operations belong to tenants "tenant-a" (claimed) and "tenant-private"
# (_operation); capture is scoped and time-bounded (fail-closed), so upstream
# capture tests opt into those tenants and a far-future window.
_CAPTURE_POLICY = DebugCapturePolicy(
    enabled=True,
    tenants=frozenset({"tenant-a", "tenant-private"}),
    expires_at=datetime(2099, 1, 1, tzinfo=UTC),
)


class DebugSink:
    def __init__(self) -> None:
        self.exchanges: list[DebugExchange] = []

    async def record(self, exchange: DebugExchange) -> None:
        self.exchanges.append(exchange)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.iterations = 0
        self.closed = False

    async def __aiter__(self):
        self.iterations += 1
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self) -> None:
        self.closed = True


def native(registry):
    model = registry.get("qwen3-8b")
    binding = replace(model.binding, endpoints={"native": "/native/predict"})
    return replace(model, gateway=replace(model.gateway, binding=binding))


def runtime(client, sink, *, maximum=4096, federation=None):
    return RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=maximum,
        client=client,
        debug_store=sink,
        federation=federation,
        debug_capture_policy=_CAPTURE_POLICY,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422, 503])
async def test_native_rejection_captures_exact_upstream_request_and_validation_response(registry, status) -> None:
    request_body = b'{ "input": {"unexpected": "synthetic fixture"}, "samples": 2 }\n'
    error_body = b'{"detail":[{"loc":["body","sequences"],"msg":"Field required","type":"missing"}]}'
    requests = []

    async def handler(request):
        requests.append(request)
        assert await request.aread() == request_body
        return httpx.Response(status, content=error_body, headers={"content-type": "application/json"})

    sink = DebugSink()
    operation = claimed(registry).model_copy(update={"protocol": "native"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await runtime(client, sink).invoke(native(registry), operation, request_body)
    assert len(requests) == len(sink.exchanges) == 1
    assert result.status_code == status and result.body == b""
    assert result.failure_code == "upstream_http_error"
    exchange = sink.exchanges[0]
    assert exchange.source == "upstream" and exchange.request_id is None
    assert exchange.operation_id == operation.id and exchange.operation_attempt == operation.attempt
    assert exchange.upstream_attempt == 1
    assert (exchange.tenant_id, exchange.principal_id, exchange.token_id, exchange.model_id) == (
        operation.tenant_id,
        operation.principal_id,
        operation.token_id,
        operation.model_id,
    )
    assert exchange.method == "POST" and exchange.endpoint == "/native/predict"
    assert exchange.http_status == status and exchange.error_type == "upstream_http_error"
    assert exchange.started_at <= exchange.completed_at
    # The request (debugging target) is captured verbatim. The error response is
    # fail-closed: structural fields (loc/type) are kept, but free-text detail (msg) is
    # redacted (no free-text or reversible hash); only structure/numbers remain.
    assert exchange.request_body.data.encode() == request_body and exchange.request_body.complete
    assert exchange.response_body.complete and exchange.response_body.redacted
    stored = exchange.response_body.data
    assert "Field required" not in stored and "missing" not in stored and "[sha256:" not in stored
    assert '"detail":[' in stored and '"[REDACTED]"' in stored  # key structure kept, string values redacted
    assert exchange.request_body.observed_bytes == len(request_body)
    assert exchange.response_body.observed_bytes == len(error_body)
    assert not exchange.request_body.redacted
    assert dict(exchange.request_headers)["x-request-id"] == f"{operation.id}:1"
    assert dict(exchange.response_headers)["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_success_capture_preserves_request_and_hashes_response_content(registry) -> None:
    request_body = '{"messages":[{"content":"synthetic café fixture"}]}\n'.encode()
    response_body = b'{ "choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 3} }\n'

    async def handler(request):
        assert request.content == request_body
        return httpx.Response(200, content=response_body, headers={"content-type": "application/json"})

    sink = DebugSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await runtime(client, sink).invoke(registry.get("qwen3-8b"), claimed(registry), request_body)
    assert result.body == response_body and result.semantic_outcome == "protocol_valid"
    assert result.usage.input_tokens == 3
    exchange = sink.exchanges[0]
    assert exchange.error_type is None and exchange.http_status == 200
    # The request (debugging target) is kept verbatim; the response string values are
    # redacted (free-text never stored), while numeric fields and structure are kept.
    assert exchange.request_body.data.encode() == request_body
    stored = exchange.response_body.data
    assert exchange.response_body.complete and exchange.response_body.redacted
    assert '"content":"[REDACTED]"' in stored and '"prompt_tokens":3' in stored and '"content": "ok"' not in stored


@pytest.mark.asyncio
async def test_out_of_scope_operation_is_gated_before_capture(registry) -> None:
    """SAI-01: an out-of-scope upstream exchange is gated before wrapping/buffering."""
    response_body = b'{ "choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 3} }\n'

    async def handler(_request):
        return httpx.Response(200, content=response_body, headers={"content-type": "application/json"})

    sink = DebugSink()
    scoped_elsewhere = DebugCapturePolicy(
        enabled=True, tenants=frozenset({"some-other-tenant"}), expires_at=datetime(2099, 1, 1, tzinfo=UTC)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime_client = RuntimeClient(
            activation_timeout_seconds=2,
            runtime_timeout_seconds=2,
            max_response_bytes=4096,
            client=client,
            debug_store=sink,
            debug_capture_policy=scoped_elsewhere,
        )
        result = await runtime_client.invoke(registry.get("qwen3-8b"), claimed(registry), b'{"messages":[]}')
    assert result.body == response_body  # response still flows to the caller
    assert sink.exchanges == []  # nothing captured for the out-of-scope tenant


@pytest.mark.asyncio
async def test_binary_error_response_is_withheld_without_changing_public_result(registry) -> None:
    """SAI-01: an arbitrary/binary response body is not structurally recognized and is
    withheld (fail closed) rather than stored, since a secret in opaque bytes cannot
    be scrubbed. The public result and status are unchanged."""
    raw = b"\xff\x00\x81validation failure\x80"

    async def handler(_request):
        return httpx.Response(400, content=raw, headers={"content-type": "application/octet-stream"})

    sink = DebugSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await runtime(client, sink).invoke(
            native(registry), claimed(registry).model_copy(update={"protocol": "native"}), b"{}"
        )
    assert result.status_code == 400 and result.body == b""
    captured = sink.exchanges[0].response_body
    assert _stored(captured) == b"[REDACTED]" and captured.redacted and captured.truncated
    assert captured.complete and captured.observed_bytes == len(raw)  # true length still reported


@pytest.mark.asyncio
async def test_stored_error_is_retrievable_with_shared_header_and_query_credential_redaction(registry) -> None:
    header_secret = "synthetic-header-credential-123456"
    query_secret = "synthetic-query-credential-987654"
    body_secret = "synthetic-body-credential-123987"
    request_body = b'{"fixture":"synthetic request","api_key":"synthetic-body-credential-123987"}'
    model = native(registry)
    model = replace(
        model,
        gateway=replace(
            model.gateway,
            binding=replace(
                model.binding, endpoints={"native": f"/native/predict?access_token={query_secret}&format=json"}
            ),
        ),
    )

    async def handler(request):
        assert request.headers["x-auth-token"] == header_secret
        assert request.url.params["access_token"] == query_secret
        assert request.content == request_body
        return httpx.Response(422, json={"detail": "missing input", "echo": [header_secret, query_secret, body_secret]})

    sink = InMemoryDebugStore()
    operation = claimed(registry).model_copy(update={"protocol": "native"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"x-auth-token": header_secret}
    ) as client:
        result = await runtime(client, sink).invoke(model, operation, request_body)
    assert result.status_code == 422 and result.body == b""
    listing = await sink.list(operation_id=operation.id, tenant_id=operation.tenant_id)
    assert len(listing.items) == 1
    detail = await sink.get(listing.items[0].id, tenant_id=operation.tenant_id)
    assert detail is not None and detail.response_body.complete
    # Response string values are redacted (free-text never stored); echoed secrets gone.
    assert detail.response_body.redacted and "missing input" not in detail.response_body.data
    assert "[sha256:" not in detail.response_body.data and "[REDACTED]" in detail.response_body.data
    assert detail.request_body.redacted and "synthetic request" in detail.request_body.data
    rendered = detail.model_dump_json()
    assert header_secret not in rendered and query_secret not in rendered and body_secret not in rendered
    assert "format=json" in detail.query_string
    assert await sink.get(detail.id, tenant_id="unrelated-tenant") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["content_type", "invalid_json", "preempted"])
async def test_protocol_failures_capture_body_without_reclassifying_public_exception(registry, case) -> None:
    body = b"synthetic non-JSON validation detail"
    headers = {"content-type": "text/plain" if case == "content_type" else "application/json"}
    status = 409 if case == "preempted" else 200
    if case == "preempted":
        headers["x-fs2-preempted"] = "true"

    async def handler(_request):
        return httpx.Response(status, content=body, headers=headers)

    sink = DebugSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PreemptedError if case == "preempted" else RuntimeProtocolError):
            await runtime(client, sink).invoke(registry.get("qwen3-8b"), claimed(registry), b"{}")
    exchange = sink.exchanges[0]
    # None of these bodies is a valid JSON document (plain text, or JSON-labeled but
    # non-JSON), so all are withheld fail-closed regardless of content type.
    assert _stored(exchange.response_body) == b"[REDACTED]" and exchange.response_body.redacted
    assert exchange.response_body.complete
    assert exchange.http_status == status
    assert exchange.error_type == ("PreemptedError" if case == "preempted" else "RuntimeProtocolError")


@pytest.mark.asyncio
async def test_timeout_before_headers_captures_actual_request_and_incomplete_response(registry) -> None:
    calls = []

    async def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic upstream timed out", request=request)

    sink = DebugSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeTransportError, match="timed out"):
            await runtime(client, sink).invoke(registry.get("qwen3-8b"), claimed(registry), b'{"fixture":true}')
    assert len(calls) == len(sink.exchanges) == 1
    exchange = sink.exchanges[0]
    assert exchange.http_status is None and exchange.error_type == "ReadTimeout"
    # The raw exception string is never stored (it can embed a secret); only a generic,
    # payload-independent detail is kept, with the error_type carrying the classification.
    assert exchange.error_detail == "runtime operation failed" and "synthetic" not in exchange.error_detail
    assert exchange.request_body.data == '{"fixture":true}'
    assert not exchange.response_body.complete and exchange.response_body.observed_bytes == 0
    assert dict(exchange.request_headers)["host"] == calls[0].headers["host"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 422])
async def test_partial_read_preserves_prefix_and_original_failure_behavior(registry, status) -> None:
    stream = Chunks([b'{"partial":'], httpx.ReadTimeout("synthetic interrupted body"))

    async def handler(_request):
        return httpx.Response(status, stream=stream, headers={"content-type": "application/json"})

    sink = DebugSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        target = runtime(client, sink)
        if status == 200:
            with pytest.raises(RuntimeTransportError):
                await target.invoke(registry.get("qwen3-8b"), claimed(registry), b"{}")
        else:
            result = await target.invoke(registry.get("qwen3-8b"), claimed(registry), b"{}")
            assert result.status_code == 422 and result.failure_code == "upstream_http_error"
    exchange = sink.exchanges[0]
    assert exchange.http_status == status and exchange.error_type == "ReadTimeout"
    # An incomplete (interrupted) response is withheld fail-closed, never stored as a prefix.
    assert _stored(exchange.response_body) == b"[REDACTED]" and not exchange.response_body.complete
    assert exchange.response_body.observed_bytes == len(b'{"partial":')
    assert stream.iterations == 1 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 422])
async def test_oversized_response_is_explicitly_incomplete_at_existing_bound(registry, status) -> None:
    stream = Chunks([b"123456", b"789012", b"not consumed"])

    async def handler(_request):
        return httpx.Response(status, stream=stream, headers={"content-type": "application/json"})

    sink = DebugSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        target = runtime(client, sink, maximum=8)
        if status == 200:
            with pytest.raises(RuntimeProtocolError, match="configured maximum"):
                await target.invoke(registry.get("qwen3-8b"), claimed(registry), b"{}")
        else:
            result = await target.invoke(registry.get("qwen3-8b"), claimed(registry), b"{}")
            assert result.status_code == 422 and result.body == b""
    exchange = sink.exchanges[0]
    assert exchange.error_type == "ResponseBodyLimitExceeded"
    # Over the existing response bound -> incomplete -> withheld fail-closed.
    assert _stored(exchange.response_body) == b"[REDACTED]" and not exchange.response_body.complete
    assert exchange.response_body.observed_bytes == 12
    assert stream.iterations == 1 and stream.closed


@pytest.mark.asyncio
async def test_disabled_capture_does_not_consume_error_body(registry) -> None:
    stream = Chunks([], AssertionError("disabled debug must not read failure body"))

    async def handler(_request):
        return httpx.Response(422, stream=stream, headers={"content-type": "application/json"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await runtime(client, None).invoke(registry.get("qwen3-8b"), claimed(registry), b"{}")
    assert result.status_code == 422 and stream.iterations == 0 and stream.closed


@pytest.mark.asyncio
async def test_capture_failure_never_retries_or_replaces_valid_result_or_logs_payload(registry, caplog) -> None:
    calls = []

    class FailingSink(DebugSink):
        async def record(self, exchange):
            raise RuntimeError("SYNTHETIC_STORE_CREDENTIAL_OR_PAYLOAD")

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await runtime(client, FailingSink()).invoke(registry.get("qwen3-8b"), claimed(registry), b"{}")
    assert result.status_code == 200 and len(calls) == 1
    assert "SYNTHETIC_STORE_CREDENTIAL_OR_PAYLOAD" not in caplog.text


@pytest.mark.asyncio
async def test_distinct_existing_operation_attempts_are_not_merged_or_retried(registry) -> None:
    seen = []

    async def handler(request):
        seen.append(request.headers["x-request-id"])
        return httpx.Response(422, json={"detail": "synthetic missing field"})

    sink = DebugSink()
    operation = claimed(registry)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        target = runtime(client, sink)
        for attempt in (1, 2):
            result = await target.invoke(
                registry.get("qwen3-8b"), operation.model_copy(update={"attempt": attempt}), b"{}"
            )
            assert result.status_code == 422
    assert seen == [f"{operation.id}:1", f"{operation.id}:2"]
    assert [exchange.operation_attempt for exchange in sink.exchanges] == [1, 2]
    assert {exchange.operation_id for exchange in sink.exchanges} == {operation.id}
    assert len({exchange.id for exchange in sink.exchanges}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["http_status", "timeout"])
async def test_federated_internal_retry_captures_each_attempt_and_redacts_actual_credentials(
    tmp_path, registry, first_failure
) -> None:
    calls = []

    async def handler(request):
        calls.append(request)
        token = request.headers["authorization"].partition(" ")[2]
        if len(calls) == 1:
            if first_failure == "timeout":
                raise httpx.ReadTimeout(f"synthetic timeout credential={token}", request=request)
            return httpx.Response(503, json={"detail": "busy", "echo": token})
        return httpx.Response(200, json={"choices": [{}]}, headers={"set-cookie": "session=fake-cookie-secret"})

    router, model, _clients = _router(tmp_path, registry, handler)
    sink = DebugSink()
    try:
        async with httpx.AsyncClient() as unused_local_client:
            result = await runtime(unused_local_client, sink, federation=router).invoke(
                model, _operation(model), b'{"messages":[{"content":"synthetic fixture"}]}'
            )
        assert result.status_code == 200 and len(calls) == 2
        assert [exchange.upstream_attempt for exchange in sink.exchanges] == [1, 2]
        assert [exchange.http_status for exchange in sink.exchanges] == [
            503 if first_failure == "http_status" else None,
            200,
        ]
        assert sink.exchanges[0].response_body.complete == (first_failure == "http_status")
        assert sink.exchanges[1].response_body.complete
        rendered = "\n".join(exchange.model_dump_json() for exchange in sink.exchanges)
        assert "federation-test-value-one" not in rendered and "fake-cookie-secret" not in rendered
        if first_failure == "http_status":
            # 503 error response: string values redacted; the echoed auth token is gone.
            assert sink.exchanges[0].response_body.redacted
            assert "busy" not in sink.exchanges[0].response_body.data
            assert "[sha256:" not in sink.exchanges[0].response_body.data
        else:
            # The timeout message embedded a credential; the raw string is never stored,
            # only a generic detail — the token cannot leak through error_detail.
            assert sink.exchanges[0].error_type == "ReadTimeout"
            assert sink.exchanges[0].error_detail == "runtime operation failed"
        assert calls[0].content == calls[1].content
        assert calls[0].headers["idempotency-key"] == calls[1].headers["idempotency-key"]
    finally:
        await router.close()


@pytest.mark.asyncio
async def test_cancellation_records_incomplete_attempt_and_does_not_retry(registry) -> None:
    calls = []

    async def handler(request):
        calls.append(request)
        raise asyncio.CancelledError()

    sink = DebugSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asyncio.CancelledError):
            await runtime(client, sink).invoke(registry.get("qwen3-8b"), claimed(registry), b"{}")
    assert len(calls) == len(sink.exchanges) == 1
    exchange = sink.exchanges[0]
    assert exchange.disconnected and exchange.error_type == "CancelledError"
    assert not exchange.response_body.complete


@pytest.mark.asyncio
async def test_upstream_bounded_response_does_not_store_a_boundary_credential(registry) -> None:
    """SAI-01: a credential echoed in a large upstream response, straddling the debug
    cap in a body that exceeds the bounded buffer, must not be stored verbatim."""
    secret = "UPSTREAMKEY" + "".join(f"{i % 10}" for i in range(120))  # 131-char credential
    request_body = b'{"api_key":"' + secret.encode() + b'","messages":[]}'
    response_body = b'{"detail":"rejected ' + secret.encode() + b'","pad":"' + b"Z" * 4000 + b'"}'

    async def handler(_request):
        return httpx.Response(400, content=response_body, headers={"content-type": "application/json"})

    sink = DebugSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime_client = RuntimeClient(
            activation_timeout_seconds=2,
            runtime_timeout_seconds=2,
            max_response_bytes=1 << 20,
            client=client,
            debug_store=sink,
            debug_max_body_bytes=64,
            debug_capture_policy=_CAPTURE_POLICY,
        )
        await runtime_client.invoke(registry.get("qwen3-8b"), claimed(registry), request_body)
    exchange = sink.exchanges[0]
    assert exchange.response_body.observed_bytes == len(response_body)
    assert exchange.response_body.truncated
    detail = exchange.model_dump_json()
    assert all(secret[:size] not in detail for size in range(8, len(secret) + 1))


def _stored(body):
    return body.data.encode() if body.encoding == "utf-8" else base64.b64decode(body.data)


@pytest.mark.asyncio
async def test_upstream_request_body_is_captured_bounded_not_whole(registry) -> None:
    """SAI-01: a large upstream request is captured as a bounded prefix, never whole."""
    cap = 64
    big_request = b'{"input":"' + b"Q" * (100 * 1024) + b'"}'

    async def handler(_request):
        return httpx.Response(200, json={"choices": [{}]}, headers={"content-type": "application/json"})

    sink = DebugSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime_client = RuntimeClient(
            activation_timeout_seconds=2,
            runtime_timeout_seconds=2,
            max_response_bytes=1 << 20,
            client=client,
            debug_store=sink,
            debug_max_body_bytes=cap,
            debug_capture_policy=_CAPTURE_POLICY,
        )
        await runtime_client.invoke(registry.get("qwen3-8b"), claimed(registry), big_request)
    exchange = sink.exchanges[0]
    assert exchange.request_body.observed_bytes == len(big_request)  # true length reported
    assert exchange.request_body.truncated  # stored as a bounded prefix
    assert len(_stored(exchange.request_body)) <= cap  # never the whole request body


@pytest.mark.asyncio
async def test_upstream_response_suppressed_when_request_tail_uninspected(registry) -> None:
    """SAI-01: an arbitrary credential beyond the request inspection buffer must not be
    echoed into a stored upstream response body."""
    cap = 64
    secret = "UPSTREAMTAILSECRET0123456789ABCDEF"
    # The secret lies far past the inspection buffer (cap + overlap) in the request.
    big_request = b'{"pad":"' + b"Q" * (cap + 8192 + 500) + b'","k":"' + secret.encode() + b'"}'
    response_body = b'{"echo":"' + secret.encode() + b'"}'

    async def handler(_request):
        return httpx.Response(400, content=response_body, headers={"content-type": "application/json"})

    sink = DebugSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime_client = RuntimeClient(
            activation_timeout_seconds=2,
            runtime_timeout_seconds=2,
            max_response_bytes=1 << 20,
            client=client,
            debug_store=sink,
            debug_max_body_bytes=cap,
            debug_capture_policy=_CAPTURE_POLICY,
        )
        await runtime_client.invoke(registry.get("qwen3-8b"), claimed(registry), big_request)
    exchange = sink.exchanges[0]
    assert secret not in exchange.model_dump_json()  # not echoed anywhere in the row
    assert _stored(exchange.response_body) == b"[REDACTED]"  # response body withheld
    assert exchange.response_body.observed_bytes == len(response_body)  # true length still reported
