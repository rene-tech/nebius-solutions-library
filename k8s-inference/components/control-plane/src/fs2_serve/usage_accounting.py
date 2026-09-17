"""Read-only, non-billing projections of immutable lifecycle and admission facts.

The old occupied_idle rollup is occupied minus active: it includes load and
unknown time. Never reuse that residual as classified idle. Online operations
can overlap on a shared GPU, so their subject clocks are not additive bills.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from pydantic import Field

from .admin_models import AdminMeasurement, AdminValueState
from .lifecycle import LifecycleWorkloadSummary, MeasurementQuality, WorkloadTelemetryKind
from .models import StrictModel

STARTUP_PHASES = ("image_pull", "artifact_load", "restore", "compile", "warmup")
IDLE_PHASES = ("workflow_wait", "resident_idle", "cooldown_grace")
OTHER_PHASES = ("checkpoint_drain", "teardown")


def unavailable(reason: str) -> AdminMeasurement:
    return AdminMeasurement(
        value=None, unit="gpu-seconds", state=AdminValueState.UNAVAILABLE, source="lifecycle", reason=reason
    )


class LifecycleUsageAccounting(StrictModel):
    source: str = "immutable lifecycle latest-rollup projection; no ledger mutation"
    attribution: str = "Exclusive scientific attempts only; shared serving overhead is unallocated. Not a bill."
    subjects: int = Field(default=0, ge=0)
    covered_operations: int = Field(default=0, ge=0)
    expected_operations: int = Field(default=0, ge=0)
    occupied_complete: bool = False
    phases_complete: bool = False
    quality: MeasurementQuality = MeasurementQuality.UNAVAILABLE
    data_gaps: list[str] = Field(default_factory=list)
    occupied: AdminMeasurement
    active: AdminMeasurement
    classified_idle: AdminMeasurement
    startup: AdminMeasurement
    other: AdminMeasurement
    unknown: AdminMeasurement
    queue: AdminMeasurement = Field(
        default_factory=lambda: AdminMeasurement(
            value=None,
            unit="seconds",
            state=AdminValueState.UNAVAILABLE,
            source="lifecycle",
            reason="Queue wall time is not GPU occupancy; no complete owner-level queue interval attribution.",
        )
    )


def lifecycle_usage_from_counts(row: Mapping[str, Any]) -> LifecycleUsageAccounting:
    """Project aggregates without replacing missing phase evidence with zero."""
    subjects = int(row.get("lifecycle_subjects") or 0)
    expected = int(row.get("scientific_requests") or 0)
    covered = int(row.get("lifecycle_operations") or 0)
    complete = bool(subjects and row.get("lifecycle_complete") and covered == expected)
    unknown = float(row.get("unknown") or 0)
    gaps = []
    if not subjects:
        gaps.append("exclusive_lifecycle_unavailable")
    if covered != expected:
        gaps.append("operation_coverage_incomplete")
    if subjects and not complete:
        gaps.append("occupied_clock_incomplete")
    phase_complete = complete and bool(row.get("phases_complete")) and unknown <= 0.001
    if not phase_complete:
        gaps.append("phase_classification_incomplete")
    quality = MeasurementQuality(row.get("lifecycle_quality") or "unavailable")
    if quality is MeasurementQuality.UNAVAILABLE:
        complete = phase_complete = False
        if "measurement_quality_unavailable" not in gaps:
            gaps.append("measurement_quality_unavailable")

    def metric(name: str, *, phase: bool = True) -> AdminMeasurement:
        if not complete:
            return unavailable(
                "No complete reconciled exclusive scientific occupancy for this cohort; shared serving is unallocated."
            )
        # A partial phase sum is a known classified lower bound, not a complete
        # active/idle total. Keep numeric evidence with explicit estimated state.
        reason = "Exclusive scientific lifecycle; shared serving overhead unallocated. Not billable usage."
        if phase and not phase_complete:
            reason += " Incomplete classification; known phase sum only (unknown shown separately)."
        return AdminMeasurement(
            value=float(row.get(name) or 0),
            unit="gpu-seconds",
            source="lifecycle",
            state=(
                AdminValueState.AVAILABLE
                if quality is MeasurementQuality.MEASURED and (not phase or phase_complete)
                else AdminValueState.ESTIMATED
            ),
            reason=reason,
        )

    return LifecycleUsageAccounting(
        subjects=subjects,
        covered_operations=covered,
        expected_operations=expected,
        occupied_complete=complete,
        phases_complete=phase_complete,
        quality=quality,
        data_gaps=gaps,
        occupied=metric("occupied", phase=False),
        active=metric("active"),
        classified_idle=metric("classified_idle"),
        startup=metric("startup"),
        other=metric("other"),
        unknown=metric("unknown", phase=False),
    )


def project_attempt(workload: LifecycleWorkloadSummary) -> dict[str, Any]:
    """A payload-free per-subject receipt, preserving original quality and IDs."""
    subject, rollup = workload.subject, workload.rollup
    result: dict[str, Any] = {
        "tenant_id": subject.tenant_id,
        "principal_id": subject.principal_id,
        "api_key_id": str(subject.api_key_id) if subject.api_key_id else None,
        "model_id": subject.model_id,
        "operation_id": str(subject.operation_id),
        "attempt_id": str(subject.attempt_id) if subject.attempt_id else None,
        "subject_id": str(subject.subject_id),
        "workload_kind": subject.workload_kind.value,
        "accepted_at": subject.accepted_at.isoformat(),
        "additive_owner_usage": subject.workload_kind is WorkloadTelemetryKind.SCIENTIFIC_BATCH,
        "rollup_id": str(rollup.rollup_id) if rollup else None,
        "quality": rollup.quality.value if rollup else "unavailable",
        "terminal": rollup.terminal if rollup else False,
        "outcome": rollup.outcome if rollup else None,
        "reconciled": rollup.reconciled if rollup else False,
        "data_gaps": list(rollup.data_gaps) if rollup else ["rollup_missing"],
        "occupied": None,
        "active": None,
        "classified_idle": None,
        "startup": None,
        "other": None,
        "unknown": None,
    }
    if rollup is None:
        return result
    phases = rollup.phase_gpu_seconds
    active = phases.get("active_compute", 0.0)
    idle = sum(phases.get(phase, 0.0) for phase in IDLE_PHASES)
    startup = sum(phases.get(phase, 0.0) for phase in STARTUP_PHASES)
    other = sum(phases.get(phase, 0.0) for phase in OTHER_PHASES)
    # Include residual or unrecognized historical phase names as unknown.
    unknown = max(0.0, rollup.scheduler_occupied_gpu_seconds - active - idle - startup - other)
    result.update(
        occupied=rollup.scheduler_occupied_gpu_seconds,
        active=active,
        classified_idle=idle,
        startup=startup,
        other=other,
        unknown=unknown,
        quota_reserved_gpu_seconds=rollup.quota_reserved_gpu_seconds,
        device_allocated_gpu_seconds=rollup.device_allocated_gpu_seconds,
        legacy_nonactive_residual_gpu_seconds=rollup.occupied_idle_gpu_seconds,
        phase_gpu_seconds=phases,
        events_sha256=rollup.events_sha256,
        generated_at=rollup.generated_at.isoformat(),
    )
    return result


def reconciliation_report(
    workloads: Sequence[LifecycleWorkloadSummary],
    *,
    expected_operation_ids: set[UUID],
    admission_snapshots: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Deduplicate immutable subject snapshots; never multiply retry estimates.

    A caller supplies latest rollups from a bounded, accepted-in-window cohort.
    Conflicting snapshots are rejected rather than silently summing both.
    """
    unique: dict[UUID, LifecycleWorkloadSummary] = {}
    for workload in workloads:
        previous = unique.setdefault(workload.subject.subject_id, workload)
        if previous != workload:
            raise ValueError("conflicting rollups for the same subject; export a consistent latest snapshot")
    rows = [project_attempt(workload) for workload in unique.values()]
    exclusive = [row for row in rows if row["additive_owner_usage"]]
    valid = all(
        row["rollup_id"]
        and row["terminal"]
        and row["reconciled"]
        and "scheduler_occupancy_clock_missing" not in row["data_gaps"]
        for row in exclusive
    )
    quality_order = {"measured": 0, "application_observed": 1, "estimated": 2, "unavailable": 3}
    quality = max((row["quality"] for row in exclusive), key=quality_order.__getitem__, default="unavailable")
    counts: dict[str, Any] = {
        "lifecycle_subjects": len(exclusive),
        "scientific_requests": len(expected_operation_ids),
        "lifecycle_operations": len({row["operation_id"] for row in exclusive}),
        "lifecycle_complete": valid
        and {row["operation_id"] for row in exclusive} == {str(value) for value in expected_operation_ids},
        "phases_complete": all(not row["data_gaps"] for row in exclusive),
        "lifecycle_quality": quality,
    }
    for phase in ("occupied", "active", "classified_idle", "startup", "other", "unknown"):
        counts[phase] = sum(row[phase] or 0 for row in exclusive)
    return {
        "schema": "fs2.usage-reconciliation/v1",
        "billing": False,
        "ledger_mutated": False,
        "cohort_basis": (
            "operations accepted in [from,to); full recorded attempt lifetimes, not clipped wall-time billing"
        ),
        "admission_snapshot_basis": (
            "Lifetime key counters at snapshot time; NOT window usage; "
            "rotations may inherit counters. Do not sum across keys."
        ),
        "admission_snapshots": list(admission_snapshots),
        "exclusive_scientific_usage": lifecycle_usage_from_counts(counts).model_dump(mode="json"),
        "online_subjects_excluded_from_additive_totals": len(rows) - len(exclusive),
        "attempts": rows,
        "notes": [
            "No currency or bill is calculated.",
            "Shared-serving intervals may overlap. Per-subject online observations are not additive customer usage.",
            "Failed and retried attempts remain visible once per immutable subject.",
            "Queue wall time is not occupied GPU time and is unavailable without interval attribution.",
        ],
    }
