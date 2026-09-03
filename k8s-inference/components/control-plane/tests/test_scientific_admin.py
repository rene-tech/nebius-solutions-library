from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from fs2_serve.access_models import OperatorPrincipalCreate, OperatorRole, PrincipalKind
from fs2_serve.admin import AdminProblemError, AdminReadService
from fs2_serve.admin_models import AdminContext, AdminSourceState
from fs2_serve.admission import AdmissionService
from fs2_serve.api import ADMIN_SESSION_COOKIE, AppRuntime, create_app
from fs2_serve.auth import OperatorSessionService, PepperRing, TokenService
from fs2_serve.memory_store import MemoryStore
from fs2_serve.registry import Registry
from fs2_serve.runtime import StubRuntimeClient
from fs2_serve.scientific_admin import (
    ScientificAdminQueryError,
    ScientificAdminReadService,
    ScientificArtifactAttemptEvidence,
    ScientificArtifactSnapshot,
    ScientificModelSnapshot,
    ScientificRunDetailSnapshot,
    ScientificRunListSnapshot,
    ScientificRunQuery,
)
from fs2_serve.scientific_admin_models import (
    ScientificArtifact,
    ScientificModelReadinessList,
    ScientificRunDetail,
    ScientificRunList,
    ScientificRunSummary,
    ScientificSemanticValidation,
    ScientificServiceClass,
)
from fs2_serve.settings import Settings
from fs2_serve.telemetry import Metrics

FIXED_NOW = datetime(2026, 9, 2, 21, 0, tzinfo=UTC)
OPERATION_ID = UUID("018f0f3a-0f9b-7ccd-8d87-6e5201c95001")


def _unavailable(unit: str, reason: str) -> dict[str, object]:
    return {
        "value": None,
        "unit": unit,
        "evidence": "unavailable",
        "source": "lifecycle-ledger",
        "reason": reason,
    }


def _run() -> ScientificRunSummary:
    return ScientificRunSummary.model_validate(
        {
            "id": str(OPERATION_ID),
            "batch_id": "batch-cd8-screen-0042",
            "display_name": "CD8 binder backbone screen",
            "operation": "generate-backbone",
            "status": "queued",
            "submitted_at": FIXED_NOW - timedelta(minutes=2),
            "completed_at": None,
            "attribution": {
                "tenant_id": "tenant-oncology",
                "user_id": "researcher-ada",
                "principal_id": "svc-cd8-design",
                "api_key_prefix": "fs2_pat_7c91",
            },
            "model": {
                "model_id": "rfdiffusion",
                "display_name": "RFdiffusion",
                "execution_mode": "scientific-batch",
                "backend": {
                    "backend_id": "rfdiffusion:native-upstream",
                    "kind": "containerized-scientific-runtime",
                    "source_repository": "https://github.com/RosettaCommons/RFdiffusion",
                    "source_revision": "1" * 40,
                    "model_revision": None,
                    "runtime_image_digest": None,
                    "execution_identity_digest": None,
                },
            },
            "access": {
                "profile": "standard",
                "state": "not-required",
                "gate": "No restricted academic asset is required by this backend.",
                "receipt_digest": None,
                "credentials_exposed": False,
                "alternative": None,
            },
            "service_class": {
                "requested": "customer-batch",
                "effective": "customer-batch",
                "reason": "Tenant policy accepted the requested service class.",
                "policy_revision": "policy-v1",
            },
            "queue": {
                "tenant_queue": "tenant-oncology",
                "model_lane": "rfdiffusion",
                "local_queue": "scientific-runs",
                "cluster_queue": "inference-accelerators",
                "workload_priority_class": "scientific-customer-batch",
                "priority_value": 500,
                "admission_state": "pending",
                "admission_reason": "Waiting for capacity.",
                "admitted_at": None,
                "queue_position": _unavailable("count", "Queue position is not measured."),
            },
            "fast_start": {
                "tier": "not-observed",
                "evidence": "unavailable",
                "observed_at": None,
                "runtime_identity_digest": None,
                "reason": "No exact runtime start observation is available.",
            },
            "stage_counts": {
                "pending": 1,
                "queued": 0,
                "admitted": 0,
                "running": 0,
                "succeeded": 0,
                "failed": 0,
                "cancelled": 0,
                "skipped": 0,
            },
            "gpu_accounting": {
                "gpu_count": None,
                "capacity_type": "unknown",
                "allocated": _unavailable("gpu-seconds", "No allocation boundary is available."),
                "active": _unavailable("gpu-seconds", "No active-compute event is available."),
                "idle_total": _unavailable("gpu-seconds", "No allocation boundary is available."),
                "idle_by_cause": [],
                "grace_drain": _unavailable("gpu-seconds", "No grace event is available."),
                "reconciliation_delta": _unavailable("gpu-seconds", "The lifecycle cannot be reconciled."),
            },
            "error": None,
            "cancellation": {
                "state": "not-requested",
                "requested_at": None,
                "requested_by": None,
                "reason": None,
                "mode": "terminate-attempt",
                "grace_seconds": 30,
                "can_cancel": True,
            },
        }
    )


