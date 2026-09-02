from __future__ import annotations

import json
import traceback
from uuid import UUID

import httpx
import pytest
from conftest import CONTROL_ROOT

from fs2_serve.models import ClaimedOperation, OperationStatus, RuntimeIdentity, TerminalAccounting
from fs2_serve.runtime import ActivationError, RuntimeClient, RuntimeOperationError
from fs2_serve.settings import Settings
from fs2_serve.telemetry import Metrics


def claimed(registry) -> ClaimedOperation:
    model = registry.get("qwen3-8b")
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    now = datetime.now(UTC)
    return ClaimedOperation(
        id=uuid4(),
        tenant_id="tenant-a",
        principal_id="principal-a",
        token_id=uuid4(),
        model_id=model.id,
        model_revision=model.model_revision,
        protocol="openai-chat",
        operation="chat",
        idempotency_key="runtime-key-0001",
        status=OperationStatus.ACTIVATING,
        accepted_at=now,
        available_at=now,
        deadline_at=now + timedelta(seconds=10),
        payload_expires_at=now + timedelta(seconds=60),
        attempt=1,
        max_attempts=2,
        fencing_token=1,
        request_content_type="application/json",
        worker_id="worker-a",
    )


class FixedTrustedMetadata:
    def __init__(self, identity: RuntimeIdentity) -> None:
        self.identity = identity
        self.calls: list[tuple[UUID, str]] = []

    async def resolve(self, *, operation_id: UUID, model_id: str) -> RuntimeIdentity:
        self.calls.append((operation_id, model_id))
        return self.identity


def test_runtime_header_boundary_preserves_bounded_whitespace_normalization() -> None:
    response = httpx.Response(200, headers={"x-runtime-state": "  ready  "})
    assert RuntimeClient._header(response, "x-runtime-state") == "ready"
    assert RuntimeClient._header(response, "x-missing") is None


@pytest.mark.parametrize(
    ("protocol", "usage", "expected"),
    [
        ("openai-chat", {"prompt_tokens": 3, "completion_tokens": 5}, (3, 5)),
        ("openai-completions", {"input_tokens": 0, "output_tokens": 8}, (0, 8)),
        ("openai-embeddings", {"prompt_tokens": 11}, (11, None)),
    ],
)
def test_runtime_extracts_strict_openai_reported_token_aliases(
    protocol: str,
    usage: dict[str, int],
    expected: tuple[int | None, int | None],
) -> None:
    body = json.dumps({"usage": usage}).encode()
    reported = RuntimeClient._reported_usage(protocol, body)
    assert reported is not None
    assert (reported.input_tokens, reported.output_tokens) == expected
    assert reported.modalities is None


