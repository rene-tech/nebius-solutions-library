from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from fs2_serve.admin import (
    AdminContextConfig,
    AdminProblemError,
    AdminReadService,
    CachedKubernetesAdminAdapter,
    PrometheusQueryTemplates,
    UnavailablePrometheusAdminAdapter,
    derive_model_state,
)
from fs2_serve.admin_models import (
    AdminContextOption,
    AdminKubernetesModel,
    AdminKubernetesSnapshot,
    AdminMeasurement,
    AdminModelState,
    AdminPrometheusModel,
    AdminPrometheusSnapshot,
    AdminValueState,
)
from fs2_serve.admission import AdmissionService
from fs2_serve.api import AppRuntime, create_app
from fs2_serve.auth import OperatorSessionService, PepperRing, TokenService
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import AdmissionRequest, OperationStatus, RuntimeIdentity, Scope, TokenCreate
from fs2_serve.registry import Registry
from fs2_serve.runtime import StubRuntimeClient
from fs2_serve.settings import Settings
from fs2_serve.telemetry import Metrics

FIXED_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
ADMIN_AUTH = {"authorization": f"Bearer {'a' * 32}"}
SECRET_PROMPT = "ADMIN_BFF_PROMPT_MUST_NOT_LEAK_9031"
SECRET_ERROR = "ADMIN_BFF_BACKEND_ERROR_MUST_NOT_LEAK_9031"
SECRET_IDEMPOTENCY = "ADMIN_BFF_IDEMPOTENCY_MUST_NOT_LEAK_9031"


class FakeKubernetesAdapter:
    def __init__(
        self,
        observed_at: datetime = FIXED_NOW,
        *,
        desired_replicas: int = 1,
        ready_replicas: int = 1,
        semantic_healthy: bool | None = True,
    ) -> None:
        self.observed_at = observed_at
        self.desired_replicas = desired_replicas
        self.ready_replicas = ready_replicas
        self.semantic_healthy = semantic_healthy
        self.calls = 0

    async def snapshot(self, model_ids: tuple[str, ...]) -> AdminKubernetesSnapshot:
        self.calls += 1
        return AdminKubernetesSnapshot(
            observed_at=self.observed_at,
            models=[
                AdminKubernetesModel(
                    model_id=model_id,
                    desired_replicas=self.desired_replicas if model_id == "qwen3-8b" else 0,
                    ready_replicas=self.ready_replicas if model_id == "qwen3-8b" else 0,
                    semantic_healthy=self.semantic_healthy,
                )
                for model_id in model_ids
            ],
            allocatable_gpus=38,
            ready_gpu_nodes=10,
            preemptible_gpu_nodes=10,
            active_gpu_replicas=16,
        )


class FakePrometheusAdapter:
    def __init__(self, observed_at: datetime = FIXED_NOW) -> None:
        self.observed_at = observed_at

    async def snapshot(
        self, model_ids: tuple[str, ...], *, from_at: datetime, to_at: datetime
    ) -> AdminPrometheusSnapshot:
        del from_at, to_at
        return AdminPrometheusSnapshot(
            observed_at=self.observed_at,
            models=[
                AdminPrometheusModel(
                    model_id=model_id,
                    requests_per_second=0.25 if model_id == "qwen3-8b" else 0,
                    terminal_operations=0,
                    error_rate=0,
                    latency_p50_seconds=0.4,
                    latency_p95_seconds=0.8,
                    latency_p99_seconds=1.2,
                )
                for model_id in model_ids
            ],
            requests_per_second=0.25,
            terminal_operations=0,
            error_rate=0,
            latency_p50_seconds=0.4,
            latency_p95_seconds=0.8,
            latency_p99_seconds=1.2,
        )


class FailingKubernetesAdapter:
    async def snapshot(self, model_ids: tuple[str, ...]) -> AdminKubernetesSnapshot:
        del model_ids
        raise RuntimeError("SENSITIVE_KUBERNETES_TRANSPORT_DETAIL")


