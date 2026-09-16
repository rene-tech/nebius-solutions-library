import asyncio
import base64
import io
import json
import wave
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from fs2_serve.runtime import RuntimeProtocolError, RuntimeTransportError
from fs2_serve.voice_routes import MAGPIE, VoiceSynthesisRequest, relay_synthesis


def objects():
    return (
        SimpleNamespace(binding=SimpleNamespace(backend_class="local-kubernetes", service_origin="http://voice:8000")),
        SimpleNamespace(id=uuid4(), deadline_at=None),
    )


def events():
    return [
        {"type": "audio.start", "sample_rate_hz": 22050, "encoding": "pcm_s16le"},
        {"type": "audio.chunk", "sequence": 0, "audio_base64": base64.b64encode(b"\1\0" * 20).decode()},
        {"type": "audio.done", "samples": 20, "chunks": 1},
    ]


@pytest.mark.asyncio
async def test_relay_preserves_wav_and_counts_exact_usage():
    model, operation = objects()
    seen = []

    async def send(event):
        seen.append(event)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="\n".join(map(json.dumps, events()))))
    )
    async with client:
        result = await relay_synthesis(model, operation, b'{"text":"Hello"}', send, client=client)
    with wave.open(io.BytesIO(result.body), "rb") as wav:
        assert wav.getframerate() == 22050 and wav.getnframes() == 20
        assert wav.readframes(20) == b"\1\0" * 20
    assert result.content_type == "audio/wav"
    assert result.usage.modalities[1].amount == 20 / 22050
    assert [e["type"] for e in seen] == ["audio.start", "audio.chunk"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing_done", "wrong_count", "out_of_order", "bad_base64", "after_done"])
async def test_incomplete_or_corrupt_audio_never_becomes_artifact(mutation):
    stream = events()
    if mutation == "missing_done":
        stream.pop()
    elif mutation == "wrong_count":
        stream[-1]["samples"] = 19
    elif mutation == "out_of_order":
        stream[1]["sequence"] = 2
    elif mutation == "bad_base64":
        stream[1]["audio_base64"] = "invalid!!!!"
    else:
        stream.append(stream[1])
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="\n".join(map(json.dumps, stream))))
    )
    async with client:
        with pytest.raises((RuntimeProtocolError, RuntimeTransportError)):
            await relay_synthesis(*objects(), b'{"text":"Hello"}', asyncio.Queue().put, client=client)


@pytest.mark.parametrize("fields", [{"voice": "unknown"}, {"text": " "}, {"language": "xx"}, {"speaker": 0}])
def test_voice_contract_rejects_unsupported_options(fields):
    with pytest.raises(ValidationError):
        VoiceSynthesisRequest(**{"text": "hello", **fields})
    assert VoiceSynthesisRequest(text="hello").model == MAGPIE