@pytest.mark.parametrize(
    "usage",
    [
        None,
        [],
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": -1, "completion_tokens": 1},
        {"prompt_tokens": 2**63, "completion_tokens": 1},
        {"prompt_tokens": 1, "input_tokens": 2},
        {f"unknown_{index}": index for index in range(17)},
    ],
)
@pytest.mark.asyncio
async def test_absent_malformed_or_oversized_usage_does_not_fail_valid_inference(registry, usage: object) -> None:
    payload: dict[str, object] = {"choices": [{"message": {"content": "ok"}}]}
    if usage is not None:
        payload["usage"] = usage

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode(),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=4096,
        client=client,
    )
    try:
        result = await runtime.invoke(registry.get("qwen3-8b"), claimed(registry), b"{}")
        assert result.status_code == 200
        assert result.usage is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_runtime_attaches_reported_usage_to_successful_result(registry) -> None:
    body = b'{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":4,"completion_tokens":6}}'

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=1024,
        client=client,
    )
    try:
        result = await runtime.invoke(registry.get("qwen3-8b"), claimed(registry), b"{}")
        assert result.usage is not None
        assert result.usage.input_tokens == 4
        assert result.usage.output_tokens == 6
        assert result.usage.modalities is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_runtime_owned_client_ignores_proxy_environment() -> None:
    runtime = RuntimeClient(
        activation_timeout_seconds=10,
        runtime_timeout_seconds=10,
        max_response_bytes=1024,
    )
    try:
        assert runtime.client._trust_env is False  # type: ignore[attr-defined]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_activation_and_inference_are_streamed_and_response_is_bounded(registry) -> None:
    requested_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, headers={"content-type": "application/json"}, content=b"x" * 65)
        raise AssertionError(f"unexpected request path {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=64,
        client=client,
    )
    operation = claimed(registry)
    try:
        await runtime.activate(registry.get("qwen3-8b"), operation)
        with pytest.raises(RuntimeOperationError, match="configured maximum"):
            await runtime.invoke(registry.get("qwen3-8b"), operation, b'{"model":"qwen3-8b"}')
        assert all("activate" not in path for path in requested_paths)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_runtime_has_no_internal_activation_dns_dependency(registry) -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200)
        raise AssertionError(f"unexpected request path {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=1024,
        client=client,
    )
    try:
        await runtime.activate(registry.get("qwen3-8b"), claimed(registry))
        assert requested_paths == ["/health"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_kind",
    ["transport", "protocol", "decode", "invalid_url", "stream"],
)
async def test_runtime_transport_and_activation_errors_are_bounded_and_drop_exception_context(
    registry, failure_kind: str
) -> None:
    secret = "UPSTREAM_TRANSPORT_SECRET_MUST_NOT_LEAK_7a32"

    async def handler(request: httpx.Request) -> httpx.Response:
        if failure_kind == "transport":
            raise httpx.ReadError(secret, request=request)
        if failure_kind == "protocol":
            raise httpx.RemoteProtocolError(secret, request=request)
        if failure_kind == "decode":
            raise httpx.DecodingError(secret, request=request)
        if failure_kind == "invalid_url":
            raise httpx.InvalidURL(secret)
        raise httpx.StreamError(secret)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=1024,
        client=client,
    )
    operation = claimed(registry)
    try:
        with pytest.raises(ActivationError) as activation:
            await runtime.activate(registry.get("qwen3-8b"), operation)
        with pytest.raises(RuntimeOperationError) as invocation:
            await runtime.invoke(registry.get("qwen3-8b"), operation, b'{"model":"qwen3-8b"}')
        for captured in (activation.value, invocation.value):
            rendered = "".join(traceback.format_exception(captured))
            assert secret not in rendered
            assert captured.__cause__ is None and captured.__suppress_context__ is True
            assert len(str(captured)) <= 80
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"content-type": "application/json"}, b"PROMPT_SHAPED_INVALID_JSON_RESPONSE"),
        ({"content-type": "application/json"}, b"{}"),
        ({"content-type": "PROMPT_SHAPED_BAD_MEDIA_TYPE"}, b'{"choices":[{}]}'),
    ],
)
async def test_runtime_decode_schema_and_content_type_failures_never_reflect_upstream(
    registry, headers: dict[str, str], body: bytes
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=1024,
        client=client,
    )
    try:
        with pytest.raises(RuntimeOperationError) as captured:
            await runtime.invoke(registry.get("qwen3-8b"), claimed(registry), b'{"model":"qwen3-8b"}')
        rendered = "".join(traceback.format_exception(captured.value))
        assert body.decode(errors="ignore") not in rendered
        assert all(value not in rendered for value in headers.values())
        assert captured.value.__cause__ is None
        assert len(str(captured.value)) <= 80
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_prompt_runtime_receives_only_opaque_operation_correlation_and_ignores_forged_identity(registry) -> None:
    request_headers: dict[str, str] = {}
    request_body = b'{"messages":[{"role":"user","content":"PRIVATE_PROMPT_7d91"}]}'
    forged = {
        "x-fs2-pod-uid": "forged-pod",
        "x-fs2-node-uid": "forged-node",
        "x-fs2-gpu-uuids": "GPU-forged",
        "x-fs2-gpu-count": "64",
        "x-fs2-preemptible": "false",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        request_headers.update(request.headers)
        assert await request.aread() == request_body
        return httpx.Response(
            200,
            headers={"content-type": "application/json", **forged},
            content=b'{"choices":[{"message":{"content":"ok"}}]}',
        )

    trusted_identity = RuntimeIdentity(
        pod_uid="trusted-pod",
        node_uid="trusted-node",
        gpu_uuids=["GPU-trusted"],
        gpu_count=1,
        preemptible=True,
    )
    metadata = FixedTrustedMetadata(trusted_identity)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=1024,
        client=client,
        metadata_provider=metadata,
    )
    operation = claimed(registry).model_copy(
        update={"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"}
    )
    try:
        result = await runtime.invoke(registry.get("qwen3-8b"), operation, request_body)
        fs2_headers = {name: value for name, value in request_headers.items() if name.startswith("x-fs2-")}
        assert fs2_headers == {"x-fs2-operation-id": str(operation.id)}
        assert request_headers["x-request-id"] == f"{operation.id}:{operation.attempt}"
        assert request_headers["traceparent"] == operation.traceparent
        assert operation.tenant_id not in repr(request_headers)
        assert operation.principal_id not in repr(request_headers)
        assert str(operation.token_id) not in repr(request_headers)
        assert result.runtime == trusted_identity
        assert metadata.calls == [(operation.id, operation.model_id)]
        assert all(value not in result.runtime.model_dump().values() for value in forged.values())
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "traceparent",
    [
        "caller-controlled-value",
        "00-00000000000000000000000000000000-0123456789abcdef-01",
        "00-0123456789abcdef0123456789abcdef-0000000000000000-01",
        "00-0123456789ABCDEF0123456789ABCDEF-0123456789abcdef-01",
    ],
)
def test_runtime_drops_trace_context_outside_the_strict_forwarding_allowlist(registry, traceparent: str) -> None:
    operation = claimed(registry).model_copy(update={"traceparent": traceparent})
    assert RuntimeClient._correlation_headers(operation) == {
        "x-fs2-operation-id": str(operation.id),
        "x-request-id": f"{operation.id}:{operation.attempt}",
    }


