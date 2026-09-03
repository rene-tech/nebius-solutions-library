"""Authorized Streamable-HTTP MCP facade over the durable gateway path."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.caching import CacheHint
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS, ListToolsResult
from pydantic import AnyHttpUrl, ValidationError
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Send
from starlette.types import Scope as ASGIScope

from .api import AppRuntime, _model_view
from .auth import AuthenticationError, require_operation_access
from .models import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MIN_IDEMPOTENCY_KEY_LENGTH,
    AdmissionRequest,
    OperationView,
    Principal,
    Scope,
)
from .registry import OperationalModel
from .scientific_input_uploads import ScientificInputUploadRequest
from .store import NotFoundError

CORE_TOOLS = {
    "list_models",
    "list_scientific_models",
    "invoke_model",
    "get_operation",
    "get_operation_result",
    "cancel_operation",
    "acknowledge_operation",
    "submit_scientific_run",
    "get_scientific_status",
    "cancel_scientific_run",
    "list_scientific_events",
    "get_scientific_artifact",
    "get_scientific_result",
    "begin_scientific_artifact_upload",
    "finalize_scientific_artifact_upload",
    "download_scientific_artifact",
}
MCP_HTTP_PATH = "/mcp"
MCP_CHILD_MOUNT_PATH = "/"
MCP_STREAMABLE_HTTP_PATH = MCP_HTTP_PATH


class MCPPublicGatewayBoundary:
    """Reject untrusted public authority headers before MCP allocates a session."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: tuple[str, ...],
        allowed_origins: tuple[str, ...],
        max_body_bytes: int,
    ) -> None:
        self.app = app
        self.allowed_hosts = frozenset(allowed_hosts)
        self.allowed_origins = frozenset(allowed_origins)
        self.max_body_bytes = max_body_bytes

    @staticmethod
    def _header_values(scope: ASGIScope, name: bytes) -> list[str] | None:
        try:
            return [value.decode("ascii") for key, value in scope.get("headers", []) if key.lower() == name]
        except UnicodeDecodeError:
            return None

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        hosts = self._header_values(scope, b"host")
        if hosts is None or len(hosts) != 1 or hosts[0] not in self.allowed_hosts:
            await Response("Invalid Host header", status_code=421)(scope, receive, send)
            return
        origins = self._header_values(scope, b"origin")
        if origins is None or len(origins) > 1 or (origins and origins[0] not in self.allowed_origins):
            await Response("Invalid Origin header", status_code=403)(scope, receive, send)
            return
        # A request that declares any modern routing header must carry the
        # complete routing prefix.  Otherwise the SDK's era router could
        # interpret a malformed modern request as legacy and silently bypass
        # the 2026 envelope/header equality ladder.  A request with none of
        # these headers remains available only to the explicit legacy client.
        methods = self._header_values(scope, b"mcp-method")
        names = self._header_values(scope, b"mcp-name")
        versions = self._header_values(scope, b"mcp-protocol-version")
        if methods is None or names is None or versions is None:
            await Response("Invalid MCP routing headers", status_code=400)(scope, receive, send)
            return
        if len(methods) > 1 or len(names) > 1 or len(versions) > 1:
            await Response("Incomplete MCP routing headers", status_code=400)(scope, receive, send)
            return
        if (methods and len(versions) != 1) or (names and len(methods) != 1):
            await Response("Incomplete MCP routing headers", status_code=400)(scope, receive, send)
            return
        if scope.get("method") == "POST" and not methods and not versions:
            upstream_receive = receive
            events: list[Message] = []
            body = bytearray()
            while True:
                event = await upstream_receive()
                events.append(event)
                if event.get("type") != "http.request":
                    break
                body.extend(event.get("body", b""))
                if len(body) > self.max_body_bytes:
                    await Response("MCP request is too large", status_code=413)(scope, receive, send)
                    return
                if not event.get("more_body", False):
                    break

            async def replay() -> Message:
                if events:
                    return events.pop(0)
                return await upstream_receive()

            try:
                decoded = json.loads(body)
                params = decoded.get("params", {}) if isinstance(decoded, dict) else {}
                meta = params.get("_meta", {}) if isinstance(params, dict) else {}
                modern_envelope = (
                    isinstance(meta, dict) and meta.get("io.modelcontextprotocol/protocolVersion") == "2026-07-28"
                )
                discover = isinstance(decoded, dict) and decoded.get("method") == "server/discover"
            except (UnicodeDecodeError, ValueError, RecursionError):
                modern_envelope = discover = False
            if modern_envelope or discover:
                await Response("Incomplete MCP routing headers", status_code=400)(scope, replay, send)
                return
            receive = replay
        await self.app(scope, receive, send)


