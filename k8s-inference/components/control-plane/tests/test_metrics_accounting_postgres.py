"""Real PostgreSQL settings/cancellation and aggregate equivalence, isolated DB only."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from test_request_telemetry_postgres import observation
from test_users_apps_postgres import NOW, database, token  # noqa: F401

from fs2_serve.metrics_accounting import historical_reporting_connection
from fs2_serve.request_telemetry import PostgresRequestTelemetryStore

pytestmark = pytest.mark.postgres


async def settings(connection):
    return dict(
        await connection.fetchrow(
            "SELECT current_setting('max_parallel_workers_per_gather') AS parallel_workers,"
            "current_setting('statement_timeout') AS statement_timeout,"
            "current_setting('transaction_read_only') AS read_only"
        )
    )


async def test_historical_transaction_bounds_and_cancellation_do_not_change_pool_defaults(database):  # noqa: F811
    async with database.pool.acquire() as connection:
        original = await settings(connection)
    async with historical_reporting_connection(database.pool) as connection:
        assert await settings(connection) == {"parallel_workers": "0", "statement_timeout": "3s", "read_only": "on"}
    async with database.pool.acquire() as connection:
        assert await settings(connection) == original

    entered = asyncio.Event()

    async def cancelled_query():
        async with historical_reporting_connection(database.pool) as connection:
            entered.set()
            await connection.execute("SELECT pg_sleep(10)")

    task = asyncio.create_task(cancelled_query())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with database.pool.acquire() as connection:
        assert await settings(connection) == original
        assert await connection.fetchval("SELECT 1") == 1


async def test_historical_semantics_match_full_ledger_and_preserve_exactly_once_rows(database):  # noqa: F811
    owner = await token(database)
    repository = PostgresRequestTelemetryStore(database.pool)
    rows = [
        observation(
            None,
            owner,
            model_id="gromacs",
            transport="mcp",
            mcp_tool="run_gromacs",
            semantic_outcome="succeeded",
            admission_stage="admitted",
        ),
        observation(
            None,
            owner,
            model_id="gromacs",
            transport="mcp",
            mcp_tool="run_gromacs",
            semantic_outcome="failed",
            admission_stage="pre_admission",
            mcp_is_error=True,
        ),
        observation(None, owner),
    ]
    for row in rows:
        await repository.record(row)
    await repository.record(rows[0])
    # A much older row is still historical truth, not dropped for a cheap lookback.
    await repository.record(
        rows[0].model_copy(
            update={
                "request_id": uuid4(),
                "started_at": NOW - timedelta(days=90),
                "completed_at": NOW - timedelta(days=90) + timedelta(seconds=2),
            }
        )
    )
    result = await repository.semantic_metric_rows()
    assert sum(row.exchanges for row in result) == 4
    by_outcome = {(row.model_id, row.semantic_outcome): row.exchanges for row in result}
    assert by_outcome == {("gromacs", "succeeded"): 2, ("gromacs", "failed"): 1, ("unattributed", "unknown"): 1}
