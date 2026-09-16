"""Nebius customer buckets and IAM identities; never a project-wide S3 grant."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from grpc import StatusCode  # type: ignore[import-untyped]
from nebius.aio.service_error import RequestError
from nebius.api.nebius.common.v1 import ResourceMetadata
from nebius.api.nebius.iam import v1 as iam
from nebius.api.nebius.iam import v2 as keys
from nebius.api.nebius.storage import v1 as storage
from nebius.sdk import SDK

from .crypto import KeyedHasher


class StorageOperationError(RuntimeError):
    """Safe operation identifiers, without provider request/secret contents."""

    def __init__(self, operation: Any) -> None:
        status = operation.status()
        self.code = status.code.name if status is not None else "UNKNOWN"
        self.operation_id = str(operation.id)
        super().__init__(f"customer storage operation failed: {self.code} ({self.operation_id})")


class NebiusUserStorage:
    def __init__(
        self,
        resource_sdk: SDK,
        *,
        iam_sdk: SDK,
        project_id: str,
        tenant_id: str,
        region: str,
        name_hasher: KeyedHasher,
        prefix: str = "fs2-data",
        key_ttl_days: int = 90,
    ) -> None:
        self.resource_sdk = resource_sdk
        self.iam_sdk = iam_sdk
        self.project_id = project_id
        self.tenant_id = tenant_id
        self.region = region
        self.prefix = prefix
        self.name_hasher = name_hasher
        self.key_ttl_days = key_ttl_days
        self.buckets = storage.BucketServiceClient(resource_sdk)
        self.groups = iam.GroupServiceClient(iam_sdk)
        self.accounts = iam.ServiceAccountServiceClient(resource_sdk)
        self.memberships = iam.GroupMembershipServiceClient(iam_sdk)
        self.keys = keys.AccessKeyServiceClient(resource_sdk)

    @staticmethod
    def lifecycle() -> storage.LifecycleConfiguration:
        """Retain data while destructive customer lifecycle is unauthorized.

        Keep the two historical rule identities in a disabled state so a
        reconciliation turns an accidentally enabled rule off without deleting
        the rule or any customer object/version. Enabling either rule requires
        a separately reviewed retention authorization.
        """

        return storage.LifecycleConfiguration(
            rules=[
                storage.LifecycleRule(
                    id="expire-noncurrent-versions",
                    status=storage.LifecycleRule__Status.DISABLED,
                    noncurrent_version_expiration=storage.LifecycleNoncurrentVersionExpiration(
                        newer_noncurrent_versions=3,
                        noncurrent_days=30,
                    ),
                ),
                storage.LifecycleRule(
                    id="abort-incomplete-multipart-uploads",
                    status=storage.LifecycleRule__Status.DISABLED,
                    abort_incomplete_multipart_upload=storage.LifecycleAbortIncompleteMultipartUpload(
                        days_after_initiation=7
                    ),
                ),
            ]
        )

    def name(self, kind: str, tenant: str, owner: str) -> str:
        digest = hashlib.sha256(f"{self.project_id}\0{tenant}\0{owner}".encode()).hexdigest()[:24]
        return f"{self.prefix}-{kind}-{digest}"

    def bucket_name(self, tenant: str, owner: str) -> str:
        """Opaque keyed name: bucket listings disclose no tenant or user slug."""
        _, digest = self.name_hasher.digest(
            f"{self.project_id}\0{tenant}\0{owner}".encode(),
            context="fs2.user-storage-bucket/v1",
        )
        return f"fs2-{digest[:32]}"

    @staticmethod
    async def _operation(request: Any) -> str:
        operation = await request
        await operation.wait()
        # SDK wait() means terminal, not successful. Never publish failed
        # creates, key activations or quota updates as completed.
        if not operation.successful():
            raise StorageOperationError(operation)
        return str(operation.resource_id)

    async def _named(self, client: Any, get: Any, create: Any, name: str) -> Any:
        try:
            resource = await client.get_by_name(get)
        except RequestError as exc:
            if exc.status.code != StatusCode.NOT_FOUND:
                raise
            await self._operation(client.create(create))
            for attempt in range(5):
                try:
                    resource = await client.get_by_name(get)
                    break
                except RequestError as pending:
                    if pending.status.code != StatusCode.NOT_FOUND or attempt == 4:
                        raise
                    await asyncio.sleep(0.5 * 2**attempt)
        if resource.metadata.labels.get("fs2-storage-owner") != name:
            raise ValueError("existing resource is not owned by this customer-storage controller")
        return resource

    @staticmethod
    def _metadata(parent: str, name: str) -> ResourceMetadata:
        return ResourceMetadata(parent_id=parent, name=name, labels={"fs2-storage-owner": name})

    async def ensure_bucket(
        self,
        tenant: str,
        owner: str,
        quota: int,
        *,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = self.name("bucket", tenant, owner)
        readable_name = self.bucket_name(tenant, owner)
        group = await self._named(
            self.groups,
            iam.GetGroupByNameRequest(parent_id=self.project_id, name=name),
            iam.CreateGroupRequest(metadata=self._metadata(self.project_id, name), spec=iam.GroupSpec()),
            name,
        )
        policy = storage.BucketPolicy(
            rules=[
                storage.BucketPolicy__Rule(
                    paths=["*"],
                    roles=["storage.object-editor"],
                    group_id=group.metadata.id,
                )
            ]
        )
        bucket = None
        if existing is not None:
            # Never create a replacement for an existing bucket. Preserve its
            # objects, IAM group and credentials, including after a rename retry.
            bucket = await self.buckets.get(storage.GetBucketRequest(id=existing["bucket_id"]))
        else:
            # Adopt a legacy create that succeeded before the DB step completed.
            try:
                bucket = await self.buckets.get_by_name(
                    storage.GetBucketByNameRequest(parent_id=self.project_id, name=name)
                )
            except RequestError as exc:
                if exc.status.code != StatusCode.NOT_FOUND:
                    raise
        if bucket is None:
            metadata = self._metadata(self.project_id, name)
            metadata.name = readable_name
            bucket = await self._named(
                self.buckets,
                storage.GetBucketByNameRequest(parent_id=self.project_id, name=readable_name),
                storage.CreateBucketRequest(
                    metadata=metadata,
                    spec=storage.BucketSpec(
                        max_size_bytes=quota,
                        bucket_policy=policy,
                        versioning_policy=storage.VersioningPolicy.ENABLED,
                        lifecycle_configuration=self.lifecycle(),
                    ),
                ),
                name,
            )
        if bucket.metadata.labels.get("fs2-storage-owner") != name:
            raise ValueError("existing bucket is not owned by this customer-storage controller")
        if existing is not None and existing["group_id"] != group.metadata.id:
            raise ValueError("existing bucket IAM group identity changed")
        # Preserve unrelated bucket settings and use resource-version checking.
        # Nebius bucket names are immutable (verified against the live API).
        # Existing names require an explicit data migration, never replacement
        # as a side effect of reconciliation or a byte-quota change.
        needs_update = bucket.spec.max_size_bytes != quota
        if needs_update:
            bucket.spec.max_size_bytes = quota
        if bucket.spec.versioning_policy != storage.VersioningPolicy.ENABLED:
            bucket.spec.versioning_policy = storage.VersioningPolicy.ENABLED
            needs_update = True
        if repr(bucket.spec.bucket_policy.rules) != repr(policy.rules):
            # The managed bucket has one exact editor group. Preserve no
            # unreviewed group, principal, role, or path grant.
            bucket.spec.bucket_policy = policy
            needs_update = True
        current_lifecycle = bucket.spec.lifecycle_configuration
        current_rules_repr = repr(current_lifecycle.rules)
        # No customer-object deletion policy is authorized. Preserve every
        # discovered rule, including the historical managed IDs, in its exact
        # provider-returned order and with every provider-specific field
        # unchanged. Only the status is allowed to move to DISABLED. Missing
        # historical rules are not synthesized for an existing bucket: doing
        # so would replace an observed lifecycle contract rather than preserve
        # it under the no-delete/no-replacement custody rule.
        for rule in current_lifecycle.rules:
            rule.status = storage.LifecycleRule__Status.DISABLED
        if current_rules_repr != repr(current_lifecycle.rules):
            bucket.spec.lifecycle_configuration = current_lifecycle
            needs_update = True
        if needs_update:
            await self._operation(
                self.buckets.update(
                    storage.UpdateBucketRequest(
                        metadata=bucket.metadata,
                        spec=bucket.spec,
                    )
                )
            )
        return {
            "bucket_id": bucket.metadata.id,
            "bucket_name": bucket.metadata.name,
            "group_id": group.metadata.id,
            "endpoint": f"https://storage.{self.region}.nebius.cloud",
            "region": self.region,
            "quota_bytes": quota,
        }

    async def ensure_identity_access(self, group_id: str, account_id: str) -> None:
        """Enforce exactly one permitted service account in a per-user group."""

        page_token = ""
        memberships: list[Any] = []
        while True:
            page = await self.memberships.list_members(
                iam.ListGroupMembershipsRequest(parent_id=group_id, page_size=100, page_token=page_token)
            )
            memberships.extend(page.memberships)
            page_token = page.next_page_token
            if not page_token:
                break
        expected = [item for item in memberships if item.spec.member_id == account_id]
        unexpected = [item for item in memberships if item.spec.member_id != account_id]
        if len(expected) > 1:
            raise RuntimeError("customer storage IAM group has duplicate expected memberships")
        if unexpected:
            # Membership deletion is deliberately not an automatic recovery
            # action. Fail before adding or enabling anything so the owner can
            # preserve and review the exact unexpected membership identities
            # under a separately authorized remediation.
            raise RuntimeError("customer storage IAM group has unexpected memberships")
        if not expected:
            await self._operation(
                self.memberships.create(
                    iam.CreateGroupMembershipRequest(
                        metadata=ResourceMetadata(parent_id=group_id),
                        spec=iam.GroupMembershipSpec(member_id=account_id),
                    )
                )
            )

    async def key_state(self, key_id: str) -> str:
        key = await self.keys.get(keys.GetAccessKeyRequest(id=key_id))
        state = key.status.state
        return state.name if hasattr(state, "name") else str(state).rsplit(".", maxsplit=1)[-1]

    async def _account_keys(self, account_id: str) -> list[Any]:
        identity = iam.Account(service_account=iam.Account__ServiceAccount(id=account_id))
        page_token = ""
        result: list[Any] = []
        while True:
            page = await self.keys.list_by_account(
                keys.ListAccessKeysByAccountRequest(
                    account=identity,
                    page_size=100,
                    page_token=page_token,
                )
            )
            result.extend(page.items)
            page_token = page.next_page_token
            if not page_token:
                return result

    @staticmethod
    def _controller_key(item: Any, base_name: str) -> bool:
        """Require the exact per-resource controller ownership label."""

        name = str(item.metadata.name)
        labels = dict(getattr(item.metadata, "labels", {}) or {})
        if labels.get("fs2-storage-owner") != name:
            return False
        if name == base_name:
            return True
        prefix = f"{base_name}-r-"
        suffix = name.removeprefix(prefix)
        return (
            name.startswith(prefix)
            and len(suffix) == 8
            and all(character in "0123456789abcdef" for character in suffix)
        )

    async def _force_key_inactive(self, key_id: str) -> str:
        state = await self.key_state(key_id)
        if state == "ACTIVE":
            await self.set_enabled(key_id, False)
            state = await self.key_state(key_id)
        if state not in {"INACTIVE", "EXPIRED", "DELETING", "DELETED"}:
            raise RuntimeError("untracked customer-storage key did not reach a fail-closed state")
        return state

    async def reconcile_key_inventory(
        self,
        tenant: str,
        principal: str,
        credential: dict[str, Any],
        *,
        effective_enabled: bool,
    ) -> bool:
        """Inventory every key on the managed account and quarantine extras.

        Only the exact DB-tracked current key may remain active, and only when
        its provider metadata carries the controller ownership label. A legacy
        unlabeled current key is made inactive and returned as unverified so
        the durable state machine rotates it instead of disclosing it.
        """

        base_name = self.name("user", tenant, principal)
        current_id = str(credential["access_key_resource_id"])
        inventory = await self._account_keys(str(credential["service_account_id"]))
        by_id = {str(item.metadata.id): item for item in inventory}
        current_owned = current_id in by_id and self._controller_key(by_id[current_id], base_name)
        foreign_managed_name = False
        for item in inventory:
            key_id = str(item.metadata.id)
            owned = self._controller_key(item, base_name)
            managed_name = str(item.metadata.name) == base_name or str(item.metadata.name).startswith(f"{base_name}-r-")
            if managed_name and not owned:
                foreign_managed_name = True
            keep_active = key_id == current_id and current_owned and effective_enabled
            if not keep_active:
                await self._force_key_inactive(key_id)
            else:
                state = await self.key_state(key_id)
                if state not in {"ACTIVE", "INACTIVE"}:
                    raise RuntimeError("managed current S3 key has an unusable provider state")
        if foreign_managed_name:
            raise RuntimeError("foreign same-name customer-storage key was quarantined")
        replacement_id = credential.get("replacement_access_key_resource_id")
        if replacement_id is not None:
            replacement = by_id.get(str(replacement_id))
            if replacement is None or not self._controller_key(replacement, base_name):
                raise RuntimeError("staged customer-storage replacement is not controller-owned")
        return current_owned

    def _require_bounded_expiry(self, value: datetime | None) -> datetime:
        now = datetime.now(UTC)
        if value is None or value <= now or value > now + timedelta(days=self.key_ttl_days, minutes=5):
            raise RuntimeError("managed S3 key expiry is missing, expired, or exceeds the configured TTL")
        return value

    async def ensure_credentials(self, tenant: str, principal: str, group_id: str) -> dict[str, Any]:
        name = self.name("user", tenant, principal)
        account = await self._named(
            self.accounts,
            iam.GetServiceAccountByNameRequest(parent_id=self.project_id, name=name),
            iam.CreateServiceAccountRequest(
                metadata=self._metadata(self.project_id, name),
                spec=iam.ServiceAccountSpec(description="Scientific AI customer S3 access; bucket policy only"),
            ),
            name,
        )
        await self.ensure_identity_access(group_id, account.metadata.id)
        identity = iam.Account(service_account=iam.Account__ServiceAccount(id=account.metadata.id))
        inventory = await self._account_keys(account.metadata.id)
        foreign_same_name = [
            item for item in inventory if item.metadata.name == name and not self._controller_key(item, name)
        ]
        for item in foreign_same_name:
            await self._force_key_inactive(str(item.metadata.id))
        if foreign_same_name:
            raise RuntimeError("foreign same-name customer-storage key was quarantined")
        existing = [item for item in inventory if item.metadata.name == name and self._controller_key(item, name)]
        existing_ids = {str(item.metadata.id) for item in existing}
        for item in inventory:
            if str(item.metadata.id) not in existing_ids:
                await self._force_key_inactive(str(item.metadata.id))
        expires_at: datetime | None = None
        provider_state = "INACTIVE"
        if existing:
            existing.sort(key=lambda item: (item.spec.expires_at or datetime.min.replace(tzinfo=UTC)), reverse=True)
            key_id = existing[0].metadata.id
            for stale in existing[1:]:
                await self._force_key_inactive(str(stale.metadata.id))
            state = await self.key_state(key_id)
            if (
                existing[0].spec.expires_at is None
                or existing[0].spec.expires_at <= datetime.now(UTC)
                or state == "EXPIRED"
            ):
                replacement = await self.prepare_rotation(
                    tenant,
                    principal,
                    group_id,
                    {
                        "service_account_id": account.metadata.id,
                        "access_key_resource_id": key_id,
                    },
                )
                replacement["previous_access_key_resource_id"] = key_id
                return replacement
            if state not in {"ACTIVE", "INACTIVE"}:
                raise RuntimeError("existing managed S3 key is not adoptable")
            provider_state = state
        else:
            expires_at = datetime.now(UTC) + timedelta(days=self.key_ttl_days)
            key_id = await self._operation(
                self.keys.create(
                    keys.CreateAccessKeyRequest(
                        metadata=self._metadata(self.project_id, name),
                        spec=keys.AccessKeySpec(
                            account=identity,
                            expires_at=expires_at,
                            description="Expiring Scientific AI customer bucket credential",
                            secret_delivery_mode=keys.SecretDeliveryMode.EXPLICIT,
                        ),
                    )
                )
            )
        secret = await self.keys.get_secret(keys.GetAccessKeySecretRequest(id=key_id))
        key = next((item for item in existing if item.metadata.id == key_id), None)
        expires_at = key.spec.expires_at if key is not None else expires_at
        expires_at = self._require_bounded_expiry(expires_at)
        if not existing:
            if await self.key_state(key_id) == "ACTIVE":
                await self.set_enabled(key_id, False)
            provider_state = await self.key_state(key_id)
            if provider_state != "INACTIVE":
                raise RuntimeError("new managed S3 key did not become inactive before persistence")
        return {
            "service_account_id": account.metadata.id,
            "access_key_resource_id": key_id,
            "access_key_id": secret.aws_access_key_id,
            "secret_access_key": secret.secret,
            "expires_at": expires_at,
            "provider_state": provider_state,
            "provider_ownership_verified": True,
        }

    async def prepare_rotation(
        self,
        tenant: str,
        principal: str,
        group_id: str,
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        name = self.name("user", tenant, principal)
        account_id = previous["service_account_id"]
        identity = iam.Account(service_account=iam.Account__ServiceAccount(id=account_id))
        started_at = previous.get("requested_at") or previous.get("rotation_started_at") or "adoption"
        rotation_identity = f"{previous['access_key_resource_id']}\0{started_at}"
        rotation_name = f"{name}-r-{hashlib.sha256(rotation_identity.encode()).hexdigest()[:8]}"
        expires_at: datetime | None = datetime.now(UTC) + timedelta(days=self.key_ttl_days)
        inventory = await self._account_keys(account_id)
        foreign_same_name = [
            item
            for item in inventory
            if (
                str(item.metadata.name) == name
                or str(item.metadata.name).startswith(f"{name}-r-")
            )
            and not self._controller_key(item, name)
        ]
        for item in foreign_same_name:
            await self._force_key_inactive(str(item.metadata.id))
        if foreign_same_name:
            raise RuntimeError("foreign same-name rotation key was quarantined")
        replacement = next(
            (
                item
                for item in inventory
                if item.metadata.name == rotation_name and self._controller_key(item, name)
            ),
            None,
        )
        allowed_ids = {str(previous["access_key_resource_id"])}
        if replacement is not None:
            allowed_ids.add(str(replacement.metadata.id))
        for item in inventory:
            if str(item.metadata.id) not in allowed_ids:
                await self._force_key_inactive(str(item.metadata.id))
        key_id = (
            replacement.metadata.id
            if replacement is not None
            else await self._operation(
                self.keys.create(
                    keys.CreateAccessKeyRequest(
                        metadata=self._metadata(self.project_id, rotation_name),
                        spec=keys.AccessKeySpec(
                            account=identity,
                            expires_at=expires_at,
                            description="Rotated expiring Scientific AI customer bucket credential",
                            secret_delivery_mode=keys.SecretDeliveryMode.EXPLICIT,
                        ),
                    )
                )
            )
        )
        if replacement is not None:
            expires_at = replacement.spec.expires_at
            state = await self.key_state(key_id)
            if state not in {"ACTIVE", "INACTIVE"}:
                raise RuntimeError("existing replacement S3 key is not reusable")
        expires_at = self._require_bounded_expiry(expires_at)
        try:
            secret = await self.keys.get_secret(keys.GetAccessKeySecretRequest(id=key_id))
            # A replacement remains inactive until its secret is durably
            # encrypted in PostgreSQL. The current key is untouched here.
            if await self.key_state(key_id) == "ACTIVE":
                await self.set_enabled(key_id, False)
            if await self.key_state(key_id) != "INACTIVE":
                raise RuntimeError("replacement S3 key did not become inactive before persistence")
        except BaseException:
            # Best-effort compensation for provider/transport failures. A hard
            # process loss is recovered by the deterministic name on retry.
            with suppress(Exception):
                await self.set_enabled(key_id, False)
            raise
        return {
            "service_account_id": account_id,
            "access_key_resource_id": key_id,
            "access_key_id": secret.aws_access_key_id,
            "secret_access_key": secret.secret,
            "expires_at": expires_at,
            "provider_state": "INACTIVE",
            "provider_ownership_verified": True,
        }

    async def set_enabled(self, key_id: str, enabled: bool) -> None:
        request = (
            self.keys.activate(keys.ActivateAccessKeyRequest(id=key_id))
            if enabled
            else self.keys.deactivate(keys.DeactivateAccessKeyRequest(id=key_id))
        )
        await self._operation(request)

    async def close(self) -> None:
        await self.resource_sdk.close()
        await self.iam_sdk.close()
