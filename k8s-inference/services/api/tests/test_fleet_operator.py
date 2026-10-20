"""The fleet operator surface (api 0.11.110; docs/API.md "Fleet operators"): a key with `metadata.role=fleet` (minted
only by the fleet's own bootstrap) lists the fleet's tenants (the fleet document's and those with keys on record, with
counts, spend and what their keys hold), lists, creates, edits, blocks, revokes and imports keys of an explicit tenant
through the tenant's own functions, and inspects a tenant's usage. Ordinary tenant admins stay tenant-scoped (403 on the
fleet surface); the fleet operator is not a tenant admin (403 on the tenant surface) and runs nothing. Against
PostgreSQL with the fake cluster; skipped without TEST_DATABASE_URL."""

import copy

import auth
import db
import keys
import kube
from test_api import ADMIN_INFO, KEY_INFO, H, client, cluster  # noqa: F401
from test_database import LEGACY, database  # noqa: F401

FLEET_KEY = {"Authorization": "Bearer sk-fleet"}
ADMIN_KEY = {"Authorization": "Bearer sk-admin"}
FLEET_INFO = {
    **KEY_INFO,
    "key_alias": "platform-operator",
    "models": [],
    "scopes": ["read:catalog"],
    "max_budget": None,
    "metadata": {"tenant": "platform", "role": "fleet"},
}


def _rig(database, client, monkeypatch):
    c, fake = client
    db.migrate()
    keys.import_records(
        [
            {
                **LEGACY,
                "hash": "a" * 64,
                "alias": "lab-1",
                "principal_id": "p-1",
                "max_concurrent": 2,
                "budget_requests": 5,
                "budget_gpu_seconds": 7200,
                "rate_limit": None,
            },
            {**LEGACY, "hash": "b" * 64, "alias": "lab-2", "principal_id": "p-2", "rate_limit": None},
        ],
        "demo",
        "admin",
    )
    keys.import_records(
        [{**LEGACY, "tenant": "acme", "hash": "c" * 64, "alias": "qa-1", "principal_id": "p-9", "rate_limit": None}],
        "acme",
        "admin",
    )
    keys.block("acme", "acme-qa-1")
    keys.admit("a" * 64, "op-live-1", "demo", gpu_seconds=600.0, run_member=True)
    infos = {"sk-fleet": FLEET_INFO, "sk-admin": ADMIN_INFO, "sk-good": KEY_INFO}

    async def info(key):
        if key not in infos:
            from fastapi import HTTPException

            raise HTTPException(401, "invalid API key")
        return copy.deepcopy(infos[key])

    monkeypatch.setattr(auth, "key_info", info)
    monkeypatch.setattr(kube, "stamp_last_request", lambda *a, **k: None)
    fleet = dict(kube.fleet())
    fleet["tenants"] = {
        "demo": {"gpu_quota": 16, "fair_share_weight": 2, "dedicated_pools": [], "assets": ["weights-x"]},
        "quiet": {"gpu_quota": None, "fair_share_weight": 1, "dedicated_pools": ["north-h100"], "assets": []},
    }
    monkeypatch.setattr(kube, "fleet", lambda: fleet)
    return c, fake


