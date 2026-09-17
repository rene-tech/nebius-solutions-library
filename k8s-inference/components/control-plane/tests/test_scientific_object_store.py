"""Real S3-compatible object-store tests.

These run against an actual S3 gateway so the presigned handles are proven by
the gateway itself rather than by a local re-implementation of SigV4. Set
``FS2_TEST_S3_ENDPOINT``, ``FS2_TEST_S3_ACCESS_KEY`` and
``FS2_TEST_S3_SECRET_KEY`` to enable them.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import urllib.parse
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError

from fs2_serve.scientific_artifacts import (
    MULTIPART_PART_BYTES,
    ArtifactAccess,
    ArtifactCompression,
    ArtifactDirection,
    ArtifactNotFoundError,
    ArtifactPolicyError,
    ArtifactRemovalEvidenceKind,
    ArtifactRemovalTarget,
    ArtifactUploadSession,
    ArtifactVerificationError,
    EphemeralHandle,
    LegacyArtifactVersionTarget,
    UploadIntent,
    artifact_storage_key,
)
from fs2_serve.scientific_object_store import ObjectStoreConfig, S3ArtifactObjectStore

pytestmark = pytest.mark.objectstore

BUCKET = "fs2-scientific-artifacts-test"
KEY_PREFIX = "scientific/v1/tenants/tenant-a/operations"


def removal_target(storage_key: str) -> ArtifactRemovalTarget:
    now = datetime.now(UTC)
    return ArtifactRemovalTarget(
        upload_id=uuid4(),
        operation_id=uuid4(),
        attempt_id=uuid4(),
        tenant_id="tenant-a",
        storage_key=storage_key,
        latest_upload_capability_expires_at=now - timedelta(hours=2),
        removal_generation=1,
        verification_generation=1,
        removal_claimed_at=now - timedelta(hours=1),
        verification_claimed_at=now - timedelta(minutes=30),
        eligible_at=now - timedelta(hours=2),
    )


def store_config(**overrides: object) -> ObjectStoreConfig:
    endpoint = os.environ.get("FS2_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("FS2_TEST_S3_ENDPOINT is not set")
    defaults: dict[str, object] = {
        "endpoint_url": endpoint,
        "bucket": BUCKET,
        "region": os.environ.get("FS2_TEST_S3_REGION", "eu-north1"),
        "access_key": os.environ["FS2_TEST_S3_ACCESS_KEY"],
        "secret_key": os.environ["FS2_TEST_S3_SECRET_KEY"],
        "addressing_style": "path",
        "verify_tls": endpoint.startswith("https://"),
        "chunk_bytes": 64 * 1024,
    }
    return ObjectStoreConfig(**{**defaults, **overrides})  # type: ignore[arg-type]


@pytest.fixture
async def object_store():
    store = S3ArtifactObjectStore(store_config())
    try:
        await asyncio.to_thread(store._client.create_bucket, Bucket=BUCKET)
    except ClientError as error:
        code = str((error.response.get("Error") or {}).get("Code", ""))
        if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise
    await asyncio.to_thread(
        store._client.put_bucket_versioning,
        Bucket=BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )
    try:
        yield store
    finally:
        await store.close()


def key_for(digest: str, *, direction: str = "output") -> str:
    return f"{KEY_PREFIX}/{uuid4()}/stages/design/shards/-/attempts/{uuid4()}/{direction}/sha256/{digest[7:]}"


async def multipart_upload(
    object_store: S3ArtifactObjectStore,
    payload: bytes,
    *,
    media_type: str,
    compression: ArtifactCompression | None = None,
    ttl: timedelta = timedelta(minutes=5),
) -> tuple[UploadIntent, ArtifactUploadSession, EphemeralHandle]:
    operation_id = uuid4()
    attempt_id = uuid4()
    upload_id = uuid4()
    expected_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    storage_key = artifact_storage_key(
        tenant_id="tenant-a",
        operation_id=operation_id,
        stage_id="design",
        shard_id=None,
        attempt_id=attempt_id,
        direction=ArtifactDirection.OUTPUT,
        digest=expected_digest,
    )
    provider_upload_id, initiated_at = await object_store.create_upload_session(
        storage_key=storage_key,
        media_type=media_type,
        compression=compression,
    )
    session = ArtifactUploadSession(
        upload_id=upload_id,
        tenant_id="tenant-a",
        storage_key=storage_key,
        provider_upload_id=provider_upload_id,
        session_generation=1,
        part_size_bytes=MULTIPART_PART_BYTES,
        part_count=1,
        initiated_at=initiated_at,
    )
    intent = UploadIntent(
        upload_id=upload_id,
        attempt_id=attempt_id,
        operation_id=operation_id,
        tenant_id="tenant-a",
        stage_id="design",
        direction=ArtifactDirection.OUTPUT,
        expected_digest=expected_digest,
        expected_size_bytes=len(payload),
        media_type=media_type,
        compression=compression,
        storage_key=storage_key,
        access=ArtifactAccess(),
        begun_at=initiated_at,
    )
    handle = await object_store.presign_upload_part(
        session=session,
        part_number=1,
        size_bytes=len(payload),
        checksum=expected_digest,
        ttl=ttl,
    )
    return intent, session, handle


def test_legacy_version_inventory_persists_a_bounded_exact_key_cursor() -> None:
    storage_key = key_for("sha256:" + "7" * 64)

    class CursorClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def list_object_versions(self, **params: object) -> dict[str, object]:
            self.calls.append(params)
            if "VersionIdMarker" not in params:
                return {
                    "Versions": [{"Key": storage_key, "VersionId": "v2", "IsLatest": True}],
                    "IsTruncated": True,
                    "NextKeyMarker": storage_key,
                    "NextVersionIdMarker": "v2",
                    "ResponseMetadata": {"RequestId": "list-page-1"},
                }
            return {
                "Versions": [{"Key": storage_key, "VersionId": "v1", "IsLatest": False}],
                "IsTruncated": False,
                "ResponseMetadata": {"RequestId": "list-page-2"},
            }

    store = object.__new__(S3ArtifactObjectStore)
    store._config = ObjectStoreConfig(
        endpoint_url="https://storage.invalid",
        bucket="legacy-version-test",
        region="eu-north1",
        access_key="access",
        secret_key="secret",
    )
    store._client = CursorClient()
    target = LegacyArtifactVersionTarget(
        artifact_id=uuid4(),
        upload_id=uuid4(),
        tenant_id="tenant-a",
        storage_key=storage_key,
        expected_digest="sha256:" + "7" * 64,
        expected_size_bytes=7,
        expected_media_type="application/json",
        claim_generation=1,
        claimed_at=datetime.now(UTC),
        eligible_at=datetime.now(UTC),
    )

    first, first_request, next_key, next_version = store._legacy_versions_page(target)
    assert first[0][0] == "v2"
    assert (first_request, next_key, next_version) == ("list-page-1", storage_key, "v2")
    second_target = target.model_copy(
        update={"list_key_marker": next_key, "list_version_id_marker": next_version}
    )
    second, second_request, final_key, final_version = store._legacy_versions_page(second_target)
    assert second[0][0] == "v1"
    assert (second_request, final_key, final_version) == ("list-page-2", None, None)
    assert store._client.calls[1]["KeyMarker"] == storage_key
    assert store._client.calls[1]["VersionIdMarker"] == "v2"


async def test_presigned_upload_carries_a_real_signature_and_no_secret(object_store) -> None:
    payload = b"ATOM  CA  ALA A   1\n" * 2000
    intent, session, handle = await multipart_upload(
        object_store,
        payload,
        media_type="chemical/x-pdb",
        ttl=timedelta(minutes=10),
    )
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(handle.url).query))
    assert query["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    assert len(query["X-Amz-Signature"]) == 64
    assert query["X-Amz-Credential"].endswith("/s3/aws4_request")
    assert "X-Amz-Date" in query and "X-Amz-SignedHeaders" in query
    assert os.environ["FS2_TEST_S3_SECRET_KEY"] not in handle.url
    assert handle.write_once is True
    assert handle.headers["content-length"] == str(len(payload))
    assert handle.headers["x-amz-checksum-sha256"]

    async with httpx.AsyncClient(timeout=30) as client:
        accepted = await client.put(handle.url, content=payload, headers=dict(handle.headers))
        assert accepted.status_code == 200, accepted.text[:300]

    verified = await object_store.complete_upload_session(session=session, intent=intent)
    assert verified.digest == intent.expected_digest
    assert verified.size_bytes == len(payload)
    assert verified.media_type == "chemical/x-pdb"
    assert verified.storage_key == intent.storage_key
    assert verified.provider_version_id
    await object_store.delete(removal_target(intent.storage_key))


async def test_the_signature_binds_the_declared_part_length_and_checksum(object_store) -> None:
    payload = b"ATOM  CA"
    _intent, session, handle = await multipart_upload(
        object_store,
        payload,
        media_type="chemical/x-pdb",
    )
    async with httpx.AsyncClient(timeout=30) as client:
        refused = await client.put(handle.url, content=b"ATOM  CB", headers=dict(handle.headers))
    assert refused.status_code >= 400
    await object_store.abort_upload_session(session=session)


async def test_download_handles_round_trip_and_expire(object_store) -> None:
    payload = b"MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQ"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    key = key_for(digest, direction="input")
    stored = await object_store.put_object(
        storage_key=key,
        payload=payload,
        media_type="text/x-fasta",
        compression=None,
    )
    async with httpx.AsyncClient(timeout=30) as client:
        download = await object_store.presign_download(
            storage_key=key,
            provider_version_id=stored.provider_version_id,
            ttl=timedelta(minutes=5),
        )
        assert download.write_once is False
        fetched = await client.get(download.url)
        assert fetched.status_code == 200
        assert fetched.content == payload

        brief = await object_store.presign_download(
            storage_key=key,
            provider_version_id=stored.provider_version_id,
            ttl=timedelta(seconds=1),
        )
        await asyncio.sleep(2.5)
        expired = await client.get(brief.url)
        assert expired.status_code == 403
    await object_store.delete(removal_target(key))


async def test_streaming_verification_refuses_an_object_over_the_ceiling(object_store) -> None:
    payload = b"x" * (256 * 1024)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    key = key_for(digest)
    stored = await object_store.put_object(
        storage_key=key,
        payload=payload,
        media_type="application/json",
        compression=None,
    )
    with pytest.raises(ArtifactVerificationError, match="ceiling"):
        await object_store.inspect(key, provider_version_id=stored.provider_version_id, max_bytes=1024)
    assert (
        await object_store.inspect(key, provider_version_id=stored.provider_version_id)
    ).size_bytes == len(payload)
    await object_store.delete(removal_target(key))


async def test_inline_write_and_stream_round_trip_on_a_real_gateway(object_store) -> None:
    """The gateway byte path must work against a real S3 implementation.

    ``put_object`` writes without any presigned handle and reports what the
    gateway actually stored; ``stream_object`` returns the same bytes in
    chunks, which is what the public content route hands to a customer.
    """

    payload = b"".join(f"ATOM  {index:5d}  CA  ALA A\n".encode() for index in range(20000))
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    key = key_for(digest, direction="input")
    stored = await object_store.put_object(
        storage_key=key, payload=payload, media_type="chemical/x-pdb", compression=None
    )
    assert stored.digest == digest
    assert stored.size_bytes == len(payload)
    assert stored.media_type == "chemical/x-pdb"
    assert stored.storage_key == key

    chunks = [
        chunk
        async for chunk in object_store.stream_object(
            key,
            provider_version_id=stored.provider_version_id,
            max_bytes=len(payload),
        )
    ]
    assert b"".join(chunks) == payload
    # A 64 KiB chunk size against a 640 KB object must really be chunked.
    assert len(chunks) > 1
    assert all(chunk for chunk in chunks)
    await object_store.delete(removal_target(key))


async def test_inline_write_reports_the_encoding_it_persisted(object_store) -> None:
    payload = b"\x1f\x8b" + b"compressed" * 32
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    key = key_for(digest)
    stored = await object_store.put_object(
        storage_key=key,
        payload=payload,
        media_type="application/json",
        compression=ArtifactCompression.GZIP,
    )
    assert stored.compression is ArtifactCompression.GZIP
    assert b"".join(
        [
            chunk
            async for chunk in object_store.stream_object(
                key,
                provider_version_id=stored.provider_version_id,
            )
        ]
    ) == payload
    await object_store.delete(removal_target(key))


async def test_inline_write_refuses_an_object_over_the_ceiling(object_store) -> None:
    store = S3ArtifactObjectStore(store_config(max_stream_bytes=1024))
    try:
        with pytest.raises(ArtifactPolicyError):
            await store.put_object(
                storage_key=key_for("sha256:" + "0" * 64),
                payload=b"y" * 1025,
                media_type="application/json",
                compression=None,
            )
    finally:
        await store.close()


async def test_streaming_an_absent_object_is_not_found(object_store) -> None:
    with pytest.raises(ArtifactNotFoundError):
        [
            chunk
            async for chunk in object_store.stream_object(
                key_for("sha256:" + "1" * 64),
                provider_version_id="absent-version",
            )
        ]


async def test_streaming_stops_at_the_requested_bound(object_store) -> None:
    payload = b"z" * (192 * 1024)
    key = key_for("sha256:" + hashlib.sha256(payload).hexdigest())
    stored = await object_store.put_object(
        storage_key=key,
        payload=payload,
        media_type="application/json",
        compression=None,
    )
    with pytest.raises(ArtifactVerificationError):
        [
            chunk
            async for chunk in object_store.stream_object(
                key,
                provider_version_id=stored.provider_version_id,
                max_bytes=len(payload) - 1,
            )
        ]
    await object_store.delete(removal_target(key))


async def test_compression_is_signed_and_reported(object_store) -> None:
    payload = b"\x1f\x8b" + b"compressed-body"
    intent, session, handle = await multipart_upload(
        object_store,
        payload,
        media_type="application/json",
        compression=ArtifactCompression.GZIP,
    )
    async with httpx.AsyncClient(timeout=30) as client:
        stored = await client.put(handle.url, content=payload, headers=dict(handle.headers))
        assert stored.status_code == 200
    verified = await object_store.complete_upload_session(session=session, intent=intent)
    assert verified.compression is ArtifactCompression.GZIP
    await object_store.delete(removal_target(intent.storage_key))


async def test_absent_and_repeated_deletes_are_reported_faithfully(object_store) -> None:
    key = key_for("sha256:" + "0" * 64)
    with pytest.raises(ArtifactNotFoundError):
        await object_store.inspect(key, provider_version_id="absent-version")
    target = removal_target(key)
    first = await object_store.delete(target)
    second = await object_store.delete(target)
    assert first.kind is ArtifactRemovalEvidenceKind.ABSENCE_CONFIRMED
    assert second.kind is ArtifactRemovalEvidenceKind.ABSENCE_CONFIRMED
    assert first.removed_version_count == second.removed_version_count == 0
    independently_verified = await object_store.verify_absent(target)
    assert independently_verified.kind is ArtifactRemovalEvidenceKind.ABSENCE_CONFIRMED
    assert independently_verified.removed_version_count == 0


async def test_delete_evidence_requires_every_exact_key_version_to_be_absent(object_store) -> None:
    await asyncio.to_thread(
        object_store._client.put_bucket_versioning,
        Bucket=BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )
    key = key_for("sha256:" + "3" * 64)
    for payload in (b"first-version", b"second-version"):
        await asyncio.to_thread(
            object_store._client.put_object,
            Bucket=BUCKET,
            Key=key,
            Body=payload,
            ContentType="application/json",
            ContentLength=len(payload),
        )

    target = removal_target(key)
    evidence = await object_store.delete(target)
    assert evidence.kind is ArtifactRemovalEvidenceKind.ALL_VERSIONS_REMOVED
    assert evidence.removed_version_count >= 2
    with pytest.raises(ArtifactNotFoundError):
        await object_store.inspect(key, provider_version_id="removed-version")
    remaining, _request_id = await asyncio.to_thread(object_store._exact_versions, key)
    assert remaining == []
    independently_verified = await object_store.verify_absent(target)
    assert independently_verified.kind is ArtifactRemovalEvidenceKind.ABSENCE_CONFIRMED
    assert independently_verified.removed_version_count == 0


async def test_a_non_positive_lifetime_is_refused(object_store) -> None:
    with pytest.raises(ArtifactPolicyError, match="lifetime"):
        await object_store.presign_download(
            storage_key=key_for("sha256:" + "1" * 64),
            provider_version_id="unused-version",
            ttl=timedelta(0),
        )


def test_config_refuses_anonymous_or_plaintext_credentials() -> None:
    with pytest.raises(ValueError, match="credentials are required"):
        ObjectStoreConfig(endpoint_url="https://storage.invalid", bucket="b", region="r", access_key="", secret_key="")
    with pytest.raises(ValueError, match="TLS verification"):
        ObjectStoreConfig(
            endpoint_url="http://storage.invalid",
            bucket="b",
            region="r",
            access_key="a",
            secret_key="s",
            verify_tls=True,
        )
    assert "secret" not in repr(
        ObjectStoreConfig(
            endpoint_url="https://storage.invalid",
            bucket="b",
            region="r",
            access_key="access",
            secret_key="secret",
        )
    )


@pytest.mark.postgres
async def test_the_production_wiring_runs_the_whole_lifecycle_on_real_infrastructure(tmp_path) -> None:
    """Build the service exactly as ``cli.build_runtime`` does and use it.

    This covers the real construction path end to end: settings, the mounted
    credentials file, the boto3 client, the PostgreSQL repository, a genuinely
    presigned upload, streamed digest verification and the canonical result.
    """

    import json
    from datetime import UTC, datetime
    from uuid import uuid4

    import asyncpg
    from test_scientific_artifacts import (
        TRUNCATE,
        execution_identity,
        insert_operation,
        scheduling_snapshot,
    )

    from fs2_serve.cli import _artifact_service
    from fs2_serve.crypto import KeyedHasher, PayloadCipher
    from fs2_serve.postgres import PostgresStore
    from fs2_serve.scientific_artifacts import (
        ArtifactDirection,
        AttemptStatus,
        BeginArtifactUpload,
        CloseStageAttempt,
        CommitStageResult,
        FinalizeArtifactUpload,
        KueueAdmission,
        ManifestEntryDraft,
        OpenStageAttempt,
        PostgresArtifactRepository,
        RunResultDraft,
    )
    from fs2_serve.settings import Settings

    database_url = os.environ.get("FS2_TEST_DATABASE_URL")
    endpoint = os.environ.get("FS2_TEST_S3_ENDPOINT")
    if not database_url or not endpoint:
        pytest.skip("FS2_TEST_DATABASE_URL and FS2_TEST_S3_ENDPOINT are both required")

    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "access_key_id": os.environ["FS2_TEST_S3_ACCESS_KEY"],
                "secret_access_key": os.environ["FS2_TEST_S3_SECRET_KEY"],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        scientific_artifacts_enabled=True,
        artifact_store_endpoint=endpoint,
        artifact_store_bucket=BUCKET,
        artifact_store_region=os.environ.get("FS2_TEST_S3_REGION", "eu-north1"),
        artifact_store_verify_tls=endpoint.startswith("https://"),
        artifact_store_credentials_file=credentials,
        allow_non_cluster_urls=not endpoint.startswith("https://"),
    )

    store = await PostgresStore.connect(
        database_url,
        __import__("pathlib").Path(__file__).resolve().parents[1] / "migrations",
        PayloadCipher(active_key_id="payload-v1", keys={"payload-v1": b"p" * 32}),
        KeyedHasher(active_key_id="ledger-v1", keys={"ledger-v1": b"h" * 32}),
        payload_ttl_seconds=3600,
    )
    await store.migrate()
    async with store.pool.acquire() as connection:
        await connection.execute(TRUNCATE)

    # ``build_runtime`` builds the repository over the pool and passes *that*;
    # handing the store itself in would only fail once a repository method is
    # first called, which is exactly what used to happen here.
    service = _artifact_service(settings, PostgresArtifactRepository(store.pool))
    assert service is not None
    try:
        await asyncio.to_thread(service._store._client.create_bucket, Bucket=BUCKET)
    except ClientError as error:
        if str((error.response.get("Error") or {}).get("Code", "")) not in {
            "BucketAlreadyOwnedByYou",
            "BucketAlreadyExists",
        }:
            raise

    tenant = "tenant-a"
    operation_id = uuid4()
    attempt_id = uuid4()
    started = datetime.now(UTC)
    try:
        await insert_operation(store.pool, operation_id)
        await service.open_attempt(
            OpenStageAttempt(
                attempt_id=attempt_id,
                operation_id=operation_id,
                tenant_id=tenant,
                stage_id="design",
                shard_id="candidate-0001",
                attempt_number=1,
                admission=KueueAdmission(
                    resolved_pool_id="gpu-preemptible",
                    admitted_resource_flavor="gpu-preemptible",
                    accelerator_resource_name="nvidia.com/gpu",
                    accelerator_count=1,
                    admitted_at=started,
                ),
                kueue_workload_uid="kueue-live",
                k8s_job_uid="job-live",
                started_at=started,
            )
        )

        published = []
        for direction, payload, media_type in (
            (ArtifactDirection.INPUT, b'{"sequence":"MKTAYIAKQRQ"}', "application/json"),
            (ArtifactDirection.OUTPUT, b"ATOM  CA  ALA A   1\n" * 500, "chemical/x-pdb"),
        ):
            upload_id = uuid4()
            begun = await service.begin_upload(
                BeginArtifactUpload(
                    upload_id=upload_id,
                    attempt_id=attempt_id,
                    operation_id=operation_id,
                    tenant_id=tenant,
                    direction=direction,
                    expected_digest="sha256:" + hashlib.sha256(payload).hexdigest(),
                    expected_size_bytes=len(payload),
                    media_type=media_type,
                )
            )
            # A real gateway must accept the real signature.
            async with httpx.AsyncClient(timeout=30) as client:
                stored = await client.put(begun.handle.url, content=payload, headers=dict(begun.handle.headers))
                assert stored.status_code == 200, stored.text[:300]
            published.append(
                await service.finalize_upload(
                    FinalizeArtifactUpload(upload_id=upload_id, operation_id=operation_id, tenant_id=tenant)
                )
            )

        completed = datetime.now(UTC)
        await service.close_attempt(
            CloseStageAttempt(
                attempt_id=attempt_id,
                operation_id=operation_id,
                tenant_id=tenant,
                status=AttemptStatus.SUCCEEDED,
                completed_at=completed,
                pod_uids=("pod-live",),
                node_uids=("node-live",),
                gpu_uuids=("GPU-live",),
            )
        )
        commit = await service.commit_stage(
            CommitStageResult(
                operation_id=operation_id,
                tenant_id=tenant,
                stage_id="design",
                attempt_ids=(attempt_id,),
                entries=(
                    ManifestEntryDraft(
                        name="designed-backbone",
                        semantic_type="protein.structure/v1",
                        artifact_id=published[1].artifact_id,
                    ),
                ),
                validation_digest="sha256:" + "9" * 64,
                semantic_valid=True,
                committed_at=completed,
                validated_at=completed,
            )
        )
        controller_view = await service.artifact_commit(operation_id, stage_id="design", tenant_id=tenant)
        assert controller_view is not None
        assert controller_view.manifest_digest == commit.manifest_digest
        assert controller_view.attempt_ids == (attempt_id,)

        result = await service.commit_run_result(
            RunResultDraft(
                operation_id=operation_id,
                tenant_id=tenant,
                terminal_status="succeeded",
                submitted_at=started,
                completed_at=completed,
                execution_identity=execution_identity(),
                scheduling_snapshot=scheduling_snapshot(),
                input_manifest_artifact_id=published[0].artifact_id,
                output_manifest_artifact_id=published[1].artifact_id,
                validator_id="proteina-complexa-validator",
                validation_status="passed",
                validation_receipt_digest="sha256:" + "2" * 64,
            )
        )
        assert result.result.terminal_status.value == "succeeded"
        assert result.result.attempts[0].gpu_uuids == ("GPU-live",)

        # A download handle issued now must be honoured by the real gateway.
        download = await service.download(published[1].artifact_id, tenant_id=tenant)
        async with httpx.AsyncClient(timeout=30) as client:
            fetched = await client.get(download.handle.url)
        assert fetched.status_code == 200
        assert hashlib.sha256(fetched.content).hexdigest() == published[1].digest.removeprefix("sha256:")

        removal_targets = await service._repository.purge_keys(operation_id, tenant_id=tenant)
        assert len(removal_targets) == 2
        assert {target.storage_key for target in removal_targets} == {record.storage_key for record in published}
    finally:
        async with store.pool.acquire() as connection:
            await connection.execute(TRUNCATE)
        await store.close()
        for key in await asyncio.to_thread(
            lambda: [
                item["Key"]
                for item in (
                    service._store._client.list_objects_v2(
                        Bucket=BUCKET, Prefix=f"scientific/v1/tenants/{tenant}/operations/{operation_id}"
                    ).get("Contents")
                    or []
                )
            ]
        ):
            await service._store.delete(removal_target(key))
        await service._store.close()
    assert isinstance(store.pool, asyncpg.Pool)
