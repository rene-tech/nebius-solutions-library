from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import httpx2
import pytest
import uvicorn
from conftest import CATALOG_ROOT, REPO_ROOT
from fastapi.testclient import TestClient as FastAPITestClient
from fs2_serve_catalog.loader import load_catalog
from mcp import Client, ClientSession
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.client.auth.utils import (
    credentials_match_issuer,
    validate_authorization_response_iss,
    validate_metadata_issuer,
)
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.mcpserver import Context
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata
from mcp.shared.exceptions import MCPError
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, ValidationError

from fs2_serve import cli
from fs2_serve.activation_health import activation_set
from fs2_serve.admission import AdmissionService
from fs2_serve.api import AppRuntime, _model_view, create_app
from fs2_serve.auth import OperatorSessionService, PepperRing, TokenService
from fs2_serve.mcp_server import (
    CORE_TOOLS,
    MCP_CHILD_MOUNT_PATH,
    MCP_HTTP_PATH,
    MCP_STREAMABLE_HTTP_PATH,
    MCPAuthorizationMiddleware,
    PATTokenVerifier,
    _admit,
    build_mcp_server,
    mount_mcp,
)
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import ActivationLeaderIdentity, AdmissionRequest, ClaimedOperation, Scope, TokenCreate
from fs2_serve.registry import Registry
from fs2_serve.runtime import StubRuntimeClient
from fs2_serve.settings import Settings
from fs2_serve.telemetry import Metrics

SECRET_PROMPT = "HTTP_PROMPT_MUST_NOT_LEAK_8427"
CALLER_IDENTITY = "CALLER_SUPPLIED_IDENTITY_MUST_BE_STRIPPED"


def canonical_endpoints(model_id: str) -> MappingProxyType[str, str]:
    """Read synthetic-route endpoints from the canonical base, never a test-owned map."""

    record = load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT).model(model_id).to_dict()
    return MappingProxyType(dict(record["interface"]["endpoints"]))


class TestClient(FastAPITestClient):
    """Exercise the same exact public Host authority as the configured edge."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("base_url", "https://inference.test.invalid")
        super().__init__(*args, **kwargs)


def build_runtime(
    registry,
    cipher,
    hasher,
    *,
    run_workers: bool = False,
    store: MemoryStore | None = None,
) -> AppRuntime:
    store = store or MemoryStore(cipher, hasher)
    settings = Settings(
        run_workers=run_workers,
        max_request_bytes=1024,
        public_base_url="https://inference.test.invalid",
        authorization_server_url="https://identity.test.invalid",
        allow_non_cluster_urls=False,
        catalog_dir=Path("/unused"),
        bindings_file=Path("/unused"),
    )
    metrics = Metrics(registry.list(enabled_only=True))
    admission = AdmissionService(
        registry=registry,
        store=store,
        runtime=StubRuntimeClient(),
        metrics=metrics,
        worker_concurrency=1,
        poll_seconds=0.01,
        lease_seconds=30,
        maintenance_interval_seconds=1,
        shutdown_grace_seconds=1,
    )
    peppers = PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32})
    return AppRuntime(
        settings=settings,
        registry=registry,
        store=store,
        tokens=TokenService(store, peppers),
        admission=admission,
        metrics=metrics,
        admin_token=b"a" * 32,
        operator_sessions=OperatorSessionService(store, peppers),
        owns_store=False,
    )


def publish_controller(runtime: AppRuntime, controller_id: str = "controller-a") -> None:
    assert isinstance(runtime.store, MemoryStore)
    activation_identity = activation_set(runtime.registry.list(enabled_only=True))

    async def publish() -> None:
        current = await runtime.store.current_activation_controller_fence()
        if runtime.store.activation_controller_identity is not None:
            previous = runtime.store.activation_controller_identity
            identity = previous.model_copy(
                update={"lease_resource_version": str(int(previous.lease_resource_version) + 1)}
            )
        else:
            pod_uid = f"pod-{controller_id}"
            identity = ActivationLeaderIdentity(
                pod_namespace="fs2-system",
                pod_name=controller_id,
                pod_uid=pod_uid,
                service_account_name="fs2-model-activation-controller",
                service_account_uid="ksa-activation",
                lease_namespace="fs2-system",
                lease_name="fs2-serve-activation-controller",
                lease_uid="lease-activation",
                lease_resource_version="101",
                lease_holder_identity=f"fs2:{pod_uid}",
                lease_duration_seconds=15,
                lease_renew_time=datetime.now(UTC),
                lease_observed_remaining_seconds=15.0,
            )
        await runtime.store.publish_activation_controller_heartbeat(
            identity,
            activation_set_digest=activation_identity.digest,
            lease_expires_at=await runtime.store.database_clock() + timedelta(seconds=15),
            expected_fencing_token=current,
        )

    asyncio.run(publish())


class PersistentlyUnavailableLoopStore(MemoryStore):
    """Keep DB ping healthy while both supervised admission loops fail."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.claim_calls = 0
        self.maintenance_calls = 0

    async def claim_operation(self, worker_id: str, *, lease_seconds: float) -> ClaimedOperation | None:
        del worker_id, lease_seconds
        self.claim_calls += 1
        raise RuntimeError("claim transport detail must not reach readiness")

    async def expire_deadline_operations(self) -> int:
        self.maintenance_calls += 1
        raise RuntimeError("janitor transport detail must not reach readiness")


def issue(
    client: TestClient,
    *,
    principal: str,
    scopes: list[str],
    tenant: str = "tenant-a",
    models: list[str] | None = None,
) -> str:
    response = client.post(
        "/admin/v1/tokens",
        headers={"authorization": f"Bearer {'a' * 32}"},
        json={
            "principal_id": principal,
            "tenant_id": tenant,
            "scopes": scopes,
            "models": models or ["qwen3-8b"],
            "max_concurrency": 4,
        },
    )
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["created_by"] == "bootstrap-admin"
    return value["token"]


def bound_model_registry(registry: Registry, model_id: str, *, operations: tuple[str, ...] | None = None) -> Registry:
    """Build a direct route unit fixture without creating a second catalog authority."""

    seed = registry.get("qwen3-8b")
    source = registry.get(model_id, require_enabled=False)
    policy_operations = source.gateway.policy_operations if operations is None else operations
    runtime_digest = source.gateway.runtime_image_digest
    assert runtime_digest is not None
    binding = replace(
        seed.binding,
        model_id=model_id,
        backend_service_name=model_id,
        service_origin=f"http://{model_id}.fs2-models.svc.cluster.local:8000",
        backend_gpu_class=source.gateway.gpu_class,
        backend_runtime_image_digest=runtime_digest,
        protocols=source.gateway.protocols,
        endpoints=canonical_endpoints(model_id),
        operations=policy_operations,
        mcp_tool_name=model_id.replace("-", "_"),
    )
    gateway = replace(
        source.gateway,
        policy_operations=policy_operations,
        routable=True,
        binding=binding,
    )
    return Registry(registry.catalog, {model_id: replace(source, gateway=gateway)})


def mixed_readiness_registry(registry: Registry, *, include_activation: bool = True) -> Registry:
    """Project three typed routes to exercise dependency-specific readiness."""

    seed = registry.get("qwen3-8b")
    routes = {}
    specifications = [
        ("sdxl", "local-kubernetes", False),
        ("glm-5-2-fp8", "federated", False),
    ]
    if include_activation:
        specifications.append(("qwen3-8b", "local-kubernetes", True))
    for model_id, backend_class, activation_enabled in specifications:
        source = registry.get(model_id, require_enabled=False)
        runtime_digest = source.gateway.runtime_image_digest
        assert runtime_digest is not None
        binding = replace(
            seed.binding,
            model_id=model_id,
            backend_class=backend_class,
            backend_service_name=model_id,
            service_origin=f"http://{model_id}.fs2-models.svc.cluster.local:8000",
            backend_gpu_class=source.gateway.gpu_class,
            backend_runtime_image_digest=runtime_digest,
            protocols=source.gateway.protocols,
            endpoints=canonical_endpoints(model_id),
            operations=source.gateway.policy_operations,
            activation=replace(seed.binding.activation, enabled=activation_enabled),
            mcp_tool_name=model_id.replace("-", "_"),
        )
        routes[model_id] = replace(
            source,
            gateway=replace(source.gateway, routable=True, binding=binding),
        )
    return Registry(registry.catalog, routes)


