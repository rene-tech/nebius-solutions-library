from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter

MANIFEST = Path(__file__).parents[1] / "k8s" / "cosmos3-nano.yaml"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32


def _data_url(media_type: str, raw: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"


@pytest.fixture(scope="module")
def adapter() -> ModuleType:
    documents = [item for item in yaml.safe_load_all(MANIFEST.read_text()) if item]
    source = next(
        item["data"]["adapter.py"]
        for item in documents
        if item.get("kind") == "ConfigMap" and "adapter.py" in item.get("data", {})
    )
    module = ModuleType("fs2_cosmos3_adapter_test")
    module.__file__ = str(MANIFEST) + "#adapter.py"
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102
    return module


class _StreamResponse:
    def __init__(self, body: bytes, *, status_code: int = 200, content_type: str = "video/mp4") -> None:
        self.body = body
        self.status_code = status_code
        self.headers = {"content-type": content_type, "content-length": str(len(body))}

    async def aiter_bytes(self):
        yield self.body


class _StreamContext:
    def __init__(self, response: _StreamResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _StreamResponse:
        return self.response

    async def __aexit__(self, *args: Any) -> None:
        return None


class _VideoClient:
    def __init__(self, adapter: ModuleType, *, tmp_path: Path | None = None) -> None:
        self.adapter = adapter
        self.tmp_path = tmp_path
        self.calls: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> _StreamContext:
        assert method == "POST" and url.endswith("/v1/videos/sync")
        files = kwargs["files"]
        call = {
            "files": files,
            "extra_params": json.loads(files["extra_params"][1]),
        }
        self.calls.append(call)
        if self.tmp_path is not None:
            for control in call["extra_params"].values():
                if isinstance(control, dict) and "control_path" in control:
                    assert (self.tmp_path / Path(control["control_path"]).name).is_file()
        return _StreamContext(_StreamResponse(MP4))


@pytest.mark.asyncio
async def test_video_to_video_maps_url_compatibility_to_upstream_multipart(adapter):
    request = TypeAdapter(adapter.GenerateRequest).validate_python(
        {
            "mode": "video-to-video",
            "prompt": "Preserve the robot motion while changing the lighting.",
            "vision_path": _data_url("video/mp4", MP4),
            "condition_frame_indexes_vision": [0, 2],
            "condition_video_keep": "last",
            "output_delivery": "artifact",
        }
    )
    client = _VideoClient(adapter)

    raw, media_type = await adapter.generate_video(client, request)

    assert raw == MP4 and media_type == "video/mp4"
    (call,) = client.calls
    assert call["files"]["input_reference"] == ("reference.mp4", MP4, "video/mp4")
    assert call["extra_params"] == {
        "use_resolution_template": False,
        "use_duration_template": False,
        "guardrails": False,
        "condition_frame_indexes_vision": [0, 2],
        "condition_video_keep": "last",
    }


@pytest.mark.asyncio
async def test_transfer_control_is_shared_with_upstream_and_removed(adapter, tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "UPSTREAM_TMP", tmp_path)
    monkeypatch.setattr(adapter, "UPSTREAM_VISIBLE_TMP", Path("/cosmos-control-tmp"))
    request = TypeAdapter(adapter.GenerateRequest).validate_python(
        {
            "mode": "transfer-video",
            "prompt": "Follow the supplied depth while preserving the scene.",
            "controls": [
                {
                    "control_type": "depth",
                    "reference": _data_url("video/mp4", MP4),
                    "control_weight": 1.25,
                }
            ],
            "resolution": 256,
            "output_delivery": "artifact",
        }
    )
    client = _VideoClient(adapter, tmp_path=tmp_path)

    raw, media_type = await adapter.generate_video(client, request)

    assert raw == MP4 and media_type == "video/mp4"
    depth = client.calls[0]["extra_params"]["depth"]
    assert depth["control_path"].startswith("/cosmos-control-tmp/fs2-cosmos-control-")
    assert depth["control_weight"] == 1.25
    assert client.calls[0]["extra_params"]["resolution"] == 256
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_inverse_dynamics_returns_validated_compact_action_json(adapter, monkeypatch):
    request = TypeAdapter(adapter.GenerateRequest).validate_python(
        {
            "mode": "inverse-dynamics",
            "prompt": "Recover the steering action.",
            "input_reference": _data_url("video/mp4", MP4),
            "domain_name": "av",
            "raw_action_dim": 2,
            "action_chunk_size": 4,
            "num_frames": 5,
        }
    )

    class Client(_VideoClient):
        async def post(self, url: str, **kwargs: Any) -> _StreamResponse:
            assert url.endswith("/v1/videos") and "input_reference" in kwargs["files"]
            return _StreamResponse(b'{"id":"video_gen_action_test"}', content_type="application/json")

        async def get(self, url: str) -> _StreamResponse:
            assert url.endswith("/v1/videos/video_gen_action_test")
            return _StreamResponse(
                json.dumps(
                    {
                        "status": "completed",
                        "action": {
                            "action_mode": "inverse_dynamics",
                            "raw_action_dim": 2,
                            "shape": [4, 2],
                            "data": [[0.0, 0.1], [0.1, 0.2], [0.2, 0.3], [0.3, 0.4]],
                        },
                    }
                ).encode(),
                content_type="application/json",
            )

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(adapter.asyncio, "sleep", no_sleep)
    raw, media_type = await adapter.generate_video(Client(adapter), request)
    result = json.loads(raw)

    assert media_type == "application/json"
    assert result["mode"] == "inverse-dynamics"
    assert result["action"]["shape"] == [4, 2]
    assert result["action"]["data"][3] == [0.3, 0.4]


@pytest.mark.asyncio
async def test_malformed_video_reference_is_rejected_before_upstream_generation(
    adapter,
):
    with pytest.raises(adapter.AdapterError) as failure:
        await adapter.load_reference(
            _VideoClient(adapter),
            _data_url("video/mp4", b"not-an-mp4"),
            expected="video",
            limit=1024,
        )
    assert failure.value.status_code == 422
    assert failure.value.code == "invalid_input_media"


@pytest.mark.asyncio
async def test_upstream_video_error_is_bounded_and_retryable(adapter):
    request = TypeAdapter(adapter.GenerateRequest).validate_python(
        {"mode": "text-to-video", "prompt": "A bounded upstream error."}
    )

    class Client(_VideoClient):
        def stream(self, method: str, url: str, **kwargs: Any) -> _StreamContext:
            del method, url, kwargs
            return _StreamContext(
                _StreamResponse(
                    b'{"error":"vendor detail"}',
                    status_code=503,
                    content_type="application/json",
                )
            )

    with pytest.raises(adapter.AdapterError) as failure:
        await adapter.generate_video(Client(adapter), request)
    assert failure.value.status_code == 502
    assert failure.value.code == "upstream_error"
    assert failure.value.retryable is True


@pytest.mark.asyncio
async def test_inverse_dynamics_timeout_is_terminal_and_bounded(adapter, monkeypatch):
    request = TypeAdapter(adapter.GenerateRequest).validate_python(
        {
            "mode": "inverse-dynamics",
            "prompt": "Recover the action trajectory.",
            "input_reference": _data_url("video/mp4", MP4),
            "domain_name": "av",
            "raw_action_dim": 2,
            "action_chunk_size": 4,
            "num_frames": 5,
        }
    )

    class Client(_VideoClient):
        async def post(self, url: str, **kwargs: Any) -> _StreamResponse:
            del url, kwargs
            return _StreamResponse(b'{"id":"video_gen_timeout_test"}', content_type="application/json")

    times = iter((100.0, 2_000.0))
    monkeypatch.setattr(adapter, "time", SimpleNamespace(monotonic=lambda: next(times)))
    with pytest.raises(adapter.AdapterError) as failure:
        await adapter.generate_video(Client(adapter), request)
    assert failure.value.status_code == 504
    assert failure.value.code == "upstream_timeout"
    assert failure.value.retryable is True


@pytest.mark.asyncio
async def test_inverse_dynamics_cancellation_deletes_upstream_job(adapter, monkeypatch):
    request = TypeAdapter(adapter.GenerateRequest).validate_python(
        {
            "mode": "inverse-dynamics",
            "prompt": "Recover the action trajectory.",
            "input_reference": _data_url("video/mp4", MP4),
            "domain_name": "av",
            "raw_action_dim": 2,
            "action_chunk_size": 4,
            "num_frames": 5,
        }
    )

    class Client(_VideoClient):
        def __init__(self, module: ModuleType) -> None:
            super().__init__(module)
            self.deleted: list[str] = []

        async def post(self, url: str, **kwargs: Any) -> _StreamResponse:
            del url, kwargs
            return _StreamResponse(b'{"id":"video_gen_cancel_test"}', content_type="application/json")

        async def delete(self, url: str) -> None:
            self.deleted.append(url)

    async def cancel(_: float) -> None:
        raise asyncio.CancelledError

    client = Client(adapter)
    monkeypatch.setattr(adapter.asyncio, "sleep", cancel)
    with pytest.raises(asyncio.CancelledError):
        await adapter.generate_video(client, request)
    assert client.deleted == [f"{adapter.UPSTREAM_BASE_URL}/v1/videos/video_gen_cancel_test"]


@pytest.fixture
def encoded_video(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.fail("Cosmos output-contract qualification requires ffmpeg and ffprobe")

    def build(*, frames=9, audio=False):
        video = tmp_path / f"source-{frames}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=256x256:rate=25",
                "-frames:v",
                str(frames),
                "-c:v",
                "libx264",
                "-bf",
                "2",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ],
            check=True,
            capture_output=True,
        )
        if not audio:
            return video
        sound = tmp_path / "audio.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=1",
                "-c:a",
                "aac",
                str(sound),
            ],
            check=True,
            capture_output=True,
        )
        joined = tmp_path / "with-audio.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(video),
                "-i",
                str(sound),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c",
                "copy",
                str(joined),
            ],
            check=True,
            capture_output=True,
        )
        return joined

    return build


