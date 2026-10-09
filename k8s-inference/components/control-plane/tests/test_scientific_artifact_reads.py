"""Bounded tenant reads must preserve aliases, ordering and commit fences."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_scientific_artifact_batches import cohort, finalizations
from test_scientific_artifacts import NOW, TENANT, FakeObjectStore, build_service, insert_operation, open_attempt
from test_scientific_artifacts import postgres_store as postgres_store  # noqa: F401
from test_scientific_artifacts import runtime_pool as runtime_pool  # noqa: F401
from test_scientific_batch_execution_handoff import artifact

from fs2_serve.scientific_artifacts import (
    ArtifactAccess,
    ArtifactNotFoundError,
    ArtifactPolicyError,
    AttemptStatus,
    CloseStageAttempt,
    CommitStageResult,
    ManifestEntryDraft,
    MemoryArtifactRepository,
    PostgresArtifactRepository,
)
from fs2_serve.scientific_batch.artifact_bridge import ArtifactServiceBridge
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog


async def publish(repository, operation, count=64):
    objects = FakeObjectStore()
    service = build_service(repository, objects)
    attempt = await open_attempt(service, operation_id=operation)
    records = []
    for offset in range(0, count, 64):
        requests, values = cohort(operation, attempt, count=min(count - offset, 64), offset=offset)
        begun = await service.begin_uploads(requests)
        for item, value in zip(begun, values, strict=True):
            objects.put(item.upload.storage_key, value, item.upload.media_type)
        records.extend(await service.finalize_uploads(finalizations(requests)))
    return service, records, attempt


async def ordered_reads(repository, operation):
    service, records, _ = await publish(repository, operation)
    ids = tuple(record.artifact_id for record in records)
    requested = (*reversed(ids), ids[0], ids[-1])
    assert await repository.get_artifacts(requested, tenant_id=TENANT) == [*reversed(records), records[0], records[-1]]
    handles = await service.downloads(requested, tenant_id=TENANT)
    assert [item.artifact.artifact_id for item in handles] == list(requested)
    for bad in ((), ids + ids + (uuid4(),)):
        with pytest.raises(ArtifactPolicyError):
            await repository.get_artifacts(bad, tenant_id=TENANT)
    for bad_ids, tenant in ((ids, "foreign"), ((ids[0], uuid4()), TENANT)):
        with pytest.raises(ArtifactNotFoundError):
            await repository.get_artifacts(bad_ids, tenant_id=tenant)


async def test_memory_batch_read_scope_and_aliases():
    repository = MemoryArtifactRepository(clock=lambda: NOW)
    operation = uuid4()
    await repository.register_operation(operation, tenant_id=TENANT)
    await ordered_reads(repository, operation)


@pytest.mark.postgres
async def test_postgres_runtime_role_batch_read_scope_and_aliases(runtime_pool):  # noqa: F811
    operation = uuid4()
    await insert_operation(runtime_pool, operation)
    await ordered_reads(PostgresArtifactRepository(runtime_pool), operation)


async def test_bulk_download_signing_is_bounded_and_keeps_original_ttl_policy():
    class Store(FakeObjectStore):
        active = 0
        peak = 0

        async def presign_download(self, **kwargs):
            self.active += 1
            self.peak = max(self.active, self.peak)
            try:
                await asyncio.sleep(0.001)
                return await super().presign_download(**kwargs)
            finally:
                self.active -= 1

    repository = MemoryArtifactRepository(clock=lambda: NOW)
    operation = uuid4()
    await repository.register_operation(operation, tenant_id=TENANT)
    _, records, _ = await publish(repository, operation)
    objects = Store()
    service = build_service(repository, objects)
    await service.downloads(tuple(row.artifact_id for row in records), tenant_id=TENANT)
    assert 1 < objects.peak <= 8
    with pytest.raises(ArtifactPolicyError):
        await service.downloads((records[0].artifact_id,), tenant_id=TENANT, handle_ttl=timedelta(days=1))


@pytest.mark.parametrize("damage", [None, "pointer", "access"])
async def test_large_input_manifest_reads128_rows_at_a_time_and_verifies_each_pointer(damage):
    from conftest import CONTROL_ROOT

    count = 20000
    operation = uuid4()
    records = [
        artifact(operation, uuid4(), str(index).encode(), "application/json", ArtifactAccess())
        for index in range(count)
    ]
    document = {
        "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
        "manifest_id": "late-native-recovery",
        "entries": [
            {
                "name": f"native-{index:05d}",
                "semantic_type": "gromacs-continuation-file/v1",
                "artifact": record.to_public_ref().model_dump(mode="json"),
            }
            for index, record in enumerate(records)
        ],
    }
    if damage == "pointer":
        document["entries"][-1]["artifact"]["size_bytes"] += 1
    if damage == "access":
        records[-1] = records[-1].model_copy(update={"access": ArtifactAccess(profile="academic")})
    payload = json.dumps(document).encode()
    manifest = artifact(operation, uuid4(), payload, "application/vnd.fs2.scientific-manifest+json", ArtifactAccess())
    indexed = {row.artifact_id: row for row in records}
    calls = []

    async def get_one(artifact_id, *, tenant_id):
        assert artifact_id == manifest.artifact_id and tenant_id == "academic-poc"
        return manifest

    async def get_many(artifact_ids, *, tenant_id):
        assert tenant_id == "academic-poc" and 1 <= len(artifact_ids) <= 128
        calls.append(len(artifact_ids))
        return [indexed[artifact_id] for artifact_id in artifact_ids]

    async def read(*args, **kwargs):
        return payload

    bridge = ArtifactServiceBridge(
        artifacts=SimpleNamespace(get_artifact=get_one, get_artifacts=get_many),
        batches=SimpleNamespace(),
        profiles=ScientificProfileCatalog.load(CONTROL_ROOT.parents[1] / "catalog/runtime"),
        store=SimpleNamespace(),
        content_reader=SimpleNamespace(read=read),
    )
    pointer = manifest.to_public_ref().model_dump(mode="json")
    if damage is not None:
        with pytest.raises(ArtifactNotFoundError):
            await bridge.validate_input(pointer, tenant_id="academic-poc")
    else:
        admitted = await bridge.validate_input(pointer, tenant_id="academic-poc")
        assert [entry.artifact_id for entry in admitted.manifest.entries] == [row.artifact_id for row in records]
    assert len(calls) == 157 and sum(calls) == count


@pytest.mark.postgres
async def test_postgres20k_output_stage_commit_uses_scoped_batches_and_preserves_aliases(runtime_pool):  # noqa: F811
    operation = uuid4()
    await insert_operation(runtime_pool, operation)
    repository = PostgresArtifactRepository(runtime_pool)
    service, records, attempt = await publish(repository, operation, count=20000)
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt,
            operation_id=operation,
            tenant_id=TENANT,
            status=AttemptStatus.SUCCEEDED,
            completed_at=NOW + timedelta(minutes=5),
        )
    )
    entries = tuple(
        ManifestEntryDraft(
            name=f"part-{index:05d}",
            semantic_type="gromacs-native-file/v1",
            artifact_id=record.artifact_id,
        )
        for index, record in enumerate(records)
    )
    entries += (
        ManifestEntryDraft(
            name="native-alias", semantic_type="gromacs-native-file/v1", artifact_id=records[0].artifact_id
        ),
    )
    request = CommitStageResult(
        operation_id=operation,
        tenant_id=TENANT,
        stage_id="design",
        attempt_ids=(attempt,),
        entries=entries,
        validation_digest="sha256:" + "a" * 64,
        semantic_valid=True,
        committed_at=NOW + timedelta(minutes=6),
        validated_at=NOW + timedelta(minutes=6),
    )
    first, replay = await asyncio.gather(service.commit_stage(request), service.commit_stage(request))
    assert first.manifest_digest == replay.manifest_digest and len(first.manifest.entries) == 20001
    with pytest.raises(ArtifactNotFoundError):
        await repository.get_artifacts((records[0].artifact_id,), tenant_id="foreign")
