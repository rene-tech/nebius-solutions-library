"""Users router using the existing session and context contract."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from .access_models import AdminApiKeyCreate, AdminApiKeyDisclosure, AdminApiKeyList, OperatorPrincipal
from .admin import AdminProblemError
from .admin_models import AdminContext, AdminEnvelope
from .user_models import InferenceUser, UserCreate, UserDetail, UserList, UserPatch
from .users import UserService


def user_router(
    *,
    service: UserService,
    operator_dependency: Callable[..., Any],
    context_dependency: Callable[..., Any],
    selected_context: Callable[[Any], AdminContext],
    envelope: Callable[[AdminContext, Any], AdminEnvelope[Any]],
    problem_responses: dict[int | str, dict[str, Any]],
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(operator_dependency)])
    context_dep = Depends(context_dependency)

    def identity(request: Request) -> OperatorPrincipal:
        value = getattr(request.state, "operator_principal", None)
        if not isinstance(value, OperatorPrincipal):
            raise AdminProblemError(401, "operator_session_required", "operator session is required")
        return value

    @router.get("/admin/api/v1/users", response_model=AdminEnvelope[UserList], responses=problem_responses)
    async def users(
        request: Request,
        params: Any = context_dep,
        tenant_id: str | None = Query(None, max_length=120),
        limit: int = Query(200, ge=1, le=1000),
    ) -> Any:
        context = selected_context(params)
        return envelope(context, await service.list(identity(request), context, tenant_id=tenant_id, limit=limit))

    @router.post(
        "/admin/api/v1/users", response_model=AdminEnvelope[InferenceUser], status_code=201, responses=problem_responses
    )
    async def create_user(request: Request, payload: UserCreate, params: Any = context_dep) -> Any:
        return envelope(selected_context(params), await service.create(identity(request), payload))

    @router.get("/admin/api/v1/users/{user_id}", response_model=AdminEnvelope[UserDetail], responses=problem_responses)
    async def detail(request: Request, user_id: UUID, params: Any = context_dep) -> Any:
        context = selected_context(params)
        return envelope(context, await service.detail(identity(request), user_id, context))

    @router.patch(
        "/admin/api/v1/users/{user_id}", response_model=AdminEnvelope[InferenceUser], responses=problem_responses
    )
    async def update(request: Request, user_id: UUID, payload: UserPatch, params: Any = context_dep) -> Any:
        return envelope(selected_context(params), await service.update(identity(request), user_id, payload))

    @router.get(
        "/admin/api/v1/users/{user_id}/keys", response_model=AdminEnvelope[AdminApiKeyList], responses=problem_responses
    )
    async def keys(request: Request, user_id: UUID, params: Any = context_dep) -> Any:
        context = selected_context(params)
        detail = await service.detail(identity(request), user_id, context)
        return envelope(context, AdminApiKeyList(items=detail.keys))

    @router.post(
        "/admin/api/v1/users/{user_id}/keys",
        response_model=AdminEnvelope[AdminApiKeyDisclosure],
        status_code=201,
        responses=problem_responses,
    )
    async def issue_key(request: Request, user_id: UUID, payload: AdminApiKeyCreate, params: Any = context_dep) -> Any:
        return envelope(selected_context(params), await service.issue_key(identity(request), user_id, payload))

    return router
