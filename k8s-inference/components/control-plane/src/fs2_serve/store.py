"""Persistence contract shared by PostgreSQL and deterministic tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from .access_models import (
    AdminApiKeyPolicyPatch,
    AdminKeyUsageRecord,
    OperatorPrincipal,
    OperatorPrincipalCreate,
    OperatorPrincipalPatch,
    OperatorSession,
    OperatorSessionRecord,
)
from .admin_models import (
    AdminModelActivity,
    AdminOperationQuery,
    AdminOperationRecord,
    AdminUsageWindow,
)
from .configuration_models import (
    ConfigurationPlan,
    ConfigurationRevision,
    PlatformConfiguration,
    ReconciliationStatus,
    TerraformApplyReceipt,
)
from .model_deployment_records import (
    ModelDeploymentAppendRequest,
    ModelDeploymentAppendResult,
    ModelDeploymentRevision,
    ModelDeploymentStatusObservation,
)
from .models import (
    ActivationIntent,
    ActivationLeaderIdentity,
    ActivationTargetState,
    AdmissionRequest,
    AuditEvent,
    ClaimedActivationIntent,
    ClaimedOperation,
    OperationResult,
    OperationStatus,
    OperationView,
    Principal,
    ReportedUsage,
    RuntimeIdentity,
    TerminalAccounting,
    TokenCreate,
    TokenView,
)


class StoreError(RuntimeError):
    code = "store_error"


class ConflictError(StoreError):
    code = "conflict"


class BudgetExceededError(StoreError):
    code = "budget_exceeded"


class ConcurrencyExceededError(StoreError):
    code = "concurrency_exceeded"


class RateLimitExceededError(StoreError):
    code = "rate_limit_exceeded"


class NotFoundError(StoreError):
    code = "not_found"


class StaleLeaseError(StoreError):
    code = "stale_lease"


class Store(Protocol):
    async def close(self) -> None: ...

    async def ping(self) -> bool: ...

    async def activation_controller_ready(self, activation_set_digest: str) -> bool: ...

    async def migrate(self) -> None: ...

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
    ) -> TokenView: ...

    async def token_for_verification(self, token_id: UUID) -> tuple[TokenView, str] | None: ...

    async def get_token(self, token_id: UUID) -> TokenView: ...

    async def rehash_token(self, token_id: UUID, *, pepper_key_id: str, digest: str) -> None: ...

    async def list_tokens(self, *, tenant_id: str | None = None, limit: int = 200) -> list[TokenView]: ...

    async def record_token_expired(self, token_id: UUID, *, actor: str) -> None: ...

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
    ) -> TokenView: ...

    async def revoke_token(self, token_id: UUID, *, actor: str) -> TokenView: ...

    async def update_token_policy(
        self,
        token_id: UUID,
        *,
        request: AdminApiKeyPolicyPatch,
        actor: str,
    ) -> TokenView: ...

    async def list_operator_principals(
        self, *, tenant_id: str | None, include_global: bool, limit: int
    ) -> list[OperatorPrincipal]: ...

    async def get_operator_principal(self, principal_id: UUID) -> OperatorPrincipal: ...

    async def create_operator_principal(
        self, *, principal_id: UUID, request: OperatorPrincipalCreate, actor: str
    ) -> OperatorPrincipal: ...

    async def update_operator_principal(
        self, principal_id: UUID, *, request: OperatorPrincipalPatch, actor: str
    ) -> OperatorPrincipal: ...

    async def create_operator_session(
        self,
        *,
        session_id: UUID,
        principal_id: UUID,
        pepper_key_id: str,
        digest: str,
        expires_at: datetime,
        actor: str,
    ) -> OperatorSession: ...

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
    ) -> OperatorSession: ...

    async def operator_session_for_verification(self, session_id: UUID) -> OperatorSessionRecord | None: ...

    async def touch_operator_session(self, session_id: UUID, *, seen_at: datetime) -> None: ...

    async def revoke_operator_session(self, session_id: UUID, *, actor: str) -> OperatorSession: ...

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
    ) -> None: ...

    async def configuration_current(self) -> ConfigurationRevision | None: ...

    async def configuration_get_revision(self, revision: int) -> ConfigurationRevision | None: ...

    async def configuration_ensure_initial(
        self,
        configuration: PlatformConfiguration,
        *,
        actor: str,
    ) -> ConfigurationRevision: ...

    async def configuration_accept_terraform_applied(
        self,
        configuration: PlatformConfiguration,
        receipt: TerraformApplyReceipt,
        *,
        actor: str,
    ) -> ConfigurationRevision: ...

    async def configuration_save_plan(self, plan: ConfigurationPlan) -> None: ...

    async def configuration_get_plan(self, plan_id: UUID) -> ConfigurationPlan | None: ...

    async def configuration_save_status(self, status: ReconciliationStatus) -> None: ...

    async def configuration_get_status(self, reconciliation_id: UUID) -> ReconciliationStatus | None: ...

    async def model_deployment_append_revision(
        self,
        request: ModelDeploymentAppendRequest,
    ) -> ModelDeploymentAppendResult: ...

    async def model_deployment_list(
        self,
        *,
        namespace: str,
        tenant_id: str | None,
        after_name: str | None,
        limit: int,
    ) -> list[ModelDeploymentRevision]: ...

    async def model_deployment_current(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentRevision | None: ...

    async def model_deployment_history(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
        before_revision: int | None,
        limit: int,
    ) -> list[ModelDeploymentRevision]: ...

    async def model_deployment_append_status(
        self,
        observation: ModelDeploymentStatusObservation,
    ) -> ModelDeploymentStatusObservation: ...

    async def model_deployment_status(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentStatusObservation | None: ...

    async def admin_key_usage(
        self, token_ids: tuple[UUID, ...], *, tenant_id: str | None
    ) -> list[AdminKeyUsageRecord]: ...

    async def append_operation(
        self,
        *,
        principal: Principal,
        admission: AdmissionRequest,
        model_revision: str,
        reserved_gpu_seconds: float,
        max_attempts: int,
    ) -> OperationView: ...

    async def get_operation(self, operation_id: UUID, *, tenant_id: str | None = None) -> OperationView: ...

    async def get_operation_result(self, operation_id: UUID, *, tenant_id: str) -> OperationResult: ...

    async def claim_operation(self, worker_id: str, *, lease_seconds: float) -> ClaimedOperation | None: ...

    async def ensure_activation_intent(
        self,
        operation: ClaimedOperation,
        *,
        binding_digest: str,
        worker_id: str,
        fencing_token: int,
    ) -> ActivationIntent: ...

    async def get_activation_intent(self, operation_id: UUID) -> ActivationIntent: ...

    async def claim_activation_intent(
        self,
        identity: ActivationLeaderIdentity,
        *,
        leadership_fencing_token: int,
        lease_seconds: float,
    ) -> ClaimedActivationIntent | None: ...

    async def heartbeat_activation_intent(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        lease_seconds: float,
    ) -> None: ...

    async def activation_wait_budget(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        maximum_seconds: float,
    ) -> float: ...

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
    ) -> ActivationIntent: ...

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
    ) -> ActivationIntent: ...

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
    ) -> ActivationIntent | None: ...

    async def get_activation_target_state(self, model_id: str) -> ActivationTargetState | None: ...

    def activation_mutation_guard(
        self,
        intent: ClaimedActivationIntent,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
    ) -> AbstractAsyncContextManager[None]: ...

    async def read_request_payload(self, operation_id: UUID, *, worker_id: str, fencing_token: int) -> bytes: ...

    async def heartbeat(
        self, operation_id: UUID, *, worker_id: str, fencing_token: int, lease_seconds: float
    ) -> None: ...

    async def mark_ready(self, operation_id: UUID, *, worker_id: str, fencing_token: int) -> None: ...

    async def mark_running(
        self, operation_id: UUID, runtime: RuntimeIdentity, *, worker_id: str, fencing_token: int
    ) -> None: ...

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
    ) -> OperationView: ...

    async def retry_operation(
        self,
        operation_id: UUID,
        *,
        worker_id: str,
        fencing_token: int,
        available_at: datetime,
        error_code: str,
        error_detail: str,
    ) -> OperationView: ...

    async def release_operation(self, operation_id: UUID, *, worker_id: str, fencing_token: int) -> OperationView: ...

    async def cancel_operation(self, operation_id: UUID, *, tenant_id: str, actor: str) -> OperationView: ...

    async def purge_operation_payload(self, operation_id: UUID, *, tenant_id: str) -> None: ...

    async def purge_expired_payloads(self) -> int: ...

    async def expire_deadline_operations(self) -> int: ...

    async def reap_stale_operations(self) -> int: ...

    async def delete_expired_rows(
        self,
        *,
        operation_retention_seconds: int,
        token_retention_seconds: int,
        audit_retention_seconds: int = 2592000,
        usage_retention_seconds: int = 7776000,
    ) -> dict[str, int]: ...

    async def list_audit(self, *, tenant_id: str | None = None, limit: int = 100) -> list[AuditEvent]: ...

    async def queue_counts(self) -> dict[tuple[str, str], int]: ...

    async def oldest_queue_age(self) -> dict[str, float]: ...

    async def terminal_accounting(self) -> list[TerminalAccounting]: ...

    async def admin_model_activity(self, model_ids: tuple[str, ...]) -> list[AdminModelActivity]: ...

    async def admin_usage_window(self, *, from_at: datetime, to_at: datetime) -> AdminUsageWindow: ...

    async def admin_list_operations(self, query: AdminOperationQuery) -> list[AdminOperationRecord]: ...

    async def admin_get_operation(
        self, operation_id: UUID, *, tenant_id: str | None = None
    ) -> AdminOperationRecord: ...


@asynccontextmanager
async def store_lifespan(store: Store) -> AsyncIterator[Store]:
    try:
        yield store
    finally:
        await store.close()
