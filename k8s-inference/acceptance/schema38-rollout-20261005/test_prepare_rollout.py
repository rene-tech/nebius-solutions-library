import copy
import importlib.util
import json
from pathlib import Path

import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p = load("prepare_rollout")
g = load("guarded_migrate")
IMAGE = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane@sha256:"
    + "a" * 64
)
OLD = IMAGE.replace("a" * 64, "b" * 64)


def resource(key, name, role, secret="fs2-serve-database"):
    pod = {
        "containers": [
            {
                "name": name,
                "image": OLD,
                "args": [role],
                "env": [
                    {
                        "name": "FS2_DATABASE_URL",
                        "valueFrom": {"secretKeyRef": {"name": secret, "key": "url"}},
                    },
                    {"name": "DO_NOT_EXPORT", "value": "private-fixture"},
                ],
                "resources": {"requests": {"cpu": "1"}},
            }
        ],
        "volumes": [{"name": "catalog", "configMap": {"name": "keep-current-catalog"}}],
    }
    spec = {"template": {"spec": pod}}
    if key[0] == "CronJob":
        spec = {
            "jobTemplate": {"spec": spec},
            "schedule": "* * * * *",
            "suspend": False,
        }
    else:
        spec.update(
            {
                "replicas": 3,
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
                },
            }
        )
    return {
        "kind": key[0],
        "metadata": {
            "namespace": key[1],
            "name": key[2],
            "uid": key[2] + "-uid",
            "resourceVersion": "123",
        },
        "spec": spec,
    }


@pytest.fixture
def items():
    api = resource(p.API, "control-plane", "serve")
    p.pod_spec(api)["containers"][0]["env"].append(
        {"name": "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE", "value": OLD}
    )
    p.pod_spec(api)["initContainers"] = [
        {"name": "starter", "image": "starter@sha256:" + "c" * 64},
        {
            "name": "wait-schema",
            "image": OLD,
            "args": ["wait-schema"],
            "env": [
                {
                    "name": "FS2_DATABASE_URL",
                    "valueFrom": {
                        "secretKeyRef": {"name": "fs2-serve-database", "key": "url"}
                    },
                }
            ],
        },
    ]
    return [
        api,
        resource(p.CONTROLLER, "model-controller", "model-controller"),
        resource(
            p.MAINTENANCE,
            "maintenance",
            "maintenance",
            "fs2-serve-database-maintenance",
        ),
        resource(p.WORKSHOP, "workshop", "workshop"),
    ]


def apply_patch(obj, patch):
    obj = copy.deepcopy(obj)
    for item in patch:
        parts = item["path"].strip("/").split("/")
        parent = obj
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        leaf = int(parts[-1]) if isinstance(parent, list) else parts[-1]
        if item["op"] == "test":
            assert parent[leaf] == item["value"]
        elif item["op"] == "remove":
            del parent[leaf]
        else:
            parent[leaf] = item["value"]
    return obj


def test_proposal_updates_main_and_waiter_without_other_spec_changes(items):
    result = p.prepare(items, IMAGE)
    assert "private-fixture" not in json.dumps(result)
    assert [r["name"] for r in result["stage_in_order"]] == [
        p.MAINTENANCE[2],
        p.API[2],
        p.CONTROLLER[2],
    ]
    for entry in result["stage_in_order"]:
        original = next(o for o in items if o["metadata"]["name"] == entry["name"])
        patched = apply_patch(original, entry["patch"])
        assert p.preserved_hash(patched) == p.preserved_hash(original)
        assert p.configmaps(patched) == p.configmaps(original)
    api = apply_patch(items[0], result["stage_in_order"][1]["patch"])
    assert api["spec"]["paused"] is True
    assert p.pod_spec(api)["containers"][0]["image"] == IMAGE
    assert p.pod_spec(api)["initContainers"][1]["image"] == IMAGE
    assert (
        p.pod_spec(api)["initContainers"][0]
        == p.pod_spec(items[0])["initContainers"][0]
    )


