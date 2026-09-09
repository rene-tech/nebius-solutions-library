"""Operator access to captured customer exchanges, including pre-admission errors."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from .access import AdminAccessService
from .access_models import OperatorPrincipal, OperatorRole
from .admin import AdminProblemError
from .admin_models import AdminContext, AdminEnvelope
from .apps import AppsService
from .request_debug import DebugExchange, DebugExchangeList, DebugStore


def request_debug_router(
    *,
    apps: AppsService,
    store: DebugStore,
    access: AdminAccessService,
    operator_dependency: Callable[..., Any],
    context_dependency: Callable[..., AdminContext],
    envelope: Callable[[Any, AdminContext], AdminEnvelope[Any]],
    problem_responses: dict[int | str, dict[str, Any]],
) -> APIRouter:
    router = APIRouter(prefix="/admin/api/v1", dependencies=[Depends(operator_dependency)])

    async def authorize(request: Request) -> str | None:
        identity = getattr(request.state, "operator_principal", None)
        if not isinstance(identity, OperatorPrincipal):
            raise AdminProblemError(401, "operator_session_required", "operator session is required")
        return await access.authorize(identity, OperatorRole.VIEWER, action="request.debug.read")

    async def model_for(app_id: UUID | None) -> str | None:
        if app_id is None:
            return None
        return (await apps.require(app_id)).public_model_id

    @router.get("/requests", response_model=AdminEnvelope[DebugExchangeList], responses=problem_responses)
    @router.get("/apps/{app_id}/requests", response_model=AdminEnvelope[DebugExchangeList], responses=problem_responses)
    async def listing(
        request: Request,
        context: Annotated[AdminContext, Depends(context_dependency)],
        app_id: UUID | None = None,
        operation_id: UUID | None = None,
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = Query(None, max_length=512),
    ) -> Any:
        tenant = await authorize(request)
        try:
            result = await store.list(
                model_id=await model_for(app_id),
                operation_id=operation_id,
                tenant_id=tenant,
                from_at=context.from_at,
                to_at=context.to_at,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as error:
            raise AdminProblemError(400, "invalid_debug_cursor", "request log cursor is invalid") from error
        return envelope(result, context)

    @router.get("/requests/{exchange_id}", response_model=AdminEnvelope[DebugExchange], responses=problem_responses)
    @router.get(
        "/apps/{app_id}/requests/{exchange_id}",
        response_model=AdminEnvelope[DebugExchange],
        responses=problem_responses,
    )
    async def detail(
        request: Request,
        exchange_id: UUID,
        context: Annotated[AdminContext, Depends(context_dependency)],
        app_id: UUID | None = None,
    ) -> Any:
        tenant = await authorize(request)
        model_id = await model_for(app_id)
        result = await store.get(exchange_id, tenant_id=tenant)
        if result is None or (model_id is not None and result.model_id != model_id):
            raise AdminProblemError(404, "request_debug_not_found", "captured request was not found")
        return envelope(result, context)

    return router
