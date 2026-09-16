"""Exercise storage against the real migration, locks and encrypted DB rows."""

import asyncio
import hashlib
import hmac
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from test_user_storage import Provider, user

from fs2_serve.access_models import OperatorPrincipal, OperatorRole, PrincipalKind
from fs2_serve.auth import OPERATOR_SESSION_DIGEST_CONTEXT, AuthenticationError, PepperRing
from fs2_serve.crypto import PayloadCipher
from fs2_serve.postgres import PostgresStore
from fs2_serve.postgresql_release import EXPECTED_MIGRATIONS
from fs2_serve.store import ConflictError
from fs2_serve.user_models import UserPatch
from fs2_serve.user_repository import PostgresUserRepository
from fs2_serve.user_storage import UserStorageService
from fs2_serve.user_storage_disclosure import PostgresStorageDisclosureRepository
from fs2_serve.user_storage_models import StoragePolicy
from fs2_serve.user_storage_repository import PostgresUserStorageRepository
from fs2_serve.users import UserService

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
            for version, _ in EXPECTED_MIGRATIONS[:-2]:
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
            for version, _ in EXPECTED_MIGRATIONS[-2:]:
                await candidate.execute((migration_dir / version).read_text(encoding="utf-8"))

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
            durable = await candidate.fetch(
                """SELECT action.tenant_id,action.principal_id,action.action,action.actor,
                action.status,credential.current_action_id=action.id AS bound
                FROM fs2_storage_actions action JOIN fs2_user_storage credential
                USING(tenant_id,principal_id) ORDER BY principal_id"""
            )
            assert [(row["principal_id"], row["action"], row["status"], row["bound"]) for row in durable] == [
                ("alice", "rotate", "requested", True),
                ("bob", "revoke", "requested", True),
            ]
            assert {row["actor"] for row in durable} == {"storage-reconciler"}
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
    await store.pool.execute(
        """TRUNCATE fs2_storage_disclosure_entitlements,fs2_user_storage,
        fs2_storage_actions,fs2_storage_buckets,fs2_storage_policies RESTART IDENTITY"""
    )
    await store.pool.execute("DELETE FROM fs2_audit_events WHERE target_type='user_storage'")
    await store.pool.execute("DELETE FROM fs2_operator_sessions WHERE created_by='sai08-test'")
    await store.pool.execute("DELETE FROM fs2_operator_principals WHERE created_by='sai08-test'")
    await store.pool.execute("DELETE FROM fs2_tokens WHERE created_by='sai08-test'")
    await store.pool.execute("DELETE FROM fs2_inference_users WHERE principal_id LIKE 'sai08-%'")
    await store.pool.execute(
        """DROP TRIGGER IF EXISTS sai08_fail_storage_audit ON fs2_audit_events;
        DROP FUNCTION IF EXISTS sai08_fail_storage_audit()"""
    )
    try:
        yield store
    finally:
        await store.pool.execute(
            """TRUNCATE fs2_storage_disclosure_entitlements,fs2_user_storage,
            fs2_storage_actions,fs2_storage_buckets,fs2_storage_policies RESTART IDENTITY"""
        )
        await store.pool.execute("DELETE FROM fs2_audit_events WHERE target_type='user_storage'")
        await store.pool.execute("DELETE FROM fs2_operator_sessions WHERE created_by='sai08-test'")
        await store.pool.execute("DELETE FROM fs2_operator_principals WHERE created_by='sai08-test'")
        await store.pool.execute("DELETE FROM fs2_tokens WHERE created_by='sai08-test'")
        await store.pool.execute("DELETE FROM fs2_inference_users WHERE principal_id LIKE 'sai08-%'")
        await store.pool.execute(
            """DROP TRIGGER IF EXISTS sai08_fail_storage_audit ON fs2_audit_events;
            DROP FUNCTION IF EXISTS sai08_fail_storage_audit()"""
        )
        await store.close()


