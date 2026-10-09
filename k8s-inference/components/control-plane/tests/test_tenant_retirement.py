"""Real PostgreSQL event offboarding; history is preserved, access is not."""
# ruff: noqa: F811 -- imported pytest fixture is injected into test parameters

import os
from uuid import uuid4

import asyncpg
import pytest
from test_users_apps_postgres import NOW, database, operation, token  # noqa: F401

from fs2_serve.store import ConflictError, NotFoundError
from fs2_serve.tenant_retirement import TenantRetirementRequest, retire_event_tenant
from fs2_serve.user_models import InferenceUser, owner_id
from fs2_serve.user_repository import PostgresUserRepository

pytestmark = pytest.mark.postgres


async def setup_tenant(database):
    tenant = "event-" + uuid4().hex
    repo = PostgresUserRepository(database.pool)
    key = await token(database, tenant=tenant)
    await repo.save(
        InferenceUser(
            id=owner_id(tenant, "researcher"),
            tenant_id=tenant,
            principal_id="researcher",
            display_name="Event scientist",
            source="configured",
            created_at=NOW,
            updated_at=NOW,
        ),
        create=True,
    )
    op = await operation(database, key)
    return tenant, repo, key, op


async def test_retirement_preserves_history_and_other_tenants(database):
    tenant, repo, key, op = await setup_tenant(database)
    foreign = await token(database, tenant="retained-customer")
    request = TenantRetirementRequest(archive_sha256="a" * 64, expected_users=1, expected_keys=1)

    # Exercise the actual granted runtime role, not the owner role.
    async def assume(connection):
        await connection.execute("SET ROLE fs2_serve_runtime")

    runtime_pool = await asyncpg.create_pool(os.environ["FS2_TEST_DATABASE_URL"], min_size=1, max_size=2, setup=assume)
    try:
        assert await runtime_pool.fetchval("SELECT current_user") == "fs2_serve_runtime"
        result = await retire_event_tenant(runtime_pool, tenant, request, "test-admin")
        assert result["user_count"] == result["key_count"] == 1
        assert await repo.list(tenant) == []
        assert await database.list_tokens(tenant_id=tenant) == []
        assert await database.token_for_verification(key.id) is None
        assert await database.token_for_verification(foreign.id) is not None
        assert await database.pool.fetchval("SELECT count(*) FROM fs2_operations WHERE id=$1", op) == 1
        assert await database.pool.fetchval("SELECT revoked_at IS NOT NULL FROM fs2_tokens WHERE id=$1", key.id)
        assert await retire_event_tenant(database.pool, tenant, request, "test-admin") == result
        with pytest.raises(ConflictError):
            await token(database, tenant=tenant)
        with pytest.raises(ConflictError):
            await repo.save(
                InferenceUser(
                    id=owner_id(tenant, "new"),
                    tenant_id=tenant,
                    principal_id="new",
                    display_name="New",
                    source="configured",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                create=True,
            )
    finally:
        await runtime_pool.close()


async def test_counts_and_active_work_abort_without_partial_deletion(database):
    tenant, repo, key, op = await setup_tenant(database)
    with pytest.raises(ConflictError, match="changed"):
        await retire_event_tenant(
            database.pool,
            tenant,
            TenantRetirementRequest(archive_sha256="b" * 64, expected_users=2, expected_keys=1),
            "test",
        )
    # A queued operation must be genuinely nonterminal; use a new row to avoid
    # the immutable terminal usage trigger on an already completed operation.
    await database.pool.execute(
        """INSERT INTO fs2_operations
        (id,tenant_id,principal_id,token_id,model_id,model_revision,protocol,operation,idempotency_key,
         request_hmac_key_id,request_hmac,request_content_type,status,payload_expires_at,max_attempts)
        VALUES($1,$2,'researcher',$3,'qwen3-8b','revision','openai-chat','test',$4,
         'test',$5,'application/json','queued',now()+interval '1 hour',1)""",
        uuid4(),
        tenant,
        key.id,
        uuid4().hex,
        "0" * 64,
    )
    with pytest.raises(ConflictError, match="active operations"):
        await retire_event_tenant(
            database.pool,
            tenant,
            TenantRetirementRequest(archive_sha256="b" * 64, expected_users=1, expected_keys=1),
            "test",
        )
    assert len(await repo.list(tenant)) == 1
    assert await database.token_for_verification(key.id) is not None
    assert not await database.pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM fs2_retired_tenants WHERE tenant_id=$1)", tenant
    )


async def test_owned_cloud_storage_prevents_retirement(database):
    tenant, repo, key, op = await setup_tenant(database)
    await database.pool.execute(
        """INSERT INTO fs2_storage_buckets
        (tenant_id,owner_key,bucket_id,bucket_name,group_id,endpoint,region,quota_bytes)
        VALUES($1,'','bucket',$2,'group','https://example.invalid','test',5000000000)""",
        tenant,
        tenant,
    )
    try:
        with pytest.raises(ConflictError, match="owned storage"):
            await retire_event_tenant(
                database.pool,
                tenant,
                TenantRetirementRequest(archive_sha256="c" * 64, expected_users=1, expected_keys=1),
                "test",
            )
        assert len(await repo.list(tenant)) == 1
    finally:
        await database.pool.execute("DELETE FROM fs2_storage_buckets WHERE tenant_id=$1", tenant)


async def test_missing_tenant_and_archive_mismatch(database):
    with pytest.raises(NotFoundError):
        await retire_event_tenant(
            database.pool,
            "missing-" + uuid4().hex,
            TenantRetirementRequest(archive_sha256="d" * 64, expected_users=0, expected_keys=0),
            "test",
        )
    tenant, repo, key, op = await setup_tenant(database)
    await retire_event_tenant(
        database.pool,
        tenant,
        TenantRetirementRequest(archive_sha256="d" * 64, expected_users=1, expected_keys=1),
        "test",
    )
    with pytest.raises(ConflictError, match="different archive"):
        await retire_event_tenant(
            database.pool,
            tenant,
            TenantRetirementRequest(archive_sha256="e" * 64, expected_users=1, expected_keys=1),
            "test",
        )
