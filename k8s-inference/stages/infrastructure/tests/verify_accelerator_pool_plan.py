#!/usr/bin/env python3
"""Verify that a disposable plan realizes the exact effective pool contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


FS2_ROOT = Path(__file__).resolve().parents[3]
PROFILES_ROOT = FS2_ROOT / "catalog/profiles"
POOL_TEMPLATES_PATH = PROFILES_ROOT / "accelerator-pools.json"
POOL_PROFILES_PATH = PROFILES_ROOT / "accelerator-pool-profiles.json"
TARGETS_PATH = Path(
    os.environ.get(
        "K8S_INFERENCE_TARGET_CONTRACT_PATH",
        PROFILES_ROOT / "approved-targets.json",
    )
)
SOURCE_CLOSURE_PATH = PROFILES_ROOT / "source-registry-closure.json"

POOL_TEMPLATES = json.loads(POOL_TEMPLATES_PATH.read_text(encoding="utf-8"))
POOL_PROFILES = json.loads(POOL_PROFILES_PATH.read_text(encoding="utf-8"))
TARGETS = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
GPU_ADDRESS_RE = re.compile(r'^nebius_mk8s_v1_node_group\.gpu\["([^"]+)"\]$')


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def variable(document: dict[str, Any], name: str) -> Any:
    return document.get("variables", {}).get(name, {}).get("value")


def nested(value: dict[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def validate_overrides(overrides: Any, selected_profile: dict[str, Any]) -> list[str]:
    if not isinstance(overrides, dict):
        return ["accelerator_pool_capacity_overrides must be a map"]

    errors: list[str] = []
    selected_ids = set(selected_profile["pools"])
    for pool_id, bounds in overrides.items():
        if pool_id not in selected_ids:
            errors.append(f"capacity override uses unknown pool ID {pool_id!r}")
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,126}[a-z0-9]", pool_id):
            errors.append(f"capacity override pool ID {pool_id!r} is not stable")
        if not isinstance(bounds, dict) or set(bounds) != {"min_nodes", "max_nodes"}:
            errors.append(
                f"capacity override {pool_id!r} must contain exactly min_nodes/max_nodes"
            )
            continue
        minimum = bounds["min_nodes"]
        maximum = bounds["max_nodes"]
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
        ):
            errors.append(f"capacity override {pool_id!r} bounds must be integers")
            continue
        profile_maximum = selected_profile["pools"][pool_id]["max_nodes"]
        if not 0 <= minimum <= maximum <= profile_maximum:
            errors.append(
                f"capacity override {pool_id!r} must satisfy "
                f"0 <= min <= max <= {profile_maximum}"
            )
    return errors


def expected_contract(
    *,
    project_id: str,
    source_commit: str,
    capacity_profile: str,
    floor_profile: str,
    overrides: dict[str, dict[str, int]],
    accelerator_profile: str | None = None,
) -> dict[str, Any]:
    effective_accelerator_profile = accelerator_profile or capacity_profile
    selected_profile = POOL_PROFILES["profiles"][effective_accelerator_profile]
    target = TARGETS["targets"][project_id]
    source_registry = TARGETS["source_registry"]
    source_closure = json.loads(SOURCE_CLOSURE_PATH.read_text(encoding="utf-8"))
    pools: dict[str, Any] = {}
    for pool_id, capacity in selected_profile["pools"].items():
        template = POOL_TEMPLATES["pool_templates"][pool_id]
        profile_minimum = capacity["floor_nodes"].get(floor_profile, -1)
        requested = overrides.get(pool_id)
        minimum = requested["min_nodes"] if requested else profile_minimum
        maximum = requested["max_nodes"] if requested else capacity["max_nodes"]
        accelerator = POOL_TEMPLATES["accelerator_classes"][
            template["accelerator_class"]
        ]
        pools[pool_id] = {
            "id": template["id"],
            "accelerator_class": template["accelerator_class"],
            "resource_api": accelerator["resource_api"],
            "provider": template["provider"],
            "node": template["node"],
            "capacity": {
                "type": template["capacity"]["default_mode"],
                "min_nodes": minimum,
                "max_nodes": maximum,
                "source": "operator-override" if requested else "profile",
                "profile_bounds": {
                    "min_nodes": profile_minimum,
                    "max_nodes": capacity["max_nodes"],
                },
                "scale_from_zero": template["capacity"]["scale_from_zero"],
            },
            "scheduling": template["scheduling"],
            "features": template["features"],
            "region_availability": template["region_availability"],
            "state": template["state"],
            "evidence": template["evidence"],
        }
    return {
        "schema": "fs2-serve.nebius.ai/terraform-accelerator-pools/v2",
        "source_commit": source_commit,
        "profile": effective_accelerator_profile,
        "floor_profile": floor_profile,
        "target_region": target["region"],
        "capacity_ownership": {
            "owner_root": "infra-disposable",
            "override_mode": "capacity-only-patch",
            "override_fields": ["max_nodes", "min_nodes"],
            "requested_overrides": overrides,
            "requested_overrides_sha256": canonical_sha256(overrides),
        },
        "artifact_source": {
            "registry": source_registry,
            "closure_schema": source_closure["schema"],
            "closure_sha256": hashlib.sha256(
                SOURCE_CLOSURE_PATH.read_bytes()
            ).hexdigest(),
            "cross_region_pull_required": target["region"] != source_registry["region"],
        },
        "pools": pools,
    }


def validate_plan(
    document: dict[str, Any],
    *,
    mode: str,
    expected_project_id: str,
    expected_source_commit: str,
) -> list[str]:
    errors: list[str] = []
    capacity_profile = variable(document, "capacity_profile")
    accelerator_profile_input = variable(document, "accelerator_pool_profile")
    accelerator_profile = (
        capacity_profile
        if accelerator_profile_input is None
        else accelerator_profile_input
    )
    floor_profile = variable(document, "gpu_floor_profile")
    project_id = variable(document, "project_id")
    source_commit = variable(document, "source_commit")
    overrides = variable(document, "accelerator_pool_capacity_overrides")

    if project_id != expected_project_id or project_id not in TARGETS["targets"]:
        errors.append("project_id differs from the exact approved target")
    if source_commit != expected_source_commit or not isinstance(source_commit, str):
        errors.append("source_commit differs from the exact reviewed commit")
    elif not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        errors.append("source_commit is not a full lowercase Git SHA")
    if accelerator_profile not in POOL_PROFILES["profiles"]:
        errors.append("effective accelerator_pool_profile is not in the catalog")
        return errors

    selected_profile = POOL_PROFILES["profiles"][accelerator_profile]
    if (
        not selected_profile["enabled"]
        or selected_profile["state"] != "hardware-validated"
    ):
        errors.append("selected accelerator pool profile is not hardware-validated")
    if floor_profile not in {"zero", "representative", "full_catalog"}:
        errors.append("gpu_floor_profile is unknown")
    elif any(
        floor_profile not in capacity["floor_nodes"]
        for capacity in selected_profile["pools"].values()
    ):
        errors.append("gpu_floor_profile is absent from a selected pool")
    errors.extend(validate_overrides(overrides, selected_profile))
    if errors:
        return errors

    contract = expected_contract(
        project_id=project_id,
        source_commit=source_commit,
        capacity_profile=capacity_profile,
        floor_profile=floor_profile,
        overrides=overrides,
        accelerator_profile=accelerator_profile,
    )
    requires_v2_only = (
        bool(overrides)
        or accelerator_profile != capacity_profile
        or any(
            variable(document, name) is not None
            for name in ("target_binding", "system_pool", "shared_cache")
        )
    )
    expected_action = {
        "create": ["create"],
        "noop": ["no-op"],
        "destroy": ["delete"],
    }[mode]
    side = "before" if mode == "destroy" else "after"
    changes = {
        change["address"]: change for change in document.get("resource_changes", [])
    }
    expected_gpu_addresses = {
        f'nebius_mk8s_v1_node_group.gpu["{pool_id}"]' for pool_id in contract["pools"]
    }
    actual_gpu_addresses = {
        address for address in changes if GPU_ADDRESS_RE.fullmatch(address)
    }
    if actual_gpu_addresses != expected_gpu_addresses:
        errors.append(
            "accelerator node-group address set differs: "
            f"missing={sorted(expected_gpu_addresses - actual_gpu_addresses)} "
            f"extra={sorted(actual_gpu_addresses - expected_gpu_addresses)}"
        )

    for pool_id, pool in contract["pools"].items():
        address = f'nebius_mk8s_v1_node_group.gpu["{pool_id}"]'
        change = changes.get(address)
        if change is None:
            continue
        if change.get("change", {}).get("actions") != expected_action:
            errors.append(f"{address} action differs from {expected_action}")
        values = change.get("change", {}).get(side) or {}
        expected_values = {
            "autoscaling.min_node_count": pool["capacity"]["min_nodes"],
            "autoscaling.max_node_count": pool["capacity"]["max_nodes"],
            "template.resources.platform": pool["provider"]["platform"],
            "template.resources.preset": pool["provider"]["preset"],
            "template.os": pool["provider"]["os"],
            "template.gpu_settings.drivers_preset": pool["provider"]["driver"][
                "preset"
            ],
        }
        for dotted_path, expected in expected_values.items():
            actual = nested(values, *dotted_path.split("."))
            if actual != expected:
                errors.append(f"{address} field {dotted_path} differs from profile")
        labels = nested(values, "template", "metadata", "labels") or {}
        for key, expected in pool["scheduling"]["stable_node_labels"].items():
            if labels.get(key) != expected:
                errors.append(f"{address} stable label {key!r} differs from profile")

    legacy_addresses = {
        "nebius_mk8s_v1_node_group.gpu_b300_1x",
        "nebius_mk8s_v1_node_group.gpu_b300_8x",
    }
    if legacy_addresses.intersection(changes):
        errors.append("plan still contains legacy singleton accelerator addresses")

    if mode != "destroy":
        outputs = document.get("planned_values", {}).get("outputs", {})
        if nested(outputs, "accelerator_pool_contract", "value") != contract:
            errors.append("planned accelerator_pool_contract output differs")
        if nested(
            outputs, "accelerator_pool_contract_sha256", "value"
        ) != canonical_sha256(contract):
            errors.append("planned accelerator_pool_contract_sha256 output differs")
        if (
            requires_v2_only
            and nested(outputs, "infrastructure_contract", "value") is not None
        ):
            errors.append("legacy infrastructure_contract must be null for a v2-only input")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan_json", type=Path)
    parser.add_argument("--mode", choices=("create", "noop", "destroy"), required=True)
    parser.add_argument("--expected-project-id", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    document = json.loads(args.plan_json.read_text(encoding="utf-8"))
    errors = validate_plan(
        document,
        mode=args.mode,
        expected_project_id=args.expected_project_id,
        expected_source_commit=args.expected_source_commit,
    )
    if errors:
        print("accelerator pool plan rejected:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        "accelerator pool plan accepted with exact capacity and immutable profile facts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
