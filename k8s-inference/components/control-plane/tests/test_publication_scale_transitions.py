"""Durable demand remains admissible while an unchanged runtime converges."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.mcpserver import Context
from test_api_mcp import TestClient, build_runtime, issue
from test_dynamic_routes import _revision
from test_model_deployment_bridge import FakeKubernetes, _ready_cr
from test_model_deployment_publication import revision, status_view

from fs2_serve.api import create_app
from fs2_serve.mcp_server import PATTokenVerifier, build_mcp_server
from fs2_serve.model_deployment import DesiredState, spec_digest
from fs2_serve.model_deployment_admin import StoreModelDeploymentRepository
from fs2_serve.model_deployment_bridge import ModelDeploymentRuntimeBridge
from fs2_serve.model_deployment_publication import (
    PublicationDisposition,
    PublicationReason,
    assess_model_publication,
)
from fs2_serve.model_deployment_records import (
    KubernetesConditionStatus,
    ModelDeploymentAppendRequest,
    ModelDeploymentConditionType,
    ModelDeploymentRevisionAction,
    ModelDeploymentRuntimePhase,
    ModelDeploymentStatusAvailability,
)


def test_desired_requires_observed_progressing_and_does_not_claim_ready():
    item = revision()
    observed = status_view(item, phase=ModelDeploymentRuntimePhase.DESIRED)
    result = assess_model_publication(item, observed)
    assert result.disposition is PublicationDisposition.PUBLISH
    assert result.reason is PublicationReason.ACTIVATABLE
    assert result.publication is not None and not result.publication.runtime_ready
    assert result.publication.etag == item.etag
    assert result.publication.endpoint == observed.observation.status.endpoint


@pytest.mark.parametrize(
    "fence",
    [
        "disabled",
        "draining-desired",
        "exposure-disabled",
        "unavailable",
        "stale",
        "missing-status",
        "new-revision",
        "wrong-name",
        "wrong-tenant",
        "wrong-spec",
        "unobserved-generation",
        "missing-condition",
        "false-condition",
        "old-condition",
        "wrong-condition",
        "missing-endpoint",
        "wrong-endpoint-namespace",
        "missing-pool",
        "failed",
        "draining",
        "infrastructure",
    ],
)
def test_desired_transition_does_not_weaken_existing_publication_fences(fence):
    item = revision()
    view = status_view(item, phase=ModelDeploymentRuntimePhase.DESIRED)
    observation = view.observation
    status = observation.status
    if fence in {"disabled", "draining-desired"}:
        state = DesiredState.DISABLED if fence == "disabled" else DesiredState.DRAINING
        spec = item.spec.model_copy(
            update={"lifecycle": item.spec.lifecycle.model_copy(update={"desired_state": state})}
        )
        item = item.model_copy(update={"spec": spec, "etag": spec_digest(spec)})
    elif fence == "exposure-disabled":
        spec = item.spec.model_copy(
            update={"exposure": item.spec.exposure.model_copy(update={"open_ai": False, "mcp": False})}
        )
        item = item.model_copy(update={"spec": spec, "etag": spec_digest(spec)})
    elif fence in {"unavailable", "stale"}:
        view = view.model_copy(update={"state": ModelDeploymentStatusAvailability(fence)})
    elif fence == "missing-status":
        view = None
    elif fence == "new-revision":
        item = item.model_copy(update={"revision": item.revision + 1})
    elif fence == "wrong-name":
        observation = observation.model_copy(update={"name": "different-app"})
    elif fence == "wrong-tenant":
        observation = observation.model_copy(update={"tenant_id": "different-tenant"})
    elif fence == "wrong-spec":
        status = status.model_copy(update={"spec_digest": "sha256:" + "f" * 64})
    elif fence == "unobserved-generation":
        status = status.model_copy(update={"observed_generation": 0})
    elif fence == "missing-condition":
        status = status.model_copy(update={"conditions": []})
    elif fence in {"false-condition", "old-condition", "wrong-condition"}:
        update = {
            "false-condition": {"status": KubernetesConditionStatus.FALSE},
            "old-condition": {"observed_generation": status.observed_generation - 1},
            "wrong-condition": {"type": ModelDeploymentConditionType.READY},
        }[fence]
        status = status.model_copy(update={"conditions": [status.conditions[0].model_copy(update=update)]})
    elif fence == "missing-endpoint":
        status = status.model_copy(update={"endpoint": None})
    elif fence == "wrong-endpoint-namespace":
        status = status.model_copy(update={"endpoint": status.endpoint.model_copy(update={"namespace": "other"})})
    elif fence == "missing-pool":
        status = status.model_copy(update={"admitted_pool_ref": None})
    else:
        phase = {
            "failed": ModelDeploymentRuntimePhase.FAILED,
            "draining": ModelDeploymentRuntimePhase.DRAINING,
            "infrastructure": ModelDeploymentRuntimePhase.INFRASTRUCTURE_REQUIRED,
        }[fence]
        status = status.model_copy(update={"phase": phase})
    if view is not None:
        view = view.model_copy(update={"observation": observation.model_copy(update={"status": status})})
    result = assess_model_publication(item, view)
    assert result.disposition is PublicationDisposition.WITHDRAW
    assert result.publication is None


def test_same_revision_scale_handoff_keeps_http_mcp_discovery_and_durable_queued_admission(registry, cipher, hasher):
    runtime = build_runtime(registry, cipher, hasher, run_workers=False)
    proposed = _revision(registry)
    item = asyncio.run(
        runtime.store.model_deployment_append_revision(
            ModelDeploymentAppendRequest(
                namespace=proposed.namespace,
                name=proposed.name,
                expected_etag=None,
                spec=proposed.spec,
                action=ModelDeploymentRevisionAction.CREATE,
                actor_id=uuid4(),
                actor="test@example.test",
                idempotency_key="scale-transition-model-create",
            )
        )
    ).value
    source = FakeKubernetes()
    bridge = ModelDeploymentRuntimeBridge(
        repository=StoreModelDeploymentRepository(runtime.store),
        writer=source,
        source=source,
        registry=registry,
    )
    server = build_mcp_server(runtime)

    async def mcp_call(token, name, arguments):
        access = await PATTokenVerifier(runtime).verify_token(token)
        assert access is not None
        context = Context(mcp_server=server, subscriptions=server._subscriptions)
        auth = auth_context_var.set(AuthenticatedUser(access))
        try:
            return await server._tool_manager.call_tool(name, arguments, context, convert_result=False)
        finally:
            auth_context_var.reset(auth)

    operations = []
    with TestClient(create_app(runtime)) as client:
        token = issue(client, principal="transition-test", scopes=["catalog.read", "inference.invoke", "mcp.invoke"])
        auth = {"authorization": f"Bearer {token}"}
        for index, phase in enumerate(("Cold", "Desired", "Ready", "Desired", "Cold")):
            raw = _ready_cr(item)
            raw["metadata"]["resourceVersion"] = str(100 + index)
            raw["status"]["phase"] = phase
            raw["status"]["lastReconcileTime"] = datetime.now(UTC).isoformat()
            raw["status"]["conditions"][0].update(
                type={"Cold": "Cold", "Desired": "Progressing", "Ready": "Ready"}[phase],
                reason="Reconciling" if phase == "Desired" else "Observed",
            )
            source.models = [raw]
            assert asyncio.run(bridge.refresh(force=True))
            model = registry.get("qwen3-8b")
            assert model.model_revision == f"dynamic:{item.etag}"
            assert model.binding.ready is (phase == "Ready")
            listed = client.get("/v1/models", headers=auth)
            assert listed.status_code == 200
            assert "qwen3-8b" in {row["id"] for row in listed.json()["data"]}
            listed_mcp = asyncio.run(mcp_call(token, "list_models", {}))
            assert "qwen3-8b" in {row["id"] for row in listed_mcp["data"]}
            if index == 1:
                response = client.post(
                    "/v1/chat/completions",
                    headers={**auth, "idempotency-key": "desired-http-demand", "x-fs2-wait-seconds": "0"},
                    json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "bounded fixture"}]},
                )
                assert response.status_code == 202, response.text
                operations.append(response.json())
            elif index == 3:
                result = asyncio.run(
                    mcp_call(
                        token,
                        "invoke_model",
                        {
                            "model_id": "qwen3-8b",
                            "protocol": "openai-chat",
                            "payload": {"messages": [{"role": "user", "content": "second bounded fixture"}]},
                            "idempotency_key": "desired-mcp-demand",
                            "wait_seconds": 0,
                        },
                    )
                )
                assert not result.is_error
                operations.append(result.structured_content)
    assert len(operations) == 2
    assert len({row["id"] for row in operations}) == 2
    assert all(row["status"] == "queued" and row["attempt"] == 0 for row in operations)
    assert len(runtime.store.operations) == 2
    assert source.applied == []
