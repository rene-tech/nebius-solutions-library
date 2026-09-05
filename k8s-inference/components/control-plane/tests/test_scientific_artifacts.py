"""Contract, fencing, privacy and storage tests for the artifact service.

The canonical JSON Schemas in ``catalog/runtime/schema`` are loaded directly so
the control plane can never drift into a look-alike of a public contract it does
not own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from conftest import CONTROL_ROOT
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from fs2_serve.crypto import KeyedHasher, PayloadCipher
from fs2_serve.postgres import PostgresStore
from fs2_serve.scientific_artifacts import (
    HANDLE_CLOCK_SKEW,
    MAX_HANDLE_TTL,
    NO_SHARD,
    SCIENTIFIC_ARTIFACT_MIGRATION,
    ArtifactAccess,
    ArtifactAccessProfile,
    ArtifactCompression,
    ArtifactConflictError,
    ArtifactDirection,
    ArtifactEventType,
    ArtifactNotFoundError,
    ArtifactPolicyError,
    ArtifactVerificationError,
    AttemptStatus,
    BeginArtifactUpload,
    CloseStageAttempt,
    CommitStageResult,
    EphemeralHandle,
    FinalizeArtifactUpload,
    KueueAdmission,
    ManifestEntryDraft,
    MemoryArtifactRepository,
    OpenStageAttempt,
    PostgresArtifactRepository,
    ResultAlreadyTerminalError,
    RunResultDraft,
    ScientificArtifactService,
    StaleArtifactAttemptError,
    VerifiedStoredObject,
    artifact_storage_key,
)

SCHEMA_ROOT = CONTROL_ROOT.parents[1] / "catalog/runtime/schema"
NOW = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
TENANT = "tenant-a"
ALLOWED_MEDIA_TYPES = {
    "application/json",
    "application/vnd.fs2.scientific-manifest+json",
    "chemical/x-pdb",
    "text/x-fasta",
}
ADMISSION = KueueAdmission(
    resolved_pool_id="gpu-preemptible",
    admitted_resource_flavor="gpu-preemptible",
    accelerator_resource_name="nvidia.com/gpu",
    accelerator_count=1,
    admitted_at=NOW,
)


def load_schema(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMA_ROOT / f"{name}.schema.json").read_text(encoding="utf-8")))


POINTER_SCHEMA = load_schema("scientific-artifact-pointer")
MANIFEST_SCHEMA = load_schema("scientific-artifact-manifest")
RESULT_SCHEMA = load_schema("scientific-run-result")


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class FakeObjectStore:
    """Records what was stored and honours the test clock exactly."""

    def __init__(self, *, clock=lambda: NOW) -> None:
        self.objects: dict[str, tuple[bytes, str, ArtifactCompression | None]] = {}
        self.deleted: list[str] = []
        self.written: list[str] = []
        # ``rewrite`` models a store that silently persists something other
        # than the submitted body, which the service must still detect.
        self.rewrite: bytes | None = None
        self.override: VerifiedStoredObject | None = None
        self.issued: list[EphemeralHandle] = []
        self._clock = clock

    def put(
        self,
        storage_key: str,
        value: bytes,
        media_type: str,
        compression: ArtifactCompression | None = None,
    ) -> None:
        self.objects[storage_key] = (value, media_type, compression)

    def _handle(self, method: str, storage_key: str, ttl: timedelta, headers: dict[str, str]) -> EphemeralHandle:
        handle = EphemeralHandle(
            method=method,  # type: ignore[arg-type]
            url=f"https://store.invalid/bucket/{storage_key}?X-Amz-Signature={'a' * 64}",
            expires_at=self._clock() + ttl,
            write_once=method == "PUT",
            headers=headers,
        )
        self.issued.append(handle)
        return handle

    async def presign_upload(
        self,
        *,
        storage_key: str,
        media_type: str,
        compression: ArtifactCompression | None,
        ttl: timedelta,
    ) -> EphemeralHandle:
        headers = {"content-type": media_type}
        if compression is not None:
            headers["content-encoding"] = compression.value
        return self._handle("PUT", storage_key, ttl, headers)

    async def presign_download(self, *, storage_key: str, ttl: timedelta) -> EphemeralHandle:
        return self._handle("GET", storage_key, ttl, {})

    async def put_object(
        self,
        *,
        storage_key: str,
        payload: bytes,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> VerifiedStoredObject:
        self.written.append(storage_key)
        if self.rewrite is not None:
            payload = self.rewrite
        self.objects[storage_key] = (payload, media_type, compression)
        return await self.inspect(storage_key, max_bytes=len(payload))

    async def stream_object(self, storage_key: str, *, max_bytes: int | None = None):
        if storage_key not in self.objects:
            raise ArtifactNotFoundError("stored object is absent")
        value = self.objects[storage_key][0]
        for offset in range(0, max(len(value), 1), 4):
            chunk = value[offset : offset + 4]
            if chunk:
                yield chunk

    async def inspect(self, storage_key: str, *, max_bytes: int | None = None) -> VerifiedStoredObject:
        if self.override is not None:
            return self.override
        if storage_key not in self.objects:
            raise ArtifactNotFoundError("stored object is absent")
        value, media_type, compression = self.objects[storage_key]
        return VerifiedStoredObject(
            storage_key=storage_key,
            digest=digest(value),
            size_bytes=len(value),
            media_type=media_type,
            compression=compression,
        )

    async def delete(self, storage_key: str) -> None:
        self.deleted.append(storage_key)
        self.objects.pop(storage_key, None)


def build_service(repository: Any, object_store: FakeObjectStore, **kwargs: Any) -> ScientificArtifactService:
    return ScientificArtifactService(
        repository=repository,
        object_store=object_store,
        allowed_media_types=ALLOWED_MEDIA_TYPES,
        clock=lambda: NOW,
        **kwargs,
    )


async def open_attempt(
    service: ScientificArtifactService,
    *,
    operation_id: UUID,
    stage_id: str = "design",
    shard_id: str | None = None,
    attempt_number: int = 1,
    attempt_id: UUID | None = None,
) -> UUID:
    identity = attempt_id or uuid4()
    await service.open_attempt(
        OpenStageAttempt(
            attempt_id=identity,
            operation_id=operation_id,
            tenant_id=TENANT,
            stage_id=stage_id,
            shard_id=shard_id,
            attempt_number=attempt_number,
            admission=ADMISSION,
            kueue_workload_uid=f"kueue-{identity.hex[:8]}",
            k8s_job_uid=f"job-{identity.hex[:8]}",
            started_at=NOW,
        )
    )
    return identity


async def upload(
    service: ScientificArtifactService,
    store: FakeObjectStore,
    *,
    operation_id: UUID,
    attempt_id: UUID,
    value: bytes,
    media_type: str = "chemical/x-pdb",
    direction: ArtifactDirection = ArtifactDirection.OUTPUT,
    compression: ArtifactCompression | None = None,
) -> Any:
    upload_id = uuid4()
    begun = await service.begin_upload(
        BeginArtifactUpload(
            upload_id=upload_id,
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            direction=direction,
            expected_digest=digest(value),
            expected_size_bytes=len(value),
            media_type=media_type,
            compression=compression,
        )
    )
    store.put(begun.upload.storage_key, value, media_type, compression)
    return await service.finalize_upload(
        FinalizeArtifactUpload(upload_id=upload_id, operation_id=operation_id, tenant_id=TENANT)
    )


def execution_identity() -> dict[str, str]:
    return {
        "model_id": "proteina-complexa",
        "variant_id": "complexa-protein",
        "model_revision": "a" * 40,
        "runtime_image_digest": "sha256:" + "b" * 64,
        "runtime_recipe_sha256": "c" * 64,
        "workload_recipe_sha256": "d" * 64,
        "model_artifact_manifest_digest": "e" * 64,
        "execution_identity_sha256": "f" * 64,
    }


def scheduling_snapshot(stage_ids: tuple[str, ...] = ("design",)) -> dict[str, Any]:
    return {
        "policy_revision": "1" * 64,
        "captured_at": NOW.isoformat().replace("+00:00", "Z"),
        "service_class": "customer-batch",
        "tenant_queue": "tenant-academic",
        "model_lane": "proteina-complexa",
        "stages": [
            {
                "stage_id": stage_id,
                "resource_class": "gpu",
                "resolved_cluster_queue": "inference-accelerators",
                "resolved_local_queue": "fs2-models",
                "workload_priority_class": "customer-batch",
                "workload_priority_value": 500,
                "resolved_pool_preference": ["gpu-preemptible"],
                "accelerator_resource_name": "nvidia.com/gpu",
                "accelerator_count": 1,
                "max_queue_seconds": 3600,
                "max_execution_seconds": 21600,
                "checkpoint_mode": "restart",
                "preemption_mode": "restartable",
            }
            for stage_id in stage_ids
        ],
    }


# --------------------------------------------------------------------------
# Canonical public contract conformance
# --------------------------------------------------------------------------


async def test_public_pointer_matches_the_canonical_artifact_schema() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=b"ATOM  CA")
    document = record.to_public_ref().model_dump(mode="json")
    POINTER_SCHEMA.validate(document)
    assert set(document) <= {"artifact_id", "sha256", "size_bytes", "media_type", "compression"}
    assert "storage_key" not in document
    assert "tenant_id" not in document
    assert document["sha256"] == record.digest.removeprefix("sha256:")


async def test_stage_commit_publishes_a_canonical_artifact_manifest() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=b"ATOM  CA  ALA")
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.SUCCEEDED,
            completed_at=NOW + timedelta(minutes=5),
            gpu_uuids=("GPU-0000",),
        )
    )
    commit = await service.commit_stage(
        CommitStageResult(
            operation_id=operation_id,
            tenant_id=TENANT,
            stage_id="design",
            attempt_ids=(attempt_id,),
            entries=(
                ManifestEntryDraft(
                    name="designed-backbone", semantic_type="protein.structure/v1", artifact_id=record.artifact_id
                ),
            ),
            validation_digest="sha256:" + "9" * 64,
            semantic_valid=True,
            committed_at=NOW + timedelta(minutes=6),
            validated_at=NOW + timedelta(minutes=7),
        )
    )
    MANIFEST_SCHEMA.validate(commit.manifest.to_document())
    assert commit.manifest_digest == commit.manifest.digest


async def test_terminal_result_matches_the_canonical_run_result_schema() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id, shard_id="candidate-0001")
    inputs = await upload(
        service,
        store,
        operation_id=operation_id,
        attempt_id=attempt_id,
        value=b'{"sequence":"MKT"}',
        media_type="application/vnd.fs2.scientific-manifest+json",
        direction=ArtifactDirection.INPUT,
    )
    outputs = await upload(
        service,
        store,
        operation_id=operation_id,
        attempt_id=attempt_id,
        value=b'{"designs":3}',
        media_type="application/vnd.fs2.scientific-manifest+json",
    )
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.SUCCEEDED,
            completed_at=NOW + timedelta(minutes=5),
            pod_uids=("pod-1",),
            node_uids=("node-1",),
            gpu_uuids=("GPU-1",),
        )
    )
    snapshot = scheduling_snapshot()
    snapshot["stages"].insert(
        0,
        {
            "stage_id": "prepare-input",
            "resource_class": "cpu",
            "resolved_cluster_queue": "scientific-cpu",
            "resolved_local_queue": "scientific-reference-data",
            "workload_priority_class": "customer-batch",
            "workload_priority_value": 500,
            "resolved_pool_preference": ["batch-cpu"],
            "accelerator_resource_name": None,
            "accelerator_count": 0,
            "max_queue_seconds": 3600,
            "max_execution_seconds": 21600,
            "checkpoint_mode": "restart",
            "preemption_mode": "restartable",
        },
    )
    record = await service.commit_run_result(
        RunResultDraft(
            operation_id=operation_id,
            tenant_id=TENANT,
            terminal_status="succeeded",
            submitted_at=NOW,
            completed_at=NOW + timedelta(minutes=5),
            execution_identity=execution_identity(),
            scheduling_snapshot=snapshot,
            input_manifest_artifact_id=inputs.artifact_id,
            output_manifest_artifact_id=outputs.artifact_id,
            validator_id="proteina-complexa-validator",
            validation_status="passed",
            validation_receipt_digest="sha256:" + "2" * 64,
        )
    )
    document = record.result.to_document()
    RESULT_SCHEMA.validate(document)
    assert document["schema"] == "fs2-serve.nebius.ai/scientific-run-result/v1"
    assert document["scheduling_snapshot"]["stages"][0]["resolved_pool_preference"] == ["batch-cpu"]
    assert document["scheduling_snapshot"]["stages"][0]["accelerator_resource_name"] is None
    assert document["scheduling_snapshot"]["stages"][0]["accelerator_count"] == 0
    assert document["attempts"][0]["scheduling_admission"]["accelerator_count"] == 1
    assert document["attempts"][0]["shard_id"] == "candidate-0001"
    assert document["attempts"][0]["gpu_uuids"] == ["GPU-1"]
    assert record.result_digest == record.result.digest
    assert "tenant_id" not in document
    assert "storage_key" not in json.dumps(document)


async def test_failed_result_carries_a_structured_error_and_no_output_manifest() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    inputs = await upload(
        service,
        store,
        operation_id=operation_id,
        attempt_id=attempt_id,
        value=b'{"sequence":"MKT"}',
        media_type="application/vnd.fs2.scientific-manifest+json",
        direction=ArtifactDirection.INPUT,
    )
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.FAILED,
            completed_at=NOW + timedelta(minutes=2),
        )
    )
    record = await service.commit_run_result(
        RunResultDraft(
            operation_id=operation_id,
            tenant_id=TENANT,
            terminal_status="failed",
            submitted_at=NOW,
            completed_at=NOW + timedelta(minutes=2),
            execution_identity=execution_identity(),
            scheduling_snapshot=scheduling_snapshot(),
            input_manifest_artifact_id=inputs.artifact_id,
            validator_id="proteina-complexa-validator",
            validation_status="not-run",
            error_code="SEMANTIC_VALIDATION_FAILED",
            error_message="designed backbone did not satisfy the clash threshold",
            error_retryable=False,
        )
    )
    RESULT_SCHEMA.validate(record.result.to_document())
    assert record.result.output_manifest is None
    assert record.result.error is not None
    assert record.result.error.retryable is False


# --------------------------------------------------------------------------
# The batch controller's ArtifactCommit contract
# --------------------------------------------------------------------------


async def test_stage_commit_satisfies_the_batch_controller_identity_check() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    shards = ("candidate-0001", "candidate-0002")
    attempts: list[UUID] = []
    entries: list[ManifestEntryDraft] = []
    for index, shard in enumerate(shards):
        attempt_id = await open_attempt(service, operation_id=operation_id, shard_id=shard)
        record = await upload(
            service,
            store,
            operation_id=operation_id,
            attempt_id=attempt_id,
            value=f"ATOM {index}".encode(),
        )
        await service.close_attempt(
            CloseStageAttempt(
                attempt_id=attempt_id,
                operation_id=operation_id,
                tenant_id=TENANT,
                status=AttemptStatus.SUCCEEDED,
                completed_at=NOW + timedelta(minutes=5),
            )
        )
        attempts.append(attempt_id)
        entries.append(
            ManifestEntryDraft(
                name=f"design-{index}", semantic_type="protein.structure/v1", artifact_id=record.artifact_id
            )
        )
    await service.commit_stage(
        CommitStageResult(
            operation_id=operation_id,
            tenant_id=TENANT,
            stage_id="design",
            attempt_ids=tuple(attempts),
            entries=tuple(entries),
            validation_digest="sha256:" + "9" * 64,
            semantic_valid=True,
            committed_at=NOW + timedelta(minutes=6),
            validated_at=NOW + timedelta(minutes=6),
        )
    )
    commit = await service.stage_commit(operation_id, stage_id="design", tenant_id=TENANT)
    assert commit is not None
    assert set(commit.attempt_ids) == set(attempts)
    assert tuple(entry.name for entry in commit.manifest.entries) == ("design-0", "design-1")
    assert commit.semantic_valid is True


async def test_a_commit_that_omits_a_succeeded_attempt_is_rejected() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    kept = await open_attempt(service, operation_id=operation_id, shard_id="candidate-0001")
    other = await open_attempt(service, operation_id=operation_id, shard_id="candidate-0002")
    record = await upload(service, store, operation_id=operation_id, attempt_id=kept, value=b"ATOM  CA")
    for attempt_id in (kept, other):
        await service.close_attempt(
            CloseStageAttempt(
                attempt_id=attempt_id,
                operation_id=operation_id,
                tenant_id=TENANT,
                status=AttemptStatus.SUCCEEDED,
                completed_at=NOW + timedelta(minutes=5),
            )
        )
    with pytest.raises(ArtifactConflictError):
        await service.commit_stage(
            CommitStageResult(
                operation_id=operation_id,
                tenant_id=TENANT,
                stage_id="design",
                attempt_ids=(kept,),
                entries=(
                    ManifestEntryDraft(
                        name="design-0", semantic_type="protein.structure/v1", artifact_id=record.artifact_id
                    ),
                ),
                validation_digest="sha256:" + "9" * 64,
                semantic_valid=True,
                committed_at=NOW,
                validated_at=NOW,
            )
        )


# --------------------------------------------------------------------------
# Content addressing, verification and idempotency
# --------------------------------------------------------------------------


def test_storage_key_binds_tenant_stage_shard_attempt_and_digest() -> None:
    operation_id = UUID("00000000-0000-0000-0000-0000000000ab")
    attempt_id = UUID("00000000-0000-0000-0000-0000000000cd")
    key = artifact_storage_key(
        tenant_id=TENANT,
        operation_id=operation_id,
        stage_id="design",
        shard_id=None,
        attempt_id=attempt_id,
        direction=ArtifactDirection.OUTPUT,
        digest=digest(b"x"),
    )
    assert f"/shards/{NO_SHARD}/" in key
    assert f"/attempts/{attempt_id}/" in key
    assert key.endswith(digest(b"x").removeprefix("sha256:"))
    with pytest.raises(ValueError, match="stage identity"):
        artifact_storage_key(
            tenant_id=TENANT,
            operation_id=operation_id,
            stage_id="Design",
            shard_id=None,
            attempt_id=attempt_id,
            direction=ArtifactDirection.OUTPUT,
            digest=digest(b"x"),
        )


async def test_finalize_is_idempotent_and_returns_one_artifact() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    upload_id = uuid4()
    payload = b"ATOM  CA  ALA A   1"
    request = BeginArtifactUpload(
        upload_id=upload_id,
        attempt_id=attempt_id,
        operation_id=operation_id,
        tenant_id=TENANT,
        direction=ArtifactDirection.OUTPUT,
        expected_digest=digest(payload),
        expected_size_bytes=len(payload),
        media_type="chemical/x-pdb",
    )
    first = await service.begin_upload(request)
    second = await service.begin_upload(request)
    assert first.upload == second.upload
    assert first.handle.url != "" and first.handle is not second.handle
    store.put(first.upload.storage_key, payload, "chemical/x-pdb")
    finalize = FinalizeArtifactUpload(upload_id=upload_id, operation_id=operation_id, tenant_id=TENANT)
    one, two = await asyncio.gather(service.finalize_upload(finalize), service.finalize_upload(finalize))
    assert one.artifact_id == two.artifact_id
    events = await service.list_events(operation_id, tenant_id=TENANT)
    assert [event.event_type for event in events].count(ArtifactEventType.ARTIFACT_FINALIZED) == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"digest": digest(b"different")}, "digest"),
        ({"size_bytes": 9999}, "size"),
        ({"media_type": "text/x-fasta"}, "media type"),
        ({"compression": ArtifactCompression.GZIP}, "compression"),
    ],
)
async def test_finalize_rejects_an_object_that_differs_from_its_intent(mutation: dict[str, Any], message: str) -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    payload = b"ATOM  CA  ALA A   1"
    upload_id = uuid4()
    begun = await service.begin_upload(
        BeginArtifactUpload(
            upload_id=upload_id,
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            direction=ArtifactDirection.OUTPUT,
            expected_digest=digest(payload),
            expected_size_bytes=len(payload),
            media_type="chemical/x-pdb",
        )
    )
    measured = {
        "storage_key": begun.upload.storage_key,
        "digest": digest(payload),
        "size_bytes": len(payload),
        "media_type": "chemical/x-pdb",
        "compression": None,
    }
    store.override = VerifiedStoredObject(**{**measured, **mutation})
    with pytest.raises(ArtifactVerificationError, match=message):
        await service.finalize_upload(
            FinalizeArtifactUpload(upload_id=upload_id, operation_id=operation_id, tenant_id=TENANT)
        )


async def test_media_type_allowlist_and_size_ceiling_are_enforced() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store, max_artifact_bytes=1024)
    attempt_id = await open_attempt(service, operation_id=operation_id)

    def request(media_type: str, size: int) -> BeginArtifactUpload:
        return BeginArtifactUpload(
            upload_id=uuid4(),
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            direction=ArtifactDirection.OUTPUT,
            expected_digest=digest(b"x"),
            expected_size_bytes=size,
            media_type=media_type,
        )

    with pytest.raises(ArtifactPolicyError, match="allowlist"):
        await service.begin_upload(request("application/x-msdownload", 10))
    with pytest.raises(ArtifactPolicyError, match="ceiling"):
        await service.begin_upload(request("chemical/x-pdb", 2048))


async def test_gated_artifacts_carry_a_receipt_and_project_academic_admission() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    receipt = "sha256:" + "7" * 64
    access = ArtifactAccess(profile=ArtifactAccessProfile.ACADEMIC, receipt_digest=receipt)
    payload = b"ATOM  CA  ALA A   1"
    upload_id = uuid4()
    begun = await service.begin_upload(
        BeginArtifactUpload(
            upload_id=upload_id,
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            direction=ArtifactDirection.INPUT,
            expected_digest=digest(payload),
            expected_size_bytes=len(payload),
            media_type="chemical/x-pdb",
            access=access,
        )
    )
    store.put(begun.upload.storage_key, payload, "chemical/x-pdb")
    record = await service.finalize_upload(
        FinalizeArtifactUpload(upload_id=upload_id, operation_id=operation_id, tenant_id=TENANT)
    )
    assert record.access == access
    # A receipt may record the deployment authorization, but ordinary inputs do
    # not need a caller-supplied license receipt.
    admission = access.to_admission().model_dump(mode="json")
    assert admission == {
        "profile": "academic",
        "state": "verified",
        "receipt_digest": "7" * 64,
    }
    assert record.to_public_ref().model_dump(mode="json").get("receipt_digest") is None

    assert ArtifactAccess(profile=ArtifactAccessProfile.ACADEMIC).receipt_digest is None
    with pytest.raises(ValidationError, match="public artifacts cannot"):
        ArtifactAccess(receipt_digest=receipt)


# --------------------------------------------------------------------------
# Fencing
# --------------------------------------------------------------------------


async def test_a_superseded_attempt_cannot_reserve_new_content() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    first = await open_attempt(service, operation_id=operation_id, shard_id="candidate-0001")
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=first,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.PREEMPTED,
            completed_at=NOW + timedelta(minutes=1),
            admission=ADMISSION,
        )
    )
    await open_attempt(service, operation_id=operation_id, shard_id="candidate-0001", attempt_number=2)
    with pytest.raises(StaleArtifactAttemptError):
        await service.begin_upload(
            BeginArtifactUpload(
                upload_id=uuid4(),
                attempt_id=first,
                operation_id=operation_id,
                tenant_id=TENANT,
                direction=ArtifactDirection.OUTPUT,
                expected_digest=digest(b"stale"),
                expected_size_bytes=5,
                media_type="chemical/x-pdb",
            )
        )


async def test_a_terminal_result_fences_every_later_write() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    inputs = await upload(
        service,
        store,
        operation_id=operation_id,
        attempt_id=attempt_id,
        value=b'{"sequence":"MKT"}',
        media_type="application/vnd.fs2.scientific-manifest+json",
        direction=ArtifactDirection.INPUT,
    )
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.CANCELLED,
            completed_at=NOW + timedelta(minutes=1),
        )
    )
    draft = RunResultDraft(
        operation_id=operation_id,
        tenant_id=TENANT,
        terminal_status="cancelled",
        submitted_at=NOW,
        completed_at=NOW + timedelta(minutes=1),
        execution_identity=execution_identity(),
        scheduling_snapshot=scheduling_snapshot(),
        input_manifest_artifact_id=inputs.artifact_id,
        validator_id="proteina-complexa-validator",
        validation_status="not-run",
    )
    first = await service.commit_run_result(draft)
    again = await service.commit_run_result(draft)
    assert first.result_digest == again.result_digest

    with pytest.raises(ResultAlreadyTerminalError):
        await open_attempt(service, operation_id=operation_id, stage_id="score")


async def test_a_second_different_terminal_result_is_refused() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    inputs = await upload(
        service,
        store,
        operation_id=operation_id,
        attempt_id=attempt_id,
        value=b'{"sequence":"MKT"}',
        media_type="application/vnd.fs2.scientific-manifest+json",
        direction=ArtifactDirection.INPUT,
    )
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.CANCELLED,
            completed_at=NOW + timedelta(minutes=1),
        )
    )

    def draft(status: str) -> RunResultDraft:
        return RunResultDraft(
            operation_id=operation_id,
            tenant_id=TENANT,
            terminal_status=status,  # type: ignore[arg-type]
            submitted_at=NOW,
            completed_at=NOW + timedelta(minutes=1),
            execution_identity=execution_identity(),
            scheduling_snapshot=scheduling_snapshot(),
            input_manifest_artifact_id=inputs.artifact_id,
            validator_id="proteina-complexa-validator",
            validation_status="not-run",
            **(
                {}
                if status == "cancelled"
                else {
                    "error_code": "RUN_FAILED",
                    "error_message": "the run failed",
                    "error_retryable": True,
                }
            ),
        )

    await service.commit_run_result(draft("cancelled"))
    with pytest.raises(ResultAlreadyTerminalError):
        await service.commit_run_result(draft("failed"))


async def test_only_one_stage_commit_survives_concurrent_writers() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=b"ATOM  CA")
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.SUCCEEDED,
            completed_at=NOW + timedelta(minutes=5),
        )
    )
    request = CommitStageResult(
        operation_id=operation_id,
        tenant_id=TENANT,
        stage_id="design",
        attempt_ids=(attempt_id,),
        entries=(
            ManifestEntryDraft(name="design-0", semantic_type="protein.structure/v1", artifact_id=record.artifact_id),
        ),
        validation_digest="sha256:" + "9" * 64,
        semantic_valid=True,
        committed_at=NOW,
        validated_at=NOW,
    )
    results = await asyncio.gather(*(service.commit_stage(request) for _ in range(8)))
    assert len({item.manifest_digest for item in results}) == 1
    events = await service.list_events(operation_id, tenant_id=TENANT)
    assert [event.event_type for event in events].count(ArtifactEventType.STAGE_COMMITTED) == 1


async def test_a_foreign_tenant_cannot_read_another_tenants_artifact() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=b"ATOM  CA")
    with pytest.raises(ArtifactNotFoundError):
        await service.download(record.artifact_id, tenant_id="tenant-b")


# --------------------------------------------------------------------------
# Handles and privacy
# --------------------------------------------------------------------------


async def test_handles_are_short_lived_write_once_and_never_persisted() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    payload = b"ATOM  CA"
    upload_id = uuid4()
    begun = await service.begin_upload(
        BeginArtifactUpload(
            upload_id=upload_id,
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            direction=ArtifactDirection.OUTPUT,
            expected_digest=digest(payload),
            expected_size_bytes=len(payload),
            media_type="chemical/x-pdb",
        )
    )
    assert begun.handle.write_once is True
    assert begun.handle.expires_at <= NOW + MAX_HANDLE_TTL + HANDLE_CLOCK_SKEW
    assert "X-Amz-Signature" not in repr(begun.handle)
    assert begun.handle.url not in begun.upload.model_dump_json()
    with pytest.raises(TypeError):
        begun.handle.headers["content-type"] = "text/plain"  # type: ignore[index]

    with pytest.raises(ArtifactPolicyError, match="lifetime"):
        await service.begin_upload(
            BeginArtifactUpload(
                upload_id=uuid4(),
                attempt_id=attempt_id,
                operation_id=operation_id,
                tenant_id=TENANT,
                direction=ArtifactDirection.OUTPUT,
                expected_digest=digest(b"other"),
                expected_size_bytes=5,
                media_type="chemical/x-pdb",
            ),
            handle_ttl=timedelta(hours=2),
        )


async def test_no_durable_record_or_log_line_carries_bearer_material(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    with caplog.at_level(logging.DEBUG):
        attempt_id = await open_attempt(service, operation_id=operation_id)
        record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=b"ATOM  CA")
        download = await service.download(record.artifact_id, tenant_id=TENANT)
    serialized = json.dumps(
        {
            "artifact": record.model_dump(mode="json"),
            "events": [
                event.model_dump(mode="json") for event in await service.list_events(operation_id, tenant_id=TENANT)
            ],
        }
    )
    for secret in ("X-Amz-Signature", "X-Amz-Credential", download.handle.url):
        assert secret not in serialized
    assert not [line for line in caplog.text.splitlines() if "X-Amz" in line]


def test_migration_is_payload_free_and_fences_every_write_path() -> None:
    sql = (CONTROL_ROOT / "migrations" / SCIENTIFIC_ARTIFACT_MIGRATION).read_text(encoding="utf-8")
    for fence in (
        "fs2_scientific_assert_writable",
        "fs2_scientific_assert_live_attempt",
        "fs2_scientific_validate_attempt_transition",
        "fs2_scientific_validate_upload_transition",
        "fs2_scientific_reject_mutation",
        "fs2_scientific_guard_retention_delete",
    ):
        assert fence in sql
    assert "REVOKE ALL ON fs2_scientific_stage_attempts" in sql
    # Scan identifiers and types only. Comments and quoted literals are prose,
    # and the file's own prose is what documents these exclusions.
    identifiers = re.sub(
        r"'(?:[^']|'')*'",
        "''",
        "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--")),
    ).lower()
    for forbidden in (" bytea", "presigned", "signature", " url ", "secret", "detail jsonb"):
        assert forbidden not in identifiers, forbidden
    assert "fs2-serve.nebius.ai/scientific-run-result/v1" in sql
    assert "fs2-serve.nebius.ai/scientific-artifact-manifest/v1" in sql


def test_settings_reject_an_insecure_artifact_store() -> None:
    from fs2_serve.settings import Settings

    with pytest.raises(ValidationError):
        Settings(
            scientific_artifacts_enabled=True,
            artifact_store_endpoint="http://storage.invalid",
            artifact_store_verify_tls=True,
        )
    relaxed = Settings(
        scientific_artifacts_enabled=True,
        artifact_store_endpoint="http://127.0.0.1:9000",
        artifact_store_verify_tls=False,
        allow_non_cluster_urls=True,
    )
    assert "chemical/x-pdb" in relaxed.artifact_media_types_set()


def test_artifact_store_credentials_come_from_a_mounted_secret(tmp_path: Path) -> None:
    from fs2_serve.settings import Settings

    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"access_key_id": "AKIA", "secret_access_key": "s" * 24}), encoding="utf-8")
    settings = Settings(artifact_store_credentials_file=path)
    assert settings.artifact_store_credentials() == ("AKIA", "s" * 24)
    path.write_text(json.dumps({"access_key_id": "AKIA"}), encoding="utf-8")
    with pytest.raises(ValueError, match="secret_access_key"):
        settings.artifact_store_credentials()


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


async def test_retention_deletes_objects_then_metadata_and_records_evidence() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store, retention=timedelta(days=1))
    attempt_id = await open_attempt(service, operation_id=operation_id)
    inputs = await upload(
        service,
        store,
        operation_id=operation_id,
        attempt_id=attempt_id,
        value=b'{"sequence":"MKT"}',
        media_type="application/vnd.fs2.scientific-manifest+json",
        direction=ArtifactDirection.INPUT,
    )
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.CANCELLED,
            completed_at=NOW + timedelta(minutes=1),
        )
    )
    await service.commit_run_result(
        RunResultDraft(
            operation_id=operation_id,
            tenant_id=TENANT,
            terminal_status="cancelled",
            submitted_at=NOW,
            completed_at=NOW + timedelta(minutes=1),
            execution_identity=execution_identity(),
            scheduling_snapshot=scheduling_snapshot(),
            input_manifest_artifact_id=inputs.artifact_id,
            validator_id="proteina-complexa-validator",
            validation_status="not-run",
        )
    )
    assert await service.purge_expired() == []

    expired = ScientificArtifactService(
        repository=repository,
        object_store=store,
        allowed_media_types=ALLOWED_MEDIA_TYPES,
        clock=lambda: NOW + timedelta(days=2),
    )
    purges = await expired.purge_expired()
    assert len(purges) == 1
    assert purges[0].artifact_count == 1
    assert purges[0].byte_count == len(b'{"sequence":"MKT"}')
    assert inputs.storage_key in store.deleted
    with pytest.raises(ArtifactNotFoundError):
        await service.download(inputs.artifact_id, tenant_id=TENANT)
    assert await expired.purge_expired() == []


# --------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------

TRUNCATE = """
TRUNCATE fs2_scientific_retention_ledger,fs2_scientific_artifact_events,
    fs2_scientific_stage_commit_attempts,fs2_scientific_stage_commits,
    fs2_scientific_run_results,fs2_scientific_uploads,fs2_scientific_artifacts,
    fs2_scientific_stage_attempts,fs2_operation_events,fs2_usage_facts,
    fs2_operations,fs2_tokens RESTART IDENTITY CASCADE
