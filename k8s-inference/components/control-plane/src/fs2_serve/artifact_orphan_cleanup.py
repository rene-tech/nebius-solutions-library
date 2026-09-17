"""Crash-safe cleanup for abandoned direct artifact uploads.

A signed upload can complete without the client returning its provider version
to finalization. Maintenance first claims the upload in PostgreSQL after a
fixed window longer than every upload handle, which fences all later customer
actions. The tenant broker then discovers at most one write-once version. Its
exact version is durably recorded before an exact-version delete, and a
standalone terminal receipt survives later operation-row retention.

This process receives no provider key and no authority signing key.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from .artifact_credential_broker import ArtifactCredentialBroker, ArtifactCredentialBrokerConfig
from .scientific_artifacts import ArtifactCompression

ABANDONED_UPLOAD_GRACE = timedelta(hours=1)
ABANDONED_UPLOAD_LIMIT = 10
ABANDONED_UPLOAD_ITEM_TIMEOUT_SECONDS = 15.0
ABANDONED_UPLOAD_RUN_BUDGET_SECONDS = 180.0
# A presigned URL expiry limits request admission, not completion. With a 1 TiB
# accepted object ceiling, elapsed wall time cannot prove that an admitted PUT
# is quiescent. Keep the claim/fence implementation preserved but unreachable
# until provider-backed multipart/in-flight closure or an equivalent transfer
# completion proof is independently reviewed.
ABANDONED_UPLOAD_CLEANUP_ACTIVATED = False

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AbandonedUpload:
    upload_id: UUID
    operation_id: UUID
    tenant_id: str
    storage_key: str
    expected_digest: str
    expected_size_bytes: int
    expected_media_type: str
    expected_compression: ArtifactCompression | None
    begun_at: datetime
    retention_expires_at: datetime
    object_version_id: str | None
    observed_size_bytes: int | None
    observed_media_type: str | None
    observed_compression: ArtifactCompression | None
    provider_observed_at: datetime | None

    @classmethod
    def from_row(cls, row: Any) -> "AbandonedUpload":
        return cls(
            upload_id=row["upload_id"],
            operation_id=row["operation_id"],
            tenant_id=str(row["tenant_id"]),
            storage_key=str(row["storage_key"]),
            expected_digest=str(row["expected_digest"]),
            expected_size_bytes=int(row["expected_size_bytes"]),
            expected_media_type=str(row["expected_media_type"]),
            expected_compression=(
                None
                if row["expected_compression"] is None
                else ArtifactCompression(str(row["expected_compression"]))
            ),
            begun_at=row["begun_at"],
            retention_expires_at=row["retention_expires_at"],
            object_version_id=(
                None if row["object_version_id"] is None else str(row["object_version_id"])
            ),
            observed_size_bytes=(
                None if row["observed_size_bytes"] is None else int(row["observed_size_bytes"])
            ),
            observed_media_type=(
                None if row["observed_media_type"] is None else str(row["observed_media_type"])
            ),
            observed_compression=(
                None
                if row["observed_compression"] is None
                else ArtifactCompression(str(row["observed_compression"]))
            ),
            provider_observed_at=row["provider_observed_at"],
        )


def _provider_observation(result: dict[str, Any]) -> str:
    token = result.get("provider_observation")
    if (
        not isinstance(token, str)
        or not token.startswith("fs2_provider_observation.")
        or len(token.encode()) > 16 * 1024
    ):
        raise RuntimeError("artifact broker omitted the issuer-signed provider observation")
    return token


def _discovery(result: dict[str, Any]) -> tuple[str, int, str, ArtifactCompression | None, datetime] | None:
    try:
        body = result["result"]
        version = body["object_version_id"]
        if version is None:
            if any(body.get(field) is not None for field in ("size_bytes", "media_type", "compression", "observed_at")):
                raise ValueError("absent object returned provider metadata")
            return None
        if not isinstance(version, str) or not version or version == "null" or len(version) > 1024:
            raise ValueError("provider version is invalid")
        size = body["size_bytes"]
        media_type = body["media_type"]
        compression = body["compression"]
        observed_at = body["observed_at"]
        if not isinstance(size, int) or not 0 <= size <= 1 << 40:
            raise ValueError("provider size is invalid")
        if not isinstance(media_type, str) or not 3 <= len(media_type) <= 128:
            raise ValueError("provider media type is invalid")
        parsed_compression = None if compression is None else ArtifactCompression(str(compression))
        parsed_observed_at = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        if parsed_observed_at.tzinfo is None or parsed_observed_at > datetime.now(UTC):
            raise ValueError("provider timestamp is invalid")
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("artifact broker returned an invalid abandoned-upload discovery") from error
    return version, size, media_type, parsed_compression, parsed_observed_at


async def cleanup_abandoned_uploads(
    *,
    database_url: str,
    broker_config: ArtifactCredentialBrokerConfig,
    now: datetime | None = None,
) -> int:
    """Retain abandoned uploads without claiming while transfer quiescence is unproved."""

    if not ABANDONED_UPLOAD_CLEANUP_ACTIVATED:
        return 0

    current = datetime.now(UTC) if now is None else now.astimezone(UTC)
    broker = ArtifactCredentialBroker(broker_config)
    pool = await asyncpg.create_pool(
        dsn=database_url.replace("postgresql+asyncpg://", "postgresql://", 1),
        min_size=1,
        max_size=1,
        command_timeout=30,
        server_settings={"application_name": "fs2-artifact-orphan-cleanup"},
    )
    assert pool is not None
    completed = 0
    deadline = time.monotonic() + ABANDONED_UPLOAD_RUN_BUDGET_SECONDS
    try:
        rows = await pool.fetch(
            "SELECT * FROM fs2_scientific_claim_abandoned_uploads($1,$2)",
            current - ABANDONED_UPLOAD_GRACE,
            ABANDONED_UPLOAD_LIMIT,
        )
        for raw in rows:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            upload = AbandonedUpload.from_row(raw)
            try:
                async with asyncio.timeout(
                    min(ABANDONED_UPLOAD_ITEM_TIMEOUT_SECONDS, remaining)
                ):
                    version = upload.object_version_id
                    if version is None:
                        discovery_result = await broker.operation(
                            broker._request(
                                tenant_id=upload.tenant_id,
                                storage_key=upload.storage_key,
                                action="list-upload-version",
                                object_version_id=None,
                                minimum_ttl_seconds=120,
                            )
                        )
                        discovered = _discovery(discovery_result)
                        _provider_observation(discovery_result)
                        if discovered is None:
                            # The issuer has atomically retained the signed,
                            # post-expiry absence receipt before the broker can
                            # return it. No caller-computed digest is trusted.
                            completed += 1
                            continue
                        # The observation issuer atomically records the exact
                        # immutable version as quarantined. Provider bytes are
                        # intentionally retained; this cleanup lane never has
                        # destructive object authority.
                    completed += 1
            except Exception as error:
                # Provider or per-item database failures are isolated to this
                # cleanup-only schedule. Mandatory retention runs in a separate
                # CronJob and never waits on this path.
                LOGGER.warning(
                    "artifact orphan cleanup item deferred",
                    extra={"upload_id": str(upload.upload_id), "error_type": type(error).__name__},
                )
    finally:
        await broker.close()
        await pool.close()
    return completed


__all__ = [
    "ABANDONED_UPLOAD_GRACE",
    "ABANDONED_UPLOAD_ITEM_TIMEOUT_SECONDS",
    "ABANDONED_UPLOAD_LIMIT",
    "ABANDONED_UPLOAD_RUN_BUDGET_SECONDS",
    "ABANDONED_UPLOAD_CLEANUP_ACTIVATED",
    "AbandonedUpload",
    "cleanup_abandoned_uploads",
]
