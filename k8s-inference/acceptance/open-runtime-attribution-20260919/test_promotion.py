import copy
import hashlib
import json

import prepare_promotion as p
import pytest
from test_candidate import original


def inputs():
    entry = json.loads(
        (p.ROOT / "catalog/runtime/deployment-runtimes/diffdock-portable-h100-20260919.json").read_bytes()
    )
    template, owner = original()
    owner["runtime"]["image"] = entry["record"]["runtime"]["image"]["reference"]
    owner["cache"] = {"tier": "NodeLocal", "snapshotPreference": "Never"}
    models = {"diffdock", *p.existing.VOICES}
    envelope = {"revision": "old", "qualifications": {model: {} for model in models}}
    envelope["qualifications"]["diffdock"] = {
        "runtimeImages": [owner["runtime"]["image"]],
        "templateDigests": [template["templateDigest"]],
        "templateRefs": {"legacy": template["templateDigest"]},
        "templateCacheTiers": {template["templateDigest"]: ["NodeLocal"]},
    }
    routes = {
        "deployment-runtimes.json": json.dumps({"models": {"diffdock": entry}}),
        "qualification-projection.json": json.dumps({"rows": [entry["qualification"]]}),
        "lean-routes.json": '{"untouched":true}',
    }
    identity = p.existing.deployment_runtime_configuration_identity(entry)
    artifact = {
        field: identity[source]
        for field, source in {
            "artifact_manifest_sha256": "artifact_manifest_sha256",
            "acquisition_contract_sha256": "acquisition_contract_sha256",
            "provenance_sha256": "provenance_sha256",
            "semantic_health_contract_sha256": "semantic_health_contract_sha256",
            "image_digest": "runtime_image_digest",
            "model_revision": "model_revision",
        }.items()
    }
    admin = {"models": {"diffdock": {"artifact": artifact, "operator_setting": 91}, "untouched": {"replicas": 3}}}
    owners = [{"metadata": {"name": "diffdock", "namespace": "fs2-models"}, "spec": owner}]
    proof = json.loads(
        (p.ROOT / "models/structure/runtime/common/qualification/diffdock-http-h100-20260919.json").read_bytes()
    )
    return envelope, [template], routes, admin, owners, p.successor(entry, proof)


def test_four_map_delta_preserves_siblings_history_and_operator_policy():
    args = inputs()
    before = copy.deepcopy(args)
    envelope, bundles, routes, admin, proposals = p.extend(*args)
    assert args == before
    assert bundles[:-1] == before[1]
    assert routes["lean-routes.json"] == before[2]["lean-routes.json"]
    assert admin["models"]["untouched"] == before[3]["models"]["untouched"]
    assert admin["models"]["diffdock"]["operator_setting"] == 91
    for model in p.existing.VOICES:
        assert envelope["qualifications"][model] == before[0]["qualifications"][model]
    spec = proposals[0]["spec"]
    for key in set(spec) - {"runtime", "cache"}:
        assert spec[key] == before[4][0]["spec"][key]
    assert spec["cache"]["snapshotPreference"] == "Never"
    assert spec["runtime"]["image"] == p.IMAGE
    assert spec["runtime"]["templateRef"]["digest"] == bundles[-1]["templateDigest"]


@pytest.mark.parametrize("fault", ["owner_image", "admin_identity", "source_identity", "false_public", "snapshot"])
def test_stale_or_overclaimed_successor_rejected(fault):
    args = inputs()
    if fault == "owner_image":
        args[4][0]["spec"]["runtime"]["image"] = p.IMAGE
    elif fault == "admin_identity":
        args[3]["models"]["diffdock"]["artifact"]["image_digest"] = "changed"
    elif fault == "source_identity":
        args[5]["record"]["model"]["source"]["revision"] = "changed"
    elif fault == "false_public":
        args[5]["qualification"]["states"]["http_mcp_qualified"] = True
    else:
        args[4][0]["spec"]["cache"]["snapshotPreference"] = "Require"
    with pytest.raises(ValueError):
        p.extend(*args)


def test_source_record_reproducibly_matches_exact_published_gpu_receipt():
    proof = json.loads(
        (p.ROOT / "models/structure/runtime/common/qualification/diffdock-http-h100-20260919.json").read_bytes()
    )
    previous = json.loads(
        (p.ROOT / "catalog/runtime/deployment-runtimes/diffdock-portable-h100-20260919.json").read_bytes()
    )
    entry = json.loads(
        (p.ROOT / "catalog/runtime/deployment-runtimes/diffdock-portable-h100-http-identity-20260919.json").read_bytes()
    )
    assert p.successor(previous, proof) == entry
    assert (
        entry["qualification"]["evidence"]["retained_deployments_sha256"]
        == hashlib.sha256(p.existing.canonical_json(proof)).hexdigest()
    )
    for key in ("model", "cache", "resources", "interface", "startup"):
        assert entry["record"][key] == previous["record"][key]
    assert {k for k, v in entry["qualification"]["states"].items() if v} == {
        "registered",
        "runtime_ready",
        "semantic_qualified",
    }


def test_values_overlay_does_not_change_observer_or_other_maps():
    before = {
        "image": {"tag": "old"},
        "gpuObserver": {"image": "fixed"},
        "catalog": {"bindingsConfigMapName": "old", "leanRoutes": {"enabled": True, "configMapName": "old"}},
    }
    delta = {"catalog": {"leanRoutes": {"configMapName": "new"}}}
    after = p.merge_values(before, delta)
    assert after["gpuObserver"] == before["gpuObserver"]
    assert after["catalog"]["bindingsConfigMapName"] == "old"
    assert after["catalog"]["leanRoutes"] == {"enabled": True, "configMapName": "new"}
    assert before["catalog"]["leanRoutes"]["configMapName"] == "old"
