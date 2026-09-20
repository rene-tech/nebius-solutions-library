"""Bounded video-to-video adapter for the exact Cosmos Transfer 2.5 NIM.

The platform materializes tenant-owned artifacts as base64 at this boundary.
No URL, local path, alternative endpoint, model or safety override is accepted.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fs2_video.media import inspect_video, validate_alignment
from pydantic import BaseModel, ConfigDict, Field

PROFILE_ID = "e74ebba119c8a196dca12cac66aa1b5323291048a855fe02ceb6b664f334c672"
MODEL_ID = "cosmos-transfer2.5-2b"
MAX_VIDEO_BYTES = 128 * 1024**2
MAX_BASE64_CHARS = ((MAX_VIDEO_BYTES + 2) // 3) * 4
MAX_JSON_BYTES = MAX_BASE64_CHARS + 64 * 1024
NEGATIVE_PROMPT = (
    "game playing with bad crappy graphics, cartoonish frames, old outdated games, "
    "fake lighting, raw basic textures, primitive geometry, pixelated poor CG quality, "
    "subtitles, unrealistic."
)


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    video: Annotated[str, Field(min_length=1, max_length=MAX_BASE64_CHARS)]
    prompt: Annotated[str, Field(min_length=1, max_length=4096)]
    negative_prompt: Annotated[str, Field(max_length=4096)] = NEGATIVE_PROMPT
    seed: Annotated[int, Field(ge=0, le=2147483647)] = 42
    num_steps: Annotated[int, Field(ge=1, le=50)] = 35
    guidance: Annotated[int, Field(ge=0, le=7)] = 7
    control_weight: Annotated[float, Field(gt=0, le=1)] = 1.0
    output_delivery: Literal["artifact"] = "artifact"


class OneBoundedRequest:
    """Reject overlap and oversized JSON before FastAPI allocates/parses it."""

    def __init__(self, app):
        self.app = app
        self.lock = asyncio.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/v1/transfer" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        if self.lock.locked():
            return await JSONResponse({"detail": "runtime_busy"}, status_code=429)(scope, receive, send)
        async with self.lock:
            chunks = bytearray()
            try:
                async with asyncio.timeout(60):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(chunks) + len(chunk) > MAX_JSON_BYTES:
                            return await JSONResponse({"detail": "input_too_large"}, status_code=413)(
                                scope, receive, send
                            )
                        chunks.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await JSONResponse({"detail": "input_timeout"}, status_code=408)(scope, receive, send)
            pending = True

            async def materialized_receive():
                nonlocal pending
                if pending:
                    pending = False
                    return {
                        "type": "http.request",
                        "body": bytes(chunks),
                        "more_body": False,
                    }
                return await receive()

            return await self.app(scope, materialized_receive, send)


@asynccontextmanager
async def lifespan(app):
    upstream = os.environ.get("COSMOS_TRANSFER_NIM_URL", "http://127.0.0.1:8001")
    parsed = urlsplit(upstream)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("NIM must be the operator-configured loopback sidecar")
    app.state.outcome_unknown = False
    async with httpx.AsyncClient(
        base_url=upstream,
        timeout=httpx.Timeout(1800, connect=10),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        app.state.nim = client
        yield


app = FastAPI(title="Scientific AI Cosmos Transfer 2.5", version="1", lifespan=lifespan)
app.add_middleware(OneBoundedRequest)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request, error):
    # Pydantic's default error envelope can echo the entire base64 video.
    return JSONResponse(
        {
            "detail": "invalid_transfer_request",
            "issue_count": min(len(error.errors()), 64),
        },
        status_code=422,
    )


@app.get("/v1/health/live")
def live():
    return {"status": "live", "model": MODEL_ID}


@app.get("/v1/health/ready")
async def ready():
    if app.state.outcome_unknown:
        raise HTTPException(503, "previous_generation_outcome_unknown")
    try:
        response = await app.state.nim.get("/v1/health/ready", timeout=5)
        metadata = await app.state.nim.get("/v1/metadata", timeout=5)
        if response.status_code != 200 or metadata.status_code != 200:
            raise ValueError("not ready")
        identity = metadata.json()
        if identity.get("selectedModelProfileId") != PROFILE_ID or identity.get("version") != "1.1.0":
            raise ValueError("wrong runtime")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "pinned_nim_not_ready") from None
    return {"status": "ready", "model": MODEL_ID, "profile_id": PROFILE_ID}


@app.post("/v1/transfer")
async def transfer(request: TransferRequest):
    await ready()
    try:
        source_bytes = base64.b64decode(request.video, validate=True)
        if not 16 <= len(source_bytes) <= MAX_VIDEO_BYTES:
            raise ValueError("invalid size")
    except (ValueError, binascii.Error):
        raise HTTPException(422, "video_must_be_materialized_mp4_base64") from None
    with TemporaryDirectory(prefix="cosmos-transfer-") as directory:
        source_path = Path(directory) / "source.mp4"
        source_path.write_bytes(source_bytes)
        try:
            source = await asyncio.to_thread(inspect_video, source_path)
        except (ValueError, OSError):
            raise HTTPException(422, "video_violates_size_frame_rate_or_geometry_contract") from None
        body = {
            "video": request.video,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "seed": request.seed,
            "guidance": request.guidance,
            "num_steps": request.num_steps,
            "resolution": str(source["height"]),
            "sigma_max": 90,
            "edge": {"control_weight": request.control_weight},
        }
        payload = bytearray()
        try:
            async with app.state.nim.stream("POST", "/v1/infer", json=body) as response:
                if response.status_code != 200:
                    code = 422 if response.status_code == 422 else 503
                    raise HTTPException(code, "nim_rejected_or_failed_generation")
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > MAX_JSON_BYTES:
                        raise ValueError("oversized output")
                    payload.extend(chunk)
        except asyncio.CancelledError:
            app.state.outcome_unknown = True
            raise
        except (httpx.HTTPError, ValueError):
            # Never admit a new generation after losing track of the previous one.
            app.state.outcome_unknown = True
            raise HTTPException(503, "generation_outcome_unknown_restart_required") from None
        try:
            result = json.loads(payload)
            if (
                not isinstance(result, dict)
                or type(result.get("seed")) is not int
                or result["seed"] != request.seed
                or not isinstance(result.get("b64_video"), str)
            ):
                raise ValueError("invalid result")
            raw = base64.b64decode(result["b64_video"], validate=True)
            if not 16 <= len(raw) <= MAX_VIDEO_BYTES:
                raise ValueError("invalid output size")
            output_path = Path(directory) / "output.mp4"
            output_path.write_bytes(raw)
            generated = await asyncio.to_thread(inspect_video, output_path)
            validate_alignment(source, generated)
        except (ValueError, OSError, binascii.Error, KeyError, TypeError):
            raise HTTPException(502, "generated_video_failed_source_alignment") from None
        return Response(content=raw, media_type="video/mp4", headers={"cache-control": "no-store"})
