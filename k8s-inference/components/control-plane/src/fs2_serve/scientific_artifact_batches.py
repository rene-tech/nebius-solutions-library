"""Bounded artifact cohorts using the existing immutable tables and SQL fences.

The native transfer protocol batches 64 small files, but a loop of per-file
transactions still spends most checkpoint time on database round trips. These
set-based adapters amortize that cost without changing artifact identities,
independent object verification, retention, event history, or attempt fencing.
Object I/O is deliberately outside database transactions.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import asyncpg

from .scientific_artifacts import (
    _ARTIFACT_COLUMNS,
    _SQLSTATE_ERRORS,
    _UPLOAD_COLUMNS,
    ArtifactConflictError,
    ArtifactNotFoundError,
    ArtifactPolicyError,
    ArtifactRecord,
    BeginArtifactUpload,
    FinalizeArtifactUpload,
    UploadIntent,
    VerifiedStoredObject,
    _artifact_from_row,
    _same_upload_request,
    _upload_from_row,
    _verify_object,
)

MAX_UPLOAD_COHORT = 64


def validate_cohort(requests: Sequence[BeginArtifactUpload | FinalizeArtifactUpload]) -> None:
    """Bound both SQL work and memory, preserving the caller's request order."""
    if not 1 <= len(requests) <= MAX_UPLOAD_COHORT:
        raise ArtifactPolicyError("artifact cohort requires 1..64 entries")
    if len({request.upload_id for request in requests}) != len(requests):
        raise ArtifactPolicyError("artifact cohort upload identities must be distinct")
    first = requests[0]
    if any((request.operation_id, request.tenant_id) != (first.operation_id, first.tenant_id)
           for request in requests):
        raise ArtifactPolicyError("artifact cohort must belong to one operation and tenant")


def _database_error(error: asyncpg.PostgresError, message: str) -> Exception:
    kind = _SQLSTATE_ERRORS.get(str(getattr(error, "sqlstate", "")), ArtifactConflictError)
    return kind(message)


def _ordered_uploads(rows: Sequence[Any], ids: list[UUID]) -> list[UploadIntent]:
    indexed = {row["id"]: _upload_from_row(row) for row in rows}
    if len(indexed) != len(ids) or any(identity not in indexed for identity in ids):
        raise ArtifactNotFoundError("upload cohort not found")
    return [indexed[identity] for identity in ids]


