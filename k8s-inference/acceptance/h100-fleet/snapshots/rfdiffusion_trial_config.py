#!/usr/bin/env python3
"""Render an unqualified RF model-only fresh-Pod restore for real input tests."""

import argparse
import json
import os
from pathlib import Path
import subprocess

from render_scientific_restore import scientific_restore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "donor", "source", "directory", "kubeconfig"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("name", "node", "pvc", "bundle-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--render-only", action="store_true", help="Prepare a paired-trial configuration without creating a Pod")
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    manifest = json.loads(args.manifest.read_bytes())
    donor = json.loads(args.donor.read_bytes())
    identity = manifest["compatibility"]["runtime_identity"]
    assert identity["runtime_id"] == "rfdiffusion"
    runtime = next(item for item in donor["spec"]["containers"] if item["name"] == "scientific-stage")
    directory = runtime["command"][runtime["command"].index("--directory") + 1].removeprefix("/checkpoints/")
    source_cm = next(item["configMap"]["name"] for item in donor["spec"]["volumes"] if item["name"] == "snapshot-source")
    log = next(item for item in manifest["manifest"]["files"] if item["path"] == "worker.log")
    config = {"schema": "fs2-serve.nebius.ai/scientific-snapshot-bundle/v1", "model_id": "rfdiffusion",
        "stage_id": "inference", "bundle_id": args.bundle_id,
        "model_revision": identity["model_revision"], "runtime_image": identity["runtime_image"],
        "tools_image": identity["tools_image"], "source_configmap": source_cm,
        "source_sha256": identity["snapshot_source_sha256"], "cli_configmap": source_cm,
        "cli_sha256": identity["snapshot_source_sha256"]["rfdiffusion_cli_proxy.py"],
        "pvc": args.pvc, "bundle_path": directory, "manifest_sha256": manifest["bundle_sha256"],
        "qualification_receipt_sha256": None, "qualified": False, "python": "/opt/conda/bin/python",
        "cli_path": "/opt/fs2/runtime_entrypoint.py", "cli_key": "rfdiffusion_cli_proxy.py",
        "worker_variable": "FS2_RFDIFFUSION_WORKER_URL", "request_mode": "server",
        "compatibility": {key: identity[key] for key in ("gpu_name", "compute_capability", "driver_version", "kernel_release")},
        "worker_log": {key: log[key] for key in ("sha256", "bytes", "mode", "uid", "gid")}, "node": args.node}
    (args.directory / "trial-config.json").write_text(json.dumps(config, indent=2))
    pod = scientific_restore(json.loads(args.source.read_bytes()), {**config, "name": args.name})
    runtime = next(item for item in pod["spec"]["containers"] if item["name"] == "scientific-stage")
    runtime["command"][runtime["command"].index("--fallback") + 1] = "fail"
    (args.directory / "pod-private.json").write_text(json.dumps(pod))
    if args.render_only:
        return
    kube = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", "k8s-inference-h100", "-n", "fs2-models"]
    subprocess.run([*kube, "create", "--dry-run=client", "-f", "-"], input=json.dumps(pod), text=True, check=True)
    subprocess.run([*kube, "create", "-f", "-"], input=json.dumps(pod), text=True, check=True)


if __name__ == "__main__":
    main()
