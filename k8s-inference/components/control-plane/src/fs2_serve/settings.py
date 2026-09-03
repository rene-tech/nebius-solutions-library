"""Environment-backed runtime settings."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import SplitResult, urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import ModelId, Scope

_PUBLIC_HOST_MAX_LENGTH = 253
_PUBLIC_URL_MAX_LENGTH = 2048
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _default_migrations_dir() -> Path:
    """Resolve the one SQL migration set in source and installed wheels."""

    packaged = Path(__file__).resolve().with_name("migrations")
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "migrations"


def _validated_public_url(value: str, *, allow_http: bool) -> SplitResult:
    if not 1 <= len(value) <= _PUBLIC_URL_MAX_LENGTH:
        raise ValueError("public_base_url length is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("public_base_url is invalid") from exc
    if parsed.scheme not in {"http", "https"} or (parsed.scheme != "https" and not allow_http):
        raise ValueError("public_base_url must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("public_base_url must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("public_base_url must be an origin without path, query, or fragment")
    hostname = parsed.hostname
    if hostname is None or not 1 <= len(hostname) <= _PUBLIC_HOST_MAX_LENGTH:
        raise ValueError("public_base_url hostname length is invalid")
    if any(_DNS_LABEL.fullmatch(label) is None for label in hostname.split(".")):
        raise ValueError("public_base_url hostname is invalid")
    if port is not None and not 1 <= port <= 65535:  # pragma: no cover - urlsplit rejects this first
        raise ValueError("public_base_url port is invalid")
    return parsed


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FS2_", extra="ignore", allow_inf_nan=False)

    host: str = "0.0.0.0"  # noqa: S104
    port: int = Field(default=8080, ge=1, le=65535)
    database_url: str = "postgresql://fs2_serve@postgres/fs2_serve"
    catalog_dir: Path = Path("/etc/fs2-serve/catalog")
    bindings_file: Path = Path("/etc/fs2-serve/serving-bindings.json")
    variant_promotions_file: Path = Path("/etc/fs2-serve/bindings/model-variant-promotions.json")
    lean_routes_file: Path | None = None
    evidence_root: Path = Path("/etc/fs2-serve/evidence")
    federation_routes_file: Path = Path("/var/run/secrets/fs2-serve/federation/routes.json")
    federation_secret_dir: Path = Path("/var/run/secrets/fs2-serve/federation")
    repo_root: Path | None = None
    migrations_dir: Path = _default_migrations_dir()
    token_pepper_file: Path = Path("/var/run/secrets/fs2-serve/token-pepper")
    payload_keyring_file: Path = Path("/var/run/secrets/fs2-serve/payload-keyring.json")
    ledger_hmac_keyring_file: Path = Path("/var/run/secrets/fs2-serve/ledger-hmac-keyring.json")
    route_attestors_file: Path | None = Path("/var/run/secrets/fs2-serve/attestors/route-attestors.json")
    admin_token_file: Path = Path("/var/run/secrets/fs2-serve/admin-token")
    bootstrap_access_token_file: Path = Path("/var/run/secrets/fs2-serve/bootstrap-access-token")
    bootstrap_access_principal_id: str = Field(
        default="terraform-bootstrap-client",
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$",
    )
    bootstrap_access_tenant_id: str = Field(
        default="terraform-bootstrap",
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    bootstrap_access_name: str = Field(default="Terraform bootstrap MCP and inference", min_length=1, max_length=120)
    bootstrap_access_scopes: set[Scope] = Field(
        default_factory=lambda: {
            Scope.CATALOG_READ,
            Scope.INFERENCE_INVOKE,
            Scope.MCP_INVOKE,
            Scope.OPERATIONS_READ,
            Scope.OPERATIONS_RESULT,
            Scope.OPERATIONS_CANCEL,
            Scope.OPERATIONS_ACKNOWLEDGE,
            Scope.USE_NONCLINICAL,
            Scope.USE_NONCOMMERCIAL,
        },
        min_length=1,
    )
    bootstrap_access_models: set[ModelId] = Field(default_factory=lambda: {"*"}, min_length=1)
    bootstrap_access_max_concurrency: int = Field(default=32, ge=1, le=100)
    admin_capacity_enabled: bool = False
    admin_kubernetes_api_url: str = Field(default="https://kubernetes.default.svc", max_length=2048)
    admin_kubernetes_token_file: Path = Path("/var/run/secrets/fs2-serve/admin-kubernetes/token")
    admin_kubernetes_ca_file: Path = Path("/var/run/secrets/fs2-serve/admin-kubernetes/ca.crt")
    admin_kubernetes_model_namespace: str = Field(
        default="fs2-models",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$",
    )
    admin_kubernetes_system_namespace: str = Field(
        default="fs2-system",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$",
    )
    admin_kueue_api_version: Literal["v1beta1", "v1beta2"] = "v1beta2"
    admin_kubernetes_cache_ttl_seconds: float = Field(default=15, ge=1, le=60)
    admin_node_scaler_provider: Literal["nebius-managed-node-group-autoscaler"] | None = None
    admin_prometheus_url: str | None = Field(default=None, max_length=2048)
    admin_observability_config_file: Path | None = None
    admin_adapter_timeout_seconds: float = Field(default=2.0, ge=0.1, le=10)
    admin_source_max_age_seconds: float = Field(default=90.0, ge=1, le=3600)
    admin_context_project: str | None = Field(default=None, min_length=1, max_length=128)
    admin_context_cluster: str | None = Field(default=None, min_length=1, max_length=128)
    admin_context_region: str | None = Field(default=None, min_length=1, max_length=64)
    admin_context_label: str | None = Field(default=None, min_length=1, max_length=200)
    admin_configuration_file: Path | None = None
    admin_configuration_receipt_file: Path | None = None
    # Experimental dynamic-model controller. Both gates are false by default;
    # the independent writer gate prevents an accidentally started process
    # from acquiring a Lease or mutating Kubernetes.
    model_controller_enabled: bool = False
    model_controller_writes_enabled: bool = False
    model_controller_namespace: str = Field(
        default="fs2-models",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$",
    )
    model_controller_system_namespace: str = Field(
        default="fs2-system",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$",
    )
    model_controller_lease_name: str = Field(
        default="fs2-model-controller",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$",
    )
    model_controller_holder_identity: str | None = Field(default=None, min_length=1, max_length=253)
    model_controller_api_url: str = Field(default="https://kubernetes.default.svc", min_length=1, max_length=2048)
    model_controller_token_file: Path = Path("/var/run/secrets/fs2-model-controller/token")
    model_controller_ca_file: Path = Path("/var/run/secrets/fs2-model-controller/ca.crt")
    model_controller_envelope_file: Path = Path("/etc/fs2-model-controller/infrastructure-envelope.json")
    model_controller_bundles_file: Path = Path("/etc/fs2-model-controller/renderer-bundles.json")
    model_controller_prometheus_server_address: str = Field(
        default="http://fs2-monitoring-prometheus.fs2-observability.svc:9090",
        min_length=1,
        max_length=2048,
    )
    model_controller_lease_duration_seconds: int = Field(default=15, ge=5, le=120)
    model_controller_poll_seconds: float = Field(default=5, ge=0.5, le=60)
    model_controller_queue_capacity: int = Field(default=256, ge=1, le=10000)
    model_controller_workers: int = Field(default=2, ge=1, le=16)
    model_controller_api_timeout_seconds: float = Field(default=5, ge=0.5, le=30)
    model_controller_health_port: int = Field(default=8081, ge=1024, le=65535)
    scientific_batch_enabled: bool = False
    scientific_batch_writes_enabled: bool = False
    scientific_batch_namespace: str = Field(
        default="fs2-models",
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$",
    )
    scientific_batch_controller_id: str | None = Field(default=None, min_length=1, max_length=253)
    scientific_batch_kubernetes_api_url: str = Field(
        default="https://kubernetes.default.svc", min_length=1, max_length=2048
    )
    scientific_batch_kubernetes_token_file: Path = Path("/var/run/secrets/fs2-scientific-batch/token")
    scientific_batch_kubernetes_ca_file: Path = Path("/var/run/secrets/fs2-scientific-batch/ca.crt")
    scientific_batch_scheduling_contract_file: Path = Path("/etc/fs2-scientific-batch/kueue-scheduling.json")
    scientific_batch_execution_map_file: Path = Path("/etc/fs2-scientific-batch/execution-map.json")
    scientific_batch_tools_image: str | None = Field(default=None, max_length=1024)
    scientific_batch_internal_api_url: str = Field(
        default="http://fs2-serve-control-plane.default.svc:8080", min_length=1, max_length=2048
    )
    scientific_batch_workers: int = Field(default=2, ge=1, le=32)
    scientific_batch_poll_seconds: float = Field(default=0.25, ge=0.05, le=60)
    scientific_batch_lease_seconds: float = Field(default=30, ge=5, le=300)
    scientific_batch_api_timeout_seconds: float = Field(default=5, ge=0.5, le=30)
    public_base_url: str = Field(default="https://inference.example.invalid", min_length=1, max_length=2048)
    public_authority_mode: Literal["dns", "ip"] = "dns"
    authorization_server_url: str = "https://identity.example.invalid"
    max_request_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    max_response_bytes: int = Field(default=128 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    payload_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    scientific_artifacts_enabled: bool = False
    artifact_store_endpoint: str = Field(
        default="https://storage.eu-north1.nebius.cloud", min_length=8, max_length=2048
    )
    artifact_store_bucket: str = Field(
        default="fs2-scientific-artifacts",
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
    )
    artifact_store_region: str = Field(
        default="eu-north1", min_length=2, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    artifact_store_addressing_style: Literal["path", "virtual"] = "path"
    artifact_store_verify_tls: bool = True
    artifact_store_credentials_file: Path = Path("/var/run/secrets/fs2-serve/artifact-store/credentials.json")
    artifact_handle_ttl_seconds: int = Field(default=600, ge=30, le=900)
    artifact_max_bytes: int = Field(default=1 << 40, ge=1024, le=1 << 40)
    artifact_retention_seconds: int = Field(default=7776000, ge=86400, le=315360000)
    artifact_media_types: str = Field(
        default=(
            "application/octet-stream,application/json,application/gzip,"
            "application/vnd.fs2.scientific-manifest+json,"
            "application/vnd.fs2.scientific-validation+json,"
            "chemical/x-pdb,chemical/x-cif,text/plain"
        ),
        min_length=3,
        max_length=2048,
    )
    operation_retention_seconds: int = Field(default=604800, ge=3600, le=2592000)
    pat_retention_seconds: int = Field(default=604800, ge=3600, le=2592000)
    audit_retention_seconds: int = Field(default=2592000, ge=3600, le=31536000)
    usage_retention_seconds: int = Field(default=7776000, ge=86400, le=31536000)
    admin_session_ttl_seconds: int = Field(default=28800, ge=300, le=86400)
    reporting_database_role: str = Field(
        default="fs2_serve_reporting", min_length=1, max_length=63, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    runtime_database_role: str = Field(
        default="fs2_serve_runtime", min_length=1, max_length=63, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    maintenance_database_role: str = Field(
        default="fs2_serve_maintenance", min_length=1, max_length=63, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    activation_database_role: str = Field(
        default="fs2_serve_activation", min_length=1, max_length=63, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    sync_wait_seconds: float = Field(default=2.0, ge=0, le=30)
    max_sync_wait_seconds: float = Field(default=30.0, ge=0, le=120)
    max_sync_waiters: int = Field(default=32, ge=1, le=1024)
    wait_poll_initial_seconds: float = Field(default=0.05, ge=0.01, le=1)
    wait_poll_max_seconds: float = Field(default=0.5, ge=0.05, le=5)
    worker_concurrency: int = Field(default=4, ge=1, le=64)
    worker_poll_seconds: float = Field(default=0.25, ge=0.05, le=5)
    worker_lease_seconds: float = Field(default=30, ge=5, le=300)
    maintenance_interval_seconds: float = Field(default=5, ge=1, le=60)
    route_revalidation_interval_seconds: float = Field(default=15, ge=1, le=300)
    max_attempts: int = Field(default=3, ge=1, le=10)
    max_gpu_seconds_per_attempt: float = Field(default=3600, gt=0, le=86400)
    retry_base_seconds: float = Field(default=1, ge=0.1, le=60)
    runtime_timeout_seconds: float = Field(default=3600, ge=1, le=86400)
    activation_timeout_seconds: float = Field(default=1800, ge=1, le=7200)
    shutdown_grace_seconds: float = Field(default=30, ge=1, le=300)
    schema_wait_seconds: float = Field(default=300, ge=30, le=3600)
    otlp_endpoint: str | None = None
    log_level: str = "INFO"
    run_workers: bool = True
    allow_non_cluster_urls: bool = False

    @model_validator(mode="after")
    def validate_urls(self) -> Settings:
        if not self.database_url.startswith(("postgresql://", "postgresql+asyncpg://")):
            raise ValueError("database_url must be PostgreSQL")
        parsed = _validated_public_url(self.public_base_url, allow_http=self.allow_non_cluster_urls)
        hostname = parsed.hostname
        assert hostname is not None
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if self.public_authority_mode == "ip":
            if not isinstance(address, ipaddress.IPv4Address):
                raise ValueError("public_base_url must contain an exact IPv4 authority in ip mode")
            if parsed.scheme != "https" or parsed.port not in {None, 443}:
                raise ValueError("IP authority mode requires HTTPS on port 443")
        elif address is not None:
            raise ValueError("public_base_url must contain a DNS hostname in dns mode")
        if not self.authorization_server_url.startswith("https://") and not self.allow_non_cluster_urls:
            raise ValueError("authorization_server_url must use HTTPS")
        if self.sync_wait_seconds > self.max_sync_wait_seconds:
            raise ValueError("sync_wait_seconds cannot exceed max_sync_wait_seconds")
        if self.wait_poll_initial_seconds > self.wait_poll_max_seconds:
            raise ValueError("wait_poll_initial_seconds cannot exceed wait_poll_max_seconds")
        if self.max_sync_waiters < self.worker_concurrency:
            raise ValueError("max_sync_waiters cannot be lower than worker_concurrency")
        if self.federation_routes_file.parent != self.federation_secret_dir:
            raise ValueError("federation_routes_file must be directly inside federation_secret_dir")
        if self.scientific_artifacts_enabled:
            if not self.artifact_store_endpoint.startswith(("https://", "http://")):
                raise ValueError("artifact_store_endpoint must be an absolute HTTP(S) URL")
            if self.artifact_store_verify_tls and not self.artifact_store_endpoint.startswith("https://"):
                raise ValueError("artifact store TLS verification requires an https endpoint")
            if not self.allow_non_cluster_urls and not self.artifact_store_endpoint.startswith("https://"):
                raise ValueError("artifact_store_endpoint must use HTTPS")
            if not self.artifact_media_types_set():
                raise ValueError("artifact_media_types must list at least one exact media type")
        database_roles = {
            self.reporting_database_role,
            self.runtime_database_role,
            self.maintenance_database_role,
            self.activation_database_role,
        }
        if len(database_roles) != 4:
            raise ValueError("reporting, runtime, maintenance, and activation database roles must differ")
        context_identity = (self.admin_context_project, self.admin_context_cluster, self.admin_context_region)
        if any(value is not None for value in context_identity) and not all(
            value is not None for value in context_identity
        ):
            raise ValueError("admin context project, cluster, and region must be configured together")
        if self.admin_context_label is not None and not all(value is not None for value in context_identity):
            raise ValueError("admin context label requires a complete context identity")
        if self.admin_node_scaler_provider is not None and not self.admin_capacity_enabled:
            raise ValueError("admin node-scaler provider requires the capacity adapter")
        if self.model_controller_writes_enabled and not self.model_controller_enabled:
            raise ValueError("model controller writes require the controller feature gate")
        if self.model_controller_writes_enabled and not self.admin_capacity_enabled:
            raise ValueError("dynamic model writes require the projected Kubernetes admin adapter credential")
        if not self.model_controller_api_url.startswith("https://"):
            raise ValueError("model controller Kubernetes API URL must use HTTPS")
        if self.model_controller_workers > self.model_controller_queue_capacity:
            raise ValueError("model controller workers cannot exceed queue capacity")
        if self.scientific_batch_writes_enabled and not self.scientific_batch_enabled:
            raise ValueError("scientific batch writes require the controller feature gate")
        if self.scientific_batch_enabled and not self.scientific_batch_writes_enabled:
            raise ValueError("scientific batch API requires the independent Kubernetes write gate")
        if self.scientific_batch_enabled and self.scientific_batch_controller_id is None:
            raise ValueError("scientific batch controller identity is required when enabled")
        if self.scientific_batch_enabled and (
            self.scientific_batch_tools_image is None
            or re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", self.scientific_batch_tools_image) is None
        ):
            raise ValueError("scientific batch requires an immutable artifact companion image")
        internal_api = urlsplit(self.scientific_batch_internal_api_url)
        if self.scientific_batch_enabled and (
            internal_api.scheme != "http"
            or internal_api.hostname is None
            or (not internal_api.hostname.endswith(".svc") and not self.allow_non_cluster_urls)
            or internal_api.path not in {"", "/"}
            or internal_api.query
            or internal_api.fragment
        ):
            raise ValueError("scientific batch internal API URL must be an in-cluster HTTP origin")
        if not self.scientific_batch_kubernetes_api_url.startswith("https://"):
            raise ValueError("scientific batch Kubernetes API URL must use HTTPS")
        if self.scientific_batch_enabled and not self.scientific_artifacts_enabled:
            raise ValueError("scientific batch requires the canonical artifact service")
        required_bootstrap_scopes = {Scope.CATALOG_READ, Scope.INFERENCE_INVOKE, Scope.MCP_INVOKE}
        if not required_bootstrap_scopes.issubset(self.bootstrap_access_scopes):
            raise ValueError("bootstrap access requires catalog.read, inference.invoke, and mcp.invoke")
        return self

    def public_transport_allowlists(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return the one exact Host/Origin policy for every public protocol."""

        parsed = _validated_public_url(self.public_base_url, allow_http=self.allow_non_cluster_urls)
        hostname = parsed.hostname
        assert hostname is not None
        default_port = 443 if parsed.scheme == "https" else 80
        authorities: tuple[str, ...]
        if parsed.port is not None and parsed.port != default_port:
            authorities = (f"{hostname}:{parsed.port}",)
        else:
            authorities = (hostname, f"{hostname}:{default_port}")
        origins = tuple(f"{parsed.scheme}://{authority}" for authority in authorities)
        return authorities, origins

    def mcp_transport_allowlists(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Compatibility accessor; MCP shares the public edge policy exactly."""

        return self.public_transport_allowlists()

    def public_origin(self) -> str:
        """Return the canonical validated public origin used in advertisements."""

        return self.public_transport_allowlists()[1][0]

    @staticmethod
    def _read_secret(path: Path, *, minimum: int) -> bytes:
        value = path.read_bytes().strip()
        if len(value) < minimum:
            raise ValueError(f"secret at {path} must be at least {minimum} bytes")
        return value

    def admin_token(self) -> bytes:
        return self._read_secret(self.admin_token_file, minimum=32)

    def artifact_media_types_set(self) -> frozenset[str]:
        """Return the exact media-type allowlist accepted for scientific bytes."""

        return frozenset(item.strip().lower() for item in self.artifact_media_types.split(",") if item.strip())

    def artifact_store_credentials(self) -> tuple[str, str]:
        """Read the object-store key pair from its mounted secret, not from env."""

        raw = self._read_secret(self.artifact_store_credentials_file, minimum=8)
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise ValueError("artifact store credentials must be a JSON object") from exc
        if not isinstance(document, dict):
            raise ValueError("artifact store credentials must be a JSON object")
        access_key = document.get("access_key_id")
        secret_key = document.get("secret_access_key")
        if not isinstance(access_key, str) or not isinstance(secret_key, str):
            raise ValueError("artifact store credentials must name access_key_id and secret_access_key")
        if not access_key or not secret_key:
            raise ValueError("artifact store credentials must be non-empty")
        return access_key, secret_key

    def bootstrap_access_token(self) -> str:
        raw = self._read_secret(self.bootstrap_access_token_file, minimum=64)
        if len(raw) > 256:
            raise ValueError("bootstrap access token exceeds the bearer-token bound")
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("bootstrap access token must be ASCII") from exc

    def trusted_route_attestors(self) -> Mapping[str, str] | None:
        """Load the bounded public trust root used for signed route evidence."""

        if self.route_attestors_file is None:
            return None
        try:
            raw = self.route_attestors_file.read_bytes()
        except FileNotFoundError:
            return None
        if not raw or len(raw) > 64 * 1024:
            raise ValueError("route attestor key set is empty or too large")

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("route attestor key set contains a duplicate key ID")
                value[key] = item
            return value

        try:
            value = json.loads(raw, object_pairs_hook=reject_duplicates)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("route attestor key set is not valid JSON") from exc
        if not isinstance(value, dict) or not 1 <= len(value) <= 32:
            raise ValueError("route attestor key set must contain between 1 and 32 keys")
        for key_id, public_key in value.items():
            if re.fullmatch(r"sha256:[a-f0-9]{64}", key_id) is None:
                raise ValueError("route attestor key ID is invalid")
            if not isinstance(public_key, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", public_key) is None:
                raise ValueError("route attestor public key is invalid")
        return value
