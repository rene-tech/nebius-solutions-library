"""Dedicated process entrypoint for the externally released network authority."""

from __future__ import annotations

import asyncio

import uvicorn

from .network_boundary_admission import (
    KubernetesBoundaryReader,
    NetworkBoundaryAdmission,
    NetworkBoundaryConfig,
    create_network_boundary_app,
)
from .settings import Settings


async def serve() -> None:
    """Run only the admission authority; never import the control-plane CLI."""

    settings = Settings()
    if not settings.network_boundary_admission_enabled:
        raise RuntimeError("network-boundary admission is disabled")
    reader = KubernetesBoundaryReader(
        base_url=settings.network_boundary_admission_api_url,
        token_file=settings.network_boundary_admission_token_file,
        ca_file=settings.network_boundary_admission_ca_file,
        timeout_seconds=settings.network_boundary_admission_api_timeout_seconds,
    )
    admission = NetworkBoundaryAdmission(
        config=NetworkBoundaryConfig(
            model_namespace=settings.network_boundary_admission_model_namespace,
            system_namespace=settings.network_boundary_admission_system_namespace,
            authority_namespace=settings.network_boundary_admission_authority_namespace,
            authorizer_writer=settings.network_boundary_admission_authorizer_writer,
            acquisition_writer=settings.network_boundary_admission_acquisition_writer,
            direct_job_writer=settings.network_boundary_admission_direct_job_writer,
            jobset_writer=settings.network_boundary_admission_jobset_writer,
            model_controller_writer=settings.network_boundary_admission_model_controller_writer,
            transition_writer=settings.network_boundary_admission_transition_writer,
            maintenance_writer=settings.network_boundary_admission_maintenance_writer,
            certificate_writer=settings.network_boundary_admission_certificate_writer,
            authorizer_groups=frozenset(settings.network_boundary_admission_authorizer_groups),
            transition_groups=frozenset(settings.network_boundary_admission_transition_groups),
            maintenance_groups=frozenset(settings.network_boundary_admission_maintenance_groups),
            certificate_groups=frozenset(settings.network_boundary_admission_certificate_groups),
        ),
        reader=reader,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_network_boundary_app(
                admission,
                readiness_files=(
                    settings.network_boundary_admission_token_file,
                    settings.network_boundary_admission_ca_file,
                    settings.network_boundary_admission_tls_cert_file,
                    settings.network_boundary_admission_tls_key_file,
                ),
                restart_on_change_files=(
                    settings.network_boundary_admission_tls_cert_file,
                    settings.network_boundary_admission_tls_key_file,
                ),
            ),
            host=settings.network_boundary_admission_host,
            port=settings.network_boundary_admission_port,
            log_level=settings.log_level.lower(),
            ssl_certfile=str(settings.network_boundary_admission_tls_cert_file),
            ssl_keyfile=str(settings.network_boundary_admission_tls_key_file),
        )
    )
    try:
        await server.serve()
    finally:
        await reader.close()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
