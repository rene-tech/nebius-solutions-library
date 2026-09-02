from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
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
    MAX_HANDLE_TTL,
    SCIENTIFIC_ARTIFACT_MIGRATION,
    SCIENTIFIC_ARTIFACT_ROLLBACK_SQL,
    ArtifactAccess,
    ArtifactAccessProfile,
    ArtifactCompression,
    ArtifactConflictError,
    ArtifactDirection,
    ArtifactEventType,
    ArtifactPolicyError,
    ArtifactVerificationError,
    BeginArtifactUpload,
    EphemeralHandle,
    ExecutionProvenance,
    FinalizeArtifactUpload,
    MemoryArtifactRepository,
    PostgresArtifactRepository,
    ScientificArtifactService,
    SemanticValidation,
    SemanticValidationStatus,
    StaleArtifactAttemptError,
    TerminalResultDraft,
    TerminalResultManifest,
    TerminalResultStatus,
    VerifiedStoredObject,
    artifact_storage_key,
    result_manifest_digest,
)
from fs2_serve.scientific_batch.postgres_repository import SCIENTIFIC_BATCH_ROLLBACK_SQL

NOW = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
ALLOWED_MEDIA_TYPES = {"application/json", "chemical/x-pdb", "text/x-fasta"}
PUBLIC_ARTIFACT_SCHEMA = json.loads(
    (CONTROL_ROOT.parents[1] / "catalog/runtime/schema/scientific-artifact-pointer.schema.json").read_text(
        encoding="utf-8"
    )
)


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class MeasuringObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, ArtifactCompression | None]] = {}
        self.override: VerifiedStoredObject | None = None

    def put(
        self,
        storage_key: str,
        value: bytes,
        media_type: str,
        compression: ArtifactCompression | None = None,
    ) -> None:
        self.objects[storage_key] = (value, media_type, compression)

    async def inspect(self, storage_key: str) -> VerifiedStoredObject:
        if self.override is not None:
            return self.override
        value, media_type, compression = self.objects[storage_key]
        return VerifiedStoredObject(
            storage_key=storage_key,
            digest=digest(value),
            size_bytes=len(value),
            media_type=media_type,
            compression=compression,
        )


class SecretHandleSigner:
    def __init__(self, *, extra_ttl: timedelta = timedelta(), write_once: bool = True) -> None:
        self.extra_ttl = extra_ttl
        self.write_once = write_once
        self.signed: list[tuple[str, str]] = []

    async def issue_upload(
        self,
        *,
        storage_key: str,
        media_type: str,
        compression: ArtifactCompression | None,
        expected_digest: str,
        expires_at: datetime,
    ) -> EphemeralHandle:
        del compression, expected_digest
        self.signed.append(("PUT", storage_key))
        return EphemeralHandle(
            method="PUT",
            url=f"https://objects.example.test/{storage_key}?signature=SIGNED_UPLOAD_SECRET",
            expires_at=expires_at + self.extra_ttl,
            write_once=self.write_once,
            headers={"x-upload-token": "UPLOAD_HEADER_SECRET", "content-type": media_type},
        )

    async def issue_download(self, *, storage_key: str, expires_at: datetime) -> EphemeralHandle:
        self.signed.append(("GET", storage_key))
        return EphemeralHandle(
            method="GET",
            url=f"https://objects.example.test/{storage_key}?signature=SIGNED_DOWNLOAD_SECRET",
            expires_at=expires_at + self.extra_ttl,
            headers={"x-download-token": "DOWNLOAD_HEADER_SECRET"},
        )


class LeakyBoundarySigner(SecretHandleSigner):
    async def issue_upload(
        self,
        *,
        storage_key: str,
        media_type: str,
        compression: ArtifactCompression | None,
        expected_digest: str,
        expires_at: datetime,
    ) -> EphemeralHandle:
        del storage_key, media_type, compression, expected_digest, expires_at
        raise RuntimeError("SIGNER_CREDENTIAL_MUST_BE_SUPPRESSED")

    async def issue_download(self, *, storage_key: str, expires_at: datetime) -> EphemeralHandle:
        del storage_key, expires_at
        raise RuntimeError("SIGNED_DOWNLOAD_LOCATION_MUST_BE_SUPPRESSED")


