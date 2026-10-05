"""Real SQL and in-memory coverage for bounded native checkpoint publication."""

import asyncio
import time
from uuid import uuid4

import pytest
from test_scientific_artifacts import (
    NOW,
    TENANT,
    FakeObjectStore,
    build_service,
    digest,
    insert_operation,
    open_attempt,
)
from test_scientific_artifacts import postgres_store as postgres_store  # noqa: F401
from test_scientific_artifacts import runtime_pool as runtime_pool  # noqa: F401

from fs2_serve.scientific_artifacts import (
    ArtifactConflictError,
    ArtifactDirection,
    ArtifactEventType,
    ArtifactNotFoundError,
    ArtifactPolicyError,
    ArtifactVerificationError,
    BeginArtifactUpload,
    FinalizeArtifactUpload,
    MemoryArtifactRepository,
    PostgresArtifactRepository,
    StaleArtifactAttemptError,
)


def cohort(operation, attempt, *, count=64, offset=0):
    values = tuple(f"retained-native-file-{index:08d}".encode() for index in range(offset, offset + count))
    requests = tuple(BeginArtifactUpload(
        upload_id=uuid4(), operation_id=operation, tenant_id=TENANT, attempt_id=attempt,
        direction=ArtifactDirection.OUTPUT, expected_digest=digest(value), expected_size_bytes=len(value),
        media_type="chemical/x-pdb",
    ) for value in values)
    return requests, values


def finalizations(requests):
    return tuple(FinalizeArtifactUpload(
        upload_id=request.upload_id, operation_id=request.operation_id, tenant_id=request.tenant_id,
    ) for request in requests)


async def exercise_cohort(repository, operation):
    objects = FakeObjectStore()
    service = build_service(repository, objects)
    attempt = await open_attempt(service, operation_id=operation)
    requests, values = cohort(operation, attempt)
    begun = await service.begin_uploads(requests)
    assert [entry.upload.upload_id for entry in begun] == [request.upload_id for request in requests]
    for entry, value in zip(begun, values, strict=True):
        objects.put(entry.upload.storage_key, value, entry.upload.media_type)
    published, replay = await asyncio.gather(
        service.finalize_uploads(finalizations(requests)), service.finalize_uploads(finalizations(requests)),
    )
    assert published == replay
    assert [entry.digest for entry in published] == [digest(value) for value in values]
    assert await service.finalize_uploads(tuple(reversed(finalizations(requests)))) == list(reversed(published))
    assert [entry.upload for entry in await service.begin_uploads(requests)] == [
        await repository.get_upload(request) for request in finalizations(requests)
    ]
    events = await service.list_events(operation, tenant_id=TENANT)
    assert sum(event.event_type == ArtifactEventType.UPLOAD_BEGUN for event in events) == 64
    assert sum(event.event_type == ArtifactEventType.ARTIFACT_FINALIZED for event in events) == 64
    return service, objects, requests


async def test_memory_cohort_replays_in_request_order():
    repository = MemoryArtifactRepository(clock=lambda: NOW)
    operation = uuid4()
    await repository.register_operation(operation, tenant_id=TENANT)
    await exercise_cohort(repository, operation)


@pytest.mark.postgres
async def test_postgres_cohort_replays_in_request_order(runtime_pool):  # noqa: F811
    operation = uuid4()
    await insert_operation(runtime_pool, operation)
    await exercise_cohort(PostgresArtifactRepository(runtime_pool), operation)


@pytest.mark.parametrize("variation", ["empty", "oversize", "duplicate", "foreign-tenant", "foreign-operation"])
async def test_invalid_cohort_rejected_before_repository_or_store(variation):
    repository = MemoryArtifactRepository(clock=lambda: NOW)
    service = build_service(repository, FakeObjectStore())
    requests, _ = cohort(uuid4(), uuid4(), count=65 if variation == "oversize" else 2)
    if variation == "empty":
        requests = ()
    elif variation == "duplicate":
        requests = (requests[0], requests[0])
    elif variation == "foreign-tenant":
        requests = (requests[0], requests[1].model_copy(update={"tenant_id": "other-tenant"}))
    elif variation == "foreign-operation":
        requests = (requests[0], requests[1].model_copy(update={"operation_id": uuid4()}))
    with pytest.raises(ArtifactPolicyError):
        await service.begin_uploads(requests)
    with pytest.raises(ArtifactPolicyError):
        await service.finalize_uploads(finalizations(requests))
    assert not repository._uploads


async def test_foreign_attempt_and_policy_checked_before_reservation():
    repository = MemoryArtifactRepository(clock=lambda: NOW)
    operation = uuid4()
    await repository.register_operation(operation, tenant_id=TENANT)
    service = build_service(repository, FakeObjectStore())
    attempt = await open_attempt(service, operation_id=operation)
    requests, _ = cohort(operation, attempt, count=2)
    with pytest.raises(ArtifactPolicyError):
        await service.begin_uploads((requests[0], requests[1].model_copy(update={"attempt_id": uuid4()})))
    with pytest.raises(ArtifactPolicyError):
        await service.begin_uploads((requests[0], requests[1].model_copy(update={"media_type": "invalid/blocked"})))
    assert not repository._uploads