def test_activation_set_digest_binds_every_exact_contract_identity(registry) -> None:
    model = registry.get("qwen3-8b")
    baseline = activation_set([model])
    assert baseline.model_ids == ("qwen3-8b",)

    binding_changed = replace(
        model,
        gateway=replace(
            model.gateway,
            binding=replace(model.binding, binding_digest="e" * 64),
        ),
    )
    revision_changed = replace(
        model,
        gateway=replace(model.gateway, model_revision=f"{model.model_revision}-next"),
    )
    interface_changed = replace(
        model,
        gateway=replace(
            model.gateway,
            binding=replace(
                model.binding,
                activation=replace(model.binding.activation, intent_interface_sha256="d" * 64),
            ),
        ),
    )
    scale_digest = "c" * 64
    scale_changed = replace(
        model,
        gateway=replace(
            model.gateway,
            scale_contract=replace(model.gateway.scale_contract, digest=scale_digest),
            binding=replace(
                model.binding,
                activation=replace(model.binding.activation, scale_contract_digest=scale_digest),
            ),
        ),
    )
    assert (
        len(
            {
                baseline.digest,
                activation_set([binding_changed]).digest,
                activation_set([revision_changed]).digest,
                activation_set([interface_changed]).digest,
                activation_set([scale_changed]).digest,
            }
        )
        == 5
    )


@pytest.mark.parametrize(
    ("model_id", "expected_operation", "policy_scopes"),
    [
        ("qwen3-8b", "chat", []),
        ("glm-5-2-fp8", "chat", []),
        ("nv-reason-cxr-3b", "analyze-image", ["use.nonclinical", "use.noncommercial"]),
    ],
)
def test_openai_chat_resolves_the_selected_models_exact_policy_operation(
    registry,
    cipher,
    hasher,
    model_id: str,
    expected_operation: str,
    policy_scopes: list[str],
) -> None:
    runtime = build_runtime(bound_model_registry(registry, model_id), cipher, hasher)
    with TestClient(create_app(runtime)) as client:
        token = issue(
            client,
            principal=f"route-{model_id}",
            scopes=["inference.invoke", *policy_scopes],
            models=[model_id],
        )
        response = client.post(
            "/v1/chat/completions",
            headers={
                "authorization": f"Bearer {token}",
                "idempotency-key": f"route-{model_id}-0001",
                "x-fs2-wait-seconds": "0",
            },
            json={"model": model_id, "messages": [{"role": "user", "content": "bounded fixture"}]},
        )

    assert response.status_code == 202, response.text
    row = next(iter(runtime.store.operations.values()))  # type: ignore[attr-defined]
    assert row.view.model_id == model_id
    assert row.view.protocol == "openai-chat"
    assert row.view.operation == expected_operation


@pytest.mark.parametrize("operations", [(), ("analyze-image", "chat")])
def test_openai_chat_fails_closed_for_zero_or_ambiguous_policy_operations(
    registry, cipher, hasher, operations: tuple[str, ...]
) -> None:
    runtime = build_runtime(bound_model_registry(registry, "qwen3-8b", operations=operations), cipher, hasher)
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        token = issue(client, principal="route-fail-closed", scopes=["inference.invoke"])
        response = client.post(
            "/v1/chat/completions",
            headers={
                "authorization": f"Bearer {token}",
                "idempotency-key": f"route-fail-closed-{len(operations)}",
                "x-fs2-wait-seconds": "0",
            },
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "bounded fixture"}]},
        )

    assert response.status_code == 503
    assert response.json() == {"error": {"type": "route_unavailable", "message": "model route is unavailable"}}
    assert not runtime.store.operations  # type: ignore[attr-defined]


def test_api_auth_model_list_openai_admission_revoke_and_nonleak(registry, cipher, hasher, caplog) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    app = create_app(runtime)
    caplog.set_level(logging.INFO, logger="fs2_serve.access")
    with TestClient(app) as client:
        assert client.get("/v1/models").status_code == 401
        token = issue(
            client,
            principal="rene",
            scopes=[
                "catalog.read",
                "inference.invoke",
                "operations.read",
                "operations.result",
                "operations.cancel",
                "operations.acknowledge",
            ],
        )
        auth = {"authorization": f"Bearer {token}"}
        models = client.get("/v1/models", headers=auth)
        assert models.status_code == 200
        assert [model["id"] for model in models.json()["data"]] == ["qwen3-8b"]
        public_catalog = json.dumps(models.json())
        assert "service_origin" not in public_catalog and "activation_url" not in public_catalog
        assert "svc.cluster.local" not in public_catalog

        response = client.post(
            "/v1/chat/completions",
            headers={**auth, "idempotency-key": "api-openai-key-0001", "x-fs2-wait-seconds": "0"},
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": SECRET_PROMPT}]},
        )
        assert response.status_code == 202
        operation_id = response.json()["id"]
        row = next(iter(runtime.store.operations.values()))  # type: ignore[attr-defined]
        assert row.view.operation == "chat"
        assert row.view.protocol == "openai-chat"
        assert SECRET_PROMPT.encode() not in row.request.value  # type: ignore[union-attr]

        status = client.get(f"/v1/operations/{operation_id}", headers=auth)
        assert status.status_code == 200 and SECRET_PROMPT not in status.text
        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert SECRET_PROMPT not in metrics.text and token not in metrics.text

        ext = client.get(
            "/internal/ext-authz",
            headers={**auth, "x-fs2-principal": CALLER_IDENTITY},
        )
        assert ext.status_code == 200
        assert ext.headers["x-fs2-principal"] == "rene"

        token_id = next(iter(runtime.store.tokens))  # type: ignore[attr-defined]
        assert (
            client.delete(f"/admin/v1/tokens/{token_id}", headers={"authorization": f"Bearer {'a' * 32}"}).status_code
            == 200
        )
        assert client.get("/v1/models", headers=auth).status_code == 401

    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert all(secret not in rendered_logs for secret in (SECRET_PROMPT, token, CALLER_IDENTITY))


