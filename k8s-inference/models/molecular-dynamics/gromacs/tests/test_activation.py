import copy
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location("gromacs_activation", HERE / "activation/prepare.py")
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)


def inputs():
    live = json.loads((ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text())
    live["models"] = [row for row in live["models"] if row["model_id"] != "gromacs"]
    live["qualification_baselines"] = {key: ids for key, ids in live.get("qualification_baselines", {}).items()
                                       if "gromacs" not in ids}
    candidate = json.loads((HERE / "activation/workload-profile.json").read_text())["profile"]
    scheduler = {"pools": {pool: {} for pool in candidate["resources"]["compatible_pool_ids"]},
                 "model_eligible_pool_ids": {"rfdiffusion": ["h100-1x"]}, "quotas": {"unchanged": True}}
    raw = activation.canonical(scheduler)
    values = {"scientificBatch": {"executionMap": live,
              "schedulingContractSha256": activation.hashlib.sha256(raw).hexdigest(),
              "schedulingContractConfigMapName": "existing-abc123", "schedulingContractNamespace": "fs2-models",
              "schedulingContractKey": "scheduling.json"}}
    image = "registry.example/gromacs@sha256:" + "1" * 64
    evidence = {"runtime_image": image, "recorded_at": "2026-09-23T07:00:00Z", "tests": [{"fixture": True}]}
    return values, raw, candidate, image, evidence, "2" * 64


def test_addition_preserves_current_models_snapshots_and_quotas(tmp_path):
    args = inputs()
    before = copy.deepcopy(args)
    profile, row, overlay, cm = activation.prepare(*args)
    assert args == before
    live = args[0]["scientificBatch"]["executionMap"]
    desired = overlay["scientificBatch"]["executionMap"]
    assert live["models"] == desired["models"][:-1]
    assert live.get("snapshot_bundles") == desired.get("snapshot_bundles")
    assert json.loads(cm["data"]["scheduling.json"])["quotas"] == {"unchanged": True}
    assert profile["qualification"]["public_completion_receipt_sha256"] is None
    assert row["stages"][0]["required_node_labels"] == {"kubernetes.io/arch": "amd64"}
    assert row["runtime_artifacts"] == []
    Draft202012Validator(json.loads((ROOT / "catalog/runtime/schema/scientific-workload-profile.schema.json").read_text())).validate(profile)

    activation.source_recipe(ROOT)
    from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer
    from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog, ScientificWorkloadProfile
    existing = ScientificProfileCatalog.load(ROOT / "catalog/runtime")
    profiles = ScientificProfileCatalog(profiles={**{p.model_id: p for p in existing.list()},
        "gromacs": ScientificWorkloadProfile(profile)}, validators=existing._validators)
    path = tmp_path / "execution.json"
    path.write_bytes(activation.canonical(desired))
    renderer = FileScientificManifestRenderer(path=path, profiles=profiles)
    assert renderer.variant_id("gromacs") == "nvidia-2026-2-single-gpu-v1"
    assert renderer.collector_id("gromacs", "workflow") == "gromacs-workflow-v1"


def test_old_scheduler_or_wrong_worker_evidence_does_not_activate():
    args = list(inputs())
    args[1] += b" "
    with pytest.raises(ValueError, match="scheduler bytes"):
        activation.prepare(*args)
    args = list(inputs())
    args[4]["runtime_image"] = "wrong"
    with pytest.raises(ValueError, match="evidence"):
        activation.prepare(*args)


def test_source_recipe_binds_storage_and_native_worker():
    paths = {entry["path"] for entry in activation.source_recipe(ROOT)["files"]}
    assert "components/control-plane/src/fs2_serve/scientific_batch/gromacs_storage_routes.py" in paths
    assert "models/molecular-dynamics/gromacs/runtime/fs2_gromacs/worker.py" in paths
