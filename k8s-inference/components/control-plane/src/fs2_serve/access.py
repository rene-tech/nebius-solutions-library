"""Tenant-aware operator access, API-key lifecycle, and accounting projection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID, uuid4

from .access_models import (
    AccessMeasurement,
    AccessValueState,
    AdminApiKey,
    AdminApiKeyCreate,
    AdminApiKeyDisclosure,
    AdminApiKeyList,
    AdminApiKeyPolicyPatch,
    AdminApiKeyRotate,
    AdminApiKeyUsage,
    AdminAuditList,
    AdminPrincipalList,
    ApiKeyState,
    OperatorPrincipal,
    OperatorPrincipalCreate,
    OperatorPrincipalPatch,
    OperatorRole,
)
from .auth import TokenService
from .models import TokenIssued, TokenView
from .store import NotFoundError, Store


class AdminAccessService:
    def __init__(self, store: Store, tokens: TokenService) -> None:
        self.store = store
        self.tokens = tokens

    @staticmethod
    def _tenant(identity: OperatorPrincipal, requested: str | None) -> str | None:
        if identity.tenant_id is not None:
            if requested is not None and requested != identity.tenant_id:
                raise PermissionError("tenant is outside operator policy")
            return identity.tenant_id
        return requested

    async def _deny(
        self,
        identity: OperatorPrincipal,
        *,
        action: str,
        reason: str,
    ) -> NoReturn:
        await self.store.append_audit_event(
            actor=identity.subject,
            tenant_id=identity.tenant_id,
            token_id=None,
            action="admin.authorization",
            target_type="admin_action",
            target_id=action,
            outcome="failed",
            detail={"reason": reason},
        )
        raise PermissionError("operator authorization failed")

    async def authorize(
        self,
        identity: OperatorPrincipal,
        role: OperatorRole,
        *,
        action: str,
        tenant_id: str | None = None,
    ) -> str | None:
        try:
            identity.require(role, tenant_id=tenant_id)
            return self._tenant(identity, tenant_id)
        except PermissionError:
            return await self._deny(identity, action=action, reason="role_or_tenant")

    async def authorize_global(
        self,
        identity: OperatorPrincipal,
        role: OperatorRole,
        *,
        action: str,
    ) -> None:
        if not identity.global_access:
            await self._deny(identity, action=action, reason="global_access_required")
        await self.authorize(identity, role, action=action)

    async def authorize_resource_tenant(
        self,
        identity: OperatorPrincipal,
        role: OperatorRole,
        *,
        action: str,
        tenant_id: str,
    ) -> str:
        """Authorize an ID-addressed tenant resource without making IDs enumerable."""

        if identity.tenant_id is not None and identity.tenant_id != tenant_id:
            try:
                await self._deny(identity, action=action, reason="role_or_tenant")
            except PermissionError:
                raise NotFoundError("resource was not found") from None
        authorized = await self.authorize(identity, role, action=action, tenant_id=tenant_id)
        if authorized is None:  # pragma: no cover - tenant_id is non-null by contract
            raise RuntimeError("tenant resource authorization lost its tenant")
        return authorized

    async def list_principals(
        self,
        identity: OperatorPrincipal,
        *,
        tenant_id: str | None,
        limit: int,
    ) -> AdminPrincipalList:
        tenant = await self.authorize(
            identity,
            OperatorRole.VIEWER,
            action="principal.list",
            tenant_id=tenant_id,
        )
        rows = await self.store.list_operator_principals(
            tenant_id=tenant,
            include_global=identity.global_access,
            limit=limit,
        )
        return AdminPrincipalList(items=rows)

    async def create_principal(
        self,
        identity: OperatorPrincipal,
        request: OperatorPrincipalCreate,
    ) -> OperatorPrincipal:
        if request.tenant_id is None:
            await self.authorize_global(identity, OperatorRole.ADMIN, action="principal.create")
        else:
            await self.authorize(
                identity,
                OperatorRole.ADMIN,
                action="principal.create",
                tenant_id=request.tenant_id,
            )
        return await self.store.create_operator_principal(
            principal_id=uuid4(),
            request=request,
            actor=identity.subject,
        )

    async def update_principal(
        self,
        identity: OperatorPrincipal,
        principal_id: UUID,
        request: OperatorPrincipalPatch,
    ) -> OperatorPrincipal:
        target = await self.store.get_operator_principal(principal_id)
        if target.tenant_id is None:
            await self.authorize_global(identity, OperatorRole.ADMIN, action="principal.update")
        else:
            await self.authorize_resource_tenant(
                identity,
                OperatorRole.ADMIN,
                action="principal.update",
                tenant_id=target.tenant_id,
            )
        return await self.store.update_operator_principal(
            principal_id,
            request=request,
            actor=identity.subject,
        )

    @staticmethod
    def _key_state(token: TokenView, now: datetime) -> ApiKeyState:
        if token.rotated_at is not None:
            return ApiKeyState.ROTATED
        if token.revoked_at is not None:
            return ApiKeyState.REVOKED
        if token.expires_at is not None and token.expires_at <= now:
            return ApiKeyState.EXPIRED
        return ApiKeyState.ACTIVE

    @staticmethod
    def _available(value: float, unit: str) -> AccessMeasurement:
        return AccessMeasurement(value=value, unit=unit, state=AccessValueState.AVAILABLE)

    @staticmethod
    def _estimated(value: float, unit: str) -> AccessMeasurement:
        return AccessMeasurement(
            value=value,
            unit=unit,
            state=AccessValueState.ESTIMATED,
            reason="admission reservation accounting",
        )

    @staticmethod
    def _unavailable(unit: str, reason: str) -> AccessMeasurement:
        return AccessMeasurement(value=None, unit=unit, state=AccessValueState.UNAVAILABLE, reason=reason)

    async def _project_keys(self, tokens: list[TokenView], *, tenant_id: str | None) -> list[AdminApiKey]:
        usage = {
            row.token_id: row
            for row in await self.store.admin_key_usage(tuple(item.id for item in tokens), tenant_id=tenant_id)
        }
        now = datetime.now(UTC)
        result: list[AdminApiKey] = []
        for token in tokens:
            row = usage.get(token.id)
            operations = row.terminal_operations if row is not None else 0
            token_coverage = row.token_reported_operations if row is not None else 0
            modality_coverage = row.modality_reported_operations if row is not None else 0
            token_complete = token_coverage == operations
            modality_complete = modality_coverage == operations
            key_usage = AdminApiKeyUsage(
                terminal_operations=operations,
                estimated_gpu_seconds=self._estimated(
                    row.estimated_gpu_seconds if row is not None else 0,
                    "gpu-seconds",
                ),
                input_tokens=(
                    self._available(float((row.input_tokens if row is not None else None) or 0), "tokens")
                    if token_complete
                    else self._unavailable("tokens", "runtime token reporting is incomplete")
                ),
                output_tokens=(
                    self._available(float((row.output_tokens if row is not None else None) or 0), "tokens")
                    if token_complete
                    else self._unavailable("tokens", "runtime token reporting is incomplete")
                ),
                token_reported_operations=token_coverage,
                modality_reported_operations=modality_coverage,
                modality_units=(row.modality_units if row is not None and modality_complete else []),
                modality_state=(AccessValueState.AVAILABLE if modality_complete else AccessValueState.UNAVAILABLE),
                modality_reason=None if modality_complete else "runtime modality reporting is incomplete",
            )
            result.append(
                AdminApiKey(
                    id=token.id,
                    name=token.name,
                    prefix=token.prefix,
                    fingerprint=token.fingerprint,
                    principal_id=token.principal_id,
                    tenant_id=token.tenant_id,
                    scopes=token.scopes,
                    models=token.models,
                    state=self._key_state(token, now),
                    expires_at=token.expires_at,
                    last_used_at=token.last_used_at,
                    request_budget=token.request_budget,
                    requests_used=token.requests_used,
                    gpu_seconds_budget=token.gpu_seconds_budget,
                    gpu_seconds_used=token.gpu_seconds_used,
                    gpu_seconds_reserved=token.gpu_seconds_reserved,
                    max_concurrency=token.max_concurrency,
                    rate_limit_requests=token.rate_limit_requests,
                    rate_window_seconds=token.rate_window_seconds,
                    rate_window_started_at=token.rate_window_started_at,
                    rate_window_requests=token.rate_window_requests,
                    rotation_parent_id=token.rotation_parent_id,
                    rotated_at=token.rotated_at,
                    created_at=token.created_at,
                    created_by=token.created_by,
                    revoked_at=token.revoked_at,
                    usage=key_usage,
                )
            )
        return result

    async def list_keys(
        self,
        identity: OperatorPrincipal,
        *,
        tenant_id: str | None,
        limit: int,
    ) -> AdminApiKeyList:
        tenant = await self.authorize(
            identity,
            OperatorRole.VIEWER,
            action="token.list",
            tenant_id=tenant_id,
        )
        tokens = await self.tokens.list(tenant_id=tenant, limit=limit)
        return AdminApiKeyList(items=await self._project_keys(tokens, tenant_id=tenant))

    async def issue_key(
        self,
        identity: OperatorPrincipal,
        request: AdminApiKeyCreate,
    ) -> AdminApiKeyDisclosure:
        tenant = await self.authorize(
            identity,
            OperatorRole.OPERATOR,
            action="token.issue",
            tenant_id=request.tenant_id,
        )
        try:
            issued = await self.tokens.issue(request, created_by=identity.subject)
        except Exception as exc:
            await self._audit_key_failure(
                identity,
                action="token.issue",
                reason=self._failure_reason(exc),
                tenant_id=request.tenant_id,
            )
            raise
        return await self._disclosure(issued, tenant_id=tenant)

    async def rotate_key(
        self,
        identity: OperatorPrincipal,
        token_id: UUID,
        request: AdminApiKeyRotate,
    ) -> AdminApiKeyDisclosure:
        try:
            existing = await self.store.get_token(token_id)
        except NotFoundError:
            await self._audit_key_failure(identity, action="token.rotate", reason="not_found")
            raise
        tenant = await self.authorize_resource_tenant(
            identity,
            OperatorRole.OPERATOR,
            action="token.rotate",
            tenant_id=existing.tenant_id,
        )
        try:
            issued = await self.tokens.rotate(
                token_id,
                actor=identity.subject,
                name=request.name,
                expires_at=request.expires_at,
            )
        except Exception as exc:
            await self._audit_key_failure(
                identity,
                action="token.rotate",
                reason=self._failure_reason(exc),
                tenant_id=existing.tenant_id,
            )
            raise
        return await self._disclosure(issued, tenant_id=tenant)

    async def revoke_key(self, identity: OperatorPrincipal, token_id: UUID) -> AdminApiKey:
        try:
            existing = await self.store.get_token(token_id)
        except NotFoundError:
            await self._audit_key_failure(identity, action="token.revoke", reason="not_found")
            raise
        tenant = await self.authorize_resource_tenant(
            identity,
            OperatorRole.OPERATOR,
            action="token.revoke",
            tenant_id=existing.tenant_id,
        )
        try:
            revoked = await self.tokens.revoke(token_id, actor=identity.subject)
        except Exception as exc:
            await self._audit_key_failure(
                identity,
                action="token.revoke",
                reason=self._failure_reason(exc),
                tenant_id=existing.tenant_id,
            )
            raise
        return (await self._project_keys([revoked], tenant_id=tenant))[0]

    async def update_key_policy(
        self,
        identity: OperatorPrincipal,
        token_id: UUID,
        request: AdminApiKeyPolicyPatch,
    ) -> AdminApiKey:
        try:
            existing = await self.store.get_token(token_id)
        except NotFoundError:
            await self._audit_key_failure(identity, action="token.policy.update", reason="not_found")
            raise
        tenant = await self.authorize_resource_tenant(
            identity,
            OperatorRole.OPERATOR,
            action="token.policy.update",
            tenant_id=existing.tenant_id,
        )
        try:
            updated = await self.store.update_token_policy(
                token_id,
                request=request,
                actor=identity.subject,
            )
        except Exception as exc:
            await self._audit_key_failure(
                identity,
                action="token.policy.update",
                reason=self._failure_reason(exc),
                tenant_id=existing.tenant_id,
            )
            raise
        return (await self._project_keys([updated], tenant_id=tenant))[0]

    async def _disclosure(self, issued: TokenIssued, *, tenant_id: str | None) -> AdminApiKeyDisclosure:
        projected = await self._project_keys(
            [TokenView.model_validate(issued.model_dump(exclude={"token"}))],
            tenant_id=tenant_id,
        )
        return AdminApiKeyDisclosure(key=projected[0], secret=issued.token)

    @staticmethod
    def _failure_reason(exc: Exception) -> str:
        if isinstance(exc, NotFoundError):
            return "not_found"
        if isinstance(exc, ValueError):
            return "invalid_request"
        return "operation_failed"

    async def _audit_key_failure(
        self,
        identity: OperatorPrincipal,
        *,
        action: str,
        reason: str,
        tenant_id: str | None = None,
    ) -> None:
        await self.store.append_audit_event(
            actor=identity.subject,
            tenant_id=tenant_id if tenant_id is not None else identity.tenant_id,
            token_id=None,
            action=action,
            target_type="token",
            target_id="unresolved",
            outcome="failed",
            detail={"reason": reason},
        )

    async def list_audit(
        self,
        identity: OperatorPrincipal,
        *,
        tenant_id: str | None,
        limit: int,
    ) -> AdminAuditList:
        tenant = await self.authorize(
            identity,
            OperatorRole.VIEWER,
            action="audit.list",
            tenant_id=tenant_id,
        )
        return AdminAuditList(items=await self.store.list_audit(tenant_id=tenant, limit=limit))
