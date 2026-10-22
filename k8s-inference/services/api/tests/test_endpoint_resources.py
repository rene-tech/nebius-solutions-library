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
    """api 0.11.184: an endpoint's `resources_by_class` is overlaid per variant for the GPU class of the variant's pool
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
    # the 96Gi envelope on the L40S pool (57Gi nodes) without an overlay: refused at registration, naming the variant
    with pytest.raises(HTTPException) as x:
        models.to_entry({**spec, "resources_by_class": None})
    assert x.value.status_code == 400 and "impossible shape: variant cheap on north/l40s-ondemand-1x" in x.value.detail
    assert "48Gi" not in x.value.detail and "resources_by_class" in x.value.detail
