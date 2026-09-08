"""Simple capacity projection alongside the retained advanced diagnostics."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request

from .access import AdminAccessService
from .access_models import OperatorPrincipal, OperatorRole
from .admin import AdminProblemError
from .admin_models import AdminEnvelope
from .capacity_summary import CapacitySummary, CapacitySummaryService


def capacity_summary_router(
    *,
    service: CapacitySummaryService,
    access: AdminAccessService,
    operator_dependency: Callable[..., Any],
    context_dependency: Callable[..., Any],
    selected_context: Callable[..., Any],
    problem_responses: dict[int | str, dict[str, Any]],
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(operator_dependency)])
    context_dep = Depends(context_dependency)

    @router.get(
        "/admin/api/v1/capacity/summary", response_model=AdminEnvelope[CapacitySummary], responses=problem_responses
    )
    async def summary(request: Request, params: Any = context_dep) -> Any:
        identity = getattr(request.state, "operator_principal", None)
        if not isinstance(identity, OperatorPrincipal):
            raise AdminProblemError(401, "operator_session_required", "operator session is required")
        await access.authorize_global(identity, OperatorRole.VIEWER, action="capacity.read")
        return await service.summary(selected_context(params))

    return router