"""


async def insert_operation(pool: asyncpg.Pool, operation_id: UUID, *, attempt: int = 1) -> None:
    token_id = uuid4()
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO fs2_tokens(
                id,prefix,pepper_key_id,digest,principal_id,tenant_id,scopes,models,
                max_concurrency,created_by
            ) VALUES($1,$2,'pepper-v1',$3,'principal-a','tenant-a',$4,$5,4,'test')
            """,
            token_id,
            f"fst_{token_id.hex[:12]}",
            "d" * 64,
            ["inference.invoke", "artifacts.write"],
            ["proteina-complexa"],
        )
        await connection.execute(
            """
            INSERT INTO fs2_operations(
                id,tenant_id,principal_id,token_id,model_id,model_revision,protocol,
                operation,idempotency_key,request_hmac_key_id,request_hmac,
                request_content_type,payload_expires_at,max_attempts,attempt
            ) VALUES($1,'tenant-a','principal-a',$2,'proteina-complexa','revision-a',
                'scientific-batch','design',$5,'ledger-v1',$3,
                'application/json',clock_timestamp()+interval '1 hour',3,$4)
            """,
            operation_id,
            token_id,
            "e" * 64,
            attempt,
            f"scientific-{operation_id}",
        )


@pytest_asyncio.fixture
async def postgres_store():
    database_url = os.environ.get("FS2_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("FS2_TEST_DATABASE_URL is not set")
    store = await PostgresStore.connect(
        database_url,
        CONTROL_ROOT / "migrations",
        PayloadCipher(active_key_id="payload-v1", keys={"payload-v1": b"p" * 32}),
        KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"h" * 32}),
        payload_ttl_seconds=3600,
        min_size=2,
        max_size=8,
    )
    await store.migrate()
    async with store.pool.acquire() as connection:
        await connection.execute(TRUNCATE)
    try:
        yield store
    finally:
        async with store.pool.acquire() as connection:
            await connection.execute(TRUNCATE)
        await store.close()


