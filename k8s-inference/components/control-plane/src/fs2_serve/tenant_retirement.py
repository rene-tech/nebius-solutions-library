"""Archive-backed retirement of quiescent event tenants without cloud resources.

Delete active inference-account records, invalidate credentials and retain the
historical records needed to understand runs. Cloud storage, deployments and
operator accounts need their own offboarding and are deliberately not deleted.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .store import ConflictError, NotFoundError


class TenantRetirementRequest(BaseModel):
    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_users: int = Field(ge=0)
    expected_keys: int = Field(ge=0)


async def retire_event_tenant(pool: Any, tenant: str, request: TenantRetirementRequest, actor: str) -> dict[str, Any]:
    from .postgres import PostgresStore

    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SET LOCAL lock_timeout = '5s'")
        await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,34))", tenant)
        prior = await connection.fetchrow("SELECT * FROM fs2_retired_tenants WHERE tenant_id=$1", tenant)
        if prior:
            if prior["archive_sha256"] != request.archive_sha256:
                raise ConflictError("tenant already retired with a different archive")
            return dict(prior)
        # Lock the same token identities used by admission before checking work.
        keys = await connection.fetch("SELECT id FROM fs2_tokens WHERE tenant_id=$1 ORDER BY id", tenant)
        for key in keys:
            await PostgresStore._token_lock(connection, key["id"])
        users = await connection.fetchval("SELECT count(*) FROM fs2_inference_users WHERE tenant_id=$1", tenant)
        if users == 0 and not keys:
            raise NotFoundError("event tenant was not found")
        if users != request.expected_users or len(keys) != request.expected_keys:
            raise ConflictError("tenant identities changed since the archive; export again")
        for table in ("fs2_storage_buckets", "fs2_user_storage", "fs2_model_deployments", "fs2_operator_principals"):
            if await connection.fetchval(f"SELECT EXISTS(SELECT 1 FROM {table} WHERE tenant_id=$1)", tenant):  # noqa: S608
                raise ConflictError("tenant has owned storage, deployments or operator accounts; offboard these first")
        active = await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM fs2_operations WHERE tenant_id=$1
               AND status NOT IN ('succeeded','failed','cancelled','preempted','expired'))""",
            tenant,
        )
        if active:
            raise ConflictError("tenant still has active operations; allow them to finish first")
        row = await connection.fetchrow(
            """INSERT INTO fs2_retired_tenants(tenant_id,retired_by,archive_sha256,user_count,key_count)
               VALUES($1,$2,$3,$4,$5) RETURNING *""",
            tenant,
            actor,
            request.archive_sha256,
            users,
            len(keys),
        )
        await connection.execute(
            "UPDATE fs2_tokens SET revoked_at=coalesce(revoked_at,clock_timestamp()) WHERE tenant_id=$1", tenant
        )
        await connection.execute("DELETE FROM fs2_inference_users WHERE tenant_id=$1", tenant)
        await connection.execute("DELETE FROM fs2_storage_policies WHERE tenant_id=$1", tenant)
        # Existing audit trail, not a new logging/retention system.
        await PostgresStore._audit(
            connection,
            actor=actor,
            tenant_id=tenant,
            token_id=None,
            action="tenant.retire",
            target_type="tenant",
            target_id=tenant,
            outcome="succeeded",
            detail={"archive_sha256": request.archive_sha256, "users_deleted": users, "keys_revoked": len(keys)},
        )
        return dict(row)
