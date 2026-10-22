"""An endpoint's `resources` keep an optional ephemeral-storage request and limit next to cpu/memory through the
catalog projection (models.to_entry) and the native renderer (render_endpoint): request = limit unless both are given;
the defaults (4 CPU, 16Gi, no ephemeral storage) are unchanged and nothing is rendered without them."""

import pytest
from fastapi import HTTPException

import catalog
import kube
import models
import render_endpoint
from test_api import FLEET

SPEC = {
    "id": "nim-two-in-one",
    "kind": "endpoint",
    "image": "example.org/nim:1",
    "port": 8000,
    "gpu": {"count": 1, "classes": ["h100"]},
}


def _entry(spec):
    kube.fleet = lambda: FLEET
    return catalog.normalise(models.to_entry(spec, managed_by="api"))


def _container_resources(entry):
    cluster = entry["regions"][0]
    for v in models.variants(entry, cluster):
        for d in render_endpoint.render(entry, cluster, "models.example", "ib", v):
            if d.get("kind") == "InferenceService":
                return d["spec"]["predictor"]["containers"][0]["resources"]
    raise AssertionError("no InferenceService rendered")


def test_defaults_are_unchanged_and_nothing_ephemeral_is_rendered_without_the_fields():
    e = _entry(SPEC)
    assert e["runtime"]["resources"] == {"gpu": 1, "cpu": "4", "memory": "16Gi"}
    res = _container_resources(e)
    assert "ephemeral-storage" not in res["requests"] and "ephemeral-storage" not in res["limits"]
    assert res["requests"]["cpu"] == "4" and res["limits"]["memory"] == "16Gi"


def test_the_aggregate_shape_keeps_its_ephemeral_request_and_limit():
    """The retained two-container shape (20 CPU/160Gi + 2 CPU/2Gi, ephemeral 8704Mi request / 13Gi limit) as one
    native container: the aggregate cpu/memory and the ephemeral request AND limit survive the projection."""
    e = _entry(
        {
            **SPEC,
            "resources": {
                "cpu": "22",
                "memory": "162Gi",
                "ephemeral_storage": "8704Mi",
                "ephemeral_storage_limit": "13Gi",
            },
        }
    )
    assert e["runtime"]["resources"] == {
        "gpu": 1,
        "cpu": "22",
        "memory": "162Gi",
        "ephemeralStorage": "8704Mi",
        "ephemeralStorageLimit": "13Gi",
    }
    res = _container_resources(e)
    assert res["requests"]["ephemeral-storage"] == "8704Mi" and res["limits"]["ephemeral-storage"] == "13Gi"
    assert res["requests"]["cpu"] == "22" and res["limits"]["cpu"] == "22" and res["limits"]["memory"] == "162Gi"
    # the request alone: limit = request; the limit alone: request = limit (as a run class's)
    e = _entry({**SPEC, "resources": {"cpu": "22", "memory": "162Gi", "ephemeral_storage": "8704Mi"}})
    res = _container_resources(e)
    assert res["requests"]["ephemeral-storage"] == res["limits"]["ephemeral-storage"] == "8704Mi"
    e = _entry({**SPEC, "resources": {"ephemeral_storage_limit": "13Gi"}})
    res = _container_resources(e)
    assert res["requests"]["ephemeral-storage"] == res["limits"]["ephemeral-storage"] == "13Gi"
    assert res["requests"]["cpu"] == "4" and res["limits"]["memory"] == "16Gi"