@pytest_asyncio.fixture
async def runtime_pool(postgres_store):
    """A pool restricted to the least-privilege runtime role, as in production."""

    database_url = os.environ["FS2_TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)

    async def assume_runtime_role(connection) -> None:
        await connection.execute("SET ROLE fs2_serve_runtime")

    pool = await asyncpg.create_pool(dsn=database_url, min_size=2, max_size=8, init=assume_runtime_role)
    assert pool is not None
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.postgres
async def test_postgres_enforces_attempt_terminal_and_immutability_fences(runtime_pool) -> None:
    operation_id = uuid4()
    await insert_operation(runtime_pool, operation_id)
    store = FakeObjectStore()
    service = build_service(PostgresArtifactRepository(runtime_pool), store)

    first = await open_attempt(service, operation_id=operation_id, shard_id="candidate-0001")
    record = await upload(service, store, operation_id=operation_id, attempt_id=first, value=b"ATOM  CA  ALA")
    assert record.storage_key.startswith(f"scientific/v1/tenants/{TENANT}/operations/{operation_id}/")

    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=first,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.PREEMPTED,
            completed_at=NOW + timedelta(minutes=1),
            admission=ADMISSION,
        )
    )
    await open_attempt(service, operation_id=operation_id, shard_id="candidate-0001", attempt_number=2)
    with pytest.raises(StaleArtifactAttemptError):
        await service.begin_upload(
            BeginArtifactUpload(
                upload_id=uuid4(),
                attempt_id=first,
                operation_id=operation_id,
                tenant_id=TENANT,
                direction=ArtifactDirection.OUTPUT,
                expected_digest=digest(b"stale"),
                expected_size_bytes=5,
                media_type="chemical/x-pdb",
            )
        )

    async with runtime_pool.acquire() as connection:
        # The runtime role has no UPDATE on artifacts at all, so the grant refuses
        # the write before the immutability trigger is even reached.
        with pytest.raises(asyncpg.PostgresError) as immutable:
            await connection.execute("UPDATE fs2_scientific_artifacts SET size_bytes=1 WHERE id=$1", record.artifact_id)
        assert immutable.value.sqlstate == "42501"
        # DELETE is granted, so here the retention trigger is what refuses.
        with pytest.raises(asyncpg.PostgresError) as guarded:
            await connection.execute("DELETE FROM fs2_scientific_artifacts WHERE id=$1", record.artifact_id)
        assert guarded.value.sqlstate == "FS202"