def test_ip_public_authority_is_enforced_on_v1_mcp_and_both_metadata_paths(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    runtime.settings = Settings(
        run_workers=False,
        max_request_bytes=1024,
        public_base_url="https://203.0.113.17",
        public_authority_mode="ip",
        authorization_server_url="https://identity.test.invalid",
        catalog_dir=Path("/unused"),
        bindings_file=Path("/unused"),
    )
    app = create_app(runtime)
    mount_mcp(app, runtime)

    public_requests = (
        ("GET", "/v1"),
        ("GET", "/v1/models"),
        ("POST", "/mcp"),
        ("GET", "/.well-known/oauth-protected-resource"),
        ("GET", "/.well-known/oauth-protected-resource/mcp"),
    )

    def request(client: FastAPITestClient, method: str, path: str, headers: dict[str, str]):
        if method == "POST":
            return client.post(path, headers=headers, json={})
        return client.get(path, headers=headers)

    with FastAPITestClient(app, base_url="https://203.0.113.17", follow_redirects=False) as client:
        for host, origin in (
            ("203.0.113.17", "https://203.0.113.17"),
            ("203.0.113.17:443", "https://203.0.113.17:443"),
        ):
            for method, path in public_requests:
                accepted = request(
                    client,
                    method,
                    path,
                    {
                        "host": host,
                        "origin": origin,
                        # Forwarded authority is never consulted, even when hostile.
                        "x-forwarded-host": "spoofed.invalid",
                    },
                )
                assert accepted.status_code not in {403, 421}, (method, path, accepted.text)

        for method, path in public_requests:
            wrong_authority = request(
                client,
                method,
                path,
                {"host": "spoofed.invalid", "x-forwarded-host": "203.0.113.17"},
            )
            assert wrong_authority.status_code == 421
            assert wrong_authority.text == "Invalid Host header"

            wrong_origin = request(
                client,
                method,
                path,
                {"host": "203.0.113.17", "origin": "https://spoofed.invalid"},
            )
            assert wrong_origin.status_code == 403
            assert wrong_origin.text == "Invalid Origin header"

        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.json()["resource"] == "https://203.0.113.17/mcp"


def test_terminal_metrics_project_cancel_and_revoke_exactly_once(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    with TestClient(create_app(runtime)) as client:
        token = issue(client, principal="terminal-accounting", scopes=["inference.invoke"])
        headers = {"authorization": f"Bearer {token}", "x-fs2-wait-seconds": "0"}
        first = client.post(
            "/v1/chat/completions",
            headers={**headers, "idempotency-key": "terminal-cancel-key-0001"},
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "private"}]},
        )
        assert first.status_code == 202
        assert client.post(f"/v1/operations/{first.json()['id']}:cancel", headers=headers).status_code == 200
        second = client.post(
            "/v1/chat/completions",
            headers={**headers, "idempotency-key": "terminal-revoke-key-0002"},
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "private"}]},
        )
        assert second.status_code == 202
        token_id = next(iter(runtime.store.tokens))  # type: ignore[attr-defined]
        assert (
            client.delete(f"/admin/v1/tokens/{token_id}", headers={"authorization": f"Bearer {'a' * 32}"}).status_code
            == 200
        )
        first_scrape = client.get("/metrics").text
        second_scrape = client.get("/metrics").text

    for outcome in ("cancelled", "token_revoked"):
        sample = f'fs2_serve_requests_total{{model="qwen3-8b",outcome="{outcome}",protocol="openai-chat"}} 1.0'
        assert sample in first_scrape and sample in second_scrape


def test_fatal_fenced_release_failure_flips_liveness_for_pod_restart(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher, run_workers=True)
    with TestClient(create_app(runtime)) as client:
        assert client.get("/livez").status_code == 200
        runtime.admission._fatal_workers.add("permanent-release-failure")
        response = client.get("/livez")
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "worker_fenced_release_failed"


def test_admin_created_by_is_rejected_without_echoing_value(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    app = create_app(runtime)
    secret_actor = "CLIENT_ACTOR_MUST_NOT_ENTER_AUDIT"
    with TestClient(app) as client:
        response = client.post(
            "/admin/v1/tokens",
            headers={"authorization": f"Bearer {'a' * 32}"},
            json={
                "principal_id": "user",
                "tenant_id": "tenant-a",
                "scopes": ["catalog.read"],
                "models": ["qwen3-8b"],
                "created_by": secret_actor,
            },
        )
        assert response.status_code == 422
        assert secret_actor not in response.text
        assert not runtime.store.tokens  # type: ignore[attr-defined]


def test_operation_reads_are_token_owner_scoped_with_explicit_tenant_admin_override(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    app = create_app(runtime)
    with TestClient(app) as client:
        owner = issue(
            client,
            principal="owner",
            scopes=["inference.invoke"],
        )
        admitted = client.post(
            "/v1/chat/completions",
            headers={
                "authorization": f"Bearer {owner}",
                "idempotency-key": "owner-scope-key-0001",
                "x-fs2-wait-seconds": "0",
            },
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "hello"}]},
        ).json()
        owner_status = client.get(
            f"/v1/operations/{admitted['id']}",
            headers={"authorization": f"Bearer {owner}"},
        )
        assert owner_status.status_code == 200
        assert owner_status.json()["id"] == admitted["id"]
        same_principal_other_token = issue(
            client,
            principal="owner",
            scopes=["operations.read", "operations.result", "operations.cancel", "operations.acknowledge"],
        )
        assert (
            client.get(
                f"/v1/operations/{admitted['id']}",
                headers={"authorization": f"Bearer {same_principal_other_token}"},
            ).status_code
            == 404
        )
        other = issue(client, principal="other", scopes=["operations.read", "operations.cancel"])
        assert (
            client.get(
                f"/v1/operations/{admitted['id']}",
                headers={"authorization": f"Bearer {other}"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/v1/operations/{admitted['id']}:cancel",
                headers={"authorization": f"Bearer {other}"},
            ).status_code
            == 404
        )
        admin = issue(client, principal="tenant-admin", scopes=["tenant.admin"])
        assert (
            client.get(
                f"/v1/operations/{admitted['id']}",
                headers={"authorization": f"Bearer {admin}"},
            ).status_code
            == 200
        )


def test_result_cancel_status_and_ack_are_owner_scoped_and_status_never_purges(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    runtime.settings.run_workers = True
    app = create_app(runtime)
    lifecycle_scopes = ["inference.invoke"]
    with TestClient(app) as client:
        owner = issue(client, principal="owner-result", scopes=lifecycle_scopes)
        completed = client.post(
            "/v1/chat/completions",
            headers={
                "authorization": f"Bearer {owner}",
                "idempotency-key": "owner-result-key-0001",
                "x-fs2-wait-seconds": "2",
            },
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "private-result"}]},
        )
        assert completed.status_code == 200
        operation_id = completed.headers["x-fs2-operation-id"]
        other = issue(client, principal="other-result", scopes=lifecycle_scopes)
        other_headers = {"authorization": f"Bearer {other}"}
        for method, path in (
            ("get", f"/v1/operations/{operation_id}"),
            ("get", f"/v1/operations/{operation_id}/result"),
            ("post", f"/v1/operations/{operation_id}:cancel"),
            ("post", f"/v1/operations/{operation_id}:acknowledge"),
        ):
            response = getattr(client, method)(path, headers=other_headers)
            assert response.status_code == 404
            assert "owner-result" not in response.text

        owner_headers = {"authorization": f"Bearer {owner}"}
        first_status = client.get(f"/v1/operations/{operation_id}", headers=owner_headers)
        assert first_status.status_code == 200
        assert first_status.json()["result_available"] is True
        assert "result" not in first_status.json()
        result = client.get(f"/v1/operations/{operation_id}/result", headers=owner_headers)
        assert result.status_code == 200 and result.json()["choices"]
        second_status = client.get(f"/v1/operations/{operation_id}", headers=owner_headers)
        assert second_status.json()["result_available"] is True

        cancelled = client.post(f"/v1/operations/{operation_id}:cancel", headers=owner_headers)
        assert cancelled.status_code == 200
        assert "choices" not in cancelled.text and "result" not in cancelled.json()
        assert cancelled.json()["result_available"] is True
        acknowledged = client.post(f"/v1/operations/{operation_id}:acknowledge", headers=owner_headers)
        assert acknowledged.status_code == 200
        assert acknowledged.json()["result_available"] is False
        assert client.get(f"/v1/operations/{operation_id}/result", headers=owner_headers).status_code == 409


