"""Per-request artifact-store authority from an external credential broker.

The control plane mounts no tenant S3 credentials.  For each operation it
presents its projected workload identity to an independently operated broker,
which authorizes the separately supplied tenant, canonical key, action and
immutable object version. Each tenant has a distinct broker workload holding
only that tenant's provider key. The broker performs the exact S3 operation;
provider credentials never cross into the shared control-plane process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
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
from .scientific_object_store import ArtifactStorageUnavailableError

BrokerAction = Literal[
    "presign-upload",
    "presign-download",
    "put",
    "finalize-inspect",
    "finalize-discover",
    "inspect",
    "backfill-inspect",
    "list-upload-version",
    "read",
    "delete",
]

_MAX_WORKLOAD_TOKEN_BYTES = 16 * 1024
_MAX_AUTHORITY_TOKEN_BYTES = 16 * 1024
_artifact_authority: ContextVar[str | None] = ContextVar("artifact_authority", default=None)


@contextmanager
def artifact_authority(token: str | None):
    """Bind the independently verifiable caller credential to one request task."""

    if token is not None and (not token or len(token.encode("utf-8")) > _MAX_AUTHORITY_TOKEN_BYTES):
        raise ArtifactPolicyError("artifact authority is invalid")
    reset = _artifact_authority.set(token)
    try:
        yield
    finally:
        _artifact_authority.reset(reset)


def current_artifact_authority() -> str | None:
    """Return the request-scoped authority without persisting or logging it."""

    return _artifact_authority.get()


@dataclass(frozen=True, slots=True)
class ArtifactCredentialBrokerConfig:
    url_template: str
    audience: str
    token_file: Path
    ca_file: Path
    timeout_seconds: float = 5.0
    operation_timeout_seconds: int = 120
    max_stream_bytes: int = 1 << 40
    readiness_bindings_file: Path | None = None

    def __post_init__(self) -> None:
        expected = "https://fs2-artifact-{tenant_hash}.fs2-system.svc:8443/v1"
        try:
            candidate = self.url_template.replace(
                "{tenant_hash}",
                "0123456789abcdef0123456789abcdef",
            )
            parsed = urlsplit(candidate)
            port = parsed.port
        except ValueError as error:
            raise ValueError("artifact tenant broker URL template is invalid") from error
        if (
            self.url_template != expected
            or self.url_template.count("{tenant_hash}") != 1
            or parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path in {"", "/"}
        ):
            raise ValueError("artifact tenant broker must be the exact in-cluster HTTPS endpoint template")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("artifact credential broker port is invalid")
        if not self.audience or len(self.audience) > 253:
            raise ValueError("artifact credential broker audience is invalid")
        if not 0 < self.timeout_seconds <= 30:
            raise ValueError("artifact credential broker timeout is outside the supported range")
        if not 30 <= self.operation_timeout_seconds <= 900:
            raise ValueError("artifact broker operation timeout is outside the supported range")
        if not 1 <= self.max_stream_bytes <= 1 << 40:
            raise ValueError("artifact broker stream ceiling is outside the supported range")


class ArtifactCredentialBroker:
    """Call one tenant-isolated broker without receiving provider credentials."""

    def __init__(
        self,
        config: ArtifactCredentialBrokerConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._operation_timeout = httpx.Timeout(
            connect=config.timeout_seconds,
            read=float(config.operation_timeout_seconds),
            write=float(config.operation_timeout_seconds),
            pool=config.timeout_seconds,
        )
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

    def _endpoint(self, tenant_id: str, suffix: str) -> str:
        tenant_hash = hashlib.sha256(tenant_id.encode()).hexdigest()[:32]
        return self._config.url_template.replace("{tenant_hash}", tenant_hash).rstrip("/") + suffix

    def _headers(self, action: BrokerAction) -> dict[str, str]:
        authority = _artifact_authority.get()
        if authority is None and action not in {"delete", "backfill-inspect", "list-upload-version"}:
            raise ArtifactStorageUnavailableError("independent artifact authority is unavailable")
        headers = {"authorization": f"Bearer {self._workload_token()}"}
        if authority is not None:
            headers["x-fs2-artifact-authority"] = f"Bearer {authority}"
        return headers

    def _expected_readiness(self, tenant_id: str) -> tuple[int, str]:
        path = self._config.readiness_bindings_file
        if path is None:
            raise ArtifactStorageUnavailableError("artifact broker readiness inventory is unavailable")
        try:
            payload = path.read_bytes()
            if not payload or len(payload) > 1024 * 1024:
                raise ValueError
            document = json.loads(payload)
            tenant = document["tenants"][tenant_id]
            generation = int(tenant["active_generation"])
            binding = tenant["authorized_generations"][str(generation)]
            binding_sha256 = str(binding["binding_sha256"])
            if (
                document.get("schema")
                != "fs2-serve.nebius.ai/artifact-provider-observation-bindings/v1"
                or len(binding_sha256) != 64
                or any(character not in "0123456789abcdef" for character in binding_sha256)
            ):
                raise ValueError
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ArtifactStorageUnavailableError(
                "tenant has no authorized artifact broker inventory entry"
            ) from None
        return generation, binding_sha256

    def _assert_response_identity(
        self,
        tenant_id: str,
        *,
        credential_generation: object,
        provider_binding_sha256: object,
    ) -> None:
        expected_generation, expected_binding = self._expected_readiness(tenant_id)
        if (
            isinstance(credential_generation, bool)
            or credential_generation != expected_generation
            or not isinstance(provider_binding_sha256, str)
            or not hmac.compare_digest(provider_binding_sha256, expected_binding)
        ):
            raise ArtifactStorageUnavailableError(
                "artifact tenant broker response came from another provider binding"
            )

    def _assert_response_headers(self, tenant_id: str, headers: httpx.Headers) -> None:
        try:
            generation = int(headers["x-fs2-artifact-credential-generation"])
            response_tenant = headers["x-fs2-artifact-tenant"]
            binding = headers["x-fs2-artifact-provider-binding-sha256"]
        except (KeyError, TypeError, ValueError):
            raise ArtifactStorageUnavailableError(
                "artifact tenant broker response omitted its provider binding"
            ) from None
        if response_tenant != tenant_id:
            raise ArtifactStorageUnavailableError(
                "artifact tenant broker response came from another tenant"
            )
        self._assert_response_identity(
            tenant_id,
            credential_generation=generation,
            provider_binding_sha256=binding,
        )

    async def ensure_tenant_ready(self, tenant_id: str) -> None:
        """Reject token issuance unless the exact active tenant broker responds."""

        generation, binding_sha256 = self._expected_readiness(tenant_id)
        try:
            response = await self._client.get(
                self._endpoint(tenant_id, "/readiness"),
                headers={"authorization": f"Bearer {self._workload_token()}"},
            )
        except httpx.HTTPError as error:
            raise ArtifactStorageUnavailableError("tenant artifact broker is not ready") from error
        if response.status_code != 200:
            raise ArtifactStorageUnavailableError("tenant artifact broker is not ready")
        try:
            result = response.json()
            exact = (
                set(result) == {"tenant_id", "credential_generation", "provider_binding_sha256"}
                and result["tenant_id"] == tenant_id
                and result["credential_generation"] == generation
                and hmac.compare_digest(result["provider_binding_sha256"], binding_sha256)
            )
        except (KeyError, TypeError, ValueError):
            exact = False
        if not exact:
            raise ArtifactStorageUnavailableError("tenant artifact broker readiness differs")

    def _request(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        action: BrokerAction,
        object_version_id: str | None,
        minimum_ttl_seconds: int = 120,
    ) -> dict[str, Any]:
        assert_tenant_storage_key(tenant_id, storage_key)
        if not 1 <= minimum_ttl_seconds <= 900:
            raise ArtifactPolicyError("artifact broker operation lifetime is outside policy")
        return {
            "audience": self._config.audience,
            "tenant_id": tenant_id,
            "storage_key": storage_key,
            "action": action,
            "object_version_id": object_version_id,
            "minimum_ttl_seconds": minimum_ttl_seconds,
        }

    async def operation(self, request: dict[str, Any]) -> dict[str, Any]:
        tenant_id = str(request["tenant_id"])
        action = str(request["action"])
        try:
            response = await self._client.post(
                self._endpoint(tenant_id, "/operations"),
                headers=self._headers(action),  # type: ignore[arg-type]
                json=request,
                timeout=self._operation_timeout,
            )
        except httpx.HTTPError as error:
            raise ArtifactStorageUnavailableError("artifact tenant broker is unavailable") from error
        if response.status_code != 200:
            raise ArtifactStorageUnavailableError("artifact tenant broker refused the storage operation")
        try:
            document = response.json()
        except ValueError as error:
            raise ArtifactStorageUnavailableError("artifact tenant broker returned an invalid result") from error
        if not isinstance(document, dict):
            raise ArtifactStorageUnavailableError("artifact tenant broker returned an invalid result")
        self._assert_response_identity(
            tenant_id,
            credential_generation=document.get("credential_generation"),
            provider_binding_sha256=document.get("provider_binding_sha256"),
        )
        return document

    async def put(self, request: dict[str, Any], payload: bytes) -> dict[str, Any]:
        tenant_id = str(request["tenant_id"])
        try:
            response = await self._client.post(
                self._endpoint(tenant_id, "/content:put"),
                headers={
                    **self._headers("put"),
                    "content-type": "application/octet-stream",
                    "x-fs2-artifact-request": json.dumps(request, separators=(",", ":")),
                },
                content=payload,
                timeout=self._operation_timeout,
            )
        except httpx.HTTPError as error:
            raise ArtifactStorageUnavailableError("artifact tenant broker is unavailable") from error
        if response.status_code != 200:
            raise ArtifactStorageUnavailableError("artifact tenant broker refused the storage write")
        try:
            result = response.json()
        except ValueError as error:
            raise ArtifactStorageUnavailableError("artifact tenant broker returned an invalid write result") from error
        if not isinstance(result, dict):
            raise ArtifactStorageUnavailableError("artifact tenant broker returned an invalid write result")
        self._assert_response_headers(tenant_id, response.headers)
        return result

    def read(self, request: dict[str, Any]) -> AsyncIterator[bytes]:
        tenant_id = str(request["tenant_id"])
        # Capture the short-lived authority while the request or executor
        # context is still active. Starlette consumes a StreamingResponse only
        # after the endpoint and its ContextVar scope have returned.
        headers = self._headers("read")

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async with self._client.stream(
                    "POST",
                    self._endpoint(tenant_id, "/content:read"),
                    headers=headers,
                    json=request,
                    timeout=self._operation_timeout,
                ) as response:
                    if response.status_code != 200:
                        raise ArtifactStorageUnavailableError(
                            "artifact tenant broker refused the storage read"
                        )
                    self._assert_response_headers(tenant_id, response.headers)
                    async for chunk in response.aiter_bytes():
                        yield chunk
            except httpx.HTTPError as error:
                raise ArtifactStorageUnavailableError("artifact tenant broker is unavailable") from error

        return chunks()

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

    async def _result(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        action: BrokerAction,
        object_version_id: str | None = None,
        minimum_ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        request = self._broker._request(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action=action,
            object_version_id=object_version_id,
            minimum_ttl_seconds=minimum_ttl_seconds or self._operation_credential_ttl_seconds,
        )
        result = await self._broker.operation(request)
        if (
            result.get("tenant_id") != tenant_id
            or result.get("storage_key") != storage_key
            or result.get("action") != action
            or result.get("object_version_id") != object_version_id
        ):
            raise ArtifactStorageUnavailableError("artifact tenant broker returned a different storage scope")
        payload = result.get("result")
        if not isinstance(payload, dict):
            raise ArtifactStorageUnavailableError("artifact tenant broker returned an invalid operation result")
        return payload

    async def presign_upload(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        expected_size_bytes: int,
        media_type: str,
        compression: ArtifactCompression | None,
        ttl: timedelta,
    ) -> EphemeralHandle:
        result = await self._result(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action="presign-upload",
            minimum_ttl_seconds=int(ttl.total_seconds()),
        )
        try:
            handle = EphemeralHandle.model_validate(result)
        except ValueError as error:
            raise ArtifactStorageUnavailableError("artifact tenant broker returned an invalid upload handle") from error
        if (
            handle.method != "PUT"
            or handle.headers.get("content-length") != str(expected_size_bytes)
            or handle.headers.get("content-type") != media_type
            or handle.headers.get("content-encoding") != (compression.value if compression else None)
        ):
            raise ArtifactStorageUnavailableError("artifact tenant broker upload handle differs from intent")
        return handle

    async def presign_download(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        object_version_id: str,
        ttl: timedelta,
    ) -> EphemeralHandle:
        result = await self._result(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action="presign-download",
            object_version_id=object_version_id,
            minimum_ttl_seconds=int(ttl.total_seconds()),
        )
        try:
            return EphemeralHandle.model_validate(result)
        except ValueError as error:
            raise ArtifactStorageUnavailableError("artifact tenant broker returned an invalid download handle") from error

    async def put_object(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        payload: bytes,
        media_type: str,
        compression: ArtifactCompression | None,
    ) -> VerifiedStoredObject:
        try:
            result = await self._broker.put(
                self._broker._request(
                    tenant_id=tenant_id,
                    storage_key=storage_key,
                    action="put",
                    object_version_id=None,
                ),
                payload,
            )
            verified = VerifiedStoredObject.model_validate(result)
        except ValueError as error:
            raise ArtifactStorageUnavailableError("artifact tenant broker returned an invalid write receipt") from error
        if (
            verified.storage_key != storage_key
            or verified.size_bytes != len(payload)
            or verified.media_type != media_type
            or verified.compression != compression
            or verified.digest != f"sha256:{hashlib.sha256(payload).hexdigest()}"
        ):
            raise ArtifactStorageUnavailableError("artifact tenant broker write receipt differs from content")
        return verified

    def stream_object(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        object_version_id: str,
        expected_size_bytes: int,
        expected_media_type: str,
        expected_compression: ArtifactCompression | None,
    ) -> AsyncIterator[bytes]:
        if not 0 <= expected_size_bytes <= self._max_stream_bytes:
            raise ArtifactPolicyError("artifact exceeds the accepted object ceiling")
        request = self._broker._request(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action="read",
            object_version_id=object_version_id,
        )
        request["expected_size_bytes"] = expected_size_bytes
        request["expected_media_type"] = expected_media_type
        request["expected_compression"] = expected_compression.value if expected_compression else None
        return self._broker.read(request)

    async def inspect(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        object_version_id: str | None = None,
        max_bytes: int | None = None,
    ) -> VerifiedStoredObject:
        if max_bytes is not None and not 0 <= max_bytes <= self._max_stream_bytes:
            raise ArtifactPolicyError("artifact exceeds the accepted object ceiling")
        if object_version_id is None:
            raise ArtifactPolicyError("exact immutable provider version is required")
        result = await self._result(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action="inspect",
            object_version_id=object_version_id,
        )
        try:
            return VerifiedStoredObject.model_validate(result)
        except ValueError as error:
            raise ArtifactStorageUnavailableError("artifact tenant broker returned invalid object metadata") from error

    async def inspect_upload(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        object_version_id: str,
        max_bytes: int,
    ) -> VerifiedStoredObject:
        if not 0 <= max_bytes <= self._max_stream_bytes:
            raise ArtifactPolicyError("artifact exceeds the accepted object ceiling")
        result = await self._result(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action="finalize-inspect",
            object_version_id=object_version_id,
        )
        try:
            return VerifiedStoredObject.model_validate(result)
        except ValueError as error:
            raise ArtifactStorageUnavailableError("artifact tenant broker returned invalid upload metadata") from error

    async def discover_upload(
        self,
        *,
        tenant_id: str,
        storage_key: str,
        max_bytes: int,
    ) -> VerifiedStoredObject:
        if not 0 <= max_bytes <= self._max_stream_bytes:
            raise ArtifactPolicyError("artifact exceeds the accepted object ceiling")
        result = await self._result(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action="finalize-discover",
            object_version_id=None,
        )
        try:
            return VerifiedStoredObject.model_validate(result)
        except ValueError as error:
            raise ArtifactStorageUnavailableError(
                "artifact tenant broker returned invalid discovered upload metadata"
            ) from error

    async def delete(self, *, tenant_id: str, storage_key: str, object_version_id: str) -> None:
        await self._result(
            tenant_id=tenant_id,
            storage_key=storage_key,
            action="delete",
            object_version_id=object_version_id,
        )

    async def close(self) -> None:
        await self._broker.close()


__all__ = [
    "ArtifactCredentialBroker",
    "ArtifactCredentialBrokerConfig",
    "BrokeredS3ArtifactObjectStore",
    "artifact_authority",
]
