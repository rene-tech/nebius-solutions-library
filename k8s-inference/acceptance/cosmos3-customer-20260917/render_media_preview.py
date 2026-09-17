#!/usr/bin/env python3
"""Render only task-owned media preview resources from the tracked Cosmos recipe."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "models/general-media/k8s/cosmos3-nano.yaml"
RUNTIME_DIGEST = "sha256:6d2630c7d637b699557573f2c3fee8df5d4d0cd718977aa22549ed6a6ef30587"
RUNTIME_IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/vllm/vllm-omni@" + RUNTIME_DIGEST
NAME = "cosmos3-nano-media-preview-r20260917"


def render(
    node: str,
    snapshot_bundle: Path | None = None,
    *,
    name: str = NAME,
    pool: str = "h100-1x",
    capacity_type: str = "preemptible",
) -> dict:
    if not name.startswith("cosmos3-nano-media-") or not name.endswith("-r20260917"):
        raise ValueError("preview name must remain in the task-owned namespace")
    documents = list(yaml.safe_load_all(SOURCE.read_text()))
    config = copy.deepcopy(next(item for item in documents if item["kind"] == "ConfigMap"))
    deployment = copy.deepcopy(next(item for item in documents if item["kind"] == "Deployment"))
    labels = {
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/instance": name,
        "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/managed-by": "cosmos-qualification",
        "fs2.nebius.ai/test-only": "true",
    }
    config["metadata"] = {"name": name, "namespace": "fs2-models", "labels": labels}
    config["immutable"] = True
    deployment["metadata"] = {"name": name, "namespace": "fs2-models", "labels": labels}
    deployment["spec"]["selector"] = {"matchLabels": {"app.kubernetes.io/instance": name}}
    template = deployment["spec"]["template"]
    template["metadata"]["labels"] = labels
    template["metadata"]["annotations"]["fs2.nebius.ai/qualification-scope"] = "normal-load-media-preview"
    template["metadata"]["annotations"]["fs2.nebius.ai/adapter-sha256"] = hashlib.sha256(
        config["data"]["adapter.py"].encode()
    ).hexdigest()
    spec = template["spec"]
    spec["nodeSelector"] = {
        "kubernetes.io/hostname": node,
        "accelerator.fs2.nebius/pool-id": pool,
        "capacity.fs2.nebius/type": capacity_type,
    }
    # Keep verification, but replace the cache-writing localizer with its
    # read-only inventory/hash helpers. No download, lock, or cache mutation.
    verifier = spec["initContainers"][0]
    verifier["name"] = "verify-existing-model"
    verifier["command"] = [
        "python3",
        "-c",
        "import sys; from pathlib import Path; sys.path.insert(0, '/contract'); "
        "from localize import load_model, safe_sha256; model=load_model(); "
        "root=Path('/model-cache/cosmos3-nano/sha256') / model['content_digest'] / 'payload'; "
        "assert all((root / f['path']).stat().st_size == f['size'] and "
        "safe_sha256(root / f['path']) == f['sha256'] for f in model['files']); "
        "print('verified existing immutable Cosmos model inventory', len(model['files']))",
    ]
    for mount in verifier["volumeMounts"]:
        if mount["name"] == "model-cache":
            mount["readOnly"] = True
    for container in [*spec["initContainers"], *spec["containers"]]:
        assert container["image"].endswith("@" + RUNTIME_DIGEST)
        container["image"] = RUNTIME_IMAGE
    for volume in spec["volumes"]:
        if volume["name"] == "adapter":
            volume["configMap"]["name"] = name
        elif volume["name"] == "model-cache":
            volume["persistentVolumeClaim"] = {
                "claimName": "cosmos3-nano-cache-rwx-8d453c7f",
                "readOnly": True,
            }
    if snapshot_bundle is not None:
        sys.path.insert(0, str(ROOT / "components/control-plane/src"))
        from fs2_serve.serving_snapshot import ServingSnapshotBundle, configure_serving_snapshot

        bundle = ServingSnapshotBundle.model_validate_json(snapshot_bundle.read_bytes())
        assert bundle.model_ref == "cosmos3-nano" and bundle.runtime_image == RUNTIME_IMAGE
        configure_serving_snapshot(spec, config=bundle, runtime_container_name="vllm-omni", fallback="fail")
        template["metadata"]["annotations"]["fs2.nebius.ai/qualification-scope"] = "snapshot-media-preview"
        template["metadata"]["annotations"]["fs2-serve.nebius.ai/snapshot-bundle"] = bundle.bundle_id
    return {"apiVersion": "v1", "kind": "List", "items": [config, deployment]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--snapshot-bundle", type=Path)
    parser.add_argument("--name", default=NAME)
    parser.add_argument("--pool", default="h100-1x", choices=("h100-1x", "h100-reserved-8x"))
    parser.add_argument("--capacity-type", default="preemptible", choices=("preemptible", "regular"))
    args = parser.parse_args()
    print(
        json.dumps(
            render(args.node, args.snapshot_bundle, name=args.name, pool=args.pool, capacity_type=args.capacity_type)
        )
    )


if __name__ == "__main__":
    main()
