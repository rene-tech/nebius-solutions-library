import base64
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest import mock

import httpx

spec = importlib.util.spec_from_file_location("qualify_nim", Path(__file__).with_name("qualify_nim.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
INFO = {"width": 1280, "height": 720, "frames": 153, "fps": 30, "sha256": "a" * 64}


def invoke(tmp_path, *, ready=200, result=200, generated=None, cap=None):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-video-content")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Synthetic private test prompt")
    destination = tmp_path / "result"
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/v1/health/ready":
            return httpx.Response(ready, json={})
        if request.url.path in {"/v1/metadata", "/v1/manifest"}:
            return httpx.Response(200, json={"model": "cosmos-transfer2.5"})
        assert request.url.path == "/v1/infer"
        body = json.loads(request.content)
        assert base64.b64decode(body["video"]) == source.read_bytes()
        assert body["edge"] == {"control_weight": 1.0}
        assert body["resolution"] == 720
        return httpx.Response(result, json={"b64_video": base64.b64encode(b"synthetic-output-content").decode()})

    argv = [
        "qualify_nim",
        "--source",
        str(source),
        "--prompt-file",
        str(prompt),
        "--output-directory",
        str(destination),
        "--base-url",
        "http://127.0.0.1:18755",
        "--profile-id",
        "synthetic-profile",
    ]
    client = httpx.Client(base_url="http://127.0.0.1:18755", transport=httpx.MockTransport(handle))
    output = io.StringIO()
    with (
        mock.patch.object(sys, "argv", argv),
        mock.patch.object(probe.httpx, "Client", return_value=client),
        mock.patch.object(probe, "inspect_video", side_effect=[INFO, generated or INFO]),
        mock.patch.object(probe, "MAX_RESULT_BYTES", cap or probe.MAX_RESULT_BYTES),
        contextlib.redirect_stdout(output),
    ):
        status = probe.main()
    receipt = json.loads((destination / "receipt.json").read_text())
    assert "Synthetic private" not in output.getvalue()
    assert "synthetic-video-content" not in output.getvalue()
    assert receipt["customer_ready"] is False
    assert receipt["public_platform_path_tested"] is False
    return status, receipt, requests, destination


def test_success_is_only_structural_evidence(tmp_path):
    status, receipt, requests, destination = invoke(tmp_path)
    assert status == 0
    assert receipt["alignment_passed"] is True
    assert receipt["weather_verified"] is False
    assert receipt["motion_verified"] is False
    assert (destination / "output.mp4").exists()
    assert len([r for r in requests if r.method == "POST"]) == 1


def test_no_inference_when_not_ready(tmp_path):
    status, receipt, requests, _ = invoke(tmp_path, ready=503)
    assert status == 1
    assert receipt["inference_request_attempted"] is False
    assert receipt["failure_stage"] == "readiness"
    assert all(r.method == "GET" for r in requests)


def test_inference_rejection_is_not_retried(tmp_path):
    status, receipt, requests, _ = invoke(tmp_path, result=422)
    assert status == 1
    assert receipt["http_status"] == 422
    assert receipt["failure_stage"] == "inference"
    assert len([r for r in requests if r.method == "POST"]) == 1


def test_alignment_mismatch_retains_output_but_is_not_a_pass(tmp_path):
    status, receipt, _, destination = invoke(tmp_path, generated={**INFO, "fps": 24})
    assert status == 1
    assert receipt["alignment_passed"] is False
    assert receipt["failure_stage"] == "output-alignment"
    assert (destination / "output.mp4").exists()


def test_oversized_response_is_rejected(tmp_path):
    status, receipt, _, destination = invoke(tmp_path, cap=16)
    assert status == 1
    assert receipt["failure_stage"] == "inference"
    assert not (destination / "output.mp4").exists()
