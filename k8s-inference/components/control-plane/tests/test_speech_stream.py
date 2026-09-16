"""Live speech uses real operation fencing; GPU acceptance is separate."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_admission_workers import request, service, setup_principal
from websockets.exceptions import ConnectionClosed

from fs2_serve.auth import AuthenticationError
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import OperationStatus, RuntimeIdentity, RuntimeResult
from fs2_serve.runtime import RuntimeProtocolError, RuntimeTransportError, StubRuntimeClient
from fs2_serve.speech_stream import relay_live, speech_stream_router
from fs2_serve.store import ConflictError


async def stored_stream(store, principal, key="stream-ownership-1"):
    return await store.append_operation(
        principal=principal, admission=request(key).model_copy(update={"protocol": "speech-stream-v1"}),
        model_revision="test", reserved_gpu_seconds=5, max_attempts=1,
    )


async def test_generic_workers_never_claim_live_audio(cipher, hasher):
    store = MemoryStore(cipher, hasher)
    principal = await setup_principal(store)
    live = await stored_stream(store, principal)
    assert await store.claim_operation("http-worker", lease_seconds=5) is None
    assert await store.claim_operation("stream-worker", lease_seconds=5, stream_operation_id=uuid4()) is None
    claims = await asyncio.gather(*[
        store.claim_operation(f"stream-{i}", lease_seconds=5, stream_operation_id=live.id) for i in range(5)
    ])
    claimed = [item for item in claims if item is not None]
    assert len(claimed) == 1 and claimed[0].id == live.id and claimed[0].max_attempts == 1


async def test_specific_stream_claim_cannot_take_a_file_operation(cipher, hasher):
    store = MemoryStore(cipher, hasher)
    principal = await setup_principal(store)
    ordinary = await store.append_operation(principal=principal, admission=request("file-op-1"),
                                            model_revision="test", reserved_gpu_seconds=5, max_attempts=2)
    assert await store.claim_operation("stream", lease_seconds=5, stream_operation_id=ordinary.id) is None
    assert (await store.claim_operation("http", lease_seconds=5)).id == ordinary.id


async def test_live_executor_persists_result_and_is_never_replayed(registry, cipher, hasher):
    store = MemoryStore(cipher, hasher)
    principal = await setup_principal(store)
    admission = service(registry, store, StubRuntimeClient())
    model = registry.get("qwen3-8b")
    operation = await store.append_operation(
        principal=principal, admission=request("live-result-1").model_copy(update={"protocol": "speech-stream-v1"}),
        model_revision=model.model_revision, reserved_gpu_seconds=5, max_attempts=1,
    )
    invoked = []

    async def run(model, claimed, body):
        invoked.append(claimed.id)
        return RuntimeResult(status_code=200, body=b'{"text":"Final words."}', content_type="application/json",
                             elapsed_seconds=1, runtime=RuntimeIdentity(), semantic_outcome="protocol_valid")

    final = await admission.execute_stream(operation, run)
    assert final.status is OperationStatus.SUCCEEDED and final.attempt == 1
    assert (await store.get_operation_result(final.id, tenant_id=principal.tenant_id)).result["text"] == "Final words."
    with pytest.raises(ConflictError):
        await admission.execute_stream(final, run)
    assert invoked == [operation.id] and admission.health()["inflight"] == 0


async def test_non_speech_models_cannot_use_live_admission(registry, cipher, hasher):
    store = MemoryStore(cipher, hasher)
    principal = await setup_principal(store)
    admission = service(registry, store, StubRuntimeClient())
    with pytest.raises(ValueError):
        await admission.admit(principal, request("non-speech-live"), streaming=True)
    assert not store.operations


class FakeSocket:
    def __init__(self, events):
        self.events, self.sent = list(events), []

    async def send(self, value):
        self.sent.append(value)

    async def recv(self):
        return json.dumps(self.events.pop(0))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        value = self.events.pop(0)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)


class Connector:
    def __init__(self, events):
        self.socket = FakeSocket(events)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *args):
        return False


READY = {"type": "session.ready", "session_id": "private-backend-id"}
FINAL = {"type": "transcript.final", "session_id": "private-backend-id", "text": "A transcript.", "sequence": 1}
COMPLETE = {"type": "session.completed", "audio_seconds": 1.0}


async def relay(events):
    connector = Connector(events)
    operation = SimpleNamespace(id=uuid4(), model_revision="revision", accepted_at=datetime.now(UTC), deadline_at=None)
    model = SimpleNamespace(id="speech-model", dynamic_policy=None, binding=SimpleNamespace(
        backend_class="local-kubernetes", service_origin="http://speech.fs2-models.svc.cluster.local:8000"))
    queue = asyncio.Queue(maxsize=2)
    await queue.put(b"\0" * 32000)
    await queue.put('{"type":"input.finish"}')
    sent = []

    async def send(event):
        sent.append(event)

    result = await relay_live(model, operation, b'{"type":"session.start"}', queue, send, connector=connector)
    return result, sent, connector, operation


async def test_relay_forwards_early_text_but_completion_waits_for_database():
    result, events, connector, operation = await relay([READY, FINAL, COMPLETE])
    assert [item["type"] for item in events] == ["session.ready", "transcript.final"]
    assert all(event["session_id"] == str(operation.id) for event in events)
    assert json.loads(result.body)["text"] == "A transcript."
    assert result.usage.modalities[0].amount == 1
    assert connector.socket.sent[-1] == '{"type":"input.finish"}'
    assert "authorization" not in connector.calls[0][1]["additional_headers"]


@pytest.mark.parametrize("events", [[READY, FINAL], [READY, {"type": "session.error", "code": "runtime_failure"}],
                                    [READY, ConnectionClosed(None, None)]])
async def test_worker_loss_never_returns_a_success(events):
    with pytest.raises(RuntimeTransportError):
        await relay(events)


async def test_premature_completion_cannot_hang_waiting_for_unsent_audio():
    connector = Connector([READY, COMPLETE])
    operation = SimpleNamespace(id=uuid4(), model_revision="revision", accepted_at=datetime.now(UTC), deadline_at=None)
    model = SimpleNamespace(id="speech-model", dynamic_policy=None, binding=SimpleNamespace(
        backend_class="local-kubernetes", service_origin="http://speech.fs2-models.svc.cluster.local:8000"))

    async def send(event):
        pass

    async with asyncio.timeout(1):
        with pytest.raises(RuntimeProtocolError, match="before client end"):
            await relay_live(model, operation, b'{}', asyncio.Queue(maxsize=2), send, connector=connector)


async def test_shutdown_drains_connection_owned_executor(registry, cipher, hasher):
    store = MemoryStore(cipher, hasher)
    principal = await setup_principal(store)
    admission = service(registry, store, StubRuntimeClient(), grace=1)
    operation = await store.append_operation(
        principal=principal, admission=request("live-drain-001").model_copy(update={"protocol": "speech-stream-v1"}),
        model_revision=registry.get("qwen3-8b").model_revision, reserved_gpu_seconds=5, max_attempts=1,
    )
    started, finish = asyncio.Event(), asyncio.Event()

    async def invoke(model, claimed, body):
        started.set()
        await finish.wait()
        return RuntimeResult(status_code=200, body=b'{"text":"Done"}', content_type="application/json",
                             elapsed_seconds=1, runtime=RuntimeIdentity(), semantic_outcome="protocol_valid")

    execution = asyncio.create_task(admission.execute_stream(operation, invoke))
    await asyncio.wait_for(started.wait(), timeout=1)
    closing = asyncio.create_task(admission.stop())
    await asyncio.sleep(0.01)
    assert not closing.done()
    finish.set()
    assert (await execution).status is OperationStatus.SUCCEEDED
    await closing
    assert not admission._stream_tasks


@pytest.mark.parametrize("events", [[READY, {"type": "bogus"}], [READY, {"type": "transcript.final", "text": 12}]])
async def test_bad_worker_events_fail_explicitly(events):
    with pytest.raises(RuntimeProtocolError):
        await relay(events)


def test_websocket_rejects_missing_and_invalid_keys_without_accepting_audio():
    async def invalid(token):
        raise AuthenticationError("invalid")

    app = FastAPI()
    app.include_router(speech_stream_router(verifier=invalid, registry=None, admission=None, store=None))
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app) as client:
        for headers in ({}, {"authorization": "Bearer not-a-platform-key"}):
            with pytest.raises(WebSocketDisconnect) as error:
                with client.websocket_connect("/v1/audio/stream", headers=headers):
                    pass
            assert error.value.code == 1008
