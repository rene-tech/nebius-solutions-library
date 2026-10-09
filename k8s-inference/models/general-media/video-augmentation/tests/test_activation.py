"""Canary authoring preserves every existing App and its historical proof."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location(
    "video_activation", HERE / "activation/prepare.py"
)
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)


def inputs():
    live = json.loads(
        (ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text()
    )
    live["models"] = [m for m in live["models"] if m["model_id"] != activation.MODEL]
    live["qualification_baselines"] = {
        digest: ids
        for digest, ids in live.get("qualification_baselines", {}).items()
        if activation.MODEL not in ids
    }
    scheduler = {
        "unrelated": {"retained": True},
        "model_eligible_pool_ids": {"cosmos3-nano": ["h100-1x"]},
    }
    raw = activation.canonical(scheduler)
    values = {
        "image": {"digest": "unrelated"},
        "scientificBatch": {
            "executionMap": live,
            "schedulingContractSha256": activation.hashlib.sha256(raw).hexdigest(),
            "schedulingContractConfigMapName": "prior-123abc",
            "schedulingContractNamespace": "fs2-models",
            "schedulingContractKey": "scheduling.json",
        },
    }
    candidate = json.loads((HERE / "activation/workload-profile.json").read_text())[
        "profile"
    ]
    candidate = copy.deepcopy(candidate)
    candidate.update(state="candidate-unqualified", route_exposed=False)
    image = "cr.eu-north1.nebius.cloud/fixture/worker@sha256:" + "1" * 64
    evidence = {
        "runtime_image": image,
        "live_gpu_generation_tested": True,
        "worker_runtime_tested": True,
        "structural_validation_passed": True,
        "weather_validation_passed": True,
        "motion_validation_passed": True,
        "native_operation_status": "succeeded",
        "recorded_at": "2026-09-20T00:00:00+00:00",
    }
    return values, raw, candidate, image, evidence, "2" * 64, "3" * 64


def test_additive_execution_and_scheduler_keep_siblings_and_bundles_exact():
    args = inputs()
    before = copy.deepcopy(args)
    profile, row, overlay, cm = activation.prepare(*args)
    assert args == before
    live = args[0]["scientificBatch"]["executionMap"]
    desired = overlay["scientificBatch"]["executionMap"]
    assert desired["models"][:-1] == live["models"]
    assert desired.get("snapshot_bundles") == live.get("snapshot_bundles")
    assert set(overlay) == {"scientificBatch"}
    scheduling = json.loads(cm["data"]["scheduling.json"])
    assert scheduling.pop("unrelated") == {"retained": True}
    assert scheduling["model_eligible_pool_ids"][activation.MODEL] == ["h100-1x"]
    assert row["variant_id"] == "paidf-cosmos3-nano-v1"
    assert profile["state"] == "active" and profile["route_exposed"] is True
    assert profile["qualification"]["public_completion_receipt_sha256"] is None
    assert profile["qualification"]["scheduler_eligibility_receipt_sha256"] is None
    Draft202012Validator(
        json.loads(
            (
                ROOT / "catalog/runtime/schema/scientific-workload-profile.schema.json"
            ).read_text()
        )
    ).validate(profile)


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_image", "different"),
        ("live_gpu_generation_tested", False),
        ("worker_runtime_tested", False),
        ("weather_validation_passed", False),
        ("motion_validation_passed", False),
        ("structural_validation_passed", False),
        ("native_operation_status", "cancelled"),
        ("recorded_at", None),
    ],
)
def test_no_activation_without_exact_worker_and_gpu_receipt(field, value):
    args = list(inputs())
    args[4][field] = value
    with pytest.raises(ValueError, match="evidence"):
        activation.prepare(*args)


def test_changed_scheduler_is_rejected():
    args = list(inputs())
    args[1] += b" "
    with pytest.raises(ValueError, match="scheduler bytes"):
        activation.prepare(*args)


def test_stale_sibling_qualification_is_rejected():
    args = list(inputs())
    live = args[0]["scientificBatch"]["executionMap"]
    key, ids = next(iter(live["qualification_baselines"].items()))
    row = next(item for item in live["models"] if item["model_id"] == ids[0])
    row["execution_identity_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="baseline"):
        activation.prepare(*args)


def test_duplicate_activation_fails_closed():
    args = list(inputs())
    args[0]["scientificBatch"]["executionMap"]["models"].append(
        {"model_id": activation.MODEL}
    )
    with pytest.raises(ValueError, match="already exists"):
        activation.prepare(*args)


def test_source_recipe_binds_adapter_runtime_and_shared_handoff():
    recipe = activation.source_recipe(ROOT)
    paths = {item["path"] for item in recipe["files"]}
    assert (
        "components/control-plane/src/fs2_serve/scientific_batch/child_routes.py"
        in paths
    )
    assert (
        "models/general-media/video-augmentation/runtime/fs2_video/bridge.py" in paths
    )
    assert (
        "components/control-plane/src/fs2_serve/scientific_batch/execution.py" in paths
    )
    assert all(
        len(item["sha256"]) == 64 and item["size_bytes"] > 0 for item in recipe["files"]
    )


def test_real_catalog_and_execution_renderer_accept_additive_row(tmp_path):
    activation.source_recipe(ROOT)
    from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
    from fs2_serve.scientific_batch.profile_catalog import (
        ScientificProfileCatalog,
        ScientificWorkloadProfile,
        profile_has_complete_qualification_evidence,
    )

    profile, row, overlay, _ = activation.prepare(*inputs())
    existing = ScientificProfileCatalog.load(ROOT / "catalog/runtime")
    combined = ScientificProfileCatalog(
        profiles={
            **{p.model_id: p for p in existing.list()},
            activation.MODEL: ScientificWorkloadProfile(profile),
        },
        validators=existing._validators,
    )
    path = tmp_path / "execution.json"
    path.write_bytes(activation.canonical(overlay["scientificBatch"]["executionMap"]))
    renderer = FileScientificManifestRenderer(path=path, profiles=combined)
    assert renderer.variant_id(activation.MODEL) == row["variant_id"]
    assert renderer.collector_id(activation.MODEL, "augment-videos") == "paidf-video-v1"
    assert renderer.qualification_matches(
        activation.MODEL, "sha256:" + profile["qualification"]["execution_map_sha256"]
    )
    assert profile_has_complete_qualification_evidence(profile, allow_active=True)
    assert not profile_has_complete_qualification_evidence(profile)


def test_retained_real_gpu_canary_is_not_activation_evidence():
    receipt = json.loads((HERE / "acceptance/canary-20260920.json").read_text())
    assert receipt["live_gpu_generation_tested"] is True
    assert receipt["weather_validation_passed"] is False
    assert receipt["motion_validation_passed"] is False
    assert receipt["customer_ready"] is False
    assert receipt["audit"]["shared_helm_values_unchanged"] is True
    assert all(
        operation["status"] in {"succeeded", "cancelled"}
        for operation in receipt["audit"]["native_operations"]
    )
    args = list(inputs())
    args[3] = receipt["runtime_image"]
    args[4] = receipt
    with pytest.raises(ValueError, match="evidence"):
        activation.prepare(*args)
