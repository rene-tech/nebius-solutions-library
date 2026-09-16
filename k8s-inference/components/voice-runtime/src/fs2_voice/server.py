"""Bounded single-GPU HTTP/WebSocket service, with cancellation and drain."""

import asyncio
import base64
import json
import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from uuid import uuid4

import anyio
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import ValidationError

from .contracts import MAGPIE, StreamControl, StreamStart, SynthesisRequest, capabilities
from .runtime import Runtime

LOG = logging.getLogger(__name__)


def create_app(runtime: Runtime, *, load: bool = True):
    registry = CollectorRegistry()
    requests = Counter("fs2_voice_requests_total", "Requests by terminal status", ["status"], registry=registry)
    occupied = Gauge("fs2_voice_occupied", "Worker occupied including cancelled GPU work", registry=registry)
    ready = Gauge("fs2_voice_ready", "Model loaded and not draining", registry=registry)
    audio = Counter(
        "fs2_voice_audio_seconds_total", "Accepted input/generated output audio", ["direction"], registry=registry
    )
    duration = Histogram("fs2_voice_request_seconds", "Total accepted request time", registry=registry)
    first = Histogram("fs2_voice_first_output_seconds", "Accepted request to first usable output", registry=registry)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-gpu")
    state = {"loaded": False, "busy": False, "draining": False}
    backend = os.getenv("HOSTNAME", "voice-runtime")

    async def call(fn, *args):
        # Shield work: cancellation cannot make a still-running CUDA step appear
        # idle. The caller must wait for it before releasing admission.
        future = asyncio.get_running_loop().run_in_executor(executor, fn, *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await future
            raise

    def acquire():
        if not state["loaded"] or state["draining"]:
            requests.labels("unavailable").inc()
            raise HTTPException(503, {"code": "worker_draining", "retryable": True})
        if state["busy"]:
            requests.labels("overloaded").inc()
            raise HTTPException(429, {"code": "worker_busy", "retryable": True}, headers={"Retry-After": "1"})
        state["busy"] = True
        occupied.set(1)

    def release():
        state["busy"] = False
        occupied.set(0)

    @asynccontextmanager
    async def lifespan(app):
        if load:
            await call(runtime.load)
        state["loaded"] = True
        ready.set(1)
        yield
        state["draining"] = True
        ready.set(0)
        executor.shutdown(wait=True, cancel_futures=False)

    app = FastAPI(title="Scientific AI voice runtime", lifespan=lifespan)
    app.state.voice = state

    @app.get("/healthz")
    async def health():
        return {"status": "alive"}

    @app.get("/readyz")
    async def readiness():
        status = 200 if state["loaded"] and not state["draining"] else 503
        return JSONResponse(
            {"ready": status == 200, "busy": state["busy"], "model": runtime.model_id, "timings": runtime.timings},
            status_code=status,
        )

    @app.get("/v1/voice/capabilities")
    async def contract():
        return capabilities(runtime.model_id)

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(registry), media_type="text/plain; version=0.0.4")

    @app.post("/drain")
    async def drain():
        state["draining"] = True
        ready.set(0)
        return {"draining": True, "active": int(state["busy"])}

    @app.post("/v1/voice/synthesize")
    async def synthesize(payload: SynthesisRequest, request: Request):
        if runtime.model_id != MAGPIE:
            raise HTTPException(422, {"code": "wrong_model"})
        acquire()
        started = time.monotonic()
        request_id = str(uuid4())
        cancelled = threading.Event()
        chunks = queue.Queue(maxsize=2)

        def put(item):
            while not cancelled.is_set():
                try:
                    chunks.put(item, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def produce():
            try:
                for chunk in runtime.synthesize(payload, cancelled):
                    put(("chunk", chunk))
                    if cancelled.is_set():
                        return
                put(("done", None))
            except Exception:
                LOG.exception("voice synthesis failed request_id=%s", request_id)
                put(("error", None))

        task = asyncio.get_running_loop().run_in_executor(executor, produce)

        async def stream():
            samples, sequence, status = 0, 0, "cancelled"
            try:
                yield (
                    json.dumps(
                        {
                            "type": "audio.start",
                            "request_id": request_id,
                            "model": MAGPIE,
                            "voice": payload.voice,
                            "language": payload.language,
                            "sample_rate_hz": 22050,
                            "channels": 1,
                            "encoding": "pcm_s16le",
                            "streaming_mode": "phrase_incremental",
                        }
                    )
                    + "\n"
                )
                while True:
                    if await request.is_disconnected():
                        return
                    if time.monotonic() - started > 180:
                        yield json.dumps({"type": "error", "code": "generation_timeout", "retryable": False}) + "\n"
                        status = "timeout"
                        return
                    try:
                        kind, chunk = chunks.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.005)
                        continue
                    if kind == "chunk":
                        if sequence == 0:
                            first.observe(time.monotonic() - started)
                        samples += len(chunk) // 2
                        audio.labels("output").inc(len(chunk) / 44100)
                        yield (
                            json.dumps(
                                {
                                    "type": "audio.chunk",
                                    "sequence": sequence,
                                    "sample_rate_hz": 22050,
                                    "audio_base64": base64.b64encode(chunk).decode(),
                                }
                            )
                            + "\n"
                        )
                        sequence += 1
                    elif kind == "done":
                        status = "completed"
                        yield (
                            json.dumps(
                                {
                                    "type": "audio.done",
                                    "samples": samples,
                                    "chunks": sequence,
                                    "duration_seconds": samples / 22050,
                                    "processing_seconds": time.monotonic() - started,
                                }
                            )
                            + "\n"
                        )
                        return
                    else:
                        status = "failed"
                        yield json.dumps({"type": "error", "code": "inference_failed", "retryable": False}) + "\n"
                        return
            finally:
                cancelled.set()
                with anyio.CancelScope(shield=True):
                    try:
                        await asyncio.shield(task)
                    finally:
                        duration.observe(time.monotonic() - started)
                        requests.labels(status).inc()
                        release()

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={
                "X-Backend-Id": backend,
                "X-Request-Id": request_id,
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.websocket("/v1/voice/stream")
    async def stream_audio(ws: WebSocket):
        await ws.accept()
        admitted, started, status = False, time.monotonic(), "failed"
        first_output = False
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
            if len(raw) > 4096:
                raise ValueError("control_too_large")
            start = StreamStart.model_validate_json(raw)
            if start.model != runtime.model_id:
                raise ValueError("wrong_model")
            acquire()
            admitted = True
            await call(runtime.reset)
            await ws.send_json(
                {
                    "type": "session.ready",
                    "session_id": str(uuid4()),
                    "backend_id": backend,
                    **capabilities(runtime.model_id),
                }
            )
            while True:
                remaining = 1800 - (time.monotonic() - started)
                if remaining <= 0:
                    raise ValueError("session_timeout")
                message = await asyncio.wait_for(ws.receive(), min(30, remaining))
                if message["type"] == "websocket.disconnect":
                    status = "cancelled"
                    return
                if message.get("bytes") is not None:
                    pcm = message["bytes"]
                    events = await call(runtime.feed, pcm)
                    audio.labels("input").inc(len(pcm) / 32000)
                else:
                    raw = message.get("text", "")
                    if len(raw) > 4096:
                        raise ValueError("control_too_large")
                    control = StreamControl.model_validate_json(raw)
                    if control.type == "session.cancel":
                        status = "cancelled"
                        await ws.send_json({"type": "session.cancelled"})
                        return
                    if control.type == "session.reset":
                        await call(runtime.reset)
                        await ws.send_json({"type": "session.reset", "resume_supported": False})
                        continue
                    events = await call(runtime.finish)
                    for event in events:
                        await ws.send_json(event)
                    await ws.send_json(
                        {
                            "type": "session.done",
                            "audio_seconds": runtime.samples / 16000,
                            "processing_seconds": time.monotonic() - started,
                        }
                    )
                    status = "completed"
                    return
                for event in events:
                    if not first_output and event["type"] in ("transcript.partial", "speaker.activity"):
                        first.observe(time.monotonic() - started)
                        first_output = True
                    await ws.send_json(event)
        except WebSocketDisconnect:
            status = "cancelled"
        except (TimeoutError, ValidationError, ValueError, HTTPException) as exc:
            code = (
                "invalid_request"
                if isinstance(exc, ValidationError)
                else "idle_timeout"
                if isinstance(exc, asyncio.TimeoutError)
                else str(exc)
            )
            if isinstance(exc, HTTPException):
                code = exc.detail["code"]
            await ws.send_json({"type": "error", "code": code, "retryable": not admitted})
        except Exception:
            LOG.exception("voice stream failed")
            try:
                await ws.send_json({"type": "error", "code": "inference_failed", "retryable": False})
            except Exception:
                pass
        finally:
            if admitted:
                with anyio.CancelScope(shield=True):
                    try:
                        await call(runtime.reset)
                    finally:
                        duration.observe(time.monotonic() - started)
                        requests.labels(status).inc()
                        release()
            try:
                await ws.close()
            except RuntimeError:
                pass

    return app


def main():
    import uvicorn

    runtime = Runtime(os.environ["FS2_VOICE_MODEL"], os.getenv("FS2_VOICE_CHECKPOINT_DIR", "/opt/fs2-voice/weights"))
    uvicorn.run(
        create_app(runtime),
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        ws_max_size=32768,
        ws_max_queue=4,
        timeout_graceful_shutdown=180,
    )


if __name__ == "__main__":
    main()
