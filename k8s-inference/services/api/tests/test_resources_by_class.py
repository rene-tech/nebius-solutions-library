"""Per-class envelopes and the feasibility-aware class choice (api 0.11.177, docs/SCHEDULING.md "Per-node fit"): a
run class declares `resources_by_class`, the run is rendered with the chosen class's envelope (and a fallback with the
next class's), and the class chosen at submission skips a preferred class no node of its pools can hold while a later
class can; no feasible class keeps the `impossible shape` refusal."""

import copy

import pytest
from fastapi import HTTPException

import catalog
import fit
import jobs
import kube
import models
import placement
import render
from config import LABEL
from test_api import H, admin_keys, client, cluster  # noqa: F401 (pytest fixtures)

# one node of each pool: an L40S node (57 GiB usable) and an H100 node (180 GiB), fit.yaml-shaped
FIT = {
    "north": {
        "l40s-ondemand-1x": {
            "kind": "gpu",
            "gpus": 1,
            "cpu": 14,
            "memory_gib": 57,
            "ephemeral_gib": 200,
            "shared_fs": True,
        },
        "h100-reserved-1x": {
            "kind": "gpu",
            "gpus": 1,
            "cpu": 14,
            "memory_gib": 180,
            "ephemeral_gib": 431,
            "shared_fs": True,
        },
    }
}
POOLS = {
    "north-l40s": {"region": "north", "pool": "l40s-ondemand-1x", "gpu_class": "l40s", "usd_per_gpu_hour": 1.5},
    "north-h100": {
        "region": "north",
        "pool": "h100-reserved-1x",
        "gpu_class": "h100",
        "usd_per_gpu_hour": 2.0,
        "capacity": "reserved",
    },
}
DOC = {"pools": POOLS, "clusters": {"north": {"region": "eu-north1"}}, "fit": FIT, "tenants": {}}
P = {
    "input_prefix": "s3://serverless2-demo-eu-north1/inputs/x",
    "output_prefix": "s3://serverless2-demo-eu-north1/out/x",
    "nsteps": "1000",
}


def model(**job):
    m = copy.deepcopy(catalog.get("batch-example-tuned"))  # per-class images: the class is chosen at submission
    m["job"] = {**m["job"], "cpu": "8", "memory": "96Gi", **job}
    m["gpu"] = {"count": 1, "classes": ["l40s", "h100"]}
    m["gpu_classes"] = ["l40s", "h100"]
    m["job"]["images"] = {"l40s": m["job"]["image"], "h100": m["job"]["image"]}  # per-class images, both classes
    return m


def test_the_overlay_is_per_class_and_never_reaches_the_job():
    job = {"cpu": "8", "memory": "96Gi", "resourcesByClass": {"l40s": {"memory": "48Gi", "cpu": 6, "bogus": "x"}}}
    assert render.class_resources(job, "l40s") == {"cpu": "6", "memory": "48Gi"}
    assert render.class_resources(job, "h100") == {"cpu": "8", "memory": "96Gi"}
    assert render.class_resources(job, None) == {"cpu": "8", "memory": "96Gi"}
    res = render.resource_block(render.class_resources(job, "l40s"), 1)
    assert res == {
        "requests": {"cpu": "6", "memory": "48Gi", "nvidia.com/gpu": "1"},
        "limits": {"memory": "48Gi", "nvidia.com/gpu": "1"},
    }


def test_an_impossible_preferred_class_is_skipped_for_a_feasible_one(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: DOC)
    m = model()
    feasible, skipped = placement.feasible_classes(m, ["l40s", "h100"], 1, None)
    assert feasible == ["h100"] and skipped and skipped[0].startswith("l40s: no node of its pools holds")
    # with a declared L40S envelope the preferred class is feasible again
    m2 = model(resourcesByClass={"l40s": {"memory": "48Gi"}})
    assert placement.feasible_classes(m2, ["l40s", "h100"], 1, None) == (["l40s", "h100"], [])
    # the sidecar counts: a main of 56.9Gi on a 57 GiB node is infeasible once the uploader's 256Mi joins it
    m3 = model(resourcesByClass={"l40s": {"memory": "56.9Gi"}})
    assert placement.feasible_classes(m3, ["l40s", "h100"], 1, None)[0] == ["h100"]
    # a class whose pools lack what the run mounts is no candidate: an H100 pool without the shared filesystem
    doc = copy.deepcopy(DOC)
    doc["fit"]["north"]["h100-reserved-1x"]["shared_fs"] = False
    monkeypatch.setattr(kube, "fleet", lambda: doc)
    m4 = model(resourcesByClass={"l40s": {"memory": "48Gi"}}, data="refdb")
    feasible, skipped = placement.feasible_classes(m4, ["h100", "l40s"], 1, None)
    assert feasible == ["l40s"] and "with the shared filesystem" in skipped[0]
    monkeypatch.setattr(kube, "fleet", lambda: DOC)
    # a class without any pool is left to choose_class (it reports "preferred (no pool)"); no fit.yaml: unchanged
    assert placement.feasible_classes(m, ["b300", "h100"], 1, None)[0] == ["b300", "h100"]
    monkeypatch.setattr(fit, "table", lambda: {})
    assert placement.feasible_classes(m, ["l40s", "h100"], 1, None) == (["l40s", "h100"], [])


