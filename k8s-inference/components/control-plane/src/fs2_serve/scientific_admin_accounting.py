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
    selected = {
        UUID(str(row["attempt_id"])): row
        for row in rows
        if UUID(str(row["attempt_id"])) in expected and row.get("rollup_id") is not None
    }
    if not selected:
        return None
    complete = len(selected) == len(expected)
    exact = complete and all(
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
        allocated=measurement(occupied),
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
    )
