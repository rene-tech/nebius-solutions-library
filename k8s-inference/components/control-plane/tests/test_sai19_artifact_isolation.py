"""Regression contracts for SAI-19 tenant isolation and read integrity."""

from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from test_scientific_artifacts import (
    TENANT,
    FakeObjectStore,
    build_service,
    open_attempt,
    upload,
)

from fs2_serve.artifact_credential_broker import (
    ArtifactCredentialBroker,
    ArtifactCredentialBrokerConfig,
    BrokeredS3ArtifactObjectStore,
    artifact_authority,
)
from fs2_serve.scientific_artifacts import (
    ArtifactDirection,
    ArtifactNotFoundError,
    ArtifactVerificationError,
    EphemeralHandle,
    MemoryArtifactRepository,
    artifact_storage_key,
)
from fs2_serve.scientific_batch.artifact_bridge import SignedArtifactContentReader
from fs2_serve.scientific_object_store import ArtifactStorageUnavailableError
from fs2_serve.tenant_object_store import (
    TenantScopedS3ArtifactObjectStore,
    load_tenant_object_store,
    tenant_from_storage_key,
)


class _RecordingTenantStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def presign_download(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        object_version_id: str,
        ttl: timedelta,
    ) -> EphemeralHandle:
        self.calls.append(storage_key)
        return EphemeralHandle(
            method="GET",
            url=f"https://store.invalid/object?versionId={object_version_id}&X-Amz-Signature=redacted",
            expires_at=datetime.now(UTC) + ttl,
        )


def _tenant_key(tenant_id: str) -> str:
    return artifact_storage_key(
        tenant_id=tenant_id,
        operation_id=uuid4(),
        stage_id="design",
        shard_id=None,
        attempt_id=uuid4(),
        direction=ArtifactDirection.OUTPUT,
        digest="sha256:" + "a" * 64,
    )


def _readiness_bindings(tmp_path: Path, *tenant_ids: str) -> Path:
    path = tmp_path / "bindings.json"
    path.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/artifact-provider-observation-bindings/v1",
                "tenants": {
                    tenant_id: {
                        "active_generation": 1,
                        "authorized_generations": {
                            "1": {"binding_sha256": hashlib.sha256(tenant_id.encode()).hexdigest()}
                        },
                    }
                    for tenant_id in tenant_ids
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_in_process_tenant_dispatch_is_not_a_runtime_fallback() -> None:
    tenant_a = _RecordingTenantStore()
    tenant_b = _RecordingTenantStore()
    with pytest.raises(RuntimeError, match="retired"):
        TenantScopedS3ArtifactObjectStore(
            {"tenant-a": tenant_a, "tenant-b": tenant_b}  # type: ignore[arg-type]
        )
    with pytest.raises(ArtifactNotFoundError):
        tenant_from_storage_key("scientific/v1/tenants/../operations/not-canonical")


def test_static_tenant_credential_loader_is_not_a_runtime_fallback(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="retired"):
        load_tenant_object_store(
            tmp_path,
            default_endpoint_url="https://storage.invalid",
            default_bucket="scientific-artifacts",
            default_region="test-1",
            default_addressing_style="path",
            default_verify_tls=True,
            max_stream_bytes=1024,
        )


@pytest.mark.asyncio
async def test_modified_object_after_finalize_is_rejected_by_open_content() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    original = b"ATOM  CA"
    record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=original)
    _, media_type, compression = store.objects[record.storage_key]
    store.objects[record.storage_key] = (b"ATOM  XX", media_type, compression)

    stream = await service.open_content(record.artifact_id, tenant_id=TENANT)
    released: list[bytes] = []
    with pytest.raises(ArtifactVerificationError, match="digest"):
        async for chunk in stream.chunks:
            released.append(chunk)
    assert released == []


@pytest.mark.asyncio
async def test_download_does_not_reread_bytes_and_is_bound_to_finalized_version() -> None:
    class CountingStore(FakeObjectStore):
        def __init__(self) -> None:
            super().__init__()
            self.inspect_calls = 0

        async def inspect(self, **kwargs):
            self.inspect_calls += 1
            return await super().inspect(**kwargs)

    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = CountingStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=b"ATOM  CA")
    store.inspect_calls = 0

    result = await service.download(record.artifact_id, tenant_id=TENANT)

    assert store.inspect_calls == 0
    assert result.artifact.object_version_id == "test-version"
    assert "versionId=test-version" in result.handle.url


@pytest.mark.asyncio
async def test_repository_key_substitution_cannot_select_victim_authority() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=b"ATOM  CA")
    victim_key = _tenant_key("tenant-b")
    repository._artifacts[record.artifact_id] = record.model_copy(update={"storage_key": victim_key})
    issued_before = len(store.issued)

    with pytest.raises(ArtifactVerificationError, match="authorized tenant scope"):
        await service.download(record.artifact_id, tenant_id=TENANT)

    assert len(store.issued) == issued_before


