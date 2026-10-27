"""Targeted session placement (docs/API.md "Sessions", "Placement"): a new session is bound, once, to the target the
model's routing resolves for an endpoint call (declared variants in the operator's class order, routing.default
priority/capacity, routing.fallback.regions unless the caller named a region) and that binding is immutable: the public
view carries it, the gateway hook binds the claim at that endpoint object only, the worker verification admits that
object's pod running that image only, the sweep reads that object's readiness. Off unless the fleet switches it on
(SESSION_TARGETED_PLACEMENT); the request hash of every pinned request is byte-equal to the one before, so earlier
sessions replay unchanged. The PostgreSQL rules (concurrent same-key winner, replay after a capacity flip, readiness by
object, legacy rows) are in test_session_placement_db.py."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app as appmod
import catalog
import config as cfg
import kube
import models
import render_endpoint
import sessions
from test_api import H, admin_keys, client, cluster, fake_keys  # noqa: F401
from test_sessions import FakeSessions, _auth, _principal
from test_variants import _measurements, _world

POOLS = {
    "h100-res-1x": {"nodes_ready": 2, "gpus_total": 2, "gpus_used": 2},
    "h100-spot-1x": {"nodes_ready": 0, "gpus_total": 0, "gpus_used": 0},
    "l40s-spot-1x": {"nodes_ready": 1, "gpus_total": 1, "gpus_used": 1},
}


@pytest.fixture
def targeted(client, admin_keys, fake_keys, monkeypatch):
    """The chat endpoint (three variants on the hub, capacity routing) with sessions and targeted placement on; the
    reserved variant is full (2 ready x 4 = 8 in flight), cheap (l40s) admits."""
    c, cl = client
    _auth(monkeypatch, {"sk-good": _principal("p1"), "sk-other": _principal("p2")})
    monkeypatch.setattr(cfg, "SESSIONS_ENABLED", True)
    monkeypatch.setattr(cfg, "SESSION_TARGETED_PLACEMENT", True)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1" if c == "hub" else c)
    e = _world(monkeypatch, {"chat": 2, "chat-spot": 0, "chat-cheap": 1})
    e = {**e, "routing": {**e["routing"], "default": "capacity"}}
    monkeypatch.setattr(catalog, "get", lambda name: e if name == "chat" else None)
    monkeypatch.setattr(catalog, "all_models", lambda: {"chat": e})
    _measurements(monkeypatch, {"chat-predictor": 8.0, "chat-cheap-predictor": 1.0}, 4, POOLS)
    fs = FakeSessions()
    for name in ("create", "get", "list_owner", "public", "cancel", "bind", "attach", "release"):
        monkeypatch.setattr(sessions, name, getattr(fs, name))
    monkeypatch.setattr(kube, "stamp_last_request", lambda *a, **k: None)
    return c, fs, e


def test_a_session_is_bound_to_the_target_an_endpoint_call_would_take(targeted, monkeypatch):
    c, fs, e = targeted
    r = c.post("/v1/models/chat:session", headers=H, json={"timeout_s": 600, "placement": "targeted"})
    assert r.status_code == 202, r.text
    s = r.json()
    # the same resolution as the call's: the full reserved variant is skipped, the admitting cheap one is taken
    assert s["target"] == {
        "cluster": "hub",
        "variant": "cheap",
        "id": "chat-cheap",
        "image": models.normalise_image("vllm/vllm-openai:v0.31.0-l40s"),
        "gpu_class": "l40s",
    }
    assert s["cluster"] == "hub" and s["region"] == "eu-north1"
    call = fs.calls[0]
    # the request's identity is its own deployment as _deployment_of names it (the normalised entry's region key, as in
    # 201), the binding is the fleet's cluster id; both are stored, only the binding is the placement
    assert call[5:7] == ("eu-north1", "eu-north1") and call[9]["id"] == "chat-cheap" and call[10] is True  # pinned
    # the owner's reads carry the binding
    assert c.get(f"/v1/sessions/{s['id']}", headers=H).json()["target"]["id"] == "chat-cheap"
    # capacity flips: a NEW request resolves anew (spot startable now), the earlier session's binding stands
    _measurements(monkeypatch, {"chat-predictor": 8.0, "chat-cheap-predictor": 4.0}, 4, POOLS)
    monkeypatch.setattr(kube, "pool_entry", lambda cluster, pool: {"max_nodes": 4, "gpu_class": "h100"})
    s2 = c.post("/v1/models/chat:session", headers=H, json={"timeout_s": 600, "placement": "targeted"}).json()
    assert s2["target"]["id"] == "chat-spot"
    assert c.get(f"/v1/sessions/{s['id']}", headers=H).json()["target"]["id"] == "chat-cheap"
    # an explicit region keeps the request pinned to that deployment (hub is the only one here)
    s3 = c.post(
        "/v1/models/chat:session", headers=H, json={"timeout_s": 600, "region": "eu-north1", "placement": "targeted"}
    ).json()
    assert s3["target"]["cluster"] == "hub" and fs.calls[-1][10] is True
    assert (
        c.post("/v1/models/chat:session", headers=H, json={"region": "elsewhere", "placement": "targeted"}).status_code
        == 400
    )
    assert c.post("/v1/models/chat:session", headers=H, json={"placement": "anything-else"}).status_code == 422


def test_the_claim_binds_at_the_sessions_own_endpoint_object_only(targeted):
    c, fs, e = targeted
    s = c.post("/v1/models/chat:session", headers=H, json={"placement": "targeted"}).json()
    assert s["target"]["id"] == "chat-cheap"
    hdr = {"x-serverless2-session-claim": s["claim_token"]}
    # presented at the base object (the model id) or at a sibling variant: refused, the claim is NOT consumed
    assert c.post("/internal/authorize/chat/v1/ws", headers=hdr).status_code == 401
    assert c.post("/internal/authorize/chat-spot/v1/ws", headers=hdr).status_code == 401
    assert fs.rows[s["id"]]["state"] == "claimed"
    # at its own object: bound once; the model identity injected towards the worker is the session's
    r = c.post("/internal/authorize/chat-cheap/v1/ws", headers=hdr)
    assert r.status_code == 200 and r.headers["x-serverless2-session"] == s["id"]
    assert c.post("/internal/authorize/chat-cheap/v1/ws", headers=hdr).status_code == 401
    # a session created before targeted placement (no object) binds at any object of its model, as before
    fs.rows[s["id"]]["state"] = "claimed"
    fs.rows[s["id"]]["object"] = None
    assert c.post("/internal/authorize/chat-spot/v1/ws", headers=hdr).status_code == 200


def test_off_by_default_the_request_is_placed_and_hashed_exactly_as_before(client, admin_keys, fake_keys, monkeypatch):
    c, _cl = client
    _auth(monkeypatch, {"sk-good": _principal("p1")})
    monkeypatch.setattr(cfg, "SESSIONS_ENABLED", True)
    assert cfg.SESSION_TARGETED_PLACEMENT is False
    fs = FakeSessions()
    for name in ("create", "get", "list_owner", "public", "cancel", "bind"):
        monkeypatch.setattr(sessions, name, getattr(fs, name))
    monkeypatch.setattr(kube, "stamp_last_request", lambda *a, **k: None)
    s = c.post("/v1/models/llm-example:session", headers=H, json={"timeout_s": 600}).json()
    assert s["target"] is None and fs.calls[0][9] is None and fs.calls[0][10] is True
    # the hash of every pinned request is the 201 formula, byte for byte; only an unpinned fallback request differs
    legacy = hashlib.sha256(
        json.dumps(
            {"model": "m", "cluster": "hub", "region": "eu-north1", "timeout_s": 600, "gpus": 1}, sort_keys=True
        ).encode()
    ).hexdigest()
    assert sessions.request_hash("m", "hub", "eu-north1", 600, 1) == legacy
    assert (
        sessions.request_hash("m", "hub", "eu-north1", 600, 1, pinned=False) == legacy
    )  # scope qualifies targeted only
    t = sessions.request_hash("m", "hub", "eu-north1", 600, 1, targeted=True)
    assert t != legacy and sessions.request_hash("m", "hub", "eu-north1", 600, 1, pinned=False, targeted=True) not in (
        legacy,
        t,
    )


def test_the_worker_must_be_the_targets_pod_running_the_targets_image():
    """sessions.worker_endpoint_check: the pod's endpoint object and its spec image (both from the pod the
    TokenReview named, never from the worker) must equal the session's binding; a legacy row checks the model only."""
    m = {"name": "chat", "namespace": "models"}
    img = models.normalise_image("vllm/vllm-openai:v0.31.0-l40s")
    s = {"model": "chat", "object": "chat-cheap", "image": img}
    good = [("kserve-container", "vllm/vllm-openai:v0.31.0-l40s"), ("queue-proxy", "queue-proxy:1")]
    sessions.worker_endpoint_check(s, m, "chat-cheap", "models", good)
    sessions.worker_endpoint_check(s, m, "chat-cheap", "models", [("endpoint", "vllm/vllm-openai:v0.31.0-l40s")])  # LWS
    with pytest.raises(HTTPException) as e:  # a sibling variant's pod
        sessions.worker_endpoint_check(s, m, "chat-spot", "models", good)
    assert e.value.status_code == 403 and "bound to chat-cheap" in e.value.detail
    with pytest.raises(HTTPException) as e:  # the same object rolled to another image
        sessions.worker_endpoint_check(
            s, m, "chat-cheap", "models", [("kserve-container", "vllm/vllm-openai:v0.32.0-l40s")]
        )
    assert e.value.status_code == 403 and "bound to image" in e.value.detail
    with pytest.raises(HTTPException) as e:  # the stored image on a SIDECAR, the worker rolled: never accepted
        sessions.worker_endpoint_check(
            s,
            m,
            "chat-cheap",
            "models",
            [("kserve-container", "vllm/vllm-openai:v0.32.0-l40s"), ("sidecar", "vllm/vllm-openai:v0.31.0-l40s")],
        )
    assert e.value.status_code == 403 and "worker runs" in e.value.detail
    with pytest.raises(HTTPException) as e:  # no serving container at all
        sessions.worker_endpoint_check(s, m, "chat-cheap", "models", [("sidecar", "vllm/vllm-openai:v0.31.0-l40s")])
    assert e.value.status_code == 403 and "0 serving containers" in e.value.detail
    with pytest.raises(HTTPException) as e:  # two serving names in one pod: ambiguous, never a worker pod
        sessions.worker_endpoint_check(
            s,
            m,
            "chat-cheap",
            "models",
            [("kserve-container", "vllm/vllm-openai:v0.31.0-l40s"), ("endpoint", "vllm/vllm-openai:v0.31.0-l40s")],
        )
    assert e.value.status_code == 403 and "2 serving containers" in e.value.detail
    with pytest.raises(HTTPException) as e:  # another model, as before
        sessions.worker_endpoint_check(s, {"name": "other", "namespace": "models"}, "chat-cheap", "models", good)
    assert e.value.status_code == 403
    legacy = {"model": "chat", "object": None, "image": None}
    sessions.worker_endpoint_check(legacy, m, "chat-spot", "models", [("sidecar", "anything:1")])