async def issue_storage_token(store, *, tenant: str, principal: str) -> tuple[UUID, str]:
    token_id = uuid4()
    raw = f"fs2_pat_{uuid4().hex}_{'A' * 43}"
    await store.pool.execute(
        """INSERT INTO fs2_tokens(
        id,prefix,pepper_key_id,digest,principal_id,tenant_id,scopes,models,
        max_concurrency,created_by,fingerprint)
        VALUES($1,$2,'pepper-v1','argon-fixture',$3,$4,$5,$6,1,'sai08-test',$7)""",
        token_id,
        raw[:24],
        principal,
        tenant,
        ["storage.credentials"],
        ["*"],
        hashlib.sha256(raw.encode()).hexdigest(),
    )
    return token_id, raw


async def disclosure_pool(database_url: str):
    async def assume_role(connection):
        await connection.execute("SET ROLE fs2_serve_storage_disclosure")

    return await asyncpg.create_pool(database_url, min_size=1, max_size=2, init=assume_role)


async def test_db_bound_one_time_disclosure_blocks_cross_tenant_enumeration_and_forged_audit(storage_database):
    store = storage_database
    users = PostgresUserRepository(store.pool)
    repository = PostgresUserStorageRepository(store.pool, store.cipher)
    provider = Provider()
    service = UserStorageService(repository, provider, users)
    alice = user(tenant="customer-a", principal="sai08-alice")
    bob = user(tenant="customer-b", principal="sai08-bob")
    await users.save(alice, create=True)
    await users.save(bob, create=True)
    await service.ensure(alice)
    await service.ensure(bob)
    assert await store.pool.fetchval("SELECT count(*) FROM fs2_storage_buckets") == 2
    assert await store.pool.fetchval("SELECT count(*) FROM fs2_user_storage") == 2

    storage_cipher = PayloadCipher(
        active_key_id="storage-v1",
        keys={"payload-v1": b"p" * 32, "storage-v1": b"s" * 32},
    )
    rotated_repository = PostgresUserStorageRepository(store.pool, storage_cipher)
    assert await rotated_repository.reencrypt_if_needed("customer-a", "sai08-alice")
    assert not await rotated_repository.reencrypt_if_needed("customer-a", "sai08-alice")
    alice_token_id, alice_raw = await issue_storage_token(store, tenant="customer-a", principal="sai08-alice")
    _, bob_raw = await issue_storage_token(store, tenant="customer-b", principal="sai08-bob")
    pepper = b"x" * 32
    operator_id = uuid4()
    session_id = uuid4()
    cookie = f"fs2_admin_{session_id.hex}_{'s' * 32}"
    session_digest = hmac.new(
        pepper,
        OPERATOR_SESSION_DIGEST_CONTEXT + cookie.encode(),
        hashlib.sha256,
    ).hexdigest()
    await store.pool.execute(
        """INSERT INTO fs2_operator_principals(
        id,subject,display_name,kind,role,tenant_id,enabled,created_by)
        VALUES($1,'sai08-operator','SAI-08 operator','human','admin','customer-a',true,'sai08-test')""",
        operator_id,
    )
    await store.pool.execute(
        """INSERT INTO fs2_operator_sessions(
        id,principal_id,pepper_key_id,digest,expires_at,created_by)
        VALUES($1,$2,'pepper-v1',$3,clock_timestamp()+interval '1 hour','sai08-test')""",
        session_id,
        operator_id,
        session_digest,
    )

    pool = await disclosure_pool(os.environ["FS2_TEST_DATABASE_URL"])
    disclosure = PostgresStorageDisclosureRepository(
        pool,
        storage_cipher,
        PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": pepper}),
    )
    try:
        revealed = await disclosure.disclose_user(alice_raw)
        assert revealed.secret_access_key == "secret-customer-a-sai08-alice"
        assert "bob" not in revealed.access_key_id
        with pytest.raises(ConflictError, match="already consumed"):
            await disclosure.disclose_user(alice_raw)
        with pytest.raises(AuthenticationError):
            await disclosure.disclose_user(f"fs2_pat_{uuid4().hex}_{'B' * 43}")
        with pytest.raises(AuthenticationError):
            await disclosure.disclose_admin(cookie, bob.id)
        with pytest.raises(AuthenticationError):
            await disclosure.disclose_admin(cookie, uuid4())
        with pytest.raises(ConflictError, match="already consumed"):
            await disclosure.disclose_admin(cookie, alice.id)
        bob_revealed = await disclosure.disclose_user(bob_raw)
        assert bob_revealed.secret_access_key == "secret-customer-b-sai08-bob"
    finally:
        await pool.close()

    # The gateway DB role cannot select envelopes or call the entitlement
    # consumer, and its repository has no storage cipher even if Python code
    # tries to instantiate the old broad boundary.
    async with store.pool.acquire() as connection:
        await connection.execute("SET ROLE fs2_serve_runtime")
        try:
            assert not await connection.fetchval(
                "SELECT has_function_privilege(current_user,'fs2_consume_storage_disclosure(uuid)','EXECUTE')"
            )
            with pytest.raises(Exception, match="permission denied"):
                await connection.fetch("SELECT secret_ciphertext FROM fs2_user_storage")
        finally:
            await connection.execute("RESET ROLE")
    with pytest.raises(RuntimeError, match="encryption material"):
        PostgresUserStorageRepository(store.pool, None)._cipher()

    audit_rows = await store.pool.fetch(
        """SELECT actor,tenant_id,token_id,action,target_id,outcome
        FROM fs2_audit_events WHERE target_type='user_storage'
        AND action LIKE 'storage.credentials.disclose%' ORDER BY id"""
    )
    assert [(row["tenant_id"], row["target_id"], row["outcome"]) for row in audit_rows] == [
        ("customer-a", "sai08-alice", "succeeded"),
        ("customer-a", "sai08-alice", "denied"),
        ("customer-a", "sai08-alice", "denied"),
        ("customer-b", "sai08-bob", "succeeded"),
    ]
    assert (audit_rows[0]["actor"], audit_rows[0]["token_id"]) == ("sai08-alice", alice_token_id)
    assert (audit_rows[2]["actor"], audit_rows[2]["token_id"]) == ("sai08-operator", None)
    assert all(row["actor"] == row["target_id"] for row in (audit_rows[0], audit_rows[1], audit_rows[3]))


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


