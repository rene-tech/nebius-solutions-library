"""Independent Ed25519 issuer for bounded internal artifact authority.

Only this workload mounts the private signing key. It authenticates the
gateway through Kubernetes TokenReview and re-derives every requested scope
from durable PostgreSQL state before signing. Tenant brokers mount only the
public key and repeat the durable-state checks when a capability is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
import httpx
import uvicorn
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .artifact_authority import (
    ArtifactAuthoritySigner,
    ArtifactAuthorityVerifier,
    ProviderObservationAction,
)
from .auth import MAX_OPERATOR_SESSION_LENGTH, MAX_PAT_LENGTH, OPERATOR_SESSION_DIGEST_CONTEXT, PepperRing
from .models import Scope, StrictModel
from .scientific_batch.codec import state_from_value
from .scientific_run_result import ScientificArtifactManifest
from .scientific_artifacts import assert_tenant_storage_key


class ArtifactAuthorityIssuerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FS2_ARTIFACT_AUTHORITY_", extra="forbid")

    database_url: str
    token_pepper_file: Path
    signing_key_file: Path
    verification_key_file: Path | None = None
    verification_key_directory: Path | None = None
    provider_bindings_file: Path = Path(
        "/var/run/fs2-artifact-authority/provider-bindings/bindings.json"
    )
    kubernetes_reviewer_token_file: Path = Path(
        "/var/run/secrets/fs2-artifact-authority/kubernetes/token"
    )
    kubernetes_tokenreview_url: str = "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews"
    kubernetes_ca_file: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    audience: str = "fs2-artifact-authority-issuer"
    allowed_gateway_subject: str = "system:serviceaccount:fs2-system:fs2-serve-control-plane-runtime"
    allowed_cutover_subject: str = "system:serviceaccount:fs2-system:fs2-artifact-authority-cutover"
    listen_host: str = "0.0.0.0"  # noqa: S104 - NetworkPolicy and authenticated TLS constrain this listener
    listen_port: int = Field(default=8443, ge=1, le=65535)
    tls_certificate_file: Path
    tls_private_key_file: Path

    @model_validator(mode="after")
    def validate_tokenreview(self) -> "ArtifactAuthorityIssuerSettings":
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
            raise ValueError("artifact authority TokenReview endpoint must be the exact in-cluster API")
        if (self.verification_key_file is None) == (self.verification_key_directory is None):
            raise ValueError("artifact authority requires exactly one verification file or key-ring directory")
        return self


class ExecutorIssueRequest(StrictModel):
    audience: str = Field(min_length=1, max_length=253)
    operation_id: UUID
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    worker_id: str = Field(min_length=1, max_length=128)
    fencing_token: int = Field(ge=1)
    attempt: int = Field(ge=1)
    access: Literal["read", "write"]
    artifact_id: UUID | None = None


class ControllerReadIssueRequest(StrictModel):
    audience: str = Field(min_length=1, max_length=253)
    operation_id: UUID
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    artifact_id: UUID


class OperatorIssueRequest(StrictModel):
    audience: str = Field(min_length=1, max_length=253)
    session_id: UUID
    session_secret: str = Field(min_length=1, max_length=MAX_OPERATOR_SESSION_LENGTH)
    principal_id: UUID
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    operation_id: UUID
    artifact_id: UUID


class WorkloadIssueRequest(StrictModel):
    audience: str = Field(min_length=1, max_length=253)
    operation_id: UUID
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    attempt_id: UUID
    attempt_number: int = Field(ge=1)
    access: Literal["read", "write"]
    artifact_id: UUID | None = None


class AdmissionInputIssueRequest(StrictModel):
    audience: str = Field(min_length=1, max_length=253)
    operation_id: UUID
    token_id: UUID
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    model_id: str = Field(min_length=1, max_length=128)
    protocol: str = Field(min_length=1, max_length=64)
    operation: str = Field(min_length=1, max_length=128)
    required_scope: Literal["inference.invoke", "mcp.invoke"]
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifact_ids: tuple[UUID, ...] = Field(max_length=128)


class IssuedAdmissionInput(StrictModel):
    artifact_id: UUID
    token: str = Field(min_length=1, max_length=16 * 1024)


class IssuedAdmissionInputs(StrictModel):
    operation_authority: str = Field(min_length=1, max_length=16 * 1024)
    bindings: tuple[IssuedAdmissionInput, ...]


class IssuedAuthority(StrictModel):
    token: str = Field(min_length=1, max_length=16 * 1024)


class LegacyCutoverRequest(StrictModel):
    audience: str = Field(min_length=1, max_length=253)


class LegacyCutoverReceipt(StrictModel):
    closed_at: datetime
    enrolled_operation_count: int = Field(ge=0)
    enrolled_input_count: int = Field(ge=0)


class ProviderObservationIssueRequest(StrictModel):
    audience: str = Field(min_length=1, max_length=253)
    action: ProviderObservationAction
    subject_id: UUID
    tenant_id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    credential_generation: int = Field(ge=1, le=1_000_000)
    provider_access_key_id: str = Field(min_length=8, max_length=256)
    provider_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    broker_pod_uid: UUID
    storage_key: str = Field(min_length=1, max_length=1024)
    object_version_id: str | None = Field(default=None, min_length=1, max_length=1024)
    digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    size_bytes: int | None = Field(default=None, ge=0, le=1 << 40)
    media_type: str | None = Field(default=None, min_length=3, max_length=128)
    compression: Literal["gzip", "zstd"] | None = None
    observed_at: datetime


class ProviderObservationReceipt(StrictModel):
    token: str = Field(min_length=1, max_length=16 * 1024)
    receipt_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


def _bearer(value: str | None) -> str:
    if value is None or not value.startswith("Bearer ") or value.count(" ") != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="workload identity is required")
    token = value.removeprefix("Bearer ")
    if not token or len(token.encode()) > 16 * 1024 or any(character.isspace() for character in token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="workload identity is invalid")
    return token


def _authorized_provider_binding(
    settings: ArtifactAuthorityIssuerSettings,
    request: ProviderObservationIssueRequest,
) -> str:
    """Resolve one operator-authorized generation without trusting broker claims."""

    try:
        payload = settings.provider_bindings_file.read_bytes()
        if not payload or len(payload) > 1024 * 1024:
            raise ValueError
        document = json.loads(payload)
        if (
            not isinstance(document, dict)
            or document.get("schema")
            != "fs2-serve.nebius.ai/artifact-provider-observation-bindings/v1"
            or not isinstance(document.get("tenants"), dict)
        ):
            raise ValueError
        tenant = document["tenants"][request.tenant_id]
        generation = tenant["authorized_generations"][str(request.credential_generation)]
        subject = generation["service_account_subject"]
        access_key_id = generation["access_key_id"]
        binding_sha256 = generation["binding_sha256"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=503, detail="provider observation binding is unavailable") from None
    if (
        not isinstance(subject, str)
        or not isinstance(access_key_id, str)
        or not isinstance(binding_sha256, str)
        or not hmac.compare_digest(access_key_id, request.provider_access_key_id)
        or not hmac.compare_digest(binding_sha256, request.provider_binding_sha256)
    ):
        raise HTTPException(status_code=403, detail="provider observation generation is not authorized")
    return subject


class KubernetesGatewayReviewer:
    def __init__(
        self,
        settings: ArtifactAuthorityIssuerSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            verify=str(settings.kubernetes_ca_file),
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def require_subject(
        self,
        caller_token: str,
        *,
        allowed_subject: str,
    ) -> dict[str, tuple[str, ...]]:
        try:
            reviewer = self._settings.kubernetes_reviewer_token_file.read_text(encoding="ascii").strip()
        except OSError as error:
            raise HTTPException(status_code=503, detail="issuer reviewer identity is unavailable") from error
        response = await self._client.post(
            self._settings.kubernetes_tokenreview_url,
            headers={"authorization": f"Bearer {reviewer}"},
            json={
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": caller_token, "audiences": [self._settings.audience]},
            },
        )
        if response.status_code != 201:
            raise HTTPException(status_code=503, detail="issuer caller authentication is unavailable")
        try:
            result = response.json()["status"]
            subject = result["user"]["username"]
            audiences = result["audiences"]
            raw_extra = result["user"].get("extra", {})
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=503, detail="issuer caller authentication is invalid") from None
        if (
            result.get("authenticated") is not True
            or self._settings.audience not in audiences
            or subject != allowed_subject
        ):
            raise HTTPException(status_code=403, detail="issuer caller workload is not authorized")
        if not isinstance(raw_extra, dict):
            raise HTTPException(status_code=503, detail="issuer caller binding is invalid")
        extra: dict[str, tuple[str, ...]] = {}
        for key, values in raw_extra.items():
            if not isinstance(key, str) or not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ):
                raise HTTPException(status_code=503, detail="issuer caller binding is invalid")
            extra[key] = tuple(values)
        return extra

    async def require_gateway(self, caller_token: str) -> None:
        await self.require_subject(caller_token, allowed_subject=self._settings.allowed_gateway_subject)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


async def _operation_admission_is_bound(
    pool: asyncpg.Pool[Any],
    verifier: ArtifactAuthorityVerifier,
    *,
    operation_id: UUID,
    tenant_id: str,
) -> bool:
    """Require an issuer signature rooted in a still-active exact PAT."""

    row = await pool.fetchrow(
        "SELECT root.authority_token,root.token_id AS root_token_id,"
        "root.model_id AS root_model_id,root.protocol AS root_protocol,"
        "root.operation AS root_operation,root.required_scope,root.request_sha256,"
        "operation.token_id AS operation_token_id,operation.model_id AS operation_model_id,"
        "operation.protocol AS operation_protocol,operation.operation AS operation_name,"
        "token.scopes,token.models,token.revoked_at,token.expires_at "
        "FROM fs2_operation_admission_authorities root "
        "JOIN fs2_operations operation ON operation.id=root.operation_id "
        "JOIN fs2_tokens token ON token.id=root.token_id "
        "WHERE root.operation_id=$1 AND root.tenant_id=$2 AND operation.tenant_id=$2",
        operation_id,
        tenant_id,
    )
    if row is None:
        legacy = await pool.fetchrow(
            "SELECT candidate.token_id,candidate.tenant_id,candidate.model_id,candidate.protocol,"
            "candidate.operation,candidate.authorized_invoke_scopes,candidate.request_hmac_key_id,"
            "candidate.request_hmac,candidate.accepted_at,token.scopes,token.models,"
            "token.revoked_at,token.expires_at "
            "FROM fs2_operation_admission_legacy_candidates candidate "
            "JOIN fs2_operations operation ON operation.id=candidate.operation_id "
            "JOIN fs2_tokens token ON token.id=candidate.token_id "
            "WHERE candidate.operation_id=$1 AND candidate.tenant_id=$2 "
            "AND ROW(operation.token_id,operation.tenant_id,operation.model_id,operation.protocol,"
            "operation.operation,operation.request_hmac_key_id,operation.request_hmac,operation.accepted_at) "
            "IS NOT DISTINCT FROM ROW(candidate.token_id,candidate.tenant_id,candidate.model_id,"
            "candidate.protocol,candidate.operation,candidate.request_hmac_key_id,"
            "candidate.request_hmac,candidate.accepted_at)",
            operation_id,
            tenant_id,
        )
        if legacy is None:
            return False
        now = datetime.now(UTC)
        scopes = frozenset(str(value) for value in legacy["scopes"])
        captured_scopes = frozenset(str(value) for value in legacy["authorized_invoke_scopes"])
        models = frozenset(str(value) for value in legacy["models"])
        return bool(
            captured_scopes == scopes.intersection({"inference.invoke", "mcp.invoke"})
            and captured_scopes
            and ("*" in models or legacy["model_id"] in models)
            and legacy["revoked_at"] is None
            and (legacy["expires_at"] is None or legacy["expires_at"] > now)
        )
    try:
        authority = verifier.verify_operation_admission(str(row["authority_token"]))
    except ValueError:
        return False
    now = datetime.now(UTC)
    return bool(
        authority.operation_id == operation_id
        and authority.token_id == row["root_token_id"]
        and row["root_token_id"] == row["operation_token_id"]
        and authority.tenant_id == tenant_id
        and authority.model_id == row["root_model_id"] == row["operation_model_id"]
        and authority.protocol == row["root_protocol"] == row["operation_protocol"]
        and authority.operation == row["root_operation"] == row["operation_name"]
        and authority.required_scope == row["required_scope"]
        and authority.required_scope in frozenset(str(value) for value in row["scopes"])
        and ("*" in row["models"] or authority.model_id in row["models"])
        and authority.request_sha256 == row["request_sha256"]
        and row["revoked_at"] is None
        and (row["expires_at"] is None or row["expires_at"] > now)
    )


async def _executor_artifact_is_bound(
    pool: asyncpg.Pool[Any],
    verifier: ArtifactAuthorityVerifier,
    *,
    operation_id: UUID,
    tenant_id: str,
    artifact_id: UUID,
) -> bool:
    """Consult the immutable admission ledger, never a caller-provided prefix."""

    row = await pool.fetchrow(
        "SELECT binding.authority_token,operation.token_id,artifact.operation_id AS producer_operation_id,"
        "artifact.object_version_id,artifact.digest,token.revoked_at,token.expires_at,"
        "root.authority_token AS operation_authority_token,root.request_sha256 "
        "FROM fs2_operation_artifact_inputs binding "
        "JOIN fs2_operations operation ON operation.id=binding.operation_id "
        "JOIN fs2_operation_admission_authorities root ON root.operation_id=binding.operation_id "
        "JOIN fs2_scientific_artifacts artifact ON artifact.id=binding.artifact_id "
        "JOIN fs2_tokens token ON token.id=operation.token_id "
        "WHERE binding.operation_id=$1 AND binding.tenant_id=$2 AND binding.artifact_id=$3",
        operation_id,
        tenant_id,
        artifact_id,
    )
    if row is None:
        legacy = await pool.fetchrow(
            "SELECT legacy.tenant_id,legacy.producer_operation_id,legacy.object_version_id,"
            "legacy.digest,legacy.size_bytes,legacy.media_type,legacy.compression,"
            "artifact.operation_id AS artifact_operation_id,artifact.object_version_id AS artifact_version_id,"
            "artifact.digest AS artifact_digest,artifact.size_bytes AS artifact_size_bytes,"
            "artifact.media_type AS artifact_media_type,artifact.compression AS artifact_compression "
            "FROM fs2_operation_admission_legacy_inputs legacy "
            "JOIN fs2_scientific_artifacts artifact ON artifact.id=legacy.artifact_id "
            "WHERE legacy.operation_id=$1 AND legacy.tenant_id=$2 AND legacy.artifact_id=$3",
            operation_id,
            tenant_id,
            artifact_id,
        )
        if legacy is None or not await _operation_admission_is_bound(
            pool,
            verifier,
            operation_id=operation_id,
            tenant_id=tenant_id,
        ):
            return False
        return bool(
            legacy["tenant_id"] == tenant_id
            and legacy["producer_operation_id"] == legacy["artifact_operation_id"]
            and legacy["object_version_id"] == legacy["artifact_version_id"]
            and legacy["digest"] == legacy["artifact_digest"]
            and legacy["size_bytes"] == legacy["artifact_size_bytes"]
            and legacy["media_type"] == legacy["artifact_media_type"]
            and legacy["compression"] == legacy["artifact_compression"]
        )
    if not await _operation_admission_is_bound(
        pool,
        verifier,
        operation_id=operation_id,
        tenant_id=tenant_id,
    ):
        return False
    try:
        authority = verifier.verify_admission_input(str(row["authority_token"]))
    except ValueError:
        return False
    now = datetime.now(UTC)
    return bool(
        authority.token_id == row["token_id"]
        and authority.tenant_id == tenant_id
        and authority.consumer_operation_id == operation_id
        and authority.operation_authority_sha256
        == hashlib.sha256(str(row["operation_authority_token"]).encode()).hexdigest()
        and authority.request_sha256 == row["request_sha256"]
        and authority.artifact_id == artifact_id
        and authority.producer_operation_id == row["producer_operation_id"]
        and authority.object_version_id == str(row["object_version_id"])
        and authority.digest == str(row["digest"])
        and row["object_version_id"] is not None
        and row["revoked_at"] is None
        and (row["expires_at"] is None or row["expires_at"] > now)
    )


def _manifest_value(raw: object) -> object:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("stored scientific artifact manifest is invalid") from error
    return raw


async def _workload_artifact_is_bound(
    pool: asyncpg.Pool[Any],
    verifier: ArtifactAuthorityVerifier,
    *,
    operation_id: UUID,
    tenant_id: str,
    stage_id: str,
    shard_id: str,
    artifact_id: UUID,
) -> bool:
    """Re-derive one attempt input from immutable batch and commit state.

    The shared gateway can name an artifact, but it cannot make that artifact
    appear in the frozen invocation, verified input manifest, or semantic
    predecessor commit.  This check therefore remains authoritative even when
    the gateway workload identity is compromised.
    """

    batch = await pool.fetchrow(
        "SELECT operation_id,tenant_id,state FROM fs2_scientific_batches "
        "WHERE operation_id=$1 AND tenant_id=$2",
        operation_id,
        tenant_id,
    )
    if batch is None:
        return False
    try:
        state = state_from_value(batch["state"])
        if (
            state.operation_id != operation_id
            or state.tenant_id != tenant_id
            or state.execution_plan is None
            or state.input_manifest is None
        ):
            return False
        invocation = state.execution_plan.invocation(
            stage_id,
            None if shard_id == "-" else shard_id,
        )
    except (KeyError, TypeError, ValueError):
        return False

    for materialization in invocation.materializations:
        logical_id = materialization.artifact_id
        try:
            source = state.input_manifest.artifact(logical_id)
        except ValueError:
            source = None
        if source is not None:
            if source.artifact_id == artifact_id and await _executor_artifact_is_bound(
                pool,
                verifier,
                operation_id=operation_id,
                tenant_id=tenant_id,
                artifact_id=artifact_id,
            ):
                return True
            continue
        producer = state.execution_plan.producer(logical_id)
        if producer is None:
            continue
        committed = await pool.fetchrow(
            "SELECT manifest,semantic_valid FROM fs2_scientific_stage_commits "
            "WHERE operation_id=$1 AND tenant_id=$2 AND stage_id=$3",
            operation_id,
            tenant_id,
            producer.stage_id,
        )
        if committed is None or committed["semantic_valid"] is not True:
            continue
        try:
            manifest = ScientificArtifactManifest.model_validate(_manifest_value(committed["manifest"]))
        except (TypeError, ValueError):
            continue
        matches = [entry for entry in manifest.entries if entry.name == logical_id]
        if len(matches) != 1:
            continue
        try:
            committed_artifact_id = UUID(matches[0].artifact.artifact_id)
        except (TypeError, ValueError):
            continue
        if committed_artifact_id != artifact_id:
            continue
        owned = await pool.fetchval(
            "SELECT true FROM fs2_scientific_artifacts "
            "WHERE id=$1 AND operation_id=$2 AND tenant_id=$3 AND object_version_id IS NOT NULL",
            artifact_id,
            operation_id,
            tenant_id,
        )
        if owned:
            return True
    return False


def create_artifact_authority_issuer_app(
    *,
    settings: ArtifactAuthorityIssuerSettings,
    pool: asyncpg.Pool[Any],
    signer: ArtifactAuthoritySigner,
    verifier: ArtifactAuthorityVerifier,
    peppers: PepperRing,
    reviewer: KubernetesGatewayReviewer,
) -> FastAPI:
    app = FastAPI(title="FS2 Artifact Authority Issuer", docs_url=None, redoc_url=None, openapi_url=None)

    async def authenticate(authorization: str | None, audience: str) -> None:
        if audience != settings.audience:
            raise HTTPException(status_code=403, detail="artifact authority audience differs")
        await reviewer.require_gateway(_bearer(authorization))

    async def independently_authorize_pat(
        request: AdmissionInputIssueRequest,
        end_user_authorization: str | None,
    ) -> frozenset[str]:
        token = _bearer(end_user_authorization)
        if len(token) > MAX_PAT_LENGTH:
            raise HTTPException(status_code=401, detail="artifact admission PAT was rejected")
        pieces = token.split("_", 3)
        if len(pieces) != 4 or pieces[:2] != ["fs2", "pat"] or len(pieces[3]) < 32:
            raise HTTPException(status_code=401, detail="artifact admission PAT was rejected")
        try:
            embedded_token_id = UUID(hex=pieces[2])
        except ValueError:
            raise HTTPException(status_code=401, detail="artifact admission PAT was rejected") from None
        if embedded_token_id != request.token_id:
            raise HTTPException(status_code=403, detail="artifact admission PAT identity differs")
        row = await pool.fetchrow(
            "SELECT id,prefix,pepper_key_id,digest,tenant_id,scopes,models,expires_at,revoked_at "
            "FROM fs2_tokens WHERE id=$1",
            request.token_id,
        )
        if row is None:
            raise HTTPException(status_code=401, detail="artifact admission PAT was rejected")
        try:
            pepper = peppers.keys[str(row["pepper_key_id"])]
            prehash = hmac.new(pepper, token.encode(), hashlib.sha256).hexdigest()
            valid = await asyncio.to_thread(
                PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16).verify,
                str(row["digest"]),
                prehash,
            )
        except (KeyError, InvalidHashError, VerifyMismatchError):
            raise HTTPException(status_code=401, detail="artifact admission PAT was rejected") from None
        now = datetime.now(UTC)
        scopes = frozenset(str(value) for value in row["scopes"])
        models = frozenset(str(value) for value in row["models"])
        if (
            not valid
            or not hmac.compare_digest(str(row["prefix"]), f"fs2_pat_{pieces[2][:12]}")
            or str(row["tenant_id"]) != request.tenant_id
            or row["revoked_at"] is not None
            or (row["expires_at"] is not None and row["expires_at"] <= now)
            or request.required_scope not in scopes
            or ("*" not in models and request.model_id not in models)
        ):
            raise HTTPException(status_code=403, detail="operation admission PAT lacks durable model authority")
        if request.artifact_ids and not (
            str(Scope.OPERATIONS_RESULT) in scopes or str(Scope.TENANT_ADMIN) in scopes
        ):
            raise HTTPException(status_code=403, detail="artifact admission PAT lacks durable input authority")
        return scopes

    @app.post("/v1/admission-inputs", response_model=IssuedAdmissionInputs)
    async def issue_admission_inputs(
        request: AdmissionInputIssueRequest,
        authorization: str | None = Header(default=None),
        x_fs2_end_user_authorization: str | None = Header(default=None),
    ) -> IssuedAdmissionInputs:
        await authenticate(authorization, request.audience)
        if len(request.artifact_ids) != len(set(request.artifact_ids)):
            raise HTTPException(status_code=400, detail="artifact admission input is repeated")
        token_scopes = await independently_authorize_pat(request, x_fs2_end_user_authorization)
        operation_authority = signer.issue_operation_admission(
            operation_id=request.operation_id,
            token_id=request.token_id,
            tenant_id=request.tenant_id,
            model_id=request.model_id,
            protocol=request.protocol,
            operation=request.operation,
            required_scope=request.required_scope,
            request_sha256=request.request_sha256,
        )
        operation_authority_sha256 = hashlib.sha256(operation_authority.encode()).hexdigest()
        rows = await pool.fetch(
            "SELECT artifact.id,artifact.tenant_id,artifact.operation_id,artifact.object_version_id,"
            "artifact.digest,producer.token_id AS producer_token_id "
            "FROM fs2_scientific_artifacts artifact "
            "JOIN fs2_operations producer ON producer.id=artifact.operation_id "
            "WHERE artifact.id=ANY($1::uuid[])",
            list(request.artifact_ids),
        )
        by_id = {row["id"]: row for row in rows}
        if set(by_id) != set(request.artifact_ids):
            raise HTTPException(status_code=403, detail="artifact admission input is unavailable")
        bindings: list[IssuedAdmissionInput] = []
        for artifact_id in request.artifact_ids:
            row = by_id[artifact_id]
            if (
                str(row["tenant_id"]) != request.tenant_id
                or row["object_version_id"] is None
                or (
                    row["producer_token_id"] != request.token_id
                    and str(Scope.TENANT_ADMIN) not in token_scopes
                )
            ):
                raise HTTPException(status_code=403, detail="artifact admission input belongs to another principal")
            bindings.append(
                IssuedAdmissionInput(
                    artifact_id=artifact_id,
                    token=signer.issue_admission_input(
                        token_id=request.token_id,
                        tenant_id=request.tenant_id,
                        consumer_operation_id=request.operation_id,
                        operation_authority_sha256=operation_authority_sha256,
                        request_sha256=request.request_sha256,
                        artifact_id=artifact_id,
                        producer_operation_id=row["operation_id"],
                        object_version_id=str(row["object_version_id"]),
                        digest=str(row["digest"]),
                    ),
                )
            )
        return IssuedAdmissionInputs(
            operation_authority=operation_authority,
            bindings=tuple(bindings),
        )

    @app.post("/v1/executor", response_model=IssuedAuthority)
    async def issue_executor(
        request: ExecutorIssueRequest,
        authorization: str | None = Header(default=None),
    ) -> IssuedAuthority:
        await authenticate(authorization, request.audience)
        operation = await pool.fetchrow(
            "SELECT tenant_id,worker_id,fencing_token,attempt,status,lease_expires_at "
            "FROM fs2_operations WHERE id=$1",
            request.operation_id,
        )
        now = datetime.now(UTC)
        if (
            operation is None
            or str(operation["tenant_id"]) != request.tenant_id
            or str(operation["worker_id"]) != request.worker_id
            or int(operation["fencing_token"]) != request.fencing_token
            or int(operation["attempt"]) != request.attempt
            or str(operation["status"]) != "running"
            or operation["lease_expires_at"] is None
            or operation["lease_expires_at"] <= now
        ):
            raise HTTPException(status_code=403, detail="executor lease is not durably active")
        if not await _operation_admission_is_bound(
            pool,
            verifier,
            operation_id=request.operation_id,
            tenant_id=request.tenant_id,
        ):
            raise HTTPException(
                status_code=403,
                detail="executor operation lacks an active admission authority",
            )
        if request.access == "read":
            if request.artifact_id is None:
                raise HTTPException(status_code=403, detail="executor read scope requires one artifact")
            if not await _executor_artifact_is_bound(
                pool,
                verifier,
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
                artifact_id=request.artifact_id,
            ):
                raise HTTPException(
                    status_code=403,
                    detail="executor artifact is absent from the immutable request binding",
                )
        elif request.artifact_id is not None:
            raise HTTPException(status_code=403, detail="executor write scope cannot name an existing artifact")
        return IssuedAuthority(
            token=signer.issue_executor_scope(
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
                worker_id=request.worker_id,
                fencing_token=request.fencing_token,
                attempt=request.attempt,
                access=request.access,
                artifact_id=request.artifact_id,
            )
        )

    @app.post("/v1/operator", response_model=IssuedAuthority)
    async def issue_operator(
        request: OperatorIssueRequest,
        authorization: str | None = Header(default=None),
    ) -> IssuedAuthority:
        await authenticate(authorization, request.audience)
        pieces = request.session_secret.split("_", 3)
        if len(pieces) != 4 or pieces[:2] != ["fs2", "admin"] or pieces[2] != request.session_id.hex:
            raise HTTPException(status_code=401, detail="operator session proof is invalid")
        session = await pool.fetchrow(
            "SELECT session.principal_id,session.pepper_key_id,session.digest,session.expires_at,"
            "session.revoked_at,principal.role,principal.tenant_id,principal.enabled "
            "FROM fs2_operator_sessions session "
            "JOIN fs2_operator_principals principal ON principal.id=session.principal_id "
            "WHERE session.id=$1",
            request.session_id,
        )
        if session is None:
            raise HTTPException(status_code=401, detail="operator session proof is invalid")
        try:
            pepper = peppers.keys[str(session["pepper_key_id"])]
        except KeyError:
            raise HTTPException(status_code=503, detail="operator session verifier is unavailable") from None
        digest = hmac.new(
            pepper,
            OPERATOR_SESSION_DIGEST_CONTEXT + request.session_secret.encode(),
            hashlib.sha256,
        ).hexdigest()
        artifact = await pool.fetchrow(
            "SELECT tenant_id,operation_id FROM fs2_scientific_artifacts WHERE id=$1",
            request.artifact_id,
        )
        now = datetime.now(UTC)
        if (
            not hmac.compare_digest(str(session["digest"]), digest)
            or session["principal_id"] != request.principal_id
            or str(session["role"]) not in {"viewer", "operator", "admin"}
            or session["revoked_at"] is not None
            or session["expires_at"] <= now
            or session["enabled"] is not True
            or (session["tenant_id"] is not None and str(session["tenant_id"]) != request.tenant_id)
            or artifact is None
            or str(artifact["tenant_id"]) != request.tenant_id
            or artifact["operation_id"] != request.operation_id
        ):
            raise HTTPException(status_code=403, detail="operator artifact scope is not durably authorized")
        return IssuedAuthority(
            token=signer.issue_operator_scope(
                session_id=request.session_id,
                principal_id=request.principal_id,
                role=str(session["role"]),
                tenant_id=request.tenant_id,
                operation_id=request.operation_id,
                artifact_id=request.artifact_id,
                session_expires_at=session["expires_at"],
            )
        )

    @app.post("/v1/controller-read", response_model=IssuedAuthority)
    async def issue_controller_read(
        request: ControllerReadIssueRequest,
        authorization: str | None = Header(default=None),
    ) -> IssuedAuthority:
        await authenticate(authorization, request.audience)
        batch = await pool.fetchrow(
            "SELECT controller_id,fencing_token,lease_expires_at "
            "FROM fs2_scientific_batches WHERE operation_id=$1 AND tenant_id=$2",
            request.operation_id,
            request.tenant_id,
        )
        artifact = await pool.fetchrow(
            "SELECT tenant_id,operation_id FROM fs2_scientific_artifacts WHERE id=$1",
            request.artifact_id,
        )
        now = datetime.now(UTC)
        if (
            batch is None
            or not str(batch["controller_id"] or "")
            or int(batch["fencing_token"]) < 1
            or batch["lease_expires_at"] is None
            or batch["lease_expires_at"] <= now
            or artifact is None
            or str(artifact["tenant_id"]) != request.tenant_id
            or artifact["operation_id"] != request.operation_id
        ):
            raise HTTPException(status_code=403, detail="scientific controller lease cannot read the artifact")
        if not await _operation_admission_is_bound(
            pool,
            verifier,
            operation_id=request.operation_id,
            tenant_id=request.tenant_id,
        ):
            raise HTTPException(status_code=403, detail="scientific controller lacks an issuer-signed admission root")
        return IssuedAuthority(
            token=signer.issue_controller_scope(
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
                controller_id=str(batch["controller_id"]),
                fencing_token=int(batch["fencing_token"]),
                artifact_id=request.artifact_id,
            )
        )

    @app.post("/v1/workload", response_model=IssuedAuthority)
    async def issue_workload(
        request: WorkloadIssueRequest,
        authorization: str | None = Header(default=None),
    ) -> IssuedAuthority:
        await authenticate(authorization, request.audience)
        attempt = await pool.fetchrow(
            "SELECT operation_id,tenant_id,stage_id,shard_id,attempt_number,status "
            "FROM fs2_scientific_stage_attempts WHERE attempt_id=$1",
            request.attempt_id,
        )
        if (
            attempt is None
            or attempt["operation_id"] != request.operation_id
            or str(attempt["tenant_id"]) != request.tenant_id
            or int(attempt["attempt_number"]) != request.attempt_number
            or str(attempt["status"]) != "running"
        ):
            raise HTTPException(status_code=403, detail="scientific workload attempt is not durably active")
        if not await _operation_admission_is_bound(
            pool,
            verifier,
            operation_id=request.operation_id,
            tenant_id=request.tenant_id,
        ):
            raise HTTPException(status_code=403, detail="scientific workload lacks an issuer-signed admission root")
        if request.access == "read":
            if request.artifact_id is None:
                raise HTTPException(status_code=403, detail="workload read scope requires one artifact")
            if not await _workload_artifact_is_bound(
                pool,
                verifier,
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
                stage_id=str(attempt["stage_id"]),
                shard_id=str(attempt["shard_id"]),
                artifact_id=request.artifact_id,
            ):
                raise HTTPException(
                    status_code=403,
                    detail="workload artifact is absent from the immutable invocation binding",
                )
        elif request.artifact_id is not None:
            raise HTTPException(status_code=403, detail="workload write scope cannot name an existing artifact")
        return IssuedAuthority(
            token=signer.issue_workload_scope(
                operation_id=request.operation_id,
                tenant_id=request.tenant_id,
                attempt_id=request.attempt_id,
                attempt_number=request.attempt_number,
                access=request.access,
                artifact_id=request.artifact_id,
            )
        )

    @app.post("/v1/provider-observations", response_model=ProviderObservationReceipt)
    async def record_provider_observation(
        request: ProviderObservationIssueRequest,
        authorization: str | None = Header(default=None),
    ) -> ProviderObservationReceipt:
        """Accept a provider fact only from the exact tenant broker identity."""

        if request.audience != settings.audience:
            raise HTTPException(status_code=403, detail="artifact authority audience differs")
        allowed_subject = _authorized_provider_binding(settings, request)
        caller_extra = await reviewer.require_subject(
            _bearer(authorization),
            allowed_subject=allowed_subject,
        )
        pod_uids = caller_extra.get("authentication.kubernetes.io/pod-uid", ())
        if pod_uids != (str(request.broker_pod_uid),):
            raise HTTPException(status_code=403, detail="provider observation pod binding differs")
        try:
            assert_tenant_storage_key(request.tenant_id, request.storage_key)
        except ValueError:
            raise HTTPException(status_code=403, detail="provider observation key is not canonical") from None

        if request.action == "backfill-observed":
            row = await pool.fetchrow(
                "SELECT tenant_id,storage_key,object_version_id,digest,size_bytes,media_type,compression "
                "FROM fs2_scientific_artifacts WHERE id=$1",
                request.subject_id,
            )
            if (
                row is None
                or str(row["tenant_id"]) != request.tenant_id
                or str(row["storage_key"]) != request.storage_key
                or row["object_version_id"] is not None
                or row["digest"] != request.digest
                or row["size_bytes"] != request.size_bytes
                or row["media_type"] != request.media_type
                or row["compression"] != request.compression
                or request.object_version_id is None
            ):
                raise HTTPException(status_code=409, detail="provider backfill observation differs")
        else:
            row = await pool.fetchrow(
                "SELECT upload.tenant_id,upload.storage_key,upload.artifact_id,upload.begun_at,"
                "claim.eligible_at,version.object_version_id "
                "FROM fs2_scientific_uploads upload "
                "JOIN fs2_scientific_abandoned_upload_claims claim ON claim.upload_id=upload.id "
                "LEFT JOIN fs2_scientific_upload_object_versions version ON version.upload_id=upload.id "
                "WHERE upload.id=$1",
                request.subject_id,
            )
            if (
                row is None
                or str(row["tenant_id"]) != request.tenant_id
                or str(row["storage_key"]) != request.storage_key
                or row["artifact_id"] is not None
                or request.observed_at < row["eligible_at"]
            ):
                raise HTTPException(status_code=409, detail="orphan provider observation differs")
            if request.action == "orphan-absence-fenced" and (
                request.object_version_id is None or row["object_version_id"] is not None
            ):
                raise HTTPException(status_code=409, detail="orphan absence fence conflicts with a known version")
        try:
            token = signer.issue_provider_observation(
                action=request.action,
                subject_id=request.subject_id,
                tenant_id=request.tenant_id,
                credential_generation=request.credential_generation,
                provider_access_key_id=request.provider_access_key_id,
                broker_service_account_subject=allowed_subject,
                broker_pod_uid=request.broker_pod_uid,
                provider_binding_sha256=request.provider_binding_sha256,
                storage_key=request.storage_key,
                object_version_id=request.object_version_id,
                digest=request.digest,
                size_bytes=request.size_bytes,
                media_type=request.media_type,
                compression=request.compression,
                observed_at=request.observed_at,
            )
        except ValueError:
            raise HTTPException(status_code=409, detail="provider observation is outside policy") from None
        try:
            verified = verifier.verify_provider_observation(token)
        except ValueError:
            raise HTTPException(status_code=503, detail="provider observation self-verification failed") from None
        if (
            verified.action != request.action
            or verified.subject_id != request.subject_id
            or verified.tenant_id != request.tenant_id
            or verified.credential_generation != request.credential_generation
            or verified.provider_access_key_id != request.provider_access_key_id
            or verified.broker_service_account_subject != allowed_subject
            or verified.broker_pod_uid != request.broker_pod_uid
            or verified.provider_binding_sha256 != request.provider_binding_sha256
            or verified.storage_key != request.storage_key
            or verified.object_version_id != request.object_version_id
            or verified.digest != request.digest
            or verified.size_bytes != request.size_bytes
            or verified.media_type != request.media_type
            or verified.compression != request.compression
            or verified.observed_at != request.observed_at
        ):
            raise HTTPException(status_code=503, detail="provider observation self-verification differs")
        receipt_digest = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        try:
            if request.action == "backfill-observed":
                await pool.execute(
                    "SELECT fs2_scientific_backfill_object_version($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                    request.subject_id,
                    request.storage_key,
                    request.object_version_id,
                    request.digest,
                    request.size_bytes,
                    request.media_type,
                    request.compression,
                    token,
                    receipt_digest,
                    request.observed_at,
                )
            elif request.action == "orphan-version-observed":
                await pool.execute(
                    "SELECT fs2_scientific_register_abandoned_upload_version($1,$2,$3,$4,$5,$6,$7,$8)",
                    request.subject_id,
                    request.object_version_id,
                    request.size_bytes,
                    request.media_type,
                    request.compression,
                    request.observed_at,
                    token,
                    receipt_digest,
                )
                await pool.execute(
                    "SELECT fs2_scientific_record_abandoned_upload_cleanup($1,$2,$3::text,$4,$5)",
                    request.subject_id,
                    "exact-version-quarantined",
                    request.object_version_id,
                    token,
                    receipt_digest,
                )
            elif request.action == "orphan-absence-fenced":
                await pool.execute(
                    "SELECT fs2_scientific_record_abandoned_upload_cleanup($1,$2,$3::text,$4,$5)",
                    request.subject_id,
                    "provider-write-fenced-absence",
                    request.object_version_id,
                    token,
                    receipt_digest,
                )
            else:
                raise HTTPException(status_code=403, detail="provider observation action is not accepted")
        except asyncpg.PostgresError:
            raise HTTPException(status_code=409, detail="provider observation conflicts with durable state") from None
        return ProviderObservationReceipt(token=token, receipt_digest=receipt_digest)

    @app.post("/v1/cutover/close", response_model=LegacyCutoverReceipt)
    async def close_legacy_cutover(
        request: LegacyCutoverRequest,
        authorization: str | None = Header(default=None),
    ) -> LegacyCutoverReceipt:
        if request.audience != settings.audience:
            raise HTTPException(status_code=403, detail="artifact authority audience differs")
        await reviewer.require_subject(
            _bearer(authorization),
            allowed_subject=settings.allowed_cutover_subject,
        )
        row = await pool.fetchrow("SELECT * FROM fs2_close_artifact_authority_legacy_enrollment()")
        if row is None:
            raise HTTPException(status_code=503, detail="artifact authority cutover receipt is unavailable")
        return LegacyCutoverReceipt(
            closed_at=row["cutover_closed_at"],
            enrolled_operation_count=int(row["enrolled_operation_count"]),
            enrolled_input_count=int(row["enrolled_input_count"]),
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "custody": "ed25519-private-key-issuer-only"}

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await reviewer.close()
        await pool.close()

    return app


async def build_artifact_authority_issuer_app(settings: ArtifactAuthorityIssuerSettings) -> FastAPI:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=5,
        command_timeout=10,
        server_settings={"application_name": "fs2-artifact-authority-issuer"},
    )
    assert pool is not None
    signer = ArtifactAuthoritySigner.from_file(settings.signing_key_file)
    verifier = (
        ArtifactAuthorityVerifier.from_directory(settings.verification_key_directory)
        if settings.verification_key_directory is not None
        else ArtifactAuthorityVerifier.from_file(settings.verification_key_file)  # type: ignore[arg-type]
    )
    if signer.key_id not in verifier.trusted_key_ids:
        await pool.close()
        raise RuntimeError("active artifact authority signer is absent from the retained verification key ring")
    required_key_ids = {
        str(row["key_id"]) for row in await pool.fetch("SELECT key_id FROM fs2_required_artifact_authority_keys")
    }
    if not required_key_ids.issubset(verifier.trusted_key_ids):
        await pool.close()
        raise RuntimeError("retained artifact authority verification keys do not cover durable admissions")
    return create_artifact_authority_issuer_app(
        settings=settings,
        pool=pool,
        signer=signer,
        verifier=verifier,
        peppers=PepperRing.from_file(settings.token_pepper_file),
        reviewer=KubernetesGatewayReviewer(settings),
    )


def serve_artifact_authority_issuer() -> None:
    import asyncio

    settings = ArtifactAuthorityIssuerSettings()  # type: ignore[call-arg]
    app = asyncio.run(build_artifact_authority_issuer_app(settings))
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
    "ArtifactAuthorityIssuerSettings",
    "create_artifact_authority_issuer_app",
    "serve_artifact_authority_issuer",
]