def _detail() -> ScientificRunDetail:
    return ScientificRunDetail(
        run=_run(),
        lifecycle_phases=[],
        stages=[
            {
                "id": "design",
                "display_name": "Design candidates",
                "ordinal": 1,
                "needs": [],
                "resource_class": "gpu",
                "admission_mode": "independent-jobs",
                "checkpoint_mode": "restart",
                "status": "queued",
                "attempts": [
                    {
                        "id": "attempt-terminal-1",
                        "number": 1,
                        "status": "queued",
                        "started_at": None,
                        "completed_at": None,
                        "workload_uid": None,
                        "job_uid": None,
                        "pod_count": None,
                        "node_count": None,
                        "gpu_count": None,
                        "checkpoint_input_artifact_id": None,
                        "checkpoint_output_artifact_id": None,
                        "error": None,
                    }
                ],
            }
        ],
        artifacts=[],
        retry={"max_attempts_per_stage": 2, "retryable_exit_codes": []},
        semantic_validation={"validator_id": "rfdiffusion-output-v1", "status": "not-run"},
        observability=[],
    )


def _context() -> AdminContext:
    return AdminContext(
        project="project-test",
        cluster="cluster-test",
        region="region-test",
        from_at=FIXED_NOW - timedelta(hours=1),
        to_at=FIXED_NOW,
        timezone="UTC",
    )


def _query(*, limit: int = 100) -> ScientificRunQuery:
    return ScientificRunQuery(
        from_at=FIXED_NOW - timedelta(hours=1),
        to_at=FIXED_NOW,
        limit=limit,
    )


class RunAdapter:
    async def list_runs(self, query: ScientificRunQuery) -> ScientificRunListSnapshot:
        assert query.limit <= 200
        return ScientificRunListSnapshot(
            data=ScientificRunList(items=[_run()]),
            observed_at=FIXED_NOW - timedelta(seconds=91),
        )

    async def get_run(self, operation_id: UUID, *, tenant_id: str | None) -> ScientificRunDetailSnapshot:
        if operation_id != OPERATION_ID or tenant_id not in {None, "tenant-oncology"}:
            raise KeyError(operation_id)
        return ScientificRunDetailSnapshot(data=_detail(), observed_at=FIXED_NOW)


class ArtifactAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def for_operation(self, operation_id: UUID, *, tenant_id: str) -> ScientificArtifactSnapshot:
        assert operation_id == OPERATION_ID
        assert tenant_id == "tenant-oncology"
        if self.fail:
            raise RuntimeError("SENSITIVE_ARTIFACT_FAILURE")
        artifact = ScientificArtifact.model_validate(
            {
                "artifact_id": "artifact-1",
                "name": "result.cif",
                "role": "output",
                "semantic_type": "protein-structure",
                "state": "available",
                "sha256": "a" * 64,
                "size_bytes": {
                    "value": 1234,
                    "unit": "bytes",
                    "evidence": "measured",
                    "source": "artifact-manifest",
                },
                "media_type": "chemical/x-mmcif",
                "created_at": FIXED_NOW,
                "download": {"available": False, "reason": "Signed download is not configured."},
            }
        )
        return ScientificArtifactSnapshot(
            artifacts=(artifact,),
            semantic_validation=ScientificSemanticValidation(
                validator_id="rfdiffusion-output-v1",
                status="passed",
                receipt_digest="sha256:" + "b" * 64,
            ),
            observed_at=FIXED_NOW,
        )


class CanonicalArtifactAdapter(ArtifactAdapter):
    async def for_operation(self, operation_id: UUID, *, tenant_id: str) -> ScientificArtifactSnapshot:
        snapshot = await super().for_operation(operation_id, tenant_id=tenant_id)
        return ScientificArtifactSnapshot(
            artifacts=snapshot.artifacts,
            semantic_validation=snapshot.semantic_validation,
            observed_at=snapshot.observed_at,
            terminal_status="succeeded",
            completed_at=FIXED_NOW,
            model_revision="2" * 40,
            runtime_image_digest="sha256:" + "3" * 64,
            execution_identity_digest="4" * 64,
            access_profile="standard",
            access_state="not-required",
            service_class=ScientificServiceClass.CUSTOMER_BATCH,
            attempts=(
                ScientificArtifactAttemptEvidence(
                    attempt_id="attempt-terminal-1",
                    status="succeeded",
                    started_at=FIXED_NOW - timedelta(minutes=1),
                    completed_at=FIXED_NOW,
                    workload_uid="workload-terminal-1",
                    job_uid="job-terminal-1",
                    pod_count=1,
                    node_count=1,
                    gpu_count=1,
                    checkpoint_input_artifact_id=None,
                    checkpoint_output_artifact_id=None,
                    admitted_at=FIXED_NOW - timedelta(minutes=1),
                    resolved_pool_id="h100-preemptible",
                    admitted_resource_flavor="inference-h100-1x",
                    accelerator_resource_name="nvidia.com/gpu",
                ),
            ),
        )


