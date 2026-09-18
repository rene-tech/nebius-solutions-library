import copy
import json
from pathlib import Path

import pytest

from scientific_runtime_successor import digest, prepare

ROOT = Path(__file__).resolve().parents[1]
MODEL = "cosmos3-lerobot-augmentation"


def original():
    return tuple(json.loads((ROOT / "catalog/runtime/contracts" / name).read_bytes()) for name in
                 ("scientific-workload-profiles.json", "scientific-execution-map.json"))


def build(profiles, execution):
    row = next(p for p in profiles["profiles"] if p["model_id"] == MODEL)
    return prepare(profiles, execution, model_id=MODEL,
        previous_digest=row["execution_identity"]["runtime_image_digest"],
        candidate_image="registry.test/worker@sha256:" + "d" * 64,
        recipe_sha256="a" * 64, semantic_receipt_sha256="b" * 64,
        measured_at="2026-09-18T23:10:00Z", limitations=["Public successor acceptance pending."])


def test_successor_changes_only_target_image_and_exact_sibling_reference():
    before_profiles, before_execution = original()
    retained = copy.deepcopy((before_profiles, before_execution))
    profiles, execution, receipt = build(before_profiles, before_execution)
    assert retained == (before_profiles, before_execution)
    assert execution.get("snapshot_bundles") == before_execution.get("snapshot_bundles")
    for old, new in zip(before_execution["models"], execution["models"], strict=True):
        if old["model_id"] != MODEL:
            assert old == new
        else:
            expected = copy.deepcopy(old)
            expected["execution_identity_sha256"] = new["execution_identity_sha256"]
            expected["stages"][0]["image"] = "registry.test/worker@sha256:" + "d" * 64
            assert expected == new
    for old, new in zip(before_profiles["profiles"], profiles["profiles"], strict=True):
        if old["model_id"] == MODEL:
            assert new["state"] == new["semantic_validation"]["state"] == "active"
            assert new["qualification"]["public_completion_receipt_sha256"] is None
            assert new["qualification"]["scheduler_eligibility_receipt_sha256"] is None
            assert old["workload"] == new["workload"]
        else:
            expected = copy.deepcopy(old)
            expected["qualification"]["execution_map_sha256"] = receipt["sibling_projection_sha256"]
            assert new == expected
    for sha, ids in execution["qualification_baselines"].items():
        rows = {r["model_id"]: r for r in execution["models"]}
        assert digest({"schema": execution["schema"], "models": [rows[mid] for mid in ids]}) == sha


def test_corrupt_baseline_cannot_be_requalified():
    profiles, execution = original()
    execution["qualification_baselines"]["e" * 64] = [MODEL]
    with pytest.raises(ValueError, match="baseline"):
        build(profiles, execution)


def test_unbound_qualification_cannot_be_requalified():
    profiles, execution = original()
    profiles["profiles"][0]["qualification"]["execution_map_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="unbound"):
        build(profiles, execution)
