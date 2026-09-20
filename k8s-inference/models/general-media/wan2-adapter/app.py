from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import struct
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

MODEL_ID = "wan-ai/wan2.2"
MODEL_REVISION = (
    "nim-1.0.0@sha256:05c1d390af4eec607b654172fa889ae8cef2b2c238e84516514e61e5ba52e63b"
)
VARIANT = os.environ.get("WAN_VARIANT", "t2v")
UPSTREAM = os.environ.get("WAN_UPSTREAM_URL", "http://127.0.0.1:8001").rstrip("/")
MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_IMAGE_DATA_URL_CHARS = 32 * 1024 * 1024

nim_client: httpx.AsyncClient | None = None
generation_lock = asyncio.Lock()


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: Annotated[str, Field(min_length=1, max_length=4096)]
    size: Literal["832x480", "480x832"] = "832x480"
    seconds: Annotated[int, Field(ge=1, le=12)] = 4
    seed: Annotated[int, Field(ge=0, lt=2**32)] = 0
    steps: Annotated[int, Field(ge=1, le=100)] = 50
    cfg_scale: Annotated[float, Field(gt=1, le=20)] = 5.0
    input_reference: Annotated[
        str | None, Field(max_length=MAX_IMAGE_DATA_URL_CHARS)
    ] = None

    @model_validator(mode="after")
    def validate_variant(self) -> "GenerateRequest":
        if VARIANT == "i2v" and self.input_reference is None:
            raise ValueError(
                "input_reference is required by the image-to-video deployment"
            )
        if VARIANT == "t2v" and self.input_reference is not None:
            raise ValueError(
                "input_reference is not accepted by the text-to-video deployment"
            )
        if self.input_reference is not None and not self.input_reference.startswith(
            (
                "data:image/png;base64,",
                "data:image/jpeg;base64,",
                "data:image/jpg;base64,",
            )
        ):
            raise ValueError("input_reference must be a PNG or JPEG data URL")
        return self


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    limit = len(data) if end is None else end
    cursor = start
    while cursor < limit:
        if limit - cursor < 8:
            raise ValueError("truncated MP4 box")
        size = struct.unpack_from(">I", data, cursor)[0]
        kind = data[cursor + 4 : cursor + 8]
        header = 8
        if size == 1:
            if limit - cursor < 16:
                raise ValueError("truncated extended MP4 box")
            size = struct.unpack_from(">Q", data, cursor + 8)[0]
            header = 16
        elif size == 0:
            size = limit - cursor
        if size < header or cursor + size > limit:
            raise ValueError("invalid MP4 box size")
        yield kind, cursor + header, cursor + size
        cursor += size


def _child(data: bytes, start: int, end: int, kind: bytes):
    return next((box for box in _boxes(data, start, end) if box[0] == kind), None)


def _mp4_metadata(raw: bytes) -> dict[str, object]:
    top = list(_boxes(raw))
    if not top or top[0][0] != b"ftyp":
        raise ValueError("missing MP4 file type box")
    moov = next((box for box in top if box[0] == b"moov"), None)
    if moov is None:
        raise ValueError("missing MP4 movie box")
    mvhd = _child(raw, moov[1], moov[2], b"mvhd")
    if mvhd is None or mvhd[2] - mvhd[1] < 20:
        raise ValueError("missing MP4 movie header")
    version = raw[mvhd[1]]
    if version == 0:
        timescale = struct.unpack_from(">I", raw, mvhd[1] + 12)[0]
        duration_units = struct.unpack_from(">I", raw, mvhd[1] + 16)[0]
    elif version == 1 and mvhd[2] - mvhd[1] >= 32:
        timescale = struct.unpack_from(">I", raw, mvhd[1] + 20)[0]
        duration_units = struct.unpack_from(">Q", raw, mvhd[1] + 24)[0]
    else:
        raise ValueError("unsupported MP4 movie header")
    if timescale == 0:
        raise ValueError("invalid MP4 timescale")

    video_tracks: list[dict[str, object]] = []
    for kind, track_start, track_end in _boxes(raw, moov[1], moov[2]):
        if kind != b"trak":
            continue
        tkhd = _child(raw, track_start, track_end, b"tkhd")
        mdia = _child(raw, track_start, track_end, b"mdia")
        if tkhd is None or mdia is None:
            continue
        hdlr = _child(raw, mdia[1], mdia[2], b"hdlr")
        if hdlr is None or hdlr[2] - hdlr[1] < 12:
            continue
        if raw[hdlr[1] + 8 : hdlr[1] + 12] != b"vide":
            continue
        tkhd_version = raw[tkhd[1]]
        dimension_offset = 80 if tkhd_version == 0 else 92
        if tkhd_version not in {0, 1} or tkhd[2] - tkhd[1] < dimension_offset + 8:
            raise ValueError("invalid MP4 track header")
        width_fixed, height_fixed = struct.unpack_from(">II", raw, tkhd[1] + dimension_offset)
        width, height = width_fixed >> 16, height_fixed >> 16
        if width <= 0 or height <= 0:
            raise ValueError("invalid MP4 dimensions")
        video_tracks.append({"width": width, "height": height})
    if len(video_tracks) != 1:
        raise ValueError("MP4 must contain exactly one video track")
    return {
        **video_tracks[0],
        "duration_seconds": duration_units / timescale,
    }


