"""PostgreSQL consumer for the staged scientific-batch controller."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import asyncpg

from .codec import state_from_value, state_to_json
from .models import (
    PUBLIC_ARTIFACT_ACCESS_CONTEXT,
    AdapterExecutionPlan,
    ArtifactAccessContext,
    ArtifactCommit,
    BatchClaim,
    BatchEvent,
    BatchEventDraft,
    BatchStatus,
    RuntimeArtifactLocalization,
    SchedulingSnapshot,
    ScientificBatchPlan,
    ScientificBatchState,
    VerifiedInputManifest,
)
from .protocols import BatchFenceLostError, BatchRepositoryConflictError

SCIENTIFIC_BATCH_MIGRATION = "0015_scientific_batch_controller.sql"


class ScientificBatchNotFoundError(RuntimeError):
    """A batch is absent or belongs to another tenant."""


def _event_from_record(record: Mapping[str, Any]) -> BatchEvent:
    from .models import BatchEventKind, LifecyclePhase

    draft = BatchEventDraft(
        event_id=str(record["event_id"]),
        operation_id=record["operation_id"],
        batch_id=record["batch_id"],
        workload_id=record["workload_id"],
        kind=BatchEventKind(str(record["kind"])),
        stage_id=record["stage_id"],
        shard_id=record["shard_id"],
        attempt_id=record["attempt_id"],
        phase=None if record["phase"] is None else LifecyclePhase(str(record["phase"])),
        code=record["code"],
    )
    return BatchEvent(sequence=int(record["sequence"]), occurred_at=record["occurred_at"], draft=draft)


class PostgresScientificBatchRepository:
    """Fenced state/event repository sharing the gateway's asyncpg pool.

    The existing ``fs2_operations`` row remains the public durable operation.
    This repository stores only orchestration state and artifact-service foreign
    keys; it does not duplicate request payloads, artifact metadata, or public
    result documents.
    """

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self.pool = pool

    @staticmethod
    def _state(record: Mapping[str, Any]) -> ScientificBatchState:
        state = state_from_value(record["state"])
        if (
            state.operation_id != record["operation_id"]
            or state.batch_id != record["batch_id"]
            or state.workload_id != record["workload_id"]
            or state.tenant_id != record["tenant_id"]
            or state.model_id != record["model_id"]
            or state.variant_id != record["variant_id"]
            or state.input_artifact_id != record["input_artifact_id"]
            or state.status.value != record["status"]
            or state.revision != record["revision"]
            or state.cancel_requested != record["cancel_requested"]
        ):
            raise RuntimeError("stored scientific-batch columns and state differ")
        return state

    @staticmethod
    def _translate(error: asyncpg.PostgresError) -> RuntimeError | None:
        if isinstance(error, asyncpg.UniqueViolationError | asyncpg.CheckViolationError):
            return BatchRepositoryConflictError("database rejected conflicting scientific-batch state")
        return None

    async def create(
        self,
        *,
        operation_id: UUID,
        tenant_id: str,
        model_id: str,
        variant_id: str,
        input_artifact_id: UUID,
        plan: ScientificBatchPlan,
        scheduling: SchedulingSnapshot,
        execution_plan: AdapterExecutionPlan | None = None,
        access_context: ArtifactAccessContext = PUBLIC_ARTIFACT_ACCESS_CONTEXT,
        input_manifest: VerifiedInputManifest | None = None,
        runtime_artifacts: tuple[RuntimeArtifactLocalization, ...] = (),
    ) -> ScientificBatchState:
        proposed = ScientificBatchState.admit(
            operation_id=operation_id,
            tenant_id=tenant_id,
            model_id=model_id,
            variant_id=variant_id,
            input_artifact_id=input_artifact_id,
            plan=plan,
            scheduling=scheduling,
            execution_plan=execution_plan,
            access_context=access_context,
            input_manifest=input_manifest,
            runtime_artifacts=runtime_artifacts,
        )
        payload = state_to_json(proposed)
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                operation = await connection.fetchrow(
                    "SELECT tenant_id,model_id,protocol,status FROM fs2_operations WHERE id=$1 FOR UPDATE",
                    operation_id,
                )
                if operation is None or operation["tenant_id"] != tenant_id:
                    raise ScientificBatchNotFoundError("operation does not exist")
                if operation["model_id"] != model_id or operation["protocol"] != "scientific-batch-v1":
                    raise BatchRepositoryConflictError("operation is not the requested scientific workload")
                if operation["status"] not in {"queued", "running"}:
                    raise BatchRepositoryConflictError("terminal operation cannot admit a scientific batch")
                await connection.execute(
                    """
                    INSERT INTO fs2_scientific_batches(
                        operation_id,batch_id,workload_id,tenant_id,model_id,variant_id,input_artifact_id,
                        scheduling_digest,status,revision,cancel_requested,state
                    ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb)
                    ON CONFLICT (operation_id) DO NOTHING
                    """,
                    proposed.operation_id,
                    proposed.batch_id,
                    proposed.workload_id,
                    proposed.tenant_id,
                    proposed.model_id,
                    proposed.variant_id,
                    proposed.input_artifact_id,
                    proposed.scheduling.digest,
                    proposed.status.value,
                    proposed.revision,
                    proposed.cancel_requested,
                    payload,
                )
                record = await connection.fetchrow(
                    "SELECT * FROM fs2_scientific_batches WHERE operation_id=$1 FOR SHARE",
                    operation_id,
                )
                if record is None:
                    raise RuntimeError("scientific-batch insert did not produce a durable row")
                current = self._state(record)
                if (
                    current.tenant_id != tenant_id
                    or current.model_id != model_id
                    or current.variant_id != variant_id
                    or current.input_artifact_id != input_artifact_id
                    or current.plan != plan
                    or current.scheduling != scheduling
                    or current.execution_plan != execution_plan
                    or current.access_context != access_context
                    or current.input_manifest != input_manifest
                    or current.runtime_artifacts != runtime_artifacts
                ):
                    raise BatchRepositoryConflictError("operation already has another frozen batch admission")
                return current
        except asyncpg.PostgresError as error:
            translated = self._translate(error)
            if translated is not None:
                raise translated from None
            raise

    async def claim_next(
        self,
        *,
        controller_id: str,
        lease_seconds: float,
        now: datetime,
    ) -> BatchClaim | None:
        del now  # PostgreSQL is the lease clock authority.
        async with self.pool.acquire() as connection, connection.transaction():
            record = await connection.fetchrow(
                """
                WITH candidate AS (
                    SELECT batch.operation_id
                    FROM fs2_scientific_batches batch
                    JOIN fs2_operations operation ON operation.id=batch.operation_id
                    WHERE (
                        (batch.status IN ('queued','running') AND operation.status IN ('queued','running'))
                        OR (
                            batch.status IN ('succeeded','failed','cancelled')
                            AND operation.status IN ('succeeded','failed','cancelled')
                            AND (batch.state->>'result_published')::boolean=false
                        )
                    )
                      AND (batch.lease_expires_at IS NULL OR batch.lease_expires_at<=clock_timestamp())
                    ORDER BY operation.accepted_at,batch.operation_id
                    FOR UPDATE OF batch SKIP LOCKED
                    LIMIT 1
                )
                UPDATE fs2_scientific_batches batch
                SET controller_id=$1,
                    fencing_token=batch.fencing_token+1,
                    lease_expires_at=clock_timestamp()+make_interval(secs=>$2::double precision),
                    updated_at=clock_timestamp()
                FROM candidate
                WHERE batch.operation_id=candidate.operation_id
                RETURNING batch.operation_id,batch.controller_id,batch.fencing_token,batch.lease_expires_at
                """,
                controller_id,
                lease_seconds,
            )
        if record is None:
            return None
        return BatchClaim(
            operation_id=record["operation_id"],
            controller_id=record["controller_id"],
            fencing_token=record["fencing_token"],
            lease_expires_at=record["lease_expires_at"],
        )

    @staticmethod
    async def _claimed_record(
        connection: asyncpg.Connection[Any], claim: BatchClaim, *, shared: bool = False
    ) -> asyncpg.Record:
        query = (
            """
            SELECT * FROM fs2_scientific_batches
            WHERE operation_id=$1 AND controller_id=$2 AND fencing_token=$3
              AND lease_expires_at>clock_timestamp()
            FOR SHARE
            """
            if shared
            else """
            SELECT * FROM fs2_scientific_batches
            WHERE operation_id=$1 AND controller_id=$2 AND fencing_token=$3
              AND lease_expires_at>clock_timestamp()
            FOR UPDATE
            """
        )
        record = await connection.fetchrow(
            query,
            claim.operation_id,
            claim.controller_id,
            claim.fencing_token,
        )
        if record is None:
            raise BatchFenceLostError("scientific-batch claim is stale")
        return record

    async def assert_fence(self, operation_id: UUID, *, controller_id: str, fencing_token: int) -> None:
        """Guard an imminent Kubernetes mutation with the current DB lease."""

        async with self.pool.acquire() as connection:
            current = await connection.fetchval(
                """
                SELECT true FROM fs2_scientific_batches
                WHERE operation_id=$1 AND controller_id=$2 AND fencing_token=$3
                  AND lease_expires_at>clock_timestamp()
                """,
                operation_id,
                controller_id,
                fencing_token,
            )
        if not current:
            raise BatchFenceLostError("scientific-batch claim is stale")

    async def load(self, claim: BatchClaim) -> ScientificBatchState:
        async with self.pool.acquire() as connection:
            return self._state(await self._claimed_record(connection, claim, shared=True))

    async def replace(
        self,
        claim: BatchClaim,
        *,
        expected_revision: int,
        record: ScientificBatchState,
        events: tuple[BatchEventDraft, ...],
        now: datetime,
    ) -> ScientificBatchState:
        del now  # PostgreSQL is the event and transition clock authority.
        try:
            async with self.pool.acquire() as connection, connection.transaction():
                current_record = await self._claimed_record(connection, claim)
                current = self._state(current_record)
                if current.revision != expected_revision or record.revision != expected_revision + 1:
                    raise BatchRepositoryConflictError("scientific-batch revision changed")
                # Cancellation is an asynchronously asserted level signal,
                # not a competing state-machine transition. A reconciler that
                # was already talking to Kubernetes must carry it forward so
                # its just-created deterministic resources are recorded and
                # deleted by the next reconciliation instead of orphaned.
                if current.cancel_requested and not record.cancel_requested:
                    record = replace(record, cancel_requested=True)
                if (
                    record.operation_id != current.operation_id
                    or record.batch_id != current.batch_id
                    or record.workload_id != current.workload_id
                    or record.tenant_id != current.tenant_id
                    or record.model_id != current.model_id
                    or record.plan != current.plan
                    or record.scheduling != current.scheduling
                    or record.execution_plan != current.execution_plan
                    or record.access_context != current.access_context
                    or record.input_manifest != current.input_manifest
                    or record.runtime_artifacts != current.runtime_artifacts
                ):
                    raise BatchRepositoryConflictError("immutable scientific-batch admission changed")
                payload = state_to_json(record)
                updated = await connection.fetchrow(
                    """
                    UPDATE fs2_scientific_batches
                    SET status=$4,revision=$5,cancel_requested=$6,state=$7::jsonb,updated_at=clock_timestamp()
                    WHERE operation_id=$1 AND controller_id=$2 AND fencing_token=$3 AND revision=$8
                      AND lease_expires_at>clock_timestamp()
                    RETURNING *
                    """,
                    claim.operation_id,
                    claim.controller_id,
                    claim.fencing_token,
                    record.status.value,
                    record.revision,
                    record.cancel_requested,
                    payload,
                    expected_revision,
                )
                if updated is None:
                    raise BatchFenceLostError("scientific-batch claim or revision is stale")
                for event in events:
                    await connection.execute(
                        """
                        INSERT INTO fs2_scientific_batch_events(
                            event_id,operation_id,batch_id,workload_id,kind,stage_id,
                            shard_id,attempt_id,phase,code
                        ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                        ON CONFLICT (operation_id,event_id) DO NOTHING
                        """,
                        event.event_id,
                        event.operation_id,
                        event.batch_id,
                        event.workload_id,
                        event.kind.value,
                        event.stage_id,
                        event.shard_id,
                        event.attempt_id,
                        event.phase.value if event.phase is not None else None,
                        event.code,
                    )
                await self._project_operation(connection, current, record)
                return self._state(updated)
        except asyncpg.PostgresError as error:
            translated = self._translate(error)
            if translated is not None:
                raise translated from None
            raise

    @staticmethod
    async def _project_operation(
        connection: asyncpg.Connection[Any],
        previous: ScientificBatchState,
        current: ScientificBatchState,
    ) -> None:
        if current.status == previous.status:
            return
        if current.status is BatchStatus.RUNNING:
            operation = await connection.fetchrow(
                """
                UPDATE fs2_operations
                SET status='running',started_at=COALESCE(started_at,clock_timestamp()),outcome=NULL,
                    semantic_outcome=NULL,error_code=NULL,error_detail=NULL
                WHERE id=$1 AND protocol='scientific-batch-v1' AND status='queued'
                RETURNING attempt
                """,
                current.operation_id,
            )
            if operation is not None:
                await connection.execute(
                    "INSERT INTO fs2_operation_events(operation_id,event,status,attempt) "
                    "VALUES($1,'scientific_batch_running','running',$2)",
                    current.operation_id,
                    operation["attempt"],
                )
            return
        if not current.status.terminal:
            return
        semantic = "passed" if current.status is BatchStatus.SUCCEEDED else "failed"
        http_status = (
            200 if current.status is BatchStatus.SUCCEEDED else 409 if current.status is BatchStatus.CANCELLED else 422
        )
        operation = await connection.fetchrow(
            """
            UPDATE fs2_operations
            SET status=$2::fs2_operation_status,completed_at=clock_timestamp(),outcome=$2::text,semantic_outcome=$3,
                http_status=$4,error_code=$5,error_detail=NULL,worker_id=NULL,
                heartbeat_at=NULL,lease_expires_at=NULL,reserved_gpu_seconds=0
            WHERE id=$1 AND protocol='scientific-batch-v1' AND status IN ('queued','running')
            RETURNING attempt
            """,
            current.operation_id,
            current.status.value,
            semantic,
            http_status,
            current.failure_code,
        )
        if operation is not None:
            await connection.execute(
                "INSERT INTO fs2_operation_events(operation_id,event,status,attempt) VALUES($1,$2,$3,$4)",
                current.operation_id,
                f"scientific_batch_{current.status.value}",
                current.status.value,
                operation["attempt"],
            )

    async def artifact_commits(self, claim: BatchClaim, *, stage_id: str) -> tuple[ArtifactCommit, ...]:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._claimed_record(connection, claim, shared=True)
            records = await connection.fetch(
                """
                SELECT operation_id,stage_id,attempt_id,logical_artifact_id,handoff_artifact_id,
                       handoff_digest,handoff_size_bytes,handoff_media_type,handoff_compression,
                       manifest_artifact_id,validation_artifact_id,manifest_digest,validation_digest,
                       collector_id,validator_id,committed_at,validated_at,semantic_valid
                FROM fs2_scientific_attempt_commits
                WHERE operation_id=$1 AND stage_id=$2
                ORDER BY attempt_id
                """,
                claim.operation_id,
                stage_id,
            )
        return tuple(
            ArtifactCommit(
                operation_id=record["operation_id"],
                stage_id=record["stage_id"],
                attempt_ids=(record["attempt_id"],),
                logical_artifact_id=record["logical_artifact_id"],
                handoff_artifact_id=record["handoff_artifact_id"],
                handoff_digest=record["handoff_digest"],
                handoff_size_bytes=record["handoff_size_bytes"],
                handoff_media_type=record["handoff_media_type"],
                handoff_compression=record["handoff_compression"],
                manifest_artifact_id=record["manifest_artifact_id"],
                validation_artifact_id=record["validation_artifact_id"],
                manifest_digest=record["manifest_digest"],
                validation_digest=record["validation_digest"],
                committed_at=record["committed_at"],
                validated_at=record["validated_at"],
                semantic_valid=record["semantic_valid"],
                collector_id=record["collector_id"],
                validator_id=record["validator_id"],
            )
            for record in records
        )

    async def list_artifact_commits(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
    ) -> tuple[ArtifactCommit, ...]:
        """Read the immutable stage ledger after controller terminalization."""

        async with self.pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT commit.* FROM fs2_scientific_attempt_commits commit
                JOIN fs2_scientific_batches batch ON batch.operation_id=commit.operation_id
                WHERE commit.operation_id=$1 AND batch.tenant_id=$2
                ORDER BY commit.stage_id,commit.attempt_id
                """,
                operation_id,
                tenant_id,
            )
        return tuple(self._artifact_commit(record) for record in records)

    @staticmethod
    def _artifact_commit(record: Mapping[str, Any]) -> ArtifactCommit:
        return ArtifactCommit(
            operation_id=record["operation_id"],
            stage_id=record["stage_id"],
            attempt_ids=(record["attempt_id"],),
            logical_artifact_id=record["logical_artifact_id"],
            handoff_artifact_id=record["handoff_artifact_id"],
            handoff_digest=record["handoff_digest"],
            handoff_size_bytes=record["handoff_size_bytes"],
            handoff_media_type=record["handoff_media_type"],
            handoff_compression=record["handoff_compression"],
            manifest_artifact_id=record["manifest_artifact_id"],
            validation_artifact_id=record["validation_artifact_id"],
            manifest_digest=record["manifest_digest"],
            validation_digest=record["validation_digest"],
            committed_at=record["committed_at"],
            validated_at=record["validated_at"],
            semantic_valid=record["semantic_valid"],
            collector_id=record["collector_id"],
            validator_id=record["validator_id"],
        )

    async def record_artifact_commit(
        self,
        commit: ArtifactCommit,
        *,
        tenant_id: str,
        manifest_artifact_id: UUID,
        validation_artifact_id: UUID,
    ) -> ArtifactCommit:
        """Link a validated stage commit to artifact-service-owned rows."""

        try:
            async with self.pool.acquire() as connection, connection.transaction():
                batch = await connection.fetchrow(
                    "SELECT * FROM fs2_scientific_batches WHERE operation_id=$1 AND tenant_id=$2 FOR UPDATE",
                    commit.operation_id,
                    tenant_id,
                )
                if batch is None:
                    raise ScientificBatchNotFoundError("scientific batch does not exist")
                state = self._state(batch)
                stage = state.stage(commit.stage_id)
                attempt = next(
                    (item for item in stage.attempts if item.attempt_id == commit.attempt_ids[0]),
                    None,
                )
                if (
                    attempt is None
                    or attempt is not stage.latest_attempt(attempt.shard_id)
                    or attempt.outcome.value != "active"
                    or state.execution_plan is None
                ):
                    raise BatchRepositoryConflictError("stage commit references a stale attempt or logical output")
                invocation = state.execution_plan.invocation(commit.stage_id, attempt.shard_id)
                if (
                    invocation.produces != commit.logical_artifact_id
                    or invocation.collector_id != commit.collector_id
                    or invocation.validator_id != commit.validator_id
                ):
                    raise BatchRepositoryConflictError("stage commit execution identity differs")
                if (manifest_artifact_id, validation_artifact_id) != (
                    commit.manifest_artifact_id,
                    commit.validation_artifact_id,
                ):
                    raise BatchRepositoryConflictError("stage commit artifact IDs differ")
                artifacts = await connection.fetch(
                    """
                    SELECT id,digest,size_bytes,media_type,compression,direction FROM fs2_scientific_artifacts
                    WHERE operation_id=$1 AND tenant_id=$2 AND id=ANY($3::uuid[])
                    """,
                    commit.operation_id,
                    tenant_id,
                    [commit.handoff_artifact_id, manifest_artifact_id, validation_artifact_id],
                )
                by_id = {item["id"]: item for item in artifacts}
                if set(by_id) != {commit.handoff_artifact_id, manifest_artifact_id, validation_artifact_id}:
                    raise BatchRepositoryConflictError("stage commit artifacts are unavailable")
                if any(item["direction"] != "output" for item in by_id.values()):
                    raise BatchRepositoryConflictError("stage commit must reference output artifacts")
                if (
                    by_id[manifest_artifact_id]["digest"] != commit.manifest_digest
                    or by_id[validation_artifact_id]["digest"] != commit.validation_digest
                    or by_id[commit.handoff_artifact_id]["digest"] != commit.handoff_digest
                    or by_id[commit.handoff_artifact_id]["size_bytes"] != commit.handoff_size_bytes
                    or by_id[commit.handoff_artifact_id]["media_type"] != commit.handoff_media_type
                    or by_id[commit.handoff_artifact_id]["compression"] != commit.handoff_compression
                ):
                    raise BatchRepositoryConflictError("stage commit digest differs from artifact metadata")
                await connection.execute(
                    """
                    INSERT INTO fs2_scientific_attempt_commits(
                        operation_id,stage_id,attempt_id,logical_artifact_id,handoff_artifact_id,
                        handoff_digest,handoff_size_bytes,handoff_media_type,handoff_compression,
                        manifest_artifact_id,validation_artifact_id,
                        manifest_digest,validation_digest,collector_id,validator_id,
                        committed_at,validated_at,semantic_valid
                    ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                    ON CONFLICT (operation_id,stage_id,attempt_id) DO NOTHING
                    """,
                    commit.operation_id,
                    commit.stage_id,
                    commit.attempt_ids[0],
                    commit.logical_artifact_id,
                    commit.handoff_artifact_id,
                    commit.handoff_digest,
                    commit.handoff_size_bytes,
                    commit.handoff_media_type,
                    commit.handoff_compression,
                    manifest_artifact_id,
                    validation_artifact_id,
                    commit.manifest_digest,
                    commit.validation_digest,
                    commit.collector_id,
                    commit.validator_id,
                    commit.committed_at,
                    commit.validated_at,
                    commit.semantic_valid,
                )
                stored = await connection.fetchrow(
                    "SELECT * FROM fs2_scientific_attempt_commits "
                    "WHERE operation_id=$1 AND stage_id=$2 AND attempt_id=$3",
                    commit.operation_id,
                    commit.stage_id,
                    commit.attempt_ids[0],
                )
                if stored is None:
                    raise RuntimeError("stage commit insert did not produce a durable row")
                comparable = (
                    stored["attempt_id"],
                    stored["logical_artifact_id"],
                    stored["handoff_artifact_id"],
                    stored["handoff_digest"],
                    stored["handoff_size_bytes"],
                    stored["handoff_media_type"],
                    stored["handoff_compression"],
                    stored["manifest_artifact_id"],
                    stored["validation_artifact_id"],
                    stored["manifest_digest"],
                    stored["validation_digest"],
                    stored["collector_id"],
                    stored["validator_id"],
                    stored["semantic_valid"],
                )
                expected = (
                    commit.attempt_ids[0],
                    commit.logical_artifact_id,
                    commit.handoff_artifact_id,
                    commit.handoff_digest,
                    commit.handoff_size_bytes,
                    commit.handoff_media_type,
                    commit.handoff_compression,
                    manifest_artifact_id,
                    validation_artifact_id,
                    commit.manifest_digest,
                    commit.validation_digest,
                    commit.collector_id,
                    commit.validator_id,
                    commit.semantic_valid,
                )
                if comparable != expected:
                    raise BatchRepositoryConflictError("stage already has another artifact commit")
                return commit
        except asyncpg.PostgresError as error:
            translated = self._translate(error)
            if translated is not None:
                raise translated from None
            raise

    async def release(self, claim: BatchClaim) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE fs2_scientific_batches
                SET controller_id=NULL,lease_expires_at=NULL,updated_at=clock_timestamp()
                WHERE operation_id=$1 AND controller_id=$2 AND fencing_token=$3
                """,
                claim.operation_id,
                claim.controller_id,
                claim.fencing_token,
            )

    async def get(self, operation_id: UUID, *, tenant_id: str) -> ScientificBatchState:
        async with self.pool.acquire() as connection:
            record = await connection.fetchrow(
                "SELECT * FROM fs2_scientific_batches WHERE operation_id=$1 AND tenant_id=$2",
                operation_id,
                tenant_id,
            )
        if record is None:
            raise ScientificBatchNotFoundError("scientific batch does not exist")
        return self._state(record)

    async def request_cancel(self, operation_id: UUID, *, tenant_id: str, actor: str) -> ScientificBatchState:
        async with self.pool.acquire() as connection, connection.transaction():
            record = await connection.fetchrow(
                "SELECT * FROM fs2_scientific_batches WHERE operation_id=$1 AND tenant_id=$2 FOR UPDATE",
                operation_id,
                tenant_id,
            )
            if record is None:
                raise ScientificBatchNotFoundError("scientific batch does not exist")
            state = self._state(record)
            if state.status.terminal or state.cancel_requested:
                return state
            replacement = replace(state, cancel_requested=True)
            updated = await connection.fetchrow(
                """
                UPDATE fs2_scientific_batches
                SET cancel_requested=true,state=$3::jsonb,updated_at=clock_timestamp()
                WHERE operation_id=$1 AND tenant_id=$2
                RETURNING *
                """,
                operation_id,
                tenant_id,
                state_to_json(replacement),
            )
            await connection.execute(
                """
                INSERT INTO fs2_audit_events(
                    actor,tenant_id,token_id,action,target_type,target_id,outcome,detail
                )
                SELECT $3,$2,operation.token_id,'scientific_batch.cancel','operation',$1::uuid::text,
                       'requested','{}'::jsonb
                FROM fs2_operations operation WHERE operation.id=$1::uuid AND operation.tenant_id=$2
                """,
                operation_id,
                tenant_id,
                actor,
            )
            assert updated is not None
            return self._state(updated)

    async def list_events(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[BatchEvent]:
        if not 0 <= after_sequence or not 1 <= limit <= 1000:
            raise ValueError("scientific-batch event page is outside the bound")
        async with self.pool.acquire() as connection:
            exists = await connection.fetchval(
                "SELECT true FROM fs2_scientific_batches WHERE operation_id=$1 AND tenant_id=$2",
                operation_id,
                tenant_id,
            )
            if not exists:
                raise ScientificBatchNotFoundError("scientific batch does not exist")
            records = await connection.fetch(
                """
                SELECT * FROM fs2_scientific_batch_events
                WHERE operation_id=$1 AND sequence>$2
                ORDER BY sequence LIMIT $3
                """,
                operation_id,
                after_sequence,
                limit,
            )
        return [_event_from_record(cast(Mapping[str, Any], record)) for record in records]


# Owner-only disposal path for migration verification. Production migrations
# remain forward-only; dropping this extension must precede any test-only
# rollback of the artifact tables it references.
SCIENTIFIC_BATCH_ROLLBACK_SQL = """
DROP TRIGGER IF EXISTS fs2_scientific_attempt_commits_append_only_trigger ON fs2_scientific_attempt_commits;
DROP TRIGGER IF EXISTS fs2_scientific_batch_events_append_only_trigger ON fs2_scientific_batch_events;
DROP TRIGGER IF EXISTS fs2_scientific_batch_state_immutable_trigger ON fs2_scientific_batches;
DROP TABLE IF EXISTS fs2_scientific_attempt_commits;
DROP TABLE IF EXISTS fs2_scientific_batch_events;
DROP TABLE IF EXISTS fs2_scientific_batches;
DROP FUNCTION IF EXISTS fs2_scientific_batch_append_only();
DROP FUNCTION IF EXISTS fs2_scientific_batch_state_immutable();
DELETE FROM fs2_schema_migrations WHERE version='0015_scientific_batch_controller.sql';
""".strip()
