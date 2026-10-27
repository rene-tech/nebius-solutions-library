"""Variants and request routing (docs/API.md "Variants"): several deployments of one endpoint in a region behind one
name, in priority order (a warm floor on one pool, overflow on another), per-GPU-class images, a request field that
picks a variant, LiteLLM deployments with priorities, and the objects every variant renders."""

import time

import pytest
from fastapi import HTTPException

import app as appmod
import catalog
import kube
import models
from test_api import FLEET, H, client, cluster  # noqa: F401

VARIANT_FLEET = {
    "pools": {
        "hub-h100-res": {
            "region": "hub",
            "pool": "h100-res-1x",
            "gpu_class": "h100",
            "gpus_per_node": 1,
            "capacity": "reserved",
            "usd_per_gpu_hour": 2.0,
        },
        "hub-h100-spot": {
            "region": "hub",
            "pool": "h100-spot-1x",
            "gpu_class": "h100",
            "gpus_per_node": 1,
            "capacity": "spot",
            "usd_per_gpu_hour": 0.8,
        },
        "hub-l40s": {
            "region": "hub",
            "pool": "l40s-spot-1x",
            "gpu_class": "l40s",
            "gpus_per_node": 1,
            "capacity": "spot",
            "usd_per_gpu_hour": 0.7,
        },
    }
}

SPEC = {
    "id": "chat",
    "kind": "endpoint",
    "protocol": "openai",
    "image": "vllm/vllm-openai:v0.31.0",
    "port": 8000,
    "images": {"l40s": "vllm/vllm-openai:v0.31.0-l40s"},
    "gpu": {"count": 1, "classes": ["h100", "l40s"]},
    "deployments": {
        "eu-north1": {
            "variants": [
                {"name": "reserved", "pool": "h100-res-1x", "min": 1, "max": 2},
                {"name": "spot", "pool": "h100-spot-1x", "min": 0, "max": 4},
                {"name": "cheap", "pool": "l40s-spot-1x", "min": 0, "max": 2},
            ]
        }
    },
    "routing": {"field": "tier", "map": {"fast": "reserved", "budget": "cheap"}, "default": "priority"},
}


@pytest.fixture(autouse=True)
def _fleet(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: VARIANT_FLEET)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1" if c == "hub" else c)


class _Ready:  # a readiness probe answered: the endpoint serves (api 0.11.143)
    status_code = 200

    def json(self):
        return {}


def test_entry_carries_variants_with_per_class_images():
    e = models.to_entry(SPEC)
    d = e["deployments"]["hub"]
    assert d["pool"] == "h100-res-1x" and [v["name"] for v in d["variants"]] == ["reserved", "spot", "cheap"]
    assert d["variants"][0]["min"] == 1 and d["variants"][1]["max"] == 4
    assert d["variants"][2]["image"].endswith("vllm/vllm-openai:v0.31.0-l40s") and d["variants"][0]["image"].endswith(
        "vllm/vllm-openai:v0.31.0"
    )
    assert e["routing"]["default"] == "priority"
    vs = models.variants(e, "hub")
    assert [v["object"] for v in vs] == ["chat", "chat-spot", "chat-cheap"]
    assert models.variants({"id": "plain", "deployments": {"hub": {"pool": "h100-spot-1x"}}}, "hub") == [
        {"name": "primary", "pool": "h100-spot-1x", "min": None, "max": None, "image": None, "object": "plain"}
    ]
    # a single deployment of a class with its own image takes it
    single = models.to_entry(
        {
            "id": "one",
            "kind": "endpoint",
            "image": "img:default",
            "images": {"h100": "img:h100"},
            "gpu": {"count": 1, "classes": ["h100"]},
        }
    )
    assert single["deployments"]["hub"]["image"].endswith("img:h100")


def test_validation_of_variants_and_routing():
    def bad(patch, code=400):
        with pytest.raises(HTTPException) as e:
            models.to_entry({**SPEC, **patch})
        assert e.value.status_code == code

    bad({"deployments": {"eu-north1": {"variants": [{"name": "x", "pool": "nope"}]}}})
    bad({"deployments": {"mars": {"variants": [{"pool": "h100-res-1x"}]}}})
    bad(
        {
            "deployments": {
                "eu-north1": {"variants": [{"name": "a", "pool": "h100-res-1x"}, {"name": "a", "pool": "h100-spot-1x"}]}
            }
        }
    )
    bad({"deployments": {"eu-north1": {"variants": [{"name": "a", "pool": "h100-res-1x", "min": 3, "max": 1}]}}})
    bad({"routing": {"field": "tier", "map": {"fast": "unknown"}}})
    bad({"kind": "job", "command": "x", "routing": {"field": "tier"}})
    bad({"routing": {"map": {"fast": "reserved"}}})  # a map without the field it maps
    bad({"routing": {"field": 7}})
    # api 0.11.185: `default` + `fallback` alone is a routing (no request-based selection), the cohort proposal's shape
    ok = models.to_entry(
        {
            **SPEC,
            "routing": {"default": "capacity", "fallback": {"max_switches": 3, "cold_wait_s": 60, "regions": True}},
        }
    )
    assert ok["routing"] == {
        "default": "capacity",
        "fallback": {"max_switches": 3, "cold_wait_s": 60, "regions": True},
    }


