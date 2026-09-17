"""Expose the loopback-only collector on a dedicated network-policy port."""

from __future__ import annotations

import httpx
from fastapi import FastAPI, Response

MAX_METRICS_RESPONSE_BYTES = 16 * 1024 * 1024


def create_metrics_proxy_app(
    *,
    application_port: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Return a fixed-destination proxy with no caller-controlled upstream."""

    app = FastAPI(
        title="fs2-serve metrics proxy",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{application_port}",
                timeout=10,
                trust_env=False,
                transport=transport,
            ) as client:
                upstream = await client.get("/metrics")
            if upstream.status_code != 200 or len(upstream.content) > MAX_METRICS_RESPONSE_BYTES:
                return Response(status_code=503)
            return Response(
                content=upstream.content,
                media_type="text/plain; version=0.0.4; charset=utf-8",
                headers={"cache-control": "no-store", "x-content-type-options": "nosniff"},
            )
        except httpx.HTTPError:
            return Response(status_code=503)

    return app
