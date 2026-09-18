import copy
import json

import pytest
from parser_candidate import ROOT
from prepare_combined import OLD_MOLMIM, TARGETS, existing, extend
from test_candidate import baseline as qwen_bundle


def fixture():
    old = json.loads((ROOT / "catalog/runtime/deployment-runtimes/molmim-portable-h100.json").read_text())
    new = json.loads((ROOT / "catalog/runtime/deployment-runtimes/molmim-portable-h100-20260918.json").read_text())
    qwen = qwen_bundle()
    names = TARGETS | existing.VOICES | {"genmol", "proteinmpnn", "cosmos3-nano"} | {f"sibling-{n}" for n in range(10)}
    image = old["record"]["runtime"]["image"]["reference"]
    qualifications = {
        name: {
            "runtimeImages": [image if name == "molmim" else "sibling-image"],
            "templateDigests": [qwen["templateDigest"]],
            "templateRefs": {"legacy": qwen["templateDigest"]},
            "templateCacheTiers": {qwen["templateDigest"]: "SharedFilesystem"},
            "gpuSnapshotBundles": {"historical": "unchanged"},
        }
        for name in names
    }
    envelope = {"revision": "old", "qualifications": qualifications, "pools": {"preserved": True}}
    runtimes = {"molmim": old, "genmol": {"successor": "97c82"}, "proteinmpnn": {"successor": "81acc"}}
    qrow = {
        "model_id": "qwen3-8b",
        "states": {key: True for key in old["qualification"]["states"]},
        "evidence": {key: "historical" for key in old["qualification"]["evidence"]},
    }
    routes = {
        "deployment-runtimes.json": json.dumps({"models": runtimes}),
        "qualification-projection.json": json.dumps(
            {"rows": [old["qualification"], qrow, {"model_id": "genmol", "frozen": "newly promoted"}]}
        ),
        "lean-routes.json": "byte preserved",
    }
    identity = existing.deployment_runtime_configuration_identity(old)
    fields = {
        "image_digest": "runtime_image_digest",
        "model_revision": "model_revision",
        "artifact_manifest_sha256": "artifact_manifest_sha256",
        "acquisition_contract_sha256": "acquisition_contract_sha256",
        "provenance_sha256": "provenance_sha256",
        "semantic_health_contract_sha256": "semantic_health_contract_sha256",
    }
    admin = {
        "models": {
            "molmim": {"artifact": {k: identity[v] for k, v in fields.items()}, "settings": {"untouched": True}},
            "genmol": {"image": "newly promoted"},
        }
    }
    deployments = [
        {
            "metadata": {"name": model, "namespace": "fs2-models"},
            "spec": {
                "modelRef": model,
                "runtime": {
                    "image": image if model == "molmim" else "same-qwen-image",
                    "templateRef": {"name": "legacy", "digest": qwen["templateDigest"]},
                },
                "cache": {"tier": "SharedFilesystem", "snapshotPreference": "Never"},
                "fastStart": {"level": "Off"},
                "availability": {
                    "minReplicas": 1 if model == "molmim" else 0,
                    "maxReplicas": 4 if model == "molmim" else 8,
                },
                "lifecycle": {"desiredState": "Enabled"},
            },
        }
        for model in sorted(TARGETS)
    ]
    deployments[1]["spec"]["cache"].update(snapshotPreference="Prefer", snapshotRef={"name": "qwen-old"})
    clone = copy.deepcopy(deployments[1])
    clone["metadata"]["name"] = "disabled-qwen-app"
    clone["spec"]["lifecycle"]["desiredState"] = "Disabled"
    deployments.append(clone)
    return (envelope, [qwen], routes, admin, deployments, new)


def test_combined_keeps_promoted_siblings_voices_old_templates_and_disabled_clone():
    values = fixture()
    before = copy.deepcopy(values)
    envelope, bundles, routes, admin, proposals = extend(*values)
    assert values == before
    assert bundles[:-1] == before[1]
    assert len(envelope["qualifications"]) == 20
    for model in set(envelope["qualifications"]) - TARGETS:
        assert envelope["qualifications"][model] == before[0]["qualifications"][model]
    for model in TARGETS:
        assert (
            envelope["qualifications"][model]["gpuSnapshotBundles"]
            == before[0]["qualifications"][model]["gpuSnapshotBundles"]
        )
    assert routes["lean-routes.json"] == before[2]["lean-routes.json"]
    for model in ("genmol", "proteinmpnn"):
        assert (
            json.loads(routes["deployment-runtimes.json"])["models"][model]
            == json.loads(before[2]["deployment-runtimes.json"])["models"][model]
        )
    assert admin["models"]["genmol"] == before[3]["models"]["genmol"]
    assert admin["models"]["molmim"]["settings"] == before[3]["models"]["molmim"]["settings"]
    assert admin["models"]["molmim"]["artifact"] != before[3]["models"]["molmim"]["artifact"]
    assert {p["name"] for p in proposals} == TARGETS
    for item in proposals:
        prior = next(d["spec"] for d in before[4] if d["metadata"]["name"] == item["name"])
        current = copy.deepcopy(item["spec"])
        current["runtime"] = prior["runtime"]
        current["cache"] = prior["cache"]
        assert current == prior
    qwen = next(p["spec"] for p in proposals if p["name"] == "qwen3-8b")
    assert qwen["cache"]["snapshotPreference"] == "Never" and "snapshotRef" not in qwen["cache"]
    rows = json.loads(routes["qualification-projection.json"])["rows"]
    assert rows[-1] == json.loads(before[2]["qualification-projection.json"])["rows"][-1]
    qrow = next(r for r in rows if r["model_id"] == "qwen3-8b")
    assert not qrow["states"]["semantic_qualified"] and not qrow["states"]["http_mcp_qualified"]
    assert qrow["evidence"]["cold_start_acceptance_sha256"] is None


@pytest.mark.parametrize("mutation", ["missing_voice", "changed_resources", "wrong_image", "stale_admin", "cold_claim"])
def test_combined_rejects_drift_or_expanded_claim(mutation):
    values = fixture()
    if mutation == "missing_voice":
        values[0]["qualifications"].pop(next(iter(existing.VOICES)))
    elif mutation == "changed_resources":
        values[5]["record"]["resources"]["cpu_millis"] += 1
    elif mutation == "wrong_image":
        values[4][0]["spec"]["runtime"]["image"] = "not-" + OLD_MOLMIM
    elif mutation == "stale_admin":
        values[3]["models"]["molmim"]["artifact"]["image_digest"] = "different"
    else:
        values[5]["qualification"]["states"]["cold_start_qualified"] = True
    with pytest.raises(ValueError):
        extend(*values)
