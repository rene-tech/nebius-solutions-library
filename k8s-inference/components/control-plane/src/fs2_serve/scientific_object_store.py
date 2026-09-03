"""Real S3-compatible object storage for scientific artifacts.

Signing is delegated to the AWS SDK (``botocore``) so that presigned handles
carry a genuine SigV4 ``X-Amz-Signature`` and are accepted by an unmodified
S3-compatible gateway. The control plane never buffers a whole artifact: the
digest is recomputed by streaming bounded chunks, and object bytes are
discarded as soon as they are hashed.

Nothing in this module logs a URL, a signature, a credential, or object bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from .scientific_artifacts import (
    MAX_ARTIFACT_BYTES,
    ArtifactCompression,
    ArtifactNotFoundError,
    ArtifactPolicyError,
    ArtifactServiceError,
    ArtifactVerificationError,
    EphemeralHandle,
    VerifiedStoredObject,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

STREAM_CHUNK_BYTES = 8 * 1024 * 1024
_CONTENT_ENCODING = {"gzip": ArtifactCompression.GZIP, "zstd": ArtifactCompression.ZSTD}
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})


class ArtifactStorageUnavailableError(ArtifactServiceError):
    """The object store could not be reached or refused the request."""

    code = "artifact_storage_unavailable"


@dataclass(frozen=True, slots=True)
class ObjectStoreConfig:
    """Bounded, provider-neutral S3-compatible connection identity."""

    endpoint_url: str
    bucket: str
    region: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    addressing_style: Literal["path", "virtual", "auto"] = "path"
    verify_tls: bool = True
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    max_attempts: int = 3
    max_stream_bytes: int = MAX_ARTIFACT_BYTES
    chunk_bytes: int = STREAM_CHUNK_BYTES

    def __post_init__(self) -> None:
        if not self.endpoint_url.startswith(("https://", "http://")):
            raise ValueError("artifact store endpoint must be an absolute HTTP(S) URL")
        if self.verify_tls and not self.endpoint_url.startswith("https://"):
            raise ValueError("TLS verification requires an https artifact store endpoint")
        if not self.bucket or not self.region:
            raise ValueError("artifact store bucket and region are required")
        if not self.access_key or not self.secret_key:
            raise ValueError("artifact store credentials are required")
        if not 1 <= self.chunk_bytes <= 64 * 1024 * 1024:
            raise ValueError("artifact stream chunk size is outside the supported range")
        if not 1 <= self.max_stream_bytes <= MAX_ARTIFACT_BYTES:
            raise ValueError("artifact stream ceiling is outside the supported range")


def _is_missing(error: ClientError) -> bool:
    response = error.response or {}
    code = str((response.get("Error") or {}).get("Code", ""))
    statuses = response.get("ResponseMetadata") or {}
    return code in _MISSING_CODES or int(statuses.get("HTTPStatusCode", 0)) == 404


class S3ArtifactObjectStore:
    """Presigns short-lived handles and independently verifies stored objects."""

    def __init__(self, config: ObjectStoreConfig) -> None:
        self._config = config
        self._client = boto3.session.Session().client(
            "s3",
            endpoint_url=config.endpoint_url,
            region_name=config.region,
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            use_ssl=config.endpoint_url.startswith("https://"),
            verify=config.verify_tls,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": config.addressing_style},
                retries={"max_attempts": config.max_attempts, "mode": "standard"},
                connect_timeout=config.connect_timeout_seconds,
                read_timeout=config.read_timeout_seconds,
                user_agent_extra="fs2-serve-scientific-artifacts",
            ),
        )

    @property
    def bucket(self) -> str:
        return self._config.bucket

    @staticmethod
    def _window(ttl: timedelta) -> tuple[int, datetime]:
        """Anchor the handle deadline to the same wall clock the SDK signs with.

        ``generate_presigned_url`` stamps ``X-Amz-Date`` from the current time
        and accepts only a duration, so the advertised ``expires_at`` is derived
        here rather than taken from the caller. Otherwise the handle would claim
        a deadline the gateway does not actually enforce.
        """

        seconds = int(ttl.total_seconds())
        if seconds < 1:
            raise ArtifactPolicyError("artifact handle lifetime must be at least one second")
        return seconds, datetime.now(UTC) + timedelta(seconds=seconds)

    def _presign(self, operation: str, params: dict[str, Any], expires_in: int) -> str:
        url: str = self._client.generate_presigned_url(operation, Params=params, ExpiresIn=expires_in, HttpMethod=None)
        return url

    async def presign_upload(
        self,
        *,
        storage_key: str,
        media_type: str,
        compression: ArtifactCompression | None,
        ttl: timedelta,
    ) -> EphemeralHandle:
        """Return a write handle whose signature binds key, type and encoding."""

        expires_in, expires_at = self._window(ttl)
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": storage_key,
            "ContentType": media_type,
        }
        headers = {"content-type": media_type}
        if compression is not None:
            params["ContentEncoding"] = compression.value
            headers["content-encoding"] = compression.value
        try:
            url = await asyncio.to_thread(self._presign, "put_object", params, expires_in)
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("artifact upload handle could not be issued") from error
        return EphemeralHandle(method="PUT", url=url, expires_at=expires_at, write_once=True, headers=headers)

    async def presign_download(self, *, storage_key: str, ttl: timedelta) -> EphemeralHandle:
        expires_in, expires_at = self._window(ttl)
        params = {"Bucket": self._config.bucket, "Key": storage_key}
        try:
            url = await asyncio.to_thread(self._presign, "get_object", params, expires_in)
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("artifact download handle could not be issued") from error
        return EphemeralHandle(method="GET", url=url, expires_at=expires_at, write_once=False, headers={})

    def _stream_digest(self, storage_key: str, ceiling: int) -> tuple[str, int, str, str | None]:
        """Hash the object in bounded chunks; bytes are never retained."""

        response = self._client.get_object(Bucket=self._config.bucket, Key=storage_key)
        body = response["Body"]
        digest = hashlib.sha256()
        size = 0
        try:
            chunks: Iterator[bytes] = body.iter_chunks(chunk_size=self._config.chunk_bytes)
            for chunk in chunks:
                size += len(chunk)
                if size > ceiling:
                    raise ArtifactVerificationError("stored object exceeds the accepted artifact ceiling")
                digest.update(chunk)
        finally:
            body.close()
        media_type = str(response.get("ContentType") or "application/octet-stream").split(";", 1)[0].strip()
        encoding = response.get("ContentEncoding")
        return f"sha256:{digest.hexdigest()}", size, media_type.lower(), encoding

    async def inspect(self, storage_key: str, *, max_bytes: int | None = None) -> VerifiedStoredObject:
        """Independently measure the stored object without returning its bytes."""

        requested = self._config.max_stream_bytes if max_bytes is None else max_bytes
        ceiling = min(requested, self._config.max_stream_bytes)
        try:
            digest, size, media_type, encoding = await asyncio.to_thread(self._stream_digest, storage_key, ceiling)
        except ClientError as error:
            if _is_missing(error):
                raise ArtifactNotFoundError("stored object is absent") from None
            raise ArtifactStorageUnavailableError("stored object could not be read") from error
        except BotoCoreError as error:
            raise ArtifactStorageUnavailableError("stored object could not be read") from error
        compression = _CONTENT_ENCODING.get(str(encoding or "").lower()) if encoding else None
        return VerifiedStoredObject(
            storage_key=storage_key,
            digest=digest,
            size_bytes=size,
            media_type=media_type,
            compression=compression,
        )

    def _delete(self, storage_key: str) -> None:
        self._client.delete_object(Bucket=self._config.bucket, Key=storage_key)

    async def delete(self, storage_key: str) -> None:
        """Idempotently remove one retired object; absence is success."""

        try:
            await asyncio.to_thread(self._delete, storage_key)
        except ClientError as error:
            if _is_missing(error):
                return
            raise ArtifactStorageUnavailableError("stored object could not be deleted") from error
        except BotoCoreError as error:
            raise ArtifactStorageUnavailableError("stored object could not be deleted") from error

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)
