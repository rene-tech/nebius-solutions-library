"""The prepared molecular repair changes only two selected runtime contracts."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from conftest import CATALOG_ROOT
from test_deployment_runtimes import inputs as canonical_inputs
from test_deployment_runtimes import project

inputs = canonical_inputs

PATH = Path(__file__).resolve().parents[3] / "acceptance/scientific-runtime-repair-20260918/prepare_promotion.py"
_spec = importlib.util.spec_from_file_location("molecular_runtime_promotion", PATH)
assert _spec is not None and _spec.loader is not None
promotion = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(promotion)


def records(suffix=""):
    return {name: json.loads((CATALOG_ROOT / f"deployment-runtimes/{name}-portable-h100{suffix}.json").read_text())
            for name in promotion.NEW}


def baseline():
    old = records()
    images = {name: item["record"]["runtime"]["image"]["reference"] for name, item in old.items()}
    keys = set(promotion.NEW) | promotion.VOICES | {f"sibling-{n}" for n in range(13)}
    qualifications = {name: {"modelRef": name, "runtimeImages": [images.get(name, "sibling-image")],
        "templateDigests": ["sha256:" + "a" * 64], "templateRefs": {name + ".legacy-v1": "sha256:" + "a" * 64},
        "templateCacheTiers": {"sha256:" + "a" * 64: "SharedFilesystem"},
        "gpuSnapshotBundles": {"historical": {"runtime_image": images.get(name, "sibling-image")}}}
        for name in keys}
    envelope = {"revision": "sha256:" + "b" * 64, "qualifications": qualifications,
                "pools": {"preserved": "pool settings"}, "maxAcceleratorsPerModel": 64}
    container = {"name": "genmol", "image": images["genmol"], "resources": {"requests": {"nvidia.com/gpu": "1"}},
        "volumeMounts": [{"name": "weights", "mountPath": "/models"}],
        "env": [{"name": name, "value": "/models/.fs2/runtime/" + promotion.OLD["genmol"] + "/weights/abi/" + name}
                for name in sorted(promotion.CACHE_ENV)] + [{"name": "MODEL_CACHE", "value": "/models"},
                    {"name": "HF_HUB_CACHE", "value": "/models/huggingface/hub"}]}
    bundle = {"modelRef": "genmol", "runtimeProfile": "custom", "runtimeContainerName": "genmol",
              "primaryWorkloadName": "genmol", "primaryServiceName": "genmol", "primaryServicePort": 8000,
              "templateDigest": "sha256:" + "a" * 64,
              "resources": [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "genmol"},
                  "spec": {"template": {"spec": {"containers": [container], "volumes": [
                      {"name": "weights", "persistentVolumeClaim": {"claimName": "retained-weights"}}]}}}},
                  {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "genmol"}, "spec": {}}]}
    bundles = [bundle, {"modelRef": "unrelated", "resources": ["unchanged"]}]
    route_data = {"deployment-runtimes.json": json.dumps({"schema": "fixture", "models": {
                     **old, "preserved-speech": {"not_changed": True}}}),
                  "qualification-projection.json": json.dumps({"observed_at": "historical-no-refresh",
                      "rows": [*[old[name]["qualification"] for name in promotion.NEW],
                               {"model_id": "preserved-speech", "historical": True}]}),
                  "lean-routes.json": '{ "routes": ["original bytes remain exact"] }\n'}
    deployments = [{"metadata": {"name": name, "namespace": "fs2-models"}, "spec": {
        "modelRef": name, "runtime": {"image": images[name], "templateRef": {
            "name": name + ".legacy-v1", "digest": "sha256:" + "a" * 64}},
        "cache": {"snapshotPreference": "Never", "tier": "SharedFilesystem"}, "fastStart": {"level": "Off"},
        "availability": {"minReplicas": 1, "maxReplicas": 4}, "queue": {"maxQueueSeconds": 7200},
        "lifecycle": {"desiredState": "Enabled"}}} for name in promotion.NEW]
    return envelope, bundles, route_data, deployments


def test_successors_validate_as_canonical_candidates_without_public_or_snapshot_claim(inputs):  # noqa: F811
    new = records("-20260918")
    projected = project(inputs, new)
    for name, entry in new.items():
        model = projected.model(name)
        assert model.runtime_image_digest == "sha256:" + promotion.NEW[name]
        assert not model.routable and not model.mcp_invocable
        assert entry["qualification"]["states"]["semantic_qualified"]
        for field in ("route_active", "http_mcp_qualified", "cold_start_qualified", "elasticity_qualified"):
            assert not entry["qualification"]["states"][field]
        for field in ("model", "resources", "cache", "interface", "startup"):
            assert entry["record"][field] == records()[name]["record"][field]


def test_additive_candidate_preserves_siblings_old_templates_weights_limits_and_snapshot_history():
    before = baseline()
    original = copy.deepcopy(before)
    envelope, bundles, routes, proposals = promotion.extend(*before, records("-20260918"))
    assert before == original
    assert bundles[:-1] == before[1]
    assert routes["lean-routes.json"] == before[2]["lean-routes.json"]
    for name in set(envelope["qualifications"]) - set(promotion.NEW):
        assert envelope["qualifications"][name] == before[0]["qualifications"][name]
    for name in promotion.NEW:
        assert (envelope["qualifications"][name]["gpuSnapshotBundles"]
                == before[0]["qualifications"][name]["gpuSnapshotBundles"])
    for proposal, previous in zip(proposals, before[3], strict=True):
        current = copy.deepcopy(proposal["spec"])
        current["runtime"] = previous["spec"]["runtime"]
        assert current == previous["spec"]
    old_container = before[1][0]["resources"][0]["spec"]["template"]["spec"]["containers"][0]
    new_container = bundles[-1]["resources"][0]["spec"]["template"]["spec"]["containers"][0]
    assert old_container["resources"] == new_container["resources"]
    assert old_container["volumeMounts"] == new_container["volumeMounts"]
    for env in new_container["env"]:
        if env["name"] in promotion.CACHE_ENV:
            assert promotion.NEW["genmol"] in env["value"] and promotion.OLD["genmol"] not in env["value"]
        else:
            assert env in old_container["env"]
    old_projection = json.loads(before[2]["qualification-projection.json"])
    new_projection = json.loads(routes["qualification-projection.json"])
    assert old_projection["rows"][-1] == new_projection["rows"][-1]
    assert old_projection["observed_at"] == new_projection["observed_at"]


@pytest.mark.parametrize("mutation", ["missing_voice", "changed_cache", "changed_limits", "wrong_image",
                                      "snapshot_selected", "cold_claim", "missing_compiler_path"])
def test_preparation_refuses_wrong_identity_or_scope(mutation):
    before, new = baseline(), records("-20260918")
    if mutation == "missing_voice":
        before[0]["qualifications"].pop(next(iter(promotion.VOICES)))
    elif mutation == "changed_cache":
        new["genmol"]["record"]["cache"]["shared_path"] = "/different-cache"
    elif mutation == "changed_limits":
        new["proteinmpnn"]["record"]["resources"]["cpu_millis"] += 1000
    elif mutation == "wrong_image":
        new["genmol"]["record"]["runtime"]["image"]["reference"] = "wrong"
    elif mutation == "snapshot_selected":
        before[3][0]["spec"]["cache"]["snapshotPreference"] = "Prefer"
    elif mutation == "cold_claim":
        new["genmol"]["qualification"]["states"]["cold_start_qualified"] = True
    else:
        before[1][0]["resources"][0]["spec"]["template"]["spec"]["containers"][0]["env"].pop(0)
    with pytest.raises(ValueError):
        promotion.extend(*before, new)


def test_immutable_configmap_names_bind_every_data_byte():
    a = promotion.configmap("fs2-science-", {"a": "one", "b": "two"})
    b = promotion.configmap("fs2-science-", {"b": "two", "a": "one"})
    c = promotion.configmap("fs2-science-", {"a": "other", "b": "two"})
    assert a == b and a["metadata"]["name"] != c["metadata"]["name"]
    assert a["immutable"] is True