def _contexts() -> AdminContextConfig:
    return AdminContextConfig(
        options=(
            AdminContextOption(
                project="project-test",
                cluster="cluster-test",
                region="region-test",
                label="Test cluster",
            ),
        )
    )


def _runtime(registry: Any, cipher: Any, hasher: Any, *, failing_kubernetes: bool = False) -> AppRuntime:
    store = MemoryStore(cipher, hasher)
    settings = Settings(
        run_workers=False,
        max_request_bytes=1024,
        public_base_url="https://inference.test.invalid",
        authorization_server_url="https://identity.test.invalid",
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
    admin_read = AdminReadService(
        registry=registry,
        store=store,
        kubernetes=FailingKubernetesAdapter() if failing_kubernetes else FakeKubernetesAdapter(),
        prometheus=FakePrometheusAdapter(),
        contexts=_contexts(),
        clock=lambda: FIXED_NOW,
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
        admin_read=admin_read,
    )


def _client(runtime: AppRuntime, *, authenticated: bool = True) -> TestClient:
    client = TestClient(create_app(runtime), base_url="https://inference.test.invalid")
    if authenticated:
        response = client.post("/admin/api/v1/session", headers=ADMIN_AUTH)
        assert response.status_code == 200, response.text
    return client


def _seed_operations(runtime: AppRuntime) -> tuple[list[str], list[str]]:
    assert isinstance(runtime.store, MemoryStore)

    async def seed() -> tuple[list[str], list[str]]:
        issued = await runtime.tokens.issue(
            TokenCreate(
                principal_id="operator-visible-principal",
                tenant_id="tenant-visible",
                scopes={Scope.INFERENCE_INVOKE},
                models={"qwen3-8b"},
                max_concurrency=10,
            ),
            created_by="test-admin",
        )
        principal = await runtime.tokens.verify(issued.token)
        identifiers: list[str] = []
        for ordinal, status in enumerate(
            (OperationStatus.SUCCEEDED, OperationStatus.FAILED, OperationStatus.SUCCEEDED),
            start=1,
        ):
            operation = await runtime.store.append_operation(
                principal=principal,
                admission=AdmissionRequest(
                    model_id="qwen3-8b",
                    operation="chat",
                    protocol="openai-chat",
                    idempotency_key=f"{SECRET_IDEMPOTENCY}-{ordinal}",
                    request_body=SECRET_PROMPT.encode(),
                ),
                model_revision="sha256:" + "b" * 64,
                reserved_gpu_seconds=8,
                max_attempts=2,
            )
            accepted = FIXED_NOW - timedelta(minutes=ordinal * 10)
            row = runtime.store.operations[operation.id]
            row.view = row.view.model_copy(
                update={
                    "status": status,
                    "accepted_at": accepted,
                    "activation_started_at": accepted + timedelta(seconds=1),
                    "ready_at": accepted + timedelta(seconds=3),
                    "started_at": accepted + timedelta(seconds=4),
                    "completed_at": accepted + timedelta(seconds=8),
                    "outcome": "succeeded" if status == OperationStatus.SUCCEEDED else "runtime_failed",
                    "semantic_outcome": "valid" if status == OperationStatus.SUCCEEDED else "failed",
                    "http_status": 200 if status == OperationStatus.SUCCEEDED else 502,
                    "error_code": None if status == OperationStatus.SUCCEEDED else "runtime_failed",
                    "error_detail": SECRET_ERROR,
                    "runtime": RuntimeIdentity(gpu_count=1, preemptible=True),
                    "estimated_gpu_seconds": 8.0,
                    "cold_start_seconds": 3.0,
                    "reserved_gpu_seconds": 0.0,
                    "input_tokens": ordinal * 10 if status == OperationStatus.SUCCEEDED else None,
                    "output_tokens": ordinal * 5 if status == OperationStatus.SUCCEEDED else None,
                }
            )
            identifiers.append(str(operation.id))
        return identifiers, [issued.token]

    return asyncio.run(seed())


def test_sealed_status_precedence_fixtures() -> None:
    cases = json.loads(
        (Path(__file__).resolve().parents[2] / "admin-console" / "acceptance" / "status-cases.json").read_text(
            encoding="utf-8"
        )
    )
    for case in cases:
        inputs = case["input"]
        state, _ = derive_model_state(
            catalog_supported=inputs["catalog_supported"],
            sources_fresh=inputs["sources_fresh"],
            health_failure=inputs["health_failure"],
            activation_phase=inputs["activation_phase"],
            desired_replicas=inputs["desired_replicas"],
            ready_replicas=inputs["ready_replicas"],
            queued_operations=inputs["queued_operations"],
        )
        assert state == case["expected"], case["name"]


def test_promql_is_fixed_bounded_and_rejects_selector_injection() -> None:
    queries = PrometheusQueryTemplates.for_window(model_id="glm-5-2-fp8", seconds=300)
    assert set(queries) == {
        "requests_per_second",
        "terminal_operations",
        "error_rate",
        "latency_p50_seconds",
        "latency_p95_seconds",
        "latency_p99_seconds",
    }
    assert all("glm-5-2-fp8" in query for query in queries.values())
    assert all("[300s]" in query for name, query in queries.items() if name != "terminal_operations")
    grouped = PrometheusQueryTemplates.by_model_for_window(seconds=300)
    assert set(grouped) == set(queries)
    assert all("glm-5-2-fp8" not in query for query in grouped.values())
    with pytest.raises(ValueError, match="selector"):
        PrometheusQueryTemplates.for_window(model_id='qwen"} or vector(1)', seconds=300)
    with pytest.raises(ValueError, match="range"):
        PrometheusQueryTemplates.for_window(model_id=None, seconds=59)


def test_kubernetes_projection_cache_is_bounded_and_returns_copies() -> None:
    now = [FIXED_NOW]
    delegate = FakeKubernetesAdapter()
    adapter = CachedKubernetesAdminAdapter(delegate, ttl_seconds=15, clock=lambda: now[0])

    async def exercise() -> None:
        first = await adapter.snapshot(("qwen3-8b",))
        second = await adapter.snapshot(("qwen3-8b",))
        assert first == second and first is not second
        assert delegate.calls == 1
        now[0] += timedelta(seconds=16)
        await adapter.snapshot(("qwen3-8b",))
        assert delegate.calls == 2

    asyncio.run(exercise())


def test_measurement_availability_contract_rejects_ambiguous_missing_values() -> None:
    with pytest.raises(ValueError, match="null with a reason"):
        AdminMeasurement(
            value=0,
            unit="tokens/second",
            state=AdminValueState.UNAVAILABLE,
            source="postgresql",
            reason="not instrumented",
        )
    with pytest.raises(ValueError, match="must be numeric"):
        AdminMeasurement(
            value=None,
            unit="requests/second",
            state=AdminValueState.AVAILABLE,
            source="prometheus",
        )


def test_context_overview_models_and_model_detail_are_typed_and_explicit(
    registry: Any, cipher: Any, hasher: Any
) -> None:
    runtime = _runtime(registry, cipher, hasher)
    with _client(runtime) as client:
        context = client.get("/admin/api/v1/context", headers=ADMIN_AUTH)
        overview = client.get("/admin/api/v1/overview", headers=ADMIN_AUTH)
        models = client.get("/admin/api/v1/models?search=qwen", headers=ADMIN_AUTH)
        detail = client.get("/admin/api/v1/models/qwen3-8b", headers=ADMIN_AUTH)
        capacity = client.get("/admin/api/v1/capacity", headers=ADMIN_AUTH)
        observability = client.get("/admin/api/v1/observability", headers=ADMIN_AUTH)

    assert (
        context.status_code
        == overview.status_code
        == models.status_code
        == detail.status_code
        == capacity.status_code
        == observability.status_code
        == 200
    )
    assert context.json()["data"]["selected"]["project"] == "project-test"
    overview_value = overview.json()
    assert overview_value["data"]["capacity"]["allocatable_gpus"]["value"] == 38
    assert overview_value["data"]["tokens_per_second"] == {
        "value": 0.0,
        "unit": "tokens/second",
        "state": "available",
        "source": "postgresql",
        "reason": None,
    }
    assert overview_value["data"]["measured_gpu_seconds"]["value"] is None
    assert (
        overview_value["data"]["measured_gpu_seconds"]["reason"]
        == "time-integrated DCGM GPU-seconds are not instrumented"
    )
    model = models.json()["data"]["items"][0]
    assert model["identity"]["id"] == "qwen3-8b"
    assert model["runtime"]["state"] == "hot"
    assert model["runtime"]["reason"]
    assert model["metrics"]["terminal_operations"]["value"] == 0
    assert model["metrics"]["error_operations"]["value"] == 0
    assert model["metrics"]["error_rate"]["value"] == 0
    assert detail.json()["data"]["snapshot_restore_seconds"]["value"] is None
    assert detail.json()["data"]["cold_start_phase_breakdown"]["reason"]
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in (context, overview, models, detail, capacity, observability)
    )


