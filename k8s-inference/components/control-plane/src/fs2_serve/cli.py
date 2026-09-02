"""Process entry point for the gateway and independent maintenance job."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fs2_serve_catalog.loader import load_catalog

from .admin import (
    AdminContextConfig,
    AdminReadService,
    CachedKubernetesAdminAdapter,
    CapacityAdminAdapter,
    KubernetesAdminAdapter,
    ObservabilityAdminAdapter,
    PrometheusAdminAdapter,
)
from .admin_adapters import (
    HttpKubernetesListReader,
    HttpPrometheusScalarReader,
    KubernetesCapacityAdminAdapter,
    KubernetesCapacityConfig,
    KubernetesModelStateAdminAdapter,
    KubernetesModelStateConfig,
    ManagedNodeScalerPoolContract,
    PrometheusModelMetricsAdminAdapter,
    PrometheusObservabilityAdminAdapter,
    PrometheusObservabilityConfig,
)
from .admin_models import AdminContextOption
from .admission import AdmissionService
from .api import AppRuntime, create_app
from .auth import OperatorSessionService, PepperRing, TokenService
from .configuration import (
    TERRAFORM_BASELINE_ACTOR,
    ConfigurationService,
    StaticCatalogConfigurationAdapter,
    StoreConfigurationAuditSink,
    StoreConfigurationRepository,
    catalog_configuration_contracts,
    load_platform_configuration,
    load_terraform_apply_receipt,
)
from .configuration_models import ConfigurationRevision, PlatformConfiguration
from .crypto import KeyedHasher, PayloadCipher
from .federation import FederationRouter
from .mcp_server import mount_mcp
from .model_deployment_admin import ModelDeploymentReadService, StoreModelDeploymentRepository
from .model_deployment_bridge import ModelDeploymentRuntimeBridge
from .model_deployment_controller import ControllerFiles, run_model_controller
from .model_deployment_mutation import HttpKubernetesDesiredWriter, ModelDeploymentMutationService
from .model_deployment_preview import ModelDeploymentPreviewService, RepositoryModelDeploymentPreviewState
from .models import TokenCreate
from .postgres import PostgresMaintenanceStore, PostgresStore
from .postgresql_release import render_postgresql_release_contract
from .registry import Registry
from .route_revalidation import RouteRevalidator
from .runtime import RuntimeClient
from .scientific_artifacts import (
    PostgresArtifactRepository,
    S3CompatibleArtifactHandleSigner,
    S3CompatibleArtifactObjectStore,
    ScientificArtifactService,
)
from .settings import Settings
from .store import ConflictError
from .telemetry import Metrics, configure_tracing


def _keys(settings: Settings) -> tuple[PayloadCipher, KeyedHasher]:
    return (
        PayloadCipher.from_file(settings.payload_keyring_file),
        KeyedHasher.from_file(settings.ledger_hmac_keyring_file),
    )


async def _store(settings: Settings) -> PostgresStore:
    cipher, hasher = _keys(settings)
    return await PostgresStore.connect(
        settings.database_url,
        settings.migrations_dir,
        cipher,
        hasher,
        settings.payload_ttl_seconds,
    )


def _admin_read_dependencies(
    settings: Settings,
    *,
    initial_configuration: PlatformConfiguration | None = None,
) -> tuple[
    KubernetesAdminAdapter | None,
    PrometheusAdminAdapter | None,
    CapacityAdminAdapter | None,
    ObservabilityAdminAdapter | None,
    AdminContextConfig,
]:
    kubernetes: KubernetesAdminAdapter | None = None
    capacity: CapacityAdminAdapter | None = None
    if settings.admin_capacity_enabled:
        kubernetes_reader = HttpKubernetesListReader(
            base_url=settings.admin_kubernetes_api_url,
            token_file=settings.admin_kubernetes_token_file,
            ca_file=settings.admin_kubernetes_ca_file,
            timeout_seconds=settings.admin_adapter_timeout_seconds,
        )
        kubernetes = CachedKubernetesAdminAdapter(
            KubernetesModelStateAdminAdapter(
                kubernetes_reader,
                config=KubernetesModelStateConfig(
                    model_namespace=settings.admin_kubernetes_model_namespace,
                ),
            ),
            ttl_seconds=settings.admin_kubernetes_cache_ttl_seconds,
        )
        capacity = KubernetesCapacityAdminAdapter(
            kubernetes_reader,
            config=KubernetesCapacityConfig(
                model_namespace=settings.admin_kubernetes_model_namespace,
                system_namespace=settings.admin_kubernetes_system_namespace,
                kueue_api_version=settings.admin_kueue_api_version,
                node_scaler_provider=settings.admin_node_scaler_provider,
                node_scaler_pools=tuple(
                    ManagedNodeScalerPoolContract(
                        pool_id=pool_id,
                        min_nodes=pool.min_nodes,
                        max_nodes=pool.max_nodes,
                    )
                    for pool_id, pool in sorted(initial_configuration.pools.items())
                )
                if initial_configuration is not None
                else (),
            ),
        )
    prometheus: PrometheusAdminAdapter | None = None
    observability: ObservabilityAdminAdapter | None = None
    if settings.admin_prometheus_url is not None:
        prometheus_reader = HttpPrometheusScalarReader(
            base_url=settings.admin_prometheus_url,
            timeout_seconds=settings.admin_adapter_timeout_seconds,
        )
        prometheus = PrometheusModelMetricsAdminAdapter(prometheus_reader)
        observability_config = (
            PrometheusObservabilityConfig.from_file(settings.admin_observability_config_file)
            if settings.admin_observability_config_file is not None
            else PrometheusObservabilityConfig()
        )
        observability = PrometheusObservabilityAdminAdapter(
            prometheus_reader,
            config=observability_config,
        )
    context = AdminContextConfig()
    if (
        settings.admin_context_project is not None
        and settings.admin_context_cluster is not None
        and settings.admin_context_region is not None
    ):
        context = AdminContextConfig(
            options=(
                AdminContextOption(
                    project=settings.admin_context_project,
                    cluster=settings.admin_context_cluster,
                    region=settings.admin_context_region,
                    label=settings.admin_context_label or settings.admin_context_cluster,
                ),
            )
        )
    return kubernetes, prometheus, capacity, observability, context


async def _synchronize_admin_configuration(
    repository: StoreConfigurationRepository,
    current: ConfigurationRevision | None,
    configuration: PlatformConfiguration,
    receipt_file: Path | None,
) -> None:
    """Adopt a Terraform baseline, or close an explicitly reviewed handoff."""

    if current is not None and receipt_file is not None:
        try:
            receipt = load_terraform_apply_receipt(receipt_file)
            await repository.accept_terraform_applied(
                configuration,
                receipt,
                actor="terraform-applied",
            )
        except (ConflictError, ValueError):
            logging.getLogger("fs2_serve.configuration").exception(
                "optional Terraform apply receipt was invalid; adopting the mounted baseline"
            )
        else:
            return
    await repository.adopt_terraform_baseline(
        configuration,
        actor=TERRAFORM_BASELINE_ACTOR,
    )


async def build_runtime(settings: Settings) -> AppRuntime:
    registry = Registry.load(
        settings.catalog_dir,
        settings.bindings_file,
        variant_promotions_file=settings.variant_promotions_file,
        lean_routes_file=settings.lean_routes_file,
        repo_root=settings.repo_root,
        evidence_root=settings.evidence_root,
        trusted_attestors_loader=settings.trusted_route_attestors,
        max_attempts=settings.max_attempts,
        max_gpu_seconds_per_attempt=settings.max_gpu_seconds_per_attempt,
        retry_base_seconds=settings.retry_base_seconds,
    )
    route_revalidator = RouteRevalidator(
        registry,
        interval_seconds=settings.route_revalidation_interval_seconds,
    )
    federation = FederationRouter.load(
        settings.federation_routes_file,
        registry.list(),
        secret_root=settings.federation_secret_dir,
    )
    store = await _store(settings)
    artifact_store = S3CompatibleArtifactObjectStore(
        endpoint=settings.artifact_store_endpoint, bucket=settings.artifact_store_bucket,
        region=settings.artifact_store_region, access_key=settings.artifact_store_access_key,
        secret_key=settings.artifact_store_secret_key,
    )
    artifact_signer = S3CompatibleArtifactHandleSigner(
        endpoint=settings.artifact_store_endpoint, bucket=settings.artifact_store_bucket,
        region=settings.artifact_store_region, access_key=settings.artifact_store_access_key,
        secret_key=settings.artifact_store_secret_key,
    )
    artifact_service = ScientificArtifactService(
        repository=PostgresArtifactRepository(store.pool), object_store=artifact_store,
        signer=artifact_signer,
        allowed_media_types={"application/octet-stream", "application/json", "chemical/x-pdb", "text/plain"},
    )
    peppers = PepperRing.from_file(settings.token_pepper_file)
    tokens = TokenService(store, peppers)
    model_deployment_preview: ModelDeploymentPreviewService | None = None
    model_deployment_read: ModelDeploymentReadService | None = None
    model_deployment_mutation: ModelDeploymentMutationService | None = None
    model_deployment_bridge: ModelDeploymentRuntimeBridge | None = None
    if settings.model_controller_enabled:
        controller_files = ControllerFiles.load(
            settings.model_controller_envelope_file,
            settings.model_controller_bundles_file,
        )
        model_repository = StoreModelDeploymentRepository(store)
        model_deployment_read = ModelDeploymentReadService(model_repository)
        model_deployment_preview = ModelDeploymentPreviewService(
            envelope=controller_files.infrastructure_envelope,
            renderer=controller_files.renderer(),
            state=RepositoryModelDeploymentPreviewState(model_repository),
            prometheus_server_address=settings.model_controller_prometheus_server_address,
            namespace=settings.model_controller_namespace,
            mutation_supported=settings.model_controller_writes_enabled,
        )
        if settings.model_controller_writes_enabled:
            kubernetes_models = HttpKubernetesDesiredWriter(
                base_url=settings.admin_kubernetes_api_url,
                token_file=settings.admin_kubernetes_token_file,
                ca_file=settings.admin_kubernetes_ca_file,
                namespace=settings.model_controller_namespace,
                timeout_seconds=settings.model_controller_api_timeout_seconds,
            )
            model_deployment_mutation = ModelDeploymentMutationService(
                repository=model_repository,
                writer=kubernetes_models,
                envelope=controller_files.infrastructure_envelope,
                namespace=settings.model_controller_namespace,
            )
            model_deployment_bridge = ModelDeploymentRuntimeBridge(
                repository=model_repository,
                writer=kubernetes_models,
                source=kubernetes_models,
                registry=registry,
                interval_seconds=settings.model_controller_poll_seconds,
                route_ttl_seconds=max(30.0, settings.model_controller_poll_seconds * 3),
                namespace=settings.model_controller_namespace,
                close_source=kubernetes_models.close,
            )
    # Canonical catalog metadata remains observable when promotion deliberately
    # leaves zero routable models. Request and queue series are still populated
    # only from durable admitted operations.
    metrics = Metrics(registry.list())
    runtime_client = RuntimeClient(
        activation_timeout_seconds=settings.activation_timeout_seconds,
        runtime_timeout_seconds=settings.runtime_timeout_seconds,
        max_response_bytes=settings.max_response_bytes,
        federation=federation,
    )
    async def refresh_routes() -> bool:
        if not await route_revalidator.refresh():
            return False
        dynamic_healthy = True
        if model_deployment_bridge is not None:
            dynamic_healthy = await model_deployment_bridge.refresh()
        metrics.sync_models(registry.list())
        return dynamic_healthy and bool(registry.validation_health()["healthy"])

    admission = AdmissionService(
        registry=registry,
        store=store,
        runtime=runtime_client,
        metrics=metrics,
        worker_concurrency=settings.worker_concurrency,
        poll_seconds=settings.worker_poll_seconds,
        lease_seconds=settings.worker_lease_seconds,
        maintenance_interval_seconds=settings.maintenance_interval_seconds,
        shutdown_grace_seconds=settings.shutdown_grace_seconds,
        max_sync_waiters=settings.max_sync_waiters,
        wait_poll_initial_seconds=settings.wait_poll_initial_seconds,
        wait_poll_max_seconds=settings.wait_poll_max_seconds,
        route_refresh=refresh_routes,
    )
    initial_configuration = (
        load_platform_configuration(settings.admin_configuration_file)
        if settings.admin_configuration_file is not None
        else None
    )
    kubernetes, prometheus, capacity, observability, contexts = _admin_read_dependencies(
        settings,
        initial_configuration=initial_configuration,
    )
    admin_read = AdminReadService(
        registry=registry,
        store=store,
        kubernetes=kubernetes,
        prometheus=prometheus,
        capacity=capacity,
        observability=observability,
        contexts=contexts,
        source_max_age_seconds=settings.admin_source_max_age_seconds,
        adapter_timeout_seconds=settings.admin_adapter_timeout_seconds,
    )
    configure_tracing(settings.otlp_endpoint)
    configuration_service: ConfigurationService | None = None
    if initial_configuration is not None:
        canonical_catalog = load_catalog(settings.catalog_dir, repo_root=settings.repo_root)
        configuration_repository = StoreConfigurationRepository(store)
        configuration_service = ConfigurationService(
            repository=configuration_repository,
            catalog=StaticCatalogConfigurationAdapter(catalog_configuration_contracts(canonical_catalog)),
            audit=StoreConfigurationAuditSink(store),
        )
        validation = await configuration_service.validate_bootstrap(initial_configuration)
        if not validation.valid:
            raise RuntimeError("initial admin configuration does not match the canonical catalog")
        await _synchronize_admin_configuration(
            configuration_repository,
            await store.configuration_current(),
            initial_configuration,
            settings.admin_configuration_receipt_file,
        )
    return AppRuntime(
        settings=settings,
        registry=registry,
        store=store,
        tokens=tokens,
        admission=admission,
        metrics=metrics,
        admin_token=settings.admin_token(),
        operator_sessions=OperatorSessionService(
            store,
            peppers,
            ttl_seconds=settings.admin_session_ttl_seconds,
        ),
        route_revalidator=route_revalidator,
        admin_read=admin_read,
        configuration=configuration_service,
        model_deployment_preview=model_deployment_preview,
        model_deployment_read=model_deployment_read,
        model_deployment_mutation=model_deployment_mutation,
        model_deployment_bridge=model_deployment_bridge,
        artifact_service=artifact_service,
    )


async def build_app(settings: Settings) -> FastAPI:
    """Build the exact composed HTTP/MCP application used by Uvicorn."""

    runtime = await build_runtime(settings)
    app = create_app(runtime)
    mount_mcp(app, runtime)
    return app


async def serve(settings: Settings) -> None:
    app = await build_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())
    )
    await server.serve()


async def maintain(settings: Settings) -> None:
    store = await PostgresMaintenanceStore.connect(settings.database_url)
    try:
        await store.purge_expired_payloads()
        await store.delete_expired_rows(
            operation_retention_seconds=settings.operation_retention_seconds,
            token_retention_seconds=settings.pat_retention_seconds,
            audit_retention_seconds=settings.audit_retention_seconds,
            usage_retention_seconds=settings.usage_retention_seconds,
        )
    finally:
        await store.close()


async def migrate(settings: Settings) -> None:
    await PostgresStore.migrate_database(
        settings.database_url,
        settings.migrations_dir,
        settings.reporting_database_role,
        settings.runtime_database_role,
        settings.maintenance_database_role,
        settings.activation_database_role,
    )


async def wait_schema(settings: Settings) -> None:
    await PostgresStore.wait_for_schema(
        settings.database_url,
        settings.migrations_dir,
        settings.schema_wait_seconds,
    )


async def bootstrap_access(settings: Settings) -> None:
    """Idempotently seed the Terraform-owned MCP/inference bootstrap PAT."""

    await PostgresStore.wait_for_schema(
        settings.database_url,
        settings.migrations_dir,
        settings.schema_wait_seconds,
    )
    store = await _store(settings)
    try:
        service = TokenService(store, PepperRing.from_file(settings.token_pepper_file))
        await service.ensure_provisioned(
            settings.bootstrap_access_token(),
            TokenCreate(
                principal_id=settings.bootstrap_access_principal_id,
                tenant_id=settings.bootstrap_access_tenant_id,
                scopes=settings.bootstrap_access_scopes,
                models=settings.bootstrap_access_models,
                max_concurrency=settings.bootstrap_access_max_concurrency,
                name=settings.bootstrap_access_name,
            ),
            created_by="terraform-bootstrap",
        )
    finally:
        await store.close()


def validate(settings: Settings) -> None:
    registry = Registry.load(
        settings.catalog_dir,
        settings.bindings_file,
        variant_promotions_file=settings.variant_promotions_file,
        lean_routes_file=settings.lean_routes_file,
        repo_root=settings.repo_root,
        evidence_root=settings.evidence_root,
        trusted_attestors_loader=settings.trusted_route_attestors,
        max_attempts=settings.max_attempts,
        max_gpu_seconds_per_attempt=settings.max_gpu_seconds_per_attempt,
        retry_base_seconds=settings.retry_base_seconds,
    )
    federation = FederationRouter.load(
        settings.federation_routes_file,
        registry.list(),
        secret_root=settings.federation_secret_dir,
    )
    asyncio.run(federation.close())


def emit_postgresql_release_contract(settings: Settings) -> None:
    """Write the exact value-suppressed PostgreSQL receipt inputs to stdout."""

    sys.stdout.buffer.write(render_postgresql_release_contract(settings.migrations_dir))


def main() -> None:
    parser = argparse.ArgumentParser(prog="fs2-serve")
    parser.add_argument(
        "command",
        choices=(
            "serve",
            "maintenance",
            "migrate",
            "wait-schema",
            "bootstrap-access",
            "validate",
            "postgresql-release-contract",
            "model-controller",
        ),
        nargs="?",
        default="serve",
    )
    args = parser.parse_args()
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "validate":
        validate(settings)
    elif args.command == "postgresql-release-contract":
        emit_postgresql_release_contract(settings)
    else:
        action = {
            "serve": serve,
            "maintenance": maintain,
            "migrate": migrate,
            "wait-schema": wait_schema,
            "bootstrap-access": bootstrap_access,
            "model-controller": run_model_controller,
        }[args.command]
        asyncio.run(action(settings))


if __name__ == "__main__":
    main()
