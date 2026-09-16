"""User-associated S3 credentials, using the existing encryption key ring."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from .crypto import Ciphertext, PayloadCipher
from .store import ConflictError
from .user_storage_models import StorageCredentials, StoragePolicy, UserStorage


class PostgresUserStorageRepository:
    def __init__(self, pool: Any, cipher: PayloadCipher) -> None:
        self.pool = pool
        self.cipher = cipher

    @staticmethod
    def aad(tenant: str, principal: str) -> bytes:
        return f"fs2.user-storage/v1\0{tenant}\0{principal}".encode()

    @asynccontextmanager
    async def tenant_lock(self, tenant: str) -> AsyncIterator[None]:
        # Session lock: cloud operations and each completed step are persisted
        # independently, so a retry can adopt a resource after a process crash.
        async with self.pool.acquire() as connection:
            await connection.execute("SELECT pg_advisory_lock(hashtextextended($1,31))", tenant)
            try:
                yield
            finally:
                await connection.execute("SELECT pg_advisory_unlock(hashtextextended($1,31))", tenant)

    async def policy(self, tenant: str, default: StoragePolicy) -> StoragePolicy:
        row = await self.pool.fetchrow("SELECT mode,quota_bytes FROM fs2_storage_policies WHERE tenant_id=$1", tenant)
        return StoragePolicy(**dict(row)) if row else default

    async def set_policy(self, tenant: str, policy: StoragePolicy, default: StoragePolicy) -> None:
        previous = await self.policy(tenant, default)
        has_buckets = await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM fs2_storage_buckets WHERE tenant_id=$1)", tenant
        )
        if has_buckets and policy.mode != previous.mode:
            raise ConflictError("storage mode cannot change after provisioning; data/access migration is required")
        await self.pool.execute(
            """INSERT INTO fs2_storage_policies(tenant_id,mode,quota_bytes) VALUES($1,$2,$3)
            ON CONFLICT(tenant_id) DO UPDATE SET mode=$2,quota_bytes=$3,updated_at=now()""",
            tenant,
            policy.mode,
            policy.quota_bytes,
        )

    async def bucket(self, tenant: str, owner: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM fs2_storage_buckets WHERE tenant_id=$1 AND owner_key=$2", tenant, owner
        )
        return dict(row) if row else None

    async def save_bucket(self, tenant: str, owner: str, bucket: dict[str, Any]) -> None:
        await self.pool.execute(
            """INSERT INTO fs2_storage_buckets
            (tenant_id,owner_key,bucket_id,bucket_name,group_id,endpoint,region,quota_bytes)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(tenant_id,owner_key)
            DO UPDATE SET quota_bytes=excluded.quota_bytes,bucket_name=excluded.bucket_name
            WHERE fs2_storage_buckets.bucket_id=excluded.bucket_id
            AND fs2_storage_buckets.group_id=excluded.group_id""",
            tenant,
            owner,
            bucket["bucket_id"],
            bucket["bucket_name"],
            bucket["group_id"],
            bucket["endpoint"],
            bucket["region"],
            bucket["quota_bytes"],
        )

    async def credential(self, tenant: str, principal: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM fs2_user_storage WHERE tenant_id=$1 AND principal_id=$2", tenant, principal
        )
        return dict(row) if row else None

    async def save_credential(self, tenant: str, principal: str, owner: str, value: dict[str, str]) -> None:
        encrypted = self.cipher.encrypt(value["secret_access_key"].encode(), aad=self.aad(tenant, principal))
        await self.pool.execute(
            """INSERT INTO fs2_user_storage
            (tenant_id,principal_id,owner_key,service_account_id,access_key_resource_id,access_key_id,
             secret_key_id,secret_nonce,secret_ciphertext)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            tenant,
            principal,
            owner,
            value["service_account_id"],
            value["access_key_resource_id"],
            value["access_key_id"],
            encrypted.key_id,
            encrypted.nonce,
            encrypted.value,
        )

    async def enabled(self, tenant: str, principal: str, enabled: bool) -> None:
        await self.pool.execute(
            "UPDATE fs2_user_storage SET enabled=$3 WHERE tenant_id=$1 AND principal_id=$2", tenant, principal, enabled
        )

    async def view(self, tenant: str, principal: str, policy: StoragePolicy) -> UserStorage:
        credential = await self.credential(tenant, principal)
        if not credential:
            return UserStorage(state="disabled" if policy.mode == "disabled" else "pending", **policy.model_dump())
        bucket = await self.bucket(tenant, credential["owner_key"])
        assert bucket is not None
        return UserStorage(
            state="ready" if credential["enabled"] else "disabled",
            mode=policy.mode,
            quota_bytes=bucket["quota_bytes"],
            bucket_name=bucket["bucket_name"],
            endpoint=bucket["endpoint"],
            region=bucket["region"],
            access_key_id=credential["access_key_id"],
        )

    async def disclose(self, tenant: str, principal: str) -> StorageCredentials:
        value = await self.credential(tenant, principal)
        if not value or not value["enabled"]:
            raise ConflictError("storage credentials are not ready or the user is disabled")
        bucket = await self.bucket(tenant, value["owner_key"])
        assert bucket is not None
        secret = self.cipher.decrypt(
            Ciphertext(value["secret_key_id"], value["secret_nonce"], value["secret_ciphertext"]),
            aad=self.aad(tenant, principal),
        ).decode()
        return StorageCredentials(
            bucket_name=bucket["bucket_name"],
            endpoint=bucket["endpoint"],
            region=bucket["region"],
            access_key_id=value["access_key_id"],
            secret_access_key=secret,
        )
