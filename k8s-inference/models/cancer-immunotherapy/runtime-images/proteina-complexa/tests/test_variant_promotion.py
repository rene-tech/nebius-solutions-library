import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("proteina_candidate_promotion", ROOT / "qualification/prepare_variant_promotion.py")
promotion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promotion)


def inputs():
    profiles, execution = [json.loads((promotion.ROOT / "catalog/runtime/contracts" / name).read_text()) for name in
        ("scientific-workload-profiles.json", "scientific-execution-map.json")]
    # A test predecessor must remain a predecessor after the real catalog has
    # promoted. Reconstruct only its image identity, then bind this synthetic
    # baseline normally; live preparation still requires exact captured maps.
    target = next(p for p in profiles["profiles"] if p["model_id"] == promotion.MODEL)
    identity = target["execution_identity"]
    identity["runtime_image_digest"] = promotion.OLD
    identity["execution_identity_sha256"] = promotion.digest({k: v for k, v in identity.items() if k != "execution_identity_sha256"})
    row = next(p for p in execution["models"] if p["model_id"] == promotion.MODEL)
    row["execution_identity_sha256"] = identity["execution_identity_sha256"]
    for stage in row["stages"]:
        stage["image"] = promotion.IMAGE.split("@", 1)[0] + "@" + promotion.OLD
    execution["qualification_baselines"] = {}
    normal = promotion.digest({"schema": execution["schema"], "models": execution["models"]})
    for profile in profiles["profiles"]:
        profile["qualification"]["execution_map_sha256"] = normal
    cases = sorted(promotion.ORIGINAL_CASES) + [name + "-matched-n400" for name in (
        "proteina-ligand-target-41_7bkc_ligand-s7", "proteina-ligand-target-41_7bkc_ligand-s42", "proteina-ame-m0584_1ldm-s7")]
    evidence = {"image": promotion.IMAGE, "recorded_at": "2026-09-19T00:00:00Z",
        "scope": "isolated_runtime_and_structural_measurements_not_public_or_biological_qualification",
        "cleanup": {"all_task_resources_absent": True}, "cases": [{"case_id": name,
            "parameters": {"diffusion_steps": 400 if name.endswith("-matched-n400") else 100},
            "stages": [{"returncode": 0} for _ in range(4)]} for name in cases]}
    return profiles, execution, evidence


def test_only_four_target_stage_images_and_unchanged_sibling_references_change():
    profiles, execution, evidence = inputs()
    originals = copy.deepcopy((profiles, execution))
    changed_profiles, changed_execution, report = promotion.candidate(profiles, execution, evidence, "b" * 64)
    assert (profiles, execution) == originals
    assert report["changed_stage_ids"] == ["generate", "filter", "evaluate", "analyze"]
    for old, new in zip(execution["models"], changed_execution["models"], strict=True):
        if old["model_id"] != promotion.MODEL:
            assert old == new
        else:
            wanted = copy.deepcopy(old)
            wanted["execution_identity_sha256"] = new["execution_identity_sha256"]
            for stage in wanted["stages"]:
                stage["image"] = promotion.IMAGE
            assert wanted == new
    target = next(p for p in changed_profiles["profiles"] if p["model_id"] == promotion.MODEL)
    assert target["state"] == "active"
    assert target["qualification"]["public_completion_receipt_sha256"] is None
    assert target["qualification"]["scheduler_eligibility_receipt_sha256"] is None
    assert execution.get("snapshot_bundles") == changed_execution.get("snapshot_bundles")


@pytest.mark.parametrize("change", ["missing", "steps", "failed", "cleanup", "image"])
def test_incomplete_or_changed_candidate_does_not_prepare(change):
    profiles, execution, evidence = inputs()
    if change == "missing":
        evidence["cases"].pop()
    elif change == "steps":
        evidence["cases"][0]["parameters"]["diffusion_steps"] = 401
    elif change == "failed":
        evidence["cases"][0]["stages"][0]["returncode"] = 1
    elif change == "cleanup":
        evidence["cleanup"]["all_task_resources_absent"] = False
    else:
        evidence["image"] = promotion.IMAGE.replace("e5e075", "a5e075")
    with pytest.raises(ValueError):
        promotion.candidate(profiles, execution, evidence, "b" * 64)
