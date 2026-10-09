"""Prepare the exact recorded-data writer successor; no live mutations."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "acceptance"))
from scientific_runtime_successor import digest, prepare  # noqa: E402

MODEL = "cosmos3-lerobot-augmentation"
OLD = "sha256:1eeb26e239243c3c088b1ce9cb184f6cde9639100a6583d0d39b45d787cd85a1"
IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-lerobot-augmentation@sha256:dacf151564d8b3387e81ad2bd8fc227dab8b7493c92c1d63758356057f4083c9"
EVIDENCE_SHA = "2897d1da60ac53d1703ce43f3fe5e9dbf3225afa5de34017a6778de151e5818f"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.evidence.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EVIDENCE_SHA:
        raise ValueError("Require exact published-container writer evidence")
    evidence = json.loads(raw)
    if (evidence["runtime_image"] != IMAGE or evidence["status"] != "passed"
            or evidence["validation"]["nonvideo_values_compared"] != 6144
            or evidence["validation"]["nonvideo_values_exact"] is not True):
        raise ValueError("Recorded-data writer qualification incomplete")
    values = json.loads((args.baseline / "values.json").read_bytes())
    original_profiles = json.loads((ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json").read_bytes())
    original_execution = json.loads((ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_bytes())
    source_keys = set(original_execution)
    # Cluster-specific checkpoint locations stay in retained Helm values, not
    # the portable library catalog. They are preserved exactly in the live map.
    if "snapshot_bundles" not in original_execution:
        original_execution["snapshot_bundles"] = copy.deepcopy(values["scientificBatch"]["executionMap"].get("snapshot_bundles", {}))
    if digest(original_execution) != digest(values["scientificBatch"]["executionMap"]):
        raise ValueError("Source execution map differs from exact live baseline")
    activation = ROOT / "models/general-media/lerobot-augmentation/activation"
    historical = json.loads((activation / "active-onboarding-20260918.json").read_bytes())
    recipe = historical["source_recipe"]
    if digest(recipe) != historical["runtime_recipe_sha256"]:
        raise ValueError("Historical LeRobot recipe is not bound to its recorded digest")
    recipe = {"schema": recipe["schema"], "files": [
        {"path": row["path"], "sha256": hashlib.sha256((ROOT / row["path"]).read_bytes()).hexdigest(),
         "size_bytes": (ROOT / row["path"]).stat().st_size} for row in recipe["files"]]}
    profiles, execution, report = prepare(original_profiles, original_execution,
        model_id=MODEL, previous_digest=OLD, candidate_image=IMAGE,
        recipe_sha256=digest(recipe), semantic_receipt_sha256=EVIDENCE_SHA,
        measured_at=evidence["container_finished_at"], limitations=[
            "Recorded-data writer successor normalizes scalar storage to declared singleton shape without changing numeric values. Exact published CPU coordinator reopened two episodes/128 frames and preserved all 6144 nonvideo values using retained successful H100 child videos.",
            "The legacy h100_semantic_receipt field here identifies explicitly scoped CPU-coordinator plus retained-H100-child evidence, not fresh whole-public-path qualification. New image is active/unqualified until public completion and scheduler receipts are captured.",
            "Old release150 acceptance describes its historical image only and is not inherited by this successor. No new snapshot, physical motion fidelity or maximum-bound throughput qualification is claimed.",
        ])
    path = ROOT / "acceptance/openfold3-inline-20260918/promotion.py"
    spec = importlib.util.spec_from_file_location("existing_scientific_validation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report["startup_validation"] = module.validate_startup(profiles, execution, values,
        json.loads((args.baseline / "configmaps.json").read_bytes()), args.baseline / "scheduling.json",
        json.loads((args.baseline / "gateway-deployment.json").read_bytes()))
    complete_values = module.merge_values(values, execution)
    rendered, report["helm_validation"] = module.validate_helm(complete_values, execution)
    profile = next(row for row in profiles["profiles"] if row["model_id"] == MODEL)
    projection = {"schema": "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1",
        "merge_target": "catalog/runtime/contracts/scientific-workload-profiles.json", "profile": profile}
    onboarding = {"schema": historical["schema"], "state": "active", "customer_ready": False,
        "runtime_image": IMAGE, "runtime_recipe_sha256": digest(recipe), "source_recipe": recipe,
        "execution_identity_sha256": profile["execution_identity"]["execution_identity_sha256"],
        "execution_map_sha256": profile["qualification"]["execution_map_sha256"],
        "semantic_evidence_sha256": EVIDENCE_SHA, "scope": evidence["scope"],
        "public_acceptance_pending": True}
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    documents = {"scientific-workload-profiles.json": profiles, "scientific-execution-map.json": execution,
        "source-execution-map.json": {key: value for key, value in execution.items() if key in source_keys},
        "workload-profile.json": projection, "active-onboarding-20260918.json": onboarding,
        "validation.json": report, "values.json": complete_values, "rendered-execution-map.json": rendered,
        "rollback-profiles.json": original_profiles, "rollback-execution.json": original_execution}
    for name, value in documents.items():
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps({"models": len(execution["models"]), "startup": "passed", "applied": False,
        "execution_map_sha256": report["execution_map_sha256"]}))


if __name__ == "__main__":
    main()
