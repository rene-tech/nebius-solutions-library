"""Nebius customer buckets and IAM identities; never a project-wide S3 grant."""

from __future__ import annotations

import asyncio
import hashlib
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
        return storage.LifecycleConfiguration(
            rules=[
                storage.LifecycleRule(
                    id="expire-noncurrent-versions",
                    status=storage.LifecycleRule__Status.ENABLED,
                    noncurrent_version_expiration=storage.LifecycleNoncurrentVersionExpiration(
                        newer_noncurrent_versions=3,
                        noncurrent_days=30,
                    ),
                ),
                storage.LifecycleRule(
                    id="abort-incomplete-multipart-uploads",
                    status=storage.LifecycleRule__Status.ENABLED,
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
        managed_lifecycle = self.lifecycle()
        managed_ids = {rule.id for rule in managed_lifecycle.rules}
        current_lifecycle = bucket.spec.lifecycle_configuration
        desired_rules = [rule for rule in current_lifecycle.rules if rule.id not in managed_ids]
        desired_rules.extend(managed_lifecycle.rules)
        if repr(current_lifecycle.rules) != repr(desired_rules):
            current_lifecycle.rules = desired_rules
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
        page_token = ""
        member_found = False
        while True:
            membership_page = await self.memberships.list_members(
                iam.ListGroupMembershipsRequest(
                    parent_id=group_id,
                    page_size=100,
                    page_token=page_token,
                )
            )
            member_found |= any(item.spec.member_id == account.metadata.id for item in membership_page.memberships)
            page_token = membership_page.next_page_token
            if not page_token:
                break
        if not member_found:
            await self._operation(
                self.memberships.create(
                    iam.CreateGroupMembershipRequest(
                        # IAM group memberships do not support resource names.
                        # Their stable identity is the group/member pair above.
                        metadata=ResourceMetadata(parent_id=group_id),
                        spec=iam.GroupMembershipSpec(member_id=account.metadata.id),
                    )
                )
            )
        identity = iam.Account(service_account=iam.Account__ServiceAccount(id=account.metadata.id))
        page_token = ""
        existing: list[keys.AccessKey] = []
        while True:
            page = await self.keys.list_by_account(
                keys.ListAccessKeysByAccountRequest(
                    account=identity,
                    page_size=100,
                    page_token=page_token,
                )
            )
            existing.extend(item for item in page.items if item.metadata.name == name)
            page_token = page.next_page_token
            if not page_token:
                break
        expires_at: datetime | None = None
        if existing:
            existing.sort(key=lambda item: (item.spec.expires_at or datetime.min.replace(tzinfo=UTC)), reverse=True)
            key_id = existing[0].metadata.id
            for stale in existing[1:]:
                await self.set_enabled(stale.metadata.id, False)
            if existing[0].spec.expires_at is None or existing[0].spec.expires_at <= datetime.now(UTC):
                return await self.rotate_credentials(
                    tenant,
                    principal,
                    group_id,
                    {
                        "service_account_id": account.metadata.id,
                        "access_key_resource_id": key_id,
                    },
                )
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
        if expires_at is None:
            raise RuntimeError("managed S3 key is missing its required expiry")
        return {
            "service_account_id": account.metadata.id,
            "access_key_resource_id": key_id,
            "access_key_id": secret.aws_access_key_id,
            "secret_access_key": secret.secret,
            "expires_at": expires_at,
        }

    async def rotate_credentials(
        self,
        tenant: str,
        principal: str,
        group_id: str,
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        name = self.name("user", tenant, principal)
        account_id = previous["service_account_id"]
        identity = iam.Account(service_account=iam.Account__ServiceAccount(id=account_id))
        rotation_name = f"{name}-r-{hashlib.sha256(previous['access_key_resource_id'].encode()).hexdigest()[:8]}"
        expires_at: datetime | None = datetime.now(UTC) + timedelta(days=self.key_ttl_days)
        page = await self.keys.list_by_account(keys.ListAccessKeysByAccountRequest(account=identity, page_size=100))
        replacement = next((item for item in page.items if item.metadata.name == rotation_name), None)
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
        if expires_at is None:
            raise RuntimeError("replacement S3 key is missing its required expiry")
        secret = await self.keys.get_secret(keys.GetAccessKeySecretRequest(id=key_id))
        await self.set_enabled(previous["access_key_resource_id"], False)
        return {
            "service_account_id": account_id,
            "access_key_resource_id": key_id,
            "access_key_id": secret.aws_access_key_id,
            "secret_access_key": secret.secret,
            "expires_at": expires_at,
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