def test_each_variant_renders_its_own_objects_under_the_model_label():
    e = models.to_entry(SPEC)
    docs = models.render(e, "hub")
    isvcs = {d["metadata"]["name"]: d for d in docs if d["kind"] == "InferenceService"}
    assert set(isvcs) == {"chat", "chat-spot", "chat-cheap"}
    assert (
        isvcs["chat"]["spec"]["predictor"]["minReplicas"] == 1
        and isvcs["chat-spot"]["spec"]["predictor"]["maxReplicas"] == 4
    )
    assert isvcs["chat-cheap"]["spec"]["predictor"]["containers"][0]["image"].endswith("-l40s")
    assert isvcs["chat-cheap"]["spec"]["predictor"]["nodeSelector"]["serverless2.nebius/pool"] == "l40s-spot-1x"
    for name, i in isvcs.items():
        assert i["metadata"]["labels"]["serverless2.nebius/model"] == "chat"
    assert (
        isvcs["chat-spot"]["metadata"]["labels"]["serverless2.nebius/variant"] == "spot"
        and "serverless2.nebius/variant" not in isvcs["chat"]["metadata"]["labels"]
    )
    policies = {d["metadata"]["name"]: d for d in docs if d["kind"] == "SecurityPolicy"}
    assert set(policies) == {"chat-authorization", "chat-spot-authorization", "chat-cheap-authorization"}
    # every object's policy names ITS object (the base: the model id): the hook learns which endpoint a request
    # reached, the identity a targeted session's claim must match (docs/API.md "Sessions", "Placement")
    assert {n: p["spec"]["extAuth"]["http"]["path"] for n, p in policies.items()} == {
        "chat-authorization": "/internal/authorize/chat",
        "chat-spot-authorization": "/internal/authorize/chat-spot",
        "chat-cheap-authorization": "/internal/authorize/chat-cheap",
    }
    assert (
        policies["chat-spot-authorization"]["spec"]["targetSelectors"][0]["matchLabels"]["serving.knative.dev/route"]
        == "chat-spot-predictor"
    )


def test_hostnames_and_litellm_deployments_follow_the_variants(monkeypatch):
    e = models.to_entry(SPEC)
    monkeypatch.setattr(models, "endpoint_domain", lambda region: "203.0.113.1.sslip.io")
    assert models.variant_hosts(e, "eu-north1") == [
        "chat-predictor.models.203.0.113.1.sslip.io",
        "chat-spot-predictor.models.203.0.113.1.sslip.io",
        "chat-cheap-predictor.models.203.0.113.1.sslip.io",
    ]
    calls = []

    class R:
        def __init__(self, code):
            self.status_code, self.text = code, ""

    class C:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def patch(self, url, json=None, headers=None):
            calls.append(("patch", url, json))
            return R(404)

        def post(self, url, json=None, headers=None):
            calls.append(("post", url, json))
            return R(200)

    import httpx

    monkeypatch.setattr(httpx, "Client", C)
    monkeypatch.setattr(models, "LITELLM_MASTER_KEY", "m")
    monkeypatch.setattr(models, "ROUTER_KEY", "i")
    monkeypatch.setattr(models, "LITELLM_URL", "http://litellm")
    assert models.litellm_group_upsert(e, "hub", "eu-north1") is None
    posted = [c[2] for c in calls if c[0] == "post"]
    assert [p["model_info"]["id"] for p in posted] == ["chat--hub", "chat--hub--spot", "chat--hub--cheap"]
    assert [p["litellm_params"]["order"] for p in posted] == [1, 2, 3]
    assert posted[2]["litellm_params"]["api_base"] == "https://chat-cheap-predictor.models.203.0.113.1.sslip.io/v1"
    calls.clear()
    assert models.litellm_group_delete("chat", "hub", ["spot", "cheap"]) is None
    assert [c[2]["id"] for c in calls] == ["chat--hub", "chat--hub--spot", "chat--hub--cheap"]

    # an entry LiteLLM no longer has is deleted (its 400 "not found in db"); any other 400 is still a failure
    def answer(code, text):
        r = R(code)
        r.text = text
        return r

    C.post = lambda self, url, json=None, headers=None: answer(
        400, "{'error': 'Model with id=chat--hub not found in db'}"
    )
    assert models.litellm_group_delete("chat", "hub", []) is None
    C.post = lambda self, url, json=None, headers=None: answer(400, "{'error': 'bad request'}")
    assert models.litellm_group_delete("chat", "hub", []).startswith("litellm /model/delete 400")


