"""Operator-only customer and workbench APIs; standard session/CSRF enforcement."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from .access_models import OperatorRole
from .workbench_models import CustomerProfile, WorkbenchCommand, WorkbenchRegistration


def workbench_router(*, service: Any, operator: Any, context: Any, envelope: Any) -> APIRouter:
    router = APIRouter()
    operator_dep, context_dep = Depends(operator), Depends(context)

    @router.get("/admin/api/v1/customers")
    async def customers(identity: Any = operator_dep, params: Any = context_dep) -> Any:
        return envelope(await service.customers(identity, params), params)

    @router.get("/admin/api/v1/customers/{tenant_id}")
    async def customer(tenant_id: str, identity: Any = operator_dep, params: Any = context_dep) -> Any:
        result = await service.customers(identity, params, tenant_id)
        if not result["items"]:
            raise HTTPException(404, "customer not found")
        return envelope({**result, "customer": result["items"][0]}, params)

    @router.put("/admin/api/v1/customers/{tenant_id}/profile")
    async def profile(
        tenant_id: str, value: CustomerProfile, identity: Any = operator_dep, params: Any = context_dep
    ) -> Any:
        return envelope(await service.save_profile(identity, tenant_id, value), params)

    @router.get("/admin/api/v1/workbench-inventory")
    async def inventory(identity: Any = operator_dep, params: Any = context_dep) -> Any:
        return envelope(await service.inventory_view(identity), params)

    @router.post("/admin/api/v1/workbench-inventory/refresh")
    async def refresh(identity: Any = operator_dep, params: Any = context_dep) -> Any:
        await service.users.access.authorize_global(identity, OperatorRole.OPERATOR, action="workbench.refresh")
        if service.inventory is None:
            raise HTTPException(503, "cloud inventory is not configured")
        try:
            result = await service.inventory.refresh()
        except Exception as exc:
            raise HTTPException(503, "cloud inventory refresh failed; previous observations retained") from exc
        return envelope(result, params)

    @router.post("/admin/api/v1/workbenches", status_code=201)
    async def register(value: WorkbenchRegistration, identity: Any = operator_dep, params: Any = context_dep) -> Any:
        return envelope(await service.register(identity, value), params)

    @router.post("/admin/api/v1/workbenches/{workbench_id}/operations", status_code=202)
    async def command(
        workbench_id: UUID, value: WorkbenchCommand, identity: Any = operator_dep, params: Any = context_dep
    ) -> Any:
        return envelope(await service.command(identity, workbench_id, value), params)

    return router
