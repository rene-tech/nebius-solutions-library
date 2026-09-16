"""Storage belongs to the authenticated user; never accept a caller's S3 identity."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from .access_models import OperatorRole
from .admin_models import AdminEnvelope
from .auth import AuthenticationError, OperatorSessionService
from .models import Scope
from .user_storage_disclosure import ADMIN_SESSION_COOKIE
from .user_storage_models import StorageCredentials, StoragePolicy, UserStorage


def user_storage_router(
    *,
    service: Any,
    disclosure: Any,
    users: Any,
    operator: Any,
    principal: Any,
    envelope: Any,
    problem_responses: dict[int | str, Any],
) -> APIRouter:
    router = APIRouter()
    principal_dep = Depends(principal)
    operator_dep = Depends(operator)

    def storage() -> Any:
        if service is None:
            raise HTTPException(503, "customer storage is not configured")
        return service

    async def own_user(identity: Any) -> Any:
        configured = await users.repository.configured(identity.tenant_id, identity.principal_id)
        if configured is not None and not configured.enabled:
            raise HTTPException(403, "inference user is disabled")
        return identity

    def request_idempotency(value: str | None) -> UUID:
        if value is None:
            return uuid4()
        try:
            return UUID(value)
        except ValueError as exc:
            raise HTTPException(422, "Idempotency-Key must be a UUID") from exc

    def operator_session(cookie_value: str | None) -> UUID:
        if cookie_value is None:
            raise HTTPException(401, "operator session required")
        try:
            return OperatorSessionService._parse(cookie_value)
        except AuthenticationError as exc:
            raise HTTPException(401, "invalid operator session") from exc

    @router.get("/v1/storage")
    async def own_storage(identity: Any = principal_dep) -> Any:
        await own_user(identity)
        return await storage().view(identity.tenant_id, identity.principal_id)

    @router.post("/v1/storage/credentials")
    async def own_credentials(
        identity: Any = principal_dep,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        await own_user(identity)
        identity.require(Scope.STORAGE_CREDENTIALS)
        current = storage()
        if (await current.policy(identity.tenant_id)).mode == "disabled":
            raise HTTPException(403, "customer storage is disabled for this tenant")
        if authorization is None:
            raise HTTPException(401, "bearer token required")
        result = await disclosure.disclose_user(authorization)
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @router.post("/v1/storage/credentials/rotate")
    async def rotate_own_credentials(
        identity: Any = principal_dep,
        idempotency: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Any:
        await own_user(identity)
        identity.require(Scope.STORAGE_CREDENTIALS)
        return await storage().rotate(
            identity.tenant_id,
            identity.principal_id,
            token_id=identity.token_id,
            operator_session_id=None,
            idempotency_key=request_idempotency(idempotency),
        )

    @router.delete("/v1/storage/credentials")
    async def revoke_own_credentials(
        identity: Any = principal_dep,
        idempotency: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Any:
        await own_user(identity)
        identity.require(Scope.STORAGE_CREDENTIALS)
        return await storage().revoke(
            identity.tenant_id,
            identity.principal_id,
            token_id=identity.token_id,
            operator_session_id=None,
            idempotency_key=request_idempotency(idempotency),
        )

    @router.get(
        "/admin/api/v1/users/{user_id}/storage",
        response_model=AdminEnvelope[UserStorage],
        responses=problem_responses,
    )
    async def user_storage(user_id: UUID, identity: Any = operator_dep) -> Any:
        user = await users._get(identity, user_id, OperatorRole.VIEWER)
        return envelope(await storage().view(user.tenant_id, user.principal_id))

    @router.post(
        "/admin/api/v1/users/{user_id}/storage/credentials",
        response_model=AdminEnvelope[StorageCredentials],
        responses=problem_responses,
    )
    async def user_credentials(
        user_id: UUID,
        identity: Any = operator_dep,
        cookie_value: str | None = Cookie(default=None, alias=ADMIN_SESSION_COOKIE),
    ) -> JSONResponse:
        user = await users._get(identity, user_id, OperatorRole.ADMIN)
        if not user.enabled or (await storage().policy(user.tenant_id)).mode == "disabled":
            raise HTTPException(403, "customer storage is disabled")
        if cookie_value is None:
            raise HTTPException(401, "operator session required")
        result = await disclosure.disclose_admin(cookie_value, user_id)
        return JSONResponse(envelope(result).model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @router.post(
        "/admin/api/v1/users/{user_id}/storage/credentials/rotate",
        response_model=AdminEnvelope[UserStorage],
        responses=problem_responses,
    )
    async def rotate_user_credentials(
        user_id: UUID,
        identity: Any = operator_dep,
        cookie_value: str | None = Cookie(default=None, alias=ADMIN_SESSION_COOKIE),
        idempotency: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Any:
        user = await users._get(identity, user_id, OperatorRole.ADMIN)
        result = await storage().rotate(
            user.tenant_id,
            user.principal_id,
            token_id=None,
            operator_session_id=operator_session(cookie_value),
            idempotency_key=request_idempotency(idempotency),
        )
        return envelope(result)

    @router.delete(
        "/admin/api/v1/users/{user_id}/storage/credentials",
        response_model=AdminEnvelope[UserStorage],
        responses=problem_responses,
    )
    async def revoke_user_credentials(
        user_id: UUID,
        identity: Any = operator_dep,
        cookie_value: str | None = Cookie(default=None, alias=ADMIN_SESSION_COOKIE),
        idempotency: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Any:
        user = await users._get(identity, user_id, OperatorRole.ADMIN)
        result = await storage().revoke(
            user.tenant_id,
            user.principal_id,
            token_id=None,
            operator_session_id=operator_session(cookie_value),
            idempotency_key=request_idempotency(idempotency),
        )
        return envelope(result)

    @router.get(
        "/admin/api/v1/tenants/{tenant_id}/storage",
        response_model=AdminEnvelope[StoragePolicy],
        responses=problem_responses,
    )
    async def tenant_policy(tenant_id: str, identity: Any = operator_dep) -> Any:
        await users.access.authorize(identity, OperatorRole.VIEWER, action="storage.read", tenant_id=tenant_id)
        return envelope(await storage().policy(tenant_id))

    @router.put(
        "/admin/api/v1/tenants/{tenant_id}/storage",
        response_model=AdminEnvelope[StoragePolicy],
        responses=problem_responses,
    )
    async def configure(tenant_id: str, payload: StoragePolicy, identity: Any = operator_dep) -> Any:
        await users.access.authorize(identity, OperatorRole.ADMIN, action="storage.configure", tenant_id=tenant_id)
        return envelope(await storage().configure(tenant_id, payload))

    return router
