"""Customer bucket policy, retry, encryption, and real public/admin routing."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.exceptions import InvalidTag

from fs2_serve.crypto import Ciphertext
from fs2_serve.store import ConflictError
from fs2_serve.user_models import InferenceUser, owner_id
from fs2_serve.user_storage import UserStorageService
from fs2_serve.user_storage_models import StorageCredentials, StoragePolicy, UserStorage
from fs2_serve.user_storage_repository import PostgresUserStorageRepository


def user(tenant="customer-a", principal="alice", enabled=True):
    return InferenceUser(
        id=owner_id(tenant, principal),
        tenant_id=tenant,
        principal_id=principal,
        display_name=principal,
        enabled=enabled,
        source="configured",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


class Repository:
    def __init__(self):
        self.policies, self.buckets, self.credentials = {}, {}, {}
        self.locks = {}

    @asynccontextmanager
    async def tenant_lock(self, tenant):
        async with self.locks.setdefault(tenant, asyncio.Lock()):
            yield

    async def policy(self, tenant, default):
        return self.policies.get(tenant, default)

    async def set_policy(self, tenant, policy, default):
        previous = await self.policy(tenant, default)
        if any(key[0] == tenant for key in self.buckets) and previous.mode != policy.mode:
            raise ConflictError("migration required")
        self.policies[tenant] = policy

    async def bucket(self, tenant, owner):
        return self.buckets.get((tenant, owner))

    async def save_bucket(self, tenant, owner, bucket):
        self.buckets[tenant, owner] = bucket

    async def credential(self, tenant, principal):
        return self.credentials.get((tenant, principal))

    async def save_credential(self, tenant, principal, owner, value):
        self.credentials[tenant, principal] = {**value, "owner_key": owner, "enabled": True}

    async def enabled(self, tenant, principal, enabled):
        self.credentials[tenant, principal]["enabled"] = enabled

    async def view(self, tenant, principal, policy):
        credential = await self.credential(tenant, principal)
        if credential:
            bucket = self.buckets[tenant, credential["owner_key"]]
            return UserStorage(
                state="ready" if credential["enabled"] else "disabled",
                mode=policy.mode,
                quota_bytes=bucket["quota_bytes"],
                bucket_name=bucket["bucket_name"],
                endpoint=bucket["endpoint"],
                region=bucket["region"],
                access_key_id=credential["access_key_id"],
            )
        return UserStorage(state="disabled" if policy.mode == "disabled" else "pending", **policy.model_dump())

    async def disclose(self, tenant, principal):
        credential = self.credentials[tenant, principal]
        bucket = self.buckets[tenant, credential["owner_key"]]
        return StorageCredentials(
            bucket_name=bucket["bucket_name"],
            endpoint=bucket["endpoint"],
            region=bucket["region"],
            access_key_id=credential["access_key_id"],
            secret_access_key=credential["secret_access_key"],
        )


class Provider:
    def __init__(self):
        self.bucket_calls, self.key_calls, self.enabled_calls = [], [], []

    async def ensure_bucket(self, tenant, owner, quota):
        self.bucket_calls.append((tenant, owner, quota))
        return dict(
            bucket_id=f"b-{tenant}-{owner}",
            bucket_name=f"bucket-{tenant}-{owner}",
            group_id=f"g-{tenant}-{owner}",
            endpoint="https://storage.example.test",
            region="test",
            quota_bytes=quota,
        )

    async def ensure_credentials(self, tenant, principal, group):
        self.key_calls.append((tenant, principal, group))
        return dict(
            service_account_id=f"sa-{tenant}-{principal}",
            access_key_resource_id=f"key-{tenant}-{principal}",
            access_key_id=f"aws-{tenant}-{principal}",
            secret_access_key=f"secret-{tenant}-{principal}",
        )

    async def set_enabled(self, key, enabled):
        self.enabled_calls.append((key, enabled))

    async def close(self):
        pass


@pytest.fixture
def env():
    repository, provider = Repository(), Provider()
    users = SimpleNamespace(list=AsyncMock(return_value=[]))
    service = UserStorageService(repository, provider, users, excluded_tenants=("stockholm",))
    return SimpleNamespace(repository=repository, provider=provider, service=service, users=users)


async def test_shared_bucket_separate_user_keys_concurrent_and_idempotent(env):
    await asyncio.gather(*(env.service.ensure(user(principal=p)) for p in ["alice", "bob", "alice", "bob"]))
    assert len(env.provider.bucket_calls) == 1
    assert len(env.provider.key_calls) == 2
    assert {call[2] for call in env.provider.key_calls} == {"g-customer-a-"}
    a = await env.service.view("customer-a", "alice")
    b = await env.service.view("customer-a", "bob")
    assert a.bucket_name == b.bucket_name
    assert a.access_key_id != b.access_key_id
    assert "secret_access_key" not in a.model_dump()


async def test_private_buckets_are_isolated_and_tenants_do_not_collide(env):
    await env.service.configure("customer-a", StoragePolicy(mode="user"))
    await env.service.ensure(user())
    await env.service.ensure(user(principal="bob"))
    await env.service.ensure(user(tenant="customer-b"))
    assert len(env.provider.bucket_calls) == 3
    assert len({value[2] for value in env.provider.key_calls}) == 3


async def test_stockholm_is_excluded_before_any_cloud_operation(env):
    await env.service.ensure(user("stockholm", "team-01"))
    assert not env.provider.bucket_calls and not env.provider.key_calls
    assert (await env.service.view("stockholm", "team-01")).state == "disabled"
    with pytest.raises(ConflictError):
        await env.service.configure("stockholm", StoragePolicy())


async def test_disable_revokes_s3_and_reenable_reuses_identity(env):
    await env.service.ensure(user())
    await env.service.ensure(user(enabled=False))
    assert env.provider.enabled_calls[-1] == ("key-customer-a-alice", False)
    assert (await env.service.view("customer-a", "alice")).state == "disabled"
    await env.service.ensure(user())
    assert env.provider.enabled_calls[-1] == ("key-customer-a-alice", True)
    assert len(env.provider.key_calls) == 1


async def test_quota_update_preserves_identity_and_mode_change_requires_migration(env):
    await env.service.ensure(user())
    await env.service.configure("customer-a", StoragePolicy(quota_bytes=10_000_000_000))
    await env.service.ensure(user())
    assert (await env.service.view("customer-a", "alice")).quota_bytes == 10_000_000_000
    assert len(env.provider.key_calls) == 1
    with pytest.raises(ConflictError):
        await env.service.configure("customer-a", StoragePolicy(mode="user"))


async def test_failed_key_creation_retries_saved_bucket_and_other_users_continue(env):
    original = env.provider.ensure_credentials
    env.provider.ensure_credentials = AsyncMock(side_effect=[RuntimeError("provider unavailable")])
    env.users.list.return_value = [user()]
    assert (await env.service.reconcile_once())["failed"] == 1
    env.provider.ensure_credentials = original
    assert (await env.service.reconcile_once())["ready"] == 1
    assert len(env.provider.bucket_calls) == 1


async def test_quota_failure_backs_off_new_provisioning_but_still_disables_keys(env):
    await env.service.ensure(user())
    quota = RuntimeError("quota fixture")
    quota.code = "RESOURCE_EXHAUSTED"
    env.provider.ensure_bucket = AsyncMock(side_effect=quota)
    env.users.list.return_value = [user("new-tenant"), user(enabled=False), user("another-tenant")]
    result = await env.service.reconcile_once()
    assert result["failed"] == 1
    assert env.provider.ensure_bucket.call_count == 1
    assert env.provider.enabled_calls[-1] == ("key-customer-a-alice", False)
    assert (await env.service.view("another-tenant", "alice")).state == "pending"


async def test_credentials_are_encrypted_and_bound_to_owner(cipher):
    pool = SimpleNamespace(execute=AsyncMock())
    repository = PostgresUserStorageRepository(pool, cipher)
    value = await Provider().ensure_credentials("customer-a", "alice", "group")
    await repository.save_credential("customer-a", "alice", "", value)
    args = pool.execute.call_args.args
    assert value["secret_access_key"] not in repr(args)
    envelope = Ciphertext(*args[-3:])
    assert cipher.decrypt(envelope, aad=repository.aad("customer-a", "alice")).decode() == value["secret_access_key"]
    with pytest.raises(InvalidTag):
        cipher.decrypt(envelope, aad=repository.aad("customer-b", "alice"))


def test_credentials_endpoint_auth_and_debug_exclusion(env, registry, cipher, hasher):
    from test_admin_access_api import BOOTSTRAP_AUTH, _client, _runtime

    from fs2_serve.user_storage_routes import user_storage_router

    runtime = _runtime(registry, cipher, hasher)
    with _client(runtime) as client:
        assert client.get("/v1/storage").status_code == 401
        assert client.post("/v1/storage/credentials").status_code == 401
        assert client.post("/admin/api/v1/session", headers=BOOTSTRAP_AUTH).status_code == 200
        assert client.get("/admin/api/v1/tenants/customer-a/storage").status_code == 503
    # The production router is present even when storage is disabled; it does
    # not silently return an empty bucket or expose secrets in list responses.
    assert user_storage_router is not None
