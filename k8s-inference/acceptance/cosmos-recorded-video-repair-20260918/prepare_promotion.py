"""Compose the approved Proteina and exact-size LeRobot successors offline.

No Kubernetes calls and no edits to canonical files. The captured rollback172
configuration must still match the pending Proteina proposal's original170
configuration. Only the reviewed Proteina delta may precede the LeRobot delta.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "acceptance"))
from scientific_runtime_successor import digest, indexed, prepare  # noqa: E402

MODEL = "cosmos3-lerobot-augmentation"
PENDING_MODEL = "proteina-complexa"
OLD = "sha256:dacf151564d8b3387e81ad2bd8fc227dab8b7493c92c1d63758356057f4083c9"
IMAGE = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-lerobot-augmentation@"
    "sha256:41a01714ed6d89b90a5b4a4527e2eb9907433f7932970544d2a712333996de04"
)
EVIDENCE_SHA = "9c7709fcebae90fa911f1e283f409da3e86ef4cb28e0226e4f87712e5e00b46b"
MEASURED_AT = "2026-09-19T00:44:47.539638499Z"
PENDING_IMAGE = "sha256:e5e075237a680dc01ace45b97ecfcfb9f95f5897aa51f36164591a74778a9cd1"
PENDING_EVIDENCE = "718a9d0e29fb0d0c65cec2f35b5a33776316acd94948599c456ad2965355d0bb"
ACTIVATION = "models/general-media/lerobot-augmentation/activation"
EXTRA_RECIPE = "models/general-media/lerobot-augmentation/runtime/Containerfile.exact-alignment"


def load(path):
    return json.loads(path.read_bytes())


def portable(execution):
    return {key: value for key, value in execution.items() if key != "snapshot_bundles"}


def verify_inputs(
    live_profiles,
    live_execution,
    rollback_profiles,
    rollback_execution,
    pending_profiles,
    pending_execution,
    source_profiles,
    source_execution,
):
    """Reject stale capture, unreviewed siblings, and lost snapshot records."""
    if live_profiles != rollback_profiles or live_execution != rollback_execution:
        raise ValueError("Captured live baseline differs from pending proposal rollback")
    if source_profiles != pending_profiles or portable(source_execution) != portable(pending_execution):
        raise ValueError("Canonical source differs from approved pending candidate")
    if source_execution.get("snapshot_bundles", live_execution.get("snapshot_bundles")) != live_execution.get(
        "snapshot_bundles"
    ):
        raise ValueError("Canonical source substitutes live snapshot records")
    if pending_execution.get("snapshot_bundles") != live_execution.get("snapshot_bundles"):
        raise ValueError("Pending candidate changes retained snapshot records")
    old_rows, rows = indexed(live_execution, "models"), indexed(pending_execution, "models")
    old_profiles, profiles = indexed(live_profiles, "profiles"), indexed(pending_profiles, "profiles")
    if set(old_rows) != set(rows) or set(rows) != set(profiles) or set(profiles) != set(old_profiles):
        raise ValueError("Pending candidate changes scientific App set")
    for mid in rows:
        if mid == PENDING_MODEL:
            continue
        if old_rows[mid] != rows[mid]:
            raise ValueError("Pending candidate changes unapproved sibling execution: " + mid)
        expected = copy.deepcopy(old_profiles[mid])
        expected["qualification"]["execution_map_sha256"] = profiles[mid]["qualification"]["execution_map_sha256"]
        if expected != profiles[mid]:
            raise ValueError("Pending candidate changes unapproved sibling profile: " + mid)
    target = profiles[PENDING_MODEL]
    if (
        target["execution_identity"]["runtime_image_digest"] != PENDING_IMAGE
        or target["qualification"]["h100_semantic_receipt_sha256"] != PENDING_EVIDENCE
        or target["qualification"]["public_completion_receipt_sha256"] is not None
    ):
        raise ValueError("Pending Proteina identity/evidence is not the reviewed successor")
    return {
        "live_matches_pending_rollback": True,
        "source_matches_pending_candidate": True,
        "pending_model": PENDING_MODEL,
        "pending_evidence_sha256": PENDING_EVIDENCE,
        "snapshot_bundles_preserved": len(live_execution.get("snapshot_bundles", {})),
    }


def validate_evidence(raw):
    if hashlib.sha256(raw).hexdigest() != EVIDENCE_SHA:
        raise ValueError("Require exact published-container alignment evidence")
    evidence = json.loads(raw)
    valid = evidence["validation"]
    if (
        evidence["runtime_image"] != IMAGE
        or evidence["status"] != "passed"
        or valid["nonvideo_values_compared"] != 6144
        or valid["nonvideo_values_exact"] is not True
        or (valid["episodes"], valid["frames"], valid["cameras"]) != (2, 128, 2)
        or len(evidence["positive_unchanged"]) != 2
        or not all(row["unchanged"] for row in evidence["positive_unchanged"])
        or len(evidence["rejected_mismatched_geometry"]) != 2
        or not all(row["code"] == "COSMOS_MEDIA_ALIGNMENT_INVALID" for row in evidence["rejected_mismatched_geometry"])
    ):
        raise ValueError("Exact-image CPU alignment/writer evidence incomplete")
    return evidence


def current_recipe(root, historical):
    if digest(historical["source_recipe"]) != historical["runtime_recipe_sha256"]:
        raise ValueError("Historical recipe digest is not bound")
    names = [row["path"] for row in historical["source_recipe"]["files"]]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate historical recipe path")
    names = sorted(set(names) | {EXTRA_RECIPE})
    files = []
    for name in names:
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Recipe path leaves solution")
        raw = path.read_bytes()
        files.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)})
    return {"schema": historical["source_recipe"]["schema"], "files": files}


def projection(schema, target, key, row):
    return {
        "schema": "fs2-serve.nebius.ai/" + schema + "/v1",
        "merge_target": "catalog/runtime/contracts/" + target,
        key: row,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--pending-proteina", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = validate_evidence(args.evidence.read_bytes())
    values, live_profiles = load(args.baseline / "values.json"), load(args.baseline / "profiles.json")
    live_execution = values["scientificBatch"]["executionMap"]
    pending_profiles = load(args.pending_proteina / "scientific-workload-profiles.json")
    pending_execution = load(args.pending_proteina / "scientific-execution-map.json")
    source_profiles = load(ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json")
    source_execution = load(ROOT / "catalog/runtime/contracts/scientific-execution-map.json")
    checks = verify_inputs(
        live_profiles,
        live_execution,
        load(args.pending_proteina / "rollback-profiles.json"),
        load(args.pending_proteina / "rollback-values.json")["scientificBatch"]["executionMap"],
        pending_profiles,
        pending_execution,
        source_profiles,
        source_execution,
    )
    historical = load(ROOT / ACTIVATION / "active-onboarding-20260918.json")
    recipe = current_recipe(ROOT, historical)
    profiles, execution, report = prepare(
        pending_profiles,
        pending_execution,
        model_id=MODEL,
        previous_digest=OLD,
        candidate_image=IMAGE,
        recipe_sha256=digest(recipe),
        semantic_receipt_sha256=EVIDENCE_SHA,
        measured_at=MEASURED_AT,
        limitations=[
            "Exact-size successor forwards recorded dimensions and rejects mismatched geometry, frame count or time "
            "base rather than silently resizing or retiming native output.",
            "The legacy h100_semantic_receipt field identifies exact-image CPU-coordinator checks using retained H100 "
            "child videos: 2 positive/2 negative alignment checks, 2 episodes, 128 frames, "
            "and all 6144 nonvideo values. "
            "Timestamp is receipt completion, not fresh GPU measurement.",
            "New coordinator remains active/unqualified pending exact public execution. Retained action values do not "
            "prove physical label validity; V2V generates future motion after a short prefix; "
            "full-sequence transfer is "
            "not guaranteed to preserve contact, camera calibration, or policy-training suitability.",
            "No old public completion, scheduler eligibility, whole-workflow snapshot or scientific-quality "
            "qualification is inherited by this successor.",
        ],
    )
    spec = importlib.util.spec_from_file_location(
        "cosmos_existing_scientific_validation", ROOT / "acceptance/openfold3-inline-20260918/promotion.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report["startup_validation"] = module.validate_startup(
        profiles,
        execution,
        values,
        load(args.baseline / "configmaps.json"),
        args.baseline / "scheduling.json",
        load(args.baseline / "gateway-deployment.json"),
    )
    complete_values = module.merge_values(values, execution)
    rendered, report["helm_validation"] = module.validate_helm(complete_values, execution)
    report.update(
        applied=False,
        composition_checks=checks,
        evidence_scope=evidence["qualification_scope"],
        evidence_timestamp_scope="CPU receipt completion; retained H100 children are earlier independent runs",
        baseline_sha256={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(args.baseline.glob("*.json"))
        },
        pending_proposal_sha256={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(args.pending_proteina.glob("*.json"))
        },
    )
    profile, row = indexed(profiles, "profiles")[MODEL], indexed(execution, "models")[MODEL]
    onboarding = {
        "schema": historical["schema"],
        "state": "active",
        "customer_ready": False,
        "runtime_image": IMAGE,
        "runtime_recipe_sha256": digest(recipe),
        "source_recipe": recipe,
        "execution_identity_sha256": profile["execution_identity"]["execution_identity_sha256"],
        "execution_map_sha256": profile["qualification"]["execution_map_sha256"],
        "semantic_evidence_sha256": EVIDENCE_SHA,
        "scope": evidence["qualification_scope"],
        "public_acceptance_pending": True,
    }
    documents = {
        "scientific-workload-profiles.json": profiles,
        "scientific-execution-map.json": execution,
        "source-execution-map.json": portable(execution),
        "workload-profile.json": projection(
            "scientific-workload-profile-projection", "scientific-workload-profiles.json", "profile", profile
        ),
        "execution-map.json": projection(
            "scientific-execution-map-projection", "scientific-execution-map.json", "model", row
        ),
        "active-onboarding-20260918.json": onboarding,
        "validation.json": report,
        "values.json": complete_values,
        "rendered-execution-map.json": rendered,
        "rollback-profiles.json": live_profiles,
        "rollback-execution.json": live_execution,
        "rollback-values.json": values,
    }
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, value in documents.items():
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(
        json.dumps(
            {
                "applied": False,
                "startup": "passed",
                "models": len(execution["models"]),
                "execution_map_sha256": report["execution_map_sha256"],
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