@pytest.mark.asyncio
async def test_default_runtime_metadata_is_empty_even_when_model_forges_identity_headers(registry) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "x-fs2-pod-uid": "forged-pod",
                "x-fs2-node-uid": "forged-node",
                "x-fs2-gpu-uuids": "GPU-forged",
                "x-fs2-gpu-count": "8",
                "x-fs2-preemptible": "true",
            },
            content=b'{"choices":[{"message":{"content":"ok"}}]}',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=1024,
        client=client,
    )
    try:
        result = await runtime.invoke(registry.get("qwen3-8b"), claimed(registry), b'{"model":"qwen3-8b"}')
        assert result.runtime == RuntimeIdentity()
    finally:
        await client.aclose()


def test_durable_gpu_estimate_never_uses_untrusted_preemptible_identity(registry) -> None:
    metrics = Metrics(registry.list(enabled_only=True))
    metrics.set_terminal_accounting(
        [
            TerminalAccounting(
                model_id="qwen3-8b",
                protocol="openai-chat",
                outcome="succeeded",
                operations=1,
                estimated_gpu_seconds=5,
                duration_seconds=1,
                cold_start_seconds=0,
            )
        ]
    )
    rendered = metrics.render()
    assert b"fs2_serve_estimated_gpu_seconds_total" in rendered
    assert registry.get("qwen3-8b").gateway.gpu_class.encode() in rendered
    assert b"preemptible" not in rendered


@pytest.mark.asyncio
async def test_upstream_failure_body_is_never_buffered_into_runtime_result(registry) -> None:
    secret = b'UPSTREAM_FAILURE_BODY_MUST_NOT_REACH_LEDGER_{"prompt":"private"}'

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"content-type": "application/json"}, content=secret)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    runtime = RuntimeClient(
        activation_timeout_seconds=2,
        runtime_timeout_seconds=2,
        max_response_bytes=1024,
        client=client,
    )
    try:
        result = await runtime.invoke(registry.get("qwen3-8b"), claimed(registry), b'{"model":"qwen3-8b"}')
        assert result.status_code == 503
        assert result.body == b""
        assert result.failure_code == "upstream_http_error"
        assert secret not in repr(result).encode()
    finally:
        await client.aclose()