def test_requests_below_limits_project_and_render_as_declared():
    """NV-Segment-CT's shape: 8 CPU / 64Gi requests, 24 CPU / 128Gi limits, 4Gi / 16Gi ephemeral; a limit alone
    never lowers a request, a request alone is also the limit."""
    e = _entry(
        {
            **SPEC,
            "resources": {
                "cpu": "8",
                "memory": "64Gi",
                "cpu_limit": "24",
                "memory_limit": "128Gi",
                "ephemeral_storage": "4Gi",
                "ephemeral_storage_limit": "16Gi",
            },
        }
    )
    assert e["runtime"]["resources"] == {
        "gpu": 1,
        "cpu": "8",
        "memory": "64Gi",
        "cpuLimit": "24",
        "memoryLimit": "128Gi",
        "ephemeralStorage": "4Gi",
        "ephemeralStorageLimit": "16Gi",
    }
    res = _container_resources(e)
    assert res["requests"] == {"nvidia.com/gpu": "1", "cpu": "8", "memory": "64Gi", "ephemeral-storage": "4Gi"}
    assert res["limits"] == {"nvidia.com/gpu": "1", "cpu": "24", "memory": "128Gi", "ephemeral-storage": "16Gi"}
    # the chart's own spelling (cpuRequest/memoryRequest below cpu/memory) renders as before
    v = {"resources": {"gpu": 1, "cpu": "24", "memory": "128Gi", "cpuRequest": "8", "memoryRequest": "64Gi"}}
    res = render_endpoint._resources(v)
    assert res["requests"]["cpu"] == "8" and res["limits"]["cpu"] == "24" and res["limits"]["memory"] == "128Gi"


def test_the_envelope_per_class_renders_per_variant_and_must_fit_its_pool(monkeypatch):
    """api 0.11.185: an endpoint's `resources_by_class` is overlaid per variant for the GPU class of the variant's pool
    (the same keys and overlay as a run's); an omitted class keeps the base `resources`; the map reaches no object;
    and every variant's rendered pod (plus Knative's queue-proxy request) must fit one node of its pool at
    registration: 400 `impossible shape` names the variant, nothing is shrunk."""
    import json

    from test_resources_by_class import DOC

    monkeypatch.setattr(kube, "fleet", lambda: DOC)
    spec = {
        **SPEC,
        "gpu": {"count": 1, "classes": ["l40s", "h100"]},
        "resources": {"cpu": "8", "memory": "96Gi"},
        "resources_by_class": {"l40s": {"memory": "48Gi", "cpu_limit": "10"}},
        "deployments": {
            "eu-north1": {
                "variants": [{"name": "big", "pool": "h100-reserved-1x"}, {"name": "cheap", "pool": "l40s-ondemand-1x"}]
            }
        },
    }
    e = models.to_entry(spec)
    assert e["runtime"]["resourcesByClass"] == {"l40s": {"memory": "48Gi", "cpuLimit": "10"}}
    by, docs = {}, []
    for v in models.variants(e, "north"):
        for d in render_endpoint.render(e, "north", "models.example", "ib", v):
            docs.append(d)
            if d["kind"] == "InferenceService":
                by[v["name"]] = d["spec"]["predictor"]["containers"][0]["resources"]
    assert (
        by["big"]["requests"] == {"nvidia.com/gpu": "1", "cpu": "8", "memory": "96Gi"}
        and by["big"]["limits"]["cpu"] == "8"
    )
    assert by["cheap"]["requests"] == {"nvidia.com/gpu": "1", "cpu": "8", "memory": "48Gi"}
    assert by["cheap"]["limits"] == {"nvidia.com/gpu": "1", "cpu": "10", "memory": "48Gi"}
    assert "resourcesByClass" not in json.dumps(docs)
    # omitted: equivalent to today, the base envelope on every variant
    e2 = models.to_entry({**spec, "resources": {"cpu": "8", "memory": "48Gi"}, "resources_by_class": None})
    assert "resourcesByClass" not in e2["runtime"]
    for v in models.variants(e2, "north"):
        isvc = next(
            d for d in render_endpoint.render(e2, "north", "models.example", "ib", v) if d["kind"] == "InferenceService"
        )
        assert isvc["spec"]["predictor"]["containers"][0]["resources"]["requests"]["memory"] == "48Gi"
    # an envelope that fills the pool's per-node budget EXACTLY is accepted (api 0.11.187): the budget's reserves
    # cover the platform sidecars, nothing is added on top (0.11.186 added queue-proxy's 25m and refused evo2-40b's
    # 14 CPU / 180Gi on h200 nodes that allocate 15.9 CPU and run it today)
    full = {
        **spec,
        "resources": {"cpu": "14", "memory": "180Gi"},
        "resources_by_class": None,
        "deployments": {"eu-north1": {"variants": [{"name": "big", "pool": "h100-reserved-1x"}]}},
    }
    assert models.to_entry(full)["runtime"]["resources"]["cpu"] == "14"
    # one CPU over the budget, or one GiB over: genuinely oversized, refused as before
    for res in ({"cpu": "15", "memory": "180Gi"}, {"cpu": "14", "memory": "181Gi"}):
        with pytest.raises(HTTPException) as over:
            models.to_entry({**full, "resources": res})
        assert (
            over.value.status_code == 400
            and "impossible shape: variant big on north/h100-reserved-1x" in over.value.detail
        )
    # the 96Gi envelope on the L40S pool (57Gi nodes) without an overlay: refused at registration, naming the variant
    with pytest.raises(HTTPException) as x:
        models.to_entry({**spec, "resources_by_class": None})
    assert x.value.status_code == 400 and "impossible shape: variant cheap on north/l40s-ondemand-1x" in x.value.detail
    assert "48Gi" not in x.value.detail and "resources_by_class" in x.value.detail