def test_readiness_accepts_fail_closed_zero_routes_and_gates_enabled_routes(registry, cipher, hasher) -> None:
    missing_controller = build_runtime(registry, cipher, hasher)
    with TestClient(create_app(missing_controller)) as client:
        unavailable = client.get("/readyz")
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["type"] == "activation_controller_unavailable"

    runtime = build_runtime(registry, cipher, hasher)
    publish_controller(runtime)
    runtime.settings.run_workers = True
    with TestClient(create_app(runtime)) as client:
        response = client.get("/readyz")
        for _ in range(100):
            if response.status_code == 200:
                break
            time.sleep(0.01)
            response = client.get("/readyz")
        assert response.status_code == 200
        value = response.json()
        assert value["models"] == 1
        assert value["activation"] == {"required": True, "ready": True}
        assert value["admission"]["workers_healthy"] is True
        assert value["admission"]["janitor_healthy"] is True
        assert value["federation"] == {"ready": True, "routes": 0, "circuits": {}}

    disabled_runtime = build_runtime(registry, cipher, hasher)
    qwen = registry.get("qwen3-8b")
    disabled_runtime.registry = Registry(
        registry.catalog,
        {qwen.id: replace(qwen, gateway=replace(qwen.gateway, routable=False))},
    )
    disabled_runtime.admission.registry = disabled_runtime.registry
    assert len(disabled_runtime.registry.list()) == 1
    assert len(disabled_runtime.registry.list(enabled_only=True)) == 0
    with TestClient(create_app(disabled_runtime)) as client:
        disabled = client.get("/readyz")
        assert disabled.status_code == 200
        assert disabled.json() == {
            "status": "ready",
            "models": 0,
            "route_evidence": {
                "healthy": True,
                "generation": 1,
                "checked_at": None,
            },
            "activation": {"required": False, "ready": None},
            "admission": None,
            "federation": {"ready": True, "routes": 0, "circuits": {}},
        }
        token = issue(
            client,
            principal="bootstrap-owner",
            scopes=[Scope.CATALOG_READ.value, Scope.INFERENCE_INVOKE.value],
        )
        headers = {"authorization": f"Bearer {token}"}
        assert client.get("/v1/models", headers=headers).json()["data"] == []
        for path, payload in (
            (
                "/v1/chat/completions",
                {"model": "qwen3-8b", "messages": [{"role": "user", "content": SECRET_PROMPT}]},
            ),
            (
                "/v1/models/qwen3-8b:invoke",
                {"operation": "chat", "payload": {"input": SECRET_PROMPT}},
            ),
        ):
            unavailable_model = client.post(path, headers=headers, json=payload)
            assert unavailable_model.status_code in {404, 503}
            assert unavailable_model.json()["error"]["type"] in {
                "not_found",
                "route_evidence_unavailable",
                "unavailable",
            }
            assert SECRET_PROMPT not in unavailable_model.text

    federation_runtime = build_runtime(registry, cipher, hasher)
    publish_controller(federation_runtime)

    async def open_federation_circuit() -> dict[str, object]:
        return {"ready": False, "routes": 1, "circuits": {"qwen3-8b": "open"}}

    federation_runtime.admission.runtime.federation_health = open_federation_circuit  # type: ignore[method-assign]
    with TestClient(create_app(federation_runtime)) as client:
        unavailable = client.get("/readyz")
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["type"] == "federation_unavailable"
        assert "qwen3-8b" not in unavailable.text


def test_readiness_gates_only_validated_local_activation_in_mixed_routes(registry, cipher, hasher) -> None:
    mixed = mixed_readiness_registry(registry)
    absent_runtime = build_runtime(mixed, cipher, hasher)
    with TestClient(create_app(absent_runtime)) as client:
        absent = client.get("/readyz")
        assert absent.status_code == 503
        assert absent.json()["error"]["type"] == "activation_controller_unavailable"

    ready_runtime = build_runtime(mixed, cipher, hasher)
    publish_controller(ready_runtime)
    with TestClient(create_app(ready_runtime)) as client:
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["activation"] == {"required": True, "ready": True}
        assert ready.json()["models"] == 3

    models = {model.id: model for model in mixed.list()}
    qwen = models["qwen3-8b"]
    models[qwen.id] = replace(
        qwen,
        gateway=replace(
            qwen.gateway,
            binding=replace(qwen.binding, binding_digest="f" * 64),
        ),
    )
    ready_runtime.registry = Registry(mixed.catalog, models)
    with TestClient(create_app(ready_runtime)) as client:
        stale_generation = client.get("/readyz")
        assert stale_generation.status_code == 503
        assert stale_generation.json()["error"]["type"] == "activation_controller_unavailable"
    publish_controller(ready_runtime, controller_id="controller-b")
    with TestClient(create_app(ready_runtime)) as client:
        recovered = client.get("/readyz")
        assert recovered.status_code == 200
        assert recovered.json()["activation"] == {"required": True, "ready": True}

    refreshed_models = {model.id: model for model in ready_runtime.registry.list()}
    qwen = refreshed_models["qwen3-8b"]
    refreshed_models[qwen.id] = replace(
        qwen,
        gateway=replace(qwen.gateway, model_revision=f"{qwen.model_revision}-next"),
    )
    ready_runtime.registry = Registry(mixed.catalog, refreshed_models)
    with TestClient(create_app(ready_runtime)) as client:
        stale_generation = client.get("/readyz")
        assert stale_generation.status_code == 503
        assert stale_generation.json()["error"]["type"] == "activation_controller_unavailable"
    publish_controller(ready_runtime, controller_id="controller-c")
    with TestClient(create_app(ready_runtime)) as client:
        assert client.get("/readyz").status_code == 200

    ready_runtime.store.activation_controller_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)  # type: ignore[attr-defined]
    with TestClient(create_app(ready_runtime)) as client:
        expired = client.get("/readyz")
        assert expired.status_code == 503
        assert expired.json()["error"]["type"] == "activation_controller_unavailable"

    no_scaler_runtime = build_runtime(mixed_readiness_registry(registry, include_activation=False), cipher, hasher)
    with TestClient(create_app(no_scaler_runtime)) as client:
        independent = client.get("/readyz")
        assert independent.status_code == 200
        assert independent.json()["activation"] == {"required": False, "ready": None}
        assert independent.json()["models"] == 2


