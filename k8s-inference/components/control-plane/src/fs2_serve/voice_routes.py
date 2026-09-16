"""Native voice streaming through ordinary grants, fenced operations and artifacts."""

import asyncio
import base64
import contextlib
import io
import json
import time
import wave
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import uuid4

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .admission import AdmissionService
from .auth import AuthenticationError
from .model_input_contracts import contract_for
from .models import (
    AdmissionRequest,
    ModalityUsage,
    OperationStatus,
    Principal,
    ReportedUsage,
    RuntimeIdentity,
    RuntimeResult,
)
from .registry import Registry
from .runtime import RuntimeProtocolError, RuntimeTransportError
from .store import Store

PARAKEET = "parakeet-realtime-eou-120m-v1"
MAGPIE = "magpie-tts-multilingual-357m"
SORTFORMER = "diar-streaming-sortformer-4spk-v2-1"
VOICE_MODELS = frozenset({PARAKEET, MAGPIE, SORTFORMER})
MAX_WAV_BYTES = 16 * 1024 * 1024


class VoiceSynthesisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(default=MAGPIE, min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4096)
    language: Literal["ar", "de", "en", "es", "fr", "hi", "it", "ja", "ko", "pt", "vi", "zh"] = "en"
    voice: Literal["Aria", "Jason", "John", "Leo", "Sofia"] = "Sofia"
    apply_text_normalization: bool = False

    @field_validator("text")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("text is empty")
        return value


async def relay_synthesis(model, operation, body: bytes, send: Callable[[dict], Awaitable[None]], *, client=None):
    """Persist a bounded complete WAV after streaming its PCM; never replay audio."""
    if model.binding.backend_class != "local-kubernetes" or not model.binding.service_origin.startswith("http://"):
        raise RuntimeProtocolError("voice requires a local registered worker")
    started = time.monotonic()
    request = json.loads(body)
    pcm, expected_sequence, started_audio, done = bytearray(), 0, False, None
    owned_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), trust_env=False)
    try:
        queue_deadline = time.monotonic() + 60
        if operation.deadline_at is not None:
            queue_deadline = min(
                queue_deadline, time.monotonic() + (operation.deadline_at - datetime.now(UTC)).total_seconds()
            )
        while True:
            async with client.stream(
                "POST",
                model.binding.service_origin + "/v1/voice/synthesize",
                content=body,
                headers={"content-type": "application/json", "x-fs2-operation-id": str(operation.id)},
            ) as response:
                if response.status_code in {429, 503}:
                    # No input/audio accepted: a new connection may reach a free
                    # replica. A 200 response is never retried or replayed.
                    if time.monotonic() >= queue_deadline:
                        raise RuntimeTransportError("voice capacity unavailable")
                    await asyncio.sleep(0.25)
                    continue
                if response.status_code != 200:
                    raise RuntimeTransportError("voice worker rejected synthesis")
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if len(line) > 16384:
                        raise RuntimeProtocolError("voice event exceeds limit")
                    event = json.loads(line)
                    kind = event.get("type")
                    if done is not None:
                        raise RuntimeProtocolError("voice sent data after completion")
                    if kind == "audio.start":
                        if (
                            started_audio
                            or event.get("sample_rate_hz") != 22050
                            or event.get("encoding") != "pcm_s16le"
                        ):
                            raise RuntimeProtocolError("invalid voice audio format")
                        started_audio = True
                        await send({**event, "operation_id": str(operation.id)})
                    elif kind == "audio.chunk":
                        if not started_audio or event.get("sequence") != expected_sequence:
                            raise RuntimeProtocolError("voice audio sequence is invalid")
                        chunk = base64.b64decode(event["audio_base64"], validate=True)
                        if not chunk or len(chunk) % 2 or len(chunk) > 4410 or len(pcm) + len(chunk) > MAX_WAV_BYTES:
                            raise RuntimeProtocolError("voice audio bounds exceeded")
                        pcm.extend(chunk)
                        expected_sequence += 1
                        await send(event)
                    elif kind == "audio.done":
                        if not pcm or event.get("samples") != len(pcm) // 2 or event.get("chunks") != expected_sequence:
                            raise RuntimeProtocolError("voice completion does not match audio")
                        done = event
                    elif kind == "error":
                        raise RuntimeTransportError("voice worker failed during synthesis")
                    else:
                        raise RuntimeProtocolError("invalid voice event")
                break
        if done is None:
            raise RuntimeTransportError("voice worker disconnected before completion")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(22050)
            output.writeframes(pcm)
        return RuntimeResult(
            status_code=200,
            content_type="audio/wav",
            body=buffer.getvalue(),
            elapsed_seconds=time.monotonic() - started,
            runtime=RuntimeIdentity(),
            semantic_outcome="protocol_valid",
            usage=ReportedUsage(
                modalities=[
                    ModalityUsage(modality="text", direction="input", unit="characters", amount=len(request["text"])),
                    ModalityUsage(modality="audio", direction="output", unit="seconds", amount=len(pcm) / 44100),
                ]
            ),
        )
    except (httpx.HTTPError, OSError, TimeoutError):
        raise RuntimeTransportError("voice worker connection lost") from None
    except (ValueError, TypeError, KeyError):
        raise RuntimeProtocolError("voice worker response invalid") from None
    finally:
        if owned_client:
            await client.aclose()


