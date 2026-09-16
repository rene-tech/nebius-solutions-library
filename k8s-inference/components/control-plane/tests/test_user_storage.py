"""Customer bucket policy, retry, encryption, and real public/admin routing."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from cryptography.exceptions import InvalidTag

from fs2_serve.crypto import Ciphertext, PayloadCipher
from fs2_serve.models import Principal, Scope
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
        self.tenant_singletons = {}

    @asynccontextmanager
    async def tenant_lock(self, tenant):
        async with self.locks.setdefault(tenant, asyncio.Lock()):
            yield

    async def policy(self, tenant, default):
        return self.policies.get(tenant, default)

    async def set_policy(self, tenant, policy, default):
        previous = await self.policy(tenant, default)
        previous_layout = self.policies.get(tenant, previous).mode
        if previous_layout == "disabled":
            previous_layout = "user"
        requested_layout = previous_layout if policy.mode == "disabled" else policy.mode
        if any(key[0] == tenant for key in self.buckets) and previous_layout != requested_layout:
            raise ConflictError("migration required")
        if requested_layout == "tenant" and policy.mode != "disabled":
            principals = {principal for (row_tenant, principal) in self.credentials if row_tenant == tenant}
            if len(principals) != 1:
                raise ConflictError("tenant mode requires one principal")
            self.tenant_singletons[tenant] = next(iter(principals))
        elif requested_layout == "user":
            self.tenant_singletons.pop(tenant, None)
        self.policies[tenant] = policy

    async def bind_tenant_singleton(self, tenant, principal, *, quota_bytes):
        del quota_bytes
        principals = {value for (row_tenant, value) in self.credentials if row_tenant == tenant}
        principals.add(principal)
        if principals != {principal} or self.tenant_singletons.get(tenant, principal) != principal:
            raise ConflictError("tenant mode is restricted to one principal")
        self.tenant_singletons[tenant] = principal
        self.policies.setdefault(tenant, StoragePolicy(mode="tenant"))

    async def bind_user_layout(self, tenant, *, quota_bytes):
        del quota_bytes
        if (await self.policy(tenant, StoragePolicy())).mode != "user":
            raise ConflictError("per-user layout unavailable")
        self.tenant_singletons.pop(tenant, None)
        self.policies.setdefault(tenant, StoragePolicy())

    async def bucket(self, tenant, owner):
        return self.buckets.get((tenant, owner))

    async def save_bucket(self, tenant, owner, bucket):
        self.buckets[tenant, owner] = bucket

    async def credential(self, tenant, principal):
        return self.credentials.get((tenant, principal))

    async def reencrypt_if_needed(self, tenant, principal):
        return False

    async def bind_user_identity(self, tenant, principal):
        return None

    async def save_credential(self, tenant, principal, owner, value):
        provider_state = value.get("provider_state", "INACTIVE")
        predecessor = value.get("previous_access_key_resource_id")
        enabled = provider_state == "ACTIVE"
        self.credentials[tenant, principal] = {
            **value,
            "owner_key": owner,
            "enabled": enabled,
            "desired_enabled": True,
            "requested_action": "rotate" if predecessor else None if enabled else "enable",
            "requested_at": datetime.now(UTC),
            "revoked_at": None,
            "replacement_access_key_resource_id": None,
            "previous_access_key_resource_id": predecessor,
            "rotation_started_at": None,
            "disclosure_consumed_at": None,
            "policy_suspension_requested": False,
            "current_action_id": None,
            "provider_ownership_verified": bool(value.get("provider_ownership_verified", False)),
            "version": 0,
        }

    async def record_provider_ownership(self, tenant, principal, *, verified, expected_version):
        credential = self.credentials[tenant, principal]
        if credential["version"] != expected_version:
            raise ConflictError("CAS")
        credential["provider_ownership_verified"] = verified
        credential["version"] += 1

    async def request_enabled(self, tenant, principal, enabled):
        credential = self.credentials[tenant, principal]
        if enabled and credential["revoked_at"] is not None:
            raise ConflictError("revoked")
        credential["desired_enabled"] = enabled
        if credential["requested_action"] is None and credential["enabled"] != enabled:
            credential["requested_action"] = "enable" if enabled else "disable"
        credential["version"] += 1

    async def complete_action(self, tenant, principal, *, expected_action, enabled, revoked=False, expected_version):
        credential = self.credentials[tenant, principal]
        if credential["version"] != expected_version or credential["requested_action"] != expected_action:
            raise ConflictError("CAS")
        credential.update(
            enabled=enabled,
            desired_enabled=credential["desired_enabled"] if expected_action == "suspend" else enabled,
            requested_action=None,
            current_action_id=None,
            revoked_at=None if enabled else datetime.now(UTC) if revoked else credential["revoked_at"],
            version=credential["version"] + 1,
        )

    async def record_inactive_preserving_rotation(self, tenant, principal, *, expected_version):
        credential = self.credentials[tenant, principal]
        if (
            credential["version"] != expected_version
            or credential["requested_action"] != "rotate"
            or credential["desired_enabled"]
        ):
            raise ConflictError("CAS")
        credential["enabled"] = False
        credential["version"] += 1

    async def request_suspended(self, tenant, principal):
        credential = self.credentials[tenant, principal]
        if credential["requested_action"] is None and credential["enabled"]:
            credential["requested_action"] = "suspend"
            credential["requested_at"] = datetime.now(UTC)
            credential["version"] += 1

    async def request_action(
        self,
        tenant,
        principal,
        action,
        *,
        token_id=None,
        operator_session_id=None,
        idempotency_key=None,
    ):
        del token_id, operator_session_id, idempotency_key
        credential = self.credentials[tenant, principal]
        if credential["requested_action"] is not None:
            raise ConflictError("pending")
        credential["requested_action"] = action
        credential["desired_enabled"] = action == "rotate"
        credential["requested_at"] = datetime.now(UTC)
        credential["rotation_started_at"] = datetime.now(UTC) if action == "rotate" else None
        credential["current_action_id"] = uuid4()
        credential["version"] += 1
        return credential["current_action_id"]

    async def request_rotation_if_due(self, tenant, principal, cutoff):
        credential = self.credentials[tenant, principal]
        if (
            credential["requested_action"] is None
            and credential["enabled"]
            and credential["desired_enabled"]
            and credential["revoked_at"] is None
            and credential["expires_at"] <= cutoff
        ):
            credential["requested_action"] = "rotate"
            credential["requested_at"] = datetime.now(UTC)
            credential["rotation_started_at"] = datetime.now(UTC)
            credential["version"] += 1
            return True
        return False

    async def request_rotation_for_provider_state(self, tenant, principal):
        credential = self.credentials[tenant, principal]
        if (
            credential["requested_action"] is None
            and credential["enabled"]
            and credential["desired_enabled"]
            and credential["revoked_at"] is None
        ):
            credential["requested_action"] = "rotate"
            credential["requested_at"] = datetime.now(UTC)
            credential["rotation_started_at"] = datetime.now(UTC)
            credential["version"] += 1
            return True
        return False

    async def stage_replacement(self, tenant, principal, value, *, expected_version):
        credential = self.credentials[tenant, principal]
        if credential["version"] != expected_version or credential["requested_action"] != "rotate":
            raise ConflictError("CAS")
        credential["replacement_access_key_resource_id"] = value["access_key_resource_id"]
        credential["replacement"] = value
        credential["version"] += 1

    async def promote_replacement(self, tenant, principal, *, expected_version):
        credential = self.credentials[tenant, principal]
        if credential["version"] != expected_version or not credential["replacement_access_key_resource_id"]:
            raise ConflictError("CAS")
        value = credential.pop("replacement")
        previous = credential["access_key_resource_id"]
        credential.update(
            **value,
            enabled=True,
            revoked_at=None,
            disclosure_consumed_at=None,
            replacement_access_key_resource_id=None,
            previous_access_key_resource_id=previous,
            version=credential["version"] + 1,
        )

    async def complete_rotation(self, tenant, principal, *, expected_version):
        credential = self.credentials[tenant, principal]
        if credential["version"] != expected_version or not credential["previous_access_key_resource_id"]:
            raise ConflictError("CAS")
        credential["previous_access_key_resource_id"] = None
        credential["rotation_started_at"] = None
        credential["requested_action"] = None if credential["desired_enabled"] else "disable"
        credential["current_action_id"] = None
        credential["version"] += 1

    async def request_tenant_suspension(self, tenant):
        for (row_tenant, _), credential in self.credentials.items():
            if row_tenant == tenant:
                credential["policy_suspension_requested"] = True
                if credential["requested_action"] not in {"rotate", "revoke"} and credential["enabled"]:
                    credential["requested_action"] = "suspend"
                credential["version"] += 1

    async def complete_policy_suspension(self, tenant, principal, *, expected_version):
        credential = self.credentials[tenant, principal]
        if credential["version"] != expected_version or not credential["policy_suspension_requested"]:
            raise ConflictError("CAS")
        credential["enabled"] = False
        credential["policy_suspension_requested"] = False
        if credential["requested_action"] == "suspend":
            credential["requested_action"] = None
        credential["version"] += 1

    async def request_tenant_resume(self, tenant):
        for (row_tenant, _), credential in self.credentials.items():
            if row_tenant == tenant:
                credential["policy_suspension_requested"] = False
                if (
                    credential["desired_enabled"]
                    and not credential["enabled"]
                    and credential["revoked_at"] is None
                    and credential["requested_action"] not in {"rotate", "revoke"}
                ):
                    credential["requested_action"] = "enable"
                credential["version"] += 1

    async def wait_tenant_suspended(self, tenant, *, timeout):
        async with asyncio.timeout(timeout):
            while any(
                row_tenant == tenant and (row["enabled"] or row["policy_suspension_requested"])
                for (row_tenant, _), row in self.credentials.items()
            ):
                await asyncio.sleep(0.01)

    async def wait_tenant_resumed(self, tenant, *, timeout):
        async with asyncio.timeout(timeout):
            while any(
                row_tenant == tenant
                and row["desired_enabled"]
                and row["revoked_at"] is None
                and (not row["enabled"] or row["requested_action"] is not None)
                for (row_tenant, _), row in self.credentials.items()
            ):
                await asyncio.sleep(0.01)

    async def wait_action(self, action_id, *, timeout):
        async with asyncio.timeout(timeout):
            while any(row["current_action_id"] == action_id for row in self.credentials.values()):
                await asyncio.sleep(0.01)

    async def wait_enabled(self, tenant, principal, *, enabled, timeout):
        async with asyncio.timeout(timeout):
            while True:
                row = self.credentials.get((tenant, principal))
                if row is None or (
                    row["enabled"] == enabled
                    and row["desired_enabled"] == enabled
                    and (row["requested_action"] is None or (not enabled and row["requested_action"] == "rotate"))
                ):
                    return
                await asyncio.sleep(0.01)

    async def pending_principals(self):
        return [
            key
            for key, value in self.credentials.items()
            if value["requested_action"] is not None
            or value["policy_suspension_requested"]
            or not value["enabled"]
            or value["revoked_at"] is not None
        ]

    async def view(self, tenant, principal, policy):
        credential = await self.credential(tenant, principal)
        if credential:
            bucket = self.buckets[tenant, credential["owner_key"]]
            return UserStorage(
                state=(
                    "disabled"
                    if policy.mode == "disabled"
                    else "revoked"
                    if credential["revoked_at"]
                    else "ready"
                    if credential["enabled"]
                    else "disabled"
                ),
                mode=policy.mode,
                quota_bytes=bucket["quota_bytes"],
                bucket_name=bucket["bucket_name"],
                endpoint=bucket["endpoint"],
                region=bucket["region"],
                access_key_id=credential["access_key_id"],
                expires_at=credential["expires_at"],
            )
        return UserStorage(state="disabled" if policy.mode == "disabled" else "pending", **policy.model_dump())

    async def disclose(self, tenant, principal, *, actor, token_id):
        del actor, token_id
        credential = self.credentials[tenant, principal]
        if credential["disclosure_consumed_at"] is not None:
            raise ConflictError("already consumed")
        credential["disclosure_consumed_at"] = datetime.now(UTC)
        bucket = self.buckets[tenant, credential["owner_key"]]
        return StorageCredentials(
            bucket_name=bucket["bucket_name"],
            endpoint=bucket["endpoint"],
            region=bucket["region"],
            access_key_id=credential["access_key_id"],
            secret_access_key=credential["secret_access_key"],
            expires_at=credential["expires_at"],
        )


class Provider:
    def __init__(self):
        self.bucket_calls, self.key_calls, self.enabled_calls, self.access_calls = [], [], [], []
        self.states = {}
        self.accounts = {}
        self.owned = set()
        self.rotation_count = 0

    def bucket_name(self, tenant, owner):
        return f"bucket-{tenant}-{owner}"

    async def ensure_bucket(self, tenant, owner, quota, *, existing=None):
        self.bucket_calls.append((tenant, owner, quota))
        if existing is not None:
            return {**existing, "quota_bytes": quota}
        return dict(
            bucket_id=f"b-{tenant}-{owner}",
            bucket_name=self.bucket_name(tenant, owner),
            group_id=f"g-{tenant}-{owner}",
            endpoint="https://storage.example.test",
            region="test",
            quota_bytes=quota,
        )

    async def ensure_credentials(self, tenant, principal, group):
        self.key_calls.append((tenant, principal, group))
        key = f"key-{tenant}-{principal}"
        self.states[key] = "INACTIVE"
        account = f"sa-{tenant}-{principal}"
        self.accounts[key] = account
        self.owned.add(key)
        return dict(
            service_account_id=account,
            access_key_resource_id=key,
            access_key_id=f"aws-{tenant}-{principal}",
            secret_access_key=f"secret-{tenant}-{principal}",
            expires_at=datetime.now(UTC) + timedelta(days=90),
            provider_state="INACTIVE",
            provider_ownership_verified=True,
        )

    async def set_enabled(self, key, enabled):
        self.enabled_calls.append((key, enabled))
        self.states[key] = "ACTIVE" if enabled else "INACTIVE"

    async def key_state(self, key):
        return self.states.get(key, "INACTIVE")

    async def ensure_identity_access(self, group, account):
        self.access_calls.append((group, account))

    async def reconcile_key_inventory(self, tenant, principal, credential, *, effective_enabled):
        del tenant, principal
        account = credential["service_account_id"]
        current = credential["access_key_resource_id"]
        current_owned = current in self.owned and self.accounts.get(current) == account
        for key, key_account in list(self.accounts.items()):
            if key_account != account:
                continue
            if key != current or not current_owned or not effective_enabled:
                if self.states.get(key) == "ACTIVE":
                    await self.set_enabled(key, False)
        return current_owned

    async def prepare_rotation(self, tenant, principal, group, previous):
        del group
        self.key_calls.append((tenant, principal, "rotation"))
        self.rotation_count += 1
        suffix = "" if self.rotation_count == 1 else f"-{self.rotation_count}"
        key = f"rotated-key-{tenant}-{principal}{suffix}"
        self.states[key] = "INACTIVE"
        self.accounts[key] = previous["service_account_id"]
        self.owned.add(key)
        return dict(
            service_account_id=previous["service_account_id"],
            access_key_resource_id=key,
            access_key_id=f"rotated-aws-{tenant}-{principal}",
            secret_access_key=f"rotated-secret-{tenant}-{principal}",
            expires_at=datetime.now(UTC) + timedelta(days=90),
            provider_state="INACTIVE",
            provider_ownership_verified=True,
        )

    async def close(self):
        pass


@pytest.fixture
def env():
    repository, provider = Repository(), Provider()
    users = SimpleNamespace(list=AsyncMock(return_value=[]))
    service = UserStorageService(repository, provider, users, excluded_tenants=("stockholm",))
    return SimpleNamespace(repository=repository, provider=provider, service=service, users=users)


async def configure_with_reconcile(env, policy, *owners):
    transition = asyncio.create_task(env.service.configure("customer-a", policy))
    await asyncio.sleep(0)
    for configured_owner in owners or (user(),):
        await env.service.ensure(configured_owner)
    return await transition


async def test_default_private_buckets_separate_user_keys_concurrent_and_idempotent(env):
    await asyncio.gather(*(env.service.ensure(user(principal=p)) for p in ["alice", "bob", "alice", "bob"]))
    assert len(env.provider.bucket_calls) == 4  # every pass revalidates provider policy drift
    assert len(env.provider.key_calls) == 2
    assert {call[2] for call in env.provider.key_calls} == {"g-customer-a-alice", "g-customer-a-bob"}
    a = await env.service.view("customer-a", "alice")
    b = await env.service.view("customer-a", "bob")
    assert a.bucket_name != b.bucket_name
    assert a.access_key_id != b.access_key_id
    assert "secret_access_key" not in a.model_dump()


async def test_private_buckets_are_isolated_and_tenants_do_not_collide(env):
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
    await env.repository.request_enabled("customer-a", "alice", True)
    await env.service.ensure(user())
    assert env.provider.enabled_calls[-1] == ("key-customer-a-alice", True)
    assert len(env.provider.key_calls) == 1


async def test_explicit_revoke_persists_and_rotation_replaces_the_key(env):
    await env.service.ensure(user())
    await env.repository.request_action("customer-a", "alice", "revoke")
    await env.service.ensure(user())
    assert (await env.service.view("customer-a", "alice")).state == "revoked"
    calls = list(env.provider.enabled_calls)
    await env.service.ensure(user())
    assert env.provider.enabled_calls == calls

    await env.repository.request_action("customer-a", "alice", "rotate")
    await env.service.ensure(user())
    rotated = await env.repository.credential("customer-a", "alice")
    assert rotated["access_key_resource_id"] == "rotated-key-customer-a-alice"
    assert rotated["requested_action"] is None
    assert (await env.service.view("customer-a", "alice")).state == "ready"


async def test_near_expiry_and_expired_credentials_rotate_automatically_with_continuity(env):
    await env.service.ensure(user())
    old = env.repository.credentials["customer-a", "alice"]
    old_id = old["access_key_resource_id"]
    old["expires_at"] = datetime.now(UTC) + timedelta(days=2)

    await env.service.ensure(user())

    current = env.repository.credentials["customer-a", "alice"]
    assert current["access_key_resource_id"] != old_id
    assert env.provider.states[old_id] == "INACTIVE"
    assert env.provider.states[current["access_key_resource_id"]] == "ACTIVE"
    assert sum(state == "ACTIVE" for state in env.provider.states.values()) == 1
    assert current["expires_at"] > datetime.now(UTC) + timedelta(days=14)

    expired_id = current["access_key_resource_id"]
    current["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    env.provider.states[expired_id] = "EXPIRED"
    await env.service.ensure(user())
    recovered = env.repository.credentials["customer-a", "alice"]
    assert recovered["access_key_resource_id"] != expired_id
    assert env.provider.states[recovered["access_key_resource_id"]] == "ACTIVE"


async def test_rotation_stage_db_failure_preserves_old_and_retry_completes(env):
    await env.service.ensure(user())
    credential = env.repository.credentials["customer-a", "alice"]
    old_id = credential["access_key_resource_id"]
    credential["expires_at"] = datetime.now(UTC) + timedelta(days=1)
    original = env.repository.stage_replacement
    env.repository.stage_replacement = AsyncMock(side_effect=RuntimeError("db unavailable"))

    with pytest.raises(RuntimeError, match="db unavailable"):
        await env.service.ensure(user())

    replacement_id = "rotated-key-customer-a-alice"
    assert env.provider.states[old_id] == "ACTIVE"
    assert env.provider.states[replacement_id] == "INACTIVE"
    assert env.repository.credentials["customer-a", "alice"]["access_key_resource_id"] == old_id

    env.repository.stage_replacement = original
    await env.service.ensure(user())
    assert env.provider.states[old_id] == "INACTIVE"
    assert env.provider.states[replacement_id] == "INACTIVE"
    assert env.provider.states[env.repository.credentials["customer-a", "alice"]["access_key_resource_id"]] == "ACTIVE"


async def test_rotation_promotion_db_failure_compensates_and_retries(env):
    await env.service.ensure(user())
    credential = env.repository.credentials["customer-a", "alice"]
    old_id = credential["access_key_resource_id"]
    credential["expires_at"] = datetime.now(UTC) + timedelta(days=1)
    original = env.repository.promote_replacement
    env.repository.promote_replacement = AsyncMock(side_effect=RuntimeError("db promotion unavailable"))

    with pytest.raises(RuntimeError, match="db promotion unavailable"):
        await env.service.ensure(user())

    replacement_id = "rotated-key-customer-a-alice"
    assert env.provider.states[old_id] == "ACTIVE"
    assert env.provider.states[replacement_id] == "INACTIVE"
    assert sum(state == "ACTIVE" for state in env.provider.states.values()) == 1

    env.repository.promote_replacement = original
    await env.service.ensure(user())
    assert env.provider.states[old_id] == "INACTIVE"
    assert env.provider.states[replacement_id] == "ACTIVE"


async def test_rotation_activation_noop_never_promotes_or_disables_predecessor(env):
    await env.service.ensure(user())
    credential = env.repository.credentials["customer-a", "alice"]
    old_id = credential["access_key_resource_id"]
    credential["expires_at"] = datetime.now(UTC) + timedelta(days=1)
    original = env.provider.set_enabled

    async def ignore_replacement_activation(key, enabled):
        if key.startswith("rotated-key-") and enabled:
            env.provider.enabled_calls.append((key, enabled))
            return
        await original(key, enabled)

    env.provider.set_enabled = ignore_replacement_activation
    with pytest.raises(RuntimeError, match="did not reach ACTIVE"):
        await env.service.ensure(user())

    replacement_id = "rotated-key-customer-a-alice"
    current = env.repository.credentials["customer-a", "alice"]
    assert current["access_key_resource_id"] == old_id
    assert current["replacement_access_key_resource_id"] == replacement_id
    assert current["requested_action"] == "rotate"
    assert env.provider.states[old_id] == "ACTIVE"
    assert env.provider.states[replacement_id] == "INACTIVE"

    env.provider.set_enabled = original
    await env.service.ensure(user())
    assert current["access_key_resource_id"] == replacement_id
    assert env.provider.states[old_id] == "INACTIVE"
    assert env.provider.states[replacement_id] == "ACTIVE"


async def test_rotation_crash_after_promotion_retires_predecessor_on_retry(env):
    await env.service.ensure(user())
    credential = env.repository.credentials["customer-a", "alice"]
    old_id = credential["access_key_resource_id"]
    credential["expires_at"] = datetime.now(UTC) + timedelta(days=1)
    original = env.provider.set_enabled
    failed = False

    async def interrupt_predecessor(key, enabled):
        nonlocal failed
        if key == old_id and not enabled and not failed:
            failed = True
            raise RuntimeError("provider predecessor disable unavailable")
        await original(key, enabled)

    env.provider.set_enabled = interrupt_predecessor
    with pytest.raises(RuntimeError, match="predecessor disable unavailable"):
        await env.service.ensure(user())

    promoted = env.repository.credentials["customer-a", "alice"]
    assert promoted["access_key_resource_id"] != old_id
    assert promoted["previous_access_key_resource_id"] == old_id
    assert env.provider.states[old_id] == "ACTIVE"
    assert env.provider.states[promoted["access_key_resource_id"]] == "ACTIVE"

    await env.service.ensure(user())
    assert promoted["previous_access_key_resource_id"] is None
    assert env.provider.states[old_id] == "INACTIVE"
    assert sum(state == "ACTIVE" for state in env.provider.states.values()) == 1


async def test_initial_enable_db_failure_compensates_and_retries(env):
    original = env.repository.complete_action
    env.repository.complete_action = AsyncMock(side_effect=RuntimeError("db enable unavailable"))

    with pytest.raises(RuntimeError, match="db enable unavailable"):
        await env.service.ensure(user())

    credential = env.repository.credentials["customer-a", "alice"]
    key_id = credential["access_key_resource_id"]
    assert credential["requested_action"] == "enable"
    assert env.provider.states[key_id] == "INACTIVE"

    env.repository.complete_action = original
    await env.service.ensure(user())
    assert credential["requested_action"] is None
    assert env.provider.states[key_id] == "ACTIVE"


async def test_provider_inactive_or_expired_state_is_repaired_by_rotation(env):
    await env.service.ensure(user())
    credential = env.repository.credentials["customer-a", "alice"]
    old_id = credential["access_key_resource_id"]
    credential["expires_at"] = datetime.now(UTC) + timedelta(days=60)
    env.provider.states[old_id] = "EXPIRED"

    await env.service.ensure(user())

    assert credential["access_key_resource_id"] != old_id
    assert env.provider.states[credential["access_key_resource_id"]] == "ACTIVE"
    assert env.provider.states[old_id] == "EXPIRED"


async def test_provider_transition_must_reach_the_requested_state(env):
    await env.service.ensure(user())
    credential = env.repository.credentials["customer-a", "alice"]
    key_id = credential["access_key_resource_id"]
    env.provider.states[key_id] = "INACTIVE"
    env.provider.set_enabled = AsyncMock()

    with pytest.raises(RuntimeError, match="did not reach ACTIVE"):
        await env.service._set_provider_state(key_id, True)

    env.provider.states[key_id] = "ACTIVE"
    with pytest.raises(RuntimeError, match="fail-closed"):
        await env.service._set_provider_state(key_id, False)


async def test_enable_requests_do_not_overwrite_pending_rotate_or_revoke(env):
    await env.service.ensure(user())
    await env.repository.request_action("customer-a", "alice", "rotate")
    await env.repository.request_enabled("customer-a", "alice", False)
    credential = env.repository.credentials["customer-a", "alice"]
    assert credential["requested_action"] == "rotate"
    assert credential["desired_enabled"] is False

    credential["requested_action"] = None
    credential["desired_enabled"] = True
    await env.repository.request_action("customer-a", "alice", "revoke")
    await env.repository.request_enabled("customer-a", "alice", True)
    assert credential["requested_action"] == "revoke"


async def test_disabled_policy_fail_closes_pending_rotation_without_reactivating(env):
    await env.service.ensure(user())
    credential = env.repository.credentials["customer-a", "alice"]
    old_id = credential["access_key_resource_id"]
    await env.repository.request_action("customer-a", "alice", "rotate")
    await configure_with_reconcile(env, StoragePolicy(mode="disabled"))

    await env.service.ensure(user())

    assert credential["requested_action"] == "rotate"
    assert credential["access_key_resource_id"] == old_id
    assert env.provider.states[old_id] == "INACTIVE"
    assert env.provider.rotation_count == 0


async def test_disabled_user_preserves_pending_rotation_without_reactivating(env):
    await env.service.ensure(user())
    credential = env.repository.credentials["customer-a", "alice"]
    old_id = credential["access_key_resource_id"]
    await env.repository.request_action("customer-a", "alice", "rotate")
    enabled_call_count = len(env.provider.enabled_calls)

    await env.service.ensure(user(enabled=False))

    assert credential["requested_action"] == "rotate"
    assert credential["desired_enabled"] is False
    assert credential["access_key_resource_id"] == old_id
    assert env.provider.states[old_id] == "INACTIVE"
    assert env.provider.rotation_count == 0
    assert (old_id, True) not in env.provider.enabled_calls[enabled_call_count:]


async def test_disabled_and_revoked_users_are_never_reactivated_by_expiry(env):
    await env.service.ensure(user())
    await env.service.ensure(user(enabled=False))
    credential = env.repository.credentials["customer-a", "alice"]
    credential["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    calls = list(env.provider.key_calls)
    await env.service.ensure(user(enabled=False))
    assert env.provider.key_calls == calls
    assert env.provider.states[credential["access_key_resource_id"]] == "INACTIVE"

    await env.repository.request_action("customer-a", "alice", "revoke")
    await env.service.ensure(user(enabled=False))
    calls = list(env.provider.key_calls)
    await env.service.ensure(user())
    assert env.provider.key_calls == calls
    assert env.provider.states[credential["access_key_resource_id"]] == "INACTIVE"
    with pytest.raises(ConflictError, match="revoked"):
        await env.repository.request_enabled("customer-a", "alice", True)


@pytest.mark.parametrize("off_state", ["user-disabled", "revoked", "tenant-disabled", "excluded"])
async def test_provider_reactivation_drift_is_repaired_for_every_effective_off_state(env, off_state):
    await env.service.ensure(user())
    credential = env.repository.credentials["customer-a", "alice"]
    key_id = credential["access_key_resource_id"]
    reconcile_user = user()

    if off_state == "user-disabled":
        reconcile_user = user(enabled=False)
        await env.service.ensure(reconcile_user)
    elif off_state == "revoked":
        await env.repository.request_action("customer-a", "alice", "revoke")
        await env.service.ensure(reconcile_user)
    elif off_state == "tenant-disabled":
        await configure_with_reconcile(env, StoragePolicy(mode="disabled"))
    else:
        env.service.excluded_tenants = frozenset({"customer-a"})
        await env.service.ensure(reconcile_user)

    assert env.provider.states[key_id] == "INACTIVE"
    env.provider.states[key_id] = "ACTIVE"  # external provider drift
    calls = len(env.provider.enabled_calls)
    await env.service.ensure(reconcile_user)
    assert env.provider.states[key_id] == "INACTIVE"
    assert env.provider.enabled_calls[calls:] == [(key_id, False)]


async def test_disabled_owner_quarantines_differently_named_active_account_key(env):
    await env.service.ensure(user())
    credential = env.repository.credentials["customer-a", "alice"]
    account = credential["service_account_id"]
    env.provider.states["manually-created-key"] = "ACTIVE"
    env.provider.accounts["manually-created-key"] = account

    await env.service.ensure(user(enabled=False))

    assert env.provider.states[credential["access_key_resource_id"]] == "INACTIVE"
    assert env.provider.states["manually-created-key"] == "INACTIVE"


async def test_policy_disable_and_reactivate_preserves_layout_and_identity(env):
    await env.service.ensure(user())
    credential = env.repository.credentials["customer-a", "alice"]
    key_id = credential["access_key_resource_id"]
    bucket_id = env.repository.buckets["customer-a", "alice"]["bucket_id"]

    await configure_with_reconcile(env, StoragePolicy(mode="disabled"))
    await env.service.ensure(user())
    assert env.provider.states[key_id] == "INACTIVE"
    assert credential["desired_enabled"] is True

    await configure_with_reconcile(env, StoragePolicy(mode="user"))
    await env.service.ensure(user())
    assert env.provider.states[key_id] == "ACTIVE"
    assert env.repository.buckets["customer-a", "alice"]["bucket_id"] == bucket_id


async def test_tenant_disable_provider_failure_returns_failure_with_durable_pending_state(env):
    await env.service.ensure(user())
    env.service.action_timeout_seconds = 0.05
    env.provider.set_enabled = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    transition = asyncio.create_task(env.service.configure("customer-a", StoragePolicy(mode="disabled")))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await env.service.ensure(user())
    with pytest.raises(RuntimeError, match="did not complete"):
        await transition
    credential = env.repository.credentials["customer-a", "alice"]
    assert (await env.service.policy("customer-a")).mode == "disabled"
    assert credential["policy_suspension_requested"]
    assert credential["requested_action"] == "suspend"


async def test_disclosure_is_consumable_and_rotation_issues_one_new_claim(env):
    await env.service.ensure(user())
    first = await env.repository.disclose("customer-a", "alice", actor="alice", token_id=None)
    assert first.secret_access_key
    with pytest.raises(ConflictError, match="consumed"):
        await env.repository.disclose("customer-a", "alice", actor="alice", token_id=None)

    await env.repository.request_action("customer-a", "alice", "rotate")
    await env.service.ensure(user())
    second = await env.repository.disclose("customer-a", "alice", actor="alice", token_id=None)
    assert second.access_key_id != first.access_key_id
    with pytest.raises(ConflictError, match="consumed"):
        await env.repository.disclose("customer-a", "alice", actor="alice", token_id=None)


async def test_quota_update_preserves_identity_and_mode_change_requires_migration(env):
    await env.service.ensure(user())
    await env.service.configure("customer-a", StoragePolicy(quota_bytes=10_000_000_000))
    await env.service.ensure(user())
    assert (await env.service.view("customer-a", "alice")).quota_bytes == 10_000_000_000
    assert len(env.provider.key_calls) == 1
    with pytest.raises(ConflictError):
        await env.service.configure("customer-a", StoragePolicy(mode="tenant"))


async def test_naming_change_does_not_replace_existing_bucket_or_credentials(env):
    await env.service.ensure(user())
    old = dict(env.repository.buckets["customer-a", "alice"])
    env.provider.bucket_name = lambda tenant, owner: "fs2-customer-a-identity"
    await env.service.ensure(user())
    current = env.repository.buckets["customer-a", "alice"]
    assert current["bucket_id"] == old["bucket_id"]
    assert current["group_id"] == old["group_id"]
    assert current["bucket_name"] == old["bucket_name"]
    assert len(env.provider.bucket_calls) == 2
    assert len(env.provider.key_calls) == 1


async def test_failed_key_creation_retries_saved_bucket_and_other_users_continue(env):
    original = env.provider.ensure_credentials
    env.provider.ensure_credentials = AsyncMock(side_effect=[RuntimeError("provider unavailable")])
    env.users.list.return_value = [user()]
    assert (await env.service.reconcile_once())["failed"] == 1
    env.provider.ensure_credentials = original
    assert (await env.service.reconcile_once())["ready"] == 1
    assert len(env.provider.bucket_calls) == 2


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
    envelope = Ciphertext(*args[8:11])
    assert cipher.decrypt(envelope, aad=repository.aad("customer-a", "alice")).decode() == value["secret_access_key"]
    with pytest.raises(InvalidTag):
        cipher.decrypt(envelope, aad=repository.aad("customer-b", "alice"))


def test_storage_aad_supports_dual_read_current_write_and_rollback():
    aad = PayloadCipher.customer_storage_aad("customer-a", "alice")
    generation_one = PayloadCipher(active_key_id="storage-v1", keys={"storage-v1": b"1" * 32})
    old = generation_one.encrypt(b"credential", aad=aad)

    rotating = PayloadCipher(
        active_key_id="storage-v2",
        keys={"storage-v1": b"1" * 32, "storage-v2": b"2" * 32},
    )
    assert rotating.decrypt(old, aad=aad) == b"credential"
    current = rotating.encrypt(rotating.decrypt(old, aad=aad), aad=aad)
    assert current.key_id == "storage-v2"

    rollback = PayloadCipher(
        active_key_id="storage-v1",
        keys={"storage-v1": b"1" * 32, "storage-v2": b"2" * 32},
    )
    assert rollback.decrypt(current, aad=aad) == b"credential"
    assert rollback.encrypt(b"rollback-write", aad=aad).key_id == "storage-v1"
    with pytest.raises(InvalidTag):
        rollback.decrypt(
            current,
            aad=PayloadCipher.customer_storage_aad("customer-a", "bob"),
        )
    with pytest.raises(ValueError, match="AAD identities"):
        PayloadCipher.customer_storage_aad("customer-a\0forged", "alice")


async def test_storage_key_usage_keeps_cipher_and_name_generations_separate():
    pool = SimpleNamespace(
        fetch=AsyncMock(
            side_effect=[
                [
                    {"key_id": "storage-v1", "credential_count": 0},
                    {"key_id": "storage-v2", "credential_count": 2},
                ],
                [
                    {"key_id": "legacy-unkeyed-v0", "bucket_count": 2},
                    {"key_id": "storage-name-v2", "bucket_count": 3},
                ],
            ]
        )
    )
    repository = PostgresUserStorageRepository(pool, None)

    assert await repository.storage_key_usage() == {
        "cipher": {"storage-v1": 0, "storage-v2": 2},
        "names": {"legacy-unkeyed-v0": 2, "storage-name-v2": 3},
    }


async def test_tenant_layout_rejects_second_principal_before_cloud_access(env):
    env.service.default = StoragePolicy(mode="tenant")
    await env.service.ensure(user(principal="alice"))
    cloud_calls = (len(env.provider.bucket_calls), len(env.provider.key_calls))

    with pytest.raises(ConflictError, match="one principal"):
        await env.service.ensure(user(principal="bob"))

    assert (len(env.provider.bucket_calls), len(env.provider.key_calls)) == cloud_calls
    assert env.repository.tenant_singletons["customer-a"] == "alice"


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


def test_storage_policy_defaults_to_per_user_isolation():
    assert StoragePolicy().mode == "user"


async def test_credentials_require_explicit_scope_and_commit_redacted_audit(env):
    from fs2_serve.user_storage_routes import user_storage_router

    disclosure = SimpleNamespace(
        disclose_user=AsyncMock(
            return_value=StorageCredentials(
                bucket_name="bucket-a",
                endpoint="https://storage.example.test",
                region="test",
                access_key_id="public-id",
                secret_access_key="sensitive-fixture",
                expires_at=datetime(2026, 12, 1, tzinfo=UTC),
            )
        ),
        disclose_admin=AsyncMock(),
    )
    env.service.policy = AsyncMock(return_value=StoragePolicy())
    users = SimpleNamespace(
        repository=SimpleNamespace(configured=AsyncMock(return_value=None)),
        access=SimpleNamespace(),
    )
    router = user_storage_router(
        service=env.service,
        disclosure=disclosure,
        users=users,
        operator=lambda: None,
        principal=lambda: None,
        envelope=lambda value: value,
        problem_responses={},
    )
    endpoint = next(route.endpoint for route in router.routes if route.path == "/v1/storage/credentials")
    identity = Principal(
        token_id=UUID("00000000-0000-0000-0000-000000000042"),
        token_prefix="pat-test",
        principal_id="alice",
        tenant_id="customer-a",
        scopes=frozenset({Scope.CATALOG_READ}),
        models=frozenset({"*"}),
    )
    with pytest.raises(PermissionError, match="storage.credentials"):
        await endpoint(identity, "Bearer fixture")
    disclosure.disclose_user.assert_not_awaited()

    response = await endpoint(
        identity.model_copy(update={"scopes": frozenset({Scope.STORAGE_CREDENTIALS})}),
        "Bearer fixture",
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    disclosure.disclose_user.assert_awaited_once_with("Bearer fixture")


async def test_public_and_admin_replay_denials_are_audited(env):
    from fs2_serve.user_storage_routes import user_storage_router

    disclosure = SimpleNamespace(
        disclose_user=AsyncMock(side_effect=ConflictError("already consumed")),
        disclose_admin=AsyncMock(side_effect=ConflictError("already consumed")),
    )
    env.service.policy = AsyncMock(return_value=StoragePolicy())
    selected = user()
    users = SimpleNamespace(
        repository=SimpleNamespace(configured=AsyncMock(return_value=None)),
        _get=AsyncMock(return_value=selected),
        access=SimpleNamespace(),
    )
    router = user_storage_router(
        service=env.service,
        disclosure=disclosure,
        users=users,
        operator=lambda: None,
        principal=lambda: None,
        envelope=lambda value: value,
        problem_responses={},
    )
    public = next(route.endpoint for route in router.routes if route.path == "/v1/storage/credentials")
    admin = next(
        route.endpoint for route in router.routes if route.path == "/admin/api/v1/users/{user_id}/storage/credentials"
    )
    identity = Principal(
        token_id=UUID("00000000-0000-0000-0000-000000000042"),
        token_prefix="pat-test",
        principal_id="alice",
        tenant_id="customer-a",
        scopes=frozenset({Scope.STORAGE_CREDENTIALS}),
        models=frozenset({"*"}),
    )
    with pytest.raises(ConflictError, match="consumed"):
        await public(identity, "Bearer fixture")
    disclosure.disclose_user.assert_awaited_once_with("Bearer fixture")

    cookie = "fs2_admin_00000000000000000000000000000042_" + "x" * 32
    with pytest.raises(ConflictError, match="consumed"):
        await admin(selected.id, SimpleNamespace(subject="operator-a"), cookie)
    disclosure.disclose_admin.assert_awaited_once_with(cookie, selected.id)


def test_storage_deployment_contract_isolated_from_public_runtime():
    root = Path(__file__).parents[3]
    helpers = (root / "charts/control-plane/fs2-serve-control-plane/templates/_helpers.tpl").read_text()
    deployment = (
        root / "charts/control-plane/fs2-serve-control-plane/templates/storage-reconciler-deployment.yaml"
    ).read_text()
    network = (root / "charts/control-plane/fs2-serve-control-plane/templates/networkpolicy.yaml").read_text()
    security_boundary = (root / "security/customer-storage-egress-boundary/main.tf").read_text()
    module = (root / "modules/customer-storage-provisioner/main.tf").read_text()
    assert "FS2_USER_STORAGE_RESOURCE_CREDENTIALS_FILE" not in helpers
    assert "FS2_USER_STORAGE_IAM_CREDENTIALS_FILE" not in helpers
    assert "storage-reconciler" in deployment
    assert "resourceCredentialsSecretName" in deployment
    assert "iamCredentialsSecretName" in deployment
    assert 'printf "%s-storage-reconciler"' in deployment
    assert "customerStorage.egressCidrs" in network
    assert 'resource "kubernetes_network_policy_v1" "contract"' in security_boundary
    assert '"app.kubernetes.io/component"             = "storage-reconciler-v2"' in security_boundary
    assert "prevent_destroy = true" in security_boundary
    assert "tls_private_key" not in module
    assert 'role        = "editor"' in module
    assert 'role        = "admin"' in module
