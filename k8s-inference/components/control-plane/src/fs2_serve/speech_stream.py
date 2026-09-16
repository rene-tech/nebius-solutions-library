"""Authenticated live audio relay using ordinary fenced platform operations."""

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jsonschema import Draft202012Validator
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .admission import AdmissionService
from .auth import AuthenticationError
from .model_input_contracts import contract_for
from .models import (
    AdmissionRequest,
    ClaimedOperation,
    ModalityUsage,
    OperationStatus,
    Principal,
    ReportedUsage,
    RuntimeIdentity,
    RuntimeResult,
)
from .registry import OperationalModel, Registry
from .runtime import RuntimeOperationError, RuntimeProtocolError, RuntimeTransportError
from .speech_routes import SPEECH_MODELS
from .store import ConflictError, Store

MAX_AUDIO_BYTES = 7200 * 32000
MAX_MESSAGE_BYTES = 65536


class ClientCancelledError(Exception):
    pass


async def relay_live(
    model: OperationalModel,
    operation: ClaimedOperation,
    start: bytes,
    audio: asyncio.Queue[bytes | str],
    send: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    connector: Any = connect,
) -> RuntimeResult:
    """One bounded duplex connection; finals persist before public completion.

    Busy workers can be retried only before a session accepts any audio. After
    readiness, transport loss is terminal: there is no invisible audio replay.
    """
    origin = model.binding.service_origin
    if model.binding.backend_class != "local-kubernetes" or not origin.startswith("http://"):
        raise RuntimeProtocolError("live speech requires a local canonical runtime")
    url = "ws://" + origin.removeprefix("http://") + "/v1/audio/stream"
    started = time.monotonic()
    deadline = started + 60
    finals: list[dict[str, Any]] = []
    result_bytes = 0
    try:
        while True:
            async with connector(url, additional_headers={"x-fs2-operation-id": str(operation.id)},
                                 open_timeout=10, close_timeout=5, max_size=1024 * 1024,
                                 max_queue=2, write_limit=65536, proxy=None) as upstream:
                await upstream.send(start.decode())
                first = json.loads(await asyncio.wait_for(upstream.recv(), timeout=30))
                if first.get("type") == "session.error" and first.get("code") in {"runtime_busy", "runtime_not_ready"}:
                    if time.monotonic() >= deadline:
                        raise RuntimeTransportError("speech capacity unavailable")
                    await asyncio.sleep(0.5)
                    continue
                if first.get("type") != "session.ready":
                    raise RuntimeProtocolError("speech worker rejected the session options")
                await send({**first, "session_id": str(operation.id)})
                finishing = asyncio.Event()

                async def upload(finishing=finishing):
                    while True:
                        message = await audio.get()
                        if message == '{"type":"input.finish"}':
                            finishing.set()
                        await upstream.send(message)
                        if message == '{"type":"input.finish"}':
                            return

                producer = asyncio.create_task(upload())
                try:
                    async for raw in upstream:
                        event = json.loads(raw)
                        kind = event.get("type")
                        if kind in {"transcript.partial", "transcript.final"}:
                            if not isinstance(event.get("text"), str):
                                raise RuntimeProtocolError("speech transcript event is invalid")
                            if kind == "transcript.final":
                                result_bytes += len(raw.encode() if isinstance(raw, str) else raw)
                                if result_bytes > 16 * 1024 * 1024:
                                    raise RuntimeProtocolError("speech result exceeds the retained result limit")
                                finals.append({**event, "session_id": str(operation.id)})
                            await send({**event, "session_id": str(operation.id)})
                        elif kind == "session.completed":
                            if not finishing.is_set():
                                raise RuntimeProtocolError("speech worker completed before client end of audio")
                            await producer  # Every client frame, including EOS, was forwarded.
                            seconds = float(event["audio_seconds"])
                            if not 0 < seconds <= 7200:
                                raise RuntimeProtocolError("speech audio duration is invalid")
                            body = {"text": "".join(item["text"] for item in finals).strip(),
                                    "segments": finals, "audio_seconds": seconds,
                                    "model": model.id, "model_revision": operation.model_revision}
                            return RuntimeResult(
                                status_code=200, content_type="application/json",
                                body=json.dumps(body, separators=(",", ":")).encode(),
                                elapsed_seconds=time.monotonic() - started, runtime=RuntimeIdentity(),
                                semantic_outcome="protocol_valid",
                                usage=ReportedUsage(modalities=[ModalityUsage(
                                    modality="audio", direction="input", unit="seconds", amount=seconds,
                                )]),
                            )
                        elif kind == "session.error":
                            raise RuntimeTransportError("speech worker failed before complete transcription")
                        else:
                            raise RuntimeProtocolError("speech worker sent an invalid event")
                    raise RuntimeTransportError("speech worker disconnected before completion")
                finally:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
    except (WebSocketException, OSError, TimeoutError):
        raise RuntimeTransportError("speech worker connection failed") from None
    except (ValueError, TypeError, KeyError):
        raise RuntimeProtocolError("speech worker response was invalid") from None


