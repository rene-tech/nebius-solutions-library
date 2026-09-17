"""Tenant-isolated S3 artifact storage.

Each configured tenant receives a distinct S3 identity.  The dispatcher derives
the tenant only from the service-owned canonical storage key and refuses unknown
or malformed prefixes before an SDK client is selected.  Credential documents
are mounted files rather than environment values and are never included in
errors or representations.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType

from .scientific_artifacts import (
    ArtifactCompression,
    ArtifactNotFoundError,
    EphemeralHandle,
    VerifiedStoredObject,
)
from .scientific_object_store import ObjectStoreConfig, S3ArtifactObjectStore

_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_STORAGE_PREFIX = "scientific/v1/tenants/"
_MAX_TENANTS = 4096
_MAX_CREDENTIAL_DOCUMENT_BYTES = 64 * 1024
_CREDENTIAL_FIELDS = frozenset(
    {
        "tenant_id",
        "access_key_id",
        "secret_access_key",
        "session_token",
        "endpoint_url",
        "bucket",
        "region",
        "addressing_style",
        "verify_tls",
    }
)


def tenant_from_storage_key(storage_key: str) -> str:
    """Extract the canonical tenant segment without accepting look-alike keys."""

    if not storage_key.startswith(_STORAGE_PREFIX):
        raise ArtifactNotFoundError("artifact storage scope is unavailable")
    tenant_id, separator, remainder = storage_key.removeprefix(_STORAGE_PREFIX).partition("/")
    if (
        not separator
        or _TENANT_ID.fullmatch(tenant_id) is None
        or not remainder.startswith("operations/")
    ):
        raise ArtifactNotFoundError("artifact storage scope is unavailable")
    return tenant_id


class TenantScopedS3ArtifactObjectStore:
    """Dispatch each operation to exactly one tenant-specific S3 identity."""

    def __init__(self, stores: Mapping[str, S3ArtifactObjectStore]) -> None:
        if not stores:
            raise ValueError("at least one tenant artifact-store identity is required")
        if any(_TENANT_ID.fullmatch(tenant_id) is None for tenant_id in stores):
            raise ValueError("tenant artifact-store identity is not canonical")
        self._stores = MappingProxyType(dict(stores))

    def _store(self, storage_key: str) -> S3ArtifactObjectStore:
        tenant_id = tenant_from_storage_key(storage_key)
        try:
            return self._stores[tenant_id]
        except KeyError:
            raise ArtifactNotFoundError("artifact storage scope is unavailable") from None

    async def presign_upload(
        self,
        *,
        storage_key: str,
        media_type: str,
        compression: ArtifactCompression | None,
        ttl: timedelta,
    ) -> EphemeralHandle:
        return await self._store(storage_key).presign_upload(
            storage_key=storage_key,
            media_type=media_type,
            compression=compression,
            ttl=ttl,
        )

    async def presign_download(self, *, storage_key: str, ttl: timedelta) -> EphemeralHandle:
        return await self._store(storage_key).presign_download(storage_key=storage_key, ttl=ttl)

    async def put_object(
        self,
        *,
        storage_key: str,
        payload: bytes,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> VerifiedStoredObject:
        return await self._store(storage_key).put_object(
            storage_key=storage_key,
            payload=payload,
            media_type=media_type,
            compression=compression,
        )

    def stream_object(self, storage_key: str, *, max_bytes: int | None = None) -> AsyncIterator[bytes]:
        return self._store(storage_key).stream_object(storage_key, max_bytes=max_bytes)

    async def inspect(self, storage_key: str, *, max_bytes: int | None = None) -> VerifiedStoredObject:
        return await self._store(storage_key).inspect(storage_key, max_bytes=max_bytes)

    async def delete(self, storage_key: str) -> None:
        await self._store(storage_key).delete(storage_key)

    async def close(self) -> None:
        for store in self._stores.values():
            await store.close()


def load_tenant_object_store(
    credentials_dir: Path,
    *,
    default_endpoint_url: str,
    default_bucket: str,
    default_region: str,
    default_addressing_style: str,
    default_verify_tls: bool,
    max_stream_bytes: int,
) -> TenantScopedS3ArtifactObjectStore:
    """Build isolated clients from bounded, strict mounted-secret documents."""

    try:
        documents = sorted(credentials_dir.glob("*.json"))
    except OSError as error:
        raise ValueError("tenant artifact-store credential directory is unavailable") from error
    if not documents or len(documents) > _MAX_TENANTS:
        raise ValueError("tenant artifact-store credential set is empty or too large")

    stores: dict[str, S3ArtifactObjectStore] = {}
    access_key_owners: dict[str, str] = {}
    for path in documents:
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ValueError("tenant artifact-store credential document is unavailable") from error
        if not raw or len(raw) > _MAX_CREDENTIAL_DOCUMENT_BYTES:
            raise ValueError("tenant artifact-store credential document has an invalid size")
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("tenant artifact-store credential document is not valid JSON") from error
        if not isinstance(document, dict) or set(document) - _CREDENTIAL_FIELDS:
            raise ValueError("tenant artifact-store credential document has unknown fields")

        tenant_id = document.get("tenant_id")
        access_key = document.get("access_key_id")
        secret_key = document.get("secret_access_key")
        session_token = document.get("session_token")
        if not isinstance(tenant_id, str) or _TENANT_ID.fullmatch(tenant_id) is None:
            raise ValueError("tenant artifact-store credential document has an invalid tenant identity")
        if path.stem != tenant_id:
            raise ValueError("tenant artifact-store credential filename must match its tenant identity")
        if tenant_id in stores:
            raise ValueError("tenant artifact-store credential set contains a duplicate tenant")
        if not isinstance(access_key, str) or not access_key:
            raise ValueError("tenant artifact-store credential document requires access_key_id")
        if not isinstance(secret_key, str) or not secret_key:
            raise ValueError("tenant artifact-store credential document requires secret_access_key")
        if session_token is not None and (not isinstance(session_token, str) or not session_token):
            raise ValueError("tenant artifact-store session_token must be a non-empty string")
        if access_key in access_key_owners:
            raise ValueError("an artifact-store access key must not be shared by tenants")

        endpoint_url = document.get("endpoint_url", default_endpoint_url)
        bucket = document.get("bucket", default_bucket)
        region = document.get("region", default_region)
        addressing_style = document.get("addressing_style", default_addressing_style)
        verify_tls = document.get("verify_tls", default_verify_tls)
        if not all(isinstance(item, str) for item in (endpoint_url, bucket, region, addressing_style)):
            raise ValueError("tenant artifact-store connection fields must be strings")
        if not isinstance(verify_tls, bool):
            raise ValueError("tenant artifact-store verify_tls must be a boolean")
        if default_verify_tls and not verify_tls:
            raise ValueError("tenant artifact-store credentials cannot weaken TLS verification")

        stores[tenant_id] = S3ArtifactObjectStore(
            ObjectStoreConfig(
                endpoint_url=endpoint_url,
                bucket=bucket,
                region=region,
                access_key=access_key,
                secret_key=secret_key,
                session_token=session_token,
                addressing_style=addressing_style,  # type: ignore[arg-type]
                verify_tls=verify_tls,
                max_stream_bytes=max_stream_bytes,
            )
        )
        access_key_owners[access_key] = tenant_id
    return TenantScopedS3ArtifactObjectStore(stores)


__all__ = [
    "TenantScopedS3ArtifactObjectStore",
    "load_tenant_object_store",
    "tenant_from_storage_key",
]
