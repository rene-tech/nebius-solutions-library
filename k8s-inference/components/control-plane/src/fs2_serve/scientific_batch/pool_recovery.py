"""Bounded pre-start pool recovery around the qualified scientific controller.

Kueue still owns admission. Node/cluster-autoscaler observations can retire an
unstarted immutable attempt, and the ordinary foreground-delete barrier releases
its reservation before a new attempt is created. Retry count, backoff clock and
excluded pools derive from the existing durable attempt history, never RAM.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote
from uuid import UUID

import yaml

from .kubernetes import POOL_LABEL, HttpScientificBatchCluster, ScientificKubernetesError
from .models import (
    AttemptOutcome,
    BatchClaim,
    BatchEventDraft,
    BatchEventKind,
    FailureKind,
    LifecyclePhase,
    ResourceClass,
    ScientificAttemptState,
    ScientificBatchState,
    ScientificStagePlan,
    ScientificStageState,
    StageSchedulingDecision,
    WorkloadObservation,
    WorkloadRef,
    WorkloadResource,
    WorkloadState,
    attempt_identity,
    workload_name,
)
from .observation import DiagnosedWorkloadObservation
from .policy import PolicyAwareScientificBatchController
from .protocols import BatchRepositoryConflictError

POOL_UNAVAILABLE = "admitted_pool_unavailable"
SCHEDULING_TIMEOUT = "admitted_scheduling_timeout"
RECOVERY_CODES = frozenset({POOL_UNAVAILABLE, SCHEDULING_TIMEOUT})


@dataclass(frozen=True, slots=True)
class PoolRecoveryPolicy:
    confirmation_seconds: int = 120
    unscheduled_timeout_seconds: int = 7200
    backoff_base_seconds: int = 15
    backoff_max_seconds: int = 300
    autoscaler_status_max_age_seconds: int = 120
    autoscaler_namespace: str = "kube-system"
    autoscaler_configmap: str = "cluster-autoscaler-status"
    node_group_label: str = "nebius.com/node-group-id"

    def __post_init__(self) -> None:
        if not 30 <= self.confirmation_seconds <= 1800:
            raise ValueError("pool failure confirmation must be between 30 and 1800 seconds")
        if not self.confirmation_seconds <= self.unscheduled_timeout_seconds <= 86400:
            raise ValueError("unscheduled deadline must cover confirmation and be at most one day")
        if not 1 <= self.backoff_base_seconds <= self.backoff_max_seconds <= 1800:
            raise ValueError("pool recovery backoff is outside its bound")

    def retry_at(self, attempt: ScientificAttemptState) -> datetime | None:
        if attempt.failure_code not in RECOVERY_CODES or attempt.completed_at is None:
            return None
        delay = min(self.backoff_max_seconds, self.backoff_base_seconds * 2 ** (attempt.attempt_number - 1))
        return attempt.completed_at + timedelta(seconds=delay)


def retry_pools(
    state: ScientificBatchState,
    *,
    stage_id: str,
    shard_id: str | None,
    attempt_number: int,
) -> tuple[str, ...]:
    """A deterministic subset of the frozen qualified set, stable across crashes.

    Once all eligible pools have failed, the remaining *existing* attempt budget
    can retest the original set. This permits capacity to return without an
    unbounded exclusion/requeue loop. Scientific inputs and resources never change.
    """
    eligible = state.scheduling.stage(stage_id).resolved_pool_preference
    failed = {
        attempt.scheduling_admission.resolved_pool_id
        for attempt in state.stage(stage_id).attempts
        if attempt.shard_id == shard_id
        and attempt.attempt_number < attempt_number
        and attempt.failure_code in RECOVERY_CODES
        and attempt.scheduling_admission is not None
    }
    return tuple(pool for pool in eligible if pool not in failed) or eligible


def _time(value: object) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def confirmed_pool_unavailability(
    nodes: list[Any],
    autoscaler: Mapping[str, Any],
    *,
    now: datetime,
    policy: PoolRecoveryPolicy,
) -> tuple[datetime | None, str]:
    """Require complete old-node failure AND fresh evidence of no scale-up.

    Empty/new pools, missing telemetry, unregistered replacements, healthy nodes
    and active autoscaler growth are explicitly not confirmed pool failure.
    """
    if not nodes:
        return None, "NodeProvisioning"
    groups: set[str] = set()
    transitions: list[datetime] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            return None, "PoolHealthUnknown"
        metadata, status = node.get("metadata", {}), node.get("status", {})
        if not isinstance(metadata, Mapping) or not isinstance(status, Mapping):
            return None, "PoolHealthUnknown"
        conditions = status.get("conditions", [])
        if not isinstance(conditions, list) or not isinstance(metadata.get("labels", {}), Mapping):
            return None, "PoolHealthUnknown"
        ready = next((item for item in conditions if isinstance(item, Mapping) and item.get("type") == "Ready"), {})
        if ready.get("status") == "True":
            return None, "NodeProvisioning"
        # Unknown means the kubelet stopped reporting. A fresh NotReady node
        # may simply be installing its driver; leave it the full startup window.
        if ready.get("status") != "Unknown" or ready.get("reason") != "NodeStatusUnknown":
            return None, "NodeProvisioning"
        transition, created = _time(ready.get("lastTransitionTime")), _time(metadata.get("creationTimestamp"))
        if transition is None or created is None or transition > now or created > transition:
            return None, "PoolHealthUnknown"
        group = metadata.get("labels", {}).get(policy.node_group_label)
        if not isinstance(group, str) or not group:
            return None, "PoolHealthUnknown"
        groups.add(group)
        transitions.append(transition)
    raw_groups = autoscaler.get("nodeGroups", [])
    if not isinstance(raw_groups, list):
        return None, "PoolHealthUnknown"
    group_status = {item.get("name"): item for item in raw_groups if isinstance(item, Mapping)}
    for group_id in groups:
        group = group_status.get(group_id, {})
        health, growth = group.get("health", {}), group.get("scaleUp", {})
        if not isinstance(health, Mapping) or not isinstance(growth, Mapping):
            return None, "PoolHealthUnknown"
        probed = _time(health.get("lastProbeTime"))
        if probed is None or not 0 <= (now - probed).total_seconds() <= policy.autoscaler_status_max_age_seconds:
            return None, "PoolHealthUnknown"
        counts = health.get("nodeCounts", {})
        registered = counts.get("registered", {}) if isinstance(counts, Mapping) else {}
        if not isinstance(counts, Mapping) or not isinstance(registered, Mapping):
            return None, "PoolHealthUnknown"
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (
                counts.get("unregistered"),
                registered.get("notStarted"),
                registered.get("ready"),
                registered.get("total"),
                health.get("cloudProviderTarget"),
            )
        ):
            return None, "PoolHealthUnknown"
        if (
            growth.get("status") in {"InProgress", "Backoff"}
            or counts.get("unregistered", 0)
            or registered.get("notStarted", 0)
            or registered.get("ready", 0)
            or health.get("cloudProviderTarget", 0) > registered.get("total", 0)
        ):
            return None, "PoolScaleUpInProgress"
        if health.get("status") != "Unhealthy" or growth.get("status") not in {"Unhealthy", "NoActivity"}:
            return None, "PoolHealthUnknown"
    return max(transitions), "AdmittedPoolUnavailable"


class _StateReader(Protocol):
    async def get(self, operation_id: UUID, *, tenant_id: str) -> ScientificBatchState: ...


class PoolRecoveryScientificCluster(HttpScientificBatchCluster):
    def __init__(self, *, recovery_repository: _StateReader, recovery_policy: PoolRecoveryPolicy, **kwargs: Any):
        super().__init__(**kwargs)
        self.recovery_repository = recovery_repository
        self.recovery_policy = recovery_policy

    async def apply(self, resource: WorkloadResource, *, controller_fence: int) -> WorkloadRef:
        if resource.attempt_number > 1 and resource.scheduling.resource_class is ResourceClass.GPU:
            state = await self.recovery_repository.get(resource.operation_id, tenant_id=resource.tenant_id)
            attempt = state.stage(resource.stage_id).latest_attempt(resource.shard_id)
            if (
                attempt is None
                or attempt.attempt_id != resource.attempt_id
                or attempt.outcome is not AttemptOutcome.ACTIVE
            ):
                raise BatchRepositoryConflictError("retry placement has no current durable attempt")
            pools = retry_pools(
                state, stage_id=resource.stage_id, shard_id=resource.shard_id, attempt_number=resource.attempt_number
            )
            if not set(pools).issubset(resource.scheduling.resolved_pool_preference):
                raise BatchRepositoryConflictError("retry placement escaped the frozen qualified pools")
            resource = replace(resource, scheduling=replace(resource.scheduling, resolved_pool_preference=pools))
        return await super().apply(resource, controller_fence=controller_fence)

    async def observe(self, ref: WorkloadRef, *, scheduling: StageSchedulingDecision) -> WorkloadObservation:
        observed = await super().observe(ref, scheduling=scheduling)
        if not (
            isinstance(observed, DiagnosedWorkloadObservation)
            and observed.pods_unstarted
            and scheduling.resource_class is ResourceClass.GPU
            and observed.scheduling_admission is not None
            and observed.scheduling_admission.admitted_at is not None
        ):
            return observed
        pool = observed.scheduling_admission.resolved_pool_id
        if pool is None:
            return observed
        try:
            response = await self._request(
                "GET",
                "/api/v1/nodes",
                params={
                    "labelSelector": f"{POOL_LABEL}={pool}",
                    "limit": "1000",
                },
            )
            document = response.json()
            if (
                response.status_code != 200
                or not isinstance(document, Mapping)
                or not isinstance(document.get("metadata", {}), Mapping)
                or document.get("metadata", {}).get("continue")
                or not isinstance(document.get("items"), list)
            ):
                return replace(observed, pending_code="PoolHealthUnknown")
            policy = self.recovery_policy
            response = await self._request(
                "GET",
                "/api/v1/namespaces/"
                f"{quote(policy.autoscaler_namespace, safe='')}/configmaps/"
                f"{quote(policy.autoscaler_configmap, safe='')}",
            )
            if response.status_code != 200:
                return replace(observed, pending_code="PoolHealthUnknown")
            configmap = response.json()
            if not isinstance(configmap, Mapping) or not isinstance(configmap.get("data"), Mapping):
                return replace(observed, pending_code="PoolHealthUnknown")
            raw_status = configmap["data"].get("status", "")
            if not isinstance(raw_status, str) or len(raw_status) > 1024 * 1024:
                return replace(observed, pending_code="PoolHealthUnknown")
            autoscaler = yaml.safe_load(raw_status)
            if not isinstance(autoscaler, Mapping):
                return replace(observed, pending_code="PoolHealthUnknown")
            since, code = confirmed_pool_unavailability(
                document.get("items", []), autoscaler, now=self.clock(), policy=policy
            )
            # Preserve specific resource/affinity diagnoses for a healthy pool.
            return replace(
                observed,
                pool_unavailable_since=since,
                pending_code=observed.pending_code if code == "NodeProvisioning" else code,
            )
        except (ScientificKubernetesError, ValueError, TypeError, yaml.YAMLError):
            return replace(observed, pending_code="PoolHealthUnknown")


class PoolRecoveryScientificController(PolicyAwareScientificBatchController):
    def __init__(self, *, recovery_policy: PoolRecoveryPolicy | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.recovery_policy = recovery_policy or PoolRecoveryPolicy()

    async def _reconcile_active_stage(
        self,
        claim: BatchClaim,
        record: ScientificBatchState,
        stage: ScientificStageState,
        *,
        now: datetime,
    ) -> None:
        """Retry released independent shards while unaffected peers keep running."""
        spec = record.plan.stage(stage.stage_id)
        latest = [stage.latest_attempt(shard) for shard in spec.workload_units]
        fatal = stage.failure_code is not None or any(
            attempt is not None
            and attempt.outcome in {AttemptOutcome.FAILED, AttemptOutcome.PREEMPTED}
            and (
                attempt.failure_kind is None
                or not attempt.failure_kind.retryable
                or attempt.attempt_number >= spec.max_attempts
            )
            for attempt in latest
        )
        retrying = (
            []
            if fatal
            else [
                attempt
                for attempt in latest
                if attempt is not None
                and attempt.failure_code in RECOVERY_CODES
                and attempt.outcome is AttemptOutcome.FAILED
                and attempt.resource_released
                and (ready := self.recovery_policy.retry_at(attempt)) is not None
                and now >= ready
            ]
        )
        if not retrying:
            await super()._reconcile_active_stage(claim, record, stage, now=now)
            return
        attempts, events = list(stage.attempts), []
        for prior in retrying:
            number = prior.attempt_number + 1
            identity = attempt_identity(record.operation_id, stage.stage_id, prior.shard_id, number)
            attempts.append(
                ScientificAttemptState(
                    attempt_id=identity,
                    stage_id=stage.stage_id,
                    shard_id=prior.shard_id,
                    attempt_number=number,
                    started_at=now,
                    workload=WorkloadRef(
                        namespace=prior.workload.namespace,
                        route_namespace=prior.workload.route_namespace,
                        kind=prior.workload.kind,
                        name=workload_name(record.operation_id, stage.stage_id, prior.shard_id, number),
                    ),
                )
            )
            for phase in (LifecyclePhase.QUEUED, LifecyclePhase.SCHEDULING):
                events.append(
                    self._event(
                        record,
                        BatchEventKind.LIFECYCLE,
                        stage_id=stage.stage_id,
                        shard_id=prior.shard_id,
                        attempt_id=identity,
                        phase=phase,
                    )
                )
            events.append(
                self._event(
                    record,
                    BatchEventKind.RETRY_SCHEDULED,
                    stage_id=stage.stage_id,
                    shard_id=prior.shard_id,
                    attempt_id=identity,
                    code=prior.failure_code,
                )
            )
        reserved = await self._write(
            claim, record, self._replace_stage(record, replace(stage, attempts=tuple(attempts))), tuple(events), now=now
        )
        await self._apply_pending_attempts(claim, reserved, reserved.stage(stage.stage_id), now=now)

    @staticmethod
    def _fence_same_workload_requeue(
        attempt: ScientificAttemptState,
        observation: WorkloadObservation,
    ) -> WorkloadObservation:
        observed = PolicyAwareScientificBatchController._fence_same_workload_requeue(attempt, observation)
        if observed.failure_code in {
            "kueue_workload_recreated",
            "kueue_same_workload_rereserved",
            "kueue_reservation_lost",
        }:
            # A changed/deleted reservation proves an infrastructure boundary,
            # not provider preemption. Preserve its bounded cause without
            # inventing who removed it. Explicit Kueue eviction reasons retain
            # their existing classification.
            return replace(
                observed,
                state=WorkloadState.FAILED,
                failure_kind=FailureKind.INFRASTRUCTURE,
                phases=tuple(phase for phase in observed.phases if phase is not LifecyclePhase.PREEMPTED),
            )
        return observed

    def _ingest_observation(
        self,
        record: ScientificBatchState,
        attempt: ScientificAttemptState,
        observed: WorkloadObservation,
    ) -> tuple[ScientificAttemptState, tuple[BatchEventDraft, ...]]:
        updated, events = super()._ingest_observation(record, attempt, observed)
        if not (
            isinstance(observed, DiagnosedWorkloadObservation)
            and observed.pods_unstarted
            and observed.state is WorkloadState.PENDING
            and attempt.last_phase.rank <= LifecyclePhase.NODE_PENDING.rank
        ):
            return updated, events
        admission = observed.scheduling_admission or attempt.scheduling_admission
        if admission is None or admission.admitted_at is None or admission.accelerator_count == 0:
            return updated, events
        now, policy = self.clock(), self.recovery_policy
        code = None
        if observed.pool_unavailable_since is not None and now >= max(
            admission.admitted_at, observed.pool_unavailable_since
        ) + timedelta(seconds=policy.confirmation_seconds):
            code = POOL_UNAVAILABLE
        elif now >= admission.admitted_at + timedelta(seconds=policy.unscheduled_timeout_seconds):
            code = SCHEDULING_TIMEOUT
        if code is None:
            return updated, events
        return replace(
            updated,
            outcome=AttemptOutcome.FAILED,
            completed_at=now,
            failure_kind=FailureKind.INFRASTRUCTURE,
            failure_code=code,
        ), events

    async def _start_stage(
        self,
        claim: BatchClaim,
        record: ScientificBatchState,
        spec: ScientificStagePlan,
        *,
        now: datetime,
    ) -> None:
        retry_times = [
            self.recovery_policy.retry_at(attempt)
            for shard in spec.workload_units
            if (attempt := record.stage(spec.stage_id).latest_attempt(shard)) is not None
        ]
        if any(ready is not None and now < ready for ready in retry_times):
            return
        await super()._start_stage(claim, record, spec, now=now)