def test_a_fleet_operator_lists_tenants_with_their_keys_and_usage(database, client, monkeypatch):
    c, _ = _rig(database, client, monkeypatch)
    r = c.get("/v1/fleet/tenants", headers=FLEET_KEY)
    assert r.status_code == 200, r.text
    by = {t["tenant"]: t for t in r.json()}
    # the fleet document's tenants and every tenant with keys on record; the platform's own keys are not a tenant
    assert set(by) == {"demo", "quiet", "acme"} and "platform" not in by
    demo = by["demo"]
    assert demo["in_fleet_document"] is True and demo["gpu_quota"] == 16 and demo["assets"] == ["weights-x"]
    assert demo["namespace"] == "tenant-demo" and demo["keys"] == 2 and demo["admins"] == 0 and demo["blocked"] == 0
    assert demo["usage"] == {"active_operations": 1, "reserved_gpu_seconds": 600.0, "unknown_gpu_seconds": 0.0}
    acme = by["acme"]
    assert acme["in_fleet_document"] is False and acme["gpu_quota"] is None and acme["assets"] is None
    assert acme["keys"] == 1 and acme["blocked"] == 1 and acme["usage"]["active_operations"] == 0
    quiet = by["quiet"]
    assert quiet["in_fleet_document"] is True and quiet["keys"] == 0 and quiet["dedicated_pools"] == ["north-h100"]
    one = c.get("/v1/fleet/tenants/demo", headers=FLEET_KEY)
    assert one.status_code == 200 and one.json() == demo
    assert c.get("/v1/fleet/tenants/nobody", headers=FLEET_KEY).status_code == 404
    assert c.get("/v1/fleet/tenants/platform", headers=FLEET_KEY).status_code == 404
    assert c.get("/v1/fleet/tenants/Bad_Name", headers=FLEET_KEY).status_code == 404
    # never a hash or a value anywhere in the listing
    text = r.text
    assert "a" * 64 not in text and "c" * 64 not in text and "sk-" not in text


