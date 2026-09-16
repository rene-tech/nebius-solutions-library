"""Pinned local Magpie WAV validation, artifact preservation and audio accounting."""

import hashlib
import io
import json
import wave
from dataclasses import replace

import httpx
import pytest
from test_artifact_outputs import _Artifacts
from test_runtime_and_schema import claimed

from fs2_serve.artifact_outputs import ServingOutputArtifactizer
from fs2_serve.runtime import RuntimeBusyError, RuntimeClient, RuntimeProtocolError

MAGPIE = "magpie-tts-multilingual-357m"


def wav(*, rate=22050, channels=1, width=2, frames=2205):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(b"\x01" * frames * channels * width)
    return output.getvalue()


async def invoke(registry, content, *, model_id=MAGPIE, status=200, content_type="audio/wav"):
    model = registry.get("qwen3-8b")
    model = replace(
        model,
        gateway=replace(
            model.gateway, model_id=model_id, binding=replace(model.binding, endpoints={"native": "/generate"})
        ),
    )
    operation = claimed(registry).model_copy(update={"protocol": "native", "model_id": model_id})

    def handler(request):
        if model_id == MAGPIE:
            assert request.headers["connection"] == "close"
        return httpx.Response(status, content=content, headers={"content-type": content_type})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = RuntimeClient(
            activation_timeout_seconds=2, runtime_timeout_seconds=2, max_response_bytes=32768, client=client
        )
        return operation, await runtime.invoke(model, operation, b'{"text":"Synthetic test"}')


@pytest.mark.asyncio
async def test_magpie_wav_stays_complete_through_normal_artifact_result(registry):
    body = wav()
    operation, result = await invoke(registry, body)
    assert result.status_code == 200 and result.semantic_outcome == "protocol_valid"
    assert result.body == body and result.content_type == "audio/wav"
    usage = result.usage.modalities[0]
    assert (usage.modality, usage.direction, usage.unit, usage.amount) == ("audio", "output", "seconds", 0.1)
    artifacts = _Artifacts()
    published = await ServingOutputArtifactizer(artifacts).externalize(operation, result)
    envelope = json.loads(published.body)
    assert envelope["content_type"] == "audio/wav"
    assert envelope["artifact"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert envelope["artifact"]["size_bytes"] == len(body)
    assert artifacts.content == body and artifacts.opened.tenant_id == operation.tenant_id
    assert published.usage == result.usage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,content_type",
    [
        (b"not a wave", "audio/wav"),
        (wav()[:-2], "audio/wav"),
        (wav() + b"trailing", "audio/wav"),
        (wav(rate=16000), "audio/wav"),
        (wav(channels=2), "audio/wav"),
        (wav(width=1), "audio/wav"),
        (wav(frames=0), "audio/wav"),
        (wav(), "application/json"),
        (b'{"error":"generation failed"}', "application/json"),
        (b'{"error":"generation failed"}', "audio/wav"),
    ],
)
async def test_magpie_rejects_malformed_wrong_format_and_json_success_errors(registry, body, content_type):
    with pytest.raises(RuntimeProtocolError):
        await invoke(registry, body, content_type=content_type)


@pytest.mark.asyncio
async def test_magpie_http_error_remains_payload_free_failure(registry):
    _, result = await invoke(registry, b'{"detail":"generation failed"}', status=503, content_type="application/json")
    assert result.status_code == 503 and result.body == b"" and result.usage is None
    assert result.failure_code == "upstream_http_error"


@pytest.mark.asyncio
async def test_other_native_model_still_requires_json(registry):
    with pytest.raises(RuntimeProtocolError):
        await invoke(registry, wav(), model_id="qwen3-8b")
    _, result = await invoke(registry, b'{"ok":true}', model_id="qwen3-8b", content_type="application/json")
    assert result.semantic_outcome == "protocol_valid" and result.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", [MAGPIE, "parakeet-realtime-eou-120m-v1", "diar-streaming-sortformer-4spk-v2-1"])
async def test_known_voice_pre_admission_busy_can_reselect_a_sibling(registry, model_id):
    with pytest.raises(RuntimeBusyError):
        await invoke(
            registry, b'{"detail":"runtime_busy"}', model_id=model_id, status=429, content_type="application/json"
        )
