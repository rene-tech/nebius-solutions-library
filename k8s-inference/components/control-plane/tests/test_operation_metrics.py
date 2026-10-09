from __future__ import annotations

import os
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest
from prometheus_client.parser import text_string_to_metric_families

from fs2_serve.operation_metrics import CustomerOperationMetric, customer_operation_metrics
from fs2_serve.telemetry import Metrics


def test_terminal_failure_is_not_erased_by_http_success_or_a_repeat_projection():
    metrics = Metrics([])
    row = CustomerOperationMetric(
        tenant="stockholm",
        model="openfold2",
        protocol="native",
        outcome="failed",
        workload_class="serving",
        error_class="upstream",
        operations=1,
        recent_operations=1,
    )
    metrics.set_customer_operations([row])
    metrics.set_customer_operations([row])
    samples = [
        sample
        for family in text_string_to_metric_families(metrics.render().decode())
        for sample in family.samples
        if sample.name == "fs2_serve_customer_operations_last_10m"
    ]
    assert len(samples) == 1 and samples[0].value == 1
    assert samples[0].labels["outcome"] == "failed"
    assert not ({"http_status", "operation_id", "principal", "token_id"} & samples[0].labels.keys())
    metrics.set_customer_operations([])
    assert 'workload_class="serving"} 0.0' in metrics.render().decode()


@pytest.mark.postgres
async def test_real_sql_preserves_terminal_failure_after_operation_detail_is_gone():
    url = os.environ.get("FS2_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FS2_TEST_DATABASE_URL is not set")
    connection = await asyncpg.connect(url)
    try:
        # Connection-private fixtures: neither cloud data nor another test's
        # tables/roles are altered. Exercise the exact production aggregation.
        await connection.execute(
            """CREATE TEMP TABLE fs2_usage_facts (
                operation_id uuid,tenant_id text,model_id text,protocol text,status text,occurred_at timestamptz);
            CREATE TEMP TABLE fs2_operations (id uuid,error_code text,started_at timestamptz);"""
        )
        first, retained = uuid4(), uuid4()
        await connection.execute(
            """INSERT INTO fs2_usage_facts VALUES
                ($1,'stockholm','openfold2','native','failed',clock_timestamp()),
                ($2,'stockholm','openfold2','native','failed',clock_timestamp()-interval '1 hour');
            """,
            first,
            retained,
        )
        await connection.execute("INSERT INTO fs2_operations VALUES ($1,'upstream_http_422',clock_timestamp())", first)

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield connection

        rows = await customer_operation_metrics(Pool())
        assert sum(row.operations for row in rows) == 2
        assert sum(row.recent_operations for row in rows) == 1
        assert {row.error_class for row in rows} == {"upstream", "unknown"}
        assert {row.outcome for row in rows} == {"failed"}
        assert sum(row.recent_long_running_failures for row in rows) == 0
        await connection.execute(
            "UPDATE fs2_usage_facts SET protocol='scientific-batch-v1' WHERE operation_id=$1", first
        )
        await connection.execute("UPDATE fs2_operations SET started_at=clock_timestamp()-interval '6 minutes'")
        assert sum(row.recent_long_running_failures for row in await customer_operation_metrics(Pool())) == 1
        await connection.execute("UPDATE fs2_usage_facts SET status='cancelled' WHERE operation_id=$1", first)
        assert sum(row.recent_long_running_failures for row in await customer_operation_metrics(Pool())) == 0
    finally:
        await connection.close()


def test_long_job_failure_gauge_deduplicates_and_clears_with_shared_projection():
    metrics = Metrics([])
    row = CustomerOperationMetric(
        tenant="customer",
        model="gromacs",
        protocol="scientific-batch-v1",
        outcome="failed",
        workload_class="scientific",
        error_class="unknown",
        operations=1,
        recent_operations=1,
        recent_long_running_failures=1,
    )
    for _ in range(2):
        metrics.set_customer_operations([row])

    def samples():
        return [
            s
            for f in text_string_to_metric_families(metrics.render().decode())
            for s in f.samples
            if s.name == "fs2_serve_customer_long_running_failures_last_10m"
        ]

    assert len(samples()) == 1 and samples()[0].value == 1
    assert "operation_id" not in samples()[0].labels
    metrics.set_customer_operations([])
    assert samples()[0].value == 0
