"""Bounded PostgreSQL snapshot export; SELECT only, repeatable-read/read-only.

Run as ``python -m fs2_serve.usage_reconciliation --help``. No migrations, ledger
reconciliation, token updates, secrets, payloads or billing are performed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from .lifecycle import LifecycleWorkloadSummary, _rollup_from_row, _subject_from_row
from .usage_accounting import reconciliation_report


async def export_snapshot(
    connection: Any,
    *,
    tenant_id: str,
    from_at: datetime,
    to_at: datetime,
    principal_id: str | None = None,
    model_id: str | None = None,
    api_key_id: UUID | None = None,
    limit: int = 10_000,
) -> dict[str, Any]:
    if from_at.tzinfo is None or to_at.tzinfo is None or from_at >= to_at:
        raise ValueError("from/to must be increasing timezone-aware timestamps")
    if not tenant_id or not 1 <= limit <= 10_000:
        raise ValueError("tenant is required and limit must be between 1 and 10000")
    async with connection.transaction(isolation="repeatable_read", readonly=True):
        snapshot_at = await connection.fetchval("SELECT transaction_timestamp()")
        operations = await connection.fetch(
            """SELECT id,protocol,status,model_id,principal_id,token_id,accepted_at,
                      estimated_gpu_seconds AS conservative_attempted_gpu_seconds
               FROM fs2_operations WHERE tenant_id=$1 AND accepted_at >= $2 AND accepted_at < $3
                 AND ($4::text IS NULL OR principal_id=$4) AND ($5::text IS NULL OR model_id=$5)
                 AND ($6::uuid IS NULL OR token_id=$6) AND protocol<>'scientific-artifact-upload-v1'
               ORDER BY accepted_at,id LIMIT $7""",
            tenant_id,
            from_at,
            to_at,
            principal_id,
            model_id,
            api_key_id,
            limit + 1,
        )
        if len(operations) > limit:
            raise ValueError("operation export exceeds limit; narrow the cohort (no partial total emitted)")
        operation_ids = [row["id"] for row in operations]
        rows = await connection.fetch(
            """SELECT to_jsonb(s) AS subject, to_jsonb(r) AS rollup
               FROM fs2_telemetry_subjects s
               LEFT JOIN fs2_reporting_lifecycle_latest r USING(subject_id)
               WHERE s.tenant_id=$1 AND s.operation_id=ANY($2::uuid[])
               ORDER BY s.accepted_at,s.subject_id LIMIT $3""",
            tenant_id,
            operation_ids,
            limit + 1,
        )
        if len(rows) > limit:
            raise ValueError("attempt export exceeds limit; narrow the cohort (no partial total emitted)")
        snapshots = await connection.fetch(
            """SELECT id,tenant_id,principal_id,rotation_parent_id,rotated_at,
                      gpu_seconds_budget AS admission_budget_limit_gpu_seconds,
                      gpu_seconds_used AS admission_budget_consumed_gpu_seconds,
                      gpu_seconds_reserved AS admission_budget_reserved_gpu_seconds
               FROM fs2_tokens WHERE tenant_id=$1 AND ($2::text IS NULL OR principal_id=$2)
                 AND ($3::uuid IS NULL OR id=$3) ORDER BY created_at,id LIMIT $4""",
            tenant_id,
            principal_id,
            api_key_id,
            limit + 1,
        )
        if len(snapshots) > limit:
            raise ValueError("key snapshot exceeds limit; narrow principal/key scope")
    workloads = []
    for row in rows:
        subject = json.loads(row["subject"]) if isinstance(row["subject"], str) else row["subject"]
        rollup = json.loads(row["rollup"]) if isinstance(row["rollup"], str) else row["rollup"]
        workloads.append(
            LifecycleWorkloadSummary(
                subject=_subject_from_row(subject),
                rollup=_rollup_from_row(rollup) if rollup else None,
            )
        )
    report = reconciliation_report(
        workloads,
        expected_operation_ids={row["id"] for row in operations if row["protocol"] == "scientific-batch-v1"},
        admission_snapshots=[dict(row) for row in snapshots],
    )
    represented = {value.subject.operation_id for value in workloads}
    report.update(
        snapshot_at=snapshot_at,
        tenant_id=tenant_id,
        principal_id=principal_id,
        model_id=model_id,
        api_key_id=api_key_id,
        from_at=from_at,
        to_at=to_at,
        operations=[dict(row) for row in operations],
        operations_without_lifecycle=[str(value) for value in operation_ids if value not in represented],
    )
    return report


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    # DSN comes only from the selected environment variable, never an argv log.
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        raise ValueError("read-only database DSN environment variable is not configured")
    connection = await asyncpg.connect(dsn, command_timeout=30)
    try:
        return await export_snapshot(
            connection,
            tenant_id=args.tenant,
            from_at=datetime.fromisoformat(args.from_at),
            to_at=datetime.fromisoformat(args.to_at),
            principal_id=args.principal,
            model_id=args.model,
            api_key_id=UUID(args.key) if args.key else None,
            limit=args.limit,
        )
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--from", dest="from_at", required=True)
    parser.add_argument("--to", dest="to_at", required=True)
    parser.add_argument("--principal")
    parser.add_argument("--model")
    parser.add_argument("--key")
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--dsn-env", default="FS2_USAGE_DATABASE_URL")
    args = parser.parse_args()
    try:
        report = asyncio.run(_run(args))
    except ValueError as exc:
        parser.exit(2, f"Reconciliation refused: {exc}\n")
    except Exception:
        # Driver errors can carry a DSN; do not print their contents.
        parser.exit(1, "Reconciliation unavailable: database read failed; no partial report emitted.\n")
    print(json.dumps(report, default=str, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
