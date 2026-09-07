from __future__ import annotations

import asyncio
import gzip
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
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
    ScientificModelPolicyInvalidError,
    ScientificModelPolicySnapshot,
    ScientificModelPolicyStaleRevisionError,
    ScientificModelSnapshot,
    ScientificRunCancelOutcome,
    ScientificRunDetailSnapshot,
    ScientificRunListSnapshot,
    ScientificRunQuery,
)
from fs2_serve.scientific_admin_models import (
    ScientificArtifact,
    ScientificModelPolicy,
    ScientificModelPolicyList,
    ScientificModelPolicyUpdate,
    ScientificModelReadiness,
    ScientificModelReadinessList,
    ScientificRunDetail,
    ScientificRunList,
    ScientificRunSummary,
    ScientificSemanticValidation,
    ScientificServiceClass,
)
from fs2_serve.scientific_artifacts import ArtifactContentStream
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


def _readiness(model_id: str = "rfdiffusion") -> ScientificModelReadiness:
    return ScientificModelReadiness.model_validate(
        {
            "model_id": model_id,
            "candidate_id": model_id,
            "display_name": model_id.title(),
            "readiness": "candidate",
            "readiness_reason": "Candidate runtime.",
            "workload_profile": "published",
            "missing_evidence": ["qualified-evidence"],
            "qualification": {"state": "evidence-absent", "reason": "No joined qualification evidence."},
            "execution_mode": "scientific-batch",
            "batch_supported": True,
            "interactive_supported": False,
            "service_classes": ["customer-batch", "bulk-backfill"],
            "backend": {
                "backend_id": f"{model_id}:native-upstream",
                "kind": "containerized-scientific-runtime",
                "source_repository": "https://github.com/RosettaCommons/RFdiffusion",
                "source_revision": "1" * 40,
            },
            "access": {
                "profile": "standard",
                "state": "not-required",
                "gate": "No restricted academic asset is required by this backend.",
            },
            "caching": {
                "exact_tier": "not-observed",
                "image": "candidate",
                "artifacts": "candidate",
                "reference_data": "unsupported",
                "runtime_checkpoint": "unavailable",
                "gpu_snapshot": "unavailable",
                "reason": "No exact fast-start observation is available.",
            },
        }
    )


class KnownModelAdapter(ModelAdapter):
    async def list_models(self, *, tenant_id: str | None = None) -> ScientificModelSnapshot:
        del tenant_id
        return ScientificModelSnapshot(
            data=ScientificModelReadinessList(items=[_readiness("rfdiffusion"), _readiness("boltzgen")]),
            observed_at=FIXED_NOW,
        )


def _policy(
    model_id: str,
    *,
    tenant_id: str | None = None,
    revision: int = 0,
    paused: bool = False,
    max_active_runs: int | None = None,
    running: int = 0,
    queued: int = 0,
) -> ScientificModelPolicy:
    stored = revision > 0
    state = "paused" if paused else "at-limit" if max_active_runs is not None and running >= max_active_runs else "open"
    return ScientificModelPolicy.model_validate(
        {
            "model_id": model_id,
            "scope_tenant_id": tenant_id,
            "catalog_known": True,
            "desired": {
                "tenant_id": tenant_id,
                "revision": revision,
                "paused": paused,
                "max_active_runs": max_active_runs,
                "reason": "hold" if stored and paused else None,
                "updated_by": "operator-ada" if stored else None,
                "updated_at": FIXED_NOW if stored else None,
            },
            "inherited": None if tenant_id is None else {"tenant_id": None, "revision": 0, "paused": False},
            "effective": {
                "state": state,
                "paused": paused,
                "max_active_runs": max_active_runs,
                "reason": f"Dispatch is {state}.",
            },
            "counts": {"queued": queued, "running": running},
            "all_tenants_counts": {"queued": queued, "running": running},
        }
    )


