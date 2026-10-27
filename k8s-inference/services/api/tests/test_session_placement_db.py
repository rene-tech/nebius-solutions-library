"""Targeted session placement against PostgreSQL (docs/API.md "Sessions", "Placement"): the binding is written once and
answered as it is by every replay; concurrent same-key creates that resolved different targets make one row, one
admission, one usable claim and every caller gets the winner's target; a retry after a capacity flip keeps the target
and both deadlines; region alias/default equivalence and a genuinely changed request are told apart; the claim binds
at the session's object only and is not consumed elsewhere; readiness is the object's, never the base's; rows created
before targeted placement keep the model-level rules. Skipped without TEST_DATABASE_URL."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

import app as appmod
import catalog
import config as cfg
import db
import kube
import models
import sessions
from test_api import H, client, cluster  # noqa: F401
from test_database import database  # noqa: F401
from test_session_idempotency import _rig
from test_sessions_db import KEY, _create, _reservation, _sql, live  # noqa: F401

CHEAP = {
    "cluster": "hub",
    "region": "eu-north1",
    "variant": "cheap",
    "id": "speech-cheap",
    "image": "img:l40s",
    "gpu_class": "l40s",
}
SPOT = {
    "cluster": "hub",
    "region": "eu-north1",
    "variant": "spot",
    "id": "speech-spot",
    "image": "img:h100",
    "gpu_class": "h100",
}


def _targeted(target, **kw):
    return sessions.create(KEY, "demo", "p-1", "speech", "hub", "eu-north1", 600, 1, target=target, **kw)


def _state(sid):
    with db.pool().connection() as c:
        return c.execute(
            "select state, object, image, variant, cluster, region from sessions where id = %s", (sid,)
        ).fetchone()


def test_the_binding_is_written_once_and_the_claim_binds_at_its_object_only(live):
    s = _targeted(CHEAP)
    assert s["target"] == {
        "cluster": "hub",
        "variant": "cheap",
        "id": "speech-cheap",
        "image": "img:l40s",
        "gpu_class": "l40s",
    }
    assert _state(s["id"]) == ("claimed", "speech-cheap", "img:l40s", "cheap", "hub", "eu-north1")
    # at the base object or a sibling: 401 and the claim is NOT consumed; at its own object: bound once
    for wrong in ("speech", "speech-spot", None):
        with pytest.raises(HTTPException) as e:
            sessions.bind(s["claim_token"], "speech", wrong)
        assert e.value.status_code == 401
    assert _state(s["id"])[0] == "claimed"
    b = sessions.bind(s["claim_token"], "speech", "speech-cheap")
    assert b["id"] == s["id"] and _state(s["id"])[0] == "bound"
    # a row created before targeted placement (no object): any object of the model binds it, as before
    legacy = _create()
    assert _state(legacy["id"])[1] is None and legacy["target"] is None
    assert sessions.bind(legacy["claim_token"], "speech", "speech-spot")["id"] == legacy["id"]


def test_concurrent_same_key_creates_with_different_resolutions_make_one_session_with_the_winners_target(live):
    """Six creates under one Idempotency-Key at once, each having resolved a different target (capacity flipping
    between callers): one row, one admission, one usable claim, and every answer carries the winner's target."""
    with ThreadPoolExecutor(max_workers=6) as ex:
        outs = list(ex.map(lambda i: _targeted(CHEAP if i % 2 else SPOT, idempotency_key="iso-k1"), range(6)))
    ids = {o["id"] for o in outs}
    assert len(ids) == 1
    sid = ids.pop()
    assert sum(1 for o in outs if o["replay"] is False) == 1
    assert len({o["target"]["id"] for o in outs}) == 1 and outs[0]["target"]["id"] == _state(sid)[1]
    with db.pool().connection() as c:
        assert c.execute("select count(*) from sessions").fetchone()[0] == 1
        assert c.execute("select count(*) from key_reservations where operation = %s", (sid,)).fetchone()[0] == 1
    assert sum(1 for o in outs if o.get("claim_token")) >= 1


def test_a_retry_after_a_capacity_flip_keeps_the_target_and_deadlines_at_the_api(database, client, monkeypatch):
    """At the API with the PostgreSQL rows: the same request again answers the same session and binding although
    the resolver now picks another target; `{}` and the effective region are one request; another timeout is 409."""
    c = _rig(database, client, monkeypatch)
    monkeypatch.setattr(cfg, "SESSION_TARGETED_PLACEMENT", True)
    m = catalog.get("llm-example")
    picks = iter(["cheap", "spot", "spot", "spot"])
    base = models.variants(m, m["regions"][0])[0]
    monkeypatch.setattr(
        appmod,
        "_call_targets",
        lambda mm, cl, inp, rg: [(cl, {**base, "name": next(picks), "object": f"llm-example-{picks}"})],
    )
    monkeypatch.setattr(
        appmod,
        "_target_record",
        lambda mm, cl, v: {
            "cluster": cl,
            "variant": v["name"],
            "id": f"llm-example-{v['name']}",
            "image": "img",
            "gpu_class": "l40s",
        },
    )
    hk = {**H, "Idempotency-Key": "flip-1"}
    r1 = c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 600})
    assert r1.status_code == 202, r1.text
    s1 = r1.json()
    assert s1["replay"] is False and s1["target"]["variant"] == "cheap"
    r2 = c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 600, "region": "eu-north1"})
    assert r2.status_code == 202, r2.text
    s2 = r2.json()
    assert s2["id"] == s1["id"] and s2["replay"] is True and s2["target"] == s1["target"]
    assert (s2["expires_at"], s2["deadline_at"]) == (s1["expires_at"], s1["deadline_at"])
    assert c.post("/v1/models/llm-example:session", headers=hk, json={"timeout_s": 300}).status_code == 409
    # a new key is a new request: resolved anew (spot), its own row
    s3 = c.post(
        "/v1/models/llm-example:session", headers={**H, "Idempotency-Key": "flip-2"}, json={"timeout_s": 600}
    ).json()
    assert s3["id"] != s1["id"] and s3["target"]["variant"] == "spot"
    # an unpinned request on a fallback model is another request than a pinned one (the hash carries the scope)
    assert sessions.request_hash("llm-example", "hub", "eu-north1", 600, 1, pinned=False) != sessions.request_hash(
        "llm-example", "hub", "eu-north1", 600, 1
    )


def test_readiness_is_the_targets_own_object_never_the_base_or_a_sibling(live, monkeypatch):
    monkeypatch.setattr(cfg, "SESSION_ATTACH_WAIT_S", 60)
    entry = {"k8s_name": "speech", "namespace": "models", "nodes": 1}
    monkeypatch.setattr(catalog, "get", lambda name: entry if name == "speech" else None)
    ready = {"speech": 1}  # the BASE object is ready; the targeted objects are not
    monkeypatch.setattr(
        kube,
        "endpoint_status",
        lambda name, ns, region, nodes=1: {
            "status": "ready" if ready.get(name) else "scaled-to-zero",
            "replicas_ready": ready.get(name, 0),
        },
    )
    cheap, legacy = _targeted(CHEAP), _create()
    sessions.bind(cheap["claim_token"], "speech", "speech-cheap")
    sessions.bind(legacy["claim_token"], "speech", "speech")
    out = sessions.sweep()
    assert (
        f"{legacy['id']}:ready-seen" in out and f"{cheap['id']}:ready-seen" not in out
    )  # the base's readiness is the legacy row's only
    ready["speech-cheap"] = 1
    assert f"{cheap['id']}:ready-seen" in sessions.sweep()
    assert sessions.get(cheap["id"])["ready_seen_at"] is not None
