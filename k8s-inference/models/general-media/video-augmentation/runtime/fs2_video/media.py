"""Bounded all-frame preflight. Never silently crop, trim, resize or retime."""

from __future__ import annotations

import hashlib
import subprocess
from fractions import Fraction
from pathlib import Path

from . import MAX_VIDEO_BYTES


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def inspect_video(path: Path) -> dict:
    import av

    if (
        path.is_symlink()
        or not path.is_file()
        or not 16 <= path.stat().st_size <= MAX_VIDEO_BYTES
    ):
        raise ValueError("video must be a regular MP4 of at most 128 MiB")
    with path.open("rb") as stream:
        if b"ftyp" not in stream.read(32):
            raise ValueError("input must be an MP4 container")
    with av.open(str(path)) as source:
        if len(source.streams.video) != 1:
            raise ValueError("exactly one video stream is supported")
        stream = source.streams.video[0]
        width, height = stream.width, stream.height
        if (width, height) not in {(640, 480), (1280, 720)}:
            raise ValueError(
                "supported source sizes are 640x480 and 1280x720; prepare a separate explicitly resized input"
            )
        rate = stream.average_rate
        if rate is None or rate.denominator != 1 or not 1 <= rate <= 30:
            raise ValueError("video requires constant integer FPS between 1 and 30")
        frames = 0
        origin = None
        for frame in source.decode(video=0):
            frames += 1
            if frames > 400:
                raise ValueError(
                    "video exceeds 400 frames; long-video chunk/stitch is not supported"
                )
            if (
                (frame.width, frame.height) != (width, height)
                or frame.pts is None
                or frame.time_base is None
            ):
                raise ValueError("video has changing dimensions or missing timestamps")
            timestamp = frame.pts * frame.time_base
            if origin is None:
                origin = timestamp
            if (
                abs(timestamp - origin - Fraction(frames - 1, int(rate)))
                > frame.time_base
            ):
                raise ValueError(
                    "variable-rate or discontinuous timestamps are unsupported"
                )
        if frames < 16:
            raise ValueError("video needs at least 16 decoded frames")
        audio = [{"codec": s.codec_context.name} for s in source.streams.audio]
        if len(audio) > 1 or any(
            x["codec"] not in {"aac", "mp3", "alac"} for x in audio
        ):
            raise ValueError(
                "at most one MP4-compatible AAC, MP3 or ALAC audio stream is supported"
            )
    return {
        "width": width,
        "height": height,
        "frames": frames,
        "fps": int(rate),
        "duration_seconds": frames / int(rate),
        "audio": audio,
        "size_bytes": path.stat().st_size,
        "sha256": digest(path),
    }


def validate_alignment(source: dict, generated: dict) -> None:
    if any(source[k] != generated[k] for k in ("width", "height", "frames", "fps")):
        raise ValueError(
            "generated clip does not preserve source geometry, frame count and FPS"
        )


def finalize_audio(
    source: Path, generated: Path, target: Path, *, preserve: bool
) -> None:
    command = ["ffmpeg", "-v", "error", "-nostdin", "-n", "-i", str(generated)]
    if preserve:
        command += ["-i", str(source), "-map", "0:v:0", "-map", "1:a:0?"]
    else:
        command += ["-map", "0:v:0", "-an"]
    command += ["-c", "copy", "-movflags", "+faststart", str(target)]
    subprocess.run(command, check=True, capture_output=True, timeout=120)
