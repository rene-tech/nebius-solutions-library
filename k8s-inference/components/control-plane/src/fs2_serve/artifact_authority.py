"""Short-lived, independently verifiable authority for internal artifact work.

The gateway's Kubernetes identity authenticates the caller to the credential
broker but never authorizes a tenant.  Background executors and browser-admin
downloads therefore carry a separate Ed25519 capability.  A distinct issuer
workload holds the private signing key; tenant brokers mount only the public
verification key, and the gateway mounts neither.  The broker also re-reads
the durable operation or operator session, so the signed claim is not
sufficient on its own.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .access_models import OperatorRole, OperatorSession
from .models import ClaimedOperation

CONTROLLER_PREFIX = "fs2_artifact_controller"
EXECUTOR_PREFIX = "fs2_artifact_executor"
OPERATOR_PREFIX = "fs2_artifact_operator"
WORKLOAD_PREFIX = "fs2_artifact_workload"
ADMISSION_PREFIX = "fs2_artifact_admission"
OPERATION_ADMISSION_PREFIX = "fs2_operation_admission"
PROVIDER_OBSERVATION_PREFIX = "fs2_provider_observation"
CONTROLLER_SCHEMA = "fs2-serve.nebius.ai/artifact-controller-authority/v1"
EXECUTOR_SCHEMA = "fs2-serve.nebius.ai/artifact-executor-authority/v1"
OPERATOR_SCHEMA = "fs2-serve.nebius.ai/artifact-operator-authority/v1"
WORKLOAD_SCHEMA = "fs2-serve.nebius.ai/artifact-workload-authority/v1"
ADMISSION_SCHEMA = "fs2-serve.nebius.ai/artifact-admission-authority/v1"
OPERATION_ADMISSION_SCHEMA = "fs2-serve.nebius.ai/operation-admission-authority/v1"
PROVIDER_OBSERVATION_SCHEMA = "fs2-serve.nebius.ai/provider-observation/v1"
_CONTEXT = "fs2-artifact-internal-authority/v1"
_MAX_TOKEN_BYTES = 16 * 1024

ArtifactExecutorAccess = Literal["read", "write"]
ProviderObservationAction = Literal[
    "backfill-observed",
    "orphan-version-observed",
    "orphan-absence-fenced",
]


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


@dataclass(frozen=True, slots=True)
class ExecutorArtifactAuthority:
    operation_id: UUID
    tenant_id: str
    worker_id: str
    fencing_token: int
    attempt: int
    access: ArtifactExecutorAccess
    artifact_id: UUID | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ControllerArtifactAuthority:
    operation_id: UUID
    tenant_id: str
    controller_id: str
    fencing_token: int
    artifact_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OperatorArtifactAuthority:
    session_id: UUID
    principal_id: UUID
    role: str
    tenant_id: str
    operation_id: UUID
    artifact_id: UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WorkloadArtifactAuthority:
    operation_id: UUID
    tenant_id: str
    attempt_id: UUID
    attempt_number: int
    access: ArtifactExecutorAccess
    artifact_id: UUID | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AdmissionArtifactAuthority:
    """Durable input binding minted only after independent PAT authorization."""

    token_id: UUID
    tenant_id: str
    consumer_operation_id: UUID
    operation_authority_sha256: str
    request_sha256: str
    artifact_id: UUID
    producer_operation_id: UUID
    object_version_id: str
    digest: str
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class OperationAdmissionAuthority:
    operation_id: UUID
    token_id: UUID
    tenant_id: str
    model_id: str
    protocol: str
    operation: str
    required_scope: str
    request_sha256: str
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderObservationAuthority:
    """Durable provider fact signed only for one authenticated tenant broker."""

    action: ProviderObservationAction
    subject_id: UUID
    tenant_id: str
    credential_generation: int
    provider_access_key_id: str
    broker_service_account_subject: str
    broker_pod_uid: UUID
    provider_binding_sha256: str
    storage_key: str
    object_version_id: str | None
    digest: str | None
    size_bytes: int | None
    media_type: str | None
    compression: str | None
    observed_at: datetime
    issued_at: datetime


def _key_id(public_key: Ed25519PublicKey) -> str:
    import hashlib

    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


class ArtifactAuthoritySigner:
    """Private-key capability signer used only by the isolated issuer."""

    def __init__(self, private_key: Ed25519PrivateKey, *, clock: Any | None = None) -> None:
        self._private_key = private_key
        self._key_id = _key_id(private_key.public_key())
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_file(cls, path: Path, *, clock: Any | None = None) -> "ArtifactAuthoritySigner":
        try:
            loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("artifact authority signing key is invalid") from error
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ValueError("artifact authority signing key must be Ed25519")
        return cls(loaded, clock=clock)

    @property
    def key_id(self) -> str:
        return self._key_id

    def _issue(self, prefix: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = self._private_key.sign(_CONTEXT.encode() + b"\x00" + encoded)
        return f"{prefix}.{self._key_id}.{_encode(encoded)}.{_encode(signature)}"

    def issue_executor(
        self,
        operation: ClaimedOperation,
        *,
        access: ArtifactExecutorAccess,
        artifact_id: UUID | None = None,
        ttl: timedelta = timedelta(minutes=2),
    ) -> str:
        if operation.fencing_token < 1 or not operation.worker_id:
            raise ValueError("artifact executor requires a fenced operation claim")
        return self.issue_executor_scope(
            operation_id=operation.id,
            tenant_id=operation.tenant_id,
            worker_id=operation.worker_id,
            fencing_token=operation.fencing_token,
            attempt=operation.attempt,
            access=access,
            artifact_id=artifact_id,
            ttl=ttl,
        )

    def issue_admission_input(
        self,
        *,
        token_id: UUID,
        tenant_id: str,
        consumer_operation_id: UUID,
        operation_authority_sha256: str,
        request_sha256: str,
        artifact_id: UUID,
        producer_operation_id: UUID,
        object_version_id: str,
        digest: str,
    ) -> str:
        """Attest one immutable artifact after the issuer revalidates its PAT.

        This receipt is intentionally durable rather than a bearer credential:
        every later use rechecks token revocation and the exact artifact row.
        The gateway can store or replay it for the same principal, but cannot
        alter the tenant, artifact, producer, version, or digest.
        """

        if (
            not tenant_id
            or not object_version_id
            or object_version_id in {"null", "unpersisted"}
            or not digest.startswith("sha256:")
            or len(digest) != 71
            or len(operation_authority_sha256) != 64
            or any(character not in "0123456789abcdef" for character in operation_authority_sha256)
            or len(request_sha256) != 64
            or any(character not in "0123456789abcdef" for character in request_sha256)
        ):
            raise ValueError("artifact admission binding is outside policy")
        return self._issue(
            ADMISSION_PREFIX,
            {
                "artifact_id": str(artifact_id),
                "consumer_operation_id": str(consumer_operation_id),
                "digest": digest,
                "issued_at": self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "object_version_id": object_version_id,
                "operation_authority_sha256": operation_authority_sha256,
                "producer_operation_id": str(producer_operation_id),
                "request_sha256": request_sha256,
                "schema": ADMISSION_SCHEMA,
                "tenant_id": tenant_id,
                "token_id": str(token_id),
            },
        )

    def issue_operation_admission(
        self,
        *,
        operation_id: UUID,
        token_id: UUID,
        tenant_id: str,
        model_id: str,
        protocol: str,
        operation: str,
        required_scope: str,
        request_sha256: str,
    ) -> str:
        if (
            not tenant_id
            or not model_id
            or not protocol
            or not operation
            or required_scope not in {"inference.invoke", "mcp.invoke"}
            or len(request_sha256) != 64
            or any(character not in "0123456789abcdef" for character in request_sha256)
        ):
            raise ValueError("operation admission authority is outside policy")
        return self._issue(
            OPERATION_ADMISSION_PREFIX,
            {
                "issued_at": self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "model_id": model_id,
                "operation": operation,
                "operation_id": str(operation_id),
                "protocol": protocol,
                "request_sha256": request_sha256,
                "required_scope": required_scope,
                "schema": OPERATION_ADMISSION_SCHEMA,
                "tenant_id": tenant_id,
                "token_id": str(token_id),
            },
        )

    def issue_provider_observation(
        self,
        *,
        action: ProviderObservationAction,
        subject_id: UUID,
        tenant_id: str,
        credential_generation: int,
        provider_access_key_id: str,
        broker_service_account_subject: str,
        broker_pod_uid: UUID,
        provider_binding_sha256: str,
        storage_key: str,
        object_version_id: str | None,
        digest: str | None,
        size_bytes: int | None,
        media_type: str | None,
        compression: str | None,
        observed_at: datetime,
    ) -> str:
        """Sign an immutable provider fact after tenant-broker TokenReview."""

        exact_version = action in {
            "backfill-observed",
            "orphan-version-observed",
            "orphan-absence-fenced",
        }
        if (
            not tenant_id
            or credential_generation < 1
            or not provider_access_key_id
            or not broker_service_account_subject.startswith(
                "system:serviceaccount:fs2-system:fs2-artifact-"
            )
            or len(provider_binding_sha256) != 64
            or any(character not in "0123456789abcdef" for character in provider_binding_sha256)
            or not storage_key
            or observed_at.tzinfo is None
            or observed_at > self._clock()
            or exact_version != (object_version_id is not None)
            or (object_version_id is not None and object_version_id in {"", "null", "unpersisted"})
            or ((digest is None) != (action not in {"backfill-observed", "orphan-version-observed"}))
            or ((size_bytes is None) != (action not in {"backfill-observed", "orphan-version-observed"}))
            or ((media_type is None) != (action not in {"backfill-observed", "orphan-version-observed"}))
            or (digest is not None and (not digest.startswith("sha256:") or len(digest) != 71))
            or (size_bytes is not None and not 0 <= size_bytes <= 1 << 40)
            or compression not in {None, "gzip", "zstd"}
        ):
            raise ValueError("provider observation is outside policy")
        return self._issue(
            PROVIDER_OBSERVATION_PREFIX,
            {
                "action": action,
                "broker_pod_uid": str(broker_pod_uid),
                "broker_service_account_subject": broker_service_account_subject,
                "compression": compression,
                "credential_generation": credential_generation,
                "digest": digest,
                "issued_at": self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "media_type": media_type,
                "object_version_id": object_version_id,
                "observed_at": observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "provider_access_key_id": provider_access_key_id,
                "provider_binding_sha256": provider_binding_sha256,
                "schema": PROVIDER_OBSERVATION_SCHEMA,
                "size_bytes": size_bytes,
                "storage_key": storage_key,
                "subject_id": str(subject_id),
                "tenant_id": tenant_id,
            },
        )

    def issue_executor_scope(
        self,
        *,
        operation_id: UUID,
        tenant_id: str,
        worker_id: str,
        fencing_token: int,
        attempt: int,
        access: ArtifactExecutorAccess,
        artifact_id: UUID | None = None,
        ttl: timedelta = timedelta(minutes=2),
    ) -> str:
        if fencing_token < 1 or not worker_id:
            raise ValueError("artifact executor requires a fenced operation claim")
        if not timedelta(0) < ttl <= timedelta(minutes=5):
            raise ValueError("artifact executor authority lifetime is outside policy")
        return self._issue(
            EXECUTOR_PREFIX,
            {
                "access": access,
                "artifact_id": None if artifact_id is None else str(artifact_id),
                "attempt": attempt,
                "expires_at": (self._clock() + ttl).astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "fencing_token": fencing_token,
                "operation_id": str(operation_id),
                "schema": EXECUTOR_SCHEMA,
                "tenant_id": tenant_id,
                "worker_id": worker_id,
            },
        )

    def issue_operator(
        self,
        session: OperatorSession,
        *,
        tenant_id: str,
        operation_id: UUID,
        artifact_id: UUID,
        ttl: timedelta = timedelta(minutes=1),
    ) -> str:
        session.principal.require(OperatorRole.VIEWER, tenant_id=tenant_id)
        if not timedelta(0) < ttl <= timedelta(minutes=2):
            raise ValueError("operator artifact authority lifetime is outside policy")
        expires_at = min(session.expires_at, self._clock() + ttl)
        if expires_at <= self._clock():
            raise ValueError("operator session is expired")
        return self.issue_operator_scope(
            session_id=session.id,
            principal_id=session.principal.id,
            role=str(session.principal.role),
            tenant_id=tenant_id,
            operation_id=operation_id,
            artifact_id=artifact_id,
            session_expires_at=session.expires_at,
            ttl=ttl,
        )

    def issue_controller_scope(
        self,
        *,
        operation_id: UUID,
        tenant_id: str,
        controller_id: str,
        fencing_token: int,
        artifact_id: UUID,
        ttl: timedelta = timedelta(minutes=2),
    ) -> str:
        if not controller_id or fencing_token < 1 or not timedelta(0) < ttl <= timedelta(minutes=5):
            raise ValueError("artifact controller authority is outside policy")
        return self._issue(
            CONTROLLER_PREFIX,
            {
                "artifact_id": str(artifact_id),
                "controller_id": controller_id,
                "expires_at": (self._clock() + ttl).astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "fencing_token": fencing_token,
                "operation_id": str(operation_id),
                "schema": CONTROLLER_SCHEMA,
                "tenant_id": tenant_id,
            },
        )

    def issue_operator_scope(
        self,
        *,
        session_id: UUID,
        principal_id: UUID,
        role: str,
        tenant_id: str,
        operation_id: UUID,
        artifact_id: UUID,
        session_expires_at: datetime,
        ttl: timedelta = timedelta(minutes=1),
    ) -> str:
        if role not in {"viewer", "operator", "admin"}:
            raise ValueError("operator role is outside artifact policy")
        if not timedelta(0) < ttl <= timedelta(minutes=2):
            raise ValueError("operator artifact authority lifetime is outside policy")
        expires_at = min(session_expires_at, self._clock() + ttl)
        if expires_at <= self._clock():
            raise ValueError("operator session is expired")
        return self._issue(
            OPERATOR_PREFIX,
            {
                "artifact_id": str(artifact_id),
                "expires_at": expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "operation_id": str(operation_id),
                "principal_id": str(principal_id),
                "role": role,
                "schema": OPERATOR_SCHEMA,
                "session_id": str(session_id),
                "tenant_id": tenant_id,
            },
        )

    def issue_workload_scope(
        self,
        *,
        operation_id: UUID,
        tenant_id: str,
        attempt_id: UUID,
        attempt_number: int,
        access: ArtifactExecutorAccess,
        artifact_id: UUID | None = None,
        ttl: timedelta = timedelta(minutes=2),
    ) -> str:
        if attempt_number < 1 or not timedelta(0) < ttl <= timedelta(minutes=5):
            raise ValueError("artifact workload authority is outside policy")
        return self._issue(
            WORKLOAD_PREFIX,
            {
                "access": access,
                "artifact_id": None if artifact_id is None else str(artifact_id),
                "attempt_id": str(attempt_id),
                "attempt_number": attempt_number,
                "expires_at": (self._clock() + ttl).astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "operation_id": str(operation_id),
                "schema": WORKLOAD_SCHEMA,
                "tenant_id": tenant_id,
            },
        )


class ArtifactAuthorityVerifier:
    """Public-key verifier used by tenant brokers; it cannot mint authority."""

    def __init__(
        self,
        public_key: Ed25519PublicKey,
        *,
        retained_public_keys: tuple[Ed25519PublicKey, ...] = (),
        clock: Any | None = None,
    ) -> None:
        self._key_id = _key_id(public_key)
        keys = (public_key, *retained_public_keys)
        self._public_keys = {_key_id(value): value for value in keys}
        if len(self._public_keys) != len(keys):
            raise ValueError("artifact authority verification key ring contains duplicate keys")
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_file(cls, path: Path, *, clock: Any | None = None) -> "ArtifactAuthorityVerifier":
        try:
            loaded = serialization.load_pem_public_key(path.read_bytes())
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("artifact authority verification key is invalid") from error
        if not isinstance(loaded, Ed25519PublicKey):
            raise ValueError("artifact authority verification key must be Ed25519")
        return cls(loaded, clock=clock)

    @classmethod
    def from_directory(cls, path: Path, *, clock: Any | None = None) -> "ArtifactAuthorityVerifier":
        try:
            paths = tuple(sorted(value for value in path.iterdir() if value.is_file() and value.suffix == ".pem"))
        except OSError as error:
            raise ValueError("artifact authority verification key ring is unavailable") from error
        loaded_keys: list[Ed25519PublicKey] = []
        for key_path in paths:
            try:
                loaded = serialization.load_pem_public_key(key_path.read_bytes())
            except (OSError, ValueError, TypeError) as error:
                raise ValueError("artifact authority verification key ring is invalid") from error
            if not isinstance(loaded, Ed25519PublicKey):
                raise ValueError("artifact authority verification key ring must contain only Ed25519 keys")
            loaded_keys.append(loaded)
        if not loaded_keys:
            raise ValueError("artifact authority verification key ring is empty")
        return cls(loaded_keys[0], retained_public_keys=tuple(loaded_keys[1:]), clock=clock)

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def trusted_key_ids(self) -> frozenset[str]:
        return frozenset(self._public_keys)

    def _verify_payload(self, token: str, prefix: str) -> dict[str, Any]:
        if not 1 <= len(token.encode("utf-8")) <= _MAX_TOKEN_BYTES:
            raise ValueError("artifact authority is invalid")
        try:
            supplied_prefix, key_id, encoded, supplied_signature = token.split(".", 3)
            if supplied_prefix != prefix or key_id not in self._public_keys:
                raise ValueError
            payload = _decode(encoded)
            self._public_keys[key_id].verify(
                _decode(supplied_signature),
                _CONTEXT.encode() + b"\x00" + payload,
            )
            value = json.loads(payload)
        except (InvalidSignature, UnicodeError, ValueError, json.JSONDecodeError):
            raise ValueError("artifact authority is invalid") from None
        if not isinstance(value, dict):
            raise ValueError("artifact authority is invalid")
        return value

    def _expiry(self, value: object, *, maximum: timedelta) -> datetime:
        try:
            expires_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("artifact authority expiry is invalid") from None
        now = self._clock()
        if expires_at.tzinfo is None or expires_at <= now or expires_at > now + maximum:
            raise ValueError("artifact authority expiry is invalid")
        return expires_at

    def verify_executor(self, token: str) -> ExecutorArtifactAuthority:
        value = self._verify_payload(token, EXECUTOR_PREFIX)
        if set(value) != {
            "access",
            "artifact_id",
            "attempt",
            "expires_at",
            "fencing_token",
            "operation_id",
            "schema",
            "tenant_id",
            "worker_id",
        } or value.get("schema") != EXECUTOR_SCHEMA:
            raise ValueError("artifact executor authority fields differ")
        try:
            access = str(value["access"])
            if access not in {"read", "write"}:
                raise ValueError
            artifact_id = None if value["artifact_id"] is None else UUID(str(value["artifact_id"]))
            return ExecutorArtifactAuthority(
                operation_id=UUID(str(value["operation_id"])),
                tenant_id=str(value["tenant_id"]),
                worker_id=str(value["worker_id"]),
                fencing_token=int(value["fencing_token"]),
                attempt=int(value["attempt"]),
                access=access,  # type: ignore[arg-type]
                artifact_id=artifact_id,
                expires_at=self._expiry(value["expires_at"], maximum=timedelta(minutes=5)),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("artifact executor authority values are invalid") from None

    def verify_admission_input(self, token: str) -> AdmissionArtifactAuthority:
        value = self._verify_payload(token, ADMISSION_PREFIX)
        if set(value) != {
            "artifact_id",
            "consumer_operation_id",
            "digest",
            "issued_at",
            "object_version_id",
            "operation_authority_sha256",
            "producer_operation_id",
            "request_sha256",
            "schema",
            "tenant_id",
            "token_id",
        } or value.get("schema") != ADMISSION_SCHEMA:
            raise ValueError("artifact admission authority fields differ")
        try:
            issued_at = datetime.fromisoformat(str(value["issued_at"]).replace("Z", "+00:00"))
            object_version_id = str(value["object_version_id"])
            digest = str(value["digest"])
            operation_authority_sha256 = str(value["operation_authority_sha256"])
            request_sha256 = str(value["request_sha256"])
            tenant_id = str(value["tenant_id"])
            if (
                issued_at.tzinfo is None
                or issued_at > self._clock()
                or not tenant_id
                or not object_version_id
                or object_version_id in {"null", "unpersisted"}
                or len(digest) != 71
                or not digest.startswith("sha256:")
                or len(operation_authority_sha256) != 64
                or any(character not in "0123456789abcdef" for character in operation_authority_sha256)
                or len(request_sha256) != 64
                or any(character not in "0123456789abcdef" for character in request_sha256)
            ):
                raise ValueError
            return AdmissionArtifactAuthority(
                token_id=UUID(str(value["token_id"])),
                tenant_id=tenant_id,
                consumer_operation_id=UUID(str(value["consumer_operation_id"])),
                operation_authority_sha256=operation_authority_sha256,
                request_sha256=request_sha256,
                artifact_id=UUID(str(value["artifact_id"])),
                producer_operation_id=UUID(str(value["producer_operation_id"])),
                object_version_id=object_version_id,
                digest=digest,
                issued_at=issued_at,
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("artifact admission authority values are invalid") from None

    def verify_operation_admission(self, token: str) -> OperationAdmissionAuthority:
        value = self._verify_payload(token, OPERATION_ADMISSION_PREFIX)
        if set(value) != {
            "issued_at",
            "model_id",
            "operation",
            "operation_id",
            "protocol",
            "request_sha256",
            "required_scope",
            "schema",
            "tenant_id",
            "token_id",
        } or value.get("schema") != OPERATION_ADMISSION_SCHEMA:
            raise ValueError("operation admission authority fields differ")
        try:
            issued_at = datetime.fromisoformat(str(value["issued_at"]).replace("Z", "+00:00"))
            tenant_id = str(value["tenant_id"])
            model_id = str(value["model_id"])
            protocol = str(value["protocol"])
            operation = str(value["operation"])
            required_scope = str(value["required_scope"])
            request_sha256 = str(value["request_sha256"])
            if (
                issued_at.tzinfo is None
                or issued_at > self._clock()
                or not tenant_id
                or not model_id
                or not protocol
                or not operation
                or required_scope not in {"inference.invoke", "mcp.invoke"}
                or len(request_sha256) != 64
                or any(character not in "0123456789abcdef" for character in request_sha256)
            ):
                raise ValueError
            return OperationAdmissionAuthority(
                operation_id=UUID(str(value["operation_id"])),
                token_id=UUID(str(value["token_id"])),
                tenant_id=tenant_id,
                model_id=model_id,
                protocol=protocol,
                operation=operation,
                required_scope=required_scope,
                request_sha256=request_sha256,
                issued_at=issued_at,
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("operation admission authority values are invalid") from None

    def verify_controller(self, token: str) -> ControllerArtifactAuthority:
        value = self._verify_payload(token, CONTROLLER_PREFIX)
        if set(value) != {
            "artifact_id",
            "controller_id",
            "expires_at",
            "fencing_token",
            "operation_id",
            "schema",
            "tenant_id",
        } or value.get("schema") != CONTROLLER_SCHEMA:
            raise ValueError("artifact controller authority fields differ")
        try:
            controller_id = str(value["controller_id"])
            fencing_token = int(value["fencing_token"])
            if not controller_id or fencing_token < 1:
                raise ValueError
            return ControllerArtifactAuthority(
                operation_id=UUID(str(value["operation_id"])),
                tenant_id=str(value["tenant_id"]),
                controller_id=controller_id,
                fencing_token=fencing_token,
                artifact_id=UUID(str(value["artifact_id"])),
                expires_at=self._expiry(value["expires_at"], maximum=timedelta(minutes=5)),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("artifact controller authority values are invalid") from None

    def verify_operator(self, token: str) -> OperatorArtifactAuthority:
        value = self._verify_payload(token, OPERATOR_PREFIX)
        if set(value) != {
            "artifact_id",
            "expires_at",
            "operation_id",
            "principal_id",
            "role",
            "schema",
            "session_id",
            "tenant_id",
        } or value.get("schema") != OPERATOR_SCHEMA:
            raise ValueError("operator artifact authority fields differ")
        try:
            return OperatorArtifactAuthority(
                session_id=UUID(str(value["session_id"])),
                principal_id=UUID(str(value["principal_id"])),
                role=str(value["role"]),
                tenant_id=str(value["tenant_id"]),
                operation_id=UUID(str(value["operation_id"])),
                artifact_id=UUID(str(value["artifact_id"])),
                expires_at=self._expiry(value["expires_at"], maximum=timedelta(minutes=2)),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("operator artifact authority values are invalid") from None

    def verify_workload(self, token: str) -> WorkloadArtifactAuthority:
        value = self._verify_payload(token, WORKLOAD_PREFIX)
        if set(value) != {
            "access",
            "artifact_id",
            "attempt_id",
            "attempt_number",
            "expires_at",
            "operation_id",
            "schema",
            "tenant_id",
        } or value.get("schema") != WORKLOAD_SCHEMA:
            raise ValueError("artifact workload authority fields differ")
        try:
            access = str(value["access"])
            if access not in {"read", "write"}:
                raise ValueError
            return WorkloadArtifactAuthority(
                operation_id=UUID(str(value["operation_id"])),
                tenant_id=str(value["tenant_id"]),
                attempt_id=UUID(str(value["attempt_id"])),
                attempt_number=int(value["attempt_number"]),
                access=access,  # type: ignore[arg-type]
                artifact_id=(None if value["artifact_id"] is None else UUID(str(value["artifact_id"]))),
                expires_at=self._expiry(value["expires_at"], maximum=timedelta(minutes=5)),
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("artifact workload authority values are invalid") from None

    def verify_provider_observation(self, token: str) -> ProviderObservationAuthority:
        value = self._verify_payload(token, PROVIDER_OBSERVATION_PREFIX)
        if set(value) != {
            "action",
            "broker_pod_uid",
            "broker_service_account_subject",
            "compression",
            "credential_generation",
            "digest",
            "issued_at",
            "media_type",
            "object_version_id",
            "observed_at",
            "provider_access_key_id",
            "provider_binding_sha256",
            "schema",
            "size_bytes",
            "storage_key",
            "subject_id",
            "tenant_id",
        } or value.get("schema") != PROVIDER_OBSERVATION_SCHEMA:
            raise ValueError("provider observation fields differ")
        try:
            action = str(value["action"])
            if action not in {
                "backfill-observed",
                "orphan-version-observed",
                "orphan-absence-fenced",
            }:
                raise ValueError
            observed_at = datetime.fromisoformat(str(value["observed_at"]).replace("Z", "+00:00"))
            issued_at = datetime.fromisoformat(str(value["issued_at"]).replace("Z", "+00:00"))
            object_version_id = (
                None if value["object_version_id"] is None else str(value["object_version_id"])
            )
            digest = None if value["digest"] is None else str(value["digest"])
            size_bytes = None if value["size_bytes"] is None else int(value["size_bytes"])
            media_type = None if value["media_type"] is None else str(value["media_type"])
            compression = None if value["compression"] is None else str(value["compression"])
            credential_generation = int(value["credential_generation"])
            provider_access_key_id = str(value["provider_access_key_id"])
            broker_service_account_subject = str(value["broker_service_account_subject"])
            provider_binding_sha256 = str(value["provider_binding_sha256"])
            exact_version = action in {
                "backfill-observed",
                "orphan-version-observed",
                "orphan-absence-fenced",
            }
            measured = action in {"backfill-observed", "orphan-version-observed"}
            if (
                observed_at.tzinfo is None
                or issued_at.tzinfo is None
                or observed_at > issued_at
                or issued_at > self._clock()
                or credential_generation < 1
                or not provider_access_key_id
                or not broker_service_account_subject.startswith(
                    "system:serviceaccount:fs2-system:fs2-artifact-"
                )
                or len(provider_binding_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in provider_binding_sha256
                )
                or exact_version != (object_version_id is not None)
                or (object_version_id is not None and object_version_id in {"", "null", "unpersisted"})
                or measured != (digest is not None and size_bytes is not None and media_type is not None)
                or (digest is not None and (not digest.startswith("sha256:") or len(digest) != 71))
                or (size_bytes is not None and not 0 <= size_bytes <= 1 << 40)
                or compression not in {None, "gzip", "zstd"}
            ):
                raise ValueError
            return ProviderObservationAuthority(
                action=action,  # type: ignore[arg-type]
                subject_id=UUID(str(value["subject_id"])),
                tenant_id=str(value["tenant_id"]),
                credential_generation=credential_generation,
                provider_access_key_id=provider_access_key_id,
                broker_service_account_subject=broker_service_account_subject,
                broker_pod_uid=UUID(str(value["broker_pod_uid"])),
                provider_binding_sha256=provider_binding_sha256,
                storage_key=str(value["storage_key"]),
                object_version_id=object_version_id,
                digest=digest,
                size_bytes=size_bytes,
                media_type=media_type,
                compression=compression,
                observed_at=observed_at,
                issued_at=issued_at,
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("provider observation values are invalid") from None


@dataclass(frozen=True, slots=True)
class ArtifactAuthorityClientConfig:
    url: str
    audience: str
    token_file: Path
    ca_file: Path
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "fs2-artifact-authority.fs2-system.svc"
            or parsed.path.rstrip("/") != "/v1"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("artifact authority issuer must be the exact in-cluster service")
        if not self.audience or len(self.audience) > 253:
            raise ValueError("artifact authority issuer audience is invalid")
        if not 0 < self.timeout_seconds <= 30:
            raise ValueError("artifact authority issuer timeout is outside policy")


class ArtifactAuthorityClient:
    """Request bounded claims without possessing the signing key."""

    def __init__(self, config: ArtifactAuthorityClientConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            verify=str(config.ca_file),
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )

    def _identity(self) -> str:
        try:
            token = self._config.token_file.read_text(encoding="ascii").strip()
        except OSError as error:
            raise RuntimeError("artifact authority issuer identity is unavailable") from error
        if not token or len(token.encode()) > _MAX_TOKEN_BYTES or any(value.isspace() for value in token):
            raise RuntimeError("artifact authority issuer identity is invalid")
        return token

    async def _issue(self, suffix: str, payload: dict[str, Any]) -> str:
        try:
            response = await self._client.post(
                self._config.url.rstrip("/") + suffix,
                headers={"authorization": f"Bearer {self._identity()}"},
                json={"audience": self._config.audience, **payload},
            )
        except httpx.HTTPError as error:
            raise RuntimeError("artifact authority issuer is unavailable") from error
        if response.status_code != 200:
            raise RuntimeError("artifact authority issuer refused the scoped claim")
        try:
            token = response.json()["token"]
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("artifact authority issuer returned an invalid claim") from None
        if not isinstance(token, str) or not token:
            raise RuntimeError("artifact authority issuer returned an invalid claim")
        return token

    async def authorize_admission_inputs(
        self,
        *,
        operation_id: UUID,
        token_id: UUID,
        tenant_id: str,
        model_id: str,
        protocol: str,
        operation: str,
        required_scope: str,
        request_body: bytes,
        artifact_ids: tuple[UUID, ...],
        end_user_authorization: str,
    ) -> tuple[str, dict[UUID, str]]:
        """Obtain durable issuer receipts; the gateway never signs bindings."""

        if (
            not end_user_authorization.startswith("fs2_pat_")
            or len(end_user_authorization.encode("utf-8")) > _MAX_TOKEN_BYTES
            or any(character.isspace() for character in end_user_authorization)
        ):
            raise RuntimeError("artifact admission requires the exact request PAT")
        try:
            response = await self._client.post(
                self._config.url.rstrip("/") + "/admission-inputs",
                headers={
                    "authorization": f"Bearer {self._identity()}",
                    "x-fs2-end-user-authorization": f"Bearer {end_user_authorization}",
                },
                json={
                    "audience": self._config.audience,
                    "operation_id": str(operation_id),
                    "token_id": str(token_id),
                    "tenant_id": tenant_id,
                    "model_id": model_id,
                    "protocol": protocol,
                    "operation": operation,
                    "required_scope": required_scope,
                    "request_sha256": hashlib.sha256(request_body).hexdigest(),
                    "artifact_ids": [str(value) for value in artifact_ids],
                },
            )
        except httpx.HTTPError as error:
            raise RuntimeError("artifact admission authority is unavailable") from error
        if response.status_code != 200:
            raise RuntimeError("artifact admission authority refused the immutable inputs")
        try:
            document = response.json()
            operation_authority = str(document["operation_authority"])
            bindings = document["bindings"]
            result = {UUID(str(item["artifact_id"])): str(item["token"]) for item in bindings}
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("artifact admission authority returned invalid bindings") from None
        if (
            not operation_authority.startswith("fs2_operation_admission.")
            or set(result) != set(artifact_ids)
            or any(not value for value in result.values())
        ):
            raise RuntimeError("artifact admission authority returned incomplete bindings")
        return operation_authority, result

    async def issue_executor(
        self,
        operation: ClaimedOperation,
        *,
        access: ArtifactExecutorAccess,
        artifact_id: UUID | None = None,
    ) -> str:
        return await self._issue(
            "/executor",
            {
                "operation_id": str(operation.id),
                "tenant_id": operation.tenant_id,
                "worker_id": operation.worker_id,
                "fencing_token": operation.fencing_token,
                "attempt": operation.attempt,
                "access": access,
                "artifact_id": None if artifact_id is None else str(artifact_id),
            },
        )

    async def issue_operator(
        self,
        session: OperatorSession,
        *,
        session_secret: str,
        tenant_id: str,
        operation_id: UUID,
        artifact_id: UUID,
    ) -> str:
        session.principal.require(OperatorRole.VIEWER, tenant_id=tenant_id)
        return await self._issue(
            "/operator",
            {
                "session_id": str(session.id),
                "session_secret": session_secret,
                "principal_id": str(session.principal.id),
                "tenant_id": tenant_id,
                "operation_id": str(operation_id),
                "artifact_id": str(artifact_id),
            },
        )

    async def issue_controller_read(
        self,
        *,
        operation_id: UUID,
        tenant_id: str,
        artifact_id: UUID,
    ) -> str:
        return await self._issue(
            "/controller-read",
            {
                "operation_id": str(operation_id),
                "tenant_id": tenant_id,
                "artifact_id": str(artifact_id),
            },
        )

    async def issue_workload(
        self,
        *,
        operation_id: UUID,
        tenant_id: str,
        attempt_id: UUID,
        attempt_number: int,
        access: ArtifactExecutorAccess,
        artifact_id: UUID | None = None,
    ) -> str:
        return await self._issue(
            "/workload",
            {
                "operation_id": str(operation_id),
                "tenant_id": tenant_id,
                "attempt_id": str(attempt_id),
                "attempt_number": attempt_number,
                "access": access,
                "artifact_id": None if artifact_id is None else str(artifact_id),
            },
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


__all__ = [
    "AdmissionArtifactAuthority",
    "OperationAdmissionAuthority",
    "ArtifactAuthorityClient",
    "ArtifactAuthorityClientConfig",
    "ArtifactAuthoritySigner",
    "ArtifactAuthorityVerifier",
    "ControllerArtifactAuthority",
    "ExecutorArtifactAuthority",
    "OperatorArtifactAuthority",
    "ProviderObservationAuthority",
    "ProviderObservationAction",
    "WorkloadArtifactAuthority",
]
# Historical WIP residue retained as a comment for review provenance.  These
# fields belong to ProviderObservationAuthority's signed payload above; they
# are not executable statements after the module's __all__ declaration.
# "provider_access_key_id": provider_access_key_id,
# "provider_binding_sha256": provider_binding_sha256,
# "provider_access_key_id",
# "provider_binding_sha256",
