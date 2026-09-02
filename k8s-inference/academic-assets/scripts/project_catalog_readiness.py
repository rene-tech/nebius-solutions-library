#!/usr/bin/env python3
"""Project academic asset readiness into the catalog runtime contract.

The catalog copy is generated, never hand-written, so it cannot drift from the
private readiness state machine.  It carries only non-secret identities: model
and backend IDs, the tenant-private mount that a runtime must attach, artifact
digests, the pinned runtime image, and both readiness axes.  No licensed bytes,
credentials, receipt bodies or owner-only paths are ever emitted.

Usage:
  project_catalog_readiness.py --contract C --state-dir D --output O [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import academic_assets as aa  # noqa: E402

CATALOG_SCHEMA = "fs2-serve.nebius.ai/academic-asset-readiness/v1"


def build_projection(contract: dict[str, Any], readiness: dict[str, Any]) -> dict[str, Any]:
    runtime_cache = contract["runtime_cache"]
    assets = {item["asset_id"]: item for item in readiness["assets"]}
    alternatives = {
        native: fallback_id
        for fallback_id, fallback in contract["fallbacks"].items()
        for native in fallback["does_not_satisfy"]
    }

    models = []
    for asset_id in sorted(contract["assets"]):
        spec = contract["assets"][asset_id]
        state = assets[asset_id]
        delivery = spec["delivery"]
        offline = spec["runtime"]["offline_validation"]
        image = spec["runtime"].get("runtime_image")
        alternative_id = alternatives.get(spec["model_id"])
        models.append(
            {
                "asset_id": asset_id,
                "model_id": spec["model_id"],
                "backend_id": spec["backend_id"],
                "display_name": spec["display_name"],
                "access_profile": "academic",
                "state": state["state"],
                "serving_admission": state["serving_admission"],
                "artifact_status": state["artifact_status"],
                "tenant_cache_status": state["tenant_cache_status"],
                "runtime_status": state["runtime_status"],
                "deployment_status": state["deployment_status"],
                "semantic_status": state["semantic_status"],
                "use_authorization_status": state["use_authorization_status"],
                "execution_authorization_status": state["execution_authorization_status"],
                "formal_license_status": state["formal_license_status"],
                "license_id": spec["license"]["license_id"],
                "artifact_sha256": state["artifact_sha256"],
                "runtime_image_digest": state["runtime_image_digest"],
                "runtime_environment_digest": state["runtime_environment_digest"],
                "delivery": {
                    "mode": delivery["mode"],
                    "mount_path": delivery["mount_path"],
                    "embed_in_image": delivery["embed_in_image"],
                    "asset_gid": delivery["asset_gid"],
                    "consumer_access": delivery["consumer_access"],
                    "install_relative_path": delivery["install_relative_path"],
                },
                "runtime_invocation": {
                    "offline_validation_kind": offline["kind"],
                    "model_dir": offline.get("model_dir"),
                    "model_dir_flag": offline.get("model_dir_flag"),
                    "source_revision": spec["runtime"]["code_revision"],
                    "image_repository": None if image is None else image["repository"],
                    "image_tag": None if image is None else image["tag"],
                },
                "alternative": (
                    None
                    if alternative_id is None
                    else {
                        "model_id": alternative_id,
                        "relationship": "independent-operational-alternative",
                        "reason": (
                            f"{alternative_id} is a separate model with its own identity and results; "
                            f"it does not satisfy {spec['model_id']}."
                        ),
                    }
                ),
            }
        )

    return {
        "schema": CATALOG_SCHEMA,
        "generated_by": "academic-assets/scripts/project_catalog_readiness.py",
        "generation": readiness["generation"],
        "runtime_path_state": readiness["runtime_path_state"],
        "formal_license_state": readiness["formal_license_state"],
        "request_time_license_receipt_required": contract["activation_policy"][
            "request_time_license_receipt_required"
        ],
        "delivery": {
            "mode": "tenant-private-volume",
            "namespace": runtime_cache["pvc_namespace"],
            "claim": runtime_cache["pvc_name"],
            "mount_root": "/opt/fs2/academic",
            "general_shared_cache": runtime_cache["general_shared_cache"],
            "world_readable": contract["activation_policy"]["world_readable_licensed_bytes"],
        },
        "models": models,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="fail if the committed projection is stale")
    args = parser.parse_args(argv)

    contract = aa.load_contract(args.contract)
    root = aa.read_state_root(args.state_dir)
    generation = aa.active_generation(root) if root is not None else None
    readiness = aa.readiness_projection(contract, root, generation)
    projection = build_projection(contract, readiness)
    rendered = json.dumps(projection, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not args.output.exists():
            print(f"{args.output} is missing", file=sys.stderr)
            return 1
        if args.output.read_text() != rendered:
            print(f"{args.output} is stale; regenerate it", file=sys.stderr)
            return 1
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