def test_public_application_has_no_activation_mutation_endpoint(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    with TestClient(create_app(runtime)) as client:
        assert client.post("/internal/activate/qwen3-8b", json={}).status_code == 404
        assert "/internal/activate/{model_id}" not in client.get("/openapi.json").text


def test_readiness_fails_closed_during_persistent_supervisor_failures(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher, run_workers=True)
    runtime.admission.maintenance_interval_seconds = 0.01
    unavailable_store = PersistentlyUnavailableLoopStore(cipher, hasher)
    runtime.store = unavailable_store
    runtime.admission.store = unavailable_store
    publish_controller(runtime)

    with TestClient(create_app(runtime)) as client:
        for _ in range(100):
            if unavailable_store.claim_calls >= 2 and unavailable_store.maintenance_calls >= 2:
                break
            time.sleep(0.01)

        assert unavailable_store.claim_calls >= 2
        assert unavailable_store.maintenance_calls >= 2
        assert asyncio.run(unavailable_store.ping()) is True
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {
            "error": {
                "type": "worker_unavailable",
                "message": "admission worker or janitor is unavailable",
            }
        }
        assert "transport detail" not in response.text


def test_request_limit_and_unknown_model_errors_do_not_echo_payload(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    app = create_app(runtime)
    with TestClient(app) as client:
        token = issue(client, principal="user", scopes=["inference.invoke"])
        auth = {"authorization": f"Bearer {token}", "idempotency-key": "bounded-body-key-0001"}
        too_large = client.post("/v1/chat/completions", headers=auth, content=b"x" * 1025)
        assert too_large.status_code == 413
        secret_model = "PROMPT_SHAPED_SECRET_MODEL_IDENTIFIER"
        unknown = client.post(
            "/v1/chat/completions",
            headers=auth,
            json={"model": secret_model, "messages": [{"role": "user", "content": "hello"}]},
        )
        assert unknown.status_code == 404
        assert secret_model not in unknown.text


def test_nonfinite_and_out_of_range_wait_and_deadline_headers_are_rejected(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    app = create_app(runtime)
    with TestClient(app) as client:
        token = issue(client, principal="numeric-bounds", scopes=["inference.invoke"])
        base_headers = {
            "authorization": f"Bearer {token}",
            "idempotency-key": "numeric-bounds-key-0001",
        }
        payload = {"model": "qwen3-8b", "messages": [{"role": "user", "content": "hello"}]}
        for value in ("nan", "NaN", "inf", "-inf", "Infinity", "-0.01", "30.01"):
            response = client.post(
                "/v1/chat/completions",
                headers={**base_headers, "x-fs2-wait-seconds": value},
                json=payload,
            )
            assert response.status_code == 400
            assert value not in response.text
        for value in ("nan", "NaN", "inf", "-inf", "Infinity", "0", "-0.01", "86400.01"):
            response = client.post(
                "/v1/chat/completions",
                headers={
                    **base_headers,
                    "x-fs2-wait-seconds": "0",
                    "x-fs2-deadline-seconds": value,
                },
                json=payload,
            )
            assert response.status_code == 400
            assert value not in response.text
    assert not runtime.store.operations  # type: ignore[attr-defined]


def test_external_identifiers_are_bounded_and_datetimes_require_timezones(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    app = create_app(runtime)
    oversized_model = "m" * 129
    oversized_idempotency_key = "i" * 201
    naive_expiry = "2030-01-01T00:00:00"
    with TestClient(app) as client:
        token = issue(client, principal="identifier-bounds", scopes=["inference.invoke"])
        auth = {"authorization": f"Bearer {token}"}
        model_response = client.post(
            "/v1/chat/completions",
            headers={**auth, "idempotency-key": "model-bound-key-0001"},
            json={"model": oversized_model, "messages": [{"role": "user", "content": "hello"}]},
        )
        assert model_response.status_code == 400
        assert oversized_model not in model_response.text
        key_response = client.post(
            "/v1/chat/completions",
            headers={**auth, "idempotency-key": oversized_idempotency_key},
            json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "hello"}]},
        )
        assert key_response.status_code == 400
        assert oversized_idempotency_key not in key_response.text
        expiry_response = client.post(
            "/admin/v1/tokens",
            headers={"authorization": f"Bearer {'a' * 32}"},
            json={
                "principal_id": "naive-expiry",
                "tenant_id": "tenant-a",
                "scopes": ["catalog.read"],
                "models": ["qwen3-8b"],
                "expires_at": naive_expiry,
            },
        )
        assert expiry_response.status_code == 422
        assert naive_expiry not in expiry_response.text
    assert not runtime.store.operations  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        AdmissionRequest(
            model_id="qwen3-8b",
            operation="chat",
            protocol="openai-chat",
            idempotency_key="naive-deadline-key-0001",
            request_body=b"{}",
            deadline_at=datetime(2030, 1, 1),
        )


def test_mcp_discovery_is_protocol_specific_and_includes_async_lifecycle_tools(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    server = build_mcp_server(runtime)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert CORE_TOOLS <= names
    assert "qwen3_8b_openai_chat" in names
    assert "qwen3_8b_native" not in names
    app = server.streamable_http_app(
        streamable_http_path=MCP_HTTP_PATH,
        stateless_http=True,
        max_request_body_size=1024,
    )
    assert app is not None
    assert any(route.path == MCP_HTTP_PATH for route in app.routes)
    assert server.session_manager.stateless is True


def test_mcp_tool_metadata_uses_the_same_runtime_and_qualification_projection(registry, cipher, hasher) -> None:
    source = registry.get("qwen3-8b")
    projection = MappingProxyType(
        {
            "qualification_authority": "reviewed-retained-evidence",
            "observed_at": "2026-08-30T13:44:39Z",
            "runtime_origin": {
                "variant_id": None,
                "kind": "independent-runtime",
                "source_kind": "huggingface",
                "repository": "Qwen/Qwen3-8B",
                "relationship": "canonical-runtime",
                "nim_artifact_parity": "not-applicable",
            },
            "states": {
                "registered": True,
                "route_active": True,
                "runtime_ready": True,
                "semantic_qualified": True,
                "http_mcp_qualified": True,
                "cold_start_qualified": True,
                "elasticity_qualified": False,
            },
        }
    )
    projected = replace(
        source,
        gateway=replace(source.gateway, qualification=projection),
    )
    runtime = build_runtime(Registry(registry.catalog, {source.id: projected}), cipher, hasher)
    tools = asyncio.run(build_mcp_server(runtime).list_tools())
    tool = next(item for item in tools if item.name == "qwen3_8b_openai_chat")
    view = _model_view(projected)

    assert tool.meta is not None
    assert tool.meta["fs2_active_runtime"] == view["active_runtime"]
    assert tool.meta["fs2_qualification"] == view["qualification"]


def _mcp_payload(result) -> dict[str, object]:
    assert result.is_error is False
    assert isinstance(result.structured_content, dict)
    return result.structured_content


@pytest.mark.asyncio
async def test_real_streamable_http_client_uses_parent_lifespan_and_full_async_lifecycle(
    registry, cipher, hasher
) -> None:
    runtime = build_runtime(registry, cipher, hasher, run_workers=True)
    app = create_app(runtime)
    server = mount_mcp(app, runtime)
    issued = await runtime.tokens.issue(
        TokenCreate(
            principal_id="mcp-http-owner",
            tenant_id="tenant-a",
            scopes={Scope.MCP_INVOKE},
            models={"qwen3-8b"},
            max_concurrency=4,
        ),
        created_by="bootstrap-admin",
    )
    assert MCP_HTTP_PATH == MCP_STREAMABLE_HTTP_PATH == "/mcp"
    assert MCP_CHILD_MOUNT_PATH == "/"
    assert any(route.path == "" and route.app is app.state.mcp_child for route in app.routes)
    assert any(route.path == MCP_HTTP_PATH for route in app.state.mcp_child.routes)
    advertised_url = f"{runtime.settings.public_origin()}{MCP_HTTP_PATH}"
    resource_url = f"{runtime.settings.public_origin()}{MCP_HTTP_PATH}"
    assert str(server.settings.auth.resource_server_url) == resource_url
    verified = await PATTokenVerifier(runtime).verify_token(issued.token)
    assert verified is not None and verified.resource == resource_url
    assert server.session_manager.stateless is True
    security = server.session_manager.security_settings
    assert security is not None and security.enable_dns_rebinding_protection
    assert security.allowed_hosts == ["inference.test.invalid", "inference.test.invalid:443"]
    assert security.allowed_origins == [
        "https://inference.test.invalid",
        "https://inference.test.invalid:443",
    ]

    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(
        transport=transport,
        headers={
            "authorization": f"Bearer {issued.token}",
            "origin": runtime.settings.public_origin(),
        },
        base_url=runtime.settings.public_origin(),
        trust_env=False,
        follow_redirects=False,
    )
    assert server.session_manager._task_group is None  # type: ignore[attr-defined]
    async with app.router.lifespan_context(app):
        manager_task_group = server.session_manager._task_group  # type: ignore[attr-defined]
        assert manager_task_group is not None
        assert server.session_manager._task_group is manager_task_group  # type: ignore[attr-defined]
        async with client:
            metadata = await client.get("/.well-known/oauth-protected-resource")
            assert metadata.status_code == 200
            assert metadata.json()["resource"] == resource_url
            nested_metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
            assert nested_metadata.status_code == 200
            assert nested_metadata.json()["resource"] == resource_url
            assert "location" not in nested_metadata.headers
            slash = await client.post(f"{MCP_HTTP_PATH}/", json={}, headers={"x-forwarded-proto": "http"})
            assert slash.status_code == 404
            assert "location" not in slash.headers
            spoofed_host = await client.post(MCP_HTTP_PATH, headers={"host": "spoofed.invalid"}, json={})
            assert spoofed_host.status_code == 421 and spoofed_host.text == "Invalid Host header"
            spoofed_origin = await client.post(
                MCP_HTTP_PATH,
                headers={"origin": "https://spoofed.invalid"},
                json={},
            )
            assert spoofed_origin.status_code == 403 and spoofed_origin.text == "Invalid Origin header"
            assert not server.session_manager._server_instances  # type: ignore[attr-defined]
            async with streamable_http_client(advertised_url, http_client=client) as streams:
                async with ClientSession(*streams) as session:
                    initialized = await session.initialize()
                    assert initialized.server_info.name == "fs2-serve"
                    assert not server.session_manager._server_instances  # type: ignore[attr-defined]
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    assert CORE_TOOLS <= names
                    assert "qwen3_8b_openai_chat" in names

                    admitted = _mcp_payload(
                        await session.call_tool(
                            "qwen3_8b_openai_chat",
                            {
                                "payload": {
                                    "model": "qwen3-8b",
                                    "messages": [{"role": "user", "content": "private"}],
                                },
                                "idempotency_key": "mcp-http-lifecycle-0001",
                                "wait_seconds": 0,
                            },
                        )
                    )
                    operation_id = str(admitted["id"])
                    assert admitted["status"] in {"queued", "activating", "running", "succeeded"}

                    status: dict[str, object] = admitted
                    for _ in range(100):
                        status = _mcp_payload(await session.call_tool("get_operation", {"operation_id": operation_id}))
                        if status["status"] == "succeeded":
                            break
                        await asyncio.sleep(0.01)
                    assert status["status"] == "succeeded"
                    assert status["result_available"] is True
                    assert "result" not in status

                    result = _mcp_payload(
                        await session.call_tool("get_operation_result", {"operation_id": operation_id})
                    )
                    assert isinstance(result["result"], dict)
                    assert result["result"]["choices"]  # type: ignore[index]

                    acknowledged = _mcp_payload(
                        await session.call_tool("acknowledge_operation", {"operation_id": operation_id})
                    )
                    assert acknowledged["result_available"] is False
                    assert "result" not in acknowledged
        assert server.session_manager._task_group is not None  # type: ignore[attr-defined]
    assert server.session_manager._task_group is None  # type: ignore[attr-defined]
    assert not server.session_manager._server_instances  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_official_modern_client_and_raw_wire_enforce_discovery_headers_cache_and_issuer(
    registry, cipher, hasher
) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    app = create_app(runtime)
    server = mount_mcp(app, runtime)
    issued = await runtime.tokens.issue(
        TokenCreate(
            principal_id="modern-owner",
            tenant_id="tenant-a",
            scopes={Scope.MCP_INVOKE},
            models={"qwen3-8b"},
            max_concurrency=2,
        ),
        created_by="bootstrap-admin",
    )
    access = await PATTokenVerifier(runtime).verify_token(issued.token)
    assert access is not None
    assert access.claims is not None
    assert access.claims["iss"] == runtime.settings.authorization_server_url

    metadata = OAuthMetadata(
        issuer=runtime.settings.authorization_server_url,
        authorization_endpoint=f"{runtime.settings.authorization_server_url}/authorize",
        token_endpoint=f"{runtime.settings.authorization_server_url}/token",
        authorization_response_iss_parameter_supported=True,
    )
    validate_metadata_issuer(metadata, runtime.settings.authorization_server_url)
    validate_authorization_response_iss(runtime.settings.authorization_server_url, metadata)
    with pytest.raises(OAuthFlowError, match="issuer mismatch"):
        validate_metadata_issuer(metadata, "https://substitution.test.invalid")
    with pytest.raises(OAuthFlowError, match="iss mismatch"):
        validate_authorization_response_iss("https://substitution.test.invalid", metadata)
    with pytest.raises(OAuthFlowError, match="missing iss"):
        validate_authorization_response_iss(None, metadata)
    credentials = OAuthClientInformationFull(
        client_id="registered-client",
        issuer=runtime.settings.authorization_server_url,
    )
    assert credentials_match_issuer(credentials, runtime.settings.authorization_server_url, None)
    assert not credentials_match_issuer(credentials, "https://substitution.test.invalid", None)

    restricted = await runtime.tokens.issue(
        TokenCreate(
            principal_id="modern-restricted",
            tenant_id="tenant-a",
            scopes={Scope.MCP_INVOKE},
            models={"nv-reason-cxr-3b"},
            max_concurrency=1,
        ),
        created_by="bootstrap-admin",
    )
    restricted_access = await PATTokenVerifier(runtime).verify_token(restricted.token)
    assert restricted_access is not None and restricted_access.claims is not None
    assert restricted_access.claims["models"] == ["nv-reason-cxr-3b"]
    assert "nv-reason-cxr-3b" not in {model.id for model in runtime.registry.list(enabled_only=True)}

    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        headers={
            "authorization": f"Bearer {issued.token}",
            "origin": runtime.settings.public_origin(),
        },
        base_url=runtime.settings.public_origin(),
        trust_env=False,
        follow_redirects=False,
    )
    url = f"{runtime.settings.public_origin()}{MCP_HTTP_PATH}"
    async with app.router.lifespan_context(app), client:
        # Auto mode MUST negotiate server/discover, never silently downgrade to
        # the 2025 initialize handshake on a capable production endpoint.
        async with Client(streamable_http_client(url, http_client=client), mode="auto") as modern:
            assert modern.protocol_version == "2026-07-28"
            discover = modern.session.discover_result
            assert discover is not None
            assert discover.supported_versions == ["2026-07-28"]
            assert discover.ttl_ms == 0
            assert discover.cache_scope == "private"
            tools = await modern.list_tools()
            assert tools.ttl_ms == 0
            assert tools.cache_scope == "private"
            assert "qwen3_8b_openai_chat" in {tool.name for tool in tools.tools}

        # Exact mode adopts only the requested modern revision. This is a
        # separate connection so no negotiation/cache state can bleed in.
        async with Client(streamable_http_client(url, http_client=client), mode="2026-07-28") as exact:
            assert exact.protocol_version == "2026-07-28"
            tools = await exact.list_tools()
            assert tools.ttl_ms == 0 and tools.cache_scope == "private"

        # A second authorization context sees neither the first PAT's private
        # listing nor its qualified model. The zero TTL and private scope make
        # this invariant explicit to conforming caches as well as the server.
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            headers={
                "authorization": f"Bearer {restricted.token}",
                "origin": runtime.settings.public_origin(),
            },
            base_url=runtime.settings.public_origin(),
            trust_env=False,
        ) as restricted_http:
            async with Client(streamable_http_client(url, http_client=restricted_http), mode="auto") as isolated:
                isolated_tools = await isolated.list_tools()
                assert isolated_tools.ttl_ms == 0 and isolated_tools.cache_scope == "private"
                assert "qwen3_8b_openai_chat" not in {tool.name for tool in isolated_tools.tools}

        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "raw-conformance", "version": "1"},
        }
        raw_headers = {
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2026-07-28",
            "mcp-method": "server/discover",
        }
        raw_discover = await client.post(
            MCP_HTTP_PATH,
            headers=raw_headers,
            json={"jsonrpc": "2.0", "id": "discover-1", "method": "server/discover", "params": {"_meta": meta}},
        )
        assert raw_discover.status_code == 200
        assert "2026-07-28" in raw_discover.text

        mismatched_method = await client.post(
            MCP_HTTP_PATH,
            headers={**raw_headers, "mcp-method": "tools/list"},
            json={"jsonrpc": "2.0", "id": "bad-method", "method": "server/discover", "params": {"_meta": meta}},
        )
        assert mismatched_method.status_code == 400
        assert "header does not match" in mismatched_method.text

        wrong_name = await client.post(
            MCP_HTTP_PATH,
            headers={
                **raw_headers,
                "mcp-method": "tools/call",
                "mcp-name": "another_tool",
            },
            json={
                "jsonrpc": "2.0",
                "id": "bad-name",
                "method": "tools/call",
                "params": {"_meta": meta, "name": "list_models", "arguments": {}},
            },
        )
        assert wrong_name.status_code == 400
        assert "header does not match" in wrong_name.text

        missing_version = await client.post(
            MCP_HTTP_PATH,
            headers={"accept": "application/json, text/event-stream", "mcp-method": "server/discover"},
            json={"jsonrpc": "2.0", "id": "missing-version", "method": "server/discover", "params": {"_meta": meta}},
        )
        assert missing_version.status_code == 400
        assert missing_version.text == "Incomplete MCP routing headers"

        headerless_modern = await client.post(
            MCP_HTTP_PATH,
            headers={"accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": "headerless-modern", "method": "server/discover", "params": {"_meta": meta}},
        )
        assert headerless_modern.status_code == 400
        assert headerless_modern.text == "Incomplete MCP routing headers"

        # Legacy remains an explicit, isolated compatibility lane rather than
        # an automatic fallback for malformed modern envelopes.
        async with Client(streamable_http_client(url, http_client=client), mode="legacy") as legacy:
            assert legacy.protocol_version != "2026-07-28"
            assert legacy.session.discover_result is None
            assert "qwen3_8b_openai_chat" in {tool.name for tool in (await legacy.list_tools()).tools}
    assert server.session_manager._task_group is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cli_composed_app_serves_stateless_mcp_over_real_uvicorn(
    registry, cipher, hasher, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = build_runtime(registry, cipher, hasher, run_workers=True)

    async def runtime_factory(settings: Settings) -> AppRuntime:
        assert settings is runtime.settings
        return runtime

    monkeypatch.setattr(cli, "build_runtime", runtime_factory)
    app = await cli.build_app(runtime.settings)
    mcp_server = app.state.mcp_server
    assert mcp_server.session_manager.stateless is True
    root_mount = app.routes[-1]
    assert root_mount.path == "" and root_mount.app is app.state.mcp_child
    assert any(route.path == "/livez" for route in app.routes[:-1])
    assert any(route.path == "/v1/models" for route in app.routes[:-1])
    assert any(route.path == "/.well-known/oauth-protected-resource" for route in app.routes[:-1])

    issued = await runtime.tokens.issue(
        TokenCreate(
            principal_id="mcp-uvicorn-owner",
            tenant_id="tenant-a",
            scopes={Scope.MCP_INVOKE, Scope.CATALOG_READ},
            models={"qwen3-8b"},
            max_concurrency=4,
        ),
        created_by="bootstrap-admin",
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False, lifespan="on")
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    response_headers: list[dict[str, str]] = []

    async def capture_response(response: httpx2.Response) -> None:
        response_headers.append(dict(response.headers))

    try:
        for _ in range(200):
            if server.started:
                break
            if server_task.done():
                await server_task
            await asyncio.sleep(0.01)
        assert server.started
        assert mcp_server.session_manager._task_group is not None  # type: ignore[attr-defined]
        async with httpx2.AsyncClient(
            headers={
                "authorization": f"Bearer {issued.token}",
                "host": "inference.test.invalid",
                "origin": runtime.settings.public_origin(),
            },
            event_hooks={"response": [capture_response]},
            trust_env=False,
            follow_redirects=False,
        ) as client:
            base_url = f"http://127.0.0.1:{port}"
            # The MCP child is mounted at root last. These successful parent
            # responses prove Starlette route order prevents it from shadowing
            # the existing FastAPI surface in the production composition.
            live = await client.get(f"{base_url}/livez")
            assert live.status_code == 200 and live.json() == {"status": "ok"}
            models = await client.get(f"{base_url}/v1/models")
            assert models.status_code == 200
            assert "qwen3-8b" in {model["id"] for model in models.json()["data"]}
            compatibility_metadata = await client.get(f"{base_url}/.well-known/oauth-protected-resource")
            assert compatibility_metadata.status_code == 200
            assert compatibility_metadata.json()["resource"] == "https://inference.test.invalid/mcp"
            canonical_metadata = await client.get(f"{base_url}/.well-known/oauth-protected-resource/mcp")
            assert canonical_metadata.status_code == 200
            assert canonical_metadata.json()["resource"] == "https://inference.test.invalid/mcp"
            assert "location" not in canonical_metadata.headers
            spoofed_host = await client.post(f"{base_url}{MCP_HTTP_PATH}", headers={"host": "spoofed.invalid"}, json={})
            assert spoofed_host.status_code == 421
            spoofed_origin = await client.post(
                f"{base_url}{MCP_HTTP_PATH}",
                headers={"origin": "https://spoofed.invalid"},
                json={},
            )
            assert spoofed_origin.status_code == 403
            async with streamable_http_client(f"{base_url}{MCP_HTTP_PATH}", http_client=client) as streams:
                async with ClientSession(*streams) as session:
                    initialized = await session.initialize()
                    assert initialized.server_info.name == "fs2-serve"
                    tools = await session.list_tools()
                    assert "qwen3_8b_openai_chat" in {tool.name for tool in tools.tools}
                    admitted = _mcp_payload(
                        await session.call_tool(
                            "qwen3_8b_openai_chat",
                            {
                                "payload": {
                                    "model": "qwen3-8b",
                                    "messages": [{"role": "user", "content": "private"}],
                                },
                                "idempotency_key": "mcp-uvicorn-transport-0001",
                                "wait_seconds": 1,
                            },
                        )
                    )
                    assert admitted["status"] == "succeeded"
        assert response_headers
        assert all("mcp-session-id" not in headers for headers in response_headers)
        assert not mcp_server.session_manager._server_instances  # type: ignore[attr-defined]
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5)
        listener.close()
    assert mcp_server.session_manager._task_group is None  # type: ignore[attr-defined]


