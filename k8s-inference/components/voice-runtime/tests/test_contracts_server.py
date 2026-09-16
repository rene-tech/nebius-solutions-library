import base64
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fs2_voice.contracts import MAGPIE, MODELS, PARAKEET, SORTFORMER, StreamStart, SynthesisRequest
from fs2_voice.runtime import Runtime, phrases
from fs2_voice.server import create_app


class FakeRuntime:
    model_id = PARAKEET
    timings = {}
    samples = 0
    resets = 0

    def reset(self):
        self.resets += 1
        self.samples = 0

    def feed(self, pcm):
        if not pcm or len(pcm) % 2 or len(pcm) > 32000:
            raise ValueError("invalid_pcm_frame")
        self.samples += len(pcm) // 2
        return [{"type": "transcript.partial", "text": "hello", "segment": 0}]

    def finish(self):
        return [{"type": "transcript.final", "text": "hello", "segment": 0}]

    def synthesize(self, request, cancel):
        yield b"\x01\x02" * 400
        yield b"\x03\x04" * 500


@pytest.mark.parametrize(
    "payload",
    [
        {"text": " "},
        {"text": "hi", "voice": "unknown"},
        {"text": "hi", "language": "xx"},
        {"text": "hi", "speaker": 2},
        {"text": "hi", "apply_text_normalization": "true"},
        {"text": "x" * 4097},
        {"text": "hi", "model": PARAKEET},
    ],
)
def test_invalid_synthesis(payload):
    with pytest.raises(ValidationError):
        SynthesisRequest(**payload)


def test_only_streaming_models():
    with pytest.raises(ValidationError):
        StreamStart(model=MAGPIE)
    assert StreamStart(model=SORTFORMER).audio.sample_rate_hz == 16000


def test_pins_and_phrase_content():
    for spec in MODELS.values():
        assert len(spec.revision) == 40 and len(spec.sha256) == 64
    text = "Hello there! " + "long words " * 40 + "再见。More words?"
    chunks = list(phrases(text))
    assert all(0 < len(c) <= 100 for c in chunks)
    assert "".join("".join(chunks).split()) == "".join(text.split())


def test_asr_delta_and_actual_turn_tokens():
    runtime = Runtime(PARAKEET)
    results = iter(
        [
            SimpleNamespace(text=" hello", is_final=False),
            SimpleNamespace(text=" world<EOU>", is_final=True, eou_prob=0.8, eob_prob=None),
            SimpleNamespace(text=" next", is_final=False),
        ]
    )
    runtime.model = SimpleNamespace(transcribe=lambda _: next(results))
    events = runtime.feed(bytes(runtime.frame_bytes * 3))
    assert [e["text"] for e in events if e["type"] == "transcript.partial"] == ["hello", "hello world", "next"]
    assert [e["type"] for e in events].count("turn.eou") == 1
    assert "turn.eob" not in [e["type"] for e in events]
    assert events[-1]["segment"] == 1


def test_stream_finish_reset_cancel_overload_and_drain():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime, load=False)) as client:
        assert client.get("/readyz").status_code == 200
        with client.websocket_connect("/v1/voice/stream") as ws:
            ws.send_json({"type": "session.start", "model": PARAKEET})
            assert ws.receive_json()["type"] == "session.ready"
            with client.websocket_connect("/v1/voice/stream") as other:
                other.send_json({"type": "session.start", "model": PARAKEET})
                assert other.receive_json()["code"] == "worker_busy"
            ws.send_bytes(bytes(2560))
            assert ws.receive_json()["type"] == "transcript.partial"
            ws.send_json({"type": "session.reset"})
            assert ws.receive_json()["type"] == "session.reset"
            assert runtime.samples == 0
            ws.send_bytes(bytes(2560))
            ws.receive_json()
            assert client.post("/drain").json()["active"] == 1
            assert client.get("/readyz").status_code == 503
            ws.send_json({"type": "session.finish"})
            assert ws.receive_json()["type"] == "transcript.final"
            assert ws.receive_json()["audio_seconds"] == 0.08
        assert not client.app.state.voice["busy"]
        assert 'fs2_voice_requests_total{status="completed"} 1.0' in client.get("/metrics").text


def test_invalid_audio_releases_worker_and_new_session_is_fresh():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime, load=False)) as client:
        with client.websocket_connect("/v1/voice/stream") as ws:
            ws.send_json({"model": PARAKEET})
            ws.receive_json()
            ws.send_bytes(b"odd")
            assert ws.receive_json()["code"] == "invalid_pcm_frame"
        with client.websocket_connect("/v1/voice/stream") as ws:
            ws.send_json({"model": PARAKEET})
            assert ws.receive_json()["type"] == "session.ready"
            assert runtime.samples == 0
            ws.send_json({"type": "session.cancel"})
            assert ws.receive_json()["type"] == "session.cancelled"
        assert not client.app.state.voice["busy"]


def test_synthesis_stream_has_exact_audio_and_terminal_accounting():
    runtime = FakeRuntime()
    runtime.model_id = MAGPIE
    with TestClient(create_app(runtime, load=False)) as client:
        response = client.post("/v1/voice/synthesize", json={"text": "Hello.", "voice": "Jason"})
        events = [json.loads(line) for line in response.text.splitlines()]
        pcm = b"".join(base64.b64decode(e["audio_base64"]) for e in events if e["type"] == "audio.chunk")
        assert len(pcm) == 1800
        assert events[0]["voice"] == "Jason"
        assert events[-1]["type"] == "audio.done"
        assert events[-1]["samples"] == 900
        assert not client.app.state.voice["busy"]
        assert client.post("/v1/voice/synthesize", json={"text": "again"}).status_code == 200
