"""Regression contracts for SAI-19 tenant isolation and read integrity."""

from __future__ import annotations

import json
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


@pytest.mark.asyncio
async def test_tenant_dispatch_never_falls_back_to_another_identity() -> None:
    tenant_a = _RecordingTenantStore()
    tenant_b = _RecordingTenantStore()
    store = TenantScopedS3ArtifactObjectStore(
        {"tenant-a": tenant_a, "tenant-b": tenant_b}  # type: ignore[arg-type]
    )
    key = _tenant_key("tenant-a")

    await store.presign_download(
        tenant_id="tenant-a",
        storage_key=key,
        object_version_id="version-a",
        ttl=timedelta(minutes=2),
    )

    assert tenant_a.calls == [key]
    assert tenant_b.calls == []
    with pytest.raises(ArtifactNotFoundError):
        await store.presign_download(
            tenant_id="tenant-c",
            storage_key=_tenant_key("tenant-c"),
            object_version_id="version-c",
            ttl=timedelta(minutes=2),
        )
    with pytest.raises(ArtifactNotFoundError):
        await store.presign_download(
            tenant_id="tenant-a",
            storage_key=_tenant_key("tenant-b"),
            object_version_id="version-b",
            ttl=timedelta(minutes=2),
        )
    with pytest.raises(ArtifactNotFoundError):
        tenant_from_storage_key("scientific/v1/tenants/../operations/not-canonical")


def test_tenant_credential_loader_rejects_one_key_shared_by_two_tenants(tmp_path: Path) -> None:
    for tenant_id in ("tenant-a", "tenant-b"):
        (tmp_path / f"{tenant_id}.json").write_text(
            json.dumps(
                {
                    "tenant_id": tenant_id,
                    "access_key_id": "reused-key-id",
                    "secret_access_key": "not-a-real-secret",
                }
            ),
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="must not be shared"):
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
async def test_broker_exchanges_one_uncached_short_lived_tenant_session_per_request(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-workload-token", encoding="ascii")
    now = datetime(2026, 9, 17, 13, 0, tzinfo=UTC)
    seen: list[dict[str, object]] = []

    async def exchange(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer projected-workload-token"
        body = json.loads(request.content)
        seen.append(body)
        tenant_id = body["tenant_id"]
        return httpx.Response(
            200,
            json={
                "tenant_id": tenant_id,
                "storage_key": body["storage_key"],
                "action": body["action"],
                "object_version_id": body["object_version_id"],
                "access_key_id": f"{tenant_id}-ephemeral-key",
                "secret_access_key": "ephemeral-secret",
                "session_token": "ephemeral-session-token",
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "endpoint_url": "https://storage.invalid",
                "bucket": f"{tenant_id}-artifacts",
                "region": "test-1",
                "addressing_style": "path",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(exchange))
    broker = ArtifactCredentialBroker(
        ArtifactCredentialBrokerConfig(
            url="https://broker.invalid/v1/artifact-credentials:exchange",
            audience="fs2-artifact-credential-broker",
            token_file=token_file,
            ca_file=tmp_path / "ca.crt",
        ),
        client=client,
        clock=lambda: now,
    )
    store = BrokeredS3ArtifactObjectStore(broker)
    try:
        first = await store.presign_upload(
            tenant_id="tenant-a",
            storage_key=_tenant_key("tenant-a"),
            media_type="chemical/x-pdb",
            compression=None,
            ttl=timedelta(minutes=2),
        )
        second = await store.presign_upload(
            tenant_id="tenant-b",
            storage_key=_tenant_key("tenant-b"),
            media_type="chemical/x-pdb",
            compression=None,
            ttl=timedelta(minutes=2),
        )
        with pytest.raises(ArtifactNotFoundError):
            await store.presign_upload(
                tenant_id="tenant-a",
                storage_key=_tenant_key("tenant-b"),
                media_type="chemical/x-pdb",
                compression=None,
                ttl=timedelta(minutes=2),
            )
    finally:
        await client.aclose()

    assert [item["tenant_id"] for item in seen] == ["tenant-a", "tenant-b"]
    assert all(item["action"] == "presign-upload" for item in seen)
    assert "tenant-a-ephemeral-key" in first.url
    assert "tenant-b-ephemeral-key" in second.url


@pytest.mark.asyncio
async def test_broker_rejects_a_credential_attested_for_a_different_key(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-workload-token", encoding="ascii")
    now = datetime(2026, 9, 17, 13, 0, tzinfo=UTC)

    async def mismatched_scope(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "tenant_id": body["tenant_id"],
                "storage_key": _tenant_key("tenant-a"),
                "action": body["action"],
                "object_version_id": body["object_version_id"],
                "access_key_id": "ephemeral-key",
                "secret_access_key": "ephemeral-secret",
                "session_token": "ephemeral-session-token",
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "endpoint_url": "https://storage.invalid",
                "bucket": "tenant-a-artifacts",
                "region": "test-1",
                "addressing_style": "path",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(mismatched_scope))
    broker = ArtifactCredentialBroker(
        ArtifactCredentialBrokerConfig(
            url="https://broker.invalid/v1/artifact-credentials:exchange",
            audience="fs2-artifact-credential-broker",
            token_file=token_file,
            ca_file=tmp_path / "ca.crt",
        ),
        client=client,
        clock=lambda: now,
    )
    try:
        with pytest.raises(ArtifactStorageUnavailableError, match="different storage scope"):
            await broker.exchange(
                tenant_id="tenant-a",
                storage_key=_tenant_key("tenant-a"),
                action="presign-upload",
                object_version_id=None,
                minimum_ttl_seconds=120,
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
