import asyncio
import base64
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location("transfer25_adapter", Path(__file__).with_name("app.py"))
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    output = tmp_path_factory.mktemp("transfer25-adapter") / "fixture.mp4"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None, "real MP4 adapter tests require ffmpeg"
    subprocess.run(  # noqa: S603 - fixed synthetic fixture, no shell or customer input
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x480:rate=16",
            "-frames:v",
            "93",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        check=True,
    )
    return output.read_bytes()


def client(monkeypatch, clip, *, status=200, malformed=None, timeout=False, wrong_profile=False):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/v1/health/ready":
            return httpx.Response(200, json={"status": "ready"})
        if request.url.path == "/v1/metadata":
            return httpx.Response(
                200,
                json={
                    "selectedModelProfileId": "wrong" if wrong_profile else adapter.PROFILE_ID,
                    "version": "1.1.0",
                },
            )
        assert request.url.path == "/v1/infer"
        body = json.loads(request.content)
        assert body["resolution"] == "720"
        assert body["sigma_max"] == 90
        assert body["guidance"] == 7
        assert body["edge"] == {"control_weight": 1.0}
        assert "output_delivery" not in body
        assert base64.b64decode(body["video"]) == clip
        if timeout:
            raise httpx.ReadTimeout("synthetic-private-response")
        value = {"b64_video": base64.b64encode(clip).decode(), "seed": 42} if malformed is None else malformed
        return httpx.Response(status, json=value)

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        adapter.httpx,
        "AsyncClient",
        lambda **kwargs: factory(**kwargs, transport=httpx.MockTransport(handle)),
    )
    return TestClient(adapter.app), requests


def payload(clip):
    return {
        "video": base64.b64encode(clip).decode(),
        "prompt": "synthetic cloudy weather",
    }


def test_full_decode_alignment_and_raw_mp4_transport(monkeypatch, clip):
    test, requests = client(monkeypatch, clip)
    with test:
        response = test.post("/v1/transfer", json=payload(clip))
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == clip
    assert len([r for r in requests if r.method == "POST"]) == 1


def test_reference_native_parameters_are_not_flattened_or_discarded(monkeypatch, clip):
    test, requests = client(monkeypatch, clip)
    with test:
        response = test.post("/v1/transfer", json={**payload(clip), "resolution": "720", "sigma_max": 90,
                                                  "edge": {"control_weight": 1.0}, "num_steps": 35})
    assert response.status_code == 200
    body = json.loads(next(request for request in requests if request.method == "POST").content)
    assert body["edge"] == {"control_weight": 1.0}
    assert body["resolution"] == "720" and body["sigma_max"] == 90 and body["num_steps"] == 35


def test_nim_seed_and_step_bounds_do_not_inherit_old_adapter_caps(clip):
    value = adapter.TransferRequest.model_validate({**payload(clip), "seed": 4294967295, "num_steps": 51,
                                                    "edge": {"control_weight": 0.0}})
    assert value.seed == 4294967295 and value.num_steps == 51 and value.edge.control_weight == 0


@pytest.mark.parametrize("frames,valid", [(92, False), (93, True), (480, True), (481, False)])
def test_native_frame_range_arbitrary_geometry_and_fractional_fps(tmp_path, frames, valid):
    import hashlib
    output = tmp_path / "fractional.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "color=size=320x240:rate=30000/1001", "-frames:v", str(frames), "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(output)], check=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    if valid:
        value = adapter.inspect_video(output)
        assert (value["width"], value["height"], value["frames"], value["fps"]) == (320, 240, frames, "30000/1001")
    else:
        with pytest.raises(ValueError, match="93–480"):
            adapter.inspect_video(output)
    assert hashlib.sha256(output.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    "extra",
    [
        {"guidance": 8},
        {"guidance": 7.5},
        {"control_weight": 1.5},
        {"seed": True},
        {"num_steps": 0},
        {"edge": {"control_weight": 1.0}, "control_weight": 0.5},
        {"resolution": 720},
        {"endpoint": "https://example.invalid"},
    ],
)
def test_closed_schema_rejects_before_upstream(monkeypatch, clip, extra):
    test, requests = client(monkeypatch, clip)
    with test:
        response = test.post("/v1/transfer", json={**payload(clip), **extra})
    assert response.status_code == 422
    assert not requests
    assert "video" not in response.text


def test_url_rejected_without_generation(monkeypatch, clip):
    test, requests = client(monkeypatch, clip)
    with test:
        response = test.post(
            "/v1/transfer",
            json={**payload(clip), "video": "https://example.invalid/video.mp4"},
        )
    assert response.status_code == 422
    assert not [r for r in requests if r.method == "POST"]


def test_wrong_runtime_is_not_accepted(monkeypatch, clip):
    test, requests = client(monkeypatch, clip, wrong_profile=True)
    with test:
        response = test.post("/v1/transfer", json=payload(clip))
    assert response.status_code == 503
    assert not [r for r in requests if r.method == "POST"]


@pytest.mark.parametrize(
    "invalid",
    [
        {"b64_video": "invalid", "seed": 42},
        {"b64_video": "aGVsbG8=", "seed": 43},
        {"bad": True},
        [],
        None,
    ],
)
def test_malformed_output_cannot_pass(monkeypatch, clip, invalid):
    if invalid is None:
        invalid = {
            "b64_video": base64.b64encode(b"not-mp4-file-at-least-16-bytes").decode(),
            "seed": 42,
        }
    test, _ = client(monkeypatch, clip, malformed=invalid)
    with test:
        response = test.post("/v1/transfer", json=payload(clip))
    assert response.status_code == 502
    assert "b64_video" not in response.text


def test_unknown_outcome_blocks_subsequent_generation(monkeypatch, clip):
    test, requests = client(monkeypatch, clip, timeout=True)
    with test:
        assert test.post("/v1/transfer", json=payload(clip)).status_code == 503
        assert test.get("/v1/health/ready").status_code == 503
        assert test.post("/v1/transfer", json=payload(clip)).status_code == 503
    assert len([r for r in requests if r.method == "POST"]) == 1


def test_oversized_input_stops_before_nim(monkeypatch, clip):
    test, requests = client(monkeypatch, clip)
    monkeypatch.setattr(adapter, "MAX_JSON_BYTES", 100)
    with test:
        assert test.post("/v1/transfer", json=payload(clip)).status_code == 413
    assert not requests


def test_overlapping_request_is_rejected_before_reading_body():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        sent = []

        async def inner(scope, receive, send):
            entered.set()
            await release.wait()

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            sent.append(message)

        middleware = adapter.OneBoundedRequest(inner)
        scope = {"type": "http", "path": "/v1/transfer", "method": "POST"}
        first = asyncio.create_task(middleware(scope, receive, send))
        await entered.wait()
        await middleware(scope, receive, send)
        assert sent[0]["status"] == 429
        release.set()
        await first

    asyncio.run(scenario())
