from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException

from fs2_serve.mindguard_routes import mindguard_router
from fs2_serve.models import Principal
from fs2_serve.request_telemetry import InMemoryRequestTelemetryStore, RequestTelemetryMiddleware

ENDPOINT = "http://fs2-mindguard-r20260916-4b.fs2-models.svc.cluster.local:8000/v1"
BODY = {
    "model": "mindguard-4b",
    "messages": [
        {"role": "user", "content": "I am anxious."},
        {"role": "assistant", "content": "What is happening?"},
        {"role": "user", "content": "I am worried about an interview."},
    ],
}


def identity(**changes: object) -> Principal:
    return Principal.model_validate(
        {
            "token_id": uuid4(),
            "token_prefix": "test",
            "principal_id": "alice",
            "tenant_id": "team-a",
            "scopes": ["inference.invoke"],
            "models": ["mindguard-4b"],
            **changes,
        }
    )


def application(who: Principal, runtime: httpx.AsyncClient, endpoint: str | None = ENDPOINT):
    async def principal(authorization: str | None = Header(None)) -> Principal:
        if authorization != "Bearer test":
            raise HTTPException(401, "invalid bearer token")
        return who

    app = FastAPI()
    telemetry = InMemoryRequestTelemetryStore()
    app.add_middleware(RequestTelemetryMiddleware, store=telemetry)
    app.include_router(mindguard_router(principal=principal, endpoints={"mindguard-4b": endpoint}, client=runtime))
    return app, telemetry


def runtime_reply(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "mindguard-4b",
            "choices": [{"finish_reason": "stop", "message": {"content": "Safety: Safe\nCategories: None"}}],
            "usage": {"prompt_tokens": 700, "completion_tokens": 8},
        },
    )


async def test_normal_auth_model_grant_and_request_telemetry() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(runtime_reply)) as runtime:
        app, telemetry = application(identity(), runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/mindguard/assess", json=BODY, headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    body = response.json()
    assert body["evaluated_user_turns"] == 2
    assert body["enforcement"] == "observe"
    assert body["usage"]["accounting_mode"] == "observational_unbilled"
    assert body["usage"]["input_tokens"] == 1400
    assert body["usage"]["gpu_seconds"] is None
    assert len(telemetry.observations) == 1
    assert telemetry.observations[0].model_id == "mindguard-4b"
    assert telemetry.observations[0].tenant_id == "team-a"
    assert str(telemetry.observations[0].request_id) == body["request_id"]


@pytest.mark.parametrize(
    ("changes", "authorization", "expected"),
    [
        ({}, None, 401),
        ({"scopes": ["catalog.read"]}, "Bearer test", 403),
        ({"models": ["mindeval"]}, "Bearer test", 403),
        ({"models": ["mindguard-8b"]}, "Bearer test", 403),
        ({"request_budget": 20}, "Bearer test", 503),
        ({"gpu_seconds_budget": 100.0}, "Bearer test", 503),
    ],
)
async def test_auth_or_budget_failure_never_reaches_runtime(changes: dict, authorization: str | None, expected: int):
    def no_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("runtime must not be called")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_request)) as runtime:
        app, _ = application(identity(**changes), runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/mindguard/assess", json=BODY, headers={"Authorization": authorization} if authorization else {}
            )
    assert response.status_code == expected


async def test_missing_classifier_is_observation_unavailable_not_safe() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(runtime_reply)) as runtime:
        app, _ = application(identity(), runtime, endpoint=None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/mindguard/assess", json=BODY, headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["evaluated_user_turns"] == 0
    assert body["usage"]["input_tokens"] is None
    assert all(row["safety"] is None for row in body["assessments"])


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "mindguard-v2"},
        {"language": "pt"},
        {"enforcement": "block"},
        {"endpoint": "https://production.invalid/v1"},
        {"messages": [{"role": "assistant", "content": "No user turn"}]},
    ],
)
async def test_request_cannot_override_role_endpoint_language_or_enforcement(changes: dict):
    async with httpx.AsyncClient(transport=httpx.MockTransport(runtime_reply)) as runtime:
        app, _ = application(identity(), runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/mindguard/assess", json={**BODY, **changes}, headers={"Authorization": "Bearer test"}
            )
    assert response.status_code == 422