def test_verify_worker_reads_the_image_from_the_pod_object_not_the_worker(monkeypatch):
    """The whole verification path with a faked cluster: TokenReview names the pod, the pod object (same UID) carries
    the endpoint label and the spec image; a pod of the right object with another image is refused."""
    from kubernetes import client as k8s

    img = models.normalise_image("vllm/vllm-openai:v0.31.0-l40s")
    s = {"model": "chat", "region": "eu-north1", "object": "chat-cheap", "image": img}
    m = {"name": "chat", "namespace": "models"}
    pods = {}

    class Reviews:
        def __init__(self, *a, **k):
            pass

        def create_token_review(self, body, **kw):
            return SimpleNamespace(
                status=SimpleNamespace(
                    authenticated=True,
                    user=SimpleNamespace(
                        username="system:serviceaccount:models:worker",
                        extra={
                            "authentication.kubernetes.io/pod-name": [body.spec.token],
                            "authentication.kubernetes.io/pod-uid": ["uid-" + body.spec.token],
                        },
                    ),
                )
            )

    class Core:
        def read_namespaced_pod(self, name, ns, **kw):
            return pods[name]

    def pod(name, label, image):
        return SimpleNamespace(
            metadata=SimpleNamespace(uid="uid-" + name, labels={"serving.kserve.io/inferenceservice": label}),
            spec=SimpleNamespace(
                containers=[
                    SimpleNamespace(name="kserve-container", image=image),
                    SimpleNamespace(name="queue-proxy", image="queue-proxy:1"),
                ]
            ),
            status=SimpleNamespace(container_statuses=[]),
        )

    pods["good"] = pod("good", "chat-cheap", "vllm/vllm-openai:v0.31.0-l40s")
    pods["rolled"] = pod("rolled", "chat-cheap", "vllm/vllm-openai:v0.32.0-l40s")
    pods["sibling"] = pod("sibling", "chat-spot", "vllm/vllm-openai:v0.31.0-l40s")
    pods["decoy"] = pod(
        "decoy", "chat-cheap", "vllm/vllm-openai:v0.32.0-l40s"
    )  # the worker rolled, a sidecar keeps the old image
    pods["decoy"].spec.containers.append(SimpleNamespace(name="sidecar", image="vllm/vllm-openai:v0.31.0-l40s"))
    monkeypatch.setattr(k8s, "AuthenticationV1Api", Reviews)
    monkeypatch.setattr(kube, "_client", lambda region: None)
    monkeypatch.setattr(kube, "core", lambda region: Core())
    w = sessions.verify_worker("good", s, lambda label: m)
    assert w["uid"] == "uid-good" and w["region"] == "eu-north1"
    for token in ("rolled", "sibling", "decoy"):
        with pytest.raises(HTTPException) as e:
            sessions.verify_worker(token, s, lambda label: m)
        assert e.value.status_code == 403
    legacy = {"model": "chat", "region": "eu-north1", "object": None, "image": None}
    assert sessions.verify_worker("rolled", legacy, lambda label: m)["uid"] == "uid-rolled"


