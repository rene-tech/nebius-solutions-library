import copy
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("cosmos_prepare", Path(__file__).with_name("prepare_promotion.py"))
promotion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promotion)


def inputs():
    live_profiles = {
        "profiles": [
            {
                "model_id": mid,
                "execution_identity": {"runtime_image_digest": "old"},
                "qualification": {
                    "execution_map_sha256": "old",
                    "h100_semantic_receipt_sha256": "old",
                    "public_completion_receipt_sha256": "old",
                },
            }
            for mid in (promotion.PENDING_MODEL, promotion.MODEL, "sibling")
        ]
    }
    live_execution = {
        "models": [{"model_id": p["model_id"], "image": "old"} for p in live_profiles["profiles"]],
        "snapshot_bundles": {"retained": {"immutable": "old"}},
    }
    pending_profiles, pending_execution = copy.deepcopy((live_profiles, live_execution))
    pending_profiles["profiles"][0]["execution_identity"]["runtime_image_digest"] = promotion.PENDING_IMAGE
    pending_profiles["profiles"][0]["qualification"].update(
        h100_semantic_receipt_sha256=promotion.PENDING_EVIDENCE, public_completion_receipt_sha256=None
    )
    for row in pending_profiles["profiles"]:
        row["qualification"]["execution_map_sha256"] = "new-projection"
    pending_execution["models"][0]["image"] = promotion.PENDING_IMAGE
    return [
        copy.deepcopy(value)
        for value in (
            live_profiles,
            live_execution,
            live_profiles,
            live_execution,
            pending_profiles,
            pending_execution,
            pending_profiles,
            promotion.portable(pending_execution),
        )
    ]


def test_exact_pending_candidate_and_rollback_preserve_snapshots():
    values = inputs()
    retained = copy.deepcopy(values)
    result = promotion.verify_inputs(*values)
    assert result["snapshot_bundles_preserved"] == 1
    assert values == retained


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda v: v[0]["profiles"][0].update(unreviewed=True), "live baseline"),
        (lambda v: v[1]["models"][0].update(image="other"), "live baseline"),
        (lambda v: v[6]["profiles"][0].update(unreviewed=True), "Canonical source"),
        (lambda v: v[7]["models"][0].update(image="other"), "Canonical source"),
        (lambda v: v[5]["snapshot_bundles"].clear(), "retained snapshot"),
        (lambda v: v[7].update(snapshot_bundles={}), "substitutes live snapshot"),
    ],
)
def test_stale_or_unreviewed_composition_rejected(change, message):
    values = inputs()
    change(values)
    with pytest.raises(ValueError, match=message):
        promotion.verify_inputs(*values)


@pytest.mark.parametrize("kind", ["execution", "profile"])
def test_matched_source_cannot_smuggle_unapproved_sibling(kind):
    values = inputs()
    if kind == "execution":
        values[5]["models"][2]["image"] = "other"
        values[7] = promotion.portable(copy.deepcopy(values[5]))
    else:
        values[4]["profiles"][2]["unreviewed"] = True
        values[6] = copy.deepcopy(values[4])
    with pytest.raises(ValueError, match="unapproved sibling"):
        promotion.verify_inputs(*values)


def test_exact_cpu_evidence_has_narrow_scope_and_negative_geometry_checks():
    path = Path(__file__).with_name("exact-alignment-image-cpu.json")
    evidence = promotion.validate_evidence(path.read_bytes())
    assert "no public transfer proof" in evidence["qualification_scope"]
    assert evidence["validation"]["physical_alignment_verified"] is False
    with pytest.raises(ValueError, match="exact published-container"):
        promotion.validate_evidence(path.read_bytes() + b" ")


def test_recipe_adds_derivative_and_rehashes_current_source():
    root = promotion.ROOT
    historical = json.loads((root / promotion.ACTIVATION / "active-onboarding-20260918.json").read_bytes())
    recipe = promotion.current_recipe(root, historical)
    paths = [row["path"] for row in recipe["files"]]
    assert paths.count(promotion.EXTRA_RECIPE) == 1
    assert len(paths) == len(historical["source_recipe"]["files"]) + (
        promotion.EXTRA_RECIPE not in [row["path"] for row in historical["source_recipe"]["files"]]
    )


def test_real_successor_resets_qualification_and_projection_is_exact():
    root = promotion.ROOT
    profiles = json.loads((root / "catalog/runtime/contracts/scientific-workload-profiles.json").read_bytes())
    execution = json.loads((root / "catalog/runtime/contracts/scientific-execution-map.json").read_bytes())
    profiles, execution, _ = promotion.prepare(
        profiles,
        execution,
        model_id=promotion.MODEL,
        previous_digest=promotion.OLD,
        candidate_image=promotion.IMAGE,
        recipe_sha256="a" * 64,
        semantic_receipt_sha256=promotion.EVIDENCE_SHA,
        measured_at=promotion.MEASURED_AT,
        limitations=["Public qualification pending."],
    )
    profile = promotion.indexed(profiles, "profiles")[promotion.MODEL]
    row = promotion.indexed(execution, "models")[promotion.MODEL]
    assert profile["qualification"]["public_completion_receipt_sha256"] is None
    assert profile["qualification"]["scheduler_eligibility_receipt_sha256"] is None
    assert profile["state"] == profile["semantic_validation"]["state"] == "active"
    assert row["execution_identity_sha256"] == profile["execution_identity"]["execution_identity_sha256"]
    assert all(stage["image"] == promotion.IMAGE for stage in row["stages"])
    assert (
        promotion.projection("scientific-execution-map-projection", "scientific-execution-map.json", "model", row)[
            "model"
        ]
        == row
    )
