"""Crash-safe reconciliation of customer-owned storage."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from .store import ConflictError
from .user_models import InferenceUser, owner_id
from .user_storage_models import StoragePolicy, UserStorage

LOG = logging.getLogger(__name__)
_ACTIVE = "ACTIVE"
_INACTIVE = {"INACTIVE", "EXPIRED", "DELETING", "DELETED"}


class UserStorageService:
    def __init__(
        self,
        repository: Any,
        provider: Any,
        users: Any,
        *,
        default: StoragePolicy | None = None,
        excluded_tenants: tuple[str, ...] = (),
        poll_seconds: float = 60,
        action_timeout_seconds: float = 30,
        rotation_window_days: int = 14,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.users = users
        self.default = default or StoragePolicy()
        self.excluded_tenants = frozenset(excluded_tenants)
        self.poll_seconds = poll_seconds
        self.action_timeout_seconds = action_timeout_seconds
        self.rotation_window_days = rotation_window_days
        self.task: asyncio.Task[None] | None = None
        self.provisioning_retry_at = 0.0

    async def policy(self, tenant: str) -> StoragePolicy:
        if tenant in self.excluded_tenants:
            return StoragePolicy(mode="disabled", quota_bytes=self.default.quota_bytes)
        return StoragePolicy.model_validate(await self.repository.policy(tenant, self.default))

    async def configure(self, tenant: str, policy: StoragePolicy) -> StoragePolicy:
        if tenant in self.excluded_tenants and policy.mode != "disabled":
            raise ConflictError("tenant is explicitly excluded from bucket provisioning")
        async with self.repository.tenant_lock(tenant):
            await self.repository.set_policy(tenant, policy, self.default)
        return await self.policy(tenant)

    async def view(self, tenant: str, principal: str) -> UserStorage:
        return UserStorage.model_validate(await self.repository.view(tenant, principal, await self.policy(tenant)))

    async def _provider_state(self, resource_id: str) -> str:
        state = await self.provider.key_state(resource_id)
        if state not in {_ACTIVE, *_INACTIVE}:
            raise RuntimeError("provider returned an indeterminate storage-key state")
        return state

    async def _set_provider_state(self, resource_id: str, enabled: bool) -> None:
        state = await self._provider_state(resource_id)
        if enabled and state != _ACTIVE:
            if state in {"EXPIRED", "DELETING", "DELETED"}:
                raise RuntimeError("expired or deleted storage key cannot be activated")
            await self.provider.set_enabled(resource_id, True)
        elif not enabled and state not in _INACTIVE:
            await self.provider.set_enabled(resource_id, False)
        observed = await self._provider_state(resource_id)
        if enabled and observed != _ACTIVE:
            raise RuntimeError("storage key activation did not reach ACTIVE")
        if not enabled and observed not in _INACTIVE:
            raise RuntimeError("storage key deactivation did not reach a fail-closed state")

    async def _rotation_step(
        self,
        user: InferenceUser,
        credential: dict[str, Any],
        bucket: dict[str, Any],
    ) -> dict[str, Any]:
        tenant = user.tenant_id
        principal = user.principal_id
        predecessor = credential["previous_access_key_resource_id"]
        replacement = credential["replacement_access_key_resource_id"]
        if predecessor is not None:
            # Adoption of a historical/unbounded key persists the inactive
            # replacement as the current row with the old resource retained
            # as predecessor. Activate only after that durable write, then
            # retire the predecessor. A crash between these calls can leave
            # two active keys only until the next bounded reconciliation pass.
            current = credential["access_key_resource_id"]
            state = await self._provider_state(current)
            if state != _ACTIVE:
                if state != "INACTIVE":
                    raise RuntimeError("persisted storage replacement is no longer activatable")
                await self.provider.set_enabled(current, True)
            await self._set_provider_state(predecessor, False)
            await self.repository.complete_rotation(
                tenant,
                principal,
                expected_version=credential["version"],
            )
            return await self.repository.credential(tenant, principal)

        if replacement is None:
            value = await self.provider.prepare_rotation(
                tenant,
                principal,
                bucket["group_id"],
                credential,
            )
            if value["expires_at"] <= datetime.now(UTC):
                raise RuntimeError("provider prepared an expired storage key")
            await self.repository.stage_replacement(
                tenant,
                principal,
                value,
                expected_version=credential["version"],
            )
            return await self.repository.credential(tenant, principal)

        state = await self._provider_state(replacement)
        if state in {"EXPIRED", "DELETING", "DELETED"}:
            raise RuntimeError("staged storage replacement is no longer activatable")
        if state != _ACTIVE:
            await self.provider.set_enabled(replacement, True)
        try:
            await self.repository.promote_replacement(
                tenant,
                principal,
                expected_version=credential["version"],
            )
        except BaseException:
            # If PostgreSQL rejects the CAS, the predecessor remains current.
            # Compensate the newly activated key so no unowned active key lasts
            # beyond this bounded cutover attempt.
            with suppress(Exception):
                await self.provider.set_enabled(replacement, False)
            raise
        return await self.repository.credential(tenant, principal)

    async def _finish_pending(
        self,
        user: InferenceUser,
        credential: dict[str, Any],
        bucket: dict[str, Any],
    ) -> dict[str, Any]:
        # One ensure pass may finish every already-durable step, while every
        # individual mutation remains retryable after a crash.
        for _ in range(8):
            action = credential["requested_action"]
            if action is None:
                return credential
            if action == "rotate":
                credential = await self._rotation_step(user, credential, bucket)
                continue
            target = action == "enable" and credential["desired_enabled"] and user.enabled
            if target and (await self.policy(user.tenant_id)).mode == "disabled":
                target = False
            try:
                await self._set_provider_state(credential["access_key_resource_id"], target)
                await self.repository.complete_action(
                    user.tenant_id,
                    user.principal_id,
                    expected_action=action,
                    enabled=target,
                    revoked=action == "revoke",
                    expected_version=credential["version"],
                )
            except BaseException:
                # Enabling is the only unsafe direction after a DB failure.
                if target:
                    with suppress(Exception):
                        await self.provider.set_enabled(credential["access_key_resource_id"], False)
                raise
            return await self.repository.credential(user.tenant_id, user.principal_id)
        raise RuntimeError("storage credential transition exceeded its bounded reconciliation steps")

    async def ensure(self, user: InferenceUser) -> None:
        if self.provider is None:
            raise RuntimeError("customer storage cloud operations are isolated to the reconciler")
        async with self.repository.tenant_lock(user.tenant_id):
            policy = await self.policy(user.tenant_id)
            credential = await self.repository.credential(user.tenant_id, user.principal_id)
            if credential and await self.repository.reencrypt_if_needed(user.tenant_id, user.principal_id):
                credential = await self.repository.credential(user.tenant_id, user.principal_id)

            if credential and not user.enabled and credential["desired_enabled"]:
                await self.repository.request_enabled(user.tenant_id, user.principal_id, False)
                credential = await self.repository.credential(user.tenant_id, user.principal_id)
            elif credential and policy.mode == "disabled" and credential["enabled"]:
                await self.repository.request_suspended(user.tenant_id, user.principal_id)
                credential = await self.repository.credential(user.tenant_id, user.principal_id)

            owner = user.principal_id if policy.mode == "user" else ""
            if credential is not None:
                owner = credential["owner_key"]
            bucket = await self.repository.bucket(user.tenant_id, owner)

            # Pending actions always precede serial user inventory and policy
            # drift checks. A disabled/revoked owner is never auto-rotated.
            if credential and credential["requested_action"] is not None:
                if bucket is None:
                    raise ConflictError("storage action references missing bucket metadata")
                if policy.mode == "disabled" and credential["requested_action"] in {"enable", "rotate"}:
                    # Do not overwrite a durable enable/rotation request, but
                    # fail closed while a tenant policy or legacy-layout gate
                    # is disabled. Any already staged/current/predecessor key
                    # is inactive before reconciliation pauses.
                    for resource_id in {
                        credential["access_key_resource_id"],
                        credential["replacement_access_key_resource_id"],
                        credential["previous_access_key_resource_id"],
                    }:
                        if resource_id is not None:
                            await self._set_provider_state(resource_id, False)
                    return
                credential = await self._finish_pending(user, credential, bucket)
                if credential["requested_action"] is not None:
                    return

            if (
                credential
                and user.enabled
                and policy.mode != "disabled"
                and credential["desired_enabled"]
                and not credential["enabled"]
            ):
                await self.repository.request_enabled(user.tenant_id, user.principal_id, True)
                credential = await self.repository.credential(user.tenant_id, user.principal_id)
                assert bucket is not None
                credential = await self._finish_pending(user, credential, bucket)

            if credential and user.enabled and policy.mode != "disabled":
                provider_state = await self._provider_state(credential["access_key_resource_id"])
                if credential["enabled"] and provider_state != _ACTIVE:
                    if await self.repository.request_rotation_for_provider_state(
                        user.tenant_id,
                        user.principal_id,
                    ):
                        credential = await self.repository.credential(user.tenant_id, user.principal_id)
                        assert bucket is not None
                        credential = await self._finish_pending(user, credential, bucket)
                cutoff = datetime.now(UTC) + timedelta(days=self.rotation_window_days)
                if await self.repository.request_rotation_if_due(
                    user.tenant_id,
                    user.principal_id,
                    cutoff,
                ):
                    credential = await self.repository.credential(user.tenant_id, user.principal_id)
                    assert bucket is not None
                    credential = await self._finish_pending(user, credential, bucket)

            enabled = (
                user.enabled
                and policy.mode != "disabled"
                and (bool(credential["desired_enabled"]) if credential is not None else True)
                and (credential is None or credential["revoked_at"] is None)
            )
            if not enabled:
                return
            if credential is None and time.monotonic() < self.provisioning_retry_at:
                return

            # Always inspect the bucket and its exact policy; quota equality is
            # not sufficient evidence that IAM drift has not occurred.
            bucket = await self.provider.ensure_bucket(
                user.tenant_id,
                owner,
                policy.quota_bytes,
                existing=bucket,
            )
            await self.repository.save_bucket(user.tenant_id, owner, bucket)
            if credential is None:
                value = await self.provider.ensure_credentials(
                    user.tenant_id,
                    user.principal_id,
                    bucket["group_id"],
                )
                await self.repository.save_credential(user.tenant_id, user.principal_id, owner, value)
                credential = await self.repository.credential(user.tenant_id, user.principal_id)
                assert credential is not None
                await self._finish_pending(user, credential, bucket)
            else:
                await self.provider.ensure_identity_access(
                    bucket["group_id"],
                    credential["service_account_id"],
                )
                state = await self._provider_state(credential["access_key_resource_id"])
                if credential["enabled"] and state != _ACTIVE:
                    raise RuntimeError("database-enabled storage key is not ACTIVE at the provider")

    async def _pending_users(self, inventory: list[InferenceUser]) -> list[InferenceUser]:
        users = {(item.tenant_id, item.principal_id): item for item in inventory}
        pending: list[InferenceUser] = []
        for tenant, principal in await self.repository.pending_principals():
            item = users.pop((tenant, principal), None)
            if item is None:
                item = await self.users.configured(tenant, principal)
            if item is None:
                now = datetime.now(UTC)
                item = InferenceUser(
                    id=owner_id(tenant, principal),
                    tenant_id=tenant,
                    principal_id=principal,
                    display_name=principal,
                    enabled=False,
                    source="configured",
                    created_at=now,
                    updated_at=now,
                )
            pending.append(item)
        return [*pending, *users.values()]

    async def reconcile_once(self) -> dict[str, int]:
        counts = {"ready": 0, "skipped": 0, "failed": 0}
        inventory = list(await self.users.list(None))
        for user in await self._pending_users(inventory):
            try:
                if user.source == "existing-key-owner":
                    keys = await self.users.keys(user.tenant_id, user.principal_id)
                    now = datetime.now(UTC)
                    active = any(
                        key.revoked_at is None
                        and key.rotated_at is None
                        and (key.expires_at is None or key.expires_at > now)
                        for key in keys
                    )
                    if not active and not await self.repository.credential(user.tenant_id, user.principal_id):
                        counts["skipped"] += 1
                        continue
                async with asyncio.timeout(120):
                    await self.ensure(user)
                state = (await self.view(user.tenant_id, user.principal_id)).state
                counts["ready" if state == "ready" else "skipped"] += 1
            except Exception as exc:
                code = getattr(exc, "code", "") or getattr(
                    getattr(getattr(exc, "status", None), "code", None), "name", ""
                )
                if code == "RESOURCE_EXHAUSTED":
                    self.provisioning_retry_at = time.monotonic() + 300
                LOG.warning(
                    "user storage reconciliation failed user_id=%s error_type=%s cloud_code=%s operation_id=%s",
                    user.id,
                    type(exc).__name__,
                    code,
                    getattr(exc, "operation_id", ""),
                )
                counts["failed"] += 1
        return counts

    def start(self) -> None:
        if self.provider is None:
            raise RuntimeError("customer storage reconciler requires cloud credentials")
        self.task = asyncio.create_task(self._run(), name="customer-storage")

    async def rotate(self, tenant: str, principal: str) -> UserStorage:
        await self.repository.request_action(tenant, principal, "rotate")
        try:
            await self.repository.wait_action(tenant, principal, timeout=self.action_timeout_seconds)
        except TimeoutError:
            raise RuntimeError("storage reconciler did not complete credential rotation") from None
        return await self.view(tenant, principal)

    async def revoke(self, tenant: str, principal: str) -> UserStorage:
        await self.repository.request_action(tenant, principal, "revoke")
        try:
            await self.repository.wait_action(tenant, principal, timeout=self.action_timeout_seconds)
        except TimeoutError:
            raise RuntimeError("storage reconciler did not complete credential revocation") from None
        return await self.view(tenant, principal)

    async def set_enabled(self, tenant: str, principal: str, enabled: bool) -> None:
        if await self.repository.credential(tenant, principal) is None:
            return
        await self.repository.request_enabled(tenant, principal, enabled)
        try:
            await self.repository.wait_enabled(
                tenant,
                principal,
                enabled=enabled,
                timeout=self.action_timeout_seconds,
            )
        except TimeoutError:
            raise RuntimeError("storage reconciler did not apply the user state") from None

    async def _run(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except Exception as exc:
                LOG.warning("user storage inventory failed error_type=%s", type(exc).__name__)
            await asyncio.sleep(self.poll_seconds)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.provider is not None:
            await self.provider.close()
