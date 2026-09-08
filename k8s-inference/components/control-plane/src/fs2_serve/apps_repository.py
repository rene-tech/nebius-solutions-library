"""Durable, create-only app registration and optimistic metadata revisions."""

from __future__ import annotations

import json
from typing import Any, Protocol
from uuid import UUID

import asyncpg

from .admin_models import AdminContext
from .apps_models import AppRecord
from .user_models import owner_id


class AppConflictError(RuntimeError):
    pass


class AppsRepository(Protocol):
    async def list_records(self) -> list[AppRecord]: ...
    async def get(self, app_id: UUID) -> AppRecord | None: ...
    async def seed(self, record: AppRecord) -> AppRecord: ...
    async def update(self, record: AppRecord, *, expected_revision: int) -> AppRecord: ...


class PostgresAppsRepository:
    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self.pool = pool

    async def list_records(self) -> list[AppRecord]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch("SELECT * FROM fs2_apps ORDER BY created_at,app_id")
        return [AppRecord.model_validate(dict(row)) for row in rows]

    async def get(self, app_id: UUID) -> AppRecord | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM fs2_apps WHERE app_id=$1", app_id)
        return AppRecord.model_validate(dict(row)) if row else None

    async def seed(self, record: AppRecord) -> AppRecord:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """INSERT INTO fs2_apps(app_id,display_name,model_ref,public_model_id,execution_mode,
                    namespace,deployment_name,academic_required,revision,created_at,updated_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT(app_id) DO NOTHING""",
                record.app_id,
                record.display_name,
                record.model_ref,
                record.public_model_id,
                record.execution_mode,
                record.namespace,
                record.deployment_name,
                record.academic_required,
                record.revision,
                record.created_at,
                record.updated_at,
            )
            row = await connection.fetchrow("SELECT * FROM fs2_apps WHERE app_id=$1", record.app_id)
        assert row is not None
        existing = AppRecord.model_validate(dict(row))
        if any(
            getattr(existing, key) != getattr(record, key)
            for key in ("model_ref", "public_model_id", "execution_mode", "namespace", "deployment_name")
        ):
            raise AppConflictError("app identity already belongs to a different deployment")
        return existing

    async def update(self, record: AppRecord, *, expected_revision: int) -> AppRecord:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """UPDATE fs2_apps SET display_name=$2,academic_required=$3,updated_at=$4,revision=revision+1
                WHERE app_id=$1 AND revision=$5 RETURNING *""",
                record.app_id,
                record.display_name,
                record.academic_required,
                record.updated_at,
                expected_revision,
            )
        if row is None:
            raise AppConflictError("app metadata revision changed; reload before saving")
        return AppRecord.model_validate(dict(row))

    async def usage(self, model_id: str, context: AdminContext, tenant_id: str | None) -> dict[str, Any]:
        bucket_seconds = 300 if (context.to_at - context.from_at).total_seconds() <= 86400 else 3600
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """WITH operations AS (
                    SELECT * FROM fs2_operations WHERE model_id=$1 AND accepted_at >= $2 AND accepted_at < $3
                        AND ($4::text IS NULL OR tenant_id=$4)
                ), attempts AS (
                    SELECT s.operation_id,r.* FROM fs2_telemetry_subjects s JOIN operations o ON o.id=s.operation_id
                    LEFT JOIN fs2_reporting_lifecycle_latest r USING(subject_id)
                    WHERE s.workload_kind='scientific_batch'
                ), users AS (
                    SELECT tenant_id,principal_id,count(*) AS logical_runs FROM operations
                    GROUP BY tenant_id,principal_id
                ), buckets AS (
                    SELECT to_timestamp(floor(extract(epoch FROM accepted_at)/$5)*$5) AS timestamp,
                        count(*) AS logical_runs FROM operations GROUP BY 1
                ), classes AS (
                    SELECT CASE WHEN http_status IS NULL THEN 'unknown'
                        ELSE (http_status / 100)::text || 'xx' END AS class,count(*) AS total
                    FROM operations GROUP BY 1
                ) SELECT count(*) AS logical_runs,
                    count(*) FILTER(WHERE status='succeeded') AS succeeded_runs,
                    count(*) FILTER(WHERE status IN ('failed','cancelled','expired','preempted')) AS failed_runs,
                    count(*) FILTER(WHERE status IN ('queued','activating','running')) AS active_runs,
                    min(accepted_at) AS first_used_at,max(accepted_at) AS last_used_at,
                    sum(estimated_gpu_seconds) AS estimated_gpu_seconds,
                    CASE WHEN count(*) FILTER(WHERE input_tokens IS NULL)=0 THEN sum(input_tokens) END AS input_tokens,
                    CASE WHEN count(*) FILTER(WHERE output_tokens IS NULL)=0
                        THEN sum(output_tokens) END AS output_tokens,
                    (SELECT count(*) FROM users) AS unique_users,
                    (SELECT coalesce(jsonb_agg(to_jsonb(u) ORDER BY tenant_id,principal_id),'[]')
                        FROM users u) AS users,
                    (SELECT coalesce(jsonb_agg(to_jsonb(b) ORDER BY timestamp),'[]')
                        FROM buckets b) AS requests_over_time,
                    (SELECT coalesce(jsonb_object_agg(class,total),'{}') FROM classes) AS status_classes,
                    (SELECT CASE WHEN count(*) > 0 AND count(DISTINCT operation_id)=(
                            SELECT count(*) FROM operations WHERE protocol='scientific-batch-v1')
                        AND bool_and(coalesce(terminal AND reconciled
                            AND cardinality(data_gaps)=0,false)) THEN jsonb_build_object(
                        'occupied_seconds',sum(scheduler_occupied_gpu_seconds),
                        'active_compute_seconds',sum(active_gpu_seconds),
                        'occupied_idle_seconds',sum(occupied_idle_gpu_seconds)) END FROM attempts) AS scientific_gpu
                FROM operations""",
                model_id,
                context.from_at,
                context.to_at,
                tenant_id,
                bucket_seconds,
            )
        assert row is not None
        values = dict(row)
        for key in ("users", "requests_over_time", "status_classes", "scientific_gpu"):
            if isinstance(values[key], str):
                values[key] = json.loads(values[key])
        values["time_bucket_seconds"] = bucket_seconds
        for user in values["users"]:
            user["user_id"] = str(owner_id(user["tenant_id"], user["principal_id"]))
        return values

    async def last_used(self, model_id: str, tenant_id: str | None) -> Any:
        async with self.pool.acquire() as connection:
            return await connection.fetchval(
                """SELECT max(accepted_at) FROM fs2_operations
                WHERE model_id=$1 AND ($2::text IS NULL OR tenant_id=$2)""",
                model_id,
                tenant_id,
            )