def test_admin_bff_enforces_auth_and_same_origin_transport(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    with _client(runtime, authenticated=False) as client:
        unauthenticated = client.get("/admin/api/v1/context")
        login = client.post("/admin/api/v1/session", headers=ADMIN_AUTH)
        assert login.status_code == 200
        cross_origin = client.get(
            "/admin/api/v1/context",
            headers={**ADMIN_AUTH, "origin": "https://caller.example.invalid"},
        )
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == "operator_session_required"
    assert unauthenticated.headers["x-request-id"] == unauthenticated.json()["request_id"]
    assert cross_origin.status_code == 403
    assert "project-test" not in cross_origin.text


def test_partial_adapter_failure_is_unknown_not_zero_and_does_not_reflect_exception(
    registry: Any, cipher: Any, hasher: Any
) -> None:
    runtime = _runtime(registry, cipher, hasher, failing_kubernetes=True)
    with _client(runtime) as client:
        response = client.get("/admin/api/v1/models?search=qwen", headers=ADMIN_AUTH)
    assert response.status_code == 200
    value = response.json()
    assert value["data"]["items"][0]["runtime"]["state"] == "unknown"
    source = next(item for item in value["meta"]["sources"] if item["id"] == "kubernetes")
    assert source["state"] == "unavailable"
    assert source["reason"] == "source adapter or query is unavailable"
    assert "SENSITIVE_KUBERNETES_TRANSPORT_DETAIL" not in response.text


def test_stale_observed_state_is_labeled_and_forces_unknown(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    assert runtime.admin_read is not None
    runtime.admin_read.kubernetes = FakeKubernetesAdapter(FIXED_NOW - timedelta(minutes=2))
    with _client(runtime) as client:
        response = client.get("/admin/api/v1/models?search=qwen", headers=ADMIN_AUTH)
    assert response.status_code == 200
    value = response.json()
    assert value["data"]["items"][0]["runtime"]["state"] == "unknown"
    source = next(item for item in value["meta"]["sources"] if item["id"] == "kubernetes")
    assert source["state"] == "stale"
    assert source["age_seconds"] == 120
    assert source["reason"] == "source observation exceeded the freshness bound"


@pytest.mark.parametrize(
    ("support_state", "desired", "ready", "semantic_healthy", "expected"),
    [
        ("lean-live-verified", 1, 1, True, "hot"),
        ("lean-live-verified", 2, 1, True, "loading"),
        ("lean-live-verified", 0, 0, True, "cold"),
        ("lean-live-verified", 1, 1, None, "unknown"),
        ("lean-live-verified", 1, 1, False, "unhealthy"),
        ("blocked", 1, 1, True, "unsupported"),
        ("unqualified", 1, 1, True, "unsupported"),
    ],
)
def test_live_route_compatibility_and_explicit_semantic_health_gate(
    registry: Registry,
    cipher: Any,
    hasher: Any,
    support_state: str,
    desired: int,
    ready: int,
    semantic_healthy: bool | None,
    expected: str,
) -> None:
    source = registry.get("qwen3-8b")
    projected = replace(
        source,
        gateway=replace(source.gateway, support_state=support_state, routable=True),
        lean_static=support_state == "lean-live-verified",
    )
    live_registry = Registry(registry.catalog, {projected.id: projected})
    runtime = _runtime(live_registry, cipher, hasher)
    assert runtime.admin_read is not None
    runtime.admin_read.kubernetes = FakeKubernetesAdapter(
        desired_replicas=desired,
        ready_replicas=ready,
        semantic_healthy=semantic_healthy,
    )
    with _client(runtime) as client:
        response = client.get("/admin/api/v1/models", headers=ADMIN_AUTH)
    assert response.status_code == 200
    model = response.json()["data"]["items"][0]
    assert model["identity"]["support_state"] == support_state
    assert model["runtime"]["semantic_healthy"] is semantic_healthy
    assert model["runtime"]["state"] == expected


def test_retained_ready_evidence_never_overrides_current_cluster_state(
    registry: Registry,
    cipher: Any,
    hasher: Any,
) -> None:
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
        gateway=replace(
            source.gateway,
            support_state="lean-live-verified",
            routable=True,
            qualification=projection,
        ),
        lean_static=True,
    )
    runtime = _runtime(Registry(registry.catalog, {projected.id: projected}), cipher, hasher)
    assert runtime.admin_read is not None
    runtime.admin_read.kubernetes = FakeKubernetesAdapter(
        desired_replicas=0,
        ready_replicas=0,
        semantic_healthy=True,
    )

    with _client(runtime) as client:
        response = client.get("/admin/api/v1/models", headers=ADMIN_AUTH)

    assert response.status_code == 200
    model = response.json()["data"]["items"][0]
    assert model["identity"]["qualification"]["kind"] == "reviewed-evidence-snapshot"
    assert model["identity"]["qualification"]["states"]["runtime_ready"] is True
    assert model["runtime"]["ready_replicas"] == 0
    assert model["runtime"]["state"] == "cold"


def test_operation_cursor_filters_redaction_and_detail(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    operation_ids, secret_values = _seed_operations(runtime)
    with _client(runtime) as client:
        first = client.get("/admin/api/v1/operations?limit=2", headers=ADMIN_AUTH)
        cursor = first.json()["data"]["next_cursor"]
        second = client.get(f"/admin/api/v1/operations?limit=2&cursor={cursor}", headers=ADMIN_AUTH)
        failed = client.get("/admin/api/v1/operations?status=failed", headers=ADMIN_AUTH)
        detail = client.get(f"/admin/api/v1/operations/{operation_ids[1]}", headers=ADMIN_AUTH)
        overview = client.get("/admin/api/v1/overview", headers=ADMIN_AUTH)
        model = client.get("/admin/api/v1/models/qwen3-8b", headers=ADMIN_AUTH)

    assert {
        first.status_code,
        second.status_code,
        failed.status_code,
        detail.status_code,
        overview.status_code,
        model.status_code,
    } == {200}
    assert len(first.json()["data"]["items"]) == 2
    assert len(second.json()["data"]["items"]) == 1
    assert first.json()["data"]["next_cursor"] is not None
    assert second.json()["data"]["next_cursor"] is None
    assert [item["status"] for item in failed.json()["data"]["items"]] == ["failed"]
    detail_value = detail.json()["data"]
    assert detail_value["payloads_exposed"] is False
    assert detail_value["operation"]["input_tokens"]["value"] is None
    assert detail_value["operation"]["estimated_gpu_seconds"]["state"] == "estimated"
    reconciliation = overview.json()["data"]["reconciliation"]
    assert reconciliation["durable_terminal_operations"]["value"] == 3
    assert reconciliation["prometheus_terminal_operations"]["value"] == 0
    assert reconciliation["difference"]["value"] == -3
    assert overview.json()["data"]["estimated_gpu_seconds"]["state"] == "estimated"
    token_rate = overview.json()["data"]["tokens_per_second"]
    assert token_rate["value"] == pytest.approx(60 / 3600)
    assert token_rate["state"] == "estimated"
    assert token_rate["reason"] == "2 of 3 terminal operations reported token usage"
    latency = overview.json()["data"]["latency"]
    assert latency["p50_seconds"] == {
        "value": 8.0,
        "unit": "seconds",
        "state": "available",
        "source": "postgresql",
        "reason": None,
    }
    assert latency["p95_seconds"]["value"] == 8.0
    assert latency["p99_seconds"]["value"] == 8.0
    assert latency["ttft_p95_seconds"]["value"] is None
    model_metrics = model.json()["data"]["model"]["metrics"]
    assert model_metrics["tokens_per_second"] == token_rate
    assert model_metrics["latency"]["p95_seconds"]["value"] == 8.0
    combined = "\n".join((first.text, second.text, failed.text, detail.text, overview.text))
    for secret in (SECRET_PROMPT, SECRET_ERROR, SECRET_IDEMPOTENCY, *secret_values):
        assert secret not in combined


def test_invalid_time_cursor_and_query_return_stable_problem_details(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    with _client(runtime) as client:
        invalid_range = client.get(
            "/admin/api/v1/overview?from=2026-01-01T00:00:00Z&to=2026-08-30T12:00:00Z",
            headers=ADMIN_AUTH,
        )
        invalid_cursor = client.get(
            "/admin/api/v1/operations?cursor=not-a-cursor",
            headers={**ADMIN_AUTH, "x-request-id": "CALLER_REQUEST_ID_MUST_NOT_BE_TRUSTED"},
        )
        invalid_limit = client.get("/admin/api/v1/operations?limit=10000", headers=ADMIN_AUTH)

    assert invalid_range.status_code == invalid_cursor.status_code == 400
    assert invalid_range.headers["content-type"].startswith("application/problem+json")
    assert invalid_range.json()["code"] == "invalid_time_range"
    assert invalid_cursor.json()["code"] == "invalid_cursor"
    assert invalid_limit.status_code == 422
    assert invalid_limit.json()["code"] == "validation_error"
    assert "10000" not in invalid_limit.text
    for response in (invalid_range, invalid_cursor, invalid_limit):
        request_id = response.json()["request_id"]
        assert UUID(request_id)
        assert response.headers["x-request-id"] == request_id
    assert len({invalid_range.json()["request_id"], invalid_cursor.json()["request_id"]}) == 2
    assert invalid_cursor.json()["request_id"] != "CALLER_REQUEST_ID_MUST_NOT_BE_TRUSTED"


def test_zero_is_only_emitted_for_available_sources(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    assert runtime.admin_read is not None
    runtime.admin_read.prometheus = UnavailablePrometheusAdminAdapter()
    with _client(runtime) as client:
        prometheus_absent = client.get("/admin/api/v1/overview", headers=ADMIN_AUTH)
    assert prometheus_absent.status_code == 200
    value = prometheus_absent.json()["data"]
    assert value["terminal_operations"]["value"] == 0
    assert value["error_rate"]["value"] == 0
    assert value["requests_per_second"]["value"] is None
    assert value["requests_per_second"]["state"] == "unavailable"

    async def fail_usage(*_: Any, **__: Any) -> Any:
        raise RuntimeError("SENSITIVE_USAGE_QUERY_DETAIL")

    runtime = _runtime(registry, cipher, hasher)
    runtime.store.admin_usage_window = fail_usage  # type: ignore[method-assign]
    with _client(runtime) as client:
        database_absent = client.get("/admin/api/v1/overview", headers=ADMIN_AUTH)
        model_absent = client.get("/admin/api/v1/models?search=qwen", headers=ADMIN_AUTH)
    assert database_absent.status_code == model_absent.status_code == 200
    overview = database_absent.json()
    assert overview["data"]["terminal_operations"]["value"] is None
    assert overview["data"]["error_rate"]["value"] is None
    model_metrics = model_absent.json()["data"]["items"][0]["metrics"]
    assert model_metrics["terminal_operations"]["value"] is None
    assert model_metrics["error_rate"]["value"] is None
    assert "SENSITIVE_USAGE_QUERY_DETAIL" not in database_absent.text + model_absent.text


def test_operation_store_failure_is_sanitized_problem_detail(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)

    async def fail(_: Any) -> Any:
        raise RuntimeError("SENSITIVE_DATABASE_TRANSPORT_DETAIL")

    runtime.store.admin_list_operations = fail  # type: ignore[method-assign]
    with _client(runtime) as client:
        response = client.get("/admin/api/v1/operations", headers=ADMIN_AUTH)
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "operations_unavailable"
    assert response.headers["x-request-id"] == response.json()["request_id"]
    assert "SENSITIVE_DATABASE_TRANSPORT_DETAIL" not in response.text


def test_openapi_matches_typed_versioned_admin_contract(registry: Any, cipher: Any, hasher: Any) -> None:
    from test_admin_configuration import qualified_configuration

    from fs2_serve.configuration import ConfigurationService, InMemoryConfigurationRepository

    runtime = _runtime(registry, cipher, hasher)
    initial, configuration_catalog = qualified_configuration()
    runtime.configuration = ConfigurationService(
        repository=InMemoryConfigurationRepository(initial),
        catalog=configuration_catalog,
    )
    # OpenAPI is generated from endpoint annotations; concrete services are not
    # invoked here. Enable every feature-gated admin router so this assertion
    # seals the complete production contract instead of only the base routes.
    runtime.model_deployment_preview = cast(Any, object())
    runtime.model_deployment_read = cast(Any, object())
    runtime.model_deployment_mutation = cast(Any, object())
    schema = create_app(runtime).openapi()
    contract = json.loads(
        (Path(__file__).resolve().parents[2] / "admin-console" / "contracts" / "admin-api-v1.json").read_text(
            encoding="utf-8"
        )
    )
    expected = {(route["path"], route["method"].lower()) for route in contract["routes"]}
    actual = {
        (path, method)
        for path, path_item in schema["paths"].items()
        if path.startswith(contract["api_prefix"])
        for method in path_item
        if method in {"get", "post", "patch", "delete"}
    }
    assert actual == expected
    encoded = json.dumps({path: schema["paths"][path] for path, _ in expected}, sort_keys=True)
    for forbidden in (
        "request_ciphertext",
        "response_ciphertext",
        "request_hmac",
        "idempotency_key",
        "error_detail",
        "gpu_uuids",
        "database_url",
        "kubeconfig",
        "raw_api_key",
    ):
        assert forbidden not in encoded
        assert forbidden in contract["forbidden_response_fields"]
    for route in contract["routes"]:
        operation = schema["paths"][route["path"]][route["method"].lower()]
        responses = operation["responses"]
        success_response = responses[str(route["success_status"])]
        expected_data_schema = route["data_schema"]
        if expected_data_schema is None:
            assert "content" not in success_response
        else:
            success_schema = success_response["content"]["application/json"]["schema"]
            assert expected_data_schema in success_schema["$ref"]
        for status_code in contract["problem"]["status_codes"]:
            response = responses[str(status_code)]
            assert response["content"]["application/problem+json"]["schema"] == {
                "$ref": "#/components/schemas/AdminProblem"
            }
            assert response["headers"]["x-request-id"]["schema"] == {
                "type": "string",
                "format": "uuid",
            }
        if route.get("secret_disclosure") == "one-time":
            disclosure_schema = success_response["content"]["application/json"]["schema"]
            assert "AdminApiKeyDisclosure" in disclosure_schema["$ref"]
            assert route["cache_control"] == "no-store"
    assert contract["model_states"] == [state.value for state in AdminModelState]
    assert contract["compatibility_supported_states"] == ["qualified", "lean-live-verified"]


def test_unconfigured_context_rejects_browser_supplied_cluster(registry: Any, cipher: Any, hasher: Any) -> None:
    runtime = _runtime(registry, cipher, hasher)
    assert runtime.admin_read is not None
    runtime.admin_read.contexts = AdminContextConfig()
    with pytest.raises(AdminProblemError, match="no server-authorized"):
        runtime.admin_read.resolve_context(
            project="caller-invented",
            cluster=None,
            region=None,
            from_at=None,
            to_at=None,
            timezone="UTC",
        )
