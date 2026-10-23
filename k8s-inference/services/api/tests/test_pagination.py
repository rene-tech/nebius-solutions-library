"""Every list that grows with use is paged (api 0.11.124, docs/API.md "Pagination"): `limit` + `cursor`, headers
`X-Next-Cursor` and `X-Total-Count`, the expensive per-item work on the page only; the tenant-wide Job listing behind
GET /v1/operations is reused for a few seconds; the fleet operator reads the operations in flight across tenants."""

import copy
import time

import pytest
from fastapi import HTTPException, Response

import app as appmod
import catalog
import config as cfg
import jobs
import kube
import store
from test_api import ADMIN, H, admin_keys, client, cluster, pod, store_sync  # noqa: F401 - pytest fixture registration


def test_page_helper_orders_cuts_and_issues_cursors():
    r = Response()
    items = [{"k": f"item-{i:02d}"} for i in range(7)][::-1]  # unsorted on purpose
    first = appmod._page(r, items, lambda x: x["k"], None, 3)
    assert [x["k"] for x in first] == ["item-00", "item-01", "item-02"]
    assert r.headers["X-Total-Count"] == "7" and r.headers.get("X-Next-Cursor")
    r2 = Response()
    second = appmod._page(r2, items, lambda x: x["k"], r.headers["X-Next-Cursor"], 3)
    assert [x["k"] for x in second] == ["item-03", "item-04", "item-05"] and r2.headers.get("X-Next-Cursor")
    r3 = Response()
    third = appmod._page(r3, items, lambda x: x["k"], r2.headers["X-Next-Cursor"], 3)
    assert [x["k"] for x in third] == ["item-06"] and "X-Next-Cursor" not in r3.headers
    assert r3.headers["X-Total-Count"] == "7"  # the total is the whole list, not the rest
    with pytest.raises(HTTPException) as e:
        appmod._page(Response(), items, lambda x: x["k"], "not-a-cursor", 3)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException):
        appmod._decode_key_cursor(appmod._encode_cursor("2026-01-01T00:00:00Z", "op-x"))  # the other cursor kind
    # the descending complement orders newest first with the same ascending helper
    keys = ["2026-10-17T10:00:00Z|op-b", "2026-10-17T09:00:00Z|op-a", "2026-10-17T10:00:00Z|op-a"]
    assert sorted(keys, key=appmod._desc_key) == [
        "2026-10-17T10:00:00Z|op-b",
        "2026-10-17T10:00:00Z|op-a",
        "2026-10-17T09:00:00Z|op-a",
    ]


