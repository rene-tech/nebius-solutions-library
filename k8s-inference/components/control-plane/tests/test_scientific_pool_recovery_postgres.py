"""Recovery budget and backoff reopened from a real disposable PostgreSQL DB."""

from dataclasses import replace
from datetime import timedelta

import pytest
import test_scientific_batch_postgres_state as postgres_fixtures
from scientific_batch_fakes import FakeScientificBatchCluster
from test_scientific_batch_postgres_state import TENANT, admit_batch, principal_of
from test_scientific_pool_recovery import pending

from fs2_serve.scientific_batch.models import AttemptOutcome, BatchStatus
from fs2_serve.scientific_batch.pool_recovery import POOL_UNAVAILABLE, PoolRecoveryScientificController
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository

store = postgres_fixtures.store


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_restart_preserves_recovery_cause_budget_cleanup_and_idempotency(store):
    principal = await principal_of(store)
    operation, repository = await admit_batch(store, principal, idempotency_key="pool-recovery-durable-0001")
    cluster = FakeScientificBatchCluster()
    state = await repository.get(operation, tenant_id=TENANT)
    clock = [state.scheduling.captured_at + timedelta(seconds=1)]
    controller = PoolRecoveryScientificController(
        repository=repository,
        cluster=cluster,
        controller_id="before-restart",
        namespace="fs2-models",
        clock=lambda: clock[0],
    )
    await controller.reconcile_once()
    state = await repository.get(operation, tenant_id=TENANT)
    first = state.stage("design").attempts[0]
    observed = pending(first)
    observed = replace(
        observed,
        scheduling_admission=replace(observed.scheduling_admission, admitted_at=clock[0]),
        pool_unavailable_since=clock[0] - timedelta(hours=1),
    )
    cluster.set_observation(first.workload, observed)
    clock[0] += timedelta(seconds=120)
    await controller.reconcile_once()
    # A new repository/controller instance reopens the durable result and timers.
    repository = PostgresScientificBatchRepository(store.pool)
    controller = PoolRecoveryScientificController(
        repository=repository,
        cluster=cluster,
        controller_id="after-restart",
        namespace="fs2-models",
        clock=lambda: clock[0],
    )
    state = await repository.get(operation, tenant_id=TENANT)
    assert state.stage("design").attempts[0].failure_code == POOL_UNAVAILABLE
    for _ in range(4):
        await controller.reconcile_once()
    assert len(cluster.apply_history) == 1
    assert (await repository.get(operation, tenant_id=TENANT)).stage("design").attempts[0].resource_released
    clock[0] += timedelta(seconds=15)
    for _ in range(2):
        await controller.reconcile_once()
    state = await repository.get(operation, tenant_id=TENANT)
    retry = state.stage("design").latest_attempt("main")
    assert retry.attempt_number == 2 and retry.attempt_id != first.attempt_id
    cluster.set_observation(retry.workload, replace(observed, ref=retry.workload, attempt_id=retry.attempt_id))
    for _ in range(8):
        await controller.reconcile_once()
    state = await repository.get(operation, tenant_id=TENANT)
    assert state.status is BatchStatus.FAILED
    assert len(state.stage("design").attempts) == 2
    assert all(
        attempt.resource_released and attempt.outcome is AttemptOutcome.FAILED
        for attempt in state.stage("design").attempts
    )
    operation_view = await store.get_operation(operation, tenant_id=TENANT)
    assert operation_view.error_code == POOL_UNAVAILABLE
    async with store.pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT id, idempotency_key, max_attempts FROM fs2_operations WHERE id=$1", operation
        )
        assert row["id"] == operation and row["idempotency_key"] == "pool-recovery-durable-0001"
        assert row["max_attempts"] == 1
    assert len(cluster.apply_history) == 2 and len(cluster.delete_history) == 2
