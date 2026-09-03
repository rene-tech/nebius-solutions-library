from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import httpx2
import pytest
import pytest_asyncio
from conftest import CONTROL_ROOT
from fastapi import FastAPI
from scientific_batch_fakes import FakeScientificBatchCluster

from fs2_serve.access import AdminAccessService
from fs2_serve.access_models import (
    BOOTSTRAP_OPERATOR_PRINCIPAL_ID,
    AdminApiKeyPolicyPatch,
    OperatorPrincipalCreate,
    OperatorRole,
    PrincipalKind,
)
from fs2_serve.activation_contract import ScaleContract
from fs2_serve.activation_health import activation_set
from fs2_serve.activation_postgres import PostgresActivationStore
from fs2_serve.admin_models import AdminOperationQuery
from fs2_serve.admission import AdmissionService
from fs2_serve.api import AppRuntime, create_app
from fs2_serve.auth import AuthenticationError, OperatorSessionService, PepperRing, TokenService
from fs2_serve.configuration import (
    ConfigurationService,
    StoreConfigurationAuditSink,
    StoreConfigurationRepository,
    configuration_etag,
)
from fs2_serve.configuration_models import ConfigurationProposal, ReconciliationPhase, TerraformApplyReceipt
from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.model_deployment_admin import (
    ModelDeploymentReadService,
    StoreModelDeploymentRepository,
)
from fs2_serve.model_deployment_records import (
    ModelDeploymentRevisionAction,
    ModelDeploymentStatusAvailability,
)
from fs2_serve.models import (
    ActivationIntentStatus,
    ActivationLeaderIdentity,
    ActivationTargetState,
    AdmissionRequest,
    ModalityUsage,
    OperationStatus,
    Principal,
    ReportedUsage,
    RuntimeIdentity,
    Scope,
    TokenCreate,
    UsageDirection,
)
from fs2_serve.postgres import PostgresMaintenanceStore, PostgresStore, _decode_audit_detail
from fs2_serve.postgresql_release import EXPECTED_MIGRATIONS
from fs2_serve.runtime import ActivationError, StubRuntimeClient
from fs2_serve.scientific_batch.controller import ScientificBatchController
from fs2_serve.scientific_batch.models import (
    CheckpointMode,
    PreemptionMode,
    ResourceClass,
    SchedulingSnapshot,
    ScientificBatchPlan,
    ScientificStagePlan,
    ServiceClass,
    StageSchedulingDecision,
)
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository
from fs2_serve.scientific_batch.protocols import BatchFenceLostError
from fs2_serve.settings import Settings
from fs2_serve.store import (
    ConcurrencyExceededError,
    ConflictError,
    NotFoundError,
    RateLimitExceededError,
    StaleLeaseError,
)
from fs2_serve.telemetry import Metrics

ADMIN_TOKEN = "a" * 32
INITIAL_RESOURCE_VERSION = "1"
INITIAL_GENERATION = 1


def leader_identity(controller: str, resource_version: int) -> ActivationLeaderIdentity:
    pod_uid = f"pod-{controller}"
    return ActivationLeaderIdentity(
        pod_namespace="fs2-system",
        pod_name=controller,
        pod_uid=pod_uid,
        service_account_name="fs2-model-activation-controller",
        service_account_uid="cccccccc-cccc-cccc-cccc-cccccccccccc",
        lease_namespace="fs2-system",
        lease_name="fs2-serve-activation-controller",
        lease_uid="abababab-abab-abab-abab-abababababab",
        lease_resource_version=str(resource_version),
        lease_holder_identity=f"fs2:{pod_uid}",
        lease_duration_seconds=5,
        lease_renew_time=datetime.now(UTC),
        lease_observed_remaining_seconds=5.0,
    )


async def publish_leader(
    store: PostgresActivationStore,
    controller: str,
    resource_version: int,
    activation_set_digest: str,
    *,
    lease_seconds: float = 15,
) -> tuple[ActivationLeaderIdentity, int]:
    identity = leader_identity(controller, resource_version)
    fence = await store.publish_activation_controller_heartbeat(
        identity,
        activation_set_digest=activation_set_digest,
        lease_expires_at=await store.database_clock() + timedelta(seconds=lease_seconds),
        expected_fencing_token=await store.current_activation_controller_fence(),
    )
    return identity, fence


def test_audit_detail_read_normalization_handles_default_text_and_decoded_objects() -> None:
    detail = {"nested": ["value", 1, True, None]}
    assert _decode_audit_detail('{"nested":["value",1,true,null]}') == detail
    assert _decode_audit_detail(detail) is detail


@pytest.mark.parametrize(
    ("value", "expected_error"),
    [
        ('{"MALFORMED_AUDIT_VALUE":', "stored audit detail is invalid"),
        ('{"value":NaN}', "stored audit detail is invalid"),
        ('{"value":"' + "x" * (64 * 1024) + '"}', "stored audit detail is invalid"),
        (b'{"MALFORMED_AUDIT_VALUE":"bytes"}', "stored audit detail is not an object"),
        (["MALFORMED_AUDIT_VALUE"], "stored audit detail is not an object"),
        (None, "stored audit detail is not an object"),
    ],
)
def test_audit_detail_read_normalization_fails_closed_without_reflection(value: object, expected_error: str) -> None:
    with pytest.raises(RuntimeError) as raised:
        _decode_audit_detail(value)
    assert str(raised.value) == expected_error
    assert raised.value.__cause__ is None
    assert "MALFORMED_AUDIT_VALUE" not in str(raised.value)