def test_models_and_endpoints_are_paged_and_only_the_page_is_read_live(client, monkeypatch):
    c, fake = client
    base = catalog.get("llm-example")
    many = {
        f"m-{i:02d}": {**base, "name": f"m-{i:02d}", "k8s_name": f"m-{i:02d}", "regions": ["eu-north1"]}
        for i in range(7)
    }
    monkeypatch.setattr(catalog, "all_models", lambda: many)
    reads = []
    monkeypatch.setattr(
        kube,
        "endpoint_status",
        lambda n, ns, region="eu-north1", nodes=1: reads.append(n) or {"status": "scaled-to-zero", "replicas_ready": 0},
    )
    r = c.get("/v1/models?limit=3", headers=H)
    assert r.status_code == 200 and [m["id"] for m in r.json()] == ["m-00", "m-01", "m-02"]
    assert r.headers["X-Total-Count"] == "7" and r.headers.get("X-Next-Cursor")
    assert sorted(set(reads)) == ["m-00", "m-01", "m-02"]  # no live read for the models off the page
    r2 = c.get(f"/v1/models?limit=3&cursor={r.headers['X-Next-Cursor']}", headers=H)
    assert [m["id"] for m in r2.json()] == ["m-03", "m-04", "m-05"]
    r3 = c.get(f"/v1/models?limit=3&cursor={r2.headers['X-Next-Cursor']}", headers=H)
    assert [m["id"] for m in r3.json()] == ["m-06"] and "X-Next-Cursor" not in r3.headers
    assert c.get("/v1/models?limit=0", headers=H).status_code == 422
    assert c.get("/v1/models?limit=501", headers=H).status_code == 422
    # endpoints: one entry per (model, cluster), paged the same way, the live state for the page only
    isvc = {"metadata": {"labels": {}}, "spec": {"predictor": {"maxReplicas": 2}}}
    monkeypatch.setattr(kube, "isvc", lambda name, ns, region="eu-north1": isvc)
    for mid in many:
        fake.custom[("models", "inferenceservices", mid)] = isvc
    monkeypatch.setattr(kube, "endpoint_pods", lambda *a, **k: [])
    reads.clear()
    r = c.get("/v1/endpoints?limit=2", headers=H)
    assert r.status_code == 200, r.text
    assert [(e["id"], e["cluster"]) for e in r.json()] == [("m-00", "eu-north1"), ("m-01", "eu-north1")]
    assert r.headers["X-Total-Count"] == "7" and sorted(set(reads)) == ["m-00", "m-01"]
    last = c.get(f"/v1/endpoints?limit=5&cursor={r.headers['X-Next-Cursor']}", headers=H)
    assert [e["id"] for e in last.json()] == ["m-02", "m-03", "m-04", "m-05", "m-06"]
    assert "X-Next-Cursor" not in last.headers
    # without `limit` the whole list comes back as before 0.11.124 (an unpaged client keeps its answer), counted
    whole = c.get("/v1/endpoints", headers=H)
    assert len(whole.json()) == 7 and whole.headers["X-Total-Count"] == "7" and "X-Next-Cursor" not in whole.headers
    assert len(c.get("/v1/models", headers=H).json()) == 7


def test_operations_listing_reports_the_total_and_is_served_from_the_store(client, monkeypatch):
    c, fake = client
    body = {"input": {"nsteps": "1000", "input_prefix": "s3://serverless2-demo-eu-north1/inputs/x"}}
    ids = [
        c.post("/v1/models/batch-example:invoke", json=body, headers={**H, "Idempotency-Key": f"pg-{i}"}).json()["id"]
        for i in range(3)
    ]
    listed = []
    original = fake.list_namespaced_job
    monkeypatch.setattr(fake, "list_namespaced_job", lambda *a, **k: listed.append(k) or original(*a, **k))
    r = c.get("/v1/operations?limit=2", headers=H)
    assert r.status_code == 200 and len(r.json()) == 2 and r.headers["X-Total-Count"] == "3"
    assert r.headers.get("X-Next-Cursor")
    r2 = c.get(f"/v1/operations?limit=2&cursor={r.headers['X-Next-Cursor']}", headers=H)
    assert len(r2.json()) == 1 and r2.headers["X-Total-Count"] == "3" and "X-Next-Cursor" not in r2.headers
    assert {o["id"] for o in r.json()} | {o["id"] for o in r2.json()} == set(ids)
    assert listed  # the store is not synced (no lifespan here): every page lists directly, as before
    store_sync(fake)
    listed.clear()
    r = c.get("/v1/operations?limit=2", headers=H)
    r2 = c.get(f"/v1/operations?limit=2&cursor={r.headers['X-Next-Cursor']}", headers=H)
    assert {o["id"] for o in r.json()} | {o["id"] for o in r2.json()} == set(ids) and listed == []  # from memory
    # the store's objects are shared: a listing entry is a shallow copy carrying its own `_` fields, the object
    # itself is never changed by a request
    first, _ = jobs.list_ops_all("tenant-demo", "demo", None)
    held = store.get("eu-north1", "tenant-demo", "jobs", first[0]["metadata"]["name"])
    assert first[0] is not held and first[0]["metadata"] is held["metadata"] and "_pods" not in held
    assert first[0]["_pods"] == [] and isinstance(first, jobs.Listing) and first.generation > 0
    # a Job this replica creates shows on its next listing at once (store.observe), before any watch event
    created = c.post("/v1/models/batch-example:invoke", json=body, headers={**H, "Idempotency-Key": "pg-9"})
    assert created.status_code == 202 and created.json()["id"] in {
        j["metadata"]["name"] for j in store.items("eu-north1", "tenant-demo", "jobs")
    }
    r = c.get("/v1/operations?limit=10", headers=H)
    assert r.headers["X-Total-Count"] == "4" and listed == []
    # a cancellation too: deleted on the manager (never started) -> gone from the store now
    assert c.post(f"/v1/operations/{created.json()['id']}:cancel", headers=H).status_code == 200
    assert created.json()["id"] not in {j["metadata"]["name"] for j in store.items("eu-north1", "tenant-demo", "jobs")}