async def test_tenant_layout_is_db_bound_to_one_principal_at_every_write_path(storage_database):
    store = storage_database
    users = PostgresUserRepository(store.pool)
    repository = PostgresUserStorageRepository(store.pool, store.cipher)
    alice = user(tenant="sai08-singleton", principal="sai08-singleton-alice")
    bob = user(tenant="sai08-singleton", principal="sai08-singleton-bob")
    await users.save(alice, create=True)
    await repository.set_policy(
        alice.tenant_id,
        StoragePolicy(mode="tenant"),
        StoragePolicy(),
    )
    assert await store.pool.fetchval(
        "SELECT singleton_principal_id FROM fs2_storage_policies WHERE tenant_id=$1",
        alice.tenant_id,
    ) == alice.principal_id

    with pytest.raises(asyncpg.CheckViolationError, match="another principal"):
        await users.save(bob, create=True)
    with pytest.raises(ConflictError, match="one immutable principal"):
        await repository.bind_tenant_singleton(
            alice.tenant_id,
            bob.principal_id,
            quota_bytes=5_000_000_000,
        )

    await repository.set_policy(
        alice.tenant_id,
        StoragePolicy(mode="disabled"),
        StoragePolicy(),
    )
    await users.save(bob, create=True)
    with pytest.raises(ConflictError, match="exactly one"):
        await repository.set_policy(
            alice.tenant_id,
            StoragePolicy(mode="tenant"),
            StoragePolicy(),
        )