def test_the_run_is_rendered_with_the_chosen_classs_envelope(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: DOC)
    m = model(resourcesByClass={"l40s": {"memory": "48Gi", "cpuLimit": "10"}})
    pl = {"regions": ["north"], "classes": ["l40s", "h100"]}
    j = jobs.build_run("op-1", m, P, "demo", None, None, None, None, None, {}, "h", placement=pl, gpu_class="l40s")
    main = next(c for c in j["spec"]["template"]["spec"]["containers"] if c["name"] == "main")
    assert main["resources"]["requests"]["memory"] == "48Gi" and main["resources"]["limits"]["cpu"] == "10"
    assert "resourcesByClass" not in j["metadata"]["annotations"].get(f"{LABEL}/input", "")
    j2 = jobs.build_run("op-2", m, P, "demo", None, None, None, None, None, {}, "h", placement=pl, gpu_class="h100")
    main2 = next(c for c in j2["spec"]["template"]["spec"]["containers"] if c["name"] == "main")
    assert main2["resources"]["requests"]["memory"] == "96Gi" and "cpu" not in main2["resources"]["limits"]
    # the 96Gi envelope on the L40S class: no node holds it -> the existing refusal, nothing shrunk
    with pytest.raises(HTTPException) as e:
        jobs.build_run(
            "op-3", model(), P, "demo", None, None, None, None, None, {}, "h", placement=pl, gpu_class="l40s"
        )
    assert e.value.status_code == 400 and "impossible shape" in e.value.detail


def test_registration_validates_the_classes_and_the_keys(monkeypatch):
    monkeypatch.setattr(kube, "fleet", lambda: DOC)
    spec = {
        "id": "xx",
        "kind": "job",
        "image": "img",
        "command": "true",
        "gpu": {"count": 1, "classes": ["l40s", "h100"]},
        "resources_by_class": {"l40s": {"memory": "48Gi", "cpu_limit": "10"}},
    }
    entry = models.to_entry(models.validate(copy.deepcopy(spec)))
    assert entry["job"]["resourcesByClass"] == {"l40s": {"memory": "48Gi", "cpuLimit": "10"}}
    for bad in (
        {"b300": {"memory": "1Gi"}},  # not one of gpu.classes
        {"l40s": {"gpus": 2}},  # not a resource key
        {"l40s": {}},
        "48Gi",
    ):
        with pytest.raises(HTTPException) as e:
            models.validate({**copy.deepcopy(spec), "resources_by_class": bad})
        assert e.value.status_code == 400 and "resources_by_class" in e.value.detail
    assert (
        "resourcesByClass"
        not in models.to_entry(models.validate({k: v for k, v in spec.items() if k != "resources_by_class"}))["job"]
    )


def test_a_preferred_class_absent_from_the_allowed_clusters_is_skipped_at_submission(monkeypatch, caplog):
    """The GPU portability worker's proof shape (2026-10-22): preferences [rtx-pro-6000, l40s], allowed_clusters
    [north]; RTX pools exist only on south. The class chosen at submission is l40s, with the reason recorded in the
    API log and the run rendered with the l40s image; no fallback wait, no queue time on a class that cannot place."""
    import logging

    import app as appmod
    from schemas import InvokeRequest

    doc = {
        "pools": {
            "south-rtx": {
                "region": "south",
                "pool": "rtx6000-ondemand-1x",
                "gpu_class": "rtx-pro-6000",
                "usd_per_gpu_hour": 1.8,
            },
            **POOLS,
        },
        "clusters": {"north": {"region": "eu-north1"}, "south": {"region": "eu-south1"}},
        "fit": {
            **FIT,
            "south": {
                "rtx6000-ondemand-1x": {
                    "kind": "gpu",
                    "gpus": 1,
                    "cpu": 22,
                    "memory_gib": 200,
                    "ephemeral_gib": 400,
                    "shared_fs": True,
                }
            },
        },
        "tenants": {},
    }
    monkeypatch.setattr(kube, "fleet", lambda: doc)
    m = model(memory="48Gi")
    m["gpu"] = {"count": 1, "classes": ["rtx-pro-6000", "l40s", "h100"]}
    m["gpu_classes"] = ["rtx-pro-6000", "l40s", "h100"]
    m["job"]["images"] = {
        "rtx-pro-6000": "registry.serverless2.local/nebius/md:1.0-rtx",
        "l40s": "registry.serverless2.local/nebius/md:1.0-l40s",
    }
    ex = {"placement": {"gpu_preference": ["rtx-pro-6000", "l40s"], "allowed_clusters": ["north"]}}
    req = InvokeRequest(input=P)
    with caplog.at_level(logging.INFO, logger="api"):
        cls = appmod._class_for(m, req, None, "demo", ex, ["north"])
    assert cls == "l40s"
    assert any("rtx-pro-6000: no pool on the allowed clusters ['north']" in r.getMessage() for r in caplog.records)
    assert any("GPU class l40s chosen by fleet (eu-north1/l40s-ondemand-1x" in r.getMessage() for r in caplog.records)
    pl = {"regions": ["north"], "classes": ["rtx-pro-6000", "l40s"], "profile": "prefer-rtx-pro-6000"}
    j = jobs.build_run("op-proof", m, P, "demo", None, None, None, None, None, {}, "h", placement=pl, gpu_class=cls)
    main = next(c for c in j["spec"]["template"]["spec"]["containers"] if c["name"] == "main")
    assert main["image"] == "registry.serverless2.local/nebius/md:1.0-l40s"
    assert j["metadata"]["labels"]["serverless2.nebius/gpu-class"] == "l40s"
    assert j["metadata"]["annotations"]["serverless2.nebius/image"] == "registry.serverless2.local/nebius/md:1.0-l40s"