def test_migration_enforces_cipher_envelopes_and_contains_no_plaintext_payload_columns() -> None:
    migration = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((CONTROL_ROOT / "migrations").glob("*.sql"))
    )
    lowered = migration.lower()
    assert "request_body" not in lowered and "response_body" not in lowered
    assert "request_nonce is not null" in lowered
    assert "request_ciphertext is not null" in lowered
    assert "octet_length(request_nonce)=12" in lowered
    assert "octet_length(request_ciphertext)>=16" in lowered
    assert "response_nonce is not null" in lowered
    assert "response_ciphertext is not null" in lowered
    assert "octet_length(response_nonce)=12" in lowered
    assert "octet_length(response_ciphertext)>=16" in lowered
    assert "request_hmac_key_id" in lowered and "response_hmac_key_id" in lowered
    assert "check (attempt <= max_attempts)" in lowered
    assert "check (char_length(model_id) between 1 and 128)" in lowered
    assert "check (char_length(idempotency_key) between 8 and 200)" in lowered
    assert "fs2_operations_queued_deadline_idx" in lowered


def test_store_uses_only_migration_global_lock_and_bounded_skip_locked_janitors() -> None:
    source = (CONTROL_ROOT / "src" / "fs2_serve" / "postgres.py").read_text(encoding="utf-8")
    normalized = " ".join(source.lower().split())
    assert "727201920002" not in source
    # Migration, per-token, configuration-chain, per-model scale, dynamic-model
    # identity, and dynamic-model idempotency fences. Only migration and the
    # single configuration chain are global constants; the rest are keyed.
    assert source.count("pg_advisory_xact_lock") == 6
    assert "pg_advisory_xact_lock(fs2_activation_model_lock_key($1))" in source
    assert "async def _model_deployment_lock" in source
    assert "async def _model_deployment_idempotency_lock" in source
    assert source.count("LIMIT 100") >= 4
    assert source.count("SKIP LOCKED") >= 4
    assert "async def expire_deadline_operations" in source
    assert "deadline_expired" in source
    assert "sqlstate" in source and '"40P01", "40001"' in source
    assert (
        "join fs2_tokens t on t.id=o.token_id and t.revoked_at is null "
        "and (t.expires_at is null or t.expires_at>clock_timestamp())"
    ) in normalized
    assert (
        "where o.status='queued' and o.available_at<=clock_timestamp() "
        "and o.payload_expires_at>clock_timestamp() "
        "and (o.deadline_at is null or o.deadline_at>clock_timestamp()) "
        "and o.attempt<o.max_attempts"
    ) in normalized
    assert "where o.id=charge.id and o.attempt<o.max_attempts" in normalized


def test_activation_store_contract_does_not_ship_controller_execution() -> None:
    postgres_source = (CONTROL_ROOT / "src" / "fs2_serve" / "activation_postgres.py").read_text(encoding="utf-8")
    guard = postgres_source.split("async def activation_mutation_guard", 1)[1].split(
        "async def _validate_activation_mutation", 1
    )[0]
    assert "async with self.pool.acquire() as connection, connection.transaction()" not in guard
    assert guard.count("async with connection.transaction()") == 2
    assert "SELECT pg_advisory_lock($1)" in guard and "SELECT pg_advisory_unlock($1)" in guard
    assert guard.index("yield") < guard.rindex("async with connection.transaction()")

    assert not (CONTROL_ROOT / "src" / "fs2_serve" / "activation_controller.py").exists()
    assert not (CONTROL_ROOT / "src" / "fs2_serve" / "kubernetes_activation.py").exists()
    cli_source = (CONTROL_ROOT / "src" / "fs2_serve" / "cli.py").read_text(encoding="utf-8")
    assert "activation-controller" not in cli_source