def test_json_items_reads_raw_json_or_preloaded_items(monkeypatch):
    class Raw:
        data = b'{"items": [{"metadata": {"name": "a"}}]}'

    class Pre:
        items = [{"metadata": {"name": "b"}}]

    monkeypatch.setattr(jobs, "retry", lambda fn, *a, **k: fn())
    assert jobs._json_items(lambda: Raw()) == [{"metadata": {"name": "a"}}]
    assert jobs._json_items(lambda: Pre()) == [{"metadata": {"name": "b"}}]


def test_keys_and_bindings_are_paged(client, admin_keys, monkeypatch):
    c, fake = client
    import execution

    rows = [
        {"ref": f"b-{i}", "operation": None, "keys": [], "updated_at": None, "owner": "o", "revision": "1"}
        for i in range(5)
    ]
    monkeypatch.setattr(execution, "list_bindings", lambda ns, owner=None, admin=False, region="eu-north1": list(rows))
    r = c.get("/v1/bindings?limit=2", headers=H)
    assert [b["ref"] for b in r.json()] == ["b-0", "b-1"] and r.headers["X-Total-Count"] == "5"
    r2 = c.get(f"/v1/bindings?limit=2&cursor={r.headers['X-Next-Cursor']}", headers=H)
    assert [b["ref"] for b in r2.json()] == ["b-2", "b-3"]
    import keys

    fake_keys = [
        {"key_alias": f"demo-k{i}", "spend": 0.0, "max_budget": 1.0, "blocked": False, "metadata": {"tenant": "demo"}}
        for i in range(4)
    ]
    monkeypatch.setattr(keys, "list_tenant", lambda tenant: list(fake_keys))
    monkeypatch.setattr(appmod, "_key_public", lambda k, tenant: {"alias": k["key_alias"]})
    r = c.get("/v1/keys?limit=3", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert [k["alias"] for k in r.json()] == ["demo-k0", "demo-k1", "demo-k2"] and r.headers["X-Total-Count"] == "4"
    r2 = c.get(f"/v1/keys?limit=3&cursor={r.headers['X-Next-Cursor']}", headers=ADMIN)
    assert [k["alias"] for k in r2.json()] == ["demo-k3"] and "X-Next-Cursor" not in r2.headers


def test_fleet_operations_lists_every_tenants_in_flight_work_for_the_fleet_operator(client, monkeypatch):
    import auth
    from test_api import KEY_INFO

    c, fake = client
    fleet_info = {**KEY_INFO, "metadata": {**(KEY_INFO.get("metadata") or {}), "tenant": "platform", "role": "fleet"}}

    async def info(key):
        return copy.deepcopy(fleet_info if key == "sk-fleet" else KEY_INFO)

    monkeypatch.setattr(auth, "key_info", info)
    monkeypatch.setattr(appmod, "_fleet_only", lambda: None)
    L = "serverless2.nebius"

    def job(name, tenant, created, *, active=0, start=None, successor=None, cancelled=None, done=False, mirror=False):
        ann = {f"{L}/key": "k" * 64, f"{L}/owner": "owner-1"}
        if successor:
            ann[f"{L}/successor"] = successor
        if cancelled:
            ann[f"{L}/cancelled"] = "true"
        if mirror:
            ann["kueue.x-k8s.io/prebuilt-workload-name"] = "x"
        return {
            "metadata": {
                "name": name,
                "namespace": f"tenant-{tenant}",
                "creationTimestamp": created,
                "labels": {f"{L}/tenant": tenant, f"{L}/model": "gromacs", f"{L}/mode": "run"},
                "annotations": ann,
            },
            "status": {
                "active": active,
                "startTime": start,
                "conditions": [{"type": "Complete", "status": "True"}] if done else [],
            },
        }

    run = job("op-run", "rene", "2026-10-17T05:55:00Z", active=1, start="2026-10-17T05:55:30Z")
    run["metadata"]["labels"][f"{L}/gpu-class"] = "h200"
    run["metadata"]["annotations"][f"{L}/nodes"] = "2"
    run["spec"] = {
        "template": {
            "spec": {
                "nodeSelector": {f"{L}/pool": "h200"},
                "containers": [{"name": "main", "resources": {"limits": {"nvidia.com/gpu": "8"}}}],
            }
        }
    }
    listed = [
        run,
        job("op-queued", "lynx", "2026-10-17T06:00:00Z", active=1),
        job("op-old", "system", "2026-10-16T00:00:00Z", active=0, successor="op-old-r1"),
        job("op-cancel", "system", "2026-10-17T06:30:00Z", active=1, cancelled=True),
        job("op-done", "system", "2026-10-17T07:00:00Z", done=True),
        {
            **job("op-failed", "lynx", "2026-10-17T07:10:00Z"),
            "status": {"conditions": [{"type": "Failed", "status": "True"}]},
        },
        {
            **job("op-cancelled", "rene", "2026-10-17T07:20:00Z", cancelled=True),
            "status": {"conditions": [{"type": "Failed", "status": "True"}]},
        },
    ]
    monkeypatch.setattr(
        jobs, "is_mirror", lambda j: "kueue.x-k8s.io/prebuilt-workload-name" in (j["metadata"].get("annotations") or {})
    )
    doc = dict(kube.fleet())
    doc["tenants"] = {"rene": {}, "lynx": {}, "system": {}, "empty": {}}
    doc["pools"] = {**(doc.get("pools") or {}), "h200": {"platform": "gpu-h200-sxm", "preset": "8gpu-128vcpu-1600gb"}}
    monkeypatch.setattr(kube, "fleet", lambda: doc)
    monkeypatch.setattr(
        jobs,
        "_json_items",
        lambda fn, ns, **kw: [copy.deepcopy(j) for j in listed if j["metadata"]["namespace"] == ns],
    )
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1"])
    assert jobs.tenant_namespaces() == ["tenant-empty", "tenant-lynx", "tenant-rene", "tenant-system"]
    jobs._fleet_ops_cache["until"] = 0.0
    assert c.get("/v1/fleet/operations", headers=H).status_code == 403  # a tenant key is no fleet operator
    # active (default): the work in flight only, newest first; terminal Jobs and superseded originals absent
    r = c.get("/v1/fleet/operations?limit=2", headers={"Authorization": "Bearer sk-fleet"})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [x["id"] for x in rows] == ["op-cancel", "op-queued"]
    assert r.headers["X-Total-Count"] == "3" and r.headers.get("X-Next-Cursor")
    r2 = c.get(
        f"/v1/fleet/operations?limit=2&cursor={r.headers['X-Next-Cursor']}",
        headers={"Authorization": "Bearer sk-fleet"},
    )
    assert [x["id"] for x in r2.json()] == ["op-run"] and "X-Next-Cursor" not in r2.headers
    by = {x["id"]: x for x in rows + r2.json()}
    assert (
        by["op-run"]["status"] == "RUNNING" and by["op-run"]["tenant"] == "rene" and by["op-run"]["model"] == "gromacs"
    )
    assert by["op-queued"]["status"] == "QUEUED" and by["op-cancel"]["status"] == "CANCELLED"
    assert by["op-run"]["owner"] == "owner-1" and "input" not in by["op-run"] and "result_ref" not in by["op-run"]
    # the compute shape (console Platform column): class and GPUs from the pod template, the pinned pool's instance type
    # from the fleet document; a Job without a pin or GPUs carries nulls, never a guess
    assert {k: by["op-run"][k] for k in ("gpu_class", "gpus", "pool", "instance_type")} == {
        "gpu_class": "h200",
        "gpus": 16,
        "pool": "h200",
        "instance_type": "gpu-h200-sxm/8gpu-128vcpu-1600gb",
    }
    assert {k: by["op-queued"][k] for k in ("gpu_class", "gpus", "pool", "instance_type")} == {
        "gpu_class": None,
        "gpus": None,
        "pool": None,
        "instance_type": None,
    }
    # every operation Job, terminal ones included, the same field set; newest first
    all_ = c.get("/v1/fleet/operations?active=false", headers={"Authorization": "Bearer sk-fleet"})
    assert all_.status_code == 200 and all_.headers["X-Total-Count"] == "7"
    by_all = {x["id"]: x for x in all_.json()}
    assert [x["id"] for x in all_.json()] == [
        "op-cancelled",
        "op-failed",
        "op-done",
        "op-cancel",
        "op-queued",
        "op-run",
        "op-old",
    ]
    assert by_all["op-done"]["status"] == "SUCCEEDED" and by_all["op-failed"]["status"] == "FAILED"
    assert by_all["op-cancelled"]["status"] == "CANCELLED" and by_all["op-old"]["status"] == "SUPERSEDED"
    assert set(by_all["op-done"]) == set(by["op-run"])  # identical projection: no extra field for a finished run
    for forbidden in ("input", "result_ref", "logs_url", "attempts", "execution", "checkpoint_ref", "key"):
        assert forbidden not in by_all["op-done"]
    # filters: tenant, status (comma-separated), both
    only = c.get("/v1/fleet/operations?active=false&tenant=lynx", headers={"Authorization": "Bearer sk-fleet"}).json()
    assert [x["id"] for x in only] == ["op-failed", "op-queued"]
    done = c.get(
        "/v1/fleet/operations?active=false&status=succeeded,FAILED", headers={"Authorization": "Bearer sk-fleet"}
    )
    assert [x["id"] for x in done.json()] == ["op-failed", "op-done"] and done.headers["X-Total-Count"] == "2"
    assert c.get("/v1/fleet/operations?status=SUCCEEDED", headers={"Authorization": "Bearer sk-fleet"}).json() == []
    # an owner-only detail stays denied to the fleet role
    assert c.get("/v1/operations/op-done", headers={"Authorization": "Bearer sk-fleet"}).status_code == 404
    # a region that cannot be read is named, the others answer
    import urllib3

    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])

    def batch(region="eu-north1"):
        if region == "eu-south1":
            raise urllib3.exceptions.HTTPError("down")
        return fake

    monkeypatch.setattr(kube, "batch", batch)
    jobs._fleet_ops_cache["until"] = 0.0
    r = c.get("/v1/fleet/operations?active=false", headers={"Authorization": "Bearer sk-fleet"})
    assert (
        r.status_code == 200 and r.headers.get("X-Regions-Skipped") == "eu-south1" and r.headers["X-Total-Count"] == "7"
    )
    jobs._fleet_ops_cache["until"] = 0.0


