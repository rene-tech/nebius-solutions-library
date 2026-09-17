"""Independent tenant-authorizing, tenant-isolated artifact broker.

The gateway is an untrusted requester at this boundary.  A Kubernetes
TokenReview authenticates the calling workload, while a second end-user or
scientific-workload credential is independently verified against PostgreSQL.
The broker then resolves the canonical object row itself and performs one exact
S3 operation. Each broker process mounts exactly one tenant's provider key;
neither the shared gateway nor any broker process can read every tenant key.

No control-plane request field is accepted as tenant authority.  No provider
credential is mounted by, cached in, or shared across gateway replicas.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
import httpx
import uvicorn
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .artifact_authority import ArtifactAuthorityVerifier
from .artifact_credential_broker import BrokerAction
from .auth import MAX_PAT_LENGTH, PepperRing
from .models import Scope, StrictModel
from .scientific_artifacts import ArtifactCompression, EphemeralHandle, VerifiedStoredObject, assert_tenant_storage_key
from .scientific_object_store import ObjectStoreConfig, S3ArtifactObjectStore

BROKER_SCHEMA: Final = "fs2-serve.nebius.ai/artifact-credential-broker/v1"
_KEY_PATTERN: Final = re.compile(
    r"^scientific/v1/tenants/(?P<tenant>[A-Za-z0-9][A-Za-z0-9_.-]{0,119})/operations/"
    r"(?P<operation>[0-9a-f-]{36})/stages/(?P<stage>[a-z][a-z0-9-]{0,62})/shards/"
    r"(?P<shard>[a-z0-9]([-a-z0-9.]*[a-z0-9])?|-)/attempts/(?P<attempt>[0-9a-f-]{36})/"
    r"(?P<direction>input|output)/sha256/(?P<digest>[a-f0-9]{64})$"
)
_FINALIZED_GET_ACTIONS: Final = frozenset({"presign-download", "inspect", "read"})
_PUT_ACTIONS: Final = frozenset({"presign-upload", "put"})
_WRITE_ACTIONS: Final = _PUT_ACTIONS | frozenset({"finalize-inspect", "finalize-discover"})
_SCIENTIFIC_UPLOAD_PROTOCOL: Final = "scientific-artifact-upload-v1"


class ArtifactBrokerSettings(BaseSettings):
    """Secret-bearing settings used only by the independent broker process."""

    model_config = SettingsConfigDict(env_prefix="FS2_ARTIFACT_BROKER_", extra="forbid")

    database_url: str
    token_pepper_file: Path
    allowed_tenant_id: str
    provider_endpoint_url: str
    provider_bucket: str
    provider_region: str
    provider_addressing_style: Literal["path", "virtual"] = "path"
    provider_access_key_file: Path
    provider_secret_key_file: Path
    provider_generation: int = Field(ge=1, le=1_000_000)
    provider_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    broker_pod_uid: UUID
    authority_verification_key_file: Path | None = None
    authority_verification_key_directory: Path | None = None
    observation_issuer_url: str = "https://fs2-artifact-authority.fs2-system.svc:8443/v1"
    observation_issuer_audience: str = "fs2-artifact-authority-issuer"
    observation_issuer_token_file: Path = Path(
        "/var/run/secrets/fs2-artifact-broker/observation-issuer/token"
    )
    observation_issuer_ca_file: Path = Path(
        "/var/run/secrets/fs2-artifact-broker/observation-issuer/ca.crt"
    )
    proxy_max_put_bytes: int = Field(default=256 * 1024 * 1024, ge=1, le=1 << 30)
    kubernetes_reviewer_token_file: Path = Path("/var/run/secrets/fs2-artifact-broker/kubernetes/token")
    kubernetes_tokenreview_url: str = "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews"
    kubernetes_ca_file: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    audience: str = "fs2-artifact-credential-broker"
    allowed_gateway_subject: str = "system:serviceaccount:fs2-system:fs2-serve-control-plane-runtime"
    allowed_maintenance_subject: str = "system:serviceaccount:fs2-system:fs2-serve-control-plane-maintenance"
    listen_host: str = "0.0.0.0"  # noqa: S104 - pod listener, constrained by NetworkPolicy and auth
    listen_port: int = Field(default=8443, ge=1, le=65535)
    tls_certificate_file: Path
    tls_private_key_file: Path

    @model_validator(mode="after")
    def validate_urls(self) -> "ArtifactBrokerSettings":
        parsed = urlsplit(self.kubernetes_tokenreview_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "kubernetes.default.svc"
            or parsed.path != "/apis/authentication.k8s.io/v1/tokenreviews"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("artifact broker TokenReview endpoint must be the exact in-cluster API")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}[a-z0-9]", self.provider_region):
            raise ValueError("artifact broker provider region is invalid")
        if self.provider_endpoint_url != f"https://storage.{self.provider_region}.nebius.cloud":
            raise ValueError("artifact broker provider endpoint must be the official regional Nebius endpoint")
        if self.allowed_gateway_subject == self.allowed_maintenance_subject:
            raise ValueError("artifact broker gateway and maintenance subjects must differ")
        if (self.authority_verification_key_file is None) == (
            self.authority_verification_key_directory is None
        ):
            raise ValueError("artifact broker requires exactly one verification file or key-ring directory")
        observation = urlsplit(self.observation_issuer_url)
        if (
            observation.scheme != "https"
            or observation.hostname != "fs2-artifact-authority.fs2-system.svc"
            or observation.path.rstrip("/") != "/v1"
            or observation.username is not None
            or observation.password is not None
            or observation.query
            or observation.fragment
        ):
            raise ValueError("artifact broker observation issuer must be the exact in-cluster service")
        if self.observation_issuer_audience != "fs2-artifact-authority-issuer":
            raise ValueError("artifact broker observation issuer audience is invalid")
        return self


class BrokerRequest(StrictModel):
    audience: str = Field(min_length=1, max_length=253)
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    storage_key: str = Field(min_length=1, max_length=1024)
    action: BrokerAction
    object_version_id: str | None = Field(default=None, max_length=1024)
    minimum_ttl_seconds: int = Field(ge=1, le=900)
    expected_size_bytes: int | None = Field(default=None, ge=0, le=1 << 40)
    expected_media_type: str | None = Field(default=None, min_length=3, max_length=128)
    expected_compression: ArtifactCompression | None = None


class BrokerResponse(StrictModel):
    tenant_id: str
    credential_generation: int
    provider_binding_sha256: str
    storage_key: str
    action: BrokerAction
    object_version_id: str | None
    result: dict[str, Any]
    provider_observation: str | None = Field(default=None, min_length=1, max_length=16 * 1024)


class BrokerReadiness(StrictModel):
    tenant_id: str
    credential_generation: int
    provider_binding_sha256: str


@dataclass(frozen=True, slots=True)
class StorageRecord:
    subject_id: UUID
    tenant_id: str
    operation_id: UUID
    attempt_id: UUID
    storage_key: str
    expected_digest: str
    expected_size_bytes: int
    media_type: str
    compression: ArtifactCompression | None
    object_version_id: str | None
    artifact_id: UUID | None
    is_finalized: bool
    cleanup_claimed: bool
    begun_at: datetime
    retention_expires_at: datetime


class ProviderObservationIssuer:
    """Submit provider facts using the tenant broker's own projected identity."""

    def __init__(self, settings: ArtifactBrokerSettings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            verify=str(settings.observation_issuer_ca_file),
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
            trust_env=False,
        )

    def _identity(self) -> str:
        try:
            token = self._settings.observation_issuer_token_file.read_text(encoding="ascii").strip()
        except OSError as error:
            raise RuntimeError("provider observation issuer identity is unavailable") from error
        if not token or len(token.encode()) > 16 * 1024 or any(value.isspace() for value in token):
            raise RuntimeError("provider observation issuer identity is invalid")
        return token

    async def record(
        self,
        *,
        action: str,
        record: StorageRecord,
        object_version_id: str | None,
        digest: str | None,
        size_bytes: int | None,
        media_type: str | None,
        compression: ArtifactCompression | None,
        observed_at: datetime,
    ) -> str:
        try:
            response = await self._client.post(
                self._settings.observation_issuer_url.rstrip("/") + "/provider-observations",
                headers={"authorization": f"Bearer {self._identity()}"},
                json={
                    "audience": self._settings.observation_issuer_audience,
                    "action": action,
                    "subject_id": str(record.subject_id),
                    "tenant_id": record.tenant_id,
                    "credential_generation": self._settings.provider_generation,
                    "provider_access_key_id": self._settings.provider_access_key_file.read_text(
                        encoding="ascii"
                    ).strip(),
                    "provider_binding_sha256": self._settings.provider_binding_sha256,
                    "broker_pod_uid": str(self._settings.broker_pod_uid),
                    "storage_key": record.storage_key,
                    "object_version_id": object_version_id,
                    "digest": digest,
                    "size_bytes": size_bytes,
                    "media_type": media_type,
                    "compression": None if compression is None else compression.value,
                    "observed_at": observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                },
            )
        except httpx.HTTPError as error:
            raise RuntimeError("provider observation issuer is unavailable") from error
        if response.status_code != 200:
            raise RuntimeError("provider observation issuer refused the tenant fact")
        try:
            token = response.json()["token"]
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("provider observation issuer returned an invalid receipt") from None
        if not isinstance(token, str) or not token.startswith("fs2_provider_observation."):
            raise RuntimeError("provider observation issuer returned an invalid receipt")
        return token

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@dataclass(frozen=True, slots=True)
class AuthorizedSubject:
    tenant_id: str
    operation_id: UUID
    token_id: UUID | None
    scopes: frozenset[str]