def decoded_hash(path, *, frames=None, audio=False):
    command = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path)]
    if audio:
        command += ["-map", "0:a:0", "-f", "f32le", "-"]
    else:
        command += ["-map", "0:v:0", "-pix_fmt", "rgb24", "-frames:v", str(frames), "-f", "rawvideo", "-"]
    raw = subprocess.run(command, check=True, capture_output=True).stdout
    return len(raw), hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("sound", [False, True])
def test_video_padding_removed_losslessly_with_original_longer_audio(adapter, encoded_video, tmp_path, sound):
    source = encoded_video(audio=sound)
    body = SimpleNamespace(
        size="256x256", fps=25, num_frames=8, generate_sound=sound, sound_duration=1.0 if sound else None
    )
    result = adapter.verified_video_output(source.read_bytes(), body)
    destination = tmp_path / "verified.mp4"
    destination.write_bytes(result)
    info, audio = adapter._video_info(destination)
    assert info["decoded_frames"] == 8
    assert info["frame_rate"] == 25
    assert decoded_hash(destination, frames=8) == decoded_hash(source, frames=8)
    if sound:
        assert float(audio[0]["duration"]) >= 0.99  # Must not shorten it to 8/25 seconds.
        assert decoded_hash(destination, audio=True) == decoded_hash(source, audio=True)
    else:
        assert not audio