def test_live_fleet_reads_share_one_listing_per_region(monkeypatch):
    from types import SimpleNamespace

    calls = {"nodes": 0, "pods": 0}

    class Core:
        def list_node(self, **kw):
            calls["nodes"] += 1
            node = SimpleNamespace(
                metadata=SimpleNamespace(name="n1", labels={"serverless2.nebius/pool": "p1"}),
                status=SimpleNamespace(
                    conditions=[SimpleNamespace(type="Ready", status="True")],
                    allocatable={"nvidia.com/gpu": "1"},
                    node_info=SimpleNamespace(kubelet_version="v1"),
                ),
            )
            return SimpleNamespace(items=[node])

        def list_pod_for_all_namespaces(self, **kw):
            calls["pods"] += 1
            return SimpleNamespace(items=[])

    monkeypatch.setattr(kube, "core", lambda region="eu-north1": Core())
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1")
    kube._live_cache.clear()
    for _ in range(3):
        assert kube.pool_usage("hub", "p1") == {"nodes_ready": 1, "gpus_total": 1, "gpus_used": 0}
    assert kube.cluster_summary("eu-north1")["nodes_ready"] == 1
    assert calls == {"nodes": 1, "pods": 1}
    kube._live_cache.clear()


def test_node_placement_uses_one_node_listing_per_region(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def node(name, pool):
        return SimpleNamespace(
            metadata=SimpleNamespace(
                name=name,
                labels={
                    "node.kubernetes.io/instance-type": "cpu-d3",
                    "nebius.com/resource-preset": "8vcpu-32gb",
                    "nebius.com/node-group-id": "ng-1",
                    "serverless2.nebius/pool": pool,
                },
            )
        )

    nodes = [node("a", "system")]

    class Core:
        def list_node(self, **kw):
            calls.append(time.time())
            return SimpleNamespace(items=list(nodes))

    monkeypatch.setattr(kube, "core", lambda region="eu-north1": Core())
    kube._node_cache.clear()
    assert kube.node_placement("eu-north1", "a")["pool"] == "system"
    assert kube.node_placement("eu-north1", "a")["instance_type"] == "cpu-d3" and len(calls) == 1
    assert kube.node_placement("eu-north1", "b") is None and len(calls) == 1  # a fresh index: no second listing
    nodes.append(node("b", "cpu-general"))
    kube._node_cache["eu-north1"] = (time.time() - 120, kube._node_cache["eu-north1"][1])  # the index aged a bit
    assert kube.node_placement("eu-north1", "b")["pool"] == "cpu-general" and len(calls) == 2  # looked for once more
    kube._node_cache.clear()


def test_a_stale_store_key_is_listed_directly_until_it_recovers(client, monkeypatch):
    """A region whose watch has been broken for STORE_STALE_S is read from the API server as before the store; once
    the reflector relists, the store serves again. A region the store never synced is read directly too."""
    c, fake = client
    body = {"input": {"nsteps": "1000", "input_prefix": "s3://serverless2-demo-eu-north1/inputs/x"}}
    c.post("/v1/models/batch-example:invoke", json=body, headers={**H, "Idempotency-Key": "stale-1"})
    listed = []
    original = fake.list_namespaced_job
    monkeypatch.setattr(fake, "list_namespaced_job", lambda *a, **k: listed.append(k) or original(*a, **k))
    store_sync(fake)
    assert c.get("/v1/operations?limit=5", headers=H).headers["X-Total-Count"] == "1" and listed == []
    store.degrade(("eu-north1", "tenant-demo", "pods"), "watch error")
    assert c.get("/v1/operations?limit=5", headers=H).status_code == 200 and listed == []  # fresh fault: served
    monkeypatch.setattr(store, "STORE_STALE_S", 0.0)
    r = c.get("/v1/operations?limit=5", headers=H)
    assert r.status_code == 200 and r.headers["X-Total-Count"] == "1" and listed  # listed directly
    before = len(listed)
    store_sync(fake)  # the reflector's relist
    r = c.get("/v1/operations?limit=5", headers=H)
    assert r.headers["X-Total-Count"] == "1" and len(listed) == before  # served again, nothing more listed


def test_the_listing_auth_cache_keeps_no_expired_generation(client, monkeypatch):
    """api 0.11.142: the authorization cache of GET /v1/operations holds one entry per principal and drops expired
    entries on every write, so a listing generation never outlives OPS_AUTH_CACHE_S there. Keyed by the listing's
    id before, every warmer refresh left a whole generation (~100 MB of Jobs) behind until 256 had piled up. Since
    0.11.173 the generation is the store's version (jobs.Listing.generation)."""
    import jobs

    c, _ = client
    generations = []

    def fresh(ns, tenant, model):
        generations.append(jobs.Listing())  # a new store version per listing, as a busy tenant produces
        generations[-1].generation = len(generations)
        return generations[-1], []

    monkeypatch.setattr(jobs, "list_ops_all", fresh)
    monkeypatch.setattr(appmod.cfg, "OPS_AUTH_CACHE_S", 0.0)  # every entry is expired by the next write
    appmod._ops_auth_cache.clear()
    for _ in range(12):
        assert c.get("/v1/operations", headers=H).status_code == 200
    assert len(generations) == 12
    assert len(appmod._ops_auth_cache) == 1  # the newest entry only; eleven expired generations are gone
    (entry,) = appmod._ops_auth_cache.values()
    assert entry[0] == generations[-1].generation  # the token of the newest generation, never the list


def test_relatives_come_from_the_store_without_a_new_read(client, monkeypatch):
    """api 0.11.146/173: with the store synced, list_related answers from it (shallow copies) and lists nothing;
    before the store is synced, it reads the Jobs as raw JSON once per region and completes the kept ones only."""
    c, fake = client
    r = c.post("/v1/models/hello-run:invoke", headers=H, json={"mode": "run", "input": {}})
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    calls = []
    original = fake.list_namespaced_job

    def counting(*a, **k):
        calls.append(k.get("label_selector", ""))
        return original(*a, **k)

    monkeypatch.setattr(fake, "list_namespaced_job", counting)
    cold = jobs.list_related("tenant-demo", "demo", lambda j: j["metadata"]["name"] == op)
    assert [j["metadata"]["name"] for j in cold] == [op] and calls  # not synced: the region was listed
    store_sync(fake)
    calls.clear()
    warm = jobs.list_related("tenant-demo", "demo", lambda j: j["metadata"]["name"] == op)
    assert [j["metadata"]["name"] for j in warm] == [op] and calls == []  # served from the store, nothing listed
    held = store.get("eu-north1", "tenant-demo", "jobs", op)
    assert warm[0] is not held and "_pods" not in held  # a copy carries the view's fields, the store's object none
    # an ACTIVE relative's pods are the store's current pods: a pod the watch delivers shows on the next view
    pods_calls = []
    original_pods = fake.list_namespaced_pod

    def counting_pods(*a, **k):
        pods_calls.append(k.get("label_selector", ""))
        return original_pods(*a, **k)

    monkeypatch.setattr(fake, "list_namespaced_pod", counting_pods)
    running = pod(op, "Running")
    store.apply(("eu-north1", "tenant-demo", "pods"), "ADDED", running)
    live = jobs.list_related("tenant-demo", "demo", lambda j: j["metadata"]["name"] == op)
    assert pods_calls == [] and running["metadata"]["name"] in [p["metadata"]["name"] for p in live[0]["_pods"]]


def test_counts_of_relatives_never_come_from_the_listing(client, monkeypatch):
    """api 0.11.147: a count (complete=False: the next export/continuation ordinal) lists fresh even with the tenant
    listing warm, so a relative created within the listing's age (here or on another replica) is counted; the view
    (complete=True) keeps using the listing."""
    c, fake = client
    r = c.post("/v1/models/hello-run:invoke", headers=H, json={"mode": "run", "input": {}})
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    store_sync(fake)  # the store: one Job
    # a retry created after the store's last event (another replica's export, or this one's a moment ago)
    late = copy.deepcopy(fake.jobs[("tenant-demo", op)])
    late["metadata"] = {
        **late["metadata"],
        "name": f"{op}-x1",
        "annotations": {**late["metadata"].get("annotations", {}), f"{cfg.LABEL}/export-of": op},
    }
    fake.jobs[("tenant-demo", f"{op}-x1")] = late
    assert jobs.export_retries("tenant-demo", "demo", op) == 1  # counted fresh
    assert jobs.continuations("tenant-demo", "demo", op) == 0
    view = jobs.list_related("tenant-demo", "demo", lambda j: j["metadata"]["name"].startswith(op))
    assert [j["metadata"]["name"] for j in view] == [op]  # the view is the store's: current to the last watch event


def test_listings_drop_managed_fields_and_auth_entries_pin_one_generation(client, monkeypatch):
    """api 0.11.148: the raw listing path drops metadata.managedFields (47% of a Job listing, read by nothing), and
    the listing authorization cache keeps entries of the current listing generation only, so a principal's entry
    never pins an older 100 MB generation."""
    import auth
    from test_api import KEY_INFO

    async def two_user_keys(key):  # two plain keys of the same tenant: two principals, two auth-cache entries
        return dict(KEY_INFO)

    monkeypatch.setattr(auth, "key_info", two_user_keys)
    c, fake = client
    r = c.post("/v1/models/hello-run:invoke", headers=H, json={"mode": "run", "input": {}})
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    fake.jobs[("tenant-demo", op)]["metadata"]["managedFields"] = [
        {"manager": "kube-controller-manager", "operation": "Update"}
    ]
    items, _ = jobs.list_ops_all("tenant-demo", "demo", None)
    assert items and all("managedFields" not in j["metadata"] for j in items)  # the direct read drops the ledger
    store_sync(fake)
    assert all("managedFields" not in j["metadata"] for j in store.items("eu-north1", "tenant-demo", "jobs"))  # held
    appmod._ops_auth_cache.clear()
    assert c.get("/v1/operations", headers=H).status_code == 200  # the user's entry, generation A
    gen_a = jobs.list_ops_all("tenant-demo", "demo", None)[0].generation
    store.apply(("eu-north1", "tenant-demo", "jobs"), "MODIFIED", fake.jobs[("tenant-demo", op)])  # a watch event
    gen_b = jobs.list_ops_all("tenant-demo", "demo", None)[0].generation
    assert gen_a != gen_b
    assert (
        c.get("/v1/operations", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200
    )  # another principal lists
    assert {v[0] for v in appmod._ops_auth_cache.values()} == {gen_b}  # the older generation is unpinned


def test_models_without_status_read_nothing_live_and_keep_identity_and_pagination(client, monkeypatch):
    """api 0.11.197 (Root, website catalog latency): `GET /v1/models?include_status=false` is the metadata-only
    page: zero endpoint_status reads and zero target-state reads for the page, every deployment's `status` and
    `replicas_ready` null, while authorization, ids, definition versions, deployments, per-class targets (identity,
    image, class, pool) and the pagination headers are exactly those of the default page. The default is unchanged
    (the page's definitions are read live)."""
    c, fake = client
    base = catalog.get("llm-example")
    many = {
        f"m-{i:02d}": {**base, "name": f"m-{i:02d}", "k8s_name": f"m-{i:02d}", "regions": ["eu-north1"]}
        for i in range(5)
    }
    monkeypatch.setattr(catalog, "all_models", lambda: many)
    reads = []
    monkeypatch.setattr(
        kube,
        "endpoint_status",
        lambda n, ns, region="eu-north1", nodes=1: reads.append(n) or {"status": "ready", "replicas_ready": 1},
    )
    monkeypatch.setattr(appmod, "_target_state", lambda *a, **k: (_ for _ in ()).throw(AssertionError("live read")))
    live = c.get("/v1/models?limit=3", headers=H)
    assert live.status_code == 200 and sorted(set(reads)) == ["m-00", "m-01", "m-02"]  # the default still reads
    reads.clear()
    r = c.get("/v1/models?limit=3&include_status=false", headers=H)
    assert r.status_code == 200 and reads == []  # nothing read live for the page
    assert [m["id"] for m in r.json()] == ["m-00", "m-01", "m-02"]
    assert r.headers["X-Total-Count"] == "5" and r.headers["X-Next-Cursor"] == live.headers["X-Next-Cursor"]
    for meta, full in zip(r.json(), live.json()):
        assert meta["regions"] == [
            {"region": "eu-north1", "cluster": "eu-north1", "status": None, "replicas_ready": None}
        ]
        assert full["regions"][0]["status"] == "ready" and full["regions"][0]["replicas_ready"] == 1
        strip = lambda d: {k: v for k, v in d.items() if k != "regions"}  # noqa: E731
        assert strip(meta) == strip(full)  # identity, version, targets, execution: identical
        assert meta["targets"] and all(t["image"] and t["cluster"] for t in meta["targets"])
    r2 = c.get(f"/v1/models?limit=3&include_status=false&cursor={r.headers['X-Next-Cursor']}", headers=H)
    assert [m["id"] for m in r2.json()] == ["m-03", "m-04"] and reads == [] and "X-Next-Cursor" not in r2.headers
    assert c.get("/v1/models?include_status=maybe", headers=H).status_code == 422
    # a key that may not see a model still does not see it (the same filter runs before the projection)
    assert c.get("/v1/models?include_status=false", headers={"Authorization": "Bearer nope"}).status_code == 401
