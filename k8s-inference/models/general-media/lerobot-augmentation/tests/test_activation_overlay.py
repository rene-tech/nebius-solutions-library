from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / "activation/render_overlay.py"
spec = importlib.util.spec_from_file_location("lerobot_overlay", path)
overlay_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay_module)


def fixture():
    old = {"schema": "map/v1", "models": [{"model_id": "old-scientific", "stages": ["exact-stage"]}]}
    digest = hashlib.sha256(overlay_module.canonical(old)).hexdigest()
    current = copy.deepcopy(old)
    current["snapshot_bundles"] = {"existing-snapshot": {"unchanged": True}}
    desired = copy.deepcopy(old)
    desired["models"].append({"model_id": overlay_module.MODEL, "stages": ["new-stage"]})
    desired["qualification_baselines"] = {digest: ["old-scientific"]}
    scheduling = overlay_module.canonical(
        {
            "model_eligible_pool_ids": {"cosmos3-nano": ["reserved"], "old-scientific": ["old-pool"]},
            "cpu_classes": {"general-cpu": "unchanged"},
        }
    )
    values = {
        "catalog": {"twenty_models": "all unchanged"},
        "customerStorage": {"unchanged": True},
        "scientificBatch": {
            "executionMap": current,
            "schedulingContractSha256": hashlib.sha256(scheduling).hexdigest(),
            "schedulingContractConfigMapName": "scientific-scheduling-000000000000",
            "schedulingContractNamespace": "fs2-system",
            "schedulingContractKey": "contract.json",
        },
    }
    return values, scheduling, desired


def test_overlay_is_additive_and_preserves_snapshots_and_unrelated_catalog():
    values, scheduling, desired = fixture()
    before = copy.deepcopy(values)
    overlay, cm = overlay_module.build_overlay(values, scheduling, desired)
    assert values == before
    assert set(overlay) == {"scientificBatch"}
    result = overlay["scientificBatch"]["executionMap"]
    assert result["models"][0] == values["scientificBatch"]["executionMap"]["models"][0]
    assert result["snapshot_bundles"] == values["scientificBatch"]["executionMap"]["snapshot_bundles"]
    assert cm["data"]["contract.json"].encode() != scheduling
    assert (
        overlay["scientificBatch"]["schedulingContractSha256"]
        == hashlib.sha256(cm["data"]["contract.json"].encode()).hexdigest()
    )


@pytest.mark.parametrize("change", ["remove", "mutate", "scheduler-bytes"])
def test_overlay_rejects_baseline_loss_or_identity_drift(change):
    values, scheduling, desired = fixture()
    if change == "remove":
        desired["models"].pop(0)
    elif change == "mutate":
        desired["models"][0]["stages"] = ["changed"]
    else:
        scheduling += b"\n"
    with pytest.raises(ValueError):
        overlay_module.build_overlay(values, scheduling, desired)
