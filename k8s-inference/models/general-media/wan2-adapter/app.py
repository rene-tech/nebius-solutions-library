from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
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


def _verified_mp4(
    raw: bytes, request: GenerateRequest
) -> tuple[bytes, dict[str, object]]:
    if not raw or len(raw) > MAX_VIDEO_BYTES:
        raise HTTPException(
            status_code=502, detail="NIM returned an empty or oversized video"
        )
    with tempfile.TemporaryDirectory(prefix="fs2-wan2-output-") as directory:
        path = Path(directory) / "output.mp4"
        path.write_bytes(raw)
        command = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration:stream=codec_type,codec_name,width,height,avg_frame_rate,nb_frames",
            "-of",
            "json",
            str(path),
        ]
        try:
            completed = subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=30
            )
            probe = json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=502, detail="NIM output is not a decodable MP4"
            ) from error
    streams = [
        stream
        for stream in probe.get("streams", [])
        if stream.get("codec_type") == "video"
    ]
    if len(streams) != 1 or "mp4" not in str(
        probe.get("format", {}).get("format_name", "")
    ):
        raise HTTPException(
            status_code=502,
            detail="NIM output does not contain exactly one MP4 video stream",
        )
    video = streams[0]
    expected_width, expected_height = (int(value) for value in request.size.split("x"))
    if (video.get("width"), video.get("height")) != (expected_width, expected_height):
        raise HTTPException(
            status_code=502, detail="NIM output dimensions differ from the request"
        )
    try:
        duration = float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=502, detail="NIM output has invalid duration metadata"
        ) from error
    if not 0 < duration <= request.seconds + 0.25:
        raise HTTPException(
            status_code=502, detail="NIM output duration is outside the request bound"
        )
    return raw, {
        "duration_seconds": duration,
        "width": expected_width,
        "height": expected_height,
        "codec": video.get("codec_name"),
        "frames": video.get("nb_frames"),
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