class LeakyBoundaryObjectStore(MeasuringObjectStore):
    async def inspect(self, storage_key: str) -> VerifiedStoredObject:
        del storage_key
        raise RuntimeError("BIOLOGICAL_OBJECT_BYTES_MUST_BE_SUPPRESSED")


async def memory_service(
    *,
    attempt: int = 1,
    signer: SecretHandleSigner | None = None,
) -> tuple[ScientificArtifactService, MemoryArtifactRepository, MeasuringObjectStore, UUID]:
    repository = MemoryArtifactRepository(clock=lambda: NOW)
    object_store = MeasuringObjectStore()
    operation_id = uuid4()
    await repository.register_operation(operation_id, tenant_id="tenant-a", attempt=attempt)
    service = ScientificArtifactService(
        repository=repository,
        object_store=object_store,
        signer=signer or SecretHandleSigner(),
        allowed_media_types=ALLOWED_MEDIA_TYPES,
        clock=lambda: NOW,
    )
    return service, repository, object_store, operation_id


async def upload_artifact(
    service: ScientificArtifactService,
    object_store: MeasuringObjectStore,
    *,
    operation_id: UUID,
    attempt: int,
    direction: ArtifactDirection,
    value: bytes,
    media_type: str,
    compression: ArtifactCompression | None = None,
    access: ArtifactAccess | None = None,
):
    begin = BeginArtifactUpload(
        upload_id=uuid4(),
        operation_id=operation_id,
        tenant_id="tenant-a",
        attempt=attempt,
        direction=direction,
        expected_digest=digest(value),
        expected_size_bytes=len(value),
        media_type=media_type,
        compression=compression,
        access=access or ArtifactAccess(),
    )
    started = await service.begin_upload(begin, handle_ttl=timedelta(minutes=2))
    object_store.put(started.upload.storage_key, value, media_type, compression)
    artifact = await service.finalize_upload(
        FinalizeArtifactUpload(
            upload_id=begin.upload_id,
            operation_id=operation_id,
            tenant_id="tenant-a",
            attempt=attempt,
        )
    )
    return begin, started, artifact


def result_draft(
    operation_id: UUID,
    output,
    *,
    model_revision: str = "revision-a",
) -> TerminalResultDraft:
    return TerminalResultDraft(
        operation_id=operation_id,
        tenant_id="tenant-a",
        attempt=output.attempt,
        status=TerminalResultStatus.SUCCEEDED,
        output_artifacts=(output,),
        provenance=ExecutionProvenance(
            model_id="proteina-complexa",
            model_revision=model_revision,
            runtime_image_digest="sha256:" + "1" * 64,
            workload_spec_digest="sha256:" + "2" * 64,
            scheduling_snapshot_digest="sha256:" + "3" * 64,
            job_uid="job-uid-1",
            pod_uids=("pod-uid-1",),
            started_at=NOW - timedelta(minutes=1),
            completed_at=NOW,
        ),
        validation=SemanticValidation(
            validator_id="complexa-structure-validator",
            validator_revision="sha256:" + "4" * 64,
            status=SemanticValidationStatus.PASSED,
            evidence_artifact=output,
        ),
        completed_at=NOW,
    )