class WorkloadReviewer(Protocol):
    async def review(self, token: str) -> str: ...


def _required_bearer(value: str | None, *, label: str) -> str:
    if value is None or not value.startswith("Bearer ") or value.count(" ") != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"{label} bearer is required")
    token = value.removeprefix("Bearer ")
    if not token or len(token.encode("utf-8")) > 16 * 1024 or any(character.isspace() for character in token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"{label} bearer is invalid")
    return token


class KubernetesTokenReviewer:
    """Resolve the caller subject through the cluster authentication API."""

    def __init__(self, settings: ArtifactBrokerSettings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            verify=str(settings.kubernetes_ca_file),
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
            trust_env=False,
        )

    def _reviewer_token(self) -> str:
        try:
            token = self._settings.kubernetes_reviewer_token_file.read_text(encoding="ascii").strip()
        except OSError as error:
            raise HTTPException(status_code=503, detail="artifact broker identity is unavailable") from error
        if not token or len(token) > 16 * 1024 or any(character.isspace() for character in token):
            raise HTTPException(status_code=503, detail="artifact broker identity is invalid")
        return token

    async def review(self, token: str) -> str:
        response = await self._client.post(
            self._settings.kubernetes_tokenreview_url,
            headers={"authorization": f"Bearer {self._reviewer_token()}"},
            json={
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": token, "audiences": [self._settings.audience]},
            },
        )
        if response.status_code != 201:
            raise HTTPException(status_code=503, detail="artifact broker caller authentication is unavailable")
        try:
            result = response.json()["status"]
            subject = result["user"]["username"]
            audiences = result["audiences"]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=503, detail="artifact broker caller authentication is invalid") from None
        if result.get("authenticated") is not True or self._settings.audience not in audiences:
            raise HTTPException(status_code=401, detail="artifact broker caller was rejected")
        if subject not in {
            self._settings.allowed_gateway_subject,
            self._settings.allowed_maintenance_subject,
        }:
            raise HTTPException(status_code=403, detail="artifact broker caller is not authorized")
        return str(subject)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class PostgresArtifactAuthorizer:
    """Derive tenant and exact object authority from credentials plus durable rows."""

    def __init__(
        self,
        pool: asyncpg.Pool[Any],
        peppers: PepperRing,
        internal_authority: ArtifactAuthorityVerifier,
    ) -> None:
        self._pool = pool
        self._peppers = peppers
        self._argon = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
        self._internal = internal_authority

    @staticmethod
    def _parse_key(storage_key: str) -> tuple[str, UUID, UUID, str]:
        match = _KEY_PATTERN.fullmatch(storage_key)
        if match is None:
            raise HTTPException(status_code=403, detail="artifact object scope is not canonical")
        try:
            return (
                match.group("tenant"),
                UUID(match.group("operation")),
                UUID(match.group("attempt")),
                f"sha256:{match.group('digest')}",
            )
        except ValueError:
            raise HTTPException(status_code=403, detail="artifact object scope is not canonical") from None

    async def _record(self, request: BrokerRequest) -> StorageRecord:
        artifact = await self._pool.fetchrow(
            "SELECT id,tenant_id,operation_id,attempt_id,digest,size_bytes,media_type,compression,storage_key,"
            "object_version_id,created_at,retention_expires_at "
            "FROM fs2_scientific_artifacts WHERE storage_key=$1",
            request.storage_key,
        )
        if artifact is not None:
            return StorageRecord(
                subject_id=artifact["id"],
                tenant_id=str(artifact["tenant_id"]),
                operation_id=artifact["operation_id"],
                attempt_id=artifact["attempt_id"],
                storage_key=str(artifact["storage_key"]),
                expected_digest=str(artifact["digest"]),
                expected_size_bytes=int(artifact["size_bytes"]),
                media_type=str(artifact["media_type"]),
                compression=(
                    None if artifact["compression"] is None else ArtifactCompression(str(artifact["compression"]))
                ),
                object_version_id=(
                    None if artifact["object_version_id"] is None else str(artifact["object_version_id"])
                ),
                artifact_id=artifact["id"],
                is_finalized=True,
                cleanup_claimed=False,
                begun_at=artifact["created_at"],
                retention_expires_at=artifact["retention_expires_at"],
            )
        upload = await self._pool.fetchrow(
            "SELECT upload.id,upload.tenant_id,upload.operation_id,upload.attempt_id,upload.expected_digest,"
            "upload.expected_size_bytes,upload.media_type,upload.compression,upload.storage_key,"
            "upload.begun_at,attempt.retention_expires_at,"
            "version.object_version_id,(claim.upload_id IS NOT NULL) AS cleanup_claimed "
            "FROM fs2_scientific_uploads upload "
            "JOIN fs2_scientific_stage_attempts attempt ON attempt.attempt_id=upload.attempt_id "
            "LEFT JOIN fs2_scientific_upload_object_versions version ON version.upload_id=upload.id "
            "LEFT JOIN fs2_scientific_abandoned_upload_claims claim ON claim.upload_id=upload.id "
            "WHERE upload.storage_key=$1 AND upload.artifact_id IS NULL",
            request.storage_key,
        )
        if upload is None:
            raise HTTPException(status_code=404, detail="artifact object authorization is absent")
        return StorageRecord(
            subject_id=upload["id"],
            tenant_id=str(upload["tenant_id"]),
            operation_id=upload["operation_id"],
            attempt_id=upload["attempt_id"],
            storage_key=str(upload["storage_key"]),
            expected_digest=str(upload["expected_digest"]),
            expected_size_bytes=int(upload["expected_size_bytes"]),
            media_type=str(upload["media_type"]),
            compression=(
                None if upload["compression"] is None else ArtifactCompression(str(upload["compression"]))
            ),
            object_version_id=(
                None if upload["object_version_id"] is None else str(upload["object_version_id"])
            ),
            artifact_id=None,
            is_finalized=False,
            cleanup_claimed=bool(upload["cleanup_claimed"]),
            begun_at=upload["begun_at"],
            retention_expires_at=upload["retention_expires_at"],
        )

    async def _pat_subject(self, token: str, record: StorageRecord, request: BrokerRequest) -> AuthorizedSubject:
        import asyncio

        if len(token) > MAX_PAT_LENGTH:
            raise HTTPException(status_code=401, detail="artifact tenant authority was rejected")
        pieces = token.split("_", 3)
        if len(pieces) != 4 or pieces[:2] != ["fs2", "pat"] or len(pieces[3]) < 32:
            raise HTTPException(status_code=401, detail="artifact tenant authority was rejected")
        try:
            token_id = UUID(hex=pieces[2])
        except ValueError:
            raise HTTPException(status_code=401, detail="artifact tenant authority was rejected") from None
        row = await self._pool.fetchrow(
            "SELECT id,prefix,pepper_key_id,digest,tenant_id,scopes,models,expires_at,revoked_at "
            "FROM fs2_tokens WHERE id=$1",
            token_id,
        )
        if row is None:
            raise HTTPException(status_code=401, detail="artifact tenant authority was rejected")
        try:
            pepper = self._peppers.keys[str(row["pepper_key_id"])]
            prehash = hmac.new(pepper, token.encode(), hashlib.sha256).hexdigest()
            # Keep the broker event loop available while Argon2 performs its
            # deliberately expensive verification. The database revocation
            # state was fetched first and is checked again below as part of
            # this single authorization decision.
            valid = await asyncio.to_thread(self._argon.verify, str(row["digest"]), prehash)
        except (KeyError, InvalidHashError, VerifyMismatchError):
            raise HTTPException(status_code=401, detail="artifact tenant authority was rejected") from None
        now = datetime.now(UTC)
        if (
            not valid
            or not hmac.compare_digest(str(row["prefix"]), f"fs2_pat_{pieces[2][:12]}")
            or row["revoked_at"] is not None
            or (row["expires_at"] is not None and row["expires_at"] <= now)
            or str(row["tenant_id"]) != record.tenant_id
        ):
            raise HTTPException(status_code=401, detail="artifact tenant authority was rejected")
        owner = await self._pool.fetchrow(
            "SELECT token_id,tenant_id,protocol,model_id FROM fs2_operations WHERE id=$1",
            record.operation_id,
        )
        if owner is None or str(owner["tenant_id"]) != record.tenant_id:
            raise HTTPException(status_code=403, detail="artifact operation authority is unavailable")
        scopes = frozenset(str(value) for value in row["scopes"])
        models = frozenset(str(value) for value in row["models"])
        if request.action in _WRITE_ACTIONS and str(owner["protocol"]) == _SCIENTIFIC_UPLOAD_PROTOCOL:
            # Public scientific input upload is deliberately part of the
            # existing inference.invoke contract. The gateway first authorizes
            # the exact operation/upload pair; the broker independently repeats
            # token ownership, tenant, protocol and model-grant checks before
            # allowing only this operation's canonical write-once object.
            required = Scope.INFERENCE_INVOKE
            if "*" not in models and str(owner["model_id"]) not in models:
                raise HTTPException(status_code=403, detail="artifact upload model is outside tenant authority")
        else:
            required = Scope.ARTIFACTS_WRITE if request.action in _WRITE_ACTIONS else Scope.OPERATIONS_RESULT
        if str(required) not in scopes:
            raise HTTPException(status_code=403, detail="artifact tenant authority lacks the required scope")
        if owner["token_id"] != token_id and str(Scope.TENANT_ADMIN) not in scopes:
            raise HTTPException(status_code=403, detail="artifact operation belongs to another principal")
        return AuthorizedSubject(record.tenant_id, record.operation_id, token_id, scopes)

    async def _workload_subject(
        self, token: str, record: StorageRecord, request: BrokerRequest
    ) -> AuthorizedSubject:
        try:
            capability = self._internal.verify_workload(token)
        except ValueError:
            raise HTTPException(status_code=401, detail="artifact workload authority was rejected") from None
        expected_access = "write" if request.action in _WRITE_ACTIONS else "read"
        if capability.tenant_id != record.tenant_id or capability.access != expected_access:
            raise HTTPException(status_code=403, detail="artifact workload scope differs from the object record")
        attempt = await self._pool.fetchrow(
            "SELECT status,attempt_number FROM fs2_scientific_stage_attempts "
            "WHERE attempt_id=$1 AND operation_id=$2 AND tenant_id=$3",
            capability.attempt_id,
            capability.operation_id,
            capability.tenant_id,
        )
        if (
            attempt is None
            or str(attempt["status"]) != "running"
            or int(attempt["attempt_number"]) != capability.attempt_number
        ):
            raise HTTPException(status_code=409, detail="artifact workload authority is stale")
        if capability.access == "read":
            # Inputs may have been finalized by an earlier operation.  Bind the
            # requester to its current durable stage attempt and to the exact
            # independently authorized artifact, not to the artifact producer's
            # operation or attempt.
            if record.artifact_id is None or capability.artifact_id != record.artifact_id:
                raise HTTPException(status_code=403, detail="artifact is outside workload read authority")
        elif (
            capability.artifact_id is not None
            or capability.operation_id != record.operation_id
            or capability.attempt_id != record.attempt_id
        ):
            raise HTTPException(status_code=403, detail="artifact is outside workload write authority")
        return AuthorizedSubject(record.tenant_id, capability.operation_id, None, frozenset())

    async def _executor_subject(
        self, token: str, record: StorageRecord, request: BrokerRequest
    ) -> AuthorizedSubject:
        try:
            capability = self._internal.verify_executor(token)
        except ValueError:
            raise HTTPException(status_code=401, detail="artifact executor authority was rejected") from None
        expected_access = "write" if request.action in _WRITE_ACTIONS else "read"
        operation = await self._pool.fetchrow(
            "SELECT tenant_id,worker_id,fencing_token,attempt,status,lease_expires_at "
            "FROM fs2_operations WHERE id=$1",
            capability.operation_id,
        )
        now = datetime.now(UTC)
        if (
            operation is None
            or str(operation["tenant_id"]) != capability.tenant_id
            or str(operation["worker_id"]) != capability.worker_id
            or int(operation["fencing_token"]) != capability.fencing_token
            or int(operation["attempt"]) != capability.attempt
            or str(operation["status"]) != "running"
            or operation["lease_expires_at"] is None
            or operation["lease_expires_at"] <= now
            or capability.tenant_id != record.tenant_id
            or capability.access != expected_access
        ):
            raise HTTPException(status_code=403, detail="artifact executor claim is not durably active")
        if capability.access == "read":
            if record.artifact_id is None or capability.artifact_id != record.artifact_id:
                raise HTTPException(status_code=403, detail="artifact is outside executor read authority")
        elif capability.artifact_id is not None or record.operation_id != capability.operation_id:
            raise HTTPException(status_code=403, detail="artifact is outside executor write authority")
        return AuthorizedSubject(record.tenant_id, capability.operation_id, None, frozenset())

    async def _controller_subject(
        self, token: str, record: StorageRecord, request: BrokerRequest
    ) -> AuthorizedSubject:
        try:
            capability = self._internal.verify_controller(token)
        except ValueError:
            raise HTTPException(status_code=401, detail="artifact controller authority was rejected") from None
        if (
            request.action != "read"
            or record.artifact_id is None
            or capability.tenant_id != record.tenant_id
            or capability.operation_id != record.operation_id
            or capability.artifact_id != record.artifact_id
        ):
            raise HTTPException(status_code=403, detail="artifact is outside controller read authority")
        batch = await self._pool.fetchrow(
            "SELECT controller_id,fencing_token,lease_expires_at "
            "FROM fs2_scientific_batches WHERE operation_id=$1 AND tenant_id=$2",
            capability.operation_id,
            capability.tenant_id,
        )
        if (
            batch is None
            or str(batch["controller_id"] or "") != capability.controller_id
            or int(batch["fencing_token"]) != capability.fencing_token
            or batch["lease_expires_at"] is None
            or batch["lease_expires_at"] <= datetime.now(UTC)
        ):
            raise HTTPException(status_code=409, detail="artifact controller authority is stale")
        return AuthorizedSubject(record.tenant_id, record.operation_id, None, frozenset())

    async def _operator_subject(
        self, token: str, record: StorageRecord, request: BrokerRequest
    ) -> AuthorizedSubject:
        try:
            capability = self._internal.verify_operator(token)
        except ValueError:
            raise HTTPException(status_code=401, detail="operator artifact authority was rejected") from None
        if request.action != "read" or record.artifact_id is None:
            raise HTTPException(status_code=403, detail="operator artifact authority is read-only")
        session = await self._pool.fetchrow(
            "SELECT session.principal_id,session.expires_at,session.revoked_at,"
            "principal.role,principal.tenant_id,principal.enabled "
            "FROM fs2_operator_sessions session "
            "JOIN fs2_operator_principals principal ON principal.id=session.principal_id "
            "WHERE session.id=$1",
            capability.session_id,
        )
        now = datetime.now(UTC)
        if (
            session is None
            or session["principal_id"] != capability.principal_id
            or str(session["role"]) != capability.role
            or capability.role not in {"viewer", "operator", "admin"}
            or session["revoked_at"] is not None
            or session["expires_at"] <= now
            or session["enabled"] is not True
            or (session["tenant_id"] is not None and str(session["tenant_id"]) != capability.tenant_id)
            or capability.tenant_id != record.tenant_id
            or capability.operation_id != record.operation_id
            or capability.artifact_id != record.artifact_id
        ):
            raise HTTPException(status_code=403, detail="operator artifact authority is not durably active")
        return AuthorizedSubject(record.tenant_id, record.operation_id, None, frozenset())

    async def authorize(
        self,
        *,
        request: BrokerRequest,
        caller_subject: str,
        authority_token: str | None,
        maintenance_subject: str,
    ) -> StorageRecord:
        key_tenant, key_operation, key_attempt, key_digest = self._parse_key(request.storage_key)
        record = await self._record(request)
        if (
            request.tenant_id != record.tenant_id
            or key_tenant != record.tenant_id
            or key_operation != record.operation_id
            or key_attempt != record.attempt_id
            or key_digest != record.expected_digest
            or request.storage_key != record.storage_key
        ):
            raise HTTPException(status_code=403, detail="artifact request differs from authoritative storage scope")
        if request.action in _FINALIZED_GET_ACTIONS:
            if not record.is_finalized or record.object_version_id is None:
                raise HTTPException(status_code=409, detail="artifact has no immutable provider version")
            if request.object_version_id != record.object_version_id:
                raise HTTPException(status_code=403, detail="artifact version differs from authoritative metadata")
        elif request.action == "delete":
            # No deployed tenant-broker credential has DeleteObject. Retention
            # deletion remains dormant until a separately scoped exact-version
            # principal and independently reviewed activation exist.
            raise HTTPException(
                status_code=403,
                detail="artifact provider retention deletion is not activated",
            )
        elif request.action in _PUT_ACTIONS:
            if record.is_finalized or record.object_version_id is not None or request.object_version_id is not None:
                raise HTTPException(status_code=409, detail="artifact upload is no longer writable")
        elif request.action == "finalize-inspect":
            if (
                record.is_finalized
                or request.object_version_id is None
                or (
                    record.object_version_id is not None
                    and request.object_version_id != record.object_version_id
                )
            ):
                raise HTTPException(status_code=409, detail="artifact upload version cannot be verified")
        elif request.action == "finalize-discover":
            if (
                record.is_finalized
                or record.object_version_id is not None
                or request.object_version_id is not None
                or record.cleanup_claimed
            ):
                raise HTTPException(status_code=409, detail="artifact upload cannot be discovered")
        elif request.action == "backfill-inspect":
            if not record.is_finalized or record.object_version_id is not None or request.object_version_id is None:
                raise HTTPException(status_code=409, detail="artifact does not require version backfill")
        elif request.action == "list-upload-version":
            if (
                record.is_finalized
                or not record.cleanup_claimed
                or record.object_version_id is not None
                or request.object_version_id is not None
            ):
                raise HTTPException(status_code=409, detail="artifact upload no longer requires orphan discovery")
        if caller_subject == maintenance_subject:
            if request.action == "list-upload-version":
                if record.begun_at > datetime.now(UTC) - timedelta(hours=1):
                    raise HTTPException(status_code=403, detail="upload is still inside the finalization grace period")
            elif request.action != "backfill-inspect":
                raise HTTPException(status_code=403, detail="maintenance authority is outside its exact action")
            return record
        if record.cleanup_claimed:
            raise HTTPException(status_code=409, detail="artifact upload is fenced for abandoned-upload cleanup")
        if request.action == "delete":
            # Provider deletion is a retention operation, never a capability
            # granted by a PAT or executor/controller/workload authority. The
            # maintenance branch above independently binds the caller and
            # checks the finalized record's retention deadline.
            raise HTTPException(
                status_code=403,
                detail="artifact deletion requires expired-retention maintenance authority",
            )
        if request.action in {"backfill-inspect", "list-upload-version"}:
            raise HTTPException(status_code=403, detail="artifact maintenance action requires maintenance authority")
        if authority_token is None:
            raise HTTPException(status_code=401, detail="independent artifact tenant authority is required")
        if authority_token.startswith("fs2_pat_"):
            await self._pat_subject(authority_token, record, request)
        elif authority_token.startswith("fs2_artifact_controller."):
            await self._controller_subject(authority_token, record, request)
        elif authority_token.startswith("fs2_artifact_executor."):
            await self._executor_subject(authority_token, record, request)
        elif authority_token.startswith("fs2_artifact_operator."):
            await self._operator_subject(authority_token, record, request)
        elif authority_token.startswith("fs2_artifact_workload."):
            await self._workload_subject(authority_token, record, request)
        else:
            raise HTTPException(status_code=401, detail="artifact tenant authority was rejected")
        return record


