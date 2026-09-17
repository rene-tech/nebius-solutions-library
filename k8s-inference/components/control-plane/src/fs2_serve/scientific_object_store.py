"""Real S3-compatible object storage for scientific artifacts.

Signing is delegated to the AWS SDK (``botocore``) so that presigned handles
carry a genuine SigV4 ``X-Amz-Signature`` and are accepted by an unmodified
S3-compatible gateway. Finalization measures an exact provider object version
in bounded chunks, and subsequent reads and presigned downloads name that same
immutable version. This adapter hands bounded chunks to its caller; the service
layer buffers only its separately configured small inline-read ceiling so it
can verify the complete digest before releasing the first byte. ``put_object``
holds the bounded inline upload body supplied by the service.

Nothing in this module logs a URL, a signature, a credential, or object bytes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
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
    assert_tenant_storage_key,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Iterator

STREAM_CHUNK_BYTES = 8 * 1024 * 1024
_CONTENT_ENCODING = {"gzip": ArtifactCompression.GZIP, "zstd": ArtifactCompression.ZSTD}
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})
_CONTENT_ADDRESS = re.compile(r"/sha256/([a-f0-9]{64})$")


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
    session_token: str | None = field(default=None, repr=False)
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
        if self.session_token is not None and not self.session_token:
            raise ValueError("artifact store session token must be non-empty when supplied")
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
            aws_session_token=config.session_token,
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

    @staticmethod
    def _checksum(storage_key: str) -> str:
        match = _CONTENT_ADDRESS.search(storage_key)
        if match is None:
            raise ArtifactVerificationError("stored object key has no canonical content address")
        return base64.b64encode(bytes.fromhex(match.group(1))).decode("ascii")

    async def presign_upload(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        media_type: str,
        compression: ArtifactCompression | None,
        ttl: timedelta,
    ) -> EphemeralHandle:
        """Return a write handle whose signature binds key, type and encoding."""

        assert_tenant_storage_key(tenant_id, storage_key)
        expires_in, expires_at = self._window(ttl)
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": storage_key,
            "ContentType": media_type,
            "IfNoneMatch": "*",
            "ChecksumSHA256": self._checksum(storage_key),
        }
        headers = {
            "content-type": media_type,
            "if-none-match": "*",
            "x-amz-checksum-sha256": self._checksum(storage_key),
        }
        if compression is not None:
            params["ContentEncoding"] = compression.value
            headers["content-encoding"] = compression.value
        try:
            url = await asyncio.to_thread(self._presign, "put_object", params, expires_in)
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("artifact upload handle could not be issued") from error
        return EphemeralHandle(method="PUT", url=url, expires_at=expires_at, write_once=True, headers=headers)

    async def presign_download(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        object_version_id: str,
        ttl: timedelta,
    ) -> EphemeralHandle:
        assert_tenant_storage_key(tenant_id, storage_key)
        expires_in, expires_at = self._window(ttl)
        params = {
            "Bucket": self._config.bucket,
            "Key": storage_key,
            "VersionId": object_version_id,
        }
        try:
            url = await asyncio.to_thread(self._presign, "get_object", params, expires_in)
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("artifact download handle could not be issued") from error
        return EphemeralHandle(method="GET", url=url, expires_at=expires_at, write_once=False, headers={})

    def _put(
        self,
        storage_key: str,
        payload: bytes,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> str:
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": storage_key,
            "Body": payload,
            "ContentType": media_type,
            "IfNoneMatch": "*",
            "ChecksumSHA256": self._checksum(storage_key),
        }
        if compression is not None:
            params["ContentEncoding"] = compression.value
        response = self._client.put_object(**params)
        version_id = response.get("VersionId")
        if not isinstance(version_id, str) or not version_id or version_id == "null":
            raise ArtifactVerificationError("stored object has no immutable provider version")
        return version_id

    async def put_object(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        payload: bytes,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> VerifiedStoredObject:
        """Persist one bounded object, then measure what was actually stored.

        The measurement is a fresh read rather than an echo of the request, so
        a store that rewrote or re-typed the body cannot be mistaken for one
        that accepted it verbatim.
        """

        assert_tenant_storage_key(tenant_id, storage_key)
        if len(payload) > self._config.max_stream_bytes:
            raise ArtifactPolicyError("artifact exceeds the accepted object ceiling")
        try:
            object_version_id = await asyncio.to_thread(self._put, storage_key, payload, media_type, compression)
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("stored object could not be written") from error
        return await self.inspect(
            tenant_id=tenant_id,
            storage_key=storage_key,
            object_version_id=object_version_id,
            max_bytes=len(payload),
        )

    def _open(self, storage_key: str, object_version_id: str) -> Any:
        response = self._client.get_object(
            Bucket=self._config.bucket,
            Key=storage_key,
            VersionId=object_version_id,
            ChecksumMode="ENABLED",
        )
        return response["Body"]

    async def stream_object(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        object_version_id: str,
        max_bytes: int | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield the stored object in bounded chunks without ever buffering it."""

        assert_tenant_storage_key(tenant_id, storage_key)
        requested = self._config.max_stream_bytes if max_bytes is None else max_bytes
        ceiling = min(requested, self._config.max_stream_bytes)
        try:
            body = await asyncio.to_thread(self._open, storage_key, object_version_id)
        except ClientError as error:
            if _is_missing(error):
                raise ArtifactNotFoundError("stored object is absent") from None
            raise ArtifactStorageUnavailableError("stored object could not be read") from error
        except BotoCoreError as error:
            raise ArtifactStorageUnavailableError("stored object could not be read") from error
        total = 0
        try:
            while True:
                try:
                    chunk: bytes = await asyncio.to_thread(body.read, self._config.chunk_bytes)
                except (BotoCoreError, ClientError) as error:
                    raise ArtifactStorageUnavailableError("stored object could not be read") from error
                if not chunk:
                    break
                total += len(chunk)
                if total > ceiling:
                    raise ArtifactVerificationError("stored object exceeds the accepted artifact ceiling")
                yield chunk
        finally:
            await asyncio.to_thread(body.close)

    def _stream_digest(
        self,
        storage_key: str,
        object_version_id: str | None,
        ceiling: int,
    ) -> tuple[str, int, str, str | None, str]:
        """Hash the object in bounded chunks; bytes are never retained."""

        params: dict[str, Any] = {"Bucket": self._config.bucket, "Key": storage_key, "ChecksumMode": "ENABLED"}
        if object_version_id is not None:
            params["VersionId"] = object_version_id
        response = self._client.get_object(**params)
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
        measured_version = response.get("VersionId")
        if not isinstance(measured_version, str) or not measured_version or measured_version == "null":
            raise ArtifactVerificationError("stored object has no immutable provider version")
        if object_version_id is not None and measured_version != object_version_id:
            raise ArtifactVerificationError("stored object version differs from the requested version")
        return f"sha256:{digest.hexdigest()}", size, media_type.lower(), encoding, measured_version

    async def inspect(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        object_version_id: str | None = None,
        max_bytes: int | None = None,
    ) -> VerifiedStoredObject:
        """Independently measure the stored object without returning its bytes."""

        assert_tenant_storage_key(tenant_id, storage_key)
        requested = self._config.max_stream_bytes if max_bytes is None else max_bytes
        ceiling = min(requested, self._config.max_stream_bytes)
        try:
            digest, size, media_type, encoding, measured_version = await asyncio.to_thread(
                self._stream_digest,
                storage_key,
                object_version_id,
                ceiling,
            )
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
            object_version_id=measured_version,
        )

    def _delete(self, storage_key: str, object_version_id: str) -> None:
        self._client.delete_object(
            Bucket=self._config.bucket,
            Key=storage_key,
            VersionId=object_version_id,
        )

    async def delete(self, *, tenant_id: str, storage_key: str, object_version_id: str) -> None:
        """Idempotently remove one retired object; absence is success."""

        assert_tenant_storage_key(tenant_id, storage_key)
        try:
            await asyncio.to_thread(self._delete, storage_key, object_version_id)
        except ClientError as error:
            if _is_missing(error):
                return
            raise ArtifactStorageUnavailableError("stored object could not be deleted") from error
        except BotoCoreError as error:
            raise ArtifactStorageUnavailableError("stored object could not be deleted") from error

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)