async def wait_for_value(reader, expected, *, timeout: float = 2.0):
    async with asyncio.timeout(timeout):
        while (value := await reader()) != expected:
            await asyncio.sleep(0.01)
    return value


def operator(tenant: str = "customer-a") -> OperatorPrincipal:
    now = datetime.now(UTC)
    return OperatorPrincipal(
        id=uuid4(),
        subject="sai08-admin",
        display_name="SAI-08 administrator",
        kind=PrincipalKind.HUMAN,
        role=OperatorRole.ADMIN,
        tenant_id=tenant,
        enabled=True,
        created_at=now,
        created_by="sai08-test",
        updated_at=now,
    )


async def test_real_user_service_disable_then_enable_converges_without_split_brain(storage_database):
    store = storage_database
    users = PostgresUserRepository(store.pool)
    storage_repository = PostgresUserStorageRepository(store.pool, store.cipher)
    provider = Provider()
    storage = UserStorageService(
        storage_repository,
        provider,
        users,
        action_timeout_seconds=2,
    )
    owner = user(principal="sai08-lifecycle")
    await users.save(owner, create=True)
    await storage.ensure(owner)
    access = SimpleNamespace(authorize=AsyncMock(return_value=owner.tenant_id))
    service = UserService(users, access)
    service.storage = storage

    disable = asyncio.create_task(service.update(operator(), owner.id, UserPatch(enabled=False)))
    await wait_for_value(
        lambda: store.pool.fetchval("SELECT enabled FROM fs2_inference_users WHERE id=$1", owner.id),
        False,
    )
    await storage.ensure(await users.configured(owner.tenant_id, owner.principal_id))
    disabled = await disable
    row = await storage_repository.credential(owner.tenant_id, owner.principal_id)
    assert not disabled.enabled and not row["enabled"] and not row["desired_enabled"]
    assert provider.states[row["access_key_resource_id"]] == "INACTIVE"

    enable = asyncio.create_task(service.update(operator(), owner.id, UserPatch(enabled=True)))
    await wait_for_value(
        lambda: store.pool.fetchval("SELECT enabled FROM fs2_inference_users WHERE id=$1", owner.id),
        True,
    )
    committed = await users.configured(owner.tenant_id, owner.principal_id)
    assert committed is not None and committed.enabled
    await storage.ensure(committed)
    enabled = await enable
    row = await storage_repository.credential(owner.tenant_id, owner.principal_id)
    assert enabled.enabled and row["enabled"] and row["desired_enabled"]
    assert row["requested_action"] is None
    assert provider.states[row["access_key_resource_id"]] == "ACTIVE"