@pytest.mark.asyncio
async def test_content_address_finalize_and_handles_stay_ephemeral_and_payload_free(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    service, repository, object_store, operation_id = await memory_service()
    biological_payload = b">neoantigen\nMKWVTFISLLFLFSSAYSRGVFRR"
    access = ArtifactAccess(
        profile=ArtifactAccessProfile.ACADEMIC,
        receipt_digest="sha256:" + "a" * 64,
    )

    begin, started, artifact = await upload_artifact(
        service,
        object_store,
        operation_id=operation_id,
        attempt=1,
        direction=ArtifactDirection.INPUT,
        value=biological_payload,
        media_type="text/x-fasta",
        access=access,
    )

    expected_key = artifact_storage_key(
        tenant_id="tenant-a",
        operation_id=operation_id,
        attempt=1,
        direction=ArtifactDirection.INPUT,
        digest=digest(biological_payload),
    )
    assert artifact.storage_key == expected_key
    assert artifact.digest == digest(biological_payload)
    assert artifact.size_bytes == len(biological_payload)
    assert artifact.access == access
    assert artifact.schema_version == "fs2-serve.nebius.ai/scientific-artifact-record/v1"
    assert started.upload.upload_id == begin.upload_id
    assert "SIGNED_UPLOAD_SECRET" not in repr(started)
    assert "UPLOAD_HEADER_SECRET" not in repr(started)

    download = await service.download(artifact.artifact_id, tenant_id="tenant-a", handle_ttl=timedelta(minutes=1))
    assert download.handle.method == "GET"
    assert download.handle.expires_at == NOW + timedelta(minutes=1)
    assert "SIGNED_DOWNLOAD_SECRET" not in repr(download)
    assert "DOWNLOAD_HEADER_SECRET" not in repr(download)

    public = artifact.to_public_ref().model_dump(mode="json", exclude_none=True)
    assert public == {
        "artifact_id": str(artifact.artifact_id),
        "sha256": digest(biological_payload).removeprefix("sha256:"),
        "size_bytes": len(biological_payload),
        "media_type": "text/x-fasta",
    }
    Draft202012Validator(PUBLIC_ARTIFACT_SCHEMA).validate(public)
    serialized_public = json.dumps(public, sort_keys=True)
    for internal_field in ("tenant_id", "attempt", "storage_key", "access", "receipt_digest"):
        assert internal_field not in serialized_public

    events = await repository.list_events(operation_id, tenant_id="tenant-a")
    durable = json.dumps([event.model_dump(mode="json") for event in events], sort_keys=True)
    assert [event.event_type for event in events] == [
        ArtifactEventType.UPLOAD_BEGUN,
        ArtifactEventType.ARTIFACT_FINALIZED,
    ]
    for secret in (
        biological_payload.decode(),
        "SIGNED_UPLOAD_SECRET",
        "SIGNED_DOWNLOAD_SECRET",
        "UPLOAD_HEADER_SECRET",
        "DOWNLOAD_HEADER_SECRET",
    ):
        assert secret not in durable
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_public_projection_includes_only_optional_compression_metadata() -> None:
    service, _, object_store, operation_id = await memory_service()
    body = b"compressed-object-bytes"
    _, _, artifact = await upload_artifact(
        service,
        object_store,
        operation_id=operation_id,
        attempt=1,
        direction=ArtifactDirection.OUTPUT,
        value=body,
        media_type="application/json",
        compression=ArtifactCompression.GZIP,
    )
    public = artifact.to_public_ref().model_dump(mode="json", exclude_none=True)
    assert public == {
        "artifact_id": str(artifact.artifact_id),
        "sha256": digest(body).removeprefix("sha256:"),
        "size_bytes": len(body),
        "media_type": "application/json",
        "compression": "gzip",
    }
    Draft202012Validator(PUBLIC_ARTIFACT_SCHEMA).validate(public)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["key", "digest", "size", "media_type", "compression"])
async def test_finalize_rejects_every_storage_identity_mismatch_without_reflecting_values(
    caplog, mismatch: str
) -> None:
    caplog.set_level(logging.DEBUG)
    service, _, object_store, operation_id = await memory_service()
    body = b"BIOLOGICAL_PAYLOAD_MUST_NOT_BE_REFLECTED"
    request = BeginArtifactUpload(
        upload_id=uuid4(),
        operation_id=operation_id,
        tenant_id="tenant-a",
        attempt=1,
        direction=ArtifactDirection.OUTPUT,
        expected_digest=digest(body),
        expected_size_bytes=len(body),
        media_type="chemical/x-pdb",
    )
    started = await service.begin_upload(request)
    observed = {
        "storage_key": started.upload.storage_key,
        "digest": digest(body),
        "size_bytes": len(body),
        "media_type": "chemical/x-pdb",
        "compression": None,
    }
    replacements = {
        "key": "scientific/v1/other-object",
        "digest": "sha256:" + "f" * 64,
        "size": len(body) + 1,
        "media_type": "application/json",
        "compression": ArtifactCompression.GZIP,
    }
    field = "storage_key" if mismatch == "key" else ("size_bytes" if mismatch == "size" else mismatch)
    observed[field] = replacements[mismatch]
    object_store.override = VerifiedStoredObject(**observed)

    with pytest.raises(ArtifactVerificationError) as raised:
        await service.finalize_upload(
            FinalizeArtifactUpload(
                upload_id=request.upload_id,
                operation_id=operation_id,
                tenant_id="tenant-a",
                attempt=1,
            )
        )
    assert body.decode() not in str(raised.value)
    assert body.decode() not in caplog.text


