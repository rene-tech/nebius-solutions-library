"""Real PostgreSQL proofs for serial tenant-bound scientific child operations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from test_scientific_batch_postgres_state import durable_input_artifact

from fs2_serve.models import AdmissionRequest, OperationStatus, Principal, Scope, TokenCreate
from fs2_serve.scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from fs2_serve.scientific_batch.child_routes import PREFIX, scientific_child_router
from fs2_serve.scientific_batch.codec import state_to_value
from fs2_serve.scientific_batch.models import (
    AdapterExecutionPlan,
    ArtifactAccessContext,
    CheckpointMode,
    PreemptionMode,
    ResourceClass,
    SchedulingSnapshot,
    ScientificAttemptState,
    ScientificBatchPlan,
    ScientificInputArtifact,
    ScientificStagePlan,
    ScientificStageState,
    ServiceClass,
    StageInvocation,
    StageSchedulingDecision,
    VerifiedInputManifest,
    WorkloadKind,
    WorkloadRef,
)
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository
from fs2_serve.store import BudgetExceededError, ConcurrencyExceededError, ConflictError, NotFoundError, StaleLeaseError
from fs2_serve.user_models import InferenceUser, owner_id
from fs2_serve.user_repository import PostgresUserRepository
from fs2_serve.users import UserService

pytest_plugins = ("test_scientific_batch_postgres_state",)


async def parent_fixture(store, *, request_budget=10, gpu_budget=100, video=False):
    parent_model = "physical-ai-video-augmentation" if video else "cosmos3-lerobot-augmentation"
    stage = "augment-videos" if video else "augment-dataset"
    collector = "paidf-video-v1" if video else "cosmos3-lerobot-v3-0-6-1"
    token_id = uuid4()
    token = await store.issue_token(
        token_id=token_id,
        prefix=f"fs2_pat_{token_id.hex[:12]}",
        pepper_key_id="pepper-v1",
        digest="test-digest",
        request=TokenCreate(
            principal_id="scientist",
            tenant_id="tenant-lerobot",
            scopes={Scope.INFERENCE_INVOKE},
            models={"cosmos3-nano", parent_model},
            max_concurrency=1,
            request_budget=request_budget,
            gpu_seconds_budget=gpu_budget,
        ),
        created_by="local-test",
    )
    principal = Principal(
        token_id=token_id,
        token_prefix=token.prefix,
        principal_id=token.principal_id,
        tenant_id=token.tenant_id,
        scopes=frozenset(token.scopes),
        models=frozenset(token.models),
        max_concurrency=1,
    )
    parent = await store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id=parent_model,
            operation="augment-videos" if video else "augment-lerobot-dataset",
            protocol="scientific-batch-v1",
            idempotency_key="parent-augmentation",
            request_body=b"{}",
        ),
        model_revision="a" * 40,
        reserved_gpu_seconds=0,
        max_attempts=1,
    )
    artifact_id = uuid4()
    await durable_input_artifact(store, parent.id, artifact_id=artifact_id, tenant_id=principal.tenant_id)
    plan = ScientificBatchPlan((ScientificStagePlan(stage_id=stage, resource_class=ResourceClass.CPU),))
    scheduling = SchedulingSnapshot(
        policy_revision="a" * 64,
        captured_at=datetime.now(UTC),
        service_class=ServiceClass.CUSTOMER_BATCH,
        tenant_queue="scientific",
        model_lane=parent.model_id,
        workload_namespace="fs2-models",
        route_namespace="fs2-models",
        stages=(
            StageSchedulingDecision(
                stage_id=stage,
                resource_class=ResourceClass.CPU,
                resolved_cluster_queue="cpu",
                resolved_local_queue="scientific",
                workload_priority_class="scientific-customer-batch",
                workload_priority_value=500,
                resolved_pool_preference=(),
                accelerator_resource_name=None,
                accelerator_count=0,
                max_queue_seconds=None,
                max_execution_seconds=None,
                checkpoint_mode=CheckpointMode.RESTART,
                preemption_mode=PreemptionMode.RESTARTABLE,
            ),
        ),
    )
    batches = PostgresScientificBatchRepository(store.pool)
    invocation = StageInvocation(
        stage_id=stage,
        shard_id="main",
        argv=("lerobot-worker",),
        environment=(),
        working_directory="/mnt/fs2-scientific/test",
        consumes=(),
        produces="augmentation-result",
        collector_id=collector,
        validator_id=collector,
    )
    state = await batches.create(
        operation_id=parent.id,
        tenant_id=principal.tenant_id,
        model_id=parent.model_id,
        variant_id="cosmos3-nano-lerobot-v3",
        input_artifact_id=artifact_id,
        plan=plan,
        scheduling=scheduling,
        execution_plan=AdapterExecutionPlan(
            model_id=parent.model_id,
            variant_id="cosmos3-nano-lerobot-v3",
            source_revision="a" * 40,
            request_sha256="a" * 64,
            controller_plan=plan,
            invocations=(invocation,),
            required_model_artifacts=(),
        ),
        access_context=ArtifactAccessContext(profile="public", receipt_digest=None, tenant_id=principal.tenant_id),
        input_manifest=VerifiedInputManifest(
            manifest_id="request-inputs",
            manifest_artifact_id=artifact_id,
            manifest_digest="sha256:" + "a" * 64,
            entries=(
                ScientificInputArtifact(
                    logical_artifact_id="request",
                    semantic_type="request/v1",
                    artifact_id=artifact_id,
                    digest="sha256:" + "a" * 64,
                    size_bytes=1,
                    media_type="application/json",
                ),
            ),
        ),
    )
    attempt_id = uuid4()
    attempt = ScientificAttemptState(
        attempt_id=attempt_id,
        stage_id=stage,
        shard_id="main",
        attempt_number=1,
        workload=WorkloadRef(namespace="fs2-models", name="lerobot-test", kind=WorkloadKind.JOB),
    )
    value = state_to_value(replace(state, stages=(ScientificStageState(stage, attempts=(attempt,)),)))
    async with store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_scientific_batches SET state=$2::jsonb WHERE operation_id=$1", parent.id, json.dumps(value)
        )
    return principal, parent, attempt_id, batches


async def admit_child(store, principal, parent, attempt_id, *, key="first-cosmos-child", reservation=5, **changes):
    values = dict(
        model_id="cosmos3-nano",
        operation="generate-media",
        protocol="native",
        idempotency_key=key,
        request_body=b'{"mode":"video-to-video"}',
        parent_operation_id=parent.id,
        parent_attempt_id=attempt_id,
    )
    values.update(changes)
    return await store.append_operation(
        principal=principal,
        admission=AdmissionRequest(**values),
        model_revision="b" * 40,
        reserved_gpu_seconds=reservation,
        max_attempts=1,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("video", [False, True])
async def test_child_shares_parent_slot_but_preserves_identity_budget_and_exact_replay(store, video):
    principal, parent, attempt, _ = await parent_fixture(store, video=video)
    child = await admit_child(store, principal, parent, attempt)
    assert (child.tenant_id, child.principal_id, child.token_id) == (
        parent.tenant_id,
        parent.principal_id,
        parent.token_id,
    )
    assert (child.parent_operation_id, child.parent_attempt_id) == (parent.id, attempt)
    replay = await admit_child(store, principal, parent, attempt)
    assert replay.id == child.id and replay.reused
    token = await store.get_token(principal.token_id)
    assert token.requests_used == 2 and token.gpu_seconds_reserved == 5
    with pytest.raises(ConcurrencyExceededError):
        await admit_child(store, principal, parent, attempt, key="second-cosmos-child")
    with pytest.raises(ConcurrencyExceededError):
        await admit_child(
            store,
            principal,
            parent,
            attempt,
            key="ordinary-cosmos-child",
            parent_operation_id=None,
            parent_attempt_id=None,
        )
    with pytest.raises(ConflictError):
        await admit_child(store, principal, parent, uuid4())
    await store.cancel_operation(child.id, tenant_id=principal.tenant_id, actor=principal.principal_id)
    followup = await admit_child(store, principal, parent, attempt, key="next-cosmos-child")
    assert followup.id != child.id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_child_admissions_have_one_winner(store):
    principal, parent, attempt, _ = await parent_fixture(store)
    results = await asyncio.gather(
        *(admit_child(store, principal, parent, attempt, key=f"concurrent-child-{index}") for index in range(6)),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, Exception) for value in results) == 1
    assert sum(isinstance(value, ConcurrencyExceededError) for value in results) == 5


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["request", "gpu"])
async def test_delegation_never_bypasses_budgets(store, kind):
    principal, parent, attempt, _ = await parent_fixture(
        store,
        request_budget=1 if kind == "request" else 10,
        gpu_budget=1 if kind == "gpu" else 100,
    )
    with pytest.raises(BudgetExceededError):
        await admit_child(store, principal, parent, attempt)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["cancel", "parent-terminal", "attempt-replaced", "token-revoked"])
@pytest.mark.parametrize("video", [False, True])
async def test_parent_fence_stops_running_child_and_new_admissions(store, fence, video):
    principal, parent, attempt, batches = await parent_fixture(store, video=video)
    child = await admit_child(store, principal, parent, attempt)
    claimed = await store.claim_operation("worker-test", lease_seconds=30)
    assert claimed is not None and claimed.id == child.id
    if fence == "cancel":
        await batches.request_cancel(parent.id, tenant_id=principal.tenant_id, actor=principal.principal_id)
    elif fence == "parent-terminal":
        await store.cancel_operation(parent.id, tenant_id=principal.tenant_id, actor=principal.principal_id)
    elif fence == "token-revoked":
        await store.revoke_token(principal.token_id, actor="local-test")
    else:
        async with store.pool.acquire() as connection:
            await connection.execute(
                "UPDATE fs2_scientific_batches SET state=jsonb_set(state,'{stages,0,attempts,0,attempt_id}',"
                "to_jsonb($2::text)) WHERE operation_id=$1",
                parent.id,
                str(uuid4()),
            )
    with pytest.raises(StaleLeaseError):
        await store.heartbeat(child.id, worker_id="worker-test", fencing_token=claimed.fencing_token, lease_seconds=30)
    assert (await store.get_operation(child.id)).status in {OperationStatus.CANCELLED, OperationStatus.EXPIRED}
    with pytest.raises(PermissionError):
        await admit_child(store, principal, parent, attempt, key="fenced-new-child")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_queued_child_is_cancelled_by_parent_maintenance_fence(store):
    principal, parent, attempt, batches = await parent_fixture(store)
    child = await admit_child(store, principal, parent, attempt)
    await batches.request_cancel(parent.id, tenant_id=principal.tenant_id, actor=principal.principal_id)
    await store.expire_deadline_operations()
    assert (await store.get_operation(child.id)).status is OperationStatus.CANCELLED


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tenant", "principal", "model", "attempt"])
async def test_delegation_rejects_identity_or_target_change(store, change):
    principal, parent, attempt, _ = await parent_fixture(store)
    changes = {}
    if change in {"tenant", "principal"}:
        principal = principal.model_copy(update={f"{change}_id": "foreign"})
    elif change == "model":
        changes["model_id"] = "qwen3-8b"
    else:
        attempt = uuid4()
    with pytest.raises(PermissionError):
        await admit_child(store, principal, parent, attempt, **changes)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scoped_http_routes_reject_other_children_and_stale_or_modified_capabilities(store, hasher):
    principal, parent, attempt_id, batches = await parent_fixture(store)
    state = await batches.get(parent.id, tenant_id=principal.tenant_id)
    authority = ScientificWorkloadCapabilityAuthority(hasher)
    capability = authority.issue(
        SimpleNamespace(
            operation_id=parent.id,
            batch_id=state.batch_id,
            workload_id=state.workload_id,
            attempt_id=attempt_id,
            attempt_number=1,
            tenant_id=principal.tenant_id,
            model_id=parent.model_id,
            variant_id=state.variant_id,
            stage_id="augment-dataset",
            shard_id="main",
            invocation=state.execution_plan.invocations[0],
            materializations=(),
            access_context=state.access_context,
        )
    )
    child = await admit_child(store, principal, parent, attempt_id)
    users = UserService(PostgresUserRepository(store.pool), access=None)
    app = FastAPI()

    @app.exception_handler(NotFoundError)
    async def not_found(_request, _error):
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.exception_handler(PermissionError)
    async def forbidden(_request, _error):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    app.include_router(
        scientific_child_router(
            authority=authority,
            batches=batches,
            store=store,
            admission=None,
            uploads=None,
            artifacts=None,
            principal_policy=users.constrain_principal,
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"{PREFIX}/v1/operations/{child.id}"
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers={"Authorization": "Bearer customer-static-token"})).status_code == 401
        headers = {"Authorization": f"Bearer {capability}"}
        result = await client.get(path, headers=headers)
        assert result.status_code == 200
        assert result.json()["token_id"] == str(principal.token_id)
        assert result.json()["parent_operation_id"] == str(parent.id)
        assert (await client.get(f"{PREFIX}/v1/operations/{parent.id}", headers=headers)).status_code == 404
        rejected = await client.post(
            f"{PREFIX}/v1/models/cosmos3-nano:invoke",
            headers=headers,
            json={
                "operation": "generate-media",
                "payload": {"mode": "text-to-video", "output_delivery": "artifact"},
            },
        )
        assert rejected.status_code == 422
        malformed = await client.post(
            f"{PREFIX}/v1/models/cosmos3-nano:invoke",
            headers=headers,
            json={
                "operation": "generate-media",
                "payload": {"mode": "video-to-video", "output_delivery": "artifact"},
            },
        )
        assert malformed.status_code == 422
        # Current owner policy is reloaded even though parent and child were
        # admitted before the administrative change and the token is unchanged.
        now = datetime.now(UTC)
        owner = InferenceUser(
            id=owner_id(principal.tenant_id, principal.principal_id),
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            display_name="Dataset owner",
            source="configured",
            created_at=now,
            updated_at=now,
            app_ids=[],
        )
        await users.repository.save(owner)
        assert (await client.get(path, headers=headers)).status_code == 403
        await users.repository.save(owner.model_copy(update={"app_ids": None, "enabled": False}))
        assert (await client.get(path, headers=headers)).status_code == 403
        await users.repository.save(owner.model_copy(update={"app_ids": None, "enabled": True}))
        assert (await client.get(path, headers=headers)).status_code == 200
        assert (await store.get_token(principal.token_id)).revoked_at is None
        await batches.request_cancel(parent.id, tenant_id=principal.tenant_id, actor=principal.principal_id)
        assert (await client.get(path, headers=headers)).status_code == 409


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_child_upload_and_inference_sequentially_share_parent_slot(store):
    principal, parent, attempt, _ = await parent_fixture(store)
    upload = await admit_child(
        store,
        principal,
        parent,
        attempt,
        key="episode-input-upload",
        reservation=0,
        protocol="scientific-artifact-upload-v1",
        operation="upload",
    )
    assert await store.claim_operation("worker", lease_seconds=30) is None
    await store.complete_scientific_artifact_upload(
        upload.id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
    )
    generated = await admit_child(store, principal, parent, attempt)
    claimed = await store.claim_operation("worker", lease_seconds=30)
    assert claimed is not None and claimed.id == generated.id
    assert (await store.get_token(principal.token_id)).requests_used == 3


def test_public_native_and_upload_clients_cannot_supply_parent_association(registry, cipher, hasher):
    from test_api_mcp import TestClient, build_runtime, issue

    from fs2_serve.api import create_app

    runtime = build_runtime(registry, cipher, hasher)
    with TestClient(create_app(runtime)) as client:
        token = issue(client, principal="public-caller", scopes=["inference.invoke"])
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "forged-parent-association"}
        forged = {"parent_operation_id": str(uuid4()), "parent_attempt_id": str(uuid4())}
        native = client.post(
            "/v1/models/qwen3-8b:invoke",
            headers=headers,
            json={
                "operation": "generate",
                "payload": {},
                **forged,
            },
        )
        assert native.status_code == 422
        upload = client.post(
            "/v1/scientific-artifacts/uploads",
            headers=headers,
            json={
                "model_id": "qwen3-8b",
                "sha256": "a" * 64,
                "size_bytes": 16,
                "media_type": "video/mp4",
                **forged,
            },
        )
        assert upload.status_code == 422
        assert not runtime.store.operations
