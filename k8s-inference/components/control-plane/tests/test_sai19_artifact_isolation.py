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

from fs2_serve.scientific_artifacts import (
    ArtifactDirection,
    ArtifactNotFoundError,
    ArtifactVerificationError,
    EphemeralHandle,
    MemoryArtifactRepository,
    artifact_storage_key,
)
from fs2_serve.scientific_batch.artifact_bridge import SignedArtifactContentReader
from fs2_serve.tenant_object_store import (
    TenantScopedS3ArtifactObjectStore,
    load_tenant_object_store,
    tenant_from_storage_key,
)


class _RecordingTenantStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def presign_download(self, *, storage_key: str, ttl: timedelta) -> EphemeralHandle:
        self.calls.append(storage_key)
        return EphemeralHandle(
            method="GET",
            url="https://store.invalid/object?X-Amz-Signature=redacted",
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

    await store.presign_download(storage_key=key, ttl=timedelta(minutes=2))

    assert tenant_a.calls == [key]
    assert tenant_b.calls == []
    with pytest.raises(ArtifactNotFoundError):
        await store.presign_download(storage_key=_tenant_key("tenant-c"), ttl=timedelta(minutes=2))
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

    with pytest.raises(ArtifactVerificationError, match="digest"):
        await service.open_content(record.artifact_id, tenant_id=TENANT)


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
