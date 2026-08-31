"""Opaque personal access token issuance and prompt revocation checks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .access_models import BOOTSTRAP_OPERATOR_PRINCIPAL_ID, OperatorSession
from .models import OperationView, Principal, Scope, TokenCreate, TokenIssued, TokenView
from .store import NotFoundError, Store

TOKEN_MARKER = "fs2_pat"  # noqa: S105 - public token format marker, not a credential
MAX_PAT_LENGTH = 256
SESSION_MARKER = "fs2_admin"
MAX_OPERATOR_SESSION_LENGTH = 256
OPERATOR_SESSION_DIGEST_CONTEXT = b"fs2-serve.admin-session/v1\0"


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

    def __init__(self, store: Store, peppers: PepperRing) -> None:
        self.store = store
        self._peppers = peppers
        self._hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)

    def _prehash(self, token: str, key_id: str) -> str:
        try:
            pepper = self._peppers.keys[key_id]
        except KeyError as exc:
            raise AuthenticationError("token hash key is unavailable") from exc
        return hmac.new(pepper, token.encode(), hashlib.sha256).hexdigest()

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

    async def verify(self, token: str) -> Principal:
        token_id, expected_prefix = self._parse(token)
        stored = await self.store.token_for_verification(token_id)
        if stored is None:
            raise AuthenticationError("invalid bearer token")
        view, digest = stored
        if not secrets.compare_digest(expected_prefix, view.prefix):
            raise AuthenticationError("invalid bearer token")
        try:
            valid = self._hasher.verify(digest, self._prehash(token, view.pepper_key_id))
        except (InvalidHashError, VerifyMismatchError) as exc:
            raise AuthenticationError("invalid bearer token") from exc
        now = datetime.now(UTC)
        if not valid or view.revoked_at is not None:
            raise AuthenticationError("invalid bearer token")
        if view.expires_at is not None and view.expires_at <= now:
            await self.store.record_token_expired(view.id, actor="token-verifier")
            raise AuthenticationError("invalid bearer token")
        if view.pepper_key_id != self._peppers.active_key_id:
            active_id = self._peppers.active_key_id
            replacement = self._hasher.hash(self._prehash(token, active_id))
            await self.store.rehash_token(view.id, pepper_key_id=active_id, digest=replacement)
        return Principal(
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
        return await self.store.revoke_token(token_id, actor=actor)


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
