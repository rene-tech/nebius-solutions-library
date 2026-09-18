from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

path = Path(__file__).resolve().parents[1] / "activation/render_overlay.py"
spec = importlib.util.spec_from_file_location("lerobot_overlay", path)
overlay_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay_module)


def fixture():
    old = {
        "schema": "map/v1",
        "models": [{"model_id": "old-scientific", "stages": ["exact-stage"]}],
    }
    digest = hashlib.sha256(overlay_module.canonical(old)).hexdigest()
    current = copy.deepcopy(old)
    current["snapshot_bundles"] = {"existing-snapshot": {"unchanged": True}}
    desired = copy.deepcopy(old)
    desired["models"].append(
        {"model_id": overlay_module.MODEL, "stages": ["new-stage"]}
    )
    desired["qualification_baselines"] = {digest: ["old-scientific"]}
    scheduling = overlay_module.canonical(
        {
            "model_eligible_pool_ids": {
                "cosmos3-nano": ["reserved"],
                "old-scientific": ["old-pool"],
            },
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
    assert (
        result["snapshot_bundles"]
        == values["scientificBatch"]["executionMap"]["snapshot_bundles"]
    )
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


def successor_fixture():
    values, scheduling, desired = fixture()
    desired["models"][-1].update(
        execution_identity_sha256="a" * 64,
        stages=[{"stage_id": "augment-dataset", "image": "worker@sha256:" + "b" * 64,
                 "resources": {"cpu": "2000m"}}],
    )
    values["scientificBatch"]["executionMap"]["models"].append(
        copy.deepcopy(desired["models"][-1])
    )
    desired["models"][-1]["execution_identity_sha256"] = "c" * 64
    desired["models"][-1]["stages"][0]["image"] = "worker@sha256:" + "d" * 64
    return values, scheduling, desired


def test_explicit_successor_replaces_only_lerobot_image_and_identity():
    values, scheduling, desired = successor_fixture()
    before = copy.deepcopy(values)
    with pytest.raises(ValueError, match="existing scientific"):
        overlay_module.build_overlay(values, scheduling, desired)
    overlay, _ = overlay_module.build_overlay(
        values, scheduling, desired, replace_lerobot=True
    )
    assert values == before
    rows = overlay["scientificBatch"]["executionMap"]["models"]
    assert rows == desired["models"]
    assert rows[0] == before["scientificBatch"]["executionMap"]["models"][0]
    assert overlay["scientificBatch"]["executionMap"]["snapshot_bundles"] == (
        before["scientificBatch"]["executionMap"]["snapshot_bundles"]
    )


@pytest.mark.parametrize("change", ["sibling", "remove", "resources", "stage"])
def test_explicit_successor_still_rejects_unrelated_changes(change):
    values, scheduling, desired = successor_fixture()
    if change == "sibling":
        desired["models"][0]["stages"] = ["changed"]
    elif change == "remove":
        desired["models"].pop(0)
    elif change == "resources":
        desired["models"][-1]["stages"][0]["resources"]["cpu"] = "4000m"
    else:
        desired["models"][-1]["stages"].append({"stage_id": "extra"})
    with pytest.raises(ValueError):
        overlay_module.build_overlay(values, scheduling, desired, replace_lerobot=True)


def test_current_profile_uses_only_its_exact_public_qualification():
    root = Path(__file__).resolve().parents[4]
    activation = path.parent
    profile = json.loads((activation / "workload-profile.json").read_text())["profile"]
    qualification = profile["qualification"]
    canonical = json.loads(
        (
            root / "catalog/runtime/contracts/scientific-workload-profiles.json"
        ).read_text()
    )
    assert profile == next(
        p for p in canonical["profiles"] if p["model_id"] == profile["model_id"]
    )
    assert profile["state"] == profile["semantic_validation"]["state"]
    if profile["state"] == "active":
        assert qualification["public_completion_receipt_sha256"] is None
        assert qualification["scheduler_eligibility_receipt_sha256"] is None
        onboarding = json.loads(
            (activation / "active-onboarding-20260918.json").read_text()
        )
        assert (
            onboarding["execution_identity_sha256"]
            == profile["execution_identity"]["execution_identity_sha256"]
        )
        assert onboarding["execution_map_sha256"] == qualification["execution_map_sha256"]
        return
    assert profile["state"] == "qualified"
    digest = qualification["scheduler_eligibility_receipt_sha256"]
    scheduler_path = activation / f"qualification/scheduler-eligibility-{digest}.json"
    assert hashlib.sha256(scheduler_path.read_bytes()).hexdigest() == digest
    scheduler = json.loads(scheduler_path.read_text())
    assert (
        scheduler["execution_identity_sha256"]
        == profile["execution_identity"]["execution_identity_sha256"]
    )
    assert scheduler["execution_map_sha256"] == qualification["execution_map_sha256"]
    assert (
        scheduler["public_completion_receipt_sha256"]
        == qualification["public_completion_receipt_sha256"]
    )
    matches = [
        candidate
        for candidate in (activation / "qualification").glob("public-completion-*.json")
        if hashlib.sha256(candidate.read_bytes()).hexdigest()
        == qualification["public_completion_receipt_sha256"]
    ]
    assert len(matches) == 1
    public = json.loads(matches[0].read_text())
    assert public["execution_identity_sha256"] == scheduler["execution_identity_sha256"]
    assert public["runtime_image_digest"] == profile["execution_identity"]["runtime_image_digest"]


def test_historical_r148_public_qualification_remains_exact_and_narrowly_scoped():
    root = Path(__file__).resolve().parents[4]
    activation = path.parent
    public_path = activation / "qualification/public-completion-r148.json"
    public = json.loads(public_path.read_text())
    public_digest = hashlib.sha256(public_path.read_bytes()).hexdigest()
    assert (
        public_digest
        == "013bd1a753c2fa2b440509a50bd14dbee0b068438431567055d1e6052f4b4e7e"
    )
    digest = "eecfc6336067ccc4d8fe876eaeeedde58e350d74e4ea4a731ffe6506d2e50eae"
    scheduler_path = activation / f"qualification/scheduler-eligibility-{digest}.json"
    scheduler = json.loads(scheduler_path.read_text())
    assert hashlib.sha256(scheduler_path.read_bytes()).hexdigest() == digest
    schema = json.loads(
        (
            root
            / "catalog/runtime/schema/scientific-scheduler-eligibility-receipt.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(scheduler)
    aggregate = activation / "qualification/evidence-index-r148.json"
    assert (
        hashlib.sha256(aggregate.read_bytes()).hexdigest()
        == scheduler["fleet_aggregate_sha256"]
    )
    assert (
        scheduler["public_completion_receipt_sha256"]
        == public_digest
    )
    assert (
        scheduler["execution_map_sha256"]
        == scheduler["acceptance_execution_map_sha256"]
        == "294aa00ebf9843a865ce70cf031568e1f83509e1cc46f2ecbccff6567115409f"
    )
    assert public["execution_identity_sha256"] == scheduler["execution_identity_sha256"]
    assert (
        public["runtime_image_digest"]
        == "sha256:df364675b50cd267cb80dab38ad29fa3ec2f5d704e82e30e29ec104cea511042"
    )
    assert public["status"] == "succeeded" and public["max_concurrency"] == 1
    assert public["reader_validation"]["validation"]["nonvideo_values_exact"]
    assert public["reader_validation"]["validation"]["decoded_frames_each"] == 128
    assert not public["customer_release_ready"]
    assert not public["reader_validation"]["physical_alignment_verified"]
    assert not public["visual_review"]["strict_lighting_only_preservation_verified"]
    assert scheduler["successful_admissions"][0]["accelerator_count"] == 0
    assert scheduler["successful_admissions"][0]["resolved_pool_id"] is None
    assert public["generation"]["gpu_count"] == 1
    assert len(public["generation"]["gpu_uuids"]) == 1


def test_r150_two_variant_proof_preserves_actual_scope_and_harness_failures():
    public = json.loads(
        (path.parent / "qualification/public-completion-r150.json").read_text()
    )
    assert public["helm_revision"] == 150 and public["status"] == "succeeded"
    assert public["variants"]["count"] == len(public["reader_validations"]) == 2
    assert all(
        v["validation"]["decoded_frames_each"] == 128
        and v["validation"]["nonvideo_values_exact"]
        and not v["physical_alignment_verified"]
        for v in public["reader_validations"]
    )
    assert [c["operation"] for c in public["delegation"]["children"]] == [
        "upload", "generate-media", "generate-media"
    ]
    assert all(g["attempt"] == 1 and len(g["gpu_uuids"]) == 1 for g in public["generations"])
    behavior = public["observed_failure_behavior"]
    assert behavior["invalid_selection"]["error_code"] == "INVALID_REQUEST"
    assert behavior["invalid_selection"]["generation_child_count"] == 0
    assert behavior["concurrency"]["response_error_field"] == "type"
    assert behavior["concurrency"]["error_type"] == "concurrency_exceeded"
    assert behavior["cancellation"]["child_count"] == 0
    assert behavior["cancellation"]["cpu_resources_released"]
    harness = public["harness_execution"]
    assert harness["original_aggregate_outcome"] == "failed_stop_new_admissions"
    assert harness["continuation_aggregate_outcome"] == "failed_stop_new_admissions"
    assert harness["original_aggregates_are_not_passes"]
    assert len(harness["mismatches"]) == 2
    assert harness["linked_offline_evaluation"]["outcome"] == (
        "targeted_blur_probes_passed_with_documented_harness_corrections"
    )
    assert not public["visual_review"]["semantic_intent_verified"]
    assert not public["customer_release_ready"]
