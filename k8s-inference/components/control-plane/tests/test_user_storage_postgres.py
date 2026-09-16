"""Exercise storage against the real migration, locks and encrypted DB rows."""

import os
from pathlib import Path

import pytest
import pytest_asyncio
from test_user_storage import Provider, user

from fs2_serve.postgres import PostgresStore
from fs2_serve.store import ConflictError
from fs2_serve.user_repository import PostgresUserRepository
from fs2_serve.user_storage import UserStorageService
from fs2_serve.user_storage_models import StoragePolicy
from fs2_serve.user_storage_repository import PostgresUserStorageRepository

pytestmark = pytest.mark.postgres


@pytest_asyncio.fixture
async def storage_database(cipher, hasher):
    url = os.environ.get("FS2_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FS2_TEST_DATABASE_URL is not set")
    store = await PostgresStore.connect(url, Path(__file__).parents[1] / "migrations", cipher, hasher, 3600)
    await store.migrate()
    await store.pool.execute("TRUNCATE fs2_user_storage,fs2_storage_buckets,fs2_storage_policies")
    try:
        yield store
    finally:
        await store.pool.execute("TRUNCATE fs2_user_storage,fs2_storage_buckets,fs2_storage_policies")
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
    revealed = await repository.disclose("customer-a", "alice")
    assert revealed.secret_access_key == "secret-customer-a-alice"
    row = dict(await store.pool.fetchrow("SELECT * FROM fs2_user_storage WHERE principal_id='alice'"))
    assert revealed.secret_access_key not in repr(row)
    restarted = UserStorageService(PostgresUserStorageRepository(store.pool, store.cipher), Provider(), service.users)
    await restarted.ensure(user())
    assert not restarted.provider.key_calls
    assert (await restarted.repository.disclose("customer-a", "alice")) == revealed
    with pytest.raises(ConflictError):
        await service.configure("customer-a", StoragePolicy(mode="tenant"))


async def test_runtime_role_can_read_and_write_storage_tables(storage_database):
    async with storage_database.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE fs2_serve_runtime")
            await conn.execute("INSERT INTO fs2_storage_policies(tenant_id) VALUES('new-customer')")
            assert await conn.fetchval("SELECT mode FROM fs2_storage_policies WHERE tenant_id='new-customer'") == "user"
