"""Least-privilege PostgreSQL state for customer storage."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from .crypto import Ciphertext, PayloadCipher
from .store import ConflictError
from .user_storage_models import StorageCredentials, StoragePolicy, UserStorage


class PostgresUserStorageRepository:
    def __init__(self, pool: Any, cipher: PayloadCipher | None) -> None:
        self.pool = pool
        self.cipher = cipher

    def _cipher(self) -> PayloadCipher:
        if self.cipher is None:
            raise RuntimeError("storage encryption material is unavailable")
        return self.cipher

    @staticmethod
    def aad(tenant: str, principal: str) -> bytes:
        return f"fs2.user-storage/v1\0{tenant}\0{principal}".encode()

    @asynccontextmanager
    async def tenant_lock(self, tenant: str) -> AsyncIterator[None]:
        # The lock serializes one tenant while row versions make every durable
        # transition reject stale workers and survive a process restart.
        async with self.pool.acquire() as connection:
            await connection.execute("SELECT pg_advisory_lock(hashtextextended($1,31))", tenant)
            try:
                yield
            finally:
                await connection.execute("SELECT pg_advisory_unlock(hashtextextended($1,31))", tenant)

    async def policy(self, tenant: str, default: StoragePolicy) -> StoragePolicy:
        row = await self.pool.fetchrow(
            "SELECT layout_mode,enabled,quota_bytes FROM fs2_storage_policies WHERE tenant_id=$1",
            tenant,
        )
        if row is None:
            return default
        value = dict(row)
        return StoragePolicy(
            mode=value["layout_mode"] if value["enabled"] else "disabled",
            quota_bytes=value["quota_bytes"],
        )

    async def set_policy(self, tenant: str, policy: StoragePolicy, default: StoragePolicy) -> None:
        row = await self.pool.fetchrow(
            "SELECT layout_mode,enabled,migration_state FROM fs2_storage_policies WHERE tenant_id=$1",
            tenant,
        )
        if row is not None and row["migration_state"] != "ready" and policy.mode != "disabled":
            raise ConflictError("storage layout migration must complete before this tenant can be enabled")
        current_layout = str(row["layout_mode"]) if row else (default.mode if default.mode != "disabled" else "user")
        requested_layout = current_layout if policy.mode == "disabled" else policy.mode
        has_buckets = await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM fs2_storage_buckets WHERE tenant_id=$1)",
            tenant,
        )
        if has_buckets and requested_layout != current_layout:
            raise ConflictError(
                "storage layout cannot change after provisioning; an explicit data migration is required"
            )
        await self.pool.execute(
            """INSERT INTO fs2_storage_policies(tenant_id,layout_mode,enabled,quota_bytes)
            VALUES($1,$2,$3,$4) ON CONFLICT(tenant_id) DO UPDATE
            SET layout_mode=$2,enabled=$3,quota_bytes=$4,updated_at=now()""",
            tenant,
            requested_layout,
            policy.mode != "disabled",
            policy.quota_bytes,
        )

    async def bucket(self, tenant: str, owner: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """SELECT tenant_id,owner_key,bucket_id,bucket_name,group_id,endpoint,region,quota_bytes
            FROM fs2_storage_buckets WHERE tenant_id=$1 AND owner_key=$2""",
            tenant,
            owner,
        )
        return dict(row) if row else None

    async def save_bucket(self, tenant: str, owner: str, bucket: dict[str, Any]) -> None:
        updated = await self.pool.execute(
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
        if updated == "INSERT 0 0":
            raise ConflictError("storage bucket identity changed during reconciliation")

    async def credential(self, tenant: str, principal: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """SELECT tenant_id,principal_id,owner_key,service_account_id,
            access_key_resource_id,access_key_id,expires_at,enabled,desired_enabled,revoked_at,
            requested_action,requested_at,replacement_access_key_resource_id,
            previous_access_key_resource_id,rotation_started_at,disclosure_consumed_at,version
            FROM fs2_user_storage WHERE tenant_id=$1 AND principal_id=$2""",
            tenant,
            principal,
        )
        return dict(row) if row else None

    async def reencrypt_if_needed(self, tenant: str, principal: str) -> bool:
        """Move current/staged envelopes to the dedicated active generation.

        Old PayloadCipher generations stay mounted until this inventory is
        zero; the gateway role cannot SELECT these columns directly.
        """

        row = await self.pool.fetchrow(
            """SELECT secret_key_id,secret_nonce,secret_ciphertext,
            replacement_secret_key_id,replacement_secret_nonce,
            replacement_secret_ciphertext,version
            FROM fs2_user_storage WHERE tenant_id=$1 AND principal_id=$2""",
            tenant,
            principal,
        )
        if row is None:
            return False
        value = dict(row)
        cipher = self._cipher()
        if value["secret_key_id"] == cipher.active_key_id and (
            value["replacement_secret_key_id"] is None or value["replacement_secret_key_id"] == cipher.active_key_id
        ):
            return False
        aad = self.aad(tenant, principal)
        current = cipher.encrypt(
            cipher.decrypt(
                Ciphertext(value["secret_key_id"], value["secret_nonce"], value["secret_ciphertext"]),
                aad=aad,
            ),
            aad=aad,
        )
        replacement = None
        if value["replacement_secret_key_id"] is not None:
            replacement = cipher.encrypt(
                cipher.decrypt(
                    Ciphertext(
                        value["replacement_secret_key_id"],
                        value["replacement_secret_nonce"],
                        value["replacement_secret_ciphertext"],
                    ),
                    aad=aad,
                ),
                aad=aad,
            )
        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET secret_key_id=$4,secret_nonce=$5,
            secret_ciphertext=$6,replacement_secret_key_id=$7,
            replacement_secret_nonce=$8,replacement_secret_ciphertext=$9,
            version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2 AND version=$3""",
            tenant,
            principal,
            value["version"],
            current.key_id,
            current.nonce,
            current.value,
            replacement.key_id if replacement else None,
            replacement.nonce if replacement else None,
            replacement.value if replacement else None,
        )
        if updated == "UPDATE 0":
            raise ConflictError("storage encryption generation lost its compare-and-swap")
        return True

    async def save_credential(self, tenant: str, principal: str, owner: str, value: dict[str, Any]) -> None:
        """Persist a new/adopted key before any controller-initiated activation."""

        encrypted = self._cipher().encrypt(value["secret_access_key"].encode(), aad=self.aad(tenant, principal))
        state = str(value.get("provider_state", "INACTIVE"))
        predecessor = value.get("previous_access_key_resource_id")
        if state not in {"ACTIVE", "INACTIVE"}:
            raise RuntimeError("storage credential cannot persist a non-usable provider key")
        if predecessor is not None and state != "INACTIVE":
            raise RuntimeError("storage replacement must be inactive before persistence")
        enabled = state == "ACTIVE"
        action = "rotate" if predecessor is not None else None if enabled else "enable"
        await self.pool.execute(
            """INSERT INTO fs2_user_storage
            (tenant_id,principal_id,owner_key,service_account_id,access_key_resource_id,access_key_id,
             secret_key_id,secret_nonce,secret_ciphertext,expires_at,enabled,desired_enabled,
             requested_action,requested_at,previous_access_key_resource_id,rotation_started_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,true,$12,
                   CASE WHEN $12='rotate' THEN now() ELSE NULL END,$13,
                   CASE WHEN $12='rotate' THEN now() ELSE NULL END)""",
            tenant,
            principal,
            owner,
            value["service_account_id"],
            value["access_key_resource_id"],
            value["access_key_id"],
            encrypted.key_id,
            encrypted.nonce,
            encrypted.value,
            value["expires_at"],
            enabled,
            action,
            predecessor,
        )

    async def stage_replacement(
        self,
        tenant: str,
        principal: str,
        value: dict[str, Any],
        *,
        expected_version: int,
    ) -> None:
        """Durably encrypt a prepared inactive key without replacing the live key."""

        encrypted = self._cipher().encrypt(value["secret_access_key"].encode(), aad=self.aad(tenant, principal))
        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET replacement_service_account_id=$4,
            replacement_access_key_resource_id=$5,replacement_access_key_id=$6,
            replacement_secret_key_id=$7,replacement_secret_nonce=$8,
            replacement_secret_ciphertext=$9,replacement_expires_at=$10,
            rotation_started_at=COALESCE(rotation_started_at,now()),version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2 AND version=$3 AND requested_action='rotate'
            AND replacement_access_key_resource_id IS NULL
            AND previous_access_key_resource_id IS NULL""",
            tenant,
            principal,
            expected_version,
            value["service_account_id"],
            value["access_key_resource_id"],
            value["access_key_id"],
            encrypted.key_id,
            encrypted.nonce,
            encrypted.value,
            value["expires_at"],
        )
        if updated == "UPDATE 0":
            raise ConflictError("storage credential rotation lost its staging compare-and-swap")

    async def promote_replacement(self, tenant: str, principal: str, *, expected_version: int) -> None:
        """Promote an activated durable replacement while retaining its predecessor."""

        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET
            previous_access_key_resource_id=access_key_resource_id,
            service_account_id=replacement_service_account_id,
            access_key_resource_id=replacement_access_key_resource_id,
            access_key_id=replacement_access_key_id,
            secret_key_id=replacement_secret_key_id,
            secret_nonce=replacement_secret_nonce,
            secret_ciphertext=replacement_secret_ciphertext,
            expires_at=replacement_expires_at,
            replacement_service_account_id=NULL,replacement_access_key_resource_id=NULL,
            replacement_access_key_id=NULL,replacement_secret_key_id=NULL,
            replacement_secret_nonce=NULL,replacement_secret_ciphertext=NULL,
            replacement_expires_at=NULL,enabled=true,revoked_at=NULL,
            disclosure_consumed_at=NULL,version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2 AND version=$3
            AND requested_action='rotate' AND replacement_access_key_resource_id IS NOT NULL
            AND previous_access_key_resource_id IS NULL""",
            tenant,
            principal,
            expected_version,
        )
        if updated == "UPDATE 0":
            raise ConflictError("storage credential rotation lost its promotion compare-and-swap")

    async def complete_rotation(self, tenant: str, principal: str, *, expected_version: int) -> None:
        """Clear the predecessor only after provider deactivation succeeds."""

        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET previous_access_key_resource_id=NULL,
            rotation_started_at=NULL,
            requested_action=CASE WHEN desired_enabled THEN NULL ELSE 'disable' END,
            requested_at=CASE WHEN desired_enabled THEN NULL ELSE now() END,
            version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2 AND version=$3
            AND requested_action='rotate' AND previous_access_key_resource_id IS NOT NULL""",
            tenant,
            principal,
            expected_version,
        )
        if updated == "UPDATE 0":
            raise ConflictError("storage credential rotation lost its completion compare-and-swap")

    async def request_action(self, tenant: str, principal: str, action: str) -> None:
        if action not in {"rotate", "revoke"}:
            raise ValueError("unsupported storage credential action")
        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET requested_action=$3,requested_at=now(),
            desired_enabled=CASE WHEN $3='rotate' THEN true ELSE false END,
            rotation_started_at=CASE WHEN $3='rotate' THEN now() ELSE rotation_started_at END,
            version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2 AND requested_action IS NULL""",
            tenant,
            principal,
            action,
        )
        if updated == "UPDATE 0":
            raise ConflictError("storage credentials are not ready or another action is pending")

    async def request_rotation_if_due(self, tenant: str, principal: str, cutoff: datetime) -> bool:
        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET requested_action='rotate',requested_at=now(),
            rotation_started_at=now(),version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2 AND requested_action IS NULL
            AND enabled AND desired_enabled AND revoked_at IS NULL AND expires_at <= $3""",
            tenant,
            principal,
            cutoff,
        )
        return updated != "UPDATE 0"

    async def request_rotation_for_provider_state(self, tenant: str, principal: str) -> bool:
        """CAS a repair rotation when DB-enabled provider state is unusable."""

        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET requested_action='rotate',requested_at=now(),
            rotation_started_at=now(),version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2 AND requested_action IS NULL
            AND enabled AND desired_enabled AND revoked_at IS NULL""",
            tenant,
            principal,
        )
        return updated != "UPDATE 0"

    async def complete_action(
        self,
        tenant: str,
        principal: str,
        *,
        expected_action: str,
        enabled: bool,
        revoked: bool = False,
        expected_version: int,
    ) -> None:
        if expected_action not in {"enable", "disable", "revoke", "suspend"}:
            raise ValueError("unsupported storage credential completion")
        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET enabled=$5,
            desired_enabled=CASE WHEN $4='suspend' THEN desired_enabled ELSE $5 END,
            revoked_at=CASE WHEN $5 THEN NULL WHEN $6 THEN now() ELSE revoked_at END,
            requested_action=NULL,requested_at=NULL,
            version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2 AND version=$3 AND requested_action=$4""",
            tenant,
            principal,
            expected_version,
            expected_action,
            enabled,
            revoked,
        )
        if updated == "UPDATE 0":
            raise ConflictError("storage credential action lost its completion compare-and-swap")

    async def request_suspended(self, tenant: str, principal: str) -> None:
        """Disable provider access without overwriting the user's desired state."""

        await self.pool.execute(
            """UPDATE fs2_user_storage SET requested_action='suspend',requested_at=now(),
            version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2 AND requested_action IS NULL AND enabled""",
            tenant,
            principal,
        )

    async def request_enabled(self, tenant: str, principal: str, enabled: bool) -> None:
        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET desired_enabled=$3,
            requested_action=CASE
              WHEN requested_action IS NOT NULL THEN requested_action
              WHEN enabled=$3 THEN NULL
              WHEN $3 THEN 'enable' ELSE 'disable' END,
            requested_at=CASE
              WHEN requested_action IS NOT NULL THEN requested_at
              WHEN enabled=$3 THEN NULL ELSE now() END,
            version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2 AND (NOT $3 OR revoked_at IS NULL)""",
            tenant,
            principal,
            enabled,
        )
        if updated == "UPDATE 0" and enabled:
            raise ConflictError("revoked storage credentials require explicit rotation")

    async def pending_principals(self) -> list[tuple[str, str]]:
        rows = await self.pool.fetch(
            """SELECT tenant_id,principal_id FROM fs2_user_storage
            WHERE requested_action IS NOT NULL OR desired_enabled <> enabled
            ORDER BY requested_at NULLS LAST,tenant_id,principal_id"""
        )
        return [(str(row["tenant_id"]), str(row["principal_id"])) for row in rows]

    async def wait_action(self, tenant: str, principal: str, *, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            while True:
                value = await self.credential(tenant, principal)
                if value is None:
                    raise ConflictError("storage credentials are not ready")
                if (
                    value["requested_action"] is None
                    and value["replacement_access_key_resource_id"] is None
                    and value["previous_access_key_resource_id"] is None
                ):
                    return
                await asyncio.sleep(0.1)

    async def payload_key_usage(self) -> dict[str, int]:
        """Count current and staged generations before SAI-10 retires a key."""

        rows = await self.pool.fetch(
            """SELECT key_id,count(*)::bigint AS credential_count FROM (
              SELECT secret_key_id AS key_id FROM fs2_user_storage
              UNION ALL
              SELECT replacement_secret_key_id AS key_id FROM fs2_user_storage
              WHERE replacement_secret_key_id IS NOT NULL
            ) AS generations GROUP BY key_id ORDER BY key_id"""
        )
        return {str(row["key_id"]): int(row["credential_count"]) for row in rows}

    async def wait_enabled(self, tenant: str, principal: str, *, enabled: bool, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            while True:
                value = await self.credential(tenant, principal)
                if value is None or (
                    value["enabled"] == enabled
                    and value["desired_enabled"] == enabled
                    and value["requested_action"] is None
                ):
                    return
                await asyncio.sleep(0.1)

    async def view(self, tenant: str, principal: str, policy: StoragePolicy) -> UserStorage:
        credential = await self.credential(tenant, principal)
        if not credential:
            return UserStorage(state="disabled" if policy.mode == "disabled" else "pending", **policy.model_dump())
        bucket = await self.bucket(tenant, credential["owner_key"])
        if bucket is None:
            raise ConflictError("storage bucket metadata is unavailable")
        expired = credential["expires_at"] <= datetime.now(UTC)
        state: Literal["ready", "disabled", "revoked", "expired"] = (
            "disabled"
            if policy.mode == "disabled"
            else "expired"
            if expired
            else "revoked"
            if credential["revoked_at"] is not None
            else "ready"
            if credential["enabled"] and credential["requested_action"] is None
            else "disabled"
        )
        return UserStorage(
            state=state,
            mode=policy.mode,
            quota_bytes=bucket["quota_bytes"],
            bucket_name=bucket["bucket_name"],
            endpoint=bucket["endpoint"],
            region=bucket["region"],
            access_key_id=credential["access_key_id"],
            expires_at=credential["expires_at"],
        )

    async def disclose(
        self,
        tenant: str,
        principal: str,
        *,
        actor: str,
        token_id: UUID | None,
    ) -> StorageCredentials:
        value = await self.pool.fetchrow(
            "SELECT * FROM fs2_consume_user_storage_disclosure($1,$2,$3,$4)",
            tenant,
            principal,
            actor,
            token_id,
        )
        if value is None:
            raise ConflictError("storage credential disclosure is unavailable or already consumed")
        secret = (
            self._cipher()
            .decrypt(
                Ciphertext(value["secret_key_id"], value["secret_nonce"], value["secret_ciphertext"]),
                aad=self.aad(tenant, principal),
            )
            .decode()
        )
        return StorageCredentials(
            bucket_name=value["bucket_name"],
            endpoint=value["endpoint"],
            region=value["region"],
            access_key_id=value["access_key_id"],
            secret_access_key=secret,
            expires_at=value["expires_at"],
        )
