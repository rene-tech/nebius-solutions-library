"""Generated scientific camera data may never be silently resized or retimed."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import av
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime/src"))

from fs2_lerobot_augmentation.contracts import AugmentationRequest  # noqa: E402
from fs2_lerobot_augmentation.cosmos import (  # noqa: E402
    CosmosClient,
    CosmosError,
    _validate_transfer_video_alignment,
    conditioning_scope,
)


def make_video(path: Path, *, width=320, height=256, frames=9, fps=25) -> None:
    with av.open(str(path), "w") as sink:
        stream = sink.add_stream("libx264", rate=fps)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        for i in range(frames):
            pixels = np.full((height, width, 3), i * 20, dtype=np.uint8)
            for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")):
                sink.mux(packet)
        for packet in stream.encode():
            sink.mux(packet)


def test_correct_video_is_byte_identical_after_validation(tmp_path):
    path = tmp_path / "output.mp4"
    make_video(path)
    before = path.read_bytes()
    assert _validate_transfer_video_alignment(path, width=320, height=256, frames=9, fps=25) == path
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("wrong", [
    {"width": 640, "height": 480}, {"width": 256}, {"height": 320},
    {"fps": 24}, {"frames": 8}, {"frames": 10},
])
def test_mismatched_video_fails_without_mutating_or_resizing(tmp_path, wrong):
    path = tmp_path / "output.mp4"
    make_video(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(CosmosError) as caught:
        _validate_transfer_video_alignment(path, **({"width": 320, "height": 256, "frames": 9, "fps": 25} | wrong))
    assert caught.value.code == "COSMOS_MEDIA_ALIGNMENT_INVALID" and not caught.value.retryable
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert list(tmp_path.iterdir()) == [path]


def test_transfer_payload_preserves_source_geometry_and_timing():
    request = json.loads((ROOT / "fixtures/fixture-request.json").read_text())
    request["augmentation"]["mode"] = "transfer"
    request["augmentation"]["conditioning"]["controls"] = ["edge"]
    parsed = AugmentationRequest.parse(request)
    payload = CosmosClient._video_payload(
        reference={"artifact_id": "input"}, prompt="cool lighting", negative_prompt="",
        width=640, height=480, frames=64, fps=25, seed=1, augmentation=parsed.augmentation,
    )
    assert payload["size"] == "640x480"
    assert payload["fps"] == 25 and payload["num_frames"] == 64
    assert payload["mode"] == "transfer-video"
    assert payload["controls"] == [{"control_type": "edge", "control_weight": 1.0}]
    scope = conditioning_scope(parsed.augmentation)
    assert scope["reference_usage"] == "full_sequence_spatial_controls"
    assert scope["controls"] == ["edge"]
    assert scope["physical_action_alignment_verified"] is False


@pytest.mark.parametrize(("indexes", "pixels"), [([0], 1), ([0, 1], 5), ([0, 1, 2], 9)])
def test_prefix_conditioning_scope_never_claims_recorded_action_alignment(indexes, pixels):
    request = json.loads((ROOT / "fixtures/fixture-request.json").read_text())
    request["augmentation"]["conditioning"]["frame_indexes"] = indexes
    scope = conditioning_scope(AugmentationRequest.parse(request).augmentation)
    assert scope["reference_usage"] == "selected_prefix_or_suffix_latent_frames"
    assert scope["reference_pixel_frame_budget"] == pixels
    assert scope["physical_action_alignment_verified"] is False
    assert scope["policy_training_validity_verified"] is False
