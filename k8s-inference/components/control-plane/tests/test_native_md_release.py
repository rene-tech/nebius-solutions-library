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
        for model in ("lammps", "namd", "amber")
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
        "application/vnd.fs2.amber-checkpoint+json",
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
    for model in ("lammps", "namd", "amber"):
        recipe = release.source_recipe(SOLUTION_ROOT, model)
        paths = {item["path"] for item in recipe["files"]}
        assert f"models/molecular-dynamics/{model}/runtime/fs2_{model}/worker.py" in paths
        assert "components/control-plane/src/fs2_serve/scientific_batch/native_workflows.py" in paths
        assert "components/control-plane/src/fs2_serve/scientific_batch/gromacs_storage_routes.py" in paths
        assert all(len(item["sha256"]) == 64 and item["size_bytes"] > 0 for item in recipe["files"])
        if model == "amber":
            assert "models/molecular-dynamics/amber/tools/environment-linux-64.lock" in paths
            assert "models/molecular-dynamics/amber/runtime/Containerfile.worker" in paths


def test_explicit_successor_precedes_new_app_and_preserves_legacy_rows():
    values, scheduling, candidates, evidence, recipes = inputs()
    initial, overlay, cm = release.compose(
        values, scheduling, {"namd": candidates["namd"]}, {"namd": evidence["namd"]}, {"namd": recipes["namd"]}
    )
    for key, value in overlay.items():
        values[key].update(value)
    captured = copy.deepcopy(values["scientificBatch"]["executionMap"])
    profiles, update, _ = release.compose(
        values,
        cm["data"]["scheduling.json"].encode(),
        candidates,
        evidence,
        {**recipes, "namd": {"changed_adapter": True}},
        replace_models={"namd"},
    )
    assert profiles["namd"]["execution_identity"] != initial["namd"]["execution_identity"]
    old = [row for row in captured["models"] if row["model_id"] != "namd"]
    assert [
        row for row in update["scientificBatch"]["executionMap"]["models"] if row["model_id"] not in release.MODELS
    ] == old


def test_pool_expansion_is_explicit_receipt_bound_and_preserves_capacity():
    values, scheduling, candidates, evidence, recipes = inputs()
    candidate = {"lammps": candidates["lammps"]}
    proof = {"lammps": evidence["lammps"]}
    recipe = {"lammps": recipes["lammps"]}
    _, overlay, cm = release.compose(values, scheduling, candidate, proof, recipe)
    for key, value in overlay.items():
        values[key].update(value)
    prior_schedule = cm["data"]["scheduling.json"].encode()
    proof["lammps"]["tests"].append({**proof["lammps"]["tests"][0],
                                    "pool": "l40s-1x", "gpu_name": "NVIDIA L40S"})
    with pytest.raises(ValueError, match="pool mapping"):
        release.compose(values, prior_schedule, candidate, proof, recipe, replace_models={"lammps"})
    profiles, _, cm = release.compose(values, prior_schedule, candidate, proof, recipe,
                                     replace_models={"lammps"}, expand_qualified_pools={"lammps"})
    new = json.loads(cm["data"]["scheduling.json"])
    old = json.loads(prior_schedule)
    assert profiles["lammps"]["resources"]["compatible_pool_ids"] == ["h100-1x", "l40s-1x"]
    new["model_eligible_pool_ids"]["lammps"] = old["model_eligible_pool_ids"]["lammps"]
    assert new == old
    with pytest.raises(ValueError, match="explicit successor"):
        release.compose(values, prior_schedule, candidate, proof, recipe, expand_qualified_pools={"lammps"})


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
    release.publish_catalog(
        profiles, overlay["scientificBatch"]["executionMap"], args[0]["scientificBatch"]["executionMap"]
    )
    receipts = json.loads((target / names[2]).read_text())["receipts"]
    old = before[names[2]]["receipts"]
    assert receipts[: len(old)] == old
    for model in profiles:
        receipt = next(item for item in receipts if item["model_id"] == model)
        assert receipt["qualification_state"] == "unqualified"
        assert receipt["source"]["revision"] == profiles[model]["source"]["revision"]
    catalog = json.loads((target / names[1]).read_text())["profiles"]
    assert catalog[: len(before[names[1]]["profiles"])] == before[names[1]]["profiles"]


