import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from kubernetes import client  # noqa: E402

import dispatcher as d  # noqa: E402

NOW = datetime.now(timezone.utc)


def pod(
    msg="released work=op-1-work http=200 at=t",
    exit_code=0,
    claim="op-1-work",
    ended_s=d.ORPHAN_AFTER_S + 1,
    owner_uid="job-uid-1",
    phase="Succeeded",
    ns="tenant-t",
    owners=None,
):
    vols = (
        [NS(name="work", persistent_volume_claim=NS(claim_name=claim))]
        if claim
        else [NS(name="work", persistent_volume_claim=None)]
    )
    term = NS(exit_code=exit_code, message=msg, finished_at=NOW - timedelta(seconds=ended_s))
    main = NS(exit_code=0, message=None, finished_at=NOW - timedelta(seconds=ended_s + 5))
    refs = owners if owners is not None else [NS(kind="Job", name="op-1", uid=owner_uid, controller=True)]
    return NS(
        metadata=NS(name="op-1-x1", namespace=ns, uid="pod-uid-1", resource_version="77", owner_references=refs),
        spec=NS(volumes=vols),
        status=NS(
            phase=phase,
            container_statuses=[
                NS(name="main", state=NS(terminated=main)),
                NS(name="uploader", state=NS(terminated=term)),
            ],
        ),
    )


def job(uid="job-uid-1", done=True, active=0, native=True):
    conds = [NS(type="Complete", status="True")] if done else [NS(type="Complete", status="False")]
    return NS(
        metadata=NS(uid=uid, labels={f"{d.LABEL}/tenant": "t"} if native else {}),
        status=NS(conditions=conds, active=active),
    )


class Api(Exception):
    pass


class Core:
    def __init__(self, pods, pvc="deleting"):
        self.pods, self.pvc, self.deleted = pods, pvc, []

    def list_pod_for_all_namespaces(self, field_selector=None):
        return NS(items=[p for p in self.pods if f"status.phase={p.status.phase}" == field_selector])

    def read_namespaced_persistent_volume_claim(self, name, ns):
        if self.pvc == "absent":
            raise client.exceptions.ApiException(status=404)
        return NS(metadata=NS(deletion_timestamp=NOW if self.pvc == "deleting" else None))

    def delete_namespaced_pod(self, name, ns, body=None):
        self.deleted.append((ns, name, body.preconditions.uid, body.preconditions.resource_version))
        if getattr(self, "conflict", False):
            raise client.exceptions.ApiException(status=409)


class Batch:
    def __init__(self, j):
        self.j = j

    def read_namespaced_job(self, name, ns):
        return self.j


def sweep(p, j=None, pvc="deleting", conflict=False):
    core = Core([p], pvc)
    core.conflict = conflict
    d.sweep_released_pods({"hub": {"core": core, "batch": Batch(job() if j is None else j)}})
    return core.deleted


def test_released_terminal_pod_is_deleted_with_exact_preconditions():
    """The uploader's own message names the pod's work claim, it exited 0, the owner Job (exact uid) is terminal, the
    claim is deleting or gone, the pod ended before ORPHAN_AFTER_S: deleted with its uid and resourceVersion."""
    assert sweep(pod()) == [("tenant-t", "op-1-x1", "pod-uid-1", "77")]
    assert sweep(pod(), pvc="absent") == [("tenant-t", "op-1-x1", "pod-uid-1", "77")]
    assert sweep(pod(phase="Failed")) == [("tenant-t", "op-1-x1", "pod-uid-1", "77")]
    assert sweep(pod(), conflict=True) == [("tenant-t", "op-1-x1", "pod-uid-1", "77")]  # 409: logged, nothing else


def test_every_missing_proof_keeps_the_pod():
    assert sweep(pod(msg=None)) == []  # an older runner: no message
    assert sweep(pod(msg="kept work=op-1-work")) == []
    assert sweep(pod(msg="released leaf=l1")) == []  # shared scratch: the shared claim is never the dispatcher's
    assert sweep(pod(msg="released work=other-work http=200")) == []  # names a claim that is not the pod's own
    assert sweep(pod(claim=None)) == []  # emptyDir work volume
    assert sweep(pod(exit_code=1)) == []  # the uploader failed after writing
    assert sweep(pod(ended_s=10)) == []  # too fresh
    assert sweep(pod(ns="models")) == []  # not a tenant namespace
    assert sweep(pod(owners=[])) == []
    assert (
        sweep(
            pod(
                owners=[
                    NS(kind="Job", name="op-1", uid="u", controller=True),
                    NS(kind="Job", name="op-2", uid="v", controller=True),
                ]
            )
        )
        == []
    )


def test_owner_job_and_claim_state_gate_the_delete():
    assert sweep(pod(), j=job(uid="another-uid")) == []  # a replaced Job with the same name is not the owner
    assert sweep(pod(), j=job(done=False)) == []
    assert sweep(pod(), j=job(active=1)) == []
    assert sweep(pod(), j=job(native=False)) == []  # a foreign Job
    assert sweep(pod(), pvc="bound") == []  # present and not deleting: kept or restored
    assert d.released_claim(pod()) == "op-1-work"
    assert d.terminal_job(job(), "job-uid-1") and not d.terminal_job(None, "job-uid-1")
