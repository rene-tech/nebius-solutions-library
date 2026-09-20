import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("qualify_public", Path(__file__).with_name("qualify_public.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

UPLOAD = "00000000-0000-4000-8000-000000000001"
OPERATION = "00000000-0000-4000-8000-000000000002"
ARTIFACT = "00000000-0000-4000-8000-000000000003"
PAYLOAD = b"synthetic media, only for offline transport tests"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.mark.parametrize("failure", [None, "bad-upload-path", "failed-operation", "wrong-output-hash", "duplicate-job"])
def test_public_probe_artifacts_replay_and_failures(tmp_path, monkeypatch, capsys, failure):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"secret": "synthetic-not-a-real-key"}))
    key.chmod(0o600)
    source = tmp_path / "source.mp4"
    source.write_bytes(PAYLOAD)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Private synthetic prompt")
    output = tmp_path / "result"
    requests = []
    invokes = []
    reference = {
        "artifact_id": ARTIFACT,
        "sha256": DIGEST,
        "size_bytes": len(PAYLOAD),
        "media_type": "video/mp4",
        "compression": "none",
    }

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-not-a-real-key"
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": probe.MODEL}]})
        if path == "/v1/scientific-artifacts/uploads":
            content = "/v1/scientific-artifacts/uploads/" + UPLOAD + "/content?operation_id=" + UPLOAD
            return httpx.Response(
                201,
                json={
                    "upload_id": UPLOAD,
                    "operation_id": UPLOAD,
                    "content_path": "https://example.org/upload" if failure == "bad-upload-path" else content,
                },
            )
        if path == "/v1/operations/" + UPLOAD:
            return httpx.Response(200, json={"id": UPLOAD, "status": "queued"})
        if path.endswith("/content") and request.method == "PUT":
            assert request.content == PAYLOAD
            return httpx.Response(200, json={})
        if path.endswith(":finalize"):
            return httpx.Response(200, json=reference)
        if path.endswith(":invoke"):
            invokes.append(request)
            assert json.loads(request.content)["payload"]["video"] == reference
            identity = ARTIFACT if failure == "duplicate-job" and len(invokes) == 2 else OPERATION
            return httpx.Response(202, json={"operation": {"id": identity, "status": "queued"}})
        if path == "/v1/operations/" + OPERATION:
            return httpx.Response(
                200, json={"id": OPERATION, "status": "failed" if failure == "failed-operation" else "succeeded"}
            )
        if path.endswith("/result"):
            result_reference = {**reference, "sha256": "0" * 64} if failure == "wrong-output-hash" else reference
            return httpx.Response(200, json={"result": {"content_type": "video/mp4", "artifact": result_reference}})
        assert path == "/v1/artifacts/" + ARTIFACT + "/content"
        return httpx.Response(200, content=PAYLOAD)

    factory = httpx.Client

    def client(**kwargs):
        assert kwargs["verify"] is True and kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return factory(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(probe.httpx, "Client", client)
    monkeypatch.setattr(
        probe,
        "inspect_video",
        lambda _: {
            "width": 1280,
            "height": 720,
            "frames": 153,
            "fps": 30,
            "size_bytes": len(PAYLOAD),
            "sha256": DIGEST,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify_public",
            "--source",
            str(source),
            "--prompt-file",
            str(prompt),
            "--key-file",
            str(key),
            "--output-directory",
            str(output),
        ],
    )
    code = probe.main()
    receipt = json.loads((output / "receipt.json").read_text())
    printed = capsys.readouterr().out
    assert "synthetic-not-a-real-key" not in printed and "Private synthetic prompt" not in printed
    assert receipt["customer_ready"] is False
    assert len(invokes) <= 2
    if failure:
        assert code == 1 and receipt["status"] == "failed"
    else:
        assert code == 0 and receipt["idempotent_replay_passed"] is True
        assert len(invokes) == 2 and invokes[0].content == invokes[1].content
        assert invokes[0].headers["Idempotency-Key"] == invokes[1].headers["Idempotency-Key"]
        assert (output / "output.mp4").read_bytes() == PAYLOAD
