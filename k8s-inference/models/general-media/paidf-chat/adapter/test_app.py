"""Adapter protocol tests; model responses here are explicit fixtures."""

import hashlib
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app
from contracts import MODELS, contract

APP_ID = "qwen2-5-14b-instruct"
MODEL, REVISION = MODELS[APP_ID]


def completion():
    return {
        "id": "mock",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"prompt":"cloudy"}'},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("REFERENCE_APP_ID", APP_ID)
    with TestClient(app.app) as value:
        yield value


@pytest.mark.parametrize("stream", [False, True])
def test_original_requests_and_actual_json_or_token_stream_are_preserved(
    client, stream
):
    raw = json.dumps(completion(), indent=2)
    if stream:
        raw = (
            "data: "
            + json.dumps(
                {
                    "model": MODEL,
                    "choices": [
                        {"delta": {"content": "cloudy"}, "finish_reason": "stop"}
                    ],
                }
            )
            + "\r\n\r\ndata: [DONE]\r\n\r\n"
        )
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "original prompt"}],
        "temperature": 0.3,
        "top_p": 0.95,
        "presence_penalty": 0,
        "frequency_penalty": 1.05,
        "max_tokens": 4096,
        "stream": stream,
        "response_format": {"type": "json_object"},
    }
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=raw,
            headers={
                "content-type": "text/event-stream" if stream else "application/json"
            },
        )

    app.app.state.upstream = httpx.AsyncClient(
        base_url="http://127.0.0.1:8001", transport=httpx.MockTransport(handler)
    )
    result = client.post("/v1/reference-chat", json=payload)
    assert result.status_code == 200
    assert seen == [payload]
    value = result.json()
    assert value["response_body"] == raw
    assert value["response_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert value["model_revision"] == REVISION and value["stream"] is stream
    assert (
        value["request_sha256"]
        == hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "body,stream,media",
    [
        ("[]", False, "application/json"),
        ('{"model":"different","choices":[{}]}', False, "application/json"),
        (json.dumps(completion()), False, "text/plain"),
        ("data: [DONE]\n\n", True, "text/event-stream"),
        ("data: []\n\n", True, "text/event-stream"),
        (
            "data: "
            + json.dumps({"model": MODEL, "choices": [{"finish_reason": "stop"}]})
            + "\n\n",
            True,
            "text/event-stream",
        ),
    ],
)
def test_incomplete_or_wrong_upstream_response_is_never_succeeded(
    client, body, stream, media
):
    app.app.state.upstream = httpx.AsyncClient(
        base_url="http://127.0.0.1:8001",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=body, headers={"content-type": media}
            )
        ),
    )
    response = client.post(
        "/v1/reference-chat",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "test"}],
            "stream": stream,
        },
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "reference_upstream_response_incomplete"}


@pytest.mark.parametrize("changes", [{"model": "replacement"}, {"unknown_knob": 1}])
def test_no_model_substitution_or_silent_parameter_discard(client, changes):
    app.app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("No inference"))
    )
    response = client.post(
        "/v1/reference-chat",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "test"}],
            **changes,
        },
    )
    assert response.status_code == 422


def test_readiness_checks_actual_model_identity(client):
    app.app.state.upstream = httpx.AsyncClient(
        base_url="http://127.0.0.1:8001",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"data": [{"id": "different-model"}]}
            )
        ),
    )
    assert client.get("/v1/health/ready").status_code == 503


def test_request_byte_guard_precedes_inference(client, monkeypatch):
    monkeypatch.setattr(app, "MAX_REQUEST_BYTES", 32)
    assert client.post("/v1/reference-chat", content=b"x" * 33).status_code == 413


def test_media_is_supported_only_by_the_reference_vlm():
    from jsonschema import Draft202012Validator

    body = {
        "model": MODELS["qwen3-6-27b-fp8"][0],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "video_url",
                        "video_url": {"url": "data:video/mp4;base64,bW9jaw=="},
                    }
                ],
            }
        ],
    }
    Draft202012Validator(contract("qwen3-6-27b-fp8")).validate(body)
    body["model"] = MODEL
    assert not Draft202012Validator(contract(APP_ID)).is_valid(body)


def test_disconnect_closes_the_active_upstream_before_releasing_capacity():
    async def scenario():
        started, closed = asyncio.Event(), asyncio.Event()
        incoming = asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b"{}", "more_body": False})
        async def application(scope, receive, send):
            await receive()
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        async def send(message):
            raise AssertionError("No successful response after disconnection")
        middleware = app.BoundedRequest(application)
        task = asyncio.create_task(middleware({"type": "http", "method": "POST", "path": "/v1/reference-chat"}, incoming.get, send))
        await started.wait()
        assert middleware.lock.locked()
        await incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, 2)
        assert closed.is_set() and not middleware.lock.locked()
    asyncio.run(scenario())