def test_sync_requests_pick_a_variant_by_field_then_by_readiness(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(kube, "fleet", lambda: VARIANT_FLEET)
    e = catalog.normalise(models.to_entry(SPEC))
    monkeypatch.setattr(catalog, "get", lambda name: e if name == "chat" else None)
    monkeypatch.setattr(models, "cluster_id", lambda: "hub")
    ready = {"chat": 0, "chat-spot": 2, "chat-cheap": 0}
    monkeypatch.setattr(
        kube,
        "endpoint_status",
        lambda name, ns, region="eu-north1", nodes=1: {
            "status": "ready" if ready.get(name) else "scaled-to-zero",
            "replicas_ready": ready.get(name, 0),
        },
    )
    hit = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"choices": []}

    class AC:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):  # api 0.11.127: the proxy closes its client explicitly
            return None

        async def get(self, url, headers=None):  # the readiness probe before the call (api 0.11.143)
            return _Ready()

        async def post(self, url, json=None, headers=None):
            hit["url"] = url
            return Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", AC)
    assert (
        c.post("/v1/models/chat:invoke", json={"input": {"messages": [], "tier": "budget"}}, headers=H).status_code
        == 200
    )
    assert hit["url"].startswith("http://chat-cheap-predictor.models.svc.cluster.local")
    c.post("/v1/models/chat:invoke", json={"input": {"messages": []}}, headers=H)
    assert hit["url"].startswith("http://chat-spot-predictor.models.svc.cluster.local")  # the first ready variant
    ready.update({"chat-spot": 0})
    c.post("/v1/models/chat:invoke", json={"input": {"messages": []}}, headers=H)
    assert hit["url"].startswith(e["endpoint_host"])  # nothing ready: the first variant
    # the endpoint listing names the variants and their state; a variant object name resolves to the model
    monkeypatch.setattr(
        kube,
        "isvc",
        lambda n, ns, region="eu-north1": {
            "kind": "InferenceService",
            "metadata": {"name": n},
            "spec": {"predictor": {}},
        },
    )
    monkeypatch.setattr(catalog, "all_models", lambda: {"chat": e})
    listed = c.get("/v1/endpoints", headers=H).json()
    chat = next(x for x in listed if x["model"] == "chat")
    assert [v["name"] for v in chat["variants"]] == ["reserved", "spot", "cheap"] and chat["routing"]["field"] == "tier"
    assert appmod._endpoint_model("chat-cheap")["name"] == "chat"


def _world(monkeypatch, ready):
    """The chat endpoint with three variants on the hub, a live state per object (api 0.11.179 tests)."""
    monkeypatch.setattr(kube, "fleet", lambda: VARIANT_FLEET)
    e = catalog.normalise(models.to_entry(SPEC))
    monkeypatch.setattr(catalog, "get", lambda name: e if name == "chat" else None)
    monkeypatch.setattr(models, "cluster_id", lambda: "hub")
    monkeypatch.setattr(
        kube,
        "endpoint_status",
        lambda name, ns, region="eu-north1", nodes=1: {
            "status": "ready" if ready.get(name) else ready.get(f"{name}:status", "scaled-to-zero"),
            "replicas_ready": ready.get(name, 0),
        },
    )
    return e


def test_the_probe_and_the_request_go_to_the_same_variant(client, monkeypatch):
    """api 0.11.179: the sync probe used to resolve again with an empty input, so an explicit `tier` could probe one
    variant and post to another; now one resolution serves both, and the operation records the target."""
    c, _ = client
    _world(monkeypatch, {"chat": 0, "chat-spot": 2, "chat-cheap": 0})
    seen = {"get": [], "post": []}

    class Resp:
        status_code = 200

        def json(self):
            return {"choices": []}

    class AC:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None):
            seen["get"].append(url)
            return _Ready()

        async def post(self, url, json=None, headers=None):
            seen["post"].append(url)
            return Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", AC)
    r = c.post("/v1/models/chat:invoke", json={"input": {"messages": [], "tier": "budget"}}, headers=H)
    assert r.status_code == 200, r.text
    assert seen["get"][-1].startswith("http://chat-cheap-predictor") and seen["post"][-1].startswith(
        "http://chat-cheap-predictor"
    )
    assert r.json()["operation"]["target"] == {
        "cluster": "hub",
        "variant": "cheap",
        "id": "chat-cheap",
        "image": "registry.serverless2.local/docker/vllm/vllm-openai:v0.31.0-l40s",
        "gpu_class": "l40s",
        "observed": True,  # the API sent the one request there (api 0.11.186)
    }


def _measurements(monkeypatch, in_flight: dict, limit: int | None, pools: dict):
    """api 0.11.180: the target state is measured, never guessed: Knative's stable concurrency per revision
    (monitoring.instant), the replica's concurrency limit (the deployment), the pool's free GPUs and node room."""
    import monitoring

    def instant(q, region, timeout=4.0):
        # the value for `sum(...)`, the OLDEST contributor's scrape time for `min(timestamp(...))` (api 0.11.186); a
        # plain value in the table = a sample scraped 10 s ago, a (value, age) tuple carries its own age
        v = next((v for k, v in in_flight.items() if k in q), None)
        if v is None:
            return None
        value, age = v if isinstance(v, tuple) else (v, 10.0)
        return time.time() - age if q.startswith("min(timestamp(") else value

    monkeypatch.setattr(monitoring, "instant", instant)
    monkeypatch.setattr(appmod, "_replica_concurrency", lambda m, v, region: limit)
    monkeypatch.setattr(kube, "pool_usage", lambda cluster, pool: pools.get(pool, {"nodes_ready": None}))


