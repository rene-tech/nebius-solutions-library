"""Storage belongs to the authenticated user; never accept a caller's S3 identity."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from .access_models import OperatorRole
from .admin_models import AdminEnvelope
from .models import Scope
from .user_storage_models import StorageCredentials, StoragePolicy, UserStorage


def user_storage_router(
    *,
    service: Any,
    users: Any,
    operator: Any,
    principal: Any,
    envelope: Any,
    audit: Any,
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

    @router.get("/v1/storage")
    async def own_storage(identity: Any = principal_dep) -> Any:
        await own_user(identity)
        return await storage().view(identity.tenant_id, identity.principal_id)

    @router.post("/v1/storage/credentials")
    async def own_credentials(identity: Any = principal_dep) -> JSONResponse:
        await own_user(identity)
        identity.require(Scope.STORAGE_CREDENTIALS)
        current = storage()
        if (await current.policy(identity.tenant_id)).mode == "disabled":
            raise HTTPException(403, "customer storage is disabled for this tenant")
        result = await current.repository.disclose(identity.tenant_id, identity.principal_id)
        await audit.append_audit_event(
            actor=identity.principal_id,
            tenant_id=identity.tenant_id,
            token_id=identity.token_id,
            action="storage.credentials.disclose",
            target_type="user_storage",
            target_id=identity.principal_id,
            outcome="succeeded",
        )
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @router.post("/v1/storage/credentials/rotate")
    async def rotate_own_credentials(identity: Any = principal_dep) -> Any:
        await own_user(identity)
        identity.require(Scope.STORAGE_CREDENTIALS)
        result = await storage().rotate(identity.tenant_id, identity.principal_id)
        await audit.append_audit_event(
            actor=identity.principal_id,
            tenant_id=identity.tenant_id,
            token_id=identity.token_id,
            action="storage.credentials.rotate",
            target_type="user_storage",
            target_id=identity.principal_id,
            outcome="succeeded",
        )
        return result

    @router.delete("/v1/storage/credentials")
    async def revoke_own_credentials(identity: Any = principal_dep) -> Any:
        await own_user(identity)
        identity.require(Scope.STORAGE_CREDENTIALS)
        result = await storage().revoke(identity.tenant_id, identity.principal_id)
        await audit.append_audit_event(
            actor=identity.principal_id,
            tenant_id=identity.tenant_id,
            token_id=identity.token_id,
            action="storage.credentials.revoke",
            target_type="user_storage",
            target_id=identity.principal_id,
            outcome="succeeded",
        )
        return result

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
    async def user_credentials(user_id: UUID, identity: Any = operator_dep) -> JSONResponse:
        user = await users._get(identity, user_id, OperatorRole.ADMIN)
        if not user.enabled or (await storage().policy(user.tenant_id)).mode == "disabled":
            raise HTTPException(403, "customer storage is disabled")
        result = await storage().repository.disclose(user.tenant_id, user.principal_id)
        await audit.append_audit_event(
            actor=identity.subject,
            tenant_id=user.tenant_id,
            token_id=None,
            action="storage.credentials.disclose",
            target_type="user_storage",
            target_id=user.principal_id,
            outcome="succeeded",
        )
        return JSONResponse(envelope(result).model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @router.post(
        "/admin/api/v1/users/{user_id}/storage/credentials/rotate",
        response_model=AdminEnvelope[UserStorage],
        responses=problem_responses,
    )
    async def rotate_user_credentials(user_id: UUID, identity: Any = operator_dep) -> Any:
        user = await users._get(identity, user_id, OperatorRole.ADMIN)
        result = await storage().rotate(user.tenant_id, user.principal_id)
        await audit.append_audit_event(
            actor=identity.subject,
            tenant_id=user.tenant_id,
            token_id=None,
            action="storage.credentials.rotate",
            target_type="user_storage",
            target_id=user.principal_id,
            outcome="succeeded",
        )
        return envelope(result)

    @router.delete(
        "/admin/api/v1/users/{user_id}/storage/credentials",
        response_model=AdminEnvelope[UserStorage],
        responses=problem_responses,
    )
    async def revoke_user_credentials(user_id: UUID, identity: Any = operator_dep) -> Any:
        user = await users._get(identity, user_id, OperatorRole.ADMIN)
        result = await storage().revoke(user.tenant_id, user.principal_id)
        await audit.append_audit_event(
            actor=identity.subject,
            tenant_id=user.tenant_id,
            token_id=None,
            action="storage.credentials.revoke",
            target_type="user_storage",
            target_id=user.principal_id,
            outcome="succeeded",
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