@pytest.mark.postgres
async def test_postgres_commits_exactly_one_stage_manifest_under_contention(runtime_pool) -> None:
    operation_id = uuid4()
    await insert_operation(runtime_pool, operation_id)
    store = FakeObjectStore()
    service = build_service(PostgresArtifactRepository(runtime_pool), store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=b"ATOM  CA  ALA")
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.SUCCEEDED,
            completed_at=NOW + timedelta(minutes=5),
        )
    )
    request = CommitStageResult(
        operation_id=operation_id,
        tenant_id=TENANT,
        stage_id="design",
        attempt_ids=(attempt_id,),
        entries=(
            ManifestEntryDraft(name="design-0", semantic_type="protein.structure/v1", artifact_id=record.artifact_id),
        ),
        validation_digest="sha256:" + "9" * 64,
        semantic_valid=True,
        committed_at=NOW + timedelta(minutes=6),
        validated_at=NOW + timedelta(minutes=6),
    )
    outcomes = await asyncio.gather(*(service.commit_stage(request) for _ in range(6)), return_exceptions=True)
    committed = [item for item in outcomes if not isinstance(item, BaseException)]
    assert committed, outcomes
    assert len({item.manifest_digest for item in committed}) == 1

    async with runtime_pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM fs2_scientific_stage_commits WHERE operation_id=$1", operation_id
            )
            == 1
        )
    commit = await service.artifact_commit(operation_id, stage_id="design", tenant_id=TENANT)
    assert commit is not None
    assert commit.attempt_ids == (attempt_id,)
    assert commit.semantic_valid is True


