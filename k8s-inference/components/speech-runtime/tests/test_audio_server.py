import asyncio
import hashlib
import wave

import pytest
from fastapi.testclient import TestClient
from test_stream import FINISH, START, FakeRuntime

from fs2_speech.audio import AudioInputError, DownloadAudio, decoded_pcm, download_audio, transcribe_file
from fs2_speech.contracts import ENGLISH_ID, RuntimeProfile, SpeechOptions
from fs2_speech.server import create_app


def fixture(path, samples=1600):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x01\0" * samples)


def app(runtime=None):
    return create_app(runtime or FakeRuntime(), RuntimeProfile(model=ENGLISH_ID),
                      allowed_hosts=frozenset({"storage.example.test"}), load=False)


def test_real_ffmpeg_preserves_every_sample_and_long_tail(tmp_path):
    path = tmp_path / "fixture.wav"
    fixture(path, 33001)

    async def collect():
        return b"".join([chunk async for chunk in decoded_pcm(path)])

    assert asyncio.run(collect()) == b"\x01\0" * 33001


def test_decode_limit_rejects_instead_of_reporting_truncated_success(tmp_path):
    path = tmp_path / "fixture.wav"
    fixture(path, 1600)

    async def collect():
        return [chunk async for chunk in decoded_pcm(path, max_seconds=0.01)]

    with pytest.raises(AudioInputError, match="audio_duration_exceeded"):
        asyncio.run(collect())


def test_bad_audio_does_not_return_success(tmp_path):
    path = tmp_path / "bad.wav"
    path.write_bytes(b"invalid")
    with pytest.raises(AudioInputError, match="audio_decode_failed"):
        asyncio.run(transcribe_file(FakeRuntime(), path, SpeechOptions(model=ENGLISH_ID)))


def test_complete_file_finalizes_tail_and_does_not_append_partials(tmp_path):
    path = tmp_path / "fixture.wav"
    fixture(path, 1001)
    runtime = FakeRuntime()
    result = asyncio.run(transcribe_file(runtime, path, SpeechOptions(model=ENGLISH_ID)))
    assert result["text"] == "Complete."
    assert result["audio_seconds"] == 1001 / 16000
    assert sum(frame.valid_samples for frame in runtime.frames) == 1001
    assert runtime.frames[-1].last
    assert runtime.closed == [1]


@pytest.mark.parametrize("url", [
    "http://storage.example.test/a", "https://other.test/a", "https://user@storage.example.test/a",
    "https://storage.example.test:444/a", "https://storage.example.test/a#fragment",
])
def test_internal_download_rejects_non_gateway_artifact_hosts(tmp_path, url):
    source = DownloadAudio(url=url, sha256=hashlib.sha256(b"x").hexdigest(), size_bytes=1, media_type="audio/wav")
    with pytest.raises(AudioInputError, match="invalid_audio_artifact"):
        asyncio.run(download_audio(source, tmp_path / "unused", frozenset({"storage.example.test"})))


def test_websocket_transcribes_incrementally_then_drains():
    runtime = FakeRuntime()
    with TestClient(app(runtime)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        with client.websocket_connect("/v1/audio/stream") as stream:
            stream.send_text(START)
            assert stream.receive_json()["type"] == "session.ready"
            stream.send_bytes(b"\0" * 6)
            assert stream.receive_json()["type"] == "transcript.partial"
            assert client.get("/readyz").json()["active_sessions"] == 1
            assert client.post("/drain").status_code == 200
            assert client.get("/readyz").status_code == 503
            stream.send_text(FINISH)
            assert stream.receive_json()["type"] == "transcript.final"
            assert stream.receive_json()["type"] == "session.completed"
        assert runtime.closed == [1]
        assert b'fs2_speech_sessions_total{mode="live",outcome="completed"} 1.0' in client.get("/metrics").content


def test_busy_stream_is_explicit_and_disconnect_frees_capacity():
    runtime = FakeRuntime()
    with TestClient(app(runtime)) as client:
        with client.websocket_connect("/v1/audio/stream") as first:
            first.send_text(START)
            first.receive_json()
            with client.websocket_connect("/v1/audio/stream") as second:
                assert second.receive_json()["code"] == "runtime_busy"
        assert client.get("/readyz").json()["active_sessions"] == 0
        with client.websocket_connect("/v1/audio/stream") as stream:
            stream.send_text(START)
            assert stream.receive_json()["type"] == "session.ready"
            stream.send_text('{"type":"session.cancel"}')
            assert stream.receive_json()["type"] == "session.cancelled"


def test_file_runtime_rejects_profile_mismatch_before_acquiring():
    with TestClient(app()) as client:
        result = client.post("/generate", json={
            "audio": {"url": "https://storage.example.test/file", "sha256": "a" * 64,
                      "size_bytes": 12, "media_type": "audio/wav"},
            "options": {"model": ENGLISH_ID, "chunk_size_ms": 80},
        })
        assert result.status_code == 422
        assert client.get("/readyz").json()["active_sessions"] == 0
