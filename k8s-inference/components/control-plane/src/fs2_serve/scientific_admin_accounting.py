"""Project existing per-attempt lifecycle rollups without inventing GPU time."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Literal
from uuid import UUID

from .scientific_admin_models import (
    ScientificEvidenceState,
    ScientificGpuAccounting,
    ScientificIdleCause,
    ScientificMeasurement,
)
from .scientific_batch.models import ResourceClass, ScientificBatchState


def project_gpu_accounting(
    state: ScientificBatchState,
    rows: Sequence[Mapping[str, Any]],
) -> ScientificGpuAccounting | None:
    """Sum disjoint attempt identities; CPU stages and duplicate rows never add GPU time.

    ``allocated`` is the legacy DTO field for scheduler occupancy, explicitly
    labelled as such in the console. It must not be presented as device-memory
    allocation. Missing rollups make the observed subtotal estimated, not zero.
    """
    gpu_stages = {stage.stage_id for stage in state.plan.stages if stage.resource_class is ResourceClass.GPU}
    expected = {
        attempt.attempt_id: attempt
        for stage in state.stages
        if stage.stage_id in gpu_stages
        for attempt in stage.attempts
    }
    selected: dict[UUID, Mapping[str, Any]] = {}
    for row in rows:
        attempt_id = UUID(str(row["attempt_id"]))
        if attempt_id not in expected or row.get("rollup_id") is None or row.get("quality") == "unavailable":
            continue
        previous = selected.setdefault(attempt_id, row)
        if previous != row:
            raise ValueError("conflicting latest lifecycle rollups for one immutable attempt")
    if not selected:
        return None
    complete = len(selected) == len(expected)
    data_gaps = sorted({gap for row in selected.values() for gap in row.get("data_gaps", ())})
    if not complete:
        data_gaps.append("attempt_coverage_incomplete")
    exact = complete and not data_gaps and all(
        row["quality"] == "measured" and row["reconciled"] and row["terminal"] for row in selected.values()
    )
    reason = (
        None
        if exact
        else (
            "Observed attempt subtotal; lifecycle boundaries are application-observed, incomplete or unreconciled. "
            "Not device utilization."
        )
    )

    def measurement(value: float) -> ScientificMeasurement:
        return ScientificMeasurement(
            value=value,
            unit="gpu-seconds",
            evidence=ScientificEvidenceState.MEASURED if exact else ScientificEvidenceState.ESTIMATED,
            source="lifecycle-ledger",
            reason=reason,
        )

    def unavailable(reason: str) -> ScientificMeasurement:
        return ScientificMeasurement(
            value=None, unit="gpu-seconds", evidence=ScientificEvidenceState.UNAVAILABLE,
            source="lifecycle-ledger", reason=reason,
        )

    def clock(field: str, missing_gap: str) -> ScientificMeasurement:
        if any(row.get(field) is None or missing_gap in row.get("data_gaps", ()) for row in selected.values()):
            return unavailable("This clock lacks an independently observed boundary for one or more attempts.")
        return measurement(sum(float(row[field]) for row in selected.values()))

    totals: dict[str, float] = defaultdict(float)
    for row in selected.values():
        phases = row["phase_gpu_seconds"]
        phases = json.loads(phases) if isinstance(phases, str) else phases
        for phase, seconds in phases.items():
            totals[phase] += float(seconds)
    occupied = sum(float(row["scheduler_occupied_gpu_seconds"]) for row in selected.values())
    active = sum(float(row["active_gpu_seconds"]) for row in selected.values())
    grace = sum(totals[phase] for phase in ("cooldown_grace", "checkpoint_drain", "teardown"))
    idle = max(0.0, sum(float(row["occupied_idle_gpu_seconds"]) for row in selected.values()) - grace)
    partition = {
        phase: (
            unavailable("Phase not observed; unclassified occupancy cannot be reassigned to this phase.")
            if totals[phase] == 0 and totals["unclassified"] > 0
            else measurement(totals[phase])
        )
        for phase in (
            "image_pull", "artifact_load", "restore", "compile", "warmup", "active_compute",
            "workflow_wait", "resident_idle", "cooldown_grace", "checkpoint_drain", "teardown", "unclassified",
        )
    }
    idle_causes: dict[
        Literal["image-pull", "artifact-load", "restore", "warmup", "between-stages", "unattributed"], float
    ] = {
        "image-pull": totals["image_pull"],
        "artifact-load": totals["artifact_load"],
        "restore": totals["restore"],
        "warmup": totals["compile"] + totals["warmup"],
        "between-stages": totals["workflow_wait"] + totals["resident_idle"],
        "unattributed": totals["unclassified"],
    }
    gpu_counts = [
        attempt.scheduling_admission.accelerator_count for attempt in expected.values() if attempt.scheduling_admission
    ]
    return ScientificGpuAccounting(
        gpu_count=max(gpu_counts) if gpu_counts else None,
        capacity_type="unknown",
        allocated=(
            unavailable("Scheduler occupancy boundaries are missing; device allocation is a different clock.")
            if "scheduler_occupancy_clock_missing" in data_gaps else measurement(occupied)
        ),
        active=measurement(active),
        idle_total=measurement(idle),
        idle_by_cause=[
            ScientificIdleCause(cause=cause, duration=measurement(value))
            for cause, value in idle_causes.items()
            if value > 0
        ],
        grace_drain=measurement(grace),
        reconciliation_delta=measurement(
            sum(abs(float(row["reconciliation_delta_seconds"])) for row in selected.values())
        ),
        phase_partition=partition,
        quota_reserved=clock("quota_reserved_gpu_seconds", "quota_reservation_clock_missing"),
        device_allocated=clock("device_allocated_gpu_seconds", "device_allocation_clock_missing"),
        sampled_device_activity=unavailable(
            "Device samples are joined separately in run detail. Execution wall time is not GPU busy time."
        ),
        data_gaps=data_gaps,
    )