async def test_user_disable_preserves_durable_rotation_until_safe_reenable(storage_database):
    store = storage_database
    users = PostgresUserRepository(store.pool)
    repository = PostgresUserStorageRepository(store.pool, store.cipher)
    provider = Provider()
    storage = UserStorageService(repository, provider, users, action_timeout_seconds=2)
    owner = user(principal="sai08-pending-rotation")
    await users.save(owner, create=True)
    await storage.ensure(owner)
    token_id, _ = await issue_storage_token(store, tenant=owner.tenant_id, principal=owner.principal_id)
    action_id = await repository.request_action(
        owner.tenant_id,
        owner.principal_id,
        "rotate",
        token_id=token_id,
        operator_session_id=None,
        idempotency_key=uuid4(),
    )
    before = await store.pool.fetchrow(
        "SELECT current_action_id,requested_at FROM fs2_user_storage WHERE principal_id=$1",
        owner.principal_id,
    )
    access = SimpleNamespace(authorize=AsyncMock(return_value=owner.tenant_id))
    service = UserService(users, access)
    service.storage = storage

    disable = asyncio.create_task(service.update(operator(), owner.id, UserPatch(enabled=False)))
    await wait_for_value(
        lambda: store.pool.fetchval("SELECT enabled FROM fs2_inference_users WHERE id=$1", owner.id),
        False,
    )
    pending = await store.pool.fetchrow(
        "SELECT current_action_id,requested_action,requested_at FROM fs2_user_storage WHERE principal_id=$1",
        owner.principal_id,
    )
    assert (pending["current_action_id"], pending["requested_action"], pending["requested_at"]) == (
        before["current_action_id"],
        "rotate",
        before["requested_at"],
    )
    await storage.ensure(await users.configured(owner.tenant_id, owner.principal_id))
    await disable
    assert provider.rotation_count == 0
    assert await store.pool.fetchval("SELECT status FROM fs2_storage_actions WHERE id=$1", action_id) == "requested"

    enable = asyncio.create_task(service.update(operator(), owner.id, UserPatch(enabled=True)))
    await wait_for_value(
        lambda: store.pool.fetchval("SELECT enabled FROM fs2_inference_users WHERE id=$1", owner.id),
        True,
    )
    await storage.ensure(await users.configured(owner.tenant_id, owner.principal_id))
    await enable
    final = await repository.credential(owner.tenant_id, owner.principal_id)
    assert final["enabled"] and final["desired_enabled"] and final["requested_action"] is None
    assert provider.rotation_count == 1
    assert await store.pool.fetchval("SELECT status FROM fs2_storage_actions WHERE id=$1", action_id) == "succeeded"


async def test_tenant_emergency_disable_returns_only_after_every_provider_key_is_inactive(storage_database):
    store = storage_database
    users = PostgresUserRepository(store.pool)
    repository = PostgresUserStorageRepository(store.pool, store.cipher)
    provider = Provider()
    service = UserStorageService(repository, provider, users, action_timeout_seconds=2)
    owners = [user(principal="sai08-tenant-a"), user(principal="sai08-tenant-b")]
    for owner in owners:
        await users.save(owner, create=True)
        await service.ensure(owner)
    original_ids = {
        owner.principal_id: (await repository.credential(owner.tenant_id, owner.principal_id))["access_key_resource_id"]
        for owner in owners
    }

    transition = asyncio.create_task(service.configure("customer-a", StoragePolicy(mode="disabled")))
    await wait_for_value(
        lambda: store.pool.fetchval("SELECT enabled FROM fs2_storage_policies WHERE tenant_id='customer-a'"),
        False,
    )
    for owner in owners:
        configured = await users.configured(owner.tenant_id, owner.principal_id)
        assert configured is not None
        await service.ensure(configured)
    policy = await transition
    assert policy.mode == "disabled"
    rows = await store.pool.fetch(
        """SELECT principal_id,enabled,desired_enabled,policy_suspension_requested
        FROM fs2_user_storage WHERE tenant_id='customer-a' ORDER BY principal_id"""
    )
    assert len(rows) == 2
    assert all(not row["enabled"] and row["desired_enabled"] for row in rows)
    assert all(not row["policy_suspension_requested"] for row in rows)
    assert all(provider.states[key] == "INACTIVE" for key in original_ids.values())

    resume = asyncio.create_task(service.configure("customer-a", StoragePolicy(mode="user")))
    await wait_for_value(
        lambda: store.pool.fetchval("SELECT enabled FROM fs2_storage_policies WHERE tenant_id='customer-a'"),
        True,
    )
    for owner in owners:
        configured = await users.configured(owner.tenant_id, owner.principal_id)
        assert configured is not None
        await service.ensure(configured)
    assert (await resume).mode == "user"
    assert all(provider.states[key] == "ACTIVE" for key in original_ids.values())
    assert {
        row["principal_id"]: row["access_key_resource_id"]
        for row in await store.pool.fetch(
            "SELECT principal_id,access_key_resource_id FROM fs2_user_storage WHERE tenant_id='customer-a'"
        )
    } == original_ids


