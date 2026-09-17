"""Durable owner settings and windowed usage across all of an owner's keys."""

from __future__ import annotations

from datetime import timedelta
from math import ceil
from typing import Any, Protocol

from .admin_models import AdminContext, AdminMeasurement, AdminValueState
from .models import OperationStatus, TokenView
from .store import ConflictError
from .usage_accounting import lifecycle_usage_from_counts
from .user_models import InferenceUser, UserUsage, UserUsagePoint, owner_id


def measurement(value: float | None, unit: str, *, reason: str | None = None) -> AdminMeasurement:
    return AdminMeasurement(
        value=value,
        unit=unit,
        state=AdminValueState.UNAVAILABLE if value is None else AdminValueState.AVAILABLE,
        source="postgres",
        reason=reason,
    )


def usage_from_counts(row: dict[str, Any]) -> UserUsage:
    count = int(row.get("requests") or 0)
    terminal = int(row.get("terminal") or 0)
    coverage = int(row.get("token_coverage") or 0)
    accounting = lifecycle_usage_from_counts(row)

    def tokens(name: str) -> AdminMeasurement:
        if terminal == 0 or coverage != terminal:
            return measurement(None, "tokens", reason="Not all terminal operations reported token usage.")
        return measurement(float(row.get(name) or 0), "tokens")

    return UserUsage(
        requests=count,
        succeeded=int(row.get("succeeded") or 0),
        failed=int(row.get("failed") or 0),
        cancelled=int(row.get("cancelled") or 0),
        pending=int(row.get("pending") or 0),
        running=int(row.get("running") or 0),
        scientific_requests=int(row.get("scientific_requests") or 0),
        public_exchanges=int(row.get("public_exchanges") or 0),
        semantic_succeeded=int(row.get("semantic_succeeded") or 0),
        semantic_accepted=int(row.get("semantic_accepted") or 0),
        semantic_failed=int(row.get("semantic_failed") or 0),
        semantic_cancelled=int(row.get("semantic_cancelled") or 0),
        semantic_timed_out=int(row.get("semantic_timed_out") or 0),
        semantic_unknown=int(row.get("semantic_unknown") or 0),
        pre_admission_failed=int(row.get("pre_admission_failed") or 0),
        last_request_at=row.get("last_request_at"),
        scheduler_occupied_gpu_seconds=accounting.occupied,
        active_gpu_seconds=accounting.active,
        occupied_idle_gpu_seconds=accounting.classified_idle,
        lifecycle_accounting=accounting,
        input_tokens=tokens("input_tokens"),
        output_tokens=tokens("output_tokens"),
    )


class UserRepository(Protocol):
    async def keys(self, tenant_id: str, principal_id: str) -> list[TokenView]: ...
    async def list(self, tenant_id: str | None) -> list[InferenceUser]: ...
    async def configured(self, tenant_id: str, principal_id: str) -> InferenceUser | None: ...
    async def save(self, user: InferenceUser, *, create: bool = False) -> InferenceUser: ...
    async def usage(self, tenant_id: str, principal_id: str, context: AdminContext) -> UserUsage: ...