class ModelAdapter:
    async def list_models(self, *, tenant_id: str | None = None) -> ScientificModelSnapshot:
        del tenant_id
        return ScientificModelSnapshot(data=ScientificModelReadinessList(items=[]), observed_at=FIXED_NOW)


class FailingRunAdapter(RunAdapter):
    async def list_runs(self, query: ScientificRunQuery) -> ScientificRunListSnapshot:
        del query
        raise RuntimeError("SENSITIVE_CONTROLLER_FAILURE")


class InvalidQueryRunAdapter(RunAdapter):
    async def list_runs(self, query: ScientificRunQuery) -> ScientificRunListSnapshot:
        del query
        raise ScientificAdminQueryError("SENSITIVE_INVALID_CURSOR")


def _service(*, artifacts: ArtifactAdapter | None = None, runs: RunAdapter | None = None) -> ScientificAdminReadService:
    return ScientificAdminReadService(
        runs=runs or RunAdapter(),
        artifacts=artifacts or ArtifactAdapter(),
        models=ModelAdapter(),
        clock=lambda: FIXED_NOW,
    )


def _runtime(registry: Registry, cipher, hasher) -> AppRuntime:
    store = MemoryStore(cipher, hasher)
    settings = Settings(
        run_workers=False,
        max_request_bytes=1024,
        public_base_url="https://inference.test.invalid",
        authorization_server_url="https://identity.test.invalid",
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
        admin_read=AdminReadService(registry=registry, store=store, clock=lambda: FIXED_NOW),
        scientific_admin=_service(),
    )


async def test_list_preserves_stale_source_state() -> None:
    envelope = await _service().run_list(_context(), _query(limit=25))

    assert len(envelope.data.items) == 1
    assert envelope.meta.sources[0].state is AdminSourceState.STALE
    assert envelope.meta.warnings[0].code == "partial_source_stale"


async def test_detail_degrades_to_partial_when_artifact_source_is_unavailable() -> None:
    envelope = await _service(artifacts=ArtifactAdapter(fail=True)).run_detail(
        _context(),
        OPERATION_ID,
        tenant_id="tenant-oncology",
    )

    assert envelope.data.run.id == str(OPERATION_ID)
    assert envelope.data.artifacts == []
    assert envelope.data.semantic_validation.validator_id == "unavailable"
    assert [source.state for source in envelope.meta.sources] == [
        AdminSourceState.AVAILABLE,
        AdminSourceState.UNAVAILABLE,
    ]
    assert envelope.meta.warnings[0].source == "scientific-artifacts"
    assert "SENSITIVE_ARTIFACT_FAILURE" not in envelope.model_dump_json()


async def test_detail_overlays_canonical_terminal_result_evidence() -> None:
    envelope = await _service(artifacts=CanonicalArtifactAdapter()).run_detail(
        _context(),
        OPERATION_ID,
        tenant_id="tenant-oncology",
    )

    assert envelope.data.run.status == "succeeded"
    assert envelope.data.run.completed_at == FIXED_NOW
    assert envelope.data.run.model.backend.model_revision == "2" * 40
    assert envelope.data.run.model.backend.runtime_image_digest == "sha256:" + "3" * 64
    assert envelope.data.run.gpu_accounting.gpu_count == 1
    assert envelope.data.run.queue.admission_state == "finished"
    attempt = envelope.data.stages[0].attempts[0]
    assert attempt.resolved_pool_id == "h100-preemptible"
    assert attempt.admitted_resource_flavor == "inference-h100-1x"
    assert attempt.accelerator_resource_name == "nvidia.com/gpu"
    assert attempt.admitted_at == FIXED_NOW - timedelta(minutes=1)


async def test_detail_keeps_controller_data_when_artifact_reader_is_not_configured() -> None:
    service = ScientificAdminReadService(
        runs=RunAdapter(),
        models=ModelAdapter(),
        clock=lambda: FIXED_NOW,
    )

    envelope = await service.run_detail(_context(), OPERATION_ID, tenant_id="tenant-oncology")

    assert envelope.data.run.id == str(OPERATION_ID)
    assert envelope.data.semantic_validation.status == "not-run"
    assert envelope.meta.sources[-1].id == "scientific-artifacts"
    assert envelope.meta.sources[-1].state is AdminSourceState.UNAVAILABLE


