import copy
import json
from pathlib import Path

import prepare_presets as p
import pytest
from test_prepare import bundle


def report():
    return json.loads(Path(__file__).with_name("warmed-snapshot").joinpath("qualification.json").read_bytes())


def fixture():
    previous = p.base.template(bundle())
    source_map = next(r for r in previous["resources"] if r["kind"] == "ConfigMap")
    old_name = source_map["metadata"]["name"]
    source_map["metadata"]["name"] = "cosmos-previous-adapter"
    source_map["data"]["adapter.py"] = "# prior source fixture\n"
    for resource in previous["resources"]:
        if resource["kind"] == "Deployment":
            for volume in resource["spec"]["template"]["spec"]["volumes"]:
                if volume.get("configMap", {}).get("name") == old_name:
                    volume["configMap"]["name"] = "cosmos-previous-adapter"
    digest = previous["templateDigest"]
    envelope = {
        "revision": "old",
        "qualifications": {
            "cosmos3-nano": {
                "runtimeImages": [p.base.NEW],
                "templateDigests": [digest],
                "templateRefs": {"prior": digest},
                "templateCacheTiers": {digest: ["SharedFilesystem"]},
                "gpuSnapshotBundles": {"historical": {"keep": True}},
            },
            "sibling": {"do_not_change": True},
        },
    }
    owner = {
        "metadata": {"name": "cosmos3-nano", "namespace": "fs2-models"},
        "spec": {
            "modelRef": "cosmos3-nano",
            "runtime": {
                "image": p.base.NEW,
                "profile": "vllm-omni",
                "templateRef": {"name": "prior", "digest": digest},
            },
            "availability": {"minReplicas": 0, "maxReplicas": 4},
            "cache": {"snapshotPreference": "Never"},
            "future_setting": {"unchanged": True},
        },
    }
    return envelope, [previous], owner


def test_presets_change_only_immutable_adapter_template_and_reference():
    inputs = fixture()
    before = copy.deepcopy(inputs)
    envelope, bundles, proposal = p.extend(*inputs, p.base.adapter_source(), report())
    assert inputs == before
    assert bundles[:-1] == before[1]
    assert envelope["qualifications"]["sibling"] == before[0]["qualifications"]["sibling"]
    assert envelope["qualifications"]["cosmos3-nano"]["gpuSnapshotBundles"] == {"historical": {"keep": True}}
    spec = copy.deepcopy(proposal["spec"])
    spec["runtime"]["templateRef"] = before[2]["spec"]["runtime"]["templateRef"]
    assert spec == before[2]["spec"]
    old_pod = next(r for r in bundles[0]["resources"] if r["kind"] == "Deployment")["spec"]["template"]["spec"]
    new_pod = next(r for r in bundles[1]["resources"] if r["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert new_pod["containers"] == old_pod["containers"]
    assert new_pod["initContainers"] == old_pod["initContainers"]


def test_unmeasured_adapter_source_cannot_be_promoted():
    with pytest.raises(ValueError, match="frozen measured"):
        p.extend(*fixture(), p.base.adapter_source() + "\n", report())


@pytest.mark.parametrize("mutation", ["unchanged_controls", "nonrepeatable", "gpu_mismatch"])
def test_preset_semantics_require_contrast_repeatability_and_cross_gpu_parity(mutation):
    proof = report()
    rows = {row["case"]: row for row in proof["different_gpu_cases"]}
    if mutation == "unchanged_controls":
        for field in ("same_gpu", "different_gpu"):
            rows["transfer-edge-very_high"][field] = copy.deepcopy(rows["transfer-edge-very_low"][field])
    elif mutation == "nonrepeatable":
        for field in ("same_gpu", "different_gpu"):
            rows["transfer-edge-very_low-repeat"][field] = copy.deepcopy(rows["transfer-edge-very_high"][field])
    else:
        rows["transfer-edge-very_low"]["different_gpu"]["output_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        p.validate_preset_evidence(proof)
