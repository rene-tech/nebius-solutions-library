"""Durable bindings and observations, sharing the platform PostgreSQL pool."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from .admin_models import AdminContext
from .store import ConflictError, NotFoundError
from .workbench_models import CustomerProfile, WorkbenchBinding, WorkbenchCommand, WorkbenchOperation


def document(value: Any) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else dict(value)


class PostgresWorkbenchRepository:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def profiles(self, tenant: str | None) -> dict[str, dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT * FROM fs2_customer_profiles WHERE ($1::text IS NULL OR tenant_id=$1)", tenant
        )
        return {row["tenant_id"]: dict(row) for row in rows}

    async def save_profile(self, tenant: str, value: CustomerProfile) -> None:
        await self.pool.execute(
            """INSERT INTO fs2_customer_profiles(tenant_id,display_name,purpose,archived) VALUES($1,$2,$3,$4)
            ON CONFLICT(tenant_id) DO UPDATE SET display_name=$2,purpose=$3,archived=$4,updated_at=now()""",
            tenant,
            value.display_name,
            value.purpose,
            value.archived,
        )

    async def bindings(self, tenant: str | None = None) -> list[WorkbenchBinding]:
        rows = await self.pool.fetch(
            "SELECT * FROM fs2_workbenches WHERE ($1::text IS NULL OR tenant_id=$1) ORDER BY name,id", tenant
        )
        return [WorkbenchBinding.model_validate(dict(row)) for row in rows]

    async def binding(self, identity: UUID) -> WorkbenchBinding:
        row = await self.pool.fetchrow("SELECT * FROM fs2_workbenches WHERE id=$1", identity)
        if row is None:
            raise NotFoundError("workbench not found")
        return WorkbenchBinding.model_validate(dict(row))

    async def register(self, value: WorkbenchBinding) -> WorkbenchBinding:
        async with self.pool.acquire() as connection, connection.transaction():
            # Serialize adoption across retries, without a cloud write.
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,42))", value.endpoint_id)
            old = await connection.fetchrow("SELECT * FROM fs2_workbenches WHERE endpoint_id=$1", value.endpoint_id)
            if old:
                existing = WorkbenchBinding.model_validate(dict(old))
                if (existing.tenant_id, existing.principal_ids, existing.project_id) != (
                    value.tenant_id,
                    value.principal_ids,
                    value.project_id,
                ):
                    raise ConflictError("endpoint is already bound to a different owner")
                return existing
            await connection.execute(
                """INSERT INTO fs2_workbenches
                (id,tenant_id,principal_ids,name,management,endpoint_id,project_id,bucket_name,protected,
                 protection_reason,state_filesystem_id)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                value.id,
                value.tenant_id,
                value.principal_ids,
                value.name,
                value.management,
                value.endpoint_id,
                value.project_id,
                value.bucket_name,
                value.protected,
                value.protection_reason,
                value.state_filesystem_id,
            )
        return await self.binding(value.id)

    async def observations(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch("SELECT observation,observed_at FROM fs2_workbench_observations")
        return [{**document(row["observation"]), "observed_at": row["observed_at"]} for row in rows]

    async def observe(self, project: str, observations: list[dict[str, Any]]) -> None:
        # An entire successfully paginated inventory is one snapshot. Failed
        # refreshes retain previous observations (which naturally become stale).
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,43))", project)
            await connection.execute("DELETE FROM fs2_workbench_observations WHERE project_id=$1", project)
            await connection.executemany(
                """INSERT INTO fs2_workbench_observations(resource_id,project_id,kind,observation)
                VALUES($1,$2,$3,$4::jsonb)""",
                [(item["resource_id"], project, item["kind"], json.dumps(item)) for item in observations],
            )

    async def model_usage(self, tenant: str | None, context: AdminContext) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """SELECT tenant_id,model_id,count(*) AS requests,
            count(*) FILTER (WHERE status='succeeded') AS succeeded,
            count(*) FILTER (WHERE status IN ('failed','expired','preempted')) AS failed,
            count(*) FILTER (WHERE status IN ('queued','activating','running')) AS in_progress,
            count(DISTINCT principal_id) AS users,max(accepted_at) AS last_request_at
            FROM fs2_operations WHERE ($1::text IS NULL OR tenant_id=$1)
            AND accepted_at >= $2 AND accepted_at < $3 AND protocol <> 'scientific-artifact-upload-v1'
            GROUP BY tenant_id,model_id ORDER BY count(*) DESC,model_id""",
            tenant,
            context.from_at,
            context.to_at,
        )
        return [dict(row) for row in rows]

    async def storage(self, tenant: str | None) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """SELECT tenant_id,owner_key,bucket_id,bucket_name,endpoint,region,quota_bytes
            FROM fs2_storage_buckets WHERE ($1::text IS NULL OR tenant_id=$1) ORDER BY tenant_id,owner_key""",
            tenant,
        )
        return [dict(row) for row in rows]

    async def operations(self, workbench_id: UUID) -> list[WorkbenchOperation]:
        rows = await self.pool.fetch(
            """SELECT id,workbench_id,kind,state,created_at,updated_at,error_code,progress
            FROM fs2_workbench_operations WHERE workbench_id=$1 ORDER BY created_at DESC LIMIT 30""",
            workbench_id,
        )
        return [WorkbenchOperation.model_validate({**dict(row), "progress": document(row["progress"])}) for row in rows]

    async def command(self, binding: WorkbenchBinding, command: WorkbenchCommand, actor: str) -> WorkbenchOperation:
        async with self.pool.acquire() as connection, connection.transaction():
            current = await connection.fetchrow("SELECT * FROM fs2_workbenches WHERE id=$1 FOR UPDATE", binding.id)
            if current is None:
                raise NotFoundError("workbench not found")
            if current["protected"]:
                raise ConflictError("workbench is protected: no lifecycle changes permitted")
            specification = command.model_dump(mode="json", exclude={"idempotency_key"})
            previous = await connection.fetchrow(
                "SELECT * FROM fs2_workbench_operations WHERE workbench_id=$1 AND idempotency_key=$2",
                binding.id,
                command.idempotency_key,
            )
            if previous:
                if document(previous["specification"]) != specification:
                    raise ConflictError("idempotency key belongs to a different request")
                operation_id = previous["id"]
            else:
                if current["revision"] != command.expected_revision:
                    raise ConflictError("workbench changed; refresh before requesting an update")
                active = await connection.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM fs2_workbench_operations WHERE workbench_id=$1
                    AND state IN ('queued','running','awaiting_confirmation'))""",
                    binding.id,
                )
                if active:
                    raise ConflictError("a workbench operation is already active")
                operation_id = uuid4()
                await connection.execute(
                    """INSERT INTO fs2_workbench_operations
                    (id,workbench_id,idempotency_key,kind,requested_by,specification)
                    VALUES($1,$2,$3,$4,$5,$6::jsonb)""",
                    operation_id,
                    binding.id,
                    command.idempotency_key,
                    command.kind,
                    actor,
                    json.dumps(specification),
                )
        row = await self.pool.fetchrow(
            """SELECT id,workbench_id,kind,state,created_at,updated_at,error_code,progress
            FROM fs2_workbench_operations WHERE id=$1""", operation_id
        )
        return WorkbenchOperation.model_validate({**dict(row), "progress": document(row["progress"])})


class MemoryWorkbenchRepository:
    """Same metadata semantics for API tests; never a production persistence fallback."""

    def __init__(self) -> None:
        self.profile_values: dict[str, dict[str, Any]] = {}
        self.binding_values: dict[UUID, WorkbenchBinding] = {}
        self.observation_values: list[dict[str, Any]] = []
        self.usage_values: list[dict[str, Any]] = []
        self.storage_values: list[dict[str, Any]] = []

    async def profiles(self, tenant: str | None) -> dict[str, dict[str, Any]]:
        return {key: value for key, value in self.profile_values.items() if tenant is None or key == tenant}

    async def save_profile(self, tenant: str, value: CustomerProfile) -> None:
        self.profile_values[tenant] = value.model_dump()

    async def bindings(self, tenant: str | None = None) -> list[WorkbenchBinding]:
        return [value for value in self.binding_values.values() if tenant is None or value.tenant_id == tenant]

    async def binding(self, identity: UUID) -> WorkbenchBinding:
        if identity not in self.binding_values:
            raise NotFoundError("workbench not found")
        return self.binding_values[identity]

    async def register(self, value: WorkbenchBinding) -> WorkbenchBinding:
        for old in self.binding_values.values():
            if old.endpoint_id == value.endpoint_id:
                if (old.tenant_id, old.principal_ids, old.project_id) != (
                    value.tenant_id,
                    value.principal_ids,
                    value.project_id,
                ):
                    raise ConflictError("endpoint is already bound to a different owner")
                return old
        self.binding_values[value.id] = value
        return value

    async def observations(self) -> list[dict[str, Any]]:
        return self.observation_values

    async def observe(self, project: str, observations: list[dict[str, Any]]) -> None:
        self.observation_values = [item for item in self.observation_values if item["project_id"] != project] + [
            {**item, "observed_at": datetime.now(UTC)} for item in observations
        ]

    async def model_usage(self, tenant: str | None, context: AdminContext) -> list[dict[str, Any]]:
        return [item for item in self.usage_values if tenant is None or item["tenant_id"] == tenant]

    async def storage(self, tenant: str | None) -> list[dict[str, Any]]:
        return [item for item in self.storage_values if tenant is None or item["tenant_id"] == tenant]

    async def operations(self, workbench_id: UUID) -> list[WorkbenchOperation]:
        return []
