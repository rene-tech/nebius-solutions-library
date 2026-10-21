"""Work volumes are released on proof, never with the history (docs/JOBS.md "Work volume lifecycle"): the decision
table of the reclaim pass, its preconditions and records, the resume contract for a released volume, and the operator
diagnostics. Nothing here deletes an asset, a cache, a shared claim or anything a live Job references."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import app as api
import attempts
import config as cfg
import db
import jobs
import kube
import reconciliation as rec
from test_api import H, client, cluster, fake_keys, pod  # noqa: F401

NS = "tenant-demo"
L = cfg.LABEL


def job_(name, status="FAILED", mode="run", terminal=True, ann=None):
    conds = [] if not terminal else [{"type": "Complete" if status == "SUCCEEDED" else "Failed", "status": "True"}]
    return {
        "metadata": {
            "name": name,
            "namespace": NS,
            "labels": {f"{L}/mode": mode, f"{L}/tenant": "demo"},
            "annotations": dict(ann or {}),
        },
        "status": {"conditions": conds},
        "spec": {},
        "_status": status,
    }


def claim(name, op=None, uid="u1", rv="1", jobs_ann=None, owners=None, deleting=False):
    md = {
        "name": name,
        "namespace": NS,
        "uid": uid,
        "resourceVersion": rv,
        "labels": {f"{L}/operation": op or name[:-5]},
        "annotations": {},
    }
    if jobs_ann:
        md["annotations"][f"{L}/jobs"] = ",".join(jobs_ann)
    if owners:
        md["ownerReferences"] = [{"kind": "Job", "name": o, "uid": f"uid-{o}"} for o in owners]
    if deleting:
        md["deletionTimestamp"] = "2026-10-21T00:00:00Z"
    return {"metadata": md, "spec": {"resources": {"requests": {"storage": "8Gi"}}}, "status": {"phase": "Bound"}}


def row(op, status="failed", started=True, ended=None, export=None, ckpt=False):
    t = datetime.now(timezone.utc) - timedelta(hours=1)
    return {
        "operation": op,
        "root": op,
        "status": status,
        "started_at": t if started else None,
        "ended_at": ended or (t + timedelta(minutes=5) if started else None),
        "export_state": export,
        "checkpoint_ref": "s3://c" if ckpt else None,
        "result_ref": None,
    }


@pytest.fixture(autouse=True)
def _fresh_listing_caches():
    """The tenant listing caches are module state: a test here must not hand a warm (empty) listing to a later
    listing test in the same process."""
    jobs.ops_cache_invalidate(None)
    yield
    jobs.ops_cache_invalidate(None)
    jobs._fleet_ops_cache["until"] = 0.0


@pytest.fixture
def world(monkeypatch):
    """The decision function with the API's status projection stubbed per Job (`_status`) and a dict of Jobs/rows."""
    jobs_by, rows_by = {}, {}

    def find(ns, name):
        j = jobs_by.get(name)
        if j == "unreadable":
            raise HTTPException(502, "kubernetes: unreachable")
        if j is None:
            raise HTTPException(404, "not found")
        return j, "eu-north1"

    monkeypatch.setattr(jobs, "normalise", lambda j, p=None, r=None: {"status": j["_status"]})
    monkeypatch.setattr(cfg, "WORK_RETENTION_FAILED_S", 0)

    def decide(c, mounted=frozenset()):
        return rec.work_volume_decision(
            NS, c, set(mounted), find=find, records_of=lambda ops: {o: rows_by.get(o, []) for o in ops}
        )

    return jobs_by, rows_by, decide