def test_the_weight_generation_per_class_renders_per_variant(monkeypatch):
    """api 0.11.191: `weights_by_class` (path only) picks the shared-filesystem directory per variant for the class of
    its pool; mount path, env, read-only and prepare stay the model's; a class without an entry keeps `weights.path`;
    omitted = byte-equal rendering; the map reaches no object; validation refuses unknown classes, absolute or
    non-normalised paths, parent references, an empty path and a mapping without the model's `weights`."""
    import copy
    import json

    from test_resources_by_class import DOC

    monkeypatch.setattr(kube, "fleet", lambda: DOC)
    base = {
        **SPEC,
        "gpu": {"count": 1, "classes": ["l40s", "h100"]},
        "resources": {"cpu": "4", "memory": "16Gi"},
        "weights": {
            "path": "cosmos/gen-h100",
            "mount_path": "/weights",
            "env": {"HF_HOME": "/weights"},
            "read_only": True,
        },
        "deployments": {
            "eu-north1": {
                "variants": [{"name": "big", "pool": "h100-reserved-1x"}, {"name": "cheap", "pool": "l40s-ondemand-1x"}]
            }
        },
    }
    spec = {**base, "weights_by_class": {"l40s": {"path": "cosmos/gen-l40s"}}}
    e = models.to_entry(copy.deepcopy(spec))
    assert e["runtime"]["weightsByClass"] == {"l40s": "cosmos/gen-l40s"}
    mounts, docs = {}, []
    for v in models.variants(e, "north"):
        for d in render_endpoint.render(e, "north", "models.example", "ib", v):
            docs.append(d)
            if d["kind"] == "InferenceService":
                c = d["spec"]["predictor"]["containers"][0]
                mounts[v["name"]] = next(m for m in c["volumeMounts"] if m["name"] == "weights")
    assert mounts["big"]["subPath"] == "cosmos/gen-h100" and mounts["cheap"]["subPath"] == "cosmos/gen-l40s"
    assert mounts["big"]["mountPath"] == mounts["cheap"]["mountPath"] == "/weights"
    assert mounts["big"]["readOnly"] is True and mounts["cheap"]["readOnly"] is True
    assert "weightsByClass" not in json.dumps(docs)
    # omitted: byte-equal to today's rendering
    plain = models.to_entry(copy.deepcopy(base))
    assert "weightsByClass" not in plain["runtime"]
    assert render_endpoint.render(
        plain, "north", "models.example", "ib", models.variants(plain, "north")[1]
    ) == render_endpoint.render(
        models.to_entry(copy.deepcopy(base)), "north", "models.example", "ib", models.variants(plain, "north")[1]
    )
    for bad in (
        {"weights_by_class": {"b300": {"path": "x"}}},
        {"weights_by_class": {"l40s": {"path": "/abs"}}},
        {"weights_by_class": {"l40s": {"path": "a/../b"}}},
        {"weights_by_class": {"l40s": {"path": ""}}},
        {"weights_by_class": {"l40s": {"path": "x/"}}},
        {"weights_by_class": {"l40s": {"path": "x", "mount_path": "/other"}}},
        {"weights": None, "weights_by_class": {"l40s": {"path": "x"}}},
    ):
        with pytest.raises(HTTPException) as x:
            models.to_entry(copy.deepcopy({**base, **bad}))
        assert x.value.status_code == 400 and "weights" in x.value.detail, bad
