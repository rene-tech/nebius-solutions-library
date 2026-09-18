"""Prepare image-only scientific successors without inheriting old qualification.

Existing execution rows and snapshot records are immutable evidence. When a
whole-map reference includes the changed model, retain a content-addressed
projection of the unchanged siblings; do not relabel old public receipts as
new-runtime acceptance.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def indexed(document, key):
    result = {row["model_id"]: row for row in document[key]}
    if len(result) != len(document[key]):
        raise ValueError("Duplicate scientific model identity")
    return result


def prepare(profiles, execution, *, model_id, previous_digest, candidate_image,
            recipe_sha256, semantic_receipt_sha256, measured_at, limitations):
    """Return active/unqualified candidate plus an explicit unchanged-row proof."""
    candidate_digest = candidate_image.rsplit("@", 1)[-1]
    for value in (previous_digest, candidate_digest):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
            raise ValueError("Require exact previous and candidate image digests")
    for value in (recipe_sha256, semantic_receipt_sha256):
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("Require immutable recipe and semantic evidence")
    if "@" not in candidate_image or candidate_digest == previous_digest:
        raise ValueError("Require a distinct immutable candidate image")
    before_profiles = indexed(profiles, "profiles")
    before_rows = indexed(execution, "models")
    if set(before_profiles) != set(before_rows) or model_id not in before_rows:
        raise ValueError("Scientific profile/execution model sets differ")
    baselines = execution.get("qualification_baselines", {})
    normal_before = digest({"schema": execution["schema"], "models": execution["models"]})
    for sha, ids in baselines.items():
        if digest({"schema": execution["schema"], "models": [before_rows[mid] for mid in ids]}) != sha:
            raise ValueError("Historical qualification baseline was changed")
    for mid, profile in before_profiles.items():
        ref = profile["qualification"]["execution_map_sha256"]
        if ref != normal_before and mid not in baselines.get(ref, []):
            raise ValueError("Original profile qualification reference is unbound: " + mid)
    profiles, execution = copy.deepcopy((profiles, execution))
    by_profile, rows = indexed(profiles, "profiles"), indexed(execution, "models")
    profile = by_profile[model_id]
    identity = profile["execution_identity"]
    if identity["runtime_image_digest"] != previous_digest:
        raise ValueError("Previous runtime image changed")
    changed_stages = []
    for stage in rows[model_id]["stages"]:
        if stage["image"].endswith("@" + previous_digest):
            stage["image"] = candidate_image
            changed_stages.append(stage["stage_id"])
    if not changed_stages:
        raise ValueError("No stage uses the previous runtime image")
    identity.update(runtime_image_digest=candidate_digest, runtime_recipe_sha256=recipe_sha256)
    identity["execution_identity_sha256"] = digest({k: v for k, v in identity.items()
                                                    if k != "execution_identity_sha256"})
    rows[model_id]["execution_identity_sha256"] = identity["execution_identity_sha256"]
    siblings = [row["model_id"] for row in execution["models"] if row["model_id"] != model_id]
    if any(canonical(rows[mid]) != canonical(before_rows[mid]) for mid in siblings):
        raise ValueError("Sibling execution row changed")
    sibling_sha = digest({"schema": execution["schema"], "models": [rows[mid] for mid in siblings]})
    execution["qualification_baselines"] = {key: ids for key, ids in baselines.items() if model_id not in ids}
    execution["qualification_baselines"][sibling_sha] = siblings
    references = []
    for mid in siblings:
        prior = by_profile[mid]["qualification"]["execution_map_sha256"]
        by_profile[mid]["qualification"]["execution_map_sha256"] = sibling_sha
        references.append({"model_id": mid, "previous": prior, "successor": sibling_sha,
                           "unchanged_execution_row_sha256": digest(rows[mid])})
    normal_after = digest({"schema": execution["schema"], "models": execution["models"]})
    profile["state"] = profile["semantic_validation"]["state"] = "active"
    profile["qualification"].update(h100_semantic_receipt_sha256=semantic_receipt_sha256,
        public_completion_receipt_sha256=None, scheduler_eligibility_receipt_sha256=None,
        execution_map_sha256=normal_after, qualified_at=measured_at)
    profile["policy"]["limitations"].extend(limitations)
    return profiles, execution, {
        "model_id": model_id, "previous_image_digest": previous_digest,
        "candidate_image": candidate_image, "changed_stage_ids": changed_stages,
        "sibling_reference_rebase": references, "sibling_projection_sha256": sibling_sha,
        "candidate_normal_execution_sha256": normal_after,
        "execution_map_sha256": digest(execution), "semantic_receipt_sha256": semantic_receipt_sha256,
        "historical_baselines": baselines, "new_public_or_snapshot_qualification": False,
    }
