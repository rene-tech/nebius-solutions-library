import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from fs2_serve.lifecycle import LifecycleSignal, MeasurementQuality
from fs2_serve.scientific_admin_phase_times import project_phase_times


def actual_restore_signals() -> list[LifecycleSignal]:
    document = json.loads((Path(__file__).parent / "fixtures/protenix_restore_phase_signals.json").read_text())
    return [LifecycleSignal.model_validate(row) for row in document["signals"]]


def pair(key: str, start: float, end: float, *, gpu_count: int = 1) -> list[LifecycleSignal]:
    subject = uuid4()
    origin = datetime(2026, 9, 7, tzinfo=UTC)
    return [
        LifecycleSignal.model_validate(
            {
                "event_key": f"{key}:{edge}",
                "subject_id": subject,
                "occurred_at": origin + timedelta(seconds=offset),
                "observed_at": origin + timedelta(seconds=100),
                "source": "kubernetes",
                "quality": "measured",
                "phase": "restore",
                "edge": edge,
                "clock": "phase",
                "interval_key": key,
                "gpu_count": gpu_count,
            }
        )
        for edge, offset in (("start", start), ("end", end))
    ]


def restore(signals, **kwargs):
    return next(item.duration for item in project_phase_times(signals, **kwargs) if item.phase == "restore")


def test_actual_protenix_restore_uses_retained_signal_clocks_not_gpu_seconds_or_ingestion() -> None:
    signals = actual_restore_signals()
    assert all(signal.gpu_count == 0 for signal in signals)
    result = restore(signals)
    assert result.value == pytest.approx(3.946846)
    assert result.evidence.value == "estimated"
    assert result.source == "lifecycle-signal-boundaries"
    # Synthetic equal *observation* times cannot turn distinct occurrence times into zero.
    observed = datetime(2026, 9, 7, 17, tzinfo=UTC)
    delayed = [signal.model_copy(update={"observed_at": observed}) for signal in signals]
    assert restore(delayed).value == result.value


def test_parallel_rank_observations_are_unioned_and_disjoint_retries_remain() -> None:
    first = pair("first", 0, 10, gpu_count=8)
    parallel = pair("parallel", 5, 15)
    retry = pair("retry", 20, 23)
    rank_duplicate = pair("duplicate-rank-observation", 0, 10)
    result = restore([*first, *parallel, *retry, *rank_duplicate, *first])
    assert result.value == 18
    assert result.evidence.value == "measured"


def test_open_missing_and_unavailable_quality_never_become_measured_zero() -> None:
    closed = pair("closed", 0, 4)
    open_signal = pair("open", 5, 9)[:1]
    assert restore(open_signal).value is None
    assert restore(open_signal).evidence.value == "unavailable"
    partial = restore([*closed, *open_signal])
    assert partial.value == 4 and partial.evidence.value == "estimated"
    assert "Open/missing" in partial.reason
    assert restore(closed, incomplete_attempts=True).evidence.value == "estimated"
    unavailable = [signal.model_copy(update={"quality": MeasurementQuality.UNAVAILABLE}) for signal in closed]
    assert restore(unavailable).value is None
    assert all(item.duration.value is None for item in project_phase_times(()))


def test_mismatched_or_reversed_intervals_do_not_publish_false_duration() -> None:
    reversed_pair = pair("backwards", 8, 3)
    assert restore(reversed_pair).value is None
    mismatched = pair("mismatch", 0, 3)
    mismatched[1] = mismatched[1].model_copy(update={"attempt": 2})
    assert restore(mismatched).value is None
