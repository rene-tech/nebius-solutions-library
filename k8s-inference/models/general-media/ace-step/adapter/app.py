from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator


MODEL_ID = "ace-step-1-5"
MODEL_REVISION = "ACE-Step/Ace-Step1.5@19671f406d603126926c1b7e2adc169acbcade22"
UPSTREAM = os.environ.get("ACESTEP_UPSTREAM_URL", "http://127.0.0.1:8001").rstrip("/")
POLL_SECONDS = float(os.environ.get("ACESTEP_POLL_SECONDS", "1"))
GENERATION_TIMEOUT_SECONDS = float(os.environ.get("ACESTEP_GENERATION_TIMEOUT_SECONDS", "900"))
MAX_AUDIO_BYTES = 128 * 1024 * 1024

client: httpx.AsyncClient | None = None
generation_lock = asyncio.Lock()


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: Annotated[str, Field(min_length=1, max_length=4096)]
    lyrics: Annotated[str, Field(max_length=12_000)] = "[Instrumental]"
    duration_seconds: Annotated[float, Field(ge=10, le=60)] = 20
    thinking: bool = True
    seed: Annotated[int, Field(ge=0, lt=2**32)] = 0
    bpm: Annotated[int | None, Field(ge=30, le=300)] = None
    key_scale: Annotated[str, Field(max_length=32)] = ""
    time_signature: Literal["", "2", "3", "4", "6", "2/4", "3/4", "4/4", "6/8"] = ""
    vocal_language: Annotated[str, Field(pattern=r"^[A-Za-z-]{2,16}$")] = "en"

    @model_validator(mode="after")
    def normalize_instrumental(self) -> "GenerateRequest":
        if not self.lyrics.strip():
            self.lyrics = "[Instrumental]"
        return self


def _upstream_error(message: str, status_code: int = 502) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def _unwrap(payload: object, field: str) -> object:
    if not isinstance(payload, dict) or payload.get("code") != 200 or payload.get("error") not in (None, ""):
        raise _upstream_error(f"ACE-Step returned an invalid {field} response")
    return payload.get("data")


def _validate_wav(data: bytes) -> None:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise _upstream_error("ACE-Step returned an invalid WAV")
    if int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise _upstream_error("ACE-Step returned a truncated WAV")


async def _ready() -> bool:
    if client is None:
        return False
    try:
        response = await client.get(f"{UPSTREAM}/health", timeout=5)
        data = _unwrap(response.json(), "health")
        return bool(
            response.status_code == 200
            and isinstance(data, dict)
            and data.get("status") == "ok"
            and data.get("models_initialized") is True
            and data.get("llm_initialized") is True
        )
    except (httpx.HTTPError, ValueError, TypeError, HTTPException):
        return False


@asynccontextmanager
async def lifespan(_: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
    try:
        yield
    finally:
        await client.aclose()
        client = None


app = FastAPI(title="FS2 ACE-Step 1.5 adapter", version="1.0", lifespan=lifespan)


@app.get("/v1/health/live")
async def live() -> dict[str, str]:
    return {"status": "live", "model": MODEL_ID}


@app.get("/v1/health/ready")
async def ready() -> Response:
    if not await _ready():
        return Response(status_code=503)
    return Response(content='{"status":"ready"}', media_type="application/json")


@app.post("/generate")
async def generate(request: GenerateRequest) -> Response:
    if client is None:
        raise _upstream_error("ACE-Step adapter is not initialized", 503)
    if generation_lock.locked():
        raise _upstream_error("ACE-Step worker is busy", 429)

    payload = {
        "prompt": request.prompt,
        "lyrics": request.lyrics,
        "audio_duration": request.duration_seconds,
        "thinking": request.thinking,
        "seed": request.seed,
        "use_random_seed": False,
        "bpm": request.bpm,
        "key_scale": request.key_scale,
        "time_signature": request.time_signature,
        "vocal_language": request.vocal_language,
        "inference_steps": 8,
        "guidance_scale": 7.0,
        "batch_size": 1,
        "audio_format": "wav",
        "model": "acestep-v15-turbo",
        "lm_model_path": "acestep-5Hz-lm-4B",
        "lm_backend": "pt",
        "use_cot_caption": True,
        "use_cot_language": True,
    }

    async with generation_lock:
        try:
            submitted = await client.post(f"{UPSTREAM}/release_task", json=payload, timeout=30)
            if submitted.status_code != 200:
                raise _upstream_error("ACE-Step rejected the generation request")
            submission = _unwrap(submitted.json(), "submission")
            if not isinstance(submission, dict) or not isinstance(submission.get("task_id"), str):
                raise _upstream_error("ACE-Step returned an invalid task identifier")
            task_id = submission["task_id"]

            deadline = asyncio.get_running_loop().time() + GENERATION_TIMEOUT_SECONDS
            audio_path: str | None = None
            while asyncio.get_running_loop().time() < deadline:
                result_response = await client.post(
                    f"{UPSTREAM}/query_result", json={"task_id_list": [task_id]}, timeout=30
                )
                if result_response.status_code != 200:
                    raise _upstream_error("ACE-Step result polling failed")
                results = _unwrap(result_response.json(), "result")
                if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
                    raise _upstream_error("ACE-Step returned an invalid result record")
                record = results[0]
                status = record.get("status")
                if status == 2:
                    raise _upstream_error("ACE-Step generation failed")
                if status == 1:
                    entries = json.loads(record.get("result", "[]"))
                    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
                        raise _upstream_error("ACE-Step returned an empty result")
                    candidate = entries[0].get("file")
                    if not isinstance(candidate, str) or not candidate.startswith("/v1/audio?"):
                        raise _upstream_error("ACE-Step returned an invalid audio reference")
                    audio_path = candidate
                    break
                await asyncio.sleep(POLL_SECONDS)
            if audio_path is None:
                raise _upstream_error("ACE-Step generation timed out", 504)

            audio_response = await client.get(
                urljoin(f"{UPSTREAM}/", audio_path.lstrip("/")),
                timeout=httpx.Timeout(120, connect=10),
            )
            if audio_response.status_code != 200 or len(audio_response.content) > MAX_AUDIO_BYTES:
                raise _upstream_error("ACE-Step audio download failed")
            _validate_wav(audio_response.content)
            return Response(
                content=audio_response.content,
                media_type="audio/wav",
                headers={
                    "X-FS2-Model-ID": MODEL_ID,
                    "X-FS2-Model-Revision": MODEL_REVISION,
                },
            )
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError):
            raise _upstream_error("ACE-Step runtime communication failed") from None
