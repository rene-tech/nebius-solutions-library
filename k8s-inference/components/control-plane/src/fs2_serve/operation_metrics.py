"""Read-only customer operation outcomes, separate from transport acceptance."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .models import StrictModel


class CustomerOperationMetric(StrictModel):
    tenant: str
    model: str
    protocol: str
    outcome: Literal["succeeded", "failed", "cancelled", "preempted", "expired"]
    workload_class: Literal["serving", "scientific"]
    error_class: Literal["none", "cancelled", "expired", "preempted", "retry_exhausted", "upstream", "unknown"]
    operations: int = Field(ge=0)
    recent_operations: int = Field(default=0, ge=0)


async def customer_operation_metrics(pool: Any) -> list[CustomerOperationMetric]:
    """Use exactly-once terminal facts; retained operation rows only enrich errors.

    A later operation-retention pass cannot erase terminal failure counts. Once
    its error detail is gone, that row becomes unknown rather than success.
    Caller payloads, arbitrary runtime errors, principals and key IDs are never
    labels. All replicas read the same rows: Prometheus queries must deduplicate.
    """

    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """SELECT f.tenant_id AS tenant,f.model_id AS model,f.protocol,
                f.status::text AS outcome,
                CASE WHEN f.protocol='scientific-batch-v1' THEN 'scientific' ELSE 'serving' END AS workload_class,
                CASE
                  WHEN f.status='succeeded' THEN 'none'
                  WHEN f.status='cancelled' THEN 'cancelled'
                  WHEN f.status='expired' THEN 'expired'
                  WHEN f.status='preempted' THEN 'preempted'
                  WHEN o.error_code IN ('retry_exhausted','lease_recovery_exhausted') THEN 'retry_exhausted'
                  WHEN o.error_code LIKE 'upstream_%' THEN 'upstream'
                  ELSE 'unknown'
                END AS error_class,
                count(*) AS operations,
                count(*) FILTER (WHERE f.occurred_at >= clock_timestamp()-interval '10 minutes') AS recent_operations
            FROM fs2_usage_facts f LEFT JOIN fs2_operations o ON o.id=f.operation_id
            GROUP BY 1,2,3,4,5,6 ORDER BY 1,2,3,4,5,6 LIMIT 65537"""
        )
    return [CustomerOperationMetric.model_validate(dict(row)) for row in rows]
