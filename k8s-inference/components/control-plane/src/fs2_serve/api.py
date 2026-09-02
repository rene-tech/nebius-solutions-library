"""FastAPI public/admin surface for durable fs2-serve admission."""

import json
import logging
import math
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Send
from starlette.types import Scope as ASGIScope

from .access import AdminAccessService
from .access_models import (
    BOOTSTRAP_OPERATOR_PRINCIPAL_ID,
    AdminApiKey,
    AdminApiKeyCreate,
    AdminApiKeyDisclosure,
    AdminApiKeyList,
    AdminApiKeyPolicyPatch,
    AdminApiKeyRotate,
    AdminAuditList,
    AdminPrincipalList,
    OperatorPrincipal,
    OperatorPrincipalCreate,
    OperatorPrincipalPatch,
    OperatorRole,
    OperatorSession,
    OperatorSessionHandoff,
)
from .activation_health import activation_set
from .admin import AdminProblemError, AdminReadService
from .admin_models import (
    AcademicAssetReadinessList,
    AdminCapacity,
    AdminContext,
    AdminContextData,
    AdminEnvelope,
    AdminMeta,
    AdminModelDetail,
    AdminModelList,
    AdminModelState,
    AdminObservability,
    AdminOperationDetail,
    AdminOperationList,
    AdminOverview,
    AdminProblem,
    AdminSource,
    AdminSourceState,
)
from .admission import AdmissionService
from .auth import (
    MAX_PAT_LENGTH,
    AuthenticationError,
    OperatorSessionService,
    TokenService,
    require_operation_access,
)
from .configuration import ConfigurationService
from .configuration_routes import configuration_router
from .model_deployment_admin import ModelDeploymentReadService, model_deployment_read_router
from .model_deployment_bridge import ModelDeploymentRuntimeBridge
from .model_deployment_mutation import ModelDeploymentMutationService, model_deployment_mutation_router
from .model_deployment_preview import ModelDeploymentPreviewService, model_deployment_preview_router
from .models import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_MODEL_ID_LENGTH,
    MIN_IDEMPOTENCY_KEY_LENGTH,
    AdmissionRequest,
    OperationStatus,
    OperationView,
    Principal,
    Scope,
    TokenCreate,
    TokenIssued,
    TokenView,
)
from .registry import OperationalModel, Registry, RegistryError
from .route_revalidation import RouteRevalidator
from .settings import Settings
from .store import (
    BudgetExceededError,
    ConcurrencyExceededError,
    ConflictError,
    NotFoundError,
    RateLimitExceededError,
    Store,
)
from .telemetry import Metrics

LOGGER = logging.getLogger("fs2_serve.access")
IDENTITY_HEADERS = {
    b"x-fs2-tenant",
    b"x-fs2-principal",
    b"x-fs2-token-id",
    b"x-fs2-model-scope",
    b"x-fs2-accounting-id",
}
HEADER_VALUE_LIMITS = {
    b"idempotency-key": MAX_IDEMPOTENCY_KEY_LENGTH,
    b"x-fs2-wait-seconds": 32,
    b"x-fs2-deadline-seconds": 32,
}
ADMIN_SESSION_COOKIE = "__Host-fs2_admin_session"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NativeInvocation(StrictModel):
    operation: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9._-]*$")
    payload: dict[str, Any]


class TokenCreateRequest(TokenCreate):
    pass


class AdminContextParameters(StrictModel):
    project: str | None = None
    cluster: str | None = None
    region: str | None = None
    from_at: datetime | None = None
    to_at: datetime | None = None
    timezone: str = "UTC"


def _admin_context_parameters(
    project: Annotated[str | None, Query(max_length=128)] = None,
    cluster: Annotated[str | None, Query(max_length=128)] = None,
    region: Annotated[str | None, Query(max_length=64)] = None,
    from_at: Annotated[datetime | None, Query(alias="from")] = None,
    to_at: Annotated[datetime | None, Query(alias="to")] = None,
    timezone: Annotated[str, Query(min_length=1, max_length=64)] = "UTC",
) -> AdminContextParameters:
    return AdminContextParameters(
        project=project,
        cluster=cluster,
        region=region,
        from_at=from_at,
        to_at=to_at,
        timezone=timezone,
    )


@dataclass
class AppRuntime:
    settings: Settings
    registry: Registry
    store: Store
    tokens: TokenService
    admission: AdmissionService
    metrics: Metrics
    admin_token: bytes
    operator_sessions: OperatorSessionService
    owns_store: bool = True
    route_revalidator: RouteRevalidator | None = None
    admin_read: AdminReadService | None = None
    configuration: ConfigurationService | None = None
    model_deployment_preview: ModelDeploymentPreviewService | None = None
    model_deployment_read: ModelDeploymentReadService | None = None
    model_deployment_mutation: ModelDeploymentMutationService | None = None
    model_deployment_bridge: ModelDeploymentRuntimeBridge | None = None

    async def revalidate_routes(self) -> bool:
        if self.route_revalidator is not None and not await self.route_revalidator.refresh():
            return False
        dynamic_healthy = True
        if self.model_deployment_bridge is not None:
            dynamic_healthy = await self.model_deployment_bridge.refresh()
        return dynamic_healthy and bool(self.registry.validation_health()["healthy"])


