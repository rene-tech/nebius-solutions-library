"""Typed MCP tools over the SAME application methods as REST; no separate jobs."""

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .gateway_store import uuid
from .serverless import ServerlessError, UnsupportedExecution


def mount_mcp(app, gateway):
    from .gateway import Submit

    server = MCPServer(
        "Nebius Scientific AI",
        version="0.1.0",
        instructions="GROMACS migration pilot. Upload immutable input artifacts, submit once with an "
        "idempotency key, poll the returned operation ID, then retrieve validated artifacts. "
        "Never put large file contents into tool arguments. Status is not a scientific result.",
    )

    def execute(ctx, method, *args, **kwargs):
        try:
            request = ctx.request_context.request
            with gateway.client(request.headers.get("Authorization", "")) as (client, store, info):
                return method(client, store, *args, **kwargs)
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
        return execute(ctx, lambda c, s: {"models": [gateway.model()]})

    @server.tool()
    def get_model_schema(model_id: str, ctx: Context) -> dict:
        """Get GROMACS native commands, parameter bounds, artifact and checkpoint contract."""
        if model_id != "gromacs":
            return {"error": {"code": "MODEL_NOT_FOUND", "retryable": False}}
        return execute(ctx, lambda c, s: gateway.model())

    @server.tool()
    def submit_gromacs_workflow(request: Submit, idempotency_key: str, ctx: Context) -> dict:
        """Run native GROMACS preparation, molecular dynamics and analysis on one GPU.

        Supply an immutable gromacs-inputs gzip-tar artifact manifest and typed
        native command steps. The supplied scientific protocol is not rewritten.
        Returns an operation ID, NOT finished simulation output. Identical retries
        must reuse idempotency_key. Native checkpoints survive GPU replacement.
        This migration pilot accepts one job per call; make separate calls for replicas.
        """
        return execute(ctx, gateway.submit, request, idempotency_key)

    @server.tool()
    def submit_scientific_run(model_id: str, request: Submit, idempotency_key: str, ctx: Context) -> dict:
        """Compatibility submission path for the same GROMACS contract and durable operation."""
        if model_id != "gromacs":
            return {"error": {"code": "MODEL_NOT_FOUND", "retryable": False}}
        return execute(ctx, gateway.submit, request, idempotency_key)

    @server.tool()
    def get_scientific_status(operation_id: str, ctx: Context) -> dict:
        """Poll a previously accepted run: queue, actual GPU/cluster, attempts and delivery state."""
        return execute(ctx, gateway.status, uuid(operation_id))

    @server.tool()
    def get_scientific_result(operation_id: str, ctx: Context) -> dict:
        """Get the verified native result and immutable output references after durable success."""
        return execute(ctx, gateway.result, uuid(operation_id))

    @server.tool()
    def get_scientific_checkpoints(operation_id: str, ctx: Context) -> dict:
        """Inspect the latest acknowledged native checkpoint; local-only files are not durable."""

        def get(client, store):
            result = gateway.status(client, store, uuid(operation_id))
            return {k: result.get(k) for k in ("operation_id", "checkpoint_ref", "checkpoint_generation")}

        return execute(ctx, get)

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
        return execute(ctx, gateway.resume, uuid(operation_id), idempotency_key, gpu_class=gpu_class)

    @server.tool()
    def cancel_scientific_run(operation_id: str, ctx: Context) -> dict:
        """Stop this caller-owned operation. Previously committed checkpoints remain resumable."""

        def cancel(client, store):
            value = store.get("operations", uuid(operation_id))
            client.cancel(value["s2_id"], idempotency_key="cancel-" + operation_id)
            return gateway.status(client, store, operation_id)

        return execute(ctx, cancel)

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
        )

    @server.tool()
    def finalize_scientific_artifact_upload(operation_id: str, upload_id: str, ctx: Context) -> dict:
        """Verify exact input bytes/checksum and obtain the immutable reference for a manifest."""
        return execute(ctx, lambda c, s: gateway.finalize_upload(s, uuid(upload_id)))

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
        )

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
                with gateway.client(request.headers.get("Authorization", "")):
                    pass

            try:
                await run_in_threadpool(authenticate)
            except (HTTPException, ServerlessError) as error:
                return JSONResponse({"error": "MCP authentication failed"}, status_code=error.status_code)
        return await call_next(request)

    app.mount("/", mcp_app)
    app.state.mcp = server