@pytest.mark.postgres
async def test_postgres_terminal_result_fences_writes_and_retention_purges(runtime_pool) -> None:
    operation_id = uuid4()
    await insert_operation(runtime_pool, operation_id)
    store = FakeObjectStore()
    repository = PostgresArtifactRepository(runtime_pool)
    service = build_service(repository, store, retention=timedelta(days=1))
    attempt_id = await open_attempt(service, operation_id=operation_id)
    inputs = await upload(
        service,
        store,
        operation_id=operation_id,
        attempt_id=attempt_id,
        value=b'{"sequence":"MKT"}',
        media_type="application/vnd.fs2.scientific-manifest+json",
        direction=ArtifactDirection.INPUT,
    )
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.CANCELLED,
            completed_at=NOW + timedelta(minutes=1),
        )
    )
    committed = await service.commit_run_result(
        RunResultDraft(
            operation_id=operation_id,
            tenant_id=TENANT,
            terminal_status="cancelled",
            submitted_at=NOW,
            completed_at=NOW + timedelta(minutes=1),
            execution_identity=execution_identity(),
            scheduling_snapshot=scheduling_snapshot(),
            input_manifest_artifact_id=inputs.artifact_id,
            validator_id="proteina-complexa-validator",
            validation_status="not-run",
        )
    )
    RESULT_SCHEMA.validate(committed.result.to_document())
    reloaded = await service.get_run_result(operation_id, tenant_id=TENANT)
    assert reloaded.result_digest == committed.result_digest
    assert reloaded.result == committed.result

    with pytest.raises(ResultAlreadyTerminalError):
        await open_attempt(service, operation_id=operation_id, stage_id="score")

    expired = ScientificArtifactService(
        repository=repository,
        object_store=store,
        allowed_media_types=ALLOWED_MEDIA_TYPES,
        clock=lambda: NOW + timedelta(days=3),
    )
    purges = await expired.purge_expired()
    assert [purge.operation_id for purge in purges] == [operation_id]
    assert purges[0].artifact_count == 1
    assert inputs.storage_key in store.deleted
    assert await expired.purge_expired() == []
    async with runtime_pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM fs2_scientific_artifacts WHERE operation_id=$1", operation_id
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT artifact_count FROM fs2_scientific_retention_ledger WHERE operation_id=$1",
                operation_id,
            )
            == 1
        )


