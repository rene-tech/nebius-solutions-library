#!/usr/bin/env python3
"""Build the exact capability manifest for one Cosmos customer release."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "customer-readiness" / "capability_gate.py"
SPEC = importlib.util.spec_from_file_location("customer_capability_gate", GATE_PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _scenario(
    identifier: str,
    operation: str,
    input_form: str,
    output_form: str,
    *,
    workload_states: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "operation": operation,
        "input_form": input_form,
        "output_form": output_form,
        "client_path": "librechat+mcp",
        "workload_states": workload_states or ["hot", "scale-from-zero"],
        "integrations": ["robotics-tenant"],
        "required_observability": ["containers", "logs", "metrics", "runs", "usage"],
        "min_clean_cohorts": 2,
    }


def manifest(release_identity: object) -> dict[str, Any]:
    """List every customer-visible Cosmos App workflow; no sibling mode inherits evidence."""

    identity = gate.validate_identity(release_identity)
    capabilities = [
        ("text-to-image", "text-to-image", "json-text", "image", True),
        ("text-to-video", "text-to-video", "json-text", "mp4", True),
        ("image-to-video", "image-to-video", "image-reference", "mp4", True),
        ("transfer-video", "transfer", "video-and-control-reference", "mp4", True),
        # These modes remain inventoried, but are not advertised after the
        # H100 SIGBUS/cudaErrorNotSupported runtime findings.
        ("forward-dynamics", "forward-dynamics", "video-and-actions", "mp4", False),
        ("inverse-dynamics", "inverse-dynamics", "video", "action-trajectory", False),
    ]
    rows = [
        {
            "id": capability,
            "description": f"Cosmos3-Nano {operation} through the customer-visible typed MCP tool.",
            "advertised": advertised,
            "scenarios": [
                _scenario(f"{capability}-public", operation, input_form, output_form)
            ],
        }
        for capability, operation, input_form, output_form, advertised in capabilities
    ]
    rows.insert(
        3,
        {
            "id": "video-to-video",
            "description": "Video-conditioned MP4 generation through URL and client-local upload transports.",
            "advertised": True,
            "scenarios": [
                _scenario("v2v-url", "video-to-video", "https-mp4-url", "mp4"),
                _scenario(
                    "v2v-upload", "video-to-video", "client-local-mp4-upload", "mp4"
                ),
            ],
        },
    )
    return cast(
        dict[str, Any],
        gate.validate_manifest(
            {
                "schema": gate.MANIFEST_SCHEMA,
                "app_id": "cosmos3-nano",
                "release_identity": identity,
                "max_evidence_age_seconds": 86400,
                "capabilities": rows,
                "requested_capability_ids": ["video-to-video"],
                "reduced_scope_decisions": [],
            }
        ),
    )


def lerobot_manifest(release_identity: object) -> dict[str, Any]:
    """Keep the independently deployed LeRobot App's claim independent."""

    return cast(
        dict[str, Any],
        gate.validate_manifest(
            {
                "schema": gate.MANIFEST_SCHEMA,
                "app_id": "cosmos3-lerobot-augmentation",
                "release_identity": gate.validate_identity(release_identity),
                "max_evidence_age_seconds": 86400,
                "capabilities": [
                    {
                        "id": "lerobot-augmentation",
                        "description": (
                            "LeRobot v3 lighting augmentation into a reloadable aligned LeRobot v3 dataset; "
                            "source actions are preserved, not regenerated."
                        ),
                        "advertised": True,
                        "scenarios": [
                            _scenario(
                                "lerobot-lighting",
                                "augment-lerobot-dataset",
                                "lerobot-v3-upload",
                                "lerobot-v3",
                                workload_states=["scale-from-zero"],
                            )
                        ],
                    }
                ],
                "requested_capability_ids": ["lerobot-augmentation"],
                "reduced_scope_decisions": [],
            }
        ),
    )


def manifests(release_identity: object) -> dict[str, dict[str, Any]]:
    values = [manifest(release_identity), lerobot_manifest(release_identity)]
    return {value["app_id"]: value for value in values}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-identity", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--app",
        choices=("cosmos3-nano", "cosmos3-lerobot-augmentation"),
        default="cosmos3-nano",
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output must not already exist")
    value = manifests(json.loads(args.release_identity.read_text(encoding="utf-8")))[
        args.app
    ]
    args.output.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
