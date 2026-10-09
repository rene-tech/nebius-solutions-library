import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
import respx

from fs2_workshop.worker import RemoteFailure, classifier_call, classifier_result


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled", "expired"])
@respx.mock
async def test_classifier_waits_for_real_terminal_result(terminal, monkeypatch):
    async def immediate(_):
        pass
    monkeypatch.setattr("fs2_workshop.worker.asyncio.sleep", immediate)
    identifier = str(uuid4())
    accepted = respx.post("http://platform/v1/mindguard/assess").respond(202, json={"id": identifier})
    poll = respx.get("http://platform/v1/operations/" + identifier).mock(side_effect=[
        httpx.Response(200, json={"status": "queued"}), httpx.Response(200, json={"status": terminal})])
    result = respx.get("http://platform/v1/operations/" + identifier + "/result").respond(
        200, json={"status": "completed", "evaluated_user_turns": 2})
    async with httpx.AsyncClient() as client:
        arguments = dict(body={"model": "mindguard-4b"}, host="platform.example", idempotency_key="stable-run")
        if terminal == "succeeded":
            value = await classifier_call(client, "http://platform", "internal-test", **arguments)
            assert value["status"] == "completed" and value["operation_id"] == identifier
            assert result.call_count == 1
        else:
            with pytest.raises(RemoteFailure) as failure:
                await classifier_call(client, "http://platform", "internal-test", **arguments)
            assert failure.value.code == "classifier_" + terminal
            assert result.call_count == 0
    assert poll.call_count == 2
    assert accepted.calls[0].request.headers["idempotency-key"] == "stable-run"


@respx.mock
async def test_classifier_timeout_retains_recoverable_operation():
    identifier = str(uuid4())
    respx.post("http://platform/v1/mindguard/assess").respond(202, json={"id": identifier})
    async with httpx.AsyncClient() as client:
        with pytest.raises(RemoteFailure) as failure:
            await classifier_call(client, "http://platform", "internal-test", body={}, host="platform.example",
                                  idempotency_key="stable-run", timeout=0)
    assert failure.value.code == "classifier_wait_timeout"
    assert identifier in str(failure.value)


@respx.mock
async def test_immediate_completed_result_is_preserved():
    expected = {"status": "completed", "evaluated_user_turns": 2}
    respx.post("http://platform/v1/mindguard/assess").respond(200, json=expected)
    async with httpx.AsyncClient() as client:
        assert await classifier_call(client, "http://platform", "internal-test", body={}, host="platform.example",
                                     idempotency_key="same-run") == expected


@respx.mock
async def test_worker_cancellation_does_not_retry_or_hide_durable_operation(monkeypatch):
    identifier = str(uuid4())
    accepted = respx.post("http://platform/v1/mindguard/assess").respond(202, json={"id": identifier})
    respx.get("http://platform/v1/operations/" + identifier).respond(200, json={"status": "activating"})
    async def interrupted(_):
        raise asyncio.CancelledError
    monkeypatch.setattr("fs2_workshop.worker.asyncio.sleep", interrupted)
    async with httpx.AsyncClient() as client:
        with pytest.raises(asyncio.CancelledError):
            await classifier_call(client, "http://platform", "internal-test", body={}, host="platform.example",
                                  idempotency_key="same-run")
    # The workshop's explicit Resume uses the same idempotency key, so it can
    # reconnect to this durable operation instead of billing another execution.
    assert accepted.call_count == 1


@pytest.mark.parametrize("corrupt", [False, True])
@respx.mock
async def test_large_transcript_result_uses_authenticated_artifact_and_verified_download(corrupt):
    identifier = str(uuid4())
    raw = json.dumps({"status": "completed", "evaluated_user_turns": 128}).encode()
    result = {"schema": "fs2-serve.nebius.ai/operation-artifact-result/v1", "artifact": {
        "artifact_id": identifier, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}}
    signed = "https://objects.invalid/immutable-result?signature=synthetic"
    download = respx.get(signed).respond(200, content=b"corrupt" if corrupt else raw)
    receipt = respx.get("http://platform/v1/artifacts/" + identifier + "/download").respond(200, json={
        "handle": {"method": "GET", "url": signed, "headers": {}}})
    async with httpx.AsyncClient() as client:
        if corrupt:
            with pytest.raises(RemoteFailure, match="checksum"):
                await classifier_result(client, "http://platform", "internal-test", result, "platform.example")
        else:
            value = await classifier_result(client, "http://platform", "internal-test", result, "platform.example")
            assert value["status"] == "completed" and value["evaluated_user_turns"] == 128
    assert receipt.calls[0].request.headers["authorization"] == "Bearer internal-test"
    assert "authorization" not in download.calls[0].request.headers
