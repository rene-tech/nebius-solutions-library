from __future__ import annotations

import asyncio
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from argon2.exceptions import VerifyMismatchError

from fs2_serve.auth import ArgonCapacityError, AuthenticationError, PepperRing, TokenService
from fs2_serve.memory_store import MemoryStore
from fs2_serve.models import Scope, TokenCreate

PEPPER = b"p" * 32


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DeterministicHasher:
    """Fast test double whose digest is the existing peppered prehash."""

    def __init__(self) -> None:
        self.calls = 0
        self.thread_ids: set[int] = set()

    def verify(self, digest: str, prehash: str) -> bool:
        self.calls += 1
        self.thread_ids.add(threading.get_ident())
        if digest != prehash:
            raise VerifyMismatchError("test mismatch")
        return True


class BlockingHasher(DeterministicHasher):
    def __init__(self, *, expected_active: int) -> None:
        super().__init__()
        self.expected_active = expected_active
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.saturated = threading.Event()
        self.release = threading.Event()

    def verify(self, digest: str, prehash: str) -> bool:
        with self.lock:
            self.calls += 1
            self.thread_ids.add(threading.get_ident())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == self.expected_active:
                self.saturated.set()
        try:
            if not self.release.wait(timeout=2):
                raise AssertionError("test did not release the Argon worker")
            if digest != prehash:
                raise VerifyMismatchError("test mismatch")
            return True
        finally:
            with self.lock:
                self.active -= 1


class BlockingRehashHasher(DeterministicHasher):
    def __init__(self) -> None:
        super().__init__()
        self.hash_calls = 0
        self.hash_thread_ids: set[int] = set()
        self.hash_started = threading.Event()
        self.release_hash = threading.Event()

    def hash(self, prehash: str) -> str:
        self.hash_calls += 1
        self.hash_thread_ids.add(threading.get_ident())
        self.hash_started.set()
        if not self.release_hash.wait(timeout=2):
            raise AssertionError("test did not release the Argon rehash worker")
        return prehash


class ThreadRecordingHasher(DeterministicHasher):
    def __init__(self) -> None:
        super().__init__()
        self.hash_calls = 0
        self.hash_thread_ids: set[int] = set()

    def hash(self, prehash: str) -> str:
        self.hash_calls += 1
        self.hash_thread_ids.add(threading.get_ident())
        return prehash


class ExpirationCountingStore(MemoryStore):
    expiration_record_calls = 0

    async def record_token_expired(self, token_id: UUID, *, actor: str) -> None:
        self.expiration_record_calls += 1
        await super().record_token_expired(token_id, actor=actor)


def service(store: MemoryStore, **overrides: Any) -> TokenService:
    return TokenService(
        store,
        PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": PEPPER}),
        **overrides,
    )


async def add_token(
    store: MemoryStore,
    tokens: TokenService,
    *,
    secret: str,
    expires_at: datetime | None = None,
    pepper_key_id: str = "pepper-v1",
    with_fingerprint: bool = True,
) -> tuple[UUID, str]:
    token_id = uuid4()
    token = f"fs2_pat_{token_id.hex}_{secret}"
    await store.issue_token(
        token_id=token_id,
        prefix=f"fs2_pat_{token_id.hex[:12]}",
        pepper_key_id=pepper_key_id,
        digest=tokens._prehash(token, pepper_key_id),
        request=TokenCreate(
            principal_id=f"principal-{token_id.hex[:8]}",
            tenant_id="tenant-a",
            scopes={Scope.CATALOG_READ},
            models={"qwen3-8b"},
            expires_at=expires_at,
        ),
        created_by="test",
        fingerprint=hashlib.sha256(token.encode()).hexdigest() if with_fingerprint else None,
    )
    return token_id, token


