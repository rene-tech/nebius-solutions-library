"""Observed phase wall-time unions, separate from GPU occupancy and ingestion."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from .lifecycle import (
    LifecycleClock,
    LifecyclePhase,
    LifecycleSignal,
    MeasurementQuality,
    _Interval,
    _merge_seconds,
    _paired_intervals,
)
from .scientific_admin_models import ScientificEvidenceState, ScientificLifecyclePhase, ScientificMeasurement

AdminPhase = Literal[
    "queue",
    "admission",
    "image-pull",
    "artifact-load",
    "restore",
    "semantic-warmup",
    "active-compute",
    "allocated-idle",
    "grace-drain",
    "teardown",
]
_PHASES: dict[AdminPhase, frozenset[LifecyclePhase]] = {
    "queue": frozenset({LifecyclePhase.ADMISSION_WAIT}),
    # Admission is an instant in this ledger, not a measured duration.
    "admission": frozenset(),
    "image-pull": frozenset({LifecyclePhase.IMAGE_PULL}),
    "artifact-load": frozenset({LifecyclePhase.ARTIFACT_LOAD}),
    "restore": frozenset({LifecyclePhase.RESTORE}),
    "semantic-warmup": frozenset({LifecyclePhase.COMPILE, LifecyclePhase.WARMUP}),
    "active-compute": frozenset({LifecyclePhase.ACTIVE_COMPUTE}),
    "allocated-idle": frozenset({LifecyclePhase.WORKFLOW_WAIT, LifecyclePhase.RESIDENT_IDLE}),
    "grace-drain": frozenset({LifecyclePhase.COOLDOWN_GRACE, LifecyclePhase.CHECKPOINT_DRAIN}),
    "teardown": frozenset({LifecyclePhase.TEARDOWN}),
}


def project_phase_times(
    signals: Sequence[LifecycleSignal],
    *,
    incomplete_attempts: bool = False,
) -> list[ScientificLifecyclePhase]:
    """Use the ledger's strict pairing and interval union, ignoring GPU count.

    Parallel attempts and duplicate GPU-rank observations do not multiply wall
    time. Disjoint retries remain included. Different phases can overlap, so
    these durations must not be added to obtain end-to-end request latency.
    """
    result = []
    for phase, ledger_phases in _PHASES.items():
        by_subject: dict[UUID, dict[str, LifecycleSignal]] = defaultdict(dict)
        for signal in signals:
            if signal.phase in ledger_phases and signal.clock in {LifecycleClock.PHASE, LifecycleClock.LIFECYCLE}:
                by_subject[signal.subject_id][signal.event_key] = signal
        intervals: list[_Interval] = []
        gaps = []
        for subject_signals in by_subject.values():
            paired, missing = _paired_intervals(tuple(subject_signals.values()))
            intervals.extend(item for item in paired if item.quality is not MeasurementQuality.UNAVAILABLE)
            if any(item.quality is MeasurementQuality.UNAVAILABLE for item in paired):
                gaps.append("unavailable_interval_quality")
            gaps.extend(missing)
        if not intervals:
            duration = ScientificMeasurement(
                value=None,
                unit="seconds",
                evidence=ScientificEvidenceState.UNAVAILABLE,
                source="lifecycle-signal-boundaries",
                reason="No complete observed start/end interval is available for this phase.",
            )
        else:
            measured = (
                not gaps
                and not incomplete_attempts
                and all(item.quality is MeasurementQuality.MEASURED for item in intervals)
            )
            reason = (
                None
                if measured
                else (
                    "Union of closed observed intervals; application-observed or incomplete boundaries. "
                    "Open/missing intervals are not estimated from ingestion time."
                )
            )
            duration = ScientificMeasurement(
                value=_merge_seconds((item.start, item.end) for item in intervals),
                unit="seconds",
                evidence=ScientificEvidenceState.MEASURED if measured else ScientificEvidenceState.ESTIMATED,
                source="lifecycle-signal-boundaries",
                reason=reason,
            )
        result.append(ScientificLifecyclePhase(phase=phase, duration=duration))
    return result