class PATTokenVerifier(TokenVerifier):
    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            principal = await self.runtime.tokens.verify(token)
        except AuthenticationError:
            return None
        return AccessToken(
            token="verified",  # noqa: S106 - deliberately non-secret SDK sentinel
            client_id=str(principal.token_id),
            scopes=sorted(principal.scopes),
            expires_at=int(principal.expires_at.timestamp()) if principal.expires_at else None,
            resource=f"{self.runtime.settings.public_origin()}{MCP_HTTP_PATH}",
            subject=principal.principal_id,
            claims={
                # Bind the authenticated MCP principal to the same exact
                # authorization-server issuer advertised by RFC 9728.  A
                # symbolic/local issuer would let credential or response
                # caches accidentally cross authorization-server boundaries.
                "iss": self.runtime.settings.authorization_server_url,
                "tenant_id": principal.tenant_id,
                "token_id": str(principal.token_id),
                "token_prefix": principal.token_prefix,
                "models": sorted(principal.models),
                "request_budget": principal.request_budget,
                "gpu_seconds_budget": principal.gpu_seconds_budget,
                "max_concurrency": principal.max_concurrency,
            },
        )


def _principal() -> Principal:
    token = get_access_token()
    if token is None or token.claims is None:
        raise MCPError(code=INTERNAL_ERROR, message="authenticated principal context is unavailable")
    claims = token.claims
    try:
        return Principal(
            token_id=UUID(str(claims["token_id"])),
            token_prefix=str(claims["token_prefix"]),
            principal_id=str(token.subject),
            tenant_id=str(claims["tenant_id"]),
            scopes=frozenset(token.scopes),
            models=frozenset(str(item) for item in claims["models"]),
            expires_at=None,
            request_budget=claims.get("request_budget"),
            gpu_seconds_budget=claims.get("gpu_seconds_budget"),
            max_concurrency=int(claims["max_concurrency"]),
        )
    except (KeyError, TypeError, ValueError):
        raise MCPError(code=INTERNAL_ERROR, message="authenticated principal context is invalid") from None


def _protocol_tool_names(model: OperationalModel) -> set[str]:
    if not model.enabled or not model.gateway.mcp_invocable or not model.binding.mcp_enabled:
        return set()
    return {f"{model.binding.mcp_tool_name}_{protocol.replace('-', '_')}" for protocol in model.gateway.protocols}


def _model_tool_names(runtime: AppRuntime, principal: Principal) -> set[str]:
    names: set[str] = set()
    for model in runtime.registry.allowed_for_principal(principal, surface="mcp"):
        names.update(_protocol_tool_names(model))
    return names


class MCPAuthorizationMiddleware:
    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime
        self._sync_tools: Callable[[], None] | None = None

    def set_tool_sync(self, callback: Callable[[], None]) -> None:
        self._sync_tools = callback

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        try:
            principal = _principal() if ctx.method not in {"initialize", "notifications/initialized"} else None
            if ctx.method in {"tools/list", "tools/call"}:
                await self.runtime.revalidate_routes()
                if self._sync_tools is not None:
                    self._sync_tools()
            if ctx.method == "tools/call" and principal is not None:
                name = str((ctx.params or {}).get("name", ""))
                if name not in CORE_TOOLS and name not in _model_tool_names(self.runtime, principal):
                    raise MCPError(code=INVALID_PARAMS, message="tool is outside token policy")
            result = await call_next(ctx)
            if ctx.method == "tools/list" and principal is not None:
                try:
                    listing = result if isinstance(result, ListToolsResult) else ListToolsResult.model_validate(result)
                except ValidationError:
                    raise MCPError(code=INTERNAL_ERROR, message="tool catalog is unavailable") from None
                allowed = CORE_TOOLS | _model_tool_names(self.runtime, principal)
                return listing.model_copy(update={"tools": [tool for tool in listing.tools if tool.name in allowed]})
            return result
        except MCPError:
            raise
        except Exception:
            # SDK validation/dispatch exceptions may embed the rejected payload.
            # Collapse every non-protocol failure before it can reach logs/traces.
            raise MCPError(code=INVALID_PARAMS, message="request validation failed") from None


