"""Exercise storage against the real migration, locks and encrypted DB rows."""

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from test_user_storage import Provider, user

from fs2_serve.crypto import PayloadCipher
from fs2_serve.postgres import PostgresStore
from fs2_serve.postgresql_release import EXPECTED_MIGRATIONS
from fs2_serve.store import ConflictError
from fs2_serve.user_repository import PostgresUserRepository
from fs2_serve.user_storage import UserStorageService
from fs2_serve.user_storage_models import StoragePolicy
from fs2_serve.user_storage_repository import PostgresUserStorageRepository

pytestmark = pytest.mark.postgres


async def test_security_migration_queues_historical_rotation_and_fails_shared_layout_closed():
    database_url = os.environ.get("FS2_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("FS2_TEST_DATABASE_URL is not set")
    parsed = urlsplit(database_url)
    database_name = f"fs2_storage_migration_{uuid4().hex[:12]}"
    candidate_url = urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment))
    admin = await asyncpg.connect(database_url)
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        candidate = await asyncpg.connect(candidate_url)
        try:
            migration_dir = Path(__file__).parents[1] / "migrations"
            for version, _ in EXPECTED_MIGRATIONS[:-1]:
                await candidate.execute((migration_dir / version).read_text(encoding="utf-8"))
            await candidate.execute(
                """INSERT INTO fs2_storage_policies(tenant_id,mode) VALUES('legacy-tenant','tenant');
                INSERT INTO fs2_storage_buckets
                  (tenant_id,owner_key,bucket_id,bucket_name,group_id,endpoint,region,quota_bytes)
                VALUES('legacy-tenant','','bucket-legacy','opaque-legacy','group-shared',
                       'https://storage.test.invalid','test',5000000000);
                INSERT INTO fs2_user_storage
                  (tenant_id,principal_id,owner_key,service_account_id,access_key_resource_id,
                   access_key_id,secret_key_id,secret_nonce,secret_ciphertext,enabled)
                VALUES
                  ('legacy-tenant','alice','','sa-alice','key-alice','public-alice','payload-v1',
                   decode('000000000000000000000000','hex'),decode('00','hex'),true),
                  ('legacy-tenant','bob','','sa-bob','key-bob','public-bob','payload-v1',
                   decode('000000000000000000000000','hex'),decode('00','hex'),false)"""
            )
            await candidate.execute((migration_dir / EXPECTED_MIGRATIONS[-1][0]).read_text(encoding="utf-8"))

            policy = dict(
                await candidate.fetchrow(
                    """SELECT layout_mode,enabled,migration_state,migration_principal_count
                    FROM fs2_storage_policies WHERE tenant_id='legacy-tenant'"""
                )
            )
            assert policy == {
                "layout_mode": "tenant",
                "enabled": False,
                "migration_state": "inventory_required",
                "migration_principal_count": 2,
            }
            actions = {
                row["principal_id"]: (row["requested_action"], row["desired_enabled"], row["expires_at"])
                for row in await candidate.fetch(
                    """SELECT principal_id,requested_action,desired_enabled,expires_at
                    FROM fs2_user_storage ORDER BY principal_id"""
                )
            }
            assert actions["alice"][0:2] == ("rotate", True)
            assert actions["bob"][0:2] == ("revoke", False)
            assert all(value[2] is not None for value in actions.values())
        finally:
            await candidate.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await admin.close()


@pytest_asyncio.fixture
async def storage_database(cipher, hasher):
    url = os.environ.get("FS2_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FS2_TEST_DATABASE_URL is not set")
    store = await PostgresStore.connect(url, Path(__file__).parents[1] / "migrations", cipher, hasher, 3600)
    await store.migrate()
    await store.pool.execute("TRUNCATE fs2_user_storage,fs2_storage_buckets,fs2_storage_policies")
    await store.pool.execute("DELETE FROM fs2_audit_events WHERE target_type='user_storage'")
    try:
        yield store
    finally:
        await store.pool.execute("TRUNCATE fs2_user_storage,fs2_storage_buckets,fs2_storage_policies")
        await store.pool.execute("DELETE FROM fs2_audit_events WHERE target_type='user_storage'")
        await store.close()