@pytest.mark.asyncio
async def test_policy_rejects_unlisted_media_oversize_and_overlong_signatures() -> None:
    signer = SecretHandleSigner(extra_ttl=timedelta(seconds=1))
    service, _, _, operation_id = await memory_service(signer=signer)
    request = BeginArtifactUpload(
        upload_id=uuid4(),
        operation_id=operation_id,
        tenant_id="tenant-a",
        attempt=1,
        direction=ArtifactDirection.INPUT,
        expected_digest=digest(b"{}"),
        expected_size_bytes=2,
        media_type="application/json",
    )
    with pytest.raises(ArtifactPolicyError, match="short-lived HTTPS policy"):
        await service.begin_upload(request, handle_ttl=MAX_HANDLE_TTL)

    unsafe_service, _, _, unsafe_operation = await memory_service(signer=SecretHandleSigner(write_once=False))
    with pytest.raises(ArtifactPolicyError, match="short-lived HTTPS policy"):
        await unsafe_service.begin_upload(request.model_copy(update={"operation_id": unsafe_operation}))

    valid_signer = SecretHandleSigner()
    bounded = ScientificArtifactService(
        repository=service.repository,
        object_store=service.object_store,
        signer=valid_signer,
        allowed_media_types={"application/json"},
        max_artifact_bytes=1,
        clock=lambda: NOW,
    )
    with pytest.raises(ArtifactPolicyError, match="size bound"):
        await bounded.begin_upload(request)

    disallowed = request.model_copy(update={"upload_id": uuid4(), "media_type": "chemical/x-pdb"})
    with pytest.raises(ArtifactPolicyError, match="not allowlisted"):
        await bounded.begin_upload(disallowed)


@pytest.mark.asyncio
async def test_finalized_content_address_never_mints_another_upload_handle() -> None:
    signer = SecretHandleSigner()
    service, _, object_store, operation_id = await memory_service(signer=signer)
    body = b"immutable-output"
    begin, _, _ = await upload_artifact(
        service,
        object_store,
        operation_id=operation_id,
        attempt=1,
        direction=ArtifactDirection.OUTPUT,
        value=body,
        media_type="application/json",
    )
    signed_before = list(signer.signed)
    with pytest.raises(ArtifactConflictError, match="already finalized"):
        await service.begin_upload(begin)
    assert signer.signed == signed_before

    duplicate_address = begin.model_copy(update={"upload_id": uuid4()})
    with pytest.raises(ArtifactConflictError, match="content address"):
        await service.begin_upload(duplicate_address)