async def test_controller_failure_returns_stable_problem_without_backend_detail() -> None:
    with pytest.raises(AdminProblemError) as caught:
        await _service(runs=FailingRunAdapter()).run_list(_context(), _query())

    assert caught.value.status_code == 503
    assert caught.value.code == "scientific_controller_unavailable"
    assert "SENSITIVE_CONTROLLER_FAILURE" not in caught.value.detail


async def test_invalid_cursor_returns_stable_client_problem_without_decoder_detail() -> None:
    with pytest.raises(AdminProblemError) as caught:
        await _service(runs=InvalidQueryRunAdapter()).run_list(_context(), _query())

    assert caught.value.status_code == 422
    assert caught.value.code == "invalid_scientific_query"
    assert "SENSITIVE_INVALID_CURSOR" not in caught.value.detail


def test_run_query_rejects_unbounded_windows_and_limits() -> None:
    with pytest.raises(ValueError, match="window"):
        ScientificRunQuery(
            from_at=FIXED_NOW - timedelta(days=32),
            to_at=FIXED_NOW,
        )
    with pytest.raises(ValueError, match="limit"):
        _query(limit=201)


def test_authenticated_admin_routes_use_the_real_bff_service(registry, cipher, hasher) -> None:
    client = TestClient(create_app(_runtime(registry, cipher, hasher)), base_url="https://inference.test.invalid")
    assert client.get("/admin/api/v1/scientific-runs").status_code == 401

    session = client.post(
        "/admin/api/v1/session",
        headers={"authorization": f"Bearer {'a' * 32}"},
    )
    assert session.status_code == 200

    assert client.get("/admin/api/v1/scientific-capabilities").status_code == 200
    run_list = client.get("/admin/api/v1/scientific-runs?limit=25&tenant_id=tenant-oncology")
    assert run_list.status_code == 200
    assert run_list.json()["data"]["items"][0]["id"] == str(OPERATION_ID)
    assert client.get(f"/admin/api/v1/scientific-runs/{OPERATION_ID}").status_code == 200
    assert client.get("/admin/api/v1/scientific-models").status_code == 200
    assert client.get("/admin/api/v1/scientific-runs?access_state=invented").status_code == 422


def test_absent_run_reader_removes_only_run_routes(registry, cipher, hasher) -> None:
    runtime = _runtime(registry, cipher, hasher)
    runtime.scientific_admin = ScientificAdminReadService(models=ModelAdapter(), clock=lambda: FIXED_NOW)
    client = TestClient(create_app(runtime), base_url="https://inference.test.invalid")
    assert client.post("/admin/api/v1/session", headers={"authorization": f"Bearer {'a' * 32}"}).status_code == 200

    capabilities = client.get("/admin/api/v1/scientific-capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["data"]["model_readiness"]["available"] is True
    assert capabilities.json()["data"]["run_history"]["available"] is False
    assert client.get("/admin/api/v1/scientific-models").status_code == 200
    assert client.get("/admin/api/v1/scientific-runs").status_code == 404


def test_tenant_viewer_can_discover_authorized_models_and_read_runs(
    registry,
    cipher,
    hasher,
) -> None:
    runtime = _runtime(registry, cipher, hasher)
    assert isinstance(runtime.store, MemoryStore)
    assert runtime.operator_sessions is not None
    principal_id = uuid4()
    asyncio.run(
        runtime.store.create_operator_principal(
            principal_id=principal_id,
            request=OperatorPrincipalCreate(
                subject="tenant-oncology-viewer",
                display_name="Tenant oncology viewer",
                kind=PrincipalKind.HUMAN,
                role=OperatorRole.VIEWER,
                tenant_id="tenant-oncology",
            ),
            actor="test-bootstrap",
        )
    )
    cookie = asyncio.run(runtime.operator_sessions.issue(principal_id, actor="test-bootstrap")).cookie_value
    headers = {"cookie": f"{ADMIN_SESSION_COOKIE}={cookie}"}
    client = TestClient(create_app(runtime), base_url="https://inference.test.invalid")

    capabilities = client.get("/admin/api/v1/scientific-capabilities", headers=headers)
    run_list = client.get("/admin/api/v1/scientific-runs", headers=headers)

    assert capabilities.status_code == 200
    assert capabilities.json()["data"]["run_history"]["available"] is True
    assert capabilities.json()["data"]["model_readiness"]["available"] is True
    assert capabilities.json()["data"]["model_readiness"]["reason"] is None
    assert run_list.status_code == 200
    assert client.get("/admin/api/v1/scientific-models", headers=headers).status_code == 200
