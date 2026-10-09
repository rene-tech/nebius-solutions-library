#!/usr/bin/env python3
"""Build a deterministic, tiny, non-customer MP4 acceptance fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build(output: Path) -> dict[str, object]:
    if output.exists():
        raise ValueError("output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to build the MP4 fixture")
    subprocess.run(  # noqa: S603 - resolved executable and fixed arguments
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=256x256:rate=8:duration=2",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "8",
            "-movflags",
            "+faststart",
            str(output),
        ],
        check=True,
    )
    return {
        "schema": "fs2-serve.nebius.ai/cosmos3-mp4-fixture/v1",
        "path": output.name,
        "sha256": sha256_file(output),
        "size_bytes": output.stat().st_size,
        "width": 256,
        "height": 256,
        "fps": 8,
        "frames": 16,
        "duration_seconds": 2,
        "content": "synthetic FFmpeg testsrc2; contains no customer data",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    if args.receipt.exists():
        parser.error("--receipt must not already exist")
    value = build(args.output)
    args.receipt.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
