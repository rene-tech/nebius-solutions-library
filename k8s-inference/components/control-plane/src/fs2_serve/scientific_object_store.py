"""Real S3-compatible object storage for scientific artifacts.

Signing is delegated to the AWS SDK (``botocore``) so that presigned handles
carry a genuine SigV4 ``X-Amz-Signature`` and are accepted by an unmodified
S3-compatible gateway. Verification and reads never buffer a whole artifact:
the digest is recomputed by streaming bounded chunks, object bytes are
discarded as soon as they are hashed, and ``stream_object`` hands bounded
chunks straight to its caller. The one place a whole object is held in memory
is ``put_object``, the inline write path for a customer who can reach only the
public gateway; it is bounded by the configured inline ceiling, and anything
above that ceiling must still use a presigned handle.

Nothing in this module logs a URL, a signature, a credential, or object bytes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from .scientific_artifacts import (
    MAX_ARTIFACT_BYTES,
    ArtifactCompression,
    ArtifactConflictError,
    ArtifactDeletionEvidence,
    ArtifactNotFoundError,
    ArtifactPolicyError,
    ArtifactRecord,
    ArtifactRemovalEvidence,
    ArtifactRemovalEvidenceKind,
    ArtifactRemovalTarget,
    ArtifactServiceError,
    ArtifactUploadSession,
    ArtifactUploadSessionCreationTarget,
    ArtifactUploadSessionReconciliationEvidence,
    LegacyArtifactVersionScan,
    LegacyArtifactVersionTarget,
    ArtifactVerificationError,
    EphemeralHandle,
    VerifiedStoredObject,
    StagedUploadPart,
    UploadIntent,
    artifact_absence_claim_digest,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Iterator

STREAM_CHUNK_BYTES = 8 * 1024 * 1024
MAX_REMOVAL_VERSIONS = 64
MAX_MULTIPART_ABORTS_PER_PASS = 8
MAX_LEGACY_RECOVERY_VERSIONS = 10_000
_CONTENT_ENCODING = {"gzip": ArtifactCompression.GZIP, "zstd": ArtifactCompression.ZSTD}
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})


@dataclass(frozen=True, slots=True)
class _LegacyExpectedObject:
    storage_key: str
    expected_digest: str
    expected_size_bytes: int
    expected_media_type: str
    expected_compression: ArtifactCompression | None


class ArtifactStorageUnavailableError(ArtifactServiceError):
    """The object store could not be reached or refused the request."""

    code = "artifact_storage_unavailable"


class ArtifactRemovalIncompleteError(ArtifactStorageUnavailableError):
    """A bounded cleanup page made progress but exact-key state remains."""

    code = "artifact_removal_incomplete"


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

    async def create_upload_session(
        self,
        *,
        storage_key: str,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> tuple[str, datetime]:
        """Create one provider generation that only the server can complete."""

        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": storage_key,
            "ContentType": media_type,
        }
        if compression is not None:
            params["ContentEncoding"] = compression.value
        try:
            response = await asyncio.to_thread(self._client.create_multipart_upload, **params)
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("artifact upload session could not be created") from error
        provider_upload_id = str(response.get("UploadId") or "")
        if not provider_upload_id:
            raise ArtifactStorageUnavailableError("provider omitted the multipart upload identity")
        return provider_upload_id, datetime.now(UTC)

    async def presign_upload_part(
        self,
        *,
        session: ArtifactUploadSession,
        part_number: int,
        size_bytes: int,
        checksum: str,
        ttl: timedelta,
    ) -> EphemeralHandle:
        """Sign one length/checksum-bound part; no client can complete the object."""

        expires_in, expires_at = self._window(ttl)
        checksum_b64 = base64.b64encode(bytes.fromhex(checksum.removeprefix("sha256:"))).decode()
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": session.storage_key,
            "UploadId": session.provider_upload_id,
            "PartNumber": part_number,
            "ContentLength": size_bytes,
            "ChecksumSHA256": checksum_b64,
        }
        headers = {
            "content-length": str(size_bytes),
            "x-amz-checksum-sha256": checksum_b64,
        }
        try:
            url = await asyncio.to_thread(self._presign, "upload_part", params, expires_in)
        except (BotoCoreError, ClientError, ValueError) as error:
            raise ArtifactStorageUnavailableError("artifact upload-part handle could not be issued") from error
        return EphemeralHandle(method="PUT", url=url, expires_at=expires_at, write_once=True, headers=headers)

    async def presign_legacy_upload(
        self, *, intent: UploadIntent, ttl: timedelta
    ) -> EphemeralHandle:
        """Reissue one pre-0031 PutObject handle with exact body constraints.

        A retained v1 client may legitimately retry the same idempotent begin
        after expansion. Bucket versioning means this capability is replayable,
        so the response says so explicitly; finalization resolves the exact
        digest/size/type version rather than trusting the latest version.
        """

        expires_in, expires_at = self._window(ttl)
        checksum_b64 = base64.b64encode(
            bytes.fromhex(intent.expected_digest.removeprefix("sha256:"))
        ).decode()
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": intent.storage_key,
            "ContentType": intent.media_type,
            "ContentLength": intent.expected_size_bytes,
            "ChecksumSHA256": checksum_b64,
        }
        headers = {
            "content-type": intent.media_type,
            "content-length": str(intent.expected_size_bytes),
            "x-amz-checksum-sha256": checksum_b64,
        }
        if intent.compression is not None:
            params["ContentEncoding"] = intent.compression.value
            headers["content-encoding"] = intent.compression.value
        try:
            url = await asyncio.to_thread(self._presign, "put_object", params, expires_in)
        except (BotoCoreError, ClientError, ValueError) as error:
            raise ArtifactStorageUnavailableError("legacy artifact handle could not be reissued") from error
        return EphemeralHandle(
            method="PUT",
            url=url,
            expires_at=expires_at,
            write_once=False,
            headers=headers,
        )

    def _stage_inline_upload(
        self, session: ArtifactUploadSession, intent: UploadIntent, payload: bytes
    ) -> str:
        request_id = ""
        for part_number in range(1, session.part_count + 1):
            start = (part_number - 1) * session.part_size_bytes
            body = payload[start : start + session.part_size_bytes]
            checksum_b64 = base64.b64encode(hashlib.sha256(body).digest()).decode()
            response = self._client.upload_part(
                Bucket=self._config.bucket,
                Key=session.storage_key,
                UploadId=session.provider_upload_id,
                PartNumber=part_number,
                Body=body,
                ContentLength=len(body),
                ChecksumSHA256=checksum_b64,
            )
            request_id = self._request_id(response)
        return request_id

    async def stage_inline_upload(
        self, *, session: ArtifactUploadSession, intent: UploadIntent, payload: bytes
    ) -> StagedUploadPart:
        if len(payload) != intent.expected_size_bytes:
            raise ArtifactVerificationError("inline upload length differs from its reservation")
        try:
            provider_request_id = await asyncio.to_thread(
                self._stage_inline_upload,
                session,
                intent,
                payload,
            )
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("inline upload parts could not be staged") from error
        return StagedUploadPart(
            storage_key=session.storage_key,
            digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            size_bytes=len(payload),
            media_type=intent.media_type,
            compression=intent.compression,
            provider_request_id=provider_request_id,
        )

    async def presign_download(
        self, *, storage_key: str, provider_version_id: str, ttl: timedelta
    ) -> EphemeralHandle:
        expires_in, expires_at = self._window(ttl)
        params = {
            "Bucket": self._config.bucket,
            "Key": storage_key,
            "VersionId": provider_version_id,
        }
        try:
            url = await asyncio.to_thread(self._presign, "get_object", params, expires_in)
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("artifact download handle could not be issued") from error
        return EphemeralHandle(method="GET", url=url, expires_at=expires_at, write_once=False, headers={})

    def _list_parts(self, session: ArtifactUploadSession) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        marker: int | None = None
        while True:
            params: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Key": session.storage_key,
                "UploadId": session.provider_upload_id,
                "MaxParts": 1000,
            }
            if marker is not None:
                params["PartNumberMarker"] = marker
            response = self._client.list_parts(**params)
            parts.extend(response.get("Parts", ()))
            if not response.get("IsTruncated"):
                break
            marker = int(response.get("NextPartNumberMarker") or 0)
            if marker < 1:
                raise ArtifactStorageUnavailableError("provider multipart pagination omitted its cursor")
        return sorted(parts, key=lambda item: int(item.get("PartNumber") or 0))

    def _validated_completion_parts(
        self, session: ArtifactUploadSession, intent: UploadIntent
    ) -> list[dict[str, Any]]:
        parts = self._list_parts(session)
        if len(parts) != session.part_count:
            raise ArtifactVerificationError("multipart upload does not contain every reserved part")
        total = 0
        completion_parts: list[dict[str, Any]] = []
        for index, part in enumerate(parts, start=1):
            part_number = int(part.get("PartNumber") or 0)
            size = int(part.get("Size") or 0)
            expected_size = (
                session.part_size_bytes
                if index < session.part_count
                else intent.expected_size_bytes - session.part_size_bytes * (session.part_count - 1)
            )
            if part_number != index or size != expected_size:
                raise ArtifactVerificationError("multipart upload part shape differs from its reservation")
            etag = str(part.get("ETag") or "")
            if not etag:
                raise ArtifactStorageUnavailableError("provider multipart part omitted its ETag")
            completion = {"ETag": etag, "PartNumber": part_number}
            if part.get("ChecksumSHA256"):
                completion["ChecksumSHA256"] = str(part["ChecksumSHA256"])
            completion_parts.append(completion)
            total += size
        if total != intent.expected_size_bytes:
            raise ArtifactVerificationError("multipart upload byte total differs from its reservation")
        return completion_parts

    def _complete_upload_session(
        self, session: ArtifactUploadSession, intent: UploadIntent
    ) -> tuple[str, str]:
        completion_parts = self._validated_completion_parts(session, intent)
        response = self._client.complete_multipart_upload(
            Bucket=self._config.bucket,
            Key=session.storage_key,
            UploadId=session.provider_upload_id,
            MultipartUpload={"Parts": completion_parts},
        )
        provider_version_id = str(response.get("VersionId") or "")
        if not provider_version_id:
            raise ArtifactStorageUnavailableError("provider completion omitted the immutable version identity")
        return provider_version_id, self._request_id(response)

    async def validate_upload_session(
        self, *, session: ArtifactUploadSession, intent: UploadIntent
    ) -> None:
        """Validate exact parts before installing a cleanup-excluding mutation fence."""

        try:
            await asyncio.to_thread(self._validated_completion_parts, session, intent)
        except ClientError as error:
            if _is_missing(error):
                raise ArtifactNotFoundError("multipart upload session is absent") from None
            raise ArtifactStorageUnavailableError("artifact upload session could not be checked") from error
        except BotoCoreError as error:
            raise ArtifactStorageUnavailableError("artifact upload session could not be checked") from error

    async def complete_upload_session(
        self, *, session: ArtifactUploadSession, intent: UploadIntent
    ) -> VerifiedStoredObject:
        try:
            provider_version_id, provider_request_id = await asyncio.to_thread(
                self._complete_upload_session,
                session,
                intent,
            )
        except ClientError as error:
            if _is_missing(error):
                raise ArtifactNotFoundError("multipart upload session is absent") from None
            raise ArtifactStorageUnavailableError("artifact upload session could not be completed") from error
        except BotoCoreError as error:
            raise ArtifactStorageUnavailableError("artifact upload session could not be completed") from error
        verified = await self.inspect(
            session.storage_key,
            provider_version_id=provider_version_id,
            max_bytes=intent.expected_size_bytes,
        )
        return verified.model_copy(update={"provider_request_id": provider_request_id})

    async def abort_upload_session(self, *, session: ArtifactUploadSession) -> str:
        try:
            response = await asyncio.to_thread(
                self._client.abort_multipart_upload,
                Bucket=self._config.bucket,
                Key=session.storage_key,
                UploadId=session.provider_upload_id,
            )
        except ClientError as error:
            if _is_missing(error):
                return self._request_id(error.response or {})
            raise ArtifactStorageUnavailableError("artifact upload session could not be aborted") from error
        except BotoCoreError as error:
            raise ArtifactStorageUnavailableError("artifact upload session could not be aborted") from error
        return self._request_id(response)

    async def recover_completed_upload(self, *, intent: UploadIntent) -> VerifiedStoredObject:
        """Recover only one unambiguous provider version after a completion/DB crash gap."""

        try:
            versions, _ = await asyncio.to_thread(
                self._exact_versions_page,
                intent.storage_key,
                limit=2,
            )
            object_versions = [item for item in versions if not item[1] and item[0] not in {None, "null"}]
            sessions, _ = await asyncio.to_thread(
                self._exact_multipart_uploads_page,
                intent.storage_key,
                limit=1,
            )
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("completed artifact identity could not be recovered") from error
        if len(object_versions) != 1 or sessions:
            raise ArtifactConflictError("completed artifact does not have one fenced provider version")
        provider_version_id = object_versions[0][0]
        assert provider_version_id is not None
        return await self.inspect(
            intent.storage_key,
            provider_version_id=provider_version_id,
            max_bytes=intent.expected_size_bytes,
        )

    def _recover_legacy_upload(
        self, intent: UploadIntent | _LegacyExpectedObject
    ) -> VerifiedStoredObject:
        key_marker: str | None = None
        version_marker: str | None = None
        inspected = 0
        while inspected < MAX_LEGACY_RECOVERY_VERSIONS:
            params: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Prefix": intent.storage_key,
                "MaxKeys": min(MAX_REMOVAL_VERSIONS, MAX_LEGACY_RECOVERY_VERSIONS - inspected),
            }
            if key_marker is not None:
                params["KeyMarker"] = key_marker
                params["VersionIdMarker"] = version_marker
            page = self._client.list_object_versions(**params)
            candidates: list[tuple[str, str | None, int | None]] = []
            exact_entries = 0
            for entry in page.get("Versions", ()):
                if entry.get("Key") != intent.storage_key:
                    continue
                exact_entries += 1
                provider_version_id = str(entry.get("VersionId") or "")
                if provider_version_id and provider_version_id != "null":
                    candidates.append(
                        (
                            provider_version_id,
                            str(entry.get("LastModified") or "") or None,
                            int(entry["Size"]) if entry.get("Size") is not None else None,
                        )
                    )
            exact_entries += sum(
                1
                for entry in page.get("DeleteMarkers", ())
                if entry.get("Key") == intent.storage_key
            )
            inspected += exact_entries
            for provider_version_id, _last_modified, listed_size in sorted(
                candidates,
                key=lambda item: (item[1] or "", item[0]),
                reverse=True,
            ):
                if listed_size is not None and listed_size != intent.expected_size_bytes:
                    continue
                try:
                    digest, size, media_type, encoding, request_id = self._stream_digest(
                        intent.storage_key,
                        provider_version_id,
                        intent.expected_size_bytes,
                    )
                except ClientError as error:
                    if _is_missing(error):
                        continue
                    raise
                compression = _CONTENT_ENCODING.get(str(encoding or "").lower()) if encoding else None
                if (
                    digest == intent.expected_digest
                    and size == intent.expected_size_bytes
                    and media_type == intent.media_type
                    and compression == intent.compression
                ):
                    return VerifiedStoredObject(
                        storage_key=intent.storage_key,
                        provider_version_id=provider_version_id,
                        provider_request_id=request_id,
                        digest=digest,
                        size_bytes=size,
                        media_type=media_type,
                        compression=compression,
                    )
            if not page.get("IsTruncated"):
                break
            next_key = str(page.get("NextKeyMarker") or "") or None
            next_version = str(page.get("NextVersionIdMarker") or "") or None
            if next_key != intent.storage_key or next_version is None:
                break
            key_marker, version_marker = next_key, next_version
        if inspected >= MAX_LEGACY_RECOVERY_VERSIONS:
            raise ArtifactConflictError("legacy upload version inventory exceeds the bounded recovery limit")
        raise ArtifactVerificationError("no retained provider version matches the legacy upload intent")

    async def recover_legacy_upload(self, *, intent: UploadIntent) -> VerifiedStoredObject:
        """Resolve one replayable pre-0031 PUT by immutable content identity."""

        try:
            return await asyncio.to_thread(self._recover_legacy_upload, intent)
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("legacy artifact version recovery failed") from error

    async def recover_legacy_artifact(
        self, *, artifact: ArtifactRecord
    ) -> VerifiedStoredObject:
        """Resolve one historical record without trusting the mutable latest version."""

        expected = _LegacyExpectedObject(
            storage_key=artifact.storage_key,
            expected_digest=artifact.digest,
            expected_size_bytes=artifact.size_bytes,
            expected_media_type=artifact.media_type,
            expected_compression=artifact.compression,
        )
        try:
            return await asyncio.to_thread(self._recover_legacy_upload, expected)
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("legacy artifact version recovery failed") from error

    def _put(
        self,
        storage_key: str,
        payload: bytes,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> tuple[str, str]:
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": storage_key,
            "Body": payload,
            "ContentType": media_type,
            "ContentLength": len(payload),
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(payload).digest()).decode(),
            "IfNoneMatch": "*",
        }
        if compression is not None:
            params["ContentEncoding"] = compression.value
        response = self._client.put_object(**params)
        provider_version_id = str(response.get("VersionId") or "")
        if not provider_version_id:
            raise ArtifactStorageUnavailableError("provider write omitted the immutable version identity")
        return provider_version_id, self._request_id(response)

    async def put_object(
        self,
        *,
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

        if len(payload) > self._config.max_stream_bytes:
            raise ArtifactPolicyError("artifact exceeds the accepted object ceiling")
        try:
            provider_version_id, provider_request_id = await asyncio.to_thread(
                self._put, storage_key, payload, media_type, compression
            )
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("stored object could not be written") from error
        verified = await self.inspect(
            storage_key,
            provider_version_id=provider_version_id,
            max_bytes=len(payload),
        )
        return verified.model_copy(update={"provider_request_id": provider_request_id})

    def _put_legacy_object(
        self,
        storage_key: str,
        payload: bytes,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> tuple[str, str]:
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Key": storage_key,
            "Body": payload,
            "ContentType": media_type,
            "ContentLength": len(payload),
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(payload).digest()).decode(),
        }
        if compression is not None:
            params["ContentEncoding"] = compression.value
        response = self._client.put_object(**params)
        provider_version_id = str(response.get("VersionId") or "")
        if not provider_version_id:
            raise ArtifactStorageUnavailableError(
                "legacy provider write omitted the immutable version identity"
            )
        return provider_version_id, self._request_id(response)

    async def put_legacy_object(
        self,
        *,
        storage_key: str,
        payload: bytes,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> VerifiedStoredObject:
        """Create one exact grandfathered v1 version after a durable DB fence."""

        if len(payload) > self._config.max_stream_bytes:
            raise ArtifactPolicyError("artifact exceeds the accepted object ceiling")
        try:
            provider_version_id, provider_request_id = await asyncio.to_thread(
                self._put_legacy_object,
                storage_key,
                payload,
                media_type,
                compression,
            )
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("legacy object could not be written") from error
        verified = await self.inspect(
            storage_key,
            provider_version_id=provider_version_id,
            max_bytes=len(payload),
        )
        return verified.model_copy(update={"provider_request_id": provider_request_id})

    def _open(self, storage_key: str, provider_version_id: str) -> Any:
        response = self._client.get_object(
            Bucket=self._config.bucket,
            Key=storage_key,
            VersionId=provider_version_id,
        )
        return response["Body"]

    async def stream_object(
        self,
        storage_key: str,
        *,
        provider_version_id: str,
        max_bytes: int | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield the stored object in bounded chunks without ever buffering it."""

        requested = self._config.max_stream_bytes if max_bytes is None else max_bytes
        ceiling = min(requested, self._config.max_stream_bytes)
        try:
            body = await asyncio.to_thread(self._open, storage_key, provider_version_id)
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
        self, storage_key: str, provider_version_id: str, ceiling: int
    ) -> tuple[str, int, str, str | None, str]:
        """Hash the object in bounded chunks; bytes are never retained."""

        response = self._client.get_object(
            Bucket=self._config.bucket,
            Key=storage_key,
            VersionId=provider_version_id,
        )
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
        return (
            f"sha256:{digest.hexdigest()}",
            size,
            media_type.lower(),
            encoding,
            self._request_id(response),
        )

    async def inspect(
        self,
        storage_key: str,
        *,
        provider_version_id: str,
        max_bytes: int | None = None,
    ) -> VerifiedStoredObject:
        """Independently measure the stored object without returning its bytes."""

        requested = self._config.max_stream_bytes if max_bytes is None else max_bytes
        ceiling = min(requested, self._config.max_stream_bytes)
        try:
            digest, size, media_type, encoding, provider_request_id = await asyncio.to_thread(
                self._stream_digest,
                storage_key,
                provider_version_id,
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
            provider_version_id=provider_version_id,
            provider_request_id=provider_request_id,
            digest=digest,
            size_bytes=size,
            media_type=media_type,
            compression=compression,
        )

    @staticmethod
    def _request_id(response: dict[str, Any]) -> str:
        request_id = str((response.get("ResponseMetadata") or {}).get("RequestId") or "")
        if not request_id:
            raise ArtifactStorageUnavailableError("provider removal response omitted its request identity")
        return request_id

    def _exact_versions(
        self, storage_key: str
    ) -> tuple[list[tuple[str | None, bool, bool, str | None, int | None, str | None]], str]:
        versions: list[tuple[str | None, bool, bool, str | None, int | None, str | None]] = []
        request_id = ""
        paginator = self._client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self._config.bucket, Prefix=storage_key):
            request_id = self._request_id(page)
            for entry in (*page.get("Versions", ()), *page.get("DeleteMarkers", ())):
                if entry.get("Key") != storage_key:
                    continue
                version_id = str(entry.get("VersionId") or "") or None
                last_modified = entry.get("LastModified")
                versions.append(
                    (
                        version_id,
                        entry in page.get("DeleteMarkers", ()),
                        bool(entry.get("IsLatest", False)),
                        str(entry.get("ETag") or "") or None,
                        int(entry["Size"]) if entry.get("Size") is not None else None,
                        last_modified.isoformat() if isinstance(last_modified, datetime) else None,
                    )
                )
        ordered = sorted(
            versions,
            key=lambda item: tuple("" if value is None else str(value) for value in item),
        )
        return ordered, request_id

    def _exact_multipart_uploads(self, storage_key: str) -> tuple[list[tuple[str, str]], str]:
        """Return every still-completable provider session for one exact key."""

        uploads: list[tuple[str, str]] = []
        key_marker: str | None = None
        upload_marker: str | None = None
        request_id = ""
        while True:
            params: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Prefix": storage_key,
                "MaxUploads": 1000,
            }
            if key_marker is not None:
                params["KeyMarker"] = key_marker
            if upload_marker is not None:
                params["UploadIdMarker"] = upload_marker
            page = self._client.list_multipart_uploads(**params)
            request_id = self._request_id(page)
            for entry in page.get("Uploads", ()):
                if entry.get("Key") == storage_key and entry.get("UploadId"):
                    uploads.append((storage_key, str(entry["UploadId"])))
            if not page.get("IsTruncated"):
                break
            key_marker = str(page.get("NextKeyMarker") or "") or None
            upload_marker = str(page.get("NextUploadIdMarker") or "") or None
            if key_marker is None:
                raise ArtifactStorageUnavailableError("provider multipart pagination omitted its cursor")
        return sorted(set(uploads)), request_id

    @staticmethod
    def _multipart_set_digest(uploads: list[tuple[str, str]]) -> str:
        encoded = json.dumps(uploads, sort_keys=False, separators=(",", ":"), ensure_ascii=True).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @staticmethod
    def _version_set_digest(
        versions: list[tuple[str | None, bool, bool, str | None, int | None, str | None]],
    ) -> str:
        encoded = json.dumps(versions, sort_keys=False, separators=(",", ":"), ensure_ascii=True).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def _head_absence_request_id(self, storage_key: str) -> str:
        try:
            self._client.head_object(Bucket=self._config.bucket, Key=storage_key)
        except ClientError as error:
            if _is_missing(error):
                return self._request_id(error.response or {})
            raise
        raise ArtifactStorageUnavailableError("provider did not confirm stored-object absence")

    def _exact_versions_page(
        self, storage_key: str, *, limit: int = MAX_REMOVAL_VERSIONS
    ) -> tuple[list[tuple[str | None, bool, bool, str | None, int | None, str | None]], str]:
        """Read one constant-size exact-key page.

        S3 orders an exact key before any longer key sharing its prefix, so an
        empty exact-key subset in the first page is a complete absence probe;
        attacker-amplified versions of the exact key are processed over later
        claims rather than materialized in one maintenance pass.
        """

        page = self._client.list_object_versions(
            Bucket=self._config.bucket,
            Prefix=storage_key,
            MaxKeys=max(1, min(limit, MAX_REMOVAL_VERSIONS)),
        )
        versions: list[tuple[str | None, bool, bool, str | None, int | None, str | None]] = []
        for delete_marker, entries in (
            (False, page.get("Versions", ())),
            (True, page.get("DeleteMarkers", ())),
        ):
            for entry in entries:
                if entry.get("Key") != storage_key:
                    continue
                version_id = str(entry.get("VersionId") or "") or None
                last_modified = entry.get("LastModified")
                versions.append(
                    (
                        version_id,
                        delete_marker,
                        bool(entry.get("IsLatest", False)),
                        str(entry.get("ETag") or "") or None,
                        int(entry["Size"]) if entry.get("Size") is not None else None,
                        last_modified.isoformat() if isinstance(last_modified, datetime) else None,
                    )
                )
        return sorted(
            versions,
            key=lambda item: tuple("" if value is None else str(value) for value in item),
        ), self._request_id(page)

    def _exact_multipart_uploads_page(
        self, storage_key: str, *, limit: int = MAX_MULTIPART_ABORTS_PER_PASS
    ) -> tuple[list[tuple[str, str]], str]:
        page = self._client.list_multipart_uploads(
            Bucket=self._config.bucket,
            Prefix=storage_key,
            MaxUploads=max(1, min(limit, MAX_MULTIPART_ABORTS_PER_PASS)),
        )
        uploads = sorted(
            {
                (storage_key, str(entry["UploadId"]))
                for entry in page.get("Uploads", ())
                if entry.get("Key") == storage_key and entry.get("UploadId")
            }
        )
        return uploads, self._request_id(page)

    def _delete_version_page(self, storage_key: str) -> tuple[int, str]:
        versions, request_id = self._exact_versions_page(storage_key)
        if not versions:
            try:
                request_id = self._head_absence_request_id(storage_key)
                return 0, request_id
            except ArtifactStorageUnavailableError:
                versions = [(None, False, True, None, None, None)]
        objects: list[dict[str, str]] = []
        for version_id, _delete_marker, _is_latest, _etag, _size, _last_modified in versions:
            item = {"Key": storage_key}
            if version_id not in {None, "null"}:
                item["VersionId"] = version_id
            objects.append(item)
        response = self._client.delete_objects(
            Bucket=self._config.bucket,
            Delete={"Objects": objects, "Quiet": False},
        )
        request_id = self._request_id(response)
        if response.get("Errors"):
            raise ArtifactStorageUnavailableError("provider rejected an exact-key version deletion page")
        removed = len(objects)
        remaining, list_request_id = self._exact_versions_page(storage_key, limit=1)
        if remaining:
            raise ArtifactRemovalIncompleteError("exact-key versions remain after the bounded removal page")
        absence_request_id = self._head_absence_request_id(storage_key)
        return removed, absence_request_id or list_request_id or request_id

    def _abort_multipart_upload_page(self, storage_key: str) -> tuple[int, str, str]:
        uploads, request_id = self._exact_multipart_uploads_page(storage_key)
        aborted = 0
        for key, provider_upload_id in uploads:
            response = self._client.abort_multipart_upload(
                Bucket=self._config.bucket,
                Key=key,
                UploadId=provider_upload_id,
            )
            request_id = self._request_id(response)
            aborted += 1
        remaining, list_request_id = self._exact_multipart_uploads_page(storage_key, limit=1)
        if remaining:
            raise ArtifactRemovalIncompleteError(
                "exact-key multipart sessions remain after the bounded abort page"
            )
        return aborted, list_request_id or request_id, self._multipart_set_digest(remaining)

    async def reconcile_orphan_upload_sessions(
        self, target: ArtifactUploadSessionCreationTarget
    ) -> ArtifactUploadSessionReconciliationEvidence:
        """Boundedly abort every exact-key session after a stale durable claim."""

        try:
            aborted, request_id, digest = await asyncio.to_thread(
                self._abort_multipart_upload_page,
                target.storage_key,
            )
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError(
                "orphan upload-session reconciliation failed"
            ) from error
        return ArtifactUploadSessionReconciliationEvidence(
            storage_key=target.storage_key,
            provider_request_id=request_id,
            aborted_upload_count=aborted,
            multipart_session_set_digest=digest,
            observed_at=self._clock(),
        )

    async def delete(self, target: ArtifactRemovalTarget) -> ArtifactDeletionEvidence:
        """Remove every exact-key version without authorizing quota release."""

        try:
            aborted, multipart_request_id, multipart_digest = await asyncio.to_thread(
                self._abort_multipart_upload_page,
                target.storage_key,
            )
            removed, request_id = await asyncio.to_thread(self._delete_version_page, target.storage_key)
        except ClientError as error:
            raise ArtifactStorageUnavailableError("stored object could not be deleted") from error
        except BotoCoreError as error:
            raise ArtifactStorageUnavailableError("stored object could not be deleted") from error
        return ArtifactDeletionEvidence(
            storage_key=target.storage_key,
            kind=(
                ArtifactRemovalEvidenceKind.ALL_VERSIONS_REMOVED
                if removed
                else ArtifactRemovalEvidenceKind.ABSENCE_CONFIRMED
            ),
            provider_request_id=request_id,
            removed_version_count=removed,
            aborted_upload_count=aborted,
            multipart_list_request_id=multipart_request_id,
            multipart_session_set_digest=multipart_digest,
            observed_at=max(datetime.now(UTC), target.removal_claimed_at),
        )

    def _verify_absent(self, storage_key: str) -> tuple[str, str, str, str, str, str, str, str, str]:
        """Double-snapshot exact multipart sessions and versions around HEAD."""

        first_uploads, first_multipart_request_id = self._exact_multipart_uploads_page(
            storage_key, limit=1
        )
        first_versions, first_list_request_id = self._exact_versions_page(storage_key, limit=1)
        if first_uploads or first_versions:
            raise ArtifactStorageUnavailableError("provider still reports upload sessions or object versions")
        head_request_id = self._head_absence_request_id(storage_key)
        second_versions, second_list_request_id = self._exact_versions_page(storage_key, limit=1)
        second_uploads, second_multipart_request_id = self._exact_multipart_uploads_page(
            storage_key, limit=1
        )
        if second_uploads or second_versions:
            raise ArtifactStorageUnavailableError("provider state changed during absence verification")
        first_digest = self._version_set_digest(first_versions)
        second_digest = self._version_set_digest(second_versions)
        first_multipart_digest = self._multipart_set_digest(first_uploads)
        second_multipart_digest = self._multipart_set_digest(second_uploads)
        if (
            first_versions != second_versions
            or first_digest != second_digest
            or first_uploads != second_uploads
            or first_multipart_digest != second_multipart_digest
        ):
            raise ArtifactStorageUnavailableError("provider absence snapshots were not stable")
        return (
            first_list_request_id,
            head_request_id,
            second_list_request_id,
            first_digest,
            second_digest,
            first_multipart_request_id,
            second_multipart_request_id,
            first_multipart_digest,
            second_multipart_digest,
        )

    async def verify_absent(self, target: ArtifactRemovalTarget) -> ArtifactRemovalEvidence:
        """Return independent provider-bound absence evidence for one exact key."""

        try:
            (
                first_list_request_id,
                head_request_id,
                second_list_request_id,
                first_digest,
                second_digest,
                first_multipart_request_id,
                second_multipart_request_id,
                first_multipart_digest,
                second_multipart_digest,
            ) = await asyncio.to_thread(self._verify_absent, target.storage_key)
        except ClientError as error:
            raise ArtifactStorageUnavailableError("stored-object absence could not be verified") from error
        except BotoCoreError as error:
            raise ArtifactStorageUnavailableError("stored-object absence could not be verified") from error
        assert target.verification_claimed_at is not None
        observed_at = max(datetime.now(UTC), target.verification_claimed_at)
        claim_digest = artifact_absence_claim_digest(
            target,
            observed_at=observed_at,
            first_list_request_id=first_list_request_id,
            head_request_id=head_request_id,
            second_list_request_id=second_list_request_id,
            first_version_set_digest=first_digest,
            second_version_set_digest=second_digest,
            first_multipart_list_request_id=first_multipart_request_id,
            second_multipart_list_request_id=second_multipart_request_id,
            first_multipart_session_set_digest=first_multipart_digest,
            second_multipart_session_set_digest=second_multipart_digest,
        )
        return ArtifactRemovalEvidence(
            storage_key=target.storage_key,
            kind=ArtifactRemovalEvidenceKind.ABSENCE_CONFIRMED,
            provider_request_id=second_list_request_id,
            removed_version_count=0,
            observed_at=observed_at,
            latest_upload_capability_expires_at=target.latest_upload_capability_expires_at,
            removal_generation=target.removal_generation,
            verification_generation=target.verification_generation,
            first_list_request_id=first_list_request_id,
            head_request_id=head_request_id,
            second_list_request_id=second_list_request_id,
            first_version_set_digest=first_digest,
            second_version_set_digest=second_digest,
            first_multipart_list_request_id=first_multipart_request_id,
            second_multipart_list_request_id=second_multipart_request_id,
            first_multipart_session_set_digest=first_multipart_digest,
            second_multipart_session_set_digest=second_multipart_digest,
            claim_digest=claim_digest,
        )

    def _legacy_versions_page(
        self, target: LegacyArtifactVersionTarget
    ) -> tuple[
        list[tuple[str | None, bool, bool, str | None, int | None, str | None]],
        str,
        str | None,
        str | None,
    ]:
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Prefix": target.storage_key,
            "MaxKeys": MAX_REMOVAL_VERSIONS,
        }
        if target.list_key_marker is not None:
            params["KeyMarker"] = target.list_key_marker
            params["VersionIdMarker"] = target.list_version_id_marker
        page = self._client.list_object_versions(**params)
        versions: list[tuple[str | None, bool, bool, str | None, int | None, str | None]] = []
        for delete_marker, entries in (
            (False, page.get("Versions", ())),
            (True, page.get("DeleteMarkers", ())),
        ):
            for entry in entries:
                if entry.get("Key") != target.storage_key:
                    continue
                last_modified = entry.get("LastModified")
                versions.append(
                    (
                        str(entry.get("VersionId") or "") or None,
                        delete_marker,
                        bool(entry.get("IsLatest", False)),
                        str(entry.get("ETag") or "") or None,
                        int(entry["Size"]) if entry.get("Size") is not None else None,
                        last_modified.isoformat() if isinstance(last_modified, datetime) else None,
                    )
                )
        next_key_marker: str | None = None
        next_version_id_marker: str | None = None
        if page.get("IsTruncated") and str(page.get("NextKeyMarker") or "") == target.storage_key:
            next_key_marker = target.storage_key
            next_version_id_marker = str(page.get("NextVersionIdMarker") or "") or None
            if next_version_id_marker is None:
                raise ArtifactStorageUnavailableError("legacy version pagination omitted its version cursor")
        return versions, self._request_id(page), next_key_marker, next_version_id_marker

    async def scan_legacy_version(
        self, target: LegacyArtifactVersionTarget
    ) -> LegacyArtifactVersionScan:
        """Inspect one resumable exact-key page for a retained immutable version."""

        try:
            before, request_id, next_key_marker, next_version_id_marker = await asyncio.to_thread(
                self._legacy_versions_page,
                target,
            )
        except (BotoCoreError, ClientError) as error:
            raise ArtifactStorageUnavailableError("legacy artifact versions could not be listed") from error
        candidates = sorted(
            (item for item in before if not item[1] and item[0] not in {None, "null"}),
            key=lambda item: (item[5] or "", item[0] or ""),
            reverse=True,
        )
        for candidate in candidates:
            provider_version_id = candidate[0]
            assert provider_version_id is not None
            if candidate[4] is not None and candidate[4] != target.expected_size_bytes:
                continue
            try:
                verified = await self.inspect(
                    target.storage_key,
                    provider_version_id=provider_version_id,
                    max_bytes=target.expected_size_bytes,
                )
            except (ArtifactNotFoundError, ArtifactVerificationError):
                # Legacy keys may contain replay-created or concurrently
                # lifecycle-expired versions.  A nonmatching page member is
                # negative evidence for that member, not authority to abandon
                # the bounded cursor before older retained versions are read.
                continue
            if (
                verified.digest == target.expected_digest
                and verified.size_bytes == target.expected_size_bytes
                and verified.media_type == target.expected_media_type
                and verified.compression == target.expected_compression
            ):
                return LegacyArtifactVersionScan(
                    provider_request_id=request_id,
                    observed_at=datetime.now(UTC),
                    verified=verified,
                )
        return LegacyArtifactVersionScan(
            provider_request_id=request_id,
            observed_at=datetime.now(UTC),
            next_key_marker=next_key_marker,
            next_version_id_marker=next_version_id_marker,
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)
