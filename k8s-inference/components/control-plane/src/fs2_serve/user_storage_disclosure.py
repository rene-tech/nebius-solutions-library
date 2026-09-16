"""Narrow credential-disclosure boundary with independent database authorization."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import Cookie, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .auth import (
    MAX_OPERATOR_SESSION_LENGTH,
    MAX_PAT_LENGTH,
    OPERATOR_SESSION_DIGEST_CONTEXT,
    AuthenticationError,
    PepperRing,
)
from .crypto import Ciphertext, PayloadCipher
from .store import ConflictError
from .user_storage_models import StorageCredentials

ADMIN_SESSION_COOKIE = "fs2_admin_session"  # noqa: S105 - cookie name, not credential material


def _bearer(value: str | None) -> str:
    if value is None or len(value) > MAX_PAT_LENGTH + 7 or not value.startswith("Bearer ") or not value[7:]:
        raise AuthenticationError("bearer token required")
    return value[7:]


class PostgresStorageDisclosureRepository:
    """Execute only DB-bound entitlement functions, then decrypt one envelope."""

    def __init__(self, pool: Any, cipher: PayloadCipher, peppers: PepperRing) -> None:
        self.pool = pool
        self.cipher = cipher
        self.peppers = peppers

    @staticmethod
    def _aad(tenant: str, principal: str) -> bytes:
        return f"fs2.user-storage/v1\0{tenant}\0{principal}".encode()

    def _session_proofs(self, cookie_value: str) -> tuple[UUID, tuple[str, ...]]:
        if len(cookie_value) > MAX_OPERATOR_SESSION_LENGTH:
            raise AuthenticationError("invalid operator session")
        parts = cookie_value.split("_", 3)
        if len(parts) != 4 or parts[0:2] != ["fs2", "admin"] or len(parts[3]) < 32:
            raise AuthenticationError("invalid operator session")
        try:
            session_id = UUID(hex=parts[2])
        except ValueError as exc:
            raise AuthenticationError("invalid operator session") from exc
        # SAI-10 may retain older pepper generations during coordinated
        # rotation. Try every retained generation without exposing which one
        # matched; PostgreSQL remains authoritative for session liveness.
        digests = tuple(
            hmac.new(
                pepper,
                OPERATOR_SESSION_DIGEST_CONTEXT + cookie_value.encode(),
                hashlib.sha256,
            ).hexdigest()
            for pepper in self.peppers.keys.values()
        )
        if not digests:
            raise AuthenticationError("operator session key is unavailable")
        return session_id, digests

    def _credentials(self, value: Any) -> StorageCredentials:
        tenant = str(value["tenant_id"])
        principal = str(value["principal_id"])
        secret = self.cipher.decrypt(
            Ciphertext(value["secret_key_id"], value["secret_nonce"], value["secret_ciphertext"]),
            aad=self._aad(tenant, principal),
        ).decode()
        return StorageCredentials(
            bucket_name=value["bucket_name"],
            endpoint=value["endpoint"],
            region=value["region"],
            access_key_id=value["access_key_id"],
            secret_access_key=secret,
            expires_at=value["expires_at"],
        )

    async def disclose_user(self, bearer: str) -> StorageCredentials:
        entitlement_id = uuid4()
        async with self.pool.acquire() as connection, connection.transaction():
            authorized = await connection.fetchval(
                "SELECT fs2_begin_user_storage_disclosure($1,$2)", bearer, entitlement_id
            )
            if not authorized:
                raise AuthenticationError("invalid bearer token")
            value = await connection.fetchrow("SELECT * FROM fs2_consume_storage_disclosure($1)", entitlement_id)
        if value is None:
            raise ConflictError("storage credential disclosure is unavailable or already consumed")
        return self._credentials(value)

    async def disclose_admin(self, cookie_value: str, user_id: UUID) -> StorageCredentials:
        session_id, session_digests = self._session_proofs(cookie_value)
        entitlement_id = uuid4()
        async with self.pool.acquire() as connection, connection.transaction():
            authorized = False
            for session_digest in session_digests:
                authorized = await connection.fetchval(
                    "SELECT fs2_begin_admin_storage_disclosure($1,$2,$3,$4)",
                    session_id,
                    session_digest,
                    user_id,
                    entitlement_id,
                )
                if authorized:
                    break
            if not authorized:
                raise AuthenticationError("invalid operator session or storage target")
            value = await connection.fetchrow("SELECT * FROM fs2_consume_storage_disclosure($1)", entitlement_id)
        if value is None:
            raise ConflictError("storage credential disclosure is unavailable or already consumed")
        return self._credentials(value)

    async def ready(self) -> bool:
        return bool(await self.pool.fetchval("SELECT 1"))


class StorageDisclosureClient:
    """Gateway adapter; it has neither envelope keys nor disclosure DB rights."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def _result(self, response: httpx.Response) -> StorageCredentials:
        if response.status_code == 401:
            raise AuthenticationError("storage disclosure authentication failed")
        if response.status_code == 409:
            raise ConflictError("storage credential disclosure is unavailable or already consumed")
        if response.status_code != 200:
            raise RuntimeError("storage disclosure service rejected the request")
        return StorageCredentials.model_validate(response.json())

    async def disclose_user(self, authorization: str) -> StorageCredentials:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            return await self._result(
                await client.post("/v1/storage/credentials", headers={"authorization": authorization})
            )

    async def disclose_admin(self, cookie_value: str, user_id: UUID) -> StorageCredentials:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            return await self._result(
                await client.post(
                    f"/admin/api/v1/users/{user_id}/storage/credentials",
                    cookies={ADMIN_SESSION_COOKIE: cookie_value},
                )
            )


def create_storage_disclosure_app(repository: PostgresStorageDisclosureRepository) -> FastAPI:
    """Expose only the two no-store disclosure operations and health probes."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title="fs2 storage credential disclosure",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        if not await repository.ready():
            raise HTTPException(503, "database unavailable")
        return {"status": "ready"}

    @app.post("/v1/storage/credentials")
    async def disclose_user(authorization: str | None = Header(default=None)) -> JSONResponse:
        try:
            result = await repository.disclose_user(_bearer(authorization))
        except AuthenticationError:
            raise HTTPException(401, "invalid bearer token") from None
        except ConflictError:
            raise HTTPException(409, "storage credential disclosure is unavailable or already consumed") from None
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.post("/admin/api/v1/users/{user_id}/storage/credentials")
    async def disclose_admin(
        user_id: UUID,
        cookie_value: str | None = Cookie(default=None, alias=ADMIN_SESSION_COOKIE),
    ) -> JSONResponse:
        if cookie_value is None:
            raise HTTPException(401, "operator session required")
        try:
            result = await repository.disclose_admin(cookie_value, user_id)
        except AuthenticationError:
            raise HTTPException(401, "invalid operator session or storage target") from None
        except ConflictError:
            raise HTTPException(409, "storage credential disclosure is unavailable or already consumed") from None
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    return app