class MemoryAppsRepository:
    """Same create-only semantics for focused service tests."""

    def __init__(self) -> None:
        self.records: dict[UUID, AppRecord] = {}

    async def list_records(self) -> list[AppRecord]:
        return sorted(self.records.values(), key=lambda item: (item.created_at, item.app_id))

    async def get(self, app_id: UUID) -> AppRecord | None:
        return self.records.get(app_id)

    async def seed(self, record: AppRecord) -> AppRecord:
        for item in self.records.values():
            if item.app_id != record.app_id and (
                item.public_model_id == record.public_model_id
                or (
                    item.deployment_name is not None
                    and (item.namespace, item.deployment_name) == (record.namespace, record.deployment_name)
                )
            ):
                raise AppConflictError("public route or deployment already belongs to an app")
        current = self.records.setdefault(record.app_id, record)
        if any(
            getattr(current, key) != getattr(record, key)
            for key in ("model_ref", "public_model_id", "execution_mode", "namespace", "deployment_name")
        ):
            raise AppConflictError("app identity already belongs to a different deployment")
        return current

    async def update(self, record: AppRecord, *, expected_revision: int) -> AppRecord:
        current = self.records.get(record.app_id)
        if current is None or current.revision != expected_revision:
            raise AppConflictError("app metadata revision changed; reload before saving")
        updated = current.model_copy(
            update={
                "display_name": record.display_name,
                "academic_required": record.academic_required,
                "updated_at": record.updated_at,
                "revision": expected_revision + 1,
            }
        )
        self.records[record.app_id] = updated
        return updated