def successor_inputs():
    """A captured release with a historical proof containing the NAMD predecessor."""
    values, scheduling, candidates, evidence, recipes = inputs()
    initial = {}
    for model in ("namd", "lammps"):
        profiles, overlay, cm = release.compose(
            values, scheduling, {model: candidates[model]}, {model: evidence[model]}, {model: recipes[model]}
        )
        initial.update(profiles)
        for key, value in overlay.items():
            values[key].update(value)
        scheduling = cm["data"]["scheduling.json"].encode()
    evidence["lammps"]["tests"].append(
        {**evidence["lammps"]["tests"][0], "pool": "l40s-1x", "gpu_name": "NVIDIA L40S"}
    )
    recipes.update(namd={"successor": "r5"}, lammps={"successor": "qualified-pool-expansion"})
    evidence["namd"]["runtime_image"] = "registry.test/namd@sha256:" + "9" * 64
    return (values, scheduling, candidates, evidence, recipes), initial


def test_combined_successors_project_old_proofs_without_requalifying_changed_apps():
    args, initial = successor_inputs()
    before = copy.deepcopy(args)
    captured = args[0]["scientificBatch"]["executionMap"]
    profiles, overlay, cm = release.compose(
        *args, replace_models={"namd", "lammps"}, expand_qualified_pools={"lammps"}
    )
    assert args == before
    desired = overlay["scientificBatch"]["executionMap"]
    after_rows = {row["model_id"]: row for row in desired["models"]}
    for row in captured["models"]:
        if row["model_id"] not in {"namd", "lammps"}:
            assert after_rows[row["model_id"]] == row
        else:
            updated = copy.deepcopy(after_rows[row["model_id"]])
            updated["execution_identity_sha256"] = row["execution_identity_sha256"]
            updated["stages"][0]["image"] = row["stages"][0]["image"]
            assert updated == row
    for sha, ids in captured["qualification_baselines"].items():
        unchanged = [model for model in ids if model not in {"namd", "lammps"}]
        projected = release.digest({"schema": captured["schema"], "models": [after_rows[key] for key in unchanged]})
        assert desired["qualification_baselines"][projected] == unchanged
        if set(ids) & {"namd", "lammps"}:
            assert sha not in desired["qualification_baselines"]
        else:
            assert projected == sha
    assert desired.get("snapshot_bundles") == captured.get("snapshot_bundles")
    new_schedule = json.loads(cm["data"]["scheduling.json"])
    old_schedule = json.loads(args[1])
    del new_schedule["model_eligible_pool_ids"]["amber"]
    new_schedule["model_eligible_pool_ids"]["lammps"] = old_schedule["model_eligible_pool_ids"]["lammps"]
    assert new_schedule == old_schedule
    for model in ("namd", "lammps"):
        assert profiles[model]["execution_identity"] != initial[model]["execution_identity"]
        assert profiles[model]["qualification"]["public_completion_receipt_sha256"] is None
        assert profiles[model]["qualification"]["scheduler_eligibility_receipt_sha256"] is None
        assert profiles[model]["qualification"]["h100_semantic_receipt_sha256"] == release.digest(args[3][model])


@pytest.mark.parametrize("corruption", ["changed-row", "missing-row", "duplicate-membership", "duplicate-row"])
def test_successor_rejects_invalid_captured_proofs_before_rebasing(corruption):
    args, _ = successor_inputs()
    captured = args[0]["scientificBatch"]["executionMap"]
    sha = next(key for key, ids in captured["qualification_baselines"].items() if "namd" in ids)
    if corruption == "changed-row":
        next(row for row in captured["models"] if row["model_id"] == "namd")["execution_identity_sha256"] = "f" * 64
    elif corruption == "missing-row":
        captured["qualification_baselines"][sha].append("absent-app")
    elif corruption == "duplicate-membership":
        captured["qualification_baselines"][sha].append("namd")
    else:
        captured["models"].append(copy.deepcopy(captured["models"][0]))
    with pytest.raises(ValueError, match="qualification baseline|duplicate execution"):
        release.compose(*args, replace_models={"namd", "lammps"}, expand_qualified_pools={"lammps"})


def test_single_successor_only_proof_is_removed_not_transferred():
    args, _ = successor_inputs()
    captured = args[0]["scientificBatch"]["executionMap"]
    predecessor = next(row for row in captured["models"] if row["model_id"] == "namd")
    old_sha = release.digest({"schema": captured["schema"], "models": [predecessor]})
    captured["qualification_baselines"][old_sha] = ["namd"]
    _, overlay, _ = release.compose(
        *args, replace_models={"namd", "lammps"}, expand_qualified_pools={"lammps"}
    )
    proofs = overlay["scientificBatch"]["executionMap"]["qualification_baselines"]
    assert old_sha not in proofs
    assert [] not in proofs.values()


