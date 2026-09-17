"""SAI-28 regressions for authentication ordering and bounded request parsing."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from fs2_serve.admission import AdmissionInputError
from fs2_serve.api import MAX_JSON_DEPTH, TrustedEdgeMiddleware, create_app
from fs2_serve.apps_models import AppRecord
from fs2_serve.apps_scientific import ScientificAppsInventory, ScientificAppsRefreshError
from fs2_serve.models import AdmissionRequest, Principal
from test_api_mcp import TestClient, build_runtime, issue


def _invoke_headers(token: str, key: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "idempotency-key": key,
        "x-fs2-wait-seconds": "0",
    }


class _CountingAppsRepository:
    def __init__(self, records: list[AppRecord]) -> None:
        self.records = records
        self.all_reads = 0
        self.discoverable_reads = 0
        self.fail = False
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def list_records(self) -> list[AppRecord]:
        self.all_reads += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.fail:
            raise OSError("repository detail must not escape")
        return list(self.records)

    async def list_discoverable_records(self) -> list[AppRecord]:
        self.discoverable_reads += 1
        if self.fail:
            raise OSError("repository detail must not escape")
        return list(self.records)


def _scientific_app() -> AppRecord:
    app_id = uuid4()
    now = datetime.now(UTC)
    return AppRecord(
        app_id=app_id,
        model_ref="protein-design",
        public_model_id=f"app-{app_id.hex}",
        display_name="SAI-28 cache fixture",
        execution_mode="scientific",
        namespace="fs2-models",
        created_at=now,
        updated_at=now,
    )


def _principal() -> Principal:
    return Principal(
        token_id=uuid4(),
        token_prefix="fs2_pat_sai28",
        principal_id="sai28-admission",
        tenant_id="tenant-a",
        scopes=frozenset({"inference.invoke"}),
        models=frozenset({"qwen3-8b"}),
        max_concurrency=1,
    )


def _admission(*, protocol: str, body: bytes = b"{}") -> AdmissionRequest:
    return AdmissionRequest(
        model_id="qwen3-8b",
        operation="chat",
        protocol=protocol,
        idempotency_key="sai28-typed-input-0001",
        request_body=body,
    )


@pytest.mark.asyncio
async def test_scientific_app_refresh_has_a_bounded_ttl_and_force_bypasses_it() -> None:
    now = [100.0]
    repository = _CountingAppsRepository([_scientific_app()])
    inventory = ScientificAppsInventory(
        repository,  # type: ignore[arg-type]
        refresh_ttl_seconds=5,
        refresh_failure_retry_seconds=1,
        clock=lambda: now[0],
    )

    await inventory.refresh()
    await inventory.refresh()
    assert (repository.all_reads, repository.discoverable_reads) == (1, 1)

    now[0] += 5
    await inventory.refresh()
    assert (repository.all_reads, repository.discoverable_reads) == (2, 2)

    await inventory.refresh(force=True)
    assert (repository.all_reads, repository.discoverable_reads) == (3, 3)


@pytest.mark.asyncio
async def test_scientific_app_refresh_singleflights_concurrent_cache_misses() -> None:
    repository = _CountingAppsRepository([_scientific_app()])
    repository.started = asyncio.Event()
    repository.release = asyncio.Event()
    inventory = ScientificAppsInventory(repository)  # type: ignore[arg-type]

    leader = asyncio.create_task(inventory.refresh())
    await repository.started.wait()
    followers = [asyncio.create_task(inventory.refresh()) for _ in range(8)]
    repository.release.set()
    await asyncio.gather(leader, *followers)

    assert (repository.all_reads, repository.discoverable_reads) == (1, 1)


@pytest.mark.asyncio
async def test_scientific_app_refresh_failure_retains_snapshot_but_fails_closed() -> None:
    now = [100.0]
    record = _scientific_app()
    repository = _CountingAppsRepository([record])
    inventory = ScientificAppsInventory(
        repository,  # type: ignore[arg-type]
        refresh_ttl_seconds=5,
        refresh_failure_retry_seconds=1,
        clock=lambda: now[0],
    )
    await inventory.refresh()
    snapshot = dict(inventory.records)
    now[0] += 5
    repository.fail = True

    with pytest.raises(ScientificAppsRefreshError, match="refresh unavailable"):
        await inventory.refresh()
    reads_after_failure = (repository.all_reads, repository.discoverable_reads)
    with pytest.raises(ScientificAppsRefreshError, match="refresh unavailable"):
        await inventory.refresh()

    assert (repository.all_reads, repository.discoverable_reads) == reads_after_failure
    assert inventory.records == snapshot
    assert inventory.source(record.public_model_id) == record.model_ref


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admission",
    [
        _admission(protocol="openai-unknown"),
        _admission(protocol="openai-chat", body=b"{"),
    ],
)
async def test_precise_admission_input_branches_raise_the_typed_client_error(
    registry, cipher, hasher, admission
) -> None:
    runtime = build_runtime(registry, cipher, hasher)

    with pytest.raises(AdmissionInputError):
        await runtime.admission.admit(_principal(), admission)
    assert not runtime.store.operations  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_route_refresh_value_error_remains_a_server_fault(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    runtime.admission.route_refresh = AsyncMock(side_effect=ValueError("route refresh invariant"))

    with pytest.raises(ValueError, match="route refresh invariant"):
        await runtime.admission.admit(_principal(), _admission(protocol="openai-chat"))
    assert not runtime.store.operations  # type: ignore[attr-defined]


def test_scientific_app_refresh_runs_only_after_bearer_authentication(registry, cipher, hasher, monkeypatch) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    events: list[str] = []

    async def refresh() -> None:
        events.append("refresh")

    runtime.scientific_apps = SimpleNamespace(refresh=refresh)
    original_verify = runtime.tokens.verify

    async def verify(token: str):
        events.append("verify")
        return await original_verify(token)

    monkeypatch.setattr(runtime.tokens, "verify", verify)
    with TestClient(create_app(runtime)) as client:
        rejected = client.get("/v1/models", headers={"authorization": "Bearer invalid"})
        assert rejected.status_code == 401
        assert events == ["verify"]

        token = issue(client, principal="sai28-auth-order", scopes=["catalog.read"])
        events.clear()
        accepted = client.get("/v1/models", headers={"authorization": f"Bearer {token}"})

    assert accepted.status_code == 200
    assert events == ["verify", "refresh"]


def test_openai_route_rejects_json_beyond_the_nesting_limit(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    nested = "0"
    for _ in range(MAX_JSON_DEPTH):
        nested = f"[{nested}]"
    body = f'{{"model":"qwen3-8b","input":{nested}}}'.encode()

    with TestClient(create_app(runtime)) as client:
        token = issue(client, principal="sai28-json-depth", scopes=["inference.invoke"])
        response = client.post(
            "/v1/chat/completions",
            headers=_invoke_headers(token, "sai28-json-depth-0001"),
            content=body,
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {"type": "invalid_request", "message": "request body exceeds maximum JSON depth"}
    }
    assert not runtime.store.operations  # type: ignore[attr-defined]


@pytest.mark.parametrize("include_content_type", [True, False])
def test_native_route_rejects_deep_json_before_framework_validation(
    registry, cipher, hasher, include_content_type
) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    nested = "0"
    for _ in range(MAX_JSON_DEPTH):
        nested = f"[{nested}]"
    body = f'{{"operation":"chat","payload":{{"input":{nested}}}}}'.encode()

    with TestClient(create_app(runtime)) as client:
        token = issue(client, principal="sai28-native-depth", scopes=["inference.invoke"])
        headers = _invoke_headers(token, "sai28-native-depth-0001")
        if not include_content_type:
            headers.pop("content-type")
        response = client.post(
            "/v1/models/qwen3-8b:invoke",
            headers=headers,
            content=body,
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {"type": "invalid_request", "message": "request body exceeds maximum JSON depth"}
    }
    assert not runtime.store.operations  # type: ignore[attr-defined]


def test_typed_admission_input_error_is_a_bounded_client_error(registry, cipher, hasher, monkeypatch) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    admit = AsyncMock(side_effect=AdmissionInputError("invalid canonical payload"))
    monkeypatch.setattr(runtime.admission, "admit", admit)

    with TestClient(create_app(runtime)) as client:
        token = issue(client, principal="sai28-admission", scopes=["inference.invoke"])
        response = client.post(
            "/v1/chat/completions",
            headers=_invoke_headers(token, "sai28-admission-0001"),
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "bounded"}]},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "request body is invalid"}


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [(ValueError("server invariant"), 500), (RecursionError("server recursion"), 503)],
)
def test_untyped_admission_faults_are_not_misclassified_as_client_input(
    registry, cipher, hasher, monkeypatch, error, expected_status
) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    monkeypatch.setattr(runtime.admission, "admit", AsyncMock(side_effect=error))

    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        token = issue(client, principal="sai28-server-fault", scopes=["inference.invoke"])
        response = client.post(
            "/v1/chat/completions",
            headers=_invoke_headers(token, "sai28-server-fault-0001"),
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "bounded"}]},
        )

    assert response.status_code == expected_status
    assert response.status_code != 400
    assert "server invariant" not in response.text
    assert "server recursion" not in response.text


def test_streamed_body_overflow_is_413_through_the_full_application_stack(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)

    with TestClient(create_app(runtime)) as client:
        token = issue(client, principal="sai28-stream-limit", scopes=["inference.invoke"])
        chunks = iter((b'{"model":"qwen3-8b","input":"', b"x" * 1024, b'"}'))
        response = client.post(
            "/v1/chat/completions",
            headers=_invoke_headers(token, "sai28-stream-limit-0001"),
            content=chunks,
        )

    assert response.status_code == 413
    assert response.json() == {
        "error": {"type": "request_too_large", "message": "request body exceeds limit"}
    }
    assert not runtime.store.operations  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_raw_artifact_content_is_not_treated_as_an_api_json_document() -> None:
    downstream_completed = False
    body = b"[" * (MAX_JSON_DEPTH + 1) + b"0" + b"]" * (MAX_JSON_DEPTH + 1)
    path = "/v1/scientific-artifacts/uploads/00000000-0000-0000-0000-000000000000/content"

    async def downstream(scope, receive, send) -> None:
        nonlocal downstream_completed
        message = await receive()
        assert message["body"] == body
        downstream_completed = True

    messages = iter(({"type": "http.request", "body": body, "more_body": False},))
    sent: list[dict] = []

    async def receive() -> dict:
        return next(messages)

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = TrustedEdgeMiddleware(
        downstream,
        max_request_bytes=1024,
        allowed_hosts=("inference.test.invalid",),
        allowed_origins=(),
    )
    await middleware(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"inference.test.invalid"),
                (b"content-type", b"application/json"),
            ],
            "client": ("192.0.2.1", 12345),
            "server": ("inference.test.invalid", 443),
        },
        receive,
        send,
    )

    assert downstream_completed
    assert sent == []


@pytest.mark.asyncio
async def test_streamed_body_overflow_emits_a_structured_413_response() -> None:
    downstream_completed = False

    async def downstream(scope, receive, send) -> None:
        nonlocal downstream_completed
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        downstream_completed = True

    messages = iter(
        (
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        )
    )
    sent: list[dict] = []

    async def receive() -> dict:
        return next(messages)

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = TrustedEdgeMiddleware(
        downstream,
        max_request_bytes=4,
        allowed_hosts=("inference.test.invalid",),
        allowed_origins=(),
    )
    await middleware(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"inference.test.invalid")],
            "client": ("192.0.2.1", 12345),
            "server": ("inference.test.invalid", 443),
        },
        receive,
        send,
    )

    assert not downstream_completed
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"]) == {
        "error": {"type": "request_too_large", "message": "request body exceeds limit"}
    }