def test_capacity_routing_takes_only_known_admitting_or_startable_targets(monkeypatch):
    e = _world(monkeypatch, {"chat": 2, "chat-spot": 0, "chat-cheap": 1})
    e = {**e, "routing": {**e["routing"], "default": "capacity"}}
    pools = {
        "h100-res-1x": {"nodes_ready": 2, "gpus_total": 2, "gpus_used": 2},
        "h100-spot-1x": {"nodes_ready": 0, "gpus_total": 0, "gpus_used": 0},
        "l40s-spot-1x": {"nodes_ready": 1, "gpus_total": 1, "gpus_used": 1},
    }
    # reserved: 2 ready x limit 4 = 8, 8 in flight -> full; cheap: 1 x 4 = 4, 1 in flight -> admitting
    _measurements(monkeypatch, {"chat-predictor": 8.0, "chat-cheap-predictor": 1.0}, 4, pools)
    assert appmod._variant_for(e, {}, "hub")["name"] == "cheap"
    st = appmod._target_state(e, models.variants(e, "hub")[0], "hub")
    assert st == {
        "status": "ready",
        "replicas_ready": 2,
        "admitting": False,
        "startable": None,
        "in_flight": 8.0,
        "in_flight_age_s": 10.0,
        "capacity": 8,
    }
    # a stale underlying sample (its scrape time, `timestamp()`, older than METRIC_STALE_S) at a FRESH query time is
    # unknown, never "free": admitting None, in_flight None, the age still reported (Root 20:05Z: the sum's own
    # timestamp is the evaluation time and would never show this)
    _measurements(monkeypatch, {"chat-predictor": (8.0, 400.0), "chat-cheap-predictor": 1.0}, 4, pools)
    st = appmod._target_state(e, models.variants(e, "hub")[0], "hub")
    assert st["admitting"] is None and st["in_flight"] is None and round(st["in_flight_age_s"]) == 400
    # a sample without any timestamp (no contributor answered timestamp()) is unknown too
    import monitoring

    monkeypatch.setattr(monitoring, "instant", lambda q, region, timeout=4.0: None if "timestamp(" in q else 1.0)
    st = appmod._target_state(e, models.variants(e, "hub")[0], "hub")
    assert st["admitting"] is None and st["in_flight"] is None and st["in_flight_age_s"] is None
    # no concurrency limit (rps scaling) or no measurement: admitting is UNKNOWN (None), never true
    _measurements(monkeypatch, {"chat-predictor": 8.0, "chat-cheap-predictor": 1.0}, None, pools)
    assert appmod._target_state(e, models.variants(e, "hub")[0], "hub")["admitting"] is None
    _measurements(monkeypatch, {}, 4, pools)
    assert appmod._target_state(e, models.variants(e, "hub")[0], "hub")["admitting"] is None
    # everything full or unknown: no known admitting target; the cold spot variant is startable only with room
    e2 = _world(monkeypatch, {"chat": 2, "chat-spot": 0, "chat-cheap": 0})
    e2 = {**e2, "routing": {**e2["routing"], "default": "capacity"}}
    _measurements(monkeypatch, {"chat-predictor": 8.0}, 4, pools)
    monkeypatch.setattr(
        kube, "pool_entry", lambda cluster, pool: {"max_nodes": 4} if pool == "h100-spot-1x" else {"max_nodes": 1}
    )
    assert appmod._variant_for(e2, {}, "hub")["name"] == "spot"  # 0 nodes of 4: room for one
    st = appmod._target_state(e2, models.variants(e2, "hub")[2], "hub")
    assert st["startable"] is False  # cheap: its only node's GPU is taken and the pool is at max_nodes
    monkeypatch.setattr(kube, "pool_usage", lambda cluster, pool: {"nodes_ready": None})
    assert appmod._target_state(e2, models.variants(e2, "hub")[1], "hub")["startable"] is None  # unreadable: unknown
    assert appmod._variant_for(e2, {}, "hub")["name"] == "reserved"  # nothing known: the first, the queue decides
    # a failed cold deployment is never startable; `priority` keeps the readiness-only choice
    e3 = _world(monkeypatch, {"chat": 0, "chat-spot": 0, "chat-cheap": 0, "chat:status": "error"})
    assert appmod._target_state(e3, models.variants(e3, "hub")[0], "hub")["startable"] is False
    assert appmod._variant_for(e3, {}, "hub")["name"] == "reserved"


