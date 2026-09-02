"""Closed JSON codec for durable internal scientific-batch state.

This is deliberately not an API schema.  It serializes the controller's
``Scientific*`` records for PostgreSQL and rejects any missing or extra field
when reopening them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from .models import (
    AttemptOutcome,
    BatchStatus,
    CheckpointMode,
    ExecutionMode,
    FailureKind,
    LifecyclePhase,
    PreemptionMode,
    ResourceClass,
    SchedulingSnapshot,
    ScientificAttemptState,
    ScientificBatchPlan,
    ScientificBatchState,
    ScientificStagePlan,
    ScientificStageState,
    ServiceClass,
    StageSchedulingDecision,
    StageStatus,
    WorkloadKind,
    WorkloadRef,
)

STATE_SCHEMA = "fs2-serve.nebius.ai/scientific-batch-state/v1"
MAX_STATE_BYTES = 4 * 1024 * 1024


def _object(value: object, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"stored {label} is not an object")
    result = cast(Mapping[str, Any], value)
    if set(result) != keys:
        raise ValueError(f"stored {label} fields differ")
    return result


def _items(value: object, label: str, *, maximum: int = 4096) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"stored {label} is not a bounded array")
    return value


def _datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"stored {label} is not a timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"stored {label} timestamp is naive")
    return parsed


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"stored {label} is not an integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"stored {label} is not a boolean")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"stored {label} is not a string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _uuid(value: object, label: str) -> UUID:
    try:
        return UUID(_string(value, label))
    except ValueError as error:
        raise ValueError(f"stored {label} is not a UUID") from error


def _string_items(value: object, label: str, *, maximum: int) -> tuple[str, ...]:
    return tuple(_string(item, label) for item in _items(value, label, maximum=maximum))


def state_to_value(state: ScientificBatchState) -> dict[str, Any]:
    """Return canonical, payload-free internal state."""

    return {
        "schema_version": STATE_SCHEMA,
        "operation_id": str(state.operation_id),
        "batch_id": str(state.batch_id),
        "workload_id": str(state.workload_id),
        "tenant_id": state.tenant_id,
        "model_id": state.model_id,
        "plan": {
            "stages": [
                {
                    "stage_id": stage.stage_id,
                    "depends_on": list(stage.depends_on),
                    "mode": stage.mode.value,
                    "shards": list(stage.shards),
                    "max_attempts": stage.max_attempts,
                    "gang_size": stage.gang_size,
                    "resource_class": stage.resource_class.value,
                    "min_parallelism": stage.min_parallelism,
                    "max_parallelism": stage.max_parallelism,
                    "checkpoint_mode": stage.checkpoint_mode.value,
                    "preemption_mode": stage.preemption_mode.value,
                }
                for stage in state.plan.stages
            ]
        },
        "scheduling": {
            "policy_revision": state.scheduling.policy_revision,
            "captured_at": state.scheduling.captured_at.isoformat(),
            "service_class": state.scheduling.service_class.value,
            "tenant_queue": state.scheduling.tenant_queue,
            "model_lane": state.scheduling.model_lane,
            "stages": [
                {
                    "stage_id": stage.stage_id,
                    "resolved_cluster_queue": stage.resolved_cluster_queue,
                    "resolved_local_queue": stage.resolved_local_queue,
                    "workload_priority_class": stage.workload_priority_class,
                    "workload_priority_value": stage.workload_priority_value,
                    "resolved_pool_preference": list(stage.resolved_pool_preference),
                    "admitted_resource_flavor": stage.admitted_resource_flavor,
                    "accelerator_resource_name": stage.accelerator_resource_name,
                    "accelerator_count": stage.accelerator_count,
                    "max_queue_seconds": stage.max_queue_seconds,
                    "max_execution_seconds": stage.max_execution_seconds,
                    "checkpoint_mode": stage.checkpoint_mode.value,
                    "preemption_mode": stage.preemption_mode.value,
                }
                for stage in state.scheduling.stages
            ],
        },
        "stages": [
            {
                "stage_id": stage.stage_id,
                "status": stage.status.value,
                "failure_code": stage.failure_code,
                "attempts": [
                    {
                        "attempt_id": str(attempt.attempt_id),
                        "stage_id": attempt.stage_id,
                        "shard_id": attempt.shard_id,
                        "attempt_number": attempt.attempt_number,
                        "workload": {
                            "namespace": attempt.workload.namespace,
                            "name": attempt.workload.name,
                            "kind": attempt.workload.kind.value,
                            "uid": attempt.workload.uid,
                        },
                        "outcome": attempt.outcome.value,
                        "last_phase": attempt.last_phase.value,
                        "resource_released": attempt.resource_released,
                        "failure_kind": attempt.failure_kind.value if attempt.failure_kind else None,
                        "failure_code": attempt.failure_code,
                    }
                    for attempt in stage.attempts
                ],
            }
            for stage in state.stages
        ],
        "status": state.status.value,
        "revision": state.revision,
        "cancel_requested": state.cancel_requested,
        "failure_code": state.failure_code,
    }


def state_to_json(state: ScientificBatchState) -> str:
    value = json.dumps(state_to_value(state), sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(value.encode("utf-8")) > MAX_STATE_BYTES:
        raise ValueError("scientific-batch state exceeds the durable bound")
    return value


def state_from_value(raw: object) -> ScientificBatchState:
    """Reopen and validate one exact internal state value."""

    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("stored scientific-batch state exceeds the durable bound")
        raw = json.loads(raw)
    value = _object(
        raw,
        {
            "schema_version",
            "operation_id",
            "batch_id",
            "workload_id",
            "tenant_id",
            "model_id",
            "plan",
            "scheduling",
            "stages",
            "status",
            "revision",
            "cancel_requested",
            "failure_code",
        },
        "scientific-batch state",
    )
    if value["schema_version"] != STATE_SCHEMA:
        raise ValueError("stored scientific-batch state schema is unsupported")

    plan_value = _object(value["plan"], {"stages"}, "scientific-batch plan")
    plan_stages: list[ScientificStagePlan] = []
    stage_plan_keys = {
        "stage_id",
        "depends_on",
        "mode",
        "shards",
        "max_attempts",
        "gang_size",
        "resource_class",
        "min_parallelism",
        "max_parallelism",
        "checkpoint_mode",
        "preemption_mode",
    }
    for raw_stage in _items(plan_value["stages"], "scientific-batch plan stages", maximum=64):
        stage = _object(raw_stage, stage_plan_keys, "scientific-batch plan stage")
        plan_stages.append(
            ScientificStagePlan(
                stage_id=_string(stage["stage_id"], "stage ID"),
                depends_on=_string_items(stage["depends_on"], "stage dependency", maximum=32),
                mode=ExecutionMode(_string(stage["mode"], "stage mode")),
                shards=_string_items(stage["shards"], "stage shard", maximum=1024),
                max_attempts=_integer(stage["max_attempts"], "stage max attempts"),
                gang_size=None if stage["gang_size"] is None else _integer(stage["gang_size"], "gang size"),
                resource_class=ResourceClass(_string(stage["resource_class"], "stage resource class")),
                min_parallelism=_integer(stage["min_parallelism"], "stage minimum parallelism"),
                max_parallelism=_integer(stage["max_parallelism"], "stage maximum parallelism"),
                checkpoint_mode=CheckpointMode(_string(stage["checkpoint_mode"], "stage checkpoint mode")),
                preemption_mode=PreemptionMode(_string(stage["preemption_mode"], "stage preemption mode")),
            )
        )
    plan = ScientificBatchPlan(tuple(plan_stages))

    scheduling_value = _object(
        value["scheduling"],
        {"policy_revision", "captured_at", "service_class", "tenant_queue", "model_lane", "stages"},
        "scientific-batch scheduling snapshot",
    )
    scheduling_keys = {
        "stage_id",
        "resolved_cluster_queue",
        "resolved_local_queue",
        "workload_priority_class",
        "workload_priority_value",
        "resolved_pool_preference",
        "admitted_resource_flavor",
        "accelerator_resource_name",
        "accelerator_count",
        "max_queue_seconds",
        "max_execution_seconds",
        "checkpoint_mode",
        "preemption_mode",
    }
    decisions: list[StageSchedulingDecision] = []
    for raw_decision in _items(scheduling_value["stages"], "scheduling stages", maximum=64):
        decision = _object(raw_decision, scheduling_keys, "scheduling stage")
        decisions.append(
            StageSchedulingDecision(
                stage_id=_string(decision["stage_id"], "scheduling stage ID"),
                resolved_cluster_queue=_string(decision["resolved_cluster_queue"], "cluster queue"),
                resolved_local_queue=_string(decision["resolved_local_queue"], "local queue"),
                workload_priority_class=_string(decision["workload_priority_class"], "priority class"),
                workload_priority_value=_integer(decision["workload_priority_value"], "workload priority"),
                resolved_pool_preference=_string_items(
                    decision["resolved_pool_preference"], "pool preference", maximum=256
                ),
                admitted_resource_flavor=_optional_string(
                    decision["admitted_resource_flavor"], "admitted resource flavor"
                ),
                accelerator_resource_name=_string(decision["accelerator_resource_name"], "accelerator resource"),
                accelerator_count=_integer(decision["accelerator_count"], "accelerator count"),
                max_queue_seconds=(
                    None
                    if decision["max_queue_seconds"] is None
                    else _integer(decision["max_queue_seconds"], "maximum queue seconds")
                ),
                max_execution_seconds=(
                    None
                    if decision["max_execution_seconds"] is None
                    else _integer(decision["max_execution_seconds"], "maximum execution seconds")
                ),
                checkpoint_mode=CheckpointMode(_string(decision["checkpoint_mode"], "checkpoint mode")),
                preemption_mode=PreemptionMode(_string(decision["preemption_mode"], "preemption mode")),
            )
        )
    scheduling = SchedulingSnapshot(
        policy_revision=_string(scheduling_value["policy_revision"], "scheduling policy revision"),
        captured_at=_datetime(scheduling_value["captured_at"], "scheduling capture"),
        service_class=ServiceClass(_string(scheduling_value["service_class"], "service class")),
        tenant_queue=_string(scheduling_value["tenant_queue"], "tenant queue"),
        model_lane=_string(scheduling_value["model_lane"], "model lane"),
        stages=tuple(decisions),
    )

    stage_state_keys = {"stage_id", "status", "failure_code", "attempts"}
    attempt_keys = {
        "attempt_id",
        "stage_id",
        "shard_id",
        "attempt_number",
        "workload",
        "outcome",
        "last_phase",
        "resource_released",
        "failure_kind",
        "failure_code",
    }
    workload_keys = {"namespace", "name", "kind", "uid"}
    stage_states: list[ScientificStageState] = []
    for raw_stage_state in _items(value["stages"], "scientific-batch stages", maximum=64):
        stage_state = _object(raw_stage_state, stage_state_keys, "scientific-batch stage")
        attempts: list[ScientificAttemptState] = []
        for raw_attempt in _items(stage_state["attempts"], "scientific attempts"):
            attempt = _object(raw_attempt, attempt_keys, "scientific attempt")
            workload = _object(attempt["workload"], workload_keys, "scientific attempt workload")
            attempts.append(
                ScientificAttemptState(
                    attempt_id=_uuid(attempt["attempt_id"], "attempt ID"),
                    stage_id=_string(attempt["stage_id"], "attempt stage ID"),
                    shard_id=_optional_string(attempt["shard_id"], "attempt shard ID"),
                    attempt_number=_integer(attempt["attempt_number"], "attempt number"),
                    workload=WorkloadRef(
                        namespace=_string(workload["namespace"], "workload namespace"),
                        name=_string(workload["name"], "workload name"),
                        kind=WorkloadKind(_string(workload["kind"], "workload kind")),
                        uid=_optional_string(workload["uid"], "workload UID"),
                    ),
                    outcome=AttemptOutcome(_string(attempt["outcome"], "attempt outcome")),
                    last_phase=LifecyclePhase(_string(attempt["last_phase"], "attempt phase")),
                    resource_released=_boolean(attempt["resource_released"], "attempt resource release"),
                    failure_kind=(
                        None
                        if attempt["failure_kind"] is None
                        else FailureKind(_string(attempt["failure_kind"], "attempt failure kind"))
                    ),
                    failure_code=_optional_string(attempt["failure_code"], "attempt failure code"),
                )
            )
        stage_states.append(
            ScientificStageState(
                stage_id=_string(stage_state["stage_id"], "stage state ID"),
                status=StageStatus(_string(stage_state["status"], "stage status")),
                attempts=tuple(attempts),
                failure_code=_optional_string(stage_state["failure_code"], "stage failure code"),
            )
        )

    return ScientificBatchState(
        operation_id=_uuid(value["operation_id"], "operation ID"),
        batch_id=_uuid(value["batch_id"], "batch ID"),
        workload_id=_uuid(value["workload_id"], "workload ID"),
        tenant_id=_string(value["tenant_id"], "tenant ID"),
        model_id=_string(value["model_id"], "model ID"),
        plan=plan,
        scheduling=scheduling,
        stages=tuple(stage_states),
        status=BatchStatus(_string(value["status"], "batch status")),
        revision=_integer(value["revision"], "revision"),
        cancel_requested=_boolean(value["cancel_requested"], "cancel request"),
        failure_code=_optional_string(value["failure_code"], "batch failure code"),
    )
