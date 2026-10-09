#!/usr/bin/env python3
"""Render an additive LeRobot activation from an exact live Helm baseline.

No cluster mutations. Existing scientific rows, snapshot bundles, all other
Helm values (including speech/storage/serving catalog), and scheduler fields
are preserved. The owner applies the new content-addressed scheduler ConfigMap
and merges only the returned scientificBatch Helm overlay.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

import yaml

MODEL = "cosmos3-lerobot-augmentation"
ROOT = Path(__file__).resolve().parents[4]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def build_overlay(values, scheduling_raw: bytes, execution, *, replace_lerobot=False):
    current = values["scientificBatch"]
    if hashlib.sha256(scheduling_raw).hexdigest() != current["schedulingContractSha256"]:
        raise ValueError("scheduler bytes differ from the captured Helm identity")
    live = current["executionMap"]
    if isinstance(live, str):
        live = json.loads(live)
    desired = copy.deepcopy(execution)
    if set(live) - {"schema", "models", "snapshot_bundles", "qualification_baselines"}:
        raise ValueError("unsupported live execution fields; preserve them explicitly before proceeding")
    live_rows = {row["model_id"]: row for row in live["models"]}
    new_rows = {row["model_id"]: row for row in desired["models"]}
    if len(live_rows) != len(live["models"]) or len(new_rows) != len(desired["models"]):
        raise ValueError("duplicate model identity")
    if set(new_rows) != set(live_rows) | {MODEL} or live["schema"] != desired["schema"]:
        raise ValueError("activation must append only LeRobot and preserve every existing model")
    for key, value in live_rows.items():
        if new_rows[key] == value:
            continue
        if key != MODEL or not replace_lerobot:
            raise ValueError("activation changes an existing scientific execution row")
        # An explicit successor upgrade may replace only this coordinator's
        # immutable image and execution identity. It cannot alter resources,
        # placement, mounts, stage structure, or any sibling scientific row.
        prior, successor = copy.deepcopy(value), copy.deepcopy(new_rows[key])
        for candidate in (prior, successor):
            if not candidate.get("execution_identity_sha256"):
                raise ValueError("LeRobot replacement requires an execution identity")
            candidate.pop("execution_identity_sha256")
            stages = candidate.get("stages", [])
            if len(stages) != 1 or not isinstance(stages[0], dict) or not stages[0].get("image"):
                raise ValueError("LeRobot replacement requires one pinned coordinator stage")
            stages[0].pop("image")
        if prior != successor:
            raise ValueError("LeRobot replacement changes fields beyond image and execution identity")
    if "snapshot_bundles" in live:
        desired["snapshot_bundles"] = copy.deepcopy(live["snapshot_bundles"])
    for digest, ids in live.get("qualification_baselines", {}).items():
        if desired.get("qualification_baselines", {}).get(digest) != ids:
            raise ValueError("activation removes an existing qualification baseline")
    for digest, ids in desired.get("qualification_baselines", {}).items():
        projected = {"schema": desired["schema"], "models": [new_rows[key] for key in ids]}
        if hashlib.sha256(canonical(projected)).hexdigest() != digest:
            raise ValueError("activation does not exactly preserve qualified execution baseline")

    scheduling = json.loads(scheduling_raw)
    pools = scheduling["model_eligible_pool_ids"].get("cosmos3-nano")
    if not isinstance(pools, list) or not pools:
        raise ValueError("existing Cosmos scheduler pool eligibility is absent")
    if MODEL in scheduling["model_eligible_pool_ids"] and scheduling["model_eligible_pool_ids"][MODEL] != pools:
        raise ValueError("existing LeRobot eligibility differs; explicit owner review required")
    scheduling["model_eligible_pool_ids"][MODEL] = list(pools)
    payload = canonical(scheduling)
    digest = hashlib.sha256(payload).hexdigest()
    name = current["schedulingContractConfigMapName"].rsplit("-", 1)[0] + "-" + digest[:12]
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": current["schedulingContractNamespace"]},
        "data": {current["schedulingContractKey"]: payload.decode()},
    }
    overlay = {
        "scientificBatch": {
            "executionMap": desired,
            "schedulingContractConfigMapName": name,
            "schedulingContractSha256": digest,
        }
    }
    return overlay, cm


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-values", type=Path, required=True)
    parser.add_argument("--baseline-scheduling", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--replace-lerobot", action="store_true",
        help="Explicitly allow only the existing LeRobot image/execution-identity replacement",
    )
    args = parser.parse_args()
    values = yaml.safe_load(args.baseline_values.read_text())
    execution = json.loads((ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text())
    overlay, cm = build_overlay(
        values, args.baseline_scheduling.read_bytes(), execution,
        replace_lerobot=args.replace_lerobot,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("scientific-activation.values.json", overlay), ("scientific-scheduling.configmap.json", cm)):
        path = args.output_dir / name
        if path.exists():
            raise ValueError(f"refusing to overwrite retained candidate: {path}")
        path.write_bytes(canonical(value) + b"\n")
    print(
        json.dumps(
            {
                "scheduler_configmap": cm["metadata"]["name"],
                "scientific_models": len(overlay["scientificBatch"]["executionMap"]["models"]),
                "retained_snapshot_bundles": len(
                    overlay["scientificBatch"]["executionMap"].get("snapshot_bundles", {})
                ),
                "other_helm_values_changed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
