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


def test_actual_public_qualification_is_exact_and_narrowly_scoped():
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
    assert profile["state"] == profile["semantic_validation"]["state"] == "qualified"
    public_path = activation / "qualification/public-completion-r148.json"
    public = json.loads(public_path.read_text())
    assert (
        hashlib.sha256(public_path.read_bytes()).hexdigest()
        == qualification["public_completion_receipt_sha256"]
    )
    digest = qualification["scheduler_eligibility_receipt_sha256"]
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
        == qualification["public_completion_receipt_sha256"]
    )
    assert (
        scheduler["execution_identity_sha256"]
        == profile["execution_identity"]["execution_identity_sha256"]
    )
    assert (
        scheduler["execution_map_sha256"]
        == scheduler["acceptance_execution_map_sha256"]
        == qualification["execution_map_sha256"]
    )
    assert public["execution_identity_sha256"] == scheduler["execution_identity_sha256"]
    assert (
        public["runtime_image_digest"]
        == profile["execution_identity"]["runtime_image_digest"]
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