def _verified_mp4(
    raw: bytes, request: GenerateRequest
) -> tuple[bytes, dict[str, object]]:
    if not raw or len(raw) > MAX_VIDEO_BYTES:
        raise HTTPException(
            status_code=502, detail="NIM returned an empty or oversized video"
        )
    try:
        metadata = _mp4_metadata(raw)
    except (ValueError, struct.error) as error:
        raise HTTPException(
            status_code=502, detail="NIM output is not a valid MP4 video"
        ) from error
    expected_width, expected_height = (int(value) for value in request.size.split("x"))
    if (metadata["width"], metadata["height"]) != (
        expected_width,
        expected_height,
    ):
        raise HTTPException(
            status_code=502, detail="NIM output dimensions differ from the request"
        )
    duration = float(metadata["duration_seconds"])
    if not 0 < duration <= request.seconds + 0.25:
        raise HTTPException(
            status_code=502, detail="NIM output duration is outside the request bound"
        )
    return raw, {
        "duration_seconds": duration,
        "width": expected_width,
        "height": expected_height,
    }


def _nim_error(response: httpx.Response) -> HTTPException:
    if response.status_code == 422:
        return HTTPException(
            status_code=422,
            detail="Wan2.2 rejected the request or its safety filter blocked the output",
        )
    if response.status_code == 429:
        return HTTPException(
            status_code=429, detail="Wan2.2 is busy; retry this operation later"
        )
    if response.status_code in {502, 503, 504}:
        return HTTPException(
            status_code=503, detail="Wan2.2 is temporarily unavailable"
        )
    return HTTPException(status_code=502, detail="Wan2.2 generation failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global nim_client
    if VARIANT not in {"t2v", "i2v"}:
        raise RuntimeError("WAN_VARIANT must be t2v or i2v")
    nim_client = httpx.AsyncClient(
        timeout=httpx.Timeout(620, connect=10), trust_env=False
    )
    yield
    await nim_client.aclose()
    nim_client = None


app = FastAPI(
    title=f"Nebius Scientific AI Wan2.2 {VARIANT}",
    version="1.0.0",
    description="Bounded Wan2.2 NIM video generation adapter with MP4 validation.",
    lifespan=lifespan,
)


@app.get("/v1/health/live")
def live() -> dict[str, str]:
    return {"status": "live", "model": MODEL_ID, "variant": VARIANT}


@app.get("/v1/health/ready")
async def ready() -> dict[str, str]:
    if nim_client is None:
        raise HTTPException(status_code=503, detail="adapter is not ready")
    try:
        response = await nim_client.get(f"{UPSTREAM}/v1/health/ready", timeout=3)
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=503, detail="Wan2.2 NIM is not ready"
        ) from error
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="Wan2.2 NIM is not ready")
    return {
        "status": "ready",
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "variant": VARIANT,
    }


@app.post("/v1/generate")
async def generate(request: GenerateRequest) -> Response:
    if nim_client is None:
        raise HTTPException(status_code=503, detail="adapter is not ready")
    payload = request.model_dump(exclude_none=True)
    payload["model"] = MODEL_ID
    async with generation_lock:
        try:
            upstream = await nim_client.post(
                f"{UPSTREAM}/v1/videos/generations", json=payload
            )
        except httpx.TimeoutException as error:
            raise HTTPException(
                status_code=504,
                detail="Wan2.2 generation exceeded the bounded runtime timeout",
            ) from error
        except httpx.HTTPError as error:
            raise HTTPException(
                status_code=503, detail="Wan2.2 transport failed"
            ) from error
    if upstream.status_code != 200:
        raise _nim_error(upstream)
    try:
        document = upstream.json()
        encoded = document["data"]["b64_json"]
        if (
            not isinstance(encoded, str)
            or len(encoded) > ((MAX_VIDEO_BYTES + 2) // 3) * 4 + 4
        ):
            raise ValueError("invalid base64 field")
        raw = base64.b64decode(encoded, validate=True)
    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as error:
        raise HTTPException(
            status_code=502, detail="Wan2.2 returned an invalid response envelope"
        ) from error
    raw, metadata = await asyncio.to_thread(_verified_mp4, raw, request)
    return Response(
        content=raw,
        media_type="video/mp4",
        headers={
            "X-FS2-Model-Revision": MODEL_REVISION,
            "X-FS2-Wan-Variant": VARIANT,
            "X-FS2-Output-Width": str(metadata["width"]),
            "X-FS2-Output-Height": str(metadata["height"]),
            "X-FS2-Output-Duration": str(metadata["duration_seconds"]),
        },
    )
