import hashlib
import json

import httpx

from public_probe import IDS, submit_file


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