async def _metadata(runtime: AppRuntime, principal: Principal, operation_id: UUID) -> OperationView:
    operation = await runtime.store.get_operation(operation_id, tenant_id=principal.tenant_id)
    try:
        require_operation_access(principal, operation)
    except NotFoundError:
        raise MCPError(code=INVALID_PARAMS, message="operation not found") from None
    return operation


async def _admit(
    runtime: AppRuntime,
    *,
    model_id: str,
    protocol: str,
    operation: str,
    payload: dict[str, Any],
    idempotency_key: str | None,
    wait_seconds: float,
    traceparent: str | None,
) -> dict[str, Any]:
    principal = _principal()
    if not math.isfinite(wait_seconds) or wait_seconds < 0 or wait_seconds > runtime.settings.max_sync_wait_seconds:
        raise MCPError(code=INVALID_PARAMS, message="wait_seconds is outside the configured bound")
    if idempotency_key is not None and not (
        MIN_IDEMPOTENCY_KEY_LENGTH <= len(idempotency_key) <= MAX_IDEMPOTENCY_KEY_LENGTH
    ):
        raise MCPError(code=INVALID_PARAMS, message="idempotency_key length is invalid")
    body = json.dumps(payload, separators=(",", ":")).encode()
    if len(body) > runtime.settings.max_request_bytes:
        raise MCPError(code=INVALID_PARAMS, message="tool payload exceeds the configured bound")
    admitted = await runtime.admission.admit(
        principal,
        AdmissionRequest(
            model_id=model_id,
            operation=operation,
            protocol=protocol,
            idempotency_key=idempotency_key or f"mcp-{uuid4()}",
            request_body=body,
            traceparent=traceparent,
        ),
        required_scope="mcp.invoke",
    )
    current = (
        await runtime.admission.wait(admitted.id, tenant_id=principal.tenant_id, seconds=wait_seconds)
        if wait_seconds
        else admitted
    )
    return current.model_dump(mode="json")


