"""Nebius customer buckets and IAM identities; never a project-wide S3 grant."""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from typing import Any

from grpc import StatusCode  # type: ignore[import-untyped]
from nebius.aio.service_error import RequestError
from nebius.api.nebius.common.v1 import ResourceMetadata
from nebius.api.nebius.iam import v1 as iam
from nebius.api.nebius.iam import v2 as keys
from nebius.api.nebius.storage import v1 as storage
from nebius.sdk import SDK


class StorageOperationError(RuntimeError):
    """Safe operation identifiers, without provider request/secret contents."""

    def __init__(self, operation: Any) -> None:
        status = operation.status()
        self.code = status.code.name if status is not None else "UNKNOWN"
        self.operation_id = str(operation.id)
        super().__init__(f"customer storage operation failed: {self.code} ({self.operation_id})")


class NebiusUserStorage:
    def __init__(self, sdk: SDK, *, project_id: str, tenant_id: str, region: str, prefix: str = "fs2-data") -> None:
        self.sdk = sdk
        self.project_id = project_id
        self.tenant_id = tenant_id
        self.region = region
        self.prefix = prefix
        self.buckets = storage.BucketServiceClient(sdk)
        self.groups = iam.GroupServiceClient(sdk)
        self.accounts = iam.ServiceAccountServiceClient(sdk)
        self.memberships = iam.GroupMembershipServiceClient(sdk)
        self.keys = keys.AccessKeyServiceClient(sdk)

    def name(self, kind: str, tenant: str, owner: str) -> str:
        digest = hashlib.sha256(f"{self.project_id}\0{tenant}\0{owner}".encode()).hexdigest()[:24]
        return f"{self.prefix}-{kind}-{digest}"

    def bucket_name(self, tenant: str, owner: str) -> str:
        """Readable S3 name; the raw identity hash disambiguates slugs/truncation.

        IAM names and ownership labels deliberately remain stable across the
        naming migration. A shared tenant bucket never includes a user name.
        """
        def slug(value: str, limit: int, fallback: str) -> str:
            ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
            return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")[:limit].rstrip("-") or fallback

        digest = hashlib.sha256(f"{self.project_id}\0{tenant}\0{owner}".encode()).hexdigest()[:16]
        parts = ["fs2", slug(tenant, 20 if owner else 42, "tenant")]
        if owner:
            parts.append(slug(owner, 21, "user"))
        return "-".join([*parts, digest])

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
        self, tenant: str, owner: str, quota: int, *, existing: dict[str, Any] | None = None,
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
                    spec=storage.BucketSpec(max_size_bytes=quota, bucket_policy=policy),
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
        if bucket.spec.max_size_bytes != quota:
            bucket.spec.max_size_bytes = quota
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

    async def ensure_credentials(self, tenant: str, principal: str, group_id: str) -> dict[str, str]:
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
        if len(existing) > 1:
            raise RuntimeError("multiple managed S3 keys need reconciliation")
        if existing:
            key_id = existing[0].metadata.id
        else:
            key_id = await self._operation(
                self.keys.create(
                    keys.CreateAccessKeyRequest(
                        metadata=self._metadata(self.project_id, name),
                        spec=keys.AccessKeySpec(
                            account=identity, secret_delivery_mode=keys.SecretDeliveryMode.EXPLICIT
                        ),
                    )
                )
            )
        secret = await self.keys.get_secret(keys.GetAccessKeySecretRequest(id=key_id))
        return {
            "service_account_id": account.metadata.id,
            "access_key_resource_id": key_id,
            "access_key_id": secret.aws_access_key_id,
            "secret_access_key": secret.secret,
        }

    async def set_enabled(self, key_id: str, enabled: bool) -> None:
        request = (
            self.keys.activate(keys.ActivateAccessKeyRequest(id=key_id))
            if enabled
            else self.keys.deactivate(keys.DeactivateAccessKeyRequest(id=key_id))
        )
        await self._operation(request)

    async def close(self) -> None:
        await self.sdk.close()
