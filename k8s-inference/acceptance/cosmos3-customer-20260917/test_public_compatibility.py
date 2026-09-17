"""Offline schemas and actual legacy/native result shapes; never submits inference."""

import base64
import hashlib
import json
import shutil
import subprocess

import public_compatibility as runner
import pytest

from fs2_serve.model_input_contracts import _cosmos, _cosmos_mode_schema


def legacy(raw, content_type="image/png"):
    return {
        "mode": "text-to-image",
        "mime_type": content_type,
        "data_base64": base64.b64encode(raw).decode(),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "width": 256,
        "height": 256,
    }


@pytest.mark.parametrize("wrapped", [False, True])
def test_inline_legacy_json_png_is_not_treated_as_binary(wrapped):
    raw = b"synthetic-output-not-an-inference"
    result = legacy(raw)
    decoded, metadata = runner.decode_result({"result": result} if wrapped else result, "image/png")
    assert decoded == raw
    assert metadata["delivery"] == "inline-json-base64"
    assert "data_base64" not in metadata["legacy_metadata"]


@pytest.mark.parametrize("binary", [False, True])
def test_whole_json_artifact_and_native_binary_artifact_are_distinct(binary):
    raw = b"synthetic-output-not-an-inference"
    artifact_raw = raw if binary else json.dumps(legacy(raw)).encode()
    artifact = {
        "artifact_id": "synthetic",
        "sha256": hashlib.sha256(artifact_raw).hexdigest(),
        "size_bytes": len(artifact_raw),
    }
    envelope = {"result": {"content_type": "image/png" if binary else "application/json", "artifact": artifact}}
    decoded, metadata = runner.decode_result(envelope, "image/png", artifact_raw)
    assert decoded == raw
    assert metadata["delivery"] == ("binary-artifact" if binary else "externalized-json-base64")
    with pytest.raises(ValueError, match="artifact_identity_mismatch"):
        runner.decode_result(envelope, "image/png", artifact_raw + b"changed")


@pytest.mark.parametrize(
    "field,value", [("mime_type", "video/mp4"), ("bytes", 1), ("sha256", "0" * 64), ("data_base64", "!")]
)
def test_invalid_legacy_envelope_fails_closed(field, value):
    envelope = legacy(b"synthetic-output") | {field: value}
    with pytest.raises(ValueError):
        runner.decode_result(envelope, "image/png")


def test_all_four_public_requests_validate_before_first_admission():
    tools = {}
    for mode in ("text-to-image", "text-to-video", "image-to-video"):
        schema = _cosmos_mode_schema(mode)
        schema["properties"].update(idempotency_key={"type": "string"}, wait_seconds={"type": "number"})
        tools["cosmos3_nano_" + mode.replace("-", "_")] = {"inputSchema": schema}
    schema = {"contracts": [{"tool_name": "cosmos3_nano_generate_media_native", "input_schema": _cosmos()}]}
    reference = {
        "artifact_id": "00000000-0000-4000-8000-000000000123",
        "sha256": "0" * 64,
        "size_bytes": 1,
        "media_type": "image/png",
        "compression": "none",
    }
    runner.validate_contract(tools, schema, reference)
    body = runner.payload("text-to-video")
    assert "output_delivery" not in runner.http_invocation(body)["payload"]
    assert body["num_frames"] == 33 and body["fps"] == 20 and body["generate_sound"] is False
    assert runner.payload("image-to-video", reference)["input_reference"] == reference
    assert "input_reference" not in runner.payload("text-to-image")


def test_png_probe_performs_full_decode(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable")
    raw = subprocess.run(  # noqa: S603 - fixed local synthetic codec fixture only
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=orange:s=256x256",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    path = tmp_path / "synthetic.png"
    path.write_bytes(raw)
    measured = runner.png_probe(path)
    assert measured["width"] == measured["height"] == 256
    assert measured["sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(measured["decoded_rgb_sha256"]) == 64