@pytest.mark.parametrize("which,flag", [(0, "paused"), (2, "suspend")])
@pytest.mark.parametrize("original", [None, False, True])
def test_restore_preserves_original_flag_and_uses_fresh_resource_version(
    items, which, flag, original
):
    obj = items[which]
    obj["spec"].pop(flag, None)
    if original is not None:
        obj["spec"][flag] = original
    prepared = p.stage(obj, IMAGE)
    staged = apply_patch(obj, prepared["patch"])
    staged["metadata"]["resourceVersion"] = "999"
    restored = apply_patch(staged, p.restore_patch(staged, prepared, IMAGE))
    assert (flag in restored["spec"]) is (original is not None)
    assert restored["spec"].get(flag) is original
    assert restored["metadata"]["resourceVersion"] == "999"


@pytest.mark.parametrize("change", ["map", "uid", "candidate", "resources", "strategy"])
def test_restore_refuses_concurrent_drift(items, change):
    original = items[0]
    prepared = p.stage(original, IMAGE)
    staged = apply_patch(original, prepared["patch"])
    if change == "map":
        p.pod_spec(staged)["volumes"][0]["configMap"]["name"] = "unexpected-new-catalog"
    elif change == "uid":
        staged["metadata"]["uid"] = "replacement-deployment"
    elif change == "candidate":
        p.pod_spec(staged)["initContainers"][1]["image"] = OLD
    elif change == "resources":
        p.pod_spec(staged)["containers"][0]["resources"]["requests"]["cpu"] = "2"
    else:
        staged["spec"]["strategy"]["rollingUpdate"]["maxSurge"] = 1
    with pytest.raises(ValueError):
        p.restore_patch(staged, prepared, IMAGE)


def test_independent_database_and_http_workers_are_not_staged(items):
    stt = resource(
        ("Deployment", "fs2-stt-qualification-20260927", "fs2-stt-gateway"),
        "stt",
        "serve",
        "stt-db-url-runtime",
    )
    worker = resource(
        ("Job", "fs2-models", "fs2-workflow-preserve-customer"),
        "scientific-stage",
        "science",
    )
    p.pod_spec(worker)["containers"][0]["env"] = [
        {"name": "FS2_SCIENTIFIC_INTERNAL_API_URL", "value": "http://internal"}
    ]
    result = p.prepare([*items, stt, worker], IMAGE)
    assert len(result["stage_in_order"]) == 3
    rows = {row["name"]: row for row in result["inventory"]}
    assert "separate STT database" in rows["fs2-stt-gateway"]["action"]
    assert "no direct database" in rows["fs2-workflow-preserve-customer"]["action"]
    assert "independent workshop schema" in rows[p.WORKSHOP[2]]["action"]


@pytest.mark.parametrize(
    "change", ["unknown_consumer", "env_from", "literal_dsn", "missing_api"]
)
def test_unreviewed_database_readers_are_not_silently_ignored(items, change):
    if change == "unknown_consumer":
        items.append(
            resource(("Deployment", "fs2-system", "unexpected"), "other", "serve")
        )
    elif change == "env_from":
        p.pod_spec(items[0])["containers"][0]["envFrom"] = [
            {"secretRef": {"name": "unreviewed"}}
        ]
    elif change == "literal_dsn":
        p.pod_spec(items[0])["containers"][0]["env"][0] = {
            "name": "FS2_DATABASE_URL",
            "value": "secret-dsn",
        }
    else:
        items.pop(0)
    with pytest.raises(ValueError):
        p.prepare(items, IMAGE)