def test_exact_video_is_verified_without_reencoding(adapter, encoded_video):
    raw = encoded_video(frames=9).read_bytes()
    body = SimpleNamespace(size="256x256", fps=25, num_frames=9, generate_sound=False)
    assert adapter.verified_video_output(raw, body) == raw


@pytest.mark.parametrize(
    "changes", [{"num_frames": 10}, {"num_frames": 5}, {"size": "272x256"}, {"fps": 24}, {"generate_sound": True}]
)
def test_video_contract_mismatch_is_finite_failure(adapter, encoded_video, changes):
    raw = encoded_video(frames=9).read_bytes()
    body = SimpleNamespace(**({"size": "256x256", "fps": 25, "num_frames": 9, "generate_sound": False} | changes))
    with pytest.raises(adapter.AdapterError) as failure:
        adapter.verified_video_output(raw, body)
    assert failure.value.code == "invalid_upstream_video"
    assert failure.value.retryable is False


def test_invalid_container_does_not_get_requested_frame_headers(adapter):
    body = SimpleNamespace(size="256x256", fps=25, num_frames=9, generate_sound=False)
    with pytest.raises(adapter.AdapterError) as failure:
        adapter.verified_video_output(MP4, body)
    assert failure.value.code == "invalid_upstream_video"
    assert failure.value.retryable is False