class PostgresUserRepository:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    @staticmethod
    def _user(row: Any) -> InferenceUser:
        data = dict(row)
        data.setdefault("id", owner_id(data["tenant_id"], data["principal_id"]))
        data.setdefault("source", "configured")
        return InferenceUser.model_validate(data)

    async def configured(self, tenant_id: str, principal_id: str) -> InferenceUser | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM fs2_inference_users WHERE tenant_id=$1 AND principal_id=$2", tenant_id, principal_id
        )
        return self._user(row) if row else None

    async def keys(self, tenant_id: str, principal_id: str) -> list[TokenView]:
        from .postgres import PostgresStore

        rows = await self.pool.fetch(
            "SELECT * FROM fs2_tokens WHERE tenant_id=$1 AND principal_id=$2 ORDER BY created_at DESC",
            tenant_id,
            principal_id,
        )
        return [PostgresStore._token(row) for row in rows]

    async def list(self, tenant_id: str | None) -> list[InferenceUser]:
        rows = await self.pool.fetch(
            """
            WITH owners AS (
                SELECT tenant_id, principal_id, min(created_at) AS first_seen FROM fs2_tokens
                WHERE ($1::text IS NULL OR tenant_id=$1) GROUP BY tenant_id,principal_id
                UNION ALL
                SELECT tenant_id, principal_id, min(accepted_at) FROM fs2_operations
                WHERE ($1::text IS NULL OR tenant_id=$1) GROUP BY tenant_id,principal_id
            ), discovered AS (
                SELECT tenant_id,principal_id,min(first_seen) AS first_seen FROM owners GROUP BY tenant_id,principal_id
            )
            SELECT d.tenant_id,d.principal_id,d.first_seen FROM discovered d
            WHERE NOT EXISTS (SELECT 1 FROM fs2_inference_users u
                WHERE u.tenant_id=d.tenant_id AND u.principal_id=d.principal_id)
            """,
            tenant_id,
        )
        configured = await self.pool.fetch(
            "SELECT * FROM fs2_inference_users WHERE ($1::text IS NULL OR tenant_id=$1)", tenant_id
        )
        users = [self._user(row) for row in configured]
        users.extend(
            InferenceUser(
                id=owner_id(row["tenant_id"], row["principal_id"]),
                tenant_id=row["tenant_id"],
                principal_id=row["principal_id"],
                display_name=row["principal_id"],
                source="existing-key-owner",
                created_at=row["first_seen"],
                updated_at=row["first_seen"],
            )
            for row in rows
        )
        return sorted(users, key=lambda user: (user.tenant_id, user.display_name, str(user.id)))

    async def save(self, user: InferenceUser, *, create: bool = False) -> InferenceUser:
        import asyncpg

        conflict = (
            ""
            if create
            else """ON CONFLICT (tenant_id,principal_id) DO UPDATE SET
            display_name=EXCLUDED.display_name,kind=EXCLUDED.kind,team=EXCLUDED.team,
            enabled=EXCLUDED.enabled,academic_eligible=EXCLUDED.academic_eligible,
            app_ids=EXCLUDED.app_ids,updated_at=EXCLUDED.updated_at"""
        )
        try:
            row = await self.pool.fetchrow(
                f"""  -- only the fixed server-owned conflict clause below is interpolated
                INSERT INTO fs2_inference_users
                    (id,tenant_id,principal_id,display_name,kind,team,enabled,academic_eligible,app_ids,created_at,updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) {conflict} RETURNING *
                """,  # noqa: S608 - conflict is a fixed SQL clause, all values use placeholders
                user.id,
                user.tenant_id,
                user.principal_id,
                user.display_name,
                user.kind,
                user.team,
                user.enabled,
                user.academic_eligible,
                user.app_ids,
                user.created_at,
                user.updated_at,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError("inference owner already exists") from exc
        return self._user(row)

    async def usage(self, tenant_id: str, principal_id: str, context: AdminContext) -> UserUsage:
        row = dict(
            await self.pool.fetchrow(
                """
            WITH operations AS (
                SELECT * FROM fs2_operations WHERE tenant_id=$1 AND principal_id=$2
                AND accepted_at >= $3 AND accepted_at < $4
                AND protocol <> 'scientific-artifact-upload-v1'
            ), attempts AS (
                SELECT s.operation_id,r.* FROM fs2_telemetry_subjects s
                JOIN operations o ON o.id=s.operation_id
                LEFT JOIN fs2_reporting_lifecycle_latest r USING(subject_id)
                WHERE s.workload_kind='scientific_batch'
            ), public_exchanges AS (
                SELECT * FROM fs2_request_telemetry WHERE tenant_id=$1 AND principal_id=$2
                    AND started_at >= $3 AND started_at < $4
            )
            SELECT count(*) AS requests,
                count(*) FILTER (WHERE status='succeeded') AS succeeded,
                count(*) FILTER (WHERE status IN ('failed','expired','preempted')) AS failed,
                count(*) FILTER (WHERE status='cancelled') AS cancelled,
                count(*) FILTER (WHERE status IN ('queued','activating')) AS pending,
                count(*) FILTER (WHERE status='running') AS running,
                count(*) FILTER (WHERE completed_at IS NOT NULL) AS terminal,
                count(*) FILTER (WHERE protocol='scientific-batch-v1') AS scientific_requests,
                (SELECT count(*) FROM public_exchanges) AS public_exchanges,
                (SELECT count(*) FROM public_exchanges WHERE semantic_outcome='succeeded') AS semantic_succeeded,
                (SELECT count(*) FROM public_exchanges WHERE semantic_outcome='accepted') AS semantic_accepted,
                (SELECT count(*) FROM public_exchanges WHERE semantic_outcome='failed') AS semantic_failed,
                (SELECT count(*) FROM public_exchanges WHERE semantic_outcome='cancelled') AS semantic_cancelled,
                (SELECT count(*) FROM public_exchanges WHERE semantic_outcome='timed_out') AS semantic_timed_out,
                (SELECT count(*) FROM public_exchanges
                    WHERE semantic_outcome IS NULL OR semantic_outcome='unknown') AS semantic_unknown,
                (SELECT count(*) FROM public_exchanges WHERE admission_stage='pre_admission'
                    AND semantic_outcome IN ('failed','cancelled','timed_out')) AS pre_admission_failed,
                max(accepted_at) AS last_request_at,
                count(*) FILTER (WHERE completed_at IS NOT NULL AND input_tokens IS NOT NULL
                    AND output_tokens IS NOT NULL) AS token_coverage,
                sum(input_tokens) AS input_tokens,sum(output_tokens) AS output_tokens,
                (SELECT count(*) FROM attempts) AS lifecycle_subjects,
                (SELECT count(DISTINCT operation_id) FROM attempts) AS lifecycle_operations,
                (SELECT bool_and(coalesce(terminal AND reconciled AND quality<>'unavailable'
                    AND NOT ('scheduler_occupancy_clock_missing'=ANY(data_gaps)),false))
                    FROM attempts)
                    AND (SELECT count(DISTINCT operation_id) FROM attempts)
                        = count(*) FILTER (WHERE protocol='scientific-batch-v1') AS lifecycle_complete,
                (SELECT sum(scheduler_occupied_gpu_seconds) FROM attempts) AS occupied,
                (SELECT bool_and(cardinality(data_gaps)=0) FROM attempts) AS phases_complete,
                (SELECT CASE
                    WHEN count(*) FILTER (WHERE quality IS NULL OR quality='unavailable')>0 THEN 'unavailable'
                    WHEN count(*) FILTER (WHERE quality='estimated')>0 THEN 'estimated'
                    WHEN count(*) FILTER (WHERE quality='application_observed')>0 THEN 'application_observed'
                    ELSE 'measured' END FROM attempts) AS lifecycle_quality,
                (SELECT sum(coalesce((phase_gpu_seconds->>'active_compute')::double precision,0))
                    FROM attempts) AS active,
                (SELECT sum(coalesce((phase_gpu_seconds->>'resident_idle')::double precision,0)
                    +coalesce((phase_gpu_seconds->>'workflow_wait')::double precision,0)
                    +coalesce((phase_gpu_seconds->>'cooldown_grace')::double precision,0)) FROM attempts)
                    AS classified_idle,
                (SELECT sum(coalesce((phase_gpu_seconds->>'image_pull')::double precision,0)
                    +coalesce((phase_gpu_seconds->>'artifact_load')::double precision,0)
                    +coalesce((phase_gpu_seconds->>'restore')::double precision,0)
                    +coalesce((phase_gpu_seconds->>'compile')::double precision,0)
                    +coalesce((phase_gpu_seconds->>'warmup')::double precision,0)) FROM attempts) AS startup,
                (SELECT sum(coalesce((phase_gpu_seconds->>'checkpoint_drain')::double precision,0)
                    +coalesce((phase_gpu_seconds->>'teardown')::double precision,0)) FROM attempts) AS other,
                (SELECT sum(greatest(0,scheduler_occupied_gpu_seconds-coalesce((
                    SELECT sum(value::double precision) FROM jsonb_each_text(phase_gpu_seconds)
                    WHERE key IN ('active_compute','resident_idle','workflow_wait','cooldown_grace',
                        'image_pull','artifact_load','restore','compile','warmup','checkpoint_drain','teardown')
                    ),0))) FROM attempts) AS unknown
            FROM operations
            """,
                tenant_id,
                principal_id,
                context.from_at,
                context.to_at,
            )
        )
        usage = usage_from_counts(row)
        seconds = max(1, ceil((context.to_at - context.from_at).total_seconds() / 60))
        points = await self.pool.fetch(
            """
            SELECT floor(extract(epoch FROM (accepted_at-$3::timestamptz))/$5)::integer AS bucket,
                   count(*) AS requests FROM fs2_operations
            WHERE tenant_id=$1 AND principal_id=$2 AND accepted_at >= $3 AND accepted_at < $4
                AND protocol <> 'scientific-artifact-upload-v1' GROUP BY bucket
            """,
            tenant_id,
            principal_id,
            context.from_at,
            context.to_at,
            seconds,
        )
        counts = {point["bucket"]: point["requests"] for point in points}
        usage.bucket_seconds = seconds
        usage.request_series = [
            UserUsagePoint(at=context.from_at + timedelta(seconds=index * seconds), requests=counts.get(index, 0))
            for index in range(ceil((context.to_at - context.from_at).total_seconds() / seconds))
        ]
        return usage


class MemoryUserRepository:
    """Development/test adapter over the same real in-memory operation store."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.users: dict[tuple[str, str], InferenceUser] = {}

    async def configured(self, tenant_id: str, principal_id: str) -> InferenceUser | None:
        return self.users.get((tenant_id, principal_id))

    async def keys(self, tenant_id: str, principal_id: str) -> list[TokenView]:
        return [
            value.view
            for value in self.store.tokens.values()
            if value.view.tenant_id == tenant_id and value.view.principal_id == principal_id
        ]

    async def list(self, tenant_id: str | None) -> list[InferenceUser]:
        users = dict(self.users)
        records = [
            (value.view.tenant_id, value.view.principal_id, value.view.created_at)
            for value in self.store.tokens.values()
        ]
        records += [
            (value.view.tenant_id, value.view.principal_id, value.view.accepted_at)
            for value in self.store.operations.values()
        ]
        for tenant, principal, created in sorted(records, key=lambda row: row[2]):
            users.setdefault(
                (tenant, principal),
                InferenceUser(
                    id=owner_id(tenant, principal),
                    tenant_id=tenant,
                    principal_id=principal,
                    display_name=principal,
                    source="existing-key-owner",
                    created_at=created,
                    updated_at=created,
                ),
            )
        return sorted(
            (user for user in users.values() if tenant_id is None or user.tenant_id == tenant_id),
            key=lambda user: (user.tenant_id, user.display_name, str(user.id)),
        )

    async def save(self, user: InferenceUser, *, create: bool = False) -> InferenceUser:
        key = (user.tenant_id, user.principal_id)
        if create and key in self.users:
            raise ConflictError("inference owner already exists")
        self.users[key] = user.model_copy(update={"source": "configured"})
        return self.users[key]

    async def usage(self, tenant_id: str, principal_id: str, context: AdminContext) -> UserUsage:
        values = [
            value.view
            for value in self.store.operations.values()
            if value.view.tenant_id == tenant_id
            and value.view.principal_id == principal_id
            and context.from_at <= value.view.accepted_at < context.to_at
            and value.view.protocol != "scientific-artifact-upload-v1"
        ]
        terminals = [op for op in values if op.status.terminal]
        usage = usage_from_counts(
            {
                "requests": len(values),
                "succeeded": sum(op.status == OperationStatus.SUCCEEDED for op in values),
                "failed": sum(
                    op.status in {OperationStatus.FAILED, OperationStatus.EXPIRED, OperationStatus.PREEMPTED}
                    for op in values
                ),
                "cancelled": sum(op.status == OperationStatus.CANCELLED for op in values),
                "pending": sum(op.status in {OperationStatus.QUEUED, OperationStatus.ACTIVATING} for op in values),
                "running": sum(op.status == OperationStatus.RUNNING for op in values),
                "scientific_requests": sum(op.protocol == "scientific-batch-v1" for op in values),
                "terminal": len(terminals),
                "last_request_at": max((op.accepted_at for op in values), default=None),
                "token_coverage": sum(op.input_tokens is not None and op.output_tokens is not None for op in terminals),
                "input_tokens": sum(op.input_tokens or 0 for op in terminals),
                "output_tokens": sum(op.output_tokens or 0 for op in terminals),
            }
        )
        seconds = max(1, ceil((context.to_at - context.from_at).total_seconds() / 60))
        usage.bucket_seconds = seconds
        usage.request_series = [
            UserUsagePoint(
                at=context.from_at + timedelta(seconds=index * seconds),
                requests=sum(
                    index * seconds <= (op.accepted_at - context.from_at).total_seconds() < (index + 1) * seconds
                    for op in values
                ),
            )
            for index in range(ceil((context.to_at - context.from_at).total_seconds() / seconds))
        ]
        return usage
