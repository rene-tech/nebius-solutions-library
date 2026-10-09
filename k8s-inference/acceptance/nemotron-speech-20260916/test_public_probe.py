import hashlib
import json
import subprocess
import wave

import httpx
import pytest

from public_probe import IDS, prepare_audio, resolve_result, submit_file


def test_large_recording_keeps_all_bytes_and_uses_async_artifact(tmp_path):
    content = b"a" * (8 * 1024 * 1024 + 1)
    path = tmp_path / "full.flac"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    row = {"transport_sha256": digest, "transport_bytes": len(content)}
    calls = []
    artifact = {"artifact_id": "a", "sha256": digest, "size_bytes": len(content),
                "media_type": "audio/flac", "compression": "none"}

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/scientific-artifacts/uploads":
            assert json.loads(request.content)["sha256"] == digest
            return httpx.Response(201, json={"operation_id": "u-op", "upload_id": "u",
                "content_path": "/bytes", "max_content_bytes": len(content)})
        if request.url.path == "/bytes":
            assert request.content == content
            assert request.headers["content-length"] == str(len(content))
            return httpx.Response(200, json={})
        if request.url.path.endswith(":finalize"):
            return httpx.Response(200, json=artifact)
        assert request.url.path == "/v1/models/" + IDS[1] + ":invoke"
        assert request.headers["x-fs2-wait-seconds"] == "0"
        assert json.loads(request.content) == {"operation": "transcribe", "payload": {
            "audio": artifact, "options": {"model": "nemotron-speech-multilingual-0.6b", "language": "de"}}}
        return httpx.Response(202, json={"id": "op"})

    with httpx.Client(base_url="https://platform.test", transport=httpx.MockTransport(handler)) as client:
        assert submit_file(client, path, IDS[1], "de", row).status_code == 202
    assert len(calls) == 4
    assert row["transport"] == "artifact-native-async"
    assert row["artifact_id"] == "a"


def test_small_recording_uses_multipart(tmp_path):
    path = tmp_path / "full.flac"
    path.write_bytes(b"actual-audio")
    row = {}

    def handler(request):
        assert request.url.path == "/v1/audio/transcriptions"
        assert b"actual-audio" in request.content
        assert IDS[0].encode() in request.content
        return httpx.Response(200, json={"text": "hello"})

    with httpx.Client(base_url="https://platform.test", transport=httpx.MockTransport(handler)) as client:
        assert submit_file(client, path, IDS[0], "en", row).status_code == 200
    assert row["transport"] == "multipart"


def test_long_fixture_repeats_every_source_sample(tmp_path):
    source = tmp_path / "source.wav"
    # Nonzero varying PCM makes silent padding/trimming detectable.
    raw = b"".join((i % 3000).to_bytes(2, "little", signed=True) for i in range(16000))
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(raw)
    output = tmp_path / "four.flac"
    metadata = prepare_audio(source, output, 4)
    decoded = subprocess.check_output(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                                       "-i", str(output), "-f", "s16le", "pipe:1"])
    assert decoded == raw * 4
    assert metadata["source_audio_seconds"] == 1
    assert metadata["expected_audio_seconds"] == 4
    assert metadata["source_repeat_count"] == 4
    assert metadata["transport_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


@pytest.mark.parametrize("corrupt", [False, True])
def test_result_artifact_download_is_verified_before_reading(corrupt):
    content = json.dumps({"text": "complete transcript", "audio_seconds": 1831.68}).encode()
    envelope = {"schema": "fs2-serve.nebius.ai/operation-artifact-result/v1",
                "content_type": "application/json", "artifact": {"artifact_id": "abc", "compression": "none",
                "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}}
    row = {}

    def handler(request):
        assert request.url.path == "/v1/artifacts/abc/content"
        assert request.headers["authorization"] == "Bearer ordinary-test-key"
        return httpx.Response(200, content=b"wrong" if corrupt else content)

    with httpx.Client(base_url="https://platform.test", headers={"authorization": "Bearer ordinary-test-key"},
                      transport=httpx.MockTransport(handler)) as client:
        if corrupt:
            with pytest.raises(RuntimeError, match="checksum_mismatch"):
                resolve_result(client, envelope, row)
        else:
            assert resolve_result(client, envelope, row) == json.loads(content)
            assert row["result_artifact_verified"]
        assert row["result_envelope"] == envelope
