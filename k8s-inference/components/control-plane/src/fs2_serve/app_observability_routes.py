"""App-scoped resource observations using the existing operator session."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from .access import AdminAccessService
from .access_models import OperatorPrincipal, OperatorRole
from .admin import AdminProblemError
from .admin_models import AdminContext, AdminEnvelope
from .app_observability import AppObservabilityService
from .app_observability_models import AppContainers, AppLogs, AppMetrics
from .apps import AppsService


def app_observability_router(
    *,
    apps: AppsService,
    service: AppObservabilityService,
    access: AdminAccessService,
    operator_dependency: Callable[..., Any],
    context_dependency: Callable[..., AdminContext],
    envelope: Callable[[Any, AdminContext], AdminEnvelope[Any]],
    problem_responses: dict[int | str, dict[str, Any]],
) -> APIRouter:
    router = APIRouter(prefix="/admin/api/v1/apps", dependencies=[Depends(operator_dependency)])

    async def authorize(request: Request) -> None:
        identity = getattr(request.state, "operator_principal", None)
        if not isinstance(identity, OperatorPrincipal):
            raise AdminProblemError(401, "operator_session_required", "operator session is required")
        # Pod/device observations and unredacted runtime logs are app-wide,
        # unlike the tenant-filtered logical Runs and Usage tabs.
        await access.authorize_global(identity, OperatorRole.VIEWER, action="app.observability.read")

    @router.get("/{app_id}/metrics", response_model=AdminEnvelope[AppMetrics], responses=problem_responses)
    async def metrics(
        request: Request, app_id: UUID, context: Annotated[AdminContext, Depends(context_dependency)]
    ) -> Any:
        await authorize(request)
        return envelope(
            await service.metrics(
                await apps.resolve_observability(app_id),
                context.from_at,
                context.to_at,
            ),
            context,
        )

    @router.get("/{app_id}/containers", response_model=AdminEnvelope[AppContainers], responses=problem_responses)
    async def containers(
        request: Request, app_id: UUID, context: Annotated[AdminContext, Depends(context_dependency)]
    ) -> Any:
        await authorize(request)
        return envelope(await service.containers(await apps.resolve_observability(app_id)), context)

    @router.get("/{app_id}/logs", response_model=AdminEnvelope[AppLogs], responses=problem_responses)
    async def logs(
        request: Request,
        app_id: UUID,
        context: Annotated[AdminContext, Depends(context_dependency)],
        search: str = Query("", max_length=500),
        pod: str = Query("", max_length=253),
        container: str = Query("", max_length=253),
        limit: int = Query(200, ge=1, le=500),
        cursor: str | None = Query(None, pattern=r"^[0-9]{1,20}:[0-9]{1,4}$", max_length=25),
    ) -> Any:
        await authorize(request)
        return envelope(
            await service.logs(
                await apps.resolve_observability(app_id),
                context.from_at,
                context.to_at,
                search=search,
                pod_name=pod,
                container=container,
                limit=limit,
                cursor=cursor,
            ),
            context,
        )

    return router
