import json
from pathlib import Path


HERE = Path(__file__).resolve().parent


def test_all_expected_ids_remain_visible_without_invented_qualification():
    expected = json.loads((HERE.parent / "expected-models.json").read_bytes())
    capabilities = json.loads((HERE / "capabilities.json").read_bytes())
    entries = {entry["model_id"]: entry for entry in capabilities["models"]}
    assert len(entries) == 24
    assert set(entries) == set(
        expected["serving_model_ids"] + expected["scientific_model_ids"]
    )
    assert capabilities["default"] == capabilities["fallback"] == "normal-load"
    assert entries["msa-search-pdb70"]["status"] == "not-applicable"
    assert entries["mosaic"]["status"] == "tested-incompatible"
    assert not entries["mosaic"]["fresh_pod_restore_passed"]
    assert entries["protenix-v2"]["fresh_pod_restore_passed"]
    assert entries["protenix-v2"]["restore_startup"]["n"] == 3
    for entry in entries.values():
        if entry["status"] in {
            "experimental",
            "not-yet-qualified",
            "tested-incompatible",
            "not-applicable",
        }:
            assert not entry["selectable"]
        if entry["selectable"]:
            assert entry["fresh_pod_restore_passed"]
            assert entry["distinct_inputs_passed"] >= 2
            assert entry["bundle"] is not None
