"""Offline log-window regression tests; no credentials, inference or network."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "publication_window", Path(__file__).with_name("capture_publication_window.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def event(stamp, disposition="publish", pod="cp-1", **extra):
    payload = {
        "event": "model_publication_changed", "model_ref": "qwen3-8b",
        "disposition": disposition, "phase": "Desired" if disposition == "withdraw" else "Ready",
        "reason": "not-ready" if disposition == "withdraw" else "ready", **extra,
    }
    return {
        "stream": {"k8s_pod_name": pod, "k8s_pod_uid": pod + "-uid"},
        "values": [[str(stamp), "2026-09-07 16:00:00 INFO bridge " + json.dumps(payload)]],
    }


def chunk(streams, start="2026-09-07T16:00:00Z", end="2026-09-07T16:05:00Z", code=0):
    return {
        "start": start, "end": end, "returncode": code,
        "payload": {"response": {"status": "success", "data": {"resultType": "streams", "result": streams}}},
    }


def test_windows_cover_exact_closed_range_without_gaps():
    start, end = module.timestamp("2026-09-07T16:00:00Z"), module.timestamp("2026-09-07T16:11:03.125Z")
    rows = list(module.windows(start, end))
    assert len(rows) == 3 and rows[0][0] == start and rows[-1][1] == end
    assert all(left[1] == right[0] for left, right in zip(rows, rows[1:], strict=False))


@pytest.mark.parametrize("end", ["2026-09-07T16:00:00Z", "2026-09-07T20:00:01Z"])
def test_invalid_duration_is_rejected(end):
    with pytest.raises(ValueError):
        list(module.windows(module.timestamp("2026-09-07T16:00:00Z"), module.timestamp(end)))


def test_short_withdrawal_cannot_be_hidden_by_successful_clients():
    result = module.project_transitions([chunk([event(1_000_000_000, "withdraw"), event(6_200_000_000)])])
    assert result["verdict"] == "withdrawal-observed"
    assert result["withdrawals"][0]["duration_seconds"] == 5.2
    assert result["withdrawals"][0]["open_at_window_end"] is False


def test_unmatched_withdrawal_is_unknown_duration_not_zero():
    result = module.project_transitions([chunk([event(1, "withdraw"), event(2, pod="another-cp")])])
    assert result["withdrawals"][0]["duration_seconds"] is None
    assert result["withdrawals"][0]["open_at_window_end"] is True


def test_boundary_duplicates_are_removed_without_merging_distinct_replicas():
    result = module.project_transitions([
        chunk([event(1, "withdraw", ignored_field="do-not-export")]),
        chunk([event(1, "withdraw"), event(1, "withdraw", pod="cp-2")],
              start="2026-09-07T16:05:00Z", end="2026-09-07T16:10:00Z"),
    ])
    assert len(result["events"]) == 2
    assert "do-not-export" not in json.dumps(result)


@pytest.mark.parametrize("case", ["truncated", "failed-query", "malformed", "gap"])
def test_missing_evidence_never_reports_clean(case):
    chunks = [chunk([])]
    if case == "truncated":
        chunks[0] = chunk([event(1)] * module.QUERY_LIMIT)
    elif case == "failed-query":
        chunks[0]["returncode"] = 1
    elif case == "malformed":
        chunks[0] = chunk([{"stream": {}, "values": [["1", "bad JSON"]]}])
    elif case == "gap":
        chunks.append(chunk([], start="2026-09-07T16:06:00Z", end="2026-09-07T16:10:00Z"))
    result = module.project_transitions(chunks)
    assert result["query_evidence_complete"] is False
    assert result["verdict"] == "evidence-incomplete"


def test_empty_complete_window_is_not_an_uptime_claim():
    result = module.project_transitions([chunk([])])
    assert result["query_evidence_complete"] is True
    assert result["verdict"] == "no-withdrawal-recorded"
    assert any("does not prove" in note for note in result["limitations"])
