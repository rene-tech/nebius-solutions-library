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
from fs2_serve.voice_routes import MAGPIE, VoiceSynthesisRequest, relay_synthesis, relay_voice_stream


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


class StreamSocket:
    def __init__(self, events):
        self.events = list(events)
        self.sent = []
        self.finished = asyncio.Event()

    async def send(self, value):
        self.sent.append(value)
        if value == '{"type":"session.finish"}':
            self.finished.set()

    async def recv(self):
        return json.dumps({"type": "session.ready"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.finished.wait()
        if not self.events:
            raise StopAsyncIteration
        return json.dumps(self.events.pop(0))


@pytest.mark.asyncio
async def test_stream_retains_finals_not_partials_and_records_usage():
    model, operation = objects()
    model.id = "parakeet-realtime-eou-120m-v1"
    socket = StreamSocket(
        [
            {"type": "transcript.partial", "text": "Hel"},
            {"type": "transcript.final", "text": "Hello"},
            {"type": "turn.eou", "source": "model_token"},
            {"type": "session.done", "audio_seconds": 1.0},
        ]
    )
    audio, output = asyncio.Queue(), asyncio.Queue()
    await audio.put(b"\0\0" * 16000)
    await audio.put('{"type":"session.finish"}')
    result = await relay_voice_stream(model, operation, b"{}", audio, output.put, connector=lambda *a, **k: socket)
    assert json.loads(result.body)["text"] == "Hello"
    assert len(json.loads(result.body)["events"]) == 2
    assert result.usage.modalities[0].amount == 1.0
    assert output.qsize() == 4  # ready + partial + final + actual EOU; done withheld until durable
    assert socket.sent[-1] == '{"type":"session.finish"}'


@pytest.mark.asyncio
async def test_stream_worker_loss_does_not_create_complete_result():
    model, operation = objects()
    socket = StreamSocket([{"type": "transcript.partial", "text": "incomplete"}])
    audio = asyncio.Queue()
    await audio.put('{"type":"session.finish"}')
    with pytest.raises(RuntimeTransportError):
        await relay_voice_stream(model, operation, b"{}", audio, asyncio.Queue().put, connector=lambda *a, **k: socket)