class AlternatingReplicaGateway:
    """Test-only Service/Gateway that sends every HTTP request to the next replica."""

    def __init__(self, *apps) -> None:
        self.apps = apps
        self.request_count = [0 for _ in apps]
        self._next = 0

    async def __call__(self, scope, receive, send) -> None:
        assert scope["type"] == "http"
        index = self._next
        self._next = (self._next + 1) % len(self.apps)
        self.request_count[index] += 1
        await self.apps[index](scope, receive, send)


@pytest.mark.asyncio
async def test_real_mcp_client_survives_cross_replica_requests_without_session_affinity(
    registry, cipher, hasher, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_store = MemoryStore(cipher, hasher)
    runtimes = [
        build_runtime(registry, cipher, hasher, run_workers=index == 0, store=shared_store) for index in range(2)
    ]
    runtimes_by_settings = {id(runtime.settings): runtime for runtime in runtimes}

    async def runtime_factory(settings: Settings) -> AppRuntime:
        return runtimes_by_settings[id(settings)]

    monkeypatch.setattr(cli, "build_runtime", runtime_factory)
    apps = [await cli.build_app(runtime.settings) for runtime in runtimes]
    servers = [app.state.mcp_server for app in apps]
    issued = await runtimes[0].tokens.issue(
        TokenCreate(
            principal_id="mcp-replica-owner",
            tenant_id="tenant-a",
            scopes={Scope.MCP_INVOKE},
            models={"qwen3-8b"},
            max_concurrency=4,
        ),
        created_by="bootstrap-admin",
    )
    gateway = AlternatingReplicaGateway(*apps)
    client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=gateway),
        headers={
            "authorization": f"Bearer {issued.token}",
            "origin": runtimes[0].settings.public_origin(),
        },
        base_url=runtimes[0].settings.public_origin(),
        trust_env=False,
        follow_redirects=False,
    )
    async with apps[0].router.lifespan_context(apps[0]), apps[1].router.lifespan_context(apps[1]), client:
        async with Client(
            streamable_http_client(f"{runtimes[0].settings.public_origin()}{MCP_HTTP_PATH}", http_client=client),
            mode="auto",
        ) as session:
            assert session.protocol_version == "2026-07-28"
            assert session.session.discover_result is not None
            tools = await session.list_tools()
            assert "qwen3_8b_openai_chat" in {tool.name for tool in tools.tools}
            admitted = _mcp_payload(
                await session.call_tool(
                    "qwen3_8b_openai_chat",
                    {
                        "payload": {"model": "qwen3-8b", "messages": [{"role": "user", "content": "private"}]},
                        "idempotency_key": "mcp-cross-replica-key-0001",
                        "wait_seconds": 0,
                    },
                )
            )
            operation_id = str(admitted["id"])
            status = admitted
            for _ in range(100):
                status = _mcp_payload(await session.call_tool("get_operation", {"operation_id": operation_id}))
                if status["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            assert status["status"] == "succeeded"
            result = _mcp_payload(await session.call_tool("get_operation_result", {"operation_id": operation_id}))
            assert result["result"]["choices"]  # type: ignore[index]
            acknowledged = _mcp_payload(
                await session.call_tool("acknowledge_operation", {"operation_id": operation_id})
            )
            assert acknowledged["result_available"] is False
    assert all(count > 0 for count in gateway.request_count)
    assert all(server.session_manager.stateless for server in servers)
    assert all(not server.session_manager._server_instances for server in servers)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_mcp_invocation_uses_admission_and_result_survives_status_cancel_until_explicit_ack(
    registry, cipher, hasher
) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    server = build_mcp_server(runtime)
    issued = await runtime.tokens.issue(
        TokenCreate(
            principal_id="mcp-owner",
            tenant_id="tenant-a",
            scopes={Scope.MCP_INVOKE},
            models={"qwen3-8b"},
            max_concurrency=4,
        ),
        created_by="bootstrap-admin",
    )
    access = await PATTokenVerifier(runtime).verify_token(issued.token)
    assert access is not None
    context = Context(mcp_server=server, subscriptions=server._subscriptions)  # type: ignore[attr-defined]
    auth_token = auth_context_var.set(AuthenticatedUser(access))
    await runtime.admission.start()
    try:
        admitted = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "qwen3_8b_openai_chat",
            {
                "payload": {"model": "qwen3-8b", "messages": [{"role": "user", "content": "private"}]},
                "idempotency_key": "mcp-route-state-key-0001",
                "wait_seconds": 2,
            },
            context,
            convert_result=False,
        )
        operation_id = admitted["id"]
        assert admitted["status"] == "succeeded" and admitted["result_available"] is True
        assert "result" not in admitted

        result = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "get_operation_result", {"operation_id": operation_id}, context, convert_result=False
        )
        assert result["result"]["choices"]
        status = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "get_operation", {"operation_id": operation_id}, context, convert_result=False
        )
        assert status["result_available"] is True and "result" not in status

        other_issued = await runtime.tokens.issue(
            TokenCreate(
                principal_id="mcp-other",
                tenant_id="tenant-a",
                scopes={Scope.MCP_INVOKE},
                models={"qwen3-8b"},
            ),
            created_by="bootstrap-admin",
        )
        other_access = await PATTokenVerifier(runtime).verify_token(other_issued.token)
        assert other_access is not None
        auth_context_var.reset(auth_token)
        other_context = auth_context_var.set(AuthenticatedUser(other_access))
        try:
            with pytest.raises(MCPError, match="operation not found"):
                await server._tool_manager.call_tool(  # type: ignore[attr-defined]
                    "get_operation", {"operation_id": operation_id}, context, convert_result=False
                )
        finally:
            auth_context_var.reset(other_context)
        auth_token = auth_context_var.set(AuthenticatedUser(access))

        cancelled = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "cancel_operation", {"operation_id": operation_id}, context, convert_result=False
        )
        assert cancelled["result_available"] is True and "result" not in cancelled
        acknowledged = await server._tool_manager.call_tool(  # type: ignore[attr-defined]
            "acknowledge_operation", {"operation_id": operation_id}, context, convert_result=False
        )
        assert acknowledged["result_available"] is False and "result" not in acknowledged
    finally:
        auth_context_var.reset(auth_token)
        await runtime.admission.close()


