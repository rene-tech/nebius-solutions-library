"""Operator access to captured customer exchanges, including pre-admission errors."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from .access import AdminAccessService
from .access_models import OperatorPrincipal, OperatorRole
from .admin import AdminProblemError
from .admin_models import AdminContext, AdminEnvelope
from .apps import AppsService
from .request_debug import DebugExchange, DebugExchangeList, DebugStore, RetentionPreflight


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

    async def authorized_identity(request: Request) -> tuple[OperatorPrincipal, str | None]:
        identity = getattr(request.state, "operator_principal", None)
        if not isinstance(identity, OperatorPrincipal):
            raise AdminProblemError(401, "operator_session_required", "operator session is required")
        # Captured exchanges hold the credential-redacted customer request plus typed metadata
        # (response bodies are withheld), which is still sensitive customer data, so reads
        # require ADMIN rather than the lowest operator role. Tenant scoping is
        # preserved: a tenant-scoped admin still sees only their own captures.
        tenant = await access.authorize(identity, OperatorRole.ADMIN, action="request.debug.read")
        return identity, tenant

    async def authorize(request: Request) -> str | None:
        _, tenant = await authorized_identity(request)
        return tenant

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

    @router.get(
        "/requests/retention",
        response_model=AdminEnvelope[RetentionPreflight],
        responses=problem_responses,
    )
    async def retention(
        request: Request,
        context: Annotated[AdminContext, Depends(context_dependency)],
    ) -> Any:
        # Payload-free retention proof (oldest started_at + counts at the FIXED 90-day
        # cutoff). It exposes no payload and deletes nothing; it is the pre-rollout gate
        # that proves how many rows exceed the TTL before any (separately owned) purge.
        # ADMIN-gated and audited like a payload read even though it reveals no payload.
        # Registered before /requests/{exchange_id} so the literal path wins. The aggregate
        # is scoped to the caller's authorized tenant (None for a global admin), so a
        # tenant-scoped admin never sees cross-tenant counts/oldest metadata.
        identity, tenant = await authorized_identity(request)
        result = await store.retention_preflight(now=datetime.now(UTC), tenant_id=tenant)
        await access.record_read(
            identity,
            action="request.debug.read",
            target_type="request_debug_retention",
            target_id="preflight",
        )
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
        identity, tenant = await authorized_identity(request)
        model_id = await model_for(app_id)
        result = await store.get(exchange_id, tenant_id=tenant)
        if result is None or (model_id is not None and result.model_id != model_id):
            raise AdminProblemError(404, "request_debug_not_found", "captured request was not found")
        # Record every disclosure of a captured payload, not only denials.
        await access.record_read(
            identity,
            action="request.debug.read",
            target_type="request_debug",
            target_id=str(exchange_id),
        )
        return envelope(result, context)

    return router
