"""Crash-safe reconciliation of customer-owned storage."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from .store import ConflictError
from .user_models import InferenceUser, owner_id
from .user_storage_models import StoragePolicy, UserStorage
from .user_storage_nebius import provider_operation_is_bound, track_provider_operation

LOG = logging.getLogger(__name__)
_ACTIVE = "ACTIVE"
_INACTIVE = {"INACTIVE", "EXPIRED", "DELETING", "DELETED"}


class _DurableProviderOperation:
    """Bridge SDK operation IDs into the PostgreSQL cutover ledger."""

    def __init__(
        self, repository: Any, operation_id: UUID, provider_idempotency_id: UUID
    ) -> None:
        self.repository = repository
        self.operation_id = operation_id
        self.provider_idempotency_id = provider_idempotency_id
        self.uncertain = False
        self.submitted_count = 0
        self.terminal_count = 0
        self.succeeded = False

    async def submitted(self, provider_operation_id: str) -> None:
        await self.repository.provider_operation_submitted(
            self.operation_id, provider_operation_id
        )
        self.submitted_count += 1

    async def terminal(
        self, provider_operation_id: str, *, succeeded: bool, code: str
    ) -> None:
        await self.repository.provider_operation_terminal(
            self.operation_id,
            provider_operation_id,
            succeeded=succeeded,
            code=code,
        )
        self.terminal_count += 1
        self.succeeded = self.succeeded or succeeded

    async def indeterminate(self, provider_operation_id: str | None = None) -> None:
        del provider_operation_id
        self.uncertain = True


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
        activation_fence: Any | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.users = users
        self.default = default or StoragePolicy()
        self.excluded_tenants = frozenset(excluded_tenants)
        self.poll_seconds = poll_seconds
        self.action_timeout_seconds = action_timeout_seconds
        self.rotation_window_days = rotation_window_days
        self.activation_fence = activation_fence
        self.task: asyncio.Task[None] | None = None
        self.provisioning_retry_at = 0.0

    async def _assert_active(self) -> None:
        # An operation durably admitted before a signed drain must be allowed
        # to reach and record its provider terminal result. Fresh operations
        # always re-fetch the activation envelope before DB admission.
        if provider_operation_is_bound():
            return
        if self.activation_fence is not None:
            await self.activation_fence.assert_active()

    async def _provider_mutation(
        self,
        *,
        tenant: str,
        principal: str,
        operation_kind: str,
        target_identity: str,
        invoke: Any,
        commit: Any | None = None,
    ) -> Any:
        if self.activation_fence is None or not hasattr(
            self.repository, "begin_provider_operation"
        ):
            await self._assert_active()
            result = await invoke()
            if commit is not None:
                await commit(result)
            return result
        admission = await self.activation_fence.admit_provider_operation()
        operation_id = uuid4()
        provider_idempotency_id = uuid4()
        await self.repository.begin_provider_operation(
            operation_id=operation_id,
            generation=admission["reconciler_generation"],
            activation_epoch=admission["activation_epoch"],
            activation_state_head_sha256=admission["activation_state_head_sha256"],
            transition_id=admission["transition_id"],
            tenant=tenant,
            principal=principal,
            operation_kind=operation_kind,
            target_identity=target_identity,
            provider_idempotency_id=provider_idempotency_id,
        )
        tracker = _DurableProviderOperation(
            self.repository, operation_id, provider_idempotency_id
        )
        try:
            with track_provider_operation(tracker):
                result = await invoke()
                if commit is not None:
                    await commit(result)
        except BaseException:
            if (
                tracker.uncertain
                or tracker.succeeded
                or tracker.submitted_count != tracker.terminal_count
            ):
                await self.repository.mark_provider_operation_indeterminate(operation_id)
            else:
                await self.repository.finish_provider_operation(operation_id, succeeded=False)
            raise
        await self.repository.finish_provider_operation(
            operation_id, succeeded=not tracker.uncertain
        )
        if tracker.uncertain:
            raise RuntimeError("provider operation ended without a terminal provider receipt")
        return result

    async def policy(self, tenant: str) -> StoragePolicy:
        if tenant in self.excluded_tenants:
            return StoragePolicy(mode="disabled", quota_bytes=self.default.quota_bytes)
        return StoragePolicy.model_validate(await self.repository.policy(tenant, self.default))

    async def configure(self, tenant: str, policy: StoragePolicy) -> StoragePolicy:
        if tenant in self.excluded_tenants and policy.mode != "disabled":
            raise ConflictError("tenant is explicitly excluded from bucket provisioning")
        async with self.repository.tenant_lock(tenant):
            await self.repository.set_policy(tenant, policy, self.default)
            if policy.mode == "disabled":
                await self.repository.request_tenant_suspension(tenant)
            else:
                await self.repository.request_tenant_resume(tenant)
        try:
            if policy.mode == "disabled":
                await self.repository.wait_tenant_suspended(tenant, timeout=self.action_timeout_seconds)
            else:
                await self.repository.wait_tenant_resumed(tenant, timeout=self.action_timeout_seconds)
        except TimeoutError:
            raise RuntimeError("storage reconciler did not complete the tenant policy transition") from None
        return await self.policy(tenant)

    async def view(self, tenant: str, principal: str) -> UserStorage:
        return UserStorage.model_validate(await self.repository.view(tenant, principal, await self.policy(tenant)))

    async def _provider_state(self, resource_id: str) -> str:
        await self._assert_active()
        state = cast(str, await self.provider.key_state(resource_id))
        if state not in {_ACTIVE, *_INACTIVE}:
            raise RuntimeError("provider returned an indeterminate storage-key state")
        return state

    async def _set_provider_state(
        self,
        tenant: str,
        principal: str,
        resource_id: str,
        enabled: bool,
        *,
        commit: Any | None = None,
    ) -> None:
        async def transition() -> None:
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

        await self._provider_mutation(
            tenant=tenant,
            principal=principal,
            operation_kind="access-key-activate" if enabled else "access-key-deactivate",
            target_identity=resource_id,
            invoke=transition,
            commit=commit,
        )

    async def _ensure_all_inactive(self, credential: dict[str, Any]) -> None:
        """Repair inverse provider drift for every key retained by the state machine."""

        for resource_id in {
            credential["access_key_resource_id"],
            credential["replacement_access_key_resource_id"],
            credential["previous_access_key_resource_id"],
        }:
            if resource_id is not None:
                await self._set_provider_state(
                    credential["tenant_id"], credential["principal_id"], resource_id, False
                )

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
            if state not in {_ACTIVE, "INACTIVE"}:
                raise RuntimeError("persisted storage replacement is no longer activatable")
            # Completion is forbidden until two provider reads around any
            # activation confirm the durably promoted key is still ACTIVE.
            await self._set_provider_state(tenant, principal, current, True)

            async def complete_rotation(_: object) -> None:
                await self.repository.complete_rotation(
                    tenant,
                    principal,
                    expected_version=credential["version"],
                )

            await self._set_provider_state(
                tenant,
                principal,
                predecessor,
                False,
                commit=complete_rotation,
            )
            return cast(dict[str, Any], await self.repository.credential(tenant, principal))

        if replacement is None:
            await self._assert_active()

            async def stage_replacement(value: dict[str, Any]) -> None:
                if value["expires_at"] <= datetime.now(UTC):
                    raise RuntimeError("provider prepared an expired storage key")
                await self.repository.stage_replacement(
                    tenant,
                    principal,
                    value,
                    expected_version=credential["version"],
                )

            value = await self._provider_mutation(
                tenant=tenant,
                principal=principal,
                operation_kind="access-key-prepare-rotation",
                target_identity=str(credential["access_key_resource_id"]),
                invoke=lambda: self.provider.prepare_rotation(
                    tenant,
                    principal,
                    bucket["group_id"],
                    credential,
                ),
                commit=stage_replacement,
            )
            return cast(dict[str, Any], await self.repository.credential(tenant, principal))

        state = await self._provider_state(replacement)
        if state in {"EXPIRED", "DELETING", "DELETED"}:
            raise RuntimeError("staged storage replacement is no longer activatable")
        # Always use the verified transition, even when the first read reports
        # ACTIVE, so promotion requires an independent confirming read.
        async def promote_replacement(_: object) -> None:
            await self.repository.promote_replacement(
                tenant,
                principal,
                expected_version=credential["version"],
            )

        try:
            await self._set_provider_state(
                tenant,
                principal,
                replacement,
                True,
                commit=promote_replacement,
            )
        except BaseException:
            # If PostgreSQL rejects the CAS, the predecessor remains current.
            # Compensate the newly activated key so no unowned active key lasts
            # beyond this bounded cutover attempt.
            with suppress(Exception):
                await self._set_provider_state(tenant, principal, replacement, False)
            raise
        return cast(dict[str, Any], await self.repository.credential(tenant, principal))

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
                async def complete_action(_: object) -> None:
                    await self.repository.complete_action(
                        user.tenant_id,
                        user.principal_id,
                        expected_action=action,
                        enabled=target,
                        revoked=action == "revoke",
                        expected_version=credential["version"],
                    )

                await self._set_provider_state(
                    user.tenant_id,
                    user.principal_id,
                    credential["access_key_resource_id"],
                    target,
                    commit=complete_action,
                )
            except BaseException:
                # Enabling is the only unsafe direction after a DB failure.
                if target:
                    with suppress(Exception):
                        await self._set_provider_state(
                            user.tenant_id,
                            user.principal_id,
                            credential["access_key_resource_id"],
                            False,
                        )
                raise
            return cast(
                dict[str, Any],
                await self.repository.credential(user.tenant_id, user.principal_id),
            )
        raise RuntimeError("storage credential transition exceeded its bounded reconciliation steps")

    async def ensure(self, user: InferenceUser) -> None:
        if self.provider is None:
            raise RuntimeError("customer storage cloud operations are isolated to the reconciler")
        async with self.repository.tenant_lock(user.tenant_id):
            await self._assert_active()
            configured_reader = getattr(self.users, "configured", None)
            configured = (
                await configured_reader(user.tenant_id, user.principal_id) if configured_reader is not None else None
            )
            if configured is not None:
                user = configured
            policy = await self.policy(user.tenant_id)
            if policy.mode == "tenant":
                # Tenant layout is safe only for one immutable principal. The
                # database binding also fences later user/configuration writes,
                # including when tenant mode came from the process default.
                await self.repository.bind_tenant_singleton(
                    user.tenant_id,
                    user.principal_id,
                    quota_bytes=policy.quota_bytes,
                )
                policy = await self.policy(user.tenant_id)
            elif policy.mode == "user":
                await self.repository.bind_user_layout(
                    user.tenant_id,
                    quota_bytes=policy.quota_bytes,
                )
            credential = await self.repository.credential(user.tenant_id, user.principal_id)
            if credential:
                await self.repository.bind_user_identity(user.tenant_id, user.principal_id)
                credential = await self.repository.credential(user.tenant_id, user.principal_id)
            if credential and await self.repository.reencrypt_if_needed(user.tenant_id, user.principal_id):
                credential = await self.repository.credential(user.tenant_id, user.principal_id)

            if credential:
                effective_enabled = bool(
                    user.enabled
                    and policy.mode != "disabled"
                    and credential["desired_enabled"]
                    and credential["revoked_at"] is None
                    and not credential["policy_suspension_requested"]
                )
                await self._assert_active()

                async def record_provider_ownership(verified: bool) -> None:
                    if verified != bool(credential["provider_ownership_verified"]):
                        await self.repository.record_provider_ownership(
                            user.tenant_id,
                            user.principal_id,
                            verified=verified,
                            expected_version=credential["version"],
                        )

                ownership_verified = await self._provider_mutation(
                    tenant=user.tenant_id,
                    principal=user.principal_id,
                    operation_kind="access-key-inventory-reconcile",
                    target_identity=str(credential["service_account_id"]),
                    invoke=lambda: self.provider.reconcile_key_inventory(
                        user.tenant_id,
                        user.principal_id,
                        credential,
                        effective_enabled=effective_enabled,
                    ),
                    commit=record_provider_ownership,
                )
                if ownership_verified != bool(credential["provider_ownership_verified"]):
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

            if credential and (credential["policy_suspension_requested"] or policy.mode == "disabled"):
                await self._ensure_all_inactive(credential)
                if credential["policy_suspension_requested"]:
                    await self.repository.complete_policy_suspension(
                        user.tenant_id,
                        user.principal_id,
                        expected_version=credential["version"],
                    )
                elif credential["requested_action"] == "suspend":
                    await self.repository.complete_action(
                        user.tenant_id,
                        user.principal_id,
                        expected_action="suspend",
                        enabled=False,
                        expected_version=credential["version"],
                    )
                return

            if credential and not user.enabled:
                # Provider safety outranks a previously queued rotation. Keep
                # that durable intent for a future re-enable, but never
                # activate a replacement while the authoritative user row is
                # disabled. Off-state actions can finish after every retained
                # provider key is observed inactive.
                await self._ensure_all_inactive(credential)
                action = credential["requested_action"]
                if action == "rotate" and credential["enabled"]:
                    await self.repository.record_inactive_preserving_rotation(
                        user.tenant_id,
                        user.principal_id,
                        expected_version=credential["version"],
                    )
                elif action in {"enable", "disable", "revoke", "suspend"}:
                    await self.repository.complete_action(
                        user.tenant_id,
                        user.principal_id,
                        expected_action=action,
                        enabled=False,
                        revoked=action == "revoke",
                        expected_version=credential["version"],
                    )
                return

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
                    await self._ensure_all_inactive(credential)
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
                if credential is not None:
                    await self._ensure_all_inactive(credential)
                return
            if credential is None and time.monotonic() < self.provisioning_retry_at:
                return

            # Always inspect the bucket and its exact policy; quota equality is
            # not sufficient evidence that IAM drift has not occurred.
            await self._assert_active()

            async def save_bucket(value: dict[str, Any]) -> None:
                await self.repository.save_bucket(user.tenant_id, owner, value)

            bucket = await self._provider_mutation(
                tenant=user.tenant_id,
                principal=user.principal_id,
                operation_kind="bucket-policy-reconcile",
                target_identity=str((bucket or {}).get("bucket_id") or owner),
                invoke=lambda: self.provider.ensure_bucket(
                    user.tenant_id,
                    owner,
                    policy.quota_bytes,
                    existing=bucket,
                ),
                commit=save_bucket,
            )
            if credential is None:
                await self._assert_active()

                async def save_credential(value: dict[str, Any]) -> None:
                    await self.repository.save_credential(
                        user.tenant_id, user.principal_id, owner, value
                    )

                value = await self._provider_mutation(
                    tenant=user.tenant_id,
                    principal=user.principal_id,
                    operation_kind="credential-provision",
                    target_identity=str(bucket["group_id"]),
                    invoke=lambda: self.provider.ensure_credentials(
                        user.tenant_id,
                        user.principal_id,
                        bucket["group_id"],
                    ),
                    commit=save_credential,
                )
                credential = await self.repository.credential(user.tenant_id, user.principal_id)
                assert credential is not None
                await self._finish_pending(user, credential, bucket)
            else:
                await self._assert_active()
                await self._provider_mutation(
                    tenant=user.tenant_id,
                    principal=user.principal_id,
                    operation_kind="bucket-membership-reconcile",
                    target_identity=f"{bucket['group_id']}:{credential['service_account_id']}",
                    invoke=lambda: self.provider.ensure_identity_access(
                        bucket["group_id"],
                        credential["service_account_id"],
                    ),
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
        await self._assert_active()
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

    async def rotate(
        self,
        tenant: str,
        principal: str,
        *,
        token_id: UUID | None,
        operator_session_id: UUID | None,
        idempotency_key: UUID,
    ) -> UserStorage:
        action_id = await self.repository.request_action(
            tenant,
            principal,
            "rotate",
            token_id=token_id,
            operator_session_id=operator_session_id,
            idempotency_key=idempotency_key,
        )
        try:
            await self.repository.wait_action(action_id, timeout=self.action_timeout_seconds)
        except TimeoutError:
            raise RuntimeError("storage reconciler did not complete credential rotation") from None
        return await self.view(tenant, principal)

    async def revoke(
        self,
        tenant: str,
        principal: str,
        *,
        token_id: UUID | None,
        operator_session_id: UUID | None,
        idempotency_key: UUID,
    ) -> UserStorage:
        action_id = await self.repository.request_action(
            tenant,
            principal,
            "revoke",
            token_id=token_id,
            operator_session_id=operator_session_id,
            idempotency_key=idempotency_key,
        )
        try:
            await self.repository.wait_action(action_id, timeout=self.action_timeout_seconds)
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

    async def wait_enabled(self, tenant: str, principal: str, enabled: bool) -> None:
        if await self.repository.credential(tenant, principal) is None:
            return
        try:
            await self.repository.wait_enabled(
                tenant,
                principal,
                enabled=enabled,
                timeout=self.action_timeout_seconds,
            )
        except TimeoutError:
            raise RuntimeError("storage reconciler did not apply the durable user state") from None

    async def drain_once(self) -> dict[str, Any] | None:
        """Honor a signed drain without admitting another provider operation."""

        if self.activation_fence is None:
            return None
        completed_reader = getattr(
            self.activation_fence, "completed_shutdown_receipt", None
        )
        if completed_reader is not None:
            completed = await completed_reader()
            if completed is not None:
                return cast(dict[str, Any], completed)
        intent = await self.activation_fence.current_drain_intent()
        if intent is None:
            return None
        await self.repository.begin_reconciler_drain(intent)
        return await self.repository.complete_reconciler_drain(intent["drain_id"])

    async def wait_for_signed_drain(self, timeout: float) -> dict[str, Any]:
        """Bounded pre-stop proof used before Kubernetes sends SIGTERM."""

        async with asyncio.timeout(timeout):
            while True:
                receipt = await self.drain_once()
                if receipt is not None:
                    return receipt
                await asyncio.sleep(0.5)

    async def _run(self) -> None:
        while True:
            try:
                if await self.drain_once() is not None:
                    # Keep serving the signed activation endpoint's drain
                    # epoch, but never reopen cloud-operation admission.
                    await asyncio.sleep(self.poll_seconds)
                    continue
                await self._assert_active()
                lease = getattr(self.repository, "reconciler_lease", None)
                if lease is None:
                    raise RuntimeError("storage repository lacks the singleton reconciler lease")
                async with lease() as acquired:
                    if acquired:
                        # Recheck after acquiring the independent database
                        # lease.  A cutover epoch may have changed while this
                        # generation waited behind its predecessor.
                        await self._assert_active()
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
