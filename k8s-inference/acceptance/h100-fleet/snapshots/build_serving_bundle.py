#!/usr/bin/env python3
"""Bind completed matched evidence to the production serving snapshot schema."""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in (
        "report",
        "donor",
        "deployment",
        "model-deployment",
        "entrypoint",
        "output",
    ):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--accelerator-class", required=True)
    parser.add_argument("--bundle-id", required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_bytes())
    if report["status"] != "fresh-pod-qualified":
        raise ValueError("completed matched restore qualification is required")
    donor = json.loads(args.donor.read_bytes())
    native = json.loads(args.deployment.read_bytes())
    model = json.loads(args.model_deployment.read_bytes())["spec"]
    runtime = next(
        item for item in donor["spec"]["containers"] if item["name"] == args.container
    )
    source = next(
        item
        for item in native["spec"]["template"]["spec"]["containers"]
        if item["name"] == args.container
    )
    original = runtime["command"][runtime["command"].index("--") + 1 :]
    if original[1] != "/snapshot-source/serving_launcher.py":
        raise ValueError("captured runtime must use the measured CPU-loop variant")
    original = original[2:]
    image_entrypoint = (
        source.get("command") or original[: len(original) - len(source.get("args", []))]
    )
    if image_entrypoint + source.get("args", []) != original:
        raise ValueError("captured and original production argv differ")
    identity = report["compatibility"]["runtime_identity"]
    if (
        model["runtime"]["image"] != identity["runtime_image"]
        or model["artifact"]["revision"] != identity["model_revision"]
    ):
        raise ValueError("current ModelDeployment differs from measured runtime")
    directory = runtime["command"][runtime["command"].index("--directory") + 1]
    bundle = {
        "schema": "fs2-serve.nebius.ai/serving-snapshot-bundle/v1",
        "bundle_id": args.bundle_id,
        "model_ref": report["model_id"],
        "runtime_image": identity["runtime_image"],
        "model_revision": identity["model_revision"],
        "artifact_manifest_digest": model["artifact"]["manifestDigest"],
        "manifest_sha256": report["bundle"]["manifest_sha256"],
        "qualification_receipt_sha256": hashlib.sha256(
            args.report.read_bytes()
        ).hexdigest(),
        "qualified": True,
        "accelerator_classes": [args.accelerator_class],
        "compatibility": {
            key: identity[key]
            for key in (
                "gpu_name",
                "compute_capability",
                "driver_version",
                "kernel_release",
            )
        },
        "tools_image": identity["tools_image"],
        "source_configmap": "fs2-fleet-snapshot-serving-v7",
        "source_sha256": identity["snapshot_source_sha256"],
        "entrypoint_configmap": "fs2-fleet-snapshot-serving-entrypoint-v1",
        "entrypoint_sha256": hashlib.sha256(args.entrypoint.read_bytes()).hexdigest(),
        "network_configmap": "fs2-fleet-snapshot-net-tools-v1",
        "address_configmap": "fs2-fleet-snapshot-address-v1",
        "pvc": "fs2-fleet-snapshots-rwx-r20260907",
        "bundle_path": directory.removeprefix("/checkpoints/"),
        "captured_pod_ip": donor["status"]["podIP"],
        "image_entrypoint": image_entrypoint,
        "runtime_command": original,
    }
    args.output.write_text(json.dumps(bundle, indent=2) + "\n")


if __name__ == "__main__":
    main()
