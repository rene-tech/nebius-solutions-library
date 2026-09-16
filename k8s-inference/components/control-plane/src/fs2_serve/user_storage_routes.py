"""Storage belongs to the authenticated user; never accept a caller's S3 identity."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from .access_models import OperatorRole
from .user_storage_models import StoragePolicy


def user_storage_router(*, service: Any, users: Any, operator: Any, principal: Any, envelope: Any) -> APIRouter:
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
        current = storage()
        if (await current.policy(identity.tenant_id)).mode == "disabled":
            raise HTTPException(403, "customer storage is disabled for this tenant")
        result = await current.repository.disclose(identity.tenant_id, identity.principal_id)
        return JSONResponse(result.model_dump(), headers={"Cache-Control": "no-store"})

    @router.get("/admin/api/v1/users/{user_id}/storage")
    async def user_storage(user_id: UUID, identity: Any = operator_dep) -> Any:
        user = await users._get(identity, user_id, OperatorRole.VIEWER)
        return envelope(await storage().view(user.tenant_id, user.principal_id))

    @router.post("/admin/api/v1/users/{user_id}/storage/credentials")
    async def user_credentials(user_id: UUID, identity: Any = operator_dep) -> JSONResponse:
        user = await users._get(identity, user_id, OperatorRole.ADMIN)
        if not user.enabled or (await storage().policy(user.tenant_id)).mode == "disabled":
            raise HTTPException(403, "customer storage is disabled")
        result = await storage().repository.disclose(user.tenant_id, user.principal_id)
        return JSONResponse(envelope(result).model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @router.get("/admin/api/v1/tenants/{tenant_id}/storage")
    async def tenant_policy(tenant_id: str, identity: Any = operator_dep) -> Any:
        await users.access.authorize(identity, OperatorRole.VIEWER, action="storage.read", tenant_id=tenant_id)
        return envelope(await storage().policy(tenant_id))

    @router.put("/admin/api/v1/tenants/{tenant_id}/storage")
    async def configure(tenant_id: str, payload: StoragePolicy, identity: Any = operator_dep) -> Any:
        await users.access.authorize(identity, OperatorRole.ADMIN, action="storage.configure", tenant_id=tenant_id)
        return envelope(await storage().configure(tenant_id, payload))

    return router