async def test_action_intent_survives_cancellation_and_audit_failure_without_duplicate_cloud_effects(
    storage_database,
):
    store = storage_database
    users = PostgresUserRepository(store.pool)
    repository = PostgresUserStorageRepository(store.pool, store.cipher)
    provider = Provider()
    service = UserStorageService(repository, provider, users, action_timeout_seconds=2)
    owner = user(principal="sai08-actions")
    await users.save(owner, create=True)
    await service.ensure(owner)
    token_id, _ = await issue_storage_token(store, tenant=owner.tenant_id, principal=owner.principal_id)

    first_key = uuid4()
    request = asyncio.create_task(
        service.rotate(
            owner.tenant_id,
            owner.principal_id,
            token_id=token_id,
            operator_session_id=None,
            idempotency_key=first_key,
        )
    )
    await wait_for_value(
        lambda: store.pool.fetchval(
            "SELECT requested_action FROM fs2_user_storage WHERE principal_id=$1",
            owner.principal_id,
        ),
        "rotate",
    )
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await service.ensure(owner)
    first_action = await store.pool.fetchrow(
        "SELECT id,status,audit_event_id FROM fs2_storage_actions WHERE idempotency_key=$1",
        first_key,
    )
    assert first_action["status"] == "succeeded" and first_action["audit_event_id"] is not None
    # An HTTP retry with the same idempotency key observes the same terminal
    # action; it neither queues nor repeats provider mutations.
    provider_calls = list(provider.enabled_calls)
    assert (
        await repository.request_action(
            owner.tenant_id,
            owner.principal_id,
            "rotate",
            token_id=token_id,
            operator_session_id=None,
            idempotency_key=first_key,
        )
        == first_action["id"]
    )
    assert provider.enabled_calls == provider_calls

    await store.pool.execute(
        """CREATE FUNCTION sai08_fail_storage_audit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.action='storage.credentials.rotate' THEN
            RAISE EXCEPTION 'sai08 audit unavailable';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER sai08_fail_storage_audit BEFORE INSERT ON fs2_audit_events
        FOR EACH ROW EXECUTE FUNCTION sai08_fail_storage_audit()"""
    )
    second_key = uuid4()
    second_action = await repository.request_action(
        owner.tenant_id,
        owner.principal_id,
        "rotate",
        token_id=token_id,
        operator_session_id=None,
        idempotency_key=second_key,
    )
    with pytest.raises(Exception, match="audit unavailable"):
        await service.ensure(owner)
    pending = await repository.credential(owner.tenant_id, owner.principal_id)
    assert pending["requested_action"] == "rotate"
    assert pending["previous_access_key_resource_id"] is not None
    assert await store.pool.fetchval("SELECT status FROM fs2_storage_actions WHERE id=$1", second_action) == "requested"
    assert sum(state == "ACTIVE" for state in provider.states.values()) == 1

    await store.pool.execute(
        "DROP TRIGGER sai08_fail_storage_audit ON fs2_audit_events; DROP FUNCTION sai08_fail_storage_audit()"
    )
    provider_calls = list(provider.enabled_calls)
    await service.ensure(owner)
    assert provider.enabled_calls == provider_calls
    terminal = await store.pool.fetchrow(
        "SELECT status,audit_event_id FROM fs2_storage_actions WHERE id=$1", second_action
    )
    assert terminal["status"] == "succeeded" and terminal["audit_event_id"] is not None
    assert (
        await store.pool.fetchval(
            """SELECT count(*) FROM fs2_audit_events WHERE id=$1
            AND actor=$2 AND tenant_id=$3 AND token_id=$4
            AND action='storage.credentials.rotate' AND outcome='succeeded'""",
            terminal["audit_event_id"],
            owner.principal_id,
            owner.tenant_id,
            token_id,
        )
        == 1
    )