class TrustedEdgeMiddleware:
    """Enforce the exact public authority and discard caller identity headers."""

    def __init__(
        self,
        app: ASGIApp,
        max_request_bytes: int,
        allowed_hosts: tuple[str, ...],
        allowed_origins: tuple[str, ...],
    ) -> None:
        self.app = app
        self.max_request_bytes = max_request_bytes
        self.allowed_hosts = frozenset(allowed_hosts)
        self.allowed_origins = frozenset(allowed_origins)

    @staticmethod
    def _public_path(path: str) -> bool:
        return (
            path == "/v1"
            or path.startswith("/v1/")
            or path == "/mcp"
            or path == "/admin/api/v1"
            or path.startswith("/admin/api/v1/")
            or path == "/.well-known/oauth-protected-resource"
            or path == "/.well-known/oauth-protected-resource/mcp"
        )

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = [(name, value) for name, value in scope.get("headers", []) if name.lower() not in IDENTITY_HEADERS]
        scope = dict(scope)
        scope["headers"] = headers
        if self._public_path(str(scope.get("path", ""))):
            try:
                hosts = [value.decode("ascii") for name, value in headers if name.lower() == b"host"]
                origins = [value.decode("ascii") for name, value in headers if name.lower() == b"origin"]
            except UnicodeDecodeError:
                hosts = []
                origins = []
            if len(hosts) != 1 or hosts[0] not in self.allowed_hosts:
                await Response("Invalid Host header", status_code=421)(scope, receive, send)
                return
            if len(origins) > 1 or (origins and origins[0] not in self.allowed_origins):
                await Response("Invalid Origin header", status_code=403)(scope, receive, send)
                return
        for name, value in headers:
            limit = HEADER_VALUE_LIMITS.get(name.lower())
            if limit is not None and len(value) > limit:
                response = _error(400, "invalid_request_header", "request header exceeds limit")
                await response(scope, receive, send)
                return
            if name.lower() == b"content-length":
                try:
                    if int(value) > self.max_request_bytes:
                        response = JSONResponse(
                            {"error": {"type": "request_too_large", "message": "request body exceeds limit"}},
                            status_code=413,
                        )
                        await response(scope, receive, send)
                        return
                except ValueError:
                    pass

        total = 0

        async def bounded_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_request_bytes:
                    raise HTTPException(status_code=413, detail="request body exceeds limit")
            return message

        await self.app(scope, bounded_receive, send)


def _bearer(value: str | None) -> str:
    if value is None or len(value) > MAX_PAT_LENGTH + 7 or not value.startswith("Bearer ") or not value[7:]:
        raise AuthenticationError("bearer token required")
    return value[7:]


def _error(status_code: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"type": kind, "message": message}},
        status_code=status_code,
        headers={"cache-control": "no-store"},
    )


def _model_view(model: OperationalModel) -> dict[str, Any]:
    projection = model.gateway.qualification
    runtime_origin = None if projection is None else projection["runtime_origin"]
    qualification = (
        None
        if projection is None
        else {
            "kind": "reviewed-evidence-snapshot",
            "authority": projection["qualification_authority"],
            "observed_at": projection["observed_at"],
            "states": dict(projection["states"]),
        }
    )
    return {
        "id": model.id,
        "object": "model",
        "created": 0,
        "owned_by": "fs2-serve",
        "display_name": model.gateway.display_name,
        "revision": model.model_revision,
        "enabled": model.enabled,
        "capabilities": sorted(model.gateway.protocols),
        "operations": sorted(model.gateway.policy_operations),
        "execution_mode": model.gateway.execution_mode,
        "activation": model.activation_mechanism,
        "gpu_class": model.gateway.gpu_class,
        "gpu_count": model.gateway.gpu_allocation_count,
        "active_runtime": (
            None
            if runtime_origin is None
            else {
                "variant_id": runtime_origin["variant_id"],
                "kind": runtime_origin["kind"],
                "source_kind": runtime_origin["source_kind"],
                "repository": runtime_origin["repository"],
                "relationship": runtime_origin["relationship"],
                "nim_artifact_parity": runtime_origin["nim_artifact_parity"],
            }
        ),
        "qualification": qualification,
        "policy": {
            "license_id": model.gateway.license_id,
            "non_clinical": model.gateway.non_clinical,
            "commercial_use": model.gateway.commercial_use,
        },
    }


def _operation_headers(operation: OperationView) -> dict[str, str]:
    return {
        "x-fs2-operation-id": str(operation.id),
        "x-fs2-idempotent-replay": str(operation.reused).lower(),
        "cache-control": "no-store",
    }


def _validate_model_id(model_id: str) -> str:
    if not 1 <= len(model_id) <= MAX_MODEL_ID_LENGTH:
        raise HTTPException(status_code=400, detail="model identifier length is invalid")
    return model_id


async def _operation_response(runtime: AppRuntime, operation: OperationView) -> Response:
    headers = _operation_headers(operation)
    if operation.status.value == "succeeded" and operation.result_available:
        result = await runtime.store.get_operation_result(operation.id, tenant_id=operation.tenant_id)
        return JSONResponse(result.result, status_code=operation.http_status or 200, headers=headers)
    if operation.status.terminal:
        if operation.status.value == "succeeded" and not operation.result_available:
            return _error(409, "result_expired", "operation result is no longer available")
        status_code = operation.http_status or (409 if operation.status.value == "cancelled" else 502)
        response = JSONResponse(
            {
                "error": {
                    "type": operation.error_code or operation.outcome or "operation_failed",
                    "message": operation.error_detail or "operation did not complete successfully",
                },
                "operation": operation.model_dump(mode="json"),
            },
            status_code=status_code,
            headers=headers,
        )
        return response
    return JSONResponse(
        operation.model_dump(mode="json"),
        status_code=status.HTTP_202_ACCEPTED,
        headers={
            **headers,
            "location": f"/v1/operations/{operation.id}",
            "retry-after": "1",
        },
    )