class PolicyAdapter:
    def __init__(self, *, stale: int | None = None) -> None:
        self.stale = stale
        self.list_calls: list[tuple[str | None, tuple[str, ...]]] = []
        self.set_calls: list[tuple[str, str | None, ScientificModelPolicyUpdate, str]] = []

    async def list_policies(
        self, *, tenant_id: str | None, model_ids: tuple[str, ...]
    ) -> ScientificModelPolicySnapshot:
        self.list_calls.append((tenant_id, model_ids))
        items = [
            _policy(model_id, tenant_id=tenant_id, revision=2, max_active_runs=1, running=1, queued=2)
            if model_id == "rfdiffusion"
            else _policy(model_id, tenant_id=tenant_id)
            for model_id in model_ids
        ]
        return ScientificModelPolicySnapshot(
            data=ScientificModelPolicyList(scope_tenant_id=tenant_id, items=items),
            observed_at=FIXED_NOW,
        )

    async def set_policy(
        self,
        model_id: str,
        *,
        tenant_id: str | None,
        update: ScientificModelPolicyUpdate,
        actor: str,
    ) -> ScientificModelPolicy:
        self.set_calls.append((model_id, tenant_id, update, actor))
        if self.stale is not None:
            raise ScientificModelPolicyStaleRevisionError(self.stale)
        return _policy(
            model_id,
            tenant_id=tenant_id,
            revision=update.expected_revision + 1,
            paused=update.paused,
            max_active_runs=update.max_active_runs,
        )


class FailingPolicyAdapter(PolicyAdapter):
    async def set_policy(self, model_id, *, tenant_id, update, actor):  # noqa: ANN001, ANN202
        del model_id, tenant_id, update, actor
        raise RuntimeError("SENSITIVE_POLICY_FAILURE")


class ControlAdapter:
    def __init__(self, outcome: ScientificRunCancelOutcome = "requested") -> None:
        self.outcome = outcome
        self.calls: list[tuple[UUID, str, str]] = []

    async def request_cancel(self, operation_id: UUID, *, tenant_id: str, actor: str) -> ScientificRunCancelOutcome:
        self.calls.append((operation_id, tenant_id, actor))
        if operation_id != OPERATION_ID:
            raise KeyError(operation_id)
        return self.outcome


class FailingControlAdapter(ControlAdapter):
    async def request_cancel(self, operation_id: UUID, *, tenant_id: str, actor: str) -> ScientificRunCancelOutcome:
        del operation_id, tenant_id, actor
        raise RuntimeError("SENSITIVE_CANCEL_FAILURE")


class FailingRunAdapter(RunAdapter):
    async def list_runs(self, query: ScientificRunQuery) -> ScientificRunListSnapshot:
        del query
        raise RuntimeError("SENSITIVE_CONTROLLER_FAILURE")


class InvalidQueryRunAdapter(RunAdapter):
    async def list_runs(self, query: ScientificRunQuery) -> ScientificRunListSnapshot:
        del query
        raise ScientificAdminQueryError("SENSITIVE_INVALID_CURSOR")


