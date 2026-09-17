"""Opaque personal access token issuance and prompt revocation checks."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .access_models import BOOTSTRAP_OPERATOR_PRINCIPAL_ID, AdminApiKeyPolicyPatch, OperatorSession
from .models import OperationView, Principal, Scope, TokenCreate, TokenIssued, TokenView
from .store import ConflictError, NotFoundError, Store

TOKEN_MARKER = "fs2_pat"  # noqa: S105 - public token format marker, not a credential
MAX_PAT_LENGTH = 256
TOKEN_VERIFICATION_CACHE_CONTEXT = b"fs2-serve.pat-verification-cache/v1\0"
TOKEN_VERIFICATION_CONCURRENCY = 4
TOKEN_VERIFICATION_CACHE_TTL_SECONDS = 5.0
TOKEN_VERIFICATION_CACHE_MAX_ENTRIES = 4096
TOKEN_FAILURE_LIMIT = 5
TOKEN_FAILURE_WINDOW_SECONDS = 30.0
TOKEN_FAILURE_BUCKET_MAX_ENTRIES = 4096
SESSION_MARKER = "fs2_admin"
MAX_OPERATOR_SESSION_LENGTH = 256
OPERATOR_SESSION_DIGEST_CONTEXT = b"fs2-serve.admin-session/v1\0"

VerificationCacheKey = tuple[UUID, str, str, str]
RehashKey = tuple[UUID, str, str]


class AuthenticationError(PermissionError):
    pass


@dataclass(frozen=True)
class IssuedOperatorSession:
    session: OperatorSession
    cookie_value: str


def require_operation_access(principal: Principal, operation: OperationView) -> None:
    """Authorize an operation owner or an explicit same-tenant administrator.

    The exact PAT that durably admitted an operation carries an implicit owner
    capability for its lifecycle. This prevents a valid inference or MCP token
    from receiving a 202 that it cannot subsequently inspect or acknowledge.
    """

    if operation.tenant_id != principal.tenant_id:
        raise NotFoundError("operation not found")
    if operation.token_id == principal.token_id and operation.principal_id == principal.principal_id:
        return
    if Scope.TENANT_ADMIN in principal.scopes:
        return
    raise NotFoundError("operation not found")


class PepperRing:
    """Rotatable keyed-prehash peppers loaded only from a mounted JSON file."""

    def __init__(self, *, active_key_id: str, keys: dict[str, bytes]) -> None:
        if active_key_id not in keys:
            raise ValueError("active PAT pepper is absent from key ring")
        if any(not key_id or len(key_id) > 64 or len(value) < 32 for key_id, value in keys.items()):
            raise ValueError("PAT pepper ids must be bounded and pepper values must contain at least 32 bytes")
        self.active_key_id = active_key_id
        self.keys = dict(keys)

    @classmethod
    def from_file(cls, path: Path) -> PepperRing:
        try:
            import json

            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read PAT pepper key ring {path}") from exc
        if not isinstance(raw, dict) or set(raw) != {"active_key_id", "keys"} or not isinstance(raw["keys"], dict):
            raise ValueError("PAT pepper key ring must contain only active_key_id and keys")
        keys: dict[str, bytes] = {}
        for key_id, encoded in raw["keys"].items():
            if not isinstance(key_id, str) or not isinstance(encoded, str):
                raise ValueError("PAT pepper key ring values must be base64 strings")
            try:
                keys[key_id] = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("PAT pepper key ring contains invalid base64") from exc
        if not isinstance(raw["active_key_id"], str):
            raise ValueError("PAT pepper active_key_id must be a string")
        return cls(active_key_id=raw["active_key_id"], keys=keys)


class TokenService:
    """Hash opaque PATs with a keyed prehash and memory-hard Argon2id."""

    def __init__(
        self,
        store: Store,
        peppers: PepperRing,
        *,
        principal_policy: Callable[[Principal], Awaitable[Principal]] | None = None,
        verification_concurrency: int = TOKEN_VERIFICATION_CONCURRENCY,
        verification_cache_ttl_seconds: float = TOKEN_VERIFICATION_CACHE_TTL_SECONDS,
        verification_cache_max_entries: int = TOKEN_VERIFICATION_CACHE_MAX_ENTRIES,
        failure_limit: int = TOKEN_FAILURE_LIMIT,
        failure_window_seconds: float = TOKEN_FAILURE_WINDOW_SECONDS,
        failure_bucket_max_entries: int = TOKEN_FAILURE_BUCKET_MAX_ENTRIES,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= verification_concurrency <= 64:
            raise ValueError("PAT verification concurrency is outside the bound")
        if not 0 < verification_cache_ttl_seconds <= 60:
            raise ValueError("PAT verification cache TTL is outside the bound")
        if not 1 <= verification_cache_max_entries <= 100_000:
            raise ValueError("PAT verification cache size is outside the bound")
        if not 1 <= failure_limit <= 100:
            raise ValueError("PAT verification failure limit is outside the bound")
        if not 1 <= failure_window_seconds <= 3600:
            raise ValueError("PAT verification failure window is outside the bound")
        if not 1 <= failure_bucket_max_entries <= 100_000:
            raise ValueError("PAT verification failure bucket count is outside the bound")
        self.store = store
        self._peppers = peppers
        self.principal_policy = principal_policy
        self._hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
        self._verification_slots = asyncio.BoundedSemaphore(verification_concurrency)
        self._verification_cache_ttl_seconds = verification_cache_ttl_seconds
        self._verification_cache_max_entries = verification_cache_max_entries
        self._verification_cache: OrderedDict[VerificationCacheKey, float] = OrderedDict()
        self._failure_limit = failure_limit
        self._failure_window_seconds = failure_window_seconds
        self._failure_bucket_max_entries = failure_bucket_max_entries
        self._failed_verifications: OrderedDict[UUID, deque[float]] = OrderedDict()
        self._rehash_tasks: dict[RehashKey, asyncio.Task[None]] = {}
        self._monotonic_clock = monotonic_clock

    def _prehash(self, token: str, key_id: str) -> str:
        try:
            pepper = self._peppers.keys[key_id]
        except KeyError as exc:
            raise AuthenticationError("token hash key is unavailable") from exc
        return hmac.new(pepper, token.encode(), hashlib.sha256).hexdigest()

    def _verification_cache_key(
        self,
        token_id: UUID,
        token: str,
        key_id: str,
        digest: str,
    ) -> VerificationCacheKey:
        try:
            pepper = self._peppers.keys[key_id]
        except KeyError as exc:
            raise AuthenticationError("token hash key is unavailable") from exc
        token_hmac = hmac.new(
            pepper,
            TOKEN_VERIFICATION_CACHE_CONTEXT + token.encode(),
            hashlib.sha256,
        ).hexdigest()
        digest_fingerprint = hashlib.sha256(digest.encode()).hexdigest()
        return token_id, key_id, digest_fingerprint, token_hmac

    def _verification_is_cached(self, key: VerificationCacheKey) -> bool:
        expires_at = self._verification_cache.get(key)
        if expires_at is None:
            return False
        if expires_at <= self._monotonic_clock():
            self._verification_cache.pop(key, None)
            return False
        self._verification_cache.move_to_end(key)
        return True

    def _cache_verification(self, key: VerificationCacheKey) -> None:
        self._verification_cache[key] = self._monotonic_clock() + self._verification_cache_ttl_seconds
        self._verification_cache.move_to_end(key)
        while len(self._verification_cache) > self._verification_cache_max_entries:
            self._verification_cache.popitem(last=False)

    def _discard_token_auth_state(self, token_id: UUID) -> None:
        self._failed_verifications.pop(token_id, None)
        for key in tuple(self._verification_cache):
            if key[0] == token_id:
                self._verification_cache.pop(key, None)

    def _recent_failures(self, token_id: UUID) -> deque[float] | None:
        failures = self._failed_verifications.get(token_id)
        if failures is None:
            return None
        cutoff = self._monotonic_clock() - self._failure_window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failed_verifications.pop(token_id, None)
            return None
        self._failed_verifications.move_to_end(token_id)
        return failures

    def _verification_is_throttled(self, token_id: UUID) -> bool:
        failures = self._recent_failures(token_id)
        return failures is not None and len(failures) >= self._failure_limit

    def _record_failed_verification(self, token_id: UUID) -> None:
        failures = self._recent_failures(token_id)
        if failures is None:
            failures = deque()
            self._failed_verifications[token_id] = failures
        failures.append(self._monotonic_clock())
        while len(failures) > self._failure_limit:
            failures.popleft()
        self._failed_verifications.move_to_end(token_id)
        while len(self._failed_verifications) > self._failure_bucket_max_entries:
            self._failed_verifications.popitem(last=False)

    async def _verify_digest(
        self,
        *,
        token_id: UUID,
        cache_key: VerificationCacheKey,
        digest: str,
        prehash: str,
    ) -> None:
        try:
            valid = await asyncio.to_thread(self._hasher.verify, digest, prehash)
        except (InvalidHashError, VerifyMismatchError) as exc:
            self._record_failed_verification(token_id)
            raise AuthenticationError("invalid bearer token") from exc
        if not valid:
            self._record_failed_verification(token_id)
            raise AuthenticationError("invalid bearer token")
        self._failed_verifications.pop(token_id, None)
        self._cache_verification(cache_key)

    def _argon_worker_finished(self, task: asyncio.Task[Any]) -> None:
        self._verification_slots.release()
        if not task.cancelled():
            task.exception()

    async def _verify_digest_bounded(
        self,
        *,
        token_id: UUID,
        cache_key: VerificationCacheKey,
        digest: str,
        prehash: str,
    ) -> None:
        if self._verification_is_cached(cache_key):
            self._failed_verifications.pop(token_id, None)
            return
        await self._verification_slots.acquire()
        if self._verification_is_cached(cache_key):
            self._failed_verifications.pop(token_id, None)
            self._verification_slots.release()
            return
        task = asyncio.create_task(
            self._verify_digest(
                token_id=token_id,
                cache_key=cache_key,
                digest=digest,
                prehash=prehash,
            )
        )
        # A disconnected request cannot release a slot while its worker thread
        # still consumes Argon2 memory and CPU. The callback releases only when
        # the actual worker finishes; shield keeps caller cancellation local.
        task.add_done_callback(self._argon_worker_finished)
        await asyncio.shield(task)

    async def _hash_prehash_bounded(self, prehash: str) -> str:
        await self._verification_slots.acquire()
        task = asyncio.create_task(asyncio.to_thread(self._hasher.hash, prehash))
        task.add_done_callback(self._argon_worker_finished)
        return await asyncio.shield(task)

    async def _rehash_verified_token(
        self,
        *,
        token_id: UUID,
        token: str,
        source_key_id: str,
        source_digest: str,
        source_cache_key: VerificationCacheKey,
    ) -> None:
        active_id = self._peppers.active_key_id
        replacement = await self._hash_prehash_bounded(self._prehash(token, active_id))
        updated = await self.store.rehash_token_if_current(
            token_id,
            expected_pepper_key_id=source_key_id,
            expected_digest=source_digest,
            pepper_key_id=active_id,
            digest=replacement,
        )
        self._verification_cache.pop(source_cache_key, None)
        if updated:
            self._cache_verification(self._verification_cache_key(token_id, token, active_id, replacement))

    def _rehash_finished(self, key: RehashKey, task: asyncio.Task[None]) -> None:
        if self._rehash_tasks.get(key) is task:
            self._rehash_tasks.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _rehash_after_verification(
        self,
        *,
        token_id: UUID,
        token: str,
        source_key_id: str,
        source_digest: str,
        source_cache_key: VerificationCacheKey,
    ) -> None:
        key = token_id, source_key_id, hashlib.sha256(source_digest.encode()).hexdigest()
        task = self._rehash_tasks.get(key)
        if task is None:
            if len(self._rehash_tasks) >= self._verification_cache_max_entries:
                return
            task = asyncio.create_task(
                self._rehash_verified_token(
                    token_id=token_id,
                    token=token,
                    source_key_id=source_key_id,
                    source_digest=source_digest,
                    source_cache_key=source_cache_key,
                )
            )
            self._rehash_tasks[key] = task
            task.add_done_callback(lambda completed: self._rehash_finished(key, completed))
        await asyncio.shield(task)

    @staticmethod
    def _parse(token: str) -> tuple[UUID, str]:
        if len(token) > MAX_PAT_LENGTH:
            raise AuthenticationError("invalid bearer token")
        parts = token.split("_", 3)
        if len(parts) != 4 or parts[0] != "fs2" or parts[1] != "pat":
            raise AuthenticationError("invalid bearer token")
        try:
            token_id = UUID(hex=parts[2])
        except ValueError as exc:
            raise AuthenticationError("invalid bearer token") from exc
        if len(parts[3]) < 32:
            raise AuthenticationError("invalid bearer token")
        return token_id, f"{TOKEN_MARKER}_{parts[2][:12]}"

    async def issue(self, request: TokenCreate, *, created_by: str) -> TokenIssued:
        now = datetime.now(UTC)
        if request.expires_at is not None and request.expires_at <= now:
            raise ValueError("expires_at must be in the future")
        token_id = uuid4()
        secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        token = f"{TOKEN_MARKER}_{token_id.hex}_{secret}"
        prefix = f"{TOKEN_MARKER}_{token_id.hex[:12]}"
        pepper_key_id = self._peppers.active_key_id
        digest = self._hasher.hash(self._prehash(token, pepper_key_id))
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        view = await self.store.issue_token(
            token_id=token_id,
            prefix=prefix,
            pepper_key_id=pepper_key_id,
            digest=digest,
            request=request,
            created_by=created_by,
            fingerprint=fingerprint,
        )
        return TokenIssued(**view.model_dump(), token=token)

    async def ensure_provisioned(self, token: str, request: TokenCreate, *, created_by: str) -> TokenView:
        """Create or reconcile one externally generated bootstrap PAT.

        Terraform owns the opaque material so it can return the credential after
        apply.  Only its Argon2id digest and bounded metadata enter the token
        store.  Re-running this method is safe: immutable ownership must match,
        while the scoped policy is reconciled for Helm upgrades.
        """

        now = datetime.now(UTC)
        if request.expires_at is not None and request.expires_at <= now:
            raise ValueError("expires_at must be in the future")
        token_id, prefix = self._parse(token)
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        stored = await self.store.token_for_verification(token_id)
        if stored is None:
            pepper_key_id = self._peppers.active_key_id
            digest = self._hasher.hash(self._prehash(token, pepper_key_id))
            try:
                return await self.store.issue_token(
                    token_id=token_id,
                    prefix=prefix,
                    pepper_key_id=pepper_key_id,
                    digest=digest,
                    request=request,
                    created_by=created_by,
                    fingerprint=fingerprint,
                )
            except ConflictError:
                # A concurrent post-install/upgrade hook may have inserted the
                # same deterministic token between the read and insert.
                stored = await self.store.token_for_verification(token_id)
                if stored is None:
                    raise

        view, digest = stored
        if not secrets.compare_digest(prefix, view.prefix):
            raise AuthenticationError("bootstrap token identity conflicts with stored token")
        if view.fingerprint is not None and not secrets.compare_digest(fingerprint, view.fingerprint):
            raise AuthenticationError("bootstrap token identity conflicts with stored token")
        try:
            valid = self._hasher.verify(digest, self._prehash(token, view.pepper_key_id))
        except (InvalidHashError, VerifyMismatchError) as exc:
            raise AuthenticationError("bootstrap token identity conflicts with stored token") from exc
        if not valid or view.revoked_at is not None or (view.expires_at is not None and view.expires_at <= now):
            raise AuthenticationError("bootstrap token is inactive")
        if view.principal_id != request.principal_id or view.tenant_id != request.tenant_id:
            raise AuthenticationError("bootstrap token ownership conflicts with stored token")

        desired_scopes = sorted(str(scope) for scope in request.scopes)
        desired_models = sorted(request.models)
        changes: dict[str, Any] = {}
        for name in (
            "name",
            "expires_at",
            "request_budget",
            "gpu_seconds_budget",
            "max_concurrency",
        ):
            if getattr(view, name) != getattr(request, name):
                changes[name] = getattr(request, name)
        if view.scopes != desired_scopes:
            changes["scopes"] = set(request.scopes)
        if view.models != desired_models:
            changes["models"] = set(request.models)
        if (
            view.rate_limit_requests != request.rate_limit_requests
            or view.rate_window_seconds != request.rate_window_seconds
        ):
            changes["rate_limit_requests"] = request.rate_limit_requests
            changes["rate_window_seconds"] = request.rate_window_seconds
        if changes:
            view = await self.store.update_token_policy(
                token_id,
                request=AdminApiKeyPolicyPatch(**changes),
                actor=created_by,
            )
        if view.pepper_key_id != self._peppers.active_key_id:
            active_id = self._peppers.active_key_id
            replacement = self._hasher.hash(self._prehash(token, active_id))
            await self.store.rehash_token(view.id, pepper_key_id=active_id, digest=replacement)
            view = view.model_copy(update={"pepper_key_id": active_id})
        return view

    async def verify(self, token: str) -> Principal:
        token_id, expected_prefix = self._parse(token)
        stored = await self.store.token_for_verification(token_id)
        if stored is None:
            raise AuthenticationError("invalid bearer token")
        view, digest = stored
        now = datetime.now(UTC)
        if view.revoked_at is not None:
            self._failed_verifications.pop(view.id, None)
            raise AuthenticationError("invalid bearer token")
        if view.expires_at is not None and view.expires_at <= now:
            self._failed_verifications.pop(view.id, None)
            if view.expiration_recorded_at is None:
                await self.store.record_token_expired(view.id, actor="token-verifier")
            raise AuthenticationError("invalid bearer token")
        if not secrets.compare_digest(expected_prefix, view.prefix):
            raise AuthenticationError("invalid bearer token")
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        # PATs carry 256 bits of generated secret. The durable fingerprint is
        # only a cheap rejection gate; a match still requires Argon below.
        if view.fingerprint is not None and not secrets.compare_digest(fingerprint, view.fingerprint):
            if not self._verification_is_throttled(view.id):
                self._record_failed_verification(view.id)
            raise AuthenticationError("invalid bearer token")
        prehash = self._prehash(token, view.pepper_key_id)
        cache_key = self._verification_cache_key(view.id, token, view.pepper_key_id, digest)
        await self._verify_digest_bounded(
            token_id=view.id,
            cache_key=cache_key,
            digest=digest,
            prehash=prehash,
        )
        if view.fingerprint is None and not await self.store.bind_token_fingerprint(
            view.id,
            fingerprint=fingerprint,
        ):
            self._discard_token_auth_state(view.id)
            raise AuthenticationError("invalid bearer token")
        if view.pepper_key_id != self._peppers.active_key_id:
            await self._rehash_after_verification(
                token_id=view.id,
                token=token,
                source_key_id=view.pepper_key_id,
                source_digest=digest,
                source_cache_key=cache_key,
            )
        principal = Principal(
            token_id=view.id,
            token_prefix=view.prefix,
            principal_id=view.principal_id,
            tenant_id=view.tenant_id,
            scopes=frozenset(view.scopes),
            models=frozenset(view.models),
            expires_at=view.expires_at,
            request_budget=view.request_budget,
            gpu_seconds_budget=view.gpu_seconds_budget,
            max_concurrency=view.max_concurrency,
        )
        return await self.principal_policy(principal) if self.principal_policy else principal

    async def list(self, *, tenant_id: str | None = None, limit: int = 200) -> list[TokenView]:
        return await self.store.list_tokens(tenant_id=tenant_id, limit=limit)

    async def rotate(
        self,
        token_id: UUID,
        *,
        actor: str,
        name: str | None = None,
        expires_at: datetime | None = None,
    ) -> TokenIssued:
        now = datetime.now(UTC)
        if expires_at is not None and expires_at <= now:
            raise ValueError("expires_at must be in the future")
        successor_id = uuid4()
        secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        token = f"{TOKEN_MARKER}_{successor_id.hex}_{secret}"
        prefix = f"{TOKEN_MARKER}_{successor_id.hex[:12]}"
        pepper_key_id = self._peppers.active_key_id
        digest = self._hasher.hash(self._prehash(token, pepper_key_id))
        fingerprint = hashlib.sha256(token.encode()).hexdigest()
        view = await self.store.rotate_token(
            token_id,
            token_id=successor_id,
            prefix=prefix,
            pepper_key_id=pepper_key_id,
            digest=digest,
            fingerprint=fingerprint,
            name=name,
            expires_at=expires_at,
            actor=actor,
        )
        return TokenIssued(**view.model_dump(), token=token)

    async def revoke(self, token_id: UUID, *, actor: str) -> TokenView:
        view = await self.store.revoke_token(token_id, actor=actor)
        self._discard_token_auth_state(token_id)
        return view


class OperatorSessionService:
    """Issue and verify durable opaque browser sessions with a separated HMAC domain."""

    def __init__(self, store: Store, peppers: PepperRing, *, ttl_seconds: int = 8 * 60 * 60) -> None:
        if not 5 * 60 <= ttl_seconds <= 24 * 60 * 60:
            raise ValueError("operator session TTL is outside the bound")
        self.store = store
        self._peppers = peppers
        self.ttl_seconds = ttl_seconds

    def _digest(self, cookie_value: str, key_id: str) -> str:
        try:
            pepper = self._peppers.keys[key_id]
        except KeyError as exc:
            raise AuthenticationError("operator session key is unavailable") from exc
        return hmac.new(pepper, OPERATOR_SESSION_DIGEST_CONTEXT + cookie_value.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _parse(cookie_value: str) -> UUID:
        if len(cookie_value) > MAX_OPERATOR_SESSION_LENGTH:
            raise AuthenticationError("invalid operator session")
        parts = cookie_value.split("_", 3)
        if len(parts) != 4 or parts[0] != "fs2" or parts[1] != "admin" or len(parts[3]) < 32:
            raise AuthenticationError("invalid operator session")
        try:
            return UUID(hex=parts[2])
        except ValueError as exc:
            raise AuthenticationError("invalid operator session") from exc

    async def issue_bootstrap(self) -> IssuedOperatorSession:
        return await self.issue(BOOTSTRAP_OPERATOR_PRINCIPAL_ID, actor="bootstrap-admin")

    def _new_material(self) -> tuple[UUID, str, str, str]:
        session_id = uuid4()
        secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        cookie_value = f"{SESSION_MARKER}_{session_id.hex}_{secret}"
        pepper_key_id = self._peppers.active_key_id
        digest = self._digest(cookie_value, pepper_key_id)
        return session_id, cookie_value, pepper_key_id, digest

    async def issue(self, principal_id: UUID, *, actor: str) -> IssuedOperatorSession:
        session_id, cookie_value, pepper_key_id, digest = self._new_material()
        session = await self.store.create_operator_session(
            session_id=session_id,
            principal_id=principal_id,
            pepper_key_id=pepper_key_id,
            digest=digest,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.ttl_seconds),
            actor=actor,
        )
        return IssuedOperatorSession(session=session, cookie_value=cookie_value)

    async def replace(
        self,
        prior_cookie_value: str | None,
        *,
        principal_id: UUID = BOOTSTRAP_OPERATOR_PRINCIPAL_ID,
        actor: str = "bootstrap-admin",
    ) -> IssuedOperatorSession:
        prior_session_id: UUID | None = None
        prior_digest: str | None = None
        if prior_cookie_value is not None:
            try:
                candidate_id = self._parse(prior_cookie_value)
                record = await self.store.operator_session_for_verification(candidate_id)
                if record is not None:
                    prior_session_id = candidate_id
                    prior_digest = self._digest(prior_cookie_value, record.pepper_key_id)
            except AuthenticationError:
                pass
        session_id, cookie_value, pepper_key_id, digest = self._new_material()
        session = await self.store.replace_operator_session(
            prior_session_id=prior_session_id,
            prior_digest=prior_digest,
            session_id=session_id,
            principal_id=principal_id,
            pepper_key_id=pepper_key_id,
            digest=digest,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.ttl_seconds),
            actor=actor,
        )
        return IssuedOperatorSession(session=session, cookie_value=cookie_value)

    async def verify(self, cookie_value: str) -> OperatorSession:
        session_id = self._parse(cookie_value)
        record = await self.store.operator_session_for_verification(session_id)
        if record is None or not secrets.compare_digest(
            record.digest,
            self._digest(cookie_value, record.pepper_key_id),
        ):
            raise AuthenticationError("invalid operator session")
        now = datetime.now(UTC)
        session = record.session
        if session.revoked_at is not None or session.expires_at <= now or not session.principal.enabled:
            raise AuthenticationError("invalid operator session")
        await self.store.touch_operator_session(session.id, seen_at=now)
        return session.model_copy(update={"last_seen_at": now})

    async def revoke(self, cookie_value: str, *, actor: str) -> OperatorSession:
        session = await self.verify(cookie_value)
        return await self.store.revoke_operator_session(session.id, actor=actor)

    async def revoke_if_valid(self, cookie_value: str | None, *, actor: str) -> None:
        if cookie_value is None:
            return
        try:
            await self.revoke(cookie_value, actor=actor)
        except (AuthenticationError, NotFoundError):
            return