def test_the_security_policy_names_the_object_the_hook_learns_the_target_from():
    base = render_endpoint._securitypolicy({"name": "chat", "namespace": "models", "modelId": "chat"})
    variant = render_endpoint._securitypolicy({"name": "chat-cheap", "namespace": "models", "modelId": "chat"})
    assert base["spec"]["extAuth"]["http"]["path"] == "/internal/authorize/chat"
    assert variant["spec"]["extAuth"]["http"]["path"] == "/internal/authorize/chat-cheap"
    assert (
        appmod._endpoint_model
    )  # the hook resolves the model from an object name (catalog.get(...) or _endpoint_model)


def test_the_hook_decides_on_the_canonical_model_whatever_object_the_policy_named(targeted, monkeypatch):
    """A variant's SecurityPolicy names the OBJECT; every decision of the hook (an admitted credential bound to the
    canonical model, the key's model grant, the markers) is about the model, so an endpoint child's call to a variant
    host is accepted as it was when every policy named the model; only a live session's bind uses the object."""
    c, fs, e = targeted
    seen = {}

    def principal(token, operation, model):
        seen["admitted"] = (token, operation, model)
        from auth import Principal

        return Principal(key="s2-admitted.t", tenant="demo", info={"key_alias": "a", "metadata": {}}, key_hash="h")

    monkeypatch.setattr(appmod, "_admitted_principal", principal)
    monkeypatch.setattr(
        appmod, "_authorize_model", lambda p, model, *a, **k: seen.setdefault("authorize", model) and ("op-1", "a")
    )
    hdr = {"Authorization": "Bearer s2-admitted.t", "x-serverless2-operation": "op-1", "x-serverless2-attempt": "a"}
    for obj in ("chat", "chat-spot", "chat-cheap"):
        seen.clear()
        r = c.post(f"/internal/authorize/{obj}/v1/chat/completions", headers=hdr)
        assert r.status_code == 200, (obj, r.text)
        assert seen["admitted"] == ("t", "op-1", "chat") and seen["authorize"] == "chat"
    assert c.post("/internal/authorize/no-such-object/v1/x", headers=hdr).status_code == 404