def build_mcp_server(runtime: AppRuntime) -> MCPServer:
    authorization = MCPAuthorizationMiddleware(runtime)
    server = MCPServer(
        "fs2-serve",
        title="fs2-serve model gateway",
        description="Authorized model and operation tools backed by durable fs2-serve admission.",
        instructions="Results remain encrypted until TTL or explicit acknowledgement.",
        version="0.1.0",
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(runtime.settings.authorization_server_url),
            # The exact Streamable-HTTP resource is /mcp, so RFC 9728 metadata
            # is published at /.well-known/oauth-protected-resource/mcp.
            resource_server_url=AnyHttpUrl(f"{runtime.settings.public_origin()}{MCP_HTTP_PATH}"),
            required_scopes=["mcp.invoke"],
        ),
        token_verifier=PATTokenVerifier(runtime),
        # Model/tool discovery is authorization- and live-route-dependent.
        # It must never be shared across PATs or kept after a route receipt
        # expires, even though the 2026 protocol permits response caching.
        cache_hints={"tools/list": CacheHint(ttl_ms=0, scope="private")},
        middleware=[authorization],
    )

    async def list_models() -> dict[str, Any]:
        principal = _principal()
        principal.require(Scope.CATALOG_READ)
        return {
            "object": "list",
            "data": [_model_view(model) for model in runtime.registry.allowed_for_principal(principal, surface="mcp")],
        }

    async def list_scientific_models() -> dict[str, Any]:
        """List only scientific profiles this exact caller can submit."""

        principal = _principal()
        if runtime.scientific_batches is None:
            principal.require(Scope.CATALOG_READ)
            return {"object": "list", "data": []}
        return runtime.scientific_batches.discover(principal, surface="mcp")

    async def invoke_model(
        model_id: str,
        protocol: str,
        payload: dict[str, Any],
        ctx: Context,
        idempotency_key: str | None = None,
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        """Invoke any currently authorized model discovered by ``list_models``.

        Model-specific convenience tools are fixed when the MCP process starts.
        This generic tool resolves the atomic live registry at call time, so a
        newly added ModelDeployment is immediately usable without restarting
        every MCP replica.
        """

        principal = _principal()
        await runtime.revalidate_routes()
        try:
            model = runtime.registry.get(model_id)
            runtime.registry.authorize_principal(
                model,
                principal,
                requested_model_id=model_id,
                surface="mcp",
            )
            operation = runtime.registry.operation_for_protocol(model, protocol)
        except (KeyError, PermissionError, ValueError):
            raise MCPError(code=INVALID_PARAMS, message="model or protocol is outside token policy") from None
        try:
            traceparent = (ctx.headers or {}).get("traceparent")
        except ValueError:
            traceparent = None
        return await _admit(
            runtime,
            model_id=model_id,
            protocol=protocol,
            operation=operation,
            payload=payload,
            idempotency_key=idempotency_key,
            wait_seconds=wait_seconds,
            traceparent=traceparent,
        )

    async def get_operation(operation_id: UUID) -> dict[str, Any]:
        return (await _metadata(runtime, _principal(), operation_id)).model_dump(mode="json")

    async def get_operation_result(operation_id: UUID) -> dict[str, Any]:
        principal = _principal()
        operation = await _metadata(runtime, principal, operation_id)
        if operation.protocol == "scientific-batch-v1" and runtime.scientific_batches is not None:
            return dict(await runtime.scientific_batches.result(operation.id, principal=principal))
        result = await runtime.store.get_operation_result(operation.id, tenant_id=principal.tenant_id)
        return result.model_dump(mode="json")

    async def cancel_operation(operation_id: UUID) -> dict[str, Any]:
        principal = _principal()
        operation = await _metadata(runtime, principal, operation_id)
        if operation.protocol == "scientific-batch-v1" and runtime.scientific_batches is not None:
            return await runtime.scientific_batches.cancel(operation.id, principal=principal)
        cancelled = await runtime.store.cancel_operation(
            operation.id, tenant_id=principal.tenant_id, actor=principal.principal_id
        )
        return cancelled.model_dump(mode="json")

    async def acknowledge_operation(operation_id: UUID) -> dict[str, Any]:
        principal = _principal()
        operation = await _metadata(runtime, principal, operation_id)
        if not operation.status.terminal:
            raise MCPError(code=INVALID_PARAMS, message="operation is not terminal")
        await runtime.store.purge_operation_payload(operation.id, tenant_id=principal.tenant_id)
        return (await runtime.store.get_operation(operation.id, tenant_id=principal.tenant_id)).model_dump(mode="json")

    async def submit_scientific_run(
        model_id: str,
        request: dict[str, Any],
        ctx: Context,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Submit a canonical scientific-run-request to a qualified profile."""

        if runtime.scientific_batches is None:
            raise MCPError(code=INVALID_PARAMS, message="scientific batch submission is unavailable")
        principal = _principal()
        if idempotency_key is not None and not (
            MIN_IDEMPOTENCY_KEY_LENGTH <= len(idempotency_key) <= MAX_IDEMPOTENCY_KEY_LENGTH
        ):
            raise MCPError(code=INVALID_PARAMS, message="idempotency_key length is invalid")
        try:
            traceparent = (ctx.headers or {}).get("traceparent")
        except ValueError:
            traceparent = None
        return await runtime.scientific_batches.submit(
            principal=principal,
            model_id=model_id,
            request=request,
            idempotency_key=idempotency_key or f"mcp-scientific-{uuid4()}",
            traceparent=traceparent,
            require_mcp_invocable=True,
        )

    async def get_scientific_status(operation_id: UUID) -> dict[str, Any]:
        if runtime.scientific_batches is None:
            raise MCPError(code=INVALID_PARAMS, message="scientific batch service is unavailable")
        return await runtime.scientific_batches.status(operation_id, principal=_principal())

    async def cancel_scientific_run(operation_id: UUID) -> dict[str, Any]:
        if runtime.scientific_batches is None:
            raise MCPError(code=INVALID_PARAMS, message="scientific batch service is unavailable")
        return await runtime.scientific_batches.cancel(operation_id, principal=_principal())

    async def list_scientific_events(operation_id: UUID, after_sequence: int = 0, limit: int = 1000) -> dict[str, Any]:
        if runtime.scientific_batches is None:
            raise MCPError(code=INVALID_PARAMS, message="scientific batch service is unavailable")
        return await runtime.scientific_batches.events(
            operation_id,
            principal=_principal(),
            after_sequence=after_sequence,
            limit=limit,
        )

    async def get_scientific_artifact(artifact_id: UUID) -> dict[str, Any]:
        if runtime.scientific_batches is None:
            raise MCPError(code=INVALID_PARAMS, message="scientific artifact service is unavailable")
        return dict(await runtime.scientific_batches.artifact(artifact_id, principal=_principal()))

    async def get_scientific_result(operation_id: UUID) -> dict[str, Any]:
        if runtime.scientific_batches is None:
            raise MCPError(code=INVALID_PARAMS, message="scientific result service is unavailable")
        return dict(await runtime.scientific_batches.result(operation_id, principal=_principal()))

    async def begin_scientific_artifact_upload(
        model_id: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
        compression: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a write-once customer upload handle for a scientific input."""

        if runtime.scientific_input_uploads is None or runtime.scientific_batches is None:
            raise MCPError(code=INVALID_PARAMS, message="scientific input upload is unavailable")
        if idempotency_key is not None and not (
            MIN_IDEMPOTENCY_KEY_LENGTH <= len(idempotency_key) <= MAX_IDEMPOTENCY_KEY_LENGTH
        ):
            raise MCPError(code=INVALID_PARAMS, message="idempotency_key length is invalid")
        runtime.scientific_batches.profiles.get(model_id)
        request = ScientificInputUploadRequest.model_validate(
            {
                "model_id": model_id,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "media_type": media_type,
                "compression": compression,
            }
        )
        result = await runtime.scientific_input_uploads.begin(
            principal=_principal(),
            request=request,
            idempotency_key=idempotency_key or f"mcp-scientific-upload-{uuid4()}",
        )
        return result.model_dump(mode="json")

    async def finalize_scientific_artifact_upload(operation_id: UUID, upload_id: UUID) -> dict[str, Any]:
        """Verify uploaded bytes and publish their immutable artifact pointer."""

        if runtime.scientific_input_uploads is None:
            raise MCPError(code=INVALID_PARAMS, message="scientific input upload is unavailable")
        result = await runtime.scientific_input_uploads.finalize(
            principal=_principal(),
            operation_id=operation_id,
            upload_id=upload_id,
        )
        return result.model_dump(mode="json", exclude_none=True)

    async def download_scientific_artifact(artifact_id: UUID) -> dict[str, Any]:
        """Issue a short-lived tenant-authorized result or input download handle."""

        principal = _principal()
        principal.require(Scope.OPERATIONS_RESULT)
        if runtime.artifact_service is None:
            raise MCPError(code=INVALID_PARAMS, message="scientific artifact service is unavailable")
        result = await runtime.artifact_service.download(artifact_id, tenant_id=principal.tenant_id)
        return {
            "artifact": result.artifact.to_public_ref().model_dump(mode="json", exclude_none=True),
            "handle": {
                "method": result.handle.method,
                "url": result.handle.url,
                "expires_at": result.handle.expires_at.isoformat(),
                "write_once": result.handle.write_once,
                "headers": dict(result.handle.headers),
            },
        }

    for function in (
        list_models,
        list_scientific_models,
        invoke_model,
        get_operation,
        get_operation_result,
        cancel_operation,
        acknowledge_operation,
        submit_scientific_run,
        get_scientific_status,
        cancel_scientific_run,
        list_scientific_events,
        get_scientific_artifact,
        get_scientific_result,
        begin_scientific_artifact_upload,
        finalize_scientific_artifact_upload,
        download_scientific_artifact,
    ):
        server.add_tool(function, name=function.__name__, meta={"fs2_core": True})

    def named_handler(tool_name: str) -> Callable[..., Awaitable[dict[str, Any]]]:
        async def invoke_named_model(
            payload: dict[str, Any], ctx: Context, idempotency_key: str | None = None, wait_seconds: float = 0
        ) -> dict[str, Any]:
            principal = _principal()
            await runtime.revalidate_routes()
            matches = [
                (model, protocol)
                for model in runtime.registry.allowed_for_principal(principal, surface="mcp")
                for protocol in model.gateway.protocols
                if f"{model.binding.mcp_tool_name}_{protocol.replace('-', '_')}" == tool_name
            ]
            if len(matches) != 1:
                raise MCPError(code=INVALID_PARAMS, message="model tool is unavailable or ambiguous")
            model, protocol = matches[0]
            operation = runtime.registry.operation_for_protocol(model, protocol)
            try:
                traceparent = (ctx.headers or {}).get("traceparent")
            except ValueError:
                # Programmatic calls have no request context; they must not
                # bypass admission merely to synthesize a tracing header.
                traceparent = None
            return await _admit(
                runtime,
                model_id=model.id,
                protocol=protocol,
                operation=operation,
                payload=payload,
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
                traceparent=traceparent,
            )

        return invoke_named_model

    registered_names = set(CORE_TOOLS)

    def sync_model_tools() -> None:
        # Tool handlers resolve their model from the current registry on every
        # call.  Keeping old names registered is therefore safe: middleware
        # hides withdrawn names, and a later reuse cannot dispatch to stale
        # model identity.  The cap bounds operator-driven name churn; the
        # generic invoke_model tool remains available beyond it.
        for model in runtime.registry.list(enabled_only=True):
            if len(registered_names) >= 4096:
                return
            if not model.gateway.mcp_discoverable or not model.gateway.mcp_invocable:
                continue
            if len(model.gateway.policy_operations) != 1 or not model.binding.mcp_enabled:
                continue
            for protocol in model.gateway.protocols:
                name = f"{model.binding.mcp_tool_name}_{protocol.replace('-', '_')}"
                if name in registered_names:
                    continue
                model_view = _model_view(model)
                server.add_tool(
                    named_handler(name),
                    name=name,
                    title=f"{model.gateway.display_name} ({protocol})",
                    description=model.binding.mcp_description,
                    meta={
                        "fs2_model_id": model.id,
                        "fs2_protocol": protocol,
                        "fs2_model_revision": model.model_revision,
                        "fs2_active_runtime": model_view["active_runtime"],
                        "fs2_qualification": model_view["qualification"],
                    },
                )
                registered_names.add(name)

    authorization.set_tool_sync(sync_model_tools)
    sync_model_tools()
    return server


def mount_mcp(app: FastAPI, runtime: AppRuntime) -> MCPServer:
    """Mount MCP and explicitly compose its session manager into parent lifespan."""

    server = build_mcp_server(runtime)
    allowed_hosts, allowed_origins = runtime.settings.public_transport_allowlists()
    child = server.streamable_http_app(
        streamable_http_path=MCP_STREAMABLE_HTTP_PATH,
        # Sessions cannot be safely pinned through an elastic Gateway Service.
        # Stateless Streamable HTTP makes every request replica-independent;
        # durable operation state remains in PostgreSQL, never process memory.
        stateless_http=True,
        max_request_body_size=runtime.settings.max_request_bytes,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
            allowed_origins=list(allowed_origins),
        ),
    )
    child.router.redirect_slashes = False
    child.add_middleware(
        MCPPublicGatewayBoundary,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        max_body_bytes=runtime.settings.max_request_bytes,
    )
    child_lifespan = child.router.lifespan_context
    parent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(parent: FastAPI) -> AsyncIterator[None]:
        async with parent_lifespan(parent):
            # Starlette does not enter a mounted child's lifespan. Compose the
            # SDK app explicitly so its Streamable-HTTP session manager exists
            # before the parent accepts traffic.
            async with child_lifespan(child):
                yield

    app.router.lifespan_context = lifespan

    # Retain the pathless discovery alias used by generic OAuth clients while
    # the MCP SDK owns the resource-specific RFC 9728 path below.
    async def protected_resource_metadata() -> JSONResponse:
        return JSONResponse(
            {
                "resource": f"{runtime.settings.public_origin()}{MCP_HTTP_PATH}",
                "authorization_servers": [runtime.settings.authorization_server_url],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["mcp.invoke"],
            }
        )

    app.add_api_route(
        "/.well-known/oauth-protected-resource",
        protected_resource_metadata,
        methods=["GET"],
        include_in_schema=False,
    )
    # Mount deliberately at the root so the child's exact `/mcp` route stays
    # `/mcp`. Mounting the SDK app at `/mcp` would create `/mcp/mcp` with its
    # default route, while configuring the child route as `/` would expose only
    # the slash-terminated `/mcp/` through Starlette's mount semantics.
    app.mount(MCP_CHILD_MOUNT_PATH, child)
    app.state.mcp_server = server
    app.state.mcp_child = child
    return server