def test_an_async_call_targets_the_resolved_variant_and_records_it(client, monkeypatch):
    c, fake = client
    _world(monkeypatch, {"chat": 0, "chat-spot": 3, "chat-cheap": 0})
    monkeypatch.setattr(models, "endpoint_domain", lambda region: "203.0.113.10.sslip.io")
    r = c.post("/v1/models/chat:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    assert r.status_code == 202, r.text
    job = fake.jobs[("tenant-demo", r.json()["id"])]
    env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["URL"].startswith("https://chat-spot-predictor.models.203.0.113.10.sslip.io/")
    assert env["READY_URL"].startswith("https://chat-spot-predictor.models.203.0.113.10.sslip.io/")
    import json as _json

    assert _json.loads(job["metadata"]["annotations"]["serverless2.nebius/target"]) == {
        "cluster": "hub",
        "gpu_class": "h100",
        "id": "chat-spot",
        "image": "registry.serverless2.local/docker/vllm/vllm-openai:v0.31.0",
        "variant": "spot",
    }
    op = c.get(f"/v1/operations/{r.json()['id']}", headers=H).json()
    assert op["target"]["variant"] == "spot" and op["target"]["id"] == "chat-spot"


def test_the_model_readback_lists_every_target(client, monkeypatch):
    c, _ = client
    e = _world(monkeypatch, {"chat": 1, "chat-spot": 0, "chat-cheap": 0})
    _measurements(
        monkeypatch,
        {"chat-predictor": 1.0},
        4,
        {
            "h100-res-1x": {"nodes_ready": 1, "gpus_total": 1, "gpus_used": 1},
            "h100-spot-1x": {"nodes_ready": 0, "gpus_total": 0, "gpus_used": 0},
            "l40s-spot-1x": {"nodes_ready": None},
        },
    )
    monkeypatch.setattr(
        kube, "pool_entry", lambda cluster, pool: {"gpu_class": "l40s" if "l40s" in pool else "h100", "max_nodes": 4}
    )
    monkeypatch.setattr(catalog, "all_models", lambda: {"chat": e})
    r = c.get("/v1/models/chat", headers=H)
    assert r.status_code == 200, r.text
    ts = r.json()["targets"]
    assert [t["variant"] for t in ts] == ["reserved", "spot", "cheap"] and all(t["cluster"] == "hub" for t in ts)
    by = {t["variant"]: t for t in ts}
    assert (
        by["reserved"]["admitting"] is True and by["reserved"]["in_flight"] == 1.0 and by["reserved"]["capacity"] == 4
    )
    assert by["reserved"]["startable"] is None and by["reserved"]["replicas"]["ready"] == 1
    assert by["cheap"]["image"].endswith("vllm-openai:v0.31.0-l40s") and by["cheap"]["gpu_class"] == "l40s"
    assert by["cheap"]["startable"] is None and by["cheap"]["admitting"] is None  # its pool is unreadable: unknown
    assert by["spot"]["startable"] is True and by["spot"]["admitting"] is None and by["spot"]["pool"] == "h100-spot-1x"
    assert "spec" not in by["spot"] and "env" not in by["spot"]
    # validation: `capacity` is a routing default
    ok = models.to_entry(models.validate({**SPEC, "routing": {**SPEC["routing"], "default": "capacity"}}))
    assert ok["routing"]["default"] == "capacity"


def _fallback_world(monkeypatch, ready, max_switches=1, regions=False):
    e = _world(monkeypatch, ready)
    e = {
        **e,
        "routing": {**e["routing"], "fallback": {"max_switches": max_switches, "cold_wait_s": 5, "regions": regions}},
    }
    monkeypatch.setattr(catalog, "get", lambda name: e if name == "chat" else None)
    return e


def test_sync_pre_send_fallback_moves_to_the_next_target_only_before_sending(client, monkeypatch):
    """api 0.11.182: the first target is probed for cold_wait_s; not ready, the next declared target is probed; the one
    inference request goes to the target that answered; a refusal ends the call; max_switches bounds the walk."""
    c, _ = client
    _fallback_world(monkeypatch, {"chat": 0, "chat-spot": 0, "chat-cheap": 0}, max_switches=2)
    seen = {"get": [], "post": []}

    class Resp:
        status_code = 200

        def json(self):
            return {"choices": []}

    class NotYet:
        status_code = 503
        headers = {}
        content = b""
        text = ""

        def json(self):
            return {}

    class AC:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aclose(self):
            return None

        async def get(self, url, headers=None):
            seen["get"].append(url)
            return _Ready() if "chat-cheap" in url else NotYet()

        async def post(self, url, json=None, headers=None):
            seen["post"].append(url)
            return Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", AC)
    monkeypatch.setattr(appmod, "READY_PROBE_PAUSE_S", 0)
    monkeypatch.setattr(appmod.time, "time", _Clock(step=3.0))  # every probe costs 3 s of wall clock
    r = c.post("/v1/models/chat:invoke", json={"input": {"messages": []}}, headers=H)
    assert r.status_code == 200, r.text
    assert len(seen["post"]) == 1 and seen["post"][0].startswith(
        "http://chat-cheap-predictor"
    )  # one request, third target
    assert any("chat-predictor" in u for u in seen["get"]) and any("chat-spot-predictor" in u for u in seen["get"])
    op = r.json()["operation"]
    assert op["target"]["variant"] == "cheap" and op["target"]["candidates"] == ["chat", "chat-spot", "chat-cheap"]
    # max_switches 0: the resolved target only; not ready within the budget -> 503, nothing sent
    _fallback_world(monkeypatch, {"chat": 0, "chat-spot": 0, "chat-cheap": 0}, max_switches=0)
    seen["post"].clear()
    r = c.post("/v1/models/chat:invoke", json={"input": {"messages": []}, "timeout_s": 8}, headers=H)
    assert r.status_code == 503 and r.headers.get("x-serverless2-inference") == "not-sent" and seen["post"] == []


class _Clock:
    """A monotonic test clock: every read advances by `step` seconds (the readiness loop measures its budget by it)."""

    def __init__(self, step: float):
        import time as _t

        self.now, self.step = _t.time(), step

    def __call__(self):
        self.now += self.step
        return self.now


def test_async_call_carries_the_ordered_targets_for_the_caller(client, monkeypatch):
    c, fake = client
    _fallback_world(monkeypatch, {"chat": 0, "chat-spot": 0, "chat-cheap": 0}, max_switches=1)
    monkeypatch.setattr(models, "endpoint_domain", lambda region: "203.0.113.10.sslip.io")
    r = c.post("/v1/models/chat:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    assert r.status_code == 202, r.text
    job = fake.jobs[("tenant-demo", r.json()["id"])]
    env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    import json as _json

    targets = _json.loads(env["TARGETS"])
    assert [t["id"] for t in targets] == ["chat", "chat-spot"] and env["TARGET_COLD_WAIT"] == "5"
    assert targets[0]["url"] == env["URL"] and targets[0]["ready_url"] == env["READY_URL"]
    assert targets[1]["url"].startswith("https://chat-spot-predictor.models.203.0.113.10.sslip.io/")
    rec = _json.loads(job["metadata"]["annotations"]["serverless2.nebius/target"])
    assert rec["id"] == "chat" and [c["id"] for c in rec["candidates"]] == ["chat", "chat-spot"]
    # api 0.11.186: every candidate's identity is recorded at submission (exact image, class, cluster), immutable
    assert rec["candidates"][1] == {
        "cluster": "hub",
        "variant": "spot",
        "id": "chat-spot",
        "image": "registry.serverless2.local/docker/vllm/vllm-openai:v0.31.0",
        "gpu_class": "h100",
    }
    op_id = r.json()["id"]
    op = c.get(f"/v1/operations/{op_id}", headers=H).json()
    # before the caller reports: the planned first target, never an execution
    assert op["target"]["id"] == "chat" and op["target"]["observed"] is False
    assert op["target"]["candidates"] == ["chat", "chat-spot"]
    # the caller fell to the second target (call.sh out/target.json, forwarded by the uploader's record): the
    # operation reports THAT candidate's recorded identity as observed, not the first, not a live lookup
    import artifacts

    monkeypatch.setattr(
        artifacts,
        "attempt_records",
        lambda ns, op, region, prefix: [
            {
                "pod": f"{op_id}-abc12",
                "operation": op_id,
                "status": "succeeded",
                "target": {"id": "chat-spot", "url": "https://x/", "index": 2, "of": 2, "probes": 3, "waited_s": 9},
            }
        ],
    )
    op = c.get(f"/v1/operations/{op_id}", headers=H).json()
    assert op["target"] == {
        "cluster": "hub",
        "variant": "spot",
        "id": "chat-spot",
        "image": "registry.serverless2.local/docker/vllm/vllm-openai:v0.31.0",
        "gpu_class": "h100",
        "observed": True,
        "candidates": ["chat", "chat-spot"],
    }
    # without routing.fallback: no TARGETS, one target (unchanged)
    _world(monkeypatch, {"chat": 0, "chat-spot": 0, "chat-cheap": 0})
    r = c.post("/v1/models/chat:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    job = fake.jobs[("tenant-demo", r.json()["id"])]
    env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "TARGETS" not in env


def test_fallback_validation_and_cross_deployment_candidates(monkeypatch):
    e = _fallback_world(monkeypatch, {"chat": 0, "chat-spot": 0, "chat-cheap": 0}, max_switches=7, regions=True)
    # a second deployment in another region the API reaches: its variants follow the first deployment's, in order
    e2 = {
        **e,
        "regions": ["hub", "south"],
        "deployments": {
            **e["deployments"],
            "eu-south1": {"variants": [{"name": "rtx", "pool": "rtx6000-ondemand-1x", "min": 0, "max": 2}]},
        },
    }
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])
    monkeypatch.setattr(kube, "cluster_region", lambda c: {"hub": "eu-north1", "south": "eu-south1"}.get(c, c))
    south = {"region": "south", "pool": "rtx6000-ondemand-1x", "gpu_class": "rtx-pro-6000", "gpus_per_node": 1}
    monkeypatch.setattr(
        kube, "fleet", lambda: {**VARIANT_FLEET, "pools": {**VARIANT_FLEET["pools"], "south-rtx": south}}
    )
    cands = appmod._call_targets(e2, "hub", {}, region_given=False)
    # api 0.11.185: after the resolved target the walk follows the operator's class order (gpu.classes: h100, l40s;
    # the RTX pool's class is undeclared -> last), declared order within a class; never region-major
    assert [(c, v["object"]) for c, v in cands] == [
        ("hub", "chat"),
        ("hub", "chat-spot"),
        ("hub", "chat-cheap"),
        ("south", "chat"),  # a deployment's first variant keeps the model's own object name on its cluster
    ]
    e3 = {**e2, "gpu_classes": ["h100", "rtx-pro-6000", "l40s"]}  # the operator ranks RTX above L40S: RTX south first
    assert [(c, v["object"]) for c, v in appmod._call_targets(e3, "hub", {}, region_given=False)] == [
        ("hub", "chat"),
        ("hub", "chat-spot"),
        ("south", "chat"),
        ("hub", "chat-cheap"),
    ]
    assert [v["object"] for _, v in appmod._call_targets(e2, "hub", {}, region_given=True)] == [
        "chat",
        "chat-spot",
        "chat-cheap",
    ]  # an explicit region: that deployment only
    # api 0.11.186: the RESOLVED first target follows the operator's order over every deployment too (capacity
    # over all of them: the RTX south target is the first KNOWN to admit) and a call from the control API with no
    # caller region is not refused with "give region": _deployment_of stands in with the order's first
    e4 = {**e3, "routing": {**e3["routing"], "default": "capacity"}}
    _measurements(
        monkeypatch,
        {"chat-predictor": 8.0, "chat-spot-predictor": 8.0, "chat-cheap-predictor": 8.0},
        4,
        {"rtx6000-ondemand-1x": {"nodes_ready": 1, "gpus_total": 1, "gpus_used": 0}},
    )
    ready = {"chat": 2, "chat-spot": 2, "chat-cheap": 2}  # the hub's three are full (8 in flight of 8)
    monkeypatch.setattr(  # south's deployment (its first variant keeps the object name `chat`) is cold: startable
        kube,
        "endpoint_status",
        lambda n, ns, region="eu-north1", nodes=1: {"status": "scaled-to-zero", "replicas_ready": 0}
        if region == "eu-south1"
        else {"status": "ready", "replicas_ready": ready.get(n, 0)},
    )
    monkeypatch.setattr(appmod, "REGION", "control")
    monkeypatch.setattr(kube, "regions", lambda: ["control", "eu-north1", "eu-south1"])
    monkeypatch.setattr(
        kube, "cluster_region", lambda c: {"hub": "eu-north1", "south": "eu-south1", "control": "control"}.get(c, c)
    )
    assert appmod._deployment_of(e4, None) == ("hub", "eu-north1")  # h100 ranks first in e4's gpu.classes
    first = appmod._call_targets(e4, "hub", {}, region_given=False)[0]
    assert (first[0], first[1]["object"]) == ("south", "chat")  # the only target KNOWN to be startable
    e5 = {**e4, "gpu_classes": ["rtx-pro-6000", "h100", "l40s"]}
    assert appmod._deployment_of(e5, None) == ("south", "eu-south1")
    assert [(c, v["object"]) for c, v in appmod._call_targets(e5, "hub", {}, region_given=False)] == [
        ("south", "chat"),
        ("hub", "chat"),
        ("hub", "chat-spot"),
        ("hub", "chat-cheap"),
    ]
    assert appmod._deployment_of(e5, "hub") == ("hub", "eu-north1")  # an explicit region stays authoritative
    no_fb = {**e5, "routing": {k: v for k, v in e5["routing"].items() if k != "fallback"}}
    with pytest.raises(HTTPException) as x:
        appmod._deployment_of(no_fb, None)  # without declared cross-region fallback: as before
    assert x.value.status_code == 400 and "give region" in x.value.detail
    with pytest.raises(HTTPException) as x:
        models.validate({**SPEC, "routing": {**SPEC["routing"], "fallback": {"max_switches": 9}}})
    assert x.value.status_code == 400 and "routing.fallback" in x.value.detail
    ok = models.to_entry(models.validate({**SPEC, "routing": {**SPEC["routing"], "fallback": {"max_switches": 2}}}))
    assert ok["routing"]["fallback"] == {"max_switches": 2, "cold_wait_s": 120, "regions": False}


def test_control_api_resolves_a_no_region_call_over_every_deployment(client, monkeypatch):
    """api 0.11.186 (Root 20:05Z): on the control API (REGION=control) a call with no region to a model deployed in
    north and south with routing.fallback.regions is not refused: it resolves over every deployment in the operator's
    class order; the asynchronous caller Job is created in the resolved target's region with the global walk, the
    synchronous call probes and posts to the resolved target through its gateway."""
    c, fake = client
    e = _fallback_world(monkeypatch, {"chat": 0, "chat-spot": 0, "chat-cheap": 0}, max_switches=3, regions=True)
    e2 = {
        **e,
        "regions": ["hub", "south"],
        "gpu_classes": ["rtx-pro-6000", "h100", "l40s"],
        "deployments": {
            **e["deployments"],
            "eu-south1": {"variants": [{"name": "rtx", "pool": "rtx6000-ondemand-1x", "min": 0, "max": 2}]},
        },
    }
    south = {"region": "south", "pool": "rtx6000-ondemand-1x", "gpu_class": "rtx-pro-6000", "gpus_per_node": 1}
    monkeypatch.setattr(
        kube, "fleet", lambda: {**VARIANT_FLEET, "pools": {**VARIANT_FLEET["pools"], "south-rtx": south}}
    )
    monkeypatch.setattr(catalog, "get", lambda name: e2 if name == "chat" else None)
    monkeypatch.setattr(appmod, "REGION", "control")
    monkeypatch.setattr(kube, "regions", lambda: ["control", "eu-north1", "eu-south1"])
    monkeypatch.setattr(
        kube, "cluster_region", lambda x: {"hub": "eu-north1", "south": "eu-south1", "control": "control"}.get(x, x)
    )
    monkeypatch.setattr(models, "endpoint_domain", lambda region: f"{region}.example")
    r = c.post("/v1/models/chat:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    assert r.status_code == 202, r.text
    import json as _json

    job = fake.jobs[("tenant-demo", r.json()["id"])]
    env = {x["name"]: x.get("value") for x in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    rec = _json.loads(job["metadata"]["annotations"]["serverless2.nebius/target"])
    assert rec["cluster"] == "south" and rec["id"] == "chat" and rec["gpu_class"] == "rtx-pro-6000"
    assert [x["id"] for x in _json.loads(env["TARGETS"])] == ["chat", "chat", "chat-spot", "chat-cheap"]
    assert env["URL"].startswith("https://chat-predictor.models.eu-south1.example/")
    assert r.json()["region"] == "eu-south1"
    # an explicit region keeps the caller in that deployment
    r = c.post("/v1/models/chat:invoke", json={"mode": "async", "region": "hub", "input": {"messages": []}}, headers=H)
    assert r.status_code == 202, r.text
    rec = _json.loads(
        fake.jobs[("tenant-demo", r.json()["id"])]["metadata"]["annotations"]["serverless2.nebius/target"]
    )
    assert rec["cluster"] == "hub"


def test_require_converged_fences_every_candidate_and_takes_the_first_converged(client, monkeypatch):
    """api 0.11.188 (Root 20:49Z): with execution.require_converged the convergence fence is judged on EVERY declared
    target that could receive the one request; a candidate whose rollout is not converged is left out of the walk (an
    old runtime image never serves by falling to it), the first that passes becomes the call's target; none passing
    is the first's 409, nothing created."""
    c, fake = client
    _fallback_world(monkeypatch, {"chat": 0, "chat-spot": 0, "chat-cheap": 0}, max_switches=2)
    monkeypatch.setattr(models, "endpoint_domain", lambda region: "203.0.113.10.sslip.io")
    converged = {"chat": False, "chat-spot": True, "chat-cheap": True}

    def status(n, ns, region="eu-north1", nodes=1):
        ok = converged.get(n, True)
        return {
            "status": "ready" if ok else "deploying",
            "replicas_ready": 0,
            "rollout": {
                "latest_created": "r-2",
                "latest_ready": "r-2" if ok else "r-1",
                "latest_rolled_out": "r-2" if ok else "r-1",
                "serving_images": [],
                "converged": ok,
                "reason": None if ok else "revisions differ (created r-2, ready r-1, rolled out r-1)",
            },
        }

    monkeypatch.setattr(kube, "endpoint_status", status)
    body = {"mode": "async", "input": {"messages": []}, "execution": {"require_converged": True}}
    r = c.post("/v1/models/chat:invoke", json=body, headers=H)
    assert r.status_code == 202, r.text
    import json as _json

    job = fake.jobs[("tenant-demo", r.json()["id"])]
    env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    rec = _json.loads(job["metadata"]["annotations"]["serverless2.nebius/target"])
    assert rec["id"] == "chat-spot"  # the first CONVERGED candidate, not the resolved first
    assert [t["id"] for t in _json.loads(env["TARGETS"])] == ["chat-spot", "chat-cheap"]  # the unconverged one is out
    assert job["metadata"]["annotations"]["serverless2.nebius/endpoint-revision"] == "r-2"
    # the same object name in two deployments (a deployment's first variant keeps the model's id): the fence is per
    # (cluster, object), never per object name (api 0.11.189): `chat` diverged on the hub, converged on south -> south
    e2 = {
        **catalog.get("chat"),
        "regions": ["hub", "south"],
        "gpu_classes": ["rtx-pro-6000", "h100", "l40s"],
        "deployments": {
            **catalog.get("chat")["deployments"],
            "eu-south1": {"variants": [{"name": "rtx", "pool": "rtx6000-ondemand-1x", "min": 0, "max": 2}]},
        },
        "routing": {
            **catalog.get("chat")["routing"],
            "fallback": {"max_switches": 3, "cold_wait_s": 5, "regions": True},
        },
    }
    south_pool = {"region": "south", "pool": "rtx6000-ondemand-1x", "gpu_class": "rtx-pro-6000", "gpus_per_node": 1}
    monkeypatch.setattr(
        kube, "fleet", lambda: {**VARIANT_FLEET, "pools": {**VARIANT_FLEET["pools"], "south-rtx": south_pool}}
    )
    monkeypatch.setattr(catalog, "get", lambda name: e2 if name == "chat" else None)
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])
    monkeypatch.setattr(kube, "cluster_region", lambda x: {"hub": "eu-north1", "south": "eu-south1"}.get(x, x))
    monkeypatch.setattr(models, "endpoint_domain", lambda region: f"{region}.example")

    def by_region(n, ns, region="eu-north1", nodes=1):
        return (
            status(n, ns, region, nodes) if region != "eu-south1" else {**status("south-chat", ns), "status": "ready"}
        )

    converged.update({"chat": False, "chat-spot": False, "chat-cheap": False})  # the hub's three diverged
    monkeypatch.setattr(kube, "endpoint_status", by_region)  # south's `chat` converged
    r = c.post("/v1/models/chat:invoke", json=body, headers=H)
    assert r.status_code == 202, r.text
    rec = _json.loads(
        fake.jobs[("tenant-demo", r.json()["id"])]["metadata"]["annotations"]["serverless2.nebius/target"]
    )
    assert (rec["cluster"], rec["id"]) == ("south", "chat")
    monkeypatch.setattr(kube, "endpoint_status", status)
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1"])
    e1 = {**e2, "regions": ["hub"], "deployments": {k: v for k, v in e2["deployments"].items() if k != "eu-south1"}}
    monkeypatch.setattr(catalog, "get", lambda name: e1 if name == "chat" else None)
    # every candidate diverged: the first's refusal, nothing created
    converged.update({"chat-spot": False, "chat-cheap": False})
    before = len(fake.jobs)
    r = c.post("/v1/models/chat:invoke", json=body, headers=H)
    assert (
        r.status_code == 409
        and r.json()["code"] == "rollout_pending"
        and r.headers["x-serverless2-admission"] == "refused"
    )
    assert len(fake.jobs) == before
    # without the option nothing is fenced: the resolved first target as before
    r = c.post("/v1/models/chat:invoke", json={"mode": "async", "input": {"messages": []}}, headers=H)
    assert (
        r.status_code == 202
        and _json.loads(
            fake.jobs[("tenant-demo", r.json()["id"])]["metadata"]["annotations"]["serverless2.nebius/target"]
        )["id"]
        == "chat"
    )