def test_a_fleet_operator_manages_a_tenants_keys_through_the_tenants_own_functions(database, client, monkeypatch):
    c, _ = _rig(database, client, monkeypatch)
    listed = c.get("/v1/fleet/tenants/demo/keys", headers=FLEET_KEY)
    assert listed.status_code == 200, listed.text
    assert sorted(k["alias"] for k in listed.json()) == ["demo-lab-1", "demo-lab-2"]
    lab1 = next(k for k in listed.json() if k["alias"] == "demo-lab-1")
    assert lab1["tenant"] == "demo" and lab1["usage"]["active_operations"] == 1 and lab1["max_concurrent"] == 2
    assert "a" * 64 not in listed.text and "hash" not in lab1 and "metadata" not in lab1
    # create: a user key of that tenant, no pass-through routes, the value once; the audit names the fleet operator
    made = c.post(
        "/v1/fleet/tenants/demo/keys", headers=FLEET_KEY, json={"alias": "ci", "budget": 5, "models": ["llm-example"]}
    )
    assert made.status_code == 201, made.text
    assert made.json()["alias"] == "demo-ci" and made.json()["key"].startswith("sk-") and made.json()["role"] == "user"
    row = next(k for k in keys.list_tenant("demo") if k["key_alias"] == "demo-ci")
    assert row["created_by"] == "fleet:platform-operator" and row["metadata"]["allowed_passthrough_routes"] == []
    # the grant and the budget: edit; block / unblock; revoke
    edited = c.patch(
        "/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY, json={"models": ["http-example"], "budget": 50}
    )
    assert edited.status_code == 200 and edited.json()["models"] == ["http-example"] and edited.json()["budget"] == 50
    assert c.patch("/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY, json={}).status_code == 400
    # explicit null is a request, absence is not: `budget: null` lifts the limit and touches nothing else; a PATCH
    # without the field keeps the budget (a provisioning tool that re-sends models must not reset a cap)
    unlimited = c.patch("/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY, json={"budget": None})
    assert unlimited.status_code == 200, unlimited.text
    assert unlimited.json()["budget"] is None and unlimited.json()["models"] == ["http-example"]
    assert unlimited.json()["status"] == "active" and unlimited.json()["spend"] == 0
    kept = c.patch("/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY, json={"models": ["llm-example"]})
    assert kept.status_code == 200 and kept.json()["budget"] is None and kept.json()["models"] == ["llm-example"]
    capped = c.patch("/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY, json={"budget": 0})
    assert capped.status_code == 200 and capped.json()["budget"] == 0 and capped.json()["status"] == "exhausted"
    assert c.patch("/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY, json={"budget": -1}).status_code == 422
    restored = c.patch("/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY, json={"budget": 50})
    assert restored.status_code == 200 and restored.json()["status"] == "active"
    # create: the default stays 10 USD; an explicit null creates an unlimited key
    default = c.post("/v1/fleet/tenants/demo/keys", headers=FLEET_KEY, json={"alias": "ci-default"})
    assert default.status_code == 201 and default.json()["budget"] == 10.0
    free = c.post("/v1/fleet/tenants/demo/keys", headers=FLEET_KEY, json={"alias": "ci-free", "budget": None})
    assert free.status_code == 201, free.text
    assert free.json()["budget"] is None and free.json()["status"] == "active"
    for alias in ("demo-ci-default", "demo-ci-free"):
        assert c.delete(f"/v1/fleet/tenants/demo/keys/{alias}", headers=FLEET_KEY).status_code == 204
    blocked = c.post("/v1/fleet/tenants/demo/keys/demo-ci:block", headers=FLEET_KEY)
    assert blocked.status_code == 200 and blocked.json()["status"] == "blocked" and blocked.json()["blocked"] is True
    unblocked = c.post("/v1/fleet/tenants/demo/keys/demo-ci:block?blocked=false", headers=FLEET_KEY)
    assert unblocked.status_code == 200 and unblocked.json()["status"] == "active"
    assert c.delete("/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY).status_code == 204
    assert c.delete("/v1/fleet/tenants/demo/keys/demo-ci", headers=FLEET_KEY).status_code == 404
    assert (
        c.patch("/v1/fleet/tenants/acme/keys/demo-lab-1", headers=FLEET_KEY, json={"budget": 1}).status_code == 404
    )  # wrong tenant
    # import into another tenant, with that tenant's own rules (unknown scopes block, as always)
    rep = c.post(
        "/v1/fleet/tenants/acme/keys:import",
        headers=FLEET_KEY,
        json={
            "keys": [
                {
                    **LEGACY,
                    "tenant": "acme",
                    "hash": "d" * 64,
                    "alias": "qa-2",
                    "principal_id": "p-8",
                    "rate_limit": None,
                    "scopes": ["telepathy:read"],
                }
            ]
        },
    )
    assert rep.status_code == 200, rep.text
    assert [x["alias"] for x in rep.json()["blocked_for_unknown_scope"]] == ["acme-qa-2"]
    assert c.get("/v1/fleet/tenants/acme", headers=FLEET_KEY).json()["keys"] == 2
    # the tenant admin's own listing is unchanged by all of this (tenant-scoped, the same projection)
    assert sorted(k["alias"] for k in c.get("/v1/keys", headers=ADMIN_KEY).json()) == ["demo-lab-1", "demo-lab-2"]


def test_the_fleet_surface_is_the_fleet_operators_only_and_the_fleet_operator_is_no_tenant_admin(
    database, client, monkeypatch
):
    c, _ = _rig(database, client, monkeypatch)
    # a tenant admin: 403 on every fleet route (no broadening of tenant-admin authority)
    for method, path, body in (
        ("get", "/v1/fleet/tenants", None),
        ("get", "/v1/fleet/tenants/demo", None),
        ("get", "/v1/fleet/tenants/demo/keys", None),
        ("post", "/v1/fleet/tenants/demo/keys", {"alias": "x"}),
        ("patch", "/v1/fleet/tenants/demo/keys/demo-lab-1", {"budget": 1}),
        ("post", "/v1/fleet/tenants/demo/keys/demo-lab-1:block", None),
        ("delete", "/v1/fleet/tenants/demo/keys/demo-lab-1", None),
        ("post", "/v1/fleet/tenants/demo/keys:import", {"keys": []}),
    ):
        for headers in (ADMIN_KEY, H):
            r = getattr(c, method)(path, headers=headers, **({"json": body} if body is not None else {}))
            assert r.status_code == 403, (method, path, headers, r.text)
    # the fleet operator: no tenant-admin route, no model work (its key carries read:catalog only, tenant platform)
    assert c.get("/v1/keys", headers=FLEET_KEY).status_code == 403
    assert c.post("/v1/keys", headers=FLEET_KEY, json={"alias": "x"}).status_code == 403
    assert c.post("/v1/models/llm-example:invoke", headers=FLEET_KEY, json={"input": {}}).status_code == 403
    assert (
        c.post("/v1/models/hello-run:invoke", headers=FLEET_KEY, json={"mode": "run", "input": {}}).status_code == 403
    )
    assert c.get("/v1/operations", headers=FLEET_KEY).status_code == 403
    # its own view says what it is
    me = c.get("/v1/keys/me", headers=FLEET_KEY).json()
    assert me["role"] == "fleet" and me["tenant"] == "platform"
    # the ledger never minted it as a tenant key: only the bootstrap path accepts the role
    rows, errors = keys.validate_records(
        [{**LEGACY, "hash": "e" * 64, "alias": "x", "principal_id": "p", "role": "fleet"}], "demo"
    )
    assert not rows and errors and "role must be user" in str(errors)


def test_a_successor_key_owns_the_same_operations_and_keeps_a_plain_alias(database, client, monkeypatch):
    """`owner_of` (docs/API.md "Keys", rotation): the new key is the same owner as the key it succeeds, so the retained
    operations stay readable after the old key is revoked; `scopes` restrict it; an alias that already carries the
    tenant, or is the tenant name, is not prefixed again."""
    import auth
    import keys

    c, _ = _rig(database, client, monkeypatch)
    old = keys.by_alias("demo", "demo-lab-1")
    assert old and old["token"] == "a" * 64
    made = c.post(
        "/v1/fleet/tenants/demo/keys",
        headers=FLEET_KEY,
        json={"alias": "demo", "owner_of": "demo-lab-1", "scopes": ["read:operations"]},
    )
    assert made.status_code == 201, made.text
    assert made.json()["alias"] == "demo" and made.json()["key"].startswith("sk-")
    new = keys.by_alias("demo", "demo")
    assert keys.owner_identity("demo", new, new["token"]) == keys.owner_identity("demo", old, old["token"])
    assert new["scopes"] == ["read:operations"]
    # an explicit principal is carried, not an opaque owner: the successor's public view keeps `principal_id`
    assert new["metadata"]["legacy"] == {"principal_id": "p-1"} and "owner" not in new["metadata"]
    assert auth.limits_view(new)["principal_id"] == "p-1" == auth.limits_view(old)["principal_id"]
    # an implicit (key-bound) owner is stored on the successor itself
    _, plain_info = keys.create("demo", "demo-plain", 1.0, [], None)
    succ = c.post(
        "/v1/fleet/tenants/demo/keys", headers=FLEET_KEY, json={"alias": "demo-plain-2", "owner_of": "demo-plain"}
    )
    assert succ.status_code == 201, succ.text
    s2 = keys.by_alias("demo", "demo-plain-2")
    assert s2["metadata"]["owner"] == keys.owner_identity("demo", plain_info, plain_info["token"])
    assert not s2["metadata"].get("legacy")
    assert keys.owner_identity("demo", s2, s2["token"]) == keys.owner_identity("demo", plain_info, plain_info["token"])
    # the same owner through the API: the successor reads the predecessor's operation, a plain new key does not
    plain = c.post("/v1/fleet/tenants/demo/keys", headers=FLEET_KEY, json={"alias": "demo-other"})
    assert plain.status_code == 201 and plain.json()["alias"] == "demo-other"
    other = keys.by_alias("demo", "demo-other")
    assert keys.owner_identity("demo", other, other["token"]) != keys.owner_identity("demo", old, old["token"])
    assert (
        c.post("/v1/fleet/tenants/demo/keys", headers=FLEET_KEY, json={"alias": "x", "owner_of": "nope"}).status_code
        == 404
    )
    bad = c.post("/v1/fleet/tenants/demo/keys", headers=FLEET_KEY, json={"alias": "y", "scopes": ["read:everything"]})
    assert bad.status_code == 400 and "read:everything" in bad.text


def test_a_fleet_operator_reads_any_tenants_operation(database, client, monkeypatch):
    """The operator console is the operator's view of everything on the fleet (docs/API.md "Fleet operators"): a
    fleet key reads any tenant's operation (status, attempts, logs, metrics), while a tenant key still reads only
    what it owns. Writes stay owner-only."""
    c, fake = _rig(database, client, monkeypatch)
    L = "serverless2.nebius"
    fake.jobs[("tenant-demo", "op-seen")] = {
        "metadata": {
            "name": "op-seen",
            "namespace": "tenant-demo",
            "creationTimestamp": "2026-10-18T10:00:00Z",
            "labels": {f"{L}/tenant": "demo", f"{L}/model": "llm-example", f"{L}/mode": "run"},
            "annotations": {f"{L}/key": "z" * 64, f"{L}/owner": "someone-else"},
        },
        "status": {"active": 1, "startTime": "2026-10-18T10:00:05Z", "conditions": []},
    }
    # the fixture's fleet key is scoped to the catalog: scopes bind a fleet key like any other
    assert c.get("/v1/operations/op-seen", headers=FLEET_KEY).status_code == 403
    monkeypatch.setitem(FLEET_INFO, "scopes", ["read:catalog", "read:operations"])
    seen = c.get("/v1/operations/op-seen", headers=FLEET_KEY)
    assert seen.status_code == 200, seen.text
    assert seen.json()["id"] == "op-seen"
    assert c.get("/v1/operations/op-seen", headers={"Authorization": "Bearer sk-good"}).status_code == 404
    assert c.get("/v1/operations/op-unknown", headers=FLEET_KEY).status_code == 404
    # and manages it: the operator cancels any tenant's operation (continuations run on the run's own key)
    monkeypatch.setitem(FLEET_INFO, "scopes", ["read:catalog", "read:operations", "run:control"])
    cancelled = c.post("/v1/operations/op-seen:cancel", headers=FLEET_KEY)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"


def test_a_fleet_operator_reads_the_fleet_overview_without_a_tenant_of_its_own(database, client, monkeypatch):
    """GET /v1/fleet (the console's Fleet page) answers the operator: no tenant block, no execution capabilities
    (there is no tenant-platform namespace to read them from), the pools and clusters as for everyone."""
    c, _ = _rig(database, client, monkeypatch)
    for query in ("", "?live=true"):
        r = c.get(f"/v1/fleet{query}", headers=FLEET_KEY)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("tenant") is None and body.get("execution") is None and "pools" in body


def test_a_keys_priority_is_set_at_creation_changed_by_patch_and_bounds_the_request(database, client, monkeypatch):
    """docs/API.md "Priority": the key carries low | normal | high (default normal) for everything it submits; a
    request may lower it, never raise it."""
    import keys

    c, _ = _rig(database, client, monkeypatch)
    made = c.post("/v1/fleet/tenants/demo/keys", headers=FLEET_KEY, json={"alias": "batch", "priority": "low"})
    assert made.status_code == 201, made.text
    assert made.json()["priority"] == "low"
    listed = {k["alias"]: k for k in c.get("/v1/fleet/tenants/demo/keys", headers=FLEET_KEY).json()}
    assert listed["demo-batch"]["priority"] == "low" and listed["demo-lab-1"]["priority"] == "normal"
    edited = c.patch("/v1/fleet/tenants/demo/keys/demo-batch", headers=FLEET_KEY, json={"priority": "high"})
    assert edited.status_code == 200 and edited.json()["priority"] == "high"
    assert (
        c.patch("/v1/fleet/tenants/demo/keys/demo-batch", headers=FLEET_KEY, json={"priority": "urgent"}).status_code
        == 422
    )
    info = keys.by_alias("demo", "demo-batch")
    assert keys.effective_priority(info, None) == "high"
    assert keys.effective_priority(info, "low") == "low"  # a request may lower
    assert keys.effective_priority({"metadata": {"priority": "low"}}, "high") == "low"  # never raise
    assert keys.effective_priority({}, None) == "normal" and keys.effective_priority({}, "high") == "normal"
