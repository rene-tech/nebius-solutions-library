#!/usr/bin/env python3
"""Deterministic synthetic media and request fixtures for Wan2.2 and SAM 2."""

from __future__ import annotations

import base64
import json
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path
from typing import Callable


def png(
    width: int, height: int, pixel: Callable[[int, int], tuple[int, int, int]]
) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(pixel(x, y))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + chunk(b"IEND", b"")
    )


def microscopy_png(*, shifted: bool = False) -> bytes:
    width, height = 320, 240
    offset = 26 if shifted else 0

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        base = (12 + (x * 9 + y * 5) % 18, 20 + (x * 3 + y * 7) % 22, 36 + (x + y) % 18)
        circles = (
            (95 + offset, 86, 37, (54, 227, 196)),
            (214 - offset, 145, 49, (232, 113, 255)),
            (156, 176 - offset // 2, 25, (255, 204, 72)),
        )
        for cx, cy, radius, color in circles:
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius**2:
                return color
        return base

    return png(width, height, pixel)


def tracking_mp4() -> bytes:
    with tempfile.TemporaryDirectory(prefix="fs2-sam2-fixture-") as directory:
        root = Path(directory)
        for index in range(24):
            cx = 55 + index * 8

            def pixel(x: int, y: int, center: int = cx) -> tuple[int, int, int]:
                if (x - center) ** 2 + (y - 118) ** 2 <= 30**2:
                    return (42, 226, 187)
                if (x - 250) ** 2 + (y - 70) ** 2 <= 20**2:
                    return (238, 105, 244)
                return (15 + (x + y) % 15, 22, 39 + (x * 3 + y) % 20)

            (root / f"{index + 1:06d}.png").write_bytes(png(320, 240, pixel))
        target = root / "tracking.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-framerate",
                "12",
                "-i",
                str(root / "%06d.png"),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryslow",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(target),
            ],
            check=True,
            timeout=120,
        )
        return target.read_bytes()


def sam_requests() -> list[tuple[str, dict[str, object]]]:
    image = base64.b64encode(microscopy_png()).decode()
    video = base64.b64encode(tracking_mp4()).decode()
    return [
        (
            "sam2-prompted-image-0",
            {
                "mode": "prompted-image",
                "media_base64": image,
                "media_type": "image/png",
                "points": [{"x": 95, "y": 86, "label": 1, "object_id": 1}],
            },
        ),
        (
            "sam2-prompted-video-1",
            {
                "mode": "prompted-video",
                "media_base64": video,
                "media_type": "video/mp4",
                "points": [{"x": 55, "y": 118, "label": 1, "object_id": 1}],
                "prompt_frame": 0,
            },
        ),
    ]


def sam_automatic_request() -> dict[str, object]:
    return {
        "mode": "automatic-image",
        "media_base64": base64.b64encode(microscopy_png(shifted=True)).decode(),
        "media_type": "image/png",
        "max_masks": 16,
    }


def wan_t2v_requests() -> list[tuple[str, dict[str, object]]]:
    return [
        (
            "wan2-protein-ribbon-0",
            {
                "prompt": "A colorful protein ribbon rotates slowly in a dark clean scientific visualization, smooth camera orbit, cyan and lime highlights, no text",
                "size": "832x480",
                "seconds": 4,
                "seed": 1701,
                "steps": 50,
                "cfg_scale": 5,
            },
        ),
        (
            "wan2-molecular-docking-1",
            {
                "prompt": "A cinematic molecular docking visualization, glowing ligand enters a translucent protein pocket, scientific 3D render, magenta cyan and lime accents, no text",
                "size": "832x480",
                "seconds": 4,
                "seed": 1702,
                "steps": 50,
                "cfg_scale": 5,
            },
        ),
    ]


def wan_i2v_requests() -> list[tuple[str, dict[str, object]]]:
    images = (microscopy_png(), microscopy_png(shifted=True))
    prompts = (
        "Animate this scientific image with a slow cinematic push in, subtle parallax and softly pulsing cell boundaries, preserve composition, no text",
        "Animate this microscopy visualization with gentle depth and a slow rightward camera orbit, preserve all structures, no text",
    )
    return [
        (
            f"wan2-scientific-image-{index}",
            {
                "prompt": prompts[index],
                "input_reference": "data:image/png;base64,"
                + base64.b64encode(images[index]).decode(),
                "size": "832x480",
                "seconds": 4,
                "seed": 2701 + index,
                "steps": 50,
                "cfg_scale": 5,
            },
        )
        for index in range(2)
    ]


def compact_sha256(value: dict[str, object]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


if __name__ == "__main__":
    rows = {
        "sam2-1-hiera-large": [
            (name, compact_sha256(value)) for name, value in sam_requests()
        ],
        "wan2-2-t2v-nim": [
            (name, compact_sha256(value)) for name, value in wan_t2v_requests()
        ],
        "wan2-2-i2v-nim": [
            (name, compact_sha256(value)) for name, value in wan_i2v_requests()
        ],
    }
    print(json.dumps(rows, sort_keys=True, indent=2))
