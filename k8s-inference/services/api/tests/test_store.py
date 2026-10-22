"""The list-watch store (api 0.11.173, services/api/store.py): LIST once, WATCH from its version, one copy of every
object updated in place; bookmarks move the version, 410 Gone relists, a namespace that is not onboarded is empty and
ready, a broken watch degrades to a direct read after STORE_STALE_S, this replica's own writes show at once."""

import json
import time

import pytest
from kubernetes.client.rest import ApiException

import store

K = ("eu-north1", "tenant-demo", "jobs")


def job(name, rv="1", **labels):
    return {
        "metadata": {
            "name": name,
            "resourceVersion": rv,
            "labels": {"serverless2.nebius/tenant": "demo", **labels},
            "managedFields": [{"manager": "x"}],
        },
        "spec": {},
        "status": {},
    }


class Resp:
    """What the Kubernetes client hands back with `_preload_content=False`: a list body in `.data`, a watch as a
    stream of newline-separated JSON events."""

    def __init__(self, body=None, events=()):
        self.data = json.dumps(body).encode() if body is not None else b""
        self._events = list(events)

    def stream(self, amt=None, decode_content=False):
        for ev in self._events:
            yield (json.dumps(ev) + "\n").encode()


class Lister:
    def __init__(self, listing, sessions):
        self.listing, self.sessions, self.calls = listing, list(sessions), []

    def __call__(self, region, ns, **kw):
        self.calls.append(kw)
        if kw.get("watch"):
            if not self.sessions:
                store._stop.set()  # the test's last session: end the sync loop as a shutdown would
                return Resp(events=[])
            s = self.sessions.pop(0)
            if isinstance(s, Exception):
                raise s
            return Resp(events=s)
        return Resp(body=self.listing)


@pytest.fixture(autouse=True)
def _fresh():
    store.reset()
    store._stop.clear()
    yield
    store.reset()
    store._stop.clear()


def test_list_then_watch_keeps_one_copy_current():
    listing = {"metadata": {"resourceVersion": "10"}, "items": [job("a"), job("b")]}
    lister = Lister(
        listing,
        [
            [
                {"type": "MODIFIED", "object": job("a", rv="11", phase="x")},
                {"type": "ADDED", "object": job("c", rv="12")},
                {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "13"}}},
                {"type": "DELETED", "object": job("b", rv="14")},
            ]
        ],
    )
    assert not store.ready(*K)
    store.sync(K, lister, "serverless2.nebius/tenant")
    assert store.ready(*K)
    names = sorted(j["metadata"]["name"] for j in store.items(*K))
    assert names == ["a", "c"] and store.get(*K, "a")["metadata"]["labels"]["phase"] == "x"
    assert all("managedFields" not in j["metadata"] for j in store.items(*K))  # the ledger is never held
    assert store._state[K]["rv"] == "14" and store.version(*K) == 4  # list + 3 events (the bookmark moves rv only)
    first, second = lister.calls[0], lister.calls[1]
    assert first == {"_preload_content": False, "label_selector": "serverless2.nebius/tenant"}
    assert second["watch"] and second["resource_version"] == "10" and second["allow_watch_bookmarks"]
    assert lister.calls[2]["resource_version"] == "14"  # the next session resumes from the last version


def test_gone_relists_and_other_errors_degrade_to_a_direct_read(monkeypatch):
    listing = {"metadata": {"resourceVersion": "20"}, "items": [job("a")]}
    gone = {"type": "ERROR", "object": {"kind": "Status", "code": 410, "message": "too old resource version"}}
    lister = Lister(listing, [[gone]])
    with pytest.raises(store._Gone):
        store.sync(K, lister, None)
    assert store.ready(*K)  # what was listed stands while the caller relists
    store.degrade(K, "boom")
    assert store.ready(*K)  # a fresh failure: the copy is at most seconds old, still served
    monkeypatch.setattr(store, "STORE_STALE_S", 0.0)
    assert not store.ready(*K)  # broken for longer than STORE_STALE_S: readers list directly
    store.replace(K, [job("a")], "21")
    assert store.ready(*K) and store._state[K]["error"] is None  # a successful relist clears the fault


def test_a_namespace_not_onboarded_is_ready_and_empty(monkeypatch):
    def lister(region, ns, **kw):
        raise ApiException(status=404, reason="Not Found")

    monkeypatch.setitem(store.KINDS, "jobs", (lister, None, None, store._plain))
    monkeypatch.setattr(store, "STORE_NS_REFRESH_S", 0.0)
    store._run(K)  # returns: nothing to watch
    assert store.ready(*K) and store.items(*K) == []


def test_own_writes_show_at_once_and_never_regress_the_store():
    store.replace(K, [job("a", rv="5")], "5")
    store.observe(*K, {**job("a", rv="7"), "_pods": [], "_region": "eu-north1"})  # a mutation's response
    held = store.get(*K, "a")
    assert held["metadata"]["resourceVersion"] == "7" and "_pods" not in held  # a reader's private fields never
    store.observe(*K, job("a", rv="6"))  # older than what the watch delivered meanwhile
    assert store.get(*K, "a")["metadata"]["resourceVersion"] == "7"
    store.drop(*K, "a")
    assert store.get(*K, "a") is None
    store.apply(K, "DELETED", job("a"))  # the watch's DELETED for the same object: idempotent
    assert store.items(*K) == []


def test_workloads_are_held_compact():
    import kube

    k = ("eu-north1", "tenant-demo", "workloads")
    full = {
        "metadata": {"name": "job-op-1-abc", "uid": "u1", "labels": {kube.KUEUE_JOB_UID_LABEL: "job-uid-1"}},
        "spec": {"podSets": [{"template": {"spec": {"containers": [{"image": "x"}]}}}]},
        "status": {"clusterName": "north", "conditions": []},
    }
    store.replace(k, [full], "1")
    (held,) = store.items(*k)
    assert held == kube._compact_workload(full) and "podSets" not in json.dumps(held)


def test_health_reports_every_key():
    store.replace(K, [job("a")], "9")
    h = store.health()["eu-north1/tenant-demo/jobs"]
    assert h["synced"] and h["objects"] == 1 and h["version"] == 1 and h["error"] is None
    assert isinstance(h["listed_s_ago"], int) and time.time() > 0
