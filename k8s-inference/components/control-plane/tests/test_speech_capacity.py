import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_admission_workers import service
from test_runtime_and_schema import claimed

from fs2_serve.memory_store import MemoryStore
from fs2_serve.runtime import ActivationError, RuntimeBusyError, RuntimeClient, StubRuntimeClient


@pytest.mark.parametrize("seconds", [None, True, -1, 0, 7201, "30", float("nan"), float("inf")])
def test_invalid_speech_usage_is_not_fabricated(seconds):
    assert RuntimeClient._reported_usage("native", json.dumps({"audio_seconds": seconds}).encode(), speech=True) is None


def test_file_speech_usage_uses_actual_decoded_seconds_only_for_speech():
    body = b'{"audio_seconds":421.860125,"text":"Hallo"}'
    usage = RuntimeClient._reported_usage("native", body, speech=True)
    assert usage.modalities[0].amount == 421.860125
    assert usage.modalities[0].unit == "seconds"
    assert RuntimeClient._reported_usage("native", body) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("speech,body,busy", [
    (True, {"detail": "runtime_busy"}, True),
    (True, {"detail": "rate_limited"}, False),
    (False, {"detail": "runtime_busy"}, False),
])
async def test_only_known_speech_pre_admission_busy_is_waitable(registry, speech, body, busy):
    model = registry.get("qwen3-8b")
    if speech:
        model = replace(model, gateway=replace(model.gateway, model_id="nemotron-speech-en-0-6b"))
    model = replace(model, gateway=replace(model.gateway,
        binding=replace(model.binding, endpoints={"native": "/generate"})))
    operation = claimed(registry).model_copy(update={"protocol": "native"})

    def handler(request):
        assert (request.headers.get("connection") == "close") is speech
        return httpx.Response(429, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = RuntimeClient(activation_timeout_seconds=2, runtime_timeout_seconds=2,
                                max_response_bytes=4096, client=client)
        if busy:
            with pytest.raises(RuntimeBusyError):
                await runtime.invoke(model, operation, b"{}")
        else:
            assert (await runtime.invoke(model, operation, b"{}")).status_code == 429


@pytest.mark.asyncio
async def test_busy_wait_refreshes_artifact_handles_without_new_operation_attempt(registry, cipher, hasher):
    runtime = StubRuntimeClient()
    invoke = runtime.invoke
    runtime.invoke = AsyncMock(side_effect=[RuntimeBusyError(), RuntimeBusyError(),
        await invoke(registry.get("qwen3-8b"), claimed(registry), b"{}")])
    admission = service(registry, MemoryStore(cipher, hasher), runtime)
    admission.artifact_inputs = SimpleNamespace(materialize=AsyncMock(side_effect=[b"url1", b"url2", b"url3"]))
    operation = claimed(registry)
    before = datetime.now(UTC)
    result, started = await admission._invoke_when_capacity_available(registry.get("qwen3-8b"), operation, b"artifact")
    assert result.status_code == 200 and started > before
    assert [call.args[2] for call in runtime.invoke.call_args_list] == [b"url1", b"url2", b"url3"]
    assert all(call.args[1] is operation and call.args[1].attempt == 1 for call in runtime.invoke.call_args_list)
    assert admission.artifact_inputs.materialize.await_count == 3


@pytest.mark.asyncio
async def test_busy_wait_honors_deadline_and_cancellation(registry, cipher, hasher):
    runtime = StubRuntimeClient()
    runtime.invoke = AsyncMock(side_effect=RuntimeBusyError())
    admission = service(registry, MemoryStore(cipher, hasher), runtime)
    operation = claimed(registry).model_copy(update={"deadline_at": datetime.now(UTC)-timedelta(seconds=1)})
    with pytest.raises(ActivationError, match="deadline"):
        await admission._invoke_when_capacity_available(registry.get("qwen3-8b"), operation, b"{}")
    task = asyncio.create_task(admission._invoke_when_capacity_available(
        registry.get("qwen3-8b"), claimed(registry), b"{}"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
