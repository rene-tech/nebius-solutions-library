"""Owner settings constrain invocation while key lifecycle stays in one service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from .access import AdminAccessService
from .access_models import AdminApiKeyCreate, AdminApiKeyDisclosure, OperatorPrincipal, OperatorRole
from .admin_models import AdminContext
from .models import Principal, Scope
from .store import ConflictError, NotFoundError
from .user_models import InferenceUser, UserAppChoice, UserCreate, UserDetail, UserList, UserPatch, UserRow, owner_id
from .user_repository import UserRepository


class UserService:
    def __init__(
        self,
        repository: UserRepository,
        access: AdminAccessService,
        *,
        app_catalog: Callable[[], Awaitable[list[UserAppChoice]]] | None = None,
    ) -> None:
        self.repository = repository
        self.access = access
        self.app_catalog = app_catalog

    async def apps(self) -> list[UserAppChoice]:
        return await self.app_catalog() if self.app_catalog else []

    async def _validate_apps(self, app_ids: list[UUID] | None) -> None:
        if app_ids is not None:
            choices = {app.app_id for app in await self.apps()}
            if not set(app_ids) <= choices:
                raise ValueError("one or more selected apps do not exist")

    async def _get(self, identity: OperatorPrincipal, user_id: UUID, role: OperatorRole) -> InferenceUser:
        await self.access.authorize(identity, role, action="user.read", tenant_id=identity.tenant_id)
        for user in await self.repository.list(identity.tenant_id):
            if user.id == user_id:
                return user
        raise NotFoundError("inference user was not found")

    async def _row(self, user: InferenceUser, context: AdminContext) -> UserRow:
        keys = await self.repository.keys(user.tenant_id, user.principal_id)
        now = datetime.now(UTC)
        return UserRow(
            **user.model_dump(),
            key_count=len(keys),
            active_key_count=sum(
                key.revoked_at is None and key.rotated_at is None and (key.expires_at is None or key.expires_at > now)
                for key in keys
            ),
            usage=await self.repository.usage(user.tenant_id, user.principal_id, context),
        )

    async def list(
        self, identity: OperatorPrincipal, context: AdminContext, *, tenant_id: str | None = None, limit: int = 200
    ) -> UserList:
        tenant = await self.access.authorize(identity, OperatorRole.VIEWER, action="user.list", tenant_id=tenant_id)
        users = await self.repository.list(tenant)
        rows: list[UserRow] = []
        for index in range(0, min(limit, len(users)), 10):
            rows.extend(
                await asyncio.gather(*(self._row(user, context) for user in users[index : min(index + 10, limit)]))
            )
        return UserList(items=rows, limit=limit, truncated=len(users) > limit, apps=await self.apps())

    async def detail(self, identity: OperatorPrincipal, user_id: UUID, context: AdminContext) -> UserDetail:
        user = await self._get(identity, user_id, OperatorRole.VIEWER)
        tokens = await self.repository.keys(user.tenant_id, user.principal_id)
        keys = await self.access._project_keys(
            [token for token in tokens if token.principal_id == user.principal_id], tenant_id=user.tenant_id
        )
        return UserDetail(user=await self._row(user, context), keys=keys, apps=await self.apps())

    async def create(self, identity: OperatorPrincipal, request: UserCreate) -> InferenceUser:
        await self.access.authorize(identity, OperatorRole.OPERATOR, action="user.create", tenant_id=request.tenant_id)
        await self._validate_apps(request.app_ids)
        if any(user.principal_id == request.principal_id for user in await self.repository.list(request.tenant_id)):
            raise ConflictError("inference owner already exists; edit that owner instead")
        now = datetime.now(UTC)
        return await self.repository.save(
            InferenceUser(
                **request.model_dump(),
                id=owner_id(request.tenant_id, request.principal_id),
                source="configured",
                created_at=now,
                updated_at=now,
            ),
            create=True,
        )

    async def update(self, identity: OperatorPrincipal, user_id: UUID, request: UserPatch) -> InferenceUser:
        user = await self._get(identity, user_id, OperatorRole.OPERATOR)
        changes = request.model_dump(exclude_unset=True)
        if "display_name" in changes and changes["display_name"] is None:
            raise ValueError("display name cannot be null")
        if "enabled" in changes and changes["enabled"] is None:
            raise ValueError("enabled cannot be null")
        if "app_ids" in changes:
            await self._validate_apps(request.app_ids)
        changes.update(updated_at=datetime.now(UTC), source="configured")
        return await self.repository.save(InferenceUser.model_validate({**user.model_dump(), **changes}))

    async def issue_key(
        self, identity: OperatorPrincipal, user_id: UUID, request: AdminApiKeyCreate
    ) -> AdminApiKeyDisclosure:
        user = await self._get(identity, user_id, OperatorRole.OPERATOR)
        if request.tenant_id != user.tenant_id or request.principal_id != user.principal_id:
            raise ValueError("key ownership must match the selected inference user")
        return await self.access.issue_key(identity, request)

    async def constrain_principal(self, principal: Principal) -> Principal:
        """Fresh owner policy on every PAT verification; never expand a key."""
        user = await self.repository.configured(principal.tenant_id, principal.principal_id)
        if user is None:
            return principal
        scopes = principal.scopes
        if not user.enabled:
            scopes = scopes - {Scope.INFERENCE_INVOKE, Scope.MCP_INVOKE}
        models = principal.models
        if user.app_ids is not None or user.academic_eligible is False:
            apps = await self.apps()
            if user.app_ids is not None:
                allowed = {app.public_model_id for app in apps if app.app_id in user.app_ids}
                models = frozenset(allowed if "*" in models else models & allowed)
            if user.academic_eligible is False:
                academic = {app.public_model_id for app in apps if app.academic_required}
                models = frozenset(
                    {app.public_model_id for app in apps} - academic if "*" in models else models - academic
                )
        return principal.model_copy(update={"scopes": frozenset(scopes), "models": models})

    async def require_app(self, principal: Principal, app_id: UUID, academic_required: bool) -> None:
        user = await self.repository.configured(principal.tenant_id, principal.principal_id)
        if user is None:
            return
        if not user.enabled:
            raise PermissionError("inference user is disabled")
        if user.app_ids is not None and app_id not in user.app_ids:
            raise PermissionError("app is outside inference user policy")
        if academic_required and user.academic_eligible is False:
            raise PermissionError("inference user is not academically eligible")
