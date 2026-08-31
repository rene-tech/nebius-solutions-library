"""Durable activation-store interface consumed by the separate controller lane."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from .models import (
    ActivationAction,
    ActivationIntent,
    ActivationIntentStatus,
    ActivationLeaderIdentity,
    ActivationTargetState,
    ClaimedActivationIntent,
    ClaimedOperation,
)
from .store import ConflictError, NotFoundError, StaleLeaseError

_CLAIM_BATCH_SIZE = 16


class PostgresActivationStore:
    """Activation-only DML surface; it never loads payload, PAT, or HMAC keys."""

    def __init__(self, pool: asyncpg.Pool[Any], *, owns_pool: bool) -> None:
        self.pool = pool
        self.owns_pool = owns_pool

    @classmethod
    async def connect(
        cls,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
    ) -> PostgresActivationStore:
        dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
            server_settings={"application_name": "fs2-serve-activation-controller"},
        )
        assert pool is not None
        return cls(pool, owns_pool=True)

    async def close(self) -> None:
        if self.owns_pool:
            await self.pool.close()

    async def ping(self) -> bool:
        async with self.pool.acquire() as connection:
            return bool(await connection.fetchval("SELECT true"))

    async def database_clock(self) -> datetime:
        async with self.pool.acquire() as connection:
            value = await connection.fetchval("SELECT clock_timestamp()")
            assert isinstance(value, datetime)
            return value

    async def activation_controller_ready(self, activation_set_digest: str) -> bool:
        async with self.pool.acquire() as connection:
            return bool(
                await connection.fetchval(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM fs2_activation_controller_status
                        WHERE singleton AND activation_set_digest=$1
                          AND lease_expires_at>clock_timestamp()
                    )
                    """,
                    activation_set_digest,
                )
            )

    @staticmethod
    def _validate_leader(identity: ActivationLeaderIdentity) -> None:
        identity.validate_binding()

    @staticmethod
    def _leader_values(identity: ActivationLeaderIdentity) -> tuple[str, ...]:
        return (
            identity.controller_id,
            identity.pod_namespace,
            identity.pod_name,
            identity.pod_uid,
            identity.service_account_name,
            identity.service_account_uid,
            identity.lease_namespace,
            identity.lease_name,
            identity.lease_uid,
            identity.lease_resource_version,
            identity.lease_holder_identity,
        )

    async def current_activation_controller_fence(self) -> int | None:
        async with self.pool.acquire() as connection:
            value = await connection.fetchval(
                "SELECT fencing_token FROM fs2_activation_controller_status WHERE singleton"
            )
            return None if value is None else int(value)

    async def publish_activation_controller_heartbeat(
        self,
        identity: ActivationLeaderIdentity,
        *,
        activation_set_digest: str,
        lease_expires_at: datetime,
        expected_fencing_token: int | None,
    ) -> int:
        self._validate_leader(identity)
        if (
            len(activation_set_digest) != 64
            or any(character not in "0123456789abcdef" for character in activation_set_digest)
            or lease_expires_at.tzinfo is None
            or lease_expires_at.utcoffset() is None
            or (expected_fencing_token is not None and expected_fencing_token < 1)
        ):
            raise ValueError("activation controller heartbeat is outside the closed bound")
        async with self.pool.acquire() as connection, connection.transaction():
            database_now = await connection.fetchval("SELECT clock_timestamp()")
            assert isinstance(database_now, datetime)
            if lease_expires_at <= database_now:
                raise StaleLeaseError("observed Kubernetes Lease already expired")
            if (lease_expires_at - database_now).total_seconds() > 60:
                raise ValueError("activation controller heartbeat is outside the closed bound")
            current = await connection.fetchrow(
                "SELECT * FROM fs2_activation_controller_status WHERE singleton FOR UPDATE"
            )
            if current is None:
                if expected_fencing_token is not None:
                    raise StaleLeaseError("activation leadership fence is stale")
                token = 1
            else:
                current_token = int(current["fencing_token"])
                if expected_fencing_token != current_token:
                    raise StaleLeaseError("activation leadership fence is stale")
                same_owner = bool(
                    current["controller_id"] == identity.controller_id
                    and current["pod_namespace"] == identity.pod_namespace
                    and current["pod_name"] == identity.pod_name
                    and current["pod_uid"] == identity.pod_uid
                    and current["service_account_name"] == identity.service_account_name
                    and current["service_account_uid"] == identity.service_account_uid
                    and current["lease_namespace"] == identity.lease_namespace
                    and current["lease_name"] == identity.lease_name
                    and current["lease_uid"] == identity.lease_uid
                    and current["lease_holder_identity"] == identity.lease_holder_identity
                )
                if not same_owner and not await connection.fetchval(
                    "SELECT $1::timestamptz<=clock_timestamp()", current["lease_expires_at"]
                ):
                    raise StaleLeaseError("prior activation leader lease is still live")
                if current["lease_uid"] == identity.lease_uid and int(identity.lease_resource_version) <= int(
                    current["lease_resource_version"]
                ):
                    raise StaleLeaseError("observed Kubernetes Lease resourceVersion is stale")
                token = current_token if same_owner else current_token + 1
                await connection.execute("DELETE FROM fs2_activation_controller_status WHERE singleton")
            values = self._leader_values(identity)
            inserted = await connection.fetchval(
                """
                INSERT INTO fs2_activation_controller_status
                    (singleton,controller_id,fencing_token,activation_set_digest,
                     heartbeat_at,lease_expires_at,pod_namespace,pod_name,pod_uid,
                     service_account_name,service_account_uid,lease_namespace,lease_name,
                     lease_uid,lease_resource_version,lease_holder_identity)
                VALUES(true,$1,$2,$3,clock_timestamp(),
                    $4,
                    $5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                RETURNING fencing_token
                """,
                values[0],
                token,
                activation_set_digest,
                lease_expires_at,
                *values[1:],
            )
            assert inserted is not None
            return int(inserted)

    async def clear_activation_controller_heartbeat(
        self, identity: ActivationLeaderIdentity, *, leadership_fencing_token: int
    ) -> None:
        self._validate_leader(identity)
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE fs2_activation_controller_status
                SET lease_expires_at=clock_timestamp()
                WHERE singleton AND controller_id=$1 AND fencing_token=$2
                  AND pod_uid=$3 AND service_account_uid=$4 AND lease_uid=$5
                  AND lease_holder_identity=$6
                """,
                identity.controller_id,
                leadership_fencing_token,
                identity.pod_uid,
                identity.service_account_uid,
                identity.lease_uid,
                identity.lease_holder_identity,
            )

    @staticmethod
    def _target(row: asyncpg.Record) -> ActivationTargetState | None:
        if row["target_uid"] is None:
            return None
        return ActivationTargetState(
            model_id=row["model_id"],
            target_uid=row["target_uid"],
            resource_version=row["target_resource_version"],
            observed_generation=row["target_observed_generation"],
            template_digest=row["target_template_digest"],
            active=row["target_active"],
            observed_at=row["target_observed_at"],
            controller_fencing_token=row["leadership_fencing_token"],
            model_fencing_token=row["model_fencing_token"],
        )

    @classmethod
    def _intent(cls, row: asyncpg.Record) -> ActivationIntent:
        return ActivationIntent(
            id=row["id"],
            operation_id=row["operation_id"],
            operation_attempt=row["operation_attempt"],
            model_id=row["model_id"],
            model_revision=row["model_revision"],
            binding_digest=row["binding_digest"],
            action=ActivationAction(row["action"]),
            status=ActivationIntentStatus(row["status"]),
            requested_at=row["requested_at"],
            available_at=row["available_at"],
            deadline_at=row["deadline_at"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            controller_id=row["controller_id"],
            fencing_token=row["fencing_token"],
            model_fencing_token=row["model_fencing_token"],
            leadership_fencing_token=row["leadership_fencing_token"],
            lease_expires_at=row["lease_expires_at"],
            scale_contract_digest=row["scale_contract_digest"],
            target=cls._target(row),
            error_code=row["error_code"],
        )

    @staticmethod
    async def _event(connection: asyncpg.Connection[Any], row: asyncpg.Record, event: str) -> None:
        await connection.execute(
            """
            INSERT INTO fs2_activation_events(intent_id,event,status,attempt,fencing_token)
            VALUES($1,$2,$3,$4,$5)
            """,
            row["id"],
            event,
            row["status"],
            row["attempt"],
            row["fencing_token"],
        )

    async def _recover_activation_intents(self, connection: asyncpg.Connection[Any]) -> None:
        """Atomically requeue an eligible crashed claim or seal one terminal result."""

        recovered = await connection.fetch(
            """
            WITH boundary AS MATERIALIZED (SELECT clock_timestamp() AS now)
            UPDATE fs2_activation_intents SET
                status=CASE
                  WHEN deadline_at IS NOT NULL AND deadline_at<=boundary.now
                  THEN 'expired'::fs2_activation_status
                  WHEN attempt>=max_attempts THEN 'failed'::fs2_activation_status
                  ELSE 'queued'::fs2_activation_status
                END,
                available_at=CASE
                  WHEN (deadline_at IS NULL OR deadline_at>boundary.now)
                    AND attempt<max_attempts
                  THEN boundary.now ELSE available_at END,
                controller_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                fencing_token=fencing_token+1,
                model_fencing_token=CASE
                  WHEN (deadline_at IS NULL OR deadline_at>boundary.now)
                    AND attempt<max_attempts
                  THEN NULL ELSE model_fencing_token END,
                leadership_fencing_token=CASE
                  WHEN (deadline_at IS NULL OR deadline_at>boundary.now)
                    AND attempt<max_attempts
                  THEN NULL ELSE leadership_fencing_token END,
                error_code=CASE
                  WHEN deadline_at IS NOT NULL AND deadline_at<=boundary.now
                  THEN 'deadline_exceeded'
                  WHEN attempt>=max_attempts THEN 'activation_attempts_exhausted'
                  ELSE 'controller_lease_expired' END
            FROM boundary
            WHERE status='claimed'
              AND (lease_expires_at IS NULL OR lease_expires_at<=boundary.now)
            RETURNING *
            """
        )
        for row in recovered:
            event = {
                "controller_lease_expired": "activation_intent_lease_requeued",
                "deadline_exceeded": "activation_intent_deadline_expired",
                "activation_attempts_exhausted": "activation_intent_attempts_exhausted",
            }[row["error_code"]]
            await self._event(connection, row, event)

        terminal = await connection.fetch(
            """
            WITH boundary AS MATERIALIZED (SELECT clock_timestamp() AS now)
            UPDATE fs2_activation_intents SET
                status=CASE
                  WHEN deadline_at IS NOT NULL AND deadline_at<=boundary.now
                  THEN 'expired'::fs2_activation_status
                  ELSE 'failed'::fs2_activation_status
                END,
                controller_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                fencing_token=fencing_token+1,
                error_code=CASE
                  WHEN deadline_at IS NOT NULL AND deadline_at<=boundary.now
                  THEN 'deadline_exceeded'
                  ELSE 'activation_attempts_exhausted' END
            FROM boundary
            WHERE status='queued'
              AND ((deadline_at IS NOT NULL AND deadline_at<=boundary.now)
                   OR attempt>=max_attempts)
            RETURNING *
            """
        )
        for row in terminal:
            event = (
                "activation_intent_deadline_expired"
                if row["error_code"] == "deadline_exceeded"
                else "activation_intent_attempts_exhausted"
            )
            await self._event(connection, row, event)

    async def ensure_activation_intent(
        self,
        operation: ClaimedOperation,
        *,
        binding_digest: str,
        worker_id: str,
        fencing_token: int,
    ) -> ActivationIntent:
        async with self.pool.acquire() as connection:
            try:
                intent_id = await connection.fetchval(
                    """
                    SELECT fs2_runtime_ensure_activation_intent(
                        $1,$2,$3,$4,$5,$6,$7,$8,$9
                    )
                    """,
                    operation.id,
                    operation.attempt,
                    operation.model_id,
                    operation.model_revision,
                    binding_digest,
                    operation.deadline_at,
                    operation.max_attempts,
                    worker_id,
                    fencing_token,
                )
            except asyncpg.RaiseError as exc:
                if "activation_operation_stale" in str(exc):
                    raise StaleLeaseError("operation activation subject is stale") from None
                if "activation_intent_conflict" in str(exc):
                    raise ConflictError("activation intent subject differs") from None
                raise
            row = await connection.fetchrow("SELECT * FROM fs2_activation_intents WHERE id=$1", intent_id)
            assert row is not None
            return self._intent(row)

    async def get_activation_intent(self, operation_id: UUID) -> ActivationIntent:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM fs2_activation_intents WHERE operation_id=$1", operation_id)
            if row is None:
                raise NotFoundError("activation intent not found")
            return self._intent(row)

    async def _require_current_leader(
        self,
        connection: asyncpg.Connection[Any],
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
    ) -> None:
        self._validate_leader(identity)
        current = await connection.fetchval(
            """
            SELECT fencing_token FROM fs2_activation_controller_status
            WHERE singleton AND controller_id=$1 AND fencing_token=$2
              AND pod_namespace=$3 AND pod_name=$4 AND pod_uid=$5
              AND service_account_name=$6 AND service_account_uid=$7
              AND lease_namespace=$8 AND lease_name=$9 AND lease_uid=$10
              AND lease_resource_version::numeric >= $11::numeric AND lease_holder_identity=$12
              AND lease_expires_at>clock_timestamp()
            FOR SHARE
            """,
            identity.controller_id,
            leadership_fencing_token,
            identity.pod_namespace,
            identity.pod_name,
            identity.pod_uid,
            identity.service_account_name,
            identity.service_account_uid,
            identity.lease_namespace,
            identity.lease_name,
            identity.lease_uid,
            identity.lease_resource_version,
            identity.lease_holder_identity,
        )
        if current is None:
            raise StaleLeaseError("activation leadership identity or fence is stale")

    async def claim_activation_intent(
        self,
        identity: ActivationLeaderIdentity,
        *,
        leadership_fencing_token: int,
        lease_seconds: float,
    ) -> ClaimedActivationIntent | None:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._require_current_leader(connection, identity, leadership_fencing_token)
            await self._recover_activation_intents(connection)
            candidates = await connection.fetch(
                """
                SELECT i.id FROM fs2_activation_intents i
                WHERE i.status='queued' AND i.available_at<=clock_timestamp() AND i.attempt<i.max_attempts
                  AND (i.deadline_at IS NULL OR i.deadline_at>clock_timestamp())
                  AND (
                    (i.action='activate' AND EXISTS (
                        SELECT 1 FROM fs2_operations o WHERE o.id=i.operation_id
                          AND o.model_id=i.model_id AND o.model_revision=i.model_revision
                          AND o.attempt=i.operation_attempt AND o.status='activating'
                          AND o.lease_expires_at>clock_timestamp()
                          AND (o.deadline_at IS NULL OR o.deadline_at>clock_timestamp())
                    ))
                    OR (i.action='deactivate' AND NOT EXISTS (
                        SELECT 1 FROM fs2_operations o WHERE o.model_id=i.model_id
                          AND o.status IN ('queued','activating','running')
                    ))
                  )
                ORDER BY i.available_at,i.requested_at,i.id LIMIT $1
                """,
                _CLAIM_BATCH_SIZE,
            )
            for candidate in candidates:
                model_id = await connection.fetchval(
                    "SELECT model_id FROM fs2_activation_intents WHERE id=$1", candidate["id"]
                )
                if model_id is None:
                    continue
                await connection.execute("SELECT pg_advisory_xact_lock(fs2_activation_model_lock_key($1))", model_id)
                await self._require_current_leader(connection, identity, leadership_fencing_token)
                model_fence = await connection.fetchval(
                    """
                    INSERT INTO fs2_activation_model_fences(model_id,last_issued_fence)
                    VALUES($1,1)
                    ON CONFLICT (model_id) DO UPDATE SET
                        last_issued_fence=fs2_activation_model_fences.last_issued_fence+1
                    RETURNING last_issued_fence
                    """,
                    model_id,
                )
                row = await connection.fetchrow(
                    """
                    UPDATE fs2_activation_intents SET status='claimed',attempt=attempt+1,
                        controller_id=$2,fencing_token=fencing_token+1,heartbeat_at=clock_timestamp(),
                        lease_expires_at=LEAST(
                            clock_timestamp()+make_interval(secs=>$3::double precision),
                            (SELECT lease_expires_at FROM fs2_activation_controller_status WHERE singleton),
                            COALESCE(deadline_at,'infinity'::timestamptz)
                        ),model_fencing_token=$4,leadership_fencing_token=$5,error_code=NULL
                    WHERE id=$1 AND status='queued' RETURNING *
                    """,
                    candidate["id"],
                    identity.controller_id,
                    lease_seconds,
                    model_fence,
                    leadership_fencing_token,
                )
                if row is not None:
                    await self._event(connection, row, "activation_intent_claimed")
                    return ClaimedActivationIntent.model_validate(self._intent(row).model_dump())
            return None

    async def heartbeat_activation_intent(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        lease_seconds: float,
    ) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._require_current_leader(connection, identity, leadership_fencing_token)
            result = await connection.execute(
                """
                UPDATE fs2_activation_intents i SET heartbeat_at=clock_timestamp(),
                    lease_expires_at=LEAST(
                        clock_timestamp()+make_interval(secs=>$4::double precision),
                        (SELECT lease_expires_at FROM fs2_activation_controller_status WHERE singleton),
                        COALESCE(i.deadline_at,'infinity'::timestamptz)
                    )
                WHERE i.id=$1 AND i.controller_id=$2 AND i.fencing_token=$3
                  AND i.leadership_fencing_token=$5
                  AND i.status='claimed' AND i.lease_expires_at>clock_timestamp()
                  AND (i.deadline_at IS NULL OR i.deadline_at>clock_timestamp())
                  AND (
                    i.action='deactivate' OR EXISTS (
                      SELECT 1 FROM fs2_operations o WHERE o.id=i.operation_id
                        AND o.status='activating' AND o.attempt=i.operation_attempt
                        AND o.lease_expires_at>clock_timestamp()
                        AND (o.deadline_at IS NULL OR o.deadline_at>clock_timestamp())
                    )
                  )
                """,
                intent_id,
                controller_id,
                fencing_token,
                lease_seconds,
                leadership_fencing_token,
            )
            if result == "UPDATE 0":
                raise StaleLeaseError("activation intent lease is stale")

    async def activation_wait_budget(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        maximum_seconds: float,
    ) -> float:
        if not 1 <= maximum_seconds <= 1800:
            raise ValueError("activation wait maximum is outside the closed bound")
        async with self.pool.acquire() as connection, connection.transaction():
            await self._require_current_leader(connection, identity, leadership_fencing_token)
            remaining = await connection.fetchval(
                """
                SELECT LEAST(
                    $4::double precision,
                    GREATEST(0::double precision,EXTRACT(EPOCH FROM
                        (COALESCE(deadline_at,clock_timestamp()+make_interval(secs=>$4::double precision))
                         - clock_timestamp())))
                )
                FROM fs2_activation_intents
                WHERE id=$1 AND controller_id=$2 AND fencing_token=$3 AND status='claimed'
                  AND leadership_fencing_token=$5
                  AND lease_expires_at>clock_timestamp()
                  AND (deadline_at IS NULL OR deadline_at>clock_timestamp())
                """,
                intent_id,
                controller_id,
                fencing_token,
                maximum_seconds,
                leadership_fencing_token,
            )
        if remaining is None or float(remaining) <= 0:
            raise StaleLeaseError("activation wait budget is stale")
        return float(remaining)

    async def complete_activation_intent(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        scale_contract_digest: str,
        target: ActivationTargetState,
    ) -> ActivationIntent:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._require_current_leader(connection, identity, leadership_fencing_token)
            await connection.execute("SELECT pg_advisory_xact_lock(fs2_activation_model_lock_key($1))", target.model_id)
            current = await connection.fetchrow(
                "SELECT * FROM fs2_activation_intents WHERE id=$1 FOR UPDATE", intent_id
            )
            if current is None:
                raise StaleLeaseError("activation intent lease is stale")
            if current["status"] in {"ready", "failed", "expired"}:
                if current["status"] == "ready" and (
                    current["model_id"] != target.model_id
                    or current["scale_contract_digest"] != scale_contract_digest
                    or current["target_uid"] != target.target_uid
                    or current["target_resource_version"] != target.resource_version
                    or current["target_observed_generation"] != target.observed_generation
                    or current["target_template_digest"] != target.template_digest
                    or current["target_active"] is not target.active
                ):
                    raise StaleLeaseError("activation terminal result differs from replay")
                if current["status"] == "failed" and current["error_code"] != "stale_model_fence":
                    raise StaleLeaseError("activation terminal failure differs from replay")
                return self._intent(current)
            if target.controller_fencing_token != leadership_fencing_token:
                raise StaleLeaseError("activation leadership fence differs from target")
            if (
                current["controller_id"] != controller_id
                or current["fencing_token"] != fencing_token
                or current["leadership_fencing_token"] != leadership_fencing_token
                or current["status"] != "claimed"
                or current["lease_expires_at"] is None
                or not await connection.fetchval(
                    "SELECT $1::timestamptz>clock_timestamp()", current["lease_expires_at"]
                )
                or current["model_id"] != target.model_id
                or current["model_fencing_token"] is None
                or (current["action"] == "activate") is not target.active
            ):
                raise StaleLeaseError("activation intent lease is stale")
            if current["action"] == "activate" and not await connection.fetchval(
                """
                SELECT EXISTS(
                  SELECT 1 FROM fs2_operations
                  WHERE id=$1 AND status='activating' AND attempt=$2
                    AND lease_expires_at>clock_timestamp()
                    AND (deadline_at IS NULL OR deadline_at>clock_timestamp())
                )
                """,
                current["operation_id"],
                current["operation_attempt"],
            ):
                raise StaleLeaseError("activation demand lease is stale")
            durable = await connection.fetchrow(
                "SELECT * FROM fs2_activation_target_state WHERE model_id=$1 FOR UPDATE",
                target.model_id,
            )
            stale_target = bool(
                durable is not None
                and (
                    durable["target_uid"] != target.target_uid
                    or durable["template_digest"] != target.template_digest
                    or int(current["model_fencing_token"]) <= int(durable["model_fencing_token"])
                    or leadership_fencing_token < int(durable["controller_fencing_token"])
                    or target.observed_generation < int(durable["observed_generation"])
                    or (
                        target.observed_generation == int(durable["observed_generation"])
                        and (
                            target.resource_version != durable["resource_version"]
                            or target.active is not durable["active"]
                        )
                    )
                )
            )
            if stale_target:
                row = await connection.fetchrow(
                    """
                    UPDATE fs2_activation_intents
                    SET status='failed',controller_id=NULL,heartbeat_at=NULL,
                        lease_expires_at=NULL,scale_contract_digest=$2,
                        error_code='stale_model_fence'
                    WHERE id=$1 RETURNING *
                    """,
                    intent_id,
                    scale_contract_digest,
                )
                assert row is not None
                await self._event(connection, row, "activation_intent_stale_fence")
                return self._intent(row)
            await connection.execute(
                """
                INSERT INTO fs2_activation_target_state
                    (model_id,target_uid,resource_version,observed_generation,template_digest,active,observed_at,
                     controller_fencing_token,model_fencing_token)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (model_id) DO UPDATE SET target_uid=EXCLUDED.target_uid,
                    resource_version=EXCLUDED.resource_version,
                    observed_generation=EXCLUDED.observed_generation,
                    template_digest=EXCLUDED.template_digest,active=EXCLUDED.active,
                    observed_at=EXCLUDED.observed_at,
                    controller_fencing_token=EXCLUDED.controller_fencing_token,
                    model_fencing_token=EXCLUDED.model_fencing_token
                """,
                target.model_id,
                target.target_uid,
                target.resource_version,
                target.observed_generation,
                target.template_digest,
                target.active,
                target.observed_at,
                leadership_fencing_token,
                current["model_fencing_token"],
            )
            row = await connection.fetchrow(
                """
                UPDATE fs2_activation_intents SET status='ready',controller_id=NULL,
                    heartbeat_at=NULL,lease_expires_at=NULL,scale_contract_digest=$2,
                    target_uid=$3,target_resource_version=$4,
                    target_observed_generation=$5,target_template_digest=$6,
                    target_active=$7,target_observed_at=$8,error_code=NULL
                WHERE id=$1 RETURNING *
                """,
                intent_id,
                scale_contract_digest,
                target.target_uid,
                target.resource_version,
                target.observed_generation,
                target.template_digest,
                target.active,
                target.observed_at,
            )
            assert row is not None
            await self._event(connection, row, "activation_intent_ready")
            return self._intent(row)

    async def retry_activation_intent(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        available_at: datetime,
        error_code: str,
    ) -> ActivationIntent:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._require_current_leader(connection, identity, leadership_fencing_token)
            row = await connection.fetchrow(
                """
                UPDATE fs2_activation_intents SET
                    status=CASE
                      WHEN deadline_at IS NOT NULL AND deadline_at<=clock_timestamp()
                      THEN 'expired'::fs2_activation_status
                      WHEN attempt>=max_attempts THEN 'failed'::fs2_activation_status
                      ELSE 'queued'::fs2_activation_status
                    END,
                    available_at=$4,controller_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                    error_code=CASE
                      WHEN deadline_at IS NOT NULL AND deadline_at<=clock_timestamp()
                      THEN 'deadline_exceeded' ELSE $5 END
                WHERE id=$1 AND controller_id=$2 AND fencing_token=$3 AND status='claimed'
                  AND leadership_fencing_token=$6
                  AND lease_expires_at>clock_timestamp()
                RETURNING *
                """,
                intent_id,
                controller_id,
                fencing_token,
                available_at,
                error_code,
                leadership_fencing_token,
            )
            if row is None:
                raise StaleLeaseError("activation intent lease is stale")
            await self._event(connection, row, "activation_intent_retried")
            return self._intent(row)

    async def request_scale_down(
        self,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        model_id: str,
        model_revision: str,
        binding_digest: str,
        idle_before: datetime,
        max_attempts: int,
    ) -> ActivationIntent | None:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._require_current_leader(connection, identity, leadership_fencing_token)
            await connection.execute("SELECT pg_advisory_xact_lock(fs2_activation_model_lock_key($1))", model_id)
            busy = await connection.fetchval(
                """
                SELECT EXISTS(
                  SELECT 1 FROM fs2_operations WHERE model_id=$1
                    AND status IN ('queued','activating','running')
                ) OR EXISTS(
                  SELECT 1 FROM fs2_activation_intents WHERE model_id=$1
                    AND status IN ('queued','claimed')
                )
                """,
                model_id,
            )
            target = await connection.fetchrow(
                """
                SELECT * FROM fs2_activation_target_state
                WHERE model_id=$1 AND active AND observed_at<=$2
                FOR UPDATE
                """,
                model_id,
                idle_before,
            )
            if busy or target is None:
                return None
            row = await connection.fetchrow(
                """
                INSERT INTO fs2_activation_intents
                    (id,operation_attempt,model_id,model_revision,binding_digest,action,status,max_attempts)
                VALUES($1,0,$2,$3,$4,'deactivate','queued',$5) RETURNING *
                """,
                uuid4(),
                model_id,
                model_revision,
                binding_digest,
                max_attempts,
            )
            assert row is not None
            await self._event(connection, row, "deactivation_intent_queued")
            return self._intent(row)

    async def get_activation_target_state(self, model_id: str) -> ActivationTargetState | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM fs2_activation_target_state WHERE model_id=$1", model_id)
            if row is None:
                return None
            return ActivationTargetState(
                model_id=row["model_id"],
                target_uid=row["target_uid"],
                resource_version=row["resource_version"],
                observed_generation=row["observed_generation"],
                template_digest=row["template_digest"],
                active=row["active"],
                observed_at=row["observed_at"],
                controller_fencing_token=row["controller_fencing_token"],
                model_fencing_token=row["model_fencing_token"],
            )

    @asynccontextmanager
    async def activation_mutation_guard(
        self,
        intent: ClaimedActivationIntent,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
    ) -> AsyncIterator[None]:
        async with self.pool.acquire() as connection:
            key = await connection.fetchval("SELECT fs2_activation_model_lock_key($1)", intent.model_id)
            assert key is not None
            await connection.execute("SELECT pg_advisory_lock($1)", key)
            try:
                async with connection.transaction():
                    await self._validate_activation_mutation(
                        connection,
                        intent,
                        identity=identity,
                        leadership_fencing_token=leadership_fencing_token,
                    )
                yield
                async with connection.transaction():
                    await self._validate_activation_mutation(
                        connection,
                        intent,
                        identity=identity,
                        leadership_fencing_token=leadership_fencing_token,
                    )
            finally:
                await connection.execute("SELECT pg_advisory_unlock($1)", key)

    async def _validate_activation_mutation(
        self,
        connection: asyncpg.Connection[Any],
        intent: ClaimedActivationIntent,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
    ) -> None:
        """Validate one model fence in a short DB-clock transaction."""

        await self._require_current_leader(connection, identity, leadership_fencing_token)
        current = await connection.fetchrow(
            """
            SELECT * FROM fs2_activation_intents
            WHERE id=$1 AND controller_id=$2 AND fencing_token=$3 AND status='claimed'
              AND leadership_fencing_token=$4
              AND lease_expires_at>clock_timestamp()
              AND (deadline_at IS NULL OR deadline_at>clock_timestamp())
            """,
            intent.id,
            intent.controller_id,
            intent.fencing_token,
            leadership_fencing_token,
        )
        if current is None:
            raise StaleLeaseError("activation mutation guard is stale")
        if intent.action is ActivationAction.ACTIVATE:
            valid = await connection.fetchval(
                """
                        SELECT EXISTS(
                          SELECT 1 FROM fs2_operations WHERE id=$1 AND status='activating'
                            AND attempt=$2 AND lease_expires_at>clock_timestamp()
                            AND (deadline_at IS NULL OR deadline_at>clock_timestamp())
                        )
                        """,
                intent.operation_id,
                intent.operation_attempt,
            )
        else:
            valid = not await connection.fetchval(
                """
                        SELECT EXISTS(
                          SELECT 1 FROM fs2_operations WHERE model_id=$1
                            AND status IN ('queued','activating','running')
                        )
                        """,
                intent.model_id,
            )
        if not valid:
            raise StaleLeaseError("activation mutation guard lost its demand invariant")
