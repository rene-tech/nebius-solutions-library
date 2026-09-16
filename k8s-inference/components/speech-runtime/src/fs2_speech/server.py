"""Private persistent ASR worker. Customer authentication/admission lives in the gateway."""

import asyncio
import json
import logging
import os
import socket
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from .audio import AudioInputError, DownloadAudio, download_audio, transcribe_file
from .contracts import MODELS, RuntimeProfile, SpeechOptions, StrictContract
from .nemo_runtime import NeMoRuntime
from .stream import run_stream

LOG = logging.getLogger(__name__)


class FileRequest(StrictContract):
    audio: DownloadAudio
    options: SpeechOptions


def create_app(runtime, profile: RuntimeProfile, *, allowed_hosts: frozenset[str], load: bool = True) -> FastAPI:
    """One immutable worker/profile with explicit busy and drain behavior.

    This service must remain cluster-private. The public gateway authorizes
    model/tenant access, issues artifact handles and owns durable Operations.
    """
    registry = CollectorRegistry()
    active = Gauge("fs2_speech_active_sessions", "Admitted sessions on this worker", registry=registry)
    total = Counter("fs2_speech_sessions_total", "Worker session outcomes", ["mode", "outcome"], registry=registry)
    audio = Counter("fs2_speech_audio_seconds_total", "Successfully transcribed audio seconds", registry=registry)
    duration = Histogram("fs2_speech_processing_seconds", "File processing duration", registry=registry)
    state = {"ready": not load, "busy": False, "draining": False, "error": None}

    @asynccontextmanager
    async def lifespan(app):
        async def initialize():
            try:
                await asyncio.to_thread(runtime.load)
                # Warm a public synthetic phrase, not a customer session. Keep
                # readiness false through lazy kernel/decoder initialization.
                with tempfile.TemporaryDirectory(prefix="fs2-speech-warm-") as directory:
                    path = Path(directory) / "warm.wav"
                    process = await asyncio.create_subprocess_exec(
                        "espeak-ng", "-w", str(path), "Speech recognition is ready.",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    if await process.wait() != 0:
                        raise RuntimeError("warmup_fixture_failed")
                    options = SpeechOptions(model=profile.model, chunk_size_ms=profile.chunk_size_ms,
                                            strip_language_tags=profile.strip_language_tags)
                    result = await transcribe_file(runtime, path, options)
                    if not result["text"].strip():
                        raise RuntimeError("warmup_transcription_empty")
                state["ready"] = True
                LOG.info("speech runtime ready model=%s timings=%s", profile.model, runtime.timings)
            except Exception:
                state["error"] = "initialization_failed"
                LOG.exception("speech runtime initialization failed")
        initialization = asyncio.create_task(initialize()) if load else None
        yield
        state["draining"] = True
        if initialization and not initialization.done():
            # Initial model load uses a background thread: never exit midway
            # through a load while pretending a clean snapshot can be taken.
            await initialization

    app = FastAPI(lifespan=lifespan)
    app.state.runtime_state = state

    def acquire():
        if not state["ready"] or state["draining"]:
            raise HTTPException(503, "runtime_not_ready", headers={"retry-after": "2"})
        if state["busy"]:
            raise HTTPException(429, "runtime_busy", headers={"retry-after": "1"})
        state["busy"] = True
        active.set(1)

    def release():
        state["busy"] = False
        active.set(0)

    @app.get("/healthz")
    async def health():
        return {"status": "alive", "backend_id": socket.gethostname()}

    @app.get("/readyz")
    async def ready():
        healthy = state["ready"] and not state["draining"]
        return JSONResponse({"ready": healthy, "active_sessions": int(state["busy"]),
                             "model": profile.model, "profile": profile.model_dump(),
                             "backend_id": socket.gethostname()},
                            status_code=200 if healthy else 503)

    @app.post("/drain")
    async def drain():
        state["draining"] = True
        return {"draining": True, "active_sessions": int(state["busy"])}

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(registry), media_type="text/plain; version=0.0.4")

    @app.post("/generate")
    async def generate(payload: FileRequest, request: Request):
        try:
            profile.require_match(payload.options)
        except ValueError:
            raise HTTPException(422, "runtime_profile_mismatch") from None
        acquire()
        try:
            with tempfile.TemporaryDirectory(prefix="fs2-speech-file-") as directory:
                path = Path(directory) / "audio"
                await download_audio(payload.audio, path, allowed_hosts)
                inference = asyncio.create_task(transcribe_file(runtime, path, payload.options))
                try:
                    while not inference.done():
                        if await request.is_disconnected():
                            inference.cancel()
                            raise asyncio.CancelledError
                        await asyncio.wait({inference}, timeout=0.25)
                    result = await inference
                finally:
                    if not inference.done():
                        inference.cancel()
                    await asyncio.gather(inference, return_exceptions=True)
            audio.inc(result["audio_seconds"])
            duration.observe(result["processing_seconds"])
            total.labels("file", "completed").inc()
            return JSONResponse({**result, "model_revision": MODELS[profile.model].revision},
                                headers={"x-backend-id": socket.gethostname()})
        except AudioInputError as exc:
            total.labels("file", "failed").inc()
            raise HTTPException(422, str(exc)) from None
        except asyncio.CancelledError:
            total.labels("file", "cancelled").inc()
            raise
        finally:
            release()

    @app.websocket("/v1/audio/stream")
    async def stream(websocket: WebSocket):
        try:
            acquire()
        except HTTPException as exc:
            await websocket.accept()
            await websocket.send_json({"type": "session.error", "code": exc.detail, "retryable": True})
            await websocket.close(code=1013)
            return
        await websocket.accept()
        outcome = "disconnected"

        async def messages():
            while True:
                event = await websocket.receive()
                if event["type"] == "websocket.disconnect":
                    return
                if event.get("bytes") is not None:
                    yield event["bytes"]
                elif event.get("text") is not None:
                    yield event["text"]

        async def send(event):
            nonlocal outcome
            if event["type"] == "session.completed":
                audio.inc(event["audio_seconds"])
                outcome = "completed"
            elif event["type"] in {"session.error", "session.cancelled"}:
                outcome = "failed" if event["type"] == "session.error" else "cancelled"
            await websocket.send_json(event)

        try:
            await run_stream(runtime, messages(), send, max_session_seconds=7200)
            await websocket.close()
        except (WebSocketDisconnect, RuntimeError):
            pass  # A disconnect cannot be acknowledged on the closed socket.
        finally:
            total.labels("live", outcome).inc()
            release()

    return app


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # Presigned artifact URLs contain credentials.
    profile = RuntimeProfile.model_validate(json.loads(os.environ["FS2_SPEECH_PROFILE_JSON"]))
    runtime = NeMoRuntime(profile, config_path=Path(
        "/opt/nemo/examples/asr/conf/asr_streaming_inference/cache_aware_rnnt.yaml"))
    hosts = frozenset(filter(None, os.environ.get("FS2_SPEECH_ARTIFACT_HOSTS", "").split(",")))
    uvicorn.run(create_app(runtime, profile, allowed_hosts=hosts), host="0.0.0.0", port=8000,
                loop="asyncio", ws_max_size=65536, ws_max_queue=2, timeout_graceful_shutdown=7205)


if __name__ == "__main__":
    main()
