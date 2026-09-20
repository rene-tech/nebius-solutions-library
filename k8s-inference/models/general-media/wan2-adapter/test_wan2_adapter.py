from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException


SPEC = importlib.util.spec_from_file_location("wan2_adapter", Path(__file__).with_name("app.py"))
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def request(**updates):
    values = {"prompt": "Protein ribbon rotates slowly", "size": "832x480", "seconds": 4, "seed": 7}
    values.update(updates)
    return module.GenerateRequest(**values)


def test_contract_is_bounded_and_variant_specific(monkeypatch):
    monkeypatch.setattr(module, "VARIANT", "t2v")
    assert request().steps == 50
    with pytest.raises(ValueError, match="not accepted"):
        request(input_reference="data:image/png;base64,YQ==")
    monkeypatch.setattr(module, "VARIANT", "i2v")
    with pytest.raises(ValueError, match="required"):
        request()
    assert request(input_reference="data:image/jpeg;base64,YQ==").input_reference


def test_verified_mp4_rejects_dimension_mismatch(monkeypatch):
    class Result:
        stdout = '{"format":{"format_name":"mov,mp4","duration":"3.9"},"streams":[{"codec_type":"video","codec_name":"h264","width":480,"height":832}]}'

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(HTTPException, match="dimensions"):
        module._verified_mp4(b"mp4", request())


@pytest.mark.asyncio
async def test_generate_returns_verified_raw_mp4(monkeypatch):
    class Client:
        async def post(self, url, json):
            response = httpx.Response(200, json={"data": {"b64_json": base64.b64encode(b"video").decode()}})
            return response

    monkeypatch.setattr(module, "VARIANT", "t2v")
    monkeypatch.setattr(module, "nim_client", Client())
    monkeypatch.setattr(module, "_verified_mp4", lambda raw, body: (raw, {"width": 832, "height": 480, "duration_seconds": 3.9}))
    response = await module.generate(request())
    assert response.media_type == "video/mp4"
    assert response.body == b"video"
    assert response.headers["x-fs2-wan-variant"] == "t2v"


@pytest.mark.asyncio
async def test_content_filter_is_safe_and_payload_free(monkeypatch):
    class Client:
        async def post(self, url, json):
            return httpx.Response(422, json={"detail": "contains secret prompt"})

    monkeypatch.setattr(module, "VARIANT", "t2v")
    monkeypatch.setattr(module, "nim_client", Client())
    with pytest.raises(HTTPException) as caught:
        await module.generate(request())
    assert caught.value.status_code == 422
    assert "secret prompt" not in caught.value.detail