def _service(
    *,
    artifacts: ArtifactAdapter | None = None,
    runs: RunAdapter | None = None,
    controls: ControlAdapter | None = None,
    policies: PolicyAdapter | None = None,
    models: ModelAdapter | None = None,
) -> ScientificAdminReadService:
    return ScientificAdminReadService(
        runs=runs or RunAdapter(),
        artifacts=artifacts or ArtifactAdapter(),
        controls=controls or ControlAdapter(),
        policies=policies or PolicyAdapter(),
        models=models or KnownModelAdapter(),
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


async def test_cancel_run_records_the_request_under_the_run_tenant_and_returns_the_refreshed_detail() -> None:
    controls = ControlAdapter()
    service = _service(controls=controls)

    envelope = await service.cancel_run(_context(), OPERATION_ID, tenant_id=None, actor="operator-ada")

    assert envelope.data.run.id == str(OPERATION_ID)
    # The operator is global (tenant None); the durable request is still keyed by the run's tenant.
    assert controls.calls == [(OPERATION_ID, "tenant-oncology", "operator-ada")]
    assert {source.id for source in envelope.meta.sources} == {"scientific-controller", "scientific-artifacts"}
    assert service.capabilities().run_control.available is True


async def test_cancel_run_answers_stable_problems_for_terminal_missing_unconfigured_and_failing_control() -> None:
    with pytest.raises(AdminProblemError) as terminal:
        await _service(controls=ControlAdapter("terminal")).cancel_run(
            _context(), OPERATION_ID, tenant_id="tenant-oncology", actor="operator-ada"
        )
    assert (terminal.value.status_code, terminal.value.code) == (409, "scientific_run_terminal")

    with pytest.raises(AdminProblemError) as missing:
        await _service().cancel_run(_context(), uuid4(), tenant_id=None, actor="operator-ada")
    assert (missing.value.status_code, missing.value.code) == (404, "scientific_run_not_found")

    with pytest.raises(AdminProblemError) as foreign_tenant:
        await _service().cancel_run(_context(), OPERATION_ID, tenant_id="tenant-other", actor="operator-ada")
    assert (foreign_tenant.value.status_code, foreign_tenant.value.code) == (404, "scientific_run_not_found")

    read_only = ScientificAdminReadService(runs=RunAdapter(), models=ModelAdapter(), clock=lambda: FIXED_NOW)
    assert read_only.capabilities().run_control.available is False
    assert read_only.capabilities().run_control.reason is not None
    with pytest.raises(AdminProblemError) as unconfigured:
        await read_only.cancel_run(_context(), OPERATION_ID, tenant_id=None, actor="operator-ada")
    assert (unconfigured.value.status_code, unconfigured.value.code) == (503, "scientific_run_control_unavailable")

    with pytest.raises(AdminProblemError) as failing:
        await _service(controls=FailingControlAdapter()).cancel_run(
            _context(), OPERATION_ID, tenant_id=None, actor="operator-ada"
        )
    assert (failing.value.status_code, failing.value.code) == (503, "scientific_controller_unavailable")
    assert "SENSITIVE" not in failing.value.detail

    with pytest.raises(ValueError):
        ScientificAdminReadService(controls=ControlAdapter(), models=ModelAdapter())


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

    capabilities = client.get("/admin/api/v1/scientific-capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["data"]["run_control"] == {"available": True, "reason": None}
    run_list = client.get("/admin/api/v1/scientific-runs?limit=25&tenant_id=tenant-oncology")
    assert run_list.status_code == 200
    assert run_list.json()["data"]["items"][0]["id"] == str(OPERATION_ID)
    assert client.get(f"/admin/api/v1/scientific-runs/{OPERATION_ID}").status_code == 200
    assert client.get("/admin/api/v1/scientific-models").status_code == 200
    assert client.get("/admin/api/v1/scientific-runs?access_state=invented").status_code == 422
    cancelled = client.post(f"/admin/api/v1/scientific-runs/{OPERATION_ID}:cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["run"]["id"] == str(OPERATION_ID)
    assert client.post(f"/admin/api/v1/scientific-runs/{uuid4()}:cancel").status_code == 404
    assert client.post("/admin/api/v1/scientific-runs/not-a-uuid:cancel").status_code == 422


def test_admin_artifact_download_uses_existing_tenant_authority_and_exact_bytes(registry, cipher, hasher) -> None:
    artifact_id = uuid4()
    content = gzip.compress(b"synthetic scientific structure\n", mtime=0)
    digest = hashlib.sha256(content).hexdigest()
    calls: list[tuple[UUID, str]] = []

    class DownloadArtifacts(ArtifactAdapter):
        async def for_operation(self, operation_id: UUID, *, tenant_id: str) -> ScientificArtifactSnapshot:
            original = await super().for_operation(operation_id, tenant_id=tenant_id)
            artifact = original.artifacts[0].model_copy(
                update={"artifact_id": str(artifact_id), "name": "result.cif.gz"}
            )
            return ScientificArtifactSnapshot(
                artifacts=(artifact,),
                semantic_validation=original.semantic_validation,
                observed_at=original.observed_at,
            )

    class ContentService:
        async def open_content(self, selected: UUID, *, tenant_id: str) -> ArtifactContentStream:
            calls.append((selected, tenant_id))

            async def chunks():
                yield content[:5]
                yield content[5:]

            return ArtifactContentStream(
                artifact=cast(
                    Any,
                    SimpleNamespace(
                        artifact_id=artifact_id,
                        size_bytes=len(content),
                        digest="sha256:" + digest,
                        media_type="chemical/x-mmcif",
                        compression=SimpleNamespace(value="gzip"),
                    ),
                ),
                chunks=chunks(),
            )

    runtime = _runtime(registry, cipher, hasher)
    runtime.scientific_admin = _service(artifacts=DownloadArtifacts())
    runtime.artifact_service = cast(Any, ContentService())
    client = TestClient(create_app(runtime), base_url="https://inference.test.invalid")
    endpoint = f"/admin/api/v1/scientific-runs/{OPERATION_ID}/artifacts/{artifact_id}/content"
    assert client.get(endpoint).status_code == 401
    assert client.post("/admin/api/v1/session", headers={"authorization": f"Bearer {'a' * 32}"}).status_code == 200
    response = client.get(endpoint)
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["x-fs2-artifact-sha256"] == digest
    assert "content-encoding" not in response.headers
    assert "result.cif.gz" in response.headers["content-disposition"]
    assert calls == [(artifact_id, "tenant-oncology")]
    assert client.get(endpoint.replace(str(artifact_id), str(uuid4()))).status_code == 404
    assert client.get(endpoint.replace(str(OPERATION_ID), str(uuid4()))).status_code == 404
    assert len(calls) == 1

    assert isinstance(runtime.store, MemoryStore)
    assert runtime.operator_sessions is not None
    for tenant, expected_status in (("tenant-oncology", 200), ("tenant-other", 404)):
        principal_id = uuid4()
        asyncio.run(
            runtime.store.create_operator_principal(
                principal_id=principal_id,
                request=OperatorPrincipalCreate(
                    subject=f"viewer-{tenant}",
                    display_name="Test viewer",
                    kind=PrincipalKind.HUMAN,
                    role=OperatorRole.VIEWER,
                    tenant_id=tenant,
                ),
                actor="test-bootstrap",
            )
        )
        cookie = asyncio.run(runtime.operator_sessions.issue(principal_id, actor="test-bootstrap")).cookie_value
        assert (
            client.get(endpoint, headers={"cookie": f"{ADMIN_SESSION_COOKIE}={cookie}"}).status_code == expected_status
        )


def test_cancel_route_requires_the_operator_role_and_is_absent_without_a_writer(registry, cipher, hasher) -> None:
    runtime = _runtime(registry, cipher, hasher)
    assert isinstance(runtime.store, MemoryStore)
    assert runtime.operator_sessions is not None
    viewer_id = uuid4()
    asyncio.run(
        runtime.store.create_operator_principal(
            principal_id=viewer_id,
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
    cookie = asyncio.run(runtime.operator_sessions.issue(viewer_id, actor="test-bootstrap")).cookie_value
    headers = {"cookie": f"{ADMIN_SESSION_COOKIE}={cookie}"}
    client = TestClient(create_app(runtime), base_url="https://inference.test.invalid")

    assert client.post(f"/admin/api/v1/scientific-runs/{OPERATION_ID}:cancel").status_code == 401
    denied = client.post(f"/admin/api/v1/scientific-runs/{OPERATION_ID}:cancel", headers=headers)
    assert denied.status_code == 403
    assert client.get(f"/admin/api/v1/scientific-runs/{OPERATION_ID}", headers=headers).status_code == 200
    controls = cast(ControlAdapter, cast(Any, runtime.scientific_admin).controls)
    assert controls.calls == []
    denials = [event for event in runtime.store.audit if event.action == "admin.authorization"]
    assert any(event.target_id == "scientific_run.cancel" and event.outcome == "failed" for event in denials)

    read_only = _runtime(registry, cipher, hasher)
    read_only.scientific_admin = ScientificAdminReadService(
        runs=RunAdapter(), models=ModelAdapter(), clock=lambda: FIXED_NOW
    )
    read_only_client = TestClient(create_app(read_only), base_url="https://inference.test.invalid")
    session = read_only_client.post("/admin/api/v1/session", headers={"authorization": f"Bearer {'a' * 32}"})
    assert session.status_code == 200
    capabilities = read_only_client.get("/admin/api/v1/scientific-capabilities")
    assert capabilities.json()["data"]["run_control"]["available"] is False
    assert read_only_client.get(f"/admin/api/v1/scientific-runs/{OPERATION_ID}").status_code == 200
    # Without a writer the command route is never registered; only the GET
    # projection matches the identifier, so the method itself is refused.
    assert read_only_client.post(f"/admin/api/v1/scientific-runs/{OPERATION_ID}:cancel").status_code == 405


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


async def test_policy_list_scopes_the_catalog_models_and_reports_desired_effective_and_counts() -> None:
    policies = PolicyAdapter()
    envelope = await _service(policies=policies).policy_list(_context(), tenant_id="tenant-oncology")

    assert policies.list_calls == [("tenant-oncology", ("boltzgen", "rfdiffusion"))]
    assert envelope.data.scope_tenant_id == "tenant-oncology"
    capped = next(item for item in envelope.data.items if item.model_id == "rfdiffusion")
    assert capped.desired.revision == 2 and capped.desired.max_active_runs == 1
    assert capped.effective.state == "at-limit" and capped.effective.max_active_runs == 1
    assert (capped.counts.queued, capped.counts.running) == (2, 1)
    assert capped.inherited is not None and capped.inherited.revision == 0
    assert capped.enforcement.preemptive is False and capped.enforcement.resident_runtime == "none-batch-jobs-only"
    open_model = next(item for item in envelope.data.items if item.model_id == "boltzgen")
    assert open_model.desired.revision == 0 and open_model.effective.state == "open"
    assert envelope.meta.sources[0].id == "scientific-controller"
    assert envelope.meta.sources[0].state is AdminSourceState.AVAILABLE

    capabilities = _service().capabilities()
    assert capabilities.model_policy.available is True
    read_only = ScientificAdminReadService(runs=RunAdapter(), models=ModelAdapter(), clock=lambda: FIXED_NOW)
    assert read_only.capabilities().model_policy.available is False
    assert read_only.capabilities().model_policy.reason is not None
    with pytest.raises(ValueError):
        ScientificAdminReadService(policies=PolicyAdapter())


async def test_set_policy_replaces_the_scope_row_with_the_operator_as_actor() -> None:
    policies = PolicyAdapter()
    update = ScientificModelPolicyUpdate(expected_revision=2, paused=True, max_active_runs=3, reason="maintenance")
    envelope = await _service(policies=policies).set_policy(
        _context(), "rfdiffusion", tenant_id=None, update=update, actor="operator-ada"
    )

    assert policies.set_calls == [("rfdiffusion", None, update, "operator-ada")]
    assert envelope.data.desired.revision == 3
    assert envelope.data.desired.paused is True and envelope.data.effective.state == "paused"
    assert envelope.data.scope_tenant_id is None and envelope.data.inherited is None


async def test_set_policy_answers_stable_problems_for_unknown_model_stale_revision_and_failures() -> None:
    update = ScientificModelPolicyUpdate(expected_revision=0, paused=True, max_active_runs=None, reason=None)
    with pytest.raises(AdminProblemError) as unknown:
        await _service().set_policy(_context(), "not-a-catalog-model", tenant_id=None, update=update, actor="op")
    assert (unknown.value.status_code, unknown.value.code) == (404, "scientific_model_not_found")

    with pytest.raises(AdminProblemError) as stale:
        await _service(policies=PolicyAdapter(stale=4)).set_policy(
            _context(), "rfdiffusion", tenant_id=None, update=update, actor="op"
        )
    assert (stale.value.status_code, stale.value.code) == (409, "scientific_model_policy_stale")
    assert "current revision is 4" in stale.value.detail

    class InvalidStartupPolicy(PolicyAdapter):
        async def set_policy(self, *args, **kwargs):
            raise ScientificModelPolicyInvalidError("snapshot bundle is not available for this stage")

    with pytest.raises(AdminProblemError) as invalid:
        await _service(policies=InvalidStartupPolicy()).set_policy(
            _context(), "rfdiffusion", tenant_id=None, update=update, actor="op"
        )
    assert (invalid.value.status_code, invalid.value.code) == (422, "scientific_model_policy_invalid")

    with pytest.raises(AdminProblemError) as failing:
        await _service(policies=FailingPolicyAdapter()).set_policy(
            _context(), "rfdiffusion", tenant_id=None, update=update, actor="op"
        )
    assert (failing.value.status_code, failing.value.code) == (503, "scientific_controller_unavailable")
    assert "SENSITIVE" not in failing.value.detail

    read_only = ScientificAdminReadService(runs=RunAdapter(), models=KnownModelAdapter(), clock=lambda: FIXED_NOW)
    with pytest.raises(AdminProblemError) as unconfigured:
        await read_only.set_policy(_context(), "rfdiffusion", tenant_id=None, update=update, actor="op")
    assert (unconfigured.value.status_code, unconfigured.value.code) == (503, "scientific_model_policy_unavailable")
    with pytest.raises(AdminProblemError):
        await read_only.policy_list(_context())

    with pytest.raises(ValueError):
        ScientificModelPolicyUpdate(expected_revision=0, paused=False, max_active_runs=65, reason=None)
    with pytest.raises(ValueError):
        ScientificModelPolicyUpdate(expected_revision=-1, paused=False, max_active_runs=None, reason=None)
    with pytest.raises(ValueError):
        ScientificModelPolicy.model_validate(
            _policy("rfdiffusion").model_dump() | {"desired": {"tenant_id": None, "revision": 0, "paused": True}}
        )


def test_policy_routes_require_the_operator_role_and_are_absent_without_a_repository(registry, cipher, hasher) -> None:
    runtime = _runtime(registry, cipher, hasher)
    assert isinstance(runtime.store, MemoryStore)
    assert runtime.operator_sessions is not None
    client = TestClient(create_app(runtime), base_url="https://inference.test.invalid")
    body = {"expected_revision": 0, "paused": True, "max_active_runs": 2, "reason": "PoC window"}
    assert client.get("/admin/api/v1/scientific-model-policies").status_code == 401
    assert client.put("/admin/api/v1/scientific-model-policies/rfdiffusion", json=body).status_code == 401

    assert client.post("/admin/api/v1/session", headers={"authorization": f"Bearer {'a' * 32}"}).status_code == 200
    capabilities = client.get("/admin/api/v1/scientific-capabilities")
    assert capabilities.json()["data"]["model_policy"] == {"available": True, "reason": None}
    listed = client.get("/admin/api/v1/scientific-model-policies")
    assert listed.status_code == 200
    assert listed.json()["data"]["scope_tenant_id"] is None
    assert {item["model_id"] for item in listed.json()["data"]["items"]} == {"rfdiffusion", "boltzgen"}
    scoped = client.get("/admin/api/v1/scientific-model-policies?tenant_id=tenant-oncology")
    assert scoped.status_code == 200 and scoped.json()["data"]["scope_tenant_id"] == "tenant-oncology"

    applied = client.put("/admin/api/v1/scientific-model-policies/rfdiffusion", json=body)
    assert applied.status_code == 200
    assert applied.json()["data"]["desired"]["revision"] == 1
    assert applied.json()["data"]["desired"]["paused"] is True
    policies = cast(PolicyAdapter, cast(Any, runtime.scientific_admin).policies)
    assert policies.set_calls[-1][0] == "rfdiffusion" and policies.set_calls[-1][1] is None
    assert policies.set_calls[-1][3] == "bootstrap-admin"
    tenant_scoped = client.put(
        "/admin/api/v1/scientific-model-policies/rfdiffusion?tenant_id=tenant-oncology", json=body
    )
    assert tenant_scoped.status_code == 200 and policies.set_calls[-1][1] == "tenant-oncology"
    assert client.put("/admin/api/v1/scientific-model-policies/not-a-model", json=body).status_code == 404
    assert (
        client.put(
            "/admin/api/v1/scientific-model-policies/rfdiffusion", json={**body, "max_active_runs": 65}
        ).status_code
        == 422
    )
    assert (
        client.put("/admin/api/v1/scientific-model-policies/rfdiffusion", json={**body, "extra": 1}).status_code == 422
    )
    assert client.put(f"/admin/api/v1/scientific-model-policies/{'m' * 129}", json=body).status_code == 400

    viewer_id = uuid4()
    asyncio.run(
        runtime.store.create_operator_principal(
            principal_id=viewer_id,
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
    cookie = asyncio.run(runtime.operator_sessions.issue(viewer_id, actor="test-bootstrap")).cookie_value
    headers = {"cookie": f"{ADMIN_SESSION_COOKIE}={cookie}"}
    viewer_list = client.get("/admin/api/v1/scientific-model-policies", headers=headers)
    assert viewer_list.status_code == 200 and viewer_list.json()["data"]["scope_tenant_id"] == "tenant-oncology"
    assert (
        client.get("/admin/api/v1/scientific-model-policies?tenant_id=tenant-other", headers=headers).status_code == 403
    )
    set_calls = len(policies.set_calls)
    assert (
        client.put("/admin/api/v1/scientific-model-policies/rfdiffusion", json=body, headers=headers).status_code == 403
    )
    assert len(policies.set_calls) == set_calls
    denials = [event for event in runtime.store.audit if event.action == "admin.authorization"]
    assert any(event.target_id == "scientific_model_policy.set" and event.outcome == "failed" for event in denials)

    read_only = _runtime(registry, cipher, hasher)
    read_only.scientific_admin = ScientificAdminReadService(
        runs=RunAdapter(), models=KnownModelAdapter(), clock=lambda: FIXED_NOW
    )
    read_only_client = TestClient(create_app(read_only), base_url="https://inference.test.invalid")
    assert (
        read_only_client.post("/admin/api/v1/session", headers={"authorization": f"Bearer {'a' * 32}"}).status_code
        == 200
    )
    assert (
        read_only_client.get("/admin/api/v1/scientific-capabilities").json()["data"]["model_policy"]["available"]
        is False
    )
    assert read_only_client.get("/admin/api/v1/scientific-model-policies").status_code == 404
    assert read_only_client.put("/admin/api/v1/scientific-model-policies/rfdiffusion", json=body).status_code == 404
