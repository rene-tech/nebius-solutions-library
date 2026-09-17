from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from argon2.exceptions import VerifyMismatchError

from fs2_serve.auth import AuthenticationError, PepperRing, TokenService
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
) -> tuple[UUID, str]:
    token_id = uuid4()
    token = f"fs2_pat_{token_id.hex}_{secret}"
    await store.issue_token(
        token_id=token_id,
        prefix=f"fs2_pat_{token_id.hex[:12]}",
        pepper_key_id="pepper-v1",
        digest=tokens._prehash(token, "pepper-v1"),
        request=TokenCreate(
            principal_id=f"principal-{token_id.hex[:8]}",
            tenant_id="tenant-a",
            scopes={Scope.CATALOG_READ},
            models={"qwen3-8b"},
            expires_at=expires_at,
        ),
        created_by="test",
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
async def test_failed_verification_throttle_is_per_token_id_and_recovers(cipher, hasher) -> None:
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
    assert fake.calls == 2

    assert (await tokens.verify(second)).token_id == second_id
    assert fake.calls == 3

    clock.advance(31)
    assert (await tokens.verify(first)).token_id == first_id
    assert fake.calls == 4
