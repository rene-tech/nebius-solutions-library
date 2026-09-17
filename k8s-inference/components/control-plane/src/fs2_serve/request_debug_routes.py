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
from .request_debug_authorization import RequestDebugAuthorization


def request_debug_router(
    *,
    apps: AppsService,
    store: DebugStore,
    debug_authorization: RequestDebugAuthorization,
    access: AdminAccessService,
    operator_dependency: Callable[..., Any],
    context_dependency: Callable[..., AdminContext],
    envelope: Callable[[Any, AdminContext], AdminEnvelope[Any]],
    problem_responses: dict[int | str, dict[str, Any]],
) -> APIRouter:
    router = APIRouter(prefix="/admin/api/v1", dependencies=[Depends(operator_dependency)])

    async def audit(
        identity: OperatorPrincipal,
        *,
        outcome: str,
        target_id: str,
        grant: dict[str, Any] | None = None,
        reason: str | None = None,
        connection: Any | None = None,
    ) -> None:
        parameters = {
            "actor": identity.subject,
            "tenant_id": str(grant["tenant_id"]) if grant is not None else identity.tenant_id,
            "token_id": None,
            "action": "request.debug.read",
            "target_type": "request_debug",
            "target_id": target_id,
            "outcome": outcome,
            "detail": (
                {
                    "activation_payload_sha256": str(grant["activation_payload_sha256"]),
                    "app_id": str(grant["app_id"]),
                    "broker_id": str(grant["broker_id"]),
                    "expires_at": str(grant["expires_at"]),
                    "model_id": str(grant["model_id"]),
                    "session_id": str(grant["session_id"]),
                }
                if grant is not None
                else {"reason": reason or "authorization_failed"}
            ),
        }
        on_connection = getattr(access.store, "append_audit_event_on_connection", None)
        if connection is not None:
            if not callable(on_connection):
                raise RuntimeError("request-debug audit store lacks transaction-aware append")
            await on_connection(connection, **parameters)
            return
        await access.store.append_audit_event(**parameters)

    async def authorize(
        request: Request,
        app_id: UUID | None,
        model_id: str | None,
        *,
        target_id: str,
    ) -> tuple[str, str, UUID, dict[str, Any], OperatorPrincipal]:
        identity = getattr(request.state, "operator_principal", None)
        if not isinstance(identity, OperatorPrincipal):
            raise AdminProblemError(401, "operator_session_required", "operator session is required")
        grant: dict[str, Any] | None = None
        try:
            grant = dict(
                await debug_authorization.authorize(
                    request,
                    app_id=app_id,
                    public_model_id=model_id,
                )
            )
            signed_app_id = UUID(str(grant["app_id"]))
            signed_app = await apps.require(signed_app_id)
            if signed_app.public_model_id != grant["model_id"]:
                raise AdminProblemError(
                    403, "debug_scope_mismatch", "signed App and public model no longer match"
                )
            tenant = await access.authorize(
                identity,
                OperatorRole.VIEWER,
                action="request.debug.read",
                tenant_id=str(grant["tenant_id"]),
            )
            return tenant, str(grant["model_id"]), signed_app_id, grant, identity
        except (AdminProblemError, PermissionError) as exc:
            await audit(
                identity,
                outcome="failed",
                target_id=target_id,
                grant=grant,
                reason=getattr(exc, "code", type(exc).__name__),
            )
            raise

    async def scoped_model(
        request: Request, app_id: UUID | None, *, target_id: str
    ) -> tuple[str, str, dict[str, Any], OperatorPrincipal]:
        model_id = await model_for(app_id)
        tenant, signed_model, _signed_app_id, grant, identity = await authorize(
            request, app_id, model_id, target_id=target_id
        )
        return tenant, signed_model, grant, identity

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
        tenant, model_id, grant, identity = await scoped_model(
            request, app_id, target_id=str(app_id or "signed-app-list")
        )
        async def read(connection: Any | None) -> DebugExchangeList:
            try:
                read_list = (
                    getattr(store, "list_on_connection", None)
                    if connection is not None
                    else None
                )
                if connection is not None and not callable(read_list):
                    raise RuntimeError(
                        "request-debug store lacks transaction-aware list"
                    )
                result = await (
                    read_list(
                        connection,
                        model_id=model_id,
                        operation_id=operation_id,
                        tenant_id=tenant,
                        from_at=context.from_at,
                        to_at=context.to_at,
                        limit=limit,
                        cursor=cursor,
                    )
                    if callable(read_list)
                    else store.list(
                        model_id=model_id,
                        operation_id=operation_id,
                        tenant_id=tenant,
                        from_at=context.from_at,
                        to_at=context.to_at,
                        limit=limit,
                        cursor=cursor,
                    )
                )
            except Exception as error:
                failure = (
                    AdminProblemError(
                    400, "invalid_debug_cursor", "request log cursor is invalid"
                    )
                    if isinstance(error, ValueError)
                    else error
                )
                await audit(
                    identity,
                    outcome="failed",
                    target_id=str(app_id or "signed-app-list"),
                    grant=grant,
                    reason=getattr(failure, "code", type(failure).__name__),
                    connection=connection,
                )
                return failure  # type: ignore[return-value]
            await audit(
                identity,
                outcome="succeeded",
                target_id=str(app_id or "signed-app-list"),
                grant=grant,
                connection=connection,
            )
            return result

        try:
            result = await debug_authorization.execute(grant, read)
        except Exception as error:
            await audit(
                identity,
                outcome="failed",
                target_id=str(app_id or "signed-app-list"),
                grant=grant,
                reason=getattr(error, "code", type(error).__name__),
            )
            raise
        if isinstance(result, Exception):
            raise result
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
        tenant, model_id, grant, identity = await scoped_model(
            request, app_id, target_id=str(exchange_id)
        )
        async def read(connection: Any | None) -> DebugExchange:
            try:
                read_get = (
                    getattr(store, "get_on_connection", None)
                    if connection is not None
                    else None
                )
                if connection is not None and not callable(read_get):
                    raise RuntimeError("request-debug store lacks transaction-aware get")
                result = await (
                    read_get(connection, exchange_id, tenant_id=tenant)
                    if callable(read_get)
                    else store.get(exchange_id, tenant_id=tenant)
                )
                if result is None or result.model_id != model_id:
                    raise AdminProblemError(
                        404,
                        "request_debug_not_found",
                        "captured request was not found",
                    )
            except Exception as error:
                await audit(
                    identity,
                    outcome="failed",
                    target_id=str(exchange_id),
                    grant=grant,
                    reason=getattr(error, "code", type(error).__name__),
                    connection=connection,
                )
                return error  # type: ignore[return-value]
            await audit(
                identity,
                outcome="succeeded",
                target_id=str(exchange_id),
                grant=grant,
                connection=connection,
            )
            return result

        try:
            result = await debug_authorization.execute(grant, read)
        except Exception as error:
            await audit(
                identity,
                outcome="failed",
                target_id=str(exchange_id),
                grant=grant,
                reason=getattr(error, "code", type(error).__name__),
            )
            raise
        if isinstance(result, Exception):
            raise result
        return envelope(result, context)

    return router
