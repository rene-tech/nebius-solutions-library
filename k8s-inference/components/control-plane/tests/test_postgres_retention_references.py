"""Retention regression against PostgreSQL, including the maintenance role.

Use a disposable FS2_TEST_DATABASE_URL: the imported fixture resets its test DB.
No historical scientific data is eligible for deletion by these tests.
"""

import asyncio
import json
import os
from uuid import uuid4

import asyncpg
import pytest
import test_postgres_integration
from test_postgres_integration import add_token, append
from test_scientific_batch_postgres_state import durable_input_artifact

from fs2_serve.postgres import PostgresMaintenanceStore

postgres_store = test_postgres_integration.postgres_store

RETENTION = {
    "operation_retention_seconds": 3600,
    "token_retention_seconds": 3600,
    "audit_retention_seconds": 2592000,
    "usage_retention_seconds": 7776000,
}


async def _terminal(store, principal, key):
    operation = await append(store, principal, key)
    async with store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_operations SET status='failed',completed_at=clock_timestamp()-interval '2 days' WHERE id=$1",
            operation.id,
        )
    return operation


async def _reference(connection, kind, operation):
    if kind == "attempt":
        await connection.execute(
            """INSERT INTO fs2_scientific_stage_attempts(
                attempt_id,operation_id,tenant_id,stage_id,shard_id,attempt_number,status,
                started_at,completed_at,retention_expires_at
            ) VALUES($1,$2,$3,'input','-',1,'failed',clock_timestamp()-interval '5 days',
                clock_timestamp()-interval '4 days',clock_timestamp()-interval '3 days')""",
            uuid4(), operation.id, operation.tenant_id,
        )
    elif kind == "commit":
        manifest = {
            "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
            "manifest_id": f"{operation.id}:input",
            "entries": [],
        }
        await connection.execute(
            """INSERT INTO fs2_scientific_stage_commits(
                operation_id,stage_id,tenant_id,manifest_digest,validation_digest,semantic_valid,
                manifest,committed_at,validated_at
            ) VALUES($1,'input',$2,$3,$3,false,$4::jsonb,statement_timestamp(),statement_timestamp())""",
            operation.id, operation.tenant_id, "sha256:" + "a" * 64, json.dumps(manifest),
        )
    elif kind == "result":
        document = {
            "schema": "fs2-serve.nebius.ai/scientific-run-result/v1",
            "operation_id": str(operation.id),
            "terminal_status": "failed",
            "semantic_validation": {"status": "failed"},
            "attempts": [],
            "scheduling_snapshot": {},
            "execution_identity": {},
        }
        await connection.execute(
            """INSERT INTO fs2_scientific_run_results(
                operation_id,tenant_id,result_digest,terminal_status,semantic_validation_status,document,
                submitted_at,completed_at,committed_at,retention_expires_at
            ) VALUES($1,$2,$3,'failed','failed',$4::jsonb,statement_timestamp(),statement_timestamp(),
                statement_timestamp(),statement_timestamp()+interval '1 day')""",
            operation.id, operation.tenant_id, "sha256:" + "a" * 64, json.dumps(document),
        )
    elif kind == "event":
        await connection.execute(
            """INSERT INTO fs2_scientific_artifact_events(event_type,operation_id,tenant_id,manifest_digest)
                VALUES('result_committed',$1,$2,$3)""",
            operation.id, operation.tenant_id, "sha256:" + "a" * 64,
        )
    elif kind == "outbox":
        document = {
            "schema_version": "fs2-serve.nebius.ai/scientific-batch-state/v8",
            "operation_id": str(operation.id), "status": "queued", "revision": 0,
        }
        await connection.execute(
            "INSERT INTO fs2_scientific_admission_outbox(operation_id,payload) VALUES($1,$2::jsonb)",
            operation.id, json.dumps(document),
        )
    else:
        raise AssertionError(kind)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retention_preserves_references_under_real_maintenance_role(postgres_store):
    principal = await add_token(postgres_store, max_concurrency=16)
    referenced = []
    for kind in ("attempt", "commit", "result", "event", "outbox"):
        operation = await _terminal(postgres_store, principal, f"retention-reference-{kind}")
        async with postgres_store.pool.acquire() as connection:
            await _reference(connection, kind, operation)
        referenced.append(operation.id)
    parent = await _terminal(postgres_store, principal, "retention-active-child-parent")
    child = await append(postgres_store, principal, "retention-active-child")
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            "UPDATE fs2_operations SET parent_operation_id=$2,parent_attempt_id=$3 WHERE id=$1",
            child.id, parent.id, uuid4(),
        )
        # Reproduce the exact historical failure in a rolled-back transaction.
        with pytest.raises(asyncpg.ForeignKeyViolationError) as failure:
            async with connection.transaction():
                await connection.execute("DELETE FROM fs2_operations WHERE id=$1", referenced[0])
        assert failure.value.constraint_name == "fs2_scientific_stage_attempts_operation_id_fkey"
    unreferenced = await _terminal(postgres_store, principal, "retention-unreferenced")

    async def assume_role(connection):
        await connection.execute("SET ROLE fs2_serve_maintenance")

    pool = await asyncpg.create_pool(
        os.environ["FS2_TEST_DATABASE_URL"], min_size=3, max_size=6, init=assume_role,
    )
    assert pool is not None
    try:
        maintenance = PostgresMaintenanceStore(pool)
        outcomes = await asyncio.gather(*(maintenance.delete_expired_rows(**RETENTION) for _ in range(3)))
        assert sum(item["operations"] for item in outcomes) == 1
        for _ in range(3):
            assert (await maintenance.delete_expired_rows(**RETENTION))["operations"] == 0
        async with pool.acquire() as connection:
            assert await connection.fetchval("SELECT count(operation_id) FROM fs2_scientific_stage_attempts") == 1
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.fetch("SELECT document FROM fs2_scientific_run_results")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute("DELETE FROM fs2_scientific_stage_attempts")
    finally:
        await pool.close()
    async with postgres_store.pool.acquire() as connection:
        remaining = {row["id"] for row in await connection.fetch("SELECT id FROM fs2_operations")}
        assert remaining == set(referenced) | {parent.id, child.id}
        assert unreferenced.id not in remaining
        for table in (
            "fs2_scientific_stage_attempts", "fs2_scientific_stage_commits", "fs2_scientific_run_results",
            "fs2_scientific_artifact_events", "fs2_scientific_admission_outbox",
        ):
            assert await connection.fetchval(f"SELECT count(*) FROM {table}") == 1  # noqa: S608 - fixed table inventory


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retention_skips_parent_locked_by_concurrent_reference_then_preserves_it(postgres_store):
    principal = await add_token(postgres_store)
    operation = await _terminal(postgres_store, principal, "retention-concurrent-reference")
    async with postgres_store.pool.acquire() as connection, connection.transaction():
        await _reference(connection, "attempt", operation)
        # The in-flight FK holds a key-share lock; SKIP LOCKED cannot delete it.
        result = await asyncio.wait_for(postgres_store.delete_expired_rows(**RETENTION), timeout=2)
        assert result["operations"] == 0
    assert (await postgres_store.delete_expired_rows(**RETENTION))["operations"] == 0
    assert (await postgres_store.get_operation(operation.id)).id == operation.id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retention_preserves_cascading_batch_history_and_actual_input_artifact(postgres_store):
    principal = await add_token(postgres_store)
    input_owner = await _terminal(postgres_store, principal, "retention-batch-input-owner")
    artifact_id, batch_id, workload_id = uuid4(), uuid4(), uuid4()
    await durable_input_artifact(
        postgres_store, input_owner.id, artifact_id=artifact_id, tenant_id=principal.tenant_id,
    )
    operation = await _terminal(postgres_store, principal, "retention-batch-history")
    state = {
        "schema_version": "fs2-serve.nebius.ai/scientific-batch-state/v8",
        "operation_id": str(operation.id), "batch_id": str(batch_id), "workload_id": str(workload_id),
        "tenant_id": principal.tenant_id, "model_id": "qwen3-8b", "variant_id": "test-batch",
        "input_artifact_id": str(artifact_id), "status": "failed", "revision": 0, "cancel_requested": False,
    }
    async with postgres_store.pool.acquire() as connection:
        await connection.execute(
            """INSERT INTO fs2_scientific_batches(
                operation_id,batch_id,workload_id,tenant_id,model_id,variant_id,input_artifact_id,
                scheduling_digest,status,state
            ) VALUES($1,$2,$3,$4,'qwen3-8b','test-batch',$5,$6,'failed',$7::jsonb)""",
            operation.id, batch_id, workload_id, principal.tenant_id, artifact_id,
            "sha256:" + "a" * 64, json.dumps(state),
        )
    assert (await postgres_store.delete_expired_rows(**RETENTION))["operations"] == 0
    async with postgres_store.pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM fs2_operations") == 2
        assert await connection.fetchval("SELECT count(*) FROM fs2_scientific_batches") == 1
        assert await connection.fetchval("SELECT id FROM fs2_scientific_artifacts") == artifact_id
