"""Workspace enumeration and completion are independent of bucket contents."""

import asyncio
from contextlib import asynccontextmanager
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_starter_packs import S3, pack
from test_user_storage import Repository, user

from fs2_serve.starter_pack_service import StarterPackService
from fs2_serve.starter_packs import BucketPackInstaller, PackError


class Pool:
    def __init__(self):
        self.rows = {}
        self.locked = False

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetchval(self, sql):
        if self.locked:
            return False
        self.locked = True
        return True

    async def fetchrow(self, sql, bucket, version, digest=None):
        key = (bucket, version)
        if sql.lstrip().startswith("SELECT"):
            return self.rows.get(key)
        if key in self.rows:
            return None  # Existing receipt is complete, backoff-bound or immutable.
        self.rows[key] = {"state": "pending", "version": version, "manifest_sha256": digest}
        return {"bucket_id": bucket}

    async def execute(self, sql, *args):
        if "pg_advisory_unlock" in sql:
            self.locked = False
            return
        row = self.rows[args[0], args[1]]
        if "state='complete'" in sql:
            row.update(state="complete", object_count=args[3], total_bytes=args[4])
        else:
            row.update(state="partial", error_code=args[3])


def setup(tmp_path, users):
    candidate = pack(tmp_path)
    repository = Repository()
    repository.pool = Pool()
    storage = SimpleNamespace(
        repository=repository,
        users=SimpleNamespace(list=AsyncMock(return_value=users), configured=AsyncMock(return_value=None)),
        policy=AsyncMock(return_value=SimpleNamespace(mode="tenant")),
    )
    service = StarterPackService(storage, tmp_path)
    service.pack = candidate
    clients = {}
    for item in users:
        bucket = {
            "bucket_id": item.tenant_id,
            "bucket_name": item.tenant_id,
            "quota_bytes": 1000000,
            "endpoint": "https://storage.invalid",
            "region": "test",
        }
        repository.buckets[item.tenant_id, ""] = bucket
        repository.credentials[item.tenant_id, item.principal_id] = {
            "owner_key": "",
            "enabled": True,
            "access_key_id": "test",
            "secret_access_key": "test",
        }
        clients[item.tenant_id] = S3()

    def install(credentials, bucket, candidate, stop):
        assert credentials.bucket_name == bucket["bucket_name"]
        return BucketPackInstaller(
            clients[bucket["bucket_id"]], bucket["bucket_name"], quota_bytes=bucket["quota_bytes"], stop=stop
        ).install(candidate)

    service._install = install
    return service, clients


def test_shared_bucket_once_and_completed_deletions_not_restored(tmp_path):
    service, clients = setup(tmp_path, [user("a", "one"), user("a", "two")])
    asyncio.run(service.reconcile_once())
    assert len(service.pool.rows) == 1
    assert asyncio.run(service.view("a"))["state"] == "complete"
    clients["a"].objects.pop("examples/v1/README.md")
    clients["a"].puts.clear()
    asyncio.run(service.reconcile_once())
    assert clients["a"].puts == []
    assert "examples/v1/README.md" not in clients["a"].objects


def test_private_buckets_each_seeded_without_cross_customer_reads(tmp_path):
    service, clients = setup(tmp_path, [user("a", "one"), user("a", "two"), user("b", "one")])
    repository = service.repository
    for principal in ("one", "two"):
        bucket = {**repository.buckets["a", ""], "bucket_id": "a-" + principal, "bucket_name": "a-" + principal}
        repository.buckets["a", principal] = bucket
        repository.credentials["a", principal]["owner_key"] = principal
        clients[bucket["bucket_id"]] = S3()
        clients[bucket["bucket_id"]].objects["private.txt"] = principal.encode()
    asyncio.run(service.reconcile_once())
    assert {key[0] for key in service.pool.rows} == {"a-one", "a-two", "b"}
    assert clients["a-one"].objects["private.txt"] == b"one"
    assert clients["a-two"].objects["private.txt"] == b"two"


@pytest.mark.parametrize("disabled", ["user", "policy", "key", "missing_key"])
def test_disabled_and_excluded_storage_never_seeded(tmp_path, disabled):
    service, clients = setup(tmp_path, [user(enabled=disabled != "user")])
    key = ("customer-a", "alice")
    if disabled == "policy":
        service.storage.policy.return_value = SimpleNamespace(mode="disabled")
    elif disabled == "key":
        service.repository.credentials[key]["enabled"] = False
    elif disabled == "missing_key":
        del service.repository.credentials[key]
    asyncio.run(service.reconcile_once())
    assert not service.pool.rows
    assert not clients["customer-a"].puts


def test_other_replica_owns_lock(tmp_path):
    service, clients = setup(tmp_path, [user()])
    service.pool.locked = True
    asyncio.run(service.reconcile_once())
    assert not clients["customer-a"].puts


def test_canary_selection_does_not_touch_other_eligible_buckets(tmp_path):
    service, clients = setup(tmp_path, [user("canary", "one"), user("customer", "one")])
    service.tenant_ids = frozenset({"canary"})
    asyncio.run(service.reconcile_once())
    assert clients["canary"].puts
    assert not clients["customer"].puts
    assert {key[0] for key in service.pool.rows} == {"canary"}


def test_error_receipt_is_payload_free_and_does_not_rewrite_customer_data(tmp_path):
    service, clients = setup(tmp_path, [user()])
    clients["customer-a"].objects["examples/v1/README.md"] = b"private edited content"
    asyncio.run(service.reconcile_once())
    assert asyncio.run(service.view("customer-a"))["error_code"] == "existing_object_conflict"
    assert "private edited content" not in str(service.pool.rows)


def test_same_version_different_manifest_reported_without_changes(tmp_path):
    service, clients = setup(tmp_path, [user()])
    service.pool.rows["customer-a", "v1"] = {"state": "complete", "manifest_sha256": "0" * 64}
    asyncio.run(service.reconcile_once())
    assert asyncio.run(service.view("customer-a"))["error_code"] == "immutable_version_conflict"
    assert not clients["customer-a"].puts


def test_database_receipt_unavailable_does_not_raise_to_storage_api(tmp_path):
    service, _ = setup(tmp_path, [user()])
    service.pool.fetchrow = AsyncMock(side_effect=RuntimeError("private database details"))
    assert asyncio.run(service.view("customer-a"))["error_code"] == "receipt_unavailable"


def test_stop_prevents_any_s3_work(tmp_path):
    candidate, client, stop = pack(tmp_path), S3(), Event()
    stop.set()
    with pytest.raises(PackError, match="seeding_stopped"):
        BucketPackInstaller(client, "owned", quota_bytes=1000000, stop=stop).install(candidate)
    assert client.puts == []