@pytest.mark.postgres
async def test_postgres_events_are_durable_ordered_and_payload_free(runtime_pool) -> None:
    operation_id = uuid4()
    await insert_operation(runtime_pool, operation_id)
    store = FakeObjectStore()
    service = build_service(PostgresArtifactRepository(runtime_pool), store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=b"ATOM  CA")
    events = await service.list_events(operation_id, tenant_id=TENANT)
    assert [event.event_type for event in events] == [
        ArtifactEventType.ATTEMPT_OPENED,
        ArtifactEventType.UPLOAD_BEGUN,
        ArtifactEventType.ARTIFACT_FINALIZED,
    ]
    assert [event.event_id for event in events] == sorted(event.event_id for event in events)
    page = await service.list_events(operation_id, tenant_id=TENANT, after_id=events[0].event_id, limit=1)
    assert len(page) == 1 and page[0].event_id == events[1].event_id
    assert await service.list_events(operation_id, tenant_id="tenant-b") == []


async def _terminal_operation(service: ScientificArtifactService, repository, store) -> UUID:
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    inputs = await upload(
        service,
        store,
        operation_id=operation_id,
        attempt_id=attempt_id,
        value=b'{"sequence":"MKT"}',
        media_type="application/vnd.fs2.scientific-manifest+json",
        direction=ArtifactDirection.INPUT,
    )
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.CANCELLED,
            completed_at=NOW + timedelta(minutes=1),
        )
    )
    await service.commit_run_result(
        RunResultDraft(
            operation_id=operation_id,
            tenant_id=TENANT,
            terminal_status="cancelled",
            submitted_at=NOW,
            completed_at=NOW + timedelta(minutes=1),
            execution_identity=execution_identity(),
            scheduling_snapshot=scheduling_snapshot(),
            input_manifest_artifact_id=inputs.artifact_id,
            validator_id="proteina-complexa-validator",
            validation_status="not-run",
        )
    )
    return operation_id


