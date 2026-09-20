"""Real ledger claims, completion persistence and restricted runtime grants."""

# ruff: noqa: F811 - pytest imports and injects the shared fixture by name.

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_starter_packs import S3, pack
from test_user_storage import user
from test_user_storage_postgres import storage_database  # noqa: F401 - shared database fixture

from fs2_serve.starter_pack_service import StarterPackService
from fs2_serve.starter_packs import BucketPackInstaller
from fs2_serve.user_storage_repository import PostgresUserStorageRepository

pytestmark = pytest.mark.postgres


async def test_real_ledger_complete_survives_deleted_object_and_retry(storage_database, tmp_path):
    store = storage_database
    await store.pool.execute("TRUNCATE fs2_customer_starter_packs")
    repository = PostgresUserStorageRepository(store.pool, store.cipher)
    storage = SimpleNamespace(
        repository=repository,
        users=SimpleNamespace(configured=AsyncMock(return_value=None)),
        policy=AsyncMock(return_value=SimpleNamespace(mode="tenant")),
    )
    repository.credential = AsyncMock(return_value={"enabled": True})
    repository.disclose = AsyncMock(return_value=SimpleNamespace(bucket_name="owned"))
    service = StarterPackService(storage, tmp_path)
    service.pack = pack(tmp_path)
    s3 = S3()
    bucket = {"bucket_id": "test-starter-bucket", "bucket_name": "owned", "quota_bytes": 1000000}
    service._install = lambda credentials, bucket, candidate, stop: BucketPackInstaller(
        s3,
        bucket["bucket_name"],
        quota_bytes=bucket["quota_bytes"],
        stop=stop,
    ).install(candidate)
    await service._seed(user(), bucket)
    assert (await service.view(bucket["bucket_id"]))["state"] == "complete"
    del s3.objects["examples/v1/README.md"]
    s3.puts.clear()
    await service._seed(user(), bucket)
    assert not s3.puts
    assert await store.pool.fetchval("SELECT attempts FROM fs2_customer_starter_packs") == 1
    await store.pool.execute("TRUNCATE fs2_customer_starter_packs")


async def test_real_ledger_backoff_and_same_digest_retry(storage_database, tmp_path):
    store = storage_database
    await store.pool.execute("TRUNCATE fs2_customer_starter_packs")
    repository = PostgresUserStorageRepository(store.pool, store.cipher)
    storage = SimpleNamespace(
        repository=repository,
        users=SimpleNamespace(configured=AsyncMock(return_value=None)),
        policy=AsyncMock(return_value=SimpleNamespace(mode="tenant")),
    )
    repository.credential = AsyncMock(return_value={"enabled": True})
    repository.disclose = AsyncMock(return_value=SimpleNamespace(bucket_name="owned"))
    service = StarterPackService(storage, tmp_path)
    service.pack = pack(tmp_path)
    s3 = S3()
    bucket = {"bucket_id": "test-starter-retry", "bucket_name": "owned", "quota_bytes": 1000000}
    s3.fail_at = "examples/v1/structure/protein.fasta"
    service._install = lambda credentials, bucket, candidate, stop: BucketPackInstaller(
        s3,
        bucket["bucket_name"],
        quota_bytes=bucket["quota_bytes"],
        stop=stop,
    ).install(candidate)
    await service._seed(user(), bucket)
    assert (await service.view(bucket["bucket_id"]))["state"] == "partial"
    s3.fail_at = None
    await service._seed(user(), bucket)
    assert await store.pool.fetchval("SELECT attempts FROM fs2_customer_starter_packs") == 1
    await store.pool.execute("UPDATE fs2_customer_starter_packs SET updated_at=now()-interval '6 minutes'")
    await service._seed(user(), bucket)
    assert (await service.view(bucket["bucket_id"]))["state"] == "complete"
    assert await store.pool.fetchval("SELECT attempts FROM fs2_customer_starter_packs") == 2
    await store.pool.execute("TRUNCATE fs2_customer_starter_packs")


async def test_runtime_role_has_only_required_receipt_privileges(storage_database):
    async with storage_database.pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE fs2_serve_runtime")
            for privilege in ("SELECT", "INSERT", "UPDATE"):
                assert await connection.fetchval(
                    "SELECT has_table_privilege(current_user,'fs2_customer_starter_packs',$1)", privilege
                )
            assert not await connection.fetchval(
                "SELECT has_table_privilege(current_user,'fs2_customer_starter_packs','DELETE')"
            )