@pytest.mark.asyncio
async def test_storage_and_signer_failures_drop_secret_exception_context(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    service, repository, _, operation_id = await memory_service(signer=LeakyBoundarySigner())
    request = BeginArtifactUpload(
        upload_id=uuid4(),
        operation_id=operation_id,
        tenant_id="tenant-a",
        attempt=1,
        direction=ArtifactDirection.INPUT,
        expected_digest=digest(b"private-sequence"),
        expected_size_bytes=len(b"private-sequence"),
        media_type="text/x-fasta",
    )
    with pytest.raises(ArtifactPolicyError, match="handle generation failed") as signer_error:
        await service.begin_upload(request)
    assert signer_error.value.__cause__ is None
    assert "SIGNER_CREDENTIAL_MUST_BE_SUPPRESSED" not in str(signer_error.value)

    retry_service = ScientificArtifactService(
        repository=repository,
        object_store=LeakyBoundaryObjectStore(),
        signer=SecretHandleSigner(),
        allowed_media_types=ALLOWED_MEDIA_TYPES,
        clock=lambda: NOW,
    )
    await retry_service.begin_upload(request)
    with pytest.raises(ArtifactVerificationError, match="inspection failed") as store_error:
        await retry_service.finalize_upload(
            FinalizeArtifactUpload(
                upload_id=request.upload_id,
                operation_id=operation_id,
                tenant_id="tenant-a",
                attempt=1,
            )
        )
    assert store_error.value.__cause__ is None
    for secret in (
        "SIGNER_CREDENTIAL_MUST_BE_SUPPRESSED",
        "BIOLOGICAL_OBJECT_BYTES_MUST_BE_SUPPRESSED",
    ):
        assert secret not in str(store_error.value)
        assert secret not in caplog.text


def test_gated_receipts_are_required_and_validation_errors_hide_payload_values() -> None:
    with pytest.raises(ValidationError):
        ArtifactAccess(profile=ArtifactAccessProfile.RESTRICTED)

    credential = "ACADEMIC_LICENSE_CREDENTIAL_DO_NOT_LOG"
    sequence = "MKWVTFISLLFLFSSAYSRGVFRR"
    with pytest.raises(ValidationError) as raised:
        BeginArtifactUpload.model_validate(
            {
                "upload_id": str(uuid4()),
                "operation_id": str(uuid4()),
                "tenant_id": "tenant-a",
                "attempt": 1,
                "direction": "input",
                "expected_digest": "sha256:" + "a" * 64,
                "expected_size_bytes": len(sequence),
                "media_type": "text/x-fasta",
                "credential": credential,
                "sequence": sequence,
            }
        )
    assert credential not in str(raised.value)
    assert sequence not in str(raised.value)


@pytest.mark.asyncio
async def test_stale_attempt_cannot_begin_finalize_or_commit() -> None:
    service, repository, object_store, operation_id = await memory_service(attempt=1)
    body = b"MODEL_OUTPUT"
    request = BeginArtifactUpload(
        upload_id=uuid4(),
        operation_id=operation_id,
        tenant_id="tenant-a",
        attempt=1,
        direction=ArtifactDirection.OUTPUT,
        expected_digest=digest(body),
        expected_size_bytes=len(body),
        media_type="chemical/x-pdb",
    )
    started = await service.begin_upload(request)
    object_store.put(started.upload.storage_key, body, "chemical/x-pdb")
    artifact = await service.finalize_upload(
        FinalizeArtifactUpload(
            upload_id=request.upload_id,
            operation_id=operation_id,
            tenant_id="tenant-a",
            attempt=1,
        )
    )
    await repository.advance_attempt(operation_id, attempt=2)

    with pytest.raises(StaleArtifactAttemptError):
        await service.begin_upload(request)
    with pytest.raises(StaleArtifactAttemptError):
        await service.finalize_upload(
            FinalizeArtifactUpload(
                upload_id=request.upload_id,
                operation_id=operation_id,
                tenant_id="tenant-a",
                attempt=1,
            )
        )
    with pytest.raises(StaleArtifactAttemptError):
        await service.commit_terminal_result(result_draft(operation_id, artifact))


@pytest.mark.asyncio
async def test_exactly_one_terminal_manifest_wins_concurrent_conflicting_commits() -> None:
    service, repository, object_store, operation_id = await memory_service()
    _, _, artifact = await upload_artifact(
        service,
        object_store,
        operation_id=operation_id,
        attempt=1,
        direction=ArtifactDirection.OUTPUT,
        value=b"ATOM      1  CA  ALA A   1",
        media_type="chemical/x-pdb",
    )
    first = result_draft(operation_id, artifact, model_revision="revision-a")
    second = result_draft(operation_id, artifact, model_revision="revision-b")

    outcomes = await asyncio.gather(
        service.commit_terminal_result(first),
        service.commit_terminal_result(second),
        return_exceptions=True,
    )
    manifests = [value for value in outcomes if isinstance(value, TerminalResultManifest)]
    conflicts = [value for value in outcomes if isinstance(value, ArtifactConflictError)]
    assert len(manifests) == 1
    assert len(conflicts) == 1
    assert manifests[0].manifest_digest in {result_manifest_digest(first), result_manifest_digest(second)}
    events = await repository.list_events(operation_id, tenant_id="tenant-a")
    assert [event.event_type for event in events].count(ArtifactEventType.RESULT_COMMITTED) == 1


@pytest.mark.asyncio
async def test_identical_terminal_manifest_commit_is_idempotent_and_digest_is_canonical() -> None:
    service, repository, object_store, operation_id = await memory_service()
    _, _, artifact = await upload_artifact(
        service,
        object_store,
        operation_id=operation_id,
        attempt=1,
        direction=ArtifactDirection.OUTPUT,
        value=b'{"score":0.98}',
        media_type="application/json",
    )
    draft = result_draft(operation_id, artifact)
    first, second = await asyncio.gather(
        service.commit_terminal_result(draft),
        service.commit_terminal_result(draft),
    )
    assert first == second
    assert first.manifest_digest == result_manifest_digest(first)
    reloaded = TerminalResultManifest.model_validate_json(first.model_dump_json())
    assert reloaded == first
    assert first.schema_version == "fs2-serve.nebius.ai/scientific-result-record/v1"
    events = await repository.list_events(operation_id, tenant_id="tenant-a")
    assert [event.event_type for event in events].count(ArtifactEventType.RESULT_COMMITTED) == 1


def test_migration_has_closed_payload_free_tables_fences_and_one_owner_down_path() -> None:
    migration_path = CONTROL_ROOT / "migrations" / SCIENTIFIC_ARTIFACT_MIGRATION
    sql = migration_path.read_text(encoding="utf-8")
    assert "UNIQUE (operation_id,manifest_digest)" in sql
    assert "fs2_scientific_assert_current_attempt" in sql
    assert "fs2_scientific_reject_mutation" in sql
    assert "REVOKE ALL ON fs2_scientific_artifacts" in sql
    assert " bytea" not in sql.lower()
    assert " presigned_url" not in sql.lower()
    assert " credential text" not in sql.lower()
    assert " detail jsonb" not in sql.lower()
    assert "scientific-artifact-ref/v1" not in sql
    assert "scientific-result-manifest/v1" not in sql
    for table in (
        "fs2_scientific_artifact_events",
        "fs2_scientific_result_manifests",
        "fs2_scientific_uploads",
        "fs2_scientific_artifacts",
    ):
        assert f"DROP TABLE IF EXISTS {table}" in SCIENTIFIC_ARTIFACT_ROLLBACK_SQL
    assert f"version='{SCIENTIFIC_ARTIFACT_MIGRATION}'" in SCIENTIFIC_ARTIFACT_ROLLBACK_SQL


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
            ["inference.invoke"],
            ["proteina-complexa"],
        )
        await connection.execute(
            """
            INSERT INTO fs2_operations(
                id,tenant_id,principal_id,token_id,model_id,model_revision,protocol,
                operation,idempotency_key,request_hmac_key_id,request_hmac,
                request_content_type,payload_expires_at,max_attempts,attempt
            ) VALUES($1,'tenant-a','principal-a',$2,'proteina-complexa','revision-a',
                'scientific-batch','design','scientific-test-key','ledger-v1',$3,
                'application/json',clock_timestamp()+interval '1 hour',3,$4)
            """,
            operation_id,
            token_id,
            "e" * 64,
            attempt,
        )