def test_migration_preserves_owner_and_resources(items):
    manifest = p.prepare(items, IMAGE)["migration_job"]
    pod = p.pod_spec(manifest)
    assert pod["serviceAccountName"] == "fs2-serve-control-plane-migration"
    container = pod["containers"][0]
    assert container["image"] == IMAGE
    assert container["env"][0]["valueFrom"]["secretKeyRef"] == {
        "name": "fs2-serve-database-migrations",
        "key": "url",
    }
    assert container["resources"]["limits"] == {"cpu": "1", "memory": "512Mi"}
    compile(container["args"][0], "guarded_migrate.py", "exec")
    assert "INSERT INTO fs2_schema_migrations" not in container["args"][0]
    manifest["metadata"]["uid"] = "new-migration-uid"
    assert any(
        row["name"] == manifest["metadata"]["name"]
        for row in p.inventory([*items, manifest])
    )


@pytest.mark.parametrize("change", ["sidecar", "namespace", "mounted"])
def test_database_inventory_catches_extra_consumers(items, change):
    if change == "sidecar":
        extra = copy.deepcopy(p.pod_spec(items[0])["containers"][0])
        extra["name"] = "future-old-schema-waiter"
        p.pod_spec(items[0])["containers"].append(extra)
    else:
        extra = resource(
            ("Deployment", "other-namespace", "db-consumer"), "app", "serve"
        )
        if change == "mounted":
            extra["metadata"]["namespace"] = "fs2-system"
            p.pod_spec(extra)["containers"][0]["env"] = []
            p.pod_spec(extra)["volumes"] = [
                {"name": "db", "secret": {"secretName": "fs2-serve-database"}}
            ]
        items.append(extra)
    with pytest.raises(ValueError):
        p.inventory(items)


@pytest.mark.parametrize(
    "bad_image",
    ["image:latest", IMAGE[:-1], IMAGE.replace("fs2-serve-control-plane", "other")],
)
def test_mutable_or_wrong_image_is_rejected(items, bad_image):
    with pytest.raises(ValueError):
        p.prepare(items, bad_image)


def valid_index():
    return {"definition": g.EXPECTED_INDEX, "indisvalid": True, "indisready": True}


@pytest.mark.parametrize("count", [37, 38])
def test_exact_migration_prefix_or_idempotent_completed_ledger(count):
    expected = list(g.EXPECTED_MIGRATIONS)
    g.validate_state(expected, expected[:count], valid_index(), before=True)
    g.validate_state(expected, expected, valid_index(), before=False)


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "invalid",
        "not_ready",
        "definition",
        "ledger_digest",
        "ledger_extra",
        "candidate",
        "incomplete_after",
    ],
)
def test_migration_refuses_wrong_ledger_or_index(failure):
    expected = list(g.EXPECTED_MIGRATIONS)
    ledger, index, before = expected[:37], valid_index(), True
    if failure == "missing":
        index = None
    elif failure in {"invalid", "not_ready"}:
        index["indisvalid" if failure == "invalid" else "indisready"] = False
    elif failure == "definition":
        index["definition"] += "unexpected"
    elif failure == "ledger_digest":
        ledger[0] = (ledger[0][0], "wrong")
    elif failure == "ledger_extra":
        ledger = [*expected, ("0039_unreviewed.sql", "wrong")]
    elif failure == "candidate":
        expected = expected[:37]
    else:
        before = False
    with pytest.raises(RuntimeError):
        g.validate_state(expected, ledger, index, before=before)


@pytest.mark.asyncio
async def test_normal_migrator_called_only_after_preflight_connection_is_closed(
    monkeypatch,
):
    calls = []

    async def inspect(dsn, expected, *, before):
        calls.append("preflight-closed" if before else "verified")
        return 37 if before else 38

    async def migrate(*args):
        assert calls == ["preflight-closed"]
        calls.append("normal-migrator")

    monkeypatch.setenv("FS2_DATABASE_URL", "postgresql://unused/fixture")
    monkeypatch.setattr(g, "inspect_state", inspect)
    monkeypatch.setattr(g.PostgresStore, "migrate_database", migrate)
    await g.main()
    assert calls == ["preflight-closed", "normal-migrator", "verified"]
