"""Real PostgreSQL contracts for independent apps and durable inference owners."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

from fs2_serve.admin_models import AdminContext, AdminOperationQuery
from fs2_serve.app_observability import AppObservationHistory
from fs2_serve.apps_models import AppObservabilityTarget, AppRecord
from fs2_serve.apps_repository import AppConflictError, PostgresAppsRepository
from fs2_serve.auth import PepperRing, TokenService
from fs2_serve.lifecycle import LifecycleCorrelation, LifecycleSignal, LifecycleSubject, PostgresLifecycleRepository
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.postgres import PostgresStore
from fs2_serve.user_models import InferenceUser, owner_id
from fs2_serve.user_repository import PostgresUserRepository

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
CONTEXT = AdminContext(from_at=NOW - timedelta(hours=1), to_at=NOW + timedelta(hours=1), timezone="UTC")


@pytest_asyncio.fixture
async def database(cipher, hasher):
    url = os.environ.get("FS2_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FS2_TEST_DATABASE_URL is not set")
    store = await PostgresStore.connect(
        url, Path(__file__).parents[1] / "migrations", cipher, hasher, payload_ttl_seconds=3600
    )
    await store.migrate()
    clean = """TRUNCATE fs2_inference_users,fs2_apps,fs2_operations,fs2_tokens,
        fs2_telemetry_subjects,fs2_request_telemetry,fs2_scientific_model_policies RESTART IDENTITY CASCADE"""
    await store.pool.execute(clean)
    try:
        yield store
    finally:
        await store.pool.execute(clean)
        await store.close()


async def token(store, principal="researcher", tenant="tenant-a"):
    tokens = TokenService(store, PepperRing(active_key_id="test", keys={"test": b"t" * 32}))
    return await tokens.issue(
        TokenCreate(
            principal_id=principal, tenant_id=tenant, scopes={Scope.INFERENCE_INVOKE}, models={"*"}, max_concurrency=8
        ),
        created_by="different-key-issuer",
    )


async def operation(store, key, *, model="qwen3-8b", at=NOW, protocol="openai-chat", http_status=200):
    operation_id = uuid4()
    await store.pool.execute(
        """INSERT INTO fs2_operations
        (id,tenant_id,principal_id,token_id,model_id,model_revision,protocol,operation,idempotency_key,
         request_hmac_key_id,request_hmac,request_content_type,status,accepted_at,available_at,completed_at,
         http_status,payload_expires_at,max_attempts,input_tokens,output_tokens)
        VALUES($1,$2,$3,$4,$5,'revision',$6,'test',$7,'test',$8,'application/json','succeeded',$9,$9,$9,
               $10,$9::timestamptz+interval '1 day',1,3,7)""",
        operation_id,
        key.tenant_id,
        key.principal_id,
        key.id,
        model,
        protocol,
        f"test-{operation_id}",
        "0" * 64,
        at,
        http_status,
    )
    return operation_id


def app(route="qwen3-8b", deployment="qwen-hot"):
    return AppRecord(
        app_id=uuid4(),
        display_name="Qwen",
        model_ref="qwen3-8b",
        public_model_id=route,
        execution_mode="serving",
        namespace="models",
        deployment_name=deployment,
        created_at=NOW,
        updated_at=NOW,
    )


async def test_actual_user_discovery_settings_and_usage_across_key_rotation(database):
    repository = PostgresUserRepository(database.pool)
    key = await token(database)
    second = await token(database)
    foreign = await token(database, tenant="tenant-b")
    await operation(database, key)
    await operation(database, second, protocol="scientific-batch-v1")
    await operation(database, second, protocol="scientific-artifact-upload-v1")
    await operation(database, second, protocol="scientific-artifact-upload-v1")
    await operation(database, key, at=NOW - timedelta(days=3))
    await operation(database, foreign)
    rows = await repository.list("tenant-a")
    assert len(rows) == 1 and rows[0].id == owner_id("tenant-a", "researcher")
    assert rows[0].source == "existing-key-owner" and rows[0].academic_eligible is None
    assert not await database.pool.fetchval("SELECT count(*) FROM fs2_inference_users")
    saved = await repository.save(
        InferenceUser(
            **{**rows[0].model_dump(), "display_name": "Research owner", "academic_eligible": False, "enabled": False}
        )
    )
    assert saved.source == "configured" and not saved.enabled
    assert (await repository.configured("tenant-a", "researcher")).display_name == "Research owner"
    assert await repository.configured("tenant-b", "researcher") is None
    assert len(await repository.keys("tenant-a", "researcher")) == 2
    usage = await repository.usage("tenant-a", "researcher", CONTEXT)
    assert usage.requests == 2 and usage.scientific_requests == 1
    assert usage.input_tokens.value == 6 and usage.output_tokens.value == 14
    assert sum(point.requests for point in usage.request_series) == 2
    assert usage.scheduler_occupied_gpu_seconds.value is None


async def test_actual_apps_seed_is_create_only_and_concurrent_settings_are_fenced(database):
    repository = PostgresAppsRepository(database.pool)
    original = await repository.seed(app())
    duplicate = await repository.seed(app("app-independent", "qwen-independent"))
    assert original.model_ref == duplicate.model_ref and original.public_model_id != duplicate.public_model_id
    outcomes = await asyncio.gather(
        repository.update(original.model_copy(update={"display_name": "A"}), expected_revision=1),
        repository.update(original.model_copy(update={"display_name": "B"}), expected_revision=1),
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, AppConflictError) for outcome in outcomes) == 1
    current = await repository.get(original.app_id)
    assert current.revision == 2
    restarted = await repository.seed(original)
    assert restarted.display_name == current.display_name and restarted.revision == 2
    assert (await repository.get(duplicate.app_id)).revision == 1


async def test_actual_apps_usage_is_independent_windowed_and_owner_attributed(database):
    repository = PostgresAppsRepository(database.pool)
    key = await token(database)
    second = await token(database, principal="service-owner")
    first_run = await operation(database, key)
    second_run = await operation(database, second, http_status=503)
    await operation(database, key, at=NOW - timedelta(days=3))
    await operation(database, key, model="app-independent")
    await operation(database, key, protocol="scientific-artifact-upload-v1", at=NOW + timedelta(minutes=1))
    usage = await repository.usage("qwen3-8b", CONTEXT, None)
    assert usage["logical_runs"] == 2 and usage["unique_users"] == 2
    assert usage["status_classes"] == {"2xx": 1, "5xx": 1}
    assert usage["input_tokens"] == 6 and usage["output_tokens"] == 14
    assert {row["principal_id"] for row in usage["users"]} == {"researcher", "service-owner"}
    assert sum(row["logical_runs"] for row in usage["requests_over_time"]) == 2
    assert usage["scientific_gpu"] is None
    assert (await repository.usage("app-independent", CONTEXT, None))["logical_runs"] == 1
    empty = CONTEXT.model_copy(update={"from_at": NOW + timedelta(minutes=1)})
    assert (await repository.usage("qwen3-8b", empty, None))["logical_runs"] == 0
    assert await repository.last_used("qwen3-8b", None) == NOW
    query = AdminOperationQuery(
        from_at=CONTEXT.from_at,
        to_at=CONTEXT.to_at,
        model_id="qwen3-8b",
        limit=1,
        exclude_protocols=("scientific-artifact-upload-v1",),
    )
    first_page = await database.admin_list_operations(query)
    assert len(first_page) == 1 and first_page[0].protocol == "openai-chat"
    second_query = query.model_copy(update={"after_at": first_page[0].accepted_at, "after_id": first_page[0].id})
    second_page = await database.admin_list_operations(second_query)
    assert {first_page[0].id, second_page[0].id} == {first_run, second_run}
    third = second_query.model_copy(update={"after_at": second_page[0].accepted_at, "after_id": second_page[0].id})
    assert not await database.admin_list_operations(third)
    original_history = await database.admin_list_operations(
        query.model_copy(update={"exclude_protocols": (), "limit": 200})
    )
    assert len(original_history) == 3 and original_history[0].protocol == "scientific-artifact-upload-v1"


async def test_actual_scientific_attempt_join_reports_missing_rollup_not_zero(database):
    key = await token(database)
    op = await operation(database, key, protocol="scientific-batch-v1")
    subject = LifecycleSubject(
        subject_id=uuid4(),
        workload_kind="scientific_batch",
        operation_id=op,
        request_id=op,
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=uuid4(),
        tenant_id=key.tenant_id,
        principal_id=key.principal_id,
        model_id="qwen3-8b",
        model_revision="test",
        protocol="scientific-batch-v1",
        trace_id="1" * 32,
        parent_span_id="2" * 16,
        accepted_at=NOW,
    )
    await PostgresLifecycleRepository(database.pool).register_subject(subject)
    usage = await PostgresUserRepository(database.pool).usage(key.tenant_id, key.principal_id, CONTEXT)
    assert usage.requests == 1 and usage.scheduler_occupied_gpu_seconds.value is None
    assert (await PostgresAppsRepository(database.pool).usage("qwen3-8b", CONTEXT, None))["scientific_gpu"] is None
    # One fully accounted attempt cannot hide a different scientific operation
    # whose lifecycle subject has not arrived at all.
    await database.pool.execute(
        """INSERT INTO fs2_lifecycle_rollups
        (rollup_id,subject_id,generated_at,event_watermark,events_sha256,terminal,outcome,
         quota_reserved_gpu_seconds,scheduler_occupied_gpu_seconds,device_allocated_gpu_seconds,
         active_gpu_seconds,occupied_idle_gpu_seconds,phase_gpu_seconds,reconciliation_delta_seconds,
         device_scheduler_delta_seconds,tolerance_seconds,reconciled,quality,data_gaps,output_shape)
        VALUES($1,$2,$3,1,$4,true,'succeeded',10,10,10,7,3,'{}',0,0,1,true,'estimated','{}','{}')""",
        uuid4(),
        subject.subject_id,
        NOW,
        "f" * 64,
    )
    usage = await PostgresUserRepository(database.pool).usage(key.tenant_id, key.principal_id, CONTEXT)
    assert usage.scheduler_occupied_gpu_seconds.value == 10
    assert (await PostgresAppsRepository(database.pool).usage("qwen3-8b", CONTEXT, None))["scientific_gpu"] == {
        "occupied_seconds": 10,
        "active_compute_seconds": 7,
        "occupied_idle_seconds": 3,
    }
    await operation(database, key, protocol="scientific-batch-v1")
    usage = await PostgresUserRepository(database.pool).usage(key.tenant_id, key.principal_id, CONTEXT)
    assert usage.scientific_requests == 2 and usage.scheduler_occupied_gpu_seconds.value is None
    assert (await PostgresAppsRepository(database.pool).usage("qwen3-8b", CONTEXT, None))["scientific_gpu"] is None


async def test_actual_custom_runtime_role_can_manage_users_apps_and_read_owner_history(database):
    suffix = uuid4().hex[:10]
    role = f"fs2_users_runtime_{suffix}"
    await PostgresStore.migrate_database(
        os.environ["FS2_TEST_DATABASE_URL"],
        Path(__file__).parents[1] / "migrations",
        f"fs2_users_reporting_{suffix}",
        role,
        f"fs2_users_maintenance_{suffix}",
        f"fs2_users_activation_{suffix}",
    )
    key = await token(database)
    operation_id = await operation(database, key)

    async def assume(connection):
        await connection.execute(f'SET ROLE "{role}"')  # noqa: S608 - locally generated identifier

    pool = await asyncpg.create_pool(os.environ["FS2_TEST_DATABASE_URL"], min_size=1, max_size=2, init=assume)
    try:
        users = PostgresUserRepository(pool)
        row = (await users.list("tenant-a"))[0]
        saved = await users.save(row.model_copy(update={"display_name": "Runtime persisted", "enabled": False}))
        assert not saved.enabled
        assert (await users.usage("tenant-a", "researcher", CONTEXT)).requests == 1
        apps = PostgresAppsRepository(pool)
        created = await apps.seed(app())
        changed = await apps.update(created.model_copy(update={"display_name": "Runtime app"}), expected_revision=1)
        assert changed.revision == 2
        assert (await apps.usage(created.public_model_id, CONTEXT, "tenant-a"))["logical_runs"] == 1
        async with pool.acquire() as connection:
            assert await connection.fetchval(
                "SELECT has_table_privilege(current_user,'fs2_request_telemetry','SELECT,INSERT')"
            )
        from test_request_telemetry_postgres import observation

        from fs2_serve.request_telemetry import PostgresRequestTelemetryStore

        telemetry = PostgresRequestTelemetryStore(pool)
        await telemetry.record(observation(operation_id, key))
        assert (await telemetry.usage("qwen3-8b", CONTEXT.from_at, CONTEXT.to_at, "tenant-a")).request_count == 1
    finally:
        await pool.close()


async def test_actual_app_history_reads_exact_pod_and_gpu_allocation_identity(database):
    key = await token(database)
    op = await operation(database, key, model="app-independent", protocol="scientific-batch-v1")
    lifecycle = PostgresLifecycleRepository(database.pool)
    subject = LifecycleSubject(
        subject_id=uuid4(),
        workload_kind="scientific_batch",
        operation_id=op,
        request_id=op,
        batch_id=uuid4(),
        workload_id=uuid4(),
        attempt_id=uuid4(),
        tenant_id=key.tenant_id,
        principal_id=key.principal_id,
        model_id="app-independent",
        model_revision="test",
        protocol="scientific-batch-v1",
        trace_id="1" * 32,
        parent_span_id="2" * 16,
        accepted_at=NOW,
    )
    await lifecycle.register_subject(subject)
    await lifecycle.append_correlations(
        [
            LifecycleCorrelation(
                correlation_key="test-pod",
                subject_id=subject.subject_id,
                observed_at=NOW,
                source="kubernetes",
                namespace="models",
                pod_name="exact-pod",
                pod_uid="exact-pod-uid",
            )
        ]
    )
    await lifecycle.append_signals(
        [
            LifecycleSignal(
                event_key=f"gpu-{edge}",
                subject_id=subject.subject_id,
                occurred_at=at,
                observed_at=at,
                source="kubelet",
                quality="measured",
                phase="gpu_allocation",
                edge=edge,
                clock="device_allocated",
                interval_key="allocation",
                namespace="models",
                pod_name="exact-pod",
                pod_uid="exact-pod-uid",
                gpu_uuid="GPU-test",
                gpu_rank=0,
                gpu_count=1,
            )
            for edge, at in (("start", NOW), ("end", NOW + timedelta(seconds=10)))
        ]
    )
    target = AppObservabilityTarget(
        app_id=str(uuid4()), model_id="app-independent", namespace="models", pod_labels={}, execution_mode="scientific"
    )
    rows = await AppObservationHistory(database.pool).pods(target, CONTEXT.from_at, CONTEXT.to_at)
    assert len(rows) == 1
    assert rows[0].uid == "exact-pod-uid"
    assert rows[0].run_id == str(op)
    assert rows[0].gpu_windows == {"GPU-test": [(NOW, NOW + timedelta(seconds=10))]}
    assert not await AppObservationHistory(database.pool).pods(
        target.model_copy(update={"model_id": "qwen3-8b"}), CONTEXT.from_at, CONTEXT.to_at
    )


async def test_actual_independent_scientific_app_dispatch_limits_and_pause(database):
    from dataclasses import replace

    from scientific_batch_fakes import FakeScientificBatchCluster
    from test_scientific_model_policy_postgres import PLAN, controller_for, durable_input_artifact, scheduling_for

    from fs2_serve.models import AdmissionRequest, Principal
    from fs2_serve.scientific_batch.models import ServiceClass
    from fs2_serve.scientific_batch.policy import PostgresScientificModelPolicyRepository
    from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository

    key = await token(database)
    principal = Principal(
        token_id=key.id,
        token_prefix=key.prefix,
        principal_id=key.principal_id,
        tenant_id=key.tenant_id,
        scopes=frozenset(key.scopes),
        models=frozenset({"*"}),
        max_concurrency=8,
    )
    routes = [f"app-{uuid4().hex}", f"app-{uuid4().hex}"]
    policies = PostgresScientificModelPolicyRepository(database.pool)
    await policies.set(
        routes[0], tenant_id=None, expected_revision=0, paused=False, max_active_runs=1, reason=None, actor="test"
    )
    await policies.set(
        routes[1], tenant_id=None, expected_revision=0, paused=True, max_active_runs=2, reason=None, actor="test"
    )
    ids = []
    for route in (routes[0], routes[0], routes[1]):
        operation = await database.append_operation(
            principal=principal,
            admission=AdmissionRequest(
                model_id=route,
                operation="generate-backbone",
                protocol="scientific-batch-v1",
                idempotency_key=f"test-{uuid4()}",
                request_body=b"{}",
            ),
            model_revision="2" * 40,
            reserved_gpu_seconds=0,
            max_attempts=1,
        )
        artifact_id = uuid4()
        await durable_input_artifact(database, operation.id, artifact_id=artifact_id, tenant_id=key.tenant_id)
        await PostgresScientificBatchRepository(database.pool).create(
            operation_id=operation.id,
            tenant_id=key.tenant_id,
            model_id=route,
            variant_id="rfdiffusion-h100",
            input_artifact_id=artifact_id,
            plan=PLAN,
            scheduling=replace(scheduling_for(ServiceClass.CUSTOMER_BATCH, operation.accepted_at), model_lane=route),
        )
        ids.append(operation.id)
    controller = controller_for(database, FakeScientificBatchCluster(), controller_id="test-independent-apps")
    await controller.reconcile_once()
    first = await policies.get(routes[0], tenant_id=None)
    second = await policies.get(routes[1], tenant_id=None)
    source = await policies.get("rfdiffusion", tenant_id=None)
    assert (first.counts.running, first.counts.queued) == (1, 1)
    assert (second.counts.running, second.counts.queued) == (0, 1)
    assert source.policy is None
    assert await database.pool.fetchval("SELECT count(*) FROM fs2_operations WHERE id=ANY($1::uuid[])", ids) == 3
    from fs2_serve.capacity_summary import CapacitySummaryService

    rows = await database.pool.fetch(
        "SELECT o.status,b.state FROM fs2_operations o JOIN fs2_scientific_batches b ON b.operation_id=o.id"
    )
    expected = 0
    for row in rows:
        state = json.loads(row["state"]) if isinstance(row["state"], str) else row["state"]
        waiting = any(
            attempt["outcome"] == "active" and attempt.get("last_phase") in {"queued", "scheduling", "node_pending"}
            for stage in state["stages"]
            for attempt in stage["attempts"]
        )
        expected += row["status"] in {"queued", "activating"} or row["status"] == "running" and waiting
    pending, _ = await CapacitySummaryService(SimpleNamespace(capacity_adapter=None), database)._queue(
        datetime.now(UTC)
    )
    assert pending.value == expected  # Real SQL, including existing integer o.attempt and JSON attempts.
    # The separate paused row can resume without mutating the first app's limit.
    await policies.set(
        routes[1], tenant_id=None, expected_revision=1, paused=False, max_active_runs=2, reason=None, actor="test"
    )
    await controller.reconcile_once()
    await controller.reconcile_once()
    assert (await policies.get(routes[0], tenant_id=None)).policy.max_active_runs == 1
    assert (await policies.get(routes[1], tenant_id=None)).counts.running == 1


async def test_actual_capacity_queue_excludes_upload_bookkeeping_without_deleting_history(database):
    from fs2_serve.capacity_summary import CapacitySummaryService

    key = await token(database)
    run = await operation(database, key)
    upload = await operation(database, key, protocol="scientific-artifact-upload-v1")
    await database.pool.execute(
        "UPDATE fs2_operations SET status='queued',completed_at=NULL WHERE id=ANY($1::uuid[])", [run, upload]
    )
    pending, oldest = await CapacitySummaryService(SimpleNamespace(capacity_adapter=None), database)._queue(
        NOW + timedelta(seconds=30)
    )
    assert pending.value == 1 and oldest.value == 30
    assert await database.pool.fetchval("SELECT count(*) FROM fs2_operations") == 2


async def test_actual_serving_clone_admission_replay_payload_and_fences(database, registry):
    from test_admission_workers import service
    from test_apps import _app_spec
    from test_dynamic_routes import _principal, _revision
    from test_model_deployment_publication import status_view

    from fs2_serve.model_deployment import DesiredState, spec_digest
    from fs2_serve.model_deployment_publication import project_dynamic_publications
    from fs2_serve.model_deployment_records import ModelDeploymentAppendRequest, ModelDeploymentRevisionAction
    from fs2_serve.models import AdmissionRequest, DynamicAdmissionFence
    from fs2_serve.runtime import RuntimeClient
    from fs2_serve.store import ConflictError

    original = _revision(registry)
    spec = _app_spec(original.spec)
    revision = original.model_copy(update={"name": spec.public_model_id, "spec": spec, "etag": spec_digest(spec)})
    registry.set_dynamic_publications(
        project_dynamic_publications([revision], {(revision.namespace, revision.name): status_view(revision)}),
        valid_until=datetime.now(UTC) + timedelta(minutes=10),
    )
    create = ModelDeploymentAppendRequest(
        namespace=revision.namespace,
        name=revision.name,
        expected_etag=None,
        spec=spec,
        action=ModelDeploymentRevisionAction.CREATE,
        actor_id=uuid4(),
        actor="test",
        idempotency_key=f"clone-{uuid4()}",
    )
    await database.model_deployment_append_revision(create)
    principal = _principal().model_copy(update={"models": frozenset({spec.public_model_id})})
    await database.issue_token(
        token_id=principal.token_id,
        prefix=principal.token_prefix,
        pepper_key_id="test",
        digest="test",
        request=TokenCreate(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            scopes={Scope.INFERENCE_INVOKE},
            models={spec.public_model_id},
            max_concurrency=8,
        ),
        created_by="test",
    )
    request = AdmissionRequest(
        model_id=spec.public_model_id,
        operation="chat",
        protocol="openai-chat",
        idempotency_key=f"clone-request-{uuid4()}",
        request_body=json.dumps(
            {"model": spec.public_model_id, "messages": [{"role": "user", "content": "test"}]}
        ).encode(),
    )
    sent = []

    async def respond(outbound):
        sent.append(outbound)
        assert json.loads(outbound.content)["model"] == "qwen3-8b"
        assert spec.public_model_id in str(outbound.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
        runtime = RuntimeClient(
            activation_timeout_seconds=2, runtime_timeout_seconds=2, max_response_bytes=4096, client=client
        )
        admission = service(registry, database, runtime)
        admitted = await admission.admit(principal, request)
        replay = await admission.admit(principal, request)
        assert replay.id == admitted.id and replay.reused
        assert admitted.model_id == spec.public_model_id
        claimed = await database.claim_operation("clone-test-worker", lease_seconds=30)
        assert claimed is not None and claimed.model_id == spec.public_model_id
        payload = await database.read_request_payload(
            claimed.id, worker_id="clone-test-worker", fencing_token=claimed.fencing_token
        )
        assert json.loads(payload)["model"] == "qwen3-8b"
        result = await runtime.invoke(registry.get(spec.public_model_id), claimed, payload)
        assert result.status_code == 200 and len(sent) == 1
        assert sent[0].headers["x-fs2-operation-id"] == str(admitted.id)

    model = registry.get(spec.public_model_id)

    async def reject_with(etag):
        with pytest.raises(ConflictError, match="no longer accepts"):
            await database.append_operation(
                principal=principal,
                admission=request.model_copy(update={"idempotency_key": f"denied-{uuid4()}"}),
                model_revision=model.model_revision,
                reserved_gpu_seconds=0,
                max_attempts=1,
                dispatch_snapshot=registry.dispatch_snapshot(model),
                dynamic_fence=DynamicAdmissionFence(namespace=revision.namespace, name=revision.name, etag=etag),
            )

    await reject_with("sha256:" + "f" * 64)
    disabled = spec.model_copy(
        update={
            "lifecycle": spec.lifecycle.model_copy(update={"desired_state": DesiredState.DISABLED}),
            "availability": spec.availability.model_copy(update={"min_replicas": 0}),
        }
    )
    await database.model_deployment_append_revision(
        create.model_copy(
            update={
                "expected_etag": revision.etag,
                "spec": disabled,
                "action": ModelDeploymentRevisionAction.UPDATE,
                "idempotency_key": f"disable-{uuid4()}",
            }
        )
    )
    await reject_with(spec_digest(disabled))
    assert (
        await database.pool.fetchval("SELECT count(*) FROM fs2_operations WHERE model_id=$1", spec.public_model_id) == 1
    )
