#!/usr/bin/env python3
"""Bind a verified independent ESM capture for isolated paired trials.

This emits an unqualified candidate, never a selectable production bundle.
Completed measured semantic receipts are required for subsequent promotion.
"""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "donor", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--model", choices=("esmfold2", "esmfold2-fast"), required=True)
    parser.add_argument("--node", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[4]
    profile = next(item for item in json.loads(
        (root / "catalog/runtime/contracts/scientific-workload-profiles.json").read_bytes()
    )["profiles"] if item["model_id"] == args.model)
    manifest = json.loads(args.manifest.read_bytes())
    identity = manifest["compatibility"]["runtime_identity"]
    assert identity["runtime_id"] == args.model
    assert identity["runtime_image"].split("@", 1)[1] == profile["execution_identity"]["runtime_image_digest"]
    donor = json.loads(args.donor.read_bytes())
    runtime = next(c for c in donor["spec"]["containers"] if c["name"] == "scientific-stage")
    directory = runtime["command"][runtime["command"].index("--directory") + 1].removeprefix("/checkpoints/")
    source_cm = next(v["configMap"]["name"] for v in donor["spec"]["volumes"] if v["name"] == "snapshot-source")
    worker_log = next(row for row in manifest["manifest"]["files"] if row["path"] == "worker.log")
    config = {
        "schema": "fs2-serve.nebius.ai/scientific-snapshot-bundle/v1",
        "bundle_id": args.model + "-h100-cuda-criu-20260907-r2",
        "model_id": args.model, "stage_id": "fold",
        "model_revision": identity["model_revision"],
        "profile_model_revision": profile["execution_identity"]["model_revision"],
        "runtime_image": identity["runtime_image"], "tools_image": identity["tools_image"],
        "source_configmap": source_cm, "source_sha256": identity["snapshot_source_sha256"],
        "cli_configmap": source_cm, "cli_sha256": identity["snapshot_source_sha256"]["run_esmfold2.py"],
        "pvc": "fs2-fleet-snapshots-rwx-r20260907", "bundle_path": directory,
        "manifest_sha256": manifest["bundle_sha256"], "qualification_receipt_sha256": None,
        "qualified": False, "python": "/opt/esm/.pixi/envs/gpu/bin/python",
        "cli_path": "/opt/fs2/run_esmfold2.py", "cli_key": "run_esmfold2.py",
        "worker_variable": "FS2_ESMFOLD2_WORKER_URL",
        "compatibility": {key: identity[key] for key in (
            "gpu_name", "compute_capability", "driver_version", "kernel_release",
        )},
        "worker_log": {key: worker_log[key] for key in ("sha256", "bytes", "mode", "uid", "gid")},
        "node": args.node,
    }
    args.output.write_text(json.dumps(config, indent=2) + "\n")


if __name__ == "__main__":
    main()