@pytest.mark.asyncio
async def test_mcp_rejects_nonfinite_wait_and_oversized_idempotency_before_admission(registry, cipher, hasher) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    issued = await runtime.tokens.issue(
        TokenCreate(
            principal_id="mcp-bounds",
            tenant_id="tenant-a",
            scopes={Scope.MCP_INVOKE},
            models={"qwen3-8b"},
        ),
        created_by="bootstrap-admin",
    )
    access = await PATTokenVerifier(runtime).verify_token(issued.token)
    assert access is not None
    auth_token = auth_context_var.set(AuthenticatedUser(access))
    try:
        for wait_seconds in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(MCPError, match="configured bound"):
                await _admit(
                    runtime,
                    model_id="qwen3-8b",
                    protocol="openai-chat",
                    operation="chat",
                    payload={"model": "qwen3-8b"},
                    idempotency_key="mcp-numeric-bound-0001",
                    wait_seconds=wait_seconds,
                    traceparent=None,
                )
        with pytest.raises(MCPError, match="idempotency_key length"):
            await _admit(
                runtime,
                model_id="qwen3-8b",
                protocol="openai-chat",
                operation="chat",
                payload={"model": "qwen3-8b"},
                idempotency_key="i" * 201,
                wait_seconds=0,
                traceparent=None,
            )
    finally:
        auth_context_var.reset(auth_token)
    assert not runtime.store.operations  # type: ignore[attr-defined]