@pytest_asyncio.fixture
async def postgres_artifact_store():
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
        await connection.execute(
            """
            TRUNCATE fs2_scientific_artifact_events,fs2_scientific_result_manifests,
                fs2_scientific_uploads,fs2_scientific_artifacts,fs2_operation_events,
                fs2_usage_facts,fs2_operations,fs2_tokens RESTART IDENTITY CASCADE
            """
        )
    try:
        yield store
    finally:
        async with store.pool.acquire() as connection:
            await connection.execute(
                """
                TRUNCATE fs2_scientific_artifact_events,fs2_scientific_result_manifests,
                    fs2_scientific_uploads,fs2_scientific_artifacts,fs2_operation_events,
                    fs2_usage_facts,fs2_operations,fs2_tokens RESTART IDENTITY CASCADE
                """
            )
        await store.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_repository_fences_attempt_and_commits_one_manifest(postgres_artifact_store) -> None:
    operation_id = uuid4()
    await insert_operation(postgres_artifact_store.pool, operation_id)
    object_store = MeasuringObjectStore()
    database_url = os.environ["FS2_TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)

    async def assume_runtime_role(connection) -> None:
        await connection.execute("SET ROLE fs2_serve_runtime")

    runtime_pool = await asyncpg.create_pool(
        dsn=database_url,
        min_size=2,
        max_size=8,
        init=assume_runtime_role,
    )
    assert runtime_pool is not None
    try:
        repository = PostgresArtifactRepository(runtime_pool)
        service = ScientificArtifactService(
            repository=repository,
            object_store=object_store,
            signer=SecretHandleSigner(),
            allowed_media_types=ALLOWED_MEDIA_TYPES,
            clock=lambda: NOW,
        )
        original_upload, _, artifact = await upload_artifact(
            service,
            object_store,
            operation_id=operation_id,
            attempt=1,
            direction=ArtifactDirection.OUTPUT,
            value=b"ATOM      1  CA  ALA A   1",
            media_type="chemical/x-pdb",
        )

        duplicate_address = BeginArtifactUpload(
            upload_id=uuid4(),
            operation_id=operation_id,
            tenant_id="tenant-a",
            attempt=1,
            direction=ArtifactDirection.OUTPUT,
            expected_digest=artifact.digest,
            expected_size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
        )
        with pytest.raises(ArtifactConflictError):
            await service.begin_upload(duplicate_address)

        outcomes = await asyncio.gather(
            service.commit_terminal_result(result_draft(operation_id, artifact, model_revision="revision-a")),
            service.commit_terminal_result(result_draft(operation_id, artifact, model_revision="revision-b")),
            return_exceptions=True,
        )
        assert sum(isinstance(value, TerminalResultManifest) for value in outcomes) == 1, outcomes
        assert sum(isinstance(value, ArtifactConflictError) for value in outcomes) == 1, outcomes
        events = await repository.list_events(operation_id, tenant_id="tenant-a")
        assert [event.event_type for event in events] == [
            ArtifactEventType.UPLOAD_BEGUN,
            ArtifactEventType.ARTIFACT_FINALIZED,
            ArtifactEventType.RESULT_COMMITTED,
        ]

        async with postgres_artifact_store.pool.acquire() as connection:
            await connection.execute("UPDATE fs2_operations SET attempt=2 WHERE id=$1", operation_id)
        stale = BeginArtifactUpload(
            upload_id=uuid4(),
            operation_id=operation_id,
            tenant_id="tenant-a",
            attempt=1,
            direction=ArtifactDirection.INPUT,
            expected_digest=digest(b"stale"),
            expected_size_bytes=5,
            media_type="application/json",
        )
        stale_outcomes = await asyncio.gather(
            service.begin_upload(stale),
            service.finalize_upload(
                FinalizeArtifactUpload(
                    upload_id=original_upload.upload_id,
                    operation_id=operation_id,
                    tenant_id="tenant-a",
                    attempt=1,
                )
            ),
            service.commit_terminal_result(result_draft(operation_id, artifact)),
            return_exceptions=True,
        )
        assert all(isinstance(value, StaleArtifactAttemptError) for value in stale_outcomes), stale_outcomes
    finally:
        await runtime_pool.close()

    async with postgres_artifact_store.pool.acquire() as connection:
        with pytest.raises(asyncpg.PostgresError, match="immutable scientific artifact record"):
            await connection.execute(
                "UPDATE fs2_scientific_artifacts SET media_type='application/json' WHERE id=$1",
                artifact.artifact_id,
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scientific_migration_executes_up_down_up(postgres_artifact_store) -> None:
    async with postgres_artifact_store.pool.acquire() as connection:
        assert await connection.fetchval("SELECT to_regclass('public.fs2_scientific_artifacts')") is not None
        await connection.execute(SCIENTIFIC_BATCH_ROLLBACK_SQL)
        await connection.execute(SCIENTIFIC_ARTIFACT_ROLLBACK_SQL)
        assert await connection.fetchval("SELECT to_regclass('public.fs2_scientific_artifacts')") is None
        assert not await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM fs2_schema_migrations WHERE version=$1)",
            SCIENTIFIC_ARTIFACT_MIGRATION,
        )
    await postgres_artifact_store.migrate()
    async with postgres_artifact_store.pool.acquire() as connection:
        assert await connection.fetchval("SELECT to_regclass('public.fs2_scientific_artifacts')") is not None
        assert await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM fs2_schema_migrations WHERE version=$1)",
            SCIENTIFIC_ARTIFACT_MIGRATION,
        )


def test_documentation_exists_and_declares_no_integration_or_live_deployment() -> None:
    documentation = (CONTROL_ROOT / "docs/scientific-artifact-results.md").read_text(encoding="utf-8")
    assert "No API, MCP, controller, model-adapter" in documentation
    assert "live-deployment wiring is included" in documentation
    assert SCIENTIFIC_ARTIFACT_MIGRATION in documentation
    assert "presigned" in documentation.lower()
