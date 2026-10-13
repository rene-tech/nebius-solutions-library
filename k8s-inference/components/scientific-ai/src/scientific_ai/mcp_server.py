"""Typed MCP tools over the SAME application methods as REST; no separate jobs."""

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .endpoint_catalog import catalog
from .endpoint_contracts import (
    EndpointArtifactError,
    EndpointInputError,
    NativeResultError,
    apply_tool_input_contract,
)
from .endpoint_gateway import NativeCallError, NativeCapabilityError, has_scope, model_allowed
from .gateway_store import uuid
from .serverless import ServerlessError, UnsupportedExecution


def mount_mcp(app, gateway):
    from .gateway import Submit

    native_tools = {
        contract.tool_name: model_id for model_id, contract in catalog().items() if contract.tool_name
    }
    gromacs_scopes = {
        "submit_gromacs_workflow": "run:submit",
        "submit_scientific_run": "run:submit",
        "get_scientific_checkpoints": "read:operations",
        "resume_gromacs_workflow": "run:control",
        "retry_scientific_delivery": "run:control",
        "cancel_scientific_run": "run:control",
    }
    utility_scopes = {
        "list_scientific_models": "read:catalog",
        "get_model_schema": "read:catalog",
        "get_scientific_status": "read:operations",
        "get_scientific_result": "read:results",
        "begin_scientific_artifact_upload": "artifacts:write",
        "finalize_scientific_artifact_upload": "artifacts:write",
        "get_scientific_artifact": "read:results",
    }

    class AuthorizedMCPServer(MCPServer):
        async def _handle_list_tools(self, ctx, params):
            result = await super()._handle_list_tools(ctx, params)

            def visible():
                with gateway.client(ctx.request.headers.get("Authorization", ""), model_id=None) as (
                    client,
                    _,
                    info,
                ):
                    live = (
                        gateway.endpoints.available(client, info) if has_scope(info, "read:catalog") else {}
                    )
                    names = {name for name, scope in utility_scopes.items() if has_scope(info, scope)}
                    if model_allowed(info, gateway.settings.model_id):
                        names.update(name for name, scope in gromacs_scopes.items() if has_scope(info, scope))
                    if has_scope(info, "invoke:sync"):
                        names.update(name for name, model_id in native_tools.items() if model_id in live)
                    return names

            names = await run_in_threadpool(visible)
            result.tools = [tool for tool in result.tools if tool.name in names]
            return result

        async def call_tool(self, name, arguments, context=None):
            if name in native_tools:
                try:
                    gateway.endpoints.synchronous_options(options=arguments)
                except NativeCapabilityError as error:
                    raise MCPError(INVALID_PARAMS, error.public()["message"], data=error.public()) from None
            return await super().call_tool(name, arguments, context)

    server = AuthorizedMCPServer(
        "Nebius Scientific AI",
        version="0.1.0",
        instructions="Scientific AI migration pilot. GROMACS: upload immutable inputs, submit once with an "
        "idempotency key, poll the returned operation ID, then retrieve validated artifacts. "
        "Never put large file contents into tool arguments. Status is not a scientific result. "
        "Native endpoint tools return synchronous results only: no durable async or idempotent retry. "
        "A timeout has unknown admission; never automatically repeat it. "
        "Tools are filtered by current S2 availability.",
    )

    def execute(ctx, method, *args, model_id="gromacs", scope=None, with_info=False, **kwargs):
        try:
            request = ctx.request_context.request
            with gateway.client(request.headers.get("Authorization", ""), model_id=model_id, scope=scope) as (
                client,
                store,
                info,
            ):
                return method(client, store, *([info] if with_info else []), *args, **kwargs)
        except (NativeCallError, NativeResultError, NativeCapabilityError, EndpointInputError) as error:
            return {"error": error.public()}
        except EndpointArtifactError as error:
            return {"error": {"code": error.code, "retryable": False, "admission": "not-submitted"}}
        except HTTPException as error:
            return {
                "error": {
                    "code": "REQUEST_REJECTED",
                    "status_code": error.status_code,
                    "message": error.detail,
                    "retryable": False,
                }
            }
        except UnsupportedExecution:
            return {
                "error": {
                    "code": "EXECUTION_CAPABILITY_UNAVAILABLE",
                    "status_code": 503,
                    "retryable": True,
                    "admission": "not-confirmed",
                }
            }
        except ServerlessError as error:
            return {
                "error": {
                    "code": "SERVERLESS_REQUEST_FAILED",
                    "status_code": error.status_code,
                    "retryable": error.retryable,
                    "admission": "unknown" if error.retryable else "not-confirmed",
                }
            }
        except (KeyError, ValueError, PermissionError):
            return {"error": {"code": "INVALID_OR_UNAUTHORIZED_REFERENCE", "retryable": False}}

    @server.tool()
    def list_scientific_models(ctx: Context) -> dict:
        """List the caller's available scientific Apps and exact runtime/schema capabilities."""
        return execute(
            ctx,
            lambda c, s, i: {"models": gateway.models(c, i)},
            model_id=None,
            scope="read:catalog",
            with_info=True,
        )

    @server.tool()
    def get_model_schema(model_id: str, ctx: Context) -> dict:
        """Get the selected App's exact native fields, artifact transport and execution limits."""
        return execute(
            ctx,
            lambda c, s, i: gateway.schema(c, i, model_id),
            model_id=model_id,
            scope="read:catalog",
            with_info=True,
        )

    @server.tool()
    def submit_gromacs_workflow(request: Submit, idempotency_key: str, ctx: Context) -> dict:
        """Run native GROMACS preparation, molecular dynamics and analysis on one GPU.

        Supply an immutable gromacs-inputs gzip-tar artifact manifest and typed
        native command steps. The supplied scientific protocol is not rewritten.
        Returns an operation ID, NOT finished simulation output. Identical retries
        must reuse idempotency_key. Native checkpoints survive GPU replacement.
        This migration pilot accepts one job per call; make separate calls for replicas.
        """
        return execute(ctx, gateway.submit, request, idempotency_key, scope="run:submit")

    @server.tool()
    def submit_scientific_run(model_id: str, request: Submit, idempotency_key: str, ctx: Context) -> dict:
        """Compatibility submission path for the same GROMACS contract and durable operation."""
        if model_id != "gromacs":
            return {"error": {"code": "MODEL_NOT_FOUND", "retryable": False}}
        return execute(ctx, gateway.submit, request, idempotency_key, scope="run:submit")

    @server.tool()
    def get_scientific_status(operation_id: str, ctx: Context) -> dict:
        """Poll a previously accepted run: queue, actual GPU/cluster, attempts and delivery state."""
        return execute(
            ctx,
            gateway.operation_status,
            operation_id,
            model_id=None,
            scope="read:operations",
            with_info=True,
        )

    @server.tool()
    def get_scientific_result(operation_id: str, ctx: Context) -> dict:
        """Get the verified native result and immutable output references after durable success."""
        return execute(
            ctx,
            gateway.operation_status,
            operation_id,
            result=True,
            model_id=None,
            scope="read:results",
            with_info=True,
        )

    @server.tool()
    def get_scientific_checkpoints(operation_id: str, ctx: Context) -> dict:
        """Inspect the latest acknowledged native checkpoint; local-only files are not durable."""

        def get(client, store):
            result = gateway.status(client, store, uuid(operation_id))
            return {k: result.get(k) for k in ("operation_id", "checkpoint_ref", "checkpoint_generation")}

        return execute(ctx, get, scope="read:operations")

    @server.tool()
    def resume_gromacs_workflow(
        operation_id: str,
        idempotency_key: str,
        ctx: Context,
        gpu_class: str | None = None,
    ) -> dict:
        """Continue from the last acknowledged native checkpoint on an approved GPU.

        Returns a NEW operation ID to poll. Reuses TPR, coordinates, velocities,
        completed preparation and output history. Does not regenerate velocities.
        This pilot preserves the submitted time budget. Reuse the key on retries.
        GPU class must be one of the original run's qualified runtime choices.
        """
        return execute(
            ctx, gateway.resume, uuid(operation_id), idempotency_key, gpu_class=gpu_class, scope="run:control"
        )

    @server.tool()
    def retry_scientific_delivery(operation_id: str, idempotency_key: str, ctx: Context) -> dict:
        """Retry delivery of completed scientific files using CPU only, without rerunning MD.

        Use after a completed calculation could not upload its final outputs.
        Poll the SAME operation ID. Only the model's fixed transfer command is
        permitted; failed scientific computation cannot be promoted to success.
        Reuse idempotency_key on network retries. Serverless bounds retries and
        cancellation. Unconfigured deployments return a capability error.
        """
        return execute(ctx, gateway.retry_delivery, uuid(operation_id), idempotency_key, scope="run:control")

    @server.tool()
    def cancel_scientific_run(operation_id: str, ctx: Context) -> dict:
        """Stop this caller-owned operation. Previously committed checkpoints remain resumable."""

        def cancel(client, store):
            value = store.get("operations", uuid(operation_id))
            client.cancel(value["s2_id"], idempotency_key="cancel-" + operation_id)
            return gateway.status(client, store, operation_id)

        return execute(ctx, cancel, scope="run:control")

    @server.tool()
    def begin_scientific_artifact_upload(
        model_id: str,
        sha256: str,
        size_bytes: int,
        media_type: str,
        ctx: Context,
        compression: str = "none",
        idempotency_key: str | None = None,
    ) -> dict:
        """Reserve an immutable input. Transfer bytes using returned HTTPS URL, then finalize."""
        return execute(
            ctx,
            lambda c, s: gateway.reserve(
                s,
                {
                    "model_id": model_id,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "media_type": media_type,
                    "compression": compression,
                },
            ),
            model_id=model_id,
            scope="artifacts:write",
        )

    @server.tool()
    def finalize_scientific_artifact_upload(operation_id: str, upload_id: str, ctx: Context) -> dict:
        """Verify exact input bytes/checksum and obtain the immutable reference for a manifest."""
        return execute(
            ctx,
            lambda c, s: gateway.finalize_upload(s, uuid(upload_id)),
            model_id=None,
            scope="artifacts:write",
        )

    @server.tool()
    def get_scientific_artifact(artifact_id: str, ctx: Context) -> dict:
        """Get metadata and an authenticated download path; never return raw trajectory bytes."""
        return execute(
            ctx,
            lambda c, s: {
                **gateway.pointer(s.get("artifacts", uuid(artifact_id))),
                "download_url": gateway.settings.public_url
                + f"/v1/scientific-artifacts/{artifact_id}/content",
            },
            model_id=None,
            scope="read:results",
        )

    def register_native(contract):
        def invoke(payload: dict, ctx: Context) -> dict:
            return execute(
                ctx,
                lambda c, s, i: gateway.endpoints.invoke(c, s, i, contract.model_id, payload, surface="mcp"),
                model_id=contract.model_id,
                scope="invoke:sync",
                with_info=True,
            )

        description = contract.schema("mcp").get("description", "")
        server.tool(
            name=contract.tool_name,
            description=description + " Synchronous result only; no durable async or idempotency. "
            "Use private immutable artifact references for files.",
        )(invoke)
        apply_tool_input_contract(server, contract.tool_name, contract)

    for contract in catalog().values():
        if contract.tool_name:
            register_native(contract)

    host = urlsplit(gateway.settings.public_url).netloc
    mcp_app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host],
            allowed_origins=["https://" + host],
        ),
    )
    old_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with old_lifespan(application), mcp_app.router.lifespan_context(mcp_app):
            yield

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def authenticate_mcp(request, call_next):
        if request.url.path.rstrip("/") == "/mcp":

            def authenticate():
                with gateway.client(
                    request.headers.get("Authorization", ""), model_id=None, scope="mcp.invoke"
                ):
                    pass

            try:
                await run_in_threadpool(authenticate)
            except (HTTPException, ServerlessError) as error:
                return JSONResponse({"error": "MCP authentication failed"}, status_code=error.status_code)
            except PermissionError:
                return JSONResponse({"error": "MCP storage binding unavailable"}, status_code=403)
        return await call_next(request)

    app.mount("/", mcp_app)
    app.state.mcp = server