def create_app(runtime: AppRuntime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if runtime.route_revalidator is not None:
            await runtime.route_revalidator.start()
        if runtime.model_deployment_bridge is not None:
            await runtime.model_deployment_bridge.start()
        if runtime.settings.run_workers:
            await runtime.admission.start()
        try:
            yield
        finally:
            if runtime.model_deployment_bridge is not None:
                await runtime.model_deployment_bridge.close()
            if runtime.route_revalidator is not None:
                await runtime.route_revalidator.close()
            await runtime.admission.close()
            if runtime.owns_store:
                await runtime.store.close()

    app = FastAPI(
        title="fs2-serve control plane",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/internal/openapi.json",
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    admin_read = runtime.admin_read or AdminReadService(registry=runtime.registry, store=runtime.store)
    admin_access = AdminAccessService(runtime.store, runtime.tokens)
    allowed_hosts, allowed_origins = runtime.settings.public_transport_allowlists()
    app.add_middleware(
        TrustedEdgeMiddleware,
        max_request_bytes=runtime.settings.max_request_bytes,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        started = time.monotonic()
        response = await call_next(request)
        principal = getattr(request.state, "principal", None)
        record = {
            "event": "http_request",
            "method": request.method,
            "path": request.url.path[:256],
            "status": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "principal_id": principal.principal_id if principal else None,
            "tenant_id": principal.tenant_id if principal else None,
            "token_id": str(principal.token_id) if principal else None,
        }
        LOGGER.info(json.dumps(record, separators=(",", ":")))
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("cache-control", "no-store")
        return response

    async def principal(request: Request, authorization: Annotated[str | None, Header()] = None) -> Principal:
        try:
            value = await runtime.tokens.verify(_bearer(authorization))
        except AuthenticationError:
            runtime.metrics.auth_failures.labels("invalid_token").inc()
            raise HTTPException(
                status_code=401,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": 'Bearer realm="fs2-serve"'},
            ) from None
        request.state.principal = value
        return value

    async def bootstrap_failure() -> None:
        try:
            await runtime.store.append_audit_event(
                actor="anonymous",
                tenant_id=None,
                token_id=None,
                action="session.bootstrap",
                target_type="operator_session",
                target_id="unresolved",
                outcome="failed",
                detail={"reason": "invalid_bootstrap_credential"},
            )
        except (OSError, RuntimeError, ValueError):
            pass

    async def admin(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
        try:
            candidate = _bearer(authorization).encode()
        except AuthenticationError:
            if request.url.path.startswith("/admin/api/v1"):
                await bootstrap_failure()
                raise AdminProblemError(
                    401,
                    "admin_authentication_required",
                    "admin authentication is required",
                ) from None
            raise HTTPException(status_code=401, detail="admin bearer authentication failed") from None
        if not secrets.compare_digest(candidate, runtime.admin_token):
            if request.url.path.startswith("/admin/api/v1"):
                await bootstrap_failure()
                raise AdminProblemError(
                    401,
                    "admin_authentication_required",
                    "admin authentication is required",
                )
            raise HTTPException(status_code=403, detail="admin authorization failed")
        return "bootstrap-admin"

    async def operator_session(
        cookie_value: Annotated[str | None, Cookie(alias=ADMIN_SESSION_COOKIE)] = None,
    ) -> OperatorSession:
        if cookie_value is None:
            raise AdminProblemError(401, "operator_session_required", "operator session is required")
        try:
            session = await runtime.operator_sessions.verify(cookie_value)
        except AuthenticationError:
            try:
                await runtime.store.append_audit_event(
                    actor="anonymous",
                    tenant_id=None,
                    token_id=None,
                    action="session.authenticate",
                    target_type="operator_session",
                    target_id="unresolved",
                    outcome="failed",
                    detail={"reason": "invalid_or_expired"},
                )
            except (OSError, RuntimeError, ValueError):
                pass
            raise AdminProblemError(401, "operator_session_invalid", "operator session is invalid") from None
        return session

    async def operator(
        request: Request,
        session: Annotated[OperatorSession, Depends(operator_session)],
    ) -> OperatorPrincipal:
        request.state.operator_principal = session.principal
        return session.principal

    def set_operator_cookie(response: Response, value: str) -> None:
        response.set_cookie(
            key=ADMIN_SESSION_COOKIE,
            value=value,
            max_age=runtime.operator_sessions.ttl_seconds,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )

    def clear_operator_cookie(response: Response) -> None:
        response.delete_cookie(
            key=ADMIN_SESSION_COOKIE,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )

    async def replace_operator_session(
        cookie_value: str | None,
        *,
        principal_id: UUID | None = None,
    ) -> tuple[OperatorSession, str]:
        issued = await runtime.operator_sessions.replace(
            cookie_value,
            principal_id=principal_id if principal_id is not None else BOOTSTRAP_OPERATOR_PRINCIPAL_ID,
        )
        return issued.session, issued.cookie_value

    def access_envelope(data: Any, params: AdminContextParameters | None = None) -> AdminEnvelope[Any]:
        context = selected_context(params or AdminContextParameters())
        observed_at = datetime.now(UTC)
        return AdminEnvelope(
            meta=AdminMeta(
                generated_at=observed_at,
                context=context,
                sources=[
                    AdminSource(
                        id="postgresql",
                        state=AdminSourceState.AVAILABLE,
                        observed_at=observed_at,
                        age_seconds=0,
                    )
                ],
            ),
            data=data,
        )

    def admin_problem_response(
        status_code: int,
        code: str,
        detail: str,
        *,
        title: str = "Admin request failed",
    ) -> JSONResponse:
        request_id = uuid4()
        return JSONResponse(
            AdminProblem(
                type=f"urn:fs2:admin:problem:{code}",
                title=title,
                status=status_code,
                code=code,
                detail=detail,
                request_id=request_id,
            ).model_dump(mode="json"),
            status_code=status_code,
            media_type="application/problem+json",
            headers={"cache-control": "no-store", "x-request-id": str(request_id)},
        )

    @app.exception_handler(AuthenticationError)
    async def authentication_error(_: Request, __: AuthenticationError) -> JSONResponse:
        return _error(401, "authentication_error", "invalid bearer token")

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # The framework default includes rejected values, which may be prompts or tokens.
        fields = [
            {
                "location": ".".join(str(item) for item in error.get("loc", ())),
                "type": str(error.get("type", "validation_error")),
            }
            for error in exc.errors()[:20]
        ]
        if request.url.path.startswith("/admin/api/v1"):
            return admin_problem_response(
                422,
                "validation_error",
                "request parameters failed bounded validation",
                title="Request validation failed",
            )
        return JSONResponse(
            {"error": {"type": "validation_error", "message": "request validation failed", "fields": fields}},
            status_code=422,
            headers={"cache-control": "no-store"},
        )

    @app.exception_handler(AdminProblemError)
    async def admin_problem(_: Request, exc: AdminProblemError) -> JSONResponse:
        return admin_problem_response(exc.status_code, exc.code, exc.detail)

    @app.exception_handler(PermissionError)
    async def permission_error(request: Request, __: PermissionError) -> JSONResponse:
        if request.url.path.startswith("/admin/api/v1"):
            return admin_problem_response(403, "permission_denied", "operator policy does not permit this request")
        return _error(403, "permission_denied", "request is outside token policy")

    @app.exception_handler(RegistryError)
    async def registry_error(_: Request, __: RegistryError) -> JSONResponse:
        return _error(503, "route_unavailable", "model route is unavailable")

    @app.exception_handler(KeyError)
    async def key_error(_: Request, __: KeyError) -> JSONResponse:
        return _error(404, "not_found", "model or operation was not found")

    @app.exception_handler(RuntimeError)
    async def runtime_error(request: Request, __: RuntimeError) -> JSONResponse:
        if request.url.path.startswith("/admin/api/v1"):
            return admin_problem_response(503, "unavailable", "admin reporting is unavailable")
        return _error(503, "unavailable", "service unavailable")

    @app.exception_handler(ConflictError)
    async def conflict_error(request: Request, exc: ConflictError) -> JSONResponse:
        if request.url.path.startswith("/admin/api/v1"):
            return admin_problem_response(409, exc.code, "the requested state transition conflicts with current state")
        return _error(409, exc.code, str(exc))

    @app.exception_handler(BudgetExceededError)
    async def budget_error(_: Request, exc: BudgetExceededError) -> JSONResponse:
        return _error(402, exc.code, str(exc))

    @app.exception_handler(ConcurrencyExceededError)
    async def concurrency_error(_: Request, exc: ConcurrencyExceededError) -> JSONResponse:
        return _error(429, exc.code, str(exc))

    @app.exception_handler(RateLimitExceededError)
    async def rate_limit_error(_: Request, exc: RateLimitExceededError) -> JSONResponse:
        return _error(429, exc.code, str(exc))

    @app.exception_handler(NotFoundError)
    async def not_found_error(request: Request, exc: NotFoundError) -> JSONResponse:
        if request.url.path.startswith("/admin/api/v1"):
            return admin_problem_response(404, exc.code, "the requested resource was not found")
        return _error(404, exc.code, str(exc))

    @app.get("/livez", include_in_schema=False)
    async def livez() -> Response:
        if runtime.settings.run_workers and not runtime.admission.live():
            return _error(503, "worker_fenced_release_failed", "admission worker requires restart")
        return JSONResponse({"status": "ok"})

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> Response:
        if not await runtime.store.ping():
            return _error(503, "database_unavailable", "database ping failed")
        route_health = (
            runtime.route_revalidator.health()
            if runtime.route_revalidator is not None
            else runtime.registry.validation_health()
        )
        if not route_health["healthy"] or route_health.get("periodic_task_healthy") is False:
            return _error(503, "route_evidence_unavailable", "canonical route evidence is unavailable")
        enabled_models = runtime.registry.list(enabled_only=True)
        routable_models = len(enabled_models)
        local_activation = activation_set(enabled_models)
        activation_required = local_activation.required
        activation_ready = (
            await runtime.store.activation_controller_ready(local_activation.digest) if activation_required else None
        )
        if activation_ready is False:
            return _error(
                503,
                "activation_controller_unavailable",
                "a local route requires an available activation controller",
            )
        worker_health: dict[str, object] | None = None
        if runtime.settings.run_workers:
            worker_health = runtime.admission.health()
            if not worker_health["ready"]:
                return _error(503, "worker_unavailable", "admission worker or janitor is unavailable")
        federation_health = (
            await runtime.admission.runtime.federation_health()
            if routable_models
            else {"ready": True, "routes": 0, "circuits": {}}
        )
        if not federation_health["ready"]:
            return _error(503, "federation_unavailable", "a federated upstream circuit is open")
        dynamic_model_health = (
            runtime.model_deployment_bridge.health() if runtime.model_deployment_bridge is not None else None
        )
        return JSONResponse(
            {
                "status": "ready",
                "models": routable_models,
                "route_evidence": route_health,
                "activation": {"required": activation_required, "ready": activation_ready},
                "admission": worker_health,
                "federation": federation_health,
                **({"dynamic_models": dynamic_model_health} if dynamic_model_health is not None else {}),
            }
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        runtime.metrics.sync_models(runtime.registry.list())
        runtime.metrics.set_terminal_accounting(await runtime.store.terminal_accounting())
        runtime.metrics.set_queue(await runtime.store.queue_counts())
        runtime.metrics.set_queue_age(await runtime.store.oldest_queue_age())
        return Response(runtime.metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/v1/models")
    async def models(identity: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
        identity.require(Scope.CATALOG_READ)
        await runtime.revalidate_routes()
        visible = runtime.registry.allowed_for_principal(identity, surface="openai")
        return {"object": "list", "data": [_model_view(model) for model in visible]}

    async def invoke(
        *,
        request: Request,
        identity: Principal,
        model_id: str,
        protocol: str,
        operation: str,
        body: bytes,
        idempotency_key: str | None,
        wait_seconds: str | None,
    ) -> Response:
        model_id = _validate_model_id(model_id)
        if idempotency_key is None:
            idempotency_key = f"generated-{uuid4()}"
        if not MIN_IDEMPOTENCY_KEY_LENGTH <= len(idempotency_key) <= MAX_IDEMPOTENCY_KEY_LENGTH:
            raise HTTPException(status_code=400, detail="Idempotency-Key length is invalid")
        try:
            wait = runtime.settings.sync_wait_seconds if wait_seconds is None else float(wait_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="x-fs2-wait-seconds must be numeric") from exc
        if not math.isfinite(wait) or wait < 0 or wait > runtime.settings.max_sync_wait_seconds:
            raise HTTPException(status_code=400, detail="x-fs2-wait-seconds is outside the configured bound")
        deadline_header = request.headers.get("x-fs2-deadline-seconds")
        deadline_at = None
        if deadline_header is not None:
            try:
                deadline_seconds = float(deadline_header)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="x-fs2-deadline-seconds must be numeric") from exc
            if not math.isfinite(deadline_seconds) or deadline_seconds <= 0 or deadline_seconds > 86400:
                raise HTTPException(status_code=400, detail="deadline is outside the accepted bound")
            deadline_at = datetime.now(UTC) + timedelta(seconds=deadline_seconds)
        admitted = await runtime.admission.admit(
            identity,
            AdmissionRequest(
                model_id=model_id,
                operation=operation,
                protocol=protocol,
                idempotency_key=idempotency_key,
                request_body=body,
                request_content_type=request.headers.get("content-type", "application/json").split(";", 1)[0],
                traceparent=request.headers.get("traceparent"),
                deadline_at=deadline_at,
            ),
        )
        current = (
            await runtime.admission.wait(admitted.id, tenant_id=identity.tenant_id, seconds=wait) if wait else admitted
        )
        return await _operation_response(runtime, current)

    async def openai_route(
        request: Request,
        identity: Principal,
        protocol: str,
        idempotency_key: str | None,
        wait_seconds: str | None,
    ) -> Response:
        body = await request.body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="request body must be JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
            raise HTTPException(status_code=400, detail="OpenAI-compatible request requires string model")
        if payload.get("stream") is True:
            raise HTTPException(status_code=400, detail="streaming is not enabled in phase 1; use an async operation")
        model_id = _validate_model_id(payload["model"])
        model = runtime.registry.get(model_id)
        resolved_operation = runtime.registry.operation_for_protocol(model, protocol)
        return await invoke(
            request=request,
            identity=identity,
            model_id=model_id,
            protocol=protocol,
            operation=resolved_operation,
            body=body,
            idempotency_key=idempotency_key,
            wait_seconds=wait_seconds,
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        identity: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        wait_seconds: Annotated[str | None, Header(alias="x-fs2-wait-seconds")] = None,
    ) -> Response:
        return await openai_route(request, identity, "openai-chat", idempotency_key, wait_seconds)

    @app.post("/v1/completions")
    async def completions(
        request: Request,
        identity: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        wait_seconds: Annotated[str | None, Header(alias="x-fs2-wait-seconds")] = None,
    ) -> Response:
        return await openai_route(request, identity, "openai-completions", idempotency_key, wait_seconds)

    @app.post("/v1/embeddings")
    async def embeddings(
        request: Request,
        identity: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        wait_seconds: Annotated[str | None, Header(alias="x-fs2-wait-seconds")] = None,
    ) -> Response:
        return await openai_route(request, identity, "openai-embeddings", idempotency_key, wait_seconds)

    @app.post("/v1/images/generations")
    async def images(
        request: Request,
        identity: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        wait_seconds: Annotated[str | None, Header(alias="x-fs2-wait-seconds")] = None,
    ) -> Response:
        return await openai_route(request, identity, "openai-images", idempotency_key, wait_seconds)

    @app.post("/v1/models/{model_id}:invoke")
    async def native_invoke(
        model_id: str,
        payload: NativeInvocation,
        request: Request,
        identity: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        wait_seconds: Annotated[str | None, Header(alias="x-fs2-wait-seconds")] = None,
    ) -> Response:
        return await invoke(
            request=request,
            identity=identity,
            model_id=model_id,
            protocol="native",
            operation=payload.operation,
            body=json.dumps(payload.payload, separators=(",", ":")).encode(),
            idempotency_key=idempotency_key,
            wait_seconds=wait_seconds,
        )

    @app.get("/v1/operations/{operation_id}")
    async def operation_status(operation_id: UUID, identity: Annotated[Principal, Depends(principal)]) -> Response:
        operation = await runtime.store.get_operation(operation_id, tenant_id=identity.tenant_id)
        require_operation_access(identity, operation)
        return JSONResponse(operation.model_dump(mode="json"), headers=_operation_headers(operation))

    @app.get("/v1/operations/{operation_id}/result")
    async def operation_result(operation_id: UUID, identity: Annotated[Principal, Depends(principal)]) -> Response:
        operation = await runtime.store.get_operation(operation_id, tenant_id=identity.tenant_id)
        require_operation_access(identity, operation)
        result = await runtime.store.get_operation_result(operation_id, tenant_id=identity.tenant_id)
        return JSONResponse(result.result, headers=_operation_headers(result.operation))

    @app.post("/v1/operations/{operation_id}:cancel")
    async def operation_cancel(operation_id: UUID, identity: Annotated[Principal, Depends(principal)]) -> OperationView:
        operation = await runtime.store.get_operation(operation_id, tenant_id=identity.tenant_id)
        require_operation_access(identity, operation)
        return await runtime.store.cancel_operation(
            operation_id,
            tenant_id=identity.tenant_id,
            actor=identity.principal_id,
        )

    @app.post("/v1/operations/{operation_id}:acknowledge")
    async def operation_acknowledge(
        operation_id: UUID, identity: Annotated[Principal, Depends(principal)]
    ) -> OperationView:
        operation = await runtime.store.get_operation(operation_id, tenant_id=identity.tenant_id)
        require_operation_access(identity, operation)
        if not operation.status.terminal:
            raise ConflictError("operation is not terminal")
        await runtime.store.purge_operation_payload(operation_id, tenant_id=identity.tenant_id)
        return await runtime.store.get_operation(operation_id, tenant_id=identity.tenant_id)

    def selected_context(params: AdminContextParameters) -> AdminContext:
        return admin_read.resolve_context(
            project=params.project,
            cluster=params.cluster,
            region=params.region,
            from_at=params.from_at,
            to_at=params.to_at,
            timezone=params.timezone,
        )

    admin_problem_responses: dict[int | str, dict[str, Any]] = {
        status_code: {
            "model": AdminProblem,
            "description": "Bounded admin API problem",
            "headers": {
                "x-request-id": {
                    "description": "Server-generated correlation identifier",
                    "schema": {"type": "string", "format": "uuid"},
                }
            },
            "content": {
                "application/problem+json": {
                    "schema": {"$ref": "#/components/schemas/AdminProblem"},
                }
            },
        }
        for status_code in (400, 401, 403, 404, 409, 422, 429, 503)
    }

    @app.post(
        "/admin/api/v1/session",
        response_model=AdminEnvelope[OperatorSession],
        responses=admin_problem_responses,
    )
    async def create_operator_session(
        response: Response,
        _: Annotated[str, Depends(admin)],
        payload: OperatorSessionHandoff | None = None,
        cookie_value: Annotated[str | None, Cookie(alias=ADMIN_SESSION_COOKIE)] = None,
    ) -> AdminEnvelope[OperatorSession]:
        session, secret = await replace_operator_session(
            cookie_value,
            principal_id=payload.principal_id if payload is not None else None,
        )
        set_operator_cookie(response, secret)
        return access_envelope(session)

    @app.get(
        "/admin/api/v1/session",
        response_model=AdminEnvelope[OperatorSession],
        responses=admin_problem_responses,
    )
    async def current_operator_session(
        session: Annotated[OperatorSession, Depends(operator_session)],
    ) -> AdminEnvelope[OperatorSession]:
        return access_envelope(session)

    @app.delete(
        "/admin/api/v1/session",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=admin_problem_responses,
    )
    async def delete_operator_session(
        response: Response,
        cookie_value: Annotated[str | None, Cookie(alias=ADMIN_SESSION_COOKIE)] = None,
    ) -> None:
        await runtime.operator_sessions.revoke_if_valid(cookie_value, actor="operator-session-logout")
        clear_operator_cookie(response)

    @app.post("/admin/v1/tokens", response_model=TokenIssued)
    async def issue_token(payload: TokenCreateRequest, actor: Annotated[str, Depends(admin)]) -> TokenIssued:
        canonical_models = {
            model_id if model_id == "*" else runtime.registry.get(model_id, require_enabled=False).id
            for model_id in payload.models
        }
        canonical = payload.model_copy(update={"models": canonical_models})
        return await runtime.tokens.issue(TokenCreate.model_validate(canonical.model_dump()), created_by=actor)

    @app.get("/admin/v1/tokens", response_model=list[TokenView])
    async def list_tokens(
        _: Annotated[str, Depends(admin)],
        tenant_id: str | None = Query(default=None, max_length=120),
    ) -> list[TokenView]:
        return await runtime.tokens.list(tenant_id=tenant_id)

    @app.delete("/admin/v1/tokens/{token_id}", response_model=TokenView)
    async def revoke_token(token_id: UUID, actor: Annotated[str, Depends(admin)]) -> TokenView:
        return await runtime.tokens.revoke(token_id, actor=actor)

    @app.get("/admin/v1/audit")
    async def audit(
        _: Annotated[str, Depends(admin)],
        tenant_id: str | None = Query(default=None, max_length=120),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        rows = await runtime.store.list_audit(tenant_id=tenant_id, limit=limit)
        return [row.model_dump(mode="json") for row in rows]

    @app.get(
        "/admin/api/v1/principals",
        response_model=AdminEnvelope[AdminPrincipalList],
        responses=admin_problem_responses,
    )
    async def admin_principals(
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        tenant_id: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> AdminEnvelope[AdminPrincipalList]:
        return access_envelope(await admin_access.list_principals(identity, tenant_id=tenant_id, limit=limit))

    @app.post(
        "/admin/api/v1/principals",
        response_model=AdminEnvelope[OperatorPrincipal],
        status_code=status.HTTP_201_CREATED,
        responses=admin_problem_responses,
    )
    async def admin_create_principal(
        payload: OperatorPrincipalCreate,
        identity: Annotated[OperatorPrincipal, Depends(operator)],
    ) -> AdminEnvelope[OperatorPrincipal]:
        return access_envelope(await admin_access.create_principal(identity, payload))

    @app.patch(
        "/admin/api/v1/principals/{principal_id}",
        response_model=AdminEnvelope[OperatorPrincipal],
        responses=admin_problem_responses,
    )
    async def admin_update_principal(
        principal_id: UUID,
        payload: OperatorPrincipalPatch,
        identity: Annotated[OperatorPrincipal, Depends(operator)],
    ) -> AdminEnvelope[OperatorPrincipal]:
        return access_envelope(await admin_access.update_principal(identity, principal_id, payload))

    @app.get(
        "/admin/api/v1/keys",
        response_model=AdminEnvelope[AdminApiKeyList],
        responses=admin_problem_responses,
    )
    async def admin_keys(
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        tenant_id: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> AdminEnvelope[AdminApiKeyList]:
        return access_envelope(await admin_access.list_keys(identity, tenant_id=tenant_id, limit=limit))

    @app.post(
        "/admin/api/v1/keys",
        response_model=AdminEnvelope[AdminApiKeyDisclosure],
        status_code=status.HTTP_201_CREATED,
        responses=admin_problem_responses,
    )
    async def admin_issue_key(
        payload: AdminApiKeyCreate,
        identity: Annotated[OperatorPrincipal, Depends(operator)],
    ) -> AdminEnvelope[AdminApiKeyDisclosure]:
        canonical_models = {
            model_id if model_id == "*" else runtime.registry.get(model_id, require_enabled=False).id
            for model_id in payload.models
        }
        return access_envelope(
            await admin_access.issue_key(identity, payload.model_copy(update={"models": canonical_models}))
        )

    @app.patch(
        "/admin/api/v1/keys/{token_id}",
        response_model=AdminEnvelope[AdminApiKey],
        responses=admin_problem_responses,
    )
    async def admin_update_key_policy(
        token_id: UUID,
        payload: AdminApiKeyPolicyPatch,
        identity: Annotated[OperatorPrincipal, Depends(operator)],
    ) -> AdminEnvelope[AdminApiKey]:
        if payload.models is not None:
            canonical_models = {
                model_id if model_id == "*" else runtime.registry.get(model_id, require_enabled=False).id
                for model_id in payload.models
            }
            payload = payload.model_copy(update={"models": canonical_models})
        return access_envelope(await admin_access.update_key_policy(identity, token_id, payload))

    @app.post(
        "/admin/api/v1/keys/{token_id}:rotate",
        response_model=AdminEnvelope[AdminApiKeyDisclosure],
        status_code=status.HTTP_201_CREATED,
        responses=admin_problem_responses,
    )
    async def admin_rotate_key(
        token_id: UUID,
        payload: AdminApiKeyRotate,
        identity: Annotated[OperatorPrincipal, Depends(operator)],
    ) -> AdminEnvelope[AdminApiKeyDisclosure]:
        return access_envelope(await admin_access.rotate_key(identity, token_id, payload))

    @app.delete(
        "/admin/api/v1/keys/{token_id}",
        response_model=AdminEnvelope[AdminApiKey],
        responses=admin_problem_responses,
    )
    async def admin_revoke_key(
        token_id: UUID,
        identity: Annotated[OperatorPrincipal, Depends(operator)],
    ) -> AdminEnvelope[AdminApiKey]:
        return access_envelope(await admin_access.revoke_key(identity, token_id))

    @app.get(
        "/admin/api/v1/audit",
        response_model=AdminEnvelope[AdminAuditList],
        responses=admin_problem_responses,
    )
    async def admin_access_audit(
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        tenant_id: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> AdminEnvelope[AdminAuditList]:
        return access_envelope(await admin_access.list_audit(identity, tenant_id=tenant_id, limit=limit))

    @app.get(
        "/admin/api/v1/context",
        response_model=AdminEnvelope[AdminContextData],
        responses=admin_problem_responses,
    )
    async def admin_context(
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        params: Annotated[AdminContextParameters, Depends(_admin_context_parameters)],
    ) -> AdminEnvelope[AdminContextData]:
        await admin_access.authorize(identity, OperatorRole.VIEWER, action="context.read")
        context = selected_context(params)
        return admin_read.context(context)

    @app.get(
        "/admin/api/v1/overview",
        response_model=AdminEnvelope[AdminOverview],
        responses=admin_problem_responses,
    )
    async def admin_overview(
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        params: Annotated[AdminContextParameters, Depends(_admin_context_parameters)],
    ) -> AdminEnvelope[AdminOverview]:
        await admin_access.authorize_global(identity, OperatorRole.VIEWER, action="overview.read")
        return await admin_read.overview(selected_context(params))

    @app.get(
        "/admin/api/v1/models",
        response_model=AdminEnvelope[AdminModelList],
        responses=admin_problem_responses,
    )
    async def admin_models(
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        params: Annotated[AdminContextParameters, Depends(_admin_context_parameters)],
        search: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        model_state: Annotated[AdminModelState | None, Query(alias="state")] = None,
        limit: Annotated[int, Query(ge=1, le=256)] = 200,
    ) -> AdminEnvelope[AdminModelList]:
        await admin_access.authorize_global(identity, OperatorRole.VIEWER, action="model.list")
        return await admin_read.model_list(
            selected_context(params),
            search=search,
            state=model_state,
            limit=limit,
        )

    @app.get(
        "/admin/api/v1/models/{model_id}",
        response_model=AdminEnvelope[AdminModelDetail],
        responses=admin_problem_responses,
    )
    async def admin_model_detail(
        model_id: str,
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        params: Annotated[AdminContextParameters, Depends(_admin_context_parameters)],
    ) -> AdminEnvelope[AdminModelDetail]:
        await admin_access.authorize_global(identity, OperatorRole.VIEWER, action="model.read")
        if not 1 <= len(model_id) <= MAX_MODEL_ID_LENGTH:
            raise AdminProblemError(400, "invalid_model_id", "model identifier length is invalid")
        return await admin_read.model_detail(selected_context(params), model_id)

    @app.get(
        "/admin/api/v1/operations",
        response_model=AdminEnvelope[AdminOperationList],
        responses=admin_problem_responses,
    )
    async def admin_operations(
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        params: Annotated[AdminContextParameters, Depends(_admin_context_parameters)],
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
        tenant_id: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
        model_id: Annotated[str | None, Query(min_length=1, max_length=MAX_MODEL_ID_LENGTH)] = None,
        principal_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
        api_key_prefix: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        operation_status: Annotated[OperationStatus | None, Query(alias="status")] = None,
        error_code: Annotated[
            str | None,
            Query(max_length=64, pattern=r"^[a-z][a-z0-9_]*$"),
        ] = None,
    ) -> AdminEnvelope[AdminOperationList]:
        authorized_tenant = await admin_access.authorize(
            identity,
            OperatorRole.VIEWER,
            action="operation.list",
            tenant_id=tenant_id,
        )
        return await admin_read.operation_list(
            selected_context(params),
            limit=limit,
            cursor=cursor,
            tenant_id=authorized_tenant,
            model_id=model_id,
            principal_id=principal_id,
            api_key_prefix=api_key_prefix,
            status=operation_status,
            error_code=error_code,
        )

    @app.get(
        "/admin/api/v1/operations/{operation_id}",
        response_model=AdminEnvelope[AdminOperationDetail],
        responses=admin_problem_responses,
    )
    async def admin_operation_detail(
        operation_id: UUID,
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        params: Annotated[AdminContextParameters, Depends(_admin_context_parameters)],
    ) -> AdminEnvelope[AdminOperationDetail]:
        authorized_tenant = await admin_access.authorize(
            identity,
            OperatorRole.VIEWER,
            action="operation.read",
            tenant_id=identity.tenant_id,
        )
        return await admin_read.operation_detail(
            selected_context(params),
            operation_id,
            tenant_id=authorized_tenant,
        )

    @app.get(
        "/admin/api/v1/capacity",
        response_model=AdminEnvelope[AdminCapacity],
        responses=admin_problem_responses,
    )
    async def admin_capacity(
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        params: Annotated[AdminContextParameters, Depends(_admin_context_parameters)],
    ) -> AdminEnvelope[AdminCapacity]:
        await admin_access.authorize_global(identity, OperatorRole.VIEWER, action="capacity.read")
        return await admin_read.capacity(selected_context(params))

    @app.get(
        "/admin/api/v1/observability",
        response_model=AdminEnvelope[AdminObservability],
        responses=admin_problem_responses,
    )
    async def admin_observability(
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        params: Annotated[AdminContextParameters, Depends(_admin_context_parameters)],
        model_id: Annotated[str | None, Query(min_length=1, max_length=MAX_MODEL_ID_LENGTH)] = None,
        operation_id: UUID | None = None,
    ) -> AdminEnvelope[AdminObservability]:
        await admin_access.authorize_global(identity, OperatorRole.VIEWER, action="observability.read")
        return await admin_read.observability(
            selected_context(params),
            model_id=model_id,
            operation_id=operation_id,
        )

    @app.get(
        "/admin/api/v1/academic-assets",
        response_model=AdminEnvelope[AcademicAssetReadinessList],
        responses=admin_problem_responses,
    )
    async def admin_academic_assets(
        identity: Annotated[OperatorPrincipal, Depends(operator)],
        params: Annotated[AdminContextParameters, Depends(_admin_context_parameters)],
    ) -> AdminEnvelope[AcademicAssetReadinessList]:
        """Licensed academic asset readiness, on two independent axes.

        Operational readiness never implies that formal institutional licence
        acceptance has happened, and no licensed bytes, credentials or
        acceptance receipt bodies are exposed here.
        """

        await admin_access.authorize_global(identity, OperatorRole.VIEWER, action="academic-assets.read")
        return await admin_read.academic_assets(selected_context(params))

    @app.get("/internal/ext-authz")
    async def ext_authz(identity: Annotated[Principal, Depends(principal)]) -> Response:
        return Response(
            status_code=200,
            headers={
                "x-fs2-tenant": identity.tenant_id,
                "x-fs2-principal": identity.principal_id,
                "x-fs2-token-id": str(identity.token_id),
            },
        )

    if runtime.configuration is not None:
        app.include_router(
            configuration_router(
                service=runtime.configuration,
                access=admin_access,
                operator_dependency=operator,
                envelope=access_envelope,
                problem_responses=admin_problem_responses,
            )
        )

    if runtime.model_deployment_preview is not None:
        app.include_router(
            model_deployment_preview_router(
                service=runtime.model_deployment_preview,
                access=admin_access,
                operator_dependency=operator,
                envelope=access_envelope,
                problem_responses=admin_problem_responses,
                writer_enabled=runtime.model_deployment_mutation is not None,
            )
        )

    if runtime.model_deployment_mutation is not None:
        app.include_router(
            model_deployment_mutation_router(
                service=runtime.model_deployment_mutation,
                access=admin_access,
                operator_dependency=operator,
                envelope=access_envelope,
                problem_responses=admin_problem_responses,
            )
        )

    if runtime.model_deployment_read is not None:
        app.include_router(
            model_deployment_read_router(
                service=runtime.model_deployment_read,
                access=admin_access,
                operator_dependency=operator,
                envelope=access_envelope,
                problem_responses=admin_problem_responses,
            )
        )

    FastAPIInstrumentor.instrument_app(app, excluded_urls="livez,readyz,metrics")
    return app