def voice_router(
    *, principal: Callable[..., Awaitable[Principal]], registry: Registry, admission: AdmissionService, store: Store
) -> APIRouter:
    router = APIRouter(tags=["Voice"])

    @router.post("/v1/voice/synthesize")
    async def synthesize(request: Request, identity: Annotated[Principal, Depends(principal)]):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 32768:
                raise HTTPException(413, "voice request exceeds limit")
        try:
            payload = VoiceSynthesisRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "invalid voice synthesis request") from None
        model = registry.get(payload.model)
        if contract_for(model, "native").model_ref != MAGPIE:
            raise HTTPException(422, "model is not a Magpie synthesis App")
        # Admission performs ordinary model grant, scope, policy and route checks.
        body = json.dumps({**payload.model_dump(), "model": MAGPIE}, separators=(",", ":")).encode()
        operation = await admission.admit(
            identity,
            AdmissionRequest(
                model_id=payload.model,
                operation="synthesize",
                protocol="native",
                idempotency_key=request.headers.get("idempotency-key") or f"voice-{uuid4()}",
                request_body=body,
                deadline_at=datetime.now(UTC) + timedelta(seconds=240),
            ),
            streaming=True,
        )
        if operation.reused:
            raise HTTPException(
                409,
                {
                    "code": "session_already_exists",
                    "operation_id": str(operation.id),
                    "result_path": f"/v1/operations/{operation.id}/result",
                },
            )
        request.state.model_id, request.state.operation_id = payload.model, operation.id
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2)

        async def invoke(selected, claimed, content):
            return await relay_synthesis(selected, claimed, content, queue.put)

        async def generate():
            task = asyncio.create_task(admission.execute_stream(operation, invoke))
            samples, chunks = 0, 0
            try:
                yield json.dumps({"type": "operation.queued", "operation_id": str(operation.id)}) + "\n"
                while not task.done() or not queue.empty():
                    try:
                        event = await asyncio.wait_for(queue.get(), 0.1)
                    except TimeoutError:
                        continue
                    if event.get("type") == "audio.chunk":
                        samples += len(base64.b64decode(event["audio_base64"])) // 2
                        chunks += 1
                    yield json.dumps(event, separators=(",", ":")) + "\n"
                final = await task
                if final.status is OperationStatus.SUCCEEDED:
                    # WAV artifact and usage have committed before public success.
                    yield (
                        json.dumps(
                            {
                                "type": "audio.done",
                                "operation_id": str(operation.id),
                                "samples": samples,
                                "chunks": chunks,
                                "sample_rate_hz": 22050,
                                "duration_seconds": samples / 22050,
                                "result_path": f"/v1/operations/{operation.id}/result",
                            }
                        )
                        + "\n"
                    )
                else:
                    yield (
                        json.dumps(
                            {
                                "type": "error",
                                "code": final.error_code or "worker_lost",
                                "operation_id": str(operation.id),
                                "retryable": False,
                            }
                        )
                        + "\n"
                    )
            finally:
                if not task.done():
                    with anyio.CancelScope(shield=True):
                        with contextlib.suppress(Exception):
                            await store.cancel_operation(
                                operation.id, tenant_id=identity.tenant_id, actor=identity.principal_id
                            )
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={"x-fs2-operation-id": str(operation.id), "cache-control": "no-store", "x-accel-buffering": "no"},
        )

    return router


