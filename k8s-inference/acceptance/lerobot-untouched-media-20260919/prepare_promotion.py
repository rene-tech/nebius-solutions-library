"""Prepare Q64 from a retained live scientific baseline; never mutate Kubernetes."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "acceptance"))
from scientific_runtime_successor import digest, prepare  # noqa: E402

MODEL = "cosmos3-lerobot-augmentation"
ACTIVATION = ROOT / "models/general-media/lerobot-augmentation/activation"


def validate_evidence(publication, evidence):
    if (evidence.get("status") != "passed" or evidence.get("runtime_image") != publication["runtime_image"]
            or evidence.get("dataset_source_sha256") != publication["context_files"]["src/fs2_lerobot_augmentation/dataset.py"]
            or evidence.get("frames") != 128 or evidence.get("decoded_frames_each") != 256
            or evidence.get("nonvideo_values_exact") != 6144
            or evidence.get("thread_settings", {}).get("torch_intraop") != 4
            or evidence.get("thread_settings", {}).get("environment") != {
                "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4"}
            or evidence.get("selected_reference_and_data_shard_bytes_unchanged") is not True
            or evidence.get("public_end_to_end_qualified") is not False):
        raise ValueError("Require exact published-image recorded-dataset preservation evidence")
    media = evidence.get("media", [])
    expected = {(episode, camera) for episode in (0, 1) for camera in (
        "observation.images.cam_high", "observation.images.cam_right_wrist")}
    if len(media) != 4 or {(item["episode_index"], item["camera"]) for item in media} != expected:
        raise ValueError("Require every recorded episode/camera pair")
    for item in media:
        if any(not isinstance(item.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", item[key])
               for key in ("source_mp4_sha256", "output_mp4_sha256")):
            raise ValueError("Require immutable source and returned MP4 digests")
        selected = (item["episode_index"], item["camera"]) == (0, "observation.images.cam_high")
        if item.get("frames") != 64 or item.get("selected") is not selected:
            raise ValueError("Recorded selection/frame count differs")
        if selected:
            if item.get("changed_frames") != 64:
                raise ValueError("Selected generation did not change every recorded frame")
        elif (item.get("changed_frames") != 0 or item.get("maximum_absolute_pixel_difference") != 0
              or item.get("source_mp4_sha256") != item.get("output_mp4_sha256")):
            raise ValueError("Untouched media is not byte/pixel exact")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    publication = json.loads(args.publication.read_bytes())
    raw = args.evidence.read_bytes()
    evidence = json.loads(raw)
    validate_evidence(publication, evidence)
    evidence_sha = hashlib.sha256(raw).hexdigest()
    values = json.loads((args.baseline / "values.json").read_bytes())
    original_profiles = json.loads((args.baseline / "profiles.json").read_bytes())
    original_execution = copy.deepcopy(values["scientificBatch"]["executionMap"])
    source_profiles = json.loads((ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json").read_bytes())
    source_execution = json.loads((ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_bytes())
    if original_profiles != source_profiles or source_execution != {
        key: value for key, value in original_execution.items() if key in source_execution
    }:
        raise ValueError("Source scientific catalog differs from the retained live baseline")
    historical = json.loads((ACTIVATION / "active-onboarding-20260918.json").read_bytes())
    if digest(historical["source_recipe"]) != historical["runtime_recipe_sha256"]:
        raise ValueError("Historical source recipe identity is unbound")
    paths = {row["path"] for row in historical["source_recipe"]["files"]}
    paths.add("models/general-media/lerobot-augmentation/runtime/Containerfile.untouched-media")
    recipe = {"schema": historical["source_recipe"]["schema"], "files": [
        {"path": path, "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest(),
         "size_bytes": (ROOT / path).stat().st_size} for path in sorted(paths)]}
    previous = next(row for row in original_profiles["profiles"] if row["model_id"] == MODEL)
    profiles, execution, report = prepare(
        original_profiles, original_execution, model_id=MODEL,
        previous_digest=previous["execution_identity"]["runtime_image_digest"],
        candidate_image=publication["runtime_image"], recipe_sha256=digest(recipe),
        semantic_receipt_sha256=evidence_sha, measured_at=evidence["container_finished_at"], limitations=[
            "Q64 exact published CPU coordinator preserves untouched source MP4 bytes, original offsets and every decoded pixel after packaging/reload on a 128-row recorded dataset with retained public GPU generation; no new inference.",
            "The legacy h100_semantic_receipt field binds CPU-coordinator/retained-GPU-media evidence only. Fresh public completion and scheduler evidence are pending; customer_ready remains false.",
            "Temporary redundant encoding precedes source-shard preservation. No physical action alignment, training quality, maximum-size throughput or new snapshot qualification is claimed.",
        ])
    path = ROOT / "acceptance/openfold3-inline-20260918/promotion.py"
    spec = importlib.util.spec_from_file_location("q64_existing_validation", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    report["startup_validation"] = module.validate_startup(
        profiles, execution, values, json.loads((args.baseline / "configmaps.json").read_bytes()),
        args.baseline / "scheduling.json", json.loads((args.baseline / "gateway-deployment.json").read_bytes()))
    complete_values = module.merge_values(values, execution)
    rendered, report["helm_validation"] = module.validate_helm(complete_values, execution)
    assert execution.get("snapshot_bundles") == original_execution.get("snapshot_bundles")
    report["snapshot_bundles_unchanged"] = True
    profile = next(row for row in profiles["profiles"] if row["model_id"] == MODEL)
    model = next(row for row in execution["models"] if row["model_id"] == MODEL)
    projection = json.loads((ACTIVATION / "workload-profile.json").read_bytes())
    projection["profile"] = profile
    execution_projection = json.loads((ACTIVATION / "execution-map.json").read_bytes())
    execution_projection["model"] = model
    onboarding = {"schema": historical["schema"], "state": "active", "customer_ready": False,
                  "runtime_image": publication["runtime_image"], "runtime_recipe_sha256": digest(recipe),
                  "source_recipe": recipe, "execution_identity_sha256": profile["execution_identity"]["execution_identity_sha256"],
                  "execution_map_sha256": profile["qualification"]["execution_map_sha256"],
                  "semantic_evidence_sha256": evidence_sha, "scope": evidence["scope"], "public_acceptance_pending": True}
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    documents = {"scientific-workload-profiles.json": profiles, "scientific-execution-map.json": execution,
                 "source-execution-map.json": {key: value for key, value in execution.items() if key in source_execution},
                 "workload-profile.json": projection, "execution-map.json": execution_projection,
                 "active-onboarding-20260918.json": onboarding, "validation.json": report,
                 "values.json": complete_values, "rendered-execution-map.json": rendered,
                 "rollback-profiles.json": original_profiles, "rollback-execution.json": original_execution}
    for name, value in documents.items():
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps({"models": len(execution["models"]), "startup": "passed", "applied": False,
                      "execution_map_sha256": report["execution_map_sha256"], "recipe_sha256": digest(recipe)}))


if __name__ == "__main__":
    main()
