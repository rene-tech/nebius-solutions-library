"""Durable operator dispatch policy for scientific batch models.

A policy is the one operator lever over scientific batch admission that does
not require a Terraform apply: pause new dispatch for a model, or cap how many
of its runs may be active at once. It is enforced by the controller at the
``queued -> running`` transition through the same PostgreSQL row set every
API and controller replica reads, so it survives restarts and applies
consistently across replicas. It never touches an admitted batch, Kueue quota,
node-pool bounds, or per-run resource requests.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import asyncpg

from .controller import ScientificBatchController
from .models import (
    BatchClaim,
    BatchEventDraft,
    BatchEventKind,
    LifecyclePhase,
    ScientificAttemptState,
    ScientificBatchState,
    WorkloadObservation,
)
from .observation import DiagnosedWorkloadObservation

MAX_ACTIVE_RUNS_BOUND = 64
POLICY_AUDIT_ACTION = "scientific_model_policy.set"

# Transaction-scoped advisory lock shared by policy writes and the controller's
# fenced dispatch transition, so a pause or concurrency change is linearizable
# with every queued -> running decision for the same model.
DISPATCH_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('fs2-scientific-model-policy' || chr(31) || $1, 0))"


class ScientificDispatchHeldError(RuntimeError):
    """An operator model policy holds this queued batch back from dispatch.

    The batch stays durable and queued at its current revision; no Kubernetes
    resource was created. A later poll re-evaluates the policy.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"scientific dispatch is held by model policy: {reason}")
        self.reason = reason


class PolicyAwareScientificBatchController(ScientificBatchController):
    """The production reconciler: a policy hold is a quiet no-op, not a failure.

    The fenced repository refuses the one ``queued -> running`` write while an
    operator pause or active-run cap applies. That refusal happens before any
    Kubernetes apply, so the batch is left exactly as it was and re-evaluated
    on a later poll. Keeping the adaptation here leaves the frozen controller
    module, and with it every qualified runtime recipe identity, untouched.
    """

    async def _reconcile(self, claim: BatchClaim, record: ScientificBatchState, *, now: datetime) -> None:
        try:
            await super()._reconcile(claim, record, now=now)
        except ScientificDispatchHeldError:
            return

    async def reconcile_once(self) -> UUID | None:
        return await super().reconcile_once()

    def _ingest_observation(
        self,
        record: ScientificBatchState,
        attempt: ScientificAttemptState,
        observation: WorkloadObservation,
    ) -> tuple[ScientificAttemptState, tuple[BatchEventDraft, ...]]:
        updated, events = super()._ingest_observation(record, attempt, observation)
        if isinstance(observation, DiagnosedWorkloadObservation) and observation.pending_code is not None:
            # A reason may arrive after the first pending phase. Code-specific
            # event identity deduplicates polls without changing failure state
            # or any qualified execution/controller recipe.
            events = (*events, self._event(
                record, BatchEventKind.LIFECYCLE,
                stage_id=attempt.stage_id, shard_id=attempt.shard_id, attempt_id=attempt.attempt_id,
                phase=LifecyclePhase.NODE_PENDING, code=observation.pending_code,
            ))
        return updated, events


class ScientificModelPolicyStaleError(RuntimeError):
    """The caller's expected policy revision is not the durable revision."""

    def __init__(self, current_revision: int) -> None:
        super().__init__("scientific model policy revision is stale")
        self.current_revision = current_revision


@dataclass(frozen=True, slots=True)
class ScientificModelPolicyRecord:
    """One durable (model, scope) policy row."""

    model_id: str
    tenant_id: str | None
    revision: int
    paused: bool
    max_active_runs: int | None
    reason: str | None
    updated_by: str
    created_at: datetime
    updated_at: datetime
    startup_policies: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("a stored scientific model policy has a positive revision")
        if self.max_active_runs is not None and not 1 <= self.max_active_runs <= MAX_ACTIVE_RUNS_BOUND:
            raise ValueError("scientific model policy concurrency is outside the bound")


@dataclass(frozen=True, slots=True)
class ScientificDispatchCounts:
    """Durable batch counts for one model scope.

    ``queued`` batches are accepted and durable but not yet dispatched (and not
    cancelling); ``running`` batches have been dispatched to Kubernetes/Kueue
    and are not yet terminal, which includes Kueue-pending and cancelling work.
    """

    queued: int
    running: int


@dataclass(frozen=True, slots=True)
class ScientificModelPolicyView:
    """Everything the admin surface needs for one model in one scope."""

    model_id: str
    tenant_id: str | None
    policy: ScientificModelPolicyRecord | None
    inherited: ScientificModelPolicyRecord | None
    counts: ScientificDispatchCounts
    all_tenants_counts: ScientificDispatchCounts

    @property
    def effective_paused(self) -> bool:
        return any(row.paused for row in (self.inherited, self.policy) if row is not None)

    @property
    def effective_max_active_runs(self) -> int | None:
        limits = [row.max_active_runs for row in (self.inherited, self.policy) if row is not None]
        bounded = [limit for limit in limits if limit is not None]
        return min(bounded) if bounded else None

    @property
    def at_limit(self) -> bool:
        if self.inherited is not None and self.inherited.max_active_runs is not None:
            if self.all_tenants_counts.running >= self.inherited.max_active_runs:
                return True
        if self.policy is not None and self.policy.max_active_runs is not None:
            scope_running = self.counts.running if self.tenant_id is not None else self.all_tenants_counts.running
            if scope_running >= self.policy.max_active_runs:
                return True
        return False

    @property
    def dispatch_state(self) -> str:
        if self.effective_paused:
            return "paused"
        if self.at_limit:
            return "at-limit"
        return "open"