async def test_durable_encrypted_credentials_and_restart(storage_database):
    store = storage_database
    repository = PostgresUserStorageRepository(store.pool, store.cipher)
    provider = Provider()
    service = UserStorageService(repository, provider, PostgresUserRepository(store.pool))
    await service.ensure(user())
    await service.ensure(user(principal="bob"))
    assert await store.pool.fetchval("SELECT count(*) FROM fs2_storage_buckets") == 2
    assert await store.pool.fetchval("SELECT count(*) FROM fs2_user_storage") == 2
    storage_cipher = PayloadCipher(
        active_key_id="storage-v1",
        keys={"payload-v1": b"p" * 32, "storage-v1": b"s" * 32},
    )
    rotated_repository = PostgresUserStorageRepository(store.pool, storage_cipher)
    assert await rotated_repository.reencrypt_if_needed("customer-a", "alice")
    assert not await rotated_repository.reencrypt_if_needed("customer-a", "alice")
    assert (
        await store.pool.fetchval(
            "SELECT secret_key_id FROM fs2_user_storage WHERE tenant_id='customer-a' AND principal_id='alice'"
        )
        == "storage-v1"
    )
    token_id = uuid4()
    await store.pool.execute(
        """INSERT INTO fs2_storage_policies(tenant_id,layout_mode,enabled)
        VALUES('customer-a','user',false) ON CONFLICT(tenant_id) DO UPDATE SET enabled=false"""
    )
    with pytest.raises(ConflictError, match="unavailable"):
        await rotated_repository.disclose(
            "customer-a",
            "alice",
            actor="alice",
            token_id=token_id,
        )
    await store.pool.execute("UPDATE fs2_storage_policies SET enabled=true WHERE tenant_id='customer-a'")
    revealed = await rotated_repository.disclose(
        "customer-a",
        "alice",
        actor="alice",
        token_id=token_id,
    )
    assert revealed.secret_access_key == "secret-customer-a-alice"
    row = dict(await store.pool.fetchrow("SELECT * FROM fs2_user_storage WHERE principal_id='alice'"))
    assert revealed.secret_access_key not in repr(row)
    key_calls = list(provider.key_calls)
    restarted = UserStorageService(
        rotated_repository,
        provider,
        service.users,
    )
    await restarted.ensure(user())
    assert restarted.provider.key_calls == key_calls
    with pytest.raises(ConflictError, match="already consumed"):
        await restarted.repository.disclose(
            "customer-a",
            "alice",
            actor="operator-a",
            token_id=None,
        )
    audit_rows = await store.pool.fetch(
        """SELECT actor,tenant_id,token_id,action,target_id,outcome,detail
        FROM fs2_audit_events WHERE target_type='user_storage' ORDER BY id"""
    )
    assert [row["action"] for row in audit_rows] == [
        "storage.credentials.disclose.replay_denied",
        "storage.credentials.disclose",
        "storage.credentials.disclose.replay_denied",
    ]
    assert (audit_rows[1]["actor"], audit_rows[1]["tenant_id"], audit_rows[1]["token_id"]) == (
        "alice",
        "customer-a",
        token_id,
    )
    assert (audit_rows[2]["actor"], audit_rows[2]["outcome"]) == ("operator-a", "denied")
    assert all(dict(row)["detail"] in ({}, "{}") for row in audit_rows)
    with pytest.raises(ConflictError):
        await service.configure("customer-a", StoragePolicy(mode="tenant"))


async def test_storage_and_runtime_roles_are_separate_and_ciphertext_is_not_enumerable(storage_database):
    async with storage_database.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE fs2_serve_runtime")
            await conn.execute("INSERT INTO fs2_storage_policies(tenant_id) VALUES('new-customer')")
            assert (
                await conn.fetchval("SELECT layout_mode FROM fs2_storage_policies WHERE tenant_id='new-customer'")
                == "user"
            )
            assert not await conn.fetchval("SELECT has_table_privilege(current_user,'fs2_user_storage','SELECT')")
            assert not await conn.fetchval(
                "SELECT has_column_privilege(current_user,'fs2_user_storage','secret_ciphertext','SELECT')"
            )

    async with storage_database.pool.acquire() as conn:
        await conn.execute("SET ROLE fs2_serve_runtime")
        try:
            with pytest.raises(Exception, match="permission denied"):
                await conn.fetch("SELECT secret_ciphertext FROM fs2_user_storage")
        finally:
            await conn.execute("RESET ROLE")

    async with storage_database.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE fs2_serve_storage")
            assert await conn.fetchval(
                "SELECT has_table_privilege(current_user,'fs2_user_storage','SELECT,INSERT,UPDATE')"
            )
            for unrelated in ("fs2_operations", "fs2_audit_events", "fs2_tokens", "fs2_request_debug"):
                assert not await conn.fetchval(
                    "SELECT has_table_privilege(current_user,$1,'SELECT')",
                    unrelated,
                )

    for query in (
        "SELECT * FROM fs2_operations WHERE false",
        "SELECT * FROM fs2_audit_events WHERE false",
        "SELECT * FROM fs2_tokens WHERE false",
        "SELECT * FROM fs2_request_debug WHERE false",
    ):
        async with storage_database.pool.acquire() as conn:
            await conn.execute("SET ROLE fs2_serve_storage")
            try:
                with pytest.raises(Exception, match="permission denied"):
                    await conn.fetch(query)
            finally:
                await conn.execute("RESET ROLE")
