"""Bounded, exact-identity DCGM sample summaries in the existing lifecycle ledger.

Samples are observations, never intervals of GPU work or billable idle time.
Raw vectors remain in Prometheus; their canonical hashes and summaries survive
its retention window in the existing immutable attempt ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .lifecycle import (
    ACCOUNTING_PHASES,
    LifecycleClock,
    LifecycleCorrelation,
    LifecycleEdge,
    LifecyclePhase,
    LifecycleRepository,
    LifecycleSignal,
    LifecycleSource,
    MeasurementQuality,
    _paired_intervals,
)
from .models import StrictModel
from .scientific_batch.models import ScientificAttemptState, ScientificBatchState

METRIC = "DCGM_FI_DEV_GPU_UTIL"
MAX_SERIES = 128
MAX_SAMPLES = 100000
MAX_WINDOW_SECONDS = 10 * 24 * 60 * 60
_SUMMARY_KEYS = (
    "sample_count", "zero_samples", "positive_samples", "first_sample_at", "last_sample_at",
    "allocation_start", "allocation_end", "max_gap_seconds", "min_percent", "max_percent",
    "samples_sha256", "phase_samples", "phase_zero_samples", "phase_positive_samples",
)
ACTIVITY_DETAIL_KEYS = frozenset(f"dcgm_{key}" for key in (*_SUMMARY_KEYS, "rejected_groups"))


class DeviceActivitySummary(StrictModel):
    pod_uid: str
    node_uid: str | None = None
    gpu_uuid: str
    sample_count: int = Field(ge=1, le=MAX_SAMPLES)
    zero_samples: int = Field(ge=0)
    positive_samples: int = Field(ge=0)
    first_sample_at: AwareDatetime
    last_sample_at: AwareDatetime
    allocation_start: AwareDatetime
    allocation_end: AwareDatetime
    # Includes the leading/trailing unsampled edges of the allocation window.
    max_gap_seconds: float = Field(ge=0, allow_inf_nan=False)
    min_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    max_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    samples_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    phase_samples: dict[str, int]
    phase_zero_samples: dict[str, int]
    phase_positive_samples: dict[str, int]

    @model_validator(mode="after")
    def consistent(self) -> DeviceActivitySummary:
        if not self.allocation_start <= self.first_sample_at <= self.last_sample_at <= self.allocation_end:
            raise ValueError("activity sample timestamps are outside the verified allocation")
        if self.zero_samples + self.positive_samples != self.sample_count or self.min_percent > self.max_percent:
            raise ValueError("activity sample counts/range are inconsistent")
        valid_phases = {str(phase) for phase in ACCOUNTING_PHASES}
        for values, total in (
            (self.phase_samples, self.sample_count),
            (self.phase_zero_samples, self.zero_samples),
            (self.phase_positive_samples, self.positive_samples),
        ):
            if (set(values) - valid_phases or any(value < 0 for value in values.values())
                    or sum(values.values()) != total):
                raise ValueError("activity phase counts are inconsistent")
        for phase, count in self.phase_samples.items():
            if self.phase_zero_samples.get(phase, 0) + self.phase_positive_samples.get(phase, 0) != count:
                raise ValueError("activity phase zero/positive counts are inconsistent")
        return self

    def signal_detail(self) -> dict[str, str]:
        value = self.model_dump(mode="json")
        return {f"dcgm_{key}": json.dumps(value[key], separators=(",", ":"), sort_keys=True) for key in _SUMMARY_KEYS}


def activity_summaries(signals: Sequence[LifecycleSignal]) -> list[DeviceActivitySummary]:
    result = []
    for signal in signals:
        if signal.source is not LifecycleSource.DCGM or signal.detail.get("reason_code") != "dcgm_sample_summary":
            continue
        assert signal.pod_uid is not None and signal.gpu_uuid is not None
        value = {key: json.loads(str(signal.detail[f"dcgm_{key}"])) for key in _SUMMARY_KEYS}
        result.append(DeviceActivitySummary(
            pod_uid=signal.pod_uid, node_uid=signal.node_uid, gpu_uuid=signal.gpu_uuid, **value,
        ))
    return result


def activity_capture_reason(signals: Sequence[LifecycleSignal]) -> str:
    return next((str(signal.detail["reason_code"]) for signal in signals
                 if signal.source is LifecycleSource.DCGM and signal.event_key.endswith(":dcgm-capture:v1")),
                "dcgm_not_captured")


class ScientificActivityReader(Protocol):
    async def scientific_activity_matrix(
        self, *, operation_id: UUID, attempt_id: UUID, from_at: datetime, to_at: datetime,
    ) -> Mapping[str, Any]: ...


def summarize_matrix(
    data: Mapping[str, Any], *, state: ScientificBatchState, attempt: ScientificAttemptState,
    signals: Sequence[LifecycleSignal], correlations: Sequence[LifecycleCorrelation], observed_at: datetime,
) -> tuple[list[LifecycleSignal], int]:
    """Join exact operation/attempt/model/tenant/Pod/device and allocation time.

    Duplicate exporter series at the same timestamp must agree. A conflict
    rejects that whole device, not one inconvenient value. No nearest timestamp,
    ready-Pod guessing, resampling, interpolation, or average-util integration.
    """
    rows = data.get("result")
    if data.get("resultType") != "matrix" or not isinstance(rows, list) or len(rows) > MAX_SERIES:
        raise ValueError("DCGM raw matrix is outside the bound")
    intervals, _ = _paired_intervals(signals)
    allocations = [item for item in intervals if item.clock is LifecycleClock.DEVICE_ALLOCATED]
    starts = {item.interval_key: item for item in signals if item.clock is LifecycleClock.DEVICE_ALLOCATED
              and item.edge is LifecycleEdge.START}
    device_pairs = {(item.pod_uid, item.gpu_uuid, item.gpu_rank) for item in correlations
                    if item.subject_id == attempt.attempt_id and item.source is LifecycleSource.KUBELET}
    node_uids: dict[str, set[str]] = defaultdict(set)
    for item in correlations:
        if item.subject_id == attempt.attempt_id and item.pod_uid and item.node_uid:
            node_uids[item.pod_uid].add(item.node_uid)
    phases = [item for item in intervals if item.clock is LifecycleClock.PHASE
              and item.quality is not MeasurementQuality.UNAVAILABLE]
    samples: dict[str, dict[datetime, float]] = defaultdict(dict)
    conflicts: set[str] = set()
    rejected = 0
    total = 0
    for row in rows:
        metric = row.get("metric") if isinstance(row, dict) else None
        values = row.get("values") if isinstance(row, dict) else None
        if not isinstance(metric, dict) or not isinstance(values, list):
            raise ValueError("DCGM series shape is invalid")
        total += len(values)
        if total > MAX_SAMPLES:
            raise ValueError("DCGM raw sample count exceeds the bound")
        expected = {
            "__name__": METRIC, "fs2_nebius_ai_operation_id": str(state.operation_id),
            "fs2_nebius_ai_attempt_id": str(attempt.attempt_id), "fs2_nebius_ai_model_id": state.model_id,
            "fs2_nebius_ai_tenant_id": state.tenant_id, "fs2_nebius_ai_stage_id": attempt.stage_id,
        }
        matched = [item for item in allocations if item.pod_uid == metric.get("pod_uid")
                   and item.gpu_uuid == metric.get("UUID")]
        if any(metric.get(key) != value for key, value in expected.items()) or len(matched) != 1:
            rejected += 1
            continue
        allocation = matched[0]
        start = starts[allocation.key]
        assert start.pod_uid is not None and start.gpu_uuid is not None
        if (metric.get("pod") != start.pod_name or metric.get("namespace") != start.namespace
                or (start.pod_uid, start.gpu_uuid, start.gpu_rank) not in device_pairs
                or len(node_uids[start.pod_uid]) > 1):
            rejected += 1
            continue
        for pair in values:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("DCGM raw sample shape is invalid")
            timestamp, value = float(pair[0]), float(pair[1])
            if not math.isfinite(timestamp) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError("DCGM raw sample is invalid")
            when = datetime.fromtimestamp(timestamp, UTC)
            if not allocation.start <= when < allocation.end or when > observed_at:
                continue
            previous = samples[allocation.key].setdefault(when, value)
            if previous != value:
                conflicts.add(allocation.key)
    result = []
    for allocation in allocations:
        points = sorted(samples[allocation.key].items())
        if allocation.key in conflicts:
            rejected += 1
            continue
        if not points:
            continue
        start = starts[allocation.key]
        assert start.pod_uid is not None and start.gpu_uuid is not None
        phase_counts: Counter[str] = Counter()
        zero: Counter[str] = Counter()
        positive: Counter[str] = Counter()
        for when, value in points:
            matching = {item.phase.value for item in phases if item.start <= when < item.end
                        and item.pod_uid in {None, allocation.pod_uid}}
            phase = next(iter(matching)) if len(matching) == 1 else LifecyclePhase.UNCLASSIFIED.value
            phase_counts[phase] += 1
            (positive if value > 0 else zero)[phase] += 1
        boundaries = [allocation.start, *(when for when, _ in points), allocation.end]
        raw = [[when.isoformat(), value] for when, value in points]
        summary = DeviceActivitySummary(
            pod_uid=start.pod_uid, node_uid=next(iter(node_uids[start.pod_uid]), None), gpu_uuid=start.gpu_uuid,
            sample_count=len(points), zero_samples=sum(zero.values()), positive_samples=sum(positive.values()),
            first_sample_at=points[0][0], last_sample_at=points[-1][0],
            allocation_start=allocation.start, allocation_end=allocation.end,
            max_gap_seconds=max(
                (end - begin).total_seconds() for begin, end in zip(boundaries, boundaries[1:], strict=False)
            ),
            min_percent=min(value for _, value in points), max_percent=max(value for _, value in points),
            samples_sha256=hashlib.sha256(json.dumps(raw, separators=(",", ":")).encode()).hexdigest(),
            phase_samples=dict(phase_counts), phase_zero_samples=dict(zero), phase_positive_samples=dict(positive),
        )
        result.append(LifecycleSignal(
            event_key=f"{allocation.key}:dcgm-summary:v1", subject_id=attempt.attempt_id,
            occurred_at=summary.last_sample_at, observed_at=observed_at, source=LifecycleSource.DCGM,
            quality=MeasurementQuality.MEASURED, phase=LifecyclePhase.GPU_ALLOCATION,
            edge=LifecycleEdge.INSTANT, clock=LifecycleClock.LIFECYCLE, attempt=attempt.attempt_number,
            gpu_count=1, cluster=start.cluster, namespace=start.namespace, job_uid=start.job_uid,
            pod_uid=start.pod_uid, pod_name=start.pod_name, node_uid=summary.node_uid,
            gpu_uuid=start.gpu_uuid, gpu_rank=start.gpu_rank,
            detail={"reason_code": "dcgm_sample_summary", **summary.signal_detail()},
        ))
    return result, rejected


class ScientificActivityCapture:
    def __init__(
        self, *, reader: ScientificActivityReader, lifecycle: LifecycleRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.reader, self.lifecycle, self.clock = reader, lifecycle, clock

    async def capture(
        self, state: ScientificBatchState, attempt: ScientificAttemptState, terminal_at: datetime,
    ) -> None:
        detail = await self.lifecycle.get_workload(attempt.attempt_id, tenant_id=state.tenant_id)
        if detail is None or activity_capture_reason(detail.signals) != "dcgm_not_captured":
            return
        intervals, _ = _paired_intervals(detail.signals)
        allocations = [item for item in intervals if item.clock is LifecycleClock.DEVICE_ALLOCATED]
        observed_at = max(terminal_at, self.clock())
        reason = "dcgm_no_verified_allocation"
        results: list[LifecycleSignal] = []
        rejected = 0
        if allocations:
            begin, end = min(item.start for item in allocations), max(item.end for item in allocations)
            reason = "dcgm_window_outside_bound"
            if 0 < (end - begin).total_seconds() <= MAX_WINDOW_SECONDS:
                try:
                    matrix = await self.reader.scientific_activity_matrix(
                        operation_id=state.operation_id, attempt_id=attempt.attempt_id, from_at=begin, to_at=end,
                    )
                    results, rejected = summarize_matrix(
                        matrix, state=state, attempt=attempt, signals=detail.signals,
                        correlations=detail.correlations, observed_at=observed_at,
                    )
                    reason = (
                        "dcgm_capture_observed" if len(results) == len(allocations) and not rejected
                        else "dcgm_capture_partial" if results else "dcgm_no_verified_samples"
                    )
                except Exception:
                    # Telemetry fetch/parse failure is not an inference failure.
                    # Raw exception text/response labels are deliberately not retained.
                    reason = "dcgm_source_unavailable"
        results.append(LifecycleSignal(
            event_key=f"scientific:{attempt.attempt_id}:dcgm-capture:v1", subject_id=attempt.attempt_id,
            occurred_at=terminal_at, observed_at=observed_at, source=LifecycleSource.DCGM,
            quality=MeasurementQuality.APPLICATION_OBSERVED if results else MeasurementQuality.UNAVAILABLE,
            phase=LifecyclePhase.GPU_ALLOCATION, edge=LifecycleEdge.INSTANT, clock=LifecycleClock.LIFECYCLE,
            attempt=attempt.attempt_number,
            detail={"reason_code": reason, "dcgm_rejected_groups": str(rejected)},
        ))
        # One transaction, bounded to at most one compact summary per device plus
        # one marker. Reconciliation/restarts do not re-query or rewrite a capture.
        await self.lifecycle.append_signals(results)
