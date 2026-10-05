from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from test_mindguard_routes import BODY, ENDPOINT, identity, runtime_reply
from test_runtime_and_schema import claimed
from test_runtime_debug import DebugSink, runtime

from fs2_serve.mindguard_routes import mindguard_router
from fs2_serve.models import OperationStatus
from fs2_serve.runtime import RuntimeProtocolError


def model(registry):
    original = registry.get("qwen3-8b")
    return replace(
        original,
        gateway=replace(
            original.gateway,
            model_id="mindguard-4b",
            binding=replace(
                original.binding,
                endpoints={"native": "/v1/chat/completions"},
                backend_service_name="fs2-mindguard-r20260916-4b",
            ),
        ),
    )


async def test_transcript_execution_preserves_every_prefix_and_debug_exchange(registry):
    observed = []

    def reply(request):
        import json

        observed.append(json.loads(request.content))
        return runtime_reply(request)

    sink = DebugSink()
    operation = claimed(registry).model_copy(update={"protocol": "native", "model_id": "mindguard-4b"})
    import json

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        result = await runtime(client, sink).invoke(model(registry), operation, json.dumps(BODY).encode())
    value = json.loads(result.body)
    assert result.status_code == 200 and result.semantic_outcome == "protocol_valid"
    assert value["status"] == "completed" and value["evaluated_user_turns"] == 2
    assert [len(item["messages"]) for item in observed] == [1, 3]
    assert result.usage.input_tokens == 1400 and result.usage.output_tokens == 16
    assert len(sink.exchanges) == 2
    for index, exchange in enumerate(sink.exchanges, 1):
        assert exchange.operation_id == operation.id and exchange.upstream_attempt == index
        assert exchange.request_body.complete and exchange.response_body.complete
        assert "Safety: Safe" in exchange.response_body.data


@pytest.mark.parametrize("case", ["identity", "partial", "oversize"])
async def test_bad_upstream_never_becomes_a_successful_assessment(registry, case):
    import json

    calls = 0

    def reply(request):
        nonlocal calls
        calls += 1
        if case == "partial" and calls == 2:
            return httpx.Response(503, json={"error": "synthetic unavailable"})
        if case == "oversize":
            return httpx.Response(200, content=b"x" * 65537)
        return runtime_reply(request)

    body = {**BODY, "model": "mindguard-8b"} if case == "identity" else BODY
    operation = claimed(registry).model_copy(update={"protocol": "native", "model_id": "mindguard-4b"})
    sink = DebugSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        candidate = runtime(client, sink, maximum=65536)
        if case in {"identity", "oversize"}:
            with pytest.raises(RuntimeProtocolError):
                await candidate.invoke(model(registry), operation, json.dumps(body).encode())
        else:
            result = await candidate.invoke(model(registry), operation, json.dumps(body).encode())
            assert result.status_code == 502 and result.failure_code == "mindguard_assessment_incomplete"
            assert json.loads(result.body)["status"] == "partial"
    if case == "identity":
        assert calls == 0


@pytest.mark.parametrize("status", ["queued", "succeeded", "failed", "cancelled", "expired"])
async def test_direct_endpoint_uses_ordinary_admission_and_bounded_wait(registry, status):
    who = identity(request_budget=20)
    operation = claimed(registry).model_copy(
        update={"status": OperationStatus(status), "reused": True, "model_id": "mindguard-4b"}
    )
    admission = SimpleNamespace(admit=AsyncMock(return_value=operation), wait=AsyncMock(return_value=operation))
    store = SimpleNamespace(
        get_operation_result=AsyncMock(
            return_value=SimpleNamespace(result={"status": "completed", "evaluated_user_turns": 2})
        )
    )
    selected = SimpleNamespace(get=Mock(return_value=model(registry)))

    async def principal():
        return who

    app = FastAPI()
    app.include_router(
        mindguard_router(
            principal=principal,
            endpoints={"mindguard-4b": ENDPOINT},
            admission=admission,
            store=store,
            registry=selected,
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post(
            "/v1/mindguard/assess",
            json=BODY,
            headers={"idempotency-key": "fixed-transcript", "x-fs2-wait-seconds": "0"},
        )
    admitted = admission.admit.call_args.args[1]
    assert admitted.model_id == "mindguard-4b" and admitted.operation == "assess-transcript"
    assert admitted.protocol == "native" and admitted.idempotency_key == "fixed-transcript"
    assert admission.wait.call_args.kwargs == {"tenant_id": who.tenant_id, "seconds": 0}
    assert response.headers["x-fs2-operation-id"] == str(operation.id)
    if status == "queued":
        assert response.status_code == 202 and response.json()["id"] == str(operation.id)
        assert response.headers["location"] == f"/v1/operations/{operation.id}"
        assert "assessments" not in response.json()
    elif status == "succeeded":
        assert response.status_code == 200 and response.json()["status"] == "completed"
        assert response.json()["usage"]["accounting_mode"] == "durable_operation"
    else:
        assert response.status_code >= 400 and "assessments" not in response.json()


@pytest.mark.parametrize("wait", ["nan", "inf", "-1", "31", "invalid"])
async def test_wait_cannot_be_unbounded(registry, wait):
    async def principal():
        return identity()

    admission = SimpleNamespace(admit=AsyncMock())
    app = FastAPI()
    app.include_router(
        mindguard_router(
            principal=principal,
            endpoints={"mindguard-4b": ENDPOINT},
            admission=admission,
            store=Mock(),
            registry=Mock(),
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/v1/mindguard/assess", json=BODY, headers={"x-fs2-wait-seconds": wait})
    assert response.status_code == 422 and not admission.admit.called