def test_the_decision_table(world, monkeypatch):
    jobs_by, rows_by, decide = world
    # kept whatever the records say: mounted, releasing, a live Job, an unreadable Job, an unreadable ledger
    jobs_by["op-a"] = job_("op-a", "SUCCEEDED")
    assert decide(claim("op-a-work"), {"op-a-work"})[:2] == ("keep", "mounted")
    assert decide(claim("op-a-work", deleting=True))[:2] == ("keep", "releasing")
    jobs_by["op-b"] = job_("op-b", "RUNNING", terminal=False)
    assert decide(claim("op-b-work"))[:2] == ("keep", "active")
    jobs_by["op-u"] = "unreadable"
    assert decide(claim("op-u-work"))[0] == "keep" and "unknown" in decide(claim("op-u-work"))[1]
    bad = rec.work_volume_decision(
        NS, claim("op-a-work"), set(), find=lambda ns, n: (jobs_by["op-a"], "r"), records_of=lambda ops: 1 / 0
    )
    assert bad[0] == "keep" and "attempt records unavailable" in bad[1]
    # released on proof
    assert decide(claim("op-a-work"))[:2] == ("release", "delivered")
    jobs_by["op-s"] = job_("op-s", "SUPERSEDED", terminal=False, ann={f"{L}/successor": "op-s-r-1"})
    assert decide(claim("op-s-work"))[:2] == ("release", "superseded")
    jobs_by["op-c"] = job_("op-c", "FAILED", mode="async")
    rows_by["op-c"] = [row("op-c", "failed")]
    assert decide(claim("op-c-work"))[:2] == ("release", "call ended")
    jobs_by["op-n"] = job_("op-n", "CANCELLED")
    rows_by["op-n"] = []
    assert decide(claim("op-n-work"))[:2] == ("release", "never started")
    rows_by["op-n"] = [row("op-n", "started", started=False)]  # a record without a start is not a run
    assert decide(claim("op-n-work"))[:2] == ("release", "never started")
    # kept on proof
    jobs_by["op-e"] = job_("op-e", "EXPORT_FAILED")
    assert decide(claim("op-e-work"))[:2] == ("keep", "export pending")
    jobs_by["op-f"] = job_("op-f", "FAILED")
    rows_by["op-f"] = [row("op-f", "failed")]
    assert decide(claim("op-f-work"))[:2] == ("keep", "resume, no deadline")
    rows_by["op-f"] = [row("op-f", "failed", ckpt=True)]
    assert decide(claim("op-f-work"))[:2] == ("keep", "resume (durable pointer), no deadline")
    # the window: only when the operator set one, measured from the last attempt's end
    monkeypatch.setattr(cfg, "WORK_RETENTION_FAILED_S", 3600)
    assert decide(claim("op-f-work"))[:2] == ("keep", "resume (durable pointer)")
    rows_by["op-f"] = [row("op-f", "failed", ended=datetime.now(timezone.utc) - timedelta(hours=2))]
    assert decide(claim("op-f-work"))[:2] == ("release", "resume window expired")
    # a chain shares one claim: the root is delivered but a continuation still needs it -> kept
    jobs_by["op-r"], jobs_by["op-r-r1"] = (
        job_("op-r", "SUPERSEDED", ann={f"{L}/successor": "op-r-r1"}),
        job_("op-r-r1", "EXPORT_FAILED"),
    )
    assert decide(claim("op-r-work", jobs_ann=["op-r", "op-r-r1"]))[:2] == ("keep", "export pending")
    jobs_by["op-r-r1"] = job_("op-r-r1", "SUCCEEDED")
    assert decide(claim("op-r-work", owners=["op-r", "op-r-r1"]))[:2] == ("release", "delivered; superseded")


def test_a_gone_job_is_judged_by_its_durable_rows_alone(world, monkeypatch):
    jobs_by, rows_by, decide = world
    import admissions

    monkeypatch.setattr(attempts, "synthesize", lambda op, tenant: {"status": "FAILED"})
    monkeypatch.setattr(admissions, "get", lambda op: {"labels": {f"{L}/mode": "async"}})
    rows_by["op-g"] = [row("op-g", "failed")]
    assert decide(claim("op-g-work"))[:2] == ("release", "call ended")  # a call's claim whose Job expired
    monkeypatch.setattr(admissions, "get", lambda op: {"labels": {f"{L}/mode": "run"}})
    assert decide(claim("op-g-work"))[:2] == ("keep", "resume, no deadline")
    rows_by["op-g"] = []
    assert decide(claim("op-g-work"))[:2] == ("keep", "unknown: Job gone, no attempt record")


