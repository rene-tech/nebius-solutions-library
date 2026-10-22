"""Work volumes are released on proof, never with the history (docs/JOBS.md "Work volume lifecycle"): the decision
table of the reclaim pass, its preconditions and records, the resume contract for a released volume, and the operator
diagnostics. Nothing here deletes an asset, a cache, a shared claim or anything a live Job references."""

import json
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


def row(op, status="failed", started=True, ended=None, export=None, ckpt=False, exit_code=None, role="run"):
    t = datetime.now(timezone.utc) - timedelta(hours=1)
    return {
        "exit_code": exit_code,
        "role": role,
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
    import store

    store.reset()
    yield
    store.reset()
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

    def decide(c, mounted=frozenset(), related=None):
        return rec.work_volume_decision(
            NS, c, set(mounted), find=find, records_of=lambda ops: {o: rows_by.get(o, []) for o in ops}, related=related
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
    # a run that ran and ended: scratch, released (what a resume needs is in the customer's storage); only an
    # unresolved export keeps it
    jobs_by["op-f"] = job_("op-f", "FAILED")
    rows_by["op-f"] = [row("op-f", "failed")]
    assert decide(claim("op-f-work"))[:2] == ("release", "ended")
    rows_by["op-f"] = [row("op-f", "failed", ckpt=True)]
    assert decide(claim("op-f-work"))[:2] == ("release", "ended")
    rows_by["op-f"] = [row("op-f", "failed", export="failed")]
    assert decide(claim("op-f-work"))[:2] == ("keep", "export pending")
    # a chain shares one claim: the root is delivered but a continuation still needs it -> kept
    jobs_by["op-r"], jobs_by["op-r-r1"] = (
        job_("op-r", "SUPERSEDED", ann={f"{L}/successor": "op-r-r1"}),
        job_("op-r-r1", "EXPORT_FAILED"),
    )
    assert decide(claim("op-r-work", jobs_ann=["op-r", "op-r-r1"]))[:2] == ("keep", "export pending")
    jobs_by["op-r-r1"] = job_("op-r-r1", "SUCCEEDED")
    assert decide(claim("op-r-work", owners=["op-r", "op-r-r1"]))[:2] == ("release", "delivered; superseded")


def test_an_undelivered_capture_is_kept_for_the_export_retry_whatever_the_status(world, monkeypatch):
    """Root's review of 165: a FAILED endpoint call whose exit 2/5 capture was not delivered (the last attempt record
    says export failed) is retried transfer-only by `:export` from this very volume, and status.normalise does not
    call that EXPORT_FAILED. The rows decide: kept as export pending, whatever the mode, whatever the window; a
    delivered or export-less failure of a call is still released."""
    jobs_by, rows_by, decide = world
    jobs_by["op-c"] = job_("op-c", "FAILED", mode="async")
    rows_by["op-c"] = [row("op-c", "failed", export="failed")]
    assert decide(claim("op-c-work"))[:2] == ("keep", "export pending")
    rows_by["op-c"] = [row("op-c", "failed", export="failed"), row("op-c", "failed", export="succeeded")]
    assert decide(claim("op-c-work"))[:2] == ("release", "call ended")  # the newest record is the delivery
    rows_by["op-c"] = [row("op-c", "interrupted", export=None)]
    assert decide(claim("op-c-work"))[:2] == ("release", "call ended")
    # a run whose export failed after a failure is kept; delivered, it is scratch and goes
    jobs_by["op-w"] = job_("op-w", "FAILED")
    rows_by["op-w"] = [row("op-w", "failed", ended=datetime.now(timezone.utc) - timedelta(hours=9), export="failed")]
    assert decide(claim("op-w-work"))[:2] == ("keep", "export pending")
    rows_by["op-w"] = [row("op-w", "failed", ended=datetime.now(timezone.utc) - timedelta(hours=9), export="succeeded")]
    assert decide(claim("op-w-work"))[:2] == ("release", "ended")


def test_a_gone_job_is_judged_by_its_durable_rows_alone(world, monkeypatch):
    jobs_by, rows_by, decide = world
    import admissions

    monkeypatch.setattr(attempts, "synthesize", lambda op, tenant: {"status": "FAILED"})
    monkeypatch.setattr(
        admissions,
        "get",
        lambda op: {
            "operation": op,
            "labels": {f"{L}/mode": "async", f"{L}/tenant": "demo"},
            "annotations": {},
            "timeout_s": None,
            "managed_by": None,
        },
    )
    rows_by["op-g"] = [row("op-g", "failed")]
    assert decide(claim("op-g-work"))[:2] == ("release", "call ended")  # a call's claim whose Job expired
    monkeypatch.setattr(
        admissions,
        "get",
        lambda op: {
            "operation": op,
            "labels": {f"{L}/mode": "run", f"{L}/tenant": "demo"},
            "annotations": {},
            "timeout_s": None,
            "managed_by": None,
        },
    )
    assert decide(claim("op-g-work"))[:2] == ("release", "ended")
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
    rows = {
        "op-00000000000000d1": [row("op-00000000000000d1", "succeeded", export="succeeded")],
        "op-00000000000000e2": [row("op-00000000000000e2", "failed", export="failed")],
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
        and marks["op-00000000000000e2"].startswith("retained|export pending|")
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


def test_a_resume_after_a_lifecycle_release_restarts_on_a_fresh_volume_by_itself(client):
    """The disk was scratch (released|ended): `:resume` renders the run again on a fresh volume from its inputs (or the
    chain's acknowledged checkpoint pointer) without any flag, same owner, key and lineage; `work_volume` on the
    handle says released and the fleet view carries the record. A volume gone WITHOUT the lifecycle's mark (an
    unexpected loss) still needs the explicit restart_from_inputs, so nothing is dropped unnoticed."""
    c, fake = client
    r = c.post("/v1/models/batch-example:invoke", json={"input": {"input_prefix": "s3://b/in", "nsteps": 7}}, headers=H)
    oid = r.json()["id"]
    job = fake.jobs[(NS, oid)]
    job["status"] = {"startTime": "2026-10-05T20:00:30Z", "conditions": [{"type": "Failed", "status": "True"}]}
    del fake.pvcs[(NS, f"{oid}-work")]
    r = c.post(f"/v1/operations/{oid}:resume", headers=H, json={})
    assert r.status_code == 409 and "restart_from_inputs" in r.json()["detail"], r.text  # gone, not released
    job["metadata"]["annotations"][f"{L}/work-volume"] = "released|ended|2026-10-21T10:00:00Z"
    assert c.get(f"/v1/operations/{oid}", headers=H).json()["work_volume"] == "released"
    r = c.post(f"/v1/operations/{oid}:resume", headers=H, json={})
    assert r.status_code == 202, r.text
    new = fake.jobs[(NS, f"{oid}-r1")]
    assert (
        new["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == f"{oid}-r1-work"
    )  # fresh
    assert new["metadata"]["annotations"][f"{L}/resumed-from"] == oid and (NS, f"{oid}-r1-work") in fake.pvcs
    assert new["metadata"]["annotations"][f"{L}/restarted-from-inputs"] == oid
    view = api._fleet_operation_view({**job, "_region": "eu-north1"})
    assert (
        view["storage"]["state"] == "released"
        and view["storage"]["reason"] == "ended"
        and view["storage"]["claim"] == f"{oid}-work"
    )
    assert api._fleet_operation_view({**new, "_region": "eu-north1"})["storage"]["state"] is None


def test_a_queued_same_volume_successor_known_only_to_the_listing_keeps_the_claim(world):
    """Root's review of 165: a same-volume fleet resume creates a queued manager Job the dispatcher has not recorded
    on the worker's claim yet, and the original is marked superseded. The tenant listing names that successor on the
    claim (`related`): it is live, the claim stays; the record the API now writes at create (jobs._record_claim_user)
    is the second guard."""
    jobs_by, rows_by, decide = world
    jobs_by["op-s"] = job_("op-s", "SUPERSEDED", ann={f"{L}/successor": "op-s-r1"})
    jobs_by["op-s-r1"] = job_("op-s-r1", "QUEUED", terminal=False)
    assert decide(claim("op-s-work"))[:2] == ("release", "superseded")  # what 165 would have done
    assert decide(claim("op-s-work"), related=["op-s-r1"])[:2] == ("keep", "active")
    assert decide(claim("op-s-work", jobs_ann=["op-s", "op-s-r1"]))[:2] == ("keep", "active")  # the record at create


def test_terminal_pods_are_not_mounts_and_the_listing_feeds_the_pass(client, monkeypatch):
    c, cl = client
    monkeypatch.setattr(rec, "FLEET_MANAGER", True)
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1"])
    monkeypatch.setattr(kube, "core", lambda r=None: cl)
    monkeypatch.setattr(kube, "batch", lambda r=None: cl)
    monkeypatch.setattr(jobs, "tenant_namespaces", lambda: [NS])
    monkeypatch.setattr(jobs, "normalise", lambda j, p=None, r=None: {"status": j["_status"]})
    monkeypatch.setattr(db, "enabled", lambda: False)
    done, live, succ = "op-00000000000000a1", "op-00000000000000b2", "op-00000000000000c3"
    rows = {
        done: [row(done, "succeeded", export="succeeded")],
        live: [row(live, "succeeded", export="succeeded")],
        succ: [],
    }
    monkeypatch.setattr(attempts, "for_operations", lambda ops: {o: rows.get(o, []) for o in ops})
    cl.jobs[(NS, done)] = job_(done, "SUCCEEDED")
    cl.jobs[(NS, live)] = job_(live, "SUCCEEDED")
    queued = job_(f"{succ}-r1", "QUEUED", terminal=False, ann={f"{L}/pvc": f"{succ}-work", f"{L}/resumed-from": succ})
    cl.jobs[(NS, succ)] = job_(succ, "SUPERSEDED", ann={f"{L}/successor": f"{succ}-r1"})
    cl.jobs[(NS, f"{succ}-r1")] = queued
    monkeypatch.setattr(
        jobs,
        "find",
        lambda ns, n: (cl.jobs[(ns, n)], "eu-north1")
        if (ns, n) in cl.jobs
        else (_ for _ in ()).throw(HTTPException(404, "x")),
    )
    for n in (done, live, succ):
        cl.pvcs[(NS, f"{n}-work")] = claim(f"{n}-work", uid=f"u-{n}", rv="1")
    # a terminal pod still naming the delivered claim is no mount; a running pod naming the other claim is
    cl.pods += [
        {
            "metadata": {"name": "p-done", "namespace": NS, "labels": {}},
            "spec": {"volumes": [{"name": "work", "persistentVolumeClaim": {"claimName": f"{done}-work"}}]},
            "status": {"phase": "Failed"},
        },
        {
            "metadata": {"name": "p-live", "namespace": NS, "labels": {}},
            "spec": {"volumes": [{"name": "work", "persistentVolumeClaim": {"claimName": f"{live}-work"}}]},
            "status": {"phase": "Running"},
        },
    ]
    rec._reclaim["at"] = 0.0
    assert rec.reclaim_work_volumes() == [f"eu-north1/{NS}/{done}-work"]
    assert (NS, f"{live}-work") in cl.pvcs and (
        NS,
        f"{succ}-work",
    ) in cl.pvcs  # mounted; queued successor from the listing


def test_a_fleet_resume_records_itself_on_the_workers_claim_before_its_job_exists(client, monkeypatch):
    c, cl = client
    monkeypatch.setattr(kube, "core", lambda r=None: cl)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1")
    cl.pvcs[(NS, "op-1-work")] = claim("op-1-work", uid="u1", rv="5")
    cl.pvcs[(NS, "op-1-work")]["metadata"]["annotations"][f"{L}/jobs"] = "op-1"
    j = {
        "metadata": {"name": "op-1-r1", "annotations": {f"{L}/pvc": "op-1-work", f"{L}/resumed-from": "op-1"}},
        "spec": {
            "managedBy": "kueue.x-k8s.io/multikueue",
            "template": {"metadata": {"annotations": {f"{L}/regions": "north"}}, "spec": {}},
        },
    }
    jobs._record_claim_user(NS, j, "op-1-work")
    assert cl.pvcs[(NS, "op-1-work")]["metadata"]["annotations"][f"{L}/jobs"] == "op-1,op-1-r1"
    jobs._record_claim_user(NS, j, "op-1-work")  # idempotent
    assert cl.pvcs[(NS, "op-1-work")]["metadata"]["annotations"][f"{L}/jobs"] == "op-1,op-1-r1"
    with pytest.raises(HTTPException) as e:  # a released claim: refused before any Job is created
        jobs._record_claim_user(NS, j, "op-gone-work")
    assert e.value.status_code == 409 and "restart_from_inputs" in e.value.detail
    cl.pvcs[(NS, "op-1-work")]["metadata"]["deletionTimestamp"] = "2026-10-21T09:00:00Z"
    with pytest.raises(HTTPException) as e:  # a claim being released: refused too
        jobs._record_claim_user(NS, j, "op-1-work")
    assert e.value.status_code == 409 and "released" in e.value.detail


def _pass_world(cl, monkeypatch, rows):
    monkeypatch.setattr(rec, "FLEET_MANAGER", True)
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1"])
    monkeypatch.setattr(kube, "core", lambda r=None: cl)
    monkeypatch.setattr(kube, "batch", lambda r=None: cl)
    monkeypatch.setattr(jobs, "tenant_namespaces", lambda: [NS])
    monkeypatch.setattr(jobs, "normalise", lambda j, p=None, r=None: {"status": j["_status"]})
    monkeypatch.setattr(attempts, "for_operations", lambda ops: {o: rows.get(o, []) for o in ops})
    monkeypatch.setattr(
        jobs,
        "find",
        lambda ns, n: (cl.jobs[(ns, n)], "eu-north1")
        if (ns, n) in cl.jobs
        else (_ for _ in ()).throw(HTTPException(404, "x")),
    )
    rec._reclaim["at"] = 0.0


def test_the_delete_rests_on_a_fresh_complete_reference_proof_under_the_lock(client, monkeypatch):
    """Root's review of 322ebd9: a cached or partial listing may miss a queued same-volume successor. The pass lists
    the tenant's Jobs fresh in every cluster (fail closed when one cannot be read) and, under the lineage's operation
    lock, lists them again before the delete: a successor created between the pass's index and the lock keeps the
    claim; a continuation holds the same lock (app.continue_run), so the two never interleave."""
    c, cl = client
    import db
    import keys

    done = "op-00000000000000d1"
    rows = {done: [row(done, "succeeded", export="succeeded")]}
    _pass_world(cl, monkeypatch, rows)
    monkeypatch.setattr(db, "enabled", lambda: True)
    cl.jobs[(NS, done)] = job_(done, "SUPERSEDED", ann={f"{L}/successor": f"{done}-r1"})
    cl.pvcs[(NS, f"{done}-work")] = claim(f"{done}-work", uid="u-d1", rv="10")
    from contextlib import contextmanager

    locked = []

    @contextmanager
    def lock(name):
        locked.append(name)
        # a same-volume resume lands while the lock is taken: its Job names the claim, nothing recorded on the claim
        cl.jobs[(NS, f"{done}-r1")] = job_(f"{done}-r1", "QUEUED", terminal=False, ann={f"{L}/pvc": f"{done}-work"})
        yield

    monkeypatch.setattr(keys, "operation_lock", lock)
    assert rec.reclaim_work_volumes() == []  # the fresh listing under the lock found the successor
    assert locked == [done] and (NS, f"{done}-work") in cl.pvcs
    assert f"{L}/work-volume" not in cl.jobs[(NS, done)]["metadata"]["annotations"]  # nothing recorded
    # the pass index itself fails closed: a region whose Jobs cannot be listed releases nothing in the namespace
    del cl.jobs[(NS, f"{done}-r1")]
    real_list = cl.list_namespaced_job
    cl.list_namespaced_job = lambda ns, label_selector="": (_ for _ in ()).throw(RuntimeError("region unreachable"))
    rec._reclaim["at"] = 0.0
    assert rec.reclaim_work_volumes() == [] and (NS, f"{done}-work") in cl.pvcs
    cl.list_namespaced_job = real_list
    monkeypatch.setattr(keys, "operation_lock", lambda name: __import__("contextlib").nullcontext())
    rec._reclaim["at"] = 0.0
    assert rec.reclaim_work_volumes() == [f"eu-north1/{NS}/{done}-work"]  # complete proof: released


def test_releases_per_pass_are_capped(client, monkeypatch):
    c, cl = client
    import db

    a, b = "op-00000000000000a1", "op-00000000000000b2"
    rows = {a: [row(a, "succeeded", export="succeeded")], b: [row(b, "succeeded", export="succeeded")]}
    _pass_world(cl, monkeypatch, rows)
    monkeypatch.setattr(db, "enabled", lambda: False)
    monkeypatch.setattr(cfg, "WORK_RECLAIM_MAX_PER_PASS", 1)
    for n in (a, b):
        cl.jobs[(NS, n)] = job_(n, "SUCCEEDED")
        cl.pvcs[(NS, f"{n}-work")] = claim(f"{n}-work", uid=f"u-{n}", rv="1")
    assert len(rec.reclaim_work_volumes()) == 1
    rec._reclaim["at"] = 0.0
    assert len(rec.reclaim_work_volumes()) == 1 and not [k for k in cl.pvcs if k[1].endswith("-work")]


def test_a_continuation_runs_under_the_lineage_lock(monkeypatch):
    """app.continue_run holds keys.operation_lock(root) around the whole builder (admission, existence checks, the
    claim record, the create): the same lock the reclaim pass takes before its re-read and delete, so a release and
    a continuation never interleave (a release that won answers the builder 409: test_a_resume_after_release…)."""
    from contextlib import contextmanager

    import db
    import keys

    seen = []

    @contextmanager
    def lock(name):
        seen.append(("enter", name))
        yield
        seen.append(("exit", name))

    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(keys, "operation_lock", lock)
    monkeypatch.setattr(keys, "member_of", lambda root: None)
    monkeypatch.setattr(keys, "run_head", lambda root: None)
    out = api.continue_run("op-root", lambda admit: (seen.append(("build", admit)), ("job", True))[1], ns=NS)
    assert out == ("job", True)
    assert seen == [("enter", "op-root"), ("build", None), ("exit", "op-root")]  # built inside the lock


def test_only_a_reused_claim_is_recorded_a_fresh_successor_volume_is_not_looked_up(client, monkeypatch):
    """Root's delta check: a portable checkpoint restore or a restart from inputs renders the successor on its own
    fresh volume `<successor>-work`, which the dispatcher creates once the run is placed: create() must not look it
    up (a strict 404 would refuse every portable restore); a REUSED claim (the original's) is recorded and, when
    gone, refused."""
    c, cl = client
    monkeypatch.setattr(kube, "core", lambda r=None: cl)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1")
    looked = []
    real_read = cl.read_namespaced_persistent_volume_claim

    def read(name, ns):
        looked.append(name)
        return real_read(name, ns)

    cl.read_namespaced_persistent_volume_claim = read
    base = {
        "spec": {
            "managedBy": "kueue.x-k8s.io/multikueue",
            "template": {"metadata": {"annotations": {f"{L}/regions": "north"}}, "spec": {}},
        }
    }
    fresh = {
        "metadata": {"name": "op-1-r2", "annotations": {f"{L}/pvc": "op-1-r2-work", f"{L}/resumed-from": "op-1"}},
        **base,
    }
    reused = {
        "metadata": {"name": "op-1-r3", "annotations": {f"{L}/pvc": "op-1-work", f"{L}/resumed-from": "op-1"}},
        **base,
    }
    ann = lambda j: j["metadata"]["annotations"]  # noqa: E731

    # the same guard create() applies, evaluated here on the two shapes
    def guarded(j):
        a = ann(j)
        if (
            jobs.is_manager_job(j)
            and a.get(f"{L}/resumed-from")
            and a.get(f"{L}/pvc")
            and a[f"{L}/pvc"] != f"{j['metadata']['name']}-work"
        ):
            jobs._record_claim_user(NS, j, a[f"{L}/pvc"])

    guarded(fresh)
    assert looked == []  # the fresh volume is never looked up
    with pytest.raises(HTTPException) as e:
        guarded(reused)  # the original's claim is gone: refused
    assert e.value.status_code == 409 and looked == ["op-1-work"]


def test_a_deleting_claim_is_absent_for_the_pointer_probe_so_the_checkpoint_pointer_survives(client, monkeypatch):
    """Root's review of 0.11.168: the first probe in jobs.resume took a claim with a deletionTimestamp as present, so a
    released-but-terminating volume skipped the acknowledged checkpoint pointer and the run later restarted from its
    inputs. Deleting is absent for that probe too: the pointer stands in, CHECKPOINT_REF reaches the fresh volume."""
    import catalog

    c, fake = client
    r = c.post("/v1/models/batch-example:invoke", json={"input": {"input_prefix": "s3://b/in", "nsteps": 7}}, headers=H)
    oid = r.json()["id"]
    job = fake.jobs[(NS, oid)]
    job["status"] = {"startTime": "2026-10-05T20:00:30Z", "conditions": [{"type": "Failed", "status": "True"}]}
    job["metadata"]["annotations"][f"{L}/work-volume"] = "released|ended|2026-10-21T10:00:00Z"
    fake.pvcs[(NS, f"{oid}-work")]["metadata"]["deletionTimestamp"] = "2026-10-21T10:00:01Z"  # pvc-protection holds it
    m = catalog.get("batch-example")
    new, created = jobs.resume(
        NS, oid, "eu-north1", m, catalog.region_params(m, "eu-north1"), pointer="s3://b/ckpt/gen-3"
    )
    assert created and new["metadata"]["name"] == f"{oid}-r1"
    ex = json.loads(new["metadata"]["annotations"][f"{L}/execution"])
    assert ex["recovery"]["checkpoint_ref"] == "s3://b/ckpt/gen-3"  # the pointer stood in, not a restart from inputs
    assert f"{L}/restarted-from-inputs" not in new["metadata"]["annotations"]
    main = next(x for x in new["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    assert {e["name"]: e.get("value") for e in main["env"]}["CHECKPOINT_REF"] == "s3://b/ckpt/gen-3"
    assert (
        new["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == f"{oid}-r1-work"
    )  # fresh
    assert not jobs.work_claim_present(fake, f"{oid}-work", NS) and not jobs.work_claim_present(
        fake, "op-none-work", NS
    )


def test_a_declared_finalizer_keeps_the_scratch_until_it_delivered(world, monkeypatch):
    """Root's App review of 168: a run whose main exits with a code the catalog declares in `job.finalize.on_exit_codes`
    finished its compute and left the delivery to the CPU finalizer on the SAME scratch (RESULT.json says nothing,
    the uploader records export n/a). Judged from the catalog contract and the lineage's rows, never a constant: kept
    as "finalizer pending" after the initial delivery failure and after a failed finalizer; released once a finalizer
    delivered; an undeclared failure code is an ended run (scratch released)."""
    import catalog

    jobs_by, rows_by, decide = world
    monkeypatch.setattr(
        catalog,
        "get",
        lambda name: {"job": {"finalize": {"onExitCodes": [75]}}}
        if name == "md"
        else {"job": {}}
        if name == "plain"
        else None,
    )
    jobs_by["op-p"] = job_("op-p", "FAILED")
    jobs_by["op-p"]["metadata"]["labels"][f"{L}/model"] = "md"
    rows_by["op-p"] = [{**row("op-p", "failed", exit_code=75, export="n/a"), "revision": 10}]
    assert decide(claim("op-p-work"))[:2] == ("keep", "finalizer pending")
    rows_by["op-p"] = [
        {**row("op-p", "failed", exit_code=75, export="n/a"), "revision": 10},
        {**row("op-p-x1", "failed", exit_code=1, export="failed", role="finalize"), "root": "op-p", "revision": 20},
    ]
    assert decide(claim("op-p-work"))[:2] == ("keep", "finalizer pending")  # a failed finalizer: the next :export
    rows_by["op-p"] = [
        {**row("op-p", "failed", exit_code=75, export="n/a"), "revision": 10},
        {
            **row("op-p-x1", "succeeded", exit_code=0, export="succeeded", role="finalize"),
            "root": "op-p",
            "revision": 20,
        },
    ]
    jobs_by["op-p-x1"] = job_("op-p-x1", "SUCCEEDED", ann={f"{L}/export-of": "op-p", f"{L}/pvc": "op-p-work"})
    action, why, _ = decide(claim("op-p-work", jobs_ann=["op-p", "op-p-x1"]))
    assert action == "release" and "delivered by finalizer" in why
    rows_by["op-p"] = [row("op-p", "failed", exit_code=1, export="n/a")]  # compute failed: nothing to finalize
    assert decide(claim("op-p-work"))[:2] == ("release", "ended")
    jobs_by["op-q"] = job_("op-q", "FAILED")  # a definition without a finalizer: 75 is just a failure
    jobs_by["op-q"]["metadata"]["labels"][f"{L}/model"] = "plain"
    rows_by["op-q"] = [row("op-q", "failed", exit_code=75, export="n/a")]
    assert decide(claim("op-q-work"))[:2] == ("release", "ended")


def test_only_this_operations_later_finalizer_authorizes_the_release(world, monkeypatch):
    """Root's reproduction on 5498cd990: the current run op-a-r1 (epoch 3) exits its declared 75 with export n/a; the
    lineage holds an older succeeded finalizer op-a-x1 (epoch 2) that delivered the predecessor. It must not release
    the current run's undelivered output: only a finalizer LATER than this compute attempt (row order) whose Job
    names this operation (`export-of`) counts; a later finalizer for another operation does not."""
    import catalog

    jobs_by, rows_by, decide = world
    monkeypatch.setattr(
        catalog, "get", lambda name: {"job": {"finalize": {"onExitCodes": [75]}}} if name == "md" else None
    )
    jobs_by["op-a-r1"] = job_("op-a-r1", "FAILED", ann={f"{L}/pvc": "op-a-r1-work", f"{L}/resumed-from": "op-a"})
    jobs_by["op-a-r1"]["metadata"]["labels"][f"{L}/model"] = "md"
    older = {
        **row("op-a-x1", "succeeded", exit_code=0, export="succeeded", role="finalize"),
        "root": "op-a",
        "revision": 20,
        "epoch": 2,
    }
    current = {**row("op-a-r1", "failed", exit_code=75, export="n/a"), "root": "op-a", "revision": 30, "epoch": 3}
    rows_by["op-a-r1"] = [older, current]
    assert decide(claim("op-a-r1-work", op="op-a-r1"))[:2] == ("keep", "finalizer pending")
    # a later finalizer whose Job names another operation: still pending
    jobs_by["op-a-x2"] = job_("op-a-x2", "SUCCEEDED", ann={f"{L}/export-of": "op-a", f"{L}/pvc": "op-a-r1-work"})
    other = {
        **row("op-a-x2", "succeeded", exit_code=0, export="succeeded", role="finalize"),
        "root": "op-a",
        "revision": 40,
        "epoch": 4,
    }
    rows_by["op-a-r1"] = [older, current, other]
    assert decide(claim("op-a-r1-work", op="op-a-r1", jobs_ann=["op-a-r1", "op-a-x2"]))[:2] == (
        "keep",
        "finalizer pending",
    )
    # the finalizer of THIS operation, later than its compute attempt: delivered
    jobs_by["op-a-x2"]["metadata"]["annotations"][f"{L}/export-of"] = "op-a-r1"
    action, why, _ = decide(claim("op-a-r1-work", op="op-a-r1", jobs_ann=["op-a-r1", "op-a-x2"]))
    assert action == "release" and "delivered by finalizer" in why  # both referencing operations agree


def test_absent_identity_is_not_delivery_proof_and_codes_come_from_the_pinned_definition(world, monkeypatch):
    """Root's 170 follow-up. (1) A later succeeded finalizer whose Job is not among the claim's references (it belongs
    to another claim, or is unreadable) is no proof: positive `export-of == this operation` is required. (2) The
    finalizer codes are the operation's declared version's (catalog.pinned, `model-version`), never a newer
    definition's: removing the finalizer later keeps a delivery-pending run pending; a version that cannot be found,
    or a definition that is gone, is unknown (kept)."""
    import catalog

    jobs_by, rows_by, decide = world
    defs = {"md": {"job": {"finalize": {"onExitCodes": [75]}}, "version": 7, "managed_by": "api"}}
    monkeypatch.setattr(catalog, "get", lambda name: defs.get(name))
    jobs_by["op-b-r1"] = job_("op-b-r1", "FAILED", ann={f"{L}/pvc": "op-b-r1-work", f"{L}/resumed-from": "op-b"})
    jobs_by["op-b-r1"]["metadata"]["labels"][f"{L}/model"] = "md"
    current = {**row("op-b-r1", "failed", exit_code=75, export="n/a"), "root": "op-b", "revision": 30}
    later = {
        **row("op-b-x9", "succeeded", exit_code=0, export="succeeded", role="finalize"),
        "root": "op-b",
        "revision": 40,
    }
    rows_by["op-b-r1"] = [current, later]
    # op-b-x9's Job is not a reference of this claim (another claim's finalizer): no identity, no release
    assert decide(claim("op-b-r1-work", op="op-b-r1"))[:2] == ("keep", "finalizer pending")
    # the pinned definition: the operation was rendered from version 7 (finalize 75); the catalog moved to 8 without it
    jobs_by["op-b-r1"]["metadata"]["annotations"][f"{L}/model-version"] = "7"
    defs["md"] = {"job": {}, "version": 8, "managed_by": "api"}
    monkeypatch.setattr(
        catalog,
        "pinned",
        lambda m, ann: {"job": {"finalize": {"onExitCodes": [75]}}, "version": 7}
        if ann.get(f"{L}/model-version") == "7"
        else m,
    )
    assert decide(claim("op-b-r1-work", op="op-b-r1"))[:2] == ("keep", "finalizer pending")
    monkeypatch.setattr(catalog, "pinned", lambda m, ann: m)  # history missing: pinned cannot find version 7
    assert decide(claim("op-b-r1-work", op="op-b-r1"))[:2] == ("keep", "unknown: declared version 7 missing")
    defs.pop("md")  # the definition itself is gone
    assert decide(claim("op-b-r1-work", op="op-b-r1"))[:2] == ("keep", "unknown: definition missing")


def test_a_gone_job_keeps_the_declared_finalizer_contract_through_its_admission_record(world, monkeypatch):
    """Root's last 171 branch: the Job expired (TTL) but the scratch and the records remain; a failed run with a
    declared delivery-pending exit code (export n/a) must not become "ended" because the Kubernetes object is gone.
    The admission record (admissions.as_job) stands in for the Job in that decision; without it the volume is unknown."""
    import admissions
    import catalog

    jobs_by, rows_by, decide = world
    monkeypatch.setattr(attempts, "synthesize", lambda op, tenant: {"status": "FAILED"})
    monkeypatch.setattr(
        catalog,
        "get",
        lambda name: {"job": {"finalize": {"onExitCodes": [75]}}, "version": 3, "managed_by": "api"}
        if name == "md"
        else None,
    )
    rec = {
        "operation": "op-g2",
        "labels": {f"{L}/mode": "run", f"{L}/model": "md", f"{L}/tenant": "demo"},
        "annotations": {f"{L}/model-version": "3", f"{L}/pvc": "op-g2-work"},
        "timeout_s": 3600,
        "managed_by": None,
    }
    monkeypatch.setattr(admissions, "get", lambda op: rec if op == "op-g2" else None)
    rows_by["op-g2"] = [{**row("op-g2", "failed", exit_code=75, export="n/a"), "revision": 10}]
    assert decide(claim("op-g2-work"))[:2] == ("keep", "finalizer pending")  # the contract outlives the Job
    rows_by["op-g2"] = [{**row("op-g2", "failed", exit_code=1, export="n/a"), "revision": 10}]
    assert decide(claim("op-g2-work"))[:2] == ("release", "ended")  # an undeclared failure of a gone Job
    monkeypatch.setattr(admissions, "get", lambda op: None)
    rows_by["op-g2"] = [{**row("op-g2", "failed", exit_code=75, export="n/a"), "revision": 10}]
    assert decide(claim("op-g2-work"))[:2] == ("keep", "unknown: Job gone, no admission record")