async def relay_voice_stream(model, operation, body, audio, send, *, connector=connect):
    if model.binding.backend_class != "local-kubernetes" or not model.binding.service_origin.startswith("http://"):
        raise RuntimeProtocolError("voice requires a local registered worker")
    url = "ws://" + model.binding.service_origin.removeprefix("http://") + "/v1/voice/stream"
    started, retained, retained_bytes = time.monotonic(), [], 0
    finished = asyncio.Event()
    deadline = time.monotonic() + 60
    try:
        while True:
            async with connector(
                url, max_size=65536, max_queue=2, write_limit=32768, open_timeout=10, close_timeout=5, proxy=None
            ) as upstream:
                await upstream.send(body.decode())
                ready = json.loads(await asyncio.wait_for(upstream.recv(), 30))
                if ready.get("type") == "error" and ready.get("code") in {"worker_busy", "worker_draining"}:
                    if time.monotonic() >= deadline:
                        raise RuntimeTransportError("voice capacity unavailable")
                    await asyncio.sleep(0.25)
                    continue
                if ready.get("type") != "session.ready":
                    raise RuntimeProtocolError("voice worker rejected stream")
                await send({**ready, "session_id": str(operation.id)})

                async def upload():
                    while True:
                        chunk = await audio.get()
                        if chunk == '{"type":"session.finish"}':
                            finished.set()
                        await upstream.send(chunk)
                        if finished.is_set():
                            return

                uploader = asyncio.create_task(upload())
                try:
                    async for raw in upstream:
                        event = json.loads(raw)
                        kind = event.get("type")
                        if kind == "session.done":
                            if not finished.is_set():
                                raise RuntimeProtocolError("voice completed before end of input")
                            await uploader
                            seconds = float(event["audio_seconds"])
                            if not 0 <= seconds <= 1800:
                                raise RuntimeProtocolError("invalid audio duration")
                            return RuntimeResult(
                                status_code=200,
                                content_type="application/json",
                                body=json.dumps(
                                    {
                                        "model": model.id,
                                        "events": retained,
                                        "audio_seconds": seconds,
                                        "text": " ".join(
                                            e["text"] for e in retained if e["type"] == "transcript.final"
                                        ),
                                    }
                                ).encode(),
                                elapsed_seconds=time.monotonic() - started,
                                runtime=RuntimeIdentity(),
                                semantic_outcome="protocol_valid",
                                usage=ReportedUsage(
                                    modalities=[
                                        ModalityUsage(
                                            modality="audio", direction="input", unit="seconds", amount=seconds
                                        )
                                    ]
                                ),
                            )
                        if kind == "error":
                            raise RuntimeTransportError("voice worker failed")
                        if kind not in {
                            "transcript.partial",
                            "transcript.final",
                            "turn.eou",
                            "turn.eob",
                            "speaker.activity",
                            "session.reset",
                        }:
                            raise RuntimeProtocolError("invalid voice event")
                        if kind != "transcript.partial":
                            retained_bytes += len(raw)
                            if retained_bytes > MAX_WAV_BYTES:
                                raise RuntimeProtocolError("voice result exceeds limit")
                            retained.append(event)
                        await send(event)
                    raise RuntimeTransportError("voice worker lost")
                finally:
                    uploader.cancel()
                    await asyncio.gather(uploader, return_exceptions=True)
    except (WebSocketException, OSError, TimeoutError):
        raise RuntimeTransportError("voice worker lost") from None
    except (ValueError, TypeError, KeyError):
        raise RuntimeProtocolError("voice worker sent invalid data") from None


