"""api 0.11.155: the per-read memo of stage reads remembers a 404 as (status, detail) and raises a fresh HTTPException
each time; the remembered exception object kept its traceback and every re-raise grew it by the raising frames (the
caller's payloads stayed reachable until a gc pass)."""

import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs  # noqa: E402
import reconciliation  # noqa: E402
import stages  # noqa: E402


def test_memoized_404_is_a_fresh_exception_without_traceback_growth(monkeypatch):
    calls = []

    def find(ns, name):
        calls.append(name)
        raise HTTPException(404, f"operation {name} not found")

    monkeypatch.setattr(jobs, "find", find)
    memo = {}
    raised = []
    for _ in range(3):
        with pytest.raises(HTTPException) as e:
            stages._find("tenant-t", "op-gone", memo)
        raised.append(e.value)
    assert calls == ["op-gone"]  # one read, remembered
    assert all(x.status_code == 404 and x.detail == "operation op-gone not found" for x in raised)
    assert len({id(x) for x in raised}) == 3  # a fresh exception each time
    assert memo["op-gone"] == ("404", (404, "operation op-gone not found"))  # no exception object in the memo
    depth = [
        sum(
            1
            for _ in iter(
                lambda tb=[x.__traceback__]: (tb.__setitem__(0, tb[0].tb_next) or tb[0]) if tb[0] else None, None
            )
        )
        for x in raised
    ]
    assert len(set(depth)) == 1  # the traceback does not grow across re-raises


def test_reconcile_scans_read_run_jobs_through_the_raw_listing(monkeypatch):
    """The three switches share _run_jobs: jobs._json_items on list_namespaced_job with the run label (raw response,
    managedFields dropped), no typed V1Job objects (999 of them per scan every 15 s exceeded the replicas, 2026-10-21)."""
    seen = []

    class Batch:
        def list_namespaced_job(self, ns, **kw):
            seen.append((ns, kw))
            return None

    def json_items(fn, *a, **kw):
        seen.append(("json_items", fn.__self__.__class__.__name__, a, kw))
        return [{"metadata": {"name": "op-1", "annotations": {}}, "spec": {}, "status": {}}]

    monkeypatch.setattr(reconciliation.kube, "batch", lambda: Batch())
    monkeypatch.setattr(jobs, "_json_items", json_items)
    assert reconciliation._run_jobs("tenant-t") == [
        {"metadata": {"name": "op-1", "annotations": {}}, "spec": {}, "status": {}}
    ]
    assert seen == [("json_items", "Batch", ("tenant-t",), {"label_selector": f"{reconciliation.cfg.LABEL}/mode=run"})]