@pytest.mark.asyncio
async def test_broker_routes_each_tenant_to_an_exact_service_without_returning_credentials(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-workload-token", encoding="ascii")
    bindings_file = _readiness_bindings(tmp_path, "tenant-a", "tenant-b")
    seen: list[dict[str, object]] = []

    async def operate(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer projected-workload-token"
        assert request.headers["x-fs2-artifact-authority"] == "Bearer fs2_pat_authoritative-caller-token"
        body = json.loads(request.content)
        seen.append(body)
        tenant_id = body["tenant_id"]
        tenant_hash = hashlib.sha256(tenant_id.encode()).hexdigest()[:32]
        assert request.url.host == f"fs2-artifact-{tenant_hash}.fs2-system.svc"
        return httpx.Response(
            200,
            json={
                "tenant_id": tenant_id,
                "credential_generation": 1,
                "provider_binding_sha256": hashlib.sha256(tenant_id.encode()).hexdigest(),
                "storage_key": body["storage_key"],
                "action": body["action"],
                "object_version_id": body["object_version_id"],
                "result": {
                    "method": "PUT",
                    "url": f"https://storage.eu-north1.nebius.cloud/{tenant_hash}",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                    "write_once": True,
                    "headers": {
                        "content-type": "chemical/x-pdb",
                        "content-length": "1024",
                    },
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(operate))
    broker = ArtifactCredentialBroker(
        ArtifactCredentialBrokerConfig(
            url_template="https://fs2-artifact-{tenant_hash}.fs2-system.svc:8443/v1",
            audience="fs2-artifact-credential-broker",
            token_file=token_file,
            ca_file=tmp_path / "ca.crt",
            readiness_bindings_file=bindings_file,
        ),
        client=client,
    )
    store = BrokeredS3ArtifactObjectStore(broker)
    try:
        with artifact_authority("fs2_pat_authoritative-caller-token"):
            first = await store.presign_upload(
                tenant_id="tenant-a",
                storage_key=_tenant_key("tenant-a"),
                expected_size_bytes=1024,
                media_type="chemical/x-pdb",
                compression=None,
                ttl=timedelta(minutes=2),
            )
            second = await store.presign_upload(
                tenant_id="tenant-b",
                storage_key=_tenant_key("tenant-b"),
                expected_size_bytes=1024,
                media_type="chemical/x-pdb",
                compression=None,
                ttl=timedelta(minutes=2),
            )
            with pytest.raises(ArtifactNotFoundError):
                await store.presign_upload(
                    tenant_id="tenant-a",
                    storage_key=_tenant_key("tenant-b"),
                    expected_size_bytes=1024,
                    media_type="chemical/x-pdb",
                    compression=None,
                    ttl=timedelta(minutes=2),
                )
    finally:
        await client.aclose()

    assert [item["tenant_id"] for item in seen] == ["tenant-a", "tenant-b"]
    assert all(item["action"] == "presign-upload" for item in seen)
    assert first.url != second.url
    assert "access_key" not in json.dumps(seen)


@pytest.mark.asyncio
async def test_broker_rejects_an_operation_result_for_a_different_key(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-workload-token", encoding="ascii")
    bindings_file = _readiness_bindings(tmp_path, "tenant-a")
    async def mismatched_scope(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "tenant_id": body["tenant_id"],
                "credential_generation": 1,
                "provider_binding_sha256": hashlib.sha256(b"tenant-a").hexdigest(),
                "storage_key": _tenant_key("tenant-a"),
                "action": body["action"],
                "object_version_id": body["object_version_id"],
                "result": {},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mismatched_scope))
    broker = ArtifactCredentialBroker(
        ArtifactCredentialBrokerConfig(
            url_template="https://fs2-artifact-{tenant_hash}.fs2-system.svc:8443/v1",
            audience="fs2-artifact-credential-broker",
            token_file=token_file,
            ca_file=tmp_path / "ca.crt",
            readiness_bindings_file=bindings_file,
        ),
        client=client,
    )
    try:
        with pytest.raises(ArtifactStorageUnavailableError, match="different storage scope"):
            with artifact_authority("fs2_pat_authoritative-caller-token"):
                store = BrokeredS3ArtifactObjectStore(broker)
                await store.presign_upload(
                    tenant_id="tenant-a",
                    storage_key=_tenant_key("tenant-a"),
                    expected_size_bytes=1024,
                    media_type="chemical/x-pdb",
                    compression=None,
                    ttl=timedelta(minutes=2),
                )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_signed_reader_rejects_same_size_digest_substitution() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    original = b"ATOM  CA"
    record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=original)

    async def substituted(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ATOM  XX")

    client = httpx.AsyncClient(transport=httpx.MockTransport(substituted))
    reader = SignedArtifactContentReader(service, client=client)
    try:
        with pytest.raises(ArtifactNotFoundError, match="digest"):
            await reader.read(record.artifact_id, tenant_id=TENANT, maximum_bytes=len(original))
    finally:
        await client.aclose()