@pytest.mark.asyncio
async def test_revoked_and_expired_tokens_are_rejected_before_argon(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tokens = service(store)
    revoked_id, revoked = await add_token(store, tokens, secret="r" * 32)
    _, expired = await add_token(
        store,
        tokens,
        secret="e" * 32,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await store.revoke_token(revoked_id, actor="test")
    fake = DeterministicHasher()
    tokens._hasher = fake  # type: ignore[assignment]

    with pytest.raises(AuthenticationError, match="invalid bearer token"):
        await tokens.verify(revoked)
    with pytest.raises(AuthenticationError, match="invalid bearer token"):
        await tokens.verify(expired)

    assert fake.calls == 0


@pytest.mark.asyncio
async def test_expired_token_replay_records_expiration_once(cipher, hasher) -> None:
    store = ExpirationCountingStore(cipher, hasher)
    tokens = service(store)
    token_id, expired = await add_token(
        store,
        tokens,
        secret="e" * 32,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    for _ in range(20):
        with pytest.raises(AuthenticationError, match="invalid bearer token"):
            await tokens.verify(expired)

    stored = await store.token_for_verification(token_id)
    assert stored is not None
    assert stored[2]
    assert store.expiration_record_calls == 1


@pytest.mark.asyncio
async def test_revoked_token_replay_does_not_consume_other_tokens_argon_budget(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tokens = service(store, verification_concurrency=1)
    revoked_id, revoked = await add_token(store, tokens, secret="r" * 32)
    _, valid = await add_token(store, tokens, secret="v" * 32)
    await store.revoke_token(revoked_id, actor="test")
    fake = DeterministicHasher()
    tokens._hasher = fake  # type: ignore[assignment]

    replay_results = await asyncio.gather(
        *(tokens.verify(revoked) for _ in range(128)),
        return_exceptions=True,
    )
    principal = await tokens.verify(valid)

    assert all(isinstance(result, AuthenticationError) for result in replay_results)
    assert principal.token_id != revoked_id
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_argon_verification_runs_off_loop_with_bounded_parallelism(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tokens = service(store, verification_concurrency=2)
    credentials = [await add_token(store, tokens, secret=character * 32) for character in "abcd"]
    fake = BlockingHasher(expected_active=2)
    tokens._hasher = fake  # type: ignore[assignment]

    pending = [asyncio.create_task(tokens.verify(token)) for _, token in credentials]
    assert await asyncio.wait_for(asyncio.to_thread(fake.saturated.wait, 1), timeout=1.5)
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=0.1)
    assert fake.max_active == 2

    fake.release.set()
    principals = await asyncio.gather(*pending)
    assert {principal.token_id for principal in principals} == {token_id for token_id, _ in credentials}
    assert fake.max_active == 2
    assert threading.get_ident() not in fake.thread_ids


@pytest.mark.asyncio
async def test_argon_overload_fails_fast_without_delaying_cached_or_uncached_other_keys(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tokens = service(store, verification_concurrency=1, argon_queue_capacity=1)
    cached_id, cached = await add_token(store, tokens, secret="c" * 32)
    first_id, first = await add_token(store, tokens, secret="a" * 32)
    second_id, second = await add_token(store, tokens, secret="b" * 32)
    _, uncached = await add_token(store, tokens, secret="u" * 32)
    fast = DeterministicHasher()
    tokens._hasher = fast  # type: ignore[assignment]
    assert (await tokens.verify(cached)).token_id == cached_id

    blocking = BlockingHasher(expected_active=1)
    tokens._hasher = blocking  # type: ignore[assignment]
    active = asyncio.create_task(tokens.verify(first))
    assert await asyncio.wait_for(asyncio.to_thread(blocking.saturated.wait, 1), timeout=1.5)
    queued = asyncio.create_task(tokens.verify(second))
    await asyncio.sleep(0)
    assert tokens._argon_admitted == 2

    assert (await asyncio.wait_for(tokens.verify(cached), timeout=0.1)).token_id == cached_id
    with pytest.raises(ArgonCapacityError, match="capacity"):
        await asyncio.wait_for(tokens.verify(uncached), timeout=0.1)

    blocking.release.set()
    assert (await active).token_id == first_id
    assert (await queued).token_id == second_id
    assert tokens._argon_admitted == 0


@pytest.mark.asyncio
async def test_cancelled_request_keeps_argon_slot_until_worker_finishes(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tokens = service(store, verification_concurrency=1)
    _, first = await add_token(store, tokens, secret="a" * 32)
    second_id, second = await add_token(store, tokens, secret="b" * 32)
    fake = BlockingHasher(expected_active=1)
    tokens._hasher = fake  # type: ignore[assignment]

    cancelled = asyncio.create_task(tokens.verify(first))
    assert await asyncio.wait_for(asyncio.to_thread(fake.saturated.wait, 1), timeout=1.5)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    waiting = asyncio.create_task(tokens.verify(second))
    await asyncio.sleep(0)
    assert fake.calls == 1

    fake.release.set()
    assert (await waiting).token_id == second_id
    assert fake.max_active == 1


@pytest.mark.asyncio
async def test_old_pepper_rehash_is_off_loop_bounded_coalesced_and_cancellation_safe(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tokens = TokenService(
        store,
        PepperRing(active_key_id="pepper-v2", keys={"pepper-v1": PEPPER, "pepper-v2": b"q" * 32}),
        verification_concurrency=1,
    )
    token_id, token = await add_token(
        store,
        tokens,
        secret="o" * 32,
        pepper_key_id="pepper-v1",
    )
    other_id, other = await add_token(
        store,
        tokens,
        secret="n" * 32,
        pepper_key_id="pepper-v2",
    )
    fake = BlockingRehashHasher()
    tokens._hasher = fake  # type: ignore[assignment]

    leader = asyncio.create_task(tokens.verify(token))
    assert await asyncio.wait_for(asyncio.to_thread(fake.hash_started.wait, 1), timeout=1.5)
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=0.1)

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    follower = asyncio.create_task(tokens.verify(token))
    waiting = asyncio.create_task(tokens.verify(other))
    await asyncio.sleep(0)
    assert fake.hash_calls == 1
    assert fake.calls == 1

    fake.release_hash.set()
    assert (await follower).token_id == token_id
    assert (await waiting).token_id == other_id
    stored = await store.token_for_verification(token_id)
    assert stored is not None
    assert stored[0].pepper_key_id == "pepper-v2"
    assert stored[1] == tokens._prehash(token, "pepper-v2")
    assert threading.get_ident() not in fake.hash_thread_ids


@pytest.mark.asyncio
async def test_issue_and_rotate_hash_off_loop_without_blocking_heartbeat(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tokens = service(store, verification_concurrency=1)
    request = TokenCreate(
        principal_id="heartbeat-user",
        tenant_id="tenant-a",
        scopes={Scope.CATALOG_READ},
        models={"qwen3-8b"},
    )

    issue_hasher = BlockingRehashHasher()
    tokens._hasher = issue_hasher  # type: ignore[assignment]
    issuing = asyncio.create_task(tokens.issue(request, created_by="test"))
    assert await asyncio.wait_for(asyncio.to_thread(issue_hasher.hash_started.wait, 1), timeout=1.5)
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=0.1)
    issue_hasher.release_hash.set()
    issued = await issuing

    rotate_hasher = BlockingRehashHasher()
    tokens._hasher = rotate_hasher  # type: ignore[assignment]
    rotating = asyncio.create_task(tokens.rotate(issued.id, actor="test"))
    assert await asyncio.wait_for(asyncio.to_thread(rotate_hasher.hash_started.wait, 1), timeout=1.5)
    heartbeat.clear()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=0.1)
    rotate_hasher.release_hash.set()
    rotated = await rotating

    assert rotated.rotation_parent_id == issued.id
    assert threading.get_ident() not in issue_hasher.hash_thread_ids
    assert threading.get_ident() not in rotate_hasher.hash_thread_ids


@pytest.mark.asyncio
async def test_bootstrap_hash_and_verify_share_off_loop_argon_admission(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tokens = service(store, verification_concurrency=1)
    fake = ThreadRecordingHasher()
    tokens._hasher = fake  # type: ignore[assignment]
    token_id = uuid4()
    token = f"fs2_pat_{token_id.hex}_{'z' * 32}"
    request = TokenCreate(
        principal_id="bootstrap-user",
        tenant_id="tenant-a",
        scopes={Scope.CATALOG_READ},
        models={"qwen3-8b"},
    )

    first = await tokens.ensure_provisioned(token, request, created_by="test")
    second = await tokens.ensure_provisioned(token, request, created_by="test")

    assert first == second
    assert fake.hash_calls == 1
    assert fake.calls == 1
    assert threading.get_ident() not in fake.hash_thread_ids
    assert threading.get_ident() not in fake.thread_ids


@pytest.mark.asyncio
async def test_rehash_compare_and_swap_preserves_a_newer_verifier(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    tokens = TokenService(
        store,
        PepperRing(active_key_id="pepper-v2", keys={"pepper-v1": PEPPER, "pepper-v2": b"q" * 32}),
    )
    token_id, token = await add_token(
        store,
        tokens,
        secret="c" * 32,
        pepper_key_id="pepper-v1",
    )
    original = await store.token_for_verification(token_id)
    assert original is not None
    newer_digest = tokens._prehash(token, "pepper-v2")
    await store.rehash_token(token_id, pepper_key_id="pepper-v2", digest=newer_digest)

    updated = await store.rehash_token_if_current(
        token_id,
        expected_pepper_key_id="pepper-v1",
        expected_digest=original[1],
        pepper_key_id="pepper-v2",
        digest="stale-worker-digest",
    )

    assert not updated
    current = await store.token_for_verification(token_id)
    assert current is not None
    assert current[0].pepper_key_id == "pepper-v2"
    assert current[1] == newer_digest


@pytest.mark.asyncio
async def test_success_cache_is_short_lived_hmac_keyed_and_never_stores_bearer(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    clock = FakeClock()
    tokens = service(store, verification_cache_ttl_seconds=5, monotonic_clock=clock)
    _, token = await add_token(store, tokens, secret="c" * 32)
    fake = DeterministicHasher()
    tokens._hasher = fake  # type: ignore[assignment]

    await tokens.verify(token)
    await tokens.verify(token)
    assert fake.calls == 1
    assert token not in repr(tokens._verification_cache)

    clock.advance(6)
    await tokens.verify(token)
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_failure_throttle_never_locks_out_stored_fingerprint(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    clock = FakeClock()
    tokens = service(store, failure_limit=2, failure_window_seconds=30, monotonic_clock=clock)
    first_id, first = await add_token(store, tokens, secret="f" * 32)
    second_id, second = await add_token(store, tokens, secret="s" * 32)
    wrong = f"fs2_pat_{first_id.hex}_{'x' * 32}"
    fake = DeterministicHasher()
    tokens._hasher = fake  # type: ignore[assignment]

    for _ in range(2):
        with pytest.raises(AuthenticationError, match="invalid bearer token"):
            await tokens.verify(wrong)
    with pytest.raises(AuthenticationError, match="invalid bearer token"):
        await tokens.verify(wrong)
    assert fake.calls == 0
    assert tokens._verification_is_throttled(first_id)

    assert (await tokens.verify(second)).token_id == second_id
    assert fake.calls == 1
    assert (await tokens.verify(first)).token_id == first_id
    assert fake.calls == 2
    assert not tokens._verification_is_throttled(first_id)

    clock.advance(31)
    with pytest.raises(AuthenticationError, match="invalid bearer token"):
        await tokens.verify(wrong)
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_legacy_wrong_secrets_stop_at_budget_then_recovery_probe_binds_fingerprint(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    clock = FakeClock()
    tokens = service(
        store,
        failure_limit=2,
        legacy_probe_interval_seconds=5,
        monotonic_clock=clock,
    )
    token_id, token = await add_token(store, tokens, secret="l" * 32, with_fingerprint=False)
    fake = DeterministicHasher()
    tokens._hasher = fake  # type: ignore[assignment]

    for character in "xy":
        with pytest.raises(AuthenticationError, match="invalid bearer token"):
            await tokens.verify(f"fs2_pat_{token_id.hex}_{character * 32}")
    with pytest.raises(AuthenticationError, match="invalid bearer token"):
        await tokens.verify(f"fs2_pat_{token_id.hex}_{'w' * 32}")
    with pytest.raises(AuthenticationError, match="invalid bearer token"):
        await tokens.verify(token)
    assert fake.calls == 2

    clock.advance(5)
    assert (await tokens.verify(token)).token_id == token_id

    stored = await store.token_for_verification(token_id)
    assert stored is not None
    assert stored[0].fingerprint == hashlib.sha256(token.encode()).hexdigest()
    assert fake.calls == 3

    with pytest.raises(AuthenticationError, match="invalid bearer token"):
        await tokens.verify(f"fs2_pat_{token_id.hex}_{'x' * 32}")
    with pytest.raises(AuthenticationError, match="invalid bearer token"):
        await tokens.verify(f"fs2_pat_{token_id.hex}_{'y' * 32}")
    assert fake.calls == 3
