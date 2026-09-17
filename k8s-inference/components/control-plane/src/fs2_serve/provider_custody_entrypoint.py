"""Entrypoint for the external provider-custody gateway mutual-TLS service."""

from __future__ import annotations

import asyncio
import hashlib
import os
import ssl
from pathlib import Path

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from .provider_custody_gateway import (
    GatewayPolicy,
    ProviderCustodyGateway,
    create_provider_custody_app,
)


def _private_file(environment_name: str) -> Path:
    path = Path(os.environ.get(environment_name, ""))
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_mode & 0o077
    ):
        raise RuntimeError(
            f"{environment_name} must be an absolute mode-0600 regular file"
        )
    return path


class ProviderMutualTLSProtocol(H11Protocol):
    """Bind ASGI identity to the certificate verified on this TLS transport."""

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        tls = transport.get_extra_info("ssl_object")
        certificate = tls.getpeercert(binary_form=True) if tls is not None else None
        if not certificate:
            transport.abort()
            return
        self.app_state = {
            **self.app_state,
            "fs2_provider_client_certificate_sha256": hashlib.sha256(
                certificate
            ).hexdigest(),
        }
        super().connection_made(transport)


def main() -> None:
    """Terminate authenticated TLS in the same measured gateway process."""

    policy = GatewayPolicy.load()
    host = os.environ.get("FS2_PROVIDER_CUSTODY_LISTEN_HOST", "")
    port = int(os.environ.get("FS2_PROVIDER_CUSTODY_LISTEN_PORT", "0"))
    if (
        host != policy.member["listener_address"]
        or port != policy.member["listener_port"]
    ):
        raise RuntimeError(
            "the provider custody listener must equal the measured member address and port"
        )
    server_certificate = _private_file("FS2_PROVIDER_GATEWAY_SERVER_CERT")
    server_key = _private_file("FS2_PROVIDER_GATEWAY_SERVER_KEY")
    client_ca = _private_file("FS2_PROVIDER_GATEWAY_CLIENT_CA")
    app = create_provider_custody_app(ProviderCustodyGateway(policy))
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        http=ProviderMutualTLSProtocol,
        ws="none",
        access_log=False,
        server_header=False,
        ssl_certfile=str(server_certificate),
        ssl_keyfile=str(server_key),
        ssl_ca_certs=str(client_ca),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        ssl_version=ssl.PROTOCOL_TLS_SERVER,
    )
    config.load()
    if config.ssl is None:
        raise RuntimeError("provider custody TLS context was not created")
    config.ssl.minimum_version = ssl.TLSVersion.TLSv1_3
    config.ssl.maximum_version = ssl.TLSVersion.TLSv1_3
    config.ssl.verify_mode = ssl.CERT_REQUIRED
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