def _record(row: Mapping[str, Any]) -> ScientificModelPolicyRecord:
    startup = row.get("startup_policies", {})
    if isinstance(startup, str):
        startup = json.loads(startup)
    return ScientificModelPolicyRecord(
        model_id=str(row["model_id"]),
        tenant_id=None if row["tenant_id"] is None else str(row["tenant_id"]),
        revision=int(row["revision"]),
        paused=bool(row["paused"]),
        max_active_runs=None if row["max_active_runs"] is None else int(row["max_active_runs"]),
        reason=None if row["reason"] is None else str(row["reason"]),
        updated_by=str(row["updated_by"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        startup_policies=dict(startup),
    )


class PostgresScientificModelPolicyRepository:
    """Owner of ``fs2_scientific_model_policies`` writes and the admin read view.

    The controller never imports this class; it evaluates the shared SQL
    predicate ``fs2_scientific_dispatch_hold`` directly so the enforcement
    path has no Python-side cache to drift.
    """

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self.pool = pool

    @staticmethod
    def _counts_sql(tenant_id: str | None) -> str:
        if tenant_id is None:
            return """
                SELECT model_id,
                       count(*) FILTER (WHERE status='queued' AND NOT cancel_requested) AS queued,
                       count(*) FILTER (WHERE status='running') AS running,
                       count(*) FILTER (WHERE status='queued' AND NOT cancel_requested) AS scope_queued,
                       count(*) FILTER (WHERE status='running') AS scope_running
                FROM fs2_scientific_batches
                WHERE status IN ('queued','running') AND ($1::text[] IS NULL OR model_id=ANY($1::text[]))
                GROUP BY model_id
            """
        return """
            SELECT model_id,
                   count(*) FILTER (WHERE status='queued' AND NOT cancel_requested) AS queued,
                   count(*) FILTER (WHERE status='running') AS running,
                   count(*) FILTER (WHERE status='queued' AND NOT cancel_requested AND tenant_id=$2) AS scope_queued,
                   count(*) FILTER (WHERE status='running' AND tenant_id=$2) AS scope_running
            FROM fs2_scientific_batches
            WHERE status IN ('queued','running') AND ($1::text[] IS NULL OR model_id=ANY($1::text[]))
            GROUP BY model_id
        """

    async def _views(
        self,
        connection: asyncpg.Connection[Any],
        *,
        tenant_id: str | None,
        model_ids: Iterable[str] | None,
    ) -> list[ScientificModelPolicyView]:
        requested = None if model_ids is None else sorted(set(model_ids))
        policy_rows = await connection.fetch(
            """
            SELECT * FROM fs2_scientific_model_policies
            WHERE (tenant_id IS NULL OR tenant_id=$2)
              AND ($1::text[] IS NULL OR model_id=ANY($1::text[]))
            ORDER BY model_id, tenant_id NULLS FIRST
            """,
            requested,
            tenant_id,
        )
        if tenant_id is None:
            count_rows = await connection.fetch(self._counts_sql(None), requested)
        else:
            count_rows = await connection.fetch(self._counts_sql(tenant_id), requested, tenant_id)
        policies: dict[tuple[str, str | None], ScientificModelPolicyRecord] = {}
        for row in policy_rows:
            record = _record(cast(Mapping[str, Any], row))
            policies[(record.model_id, record.tenant_id)] = record
        counts: dict[str, tuple[ScientificDispatchCounts, ScientificDispatchCounts]] = {}
        for row in count_rows:
            counts[str(row["model_id"])] = (
                ScientificDispatchCounts(queued=int(row["scope_queued"]), running=int(row["scope_running"])),
                ScientificDispatchCounts(queued=int(row["queued"]), running=int(row["running"])),
            )
        model_set = set(requested or ()) | {model for model, _ in policies} | set(counts)
        empty = ScientificDispatchCounts(queued=0, running=0)
        views: list[ScientificModelPolicyView] = []
        for model_id in sorted(model_set):
            scope_counts, all_counts = counts.get(model_id, (empty, empty))
            views.append(
                ScientificModelPolicyView(
                    model_id=model_id,
                    tenant_id=tenant_id,
                    policy=policies.get((model_id, tenant_id)),
                    inherited=None if tenant_id is None else policies.get((model_id, None)),
                    counts=scope_counts,
                    all_tenants_counts=all_counts,
                )
            )
        return views

    async def list(
        self,
        *,
        tenant_id: str | None,
        model_ids: Iterable[str],
    ) -> tuple[ScientificModelPolicyView, ...]:
        """Return one view per known model plus any model that has a row or live batch."""

        async with self.pool.acquire() as connection:
            known = set(model_ids)
            # A stale policy row or a live batch for a model that left the
            # catalog stays visible: an operator must still be able to see and
            # clear it, so the requested set is widened rather than filtered.
            extra = await connection.fetch(
                """
                SELECT DISTINCT model_id FROM fs2_scientific_model_policies WHERE tenant_id IS NULL OR tenant_id=$1
                UNION
                SELECT DISTINCT model_id FROM fs2_scientific_batches WHERE status IN ('queued','running')
                """,
                tenant_id,
            )
            known |= {str(row["model_id"]) for row in extra}
            return tuple(await self._views(connection, tenant_id=tenant_id, model_ids=known))

    async def get(self, model_id: str, *, tenant_id: str | None) -> ScientificModelPolicyView:
        async with self.pool.acquire() as connection:
            views = await self._views(connection, tenant_id=tenant_id, model_ids=(model_id,))
        return views[0]

    async def startup_policies(self, *, model_id: str, tenant_id: str) -> dict[str, dict[str, Any]]:
        """Resolve operator defaults once, before the run freezes its binding."""
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT startup_policies FROM fs2_scientific_model_policies
                WHERE model_id=$1 AND (tenant_id IS NULL OR tenant_id=$2)
                ORDER BY tenant_id NULLS FIRST
                """,
                model_id,
                tenant_id,
            )
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = row["startup_policies"]
            result.update(json.loads(value) if isinstance(value, str) else value)
        return result

    async def set(
        self,
        model_id: str,
        *,
        tenant_id: str | None,
        expected_revision: int,
        paused: bool,
        max_active_runs: int | None,
        reason: str | None,
        actor: str,
        startup_policies: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ScientificModelPolicyView:
        """Replace one scope row at exactly ``expected_revision`` (0 when absent).

        The write holds the model's dispatch advisory lock so it serializes
        with the controller's fenced queued -> running transitions; a stale
        revision raises instead of silently overwriting another operator.
        """

        if expected_revision < 0:
            raise ValueError("expected policy revision cannot be negative")
        if max_active_runs is not None and not 1 <= max_active_runs <= MAX_ACTIVE_RUNS_BOUND:
            raise ValueError("scientific model policy concurrency is outside the bound")
        if reason is not None and not 1 <= len(reason) <= 300:
            raise ValueError("scientific model policy reason is outside the bound")
        if not 1 <= len(actor) <= 200:
            raise ValueError("scientific model policy actor is outside the bound")
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(DISPATCH_LOCK_SQL, model_id)
            current = await connection.fetchval(
                """
                SELECT revision FROM fs2_scientific_model_policies
                WHERE model_id=$1 AND COALESCE(tenant_id,'')=COALESCE($2,'')
                """,
                model_id,
                tenant_id,
            )
            current_revision = 0 if current is None else int(current)
            if current_revision != expected_revision:
                raise ScientificModelPolicyStaleError(current_revision)
            try:
                if current is None:
                    written = await connection.fetchrow(
                        """
                        INSERT INTO fs2_scientific_model_policies(
                            model_id,tenant_id,revision,paused,max_active_runs,reason,updated_by,startup_policies
                        ) VALUES($1,$2,1,$3,$4,$5,$6,$7::jsonb)
                        RETURNING revision
                        """,
                        model_id,
                        tenant_id,
                        paused,
                        max_active_runs,
                        reason,
                        actor,
                        json.dumps(startup_policies or {}),
                    )
                else:
                    written = await connection.fetchrow(
                        """
                        UPDATE fs2_scientific_model_policies
                        SET revision=revision+1,paused=$3,max_active_runs=$4,reason=$5,updated_by=$6,
                            updated_at=clock_timestamp(),startup_policies=COALESCE($8::jsonb,startup_policies)
                        WHERE model_id=$1 AND COALESCE(tenant_id,'')=COALESCE($2,'') AND revision=$7
                        RETURNING revision
                        """,
                        model_id,
                        tenant_id,
                        paused,
                        max_active_runs,
                        reason,
                        actor,
                        expected_revision,
                        None if startup_policies is None else json.dumps(startup_policies),
                    )
            except asyncpg.UniqueViolationError:
                raise ScientificModelPolicyStaleError(current_revision + 1) from None
            if written is None:
                raise ScientificModelPolicyStaleError(current_revision)
            await connection.execute(
                """
                INSERT INTO fs2_audit_events(actor,tenant_id,token_id,action,target_type,target_id,outcome,detail)
                VALUES($1,$2,NULL,$3,'scientific_model',$4,'applied',$5::jsonb)
                """,
                actor,
                tenant_id,
                POLICY_AUDIT_ACTION,
                model_id,
                json.dumps(
                    {
                        "revision": int(written["revision"]),
                        "paused": paused,
                        "max_active_runs": max_active_runs,
                        "reason": reason,
                        "scope": "tenant" if tenant_id is not None else "all-tenants",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            views = await self._views(connection, tenant_id=tenant_id, model_ids=(model_id,))
            return views[0]
