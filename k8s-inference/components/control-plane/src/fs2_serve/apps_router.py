"""Apps API mounting; reuse the console's operator and tenant authorization."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from .access import AdminAccessService
from .access_models import OperatorPrincipal, OperatorRole
from .admin import AdminProblemError
from .admin_models import AdminContext, AdminEnvelope
from .apps import AppsService
from .apps_models import AppCreate, AppList, AppRun, AppRunList, AppSettings, AppSettingsUpdate, AppSummary, AppUsage
from .models import OperationStatus


def apps_router(
    service: AppsService,
    *,
    access: AdminAccessService,
    operator_dependency: Callable[..., Any],
    context_dependency: Callable[..., AdminContext],
    envelope: Callable[[Any, AdminContext], AdminEnvelope[Any]],
    problem_responses: dict[int | str, dict[str, Any]],
) -> APIRouter:
    router = APIRouter(prefix="/admin/api/v1/apps", dependencies=[Depends(operator_dependency)])
    context_parameter = Depends(context_dependency)

    def actor(request: Request) -> OperatorPrincipal:
        identity = getattr(request.state, "operator_principal", None)
        if not isinstance(identity, OperatorPrincipal):
            raise AdminProblemError(401, "operator_session_required", "operator session is required")
        return identity

    async def read(request: Request, action: str) -> str | None:
        return await access.authorize(actor(request), OperatorRole.VIEWER, action=action)

    async def write(request: Request, action: str) -> OperatorPrincipal:
        identity = actor(request)
        await access.authorize_global(identity, OperatorRole.OPERATOR, action=action)
        return identity

    @router.get("", response_model=AdminEnvelope[AppList], responses=problem_responses)
    async def listing(request: Request, context: AdminContext = context_parameter) -> Any:
        tenant = await read(request, "app.list")
        return envelope(await service.list(context, tenant), context)

    @router.post("", response_model=AdminEnvelope[AppSummary], responses=problem_responses, status_code=201)
    async def create(request: Request, body: AppCreate, context: AdminContext = context_parameter) -> Any:
        identity = await write(request, "app.create")
        return envelope(await service.create(body, identity, context), context)

    @router.get("/{app_id}", response_model=AdminEnvelope[AppSummary], responses=problem_responses)
    async def detail(request: Request, app_id: UUID, context: AdminContext = context_parameter) -> Any:
        tenant = await read(request, "app.read")
        return envelope(await service.summary(await service.require(app_id), context, tenant), context)

    @router.get("/{app_id}/settings", response_model=AdminEnvelope[AppSettings], responses=problem_responses)
    async def settings(request: Request, app_id: UUID, context: AdminContext = context_parameter) -> Any:
        tenant = await read(request, "app.settings.read")
        return envelope(await service.settings(app_id, context, tenant), context)

    @router.patch("/{app_id}/settings", response_model=AdminEnvelope[AppSettings], responses=problem_responses)
    async def save_settings(
        request: Request,
        app_id: UUID,
        body: AppSettingsUpdate,
        context: AdminContext = context_parameter,
    ) -> Any:
        identity = await write(request, "app.settings.update")
        return envelope(await service.update_settings(app_id, body, identity, context), context)

    @router.get("/{app_id}/runs", response_model=AdminEnvelope[AppRunList], responses=problem_responses)
    async def runs(
        request: Request,
        app_id: UUID,
        context: AdminContext = context_parameter,
        limit: int = Query(100, ge=1, le=200),
        cursor: str | None = Query(None, max_length=512),
        principal_id: str | None = Query(None, max_length=200),
        status: OperationStatus | None = None,
    ) -> Any:
        tenant = await read(request, "app.runs.list")
        return await service.runs(
            app_id, context, tenant_id=tenant, limit=limit, cursor=cursor, principal_id=principal_id, status=status
        )

    @router.get("/{app_id}/runs/{operation_id}", response_model=AdminEnvelope[AppRun], responses=problem_responses)
    async def run_detail(
        request: Request,
        app_id: UUID,
        operation_id: UUID,
        context: AdminContext = context_parameter,
    ) -> Any:
        tenant = await read(request, "app.run.read")
        return await service.run_detail(app_id, operation_id, context, tenant)

    @router.get("/{app_id}/usage", response_model=AdminEnvelope[AppUsage], responses=problem_responses)
    async def usage(request: Request, app_id: UUID, context: AdminContext = context_parameter) -> Any:
        tenant = await read(request, "app.usage.read")
        return envelope(await service.usage(app_id, context, tenant), context)

    return router
