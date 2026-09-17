"""Dedicated entrypoint for the scientific Kubernetes writer."""

from __future__ import annotations

import asyncio

import uvicorn

from .scientific_batch.writer_proxy import ScientificWriter, create_scientific_writer_app
from .settings import Settings


async def serve() -> None:
    settings = Settings()
    if not settings.scientific_writer_enabled:
        raise RuntimeError("scientific writer is disabled")
    writer = ScientificWriter(
        api_url=settings.scientific_batch_kubernetes_api_url,
        token_file=settings.scientific_writer_kubernetes_token_file,
        ca_file=settings.scientific_batch_kubernetes_ca_file,
        caller_username=settings.scientific_writer_caller_username,
        caller_audience=settings.scientific_writer_caller_audience,
        allowed_namespaces=frozenset(settings.scientific_writer_allowed_namespaces),
        timeout_seconds=settings.scientific_batch_api_timeout_seconds,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_scientific_writer_app(writer),
            host=settings.scientific_writer_host,
            port=settings.scientific_writer_port,
            log_level=settings.log_level.lower(),
        )
    )
    try:
        await server.serve()
    finally:
        await writer.close()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