def test_operation_public_schema_omits_keyed_request_and_response_digests() -> None:
    schema = ClaimedOperation.model_json_schema()
    rendered = json.dumps(schema)
    assert "request_hmac" not in rendered
    assert "response_hmac" not in rendered


def test_route_attestor_trust_root_is_file_mounted_bounded_and_duplicate_safe(tmp_path) -> None:
    path = tmp_path / "route-attestors.json"
    key_id = "sha256:" + "1" * 64
    path.write_text(json.dumps({key_id: "A" * 43}), encoding="utf-8")
    settings = Settings(route_attestors_file=path)
    assert settings.trusted_route_attestors() == {key_id: "A" * 43}

    path.write_text(f'{{"{key_id}":"{"A" * 43}","{key_id}":"{"B" * 43}"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        settings.trusted_route_attestors()

    path.write_bytes(b"{" + b"x" * (64 * 1024) + b"}")
    with pytest.raises(ValueError, match="too large"):
        settings.trusted_route_attestors()


@pytest.mark.parametrize(
    "field",
    ["sync_wait_seconds", "max_sync_wait_seconds", "wait_poll_initial_seconds", "wait_poll_max_seconds"],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_settings_reject_nonfinite_wait_bounds(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_settings_require_coherent_wait_poll_and_concurrency_bounds() -> None:
    with pytest.raises(ValueError, match="initial"):
        Settings(wait_poll_initial_seconds=0.6, wait_poll_max_seconds=0.5)
    with pytest.raises(ValueError, match="max_sync_waiters"):
        Settings(worker_concurrency=5, max_sync_waiters=4)


@pytest.mark.parametrize(
    "overrides",
    [
        {"activation_database_role": "fs2_serve_runtime"},
        {"activation_database_role": "fs2_serve_reporting"},
        {"activation_database_role": "fs2_serve_maintenance"},
        {"runtime_database_role": "fs2_serve_reporting"},
        {"maintenance_database_role": "fs2_serve_runtime"},
    ],
)
def test_settings_require_four_distinct_database_roles(overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="database roles must differ"):
        Settings(**overrides)


def test_public_gateway_url_derives_one_bounded_exact_transport_policy() -> None:
    settings = Settings(public_base_url="https://Inference.Example.Invalid/")
    assert settings.public_transport_allowlists() == (
        ("inference.example.invalid", "inference.example.invalid:443"),
        ("https://inference.example.invalid", "https://inference.example.invalid:443"),
    )
    assert settings.mcp_transport_allowlists() == settings.public_transport_allowlists()
    assert settings.public_origin() == "https://inference.example.invalid"
    explicit = Settings(public_base_url="https://inference.example.invalid:8443")
    assert explicit.public_transport_allowlists() == (
        ("inference.example.invalid:8443",),
        ("https://inference.example.invalid:8443",),
    )
    ip = Settings(public_base_url="https://203.0.113.17", public_authority_mode="ip")
    assert ip.public_transport_allowlists() == (
        ("203.0.113.17", "203.0.113.17:443"),
        ("https://203.0.113.17", "https://203.0.113.17:443"),
    )


@pytest.mark.parametrize(
    ("url", "mode"),
    [
        ("https://203.0.113.17", "dns"),
        ("https://inference.example.invalid", "ip"),
        ("https://203.0.113.17:8443", "ip"),
        ("https://[2001:db8::1]", "ip"),
    ],
)
def test_public_authority_mode_rejects_dns_ip_ambiguity(url: str, mode: str) -> None:
    with pytest.raises(ValueError, match="public_base_url|authority"):
        Settings(public_base_url=url, public_authority_mode=mode)


@pytest.mark.parametrize(
    "public_base_url",
    [
        "https://user@inference.example.invalid",
        "https://inference.example.invalid/mcp",
        "https://inference.example.invalid?host=spoofed.invalid",
        "https://*.example.invalid",
        f"https://{'a' * 254}.invalid",
    ],
)
def test_public_gateway_url_rejects_ambiguous_or_unbounded_authorities(public_base_url: str) -> None:
    with pytest.raises(ValueError, match="public_base_url"):
        Settings(public_base_url=public_base_url)
