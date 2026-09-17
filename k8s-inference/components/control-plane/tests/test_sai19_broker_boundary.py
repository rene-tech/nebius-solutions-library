"""Authored-only SAI-19 regressions for independent artifact custody.

The remediation task's coordinator boundary forbids executing tests. These
cases intentionally remain source evidence for the independent reviewer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from fs2_serve.artifact_authority import (
    ArtifactAuthorityClient,
    ArtifactAuthorityClientConfig,
    ArtifactAuthoritySigner,
    ArtifactAuthorityVerifier,
)
from fs2_serve.artifact_credential_broker import BrokeredS3ArtifactObjectStore
from fs2_serve.artifact_broker_server import ArtifactBrokerSettings, BrokerRequest, PostgresArtifactAuthorizer
from fs2_serve.artifact_version_backfill import load_backfill_manifest
from fs2_serve.auth import PepperRing, TenantBrokerNotReadyError, TokenService
from fs2_serve.models import Scope, TokenCreate
from fs2_serve.scientific_artifacts import (
    ArtifactDirection,
    ArtifactPolicyError,
    ArtifactVerificationError,
    ScientificArtifactService,
    artifact_storage_key,
)
from fs2_serve.scientific_object_store import ObjectStoreConfig, S3ArtifactObjectStore


ROOT = Path(__file__).resolve().parents[3]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _key(tenant_id: str, operation_id, attempt_id, digest: str = "a" * 64) -> str:
    return artifact_storage_key(
        tenant_id=tenant_id,
        operation_id=operation_id,
        stage_id="design",
        shard_id=None,
        attempt_id=attempt_id,
        direction=ArtifactDirection.OUTPUT,
        digest=f"sha256:{digest}",
    )


class _IssuedTokenStore:
    calls: list[dict[str, object]]

    def __init__(self) -> None:
        self.calls = []

    async def issue_token(self, **values: object):
        self.calls.append(values)
        request = values["request"]
        assert isinstance(request, TokenCreate)
        now = datetime.now(UTC)
        return {
            "id": values["token_id"],
            "prefix": values["prefix"],
            "pepper_key_id": values["pepper_key_id"],
            "principal_id": request.principal_id,
            "tenant_id": request.tenant_id,
            "scopes": sorted(str(scope) for scope in request.scopes),
            "models": sorted(request.models),
            "expires_at": request.expires_at,
            "request_budget": request.request_budget,
            "requests_used": 0,
            "gpu_seconds_budget": request.gpu_seconds_budget,
            "gpu_seconds_used": 0,
            "gpu_seconds_reserved": 0,
            "max_concurrency": request.max_concurrency,
            "created_at": now,
            "created_by": values["created_by"],
            "revoked_at": None,
            "name": request.name,
            "rate_limit_requests": request.rate_limit_requests,
            "rate_window_seconds": request.rate_window_seconds,
            "fingerprint": values["fingerprint"],
        }


@pytest.mark.asyncio
async def test_non_artifact_pat_does_not_require_static_broker_inventory() -> None:
    readiness_calls: list[str] = []

    async def unavailable(tenant_id: str) -> None:
        readiness_calls.append(tenant_id)
        raise RuntimeError("tenant is not in artifact broker inventory")

    store = _IssuedTokenStore()
    service = TokenService(
        store,  # type: ignore[arg-type]
        PepperRing(active_key_id="test", keys={"test": b"p" * 32}),
        tenant_readiness=unavailable,
    )
    await service.issue(
        TokenCreate(
            principal_id="new-inference-customer",
            tenant_id="tenant-not-provisioned-for-artifacts",
            scopes={Scope.CATALOG_READ, Scope.INFERENCE_INVOKE},
            models={"model-a"},
        ),
        created_by="operator",
    )

    assert readiness_calls == []
    assert len(store.calls) == 1


@pytest.mark.asyncio
async def test_artifact_upload_pat_requires_exact_tenant_broker_readiness() -> None:
    readiness_calls: list[str] = []

    async def unavailable(tenant_id: str) -> None:
        readiness_calls.append(tenant_id)
        raise RuntimeError("broker fleet is not ready")

    store = _IssuedTokenStore()
    service = TokenService(
        store,  # type: ignore[arg-type]
        PepperRing(active_key_id="test", keys={"test": b"p" * 32}),
        tenant_readiness=unavailable,
    )
    with pytest.raises(TenantBrokerNotReadyError):
        await service.issue(
            TokenCreate(
                principal_id="artifact-customer",
                tenant_id="tenant-a",
                scopes={Scope.ARTIFACTS_WRITE},
                models={"model-a"},
            ),
            created_by="operator",
        )

    assert readiness_calls == ["tenant-a"]
    assert store.calls == []


def test_gateway_client_has_no_capability_signing_material() -> None:
    config = ArtifactAuthorityClientConfig(
        url="https://fs2-artifact-authority.fs2-system.svc:8443/v1",
        audience="fs2-artifact-authority-issuer",
        token_file=Path("/projected/token"),
        ca_file=Path("/issuer/ca.crt"),
    )
    client = ArtifactAuthorityClient(config, client=object())  # type: ignore[arg-type]

    assert not hasattr(client, "_private_key")
    assert not hasattr(config, "signing_key")
    with pytest.raises(ValueError, match="exact in-cluster service"):
        ArtifactAuthorityClientConfig(
            url="https://attacker.invalid/v1",
            audience="fs2-artifact-authority-issuer",
            token_file=Path("/projected/token"),
            ca_file=Path("/issuer/ca.crt"),
        )


def test_existing_inference_upload_pat_remains_exact_broker_authority() -> None:
    broker = _read("components/control-plane/src/fs2_serve/artifact_broker_server.py")
    bootstrap = _read("stages/workloads/bootstrap_access.tf")
    pat_authorizer = broker[
        broker.index("    async def _pat_subject(") : broker.index("    async def _workload_subject(")
    ]

    assert '_SCIENTIFIC_UPLOAD_PROTOCOL: Final = "scientific-artifact-upload-v1"' in broker
    assert 'str(owner["protocol"]) == _SCIENTIFIC_UPLOAD_PROTOCOL' in pat_authorizer
    assert "required = Scope.INFERENCE_INVOKE" in pat_authorizer
    assert 'str(owner["model_id"]) not in models' in pat_authorizer
    assert "owner[\"token_id\"] != token_id" in pat_authorizer
    assert "Scope.ARTIFACTS_WRITE if request.action in _WRITE_ACTIONS" in pat_authorizer
    # Existing Terraform-issued scientific and bootstrap credentials retain
    # their public contract; no forced scope rotation is hidden in this fix.
    assert '"inference.invoke"' in bootstrap
    assert '"artifacts.write"' not in bootstrap


def test_broker_can_verify_but_cannot_mint_ed25519_authority() -> None:
    private = Ed25519PrivateKey.generate()
    signer = ArtifactAuthoritySigner(private, clock=lambda: datetime(2026, 9, 17, tzinfo=UTC))
    verifier = ArtifactAuthorityVerifier(
        private.public_key(), clock=lambda: datetime(2026, 9, 17, tzinfo=UTC)
    )
    operation_id = uuid4()
    attempt_id = uuid4()

    token = signer.issue_workload_scope(
        operation_id=operation_id,
        tenant_id="tenant-a",
        attempt_id=attempt_id,
        attempt_number=3,
        access="read",
        artifact_id=uuid4(),
    )
    verified = verifier.verify_workload(token)

    assert verified.operation_id == operation_id
    assert verified.attempt_id == attempt_id
    controller_artifact_id = uuid4()
    controller = verifier.verify_controller(
        signer.issue_controller_scope(
            operation_id=operation_id,
            tenant_id="tenant-a",
            controller_id="scientific-controller-1",
            fencing_token=9,
            artifact_id=controller_artifact_id,
        )
    )
    assert controller.controller_id == "scientific-controller-1"
    assert controller.fencing_token == 9
    assert controller.artifact_id == controller_artifact_id
    assert not hasattr(verifier, "_private_key")
    wrong = ArtifactAuthorityVerifier(
        Ed25519PrivateKey.generate().public_key(), clock=lambda: datetime(2026, 9, 17, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="invalid"):
        wrong.verify_workload(token)


def test_streaming_read_captures_authority_before_context_unwinds() -> None:
    source = _read("components/control-plane/src/fs2_serve/artifact_credential_broker.py")
    read = source[source.index("    def read("):source.index("    async def close", source.index("    def read("))]

    assert read.index('headers = self._headers("read")') < read.index("async def chunks()")
    assert "headers=headers" in read


def test_background_materialization_uses_its_declared_ceiling_not_the_public_inline_ceiling() -> None:
    materializer = _read("components/control-plane/src/fs2_serve/artifact_inputs.py")
    service = _read("components/control-plane/src/fs2_serve/scientific_artifacts.py")
    cli = _read("components/control-plane/src/fs2_serve/cli.py")

    assert "open_verified_content(" in materializer
    assert "max_content_bytes=rule.max_bytes" in materializer
    assert "max_content_bytes=self._max_inline_content_bytes" in service
    assert "if record.size_bytes > max_content_bytes" in service
    assert "ArtifactInputMaterializer(artifact_service, authority=artifact_authorities)" in cli
    assert "ServingOutputArtifactizer(artifact_service, authority=artifact_authorities)" in cli
    assert "artifact_authorities=artifact_authorities" in cli


def test_artifact_disabled_runtime_does_not_construct_unused_authority_transport() -> None:
    cli = _read("components/control-plane/src/fs2_serve/cli.py")
    gate_start = cli.index("    artifact_broker: ArtifactCredentialBroker | None = None")
    gate_end = cli.index("    artifact_service = _artifact_service", gate_start)
    gated = cli[gate_start:gate_end]

    assert "artifact_authorities: ArtifactAuthorityClient | None = None" in gated
    assert "if settings.scientific_artifacts_enabled:" in gated
    assert gated.index("if settings.scientific_artifacts_enabled:") < gated.index(
        "artifact_authorities = ArtifactAuthorityClient("
    )
    assert "ArtifactAuthorityClient(" not in cli[:gate_start]
    assert "ArtifactAuthorityClient(" not in cli[gate_end:]
    assert 'raise RuntimeError("scientific artifact authority issuer is unavailable")' in cli


def test_broker_uses_bounded_operation_timeout_for_metadata_upload_and_stream_paths() -> None:
    source = _read("components/control-plane/src/fs2_serve/artifact_credential_broker.py")
    constructor = source[
        source.index("class ArtifactCredentialBroker:") : source.index(
            "    def _workload_token", source.index("class ArtifactCredentialBroker:")
        )
    ]
    operation = source[source.index("    async def operation(") : source.index("    async def put(")]
    put = source[source.index("    async def put(") : source.index("    def read(")]
    read = source[source.index("    def read(") : source.index("    async def close(")]

    assert "connect=config.timeout_seconds" in constructor
    assert "read=float(config.operation_timeout_seconds)" in constructor
    assert "write=float(config.operation_timeout_seconds)" in constructor
    assert "pool=config.timeout_seconds" in constructor
    assert "timeout=self._operation_timeout" in operation
    assert "timeout=self._operation_timeout" in put
    assert "timeout=self._operation_timeout" in read


def test_broker_readiness_and_maintenance_clients_bind_provider_generation() -> None:
    server = _read("components/control-plane/src/fs2_serve/artifact_broker_server.py")
    store = _read("components/control-plane/src/fs2_serve/scientific_object_store.py")
    workload = _read("stages/workloads/scientific_artifacts.tf")
    backfill = _read(
        "charts/control-plane/fs2-serve-control-plane/templates/artifact-version-backfill-job.yaml"
    )

    assert "await provider.assert_tenant_ready(record.tenant_id)" in server
    assert 'listing.get("Prefix") != prefix' in store
    assert 'path   = "/readyz"' in workload
    assert "FS2_ARTIFACT_BACKFILL_BROKER_READINESS_BINDINGS_FILE" in backfill
    assert "broker-readiness" in backfill


def test_terminal_batch_reads_use_an_active_durable_controller_lease() -> None:
    bridge = _read("components/control-plane/src/fs2_serve/scientific_batch/artifact_bridge.py")
    issuer = _read("components/control-plane/src/fs2_serve/artifact_authority_server.py")
    broker = _read("components/control-plane/src/fs2_serve/artifact_broker_server.py")
    grants = _read("components/control-plane/src/fs2_serve/postgres.py")

    assert "issue_controller_read(" in bridge
    assert "operation_id=state.operation_id" in bridge
    assert 'FROM fs2_scientific_batches WHERE operation_id=$1 AND tenant_id=$2' in issuer
    assert 'FROM fs2_scientific_batches WHERE operation_id=$1 AND tenant_id=$2' in broker
    assert 'authority_token.startswith("fs2_artifact_controller.")' in broker
    assert grants.count(
        "GRANT SELECT (operation_id,tenant_id,controller_id,fencing_token,lease_expires_at)"
    ) == 2


def test_executor_write_and_read_both_require_independent_operation_admission() -> None:
    issuer = _read("components/control-plane/src/fs2_serve/artifact_authority_server.py")
    endpoint = issuer[
        issuer.index('    @app.post("/v1/executor"') : issuer.index(
            '    @app.post("/v1/operator"'
        )
    ]

    admission_gate = endpoint.index("if not await _operation_admission_is_bound(")
    access_branch = endpoint.index('if request.access == "read":')
    issue = endpoint.index("token=signer.issue_executor_scope(")
    assert admission_gate < access_branch < issue
    assert "executor operation lacks an active admission authority" in endpoint


def test_provider_endpoint_is_exact_official_region_not_operator_supplied_sts() -> None:
    common = {
        "database_url": "postgresql://broker.invalid/db",
        "token_pepper_file": Path("/keys/pepper.json"),
        "allowed_tenant_id": "tenant-a",
        "provider_bucket": "scientific-artifacts",
        "provider_region": "eu-north1",
        "provider_access_key_file": Path("/keys/access"),
        "provider_secret_key_file": Path("/keys/secret"),
        "authority_verification_key_file": Path("/keys/public.pem"),
        "tls_certificate_file": Path("/tls/tls.crt"),
        "tls_private_key_file": Path("/tls/tls.key"),
    }
    with pytest.raises(ValueError, match="official regional Nebius endpoint"):
        ArtifactBrokerSettings(
            **common,
            provider_endpoint_url="https://attacker.invalid",
        )
    settings = ArtifactBrokerSettings(
        **common,
        provider_endpoint_url="https://storage.eu-north1.nebius.cloud",
    )
    assert settings.provider_endpoint_url == "https://storage.eu-north1.nebius.cloud"


class _MismatchPool:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *args: object):
        self.queries.append((query, args))
        if "FROM fs2_scientific_artifacts" in query:
            return self.row
        raise AssertionError("authorization continued after the authoritative row mismatch")


@pytest.mark.asyncio
async def test_broker_rejects_tenant_and_key_dispatch_before_provider_access() -> None:
    operation_id = uuid4()
    attempt_id = uuid4()
    victim_key = _key("victim", operation_id, attempt_id)
    pool = _MismatchPool(
        {
            "id": uuid4(),
            "tenant_id": "victim",
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "digest": "sha256:" + "a" * 64,
            "size_bytes": 7,
            "media_type": "text/plain",
            "compression": None,
            "storage_key": victim_key,
            "object_version_id": "version-victim",
            "created_at": datetime.now(UTC),
            "retention_expires_at": datetime.now(UTC) + timedelta(days=1),
        }
    )
    authorizer = object.__new__(PostgresArtifactAuthorizer)
    authorizer._pool = pool

    with pytest.raises(HTTPException) as failure:
        await authorizer.authorize(
            request=BrokerRequest(
                audience="fs2-artifact-credential-broker",
                tenant_id="attacker",
                storage_key=victim_key,
                action="read",
                object_version_id="version-victim",
                minimum_ttl_seconds=120,
                expected_size_bytes=7,
                expected_media_type="text/plain",
            ),
            caller_subject="system:serviceaccount:fs2-system:fs2-serve-control-plane-runtime",
            authority_token="fs2_pat_untrusted-echo",
            maintenance_subject="system:serviceaccount:fs2-system:fs2-serve-control-plane-maintenance",
        )

    assert failure.value.detail == "artifact request differs from authoritative storage scope"
    assert len(pool.queries) == 1


def test_terraform_separates_private_signing_and_public_verification_custody() -> None:
    workloads = _read("stages/workloads/scientific_artifacts.tf")
    gateway = _read("charts/control-plane/fs2-serve-control-plane/templates/_helpers.tpl")
    infrastructure = _read("stages/infrastructure/scientific_artifacts.tf")
    cli = _read("components/control-plane/src/fs2_serve/cli.py")
    api = _read("components/control-plane/src/fs2_serve/api.py")
    issuer_source = _read("components/control-plane/src/fs2_serve/artifact_authority_server.py")
    broker_source = _read("components/control-plane/src/fs2_serve/artifact_broker_server.py")

    issuer_start = workloads.index('resource "kubernetes_deployment_v1" "scientific_artifact_authority"')
    broker_start = workloads.index('resource "kubernetes_deployment_v1" "scientific_artifact_broker"')
    issuer = workloads[issuer_start:broker_start]
    brokers = workloads[broker_start:]
    assert "FS2_ARTIFACT_AUTHORITY_SIGNING_KEY_FILE" in issuer
    assert "authority_signing_secret_name" in issuer
    assert "FS2_ARTIFACT_BROKER_AUTHORITY_VERIFICATION_KEY_FILE" in brokers
    assert "authority_signing_secret_name" not in brokers
    assert "AUTHORITY_SIGNING" not in gateway
    assert "ArtifactAuthoritySigner" not in cli
    assert "ArtifactAuthoritySigner" not in api
    assert "ArtifactAuthoritySigner" not in broker_source
    assert "ArtifactAuthoritySigner.from_file(settings.signing_key_file)" in issuer_source
    assert "ArtifactAuthorityVerifier.from_file(settings.authority_verification_key_file)" in broker_source
    assert workloads.count("FS2_ARTIFACT_AUTHORITY_SIGNING_KEY_FILE") == 1
    assert "ledger-hmac-keyring" in gateway
    assert "ledger-hmac-keyring" not in issuer
    assert "ledger-hmac-keyring" not in brokers
    assert 'resource "nebius_iam_v2_access_key" "scientific_artifact_tenant"' in infrastructure
    assert "scientific/v1/tenants/${tenant_id}/*" in infrastructure
    assert "retained-no-object-authorization-pending-reviewed-retirement" in _read(
        "stages/infrastructure/outputs.tf"
    )


def test_public_upload_contract_names_the_provider_version_response_header() -> None:
    upload = _read("components/control-plane/src/fs2_serve/scientific_input_uploads.py")
    api = _read("components/control-plane/src/fs2_serve/api.py")
    mcp = _read("components/control-plane/src/fs2_serve/mcp_server.py")

    assert 'version_response_header: Literal["x-amz-version-id"]' in upload
    assert 'version_response_header="x-amz-version-id"' in upload
    assert '"x-fs2-object-version-id": receipt.object_version_id' in api
    assert "provider VersionId returned by the" in mcp


def test_public_input_upload_persists_an_independently_signed_operation_root() -> None:
    upload = _read("components/control-plane/src/fs2_serve/scientific_input_uploads.py")
    cli = _read("components/control-plane/src/fs2_serve/cli.py")

    assert "artifact_authorities: ArtifactAuthorityClient" in upload
    assert "current_artifact_authority()" in upload
    assert "await self.artifact_authorities.authorize_admission_inputs(" in upload
    assert 'required_scope=str(Scope.INFERENCE_INVOKE)' in upload
    assert '"authority_operation_id": authority_operation_id' in upload
    assert '"operation_admission_authority": operation_admission_authority' in upload
    assert '"operation_required_scope": str(Scope.INFERENCE_INVOKE)' in upload
    assert "artifact_authorities=artifact_authorities" in cli


def test_stack_destroy_preserves_every_protected_artifact_identity_mode() -> None:
    facade = _read("inference-stack")
    lifecycle = _read("stages/infrastructure/outputs.tf")

    assert "scientific_identities_protected" in facade
    assert "retained_mode = reference_retained or scientific_identities_protected" in facade
    assert 'destroy_status = "blocked-protected-artifact-identities"' in lifecycle
    assert "full-stack-destroy-incomplete-protected-artifact-identities" in lifecycle


def test_backfill_manifest_requires_explicit_unique_versions(tmp_path: Path) -> None:
    artifact_id = uuid4()
    manifest = tmp_path / "backfill.json"
    document = {
        "schema": "fs2-serve.nebius.ai/artifact-version-backfill/v1",
        "candidates": [{"artifact_id": str(artifact_id), "object_version_id": "version-42"}],
    }
    manifest.write_text(json.dumps(document), encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert load_backfill_manifest(manifest, expected_sha256=digest)[0].object_version_id == "version-42"

    document["candidates"].append(
        {"artifact_id": str(artifact_id), "object_version_id": "version-43"}
    )
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="repeats"):
        load_backfill_manifest(
            manifest,
            expected_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        )


def test_backfill_is_one_way_receipted_and_purge_fenced() -> None:
    migration = _read("components/control-plane/migrations/0031_scientific_artifact_version_backfill.sql")
    service = _read("components/control-plane/src/fs2_serve/scientific_artifacts.py")

    assert "fs2_scientific_artifact_version_backfills" in migration
    assert "OLD.object_version_id IS NULL" in migration
    assert "NEW.object_version_id IS NOT NULL" in migration
    assert "provider_receipt_digest" in migration
    assert "retention purge is blocked by an unbound provider object version" in service
    assert "fs2_scientific_abandoned_upload_receipts" in service
    assert "retention purge is blocked by an unresolved upload" in service
    claim = service[service.index("async def claim_expired"):service.index("async def purge_keys")]
    assert claim.index("a.object_version_id IS NULL") < claim.index("ORDER BY r.retention_expires_at")
    assert claim.index("receipt.upload_id IS NULL") < claim.index("ORDER BY r.retention_expires_at")


def test_provider_cleanup_cannot_block_mandatory_retention() -> None:
    cli = _read("components/control-plane/src/fs2_serve/cli.py")
    retention = cli[cli.index("async def maintain(") : cli.index("async def migrate(")]
    maintenance = _read(
        "charts/control-plane/fs2-serve-control-plane/templates/maintenance-cronjob.yaml"
    )
    orphan = _read(
        "charts/control-plane/fs2-serve-control-plane/templates/artifact-orphan-cleanup-cronjob.yaml"
    )

    assert "cleanup_abandoned_uploads" not in retention.split(
        "async def cleanup_artifact_orphans", 1
    )[0]
    assert 'args: ["maintenance"]' in maintenance
    assert "artifact-credential-broker" not in maintenance
    assert "$artifactOrphanCleanupActivated := false" in orphan
    assert "and $artifactOrphanCleanupActivated" in orphan
    assert 'args: ["artifact-orphan-cleanup"]' in orphan
    assert "activeDeadlineSeconds: 240" in orphan


def test_provider_delete_is_maintenance_only_and_retention_gated() -> None:
    broker = _read("components/control-plane/src/fs2_serve/artifact_broker_server.py")

    assert "if caller_subject == maintenance_subject:" in broker
    assert "record.retention_expires_at > datetime.now(UTC)" in broker
    assert "artifact deletion requires expired-retention maintenance authority" in broker


def test_public_finalize_proves_provider_version_before_irreversible_binding() -> None:
    service = _read("components/control-plane/src/fs2_serve/scientific_artifacts.py")
    broker = _read("components/control-plane/src/fs2_serve/artifact_broker_server.py")
    finalize = service[
        service.index("    async def finalize_upload(self, request: FinalizeArtifactUpload)") :
        service.index("    async def download(", service.index("    async def finalize_upload(self, request"))
    ]

    assert finalize.index("verified = await self._store.inspect_upload(") < finalize.index(
        "return await self._repository.finalize_upload("
    )
    assert "register_upload_version" not in finalize
    assert "record.object_version_id is not None" in broker
    assert "request.object_version_id != record.object_version_id" in broker


def test_backfill_has_exact_network_identity_and_exact_retry_proof() -> None:
    job = _read(
        "charts/control-plane/fs2-serve-control-plane/templates/artifact-version-backfill-job.yaml"
    )
    policy = _read("charts/control-plane/fs2-serve-control-plane/templates/networkpolicy.yaml")
    backfill = _read("components/control-plane/src/fs2_serve/artifact_version_backfill.py")

    assert "fs2-serve.artifactVersionBackfillSelectorLabels" in job
    assert "fs2-serve-artifact-version-backfill" in policy
    assert "app.kubernetes.io/component: artifact-version-backfill" in policy
    assert "backfill.provider_receipt_digest AS backfill_provider_receipt_digest" in backfill
    assert "if exact_replay:" in backfill


@pytest.mark.asyncio
async def test_exact_version_and_media_metadata_are_rejected_before_body_read() -> None:
    class Body:
        reads = 0
        closed = False

        def read(self, _: int) -> bytes:
            self.reads += 1
            return b"corrupt"

        def close(self) -> None:
            self.closed = True

    class Client:
        def __init__(self, body: Body) -> None:
            self.body = body

        def get_object(self, **_: object) -> dict[str, object]:
            return {
                "Body": self.body,
                "VersionId": "wrong-version",
                "ContentLength": 7,
                "ContentType": "text/plain",
            }

    body = Body()
    store = object.__new__(S3ArtifactObjectStore)
    store._config = ObjectStoreConfig(
        endpoint_url="https://storage.eu-north1.nebius.cloud",
        bucket="scientific-artifacts",
        region="eu-north1",
        access_key="isolated-tenant-key",
        secret_key="isolated-tenant-secret",
        max_stream_bytes=1024,
    )
    store._client = Client(body)

    with pytest.raises(ArtifactVerificationError, match="version"):
        [
            chunk
            async for chunk in store.stream_object(
                tenant_id="tenant-a",
                storage_key=_key("tenant-a", uuid4(), uuid4()),
                object_version_id="expected-version",
                expected_size_bytes=7,
                expected_media_type="text/plain",
                expected_compression=None,
            )
        ]

    assert body.reads == 0
    assert body.closed is True


@pytest.mark.asyncio
async def test_one_null_historical_version_fences_the_entire_purge() -> None:
    operation_id = uuid4()

    class Repository:
        purge_called = False

        async def claim_expired(self, **_: object):
            return [(operation_id, "tenant-a", datetime.now(UTC) - timedelta(days=1))]

        async def purge_keys(self, *_: object, **__: object):
            return [("versioned-key", "version-1"), ("historical-key", None)]

        async def purge_operation(self, *_: object, **__: object):
            self.purge_called = True
            raise AssertionError("metadata purge must remain fenced")

    class Store:
        deletes: list[tuple[str, str]] = []

        async def delete(self, **kwargs: str) -> None:
            self.deletes.append((kwargs["storage_key"], kwargs["object_version_id"]))

    repository = Repository()
    store = Store()
    service = ScientificArtifactService(
        repository=repository,  # type: ignore[arg-type]
        object_store=store,  # type: ignore[arg-type]
        allowed_media_types={"application/json"},
    )

    assert await service.purge_expired() == []
    assert store.deletes == []
    assert repository.purge_called is False


def test_upload_version_custody_survives_operation_metadata_retention() -> None:
    migration = _read(
        "components/control-plane/migrations/0031_scientific_artifact_version_backfill.sql"
    )
    repository = _read("components/control-plane/src/fs2_serve/scientific_artifacts.py")
    table = migration[
        migration.index("CREATE TABLE fs2_scientific_upload_object_versions") : migration.index(
            "CREATE TABLE fs2_scientific_artifact_version_backfills"
        )
    ]
    postgres_repository = repository[repository.index("class PostgresArtifactRepository") :]
    purge_keys = postgres_repository[
        postgres_repository.index("    async def purge_keys(") : postgres_repository.index(
            "    async def purge_operation("
        )
    ]

    assert "REFERENCES fs2_scientific_uploads" not in table
    assert "fs2_validate_scientific_upload_object_version" in table
    assert "fs2_scientific_upload_object_versions_scope" in table
    assert "fs2_scientific_upload_object_versions" not in purge_keys
    assert "SELECT storage_key,object_version_id\n            FROM fs2_scientific_artifacts" in purge_keys


def test_put_and_finalize_inspect_are_distinct_authorized_operations() -> None:
    broker = _read("components/control-plane/src/fs2_serve/artifact_broker_server.py")
    service = _read("components/control-plane/src/fs2_serve/scientific_artifacts.py")
    object_store = _read("components/control-plane/src/fs2_serve/scientific_object_store.py")

    put_handler = broker[broker.index("async def artifact_put"):broker.index("async def artifact_read")]
    assert "provider.put_version(" in put_handler
    assert "provider.put_object(" not in put_handler
    assert "provider.inspect(" not in put_handler
    assert 'request.action == "finalize-inspect"' in broker
    assert "register_upload_version(request)" in service
    assert object_store.count('"IfNoneMatch": "*"') >= 2
    assert object_store.count('"ChecksumSHA256": self._checksum(storage_key)') >= 2


@pytest.mark.asyncio
async def test_generic_inspect_refuses_a_versionless_latest_object() -> None:
    store = BrokeredS3ArtifactObjectStore(object())  # type: ignore[arg-type]

    with pytest.raises(ArtifactPolicyError, match="exact immutable provider version"):
        await store.inspect(
            tenant_id="tenant-a",
            storage_key=_key("tenant-a", uuid4(), uuid4()),
            object_version_id=None,
            max_bytes=1024,
        )


def test_abandoned_upload_cleanup_is_dormant_until_transfer_quiescence_is_proven() -> None:
    migration = _read("components/control-plane/migrations/0031_scientific_artifact_version_backfill.sql")
    cleanup = _read("components/control-plane/src/fs2_serve/artifact_orphan_cleanup.py")
    broker = _read("components/control-plane/src/fs2_serve/artifact_broker_server.py")
    chart = _read(
        "charts/control-plane/fs2-serve-control-plane/templates/artifact-orphan-cleanup-cronjob.yaml"
    )

    assert "fs2_scientific_abandoned_upload_claims" in migration
    assert "FOR UPDATE OF upload SKIP LOCKED" in migration
    assert "requested_cutoff > clock_timestamp()-interval '1 hour'" in migration
    assert "ABANDONED_UPLOAD_CLEANUP_ACTIVATED = False" in cleanup
    assert "if not ABANDONED_UPLOAD_CLEANUP_ACTIVATED:\n        return 0" in cleanup
    assert 'action="list-upload-version"' in cleanup
    assert 'action="delete"' not in cleanup
    assert "Provider bytes are\n                        # intentionally retained" in cleanup
    assert 'outcome = "object-absent"' not in cleanup
    assert "absence never releases the purge fence" in migration
    assert "record.cleanup_claimed" in broker
    assert "$artifactOrphanCleanupActivated := false" in chart
    assert "serviceAccountToken" in chart


def test_provider_version_discovery_refuses_ambiguous_history_without_reading_bytes() -> None:
    object_store = _read("components/control-plane/src/fs2_serve/scientific_object_store.py")
    method = object_store[
        object_store.index("def _discover_upload_version") : object_store.index(
            "async def discover_upload_version"
        )
    ]

    assert "head_object" in method
    assert "list_object_versions" in method
    assert 'listing.get("IsTruncated") is True' in method
    assert "len(versions) != 1 or delete_markers" in method
    assert "get_object" not in method


def test_runtime_finalization_and_orphan_claim_share_the_upload_row_fence() -> None:
    service = _read("components/control-plane/src/fs2_serve/scientific_artifacts.py")
    registration = service[
        service.index("async def register_upload_version", service.index("class Postgres")) :
        service.index("async def finalize_upload", service.index("class Postgres"))
    ]

    assert "FOR UPDATE" in registration
    assert "fs2_scientific_abandoned_upload_claims" in registration
    assert "upload is fenced for abandoned-upload cleanup" in registration


def test_broker_pat_model_checks_have_matching_column_level_database_grants() -> None:
    broker = _read("components/control-plane/src/fs2_serve/artifact_broker_server.py")
    postgres = _read("components/control-plane/src/fs2_serve/postgres.py")

    assert "SELECT id,prefix,pepper_key_id,digest,tenant_id,scopes,models,expires_at,revoked_at" in broker
    assert "SELECT token_id,tenant_id,protocol,model_id FROM fs2_operations" in broker
    assert "id,prefix,pepper_key_id,digest,tenant_id,scopes,models,expires_at,revoked_at" in postgres
    assert "id,token_id,tenant_id,protocol,model_id,worker_id,fencing_token" in postgres


def test_authority_admission_root_queries_have_exact_operation_column_grants() -> None:
    issuer = _read("components/control-plane/src/fs2_serve/artifact_authority_server.py")
    postgres = _read("components/control-plane/src/fs2_serve/postgres.py")

    binding = issuer[issuer.index("async def _operation_admission_is_bound(") : issuer.index(
        "\n\nasync def _operation_artifact_is_bound("
    )]
    for column in (
        "model_id",
        "protocol",
        "operation",
        "request_hmac_key_id",
        "request_hmac",
        "accepted_at",
    ):
        assert f"operation.{column}" in binding
    assert (
        "id,tenant_id,token_id,model_id,protocol,operation,request_hmac_key_id,"
        in postgres
    )
    assert "request_hmac,accepted_at,worker_id,fencing_token" in postgres
    assert (
        "attempt_id,operation_id,tenant_id,stage_id,shard_id,attempt_number,status"
        in postgres
    )
    assert "SELECT operation_id,tenant_id,stage_id,shard_id,attempt_number,status" in issuer
    assert "operation_id,stage_id,tenant_id,manifest,semantic_valid" in postgres
    assert "SELECT manifest,semantic_valid FROM fs2_scientific_stage_commits" in issuer


def test_provider_retention_delete_is_dormant_and_broker_roles_are_delete_free() -> None:
    broker = _read("components/control-plane/src/fs2_serve/artifact_broker_server.py")
    service = _read("components/control-plane/src/fs2_serve/scientific_artifacts.py")
    infrastructure = _read("stages/infrastructure/scientific_artifacts.tf")

    handler = broker[broker.index('if contract.action == "delete":') : broker.index(
        'raise HTTPException(status_code=400, detail="artifact action requires the content endpoint")'
    )]
    purge = service[service.index("    async def purge_expired(") : service.index(
        "\n\n@dataclass\nclass _MemoryOperation"
    )]
    assert "provider.delete(" not in handler
    assert "retention deletion is not activated" in handler
    assert "await self._store.delete" not in purge
    assert "return []" in purge
    assert '"storage.object-editor"' not in infrastructure
    assert '"storage.uploader"' in infrastructure
    assert '"storage.object-viewer"' in infrastructure
    assert '"storage.object-lister"' in infrastructure
    assert "noncurrent_days" not in infrastructure
