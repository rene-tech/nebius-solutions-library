"""Per-request artifact-store authority from an external credential broker.

The control plane mounts no tenant S3 credentials.  For each operation it
presents its projected workload identity to an independently operated broker,
which authorizes the separately supplied tenant, canonical key, action and
immutable object version before returning one short-lived session credential.
The credential is held only by the coroutine performing that operation and is
never cached, logged or represented.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from .scientific_artifacts import (
    ArtifactCompression,
    ArtifactPolicyError,
    EphemeralHandle,
    VerifiedStoredObject,
    assert_tenant_storage_key,
)
from .scientific_object_store import (
    ArtifactStorageUnavailableError,
    ObjectStoreConfig,
    S3ArtifactObjectStore,
)

BrokerAction = Literal[
    "presign-upload",
    "presign-download",
    "put",
    "inspect",
    "read",
    "delete",
]

_RESPONSE_FIELDS = frozenset(
    {
        "tenant_id",
        "storage_key",
        "action",
        "object_version_id",
        "access_key_id",
        "secret_access_key",
        "session_token",
        "expires_at",
        "endpoint_url",
        "bucket",
        "region",
        "addressing_style",
    }
)
_MAX_WORKLOAD_TOKEN_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class ArtifactCredentialBrokerConfig:
    url: str
    audience: str
    token_file: Path
    ca_file: Path
    timeout_seconds: float = 5.0
    operation_credential_ttl_seconds: int = 120
    max_credential_ttl_seconds: int = 900
    max_stream_bytes: int = 1 << 40

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
        except ValueError as error:
            raise ValueError("artifact credential broker URL is invalid") from error
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path in {"", "/"}
        ):
            raise ValueError("artifact credential broker must be an exact HTTPS endpoint")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("artifact credential broker port is invalid")
        if not self.audience or len(self.audience) > 253:
            raise ValueError("artifact credential broker audience is invalid")
        if not 0 < self.timeout_seconds <= 30:
            raise ValueError("artifact credential broker timeout is outside the supported range")
        if not 30 <= self.operation_credential_ttl_seconds <= self.max_credential_ttl_seconds <= 900:
            raise ValueError("artifact credential broker lifetime is outside the supported range")


@dataclass(frozen=True, slots=True)
class _BrokeredCredential:
    tenant_id: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    session_token: str = field(repr=False)
    expires_at: datetime
    endpoint_url: str
    bucket: str
    region: str
    addressing_style: Literal["path", "virtual", "auto"]


class ArtifactCredentialBroker:
    """Exchange projected workload identity for one tenant-scoped session."""

    def __init__(
        self,
        config: ArtifactCredentialBrokerConfig,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._clock = clock
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            verify=str(config.ca_file),
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )

    def _workload_token(self) -> str:
        try:
            raw = self._config.token_file.read_bytes()
        except OSError as error:
            raise ArtifactStorageUnavailableError("artifact broker workload identity is unavailable") from error
        if not raw or len(raw) > _MAX_WORKLOAD_TOKEN_BYTES:
            raise ArtifactStorageUnavailableError("artifact broker workload identity is invalid")
        try:
            token = raw.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise ArtifactStorageUnavailableError("artifact broker workload identity is invalid") from error
        if not token or any(character.isspace() for character in token):
            raise ArtifactStorageUnavailableError("artifact broker workload identity is invalid")
        return token

    @staticmethod
    def _required_string(document: dict[str, Any], name: str) -> str:
        value = document.get(name)
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ArtifactStorageUnavailableError("artifact broker returned an invalid credential contract")
        return value

    async def exchange(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        action: BrokerAction,
        object_version_id: str | None,
        minimum_ttl_seconds: int,
    ) -> _BrokeredCredential:
        """Request one independently authorized tenant/action credential."""

        assert_tenant_storage_key(tenant_id, storage_key)
        if not 1 <= minimum_ttl_seconds <= self._config.max_credential_ttl_seconds:
            raise ArtifactPolicyError("artifact broker credential lifetime is outside policy")
        request = {
            "audience": self._config.audience,
            "tenant_id": tenant_id,
            "storage_key": storage_key,
            "action": action,
            "object_version_id": object_version_id,
            "minimum_ttl_seconds": minimum_ttl_seconds,
        }
        try:
            response = await self._client.post(
                self._config.url,
                headers={
                    "authorization": f"Bearer {self._workload_token()}",
                    "content-type": "application/json",
                },
                json=request,
            )
        except httpx.HTTPError as error:
            raise ArtifactStorageUnavailableError("artifact credential broker is unavailable") from error
        if response.status_code != 200:
            raise ArtifactStorageUnavailableError("artifact credential broker refused the storage operation")
        try:
            document = response.json()
        except ValueError as error:
            raise ArtifactStorageUnavailableError("artifact broker returned an invalid credential contract") from error
        if not isinstance(document, dict) or set(document) != _RESPONSE_FIELDS:
            raise ArtifactStorageUnavailableError("artifact broker returned an invalid credential contract")
        authorized_tenant = self._required_string(document, "tenant_id")
        if authorized_tenant != tenant_id:
            raise ArtifactStorageUnavailableError("artifact broker authorized a different tenant")
        authorized_key = self._required_string(document, "storage_key")
        authorized_action = self._required_string(document, "action")
        authorized_version = document.get("object_version_id")
        if (
            authorized_key != storage_key
            or authorized_action != action
            or authorized_version != object_version_id
        ):
            raise ArtifactStorageUnavailableError("artifact broker authorized a different storage scope")
        try:
            expires_at = datetime.fromisoformat(self._required_string(document, "expires_at").replace("Z", "+00:00"))
        except ValueError as error:
            raise ArtifactStorageUnavailableError("artifact broker returned an invalid credential contract") from error
        now = self._clock()
        if (
            expires_at.tzinfo is None
            or expires_at < now + timedelta(seconds=minimum_ttl_seconds)
            or expires_at > now + timedelta(seconds=self._config.max_credential_ttl_seconds)
        ):
            raise ArtifactStorageUnavailableError("artifact broker credential lifetime violates policy")
        endpoint_url = self._required_string(document, "endpoint_url")
        try:
            endpoint = urlsplit(endpoint_url)
            endpoint_port = endpoint.port
        except ValueError as error:
            raise ArtifactStorageUnavailableError("artifact broker returned an invalid storage endpoint") from error
        if (
            endpoint.scheme != "https"
            or endpoint.hostname is None
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
            or (endpoint_port is not None and not 1 <= endpoint_port <= 65535)
        ):
            raise ArtifactStorageUnavailableError("artifact broker returned a non-TLS storage endpoint")
        addressing_style = self._required_string(document, "addressing_style")
        if addressing_style not in {"path", "virtual", "auto"}:
            raise ArtifactStorageUnavailableError("artifact broker returned an invalid credential contract")
        return _BrokeredCredential(
            tenant_id=tenant_id,
            access_key=self._required_string(document, "access_key_id"),
            secret_key=self._required_string(document, "secret_access_key"),
            session_token=self._required_string(document, "session_token"),
            expires_at=expires_at,
            endpoint_url=endpoint_url,
            bucket=self._required_string(document, "bucket"),
            region=self._required_string(document, "region"),
            addressing_style=addressing_style,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class BrokeredS3ArtifactObjectStore:
    """Use one uncached, tenant/action-scoped broker session per operation."""

    def __init__(
        self,
        broker: ArtifactCredentialBroker,
        *,
        operation_credential_ttl_seconds: int = 120,
        max_stream_bytes: int = 1 << 40,
    ) -> None:
        self._broker = broker
        self._operation_credential_ttl_seconds = operation_credential_ttl_seconds
        self._max_stream_bytes = max_stream_bytes

    async def _store(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        action: BrokerAction,
        object_version_id: str | None = None,
        minimum_ttl_seconds: int | None = None,
    ) -> S3ArtifactObjectStore:
        credential = await self._broker.exchange(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action=action,
            object_version_id=object_version_id,
            minimum_ttl_seconds=minimum_ttl_seconds or self._operation_credential_ttl_seconds,
        )
        return S3ArtifactObjectStore(
            ObjectStoreConfig(
                endpoint_url=credential.endpoint_url,
                bucket=credential.bucket,
                region=credential.region,
                access_key=credential.access_key,
                secret_key=credential.secret_key,
                session_token=credential.session_token,
                addressing_style=credential.addressing_style,
                verify_tls=True,
                max_stream_bytes=self._max_stream_bytes,
            )
        )

    async def presign_upload(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        media_type: str,
        compression: ArtifactCompression | None,
        ttl: timedelta,
    ) -> EphemeralHandle:
        store = await self._store(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action="presign-upload",
            minimum_ttl_seconds=int(ttl.total_seconds()),
        )
        try:
            return await store.presign_upload(
                tenant_id=tenant_id,
                storage_key=storage_key,
                media_type=media_type,
                compression=compression,
                ttl=ttl,
            )
        finally:
            await store.close()

    async def presign_download(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        object_version_id: str,
        ttl: timedelta,
    ) -> EphemeralHandle:
        store = await self._store(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action="presign-download",
            object_version_id=object_version_id,
            minimum_ttl_seconds=int(ttl.total_seconds()),
        )
        try:
            return await store.presign_download(
                tenant_id=tenant_id,
                storage_key=storage_key,
                object_version_id=object_version_id,
                ttl=ttl,
            )
        finally:
            await store.close()

    async def put_object(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        payload: bytes,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> VerifiedStoredObject:
        store = await self._store(tenant_id=tenant_id, storage_key=storage_key, action="put")
        try:
            return await store.put_object(
                tenant_id=tenant_id,
                storage_key=storage_key,
                payload=payload,
                media_type=media_type,
                compression=compression,
            )
        finally:
            await store.close()

    def stream_object(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        object_version_id: str,
        max_bytes: int | None = None,
    ) -> AsyncIterator[bytes]:
        async def stream() -> AsyncIterator[bytes]:
            store = await self._store(
                tenant_id=tenant_id,
                storage_key=storage_key,
                action="read",
                object_version_id=object_version_id,
            )
            try:
                async for chunk in store.stream_object(
                    tenant_id=tenant_id,
                    storage_key=storage_key,
                    object_version_id=object_version_id,
                    max_bytes=max_bytes,
                ):
                    yield chunk
            finally:
                await store.close()

        return stream()

    async def inspect(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        object_version_id: str | None = None,
        max_bytes: int | None = None,
    ) -> VerifiedStoredObject:
        store = await self._store(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action="inspect",
            object_version_id=object_version_id,
        )
        try:
            return await store.inspect(
                tenant_id=tenant_id,
                storage_key=storage_key,
                object_version_id=object_version_id,
                max_bytes=max_bytes,
            )
        finally:
            await store.close()

    async def delete(self, *, tenant_id: str, storage_key: str, object_version_id: str) -> None:
        store = await self._store(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action="delete",
            object_version_id=object_version_id,
        )
        try:
            await store.delete(
                tenant_id=tenant_id,
                storage_key=storage_key,
                object_version_id=object_version_id,
            )
        finally:
            await store.close()

    async def close(self) -> None:
        await self._broker.close()


__all__ = [
    "ArtifactCredentialBroker",
    "ArtifactCredentialBrokerConfig",
    "BrokeredS3ArtifactObjectStore",
]
