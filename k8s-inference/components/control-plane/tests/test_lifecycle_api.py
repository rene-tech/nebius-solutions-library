from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from fs2_serve.admission import AdmissionService
from fs2_serve.api import AppRuntime, create_app
from fs2_serve.auth import OperatorSessionService, PepperRing, TokenService
from fs2_serve.lifecycle import MemoryLifecycleRepository
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import OperationStatus, OperationView, Scope, TokenCreate
from fs2_serve.runtime import StubRuntimeClient
from fs2_serve.scientific_lifecycle import ScientificResultLifecycleProjector
from fs2_serve.scientific_run_result import ScientificRunResult
from fs2_serve.settings import Settings
from fs2_serve.telemetry import Metrics

BOOTSTRAP = "a" * 32


def runtime(registry: Any, cipher: Any, hasher: Any) -> AppRuntime:
    store = MemoryStore(cipher, hasher)
    settings = Settings(
        run_workers=False,
        max_request_bytes=16_384,
        public_base_url="https://inference.test.invalid",
        authorization_server_url="https://identity.test.invalid",
        catalog_dir=Path("/unused"),
        bindings_file=Path("/unused"),
    )
    metrics = Metrics(registry.list(enabled_only=True))
    lifecycle = MemoryLifecycleRepository()
    peppers = PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32})
    return AppRuntime(
        settings=settings,
        registry=registry,
        store=store,
        tokens=TokenService(store, peppers),
        admission=AdmissionService(
            registry=registry,
            store=store,
            runtime=StubRuntimeClient(),
            metrics=metrics,
            worker_concurrency=1,
            poll_seconds=0.01,
            lease_seconds=30,
            maintenance_interval_seconds=1,
            shutdown_grace_seconds=1,
            lifecycle=lifecycle,
        ),
        metrics=metrics,
        admin_token=BOOTSTRAP.encode(),
        operator_sessions=OperatorSessionService(store, peppers, ttl_seconds=300),
        lifecycle=lifecycle,
        owns_store=False,
    )


def test_online_admission_continues_trace_and_exposes_tenant_scoped_payload_free_admin_contract(
    registry: Any,
    cipher: Any,
    hasher: Any,
) -> None:
    current = runtime(registry, cipher, hasher)
    issued = asyncio.run(
        current.tokens.issue(
            TokenCreate(
                principal_id="scientist-a",
                tenant_id="oncology-a",
                scopes={Scope.INFERENCE_INVOKE, Scope.CATALOG_READ},
                models={"qwen3-8b"},
                max_concurrency=1,
            ),
            created_by="test",
        )
    )
    secret = "RAW_PATIENT_SEQUENCE_MUST_NOT_APPEAR_9271"
    trace_id = "1" * 32
    parent_span_id = "2" * 16
    headers = {
        "authorization": f"Bearer {issued.token}",
        "idempotency-key": "lifecycle-online-api-0001",
        "traceparent": f"00-{trace_id}-{parent_span_id}-01",
    }
    body = {
        "model": "qwen3-8b",
        "messages": [{"role": "user", "content": secret}],
        "parameters": {"seed": 7, "temperature": 0.1},
    }

    with TestClient(create_app(current), base_url="https://inference.test.invalid") as client:
        admitted = client.post("/v1/chat/completions", headers=headers, json=body)
        assert admitted.status_code == 202
        subject_id = admitted.json()["id"]
        session = client.post(
            "/admin/api/v1/session",
            headers={"authorization": f"Bearer {BOOTSTRAP}"},
        )
        assert session.status_code == 200
        listing = client.get(
            "/admin/api/v1/telemetry/workloads",
            params={"tenant_id": "oncology-a", "model_id": "qwen3-8b"},
        )
        detail = client.get(f"/admin/api/v1/telemetry/workloads/{subject_id}")
        metrics = client.get("/metrics")
        schema = client.get("/internal/openapi.json")

    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1
    assert detail.status_code == 200
    value = detail.json()["data"]
    assert value["subject"]["trace_id"] == trace_id
    assert value["subject"]["parent_span_id"] == parent_span_id
    assert value["subject"]["api_key_fingerprint"]
    assert value["payloads_exposed"] is False
    assert [item["phase"] for item in value["signals"]] == ["receive", "enqueue"]
    encoded = json.dumps(value, sort_keys=True)
    assert secret not in encoded
    assert issued.token not in encoded
    assert "temperature" not in encoded
    assert metrics.status_code == 200
    assert "fs2_serve_lifecycle_gpu_seconds_total" in metrics.text
    assert schema.status_code == 200
    schemas = schema.json()["components"]["schemas"]
    assert "LifecycleWorkloadDetail" in schemas
    assert "LifecycleAdminList" in schemas


def test_canonical_scientific_result_admission_is_visible_in_admin_detail(
    registry: Any,
    cipher: Any,
    hasher: Any,
) -> None:
    current = runtime(registry, cipher, hasher)
    result = ScientificRunResult.model_validate_json(
        (Path(__file__).parent / "fixtures" / "scientific-lifecycle-result-v1.json").read_text(encoding="utf-8")
    )
    operation = OperationView(
        id=UUID(result.operation_id),
        tenant_id="tenant-a",
        principal_id="scientist-a",
        token_id=UUID("55555555-5555-4555-8555-555555555555"),
        model_id=result.execution_identity.model_id,
        model_revision=result.execution_identity.model_revision,
        protocol="scientific-batch-v1",
        operation="design",
        idempotency_key="scientific-lifecycle-admin-0001",
        status=OperationStatus.SUCCEEDED,
        accepted_at=result.submitted_at,
        available_at=result.submitted_at,
        completed_at=result.completed_at,
        outcome="succeeded",
        semantic_outcome="passed",
        result_available=True,
    )
    asyncio.run(ScientificResultLifecycleProjector(current.lifecycle).project(operation, result))

    with TestClient(create_app(current), base_url="https://inference.test.invalid") as client:
        session = client.post(
            "/admin/api/v1/session",
            headers={"authorization": f"Bearer {BOOTSTRAP}"},
        )
        assert session.status_code == 200
        response = client.get(f"/admin/api/v1/telemetry/workloads/{result.attempts[0].attempt_id}")

    assert response.status_code == 200
    value = response.json()["data"]
    admission = next(item for item in value["signals"] if item["phase"] == "admit")
    assert admission["detail"]["service_class"] == "customer-batch"
    assert admission["detail"]["cluster_queue"] == "inference-accelerators"
    assert admission["detail"]["resource_flavor"] == "h100-capacity-block"
    assert value["payloads_exposed"] is False
