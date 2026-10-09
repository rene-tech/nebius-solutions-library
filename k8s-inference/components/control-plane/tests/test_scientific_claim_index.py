"""Real PostgreSQL plans must avoid scanning completed large batch documents.

Temporary SQL-shape fixtures exercise the unmodified production claim query,
not a native simulation or a claim that these simplified rows are valid runs.
Existing controller/policy integration tests cover full durable state semantics.
"""

import ast
import inspect
import json
import textwrap
from pathlib import Path

import pytest
from test_scientific_model_policy_postgres import store as store  # noqa: F401

from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository

MIGRATION = Path(__file__).parents[1] / "migrations/0038_scientific_claimable_index.sql"
INDEX = "fs2_scientific_batches_pending_claim_idx"


def claim_sql():
    literals = [
        node.value
        for node in ast.walk(
            ast.parse(textwrap.dedent(inspect.getsource(PostgresScientificBatchRepository.claim_next)))
        )
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "WITH candidate AS" in node.value
    ]
    assert len(literals) == 1
    return literals[0]


def index_names(value):
    if isinstance(value, dict):
        if "Index Name" in value:
            yield value["Index Name"]
        for child in value.values():
            yield from index_names(child)
    elif isinstance(value, list):
        for child in value:
            yield from index_names(child)


@pytest.mark.postgres
async def test_claim_index_excludes_terminal_history_and_keeps_exact_claim_semantics(store):  # noqa: F811
    async with store.pool.acquire() as connection, connection.transaction():
        # TEMP names shadow only this disposable connection's public tables.
        # There are no customer payloads or production writes in this test.
        await connection.execute("""
            CREATE TEMP TABLE fs2_scientific_batches (
                operation_id uuid PRIMARY KEY, status text, state jsonb,
                lease_expires_at timestamptz, cancel_requested boolean DEFAULT false,
                model_id text DEFAULT 'rfdiffusion', tenant_id text DEFAULT 'tenant-oncology',
                controller_id text, fencing_token bigint DEFAULT 0, updated_at timestamptz
            ) ON COMMIT DROP;
            CREATE TEMP TABLE fs2_operations (
                id uuid PRIMARY KEY, status text, accepted_at timestamptz DEFAULT clock_timestamp()
            ) ON COMMIT DROP;
            INSERT INTO fs2_scientific_batches(operation_id,status,state)
            SELECT md5(i::text)::uuid, 'succeeded',
                jsonb_build_object('result_published',true,'padding',repeat(md5(i::text),1024))
            FROM generate_series(1,1200) i;
            INSERT INTO fs2_operations(id,status)
            SELECT operation_id,status FROM fs2_scientific_batches;
            ANALYZE fs2_scientific_batches;
            ANALYZE fs2_operations;
        """)
        sql = claim_sql()
        before = json.loads(await connection.fetchval("EXPLAIN (FORMAT JSON) " + sql, "probe", 30.0))
        assert INDEX not in set(index_names(before))
        await connection.execute(MIGRATION.read_text())
        await connection.execute("ANALYZE fs2_scientific_batches")
        after = json.loads(await connection.fetchval("EXPLAIN (FORMAT JSON) " + sql, "probe", 30.0))
        assert INDEX in set(index_names(after)), after
        assert await connection.fetchrow(sql, "probe", 30.0) is None

        # Terminal unpublished remains claimable; a concurrent controller must
        # respect its lease. Published terminal work must disappear from the
        # index again, without changing any claim/fencing code.
        operation = await connection.fetchval("SELECT id FROM fs2_operations ORDER BY id LIMIT 1")
        await connection.execute(
            "UPDATE fs2_scientific_batches SET state=jsonb_set(state,'{result_published}','false') "
            "WHERE operation_id=$1",
            operation,
        )
        claim = await connection.fetchrow(sql, "first", 30.0)
        assert claim["operation_id"] == operation and claim["fencing_token"] == 1
        assert await connection.fetchrow(sql, "second", 30.0) is None
        await connection.execute(
            "UPDATE fs2_scientific_batches SET lease_expires_at=NULL,controller_id=NULL, "
            "state=jsonb_set(state,'{result_published}','true') WHERE operation_id=$1",
            operation,
        )
        assert await connection.fetchrow(sql, "second", 30.0) is None
        await connection.execute(
            "UPDATE fs2_scientific_batches SET status='running' WHERE operation_id=$1",
            operation,
        )
        # The public Operation fence is still enforced even with the new index.
        assert await connection.fetchrow(sql, "second", 30.0) is None
        await connection.execute("UPDATE fs2_operations SET status='running' WHERE id=$1", operation)
        claim = await connection.fetchrow(sql, "second", 30.0)
        assert claim["operation_id"] == operation and claim["fencing_token"] == 2


@pytest.mark.postgres
async def test_claim_index_migration_can_follow_online_prebuild(store):  # noqa: F811
    async with store.pool.acquire() as connection:
        before = await connection.fetchrow(
            "SELECT indexrelid,indisvalid,indisready,pg_get_indexdef(indexrelid) AS definition "
            "FROM pg_index WHERE indexrelid=to_regclass($1)",
            INDEX,
        )
        assert before is not None and before["indisvalid"] and before["indisready"]
        await connection.execute(MIGRATION.read_text())
        after = await connection.fetchrow(
            "SELECT indexrelid,indisvalid,indisready,pg_get_indexdef(indexrelid) AS definition "
            "FROM pg_index WHERE indexrelid=to_regclass($1)",
            INDEX,
        )
        assert before == after
