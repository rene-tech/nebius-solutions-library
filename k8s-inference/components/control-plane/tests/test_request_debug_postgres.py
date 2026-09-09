"""Actual isolated PostgreSQL migration/encryption/access contracts."""

import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from cryptography.exceptions import InvalidTag
from test_request_debug import NOW, row
from test_users_apps_postgres import database, operation, token  # noqa: F401

from fs2_serve.postgres import PostgresStore
from fs2_serve.request_debug import PostgresDebugStore, body_capture

pytestmark = pytest.mark.postgres


@pytest_asyncio.fixture
async def debug_database(database):  # noqa: F811
    await database.pool.execute("TRUNCATE fs2_request_debug")
    try:
        yield database
    finally:
        await database.pool.execute("TRUNCATE fs2_request_debug")


async def test_actual_encrypted_detail_pagination_unauthenticated_and_same_tenant_enrichment(debug_database):
    db = debug_database
    store = PostgresDebugStore(db.pool, db.cipher)
    key = await token(db)
    operation_id = await operation(db, key, model="boltz2")
    first = row(
        operation_id=operation_id,
        model_id=None,
        request_headers=[("x-api-key", "PRIVATE_KEY")],
        query_string="access_token=PRIVATE_QUERY&input_id=keep",
        request_body=body_capture(b"model-request-content", "text/plain", True),
        error_detail="useful upstream error",
    )
    second = row(started_at=NOW + timedelta(seconds=1), source="upstream", operation_attempt=2, upstream_attempt=1)
    unauthenticated = row(tenant_id=None, principal_id=None)
    for item in (first, second, unauthenticated):
        await store.record(item)
    await store.record(first)
    raw = await db.pool.fetchrow("SELECT * FROM fs2_request_debug WHERE id=$1", first.id)
    assert b"model-request-content" not in bytes(raw["ciphertext"])
    assert "PRIVATE_KEY" not in str(dict(raw)) and "PRIVATE_QUERY" not in str(dict(raw))
    assert not {"request_body", "request_headers", "error_detail", "query_string"} & set(raw.keys())
    detail = await store.get(first.id, "tenant-a")
    assert detail.model_id == "boltz2" and detail.request_body.data == "model-request-content"
    assert detail.request_headers == [("x-api-key", "[REDACTED]")]
    assert detail.error_detail == "useful upstream error"
    assert await store.get(first.id, "tenant-b") is None
    assert await store.get(unauthenticated.id, "tenant-a") is None
    assert await store.get(unauthenticated.id) is not None
    page = await store.list(tenant_id="tenant-a", model_id="boltz2", limit=1)
    assert page.items[0].id == second.id and page.next_cursor
    tail = await store.list(tenant_id="tenant-a", model_id="boltz2", limit=1, cursor=page.next_cursor)
    assert [item.id for item in tail.items] == [first.id] and tail.next_cursor is None
    assert len((await store.list()).items) == 3
    assert len((await store.list(operation_id=operation_id)).items) == 1
    # An authenticated metadata binding cannot be changed to another customer.
    await db.pool.execute("UPDATE fs2_request_debug SET tenant_id='tampered' WHERE id=$1", first.id)
    with pytest.raises(InvalidTag):
        await store.get(first.id)


async def test_actual_generated_runtime_role_can_insert_list_and_decrypt(debug_database):
    db = debug_database
    suffix = uuid4().hex[:10]
    role = f"fs2_debug_runtime_{suffix}"
    await PostgresStore.migrate_database(
        os.environ["FS2_TEST_DATABASE_URL"],
        Path(__file__).parents[1] / "migrations",
        f"fs2_debug_reporting_{suffix}",
        role,
        f"fs2_debug_maintenance_{suffix}",
        f"fs2_debug_activation_{suffix}",
    )

    async def assume(connection):
        await connection.execute(f'SET ROLE "{role}"')  # noqa: S608 - locally generated test role

    pool = await asyncpg.create_pool(os.environ["FS2_TEST_DATABASE_URL"], min_size=1, max_size=1, init=assume)
    try:
        store = PostgresDebugStore(pool, db.cipher)
        captured = row()
        await store.record(captured)
        assert (await store.get(captured.id, "tenant-a")).response_body.data == captured.response_body.data
        assert len((await store.list(tenant_id="tenant-a")).items) == 1
        async with pool.acquire() as connection:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute("DELETE FROM fs2_request_debug")
    finally:
        await pool.close()
