import copy
import json
import shutil
import subprocess

import pytest

from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog, profile_has_complete_qualification_evidence

from promotion import EVIDENCE_SHA, IMAGE, MODEL, NEW, ROOT, build, canonical, digest, merge_values

BASELINE = "ba4df5bd2"


def original():
    documents = []
    for name in ("scientific-workload-profiles.json", "scientific-execution-map.json"):
        raw = subprocess.check_output(["git", "show", f"{BASELINE}:k8s-inference/catalog/runtime/contracts/{name}"], cwd=ROOT)
        documents.append(json.loads(raw))
    return documents


def candidate():
    profiles, execution = original()
    return build(profiles, execution, "a" * 64, "2026-09-18T22:35:00Z")


def test_only_target_runtime_changes_and_exact_sibling_reference_rebase():
    original_profiles, original_execution = original()
    profiles, execution, receipt = candidate()
    assert receipt["sibling_count"] == 10
    for before, after in zip(original_execution["models"], execution["models"], strict=True):
        if before["model_id"] != MODEL:
            assert before == after
        else:
            expected = copy.deepcopy(before)
            expected["execution_identity_sha256"] = after["execution_identity_sha256"]
            for stage in expected["stages"]:
                stage["image"] = IMAGE
            assert expected == after
    for before, after in zip(original_profiles["profiles"], profiles["profiles"], strict=True):
        if before["model_id"] != MODEL:
            expected = copy.deepcopy(before)
            expected["qualification"]["execution_map_sha256"] = receipt["sibling_projection_sha256"]
            assert expected == after
        else:
            assert after["state"] == after["semantic_validation"]["state"] == "active"
            assert after["source"] == before["source"]
            assert after["workload"] == before["workload"]
            assert after["runtime_artifacts"] == before["runtime_artifacts"]
            assert after["execution_identity"]["runtime_image_digest"] == NEW
            assert after["qualification"]["h100_semantic_receipt_sha256"] == EVIDENCE_SHA
            assert after["qualification"]["public_completion_receipt_sha256"] is None
            assert after["qualification"]["scheduler_eligibility_receipt_sha256"] is None
            assert not profile_has_complete_qualification_evidence(after)
            assert profile_has_complete_qualification_evidence(after, allow_active=True)


def test_corrupted_historical_baseline_is_not_rebased():
    profiles, execution = original()
    execution["models"][0]["stages"][0]["image"] = IMAGE
    with pytest.raises(ValueError, match="historical qualification"):
        build(profiles, execution, "a" * 64, "2026-09-18T22:35:00Z")


def test_unbound_old_profile_reference_is_not_rebased():
    profiles, execution = original()
    profiles["profiles"][0]["qualification"]["execution_map_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="original qualification"):
        build(profiles, execution, "a" * 64, "2026-09-18T22:35:00Z")


def test_snapshot_registry_is_byte_logically_preserved():
    profiles, execution = original()
    execution["snapshot_bundles"] = {"retained-history": {"not-selected": True}}
    _, updated, receipt = build(profiles, execution, "a" * 64, "2026-09-18T22:35:00Z")
    assert updated["snapshot_bundles"] == execution["snapshot_bundles"]
    assert receipt["snapshot_registry_unchanged"]


def test_whole_subtree_replacement_preserves_sibling_helm_values():
    _, old = original()
    _, new, _ = candidate()
    values = {"scientificBatch": {"executionMap": old, "workers": 16},
              "catalog": {"leanRoutes": {"configMapName": "newest-cxr-routes"}},
              "adminConfiguration": {"configMapName": "newest-cxr-admin"}}
    retained = copy.deepcopy(values)
    merged = merge_values(values, new)
    assert values == retained
    assert merged["scientificBatch"]["executionMap"] == new
    merged["scientificBatch"]["executionMap"] = old
    assert merged == retained


def test_actual_profile_and_manifest_startup_accept_all_models(tmp_path):
    profiles, execution, receipt = candidate()
    catalog = tmp_path / "catalog"
    shutil.copytree(ROOT / "catalog/runtime", catalog)
    (catalog / "contracts/scientific-workload-profiles.json").write_bytes(canonical(profiles))
    execution_path = tmp_path / "execution.json"
    execution_path.write_bytes(canonical(execution))
    parsed = ScientificProfileCatalog.load(catalog)
    renderer = FileScientificManifestRenderer(
        path=execution_path, profiles=parsed,
        tools_image="registry.test/tools@sha256:" + "b" * 64,
        internal_api_url="http://control.test:8080",
        academic_tenant_id="tenant-academic", academic_authorization_receipt_sha256="c" * 64,
    )
    assert len(parsed.list()) == 11
    for profile in parsed.list():
        assert renderer.qualification_matches(profile.model_id, "sha256:" + profile.value["qualification"]["execution_map_sha256"])
    assert digest({"schema": execution["schema"], "models": execution["models"]}) == receipt["candidate_normal_execution_sha256"]
    # Reusing the old whole-map digest must remain a hard startup error.
    _, before = original()
    execution["qualification_baselines"].update(before["qualification_baselines"])
    execution_path.write_bytes(canonical(execution))
    with pytest.raises(ScientificExecutionMapError, match="baseline execution fields changed"):
        FileScientificManifestRenderer(path=execution_path, profiles=parsed)
