import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
PATH = SOLUTION_ROOT / "models/molecular-dynamics/prepare_native_release.py"
SPEC = importlib.util.spec_from_file_location("native_md_release", PATH)
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


def inputs():
    live = json.loads((SOLUTION_ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text())
    # Exercise first onboarding even after one or both Apps are published.
    live["models"] = [row for row in live["models"] if row["model_id"] not in release.MODELS]
    live["qualification_baselines"] = {
        sha: ids for sha, ids in live.get("qualification_baselines", {}).items() if not set(ids) & release.MODELS
    }
    candidates = {
        model: json.loads(
            (SOLUTION_ROOT / f"models/molecular-dynamics/{model}/activation/workload-profile.json").read_text()
        )["profile"]
        for model in ("lammps", "namd")
    }
    schedule = {
        "pools": {pool: {} for pool in candidates["lammps"]["resources"]["compatible_pool_ids"]},
        "model_eligible_pool_ids": {"gromacs": ["l40s-1x"], "rfdiffusion": ["h100-1x"]},
        "quotas": {"unchanged": True},
    }
    raw = release.canonical(schedule)
    values = {
        "scientificBatch": {
            "executionMap": live,
            "schedulingContractSha256": release.hashlib.sha256(raw).hexdigest(),
            "schedulingContractConfigMapName": "existing-abc123",
            "schedulingContractNamespace": "fs2-system",
            "schedulingContractKey": "scheduling.json",
        },
        "scientificArtifacts": {"mediaTypes": ["application/json", "application/vnd.fs2.gromacs-checkpoint+json"]},
    }
    evidence = {
        model: {
            "model_id": model,
            "runtime_image": f"registry.test/{model}@sha256:" + str(index) * 64,
            "recorded_at": "2026-09-23T14:00:00Z",
            "customer_ready": False,
            "status": "passed",
            "tests": [
                {
                    "case": "unit-test-synthetic-not-a-benchmark",
                    "pool": "h100-1x",
                    "gpu_name": "NVIDIA H100",
                    "driver": "fixture",
                    "status": "passed",
                    "input_sha256": "a" * 64,
                    "result_sha256": "b" * 64,
                    "validation_sha256": "c" * 64,
                }
            ],
        }
        for index, model in enumerate(candidates, 1)
    }
    recipes = {model: {"unit_test": True} for model in candidates}
    return values, raw, candidates, evidence, recipes


def test_native_additions_preserve_existing_apps_snapshots_quotas_and_proofs():
    args = inputs()
    before = copy.deepcopy(args)
    profiles, overlay, configmap = release.compose(*args)
    assert args == before
    original = args[0]["scientificBatch"]["executionMap"]
    desired = overlay["scientificBatch"]["executionMap"]
    assert desired["models"][: len(original["models"])] == original["models"]
    assert desired.get("snapshot_bundles") == original.get("snapshot_bundles")
    schedule = json.loads(configmap["data"]["scheduling.json"])
    assert schedule["quotas"] == {"unchanged": True}
    assert schedule["model_eligible_pool_ids"]["gromacs"] == ["l40s-1x"]
    schema = json.loads((SOLUTION_ROOT / "catalog/runtime/schema/scientific-workload-profile.schema.json").read_text())
    for model, profile in profiles.items():
        Draft202012Validator(schema).validate(profile)
        assert profile["resources"]["compatible_pool_ids"] == ["h100-1x"]  # no untested L40S pool
        assert profile["qualification"]["public_completion_receipt_sha256"] is None
        assert profile["qualification"]["scheduler_eligibility_receipt_sha256"] is None
        assert schedule["model_eligible_pool_ids"][model] == ["h100-1x"]
        row = next(item for item in desired["models"] if item["model_id"] == model)
        assert row["stages"][0]["collector_id"] == f"{model}-workflow-v1"
        assert row["stages"][0]["image"] == args[3][model]["runtime_image"]
    assert set(overlay["scientificArtifacts"]["mediaTypes"]) == {
        "application/json",
        "application/x-tar",
        "application/vnd.fs2.gromacs-checkpoint+json",
        "application/vnd.fs2.lammps-checkpoint+json",
        "application/vnd.fs2.namd-checkpoint+json",
    }


@pytest.mark.parametrize(
    "change", ["image", "gpu", "failed", "incomplete", "missing-status", "missing-digest", "customer-ready", "model"]
)
def test_runtime_evidence_does_not_fabricate_missing_qualification(change):
    receipt = copy.deepcopy(inputs()[3]["lammps"])
    if change == "image":
        receipt["runtime_image"] = "registry.test/lammps:latest"
    elif change == "gpu":
        receipt["tests"][0]["gpu_name"] = "NVIDIA L40S"
    elif change == "failed":
        receipt["tests"][0]["status"] = "failed"
    elif change == "incomplete":
        receipt["status"] = "incomplete"
    elif change == "missing-status":
        del receipt["status"]
    elif change == "missing-digest":
        del receipt["tests"][0]["input_sha256"]
    elif change == "customer-ready":
        receipt["customer_ready"] = True
    else:
        receipt["model_id"] = "namd"
    with pytest.raises(ValueError):
        release.validate_evidence(receipt, "lammps")


def test_recipe_covers_engine_and_shared_transports():
    for model in ("lammps", "namd"):
        recipe = release.source_recipe(SOLUTION_ROOT, model)
        paths = {item["path"] for item in recipe["files"]}
        assert f"models/molecular-dynamics/{model}/runtime/fs2_{model}/worker.py" in paths
        assert "components/control-plane/src/fs2_serve/scientific_batch/native_workflows.py" in paths
        assert "components/control-plane/src/fs2_serve/scientific_batch/gromacs_storage_routes.py" in paths
        assert all(len(item["sha256"]) == 64 and item["size_bytes"] > 0 for item in recipe["files"])


def test_publish_adds_admin_source_receipts_without_replacing_existing_apps(tmp_path, monkeypatch):
    args = inputs()
    profiles, overlay, _ = release.compose(*args)
    target = tmp_path / "catalog/runtime/contracts"
    target.mkdir(parents=True)
    names = [
        "scientific-execution-map.json",
        "scientific-workload-profiles.json",
        "scientific-source-candidate-receipts.json",
    ]
    before = {}
    for name in names:
        source = SOLUTION_ROOT / "catalog/runtime/contracts" / name
        value = json.loads(source.read_text())
        if name == "scientific-execution-map.json":
            value = args[0]["scientificBatch"]["executionMap"]
        else:
            key = "profiles" if name == "scientific-workload-profiles.json" else "receipts"
            value[key] = [row for row in value[key] if row["model_id"] not in release.MODELS]
        before[name] = value
        (target / name).write_text(json.dumps(value))
    monkeypatch.setattr(release, "ROOT", tmp_path)
    release.publish_catalog(profiles, overlay["scientificBatch"]["executionMap"], args[0]["scientificBatch"]["executionMap"])
    receipts = json.loads((target / names[2]).read_text())["receipts"]
    old = before[names[2]]["receipts"]
    assert receipts[: len(old)] == old
    for model in profiles:
        receipt = next(item for item in receipts if item["model_id"] == model)
        assert receipt["qualification_state"] == "unqualified"
        assert receipt["source"]["revision"] == profiles[model]["source"]["revision"]
    catalog = json.loads((target / names[1]).read_text())["profiles"]
    assert catalog[: len(before[names[1]]["profiles"])] == before[names[1]]["profiles"]