def speech_stream_router(
    *, verifier: Callable[[str], Awaitable[Principal]], registry: Registry,
    admission: AdmissionService, store: Store,
) -> APIRouter:
    router = APIRouter(tags=["Speech"])

    @router.websocket("/v1/audio/stream")
    async def stream(socket: WebSocket):
        # No query-string keys: ordinary platform bearer credentials and model
        # grants are the only authentication/authorization mechanism.
        authorization = socket.headers.get("authorization", "")
        try:
            scheme, token = authorization.split(" ", 1)
            if scheme.lower() != "bearer" or not token:
                raise AuthenticationError("invalid bearer")
            principal = await verifier(token)
        except (AuthenticationError, ValueError):
            await socket.close(code=1008)
            return
        await socket.accept()
        operation = None
        executor = reader = None
        disconnected = False

        async def send(event):
            await socket.send_json(event)

        try:
            message = await asyncio.wait_for(socket.receive_text(), timeout=10)
            if len(message.encode()) > 4096:
                raise ValueError("session options too large")
            start = json.loads(message)
            if (not isinstance(start, dict) or set(start) - {"type", "options", "audio"}
                    or start.get("type") != "session.start" or not isinstance(start.get("options"), dict)):
                raise ValueError("invalid session start")
            model_id = start["options"].get("model")
            if not isinstance(model_id, str):
                raise ValueError("missing model")
            model = registry.get(model_id)
            contract = contract_for(model, "native")
            if contract.model_ref not in SPEECH_MODELS:
                raise ValueError("not a speech App")
            start["options"]["model"] = contract.input_schema["properties"]["options"]["properties"]["model"]["const"]
            if not Draft202012Validator(contract.input_schema["properties"]["options"]).is_valid(start["options"]):
                raise ValueError("invalid speech options")
            expected_audio = {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1}
            if start.get("audio", expected_audio) != expected_audio:
                raise ValueError("live audio requires mono 16 kHz signed little-endian PCM16")
            start["audio"] = expected_audio
            key = socket.headers.get("idempotency-key") or f"speech-live-{uuid4()}"
            operation = await admission.admit(principal, AdmissionRequest(
                model_id=model_id, operation="transcribe", protocol="native", idempotency_key=key,
                request_body=json.dumps(start, sort_keys=True, separators=(",", ":")).encode(),
                deadline_at=datetime.now(UTC) + timedelta(seconds=7500),
            ), streaming=True)
            if operation.reused:
                # Replays may inspect the existing operation, never seize an
                # already-owned connection or cancel it in cleanup below.
                existing_id, operation = str(operation.id), None
                await send({"type": "session.error", "code": "session_already_exists",
                            "operation_id": existing_id, "retryable": False})
                return
            await send({"type": "session.queued", "session_id": str(operation.id),
                        "operation_id": str(operation.id), "status_path": f"/v1/operations/{operation.id}"})
            queue: asyncio.Queue[bytes | str] = asyncio.Queue(maxsize=2)

            async def receive_audio():
                size, finished = 0, False
                while True:
                    event = await socket.receive()
                    if event["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect(event.get("code", 1000))
                    chunk = event.get("bytes")
                    if chunk is not None:
                        size += len(chunk)
                        if finished or len(chunk) > MAX_MESSAGE_BYTES or size > MAX_AUDIO_BYTES:
                            raise ValueError("audio framing or duration limit exceeded")
                        await queue.put(chunk)
                    else:
                        text = event.get("text", "")
                        if len(text.encode()) > 4096:
                            raise ValueError("control message too large")
                        control = json.loads(text)
                        if control == {"type": "session.cancel"}:
                            raise ClientCancelledError
                        if control != {"type": "input.finish"} or finished:
                            raise ValueError("invalid control message")
                        finished = True
                        await queue.put('{"type":"input.finish"}')

            async def invoke(selected, claimed, body):
                return await relay_live(selected, claimed, body, queue, send)

            reader = asyncio.create_task(receive_audio())
            executor = asyncio.create_task(admission.execute_stream(operation, invoke))
            done, _ = await asyncio.wait({reader, executor}, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                await reader
            final = await executor
            if final.status is OperationStatus.SUCCEEDED:
                await send({"type": "session.completed", "session_id": str(final.id),
                            "operation_id": str(final.id), "result_path": f"/v1/operations/{final.id}/result"})
            else:
                await send({"type": "session.error", "session_id": str(final.id),
                            "code": final.error_code or final.status.value, "retryable": True})
        except WebSocketDisconnect:
            disconnected = True
        except ClientCancelledError:
            await store.cancel_operation(operation.id, tenant_id=principal.tenant_id, actor=principal.principal_id)
            await send({"type": "session.cancelled", "session_id": str(operation.id)})
        except (PermissionError, KeyError, ValueError, ConflictError, RuntimeOperationError, TimeoutError):
            await send({"type": "session.error", "code": "speech_session_rejected", "retryable": False})
        finally:
            if operation is not None and (executor is None or not executor.done()):
                # Mark cancellation before terminating the heartbeat/runtime so
                # a disconnected client cannot leave work queued or replayable.
                with contextlib.suppress(Exception):
                    await store.cancel_operation(operation.id, tenant_id=principal.tenant_id,
                                                 actor=principal.principal_id)
            for task in (reader, executor):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (reader, executor) if task is not None), return_exceptions=True)
            if not disconnected:
                with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                    await socket.close()

    return router