async def test_corruption_prevents_entire_finalization_and_inspection_is_bounded():
    class ObservedStore(FakeObjectStore):
        active = 0
        peak = 0

        async def inspect(self, storage_key, *, max_bytes=None):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.001)
                return await super().inspect(storage_key, max_bytes=max_bytes)
            finally:
                self.active -= 1

    repository = MemoryArtifactRepository(clock=lambda: NOW)
    operation = uuid4()
    await repository.register_operation(operation, tenant_id=TENANT)
    objects = ObservedStore()
    service = build_service(repository, objects)
    attempt = await open_attempt(service, operation_id=operation)
    requests, values = cohort(operation, attempt)
    begun = await service.begin_uploads(requests)
    for entry, value in zip(begun, values, strict=True):
        objects.put(entry.upload.storage_key, value, entry.upload.media_type)
    objects.put(begun[-1].upload.storage_key, b"truncated", begun[-1].upload.media_type)
    with pytest.raises(ArtifactVerificationError):
        await service.finalize_uploads(finalizations(requests))
    assert not repository._artifacts
    assert 1 < objects.peak <= 8
    objects.put(begun[-1].upload.storage_key, values[-1], begun[-1].upload.media_type)
    assert len(await service.finalize_uploads(finalizations(requests))) == 64


@pytest.mark.postgres
async def test_postgres_conflict_does_not_leave_partial_cohort(runtime_pool):  # noqa: F811
    operation = uuid4()
    await insert_operation(runtime_pool, operation)
    repository = PostgresArtifactRepository(runtime_pool)
    service = build_service(repository, FakeObjectStore())
    attempt = await open_attempt(service, operation_id=operation)
    requests, _ = cohort(operation, attempt)
    await service.begin_upload(requests[-1])
    corrupted = (*requests[:-1], requests[-1].model_copy(update={"expected_size_bytes": 999}))
    with pytest.raises(ArtifactConflictError):
        await service.begin_uploads(corrupted)
    assert await runtime_pool.fetchval(
        "SELECT count(*) FROM fs2_scientific_uploads WHERE operation_id=$1", operation,
    ) == 1
    assert len(await service.begin_uploads(requests)) == 64


@pytest.mark.postgres
async def test_postgres_superseded_attempt_cannot_publish_cohort(runtime_pool):  # noqa: F811
    operation = uuid4()
    await insert_operation(runtime_pool, operation)
    repository = PostgresArtifactRepository(runtime_pool)
    objects = FakeObjectStore()
    service = build_service(repository, objects)
    attempt = await open_attempt(service, operation_id=operation)
    requests, values = cohort(operation, attempt)
    begun = await service.begin_uploads(requests)
    for entry, value in zip(begun, values, strict=True):
        objects.put(entry.upload.storage_key, value, entry.upload.media_type)
    await open_attempt(service, operation_id=operation, attempt_number=2)
    with pytest.raises(StaleArtifactAttemptError):
        await service.finalize_uploads(finalizations(requests))
    assert not await service.list_artifacts(operation, tenant_id=TENANT)
    with pytest.raises(StaleArtifactAttemptError):
        await service.begin_uploads(requests)


@pytest.mark.postgres
async def test_postgres_missing_or_foreign_upload_rejects_cohort(runtime_pool):  # noqa: F811
    operation = uuid4()
    await insert_operation(runtime_pool, operation)
    service = build_service(PostgresArtifactRepository(runtime_pool), FakeObjectStore())
    attempt = await open_attempt(service, operation_id=operation)
    requests, _ = cohort(operation, attempt, count=2)
    await service.begin_upload(requests[0])
    with pytest.raises(ArtifactNotFoundError):
        await service.finalize_uploads(finalizations(requests))
    foreign = requests[0].model_copy(update={"tenant_id": "other-tenant"})
    with pytest.raises(ArtifactNotFoundError):
        await service.finalize_uploads(finalizations((foreign,)))


@pytest.mark.postgres
async def test_postgres_thousands_of_unique_native_files(runtime_pool):  # noqa: F811
    """Exercise actual runtime-role SQL; this is not an object-store benchmark."""
    operation = uuid4()
    await insert_operation(runtime_pool, operation)
    objects = FakeObjectStore()
    service = build_service(PostgresArtifactRepository(runtime_pool), objects)
    attempt = await open_attempt(service, operation_id=operation)
    started = time.monotonic()
    for offset in range(0, 2048, 64):
        requests, values = cohort(operation, attempt, offset=offset)
        begun = await service.begin_uploads(requests)
        for entry, value in zip(begun, values, strict=True):
            objects.put(entry.upload.storage_key, value, entry.upload.media_type)
        published = await service.finalize_uploads(finalizations(requests))
        assert [item.digest for item in published] == [digest(value) for value in values]
    assert await runtime_pool.fetchval(
        "SELECT count(*) FROM fs2_scientific_artifacts WHERE operation_id=$1", operation,
    ) == 2048
    print(f"actual_runtime_role_bulk_sql_2048_files_seconds={time.monotonic()-started:.3f}")