@pytest_asyncio.fixture
async def postgres_store(cipher: PayloadCipher, hasher: KeyedHasher) -> PostgresStore:
    database_url = os.environ.get("FS2_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("FS2_TEST_DATABASE_URL is not set")
    store = await PostgresStore.connect(
        database_url,
        CONTROL_ROOT / "migrations",
        cipher,
        hasher,
        payload_ttl_seconds=3600,
        min_size=2,
        max_size=12,
    )
    await asyncio.gather(store.migrate(), store.migrate())
    async with store.pool.acquire() as connection:
        await connection.execute(
            "TRUNCATE fs2_model_deployment_status_events,fs2_model_deployment_idempotency,"
            "fs2_model_deployments,fs2_model_deployment_revisions,"
            "fs2_configuration_reconciliation_events,fs2_configuration_plans,"
            "fs2_configuration_revisions,fs2_activation_controller_status,fs2_activation_target_state,"
            "fs2_activation_model_fences,fs2_activation_intents,"
            "fs2_operator_sessions,fs2_usage_facts,fs2_audit_events,"
            "fs2_operation_events,fs2_operations,fs2_tokens "
            "RESTART IDENTITY CASCADE"
        )
        await connection.execute("DELETE FROM fs2_operator_principals WHERE id<>$1", BOOTSTRAP_OPERATOR_PRINCIPAL_ID)
    try:
        yield store
    finally:
        async with store.pool.acquire() as connection:
            await connection.execute(
                "TRUNCATE fs2_model_deployment_status_events,fs2_model_deployment_idempotency,"
                "fs2_model_deployments,fs2_model_deployment_revisions,"
                "fs2_configuration_reconciliation_events,fs2_configuration_plans,"
                "fs2_configuration_revisions,fs2_activation_controller_status,fs2_activation_target_state,"
                "fs2_activation_model_fences,fs2_activation_intents,"
                "fs2_operator_sessions,fs2_usage_facts,fs2_audit_events,"
                "fs2_operation_events,fs2_operations,fs2_tokens "
                "RESTART IDENTITY CASCADE"
            )
            await connection.execute(
                "DELETE FROM fs2_operator_principals WHERE id<>$1", BOOTSTRAP_OPERATOR_PRINCIPAL_ID
            )
        await store.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_configuration_receipt_is_atomic_durable_and_exactly_replayable(
    postgres_store: PostgresStore,
) -> None:
    from test_admin_configuration import qualified_configuration, with_cooldown

    initial, catalog = qualified_configuration()
    desired = with_cooldown(initial, 301)
    repository = StoreConfigurationRepository(postgres_store)
    current = await repository.ensure_initial(initial, actor="terraform-bootstrap")
    service = ConfigurationService(
        repository=repository,
        catalog=catalog,
        audit=StoreConfigurationAuditSink(postgres_store),
    )
    actor = await postgres_store.get_operator_principal(BOOTSTRAP_OPERATOR_PRINCIPAL_ID)
    plan = await service.plan(
        ConfigurationProposal(base_etag=current.etag, desired=desired),
        actor,
    )
    awaiting = await service.reconcile(
        plan_id=plan.plan_id,
        base_etag=current.etag,
        actor=actor,
    )
    receipt = TerraformApplyReceipt(
        plan_id=plan.plan_id,
        reconciliation_id=awaiting.reconciliation_id,
        base_revision=current.revision,
        base_etag=current.etag,
        proposed_etag=plan.proposed_etag,
        configuration_sha256=configuration_etag(desired),
    )

    first, replay = await asyncio.gather(
        repository.accept_terraform_applied(desired, receipt, actor="terraform-applied"),
        repository.accept_terraform_applied(desired, receipt, actor="terraform-applied"),
    )
    status = await service.status(awaiting.reconciliation_id)

    assert first == replay
    assert first.revision == 2 and first.previous_revision == 1
    assert first.desired == first.effective == desired
    assert status.phase is ReconciliationPhase.SUCCEEDED
    assert status.applied_revision == first.revision
    async with postgres_store.pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM fs2_configuration_revisions") == 2
        assert await connection.fetchval("SELECT count(*) FROM fs2_configuration_reconciliation_events") == 2
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM fs2_audit_events WHERE action='configuration.terraform-applied'"
            )
            == 1
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_configuration_terraform_baseline_adopts_each_changed_tfvars_revision(
    postgres_store: PostgresStore,
) -> None:
    from test_admin_configuration import qualified_configuration, with_cooldown

    initial, _ = qualified_configuration()
    desired = with_cooldown(initial, 301)
    repository = StoreConfigurationRepository(postgres_store)

    first = await repository.adopt_terraform_baseline(initial)
    adopted, replay = await asyncio.gather(
        repository.adopt_terraform_baseline(desired),
        repository.adopt_terraform_baseline(desired),
    )

    assert first.revision == 1
    assert adopted == replay
    assert adopted.revision == 2
    assert adopted.previous_revision == 1
    assert adopted.reconciliation_id is None
    assert adopted.created_by == "terraform-baseline"
    assert adopted.desired == adopted.effective == desired
    async with postgres_store.pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM fs2_configuration_revisions") == 2


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_model_deployment_revisions_idempotency_status_and_audit_are_durable(
    postgres_store: PostgresStore,
) -> None:
    from test_model_deployment_admin import append_request, observation

    create = append_request(key="postgres-create-qwen-0001")
    first_result, replay_result = await asyncio.gather(
        postgres_store.model_deployment_append_revision(create),
        postgres_store.model_deployment_append_revision(create),
    )
    assert first_result.value == replay_result.value
    assert {first_result.reused, replay_result.reused} == {False, True}
    first = first_result.value

    with pytest.raises(ConflictError, match="bound to another request"):
        await postgres_store.model_deployment_append_revision(
            append_request(key=create.idempotency_key, max_replicas=3)
        )

    first_observation = observation(revision=1, etag=first.etag)
    assert await postgres_store.model_deployment_append_status(first_observation) == first_observation
    assert await postgres_store.model_deployment_append_status(first_observation) == first_observation

    second = (
        await postgres_store.model_deployment_append_revision(
            append_request(
                key="postgres-update-qwen-0002",
                action=ModelDeploymentRevisionAction.UPDATE,
                expected_etag=first.etag,
                max_replicas=3,
            )
        )
    ).value
    await postgres_store.model_deployment_append_revision(
        append_request(
            key="postgres-create-cosmos-0003",
            tenant_id="tenant-b",
            name="cosmos-live",
        )
    )

    service = ModelDeploymentReadService(StoreModelDeploymentRepository(postgres_store))
    tenant_models = await service.list(
        namespace="fs2-models",
        tenant_id="tenant-a",
        after_name=None,
        limit=100,
    )
    assert [item.name for item in tenant_models.items] == ["qwen-live"]
    history = await service.history(
        namespace="fs2-models",
        name="qwen-live",
        tenant_id="tenant-a",
        before_revision=None,
        limit=100,
    )
    assert [item.revision for item in history.items] == [2, 1]
    stale = await service.status(
        namespace="fs2-models",
        name="qwen-live",
        tenant_id="tenant-a",
    )
    assert stale.state is ModelDeploymentStatusAvailability.STALE

    second_observation = observation(revision=2, etag=second.etag)
    await postgres_store.model_deployment_append_status(second_observation)
    with pytest.raises(ConflictError, match="older than current status"):
        await postgres_store.model_deployment_append_status(
            first_observation.model_copy(update={"observation_id": uuid4()})
        )
    observed = await service.status(
        namespace="fs2-models",
        name="qwen-live",
        tenant_id="tenant-a",
    )
    assert observed.state is ModelDeploymentStatusAvailability.OBSERVED
    assert observed.observation == second_observation

    async with postgres_store.pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM fs2_model_deployment_revisions") == 3
        assert await connection.fetchval("SELECT count(*) FROM fs2_model_deployment_idempotency") == 3
        assert await connection.fetchval("SELECT count(*) FROM fs2_model_deployment_status_events") == 2
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM fs2_audit_events WHERE action LIKE 'model_deployment.revision.%'"
            )
            == 3
        )
        columns = await connection.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='fs2_model_deployment_idempotency'
            """
        )
        assert "idempotency_key" not in {str(row["column_name"]) for row in columns}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_access_migration_sessions_rotation_rate_and_reported_units_are_durable(
    postgres_store: PostgresStore,
) -> None:
    pepper = PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32})
    sessions = OperatorSessionService(postgres_store, pepper, ttl_seconds=600)
    issued_session = await sessions.issue_bootstrap()
    verified_session = await sessions.verify(issued_session.cookie_value)
    assert verified_session.principal.id == BOOTSTRAP_OPERATOR_PRINCIPAL_ID
    replacement_session = await sessions.replace(issued_session.cookie_value)
    with pytest.raises(AuthenticationError):
        await sessions.verify(issued_session.cookie_value)
    with pytest.raises(NotFoundError):
        await sessions.replace(replacement_session.cookie_value, principal_id=uuid4())
    assert (await sessions.verify(replacement_session.cookie_value)).principal.id == BOOTSTRAP_OPERATOR_PRINCIPAL_ID

    tenant_operator = await postgres_store.create_operator_principal(
        principal_id=uuid4(),
        request=OperatorPrincipalCreate(
            subject="postgres-operator@example.test",
            display_name="PostgreSQL operator",
            kind=PrincipalKind.HUMAN,
            role=OperatorRole.OPERATOR,
            tenant_id="tenant-access",
        ),
        actor="bootstrap-admin",
    )
    assert [
        item.id
        for item in await postgres_store.list_operator_principals(
            tenant_id="tenant-access", include_global=False, limit=20
        )
    ] == [tenant_operator.id]

    tokens = TokenService(postgres_store, pepper)
    issued = await tokens.issue(
        TokenCreate(
            principal_id="postgres-agent",
            tenant_id="tenant-access",
            scopes={Scope.CATALOG_READ, Scope.INFERENCE_INVOKE, Scope.MCP_INVOKE},
            models={"qwen3-8b"},
            request_budget=10,
            gpu_seconds_budget=100,
            max_concurrency=4,
            name="postgres agent key",
            rate_limit_requests=2,
            rate_window_seconds=60,
        ),
        created_by="postgres-operator",
    )
    principal = await tokens.verify(issued.token)
    verified_only = await postgres_store.token_for_verification(issued.id)
    assert verified_only is not None and verified_only[0].last_used_at is None
    first = await postgres_store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="qwen3-8b",
            operation="chat",
            protocol="openai-chat",
            idempotency_key="postgres-access-usage-0001",
            request_body=b"{}",
        ),
        model_revision="revision-a",
        reserved_gpu_seconds=2,
        max_attempts=2,
    )
    admitted = await postgres_store.token_for_verification(issued.id)
    assert admitted is not None and admitted[0].last_used_at is not None
    claimed = await postgres_store.claim_operation("postgres-access-worker", lease_seconds=30)
    assert claimed is not None and claimed.id == first.id
    await postgres_store.mark_running(
        claimed.id,
        RuntimeIdentity(gpu_count=1),
        worker_id=claimed.worker_id,
        fencing_token=claimed.fencing_token,
    )
    completed = await postgres_store.complete_operation(
        claimed.id,
        status=OperationStatus.SUCCEEDED,
        outcome="succeeded",
        semantic_outcome="protocol_valid",
        http_status=200,
        response_body=b"{}",
        response_content_type="application/json",
        error_code=None,
        error_detail=None,
        runtime=RuntimeIdentity(gpu_count=1),
        worker_id=claimed.worker_id,
        fencing_token=claimed.fencing_token,
        usage=ReportedUsage(
            input_tokens=0,
            output_tokens=9,
            modalities=[
                ModalityUsage(
                    modality="image",
                    direction=UsageDirection.INPUT,
                    unit="image",
                    amount=1,
                )
            ],
        ),
    )
    assert completed.input_tokens == 0 and completed.output_tokens == 9

    await postgres_store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="qwen3-8b",
            operation="chat",
            protocol="openai-chat",
            idempotency_key="postgres-access-rate-0002",
            request_body=b"{}",
        ),
        model_revision="revision-a",
        reserved_gpu_seconds=1,
        max_attempts=2,
    )
    with pytest.raises(RateLimitExceededError):
        await postgres_store.append_operation(
            principal=principal,
            admission=AdmissionRequest(
                model_id="qwen3-8b",
                operation="chat",
                protocol="openai-chat",
                idempotency_key="postgres-access-rate-0003",
                request_body=b"{}",
            ),
            model_revision="revision-a",
            reserved_gpu_seconds=1,
            max_attempts=2,
        )

    access = AdminAccessService(postgres_store, tokens)
    rejected_policy_value = "POSTGRES_REJECTED_POLICY_MUST_NOT_LEAK_9481"
    with pytest.raises(ConflictError):
        await access.update_key_policy(
            tenant_operator,
            issued.id,
            AdminApiKeyPolicyPatch(name=rejected_policy_value, request_budget=1),
        )
    updated_policy = await access.update_key_policy(
        tenant_operator,
        issued.id,
        AdminApiKeyPolicyPatch(
            scopes={Scope.CATALOG_READ},
            models={"qwen3-8b"},
            request_budget=5,
            max_concurrency=3,
            rate_limit_requests=None,
            rate_window_seconds=None,
        ),
    )
    assert updated_policy.scopes == ["catalog.read"]
    assert updated_policy.request_budget == 5
    assert updated_policy.rate_limit_requests is None
    policy_audit = await postgres_store.list_audit(tenant_id="tenant-access", limit=100)
    assert rejected_policy_value not in str(policy_audit)
    assert any(event.action == "token.policy.update" and event.outcome == "failed" for event in policy_audit)
    assert any(event.action == "token.policy.update" and event.outcome == "succeeded" for event in policy_audit)

    with pytest.raises(ConflictError):
        await postgres_store.rotate_token(
            issued.id,
            token_id=uuid4(),
            prefix="fs2_pat_duplicate",
            pepper_key_id="pepper-v1",
            digest="not-raw-key-material",
            fingerprint=issued.fingerprint or "0" * 64,
            name="must roll back",
            expires_at=None,
            actor="postgres-operator",
        )
    unchanged = await postgres_store.token_for_verification(issued.id)
    assert unchanged is not None and unchanged[0].revoked_at is None

    rotated = await tokens.rotate(issued.id, actor="postgres-operator", name="rotated postgres key")
    predecessor = await postgres_store.token_for_verification(issued.id)
    assert predecessor is not None and predecessor[0].revoked_at is not None
    assert rotated.rotation_parent_id == issued.id and rotated.requests_used == 2
    assert (await tokens.verify(rotated.token)).token_id == rotated.id

    async with postgres_store.pool.acquire() as connection:
        usage = await connection.fetchrow(
            "SELECT input_tokens,output_tokens,modality_usage FROM fs2_usage_facts WHERE operation_id=$1",
            completed.id,
        )
        assert usage is not None and usage["input_tokens"] == 0 and usage["output_tokens"] == 9
        assert issued.token not in str(
            await connection.fetchval("SELECT digest FROM fs2_tokens WHERE id=$1", issued.id)
        )
        assert issued_session.cookie_value not in str(
            await connection.fetchval("SELECT digest FROM fs2_operator_sessions WHERE id=$1", issued_session.session.id)
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_migration_and_schema_wait_entrypoints_need_only_database_credentials(
    postgres_store: PostgresStore,
) -> None:
    database_url = os.environ["FS2_TEST_DATABASE_URL"]
    migrations_dir = CONTROL_ROOT / "migrations"
    await asyncio.gather(
        PostgresStore.migrate_database(database_url, migrations_dir),
        PostgresStore.migrate_database(database_url, migrations_dir),
    )
    await PostgresStore.wait_for_schema(database_url, migrations_dir, timeout_seconds=1)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("adversary", ["extra", "reordered"])
async def test_real_postgres_rejects_extra_or_reordered_applied_migration_ledger(
    postgres_store: PostgresStore,
    adversary: str,
) -> None:
    del postgres_store
    database_url = os.environ["FS2_TEST_DATABASE_URL"]
    admin_url, _ = database_url.rsplit("/", 1)
    database_name = f"fs2_migration_ledger_{adversary}_{uuid4().hex[:8]}"
    admin = await asyncpg.connect(database_url)
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        candidate_url = f"{admin_url}/{database_name}"
        connection = await asyncpg.connect(candidate_url)
        try:
            await connection.execute(
                """
                CREATE TABLE fs2_schema_migrations (
                    version text PRIMARY KEY,
                    sha256 char(64) NOT NULL,
                    applied_at timestamptz NOT NULL
                )
                """
            )
            rows = (
                [EXPECTED_MIGRATIONS[0], ("0009_unreviewed.sql", "0" * 64)]
                if adversary == "extra"
                else [EXPECTED_MIGRATIONS[1], EXPECTED_MIGRATIONS[0]]
            )
            for offset, (version, digest) in enumerate(rows):
                await connection.execute(
                    "INSERT INTO fs2_schema_migrations(version,sha256,applied_at) "
                    "VALUES($1,$2,'2026-08-27T00:00:00Z'::timestamptz + $3 * interval '1 second')",
                    version,
                    digest,
                    offset,
                )
        finally:
            await connection.close()
        with pytest.raises(RuntimeError, match="missing, extra, or reordered"):
            await PostgresStore.wait_for_schema(candidate_url, CONTROL_ROOT / "migrations", timeout_seconds=1)
        with pytest.raises(RuntimeError, match="missing, extra, or reordered"):
            await PostgresStore.migrate_database(candidate_url, CONTROL_ROOT / "migrations")
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_fixture_name",
    [
        "scientific-batch-state-v6-ec3440a2.json",
        "scientific-batch-state-v7-545d71d9.json",
    ],
)
async def test_real_postgres_upgrade_preserves_prior_ledger_and_applies_only_0017(
    postgres_store: PostgresStore,
    tmp_path: Path,
    legacy_fixture_name: str,
) -> None:
    del postgres_store
    database_url = os.environ["FS2_TEST_DATABASE_URL"]
    admin_url, _ = database_url.rsplit("/", 1)
    database_name = f"fs2_scientific_batch_upgrade_{uuid4().hex[:10]}"
    prior_dir = tmp_path / "prior-migrations"
    prior_dir.mkdir()
    legacy_fixture = json.loads((CONTROL_ROOT / "tests/fixtures" / legacy_fixture_name).read_text())
    legacy_operation_id = UUID(legacy_fixture["operation_id"])
    legacy_input_artifact_id = UUID(legacy_fixture["input_artifact_id"])
    legacy_input_attempt_id = UUID("44444444-4444-4444-8444-444444444444")
    legacy_token_id = UUID("33333333-3333-4333-8333-333333333333")
    for version, _ in EXPECTED_MIGRATIONS[:-1]:
        migration = CONTROL_ROOT / "migrations" / version
        shutil.copy2(migration, prior_dir / migration.name)
    admin = await asyncpg.connect(database_url)
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        upgrade_url = f"{admin_url}/{database_name}"
        before_connection = await asyncpg.connect(upgrade_url)
        try:
            await before_connection.execute(
                """
                CREATE TABLE fs2_schema_migrations (
                    version text PRIMARY KEY,
                    sha256 char(64) NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
                )
                """
            )
            for migration in sorted(prior_dir.glob("*.sql")):
                payload = migration.read_bytes()
                await before_connection.execute(payload.decode("utf-8"))
                await before_connection.execute(
                    "INSERT INTO fs2_schema_migrations(version,sha256) VALUES($1,$2)",
                    migration.name,
                    hashlib.sha256(payload).hexdigest(),
                )
            before = {
                str(row["version"]): str(row["sha256"])
                for row in await before_connection.fetch(
                    "SELECT version,sha256 FROM fs2_schema_migrations ORDER BY version"
                )
            }
            assert list(before) == [path.name for path in sorted(prior_dir.glob("*.sql"))]
            assert (
                await before_connection.fetchval("SELECT to_regclass('public.fs2_activation_model_fences')")
                == "fs2_activation_model_fences"
            )
            assert await before_connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='fs2_operations' "
                "AND column_name='payload_purged_at')"
            )
            assert (
                await before_connection.fetchval("SELECT to_regclass('public.fs2_operator_principals')")
                == "fs2_operator_principals"
            )
            assert (
                await before_connection.fetchval("SELECT to_regclass('public.fs2_model_deployments')")
                == "fs2_model_deployments"
            )
            assert await before_connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='fs2_operations' "
                "AND column_name='dispatch_snapshot')"
            )
            assert (
                await before_connection.fetchval("SELECT to_regclass('public.fs2_scientific_artifacts')")
                == "fs2_scientific_artifacts"
            )
            assert (
                await before_connection.fetchval("SELECT to_regclass('public.fs2_scientific_batches')")
                == "fs2_scientific_batches"
            )
            await before_connection.execute(
                """
                INSERT INTO fs2_tokens(
                    id,prefix,pepper_key_id,digest,principal_id,tenant_id,scopes,models,
                    max_concurrency,created_by
                ) VALUES($1,'fs2_pat_legacyv6','pepper-v1','test-digest','legacy-principal',
                    'tenant-a',ARRAY['inference.invoke'],ARRAY['bindcraft'],1,'migration-test')
                """,
                legacy_token_id,
            )
            await before_connection.execute(
                """
                INSERT INTO fs2_operations(
                    id,tenant_id,principal_id,token_id,model_id,model_revision,protocol,operation,
                    idempotency_key,request_hmac_key_id,request_hmac,request_content_type,
                    payload_expires_at,max_attempts
                ) VALUES($1,'tenant-a','legacy-principal',$2,'bindcraft','7cd4ace1',
                    'scientific-batch-v1','design','legacy-v6-fixture','hmac-v1',$3,
                    'application/json',clock_timestamp()+interval '1 day',2)
                """,
                legacy_operation_id,
                legacy_token_id,
                "8" * 64,
            )
            await before_connection.execute(
                """
                INSERT INTO fs2_scientific_stage_attempts(
                    attempt_id,operation_id,tenant_id,stage_id,shard_id,attempt_number,status,
                    started_at,retention_expires_at
                ) VALUES($1,$2,'tenant-a','input','-',1,'running',clock_timestamp(),
                    clock_timestamp()+interval '1 day')
                """,
                legacy_input_attempt_id,
                legacy_operation_id,
            )
            input_digest = "sha256:" + "6" * 64
            await before_connection.execute(
                """
                INSERT INTO fs2_scientific_artifacts(
                    id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,digest,
                    size_bytes,media_type,storage_key,access_profile,retention_expires_at
                ) VALUES($1,$2,$3,'tenant-a','input','-','input',$4,128,'application/json',$5,
                    'public',clock_timestamp()+interval '1 day')
                """,
                legacy_input_artifact_id,
                legacy_input_attempt_id,
                legacy_operation_id,
                input_digest,
                f"scientific/v1/tenants/tenant-a/operations/{legacy_operation_id}/stages/input/shards/-/"
                f"attempts/{legacy_input_attempt_id}/input/sha256/{input_digest.removeprefix('sha256:')}",
            )
            await before_connection.execute(
                """
                INSERT INTO fs2_scientific_batches(
                    operation_id,batch_id,workload_id,tenant_id,model_id,variant_id,
                    input_artifact_id,scheduling_digest,status,revision,cancel_requested,state
                ) VALUES($1,$2,$3,'tenant-a','bindcraft','upstream-pyrosetta',$4,$5,
                    'queued',0,false,$6::jsonb)
                """,
                legacy_operation_id,
                UUID(legacy_fixture["batch_id"]),
                UUID(legacy_fixture["workload_id"]),
                legacy_input_artifact_id,
                "sha256:" + "0" * 64,
                json.dumps(legacy_fixture, sort_keys=True, separators=(",", ":")),
            )
        finally:
            await before_connection.close()

        await PostgresStore.migrate_database(upgrade_url, CONTROL_ROOT / "migrations")
        upgraded_connection = await asyncpg.connect(upgrade_url)
        try:
            after = {
                str(row["version"]): str(row["sha256"])
                for row in await upgraded_connection.fetch(
                    "SELECT version,sha256 FROM fs2_schema_migrations ORDER BY version"
                )
            }
            assert {version: after[version] for version in before} == before
            assert list(after)[-1] == "0017_scientific_batch_state_v8.sql"
            assert (
                await upgraded_connection.fetchval("SELECT to_regclass('public.fs2_activation_model_fences')")
                == "fs2_activation_model_fences"
            )
            assert await upgraded_connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='fs2_operations' "
                "AND column_name='payload_purged_at')"
            )
            assert (
                await upgraded_connection.fetchval("SELECT to_regclass('public.fs2_operator_principals')")
                == "fs2_operator_principals"
            )
            assert (
                await upgraded_connection.fetchval("SELECT to_regclass('public.fs2_model_deployments')")
                == "fs2_model_deployments"
            )
            assert await upgraded_connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='fs2_operations' "
                "AND column_name='dispatch_snapshot')"
            )
            assert (
                await upgraded_connection.fetchval("SELECT to_regclass('public.fs2_scientific_artifacts')")
                == "fs2_scientific_artifacts"
            )
            assert (
                await upgraded_connection.fetchval("SELECT to_regclass('public.fs2_scientific_batches')")
                == "fs2_scientific_batches"
            )
        finally:
            await upgraded_connection.close()

        pool = await asyncpg.create_pool(upgrade_url, min_size=1, max_size=2)
        try:
            repository = PostgresScientificBatchRepository(pool)
            claim = await repository.claim_next(
                controller_id="legacy-state-upgrader",
                lease_seconds=30,
                now=datetime.now(UTC),
            )
            assert claim is not None and claim.operation_id == legacy_operation_id
            legacy_state = await repository.load(claim)
            assert legacy_state.scheduling.stages[0].resource_class is ResourceClass.GPU
            assert legacy_state.execution_plan is not None
            assert legacy_state.execution_plan.invocations[0].runtime_mounts[0].expected_manifest_sha256 is None
            upgraded_state = await repository.replace(
                claim,
                expected_revision=0,
                record=replace(legacy_state, revision=1),
                events=(),
                now=datetime.now(UTC),
            )
            assert upgraded_state.revision == 1
            await repository.release(claim)
            cancelled = await repository.request_cancel(
                legacy_operation_id,
                tenant_id="tenant-a",
                actor="migration-test",
            )
            assert cancelled.cancel_requested is True
            async with pool.acquire() as connection:
                stored = await connection.fetchrow(
                    "SELECT scheduling_digest,state FROM fs2_scientific_batches WHERE operation_id=$1",
                    legacy_operation_id,
                )
                assert stored is not None
                stored_state = json.loads(stored["state"])
                assert stored_state["schema_version"] == "fs2-serve.nebius.ai/scientific-batch-state/v8"
                assert stored["scheduling_digest"] == cancelled.scheduling.digest
                assert stored_state["plan"]["stages"][0]["placement_class"] is None
                assert stored_state["plan"]["stages"][0]["resources"] is None
                assert stored_state["scheduling"]["raw_contract_sha256"] is None
                assert stored_state["scheduling"]["stages"][0]["resource_class"] == "gpu"
                assert "admitted_resource_flavor" not in stored_state["scheduling"]["stages"][0]
                assert (
                    stored_state["adapter_execution"]["invocations"][0]["runtime_mounts"][0]["expected_manifest_sha256"]
                    is None
                )
                assert stored_state["adapter_execution"]["execution_map_sha256"] is None
                assert stored_state["adapter_execution"]["stage_bindings"] == []
                assert stored_state["runtime_artifacts"][0]["aggregate_tree"] is None
                with pytest.raises(asyncpg.PostgresError, match="scientific batch admission is immutable"):
                    await connection.execute(
                        """
                        UPDATE fs2_scientific_batches
                        SET state=jsonb_set(state,'{scheduling,stages,0,resolved_local_queue}',
                            '"tampered-queue"'::jsonb)
                        WHERE operation_id=$1
                        """,
                        legacy_operation_id,
                    )
                with pytest.raises(asyncpg.PostgresError, match="scientific batch admission is immutable"):
                    await connection.execute(
                        """
                        UPDATE fs2_scientific_batches
                        SET state=jsonb_set(state,
                            '{adapter_execution,invocations,0,runtime_mounts,0,expected_manifest_sha256}',
                            '"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"'::jsonb)
                        WHERE operation_id=$1
                        """,
                        legacy_operation_id,
                    )
        finally:
            await pool.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_migration_rejects_a_login_role_as_the_activation_group(
    postgres_store: PostgresStore,
) -> None:
    database_url = os.environ["FS2_TEST_DATABASE_URL"]
    activation_role = f"fs2_test_activation_login_group_{uuid4().hex[:10]}"
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(f'CREATE ROLE "{activation_role}" LOGIN')
    try:
        with pytest.raises(RuntimeError, match="activation database group role must be NOLOGIN"):
            await PostgresStore.migrate_database(
                database_url,
                CONTROL_ROOT / "migrations",
                "fs2_serve_reporting",
                "fs2_serve_runtime",
                "fs2_serve_maintenance",
                activation_role,
            )
    finally:
        async with postgres_store.pool.acquire() as connection:
            await connection.execute(f'DROP ROLE "{activation_role}"')


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_controller_heartbeat_is_singleton_fenced_expires_and_gates_endpoint(
    postgres_store: PostgresStore,
    registry,
) -> None:
    expected_digest = activation_set(registry.list(enabled_only=True)).digest
    first_identity, first = await publish_leader(postgres_store.activation, "controller-a", 101, expected_digest)
    assert first == 1
    with pytest.raises(StaleLeaseError, match="prior activation leader"):
        await postgres_store.activation.publish_activation_controller_heartbeat(
            leader_identity("controller-b", 102),
            activation_set_digest=expected_digest,
            lease_expires_at=await postgres_store.activation.database_clock() + timedelta(seconds=15),
            expected_fencing_token=first,
        )
    renewed_identity = first_identity.model_copy(update={"lease_resource_version": "103"})
    assert (
        await postgres_store.activation.publish_activation_controller_heartbeat(
            renewed_identity,
            activation_set_digest=expected_digest,
            lease_expires_at=await postgres_store.activation.database_clock() + timedelta(seconds=15),
            expected_fencing_token=first,
        )
        == first
    )
    with pytest.raises(StaleLeaseError, match="leadership fence"):
        await postgres_store.activation.publish_activation_controller_heartbeat(
            renewed_identity.model_copy(update={"lease_resource_version": "104"}),
            activation_set_digest=expected_digest,
            lease_expires_at=await postgres_store.activation.database_clock() + timedelta(seconds=15),
            expected_fencing_token=first + 1,
        )
    with pytest.raises(StaleLeaseError, match="resourceVersion"):
        await postgres_store.activation.publish_activation_controller_heartbeat(
            first_identity,
            activation_set_digest=expected_digest,
            lease_expires_at=await postgres_store.activation.database_clock() + timedelta(seconds=15),
            expected_fencing_token=first,
        )
    assert await postgres_store.activation_controller_ready(expected_digest)
    async with postgres_store.pool.acquire() as connection:
        status = await connection.fetchrow(
            "SELECT count(*) OVER () AS rows,fencing_token,activation_set_digest FROM fs2_activation_controller_status"
        )
    assert status is not None
    assert status["rows"] == 1 and status["fencing_token"] == 1
    assert status["activation_set_digest"] == expected_digest

    current_app, _ = postgres_app(postgres_store, registry)
    async with current_app.router.lifespan_context(current_app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=current_app),
            base_url="https://inference.test.invalid",
            trust_env=False,
        ) as client:
            assert (await client.get("/readyz")).status_code == 200

    current = registry.get("qwen3-8b")
    next_registry = type(registry)(
        registry.catalog,
        {
            current.id: replace(
                current,
                gateway=replace(
                    current.gateway,
                    binding=replace(current.binding, binding_digest="f" * 64),
                ),
            )
        },
    )
    next_digest = activation_set(next_registry.list(enabled_only=True)).digest
    assert next_digest != expected_digest
    next_app, _ = postgres_app(postgres_store, next_registry)
    async with next_app.router.lifespan_context(next_app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=next_app),
            base_url="https://inference.test.invalid",
            trust_env=False,
        ) as client:
            stale_generation = await client.get("/readyz")
            assert stale_generation.status_code == 503
            assert stale_generation.json()["error"]["type"] == "activation_controller_unavailable"
            await postgres_store.activation.publish_activation_controller_heartbeat(
                renewed_identity.model_copy(update={"lease_resource_version": "104"}),
                activation_set_digest=next_digest,
                lease_expires_at=await postgres_store.activation.database_clock() + timedelta(seconds=15),
                expected_fencing_token=first,
            )
            assert (await client.get("/readyz")).status_code == 200
            async with postgres_store.pool.acquire() as connection:
                await connection.execute(
                    "UPDATE fs2_activation_controller_status SET lease_expires_at=clock_timestamp()"
                )
            expired = await client.get("/readyz")
            assert expired.status_code == 503
            assert expired.json()["error"]["type"] == "activation_controller_unavailable"
            recovered_identity, recovered_fence = await publish_leader(
                postgres_store.activation,
                "controller-recovered",
                105,
                next_digest,
            )
            assert recovered_fence == 2
            assert recovered_identity.pod_uid == "pod-controller-recovered"
            assert (await client.get("/readyz")).status_code == 200
    assert await postgres_store.activation_controller_ready(next_digest)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_activation_intent_is_durable_idempotent_and_claim_failover_is_fenced(
    postgres_store: PostgresStore,
    registry,
) -> None:
    principal = await add_token(postgres_store)
    accepted = await append(postgres_store, principal, "postgres-activation-intent-0001")
    operation = await postgres_store.claim_operation("gateway-worker", lease_seconds=30)
    assert operation is not None and operation.id == accepted.id
    model = registry.get(operation.model_id)
    contract = ScaleContract.from_model(model)

    first_intent, replayed_intent = await asyncio.gather(
        postgres_store.ensure_activation_intent(
            operation,
            binding_digest=model.binding.binding_digest,
            worker_id=operation.worker_id,
            fencing_token=operation.fencing_token,
        ),
        postgres_store.ensure_activation_intent(
            operation,
            binding_digest=model.binding.binding_digest,
            worker_id=operation.worker_id,
            fencing_token=operation.fencing_token,
        ),
    )
    assert first_intent.id == replayed_intent.id == operation.id
    async with postgres_store.pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM fs2_activation_intents WHERE operation_id=$1",
                operation.id,
            )
            == 1
        )
    expected_digest = activation_set(registry.list(enabled_only=True)).digest
    identity, leader_fence = await publish_leader(postgres_store.activation, "controller-a", 201, expected_digest)
    candidates = await asyncio.gather(
        postgres_store.activation.claim_activation_intent(
            identity, leadership_fencing_token=leader_fence, lease_seconds=30
        ),
        postgres_store.activation.claim_activation_intent(
            identity, leadership_fencing_token=leader_fence, lease_seconds=30
        ),
    )
    owners = [candidate for candidate in candidates if candidate is not None]
    assert len(owners) == 1
    original = owners[0]
    assert original.model_fencing_token == 1
    assert original.leadership_fencing_token == leader_fence
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_activation_intents SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            original.id,
        )
    replacement = await postgres_store.activation.claim_activation_intent(
        identity, leadership_fencing_token=leader_fence, lease_seconds=30
    )
    assert replacement is not None
    assert replacement.id == original.id and replacement.fencing_token > original.fencing_token
    assert replacement.model_fencing_token is not None
    assert replacement.model_fencing_token > (original.model_fencing_token or 0)
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_activation_intents SET deadline_at=clock_timestamp()+interval '2 seconds' WHERE id=$1",
            replacement.id,
        )
    wait_budget = await postgres_store.activation.activation_wait_budget(
        replacement.id,
        identity=identity,
        leadership_fencing_token=leader_fence,
        controller_id=replacement.controller_id,
        fencing_token=replacement.fencing_token,
        maximum_seconds=10,
    )
    assert 0 < wait_budget <= 2
    with pytest.raises(StaleLeaseError):
        await postgres_store.activation.activation_wait_budget(
            original.id,
            identity=identity,
            leadership_fencing_token=leader_fence,
            controller_id=original.controller_id,
            fencing_token=original.fencing_token,
            maximum_seconds=10,
        )
    async with postgres_store.pool.acquire() as connection:
        await connection.execute("UPDATE fs2_activation_intents SET deadline_at=NULL WHERE id=$1", replacement.id)

    target = ActivationTargetState(
        model_id=model.id,
        target_uid=contract.target.uid,
        resource_version=INITIAL_RESOURCE_VERSION,
        observed_generation=INITIAL_GENERATION,
        template_digest=contract.target.template_digest,
        active=True,
        observed_at=datetime.now(UTC),
        controller_fencing_token=leader_fence,
    )
    with pytest.raises(StaleLeaseError):
        await postgres_store.activation.complete_activation_intent(
            original.id,
            identity=identity,
            leadership_fencing_token=leader_fence,
            controller_id=original.controller_id,
            fencing_token=original.fencing_token,
            scale_contract_digest=contract.digest,
            target=target,
        )
    ready = await postgres_store.activation.complete_activation_intent(
        replacement.id,
        identity=identity,
        leadership_fencing_token=leader_fence,
        controller_id=replacement.controller_id,
        fencing_token=replacement.fencing_token,
        scale_contract_digest=contract.digest,
        target=target,
    )
    assert ready.status.value == "ready"
    assert ready.target is not None
    assert ready.target.model_fencing_token == replacement.model_fencing_token
    replayed = await postgres_store.activation.complete_activation_intent(
        replacement.id,
        identity=identity,
        leadership_fencing_token=leader_fence,
        controller_id=replacement.controller_id,
        fencing_token=replacement.fencing_token,
        scale_contract_digest=contract.digest,
        target=target,
    )
    assert replayed.status.value == "ready" and replayed.target == ready.target

    second = await append(postgres_store, principal, "postgres-activation-fence-0002")
    second_operation = await postgres_store.claim_operation("gateway-worker-2", lease_seconds=30)
    assert second_operation is not None and second_operation.id == second.id
    await postgres_store.ensure_activation_intent(
        second_operation,
        binding_digest=model.binding.binding_digest,
        worker_id=second_operation.worker_id,
        fencing_token=second_operation.fencing_token,
    )
    second_intent = await postgres_store.activation.claim_activation_intent(
        identity, leadership_fencing_token=leader_fence, lease_seconds=30
    )
    assert second_intent is not None and second_intent.id == second.id
    advanced = target.model_copy(
        update={
            "resource_version": "rv-fence-next",
            "observed_generation": target.observed_generation + 1,
            "controller_fencing_token": leader_fence - 1,
        }
    )
    with pytest.raises(StaleLeaseError, match="leadership fence"):
        await postgres_store.activation.complete_activation_intent(
            second_intent.id,
            identity=identity,
            leadership_fencing_token=leader_fence,
            controller_id=second_intent.controller_id,
            fencing_token=second_intent.fencing_token,
            scale_contract_digest=contract.digest,
            target=advanced,
        )
    advanced = advanced.model_copy(update={"controller_fencing_token": leader_fence})
    assert (
        await postgres_store.activation.complete_activation_intent(
            second_intent.id,
            identity=identity,
            leadership_fencing_token=leader_fence,
            controller_id=second_intent.controller_id,
            fencing_token=second_intent.fencing_token,
            scale_contract_digest=contract.digest,
            target=advanced,
        )
    ).status.value == "ready"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_crashed_activation_claims_requeue_or_terminalize_atomically_at_every_db_clock_boundary(
    postgres_store: PostgresStore,
    registry,
) -> None:
    principal = await add_token(postgres_store)
    model = registry.get("qwen3-8b")
    expected_digest = activation_set(registry.list(enabled_only=True)).digest
    identity, leader_fence = await publish_leader(
        postgres_store.activation,
        "crash-recovery-controller",
        601,
        expected_digest,
        lease_seconds=60,
    )

    async def create_intent(key: str, *, max_attempts: int = 2):
        accepted = await append(postgres_store, principal, key)
        if max_attempts != 2:
            async with postgres_store.pool.acquire() as connection:
                await connection.execute(
                    "UPDATE fs2_operations SET max_attempts=$2 WHERE id=$1",
                    accepted.id,
                    max_attempts,
                )
        operation = await postgres_store.claim_operation(f"worker-{key[-4:]}", lease_seconds=30)
        assert operation is not None and operation.id == accepted.id
        intent = await postgres_store.ensure_activation_intent(
            operation,
            binding_digest=model.binding.binding_digest,
            worker_id=operation.worker_id,
            fencing_token=operation.fencing_token,
        )
        assert intent.max_attempts == max_attempts
        return operation, intent

    async def event_names(intent_id: UUID) -> list[str]:
        async with postgres_store.pool.acquire() as connection:
            return [
                str(row["event"])
                for row in await connection.fetch(
                    "SELECT event FROM fs2_activation_events WHERE intent_id=$1 ORDER BY id",
                    intent_id,
                )
            ]

    # A crash before the final attempt is eligible for exactly one requeue and
    # is reclaimed in the same leader transaction.
    operation, _ = await create_intent("postgres-activation-crash-boundary-0001")
    first = await postgres_store.activation.claim_activation_intent(
        identity,
        leadership_fencing_token=leader_fence,
        lease_seconds=30,
    )
    assert first is not None and first.id == operation.id and first.attempt == 1
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_activation_intents SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            first.id,
        )
        await connection.execute(
            "UPDATE fs2_operations SET lease_expires_at=clock_timestamp()+interval '5 minutes' WHERE id=$1",
            operation.id,
        )
    second = await postgres_store.activation.claim_activation_intent(
        identity,
        leadership_fencing_token=leader_fence,
        lease_seconds=30,
    )
    assert second is not None and second.id == first.id and second.attempt == 2
    assert (await event_names(operation.id)).count("activation_intent_lease_requeued") == 1

    # A crash on the last allowed attempt is sealed, never rewritten queued.
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_activation_intents SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            second.id,
        )
    final_recovery = await asyncio.gather(
        postgres_store.activation.claim_activation_intent(
            identity,
            leadership_fencing_token=leader_fence,
            lease_seconds=30,
        ),
        postgres_store.activation.claim_activation_intent(
            identity,
            leadership_fencing_token=leader_fence,
            lease_seconds=30,
        ),
    )
    assert final_recovery == [None, None]
    exhausted = await postgres_store.get_activation_intent(operation.id)
    assert exhausted.status is ActivationIntentStatus.FAILED
    assert exhausted.error_code == "activation_attempts_exhausted"
    events_after_exhaustion = await event_names(operation.id)
    assert events_after_exhaustion.count("activation_intent_lease_requeued") == 1
    assert events_after_exhaustion.count("activation_intent_attempts_exhausted") == 1
    assert (
        await postgres_store.activation.claim_activation_intent(
            identity,
            leadership_fencing_token=leader_fence,
            lease_seconds=30,
        )
        is None
    )
    assert await postgres_store.get_activation_intent(operation.id) == exhausted
    assert await event_names(operation.id) == events_after_exhaustion
    async with postgres_store.pool.acquire() as connection:
        live_operation = await connection.fetchrow(
            "SELECT status,lease_expires_at>clock_timestamp() AS live FROM fs2_operations WHERE id=$1",
            operation.id,
        )
    assert live_operation is not None
    assert live_operation["status"] == "activating" and live_operation["live"]

    _, runtime = postgres_app(postgres_store, registry)
    for _ in range(2):
        with pytest.raises(ActivationError, match="did not establish readiness"):
            await asyncio.wait_for(
                runtime.admission._await_activation(model, operation),
                timeout=0.2,
            )

    # A queued intent whose deadline elapsed before claim is terminalized with
    # one event; a claimed intent whose deadline wins over lease recovery does
    # the same. Repeated reconciliation is idempotent in both cases.
    queued_deadline_operation, _ = await create_intent("postgres-activation-deadline-queued-0002")
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_activation_intents SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            queued_deadline_operation.id,
        )
    assert (
        await postgres_store.activation.claim_activation_intent(
            identity,
            leadership_fencing_token=leader_fence,
            lease_seconds=30,
        )
        is None
    )
    queued_deadline = await postgres_store.get_activation_intent(queued_deadline_operation.id)
    assert queued_deadline.status is ActivationIntentStatus.EXPIRED
    assert queued_deadline.error_code == "deadline_exceeded"
    queued_events = await event_names(queued_deadline_operation.id)
    assert queued_events.count("activation_intent_deadline_expired") == 1

    claimed_deadline_operation, _ = await create_intent("postgres-activation-deadline-claimed-0003")
    claimed_deadline = await postgres_store.activation.claim_activation_intent(
        identity,
        leadership_fencing_token=leader_fence,
        lease_seconds=30,
    )
    assert claimed_deadline is not None and claimed_deadline.id == claimed_deadline_operation.id
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_activation_intents SET deadline_at=clock_timestamp()-interval '1 second',"
            "lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            claimed_deadline.id,
        )
    assert (
        await postgres_store.activation.claim_activation_intent(
            identity,
            leadership_fencing_token=leader_fence,
            lease_seconds=30,
        )
        is None
    )
    claimed_deadline_result = await postgres_store.get_activation_intent(claimed_deadline_operation.id)
    assert claimed_deadline_result.status is ActivationIntentStatus.EXPIRED
    assert claimed_deadline_result.error_code == "deadline_exceeded"
    claimed_events = await event_names(claimed_deadline_operation.id)
    assert claimed_events.count("activation_intent_deadline_expired") == 1
    assert (
        await postgres_store.activation.claim_activation_intent(
            identity,
            leadership_fencing_token=leader_fence,
            lease_seconds=30,
        )
        is None
    )
    assert await event_names(queued_deadline_operation.id) == queued_events
    assert await event_names(claimed_deadline_operation.id) == claimed_events

    # With one operation attempt, the same terminal activation result produces
    # one terminal operation and a public idempotency replay returns it.
    one_attempt_operation, _ = await create_intent(
        "postgres-activation-operation-replay-0004",
        max_attempts=1,
    )
    one_attempt_claim = await postgres_store.activation.claim_activation_intent(
        identity,
        leadership_fencing_token=leader_fence,
        lease_seconds=30,
    )
    assert one_attempt_claim is not None and one_attempt_claim.id == one_attempt_operation.id
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_activation_intents SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            one_attempt_claim.id,
        )
        await connection.execute(
            "UPDATE fs2_operations SET lease_expires_at=clock_timestamp()+interval '5 minutes' WHERE id=$1",
            one_attempt_operation.id,
        )
    assert (
        await postgres_store.activation.claim_activation_intent(
            identity,
            leadership_fencing_token=leader_fence,
            lease_seconds=30,
        )
        is None
    )
    _, replay_runtime = postgres_app(postgres_store, registry)
    with pytest.raises(ActivationError, match="did not establish readiness") as activation_failure:
        await replay_runtime.admission._await_activation(model, one_attempt_operation)
    await replay_runtime.admission._terminal_failure(
        one_attempt_operation,
        activation_failure.value,
    )
    terminal_operation = await postgres_store.get_operation(one_attempt_operation.id)
    assert terminal_operation.status is OperationStatus.FAILED
    assert terminal_operation.error_code == "activation_failed"
    public_replay = await append(
        postgres_store,
        principal,
        "postgres-activation-operation-replay-0004",
    )
    assert public_replay.id == terminal_operation.id
    assert public_replay.status is OperationStatus.FAILED and public_replay.reused


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_per_model_fence_prevents_distinct_intent_target_regression_and_replays_terminal_result(
    postgres_store: PostgresStore,
    registry,
) -> None:
    principal = await add_token(postgres_store)
    model = registry.get("qwen3-8b")
    contract = ScaleContract.from_model(model)
    for key, worker in (("model-fence-old-0001", "worker-old"), ("model-fence-new-0002", "worker-new")):
        accepted = await append(postgres_store, principal, key)
        operation = await postgres_store.claim_operation(worker, lease_seconds=30)
        assert operation is not None and operation.id == accepted.id
        await postgres_store.ensure_activation_intent(
            operation,
            binding_digest=model.binding.binding_digest,
            worker_id=operation.worker_id,
            fencing_token=operation.fencing_token,
        )

    digest = activation_set(registry.list(enabled_only=True)).digest
    identity, leader_fence = await publish_leader(postgres_store.activation, "model-fence-controller", 501, digest)
    old = await postgres_store.activation.claim_activation_intent(
        identity, leadership_fencing_token=leader_fence, lease_seconds=30
    )
    new = await postgres_store.activation.claim_activation_intent(
        identity, leadership_fencing_token=leader_fence, lease_seconds=30
    )
    assert old is not None and new is not None
    assert old.model_fencing_token is not None and new.model_fencing_token is not None
    assert old.model_fencing_token < new.model_fencing_token

    newest_target = ActivationTargetState(
        model_id=model.id,
        target_uid=contract.target.uid,
        resource_version="rv9",
        observed_generation=INITIAL_GENERATION + 9,
        template_digest=contract.target.template_digest,
        active=True,
        observed_at=datetime.now(UTC),
        controller_fencing_token=leader_fence,
    )
    newest = await postgres_store.activation.complete_activation_intent(
        new.id,
        identity=identity,
        leadership_fencing_token=leader_fence,
        controller_id=new.controller_id,
        fencing_token=new.fencing_token,
        scale_contract_digest=contract.digest,
        target=newest_target,
    )
    assert newest.status.value == "ready"

    stale_target = newest_target.model_copy(
        update={
            "resource_version": "rv1",
            "observed_generation": INITIAL_GENERATION + 1,
        }
    )
    stale = await postgres_store.activation.complete_activation_intent(
        old.id,
        identity=identity,
        leadership_fencing_token=leader_fence,
        controller_id=old.controller_id,
        fencing_token=old.fencing_token,
        scale_contract_digest=contract.digest,
        target=stale_target,
    )
    assert stale.status.value == "failed" and stale.error_code == "stale_model_fence"
    replay = await postgres_store.activation.complete_activation_intent(
        old.id,
        identity=identity,
        leadership_fencing_token=leader_fence,
        controller_id=old.controller_id,
        fencing_token=old.fencing_token,
        scale_contract_digest=contract.digest,
        target=stale_target,
    )
    assert replay == stale
    durable = await postgres_store.activation.get_activation_target_state(model.id)
    assert durable is not None
    assert durable.resource_version == "rv9"
    assert durable.model_fencing_token == new.model_fencing_token


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_distinct_configured_roles_run_activation_and_retention_with_closed_privileges(
    postgres_store: PostgresStore,
    registry,
    cipher: PayloadCipher,
    hasher: KeyedHasher,
) -> None:
    from test_model_deployment_admin import append_request, observation

    database_url = os.environ["FS2_TEST_DATABASE_URL"]
    suffix = uuid4().hex[:10]
    reporting_role = f"fs2_test_reporting_{suffix}"
    runtime_role = f"fs2_test_runtime_{suffix}"
    maintenance_role = f"fs2_test_maintenance_{suffix}"
    activation_role = f"fs2_test_activation_{suffix}"
    runtime_login = f"fs2_test_runtime_login_{suffix}"
    maintenance_login = f"fs2_test_maintenance_login_{suffix}"
    activation_login = f"fs2_test_activation_login_{suffix}"
    runtime_password = uuid4().hex
    maintenance_password = uuid4().hex
    activation_password = uuid4().hex
    runtime_pool: asyncpg.Pool | None = None
    maintenance_pool: asyncpg.Pool | None = None
    activation_pool: asyncpg.Pool | None = None

    try:
        await PostgresStore.migrate_database(
            database_url,
            CONTROL_ROOT / "migrations",
            reporting_role,
            runtime_role,
            maintenance_role,
            activation_role,
        )
        async with postgres_store.pool.acquire() as connection:
            role_rows = await connection.fetch(
                "SELECT rolname,rolcanlogin FROM pg_roles WHERE rolname=ANY($1::text[])",
                [reporting_role, runtime_role, maintenance_role, activation_role],
            )
            assert {str(row["rolname"]): bool(row["rolcanlogin"]) for row in role_rows} == {
                reporting_role: False,
                runtime_role: False,
                maintenance_role: False,
                activation_role: False,
            }
            await connection.execute(
                f'CREATE ROLE "{runtime_login}" LOGIN PASSWORD \'{runtime_password}\' IN ROLE "{runtime_role}"'
            )
            await connection.execute(
                f"CREATE ROLE \"{maintenance_login}\" LOGIN PASSWORD '{maintenance_password}' "
                f'IN ROLE "{maintenance_role}"'
            )
            await connection.execute(
                f'CREATE ROLE "{activation_login}" LOGIN PASSWORD \'{activation_password}\' IN ROLE "{activation_role}"'
            )

        principal = await add_token(postgres_store)
        accepted = await append(postgres_store, principal, "distinct-role-activation-0001")
        operation = await postgres_store.claim_operation("distinct-role-worker", lease_seconds=30)
        assert operation is not None and operation.id == accepted.id
        model = registry.get(operation.model_id)
        contract = ScaleContract.from_model(model)

        runtime_pool = await asyncpg.create_pool(
            dsn=database_url,
            user=runtime_login,
            password=runtime_password,
            min_size=1,
            max_size=1,
        )
        assert runtime_pool is not None
        runtime_store = PostgresStore(
            runtime_pool,
            CONTROL_ROOT / "migrations",
            cipher,
            hasher,
            payload_ttl_seconds=3600,
        )
        model_revision = (
            await runtime_store.model_deployment_append_revision(append_request(key="restricted-runtime-model-0001"))
        ).value
        assert (
            await runtime_store.model_deployment_current(
                namespace=model_revision.namespace,
                name=model_revision.name,
                tenant_id=model_revision.tenant_id,
            )
        ) == model_revision
        model_observation = observation(revision=1, etag=model_revision.etag)
        assert await runtime_store.model_deployment_append_status(model_observation) == model_observation
        assert (
            await runtime_store.model_deployment_status(
                namespace=model_revision.namespace,
                name=model_revision.name,
                tenant_id=model_revision.tenant_id,
            )
        ) == model_observation
        intent = await runtime_store.ensure_activation_intent(
            operation,
            binding_digest=model.binding.binding_digest,
            worker_id=operation.worker_id,
            fencing_token=operation.fencing_token,
        )
        assert intent.id == operation.id

        activation_pool = await asyncpg.create_pool(
            dsn=database_url,
            user=activation_login,
            password=activation_password,
            min_size=1,
            max_size=1,
        )
        assert activation_pool is not None
        activation_store = PostgresActivationStore(activation_pool, owns_pool=False)
        expected_digest = activation_set(registry.list(enabled_only=True)).digest
        assert not await runtime_store.activation_controller_ready(expected_digest)
        identity, leader_fence = await publish_leader(
            activation_store,
            "credential-controller",
            401,
            expected_digest,
        )
        assert leader_fence == 1
        assert await runtime_store.activation_controller_ready(expected_digest)
        claimed = await activation_store.claim_activation_intent(
            identity,
            leadership_fencing_token=leader_fence,
            lease_seconds=30,
        )
        assert claimed is not None and claimed.id == intent.id
        await activation_store.heartbeat_activation_intent(
            claimed.id,
            identity=identity,
            leadership_fencing_token=leader_fence,
            controller_id=claimed.controller_id,
            fencing_token=claimed.fencing_token,
            lease_seconds=30,
        )
        target = ActivationTargetState(
            model_id=model.id,
            target_uid=contract.target.uid,
            resource_version=INITIAL_RESOURCE_VERSION,
            observed_generation=INITIAL_GENERATION,
            template_digest=contract.target.template_digest,
            active=True,
            observed_at=datetime.now(UTC),
            controller_fencing_token=leader_fence,
        )
        ready = await activation_store.complete_activation_intent(
            claimed.id,
            identity=identity,
            leadership_fencing_token=leader_fence,
            controller_id=claimed.controller_id,
            fencing_token=claimed.fencing_token,
            scale_contract_digest=contract.digest,
            target=target,
        )
        assert ready.status.value == "ready"
        assert (await runtime_store.get_activation_intent(operation.id)).status.value == "ready"

        await runtime_store.complete_operation(
            operation.id,
            status=OperationStatus.FAILED,
            outcome="least_privilege_test",
            semantic_outcome="failed",
            http_status=503,
            response_body=None,
            response_content_type=None,
            error_code="least_privilege_test",
            error_detail=None,
            runtime=RuntimeIdentity(),
            worker_id=operation.worker_id,
            fencing_token=operation.fencing_token,
        )
        key_usage = await runtime_store.admin_key_usage((principal.token_id,), tenant_id=principal.tenant_id)
        assert len(key_usage) == 1
        assert key_usage[0].token_id == principal.token_id
        assert key_usage[0].terminal_operations == 1

        async with runtime_pool.acquire() as runtime_connection:
            for statement in (
                "SELECT * FROM fs2_activation_target_state LIMIT 0",
                "SELECT * FROM fs2_activation_events LIMIT 0",
                "SELECT last_value FROM fs2_activation_events_id_seq",
                "UPDATE fs2_activation_controller_status SET lease_expires_at=clock_timestamp()",
                "UPDATE fs2_activation_intents SET status='ready',fencing_token=777,"
                "target_uid='forged',target_resource_version='777',target_observed_generation=777,"
                "target_template_digest=repeat('f',64),target_active=true,"
                "target_observed_at=clock_timestamp()",
                "UPDATE fs2_usage_facts SET outcome='forged'",
                "DELETE FROM fs2_usage_facts",
                "UPDATE fs2_audit_events SET outcome='forged'",
                "DELETE FROM fs2_audit_events",
                "UPDATE fs2_model_deployment_revisions SET action='rollback'",
                "UPDATE fs2_model_deployment_idempotency SET request_hmac=repeat('f',64)",
                "UPDATE fs2_model_deployment_status_events SET revision=777",
                "DELETE FROM fs2_model_deployments",
                "DELETE FROM fs2_model_deployment_revisions",
                "DELETE FROM fs2_operations",
                "DELETE FROM fs2_tokens",
                f'CREATE TABLE public."fs2_runtime_forbidden_{suffix}" (id integer)',
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await runtime_connection.execute(statement)

        async with postgres_store.pool.acquire() as owner_connection:
            assert (
                await owner_connection.fetchval(
                    "SELECT count(*) FROM fs2_usage_facts WHERE operation_id=$1",
                    operation.id,
                )
                == 1
            )
            await owner_connection.execute(
                "UPDATE fs2_operations SET payload_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
                operation.id,
            )
            await owner_connection.execute(
                "UPDATE fs2_usage_facts SET occurred_at=clock_timestamp()-interval '2 hours' WHERE operation_id=$1",
                operation.id,
            )
            audit_id = await owner_connection.fetchval(
                "SELECT id FROM fs2_audit_events WHERE token_id=$1 ORDER BY id LIMIT 1",
                principal.token_id,
            )
            assert audit_id is not None
            await owner_connection.execute(
                "UPDATE fs2_audit_events SET occurred_at=clock_timestamp()-interval '2 hours' WHERE id=$1",
                audit_id,
            )

        maintenance_pool = await asyncpg.create_pool(
            dsn=database_url,
            user=maintenance_login,
            password=maintenance_password,
            min_size=1,
            max_size=1,
        )
        assert maintenance_pool is not None
        maintenance_store = PostgresMaintenanceStore(maintenance_pool)
        assert await maintenance_store.purge_expired_payloads() == 1
        deleted = await maintenance_store.delete_expired_rows(
            operation_retention_seconds=31536000,
            token_retention_seconds=31536000,
            audit_retention_seconds=3600,
            usage_retention_seconds=3600,
        )
        assert deleted["operations"] == 0 and deleted["tokens"] == 0
        assert deleted["audit"] == 1 and deleted["usage"] == 1

        async with maintenance_pool.acquire() as maintenance_connection:
            for statement in (
                "SELECT request_ciphertext FROM fs2_operations LIMIT 0",
                "SELECT digest FROM fs2_tokens LIMIT 0",
                "SELECT detail FROM fs2_audit_events LIMIT 0",
                "SELECT outcome FROM fs2_usage_facts LIMIT 0",
                "SELECT * FROM fs2_operation_events LIMIT 0",
                "SELECT last_value FROM fs2_operation_events_id_seq",
                "SELECT * FROM fs2_model_deployments LIMIT 0",
                "SELECT * FROM fs2_model_deployment_status_events LIMIT 0",
                f"INSERT INTO fs2_operation_events(operation_id,event,status,attempt) "
                f"VALUES('{operation.id}','forged','failed',1)",
                "UPDATE fs2_audit_events SET outcome='forged'",
                "UPDATE fs2_usage_facts SET outcome='forged'",
                "INSERT INTO fs2_audit_events(actor,action,target_type,target_id,outcome) "
                "VALUES('forged','forged','forged','forged','forged')",
                f'CREATE TABLE public."fs2_maintenance_forbidden_{suffix}" (id integer)',
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await maintenance_connection.execute(statement)

        async with activation_pool.acquire() as activation_connection:
            for statement in (
                "SELECT token_id,tenant_id,principal_id,request_ciphertext,response_ciphertext "
                "FROM fs2_operations LIMIT 0",
                "SELECT * FROM fs2_tokens LIMIT 0",
                "SELECT * FROM fs2_audit_events LIMIT 0",
                "SELECT * FROM fs2_model_deployments LIMIT 0",
                "SELECT * FROM fs2_model_deployment_revisions LIMIT 0",
                "SELECT * FROM fs2_activation_events LIMIT 0",
                "SELECT last_value FROM fs2_activation_events_id_seq",
                f'CREATE TABLE public."fs2_activation_forbidden_{suffix}" (id integer)',
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await activation_connection.execute(statement)
    finally:
        if runtime_pool is not None:
            await runtime_pool.close()
        if maintenance_pool is not None:
            await maintenance_pool.close()
        if activation_pool is not None:
            await activation_pool.close()
        async with postgres_store.pool.acquire() as connection:
            for role in (runtime_login, maintenance_login, activation_login):
                if await connection.fetchval("SELECT true FROM pg_roles WHERE rolname=$1", role):
                    await connection.execute(f'DROP ROLE "{role}"')
            for role in (reporting_role, runtime_role, maintenance_role, activation_role):
                if await connection.fetchval("SELECT true FROM pg_roles WHERE rolname=$1", role):
                    await connection.execute(f'DROP OWNED BY "{role}"')
                    await connection.execute(f'DROP ROLE "{role}"')


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_activation_role_cannot_read_operation_identity_payload_or_result_columns(
    postgres_store: PostgresStore,
) -> None:
    allowed = (
        "id",
        "model_id",
        "model_revision",
        "status",
        "attempt",
        "lease_expires_at",
        "deadline_at",
    )
    forbidden = (
        "max_attempts",
        "worker_id",
        "fencing_token",
        "token_id",
        "tenant_id",
        "principal_id",
        "idempotency_key",
        "request_nonce",
        "request_ciphertext",
        "request_hmac",
        "response_nonce",
        "response_ciphertext",
        "response_hmac",
    )
    async with postgres_store.pool.acquire() as connection:
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_runtime','fs2_activation_intents','INSERT')"
        )
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_runtime','fs2_activation_intents','UPDATE')"
        )
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_runtime','fs2_activation_events','INSERT')"
        )
        assert await connection.fetchval(
            "SELECT has_function_privilege('fs2_serve_runtime',oid,'EXECUTE') FROM pg_proc "
            "WHERE proname='fs2_runtime_ensure_activation_intent'"
        )
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_activation','fs2_operations','SELECT')"
        )
        assert await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_activation','fs2_activation_events','INSERT')"
        )
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_activation','fs2_activation_events','SELECT')"
        )
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_activation','fs2_activation_events','UPDATE')"
        )
        assert await connection.fetchval(
            "SELECT has_sequence_privilege('fs2_serve_activation','fs2_activation_events_id_seq','USAGE')"
        )
        assert not await connection.fetchval(
            "SELECT has_sequence_privilege('fs2_serve_activation','fs2_activation_events_id_seq','SELECT')"
        )
        for column in allowed:
            assert await connection.fetchval(
                "SELECT has_column_privilege('fs2_serve_activation','fs2_operations',$1,'SELECT')",
                column,
            )
        for column in forbidden:
            assert not await connection.fetchval(
                "SELECT has_column_privilege('fs2_serve_activation','fs2_operations',$1,'SELECT')",
                column,
            )
        await connection.execute("SET ROLE fs2_serve_activation")
        try:
            await connection.fetch(
                """
                SELECT id,model_id,model_revision,status,attempt,lease_expires_at,deadline_at
                FROM fs2_operations LIMIT 0
                """
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch(
                    """
                    SELECT max_attempts,worker_id,fencing_token,token_id,tenant_id,principal_id,
                           idempotency_key,request_nonce,request_ciphertext,request_hmac,
                           response_nonce,response_ciphertext,response_hmac
                    FROM fs2_operations LIMIT 0
                    """
                )
        finally:
            await connection.execute("RESET ROLE")


async def add_token(store: PostgresStore, token_id: UUID | None = None, *, max_concurrency: int = 4) -> Principal:
    token_id = token_id or uuid4()
    await store.issue_token(
        token_id=token_id,
        prefix=f"fs2_pat_{token_id.hex[:12]}",
        pepper_key_id="pepper-v1",
        digest="argon2-test-digest",
        request=TokenCreate(
            principal_id=f"principal-{token_id.hex[:8]}",
            tenant_id="tenant-a",
            scopes={Scope.INFERENCE_INVOKE},
            models={"qwen3-8b"},
            gpu_seconds_budget=1000,
            max_concurrency=max_concurrency,
        ),
        created_by="bootstrap-admin",
    )
    return Principal(
        token_id=token_id,
        token_prefix=f"fs2_pat_{token_id.hex[:12]}",
        principal_id=f"principal-{token_id.hex[:8]}",
        tenant_id="tenant-a",
        scopes=frozenset({"inference.invoke"}),
        models=frozenset({"qwen3-8b"}),
        gpu_seconds_budget=1000,
        max_concurrency=max_concurrency,
    )


def request(key: str, body: bytes = b'{"private":"prompt"}') -> AdmissionRequest:
    return AdmissionRequest(
        model_id="qwen3-8b",
        operation="chat",
        protocol="openai-chat",
        idempotency_key=key,
        request_body=body,
    )


async def append(store: PostgresStore, principal: Principal, key: str, body: bytes = b'{"private":"prompt"}'):
    return await store.append_operation(
        principal=principal,
        admission=request(key, body),
        model_revision="b968826d",
        reserved_gpu_seconds=10,
        max_attempts=2,
    )


def postgres_app(store: PostgresStore, registry) -> tuple[FastAPI, AppRuntime]:
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
    peppers = PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32})
    runtime = AppRuntime(
        settings=settings,
        registry=registry,
        store=store,
        tokens=TokenService(store, peppers),
        admission=admission,
        metrics=metrics,
        admin_token=ADMIN_TOKEN.encode(),
        operator_sessions=OperatorSessionService(store, peppers),
        owns_store=False,
    )
    return create_app(runtime), runtime


async def issue_http_token(client: httpx2.AsyncClient, *, max_concurrency: int = 1) -> dict[str, object]:
    response = await client.post(
        "/admin/v1/tokens",
        headers={"authorization": f"Bearer {ADMIN_TOKEN}"},
        json={
            "principal_id": "postgres-endpoint-owner",
            "tenant_id": "tenant-a",
            "scopes": ["inference.invoke"],
            "models": ["qwen3-8b"],
            "gpu_seconds_budget": 1000,
            "max_concurrency": max_concurrency,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_audit_jsonb_round_trip_decodes_to_typed_object(postgres_store: PostgresStore) -> None:
    principal = await add_token(postgres_store)
    async with postgres_store.pool.acquire() as connection:
        raw_detail = await connection.fetchval(
            "SELECT detail FROM fs2_audit_events WHERE token_id=$1", principal.token_id
        )
    assert isinstance(raw_detail, str)
    rows = await postgres_store.list_audit(tenant_id=principal.tenant_id)
    assert len(rows) == 1
    event = rows[0]
    assert event.action == "token.issue"
    assert event.token_id == principal.token_id
    assert isinstance(event.detail, dict)
    assert event.detail == {
        "models": ["qwen3-8b"],
        "prefix": principal.token_prefix,
        "scopes": ["inference.invoke"],
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_audit_endpoint_decodes_asyncpg_jsonb_strings(postgres_store: PostgresStore, registry) -> None:
    principal = await add_token(postgres_store)
    async with postgres_store.pool.acquire() as connection:
        raw_detail = await connection.fetchval(
            "SELECT detail FROM fs2_audit_events WHERE token_id=$1", principal.token_id
        )
    assert isinstance(raw_detail, str)

    app, _ = postgres_app(postgres_store, registry)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="https://inference.test.invalid", trust_env=False
        ) as client:
            response = await client.get(
                "/admin/v1/audit",
                params={"tenant_id": principal.tenant_id},
                headers={"authorization": f"Bearer {ADMIN_TOKEN}"},
            )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["action"] == "token.issue"
    assert payload[0]["token_id"] == str(principal.token_id)
    assert payload[0]["detail"] == {
        "models": ["qwen3-8b"],
        "prefix": principal.token_prefix,
        "scopes": ["inference.invoke"],
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_deadline_janitor_terminalizes_unclaimed_work_and_releases_capacity(
    postgres_store: PostgresStore,
) -> None:
    principal = await add_token(postgres_store, max_concurrency=1)
    expired = await append(postgres_store, principal, "deadline-expired-before-claim-0001")
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE fs2_operations SET deadline_at=clock_timestamp()-interval '1 second',
                payload_expires_at=clock_timestamp()+interval '3 days'
            WHERE id=$1
            """,
            expired.id,
        )
        reserved = await connection.fetchval(
            "SELECT gpu_seconds_reserved FROM fs2_tokens WHERE id=$1", principal.token_id
        )
    assert reserved == 10
    with pytest.raises(ConcurrencyExceededError):
        await append(postgres_store, principal, "deadline-blocked-capacity-0002")

    assert await postgres_store.expire_deadline_operations() == 1
    assert await postgres_store.expire_deadline_operations() == 0
    terminal = await postgres_store.get_operation(expired.id, tenant_id=principal.tenant_id)
    assert terminal.status == OperationStatus.EXPIRED
    assert terminal.outcome == "expired"
    assert terminal.error_code == "deadline_exceeded"
    assert terminal.completed_at is not None
    assert terminal.reserved_gpu_seconds == 0

    async with postgres_store.pool.acquire() as connection:
        operation = await connection.fetchrow(
            """
            SELECT status,request_ciphertext,payload_expires_at>clock_timestamp() AS payload_live
            FROM fs2_operations WHERE id=$1
            """,
            expired.id,
        )
        token = await connection.fetchrow("SELECT gpu_seconds_reserved FROM fs2_tokens WHERE id=$1", principal.token_id)
        event = await connection.fetchrow(
            """
            SELECT event,status,attempt FROM fs2_operation_events
            WHERE operation_id=$1 ORDER BY id DESC LIMIT 1
            """,
            expired.id,
        )
    assert operation is not None
    assert operation["status"] == "expired"
    assert operation["request_ciphertext"] is not None and operation["payload_live"]
    assert token is not None and token["gpu_seconds_reserved"] == 0
    assert event is not None and (event["event"], event["status"], event["attempt"]) == (
        "deadline_expired",
        "expired",
        0,
    )

    replacement = await append(postgres_store, principal, "deadline-capacity-released-0003")
    claimed = await postgres_store.claim_operation("deadline-replacement-worker", lease_seconds=30)
    assert claimed is not None and claimed.id == replacement.id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_expired_queued_http_operation_releases_token_capacity_before_payload_ttl(
    postgres_store: PostgresStore, registry
) -> None:
    app, _ = postgres_app(postgres_store, registry)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="https://inference.test.invalid", trust_env=False
        ) as client:
            issued = await issue_http_token(client, max_concurrency=1)
            token = issued["token"]
            assert isinstance(token, str)
            invoke_headers = {
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
                "x-fs2-wait-seconds": "0",
            }
            first = await client.post(
                "/v1/chat/completions",
                headers={**invoke_headers, "idempotency-key": "endpoint-deadline-expired-0001"},
                json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "private"}]},
            )
            assert first.status_code == 202, first.text
            operation_id = UUID(first.json()["id"])
            async with postgres_store.pool.acquire() as connection:
                await connection.execute(
                    """
                    UPDATE fs2_operations SET deadline_at=clock_timestamp()-interval '1 second',
                        payload_expires_at=clock_timestamp()+interval '3 days'
                    WHERE id=$1
                    """,
                    operation_id,
                )
                before = await connection.fetchrow(
                    """
                    SELECT o.request_ciphertext IS NOT NULL AS payload_present,
                        o.payload_expires_at>clock_timestamp() AS payload_live,t.gpu_seconds_reserved
                    FROM fs2_operations o JOIN fs2_tokens t ON t.id=o.token_id WHERE o.id=$1
                    """,
                    operation_id,
                )
            assert before is not None and before["payload_present"] and before["payload_live"]
            assert before["gpu_seconds_reserved"] > 0

            blocked = await client.post(
                "/v1/chat/completions",
                headers={**invoke_headers, "idempotency-key": "endpoint-deadline-blocked-0002"},
                json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "second"}]},
            )
            assert blocked.status_code == 429, blocked.text

            assert await postgres_store.expire_deadline_operations() == 1
            status = await client.get(f"/v1/operations/{operation_id}", headers=invoke_headers)
            assert status.status_code == 200, status.text
            assert status.json()["status"] == "expired"
            assert status.json()["error_code"] == "deadline_exceeded"
            token_rows = await client.get(
                "/admin/v1/tokens",
                params={"tenant_id": "tenant-a"},
                headers={"authorization": f"Bearer {ADMIN_TOKEN}"},
            )
            assert token_rows.status_code == 200, token_rows.text
            current = next(row for row in token_rows.json() if row["id"] == issued["id"])
            assert current["gpu_seconds_reserved"] == 0

            async with postgres_store.pool.acquire() as connection:
                after = await connection.fetchrow(
                    """
                    SELECT status,request_ciphertext IS NOT NULL AS payload_present,
                        payload_expires_at>clock_timestamp() AS payload_live,reserved_gpu_seconds
                    FROM fs2_operations WHERE id=$1
                    """,
                    operation_id,
                )
            assert after is not None
            assert (
                after["status"],
                after["payload_present"],
                after["payload_live"],
                after["reserved_gpu_seconds"],
            ) == (
                "expired",
                True,
                True,
                0,
            )
            replacement = await client.post(
                "/v1/chat/completions",
                headers={**invoke_headers, "idempotency-key": "endpoint-deadline-released-0003"},
                json={"model": "qwen3-8b", "messages": [{"role": "user", "content": "replacement"}]},
            )
            assert replacement.status_code == 202, replacement.text


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_database_rejects_oversized_operation_identifiers(postgres_store: PostgresStore) -> None:
    principal = await add_token(postgres_store)
    operation = await append(postgres_store, principal, "database-bound-key-0001")
    async with postgres_store.pool.acquire() as connection:
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE fs2_operations SET model_id=repeat('m',129) WHERE id=$1",
                operation.id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE fs2_operations SET idempotency_key=repeat('i',201) WHERE id=$1",
                operation.id,
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_independent_token_admission_proceeds_while_another_token_lock_is_held(
    postgres_store: PostgresStore,
) -> None:
    first = await add_token(postgres_store)
    second = await add_token(postgres_store)
    blocker = await postgres_store.pool.acquire()
    transaction = blocker.transaction()
    await transaction.start()
    await postgres_store._token_lock(blocker, first.token_id)
    blocked = asyncio.create_task(append(postgres_store, first, "blocked-token-key-0001"))
    try:
        await asyncio.sleep(0.05)
        independent = await asyncio.wait_for(append(postgres_store, second, "independent-key-0001"), timeout=1)
        assert independent.token_id == second.token_id
        assert not blocked.done()
    finally:
        await transaction.rollback()
        await postgres_store.pool.release(blocker)
    assert (await asyncio.wait_for(blocked, timeout=1)).token_id == first.token_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_blocked_janitor_cannot_starve_independent_heartbeat(
    postgres_store: PostgresStore,
) -> None:
    first = await add_token(postgres_store)
    second = await add_token(postgres_store)
    first_op = await append(postgres_store, first, "janitor-first-key-0001")
    second_op = await append(postgres_store, second, "janitor-second-key-0002")
    first_claim = await postgres_store.claim_operation("worker-first", lease_seconds=30)
    second_claim = await postgres_store.claim_operation("worker-second", lease_seconds=30)
    assert first_claim is not None and second_claim is not None
    claims = {first_claim.id: first_claim, second_claim.id: second_claim}
    first_claim = claims[first_op.id]
    second_claim = claims[second_op.id]
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_operations SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            first_op.id,
        )

    blocker = await postgres_store.pool.acquire()
    transaction = blocker.transaction()
    await transaction.start()
    await postgres_store._token_lock(blocker, first.token_id)
    janitor = asyncio.create_task(postgres_store.reap_stale_operations())
    try:
        await asyncio.sleep(0.05)
        await asyncio.wait_for(
            postgres_store.heartbeat(
                second_claim.id,
                worker_id=second_claim.worker_id,
                fencing_token=second_claim.fencing_token,
                lease_seconds=30,
            ),
            timeout=1,
        )
        assert not janitor.done()
    finally:
        await transaction.rollback()
        await postgres_store.pool.release(blocker)
    assert await asyncio.wait_for(janitor, timeout=1) == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admission_replay_and_completion_share_token_then_operation_lock_order(
    postgres_store: PostgresStore,
) -> None:
    principal = await add_token(postgres_store)
    accepted = await append(postgres_store, principal, "lock-order-key-0001")
    claimed = await postgres_store.claim_operation("worker", lease_seconds=30)
    assert claimed is not None and claimed.id == accepted.id
    completion = postgres_store.complete_operation(
        claimed.id,
        status=OperationStatus.SUCCEEDED,
        outcome="succeeded",
        semantic_outcome="protocol_valid",
        http_status=200,
        response_body=b'{"choices":[{"message":{"content":"ok"}}]}',
        response_content_type="application/json",
        error_code=None,
        error_detail=None,
        runtime=RuntimeIdentity(gpu_count=1, preemptible=True),
        worker_id=claimed.worker_id,
        fencing_token=claimed.fencing_token,
    )
    replay = append(postgres_store, principal, "lock-order-key-0001")
    completed, reused = await asyncio.wait_for(asyncio.gather(completion, replay), timeout=2)
    assert completed.status == OperationStatus.SUCCEEDED
    assert reused.id == completed.id and reused.reused


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_database_rows_checks_and_logs_are_payload_free(postgres_store: PostgresStore) -> None:
    prompt = b"POSTGRES_RAW_PROMPT_53d1"
    response = b'{"choices":[{"message":{"content":"POSTGRES_RAW_RESPONSE_ea72"}}]}'
    bearer = "fs2_pat_RAW_BEARER_a1c9"
    principal = await add_token(postgres_store)
    accepted = await append(postgres_store, principal, "privacy-postgres-key-0001", prompt)
    claimed = await postgres_store.claim_operation("worker", lease_seconds=30)
    assert claimed is not None
    await postgres_store.complete_operation(
        claimed.id,
        status=OperationStatus.SUCCEEDED,
        outcome="succeeded",
        semantic_outcome="protocol_valid",
        http_status=200,
        response_body=response,
        response_content_type="application/json",
        error_code=None,
        error_detail=f"{prompt.decode()} {bearer}",
        runtime=RuntimeIdentity(gpu_count=1),
        worker_id=claimed.worker_id,
        fencing_token=claimed.fencing_token,
    )
    async with postgres_store.pool.acquire() as connection:
        rendered = await connection.fetchval(
            """
            SELECT row_to_json(value)::text FROM (
                SELECT o.*, (SELECT json_agg(e) FROM fs2_operation_events e WHERE e.operation_id=o.id) events,
                       (SELECT json_agg(a) FROM fs2_audit_events a WHERE a.token_id=o.token_id) audits
                FROM fs2_operations o WHERE o.id=$1
            ) value
            """,
            accepted.id,
        )
        assert rendered is not None
        assert all(secret not in rendered for secret in (prompt.decode(), response.decode(), bearer))
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE fs2_operations SET request_nonce=NULL WHERE id=$1",
                accepted.id,
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_revoke_atomically_cancels_and_fences_claimed_operation(postgres_store: PostgresStore) -> None:
    principal = await add_token(postgres_store)
    accepted = await append(postgres_store, principal, "postgres-revoke-key-0001")
    claimed = await postgres_store.claim_operation("worker", lease_seconds=30)
    assert claimed is not None
    token = await postgres_store.revoke_token(principal.token_id, actor="bootstrap-admin")
    current = await postgres_store.get_operation(accepted.id, tenant_id=principal.tenant_id)
    assert token.revoked_at is not None
    assert token.gpu_seconds_reserved == 0
    assert current.status == OperationStatus.CANCELLED
    assert current.fencing_token > claimed.fencing_token


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_expired_token_batch_at_queue_head_cannot_starve_valid_later_operation(
    postgres_store: PostgresStore,
) -> None:
    expired_principal = await add_token(postgres_store, max_concurrency=24)
    expired_operations = [
        await append(postgres_store, expired_principal, f"expired-head-key-{index:04d}") for index in range(20)
    ]
    valid_principal = await add_token(postgres_store)
    valid_operation = await append(postgres_store, valid_principal, "valid-after-expired-head-0001")
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_tokens SET expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            expired_principal.token_id,
        )
        await connection.execute(
            """
            UPDATE fs2_operations SET payload_expires_at=clock_timestamp()+interval '3 days'
            WHERE token_id=$1
            """,
            expired_principal.token_id,
        )

    claimed = await postgres_store.claim_operation("worker-after-expired-head", lease_seconds=30)
    assert claimed is not None and claimed.id == valid_operation.id

    async with postgres_store.pool.acquire() as connection:
        status_counts = await connection.fetchrow(
            """
            SELECT count(*) FILTER (WHERE status='expired') AS expired,
                   count(*) FILTER (WHERE status='queued') AS queued,
                   min(payload_expires_at)>clock_timestamp()+interval '2 days' AS payloads_still_live
            FROM fs2_operations WHERE token_id=$1
            """,
            expired_principal.token_id,
        )
        token = await connection.fetchrow(
            "SELECT gpu_seconds_reserved FROM fs2_tokens WHERE id=$1",
            expired_principal.token_id,
        )
    assert status_counts is not None
    assert (status_counts["expired"], status_counts["queued"], status_counts["payloads_still_live"]) == (16, 4, True)
    assert token is not None and token["gpu_seconds_reserved"] == 40

    # The next claim performs the next bounded cleanup batch even though the
    # valid operation is already leased, without waiting for the three-day TTL.
    assert await postgres_store.claim_operation("cleanup-expired-tail", lease_seconds=30) is None
    async with postgres_store.pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT status,error_code,reserved_gpu_seconds FROM fs2_operations
            WHERE id=ANY($1::uuid[]) ORDER BY accepted_at,id
            """,
            [operation.id for operation in expired_operations],
        )
        token = await connection.fetchrow(
            "SELECT gpu_seconds_reserved FROM fs2_tokens WHERE id=$1",
            expired_principal.token_id,
        )
        events = await connection.fetchval(
            """
            SELECT count(*) FROM fs2_operation_events
            WHERE operation_id=ANY($1::uuid[]) AND event='token_inactive'
            """,
            [operation.id for operation in expired_operations],
        )
    assert len(rows) == 20
    assert all(
        row["status"] == "expired" and row["error_code"] == "token_inactive" and row["reserved_gpu_seconds"] == 0
        for row in rows
    )
    assert token is not None and token["gpu_seconds_reserved"] == 0
    assert events == 20


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_last_allowed_attempt_shutdown_release_terminalizes_and_cannot_be_reclaimed(
    postgres_store: PostgresStore,
) -> None:
    principal = await add_token(postgres_store)
    accepted = await append(postgres_store, principal, "last-attempt-shutdown-0001")

    first = await postgres_store.claim_operation("rolling-pod-old", lease_seconds=30)
    assert first is not None and first.id == accepted.id and first.attempt == 1
    requeued = await postgres_store.release_operation(
        first.id,
        worker_id=first.worker_id,
        fencing_token=first.fencing_token,
    )
    assert requeued.status == OperationStatus.QUEUED
    assert requeued.attempt == 1 and requeued.error_code == "worker_released"

    last = await postgres_store.claim_operation("rolling-pod-new", lease_seconds=30)
    assert last is not None and last.id == accepted.id and last.attempt == last.max_attempts == 2
    terminal = await postgres_store.release_operation(
        last.id,
        worker_id=last.worker_id,
        fencing_token=last.fencing_token,
    )
    assert terminal.status == OperationStatus.EXPIRED
    assert terminal.attempt == terminal.max_attempts == 2
    assert terminal.error_code == "attempts_exhausted"
    assert terminal.estimated_gpu_seconds == 10
    assert terminal.reserved_gpu_seconds == 0
    assert await postgres_store.claim_operation("forbidden-n-plus-one", lease_seconds=30) is None

    async with postgres_store.pool.acquire() as connection:
        token = await connection.fetchrow(
            "SELECT gpu_seconds_used,gpu_seconds_reserved FROM fs2_tokens WHERE id=$1",
            principal.token_id,
        )
        assert token is not None
        assert token["gpu_seconds_used"] == 10 and token["gpu_seconds_reserved"] == 0
        events = await connection.fetch(
            "SELECT event,attempt FROM fs2_operation_events WHERE operation_id=$1 ORDER BY id",
            accepted.id,
        )
        assert [(event["event"], event["attempt"]) for event in events][-2:] == [
            ("activation_started", 2),
            ("attempts_exhausted", 2),
        ]
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "UPDATE fs2_operations SET attempt=max_attempts+1 WHERE id=$1",
                accepted.id,
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_terminal_usage_facts_are_exactly_once_survive_operation_retention_and_back_safe_views(
    postgres_store: PostgresStore,
) -> None:
    principal = await add_token(postgres_store, max_concurrency=2)
    first = await append(postgres_store, principal, "terminal-fact-first-0001")
    second = await append(postgres_store, principal, "terminal-fact-second-0002")
    claimed = await postgres_store.claim_operation("terminal-fact-worker", lease_seconds=30)
    assert claimed is not None and claimed.id == first.id

    await postgres_store.revoke_token(principal.token_id, actor="bootstrap-admin")
    await postgres_store.revoke_token(principal.token_id, actor="bootstrap-admin")
    accounting = await postgres_store.terminal_accounting()
    cancelled = next(row for row in accounting if row.outcome == "token_revoked")
    assert cancelled.operations == 2
    assert cancelled.estimated_gpu_seconds == 5

    async with postgres_store.pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM fs2_usage_facts") == 2
        model_view = await connection.fetchrow(
            "SELECT sum(operations) AS operations,sum(estimated_gpu_seconds) AS gpu FROM fs2_reporting_model_usage"
        )
        principal_view = await connection.fetchrow(
            "SELECT sum(operations) AS operations FROM fs2_reporting_principal_usage WHERE principal_id=$1",
            principal.principal_id,
        )
        assert model_view is not None and (model_view["operations"], model_view["gpu"]) == (2, 5)
        assert principal_view is not None and principal_view["operations"] == 2
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_reporting','fs2_operations','SELECT')"
        )
        assert await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_reporting','fs2_reporting_model_usage','SELECT')"
        )
        assert not await connection.fetchval("SELECT has_schema_privilege('fs2_serve_runtime','public','CREATE')")
        assert not await connection.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM aclexplode((SELECT nspacl FROM pg_namespace WHERE nspname='public'))
                WHERE grantee=0 AND privilege_type='CREATE'
            )
            """
        )
        assert await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_runtime','fs2_operations','SELECT,INSERT,UPDATE')"
        )
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_runtime','fs2_operations','DELETE')"
        )
        for table in ("fs2_usage_facts", "fs2_audit_events"):
            assert not await connection.fetchval(
                "SELECT has_table_privilege('fs2_serve_runtime',$1,'UPDATE,DELETE')",
                table,
            )
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_runtime','fs2_usage_facts','SELECT,INSERT')"
        )
        for column in (
            "operation_id",
            "token_id",
            "estimated_gpu_seconds",
            "input_tokens",
            "output_tokens",
            "modality_usage",
        ):
            assert await connection.fetchval(
                "SELECT has_column_privilege('fs2_serve_runtime','fs2_usage_facts',$1,'SELECT')",
                column,
            )
        for column in ("tenant_id", "principal_id", "model_id", "outcome"):
            assert not await connection.fetchval(
                "SELECT has_column_privilege('fs2_serve_runtime','fs2_usage_facts',$1,'SELECT')",
                column,
            )
        assert await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_runtime','fs2_audit_events','SELECT,INSERT')"
        )
        assert await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_maintenance','fs2_operations','DELETE')"
        )
        assert await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_maintenance','fs2_usage_facts','DELETE')"
        )
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_maintenance','fs2_usage_facts','INSERT,UPDATE')"
        )
        assert await connection.fetchval("SELECT prosecdef FROM pg_proc WHERE proname='fs2_record_terminal_usage'")
        await connection.execute(
            "UPDATE fs2_operations SET completed_at=clock_timestamp()-interval '2 days' WHERE id=ANY($1::uuid[])",
            [first.id, second.id],
        )

    deleted = await postgres_store.delete_expired_rows(
        operation_retention_seconds=3600,
        token_retention_seconds=3600,
        audit_retention_seconds=2592000,
        usage_retention_seconds=7776000,
    )
    assert deleted["operations"] == 2 and deleted["usage"] == 0
    async with postgres_store.pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM fs2_operations") == 0
        assert await connection.fetchval("SELECT count(*) FROM fs2_usage_facts") == 2
        assert await connection.fetchval("SELECT sum(operations) FROM fs2_reporting_model_usage") == 2


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_every_non_worker_terminal_path_writes_one_durable_accounting_fact(
    postgres_store: PostgresStore,
) -> None:
    terminal: dict[str, UUID] = {}

    cancel_principal = await add_token(postgres_store)
    cancel = await append(postgres_store, cancel_principal, "accounting-cancel-0001")
    cancel_claim = await postgres_store.claim_operation("accounting-cancel-worker", lease_seconds=30)
    assert cancel_claim is not None and cancel_claim.id == cancel.id
    await postgres_store.cancel_operation(
        cancel.id,
        tenant_id=cancel_principal.tenant_id,
        actor=cancel_principal.principal_id,
    )
    await postgres_store.cancel_operation(
        cancel.id,
        tenant_id=cancel_principal.tenant_id,
        actor=cancel_principal.principal_id,
    )
    terminal["cancelled_by_caller"] = cancel.id

    revoke_principal = await add_token(postgres_store)
    revoke = await append(postgres_store, revoke_principal, "accounting-revoke-0002")
    revoke_claim = await postgres_store.claim_operation("accounting-revoke-worker", lease_seconds=30)
    assert revoke_claim is not None and revoke_claim.id == revoke.id
    await postgres_store.revoke_token(revoke_principal.token_id, actor="bootstrap-admin")
    await postgres_store.revoke_token(revoke_principal.token_id, actor="bootstrap-admin")
    terminal["token_revoked"] = revoke.id

    deadline_principal = await add_token(postgres_store)
    deadline = await append(postgres_store, deadline_principal, "accounting-deadline-0003")
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_operations SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            deadline.id,
        )
    assert await postgres_store.expire_deadline_operations() == 1
    assert await postgres_store.expire_deadline_operations() == 0
    terminal["deadline_exceeded"] = deadline.id

    payload_principal = await add_token(postgres_store)
    payload = await append(postgres_store, payload_principal, "accounting-payload-0004")
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_operations SET payload_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            payload.id,
        )
    assert await postgres_store.purge_expired_payloads() == 1
    assert await postgres_store.purge_expired_payloads() == 0
    terminal["payload_expired"] = payload.id

    release_principal = await add_token(postgres_store)
    release = await append(postgres_store, release_principal, "accounting-release-0005")
    first_release_claim = await postgres_store.claim_operation("accounting-release-worker-1", lease_seconds=30)
    assert first_release_claim is not None and first_release_claim.id == release.id
    await postgres_store.release_operation(
        release.id,
        worker_id=first_release_claim.worker_id,
        fencing_token=first_release_claim.fencing_token,
    )
    final_release_claim = await postgres_store.claim_operation("accounting-release-worker-2", lease_seconds=30)
    assert final_release_claim is not None and final_release_claim.id == release.id
    await postgres_store.release_operation(
        release.id,
        worker_id=final_release_claim.worker_id,
        fencing_token=final_release_claim.fencing_token,
    )
    terminal["attempts_exhausted"] = release.id

    stale_principal = await add_token(postgres_store)
    stale = await append(postgres_store, stale_principal, "accounting-stale-0006")
    for attempt in (1, 2):
        stale_claim = await postgres_store.claim_operation(f"accounting-stale-worker-{attempt}", lease_seconds=30)
        assert stale_claim is not None and stale_claim.id == stale.id and stale_claim.attempt == attempt
        async with postgres_store.pool.acquire() as connection:
            await connection.execute(
                "UPDATE fs2_operations SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
                stale.id,
            )
        assert await postgres_store.reap_stale_operations() == 1
    assert await postgres_store.reap_stale_operations() == 0
    terminal["lease_recovery_exhausted"] = stale.id

    inactive_principal = await add_token(postgres_store)
    inactive = await append(postgres_store, inactive_principal, "accounting-inactive-token-0007")
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_tokens SET expires_at=clock_timestamp()-interval '1 second' WHERE id=$1",
            inactive_principal.token_id,
        )
    assert await postgres_store.claim_operation("accounting-inactive-cleaner", lease_seconds=30) is None
    assert await postgres_store.claim_operation("accounting-inactive-cleaner-repeat", lease_seconds=30) is None
    terminal["token_inactive"] = inactive.id

    async with postgres_store.pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT f.operation_id,f.status::text AS status,f.outcome,f.estimated_gpu_seconds,o.error_code
            FROM fs2_usage_facts f JOIN fs2_operations o ON o.id=f.operation_id
            WHERE f.operation_id=ANY($1::uuid[]) ORDER BY f.operation_id
            """,
            list(terminal.values()),
        )
        duplicate_facts = await connection.fetchval(
            """
            SELECT count(*) FROM (
                SELECT operation_id FROM fs2_usage_facts
                WHERE operation_id=ANY($1::uuid[]) GROUP BY operation_id HAVING count(*)<>1
            ) duplicated
            """,
            list(terminal.values()),
        )

    assert len(rows) == len(terminal) == 7
    assert duplicate_facts == 0
    by_error = {row["error_code"]: row for row in rows}
    assert set(by_error) == set(terminal)
    assert by_error["cancelled_by_caller"]["status"] == "cancelled"
    assert by_error["cancelled_by_caller"]["outcome"] == "cancelled"
    assert by_error["token_revoked"]["status"] == "cancelled"
    assert by_error["token_revoked"]["outcome"] == "token_revoked"
    assert all(
        by_error[reason]["status"] == "expired" for reason in set(terminal) - {"cancelled_by_caller", "token_revoked"}
    )
    assert sum(row["estimated_gpu_seconds"] for row in rows) == 30

    accounting = await postgres_store.terminal_accounting()
    assert sum(row.operations for row in accounting) == 7
    assert sum(row.estimated_gpu_seconds for row in accounting) == 30


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_malformed_audit_jsonb_fails_closed_at_store_and_endpoint(
    postgres_store: PostgresStore, registry
) -> None:
    principal = await add_token(postgres_store)
    secret = "MALFORMED_AUDIT_DETAIL_MUST_NOT_ESCAPE"
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_audit_events SET detail=jsonb_build_array($1::text) WHERE token_id=$2",
            secret,
            principal.token_id,
        )
    with pytest.raises(RuntimeError, match="stored audit detail is not an object"):
        await postgres_store.list_audit(tenant_id=principal.tenant_id)

    app, _ = postgres_app(postgres_store, registry)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="https://inference.test.invalid", trust_env=False
        ) as client:
            response = await client.get(
                "/admin/v1/audit",
                params={"tenant_id": principal.tenant_id},
                headers={"authorization": f"Bearer {ADMIN_TOKEN}"},
            )
    assert response.status_code == 503
    assert secret not in response.text


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_audit_retention_is_bounded_independently(postgres_store: PostgresStore) -> None:
    principal = await add_token(postgres_store)
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_audit_events SET occurred_at=clock_timestamp()-interval '2 hours' WHERE token_id=$1",
            principal.token_id,
        )
    deleted = await postgres_store.delete_expired_rows(
        operation_retention_seconds=604800,
        token_retention_seconds=604800,
        audit_retention_seconds=3600,
        usage_retention_seconds=7776000,
    )
    assert deleted["audit"] == 1
    assert await postgres_store.list_audit(tenant_id=principal.tenant_id) == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_reporting_queries_are_bounded_paginated_and_payload_free(
    postgres_store: PostgresStore,
) -> None:
    principal = await add_token(postgres_store, max_concurrency=4)
    operations = [
        await append(postgres_store, principal, f"admin-reporting-safe-{ordinal:04d}") for ordinal in range(3)
    ]
    for operation in operations:
        await postgres_store.cancel_operation(
            operation.id,
            tenant_id=principal.tenant_id,
            actor=principal.principal_id,
        )
    now = datetime.now(UTC)
    query = AdminOperationQuery(
        from_at=now - timedelta(hours=1),
        to_at=now + timedelta(minutes=1),
        limit=2,
        tenant_id=principal.tenant_id,
        model_id="qwen3-8b",
        status=OperationStatus.CANCELLED,
    )
    page = await postgres_store.admin_list_operations(query)
    assert len(page) == 2
    assert page == sorted(page, key=lambda row: (row.accepted_at, row.id), reverse=True)
    assert await postgres_store.admin_get_operation(page[0].id) == page[0]
    assert set(page[0].model_dump()) == {
        "id",
        "tenant_id",
        "principal_id",
        "api_key_prefix",
        "model_id",
        "model_revision",
        "protocol",
        "operation",
        "status",
        "accepted_at",
        "activation_started_at",
        "ready_at",
        "started_at",
        "completed_at",
        "outcome",
        "semantic_outcome",
        "http_status",
        "error_code",
        "attempt",
        "max_attempts",
        "gpu_count",
        "preemptible",
        "estimated_gpu_seconds",
        "cold_start_seconds",
        "input_tokens",
        "output_tokens",
    }

    next_page = await postgres_store.admin_list_operations(
        query.model_copy(update={"after_at": page[-1].accepted_at, "after_id": page[-1].id})
    )
    assert len(next_page) == 1
    assert {row.id for row in page}.isdisjoint(row.id for row in next_page)

    activity = await postgres_store.admin_model_activity(("qwen3-8b",))
    assert len(activity) == 1 and activity[0].queued_operations == 0
    usage = await postgres_store.admin_usage_window(
        from_at=now - timedelta(hours=1),
        to_at=now + timedelta(minutes=1),
    )
    row = next(item for item in usage.rows if item.model_id == "qwen3-8b")
    assert row.terminal_operations == 3 and row.error_operations == 3
    assert row.input_tokens == row.output_tokens == row.token_reported_operations == 0
    assert 0 <= row.latency_p50_seconds <= row.latency_p95_seconds <= row.latency_p99_seconds
    assert usage.latency_p50_seconds is not None
    assert usage.latency_p95_seconds is not None
    assert usage.latency_p99_seconds is not None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scientific_batch_repository_is_durable_fenced_and_excluded_from_generic_claims(
    postgres_store: PostgresStore,
) -> None:
    principal = await add_token(postgres_store)
    operation = await postgres_store.append_operation(
        principal=principal,
        admission=AdmissionRequest(
            model_id="qwen3-8b",
            operation="design",
            protocol="scientific-batch-v1",
            idempotency_key="postgres-scientific-batch-0001",
            request_body=b'{"schema":"fs2-serve.nebius.ai/scientific-run-request/v1"}',
        ),
        model_revision="b968826d",
        reserved_gpu_seconds=0,
        max_attempts=1,
    )
    assert await postgres_store.claim_operation("generic-worker", lease_seconds=30) is None

    plan = ScientificBatchPlan(stages=(ScientificStagePlan(stage_id="design", max_attempts=2),))
    scheduling = SchedulingSnapshot(
        policy_revision=hashlib.sha256(b"postgres-scientific-policy").hexdigest(),
        captured_at=operation.accepted_at,
        service_class=ServiceClass.CUSTOMER_BATCH,
        tenant_queue="scientific",
        model_lane="qwen3-8b",
        workload_namespace="fs2-models",
        route_namespace="fs2-models",
        stages=(
            StageSchedulingDecision(
                stage_id="design",
                resource_class=ResourceClass.GPU,
                resolved_cluster_queue="inference-accelerators",
                resolved_local_queue="scientific",
                workload_priority_class="customer-batch",
                workload_priority_value=100,
                resolved_pool_preference=("h100-preemptible",),
                accelerator_resource_name="nvidia.com/gpu",
                accelerator_count=1,
                max_queue_seconds=None,
                max_execution_seconds=None,
                checkpoint_mode=CheckpointMode.RESTART,
                preemption_mode=PreemptionMode.RESTARTABLE,
            ),
        ),
    )
    batches = PostgresScientificBatchRepository(postgres_store.pool)
    input_artifact_id = uuid4()
    input_attempt_id = uuid4()
    input_digest = "sha256:" + "1" * 64
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO fs2_scientific_stage_attempts(
                attempt_id,operation_id,tenant_id,stage_id,shard_id,attempt_number,
                status,started_at,retention_expires_at
            ) VALUES($1,$2,$3,'input','-',1,'running',clock_timestamp(),clock_timestamp()+interval '1 day')
            """,
            input_attempt_id,
            operation.id,
            principal.tenant_id,
        )
        await connection.execute(
            """
            INSERT INTO fs2_scientific_artifacts(
                id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,digest,size_bytes,
                media_type,storage_key,access_profile,retention_expires_at
            ) VALUES($1,$2,$3,$4,'input','-','input',$5,1,'application/json',$6,'public',
                clock_timestamp()+interval '1 day')
            """,
            input_artifact_id,
            input_attempt_id,
            operation.id,
            principal.tenant_id,
            input_digest,
            f"scientific/v1/tenants/{principal.tenant_id}/operations/{operation.id}/stages/input/shards/-/"
            f"attempts/{input_attempt_id}/"
            f"input/sha256/{input_digest.removeprefix('sha256:')}",
        )
        assert await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_runtime','fs2_scientific_batches','SELECT,INSERT')"
        )
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_runtime','fs2_scientific_batches','UPDATE')"
        )
        assert await connection.fetchval(
            "SELECT has_column_privilege('fs2_serve_runtime','fs2_scientific_batches','status','UPDATE')"
        )
        assert not await connection.fetchval(
            "SELECT has_column_privilege('fs2_serve_runtime','fs2_scientific_batches','tenant_id','UPDATE')"
        )
        assert await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_runtime','fs2_scientific_batch_events','SELECT,INSERT')"
        )
        assert not await connection.fetchval(
            "SELECT has_table_privilege('fs2_serve_runtime','fs2_scientific_batch_events','UPDATE,DELETE')"
        )
    admitted = await batches.create(
        operation_id=operation.id,
        tenant_id=principal.tenant_id,
        model_id="qwen3-8b",
        variant_id="qwen3-8b-h100",
        input_artifact_id=input_artifact_id,
        plan=plan,
        scheduling=scheduling,
    )
    assert (
        await batches.create(
            operation_id=operation.id,
            tenant_id=principal.tenant_id,
            model_id="qwen3-8b",
            variant_id="qwen3-8b-h100",
            input_artifact_id=input_artifact_id,
            plan=plan,
            scheduling=scheduling,
        )
        == admitted
    )

    first_claim = await batches.claim_next(
        controller_id="controller-a",
        lease_seconds=30,
        now=datetime.now(UTC),
    )
    assert first_claim is not None and await batches.load(first_claim) == admitted
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_scientific_batches SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=$1",
            operation.id,
        )
    second_claim = await batches.claim_next(
        controller_id="controller-b",
        lease_seconds=30,
        now=datetime.now(UTC),
    )
    assert second_claim is not None and second_claim.fencing_token == first_claim.fencing_token + 1
    with pytest.raises(BatchFenceLostError):
        await batches.load(first_claim)
    await batches.release(second_claim)

    cluster = FakeScientificBatchCluster()
    controller = ScientificBatchController(
        repository=batches,
        cluster=cluster,
        controller_id="controller-b",
        namespace="fs2-models",
        lease_seconds=30,
    )
    assert await controller.reconcile_once() == operation.id
    running = await batches.get(operation.id, tenant_id=principal.tenant_id)
    assert running.status.value == "running"
    projected_running = await postgres_store.get_operation(operation.id, tenant_id=principal.tenant_id)
    assert projected_running.status is OperationStatus.RUNNING
    async with postgres_store.pool.acquire() as connection:
        operation_events = await connection.fetch(
            "SELECT event,status FROM fs2_operation_events WHERE operation_id=$1 ORDER BY id",
            operation.id,
        )
    assert [(row["event"], row["status"]) for row in operation_events][-1] == (
        "scientific_batch_running",
        "running",
    )
    events = await batches.list_events(operation.id, tenant_id=principal.tenant_id)
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)
    assert [event.draft.phase.value for event in events] == ["queued", "scheduling"]

    revision = running.revision
    cancellation = await batches.request_cancel(
        operation.id,
        tenant_id=principal.tenant_id,
        actor=principal.principal_id,
    )
    assert cancellation.cancel_requested is True and cancellation.revision == revision
    assert await controller.reconcile_once() == operation.id
    deleting = await batches.get(operation.id, tenant_id=principal.tenant_id)
    assert deleting.status.value == "running"
    deleting_attempt = deleting.stage("design").attempts[-1]
    assert deleting_attempt.deletion_requested is True
    assert deleting_attempt.resource_released is False
    assert await controller.reconcile_once() == operation.id
    terminal = await batches.get(operation.id, tenant_id=principal.tenant_id)
    assert terminal.status.value == "cancelled"
    projected = await postgres_store.get_operation(operation.id, tenant_id=principal.tenant_id)
    assert projected.status is OperationStatus.CANCELLED and projected.error_code == "cancelled"
    async with postgres_store.pool.acquire() as connection:
        operation_events = await connection.fetch(
            "SELECT event,status FROM fs2_operation_events WHERE operation_id=$1 ORDER BY id",
            operation.id,
        )
    assert [(row["event"], row["status"]) for row in operation_events][-1] == (
        "scientific_batch_cancelled",
        "cancelled",
    )