async def test_concurrent_retention_workers_purge_an_operation_exactly_once() -> None:
    repository = MemoryArtifactRepository()
    store = FakeObjectStore()
    service = build_service(repository, store, retention=timedelta(days=1))
    operation_id = await _terminal_operation(service, repository, store)

    def worker() -> ScientificArtifactService:
        return ScientificArtifactService(
            repository=repository,
            object_store=store,
            allowed_media_types=ALLOWED_MEDIA_TYPES,
            clock=lambda: NOW + timedelta(days=2),
        )

    outcomes = await asyncio.gather(*(worker().purge_expired() for _ in range(4)))
    purged = [purge for batch in outcomes for purge in batch]
    assert [purge.operation_id for purge in purged] == [operation_id]


async def test_a_zero_byte_artifact_round_trips_without_relaxing_the_ceiling() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=b"")
    assert record.size_bytes == 0
    assert record.digest == digest(b"")
    POINTER_SCHEMA.validate(record.to_public_ref().model_dump(mode="json"))


async def test_controller_port_lists_attempt_artifacts_and_canonical_stage_commit() -> None:
    repository = MemoryArtifactRepository()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id=TENANT)
    store = FakeObjectStore()
    service = build_service(repository, store)
    attempt_id = await open_attempt(service, operation_id=operation_id)
    record = await upload(service, store, operation_id=operation_id, attempt_id=attempt_id, value=b"ATOM  CA")
    await service.close_attempt(
        CloseStageAttempt(
            attempt_id=attempt_id,
            operation_id=operation_id,
            tenant_id=TENANT,
            status=AttemptStatus.SUCCEEDED,
            completed_at=NOW + timedelta(minutes=5),
        )
    )
    published = await service.commit_stage(
        CommitStageResult(
            operation_id=operation_id,
            tenant_id=TENANT,
            stage_id="design",
            attempt_ids=(attempt_id,),
            entries=(
                ManifestEntryDraft(
                    name="design-0", semantic_type="protein.structure/v1", artifact_id=record.artifact_id
                ),
            ),
            validation_digest="sha256:" + "9" * 64,
            semantic_valid=True,
            committed_at=NOW,
            validated_at=NOW,
        )
    )
    records = await service.list_artifacts(
        operation_id,
        tenant_id=TENANT,
        stage_id="design",
        attempt_id=attempt_id,
    )
    assert records == [record]
    assert await service.stage_commit(operation_id, stage_id="design", tenant_id=TENANT) == published
    assert await service.stage_commit(operation_id, stage_id="score", tenant_id=TENANT) is None