def test_a_client_that_did_not_opt_in_keeps_the_canonical_origin_even_with_placement_on(targeted):
    """Root's cross-contract case (legacy-client-compatibility-r1): the fleet has placement on and a same-region
    alternate (cheap) admits while the base does not; an unchanged client sends timeout_s + region only and connects to
    its configured canonical origin. It must get the base object, no target, and bind there."""
    c, fs, e = targeted
    r = c.post("/v1/models/chat:session", headers=H, json={"timeout_s": 600, "region": "eu-north1"})
    assert r.status_code == 202, r.text
    s = r.json()
    assert s["target"] is None and fs.calls[-1][9] is None and fs.calls[-1][5:7] == ("eu-north1", "eu-north1")
    hdr = {"x-serverless2-session-claim": s["claim_token"]}
    assert c.post("/internal/authorize/chat/v1/ws", headers=hdr).status_code == 200  # the canonical origin binds it
    # the opted-in client, same fleet, same moment: the alternate, bound there only
    t = c.post(
        "/v1/models/chat:session", headers=H, json={"timeout_s": 600, "region": "eu-north1", "placement": "targeted"}
    ).json()
    assert t["target"]["id"] == "chat-cheap"
    assert (
        c.post("/internal/authorize/chat/v1/ws", headers={"x-serverless2-session-claim": t["claim_token"]}).status_code
        == 401
    )
    assert (
        c.post(
            "/internal/authorize/chat-cheap/v1/ws", headers={"x-serverless2-session-claim": t["claim_token"]}
        ).status_code
        == 200
    )


def test_opting_in_on_a_fleet_without_the_capability_fails_closed(client, admin_keys, fake_keys, monkeypatch):
    c, _cl = client
    _auth(monkeypatch, {"sk-good": _principal("p1")})
    monkeypatch.setattr(cfg, "SESSIONS_ENABLED", True)
    assert cfg.SESSION_TARGETED_PLACEMENT is False
    fs = FakeSessions()
    for name in ("create", "get", "public"):
        monkeypatch.setattr(sessions, name, getattr(fs, name))
    monkeypatch.setattr(kube, "stamp_last_request", lambda *a, **k: None)
    r = c.post("/v1/models/llm-example:session", headers=H, json={"timeout_s": 600, "placement": "targeted"})
    assert r.status_code == 409 and "targeted_placement" in r.text and fs.calls == []  # nothing admitted, no downgrade
    assert c.post("/v1/models/llm-example:session", headers=H, json={"timeout_s": 600}).status_code == 202