async def begin_postgres_uploads(
    pool: asyncpg.Pool[Any], reservations: tuple[tuple[BeginArtifactUpload, str], ...]
) -> list[UploadIntent]:
    requests = tuple(request for request, _ in reservations)
    validate_cohort(requests)
    first = requests[0]
    if any(request.attempt_id != first.attempt_id for request in requests):
        raise ArtifactPolicyError("upload cohort must belong to one attempt")
    ids = [request.upload_id for request in requests]
    payload = json.dumps([
        {
            "id": str(request.upload_id), "direction": request.direction.value,
            "expected_digest": request.expected_digest, "expected_size_bytes": request.expected_size_bytes,
            "media_type": request.media_type,
            "compression": request.compression.value if request.compression else None,
            "storage_key": key, "access_profile": request.access.profile.value,
            "access_receipt_digest": request.access.receipt_digest,
        }
        for request, key in reservations
    ])
    try:
        async with pool.acquire() as connection, connection.transaction():
            inserted = await connection.fetch(
                """
                INSERT INTO fs2_scientific_uploads
                    (id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,expected_digest,
                     expected_size_bytes,media_type,compression,storage_key,access_profile,
                     access_receipt_digest,begun_at)
                SELECT r.id,a.attempt_id,a.operation_id,a.tenant_id,a.stage_id,a.shard_id,
                       r.direction,r.expected_digest,r.expected_size_bytes,r.media_type,r.compression,
                       r.storage_key,r.access_profile,r.access_receipt_digest,clock_timestamp()
                FROM jsonb_to_recordset($1::jsonb) AS r(
                    id uuid,direction text,expected_digest text,expected_size_bytes bigint,
                    media_type text,compression text,storage_key text,access_profile text,
                    access_receipt_digest text)
                CROSS JOIN fs2_scientific_stage_attempts a
                WHERE a.attempt_id=$2 AND a.operation_id=$3 AND a.tenant_id=$4
                ORDER BY r.id
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                payload, first.attempt_id, first.operation_id, first.tenant_id,
            )
            rows = await connection.fetch(
                f"SELECT {_UPLOAD_COLUMNS} FROM fs2_scientific_uploads "  # noqa: S608
                "WHERE id=ANY($1::uuid[]) AND operation_id=$2 AND tenant_id=$3",
                ids, first.operation_id, first.tenant_id,
            )
            intents = _ordered_uploads(rows, ids)
            for intent, (request, key) in zip(intents, reservations, strict=True):
                if not _same_upload_request(intent, request, key):
                    raise ArtifactConflictError("upload identity is already bound to different content")
            if inserted:
                await connection.execute(
                    """
                    INSERT INTO fs2_scientific_artifact_events
                        (event_type,operation_id,tenant_id,stage_id,attempt_id,upload_id,occurred_at)
                    SELECT 'upload_begun',operation_id,tenant_id,stage_id,attempt_id,id,begun_at
                    FROM fs2_scientific_uploads WHERE id=ANY($1::uuid[])
                    """,
                    [row["id"] for row in inserted],
                )
            return intents
    except asyncpg.PostgresError as error:
        raise _database_error(error, "artifact cohort could not be reserved") from None


async def get_postgres_uploads(
    pool: asyncpg.Pool[Any], requests: tuple[FinalizeArtifactUpload, ...]
) -> list[UploadIntent]:
    validate_cohort(requests)
    first = requests[0]
    ids = [request.upload_id for request in requests]
    rows = await pool.fetch(
        f"SELECT {_UPLOAD_COLUMNS} FROM fs2_scientific_uploads "  # noqa: S608
        "WHERE id=ANY($1::uuid[]) AND operation_id=$2 AND tenant_id=$3",
        ids, first.operation_id, first.tenant_id,
    )
    return _ordered_uploads(rows, ids)


async def finalize_postgres_uploads(
    pool: asyncpg.Pool[Any],
    cohort: tuple[tuple[FinalizeArtifactUpload, VerifiedStoredObject, UUID], ...],
) -> list[ArtifactRecord]:
    requests = tuple(request for request, _, _ in cohort)
    validate_cohort(requests)
    first = requests[0]
    ids = [request.upload_id for request in requests]
    try:
        async with pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                f"SELECT {_UPLOAD_COLUMNS} FROM fs2_scientific_uploads "  # noqa: S608
                "WHERE id=ANY($1::uuid[]) AND operation_id=$2 AND tenant_id=$3 ORDER BY id FOR UPDATE",
                ids, first.operation_id, first.tenant_id,
            )
            intents = _ordered_uploads(rows, ids)
            pending = []
            for intent, (_, verified, artifact_id) in zip(intents, cohort, strict=True):
                if intent.artifact_id is None:
                    _verify_object(intent, verified)
                    pending.append({"upload_id": str(intent.upload_id), "artifact_id": str(artifact_id)})
            if pending:
                # Existing insertion triggers still fence superseded attempts
                # and terminal operations. The locked upload rows make replay
                # idempotent, including overlapping/concurrent cohorts.
                await connection.execute(
                    """
                    WITH published AS (
                        INSERT INTO fs2_scientific_artifacts
                            (id,attempt_id,operation_id,tenant_id,stage_id,shard_id,direction,digest,
                             size_bytes,media_type,compression,storage_key,access_profile,
                             access_receipt_digest,retention_expires_at,created_at)
                        SELECT r.artifact_id,u.attempt_id,u.operation_id,u.tenant_id,u.stage_id,u.shard_id,
                               u.direction,u.expected_digest,u.expected_size_bytes,u.media_type,u.compression,
                               u.storage_key,u.access_profile,u.access_receipt_digest,
                               clock_timestamp()+(a.retention_expires_at-a.started_at),clock_timestamp()
                        FROM jsonb_to_recordset($1::jsonb) AS r(upload_id uuid,artifact_id uuid)
                        JOIN fs2_scientific_uploads u ON u.id=r.upload_id
                        JOIN fs2_scientific_stage_attempts a ON a.attempt_id=u.attempt_id
                        RETURNING id,storage_key
                    )
                    UPDATE fs2_scientific_uploads u
                    SET artifact_id=p.id,finalized_at=clock_timestamp()
                    FROM published p WHERE u.storage_key=p.storage_key
                    """,
                    json.dumps(pending),
                )
                await connection.execute(
                    """
                    INSERT INTO fs2_scientific_artifact_events
                        (event_type,operation_id,tenant_id,stage_id,attempt_id,upload_id,artifact_id,occurred_at)
                    SELECT 'artifact_finalized',u.operation_id,u.tenant_id,u.stage_id,u.attempt_id,
                           u.id,u.artifact_id,a.created_at
                    FROM fs2_scientific_uploads u JOIN fs2_scientific_artifacts a ON a.id=u.artifact_id
                    WHERE u.id=ANY($1::uuid[])
                    """,
                    [UUID(item["upload_id"]) for item in pending],
                )
            artifacts = await connection.fetch(
                f"SELECT {_ARTIFACT_COLUMNS} FROM fs2_scientific_artifacts "  # noqa: S608
                "WHERE id IN (SELECT artifact_id FROM fs2_scientific_uploads WHERE id=ANY($1::uuid[]))",
                ids,
            )
            by_storage_key = {row["storage_key"]: _artifact_from_row(row) for row in artifacts}
            if any(intent.storage_key not in by_storage_key for intent in intents):
                raise ArtifactNotFoundError("finalized artifact cohort not found")
            return [by_storage_key[intent.storage_key] for intent in intents]
    except asyncpg.PostgresError as error:
        raise _database_error(error, "artifact cohort could not be published") from None
