"""Prepare preset registration plus optional snapshot against a stable release.

All public owners retain their current snapshot policy. A separate, unapplied
qualification proposal requests strict restore only after ordinary public
worker acceptance. The coordinator is responsible for every live mutation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path

import prepare_presets as presets

from fs2_serve.model_deployment import canonical_digest, canonical_json
from fs2_serve.serving_snapshot import ServingSnapshotBundle

base = presets.base


def extend(envelope, bundles, owner, snapshot, report):
    checked = ServingSnapshotBundle.model_validate(snapshot)
    if checked.qualification_receipt_sha256 != hashlib.sha256(report).hexdigest():
        raise ValueError("optional snapshot does not bind its measured report")
    proof = json.loads(report)
    if not proof.get("renderer_qualification") or proof["production_selectable"] is not False:
        raise ValueError("require completed renderer proof without an inherited public-ready claim")
    if owner["spec"]["cache"]["snapshotPreference"] != "Never":
        raise ValueError("coordinate public snapshot selection before preparing this registration")
    candidate, bundles, proposal = presets.extend(envelope, bundles, owner, base.adapter_source(), proof)
    registry = candidate["qualifications"][base.MODEL]["gpuSnapshotBundles"]
    if checked.bundle_id in registry:
        raise ValueError("optional snapshot is already registered; inspect current state")
    registry[checked.bundle_id] = copy.deepcopy(snapshot)
    candidate.pop("revision")
    candidate["revision"] = canonical_digest(candidate)
    strict = copy.deepcopy(proposal)
    strict["spec"]["cache"]["snapshotPreference"] = "Require"
    strict["spec"]["cache"]["snapshotRef"] = {
        "name": checked.bundle_id,
        "digest": "sha256:" + checked.manifest_sha256,
        "strategy": "CudaCheckpoint",
    }
    return candidate, bundles, proposal, strict


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--expected-release", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    capture = base.shared.read(args.baseline / "capture.json")
    if capture["release"]["version"] != args.expected_release or capture["release"]["status"] != "deployed":
        raise ValueError("baseline is not the selected deployed release")
    for name, digest in capture["sha256"].items():
        if base.shared.sha(args.baseline / name) != digest:
            raise ValueError("captured baseline changed: " + name)
    maps = base.shared.read(args.baseline / "live-configmaps.json")["items"]

    def document(key):
        return json.loads(next(cm["data"][key] for cm in maps if key in cm.get("data", {})))

    original, old_bundles = document("infrastructure-envelope.json"), document("renderer-bundles.json")
    owners = base.shared.read(args.baseline / "modeldeployments.json")["items"]
    owner = next(row for row in owners if row["metadata"]["name"] == base.MODEL)
    directory = Path(__file__).with_name("warmed-snapshot")
    snapshot = base.shared.read(directory / "bundle.json")
    candidate, bundles, proposal, strict = extend(
        original, old_bundles, owner, snapshot, (directory / "qualification.json").read_bytes()
    )
    checks = base.existing.validate_candidate(original, candidate, bundles, owners, [proposal])
    strict_checks = base.existing.validate_candidate(original, candidate, bundles, owners, [strict])
    changed = [
        base.existing.configmap(
            "fs2-science-envelope-", {"infrastructure-envelope.json": canonical_json(candidate).decode()}
        ),
        base.existing.configmap("fs2-science-bundles-", {"renderer-bundles.json": canonical_json(bundles).decode()}),
    ]
    values = {
        "modelController": {
            "infrastructureEnvelopeConfigMapName": changed[0]["metadata"]["name"],
            "rendererBundlesConfigMapName": changed[1]["metadata"]["name"],
        }
    }
    rollback = {
        "modelController": {
            "infrastructureEnvelopeConfigMapName": capture["configmaps"]["model-controller-envelope"],
            "rendererBundlesConfigMapName": capture["configmaps"]["model-controller-bundles"],
        }
    }
    outputs = {
        "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": changed},
        "values.json": values,
        "rollback-values.json": rollback,
        "app-proposals.json": [proposal],
        "strict-qualification-proposal-not-applied.json": [strict],
        "rollback-app-proposals.json": [
            {"name": owner["metadata"]["name"], "namespace": owner["metadata"]["namespace"], "spec": owner["spec"]}
        ],
        "validation.json": {
            "applied": False,
            "baseline": capture,
            "validation": checks,
            "strict_preview_validation": strict_checks,
            "routes_admin_and_scientific_execution_map_unchanged": True,
            "snapshot_policy_remains_never": True,
            "historical_templates_and_snapshots_preserved": True,
            "public_snapshot_qualification_inherited": False,
        },
    }
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, value in outputs.items():
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps({"applied": False, "values": values, "snapshot_policy": "Never"}))


if __name__ == "__main__":
    main()
