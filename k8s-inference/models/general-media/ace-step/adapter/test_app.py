import importlib
import json
import struct

import httpx
import pytest
from fastapi.testclient import TestClient


def wav() -> bytes:
    samples = b"\x00\x00" * 480
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(samples))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 48000, 96000, 2, 16)
        + b"data"
        + struct.pack("<I", len(samples))
        + samples
    )


@pytest.fixture
def module(monkeypatch):
    monkeypatch.setenv("ACESTEP_POLL_SECONDS", "0")
    import app

    return importlib.reload(app)


def transport(body: bytes | None = None, failed: bool = False) -> httpx.MockTransport:
    calls = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"code": 200, "error": None, "data": {
                "status": "ok", "models_initialized": True, "llm_initialized": True
            }})
        if request.url.path == "/release_task":
            payload = json.loads(request.content)
            assert payload["audio_format"] == "wav"
            assert payload["lm_model_path"] == "acestep-5Hz-lm-4B"
            assert payload["batch_size"] == 1
            return httpx.Response(200, json={"code": 200, "error": None, "data": {"task_id": "job-1"}})
        if request.url.path == "/query_result":
            calls["poll"] += 1
            if failed:
                record = {"status": 2, "result": "[]"}
            elif calls["poll"] == 1:
                record = {"status": 0, "result": "[]"}
            else:
                record = {"status": 1, "result": json.dumps([{"file": "/v1/audio?path=result.wav"}])}
            return httpx.Response(200, json={"code": 200, "error": None, "data": [record]})
        if request.url.path == "/v1/audio":
            return httpx.Response(200, content=body or wav(), headers={"content-type": "audio/wav"})
        raise AssertionError(request.url)

    return httpx.MockTransport(handler)


def test_ready_and_generate_bounded_wav(module):
    with TestClient(module.app) as api:
        module.client = httpx.AsyncClient(transport=transport())
        assert api.get("/v1/health/ready").status_code == 200
        response = api.post("/generate", json={"prompt": "restrained cinematic electronic music", "seed": 7})
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.headers["x-fs2-model-id"] == "ace-step-1-5"
        assert response.content == wav()


def test_rejects_unbounded_and_unknown_inputs(module):
    with TestClient(module.app) as api:
        assert api.post("/generate", json={"prompt": "x", "duration_seconds": 61}).status_code == 422
        assert api.post("/generate", json={"prompt": "x", "extra": True}).status_code == 422


def test_scrubs_upstream_failure(module):
    with TestClient(module.app) as api:
        module.client = httpx.AsyncClient(transport=transport(failed=True))
        response = api.post("/generate", json={"prompt": "x"})
        assert response.status_code == 502
        assert response.json() == {"detail": "ACE-Step generation failed"}
