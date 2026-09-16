"""Reconcile customer-owned data independently of shared model deployments."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from .store import ConflictError
from .user_models import InferenceUser
from .user_storage_models import StoragePolicy, UserStorage

LOG = logging.getLogger(__name__)


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
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.users = users
        self.default = default or StoragePolicy()
        self.excluded_tenants = frozenset(excluded_tenants)
        self.poll_seconds = poll_seconds
        self.action_timeout_seconds = action_timeout_seconds
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

    async def ensure(self, user: InferenceUser) -> None:
        if self.provider is None:
            raise RuntimeError("customer storage cloud operations are isolated to the reconciler")
        async with self.repository.tenant_lock(user.tenant_id):
            policy = await self.policy(user.tenant_id)
            credential = await self.repository.credential(user.tenant_id, user.principal_id)
            if (
                credential
                and not user.enabled
                and credential["desired_enabled"]
                and credential.get("requested_action") is None
            ):
                await self.repository.request_enabled(user.tenant_id, user.principal_id, False)
                credential = await self.repository.credential(user.tenant_id, user.principal_id)
            requested_action = credential.get("requested_action") if credential else None
            if credential and requested_action == "rotate":
                bucket = await self.repository.bucket(user.tenant_id, credential["owner_key"])
                assert bucket is not None
                value = await self.provider.rotate_credentials(
                    user.tenant_id,
                    user.principal_id,
                    bucket["group_id"],
                    credential,
                )
                await self.repository.replace_credential(user.tenant_id, user.principal_id, value)
                credential = await self.repository.credential(user.tenant_id, user.principal_id)
            elif credential and requested_action in {"disable", "revoke"}:
                await self.provider.set_enabled(credential["access_key_resource_id"], False)
                await self.repository.complete_action(
                    user.tenant_id,
                    user.principal_id,
                    enabled=False,
                    revoked=requested_action == "revoke",
                )
                return
            enabled = (
                user.enabled
                and policy.mode != "disabled"
                and (bool(credential["desired_enabled"]) if credential is not None else True)
            )
            if credential and credential["enabled"] != enabled:
                await self.provider.set_enabled(credential["access_key_resource_id"], enabled)
                await self.repository.enabled(user.tenant_id, user.principal_id, enabled)
            if not enabled:
                return
            if credential is None and time.monotonic() < self.provisioning_retry_at:
                return
            owner = user.principal_id if policy.mode == "user" else ""
            bucket = await self.repository.bucket(user.tenant_id, owner)
            if bucket is None or bucket["quota_bytes"] != policy.quota_bytes:
                bucket = await self.provider.ensure_bucket(
                    user.tenant_id,
                    owner,
                    policy.quota_bytes,
                    existing=bucket,
                )
                await self.repository.save_bucket(user.tenant_id, owner, bucket)
            if credential is None:
                value = await self.provider.ensure_credentials(user.tenant_id, user.principal_id, bucket["group_id"])
                await self.repository.save_credential(user.tenant_id, user.principal_id, owner, value)

    async def reconcile_once(self) -> dict[str, int]:
        counts = {"ready": 0, "skipped": 0, "failed": 0}
        for user in await self.users.list(None):
            try:
                # Historical acceptance jobs are discoverable as owners, but
                # are not new customers. Only backfill discovered owners with
                # a current key; explicitly configured users need no key yet.
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
                # Bounded even when a cloud operation stalls; next pass adopts
                # named resources. No cloud call is made from an inference path.
                async with asyncio.timeout(120):
                    await self.ensure(user)
                state = (await self.view(user.tenant_id, user.principal_id)).state
                counts["ready" if state == "ready" else "skipped"] += 1
            except Exception as exc:
                # SDK exceptions may contain request details: record type, not
                # the exception body or any credential material.
                code = getattr(exc, "code", "") or getattr(
                    getattr(getattr(exc, "status", None), "code", None), "name", ""
                )
                if code == "RESOURCE_EXHAUSTED":
                    # Back off new provisioning after a cloud quota failure.
                    # Existing users' key disable/enable checks still run.
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
