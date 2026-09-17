"""Least-privilege PostgreSQL state for customer storage."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from .crypto import Ciphertext, PayloadCipher
from .store import ConflictError
from .user_models import owner_id
from .user_storage_models import StoragePolicy, UserStorage


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
        return PayloadCipher.customer_storage_aad(tenant, principal)

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

    @asynccontextmanager
    async def reconciler_lease(self) -> AsyncIterator[bool]:
        """Hold the one database-backed customer-storage controller lease.

        The activation authority chooses the eligible immutable generation;
        this independent session lock prevents two eligible processes from
        scanning or mutating provider state concurrently during process or
        Pod overlap.  Closing the connection releases the lease, so a crash
        cannot strand leadership and no row deletion is part of recovery.
        """

        async with self.pool.acquire() as connection:
            acquired = bool(
                await connection.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1,34))",
                    "fs2-customer-storage-reconciler-singleton",
                )
            )
            try:
                yield acquired
            finally:
                if acquired:
                    await connection.execute(
                        "SELECT pg_advisory_unlock(hashtextextended($1,34))",
                        "fs2-customer-storage-reconciler-singleton",
                    )

    async def begin_provider_operation(
        self,
        *,
        operation_id: UUID,
        generation: str,
        activation_epoch: int,
        activation_state_head_sha256: str,
        transition_id: str,
        tenant: str,
        principal: str,
        operation_kind: str,
        target_identity: str,
        provider_idempotency_id: UUID,
    ) -> UUID:
        target_digest = hashlib.sha256(target_identity.encode()).hexdigest()
        admitted = await self.pool.fetchval(
            """SELECT fs2_begin_storage_provider_operation(
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
            operation_id,
            generation,
            activation_epoch,
            activation_state_head_sha256,
            transition_id,
            tenant,
            principal,
            operation_kind,
            target_digest,
            provider_idempotency_id,
        )
        if admitted != operation_id:
            raise RuntimeError("provider-operation admission closed for signed drain")
        return operation_id

    async def provider_operation_submitted(
        self, operation_id: UUID, provider_operation_id: str
    ) -> None:
        if not await self.pool.fetchval(
            "SELECT fs2_record_storage_provider_submission($1,$2)",
            operation_id,
            provider_operation_id,
        ):
            raise ConflictError("provider-operation submission identity changed")

    async def provider_operation_terminal(
        self,
        operation_id: UUID,
        provider_operation_id: str,
        *,
        succeeded: bool,
        code: str,
    ) -> None:
        if not await self.pool.fetchval(
            "SELECT fs2_record_storage_provider_terminal($1,$2,$3,$4)",
            operation_id,
            provider_operation_id,
            succeeded,
            code,
        ):
            raise ConflictError("provider-operation terminal identity changed")

    async def finish_provider_operation(self, operation_id: UUID, *, succeeded: bool) -> None:
        if not await self.pool.fetchval(
            "SELECT fs2_finish_storage_provider_operation($1,$2)",
            operation_id,
            succeeded,
        ):
            raise ConflictError("provider-operation terminal compare-and-swap failed")

    async def mark_provider_operation_indeterminate(self, operation_id: UUID) -> None:
        if not await self.pool.fetchval(
            "SELECT fs2_mark_storage_provider_operation_indeterminate($1)", operation_id
        ):
            raise ConflictError("provider-operation uncertainty could not be persisted")

    async def begin_reconciler_drain(self, intent: dict[str, Any]) -> None:
        if not await self.pool.fetchval(
            """SELECT fs2_begin_storage_reconciler_drain(
            $1,$2,$3,$4,$5,$6,$7)""",
            UUID(intent["drain_id"]),
            intent["reconciler_generation"],
            intent["activation_epoch"],
            intent["activation_state_head_sha256"],
            intent["transition_id"],
            datetime.fromisoformat(intent["requested_at"].replace("Z", "+00:00")),
            datetime.fromisoformat(intent["deadline_at"].replace("Z", "+00:00")),
        ):
            raise ConflictError("signed reconciler drain identity changed")

    async def complete_reconciler_drain(self, drain_id: str) -> dict[str, Any] | None:
        value = await self.pool.fetchval(
            "SELECT fs2_complete_storage_reconciler_drain($1)", UUID(drain_id)
        )
        return dict(value) if value is not None else None

    async def reconciler_drain_receipt(self, drain_id: str) -> dict[str, Any] | None:
        value = await self.pool.fetchval(
            "SELECT fs2_storage_reconciler_drain_receipt($1)", UUID(drain_id)
        )
        return dict(value) if value is not None else None

    @staticmethod
    def drain_receipt_canonical_sha256(receipt: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

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
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,32))", tenant)
            row = await connection.fetchrow(
                """SELECT layout_mode,enabled,migration_state,singleton_principal_id
                FROM fs2_storage_policies WHERE tenant_id=$1 FOR UPDATE""",
                tenant,
            )
            if row is not None and row["migration_state"] != "ready" and policy.mode != "disabled":
                raise ConflictError("storage layout migration must complete before this tenant can be enabled")
            current_layout = (
                str(row["layout_mode"]) if row else (default.mode if default.mode != "disabled" else "user")
            )
            requested_layout = current_layout if policy.mode == "disabled" else policy.mode
            has_buckets = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM fs2_storage_buckets WHERE tenant_id=$1)",
                tenant,
            )
            if has_buckets and requested_layout != current_layout:
                raise ConflictError(
                    "storage layout cannot change after provisioning; an explicit data migration is required"
                )
            singleton = row["singleton_principal_id"] if row is not None else None
            if requested_layout == "tenant" and policy.mode != "disabled":
                principals = await connection.fetch(
                    """SELECT principal_id FROM (
                      SELECT principal_id FROM fs2_inference_users WHERE tenant_id=$1
                      UNION
                      SELECT principal_id FROM fs2_user_storage WHERE tenant_id=$1
                    ) AS identities ORDER BY principal_id""",
                    tenant,
                )
                identities = [str(item["principal_id"]) for item in principals]
                if len(identities) != 1:
                    raise ConflictError("tenant storage mode requires exactly one bound principal")
                singleton = identities[0]
            elif requested_layout == "user":
                singleton = None
            await connection.execute(
                """INSERT INTO fs2_storage_policies
                (tenant_id,layout_mode,enabled,quota_bytes,singleton_principal_id)
                VALUES($1,$2,$3,$4,$5) ON CONFLICT(tenant_id) DO UPDATE
                SET layout_mode=$2,enabled=$3,quota_bytes=$4,singleton_principal_id=$5,
                    updated_at=now()""",
                tenant,
                requested_layout,
                policy.mode != "disabled",
                policy.quota_bytes,
                singleton,
            )

    async def bind_tenant_singleton(self, tenant: str, principal: str, *, quota_bytes: int) -> None:
        """Atomically bind a tenant-layout policy to its only identity."""

        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,32))", tenant)
            principals = await connection.fetch(
                """SELECT principal_id FROM (
                  SELECT principal_id FROM fs2_inference_users WHERE tenant_id=$1
                  UNION
                  SELECT principal_id FROM fs2_user_storage WHERE tenant_id=$1
                  UNION SELECT $2::text AS principal_id
                ) AS identities ORDER BY principal_id""",
                tenant,
                principal,
            )
            if [str(item["principal_id"]) for item in principals] != [principal]:
                raise ConflictError("tenant storage mode is restricted to one immutable principal")
            row = await connection.fetchrow(
                """SELECT layout_mode,enabled,migration_state,singleton_principal_id
                FROM fs2_storage_policies WHERE tenant_id=$1 FOR UPDATE""",
                tenant,
            )
            if row is None:
                await connection.execute(
                    """INSERT INTO fs2_storage_policies
                    (tenant_id,layout_mode,enabled,quota_bytes,singleton_principal_id)
                    VALUES($1,'tenant',true,$2,$3)""",
                    tenant,
                    quota_bytes,
                    principal,
                )
                return
            if (
                row["layout_mode"] != "tenant"
                or not row["enabled"]
                or row["migration_state"] != "ready"
                or row["singleton_principal_id"] not in {None, principal}
            ):
                raise ConflictError("tenant storage singleton binding is unavailable")
            if row["singleton_principal_id"] is None:
                await connection.execute(
                    """UPDATE fs2_storage_policies SET singleton_principal_id=$2,updated_at=now()
                    WHERE tenant_id=$1 AND singleton_principal_id IS NULL""",
                    tenant,
                    principal,
                )

    async def bind_user_layout(self, tenant: str, *, quota_bytes: int) -> None:
        """Materialize a default per-user policy before any storage write."""

        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,32))", tenant)
            await connection.execute(
                """INSERT INTO fs2_storage_policies
                (tenant_id,layout_mode,enabled,quota_bytes,singleton_principal_id)
                VALUES($1,'user',true,$2,NULL) ON CONFLICT(tenant_id) DO NOTHING""",
                tenant,
                quota_bytes,
            )
            row = await connection.fetchrow(
                """SELECT layout_mode,enabled,migration_state,singleton_principal_id
                FROM fs2_storage_policies WHERE tenant_id=$1""",
                tenant,
            )
            if (
                row is None
                or row["layout_mode"] != "user"
                or not row["enabled"]
                or row["migration_state"] != "ready"
                or row["singleton_principal_id"] is not None
            ):
                raise ConflictError("per-user storage layout binding is unavailable")

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
            previous_access_key_resource_id,rotation_started_at,disclosure_consumed_at,
            policy_suspension_requested,current_action_id,provider_ownership_verified,version
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

    async def bind_user_identity(self, tenant: str, principal: str) -> None:
        """Persist the server-derived admin route identity for DB authorization."""

        identity = owner_id(tenant, principal)
        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET inference_user_id=$3,version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2
            AND inference_user_id IS NULL""",
            tenant,
            principal,
            identity,
        )
        current = await self.pool.fetchval(
            "SELECT inference_user_id FROM fs2_user_storage WHERE tenant_id=$1 AND principal_id=$2",
            tenant,
            principal,
        )
        if updated == "UPDATE 0" and current is not None and current != identity:
            raise ConflictError("storage owner route identity changed")

    async def record_provider_ownership(
        self,
        tenant: str,
        principal: str,
        *,
        verified: bool,
        expected_version: int,
    ) -> None:
        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET provider_ownership_verified=$4,
            version=version+1,updated_at=clock_timestamp()
            WHERE tenant_id=$1 AND principal_id=$2 AND version=$3""",
            tenant,
            principal,
            expected_version,
            verified,
        )
        if updated == "UPDATE 0":
            raise ConflictError("storage provider-ownership proof lost its compare-and-swap")

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
            (tenant_id,principal_id,inference_user_id,owner_key,service_account_id,access_key_resource_id,access_key_id,
             secret_key_id,secret_nonce,secret_ciphertext,expires_at,enabled,desired_enabled,
             requested_action,requested_at,previous_access_key_resource_id,rotation_started_at,
             provider_ownership_verified)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,true,$13,
                   CASE WHEN $13='rotate' THEN now() ELSE NULL END,$14,
                   CASE WHEN $13='rotate' THEN now() ELSE NULL END,$15)""",
            tenant,
            principal,
            owner_id(tenant, principal),
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
            bool(value.get("provider_ownership_verified", False)),
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
            replacement_provider_ownership_verified=$11,
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
            bool(value.get("provider_ownership_verified", False)),
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
            provider_ownership_verified=replacement_provider_ownership_verified,
            replacement_service_account_id=NULL,replacement_access_key_resource_id=NULL,
            replacement_access_key_id=NULL,replacement_secret_key_id=NULL,
            replacement_secret_nonce=NULL,replacement_secret_ciphertext=NULL,
            replacement_expires_at=NULL,replacement_provider_ownership_verified=NULL,
            enabled=true,revoked_at=NULL,
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

    @staticmethod
    async def _complete_durable_action(
        connection: Any,
        *,
        action_id: UUID | None,
        tenant: str,
        principal: str,
        action: str,
    ) -> None:
        if action_id is None:
            return
        row = await connection.fetchrow(
            """SELECT actor,token_id,status FROM fs2_storage_actions
            WHERE id=$1 AND tenant_id=$2 AND principal_id=$3 AND action=$4 FOR UPDATE""",
            action_id,
            tenant,
            principal,
            action,
        )
        if row is None:
            raise ConflictError("storage action outbox identity changed")
        if row["status"] == "succeeded":
            return
        audit_id = await connection.fetchval(
            """INSERT INTO fs2_audit_events
            (actor,tenant_id,token_id,action,target_type,target_id,outcome,detail)
            VALUES($1,$2,$3,$4,'user_storage',$5,'succeeded',
                   jsonb_build_object('action_id',$6::text)) RETURNING id""",
            row["actor"],
            tenant,
            row["token_id"],
            f"storage.credentials.{action}",
            principal,
            str(action_id),
        )
        updated = await connection.execute(
            """UPDATE fs2_storage_actions SET status='succeeded',completed_at=clock_timestamp(),
            audit_event_id=$2 WHERE id=$1 AND status='requested'""",
            action_id,
            audit_id,
        )
        if updated == "UPDATE 0":
            raise ConflictError("storage action terminal outcome lost its compare-and-swap")

    async def complete_rotation(self, tenant: str, principal: str, *, expected_version: int) -> None:
        """Atomically clear a finished rotation and publish its terminal audit."""

        async with self.pool.acquire() as connection, connection.transaction():
            current = await connection.fetchrow(
                """SELECT current_action_id FROM fs2_user_storage
                WHERE tenant_id=$1 AND principal_id=$2 AND version=$3
                AND requested_action='rotate' AND previous_access_key_resource_id IS NOT NULL
                FOR UPDATE""",
                tenant,
                principal,
                expected_version,
            )
            if current is None:
                raise ConflictError("storage credential rotation lost its completion compare-and-swap")
            updated = await connection.execute(
                """UPDATE fs2_user_storage SET previous_access_key_resource_id=NULL,
                rotation_started_at=NULL,
                requested_action=CASE WHEN desired_enabled THEN NULL ELSE 'disable' END,
                requested_at=CASE WHEN desired_enabled THEN NULL ELSE clock_timestamp() END,
                current_action_id=NULL,version=version+1,updated_at=clock_timestamp()
                WHERE tenant_id=$1 AND principal_id=$2 AND version=$3
                AND requested_action='rotate'""",
                tenant,
                principal,
                expected_version,
            )
            if updated == "UPDATE 0":
                raise ConflictError("storage credential rotation lost its completion compare-and-swap")
            await self._complete_durable_action(
                connection,
                action_id=current["current_action_id"],
                tenant=tenant,
                principal=principal,
                action="rotate",
            )

    async def request_action(
        self,
        tenant: str,
        principal: str,
        action: str,
        *,
        token_id: UUID | None,
        operator_session_id: UUID | None,
        idempotency_key: UUID,
    ) -> UUID:
        if action not in {"rotate", "revoke"}:
            raise ValueError("unsupported storage credential action")
        action_id = await self.pool.fetchval(
            "SELECT fs2_request_user_storage_action($1,$2,$3,$4,$5,$6)",
            tenant,
            principal,
            action,
            token_id,
            operator_session_id,
            idempotency_key,
        )
        if action_id is None:
            raise ConflictError("storage action identity is invalid or another action is pending")
        return cast(UUID, action_id)

    async def _request_system_action(
        self,
        tenant: str,
        principal: str,
        action: str,
        predicate: str,
        *predicate_args: Any,
    ) -> bool:
        action_id = uuid4()
        idempotency_key = uuid4()
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """INSERT INTO fs2_storage_actions
                (id,tenant_id,principal_id,action,actor,idempotency_key)
                VALUES($1,$2,$3,$4,'storage-reconciler',$5)""",
                action_id,
                tenant,
                principal,
                action,
                idempotency_key,
            )
            updated = await connection.execute(
                f"""UPDATE fs2_user_storage SET requested_action=$3,requested_at=clock_timestamp(),
                desired_enabled=CASE WHEN $3='rotate' THEN true ELSE false END,
                rotation_started_at=CASE WHEN $3='rotate' THEN clock_timestamp() ELSE rotation_started_at END,
                current_action_id=$4,version=version+1,updated_at=clock_timestamp()
                WHERE tenant_id=$1 AND principal_id=$2 AND requested_action IS NULL
                AND current_action_id IS NULL AND {predicate}""",  # noqa: S608 - fixed internal predicates
                tenant,
                principal,
                action,
                action_id,
                *predicate_args,
            )
            if updated == "UPDATE 0":
                await connection.execute("DELETE FROM fs2_storage_actions WHERE id=$1", action_id)
                return False
        return True

    async def request_rotation_if_due(self, tenant: str, principal: str, cutoff: datetime) -> bool:
        return await self._request_system_action(
            tenant,
            principal,
            "rotate",
            "enabled AND desired_enabled AND revoked_at IS NULL AND expires_at <= $5",
            cutoff,
        )

    async def request_rotation_for_provider_state(self, tenant: str, principal: str) -> bool:
        """CAS a repair rotation when DB-enabled provider state is unusable."""

        return await self._request_system_action(
            tenant,
            principal,
            "rotate",
            "enabled AND desired_enabled AND revoked_at IS NULL",
        )

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
        async with self.pool.acquire() as connection, connection.transaction():
            current = await connection.fetchrow(
                """SELECT current_action_id FROM fs2_user_storage
                WHERE tenant_id=$1 AND principal_id=$2 AND version=$3 AND requested_action=$4
                FOR UPDATE""",
                tenant,
                principal,
                expected_version,
                expected_action,
            )
            if current is None:
                raise ConflictError("storage credential action lost its completion compare-and-swap")
            updated = await connection.execute(
                """UPDATE fs2_user_storage SET enabled=$5,
                desired_enabled=CASE WHEN $4='suspend' THEN desired_enabled ELSE $5 END,
                revoked_at=CASE WHEN $5 THEN NULL WHEN $6 THEN clock_timestamp() ELSE revoked_at END,
                requested_action=NULL,requested_at=NULL,current_action_id=NULL,
                version=version+1,updated_at=clock_timestamp()
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
            if expected_action in {"rotate", "revoke"}:
                await self._complete_durable_action(
                    connection,
                    action_id=current["current_action_id"],
                    tenant=tenant,
                    principal=principal,
                    action=expected_action,
                )

    async def record_inactive_preserving_rotation(
        self,
        tenant: str,
        principal: str,
        *,
        expected_version: int,
    ) -> None:
        """Record a verified provider-off state without consuming rotate intent."""

        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET enabled=false,version=version+1,
            updated_at=clock_timestamp()
            WHERE tenant_id=$1 AND principal_id=$2 AND version=$3
            AND requested_action='rotate' AND NOT desired_enabled""",
            tenant,
            principal,
            expected_version,
        )
        if updated == "UPDATE 0":
            raise ConflictError("storage disable lost its pending-rotation compare-and-swap")

    async def request_suspended(self, tenant: str, principal: str) -> None:
        """Disable provider access without overwriting the user's desired state."""

        await self.pool.execute(
            """UPDATE fs2_user_storage SET requested_action='suspend',requested_at=now(),
            version=version+1,updated_at=now()
            WHERE tenant_id=$1 AND principal_id=$2 AND requested_action IS NULL AND enabled""",
            tenant,
            principal,
        )

    async def request_tenant_suspension(self, tenant: str) -> None:
        await self.pool.execute(
            """UPDATE fs2_user_storage SET policy_suspension_requested=true,
            requested_action=CASE
              WHEN requested_action IN ('rotate','revoke') THEN requested_action
              WHEN enabled THEN 'suspend' ELSE requested_action END,
            requested_at=CASE
              WHEN requested_action IN ('rotate','revoke') THEN requested_at
              WHEN enabled THEN clock_timestamp() ELSE requested_at END,
            version=version+1,updated_at=clock_timestamp()
            WHERE tenant_id=$1""",
            tenant,
        )

    async def complete_policy_suspension(self, tenant: str, principal: str, *, expected_version: int) -> None:
        updated = await self.pool.execute(
            """UPDATE fs2_user_storage SET enabled=false,
            policy_suspension_requested=false,
            requested_action=CASE WHEN requested_action='suspend' THEN NULL ELSE requested_action END,
            requested_at=CASE WHEN requested_action='suspend' THEN NULL ELSE requested_at END,
            version=version+1,updated_at=clock_timestamp()
            WHERE tenant_id=$1 AND principal_id=$2 AND version=$3
            AND policy_suspension_requested""",
            tenant,
            principal,
            expected_version,
        )
        if updated == "UPDATE 0":
            raise ConflictError("tenant storage suspension lost its compare-and-swap")

    async def request_tenant_resume(self, tenant: str) -> None:
        await self.pool.execute(
            """UPDATE fs2_user_storage SET policy_suspension_requested=false,
            requested_action=CASE
              WHEN requested_action IN ('rotate','revoke') THEN requested_action
              WHEN desired_enabled AND NOT enabled AND revoked_at IS NULL THEN 'enable'
              ELSE requested_action END,
            requested_at=CASE
              WHEN requested_action IN ('rotate','revoke') THEN requested_at
              WHEN desired_enabled AND NOT enabled AND revoked_at IS NULL THEN clock_timestamp()
              ELSE requested_at END,
            version=version+1,updated_at=clock_timestamp()
            WHERE tenant_id=$1""",
            tenant,
        )

    async def wait_tenant_suspended(self, tenant: str, *, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            while await self.pool.fetchval(
                """SELECT EXISTS(SELECT 1 FROM fs2_user_storage
                WHERE tenant_id=$1 AND (enabled OR policy_suspension_requested))""",
                tenant,
            ):
                await asyncio.sleep(0.1)

    async def wait_tenant_resumed(self, tenant: str, *, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            while await self.pool.fetchval(
                """SELECT EXISTS(SELECT 1 FROM fs2_user_storage
                WHERE tenant_id=$1 AND (policy_suspension_requested OR
                  (desired_enabled AND revoked_at IS NULL AND
                   (NOT enabled OR requested_action IS NOT NULL))))""",
                tenant,
            ):
                await asyncio.sleep(0.1)

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
               OR policy_suspension_requested OR NOT enabled OR revoked_at IS NOT NULL
            ORDER BY requested_at NULLS LAST,tenant_id,principal_id"""
        )
        return [(str(row["tenant_id"]), str(row["principal_id"])) for row in rows]

    async def wait_action(self, action_id: UUID, *, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            while True:
                status = await self.pool.fetchval("SELECT fs2_storage_action_status($1)", action_id)
                if status == "succeeded":
                    return
                if status != "requested":
                    raise ConflictError("storage action is unavailable")
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
                    and (value["requested_action"] is None or (not enabled and value["requested_action"] == "rotate"))
                    and not value["policy_suspension_requested"]
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
