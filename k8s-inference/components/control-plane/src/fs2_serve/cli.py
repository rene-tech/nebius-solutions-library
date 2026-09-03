"""Process entry point for the gateway and independent maintenance job."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import uvicorn
from fastapi import FastAPI
from fs2_serve_catalog.loader import load_catalog

from .academic_assets import CatalogAcademicAssetAdminAdapter
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
from .lifecycle import PostgresLifecycleRepository
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
from .scientific_admin_postgres import postgres_scientific_admin_read_service
from .scientific_artifacts import (
    PostgresArtifactRepository,
    ScientificArtifactService,
)
from .scientific_batch.artifact_bridge import ArtifactServiceBridge, SignedArtifactContentReader
from .scientific_batch.capability import ScientificWorkloadCapabilityAuthority
from .scientific_batch.companion import (
    WorkloadArtifactHttpClient,
    collect_and_commit,
    materialize_artifact,
    prepare_workspace,
    verify_runtime_artifacts,
)
from .scientific_batch.controller import ScientificBatchController
from .scientific_batch.execution import FileScientificManifestRenderer
from .scientific_batch.kubernetes import HttpScientificBatchCluster
from .scientific_batch.models import MaterializationMode
from .scientific_batch.postgres_repository import PostgresScientificBatchRepository
from .scientific_batch.profile_catalog import ScientificProfileCatalog
from .scientific_batch.scheduling import SchedulingContractResolver
from .scientific_batch.service import ScientificBatchService
from .scientific_batch.worker import ScientificBatchWorker
from .scientific_object_store import ObjectStoreConfig, S3ArtifactObjectStore
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


def _artifact_service(
    settings: Settings,
    repository: PostgresArtifactRepository,
) -> ScientificArtifactService | None:
    """Build the artifact service only when object storage is fully configured.

    An unconfigured deployment gets no artifact routes at all, instead of
    routes backed by anonymous credentials that would fail at first use.
    """

    if not settings.scientific_artifacts_enabled:
        return None
    access_key, secret_key = settings.artifact_store_credentials()
    object_store = S3ArtifactObjectStore(
        ObjectStoreConfig(
            endpoint_url=settings.artifact_store_endpoint,
            bucket=settings.artifact_store_bucket,
            region=settings.artifact_store_region,
            access_key=access_key,
            secret_key=secret_key,
            addressing_style=settings.artifact_store_addressing_style,
            verify_tls=settings.artifact_store_verify_tls,
            max_stream_bytes=settings.artifact_max_bytes,
        )
    )
    return ScientificArtifactService(
        repository=repository,
        object_store=object_store,
        allowed_media_types=settings.artifact_media_types_set(),
        max_artifact_bytes=settings.artifact_max_bytes,
        default_handle_ttl=timedelta(seconds=settings.artifact_handle_ttl_seconds),
        retention=timedelta(seconds=settings.artifact_retention_seconds),
        require_tls_handles=settings.artifact_store_verify_tls,
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
    artifact_repository = PostgresArtifactRepository(store.pool)
    artifact_service = _artifact_service(settings, artifact_repository)
    lifecycle = PostgresLifecycleRepository(store.pool)
    peppers = PepperRing.from_file(settings.token_pepper_file)
    tokens = TokenService(store, peppers)
    model_deployment_preview: ModelDeploymentPreviewService | None = None
    model_deployment_read: ModelDeploymentReadService | None = None
    model_deployment_mutation: ModelDeploymentMutationService | None = None
    model_deployment_bridge: ModelDeploymentRuntimeBridge | None = None
    scientific_batches: ScientificBatchService | None = None
    scientific_batch_worker: ScientificBatchWorker | None = None
    scientific_batch_cluster: HttpScientificBatchCluster | None = None
    scientific_repository: PostgresScientificBatchRepository | None = None
    scientific_capabilities: ScientificWorkloadCapabilityAuthority | None = None
    artifact_content_reader: SignedArtifactContentReader | None = None
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
    if settings.scientific_batch_enabled:
        if artifact_service is None:
            raise RuntimeError("scientific batch requires the canonical artifact service")
        if settings.scientific_batch_scheduling_contract_sha256 is None:
            raise RuntimeError("scientific batch requires the Terraform scheduling-contract digest")
        scientific_profiles = ScientificProfileCatalog.load(settings.catalog_dir)
        if not scientific_profiles.list():
            raise RuntimeError("scientific batch is enabled without a runnable qualified profile")
        scientific_repository = PostgresScientificBatchRepository(store.pool)
        scientific_capabilities = ScientificWorkloadCapabilityAuthority(store.hasher)
        artifact_content_reader = SignedArtifactContentReader(artifact_service)
        scientific_renderer = FileScientificManifestRenderer(
            path=settings.scientific_batch_execution_map_file,
            profiles=scientific_profiles,
            tools_image=settings.scientific_batch_tools_image,
            internal_api_url=settings.scientific_batch_internal_api_url,
            capability_authority=scientific_capabilities,
            academic_tenant_id=settings.scientific_batch_academic_tenant_id,
            academic_authorization_receipt_sha256=(settings.scientific_batch_academic_authorization_receipt_sha256),
        )
        scientific_batch_cluster = HttpScientificBatchCluster(
            base_url=settings.scientific_batch_kubernetes_api_url,
            token_file=settings.scientific_batch_kubernetes_token_file,
            ca_file=settings.scientific_batch_kubernetes_ca_file,
            renderer=scientific_renderer,
            fence=scientific_repository,
            controller_id=settings.scientific_batch_controller_id or "scientific-batch-controller",
            writes_enabled=settings.scientific_batch_writes_enabled,
            timeout_seconds=settings.scientific_batch_api_timeout_seconds,
        )
        scientific_artifact_bridge = ArtifactServiceBridge(
            artifacts=artifact_repository,
            batches=scientific_repository,
            profiles=scientific_profiles,
            store=store,
            content_reader=artifact_content_reader,
            service=artifact_service,
        )
        scientific_controller = ScientificBatchController(
            repository=scientific_repository,
            cluster=scientific_batch_cluster,
            controller_id=settings.scientific_batch_controller_id or "scientific-batch-controller",
            namespace=settings.scientific_batch_namespace,
            result_publisher=scientific_artifact_bridge,
            artifact_lifecycle=scientific_artifact_bridge,
            lease_seconds=settings.scientific_batch_lease_seconds,
        )
        scientific_batches = ScientificBatchService(
            store=store,
            repository=scientific_repository,
            controller=scientific_controller,
            profiles=scientific_profiles,
            scheduling=SchedulingContractResolver.load(
                settings.scientific_batch_scheduling_contract_file,
                expected_sha256=settings.scientific_batch_scheduling_contract_sha256,
            ),
            artifacts=scientific_artifact_bridge,
            execution_binding=scientific_renderer,
            plan_factory=scientific_renderer,
        )
        scientific_batch_worker = ScientificBatchWorker(
            scientific_controller,
            workers=settings.scientific_batch_workers,
            poll_seconds=settings.scientific_batch_poll_seconds,
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
        lifecycle=lifecycle,
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
    # The academic readiness projection is generated into the delivered catalog, so
    # the operator endpoint reports observed state instead of falling back to the
    # unavailable adapter. It fails closed on its own if the projection is absent.
    academic_assets = CatalogAcademicAssetAdminAdapter(settings.catalog_dir)
    admin_read = AdminReadService(
        registry=registry,
        store=store,
        kubernetes=kubernetes,
        prometheus=prometheus,
        capacity=capacity,
        observability=observability,
        academic_assets=academic_assets,
        contexts=contexts,
        source_max_age_seconds=settings.admin_source_max_age_seconds,
        adapter_timeout_seconds=settings.admin_adapter_timeout_seconds,
    )
    scientific_admin = postgres_scientific_admin_read_service(
        pool=store.pool,
        registry=registry,
        catalog_dir=settings.catalog_dir,
        artifact_service=artifact_service,
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
        lifecycle=lifecycle,
        route_revalidator=route_revalidator,
        admin_read=admin_read,
        scientific_admin=scientific_admin,
        configuration=configuration_service,
        model_deployment_preview=model_deployment_preview,
        model_deployment_read=model_deployment_read,
        model_deployment_mutation=model_deployment_mutation,
        model_deployment_bridge=model_deployment_bridge,
        scientific_batches=scientific_batches,
        scientific_batch_worker=scientific_batch_worker,
        scientific_batch_cluster=scientific_batch_cluster,
        artifact_service=artifact_service,
        scientific_workload_capabilities=scientific_capabilities,
        scientific_workload_batches=scientific_repository,
        scientific_artifact_content_reader=artifact_content_reader,
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
            "scientific-materialize",
            "scientific-collect",
            "scientific-prepare-workspace",
            "scientific-verify-runtime-artifacts",
        ),
        nargs="?",
        default="serve",
    )
    parser.add_argument("--logical-artifact-id")
    parser.add_argument("--artifact-id")
    parser.add_argument("--destination")
    parser.add_argument("--mode", choices=tuple(item.value for item in MaterializationMode))
    parser.add_argument("--compression")
    parser.add_argument("--yaml-name")
    parser.add_argument("--reuse-prefix")
    parser.add_argument("--expected-digest")
    parser.add_argument("--expected-size-bytes", type=int)
    parser.add_argument("--expected-media-type")
    parser.add_argument("--collector-id")
    parser.add_argument("--workspace")
    parser.add_argument("--logical-output-id")
    parser.add_argument("--validator-id")
    parser.add_argument("--max-artifacts", type=int)
    parser.add_argument("--max-output-bytes", type=int)
    parser.add_argument("--collection-deadline-seconds", type=int)
    args = parser.parse_args()
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "scientific-prepare-workspace":
        if not args.workspace:
            parser.error("scientific workspace is required")
        runtime_localization_json = os.environ.get("FS2_RUNTIME_ARTIFACTS_JSON")
        stage_invocation_json = os.environ.get("FS2_STAGE_INVOCATION_JSON")
        if not runtime_localization_json or not stage_invocation_json:
            parser.error("scientific runtime localization marker and stage invocation are required")
        prepare_workspace(
            Path(args.workspace),
            runtime_localization_json=runtime_localization_json,
            stage_invocation_json=stage_invocation_json,
        )
    elif args.command == "scientific-verify-runtime-artifacts":
        runtime_localization_json = os.environ.get("FS2_RUNTIME_ARTIFACTS_JSON")
        if not runtime_localization_json:
            parser.error("scientific runtime localization marker is required")
        verify_runtime_artifacts(runtime_localization_json=runtime_localization_json)
    elif args.command in {"scientific-materialize", "scientific-collect"}:
        api_url = os.environ.get("FS2_SCIENTIFIC_INTERNAL_API_URL")
        capability = os.environ.get("FS2_SCIENTIFIC_WORKLOAD_CAPABILITY")
        if not api_url or not capability:
            parser.error("scientific companion API and capability are required")
        client = WorkloadArtifactHttpClient(base_url=api_url, capability=capability)
        try:
            if args.command == "scientific-materialize":
                if (
                    not args.artifact_id
                    or not args.destination
                    or not args.mode
                    or not args.expected_digest
                    or args.expected_size_bytes is None
                    or not args.expected_media_type
                ):
                    parser.error("scientific materialization identity, destination, and mode are required")
                materialize_artifact(
                    client=client,
                    artifact_id=UUID(args.artifact_id),
                    destination=Path(args.destination),
                    mode=MaterializationMode(args.mode),
                    compression=args.compression,
                    yaml_name=args.yaml_name,
                    reuse_prefix=args.reuse_prefix,
                    expected_digest=args.expected_digest,
                    expected_size_bytes=args.expected_size_bytes,
                    expected_media_type=args.expected_media_type,
                )
            else:
                invocation = os.environ.get("FS2_STAGE_INVOCATION_JSON")
                if (
                    not args.collector_id
                    or not args.validator_id
                    or not args.workspace
                    or not invocation
                    or args.max_artifacts is None
                    or args.max_output_bytes is None
                    or args.collection_deadline_seconds is None
                ):
                    parser.error(
                        "scientific collector identity, workspace, invocation, and collection deadline are required"
                    )
                collect_and_commit(
                    client=client,
                    collector_id=args.collector_id,
                    validator_id=args.validator_id,
                    invocation_json=invocation,
                    workspace=Path(args.workspace),
                    catalog_dir=settings.catalog_dir,
                    collection_deadline_seconds=args.collection_deadline_seconds,
                    max_artifacts=args.max_artifacts,
                    max_output_bytes=args.max_output_bytes,
                )
        finally:
            client.client.close()
    elif args.command == "validate":
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
