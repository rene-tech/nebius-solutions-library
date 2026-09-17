"""Entrypoint for the external provider-custody gateway loopback service."""

from __future__ import annotations

import os

import uvicorn

from .provider_custody_gateway import (
    GatewayPolicy,
    ProviderCustodyGateway,
    create_provider_custody_app,
)


def main() -> None:
    """Serve behind the same-host mTLS proxy; never bind this port publicly."""

    host = os.environ.get("FS2_PROVIDER_CUSTODY_LISTEN_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "::1"}:
        raise RuntimeError("the provider custody ASGI service must remain loopback-only")
    port = int(os.environ.get("FS2_PROVIDER_CUSTODY_LISTEN_PORT", "9080"))
    app = create_provider_custody_app(ProviderCustodyGateway(GatewayPolicy.load()))
    uvicorn.run(app, host=host, port=port, access_log=False, server_header=False)


if __name__ == "__main__":
    main()