def create_artifact_broker_app(
    *,
    settings: ArtifactBrokerSettings,
    pool: asyncpg.Pool[Any],
    reviewer: WorkloadReviewer,
    authorizer: PostgresArtifactAuthorizer,
    provider: S3ArtifactObjectStore,
    observations: ProviderObservationIssuer,
) -> FastAPI:
    app = FastAPI(title="FS2 Tenant Artifact Broker", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def response_policy(request: Request, call_next: Any):
        response = await call_next(request)
        response.headers["cache-control"] = "no-store"
        response.headers["x-content-type-options"] = "nosniff"
        return response

    async def authorized(
        contract: BrokerRequest,
        authorization: str | None = Header(default=None),
        x_fs2_artifact_authority: str | None = Header(default=None),
    ) -> StorageRecord:
        if contract.audience != settings.audience:
            raise HTTPException(status_code=403, detail="artifact broker audience differs")
        if contract.tenant_id != settings.allowed_tenant_id:
            raise HTTPException(status_code=403, detail="request was routed to another tenant broker")
        caller = await reviewer.review(_required_bearer(authorization, label="workload identity"))
        authority = None
        if x_fs2_artifact_authority is not None:
            authority = _required_bearer(x_fs2_artifact_authority, label="artifact authority")
        record = await authorizer.authorize(
            request=contract,
            caller_subject=caller,
            authority_token=authority,
            maintenance_subject=settings.allowed_maintenance_subject,
        )
        # This is an operation gate, not a configuration echo. It proves the
        # mounted generation can reach the exact versioned bucket and tenant
        # prefix before a handle, byte stream or provider mutation is exposed.
        await provider.assert_tenant_ready(record.tenant_id)
        return record

    def bind_identity_headers(result: Response) -> None:
        result.headers["x-fs2-artifact-tenant"] = settings.allowed_tenant_id
        result.headers["x-fs2-artifact-credential-generation"] = str(settings.provider_generation)
        result.headers["x-fs2-artifact-provider-binding-sha256"] = settings.provider_binding_sha256

    def response(
        contract: BrokerRequest,
        result: EphemeralHandle | VerifiedStoredObject | dict[str, Any],
        *,
        provider_observation: str | None = None,
    ) -> BrokerResponse:
        payload = result if isinstance(result, dict) else result.model_dump(mode="json")
        return BrokerResponse(
            tenant_id=contract.tenant_id,
            credential_generation=settings.provider_generation,
            provider_binding_sha256=settings.provider_binding_sha256,
            storage_key=contract.storage_key,
            action=contract.action,
            object_version_id=contract.object_version_id,
            result=payload,
            provider_observation=provider_observation,
        )

    @app.post("/v1/operations", response_model=BrokerResponse)
    async def artifact_operation(
        contract: BrokerRequest,
        authorization: str | None = Header(default=None),
        x_fs2_artifact_authority: str | None = Header(default=None),
    ) -> BrokerResponse:
        record = await authorized(contract, authorization, x_fs2_artifact_authority)
        lifetime = timedelta(seconds=contract.minimum_ttl_seconds)
        if contract.action == "presign-upload":
            handle = await provider.presign_upload(
                tenant_id=record.tenant_id,
                storage_key=record.storage_key,
                expected_size_bytes=record.expected_size_bytes,
                media_type=record.media_type,
                compression=record.compression,
                ttl=lifetime,
            )
            return response(contract, handle)
        if contract.action == "presign-download":
            assert record.object_version_id is not None
            handle = await provider.presign_download(
                tenant_id=record.tenant_id,
                storage_key=record.storage_key,
                object_version_id=record.object_version_id,
                ttl=lifetime,
            )
            return response(contract, handle)
        if contract.action in {"inspect", "finalize-inspect", "backfill-inspect"}:
            assert contract.object_version_id is not None
            measured = await provider.inspect(
                tenant_id=record.tenant_id,
                storage_key=record.storage_key,
                object_version_id=contract.object_version_id,
                max_bytes=record.expected_size_bytes,
            )
            observation = None
            if contract.action == "backfill-inspect":
                observation = await observations.record(
                    action="backfill-observed",
                    record=record,
                    object_version_id=measured.object_version_id,
                    digest=measured.digest,
                    size_bytes=measured.size_bytes,
                    media_type=measured.media_type,
                    compression=measured.compression,
                    observed_at=datetime.now(UTC),
                )
            return response(contract, measured, provider_observation=observation)
        if contract.action == "finalize-discover":
            discovered = await provider.discover_upload_version(
                tenant_id=record.tenant_id,
                storage_key=record.storage_key,
            )
            if discovered is None or discovered.is_absence_fence:
                raise HTTPException(status_code=409, detail="artifact upload version is absent")
            measured = await provider.inspect(
                tenant_id=record.tenant_id,
                storage_key=record.storage_key,
                object_version_id=discovered.object_version_id,
                max_bytes=record.expected_size_bytes,
            )
            if (
                measured.digest != record.expected_digest
                or measured.size_bytes != record.expected_size_bytes
                or measured.media_type != record.media_type
                or measured.compression != record.compression
                or measured.object_version_id != discovered.object_version_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="discovered upload differs from the reservation",
                )
            return response(contract, measured)
        if contract.action == "list-upload-version":
            discovered = await provider.discover_upload_version(
                tenant_id=record.tenant_id,
                storage_key=record.storage_key,
            )
            absence_fence_version_id = None
            measured = None
            if discovered is not None and discovered.is_absence_fence:
                # Crash recovery: a provider-committed fence may predate its
                # issuer receipt. Re-submit the exact immutable fence instead
                # of misclassifying it as customer content.
                absence_fence_version_id = discovered.object_version_id
            elif discovered is not None:
                measured = await provider.inspect(
                    tenant_id=record.tenant_id,
                    storage_key=record.storage_key,
                    object_version_id=discovered.object_version_id,
                    max_bytes=record.expected_size_bytes,
                )
                if (
                    measured.digest != record.expected_digest
                    or measured.size_bytes != record.expected_size_bytes
                    or measured.media_type != record.media_type
                    or measured.compression != record.compression
                    or measured.object_version_id != discovered.object_version_id
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="orphan provider version differs from the upload reservation",
                    )
            else:
                absence_fence_version_id = await provider.seal_absent_upload(
                    tenant_id=record.tenant_id,
                    storage_key=record.storage_key,
                )
            observation = await observations.record(
                action=(
                    "orphan-absence-fenced"
                    if absence_fence_version_id is not None
                    else "orphan-version-observed"
                ),
                record=record,
                object_version_id=(
                    absence_fence_version_id
                    if absence_fence_version_id is not None
                    else discovered.object_version_id
                ),
                digest=None if measured is None else measured.digest,
                size_bytes=None if measured is None else measured.size_bytes,
                media_type=None if measured is None else measured.media_type,
                compression=None if measured is None else measured.compression,
                observed_at=(datetime.now(UTC) if discovered is None else discovered.observed_at),
            )
            return response(
                contract,
                {
                    "object_version_id": (
                        None
                        if discovered is None or discovered.is_absence_fence
                        else discovered.object_version_id
                    ),
                    "absence_fence_version_id": absence_fence_version_id,
                    "size_bytes": (
                        None if discovered is None or discovered.is_absence_fence else discovered.size_bytes
                    ),
                    "media_type": (
                        None if discovered is None or discovered.is_absence_fence else discovered.media_type
                    ),
                    "compression": (
                        None
                        if discovered is None
                        or discovered.is_absence_fence
                        or discovered.compression is None
                        else discovered.compression.value
                    ),
                    "observed_at": (
                        None
                        if discovered is None or discovered.is_absence_fence
                        else discovered.observed_at.isoformat().replace("+00:00", "Z")
                    ),
                },
                provider_observation=observation,
            )
        if contract.action == "delete":
            raise HTTPException(
                status_code=403,
                detail="artifact provider retention deletion is not activated",
            )
        raise HTTPException(status_code=400, detail="artifact action requires the content endpoint")

    @app.post("/v1/content:put", response_model=VerifiedStoredObject)
    async def artifact_put(
        raw_request: Request,
        http_response: Response,
        x_fs2_artifact_request: str = Header(max_length=4096),
        authorization: str | None = Header(default=None),
        x_fs2_artifact_authority: str | None = Header(default=None),
    ) -> VerifiedStoredObject:
        try:
            contract = BrokerRequest.model_validate_json(x_fs2_artifact_request)
        except ValueError:
            raise HTTPException(status_code=400, detail="artifact write contract is invalid") from None
        if contract.action != "put":
            raise HTTPException(status_code=400, detail="artifact write action differs")
        record = await authorized(contract, authorization, x_fs2_artifact_authority)
        if record.expected_size_bytes > settings.proxy_max_put_bytes:
            raise HTTPException(
                status_code=413,
                detail="artifact exceeds the broker proxy ceiling; use the exact-size signed upload flow",
            )
        declared_length = raw_request.headers.get("content-length")
        if declared_length != str(record.expected_size_bytes):
            raise HTTPException(status_code=413, detail="artifact write length differs from reservation")
        payload = await raw_request.body()
        if len(payload) != record.expected_size_bytes:
            raise HTTPException(status_code=413, detail="artifact write length differs from reservation")
        if f"sha256:{hashlib.sha256(payload).hexdigest()}" != record.expected_digest:
            raise HTTPException(status_code=409, detail="artifact write digest differs from reservation")
        # The put authorization can create exactly one immutable version, but
        # cannot inspect it.  The caller must durably register the returned
        # version and make a separate ``finalize-inspect`` request, which is
        # re-authorized against that ledger entry before the provider GET.
        object_version_id = await provider.put_version(
            tenant_id=record.tenant_id,
            storage_key=record.storage_key,
            payload=payload,
            media_type=record.media_type,
            compression=record.compression,
        )
        bind_identity_headers(http_response)
        return VerifiedStoredObject(
            storage_key=record.storage_key,
            digest=record.expected_digest,
            size_bytes=record.expected_size_bytes,
            media_type=record.media_type,
            compression=record.compression,
            object_version_id=object_version_id,
        )

    @app.post("/v1/content:read")
    async def artifact_read(
        contract: BrokerRequest,
        authorization: str | None = Header(default=None),
        x_fs2_artifact_authority: str | None = Header(default=None),
    ) -> StreamingResponse:
        if contract.action != "read":
            raise HTTPException(status_code=400, detail="artifact read action differs")
        record = await authorized(contract, authorization, x_fs2_artifact_authority)
        if (
            contract.expected_size_bytes != record.expected_size_bytes
            or contract.expected_media_type != record.media_type
            or contract.expected_compression != record.compression
        ):
            raise HTTPException(status_code=409, detail="artifact read metadata differs")
        assert record.object_version_id is not None
        result = StreamingResponse(
            provider.stream_object(
                tenant_id=record.tenant_id,
                storage_key=record.storage_key,
                object_version_id=record.object_version_id,
                expected_size_bytes=record.expected_size_bytes,
                expected_media_type=record.media_type,
                expected_compression=record.compression,
            ),
            media_type="application/octet-stream",
        )
        bind_identity_headers(result)
        return result

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "schema": BROKER_SCHEMA}

    @app.get("/v1/readiness", response_model=BrokerReadiness)
    async def tenant_readiness(
        authorization: str | None = Header(default=None),
    ) -> BrokerReadiness:
        caller = await reviewer.review(_required_bearer(authorization, label="workload identity"))
        if caller != settings.allowed_gateway_subject:
            raise HTTPException(status_code=403, detail="artifact broker readiness caller differs")
        await provider.assert_tenant_ready(settings.allowed_tenant_id)
        if await pool.fetchval("SELECT 1") != 1:
            raise HTTPException(status_code=503, detail="artifact broker database readiness differs")
        return BrokerReadiness(
            tenant_id=settings.allowed_tenant_id,
            credential_generation=settings.provider_generation,
            provider_binding_sha256=settings.provider_binding_sha256,
        )

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Generic kubelet readiness backed by provider and database access."""

        try:
            await provider.assert_tenant_ready(settings.allowed_tenant_id)
            database_ready = await pool.fetchval("SELECT 1")
        except Exception as error:
            raise HTTPException(status_code=503, detail="artifact broker is not ready") from error
        if database_ready != 1:
            raise HTTPException(status_code=503, detail="artifact broker is not ready")
        return {"status": "ready", "schema": BROKER_SCHEMA}

    @app.on_event("shutdown")
    async def shutdown() -> None:
        close = getattr(reviewer, "close", None)
        if close is not None:
            await close()
        await observations.close()
        await provider.close()
        await pool.close()

    return app


async def build_artifact_broker_app(settings: ArtifactBrokerSettings) -> FastAPI:
    def credential(path: Path, label: str) -> str:
        try:
            value = path.read_text(encoding="ascii").strip()
        except OSError as error:
            raise RuntimeError(f"tenant artifact broker {label} is unavailable") from error
        if not value or len(value) > 4096 or any(character.isspace() for character in value):
            raise RuntimeError(f"tenant artifact broker {label} is invalid")
        return value

    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=5,
        command_timeout=10,
        server_settings={"application_name": "fs2-artifact-credential-broker"},
    )
    assert pool is not None
    verifier = (
        ArtifactAuthorityVerifier.from_directory(settings.authority_verification_key_directory)
        if settings.authority_verification_key_directory is not None
        else ArtifactAuthorityVerifier.from_file(settings.authority_verification_key_file)  # type: ignore[arg-type]
    )
    required_key_ids = {
        str(row["key_id"]) for row in await pool.fetch("SELECT key_id FROM fs2_required_artifact_authority_keys")
    }
    if not required_key_ids.issubset(verifier.trusted_key_ids):
        await pool.close()
        raise RuntimeError("artifact broker verification keys do not cover durable admissions")
    return create_artifact_broker_app(
        settings=settings,
        pool=pool,
        reviewer=KubernetesTokenReviewer(settings),
        authorizer=PostgresArtifactAuthorizer(
            pool,
            PepperRing.from_file(settings.token_pepper_file),
            verifier,
        ),
        provider=S3ArtifactObjectStore(
            ObjectStoreConfig(
                endpoint_url=settings.provider_endpoint_url,
                bucket=settings.provider_bucket,
                region=settings.provider_region,
                access_key=credential(settings.provider_access_key_file, "access key"),
                secret_key=credential(settings.provider_secret_key_file, "secret key"),
                addressing_style=settings.provider_addressing_style,
                verify_tls=True,
                max_stream_bytes=1 << 40,
            )
        ),
        observations=ProviderObservationIssuer(settings),
    )


def serve_artifact_broker() -> None:
    import asyncio

    settings = ArtifactBrokerSettings()  # type: ignore[call-arg]
    app = asyncio.run(build_artifact_broker_app(settings))
    uvicorn.run(
        app,
        host=settings.listen_host,
        port=settings.listen_port,
        ssl_certfile=str(settings.tls_certificate_file),
        ssl_keyfile=str(settings.tls_private_key_file),
        access_log=False,
        server_header=False,
    )


__all__ = [
    "ArtifactBrokerSettings",
    "BrokerRequest",
    "BrokerResponse",
    "PostgresArtifactAuthorizer",
    "create_artifact_broker_app",
    "serve_artifact_broker",
]