def test_the_pass_releases_with_preconditions_records_once_and_never_touches_other_claims(client, monkeypatch):
    c, cl = client
    monkeypatch.setattr(rec, "FLEET_MANAGER", True)
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1"])
    monkeypatch.setattr(kube, "core", lambda r=None: cl)
    monkeypatch.setattr(kube, "batch", lambda r=None: cl)
    monkeypatch.setattr(jobs, "tenant_namespaces", lambda: [NS])
    monkeypatch.setattr(jobs, "normalise", lambda j, p=None, r=None: {"status": j["_status"]})
    monkeypatch.setattr(db, "enabled", lambda: False)
    monkeypatch.setattr(cfg, "WORK_RETENTION_FAILED_S", 0)
    rows = {
        "op-00000000000000d1": [row("op-00000000000000d1", "succeeded", export="succeeded")],
        "op-00000000000000e2": [row("op-00000000000000e2", "failed")],
        "op-00000000000000f3": [row("op-00000000000000f3", "failed")],
    }
    monkeypatch.setattr(attempts, "for_operations", lambda ops: {o: rows.get(o, []) for o in ops})
    for name, st, mode in (
        ("op-00000000000000d1", "SUCCEEDED", "run"),
        ("op-00000000000000e2", "FAILED", "run"),
        ("op-00000000000000f3", "CANCELLED", "async"),
    ):
        cl.jobs[(NS, name)] = job_(name, st, mode)
    monkeypatch.setattr(
        jobs,
        "find",
        lambda ns, n: (cl.jobs[(ns, n)], "eu-north1")
        if (ns, n) in cl.jobs
        else (_ for _ in ()).throw(HTTPException(404, "x")),
    )
    cl.pvcs[(NS, "op-00000000000000d1-work")] = claim("op-00000000000000d1-work", uid="u-done", rv="10")
    cl.pvcs[(NS, "op-00000000000000e2-work")] = claim("op-00000000000000e2-work", uid="u-kept", rv="11")
    cl.pvcs[(NS, "op-00000000000000f3-work")] = claim("op-00000000000000f3-work", uid="u-raced", rv="12")
    cl.pvcs[(NS, "asset-af3-params")] = {
        "metadata": {
            "name": "asset-af3-params",
            "namespace": NS,
            "uid": "u-a",
            "resourceVersion": "1",
            "labels": {f"{L}/operation": "op-x"},
        }
    }
    cl.pvcs[(NS, "scratch-shared")] = {
        "metadata": {"name": "scratch-shared", "namespace": NS, "uid": "u-s", "resourceVersion": "1", "labels": {}}
    }
    # the raced claim changes between the read and the delete (a resume took it): the precondition refuses, nothing recorded
    real_delete = cl.delete_namespaced_persistent_volume_claim

    def racing_delete(name, ns, body=None):
        if name == "op-00000000000000f3-work":
            cl.pvcs[(ns, name)]["metadata"]["resourceVersion"] = "13"
        return real_delete(name, ns, body)

    cl.delete_namespaced_persistent_volume_claim = racing_delete
    rec._reclaim["at"] = 0.0
    assert rec.reclaim_work_volumes() == ["eu-north1/tenant-demo/op-00000000000000d1-work"]
    assert cl.pvc_deleted == [(NS, "op-00000000000000d1-work", "u-done", "10")]
    assert (
        (NS, "op-00000000000000e2-work") in cl.pvcs
        and (NS, "op-00000000000000f3-work") in cl.pvcs
        and (NS, "asset-af3-params") in cl.pvcs
        and (NS, "scratch-shared") in cl.pvcs
    )
    marks = {
        n: (cl.jobs[(NS, n)]["metadata"]["annotations"].get(f"{L}/work-volume") or "")
        for n in ("op-00000000000000d1", "op-00000000000000e2", "op-00000000000000f3")
    }
    assert (
        marks["op-00000000000000d1"].startswith("released|delivered|")
        and marks["op-00000000000000e2"].startswith("retained|resume, no deadline|")
        and marks["op-00000000000000f3"] == ""
    )
    # the period gate: nothing happens again before WORK_RECLAIM_EVERY_S; after it the kept mark is not rewritten
    assert rec.reclaim_work_volumes() == []
    before = marks["op-00000000000000e2"]
    rec._reclaim["at"] = 0.0
    cl.delete_namespaced_persistent_volume_claim = real_delete
    assert rec.reclaim_work_volumes() == ["eu-north1/tenant-demo/op-00000000000000f3-work"]
    assert cl.jobs[(NS, "op-00000000000000e2")]["metadata"]["annotations"][f"{L}/work-volume"] == before
    assert rec.work_volume_mark(cl.jobs[(NS, "op-00000000000000d1")]) == {
        "state": "released",
        "reason": "delivered",
        "at": marks["op-00000000000000d1"].rsplit("|", 1)[1],
    }


def test_a_resume_after_release_needs_an_explicit_restart_from_inputs(client):
    """The documented contract of an expired Job applies to a released volume while the Job remains: 409 with the
    reason and the way out; `restart_from_inputs: true` renders the run again on a fresh volume (same owner, key,
    lineage), `work_volume` on the handle says released, the fleet view carries the record."""
    c, fake = client
    r = c.post("/v1/models/batch-example:invoke", json={"input": {"input_prefix": "s3://b/in", "nsteps": 7}}, headers=H)
    oid = r.json()["id"]
    job = fake.jobs[(NS, oid)]
    job["status"] = {"startTime": "2026-10-05T20:00:30Z", "conditions": [{"type": "Failed", "status": "True"}]}
    job["metadata"]["annotations"][f"{L}/work-volume"] = "released|never started|2026-10-21T08:00:00Z"
    del fake.pvcs[(NS, f"{oid}-work")]
    r = c.post(f"/v1/operations/{oid}:resume", headers=H, json={})
    assert (
        r.status_code == 409 and "restart_from_inputs" in r.json()["detail"] and "never started" in r.json()["detail"]
    ), r.text
    assert c.get(f"/v1/operations/{oid}", headers=H).json()["work_volume"] == "released"
    r = c.post(f"/v1/operations/{oid}:resume", headers=H, json={"restart_from_inputs": True})
    assert r.status_code == 202, r.text
    new = fake.jobs[(NS, f"{oid}-r1")]
    assert (
        new["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == f"{oid}-r1-work"
    )  # fresh
    assert new["metadata"]["annotations"][f"{L}/resumed-from"] == oid and (NS, f"{oid}-r1-work") in fake.pvcs
    view = api._fleet_operation_view({**job, "_region": "eu-north1"})
    assert (
        view["storage"]["state"] == "released"
        and view["storage"]["reason"] == "never started"
        and view["storage"]["claim"] == f"{oid}-work"
    )
    assert api._fleet_operation_view({**new, "_region": "eu-north1"})["storage"]["state"] is None