def voice_stream_router(*, verifier, registry: Registry, admission: AdmissionService, store: Store):
    router = APIRouter(tags=["Voice"])

    @router.websocket("/v1/voice/stream")
    async def stream(socket: WebSocket):
        try:
            scheme, token = socket.headers.get("authorization", "").split(" ", 1)
            if scheme.lower() != "bearer" or not token:
                raise AuthenticationError("invalid bearer")
            identity = await verifier(token)
        except (ValueError, AuthenticationError):
            await socket.close(code=1008)
            return
        await socket.accept()
        operation, executor, reader = None, None, None
        try:
            raw = await asyncio.wait_for(socket.receive_text(), 10)
            if len(raw) > 4096:
                raise ValueError("control too large")
            start = json.loads(raw)
            if (
                not isinstance(start, dict)
                or set(start) - {"type", "model", "audio"}
                or start.get("type") != "session.start"
            ):
                raise ValueError("invalid session start")
            model_id = start.get("model")
            model = registry.get(model_id)
            source = contract_for(model, "native").model_ref
            if source not in {PARAKEET, SORTFORMER}:
                raise ValueError("model does not support audio stream input")
            expected_audio = {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1}
            if start.get("audio", expected_audio) != expected_audio:
                raise ValueError("invalid audio format")
            start.update(model=source, audio=expected_audio)
            operation = await admission.admit(
                identity,
                AdmissionRequest(
                    model_id=model_id,
                    operation="transcribe" if source == PARAKEET else "diarize",
                    protocol="native",
                    idempotency_key=socket.headers.get("idempotency-key") or f"voice-stream-{uuid4()}",
                    request_body=json.dumps(start).encode(),
                    deadline_at=datetime.now(UTC) + timedelta(seconds=1900),
                ),
                streaming=True,
            )
            if operation.reused:
                previous, operation = str(operation.id), None
                await socket.send_json({"type": "error", "code": "session_already_exists", "operation_id": previous})
                return
            await socket.send_json({"type": "operation.queued", "operation_id": str(operation.id)})
            audio = asyncio.Queue(maxsize=2)

            async def receive():
                total, ended = 0, False
                while True:
                    message = await asyncio.wait_for(socket.receive(), 30)
                    if message["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect()
                    if message.get("bytes") is not None:
                        chunk = message["bytes"]
                        total += len(chunk)
                        if ended or not chunk or len(chunk) % 2 or len(chunk) > 32000 or total > 1800 * 32000:
                            raise ValueError("audio bounds exceeded")
                        await audio.put(chunk)
                    else:
                        raw_control = message.get("text", "")
                        if len(raw_control) > 4096:
                            raise ValueError("control too large")
                        control = json.loads(raw_control)
                        if control == {"type": "session.cancel"}:
                            raise asyncio.CancelledError
                        if ended or control not in ({"type": "session.finish"}, {"type": "session.reset"}):
                            raise ValueError("invalid session control")
                        ended = control["type"] == "session.finish"
                        await audio.put(json.dumps(control, separators=(",", ":")))

            async def invoke(selected, claimed, body):
                return await relay_voice_stream(selected, claimed, body, audio, socket.send_json)

            reader = asyncio.create_task(receive())
            executor = asyncio.create_task(admission.execute_stream(operation, invoke))
            completed, _ = await asyncio.wait({reader, executor}, return_when=asyncio.FIRST_COMPLETED)
            if reader in completed:
                await reader
            final = await executor
            await socket.send_json(
                {
                    "type": "session.done" if final.status is OperationStatus.SUCCEEDED else "error",
                    "operation_id": str(operation.id),
                    "code": final.error_code,
                    "result_path": f"/v1/operations/{operation.id}/result",
                    "retryable": False,
                }
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await socket.send_json({"type": "session.cancelled"})
        except WebSocketDisconnect:
            pass
        except (ValueError, KeyError, PermissionError, TimeoutError):
            with contextlib.suppress(Exception):
                await socket.send_json({"type": "error", "code": "voice_session_rejected", "retryable": False})
        finally:
            with anyio.CancelScope(shield=True):
                if operation is not None and (executor is None or not executor.done()):
                    with contextlib.suppress(Exception):
                        await store.cancel_operation(
                            operation.id, tenant_id=identity.tenant_id, actor=identity.principal_id
                        )
                for task in (reader, executor):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(t for t in (reader, executor) if t is not None), return_exceptions=True)
                with contextlib.suppress(Exception):
                    await socket.close()

    return router
