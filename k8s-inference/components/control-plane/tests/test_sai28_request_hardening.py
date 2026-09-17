"""SAI-28 regressions for authentication ordering and bounded request parsing."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fs2_serve.api import MAX_JSON_DEPTH, TrustedEdgeMiddleware, create_app
from test_api_mcp import TestClient, build_runtime, issue


def _invoke_headers(token: str, key: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "idempotency-key": key,
        "x-fs2-wait-seconds": "0",
    }


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
    assert response.json() == {"detail": "request body must be JSON"}
    assert not runtime.store.operations  # type: ignore[attr-defined]


@pytest.mark.parametrize("error", [ValueError("invalid canonical payload"), RecursionError("excessive nesting")])
def test_admission_input_errors_are_bounded_client_errors(registry, cipher, hasher, monkeypatch, error) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    admit = AsyncMock(side_effect=error)
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
