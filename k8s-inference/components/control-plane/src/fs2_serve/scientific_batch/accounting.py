"""Reservation-to-usage settlement for terminal scientific GPU batches."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .models import AttemptOutcome, BatchStatus, ResourceClass, ScientificBatchState

SettlementReason = Literal[
    "observed_execution",
    "no_gpu_execution",
    "missing_evidence_fail_safe",
]


@dataclass(frozen=True, slots=True)
class ScientificGpuSettlement:
    reserved_gpu_seconds: float
    charged_gpu_seconds: float
    released_gpu_seconds: float
    evidence_complete: bool
    reason: SettlementReason


def settle_scientific_gpu_reservation(
    state: ScientificBatchState,
    *,
    reserved_gpu_seconds: float,
) -> ScientificGpuSettlement:
    """Settle a bounded admission reservation from terminal attempt evidence.

    Kueue quota reservation is the conservative start boundary and workload
    completion is the end boundary. A cancelled or failed run with no observed
    accelerator admission charges zero. Any accelerator attempt whose timing
    or resource identity is incomplete makes the bounded admission estimate the
    fail-safe charge; it can never charge beyond the amount admitted up front.
    """

    if (
        not state.status.terminal
        or not math.isfinite(reserved_gpu_seconds)
        or reserved_gpu_seconds < 0
    ):
        raise ValueError("scientific GPU settlement requires a terminal state and finite reservation")

    resource_classes = {stage.stage_id: stage.resource_class for stage in state.plan.stages}
    gpu_stage_ids = {
        stage_id for stage_id, resource_class in resource_classes.items()
        if resource_class is ResourceClass.GPU
    }
    observed_gpu_stage_ids: set[str] = set()
    observed_gpu = False
    missing_evidence = False
    observed_gpu_seconds = 0.0
    for stage in state.stages:
        for attempt in stage.attempts:
            admission = attempt.scheduling_admission
            if admission is None or admission.accelerator_count == 0:
                if (
                    stage.stage_id in gpu_stage_ids
                    and attempt.outcome is AttemptOutcome.SUCCEEDED
                ):
                    missing_evidence = True
                continue
            observed_gpu = True
            observed_gpu_stage_ids.add(stage.stage_id)
            if resource_classes.get(stage.stage_id) is not ResourceClass.GPU:
                missing_evidence = True
                continue
            started_at = admission.quota_reserved_at or admission.admitted_at
            completed_at = attempt.completed_at
            if (
                started_at is None
                or completed_at is None
                or completed_at < started_at
                or not attempt.resource_released
            ):
                missing_evidence = True
                continue
            observed_gpu_seconds += admission.accelerator_count * (
                completed_at - started_at
            ).total_seconds()

    if state.status is BatchStatus.SUCCEEDED and not gpu_stage_ids.issubset(observed_gpu_stage_ids):
        missing_evidence = True

    if missing_evidence or observed_gpu_seconds > reserved_gpu_seconds:
        charged = reserved_gpu_seconds
        evidence_complete = False
        reason: SettlementReason = "missing_evidence_fail_safe"
    elif observed_gpu:
        charged = observed_gpu_seconds
        evidence_complete = True
        reason = "observed_execution"
    else:
        charged = 0.0
        evidence_complete = True
        reason = "no_gpu_execution"
    released = max(0.0, reserved_gpu_seconds - charged)
    return ScientificGpuSettlement(
        reserved_gpu_seconds=reserved_gpu_seconds,
        charged_gpu_seconds=charged,
        released_gpu_seconds=released,
        evidence_complete=evidence_complete,
        reason=reason,
    )