def test_publish_successors_rebases_only_unchanged_profile_references(tmp_path, monkeypatch):
    args, initial = successor_inputs()
    captured = args[0]["scientificBatch"]["executionMap"]
    profiles, overlay, _ = release.compose(
        *args, replace_models={"namd", "lammps"}, expand_qualified_pools={"lammps"}
    )
    target = tmp_path / "catalog/runtime/contracts"
    target.mkdir(parents=True)
    catalog = json.loads((SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json").read_text())
    catalog["profiles"] = [item for item in catalog["profiles"] if item["model_id"] not in release.MODELS]
    # Exercise profiles referencing the whole predecessor map, not only older
    # baselines that happen to omit the changed App.
    old_digest = release.digest({"schema": captured["schema"], "models": captured["models"]})
    for item in catalog["profiles"]:
        if item.get("route_exposed"):
            item["qualification"]["execution_map_sha256"] = old_digest
    catalog["profiles"].extend(initial.values())
    receipts = {"receipts": [json.loads((release.HERE / model / "activation/source-candidate-receipt.json").read_text())
                             for model in initial]}
    before = copy.deepcopy(catalog)
    for name, value in (
        ("scientific-execution-map.json", captured),
        ("scientific-workload-profiles.json", catalog),
        ("scientific-source-candidate-receipts.json", receipts),
    ):
        (target / name).write_text(json.dumps(value))
    monkeypatch.setattr(release, "ROOT", tmp_path)
    release.publish_catalog(profiles, overlay["scientificBatch"]["executionMap"], captured,
                            replace_models={"namd", "lammps"})
    published = json.loads((target / "scientific-workload-profiles.json").read_text())
    by_id = {item["model_id"]: item for item in published["profiles"]}
    for old in before["profiles"]:
        if old["model_id"] in initial:
            assert by_id[old["model_id"]] == profiles[old["model_id"]]
            continue
        new = copy.deepcopy(by_id[old["model_id"]])
        if old.get("route_exposed"):
            assert new["qualification"]["execution_map_sha256"] != old_digest
            new["qualification"]["execution_map_sha256"] = old_digest
        assert new == old
    published_map = json.loads((target / "scientific-execution-map.json").read_text())
    release.validate_profile_qualifications(published["profiles"], published_map)
    assert published_map.get("snapshot_bundles") == captured.get("snapshot_bundles")
    assert json.loads((target / "scientific-source-candidate-receipts.json").read_text())["receipts"][:2] == receipts["receipts"]


@pytest.mark.parametrize("corruption", ["nonmember", "unknown-proof", "changed-unrelated", "missing-projection"])
def test_profile_rebase_rejects_invalid_membership_or_unrelated_changes(corruption):
    args, _ = successor_inputs()
    captured = args[0]["scientificBatch"]["executionMap"]
    _, overlay, _ = release.compose(*args, replace_models={"namd", "lammps"}, expand_qualified_pools={"lammps"})
    desired = overlay["scientificBatch"]["executionMap"]
    rows = {row["model_id"]: row for row in captured["models"]}
    full = release.digest({"schema": captured["schema"], "models": captured["models"]})
    profile = {"model_id": "rfdiffusion", "route_exposed": True, "qualification": {"execution_map_sha256": full}}
    if corruption == "nonmember":
        reference = release.digest({"schema": captured["schema"], "models": [rows["namd"]]})
        captured["qualification_baselines"][reference] = ["namd"]
        profile["qualification"]["execution_map_sha256"] = reference
    elif corruption == "unknown-proof":
        profile["qualification"]["execution_map_sha256"] = "0" * 64
    elif corruption == "changed-unrelated":
        next(row for row in desired["models"] if row["model_id"] == "rfdiffusion")["variant_id"] = "changed"
        desired["qualification_baselines"] = {}
    else:
        desired["qualification_baselines"] = {}
    with pytest.raises(ValueError, match="qualification|unrelated"):
        release.rebase_profile_qualifications([profile], captured, desired, {"namd", "lammps"})


def test_final_profile_check_requires_membership_not_just_a_known_hash():
    args, _ = successor_inputs()
    captured = args[0]["scientificBatch"]["executionMap"]
    sha, ids = next(iter(captured["qualification_baselines"].items()))
    assert "namd" not in ids
    with pytest.raises(ValueError, match="membership"):
        release.validate_profile_qualifications(
            [{"model_id": "namd", "route_exposed": True, "qualification": {"execution_map_sha256": sha}}], captured
        )
