#!/usr/bin/env python3
"""Refresh model-derived hashes and targets in the scale-contract overlay.

The policy profiles and controller boundary remain review-owned.  This helper
only updates fields that are a deterministic projection of canonical model
records, then rebinds the scale-contract file in ``catalog.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.loader import execution_identity, resource_placement_identity


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _render(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _replace(path: Path, payload: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(payload)
        temporary = Path(stream.name)
    os.replace(temporary, path)


def _target(model_id: str, model: dict[str, Any], item: dict[str, Any]) -> None:
    execution_mode = model["interface"]["execution_mode"]
    runtime_kind = model["runtime"]["kind"]
    activation_mode = {
        "http": "replica-scale",
        "batch": "batch-job",
        "unavailable": "disabled",
    }[execution_mode]
    item["activation_mode"] = activation_mode
    item["policy_profile"] = (
        "http-nim-zero-to-one-v1"
        if activation_mode == "replica-scale" and runtime_kind == "nim"
        else {
            "replica-scale": "http-deployment-zero-to-one-v1",
            "batch-job": "batch-job-v1",
            "disabled": "disabled-v1",
        }[activation_mode]
    )
    item["readiness"] = (
        None if activation_mode == "disabled" else model["interface"]["readiness"]
    )
    item["warmup"] = (
        None if activation_mode == "disabled" else model["interface"]["warmup"]
    )
    if activation_mode == "disabled":
        item["target"] = None
        return

    selector = {"fs2-serve.nebius.ai/model-id": model_id}
    if activation_mode == "batch-job":
        selector["fs2-serve.nebius.ai/job-kind"] = "batch"
        api_version, kind, name, uid_source = (
            "batch/v1",
            "Job",
            None,
            "signed-activation-receipt",
        )
    elif runtime_kind == "nim":
        api_version, kind, name, uid_source = (
            "apps.nvidia.com/v1alpha1",
            "NIMService",
            model_id,
            "signed-serving-binding",
        )
    else:
        api_version, kind, name, uid_source = (
            "apps/v1",
            "Deployment",
            model_id,
            "signed-serving-binding",
        )
    target = {
        "api_version": api_version,
        "kind": kind,
        "name": name,
        "namespace": "fs2-models",
        "selector": dict(sorted(selector.items())),
        "uid_source": uid_source,
    }
    subject = {
        **target,
        "model_digest": item["model_digest"],
        "execution_identity_sha256": item["execution_identity_sha256"],
        "resource_placement_identity_sha256": item[
            "resource_placement_identity_sha256"
        ],
    }
    subject.pop("uid_source")
    target["template_identity_sha256"] = hashlib.sha256(
        canonical_bytes(subject)
    ).hexdigest()
    item["target"] = target


def refresh(catalog_root: Path, *, check: bool) -> bool:
    index_path = catalog_root / "catalog.json"
    index = _load(index_path)
    scale_path = catalog_root / index["scale_contracts"]["path"]
    scale = _load(scale_path)
    models = {
        value["model"]["id"]: value
        for value in (
            _load(path) for path in sorted((catalog_root / "models").glob("*.json"))
        )
    }
    if set(models) != set(scale["contracts"]):
        raise SystemExit("scale contracts and model records differ")

    for model_id, model in sorted(models.items()):
        item = scale["contracts"][model_id]
        item["model_digest"] = hashlib.sha256(canonical_bytes(model)).hexdigest()
        item["execution_identity_sha256"] = execution_identity(model)
        item["resource_placement_identity_sha256"] = resource_placement_identity(model)
        _target(model_id, model, item)

    scale_payload = _render(scale)
    index["scale_contracts"]["sha256"] = hashlib.sha256(scale_payload).hexdigest()
    index_payload = _render(index)
    changed = (
        scale_path.read_bytes() != scale_payload
        or index_path.read_bytes() != index_payload
    )
    if changed and not check:
        _replace(scale_path, scale_payload)
        _replace(index_path, index_payload)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--catalog-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = refresh(args.catalog_root.resolve(), check=args.check)
    if args.check and changed:
        print("scale contracts require refresh")
        return 1
    print("scale contracts are current" if not changed else "scale contracts refreshed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
