"""Actual SQL coverage, using only the isolated test database fixture."""

from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from test_users_apps_postgres import CONTEXT, NOW, database, operation, token  # noqa: F401

from fs2_serve.request_telemetry import PostgresRequestTelemetryStore, RequestTelemetry

pytestmark = pytest.mark.postgres


@pytest_asyncio.fixture
async def telemetry_database(database):  # noqa: F811
    await database.pool.execute("TRUNCATE fs2_request_telemetry")
    try:
        yield database
    finally:
        await database.pool.execute("TRUNCATE fs2_request_telemetry")


def observation(operation_id, owner, **updates):
    values = dict(
        request_id=uuid4(),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=2),
        endpoint="/v1/operations/example",
        method="GET",
        transport="http",
        http_status=200,
        response_duration_seconds=2,
        request_bytes=0,
        response_bytes=12,
        request_bytes_observed=0,
        response_bytes_observed=12,
        request_complete=True,
        response_complete=True,
        disconnected=False,
        operation_id=operation_id,
        model_id=None,
        token_id=owner.id,
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
    )
    values.update(updates)
    return RequestTelemetry(**values)


async def test_sql_counts_replay_and_polling_without_duplicating_operations(telemetry_database):
    database = telemetry_database  # noqa: F811
    store = PostgresRequestTelemetryStore(database.pool)
    owner = await token(database)
    operation_id = await operation(database, owner)
    observations = [observation(operation_id, owner) for _ in range(3)]
    for row in observations:
        await store.record(row)
    await store.record(observations[0])  # Same evidence delivery is idempotent; separate requests are not.
    rows = await store.for_operation(operation_id, "tenant-a")
    assert len(rows) == 3 and {row.model_id for row in rows} == {"qwen3-8b"}
    assert not await store.for_operation(operation_id, "tenant-b")
    usage = await store.usage("qwen3-8b", CONTEXT.from_at, CONTEXT.to_at, "tenant-a")
    assert usage.request_count == 3 and usage.response_bytes == 36
    assert usage.request_bytes == 0 and usage.request_bytes_known_count == 3
    assert usage.average_response_duration_seconds == 2
    assert usage.status_classes == {"2xx": 3}
    assert len(usage.requests_over_time) == 60
    observed = [bucket for bucket in usage.requests_over_time if bucket.request_count is not None]
    assert len(observed) == 1 and observed[0].request_count == 3
    assert observed[0].status_classes == {"2xx": 3, "3xx": 0, "4xx": 0, "5xx": 0, "unknown": 0}
    assert usage.requests_over_time[0].request_count is None
    assert await database.pool.fetchval("SELECT count(*) FROM fs2_operations WHERE id=$1", operation_id) == 1
    await database.pool.execute("DELETE FROM fs2_request_telemetry WHERE operation_id=$1", operation_id)


async def test_sql_incomplete_coverage_tool_errors_and_tenant_window_boundaries(telemetry_database):
    database = telemetry_database  # noqa: F811
    store = PostgresRequestTelemetryStore(database.pool)
    owner = await token(database)
    operation_id = await operation(database, owner)
    complete = observation(operation_id, owner, transport="mcp", mcp_tool="invoke_model", mcp_is_error=True)
    partial = observation(operation_id, owner, response_complete=False, response_bytes=None, http_status=503)
    foreign = observation(operation_id, owner, tenant_id="tenant-b")
    old = observation(operation_id, owner, started_at=CONTEXT.from_at - timedelta(seconds=1))
    for row in (complete, partial, foreign, old):
        await store.record(row)
    usage = await store.usage("qwen3-8b", CONTEXT.from_at, CONTEXT.to_at, "tenant-a")
    assert usage.request_count == 2 and usage.completed_response_count == usage.incomplete_response_count == 1
    assert usage.successful_http_count == usage.failed_http_count == usage.mcp_tool_error_count == 1
    assert usage.response_bytes is None and usage.response_bytes_known_count == 1
    assert usage.request_bytes == 0
    assert usage.status_classes == {"2xx": 1, "5xx": 1}
    assert sum(bucket.request_count or 0 for bucket in usage.requests_over_time) == 2
    foreign_row = (await store.for_operation(operation_id, "tenant-b"))[0]
    assert foreign_row.model_id is None  # No cross-tenant operation metadata attribution.
    empty = await store.usage("historical-without-observations", CONTEXT.from_at, CONTEXT.to_at, "tenant-a")
    assert empty.request_count == 0 and empty.first_observed_at is None
    assert empty.request_bytes is None and empty.response_bytes is None
    assert empty.average_response_duration_seconds is None
    assert empty.status_classes == {}
    assert all(bucket.request_count is None and bucket.status_classes is None for bucket in empty.requests_over_time)
    await database.pool.execute("DELETE FROM fs2_request_telemetry WHERE operation_id=$1", operation_id)