def test_validation_and_telemetry_source_has_explicit_payload_nonleak_guards() -> None:
    from conftest import CONTROL_ROOT

    source = (CONTROL_ROOT / "src" / "fs2_serve" / "mcp_server.py").read_text(encoding="utf-8")
    assert 'request validation failed") from None' in source
    api_source = (CONTROL_ROOT / "src" / "fs2_serve" / "api.py").read_text(encoding="utf-8")
    assert 'error.get("input"' not in api_source
    assert "authorization" not in json.dumps(
        {"metric_labels": ["model", "protocol", "outcome", "gpu_class", "preemptible"]}
    )


@pytest.mark.asyncio
async def test_mcp_validation_exception_chain_logs_and_trace_never_capture_input(
    registry, cipher, hasher, caplog
) -> None:
    runtime = build_runtime(registry, cipher, hasher)
    middleware = MCPAuthorizationMiddleware(runtime)
    secret = "MCP_PROMPT_SHAPED_VALIDATION_SECRET_4761"

    class BoundedInput(BaseModel):
        count: int

    async def rejected(_):
        BoundedInput.model_validate({"count": secret})
        raise AssertionError("validation must fail")

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("fs2-serve-mcp-nonleak-test")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(MCPError) as captured:
        with tracer.start_as_current_span("mcp-validation"):
            await middleware(SimpleNamespace(method="initialize", params={}), rejected)  # type: ignore[arg-type]

    rendered_exception = "".join(traceback.format_exception(captured.value))
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    rendered_spans = repr(exporter.get_finished_spans())
    assert "request validation failed" in str(captured.value)
    assert captured.value.__cause__ is None and captured.value.__suppress_context__ is True
    assert all(secret not in rendered for rendered in (rendered_exception, rendered_logs, rendered_spans))
