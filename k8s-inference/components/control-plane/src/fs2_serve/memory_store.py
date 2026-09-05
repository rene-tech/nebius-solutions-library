"""Lock-correct in-memory persistence used only by deterministic tests."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from .access_models import (
    BOOTSTRAP_OPERATOR_PRINCIPAL_ID,
    AdminApiKeyPolicyPatch,
    AdminKeyUsageRecord,
    OperatorPrincipal,
    OperatorPrincipalCreate,
    OperatorPrincipalPatch,
    OperatorRole,
    OperatorSession,
    OperatorSessionRecord,
    PrincipalKind,
)
from .admin_models import (
    AdminActivationPhase,
    AdminModelActivity,
    AdminOperationQuery,
    AdminOperationRecord,
    AdminUsageRow,
    AdminUsageWindow,
)
from .configuration import (
    TERRAFORM_BOOTSTRAP_ACTOR,
    configuration_etag,
    validate_terraform_apply_correlation,
)
from .configuration_models import (
    ConfigurationPlan,
    ConfigurationRevision,
    PlatformConfiguration,
    ReconciliationPhase,
    ReconciliationStatus,
    TerraformApplyReceipt,
)
from .crypto import Ciphertext, KeyedHasher, PayloadCipher
from .model_deployment import DesiredState, spec_digest
from .model_deployment_records import (
    ModelDeploymentAppendRequest,
    ModelDeploymentAppendResult,
    ModelDeploymentRevision,
    ModelDeploymentRevisionAction,
    ModelDeploymentStatusObservation,
    model_deployment_append_payload,
    model_deployment_audit_target,
    model_deployment_status_precedes,
)
from .models import (
    ActivationAction,
    ActivationIntent,
    ActivationIntentStatus,
    ActivationLeaderIdentity,
    ActivationTargetState,
    AdmissionRequest,
    AuditEvent,
    ClaimedActivationIntent,
    ClaimedOperation,
    DynamicAdmissionFence,
    ModalityUsage,
    OperationResult,
    OperationStatus,
    OperationView,
    PendingScientificAdmission,
    Principal,
    ReportedUsage,
    RuntimeIdentity,
    TerminalAccounting,
    TokenCreate,
    TokenView,
)
from .runtime import sanitize_error_detail
from .store import (
    BudgetExceededError,
    ConcurrencyExceededError,
    ConflictError,
    NotFoundError,
    RateLimitExceededError,
    StaleLeaseError,
)


@dataclass
class _Token:
    view: TokenView
    digest: str
    expiration_recorded: bool = False


@dataclass
class _Operation:
    view: OperationView
    request_hmac_key_id: str
    request_hmac: str
    request: Ciphertext | None
    content_type: str
    traceparent: str | None
    dispatch_snapshot: str | None = None
    response_hmac_key_id: str | None = None
    response_hmac: str | None = None
    response: Ciphertext | None = None
    worker_id: str | None = None
    lease_expires_at: datetime | None = None


@dataclass
class _Activation:
    view: ActivationIntent


@dataclass(frozen=True)
class _ActivationEvent:
    intent_id: UUID
    occurred_at: datetime
    event: str
    status: ActivationIntentStatus
    attempt: int
    fencing_token: int


class MemoryStore:
    """Deterministic fake; production construction always selects PostgreSQL."""

    def __init__(
        self,
        cipher: PayloadCipher,
        hasher: KeyedHasher,
        *,
        payload_ttl_seconds: int = 3600,
        auto_activate: bool = True,
    ) -> None:
        self.cipher = cipher
        self.hasher = hasher
        self.payload_ttl_seconds = payload_ttl_seconds
        # This implementation exists only for deterministic API/ledger tests.
        # Controller tests opt out and exercise real fenced intent transitions.
        self.auto_activate = auto_activate
        self._lock = asyncio.Lock()
        self._activation_mutation_locks: dict[str, asyncio.Lock] = {}
        self.tokens: dict[UUID, _Token] = {}
        self.operations: dict[UUID, _Operation] = {}
        self.scientific_admission_outbox: dict[UUID, PendingScientificAdmission] = {}
        self.scientific_admissions_completed: set[UUID] = set()
        self.activation_intents: dict[UUID, _Activation] = {}
        self.activation_events: list[_ActivationEvent] = []
        self.activation_targets: dict[str, ActivationTargetState] = {}
        self.activation_controller_id: str | None = None
        self.activation_controller_fencing_token = 0
        self.activation_controller_set_digest: str | None = None
        self.activation_controller_lease_expires_at: datetime | None = None
        self.activation_controller_identity: ActivationLeaderIdentity | None = None
        self.activation_model_fences: dict[str, int] = {}
        self.idempotency: dict[tuple[str, str, UUID, str], UUID] = {}
        self.audit: list[AuditEvent] = []
        now = datetime.now(UTC)
        self.operator_principals: dict[UUID, OperatorPrincipal] = {
            BOOTSTRAP_OPERATOR_PRINCIPAL_ID: OperatorPrincipal(
                id=BOOTSTRAP_OPERATOR_PRINCIPAL_ID,
                subject="bootstrap-admin",
                display_name="Bootstrap administrator",
                kind=PrincipalKind.SERVICE,
                role=OperatorRole.ADMIN,
                tenant_id=None,
                enabled=True,
                created_at=now,
                created_by="schema-migration",
                updated_at=now,
            )
        }
        self.operator_sessions: dict[UUID, OperatorSessionRecord] = {}
        self.configuration_revisions: dict[int, ConfigurationRevision] = {}
        self.configuration_plans: dict[UUID, ConfigurationPlan] = {}
        self.configuration_status_events: dict[UUID, list[ReconciliationStatus]] = {}
        self.model_deployment_revisions: dict[tuple[str, str], list[ModelDeploymentRevision]] = {}
        self.model_deployment_idempotency: dict[tuple[UUID, str, str], tuple[str, str, str, int]] = {}
        self.model_deployment_status_events: dict[tuple[str, str], list[ModelDeploymentStatusObservation]] = {}
        self.model_deployment_status_by_id: dict[UUID, ModelDeploymentStatusObservation] = {}

    def _activation_event(self, intent: ActivationIntent, event: str) -> None:
        self.activation_events.append(
            _ActivationEvent(
                intent_id=intent.id,
                occurred_at=datetime.now(UTC),
                event=event,
                status=intent.status,
                attempt=intent.attempt,
                fencing_token=intent.fencing_token,
            )
        )

    def _activation_mutation_lock(self, model_id: str) -> asyncio.Lock:
        return self._activation_mutation_locks.setdefault(model_id, asyncio.Lock())

    def _aad(self, row: _Operation, direction: str) -> bytes:
        return self.cipher.aad(row.view.id, row.view.tenant_id, row.view.model_id, direction)

    @staticmethod
    def _metadata(row: _Operation, *, reused: bool | None = None) -> OperationView:
        available = (
            row.response is not None
            and row.view.payload_expires_at is not None
            and row.view.payload_expires_at > datetime.now(UTC)
        )
        updates: dict[str, Any] = {
            "result_available": available,
            "traceparent": row.traceparent,
        }
        if reused is not None:
            updates["reused"] = reused
        return row.view.model_copy(update=updates, deep=True)

    def _audit(
        self,
        *,
        actor: str,
        tenant_id: str | None,
        token_id: UUID | None,
        action: str,
        target_type: str,
        target_id: str,
        outcome: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.audit.append(
            AuditEvent(
                id=len(self.audit) + 1,
                occurred_at=datetime.now(UTC),
                actor=actor,
                tenant_id=tenant_id,
                token_id=token_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                outcome=outcome,
                detail=detail or {},
            )
        )

    @staticmethod
    def _active(status: OperationStatus) -> bool:
        return status in {OperationStatus.QUEUED, OperationStatus.ACTIVATING, OperationStatus.RUNNING}

    def _release_reservation(self, row: _Operation) -> None:
        if row.view.reserved_gpu_seconds <= 0:
            return
        token = self.tokens.get(row.view.token_id)
        if token is not None:
            token.view = token.view.model_copy(
                update={"gpu_seconds_reserved": max(0, token.view.gpu_seconds_reserved - row.view.reserved_gpu_seconds)}
            )
        row.view = row.view.model_copy(update={"reserved_gpu_seconds": 0.0})

    def _expire(self, row: _Operation, now: datetime, code: str) -> None:
        self._release_reservation(row)
        row.worker_id = None
        row.lease_expires_at = None
        row.view = row.view.model_copy(
            update={
                "status": OperationStatus.EXPIRED,
                "completed_at": now,
                "outcome": "expired",
                "error_code": code,
                "error_detail": None,
                "fencing_token": row.view.fencing_token + 1,
            }
        )

    async def close(self) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def database_clock(self) -> datetime:
        return datetime.now(UTC)

    async def activation_controller_ready(self, activation_set_digest: str) -> bool:
        async with self._lock:
            return bool(
                self.activation_controller_id is not None
                and self.activation_controller_set_digest == activation_set_digest
                and self.activation_controller_lease_expires_at is not None
                and self.activation_controller_lease_expires_at > datetime.now(UTC)
            )

    async def publish_activation_controller_heartbeat(
        self,
        identity: ActivationLeaderIdentity,
        *,
        activation_set_digest: str,
        lease_expires_at: datetime,
        expected_fencing_token: int | None,
    ) -> int:
        identity.validate_binding()
        if (
            len(activation_set_digest) != 64
            or any(character not in "0123456789abcdef" for character in activation_set_digest)
            or lease_expires_at.tzinfo is None
            or lease_expires_at.utcoffset() is None
        ):
            raise ValueError("activation controller heartbeat is outside the closed bound")
        async with self._lock:
            now = datetime.now(UTC)
            if lease_expires_at <= now:
                raise StaleLeaseError("observed Kubernetes Lease already expired")
            if (lease_expires_at - now).total_seconds() > 60:
                raise ValueError("activation controller heartbeat is outside the closed bound")
            if expected_fencing_token != (self.activation_controller_fencing_token or None):
                raise StaleLeaseError("activation leadership fence is stale")
            previous = self.activation_controller_identity
            same_owner = bool(
                previous is not None
                and previous.controller_id == identity.controller_id
                and previous.pod_uid == identity.pod_uid
                and previous.service_account_uid == identity.service_account_uid
                and previous.lease_uid == identity.lease_uid
            )
            if (
                previous is not None
                and previous.lease_uid == identity.lease_uid
                and int(identity.lease_resource_version) <= int(previous.lease_resource_version)
            ):
                raise StaleLeaseError("observed Kubernetes Lease resourceVersion is stale")
            if (
                previous is not None
                and not same_owner
                and self.activation_controller_lease_expires_at is not None
                and self.activation_controller_lease_expires_at > datetime.now(UTC)
            ):
                raise StaleLeaseError("prior activation leader lease is still live")
            if not same_owner:
                self.activation_controller_fencing_token += 1
            self.activation_controller_id = identity.controller_id
            self.activation_controller_identity = identity
            self.activation_controller_set_digest = activation_set_digest
            self.activation_controller_lease_expires_at = lease_expires_at
            return self.activation_controller_fencing_token

    async def current_activation_controller_fence(self) -> int | None:
        async with self._lock:
            return self.activation_controller_fencing_token or None

    async def clear_activation_controller_heartbeat(
        self, identity: ActivationLeaderIdentity, *, leadership_fencing_token: int
    ) -> None:
        async with self._lock:
            if (
                self.activation_controller_identity == identity
                and self.activation_controller_fencing_token == leadership_fencing_token
            ):
                self.activation_controller_lease_expires_at = datetime.now(UTC)

    def _require_current_activation_leader(
        self, identity: ActivationLeaderIdentity, leadership_fencing_token: int
    ) -> None:
        if (
            self.activation_controller_identity is None
            or self.activation_controller_identity.controller_id != identity.controller_id
            or self.activation_controller_identity.pod_namespace != identity.pod_namespace
            or self.activation_controller_identity.pod_name != identity.pod_name
            or self.activation_controller_identity.pod_uid != identity.pod_uid
            or self.activation_controller_identity.service_account_name != identity.service_account_name
            or self.activation_controller_identity.service_account_uid != identity.service_account_uid
            or self.activation_controller_identity.lease_namespace != identity.lease_namespace
            or self.activation_controller_identity.lease_name != identity.lease_name
            or self.activation_controller_identity.lease_uid != identity.lease_uid
            or self.activation_controller_identity.lease_holder_identity != identity.lease_holder_identity
            or int(self.activation_controller_identity.lease_resource_version) < int(identity.lease_resource_version)
            or self.activation_controller_fencing_token != leadership_fencing_token
            or self.activation_controller_lease_expires_at is None
            or self.activation_controller_lease_expires_at <= datetime.now(UTC)
        ):
            raise StaleLeaseError("activation leadership identity or fence is stale")

    async def migrate(self) -> None:
        return None

    async def issue_token(
        self,
        *,
        token_id: UUID,
        prefix: str,
        pepper_key_id: str,
        digest: str,
        request: TokenCreate,
        created_by: str,
        fingerprint: str | None = None,
    ) -> TokenView:
        async with self._lock:
            if token_id in self.tokens or (
                fingerprint is not None and any(row.view.fingerprint == fingerprint for row in self.tokens.values())
            ):
                raise ConflictError("token already exists")
            view = TokenView(
                id=token_id,
                prefix=prefix,
                pepper_key_id=pepper_key_id,
                principal_id=request.principal_id,
                tenant_id=request.tenant_id,
                scopes=sorted(str(scope) for scope in request.scopes),
                models=sorted(request.models),
                expires_at=request.expires_at,
                request_budget=request.request_budget,
                requests_used=0,
                gpu_seconds_budget=request.gpu_seconds_budget,
                gpu_seconds_used=0,
                gpu_seconds_reserved=0,
                max_concurrency=request.max_concurrency,
                created_at=datetime.now(UTC),
                created_by=created_by,
                revoked_at=None,
                name=request.name,
                fingerprint=fingerprint,
                rate_limit_requests=request.rate_limit_requests,
                rate_window_seconds=request.rate_window_seconds,
            )
            self.tokens[token_id] = _Token(view, digest)
            self._audit(
                actor=created_by,
                tenant_id=request.tenant_id,
                token_id=token_id,
                action="token.issue",
                target_type="token",
                target_id=str(token_id),
                outcome="succeeded",
                detail={"prefix": prefix, "scopes": view.scopes, "models": view.models},
            )
            return view.model_copy(deep=True)

    async def token_for_verification(self, token_id: UUID) -> tuple[TokenView, str] | None:
        async with self._lock:
            row = self.tokens.get(token_id)
            return (row.view.model_copy(deep=True), row.digest) if row else None

    async def get_token(self, token_id: UUID) -> TokenView:
        async with self._lock:
            row = self.tokens.get(token_id)
            if row is None:
                raise NotFoundError("token not found")
            return row.view.model_copy(deep=True)

    async def rehash_token(self, token_id: UUID, *, pepper_key_id: str, digest: str) -> None:
        async with self._lock:
            row = self.tokens.get(token_id)
            if row is None or row.view.revoked_at is not None:
                return
            row.digest = digest
            row.view = row.view.model_copy(update={"pepper_key_id": pepper_key_id})

    async def list_tokens(self, *, tenant_id: str | None = None, limit: int = 200) -> list[TokenView]:
        if not 1 <= limit <= 1000:
            raise ValueError("token list limit is outside the bound")
        async with self._lock:
            rows = [
                row.view.model_copy(deep=True)
                for row in self.tokens.values()
                if tenant_id is None or row.view.tenant_id == tenant_id
            ]
            rows.sort(key=lambda item: (item.created_at, item.id.int), reverse=True)
            return rows[:limit]

    async def record_token_expired(self, token_id: UUID, *, actor: str) -> None:
        async with self._lock:
            row = self.tokens.get(token_id)
            if row is None or row.view.expires_at is None:
                return
            if row.expiration_recorded:
                return
            row.expiration_recorded = True
            self._audit(
                actor=actor,
                tenant_id=row.view.tenant_id,
                token_id=token_id,
                action="token.expire",
                target_type="token",
                target_id=str(token_id),
                outcome="succeeded",
            )

    async def rotate_token(
        self,
        predecessor_id: UUID,
        *,
        token_id: UUID,
        prefix: str,
        pepper_key_id: str,
        digest: str,
        fingerprint: str,
        name: str | None,
        expires_at: datetime | None,
        actor: str,
    ) -> TokenView:
        async with self._lock:
            predecessor = self.tokens.get(predecessor_id)
            if predecessor is None:
                raise NotFoundError("token not found")
            if predecessor.view.revoked_at is not None:
                raise ConflictError("token is already inactive")
            now = datetime.now(UTC)
            if predecessor.view.expires_at is not None and predecessor.view.expires_at <= now:
                raise ConflictError("token is already inactive")
            if token_id in self.tokens or any(row.view.fingerprint == fingerprint for row in self.tokens.values()):
                raise ConflictError("token already exists")
            if expires_at is not None and expires_at <= now:
                raise ValueError("expires_at must be in the future")
            successor = predecessor.view.model_copy(
                update={
                    "id": token_id,
                    "prefix": prefix,
                    "pepper_key_id": pepper_key_id,
                    "name": name or predecessor.view.name,
                    "fingerprint": fingerprint,
                    "expires_at": expires_at if expires_at is not None else predecessor.view.expires_at,
                    "gpu_seconds_reserved": 0.0,
                    "created_at": now,
                    "created_by": actor,
                    "revoked_at": None,
                    "rotation_parent_id": predecessor_id,
                    "rotated_at": None,
                    "last_used_at": None,
                }
            )
            predecessor.view = predecessor.view.model_copy(update={"revoked_at": now, "rotated_at": now})
            released = 0.0
            for operation in self.operations.values():
                if operation.view.token_id != predecessor_id or not self._active(operation.view.status):
                    continue
                released += operation.view.reserved_gpu_seconds
                operation.view = operation.view.model_copy(
                    update={
                        "status": OperationStatus.CANCELLED,
                        "completed_at": now,
                        "outcome": "token_rotated",
                        "error_code": "token_rotated",
                        "error_detail": None,
                        "fencing_token": operation.view.fencing_token + 1,
                        "reserved_gpu_seconds": 0.0,
                    }
                )
                operation.worker_id = None
                operation.lease_expires_at = None
            predecessor.view = predecessor.view.model_copy(
                update={"gpu_seconds_reserved": max(0.0, predecessor.view.gpu_seconds_reserved - released)}
            )
            self.tokens[token_id] = _Token(successor, digest)
            self._audit(
                actor=actor,
                tenant_id=successor.tenant_id,
                token_id=token_id,
                action="token.rotate",
                target_type="token",
                target_id=str(token_id),
                outcome="succeeded",
                detail={"predecessor_id": str(predecessor_id), "prefix": prefix},
            )
            return successor.model_copy(deep=True)

    async def revoke_token(self, token_id: UUID, *, actor: str) -> TokenView:
        async with self._lock:
            row = self.tokens.get(token_id)
            if row is None:
                raise NotFoundError("token not found")
            if row.view.revoked_at is None:
                row.view = row.view.model_copy(update={"revoked_at": datetime.now(UTC)})
            for operation in self.operations.values():
                if operation.view.token_id != token_id or not self._active(operation.view.status):
                    continue
                self._release_reservation(operation)
                operation.view = operation.view.model_copy(
                    update={
                        "status": OperationStatus.CANCELLED,
                        "completed_at": datetime.now(UTC),
                        "outcome": "token_revoked",
                        "error_code": "token_revoked",
                        "error_detail": None,
                        "fencing_token": operation.view.fencing_token + 1,
                    }
                )
                operation.worker_id = None
                operation.lease_expires_at = None
            self._audit(
                actor=actor,
                tenant_id=row.view.tenant_id,
                token_id=token_id,
                action="token.revoke",
                target_type="token",
                target_id=str(token_id),
                outcome="succeeded",
            )
            return row.view.model_copy(deep=True)

    async def update_token_policy(
        self,
        token_id: UUID,
        *,
        request: AdminApiKeyPolicyPatch,
        actor: str,
    ) -> TokenView:
        async with self._lock:
            row = self.tokens.get(token_id)
            if row is None:
                raise NotFoundError("token not found")
            now = datetime.now(UTC)
            if row.view.revoked_at is not None or (row.view.expires_at is not None and row.view.expires_at <= now):
                raise ConflictError("token is already inactive")
            fields = request.model_fields_set
            if "expires_at" in fields and request.expires_at is not None and request.expires_at <= now:
                raise ValueError("expires_at must be in the future")
            if (
                "request_budget" in fields
                and request.request_budget is not None
                and request.request_budget < row.view.requests_used
            ):
                raise ConflictError("request budget is below durable usage")
            if (
                "gpu_seconds_budget" in fields
                and request.gpu_seconds_budget is not None
                and request.gpu_seconds_budget < row.view.gpu_seconds_used + row.view.gpu_seconds_reserved
            ):
                raise ConflictError("GPU budget is below durable usage and reservations")
            update: dict[str, Any] = {}
            for name in (
                "name",
                "expires_at",
                "request_budget",
                "gpu_seconds_budget",
                "max_concurrency",
                "rate_limit_requests",
                "rate_window_seconds",
            ):
                if name in fields:
                    update[name] = getattr(request, name)
            if "scopes" in fields:
                assert request.scopes is not None
                update["scopes"] = sorted(str(scope) for scope in request.scopes)
            if "models" in fields:
                assert request.models is not None
                update["models"] = sorted(request.models)
            if "rate_limit_requests" in fields:
                update["rate_window_started_at"] = None
                update["rate_window_requests"] = 0
            row.view = row.view.model_copy(update=update)
            self._audit(
                actor=actor,
                tenant_id=row.view.tenant_id,
                token_id=token_id,
                action="token.policy.update",
                target_type="token",
                target_id=str(token_id),
                outcome="succeeded",
                detail={f"{name}_changed": name in fields for name in sorted(fields)},
            )
            return row.view.model_copy(deep=True)

    async def list_operator_principals(
        self, *, tenant_id: str | None, include_global: bool, limit: int
    ) -> list[OperatorPrincipal]:
        if not 1 <= limit <= 1000:
            raise ValueError("principal list limit is outside the bound")
        async with self._lock:
            rows = [
                principal.model_copy(deep=True)
                for principal in self.operator_principals.values()
                if (tenant_id is None and include_global)
                or principal.tenant_id == tenant_id
                or (include_global and principal.tenant_id is None)
            ]
            rows.sort(key=lambda item: (item.created_at, item.id.int), reverse=True)
            return rows[:limit]

    async def get_operator_principal(self, principal_id: UUID) -> OperatorPrincipal:
        async with self._lock:
            principal = self.operator_principals.get(principal_id)
            if principal is None:
                raise NotFoundError("operator principal not found")
            return principal.model_copy(deep=True)

    async def create_operator_principal(
        self, *, principal_id: UUID, request: OperatorPrincipalCreate, actor: str
    ) -> OperatorPrincipal:
        async with self._lock:
            if principal_id in self.operator_principals or any(
                item.subject == request.subject and item.tenant_id == request.tenant_id
                for item in self.operator_principals.values()
            ):
                raise ConflictError("operator principal already exists")
            now = datetime.now(UTC)
            principal = OperatorPrincipal(
                id=principal_id,
                subject=request.subject,
                display_name=request.display_name,
                kind=request.kind,
                role=request.role,
                tenant_id=request.tenant_id,
                enabled=True,
                created_at=now,
                created_by=actor,
                updated_at=now,
            )
            self.operator_principals[principal_id] = principal
            self._audit(
                actor=actor,
                tenant_id=principal.tenant_id,
                token_id=None,
                action="principal.create",
                target_type="operator_principal",
                target_id=str(principal_id),
                outcome="succeeded",
                detail={"kind": str(principal.kind), "role": str(principal.role)},
            )
            return principal.model_copy(deep=True)

    async def update_operator_principal(
        self, principal_id: UUID, *, request: OperatorPrincipalPatch, actor: str
    ) -> OperatorPrincipal:
        async with self._lock:
            principal = self.operator_principals.get(principal_id)
            if principal is None:
                raise NotFoundError("operator principal not found")
            changes: dict[str, Any] = {"updated_at": datetime.now(UTC)}
            detail: dict[str, Any] = {}
            if request.display_name is not None:
                changes["display_name"] = request.display_name
                detail["display_name_changed"] = True
            if request.role is not None:
                changes["role"] = request.role
                detail["role"] = str(request.role)
            if request.enabled is not None:
                changes["enabled"] = request.enabled
                changes["disabled_at"] = None if request.enabled else datetime.now(UTC)
                detail["enabled"] = request.enabled
            principal = principal.model_copy(update=changes)
            self.operator_principals[principal_id] = principal
            if not principal.enabled:
                now = datetime.now(UTC)
                for session_id, record in tuple(self.operator_sessions.items()):
                    if record.session.principal.id != principal_id or record.session.revoked_at is not None:
                        continue
                    session = record.session.model_copy(update={"principal": principal, "revoked_at": now})
                    self.operator_sessions[session_id] = record.model_copy(update={"session": session})
            self._audit(
                actor=actor,
                tenant_id=principal.tenant_id,
                token_id=None,
                action="principal.update",
                target_type="operator_principal",
                target_id=str(principal_id),
                outcome="succeeded",
                detail=detail,
            )
            return principal.model_copy(deep=True)

    async def create_operator_session(
        self,
        *,
        session_id: UUID,
        principal_id: UUID,
        pepper_key_id: str,
        digest: str,
        expires_at: datetime,
        actor: str,
    ) -> OperatorSession:
        async with self._lock:
            principal = self.operator_principals.get(principal_id)
            now = datetime.now(UTC)
            if principal is None or not principal.enabled:
                raise NotFoundError("operator principal not found")
            if session_id in self.operator_sessions:
                raise ConflictError("operator session already exists")
            if expires_at <= now:
                raise ValueError("operator session expiry must be in the future")
            session = OperatorSession(
                id=session_id,
                principal=principal,
                created_at=now,
                expires_at=expires_at,
                last_seen_at=now,
            )
            self.operator_sessions[session_id] = OperatorSessionRecord(
                session=session,
                pepper_key_id=pepper_key_id,
                digest=digest,
            )
            self._audit(
                actor=actor,
                tenant_id=principal.tenant_id,
                token_id=None,
                action="session.issue",
                target_type="operator_session",
                target_id=str(session_id),
                outcome="succeeded",
                detail={"principal_id": str(principal_id)},
            )
            return session.model_copy(deep=True)

    async def replace_operator_session(
        self,
        *,
        prior_session_id: UUID | None,
        prior_digest: str | None,
        session_id: UUID,
        principal_id: UUID,
        pepper_key_id: str,
        digest: str,
        expires_at: datetime,
        actor: str,
    ) -> OperatorSession:
        if (prior_session_id is None) != (prior_digest is None):
            raise ValueError("prior operator session verifier is incomplete")
        async with self._lock:
            principal = self.operator_principals.get(principal_id)
            now = datetime.now(UTC)
            if principal is None or not principal.enabled:
                raise NotFoundError("operator principal not found")
            if session_id in self.operator_sessions:
                raise ConflictError("operator session already exists")
            if expires_at <= now:
                raise ValueError("operator session expiry must be in the future")
            prior = self.operator_sessions.get(prior_session_id) if prior_session_id is not None else None
            if (
                prior is not None
                and prior_digest is not None
                and secrets.compare_digest(prior.digest, prior_digest)
                and prior.session.revoked_at is None
            ):
                revoked = prior.session.model_copy(update={"revoked_at": now})
                self.operator_sessions[prior.session.id] = prior.model_copy(update={"session": revoked})
                self._audit(
                    actor=actor,
                    tenant_id=prior.session.principal.tenant_id,
                    token_id=None,
                    action="session.revoke",
                    target_type="operator_session",
                    target_id=str(prior.session.id),
                    outcome="succeeded",
                    detail={"reason": "replacement"},
                )
            session = OperatorSession(
                id=session_id,
                principal=principal,
                created_at=now,
                expires_at=expires_at,
                last_seen_at=now,
            )
            self.operator_sessions[session_id] = OperatorSessionRecord(
                session=session,
                pepper_key_id=pepper_key_id,
                digest=digest,
            )
            self._audit(
                actor=actor,
                tenant_id=principal.tenant_id,
                token_id=None,
                action="session.issue",
                target_type="operator_session",
                target_id=str(session_id),
                outcome="succeeded",
                detail={"principal_id": str(principal_id), "replacement": prior is not None},
            )
            return session.model_copy(deep=True)

    async def operator_session_for_verification(self, session_id: UUID) -> OperatorSessionRecord | None:
        async with self._lock:
            record = self.operator_sessions.get(session_id)
            if record is None:
                return None
            principal = self.operator_principals.get(record.session.principal.id)
            if principal is None:
                return None
            return record.model_copy(
                update={"session": record.session.model_copy(update={"principal": principal})},
                deep=True,
            )

    async def touch_operator_session(self, session_id: UUID, *, seen_at: datetime) -> None:
        if seen_at.tzinfo is None:
            raise ValueError("session use timestamp must be timezone-aware")
        async with self._lock:
            record = self.operator_sessions.get(session_id)
            if record is None or record.session.revoked_at is not None:
                return
            session = record.session.model_copy(update={"last_seen_at": seen_at})
            self.operator_sessions[session_id] = record.model_copy(update={"session": session})

    async def revoke_operator_session(self, session_id: UUID, *, actor: str) -> OperatorSession:
        async with self._lock:
            record = self.operator_sessions.get(session_id)
            if record is None:
                raise NotFoundError("operator session not found")
            session = record.session
            if session.revoked_at is None:
                session = session.model_copy(update={"revoked_at": datetime.now(UTC)})
                self.operator_sessions[session_id] = record.model_copy(update={"session": session})
            self._audit(
                actor=actor,
                tenant_id=session.principal.tenant_id,
                token_id=None,
                action="session.revoke",
                target_type="operator_session",
                target_id=str(session_id),
                outcome="succeeded",
            )
            return session.model_copy(deep=True)

    async def append_audit_event(
        self,
        *,
        actor: str,
        tenant_id: str | None,
        token_id: UUID | None,
        action: str,
        target_type: str,
        target_id: str,
        outcome: str,
        detail: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        if not all(1 <= len(value) <= 200 for value in (actor, action, target_type, target_id, outcome)):
            raise ValueError("audit identity is outside the bound")
        async with self._lock:
            self._audit(
                actor=actor,
                tenant_id=tenant_id,
                token_id=token_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                outcome=outcome,
                detail=dict(detail or {}),
            )

    async def configuration_current(self) -> ConfigurationRevision | None:
        async with self._lock:
            if not self.configuration_revisions:
                return None
            return self.configuration_revisions[max(self.configuration_revisions)].model_copy(deep=True)

    async def configuration_get_revision(self, revision: int) -> ConfigurationRevision | None:
        async with self._lock:
            value = self.configuration_revisions.get(revision)
            return value.model_copy(deep=True) if value is not None else None

    async def configuration_ensure_initial(
        self,
        configuration: PlatformConfiguration,
        *,
        actor: str,
    ) -> ConfigurationRevision:
        if actor != TERRAFORM_BOOTSTRAP_ACTOR:
            raise ValueError("initial configuration requires the Terraform-rendered baseline actor")
        async with self._lock:
            if self.configuration_revisions:
                current = self.configuration_revisions[max(self.configuration_revisions)]
                if current.etag == configuration_etag(configuration):
                    return current.model_copy(deep=True)
                raise ConflictError("changed configuration requires a correlated Terraform apply receipt")
            value = ConfigurationRevision(
                revision=1,
                etag=configuration_etag(configuration),
                desired=configuration.model_copy(deep=True),
                effective=configuration.model_copy(deep=True),
                created_at=datetime.now(UTC),
                created_by=actor,
            )
            self.configuration_revisions[1] = value
            self._audit(
                actor=actor,
                tenant_id=None,
                token_id=None,
                action="configuration.bootstrap",
                target_type="platform_configuration",
                target_id=value.etag,
                outcome="succeeded",
                detail={"revision": value.revision},
            )
            return value.model_copy(deep=True)

    async def configuration_adopt_terraform_baseline(
        self,
        configuration: PlatformConfiguration,
        *,
        actor: str,
    ) -> ConfigurationRevision:
        """Append the mounted Terraform baseline when its desired state changed."""

        if not 1 <= len(actor) <= 200:
            raise ValueError("configuration actor is outside the bound")
        etag = configuration_etag(configuration)
        async with self._lock:
            current = (
                self.configuration_revisions[max(self.configuration_revisions)]
                if self.configuration_revisions
                else None
            )
            if current is not None and current.etag == etag:
                return current.model_copy(deep=True)
            revision = 1 if current is None else current.revision + 1
            value = ConfigurationRevision(
                revision=revision,
                etag=etag,
                desired=configuration.model_copy(deep=True),
                effective=configuration.model_copy(deep=True),
                created_at=datetime.now(UTC),
                created_by=actor,
                previous_revision=current.revision if current is not None else None,
            )
            self.configuration_revisions[revision] = value
            return value.model_copy(deep=True)

    async def configuration_accept_terraform_applied(
        self,
        configuration: PlatformConfiguration,
        receipt: TerraformApplyReceipt,
        *,
        actor: str,
    ) -> ConfigurationRevision:
        if not 1 <= len(actor) <= 200:
            raise ValueError("configuration actor is outside the bound")
        async with self._lock:
            if not self.configuration_revisions:
                raise ConflictError("configuration is not initialized")
            current = self.configuration_revisions[max(self.configuration_revisions)]
            plan = self.configuration_plans.get(receipt.plan_id)
            events = self.configuration_status_events.get(receipt.reconciliation_id, [])
            if plan is None or not events:
                raise ConflictError("Terraform apply receipt has no durable plan and awaiting event")
            status = events[-1]
            try:
                action = validate_terraform_apply_correlation(
                    current=current,
                    plan=plan,
                    status=status,
                    configuration=configuration,
                    receipt=receipt,
                )
            except ValueError as exc:
                raise ConflictError(str(exc)) from None
            if action == "replay":
                return current.model_copy(deep=True)
            value = ConfigurationRevision(
                revision=current.revision + 1,
                etag=configuration_etag(configuration),
                desired=configuration.model_copy(deep=True),
                effective=configuration.model_copy(deep=True),
                created_at=datetime.now(UTC),
                created_by=actor,
                previous_revision=current.revision,
                reconciliation_id=receipt.reconciliation_id,
            )
            succeeded = status.model_copy(
                update={
                    "phase": ReconciliationPhase.SUCCEEDED,
                    "applied_revision": value.revision,
                    "completed_at": datetime.now(UTC),
                }
            )
            self.configuration_revisions[value.revision] = value
            events.append(succeeded)
            self._audit(
                actor=actor,
                tenant_id=None,
                token_id=None,
                action="configuration.terraform-applied",
                target_type="platform_configuration",
                target_id=value.etag,
                outcome="succeeded",
                detail={
                    "revision": value.revision,
                    "previous_revision": current.revision,
                    "plan_id": str(receipt.plan_id),
                    "reconciliation_id": str(receipt.reconciliation_id),
                },
            )
            return value.model_copy(deep=True)

    async def configuration_save_plan(self, plan: ConfigurationPlan) -> None:
        async with self._lock:
            existing = self.configuration_plans.get(plan.plan_id)
            if existing is not None and existing != plan:
                raise ConflictError("configuration plan identity was reused")
            self.configuration_plans[plan.plan_id] = plan.model_copy(deep=True)

    async def configuration_get_plan(self, plan_id: UUID) -> ConfigurationPlan | None:
        async with self._lock:
            value = self.configuration_plans.get(plan_id)
            return value.model_copy(deep=True) if value is not None else None

    async def configuration_save_status(self, status: ReconciliationStatus) -> None:
        async with self._lock:
            events = self.configuration_status_events.setdefault(status.reconciliation_id, [])
            same_phase = [item for item in events if item.phase == status.phase]
            if same_phase and same_phase[-1] != status:
                raise ConflictError("configuration reconciliation phase was rewritten")
            if not same_phase:
                events.append(status.model_copy(deep=True))

    async def configuration_get_status(self, reconciliation_id: UUID) -> ReconciliationStatus | None:
        async with self._lock:
            events = self.configuration_status_events.get(reconciliation_id, [])
            return events[-1].model_copy(deep=True) if events else None

    async def model_deployment_append_revision(
        self,
        request: ModelDeploymentAppendRequest,
    ) -> ModelDeploymentAppendResult:
        key_value = request.idempotency_key.encode("utf-8")
        request_value = model_deployment_append_payload(request)
        candidates = self.hasher.candidate_digests(
            key_value,
            context="fs2-serve.model-deployment-idempotency/v1",
        )
        async with self._lock:
            for key_id, key_hmac in candidates:
                receipt = self.model_deployment_idempotency.get((request.actor_id, key_id, key_hmac))
                if receipt is None:
                    continue
                stored_request_hmac, namespace, name, revision = receipt
                replay_hmac = self.hasher.digest_for(
                    key_id,
                    request_value,
                    context="fs2-serve.model-deployment-request/v1",
                )
                if not secrets.compare_digest(replay_hmac, stored_request_hmac):
                    raise ConflictError("model deployment idempotency key is bound to another request")
                rows = self.model_deployment_revisions.get((namespace, name), [])
                value = next((item for item in rows if item.revision == revision), None)
                if value is None:
                    raise ConflictError("model deployment idempotency receipt is incomplete")
                return ModelDeploymentAppendResult(value=value.model_copy(deep=True), reused=True)

            rows = self.model_deployment_revisions.setdefault((request.namespace, request.name), [])
            current = rows[-1] if rows else None
            if current is None:
                if request.action is not ModelDeploymentRevisionAction.CREATE or request.expected_etag is not None:
                    raise ConflictError("model deployment create does not match current state")
                revision_number = 1
                previous_revision = None
            else:
                if request.action is ModelDeploymentRevisionAction.CREATE:
                    raise ConflictError("model deployment already exists")
                if request.expected_etag != current.etag:
                    raise ConflictError("model deployment ETag is stale")
                if (
                    request.spec.tenant_id != current.tenant_id
                    or request.spec.model_ref != current.spec.model_ref
                    or request.spec.runtime.profile != current.spec.runtime.profile
                ):
                    raise ConflictError("model deployment immutable identity changed")
                if spec_digest(request.spec) == current.etag:
                    raise ConflictError("model deployment revision contains no desired-state change")
                revision_number = current.revision + 1
                previous_revision = current.revision

            now = datetime.now(UTC)
            value = ModelDeploymentRevision(
                namespace=request.namespace,
                name=request.name,
                tenant_id=request.spec.tenant_id,
                revision=revision_number,
                etag=spec_digest(request.spec),
                spec=request.spec.model_copy(deep=True),
                action=request.action,
                created_at=now,
                created_by=request.actor,
                previous_revision=previous_revision,
            )
            active_key_id, key_hmac = self.hasher.digest(
                key_value,
                context="fs2-serve.model-deployment-idempotency/v1",
            )
            request_hmac = self.hasher.digest_for(
                active_key_id,
                request_value,
                context="fs2-serve.model-deployment-request/v1",
            )
            rows.append(value)
            self.model_deployment_idempotency[(request.actor_id, active_key_id, key_hmac)] = (
                request_hmac,
                request.namespace,
                request.name,
                value.revision,
            )
            self._audit(
                actor=request.actor,
                tenant_id=request.spec.tenant_id,
                token_id=None,
                action=f"model_deployment.revision.{request.action.value}",
                target_type="model_deployment",
                target_id=model_deployment_audit_target(request.namespace, request.name),
                outcome="succeeded",
                detail={
                    "revision": value.revision,
                    "previous_revision": value.previous_revision,
                    "etag": value.etag,
                    "idempotent_replay": False,
                    "namespace": request.namespace,
                    "name": request.name,
                },
            )
            return ModelDeploymentAppendResult(value=value.model_copy(deep=True))

    @staticmethod
    def _validate_model_deployment_read(
        *,
        namespace: str,
        tenant_id: str | None,
        limit: int,
    ) -> None:
        if not 1 <= len(namespace) <= 63 or not 1 <= limit <= 201:
            raise ValueError("model deployment read is outside the bound")
        if tenant_id is not None and not 1 <= len(tenant_id) <= 120:
            raise ValueError("model deployment tenant is outside the bound")

    async def model_deployment_list(
        self,
        *,
        namespace: str,
        tenant_id: str | None,
        after_name: str | None,
        limit: int,
    ) -> list[ModelDeploymentRevision]:
        self._validate_model_deployment_read(namespace=namespace, tenant_id=tenant_id, limit=limit)
        if after_name is not None and not 1 <= len(after_name) <= 253:
            raise ValueError("model deployment cursor is outside the bound")
        async with self._lock:
            values = [
                rows[-1]
                for (row_namespace, name), rows in self.model_deployment_revisions.items()
                if row_namespace == namespace
                and (tenant_id is None or rows[-1].tenant_id == tenant_id)
                and (after_name is None or name > after_name)
            ]
            values.sort(key=lambda item: item.name)
            return [item.model_copy(deep=True) for item in values[:limit]]

    async def model_deployment_current(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentRevision | None:
        self._validate_model_deployment_read(namespace=namespace, tenant_id=tenant_id, limit=1)
        if not 1 <= len(name) <= 253:
            raise ValueError("model deployment name is outside the bound")
        async with self._lock:
            rows = self.model_deployment_revisions.get((namespace, name), [])
            if not rows or (tenant_id is not None and rows[-1].tenant_id != tenant_id):
                return None
            return rows[-1].model_copy(deep=True)

    async def model_deployment_history(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
        before_revision: int | None,
        limit: int,
    ) -> list[ModelDeploymentRevision]:
        self._validate_model_deployment_read(namespace=namespace, tenant_id=tenant_id, limit=limit)
        if not 1 <= len(name) <= 253 or (before_revision is not None and before_revision < 2):
            raise ValueError("model deployment history query is outside the bound")
        async with self._lock:
            rows = self.model_deployment_revisions.get((namespace, name), [])
            if not rows or (tenant_id is not None and rows[-1].tenant_id != tenant_id):
                return []
            values = [item for item in reversed(rows) if before_revision is None or item.revision < before_revision]
            return [item.model_copy(deep=True) for item in values[:limit]]

    async def model_deployment_append_status(
        self,
        observation: ModelDeploymentStatusObservation,
    ) -> ModelDeploymentStatusObservation:
        async with self._lock:
            existing = self.model_deployment_status_by_id.get(observation.observation_id)
            if existing is not None:
                if existing != observation:
                    raise ConflictError("model deployment observation identity was reused")
                return existing.model_copy(deep=True)
            revisions = self.model_deployment_revisions.get((observation.namespace, observation.name), [])
            revision = next((item for item in revisions if item.revision == observation.revision), None)
            if (
                revision is None
                or revision.tenant_id != observation.tenant_id
                or revision.etag != observation.status.spec_digest
            ):
                raise ConflictError("model deployment observation has no matching desired revision")
            events = self.model_deployment_status_events.get((observation.namespace, observation.name), [])
            if events and model_deployment_status_precedes(observation, events[-1]):
                raise ConflictError("model deployment observation is older than current status")
            value = observation.model_copy(deep=True)
            self.model_deployment_status_events.setdefault((observation.namespace, observation.name), []).append(value)
            self.model_deployment_status_by_id[value.observation_id] = value
            return value.model_copy(deep=True)

    async def model_deployment_status(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentStatusObservation | None:
        self._validate_model_deployment_read(namespace=namespace, tenant_id=tenant_id, limit=1)
        if not 1 <= len(name) <= 253:
            raise ValueError("model deployment name is outside the bound")
        async with self._lock:
            revisions = self.model_deployment_revisions.get((namespace, name), [])
            if not revisions or (tenant_id is not None and revisions[-1].tenant_id != tenant_id):
                return None
            events = self.model_deployment_status_events.get((namespace, name), [])
            return events[-1].model_copy(deep=True) if events else None

    async def admin_key_usage(self, token_ids: tuple[UUID, ...], *, tenant_id: str | None) -> list[AdminKeyUsageRecord]:
        if len(token_ids) > 1000 or len(set(token_ids)) != len(token_ids):
            raise ValueError("key usage request is outside the bound")
        async with self._lock:
            result: list[AdminKeyUsageRecord] = []
            for token_id in token_ids:
                token = self.tokens.get(token_id)
                if token is None or (tenant_id is not None and token.view.tenant_id != tenant_id):
                    continue
                operations = [
                    row.view
                    for row in self.operations.values()
                    if row.view.token_id == token_id and row.view.status.terminal
                ]
                token_rows = [
                    operation
                    for operation in operations
                    if operation.input_tokens is not None and operation.output_tokens is not None
                ]
                modality_rows = [operation for operation in operations if operation.modality_usage_reported]
                modality_totals: dict[tuple[str, Any, str], float] = {}
                for operation in modality_rows:
                    for item in operation.modality_usage:
                        key = (item.modality, item.direction, item.unit)
                        modality_totals[key] = modality_totals.get(key, 0.0) + item.amount
                result.append(
                    AdminKeyUsageRecord(
                        token_id=token_id,
                        terminal_operations=len(operations),
                        estimated_gpu_seconds=sum(item.estimated_gpu_seconds for item in operations),
                        input_tokens=sum(item.input_tokens or 0 for item in token_rows) if token_rows else None,
                        output_tokens=sum(item.output_tokens or 0 for item in token_rows) if token_rows else None,
                        token_reported_operations=len(token_rows),
                        modality_reported_operations=len(modality_rows),
                        modality_units=[
                            ModalityUsage(
                                modality=modality,
                                direction=direction,
                                unit=unit,
                                amount=amount,
                            )
                            for (modality, direction, unit), amount in sorted(
                                modality_totals.items(), key=lambda item: tuple(str(value) for value in item[0])
                            )
                        ],
                    )
                )
            return result

    async def append_operation(
        self,
        *,
        principal: Principal,
        admission: AdmissionRequest,
        model_revision: str,
        reserved_gpu_seconds: float,
        max_attempts: int,
        dispatch_snapshot: str | None = None,
        dynamic_fence: DynamicAdmissionFence | None = None,
        scientific_admission_factory: Callable[[OperationView], dict[str, object]] | None = None,
    ) -> OperationView:
        async with self._activation_mutation_lock(admission.model_id):
            return await self._append_operation(
                principal=principal,
                admission=admission,
                model_revision=model_revision,
                reserved_gpu_seconds=reserved_gpu_seconds,
                max_attempts=max_attempts,
                dispatch_snapshot=dispatch_snapshot,
                dynamic_fence=dynamic_fence,
                scientific_admission_factory=scientific_admission_factory,
            )

    async def _append_operation(
        self,
        *,
        principal: Principal,
        admission: AdmissionRequest,
        model_revision: str,
        reserved_gpu_seconds: float,
        max_attempts: int,
        dispatch_snapshot: str | None,
        dynamic_fence: DynamicAdmissionFence | None,
        scientific_admission_factory: Callable[[OperationView], dict[str, object]] | None,
    ) -> OperationView:
        async with self._lock:
            token = self.tokens.get(principal.token_id)
            now = datetime.now(UTC)
            if token is None or token.view.revoked_at is not None:
                raise PermissionError("token is no longer active")
            if token.view.expires_at is not None and token.view.expires_at <= now:
                raise PermissionError("token has expired")
            key = (principal.tenant_id, principal.principal_id, principal.token_id, admission.idempotency_key)
            existing_id = self.idempotency.get(key)
            if existing_id is not None:
                existing_row = self.operations[existing_id]
                existing = existing_row.view
                try:
                    request_hmac = self.hasher.digest_for(
                        existing_row.request_hmac_key_id,
                        admission.request_body,
                        context="fs2-serve.request/v1",
                    )
                except ValueError as exc:
                    raise ConflictError("idempotency replay key is unavailable") from exc
                if (
                    existing.model_id != admission.model_id
                    or existing.model_revision != model_revision
                    or existing.protocol != admission.protocol
                    or existing.operation != admission.operation
                    or existing_row.request_hmac != request_hmac
                    or existing_row.content_type != admission.request_content_type
                ):
                    raise ConflictError("idempotency key is already bound to a different request")
                operation = self._metadata(existing_row, reused=True)
                self._stage_scientific_admission(operation, scientific_admission_factory)
                return operation
            if (dynamic_fence is None) != (dispatch_snapshot is None):
                raise ConflictError("dynamic admission fence and dispatch snapshot must be supplied together")
            if dynamic_fence is not None:
                revisions = self.model_deployment_revisions.get(
                    (dynamic_fence.namespace, dynamic_fence.name),
                    [],
                )
                desired = revisions[-1] if revisions else None
                if (
                    desired is None
                    or desired.etag != dynamic_fence.etag
                    or desired.tenant_id != principal.tenant_id
                    or desired.spec.model_ref != admission.model_id
                    or desired.spec.lifecycle.desired_state is not DesiredState.ENABLED
                ):
                    raise ConflictError("dynamic model no longer accepts admissions")
            hmac_key_id, request_hmac = self.hasher.digest(admission.request_body, context="fs2-serve.request/v1")
            rate_started = token.view.rate_window_started_at
            rate_requests = token.view.rate_window_requests
            if token.view.rate_limit_requests is not None and token.view.rate_window_seconds is not None:
                if rate_started is None or now >= rate_started + timedelta(seconds=token.view.rate_window_seconds):
                    rate_started = now
                    rate_requests = 0
                if rate_requests >= token.view.rate_limit_requests:
                    raise RateLimitExceededError("token rate window is exhausted")
            if token.view.request_budget is not None and token.view.requests_used >= token.view.request_budget:
                raise BudgetExceededError("request budget exhausted")
            if token.view.gpu_seconds_budget is not None and (
                token.view.gpu_seconds_used + token.view.gpu_seconds_reserved + reserved_gpu_seconds
                > token.view.gpu_seconds_budget
            ):
                raise BudgetExceededError("GPU-seconds reservation exceeds token budget")
            active = sum(
                1
                for item in self.operations.values()
                if item.view.token_id == principal.token_id and self._active(item.view.status)
            )
            if active >= token.view.max_concurrency:
                raise ConcurrencyExceededError("token concurrency limit reached")
            operation_id = uuid4()
            encrypted = self.cipher.encrypt(
                admission.request_body,
                aad=self.cipher.aad(operation_id, principal.tenant_id, admission.model_id, "request"),
            )
            view = OperationView(
                id=operation_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                token_id=principal.token_id,
                model_id=admission.model_id,
                model_revision=model_revision,
                protocol=admission.protocol,
                operation=admission.operation,
                idempotency_key=admission.idempotency_key,
                status=OperationStatus.QUEUED,
                accepted_at=now,
                available_at=now,
                deadline_at=admission.deadline_at,
                payload_expires_at=now + timedelta(seconds=self.payload_ttl_seconds),
                max_attempts=max_attempts,
                reserved_gpu_seconds=reserved_gpu_seconds,
            )
            row = _Operation(
                view=view,
                request_hmac_key_id=hmac_key_id,
                request_hmac=request_hmac,
                request=encrypted,
                content_type=admission.request_content_type,
                traceparent=admission.traceparent,
                dispatch_snapshot=dispatch_snapshot,
            )
            self._stage_scientific_admission(view, scientific_admission_factory)
            self.operations[operation_id] = row
            self.idempotency[key] = operation_id
            token.view = token.view.model_copy(
                update={
                    "requests_used": token.view.requests_used + 1,
                    "last_used_at": now,
                    "gpu_seconds_reserved": token.view.gpu_seconds_reserved + reserved_gpu_seconds,
                    "rate_window_started_at": rate_started,
                    "rate_window_requests": rate_requests + 1 if token.view.rate_limit_requests is not None else 0,
                }
            )
            self._audit(
                actor=principal.principal_id,
                tenant_id=principal.tenant_id,
                token_id=principal.token_id,
                action="operation.admit",
                target_type="operation",
                target_id=str(operation_id),
                outcome="queued",
                detail={"model_id": admission.model_id, "protocol": admission.protocol},
            )
            return self._metadata(row)

    def _stage_scientific_admission(
        self,
        operation: OperationView,
        factory: Callable[[OperationView], dict[str, object]] | None,
    ) -> None:
        if factory is None:
            return
        if operation.protocol != "scientific-batch-v1":
            raise ConflictError("scientific admission outbox requires a scientific batch Operation")
        if operation.id in self.scientific_admissions_completed:
            return
        payload = factory(operation)
        pending = PendingScientificAdmission(
            operation_id=operation.id,
            payload=payload,
            created_at=operation.accepted_at,
        )
        current = self.scientific_admission_outbox.get(operation.id)
        if current is not None and current.payload != pending.payload:
            raise ConflictError("scientific admission outbox already contains another frozen request")
        self.scientific_admission_outbox[operation.id] = pending

    async def get_scientific_admission(self, operation_id: UUID) -> PendingScientificAdmission | None:
        async with self._lock:
            return self.scientific_admission_outbox.get(operation_id)

    async def list_scientific_admissions(self, *, limit: int = 100) -> list[PendingScientificAdmission]:
        if not 1 <= limit <= 1000:
            raise ValueError("scientific admission page is outside the bound")
        async with self._lock:
            return sorted(
                self.scientific_admission_outbox.values(),
                key=lambda item: (item.created_at, item.operation_id),
            )[:limit]

    async def complete_scientific_admission(self, operation_id: UUID) -> None:
        async with self._lock:
            if self.scientific_admission_outbox.pop(operation_id, None) is not None:
                self.scientific_admissions_completed.add(operation_id)

    async def get_operation(self, operation_id: UUID, *, tenant_id: str | None = None) -> OperationView:
        async with self._lock:
            row = self.operations.get(operation_id)
            if row is None or (tenant_id is not None and row.view.tenant_id != tenant_id):
                raise NotFoundError("operation not found")
            return self._metadata(row)

    async def get_operation_result(self, operation_id: UUID, *, tenant_id: str) -> OperationResult:
        async with self._lock:
            row = self.operations.get(operation_id)
            if row is None or row.view.tenant_id != tenant_id:
                raise NotFoundError("operation not found")
            metadata = self._metadata(row)
            if metadata.status != OperationStatus.SUCCEEDED:
                raise ConflictError("operation has no successful result")
            if not metadata.result_available or row.response is None:
                raise ConflictError("operation result is unavailable")
            raw = self.cipher.decrypt(row.response, aad=self._aad(row, "response"))
            try:
                result: Any = json.loads(raw)
            except json.JSONDecodeError:
                result = {"base64": base64.b64encode(raw).decode()}
            return OperationResult(operation=metadata, result=result)

    async def claim_operation(self, worker_id: str, *, lease_seconds: float) -> ClaimedOperation | None:
        async with self._lock:
            now = datetime.now(UTC)
            for row in sorted(
                self.operations.values(),
                key=lambda item: (item.view.available_at, item.view.accepted_at),
            ):
                if (
                    row.view.status != OperationStatus.QUEUED
                    or row.view.protocol in {"scientific-batch-v1", "scientific-artifact-upload-v1"}
                    or row.view.available_at > now
                    or row.view.attempt >= row.view.max_attempts
                ):
                    continue
                token = self.tokens.get(row.view.token_id)
                if (
                    token is None
                    or token.view.revoked_at is not None
                    or (token.view.expires_at is not None and token.view.expires_at <= now)
                ):
                    self._expire(row, now, "token_inactive")
                    continue
                if row.request is None or (row.view.deadline_at is not None and row.view.deadline_at <= now):
                    self._expire(row, now, "payload_or_deadline_expired")
                    continue
                row.view = row.view.model_copy(
                    update={
                        "status": OperationStatus.ACTIVATING,
                        "activation_started_at": row.view.activation_started_at or now,
                        "attempt": row.view.attempt + 1,
                        "fencing_token": row.view.fencing_token + 1,
                    }
                )
                row.worker_id = worker_id
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                per_attempt = row.view.reserved_gpu_seconds / max(1, row.view.max_attempts - row.view.attempt + 1)
                token = self.tokens[row.view.token_id]
                token.view = token.view.model_copy(
                    update={
                        "gpu_seconds_used": token.view.gpu_seconds_used + per_attempt,
                        "gpu_seconds_reserved": max(0, token.view.gpu_seconds_reserved - per_attempt),
                    }
                )
                row.view = row.view.model_copy(
                    update={
                        "estimated_gpu_seconds": row.view.estimated_gpu_seconds + per_attempt,
                        "reserved_gpu_seconds": max(0, row.view.reserved_gpu_seconds - per_attempt),
                    }
                )
                return ClaimedOperation(
                    **self._metadata(row).model_dump(),
                    request_content_type=row.content_type,
                    traceparent=row.traceparent,
                    dispatch_snapshot=row.dispatch_snapshot,
                    worker_id=worker_id,
                )
            return None

    async def complete_scientific_artifact_upload(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> OperationView:
        """Close one verified customer upload without exposing a worker lease."""

        async with self._lock:
            row = self.operations.get(operation_id)
            if (
                row is None
                or row.view.tenant_id != tenant_id
                or row.view.principal_id != principal_id
                or row.view.protocol != "scientific-artifact-upload-v1"
            ):
                raise NotFoundError("scientific artifact upload operation not found")
            if row.view.status is OperationStatus.SUCCEEDED:
                return self._metadata(row, reused=True)
            if row.view.status is not OperationStatus.QUEUED:
                raise ConflictError("scientific artifact upload operation is not writable")
            now = datetime.now(UTC)
            row.view = row.view.model_copy(
                update={
                    "status": OperationStatus.SUCCEEDED,
                    "completed_at": now,
                    "outcome": "artifact_uploaded",
                    "semantic_outcome": "verified",
                    "http_status": 201,
                    "reserved_gpu_seconds": 0,
                }
            )
            self._audit(
                actor=principal_id,
                tenant_id=tenant_id,
                token_id=row.view.token_id,
                action="scientific_artifact.upload.complete",
                target_type="operation",
                target_id=str(operation_id),
                outcome="succeeded",
            )
            return self._metadata(row)

    def _require_activation_claim(
        self,
        row: _Activation,
        controller_id: str,
        fencing_token: int,
        leadership_fencing_token: int,
    ) -> None:
        now = datetime.now(UTC)
        intent = row.view
        if (
            intent.status is not ActivationIntentStatus.CLAIMED
            or intent.controller_id != controller_id
            or intent.fencing_token != fencing_token
            or intent.leadership_fencing_token != leadership_fencing_token
            or intent.lease_expires_at is None
            or intent.lease_expires_at <= now
            or (intent.deadline_at is not None and intent.deadline_at <= now)
        ):
            raise StaleLeaseError("activation intent lease is stale")

    def _activation_operation_is_current(self, intent: ActivationIntent) -> bool:
        if intent.operation_id is None:
            return True
        operation = self.operations.get(intent.operation_id)
        return bool(
            operation is not None
            and operation.view.status is OperationStatus.ACTIVATING
            and operation.view.attempt == intent.operation_attempt
            and (operation.view.deadline_at is None or operation.view.deadline_at > datetime.now(UTC))
        )

    async def ensure_activation_intent(
        self,
        operation: ClaimedOperation,
        *,
        binding_digest: str,
        worker_id: str,
        fencing_token: int,
    ) -> ActivationIntent:
        async with self._lock:
            operation_row = self.operations.get(operation.id)
            if operation_row is None:
                raise StaleLeaseError("operation is absent")
            self._require_lease(operation_row, worker_id, fencing_token)
            if (
                operation_row.view.model_id != operation.model_id
                or operation_row.view.model_revision != operation.model_revision
                or operation_row.view.attempt != operation.attempt
            ):
                raise StaleLeaseError("operation activation subject is stale")
            for candidate in self.activation_intents.values():
                if (
                    candidate.view.model_id == operation.model_id
                    and candidate.view.action is ActivationAction.DEACTIVATE
                    and candidate.view.status in {ActivationIntentStatus.QUEUED, ActivationIntentStatus.CLAIMED}
                ):
                    candidate.view = candidate.view.model_copy(
                        update={
                            "status": ActivationIntentStatus.EXPIRED,
                            "controller_id": None,
                            "lease_expires_at": None,
                            "fencing_token": candidate.view.fencing_token + 1,
                            "error_code": "new_demand",
                        }
                    )
            existing = self.activation_intents.get(operation.id)
            if existing is not None:
                if (
                    existing.view.model_id != operation.model_id
                    or existing.view.model_revision != operation.model_revision
                    or existing.view.binding_digest != binding_digest
                    or existing.view.action is not ActivationAction.ACTIVATE
                ):
                    raise ConflictError("activation intent subject differs")
                if existing.view.operation_attempt < operation.attempt:
                    existing.view = existing.view.model_copy(
                        update={
                            "operation_attempt": operation.attempt,
                            "status": ActivationIntentStatus.QUEUED,
                            "available_at": datetime.now(UTC),
                            "deadline_at": operation.deadline_at,
                            "controller_id": None,
                            "lease_expires_at": None,
                            "fencing_token": existing.view.fencing_token + 1,
                            "error_code": None,
                        }
                    )
                if self.auto_activate and existing.view.status is ActivationIntentStatus.QUEUED:
                    existing.view = existing.view.model_copy(update={"status": ActivationIntentStatus.READY})
                return existing.view.model_copy(deep=True)
            now = datetime.now(UTC)
            intent = ActivationIntent(
                id=operation.id,
                operation_id=operation.id,
                operation_attempt=operation.attempt,
                model_id=operation.model_id,
                model_revision=operation.model_revision,
                binding_digest=binding_digest,
                action=ActivationAction.ACTIVATE,
                status=ActivationIntentStatus.QUEUED,
                requested_at=now,
                available_at=now,
                deadline_at=operation.deadline_at,
                attempt=0,
                max_attempts=operation.max_attempts,
                fencing_token=0,
            )
            self.activation_intents[intent.id] = _Activation(intent)
            if self.auto_activate:
                intent = intent.model_copy(update={"status": ActivationIntentStatus.READY})
                self.activation_intents[intent.id].view = intent
            return intent.model_copy(deep=True)

    async def get_activation_intent(self, operation_id: UUID) -> ActivationIntent:
        async with self._lock:
            row = self.activation_intents.get(operation_id)
            if row is None or row.view.operation_id != operation_id:
                raise NotFoundError("activation intent not found")
            return row.view.model_copy(deep=True)

    async def claim_activation_intent(
        self,
        identity: ActivationLeaderIdentity,
        *,
        leadership_fencing_token: int,
        lease_seconds: float,
    ) -> ClaimedActivationIntent | None:
        async with self._lock:
            self._require_current_activation_leader(identity, leadership_fencing_token)
            now = datetime.now(UTC)
            for row in sorted(
                self.activation_intents.values(),
                key=lambda item: (item.view.available_at, item.view.id),
            ):
                intent = row.view
                if intent.status is ActivationIntentStatus.CLAIMED and (
                    intent.lease_expires_at is None or intent.lease_expires_at <= now
                ):
                    deadline_elapsed = intent.deadline_at is not None and intent.deadline_at <= now
                    attempts_exhausted = intent.attempt >= intent.max_attempts
                    requeue = not deadline_elapsed and not attempts_exhausted
                    intent = intent.model_copy(
                        update={
                            "status": (
                                ActivationIntentStatus.EXPIRED
                                if deadline_elapsed
                                else (
                                    ActivationIntentStatus.FAILED
                                    if attempts_exhausted
                                    else ActivationIntentStatus.QUEUED
                                )
                            ),
                            "available_at": now if requeue else intent.available_at,
                            "controller_id": None,
                            "lease_expires_at": None,
                            "fencing_token": intent.fencing_token + 1,
                            "model_fencing_token": None if requeue else intent.model_fencing_token,
                            "leadership_fencing_token": None if requeue else intent.leadership_fencing_token,
                            "error_code": (
                                "deadline_exceeded"
                                if deadline_elapsed
                                else (
                                    "activation_attempts_exhausted"
                                    if attempts_exhausted
                                    else "controller_lease_expired"
                                )
                            ),
                        }
                    )
                    row.view = intent
                    self._activation_event(
                        intent,
                        (
                            "activation_intent_deadline_expired"
                            if deadline_elapsed
                            else (
                                "activation_intent_attempts_exhausted"
                                if attempts_exhausted
                                else "activation_intent_lease_requeued"
                            )
                        ),
                    )
                if intent.status is ActivationIntentStatus.QUEUED and (
                    (intent.deadline_at is not None and intent.deadline_at <= now)
                    or intent.attempt >= intent.max_attempts
                ):
                    deadline_elapsed = intent.deadline_at is not None and intent.deadline_at <= now
                    intent = intent.model_copy(
                        update={
                            "status": (
                                ActivationIntentStatus.EXPIRED if deadline_elapsed else ActivationIntentStatus.FAILED
                            ),
                            "controller_id": None,
                            "lease_expires_at": None,
                            "fencing_token": intent.fencing_token + 1,
                            "error_code": (
                                "deadline_exceeded" if deadline_elapsed else "activation_attempts_exhausted"
                            ),
                        }
                    )
                    row.view = intent
                    self._activation_event(
                        intent,
                        (
                            "activation_intent_deadline_expired"
                            if deadline_elapsed
                            else "activation_intent_attempts_exhausted"
                        ),
                    )
                if intent.status is not ActivationIntentStatus.QUEUED or intent.available_at > now:
                    continue
                if intent.action is ActivationAction.ACTIVATE and not self._activation_operation_is_current(intent):
                    row.view = intent.model_copy(
                        update={"status": ActivationIntentStatus.EXPIRED, "error_code": "operation_stale"}
                    )
                    continue
                if intent.action is ActivationAction.DEACTIVATE and any(
                    self._active(operation.view.status) and operation.view.model_id == intent.model_id
                    for operation in self.operations.values()
                ):
                    row.view = intent.model_copy(
                        update={"status": ActivationIntentStatus.EXPIRED, "error_code": "model_busy"}
                    )
                    continue
                lease_expires = now + timedelta(seconds=lease_seconds)
                assert self.activation_controller_lease_expires_at is not None
                lease_expires = min(lease_expires, self.activation_controller_lease_expires_at)
                if intent.deadline_at is not None:
                    lease_expires = min(lease_expires, intent.deadline_at)
                row.view = intent.model_copy(
                    update={
                        "status": ActivationIntentStatus.CLAIMED,
                        "attempt": intent.attempt + 1,
                        "controller_id": identity.controller_id,
                        "fencing_token": intent.fencing_token + 1,
                        "model_fencing_token": self.activation_model_fences.get(intent.model_id, 0) + 1,
                        "leadership_fencing_token": leadership_fencing_token,
                        "lease_expires_at": lease_expires,
                        "error_code": None,
                    }
                )
                self.activation_model_fences[intent.model_id] = row.view.model_fencing_token or 0
                return ClaimedActivationIntent.model_validate(row.view.model_dump())
            return None

    async def heartbeat_activation_intent(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        lease_seconds: float,
    ) -> None:
        async with self._lock:
            self._require_current_activation_leader(identity, leadership_fencing_token)
            row = self.activation_intents[intent_id]
            self._require_activation_claim(row, controller_id, fencing_token, leadership_fencing_token)
            if row.view.action is ActivationAction.ACTIVATE and not self._activation_operation_is_current(row.view):
                raise StaleLeaseError("activation operation is stale")
            lease_expires = datetime.now(UTC) + timedelta(seconds=lease_seconds)
            assert self.activation_controller_lease_expires_at is not None
            lease_expires = min(lease_expires, self.activation_controller_lease_expires_at)
            if row.view.deadline_at is not None:
                lease_expires = min(lease_expires, row.view.deadline_at)
            row.view = row.view.model_copy(update={"lease_expires_at": lease_expires})

    async def activation_wait_budget(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        maximum_seconds: float,
    ) -> float:
        if not 1 <= maximum_seconds <= 1800:
            raise ValueError("activation wait maximum is outside the closed bound")
        async with self._lock:
            self._require_current_activation_leader(identity, leadership_fencing_token)
            row = self.activation_intents[intent_id]
            self._require_activation_claim(row, controller_id, fencing_token, leadership_fencing_token)
            now = datetime.now(UTC)
            if row.view.deadline_at is None:
                return maximum_seconds
            remaining = min(maximum_seconds, (row.view.deadline_at - now).total_seconds())
            if remaining <= 0:
                raise StaleLeaseError("activation wait budget is stale")
            return remaining

    async def complete_activation_intent(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        scale_contract_digest: str,
        target: ActivationTargetState,
    ) -> ActivationIntent:
        async with self._lock:
            self._require_current_activation_leader(identity, leadership_fencing_token)
            row = self.activation_intents[intent_id]
            if row.view.status.terminal:
                durable_target = row.view.target
                if row.view.status is ActivationIntentStatus.READY and (
                    durable_target is None
                    or durable_target.model_id != target.model_id
                    or row.view.scale_contract_digest != scale_contract_digest
                    or durable_target.target_uid != target.target_uid
                    or durable_target.resource_version != target.resource_version
                    or durable_target.observed_generation != target.observed_generation
                    or durable_target.template_digest != target.template_digest
                    or durable_target.active is not target.active
                ):
                    raise StaleLeaseError("activation terminal result differs from replay")
                if row.view.status is ActivationIntentStatus.FAILED and row.view.error_code != "stale_model_fence":
                    raise StaleLeaseError("activation terminal failure differs from replay")
                return row.view.model_copy(deep=True)
            self._require_activation_claim(row, controller_id, fencing_token, leadership_fencing_token)
            if row.view.action is ActivationAction.ACTIVATE and not self._activation_operation_is_current(row.view):
                raise StaleLeaseError("activation operation is stale")
            expected_active = row.view.action is ActivationAction.ACTIVATE
            if target.model_id != row.view.model_id or target.active is not expected_active:
                raise ConflictError("activation target outcome differs from intent")
            if target.controller_fencing_token != leadership_fencing_token:
                raise StaleLeaseError("activation leadership fence differs from target")
            existing = self.activation_targets.get(row.view.model_id)
            if existing is not None and (
                existing.controller_fencing_token > target.controller_fencing_token
                or (row.view.model_fencing_token or 0) <= existing.model_fencing_token
                or existing.target_uid != target.target_uid
                or existing.template_digest != target.template_digest
                or existing.observed_generation > target.observed_generation
                or (
                    existing.observed_generation == target.observed_generation
                    and (existing.resource_version != target.resource_version or existing.active is not target.active)
                )
            ):
                row.view = row.view.model_copy(
                    update={
                        "status": ActivationIntentStatus.FAILED,
                        "controller_id": None,
                        "lease_expires_at": None,
                        "scale_contract_digest": scale_contract_digest,
                        "error_code": "stale_model_fence",
                    }
                )
                return row.view.model_copy(deep=True)
            target = target.model_copy(update={"model_fencing_token": row.view.model_fencing_token})
            row.view = row.view.model_copy(
                update={
                    "status": ActivationIntentStatus.READY,
                    "controller_id": None,
                    "lease_expires_at": None,
                    "scale_contract_digest": scale_contract_digest,
                    "target": target,
                    "error_code": None,
                }
            )
            self.activation_targets[row.view.model_id] = target.model_copy(deep=True)
            return row.view.model_copy(deep=True)

    async def retry_activation_intent(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        available_at: datetime,
        error_code: str,
    ) -> ActivationIntent:
        async with self._lock:
            self._require_current_activation_leader(identity, leadership_fencing_token)
            row = self.activation_intents[intent_id]
            self._require_activation_claim(row, controller_id, fencing_token, leadership_fencing_token)
            deadline_elapsed = row.view.deadline_at is not None and row.view.deadline_at <= datetime.now(UTC)
            terminal = deadline_elapsed or row.view.attempt >= row.view.max_attempts
            row.view = row.view.model_copy(
                update={
                    "status": ActivationIntentStatus.EXPIRED
                    if deadline_elapsed
                    else (ActivationIntentStatus.FAILED if terminal else ActivationIntentStatus.QUEUED),
                    "available_at": available_at,
                    "controller_id": None,
                    "lease_expires_at": None,
                    "error_code": "deadline_exceeded" if deadline_elapsed else error_code,
                }
            )
            return row.view.model_copy(deep=True)

    async def request_scale_down(
        self,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        model_id: str,
        model_revision: str,
        binding_digest: str,
        idle_before: datetime,
        max_attempts: int,
    ) -> ActivationIntent | None:
        async with self._lock:
            self._require_current_activation_leader(identity, leadership_fencing_token)
            if any(
                self._active(operation.view.status) and operation.view.model_id == model_id
                for operation in self.operations.values()
            ):
                return None
            if any(
                row.view.model_id == model_id
                and row.view.status in {ActivationIntentStatus.QUEUED, ActivationIntentStatus.CLAIMED}
                for row in self.activation_intents.values()
            ):
                return None
            target = self.activation_targets.get(model_id)
            if target is None or not target.active or target.observed_at > idle_before:
                return None
            now = datetime.now(UTC)
            intent = ActivationIntent(
                id=uuid4(),
                operation_id=None,
                operation_attempt=0,
                model_id=model_id,
                model_revision=model_revision,
                binding_digest=binding_digest,
                action=ActivationAction.DEACTIVATE,
                status=ActivationIntentStatus.QUEUED,
                requested_at=now,
                available_at=now,
                attempt=0,
                max_attempts=max_attempts,
                fencing_token=0,
            )
            self.activation_intents[intent.id] = _Activation(intent)
            return intent.model_copy(deep=True)

    async def get_activation_target_state(self, model_id: str) -> ActivationTargetState | None:
        async with self._lock:
            target = self.activation_targets.get(model_id)
            return None if target is None else target.model_copy(deep=True)

    @asynccontextmanager
    async def activation_mutation_guard(
        self,
        intent: ClaimedActivationIntent,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
    ) -> AsyncIterator[None]:
        async with self._activation_mutation_lock(intent.model_id):
            async with self._lock:
                self._validate_activation_mutation(intent, identity, leadership_fencing_token)
            yield
            async with self._lock:
                self._validate_activation_mutation(intent, identity, leadership_fencing_token)

    def _validate_activation_mutation(
        self,
        intent: ClaimedActivationIntent,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
    ) -> None:
        self._require_current_activation_leader(identity, leadership_fencing_token)
        row = self.activation_intents[intent.id]
        self._require_activation_claim(row, intent.controller_id, intent.fencing_token, leadership_fencing_token)
        if intent.action is ActivationAction.ACTIVATE:
            if not self._activation_operation_is_current(row.view):
                raise StaleLeaseError("activation operation is stale")
        elif any(
            self._active(operation.view.status) and operation.view.model_id == intent.model_id
            for operation in self.operations.values()
        ):
            raise StaleLeaseError("model gained active work before scale down")

    def _require_lease(self, row: _Operation, worker_id: str, fencing_token: int) -> None:
        now = datetime.now(UTC)
        if row.view.deadline_at is not None and row.view.deadline_at <= now and self._active(row.view.status):
            self._expire(row, now, "deadline_exceeded")
            raise StaleLeaseError("operation deadline elapsed")
        if (
            row.worker_id != worker_id
            or row.view.fencing_token != fencing_token
            or row.lease_expires_at is None
            or row.lease_expires_at <= now
            or row.view.status not in {OperationStatus.ACTIVATING, OperationStatus.RUNNING}
        ):
            raise StaleLeaseError("operation lease is stale")

    async def read_request_payload(self, operation_id: UUID, *, worker_id: str, fencing_token: int) -> bytes:
        async with self._lock:
            row = self.operations[operation_id]
            self._require_lease(row, worker_id, fencing_token)
            if row.request is None:
                raise StaleLeaseError("operation payload is unavailable")
            return self.cipher.decrypt(row.request, aad=self._aad(row, "request"))

    async def heartbeat(self, operation_id: UUID, *, worker_id: str, fencing_token: int, lease_seconds: float) -> None:
        async with self._lock:
            row = self.operations[operation_id]
            self._require_lease(row, worker_id, fencing_token)
            now = datetime.now(UTC)
            lease_end = now + timedelta(seconds=lease_seconds)
            if row.view.deadline_at is not None:
                lease_end = min(lease_end, row.view.deadline_at)
            row.lease_expires_at = lease_end

    async def mark_ready(self, operation_id: UUID, *, worker_id: str, fencing_token: int) -> None:
        async with self._lock:
            row = self.operations[operation_id]
            self._require_lease(row, worker_id, fencing_token)
            row.view = row.view.model_copy(update={"ready_at": datetime.now(UTC)})

    async def mark_running(
        self, operation_id: UUID, runtime: RuntimeIdentity, *, worker_id: str, fencing_token: int
    ) -> None:
        async with self._lock:
            row = self.operations[operation_id]
            self._require_lease(row, worker_id, fencing_token)
            row.view = row.view.model_copy(
                update={"status": OperationStatus.RUNNING, "started_at": datetime.now(UTC), "runtime": runtime}
            )

    async def complete_operation(
        self,
        operation_id: UUID,
        *,
        status: OperationStatus,
        outcome: str,
        semantic_outcome: str,
        http_status: int,
        response_body: bytes | None,
        response_content_type: str | None,
        error_code: str | None,
        error_detail: str | None,
        runtime: RuntimeIdentity,
        worker_id: str,
        fencing_token: int,
        usage: ReportedUsage | None = None,
    ) -> OperationView:
        if not status.terminal:
            raise ValueError("completion status must be terminal")
        async with self._lock:
            row = self.operations[operation_id]
            self._require_lease(row, worker_id, fencing_token)
            if response_body is not None:
                row.response_hmac_key_id, row.response_hmac = self.hasher.digest(
                    response_body, context="fs2-serve.response/v1"
                )
                row.response = self.cipher.encrypt(response_body, aad=self._aad(row, "response"))
            now = datetime.now(UTC)
            cold = (row.view.ready_at - row.view.accepted_at).total_seconds() if row.view.ready_at else None
            row.view = row.view.model_copy(
                update={
                    "status": status,
                    "completed_at": now,
                    "outcome": outcome,
                    "semantic_outcome": semantic_outcome,
                    "http_status": http_status,
                    "response_content_type": response_content_type,
                    "result_available": response_body is not None,
                    "error_code": error_code,
                    "error_detail": sanitize_error_detail(error_detail or "") or None,
                    "runtime": runtime,
                    "cold_start_seconds": cold,
                    "input_tokens": usage.input_tokens if usage is not None else None,
                    "output_tokens": usage.output_tokens if usage is not None else None,
                    "modality_usage": (
                        list(usage.modalities) if usage is not None and usage.modalities is not None else []
                    ),
                    "modality_usage_reported": usage is not None and usage.modalities is not None,
                }
            )
            row.worker_id = None
            row.lease_expires_at = None
            self._release_reservation(row)
            self._audit(
                actor="worker",
                tenant_id=row.view.tenant_id,
                token_id=row.view.token_id,
                action="operation.complete",
                target_type="operation",
                target_id=str(operation_id),
                outcome=outcome,
                detail={"model_id": row.view.model_id, "semantic_outcome": semantic_outcome},
            )
            return self._metadata(row)

    async def retry_operation(
        self,
        operation_id: UUID,
        *,
        worker_id: str,
        fencing_token: int,
        available_at: datetime,
        error_code: str,
        error_detail: str,
    ) -> OperationView:
        async with self._lock:
            row = self.operations[operation_id]
            self._require_lease(row, worker_id, fencing_token)
            if row.view.attempt >= row.view.max_attempts:
                raise ConflictError("operation retry attempts exhausted")
            if row.view.deadline_at is not None and available_at >= row.view.deadline_at:
                raise ConflictError("retry backoff exceeds operation deadline")
            row.view = row.view.model_copy(
                update={
                    "status": OperationStatus.QUEUED,
                    "available_at": available_at,
                    "error_code": error_code,
                    "error_detail": sanitize_error_detail(error_detail) or None,
                    "fencing_token": row.view.fencing_token + 1,
                }
            )
            row.worker_id = None
            row.lease_expires_at = None
            return self._metadata(row)

    async def release_operation(self, operation_id: UUID, *, worker_id: str, fencing_token: int) -> OperationView:
        async with self._lock:
            row = self.operations[operation_id]
            self._require_lease(row, worker_id, fencing_token)
            now = datetime.now(UTC)
            token = self.tokens.get(row.view.token_id)
            can_requeue = bool(
                token is not None
                and token.view.revoked_at is None
                and (token.view.expires_at is None or token.view.expires_at > now)
                and row.request is not None
                and row.view.payload_expires_at is not None
                and row.view.payload_expires_at > now
                and (row.view.deadline_at is None or row.view.deadline_at > now)
                and row.view.attempt < row.view.max_attempts
            )
            if not can_requeue:
                error_code = (
                    "attempts_exhausted" if row.view.attempt >= row.view.max_attempts else "release_not_admissible"
                )
                self._expire(row, now, error_code)
                return self._metadata(row)
            row.view = row.view.model_copy(
                update={
                    "status": OperationStatus.QUEUED,
                    "available_at": now,
                    "fencing_token": row.view.fencing_token + 1,
                    "error_code": "worker_released",
                    "error_detail": None,
                }
            )
            row.worker_id = None
            row.lease_expires_at = None
            return self._metadata(row)

    async def cancel_operation(self, operation_id: UUID, *, tenant_id: str, actor: str) -> OperationView:
        async with self._lock:
            row = self.operations.get(operation_id)
            if row is None or row.view.tenant_id != tenant_id:
                raise NotFoundError("operation not found")
            if not row.view.status.terminal:
                self._release_reservation(row)
                row.view = row.view.model_copy(
                    update={
                        "status": OperationStatus.CANCELLED,
                        "completed_at": datetime.now(UTC),
                        "outcome": "cancelled",
                        "error_code": "cancelled_by_caller",
                        "error_detail": None,
                        "fencing_token": row.view.fencing_token + 1,
                    }
                )
                row.worker_id = None
                row.lease_expires_at = None
            self._audit(
                actor=actor,
                tenant_id=tenant_id,
                token_id=row.view.token_id,
                action="operation.cancel",
                target_type="operation",
                target_id=str(operation_id),
                outcome=str(row.view.status),
            )
            return self._metadata(row)

    async def purge_operation_payload(self, operation_id: UUID, *, tenant_id: str) -> None:
        async with self._lock:
            row = self.operations.get(operation_id)
            if row is None or row.view.tenant_id != tenant_id:
                raise NotFoundError("operation not found")
            if not row.view.status.terminal:
                raise ConflictError("operation is not terminal")
            row.request = None
            row.response = None
            row.view = row.view.model_copy(update={"result_available": False})

    async def purge_expired_payloads(self) -> int:
        async with self._lock:
            now = datetime.now(UTC)
            count = 0
            for row in self.operations.values():
                if row.view.payload_expires_at is None or row.view.payload_expires_at > now:
                    continue
                if row.request is not None or row.response is not None:
                    count += 1
                row.request = None
                row.response = None
                if not row.view.status.terminal:
                    self._expire(row, now, "payload_expired")
                row.view = row.view.model_copy(update={"result_available": False})
            return count

    async def expire_deadline_operations(self) -> int:
        async with self._lock:
            now = datetime.now(UTC)
            count = 0
            for row in self.operations.values():
                if (
                    row.view.status == OperationStatus.QUEUED
                    and row.view.deadline_at is not None
                    and row.view.deadline_at <= now
                ):
                    self._expire(row, now, "deadline_exceeded")
                    count += 1
            return count

    async def reap_stale_operations(self) -> int:
        async with self._lock:
            now = datetime.now(UTC)
            count = 0
            for row in self.operations.values():
                if row.view.status not in {OperationStatus.ACTIVATING, OperationStatus.RUNNING}:
                    continue
                if row.lease_expires_at is None or row.lease_expires_at > now:
                    continue
                count += 1
                usable = (
                    row.request is not None
                    and row.view.payload_expires_at is not None
                    and row.view.payload_expires_at > now
                    and (row.view.deadline_at is None or row.view.deadline_at > now)
                    and row.view.attempt < row.view.max_attempts
                )
                row.worker_id = None
                row.lease_expires_at = None
                if usable:
                    row.view = row.view.model_copy(
                        update={
                            "status": OperationStatus.QUEUED,
                            "available_at": now,
                            "fencing_token": row.view.fencing_token + 1,
                            "error_code": "stale_worker_reaped",
                            "error_detail": None,
                        }
                    )
                else:
                    self._expire(row, now, "lease_recovery_exhausted")
            return count

    async def delete_expired_rows(
        self,
        *,
        operation_retention_seconds: int,
        token_retention_seconds: int,
        audit_retention_seconds: int = 2592000,
        usage_retention_seconds: int = 7776000,
    ) -> dict[str, int]:
        async with self._lock:
            del usage_retention_seconds
            now = datetime.now(UTC)
            operation_cutoff = now - timedelta(seconds=operation_retention_seconds)
            deleted_operations = [
                operation_id
                for operation_id, row in self.operations.items()
                if row.view.status.terminal
                and row.view.completed_at is not None
                and row.view.completed_at < operation_cutoff
            ]
            for operation_id in deleted_operations:
                row = self.operations.pop(operation_id)
                self.scientific_admission_outbox.pop(operation_id, None)
                self.scientific_admissions_completed.discard(operation_id)
                self.idempotency.pop(
                    (row.view.tenant_id, row.view.principal_id, row.view.token_id, row.view.idempotency_key),
                    None,
                )
            referenced = {row.view.token_id for row in self.operations.values()}
            token_cutoff = now - timedelta(seconds=token_retention_seconds)
            deleted_tokens = [
                token_id
                for token_id, row in self.tokens.items()
                if token_id not in referenced
                and (
                    (row.view.revoked_at is not None and row.view.revoked_at < token_cutoff)
                    or (row.view.expires_at is not None and row.view.expires_at < token_cutoff)
                )
            ]
            for token_id in deleted_tokens:
                del self.tokens[token_id]
            audit_cutoff = now - timedelta(seconds=audit_retention_seconds)
            retained_audit = [event for event in self.audit if event.occurred_at >= audit_cutoff]
            deleted_audit = len(self.audit) - len(retained_audit)
            self.audit = retained_audit
            return {
                "operations": len(deleted_operations),
                "tokens": len(deleted_tokens),
                "audit": deleted_audit,
                "usage": 0,
            }

    async def list_audit(self, *, tenant_id: str | None = None, limit: int = 100) -> list[AuditEvent]:
        async with self._lock:
            rows = [row for row in reversed(self.audit) if tenant_id is None or row.tenant_id == tenant_id]
            return [row.model_copy(deep=True) for row in rows[:limit]]

    async def queue_counts(self) -> dict[tuple[str, str], int]:
        async with self._lock:
            result: dict[tuple[str, str], int] = {}
            for row in self.operations.values():
                key = (row.view.model_id, str(row.view.status))
                result[key] = result.get(key, 0) + 1
            return result

    async def oldest_queue_age(self) -> dict[str, float]:
        async with self._lock:
            now = datetime.now(UTC)
            result: dict[str, float] = {}
            for row in self.operations.values():
                if row.view.status != OperationStatus.QUEUED:
                    continue
                result[row.view.model_id] = max(
                    result.get(row.view.model_id, 0.0),
                    max(0.0, (now - row.view.accepted_at).total_seconds()),
                )
            return result

    async def terminal_accounting(self) -> list[TerminalAccounting]:
        async with self._lock:
            totals: dict[tuple[str, str, str], dict[str, float | int]] = {}
            for row in self.operations.values():
                if not row.view.status.terminal:
                    continue
                key = (row.view.model_id, row.view.protocol, row.view.outcome or str(row.view.status))
                value = totals.setdefault(
                    key,
                    {"operations": 0, "estimated_gpu_seconds": 0.0, "duration_seconds": 0.0, "cold": 0.0},
                )
                value["operations"] = int(value["operations"]) + 1
                value["estimated_gpu_seconds"] = float(value["estimated_gpu_seconds"]) + row.view.estimated_gpu_seconds
                if row.view.completed_at is not None:
                    value["duration_seconds"] = float(value["duration_seconds"]) + max(
                        0.0, (row.view.completed_at - row.view.accepted_at).total_seconds()
                    )
                value["cold"] = float(value["cold"]) + (row.view.cold_start_seconds or 0.0)
            return [
                TerminalAccounting(
                    model_id=model,
                    protocol=protocol,
                    outcome=outcome,
                    operations=int(value["operations"]),
                    estimated_gpu_seconds=float(value["estimated_gpu_seconds"]),
                    duration_seconds=float(value["duration_seconds"]),
                    cold_start_seconds=float(value["cold"]),
                )
                for (model, protocol, outcome), value in sorted(totals.items())
            ]

    async def admin_model_activity(self, model_ids: tuple[str, ...]) -> list[AdminModelActivity]:
        if (
            len(model_ids) > 256
            or len(set(model_ids)) != len(model_ids)
            or any(not 1 <= len(model_id) <= 128 for model_id in model_ids)
        ):
            raise ValueError("admin model activity request is outside the bound")
        async with self._lock:
            now = datetime.now(UTC)
            result: list[AdminModelActivity] = []
            for model_id in sorted(model_ids):
                queued = sum(
                    row.view.status == OperationStatus.QUEUED
                    for row in self.operations.values()
                    if row.view.model_id == model_id
                )
                intents = [row.view for row in self.activation_intents.values() if row.view.model_id == model_id]
                latest = max(intents, key=lambda item: (item.requested_at, str(item.id))) if intents else None
                phase = AdminActivationPhase.NONE
                if latest is not None:
                    phase = AdminActivationPhase(str(latest.status))
                result.append(
                    AdminModelActivity(
                        model_id=model_id,
                        queued_operations=queued,
                        activation_phase=phase,
                        observed_at=now,
                    )
                )
            return result

    async def admin_usage_window(self, *, from_at: datetime, to_at: datetime) -> AdminUsageWindow:
        if (
            from_at.tzinfo is None
            or to_at.tzinfo is None
            or not from_at < to_at
            or (to_at - from_at).total_seconds() > 31 * 24 * 60 * 60
        ):
            raise ValueError("admin usage window is invalid")

        def percentile(values: list[float], quantile: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            position = (len(ordered) - 1) * quantile
            lower = int(position)
            upper = min(lower + 1, len(ordered) - 1)
            fraction = position - lower
            return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

        def required_percentile(values: list[float], quantile: float) -> float:
            value = percentile(values, quantile)
            assert value is not None
            return value

        async with self._lock:
            totals: dict[str, dict[str, float | int]] = {}
            latencies: dict[str, list[float]] = {}
            all_latencies: list[float] = []
            for row in self.operations.values():
                operation = row.view
                if (
                    not operation.status.terminal
                    or operation.completed_at is None
                    or not from_at <= operation.completed_at < to_at
                ):
                    continue
                value = totals.setdefault(
                    operation.model_id,
                    {
                        "operations": 0,
                        "errors": 0,
                        "gpu": 0.0,
                        "duration": 0.0,
                        "cold": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "token_reported_operations": 0,
                    },
                )
                value["operations"] = int(value["operations"]) + 1
                if operation.status != OperationStatus.SUCCEEDED:
                    value["errors"] = int(value["errors"]) + 1
                value["gpu"] = float(value["gpu"]) + operation.estimated_gpu_seconds
                latency = max(0.0, (operation.completed_at - operation.accepted_at).total_seconds())
                value["duration"] = float(value["duration"]) + latency
                value["cold"] = float(value["cold"]) + (operation.cold_start_seconds or 0.0)
                value["input_tokens"] = int(value["input_tokens"]) + (operation.input_tokens or 0)
                value["output_tokens"] = int(value["output_tokens"]) + (operation.output_tokens or 0)
                if operation.input_tokens is not None and operation.output_tokens is not None:
                    value["token_reported_operations"] = int(value["token_reported_operations"]) + 1
                latencies.setdefault(operation.model_id, []).append(latency)
                all_latencies.append(latency)
            return AdminUsageWindow(
                from_at=from_at,
                to_at=to_at,
                rows=[
                    AdminUsageRow(
                        model_id=model_id,
                        terminal_operations=int(value["operations"]),
                        error_operations=int(value["errors"]),
                        estimated_gpu_seconds=float(value["gpu"]),
                        duration_seconds=float(value["duration"]),
                        cold_start_seconds=float(value["cold"]),
                        input_tokens=int(value["input_tokens"]),
                        output_tokens=int(value["output_tokens"]),
                        token_reported_operations=int(value["token_reported_operations"]),
                        latency_p50_seconds=required_percentile(latencies[model_id], 0.50),
                        latency_p95_seconds=required_percentile(latencies[model_id], 0.95),
                        latency_p99_seconds=required_percentile(latencies[model_id], 0.99),
                    )
                    for model_id, value in sorted(totals.items())
                ],
                latency_p50_seconds=percentile(all_latencies, 0.50),
                latency_p95_seconds=percentile(all_latencies, 0.95),
                latency_p99_seconds=percentile(all_latencies, 0.99),
            )

    @staticmethod
    def _admin_operation(row: _Operation, token: _Token) -> AdminOperationRecord:
        operation = row.view
        return AdminOperationRecord(
            id=operation.id,
            tenant_id=operation.tenant_id,
            principal_id=operation.principal_id,
            api_key_prefix=token.view.prefix,
            model_id=operation.model_id,
            model_revision=operation.model_revision,
            protocol=operation.protocol,
            operation=operation.operation,
            status=operation.status,
            accepted_at=operation.accepted_at,
            activation_started_at=operation.activation_started_at,
            ready_at=operation.ready_at,
            started_at=operation.started_at,
            completed_at=operation.completed_at,
            outcome=operation.outcome,
            semantic_outcome=operation.semantic_outcome,
            http_status=operation.http_status,
            error_code=operation.error_code,
            attempt=operation.attempt,
            max_attempts=operation.max_attempts,
            gpu_count=operation.runtime.gpu_count,
            preemptible=operation.runtime.preemptible,
            estimated_gpu_seconds=operation.estimated_gpu_seconds,
            cold_start_seconds=operation.cold_start_seconds,
            input_tokens=operation.input_tokens,
            output_tokens=operation.output_tokens,
        )

    async def admin_list_operations(self, query: AdminOperationQuery) -> list[AdminOperationRecord]:
        async with self._lock:
            rows: list[_Operation] = []
            for row in self.operations.values():
                operation = row.view
                token = self.tokens.get(operation.token_id)
                if token is None or not query.from_at <= operation.accepted_at < query.to_at:
                    continue
                if query.tenant_id is not None and operation.tenant_id != query.tenant_id:
                    continue
                if query.model_id is not None and operation.model_id != query.model_id:
                    continue
                if query.principal_id is not None and operation.principal_id != query.principal_id:
                    continue
                if query.api_key_prefix is not None and token.view.prefix != query.api_key_prefix:
                    continue
                if query.status is not None and operation.status != query.status:
                    continue
                if query.error_code is not None and operation.error_code != query.error_code:
                    continue
                if query.after_at is not None and query.after_id is not None:
                    if (operation.accepted_at, operation.id.int) >= (query.after_at, query.after_id.int):
                        continue
                rows.append(row)
            rows.sort(key=lambda item: (item.view.accepted_at, item.view.id.int), reverse=True)
            return [self._admin_operation(row, self.tokens[row.view.token_id]) for row in rows[: query.limit]]

    async def admin_get_operation(self, operation_id: UUID, *, tenant_id: str | None = None) -> AdminOperationRecord:
        async with self._lock:
            row = self.operations.get(operation_id)
            if row is None or (tenant_id is not None and row.view.tenant_id != tenant_id):
                raise NotFoundError("operation not found")
            token = self.tokens.get(row.view.token_id)
            if token is None:
                raise NotFoundError("operation not found")
            return self._admin_operation(row, token)
