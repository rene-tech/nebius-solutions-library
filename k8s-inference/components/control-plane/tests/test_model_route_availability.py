"""Regressions for the customer trial's hot/burst route withdrawal."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError
from test_api_mcp import build_runtime
from test_model_deployment import model_spec, renderer, reserved_and_preemptible_envelope
from test_model_deployment_controller import FakeApi, ZeroActiveOperations, fence, model_object
from test_model_deployment_publication import revision, status_view

from fs2_serve.api import create_app
from fs2_serve.mcp_server import PATTokenVerifier, build_mcp_server, mount_mcp
from fs2_serve.model_deployment_controller import ModelDeploymentController, ModelKey
from fs2_serve.model_deployment_publication import PublicationReason, assess_model_publication
from fs2_serve.model_deployment_records import ModelDeploymentConditionType, ModelDeploymentRuntimePhase
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.registry import ModelRouteUnavailableError, Registry


@pytest.mark.asyncio
@pytest.mark.parametrize("burst_phase", ["generation-pending", "pod-pending", "warming"])
async def test_ready_hot_route_survives_independent_burst_scaleup(burst_phase: str) -> None:
    spec = model_spec().model_copy(
        update={
            "placement": model_spec().placement.model_copy(update={"pool_refs": ["reserved-h100", "preemptible-h100"]}),
            "availability": model_spec().availability.model_copy(update={"min_replicas": 1, "max_replicas": 4}),
        }
    )
    raw = model_object()
    raw["spec"] = spec.model_dump(mode="json", by_alias=True)
    api = FakeApi(raw)
    subject = ModelDeploymentController(
        api=api,
        envelope=reserved_and_preemptible_envelope(),
        renderer=renderer(),
        namespace="fs2-models",
        holder_identity="fs2-system/controller:pod-uid",
        prometheus_server_address="http://prometheus:9090",
        writes_enabled=True,
        active_operations=ZeroActiveOperations(),
    )
    key = ModelKey(namespace="fs2-models", name="qwen-live")
    for _ in range(3):
        await subject.reconcile(key, fence())
    deployments = [item for item in api.resources.values() if item.observed.kind == "Deployment"]
    hot = next(
        item
        for item in deployments
        if item.raw["metadata"]["annotations"]["fs2-serve.nebius.ai/workload-role"] == "hot"
    )
    bursts = [item for item in deployments if item is not hot]
    for item in bursts:
        item.desired_replicas = item.replicas = item.updated_replicas = 0
        item.ready_replicas = item.available_replicas = item.unavailable_replicas = 0
    burst = bursts[-1]
    burst.desired_replicas = 1
    if burst_phase == "generation-pending":
        burst.generation += 1
    elif burst_phase == "warming":
        burst.replicas = burst.updated_replicas = burst.unavailable_replicas = 1
    await subject.reconcile(key, fence())
    status = api.status_writes[-1]
    assert status["phase"] == "Ready"
    assert status["replicas"]["ready"] == 1
    assert status["replicas"]["desired"] == 2
    assert status["endpoint"] and status["publication"]["mcp"]

    # A stale hot rollout (for example, a replacement template not observed by
    # the Deployment controller) is not evidence of the desired runtime.
    hot.observed_generation = 0
    hot.updated_replicas = 0
    await subject.reconcile(key, fence())
    assert api.status_writes[-1]["phase"] != "Ready"


def test_localizing_publication_matches_the_controllers_loading_condition() -> None:
    desired = revision()
    observed = status_view(desired, phase=ModelDeploymentRuntimePhase.LOCALIZING)
    assert observed.observation is not None
    observed.observation.status.conditions[0].type = ModelDeploymentConditionType.LOADING
    assessment = assess_model_publication(desired, observed)
    assert assessment.reason is PublicationReason.ACTIVATABLE
    assert assessment.publication is not None and not assessment.publication.runtime_ready
    observed.observation.status.conditions[0].observed_generation = 6
    assert assess_model_publication(desired, observed).reason is PublicationReason.READY_CONDITION_MISSING


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["lookup", "admission-refresh"])
async def test_mcp_unavailable_route_has_structured_tool_error_and_correlation(
    registry, cipher, hasher, monkeypatch, caplog, failure_stage: str
) -> None:
    if failure_stage == "lookup":
        original = registry.get("qwen3-8b")
        disabled = replace(original, gateway=replace(original.gateway, routable=False))
        registry = Registry(registry.catalog, {original.id: disabled})
    runtime = build_runtime(registry, cipher, hasher)
    if failure_stage == "admission-refresh":

        async def unavailable(*args, **kwargs):
            raise ModelRouteUnavailableError("private route diagnostic must not leak")

        monkeypatch.setattr(runtime.admission, "admit", unavailable)
    app = create_app(runtime)
    mount_mcp(app, runtime)
    token = await runtime.tokens.issue(
        TokenCreate(principal_id="route-owner", tenant_id="tenant-a", scopes={Scope.MCP_INVOKE}, models={"qwen3-8b"}),
        created_by="bootstrap-admin",
    )
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=runtime.settings.public_origin(),
            headers={"authorization": f"Bearer {token.token}", "origin": runtime.settings.public_origin()},
            trust_env=False,
        ) as client:
            async with streamable_http_client(runtime.settings.public_origin() + "/mcp", http_client=client) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "invoke_model",
                        {
                            "model_id": "qwen3-8b",
                            "protocol": "openai-chat",
                            "payload": {"messages": [{"role": "user", "content": "private prompt must not leak"}]},
                            "idempotency_key": "route-unavailable-regression-0001",
                        },
                    )
                    assert result.is_error
                    error = result.structured_content["error"]
                    assert error["type"] == "route_unavailable" and error["retryable"] is True
                    UUID(error["request_id"])
                    assert json.loads(result.content[0].text)["error"] == error
                    assert error["request_id"] in caplog.text
                    assert "private prompt must not leak" not in caplog.text
                    assert "private route diagnostic must not leak" not in caplog.text
                    assert "unexpected exception" not in caplog.text
    assert not runtime.store.operations


@pytest.mark.asyncio
async def test_mcp_does_not_describe_unavailable_model_outside_token_policy(registry, cipher, hasher) -> None:
    original = registry.get("qwen3-8b")
    disabled = replace(original, gateway=replace(original.gateway, routable=False))
    runtime = build_runtime(Registry(registry.catalog, {original.id: disabled}), cipher, hasher)
    server = build_mcp_server(runtime)
    token = await runtime.tokens.issue(
        TokenCreate(
            principal_id="other-owner", tenant_id="tenant-b", scopes={Scope.MCP_INVOKE}, models={"other-model"}
        ),
        created_by="bootstrap-admin",
    )
    access = await PATTokenVerifier(runtime).verify_token(token.token)
    assert access is not None
    context = Context(mcp_server=server, subscriptions=server._subscriptions)
    auth_token = auth_context_var.set(AuthenticatedUser(access))
    try:
        with pytest.raises(MCPError, match="outside token policy"):
            await server._tool_manager.call_tool(
                "invoke_model",
                {"model_id": "qwen3-8b", "protocol": "openai-chat", "payload": {}},
                context,
                convert_result=False,
            )
    finally:
        auth_context_var.reset(auth_token)
    assert not runtime.store.operations
