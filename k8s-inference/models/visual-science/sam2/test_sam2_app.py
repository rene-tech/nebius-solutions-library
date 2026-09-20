from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


SPEC = importlib.util.spec_from_file_location("sam2_runtime", Path(__file__).with_name("app.py"))
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def image_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.full((32, 48, 3), 90, dtype=np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeBackend:
    def prompted_image(self, image, request):
        labels = np.zeros(image.shape[:2], dtype=np.uint16)
        labels[8:24, 10:30] = 1
        return labels, [{"object_id": 1, "score": 0.99, "area_px": 320}]

    def automatic_image(self, image, request):
        return self.prompted_image(image, request)


def request(**updates):
    values = {
        "mode": "prompted-image",
        "media_base64": base64.b64encode(image_bytes()).decode(),
        "media_type": "image/png",
        "points": [{"x": 20, "y": 16, "label": 1, "object_id": 1}],
    }
    values.update(updates)
    return module.SegmentRequest(**values)


def test_contract_requires_valid_mode_media_and_prompts():
    with pytest.raises(ValueError, match="require at least"):
        request(points=[])
    with pytest.raises(ValueError, match="requires video"):
        request(mode="prompted-video")
    automatic = request(mode="automatic-image", points=[])
    assert automatic.max_masks == 32


def test_image_result_contains_mask_overlay_and_provenance(monkeypatch):
    monkeypatch.setattr(module, "backend", FakeBackend())
    monkeypatch.setattr(module, "checkpoint_sha256", "a" * 64)
    raw = image_bytes()
    result = module._image_result(raw, request())
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        assert set(archive.namelist()) == {"manifest.json", "mask.png", "overlay.png"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["input_sha256"] == module.hashlib.sha256(raw).hexdigest()
        assert manifest["objects"][0]["area_px"] == 320
        assert Image.open(io.BytesIO(archive.read("mask.png"))).size == (48, 32)


def test_overlay_is_deterministic_and_visible():
    image = np.full((8, 8, 3), 100, dtype=np.uint8)
    labels = np.zeros((8, 8), dtype=np.uint16)
    labels[2:6, 2:6] = 4
    first = module._overlay(image, labels)
    second = module._overlay(image, labels)
    assert np.array_equal(first, second)
    assert not np.array_equal(first[3, 3], image[3, 3])
    assert (first[2, 2] == 255).all()


@pytest.mark.asyncio
async def test_endpoint_externalizes_zip_shaped_bytes(monkeypatch):
    monkeypatch.setattr(module, "backend", FakeBackend())
    monkeypatch.setattr(module, "checkpoint_sha256", "a" * 64)
    response = await module.segment_track(request())
    assert response.media_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert "overlay.png" in archive.namelist()
