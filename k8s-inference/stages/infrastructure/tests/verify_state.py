#!/usr/bin/env python3
"""Verify the exact Terraform state address contract before and after destroy."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

BASE_REQUIRED_MANAGED_RESOURCE_TYPES = {
    address: address.split(".", maxsplit=1)[0]
    for address in {
        "terraform_data.target_contract",
        "nebius_iam_v1_service_account.nodepull",
        "nebius_iam_v1_group.target_registry_readers",
        "nebius_iam_v1_group_membership.nodepull_target_registry",
        "nebius_iam_v1_access_permit.nodepull_registry",
        "nebius_registry_v1_registry.images",
        "nebius_compute_v1_filesystem.cache",
        "nebius_vpc_v1_security_group.workers",
        "nebius_vpc_v1_security_rule.workers_private_ingress",
        "nebius_vpc_v1_security_rule.workers_egress",
        "nebius_mk8s_v1_cluster.validation",
        "nebius_mk8s_v1_node_group.system",
        'nebius_mk8s_v1_node_group.gpu["nebius-b300-preemptible-1x"]',
        'nebius_mk8s_v1_node_group.gpu["nebius-b300-preemptible-8x"]',
    }
}
BASE_REQUIRED_MANAGED_ADDRESSES = frozenset(BASE_REQUIRED_MANAGED_RESOURCE_TYPES)
PUBLIC_EDGE_MANAGED_RESOURCE_TYPES = {
    address: address.split(".", maxsplit=1)[0]
    for address in {
        "nebius_vpc_v1_security_rule.workers_public_edge_ingress[0]",
        "nebius_vpc_v1_allocation.gateway[0]",
    }
}
PUBLIC_EDGE_MANAGED_ADDRESSES = frozenset(PUBLIC_EDGE_MANAGED_RESOURCE_TYPES)
REQUIRED_MANAGED_ADDRESSES = (
    BASE_REQUIRED_MANAGED_ADDRESSES | PUBLIC_EDGE_MANAGED_ADDRESSES
)
REFERENCE_DATA_COMMON_MANAGED_RESOURCE_TYPES = {
    address: address.split(".", maxsplit=1)[0]
    for address in {
        "nebius_iam_v1_service_account.reference_data[0]",
        "nebius_iam_v1_group.reference_data_writers[0]",
        "nebius_iam_v1_group_membership.reference_data_writer[0]",
        "nebius_iam_v2_access_key.reference_data[0]",
        "nebius_mk8s_v1_node_group.reference_data[0]",
    }
}
REFERENCE_DATA_MANAGED_RESOURCE_TYPES = {
    "disabled": {},
    "retain": {
        **REFERENCE_DATA_COMMON_MANAGED_RESOURCE_TYPES,
        "nebius_compute_v1_filesystem.reference_data[0]": "nebius_compute_v1_filesystem",
        "nebius_storage_v1_bucket.reference_data[0]": "nebius_storage_v1_bucket",
    },
    "disposable": {
        **REFERENCE_DATA_COMMON_MANAGED_RESOURCE_TYPES,
        "nebius_compute_v1_filesystem.reference_data_disposable[0]": "nebius_compute_v1_filesystem",
        "nebius_storage_v1_bucket.reference_data_disposable[0]": "nebius_storage_v1_bucket",
    },
}
REFERENCE_DATA_MANAGED_RESOURCE_COUNTS = {
    mode: len(resource_types)
    for mode, resource_types in REFERENCE_DATA_MANAGED_RESOURCE_TYPES.items()
}
REFERENCE_DATA_MANAGED_ADDRESSES = {
    mode: frozenset(resources)
    for mode, resources in REFERENCE_DATA_MANAGED_RESOURCE_TYPES.items()
}

ALLOWED_DATA_RESOURCE_TYPES = {
    "data.nebius_iam_v2_project.target": "nebius_iam_v2_project",
    "data.nebius_vpc_v1_network.target": "nebius_vpc_v1_network",
    "data.nebius_vpc_v1_subnet.target": "nebius_vpc_v1_subnet",
}
ALLOWED_DATA_ADDRESSES = frozenset(ALLOWED_DATA_RESOURCE_TYPES)


def expected_managed_resource_types(
    edge_mode: str, reference_data_mode: str
) -> dict[str, str]:
    return {
        **BASE_REQUIRED_MANAGED_RESOURCE_TYPES,
        **(
            PUBLIC_EDGE_MANAGED_RESOURCE_TYPES
            if edge_mode == "public"
            else {}
        ),
        **REFERENCE_DATA_MANAGED_RESOURCE_TYPES[reference_data_mode],
    }


def infer_resource_type(address: str) -> str:
    parts = address.split(".")
    return parts[1] if address.startswith("data.") and len(parts) > 1 else parts[0]


def state_resources(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten root and child-module resource records from Terraform state JSON."""
    resources: list[dict[str, Any]] = []

    def visit(module: dict[str, Any]) -> None:
        resources.extend(module.get("resources", []))
        for child in module.get("child_modules", []):
            visit(child)

    root_module = document.get("values", {}).get("root_module")
    if isinstance(root_module, dict):
        visit(root_module)
    return resources


def load_state_resources(path: Path) -> list[dict[str, Any]]:
    """Load provider state JSON, retaining state-list compatibility for operators."""
    raw = path.read_text(encoding="utf-8")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        document = None
    if isinstance(document, dict):
        return state_resources(document)
    return [
        {
            "address": address,
            "mode": "data" if address.startswith("data.") else "managed",
            "type": infer_resource_type(address),
        }
        for address in (line.strip() for line in raw.splitlines())
        if address
    ]


def validate_resources(
    resources: list[dict[str, Any]],
    mode: str,
    edge_mode: str = "public",
    reference_data_mode: str = "disabled",
) -> list[str]:
    active_state = mode in {"create", "retained"}
    expected_managed = (
        expected_managed_resource_types(edge_mode, reference_data_mode)
        if active_state
        else {}
    )
    expected_data = ALLOWED_DATA_RESOURCE_TYPES if active_state else {}
    expected = {**expected_managed, **expected_data}
    addresses = [resource.get("address", "<unknown>") for resource in resources]
    counts = Counter(addresses)
    actual = set(addresses)
    errors: list[str] = []

    if mode == "destroy" and reference_data_mode == "retain":
        errors.append(
            "retained reference data cannot satisfy an empty full-destroy state contract"
        )
    if len(resources) != len(expected):
        errors.append(
            "state resource count differs from contract: "
            f"actual={len(resources)} expected={len(expected)}"
        )
    duplicates = sorted(address for address, count in counts.items() if count > 1)
    if duplicates:
        errors.append(f"duplicate state addresses: {duplicates}")
    if actual != set(expected):
        errors.append(
            "state address set differs from contract: "
            f"missing={sorted(set(expected) - actual)} "
            f"extra={sorted(actual - set(expected))}"
        )

    for resource in resources:
        address = resource.get("address", "<unknown>")
        resource_mode = resource.get(
            "mode", "data" if address.startswith("data.") else "managed"
        )
        resource_type = resource.get("type")
        expected_mode = "data" if address in expected_data else "managed"
        if address in expected and resource_mode != expected_mode:
            errors.append(
                f"{address}: resource mode {resource_mode!r}, expected {expected_mode!r}"
            )
        if expected.get(address) != resource_type:
            errors.append(
                f"{address}: resource type {resource_type!r}, expected "
                f"{expected.get(address)!r}"
            )
    return errors


def validate_addresses(
    lines: list[str],
    mode: str,
    edge_mode: str = "public",
    reference_data_mode: str = "disabled",
) -> list[str]:
    resources = [
        {
            "address": address,
            "mode": "data" if address.startswith("data.") else "managed",
            "type": infer_resource_type(address),
        }
        for address in (line.strip() for line in lines)
        if address
    ]
    return validate_resources(resources, mode, edge_mode, reference_data_mode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_list", type=Path)
    parser.add_argument("--mode", choices=("create", "retained", "destroy"), required=True)
    parser.add_argument(
        "--reference-data-mode",
        choices=tuple(REFERENCE_DATA_MANAGED_ADDRESSES),
        default="disabled",
    )
    parser.add_argument(
        "--public-edge-mode",
        choices=("public", "internal-only"),
        default="public",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    resources = load_state_resources(args.state_list)
    errors = validate_resources(
        resources,
        args.mode,
        args.public_edge_mode,
        args.reference_data_mode,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    selected_managed = expected_managed_resource_types(
        args.public_edge_mode, args.reference_data_mode
    )
    managed_count = len(selected_managed) if args.mode in {"create", "retained"} else 0
    data_count = len(ALLOWED_DATA_ADDRESSES) if args.mode in {"create", "retained"} else 0
    print(
        f"PASS: {args.mode} state contains exactly {managed_count} managed and "
        f"{data_count} allowlisted data-source resources, including "
        f"{REFERENCE_DATA_MANAGED_RESOURCE_COUNTS[args.reference_data_mode]} "
        f"reference-data resources ({args.reference_data_mode})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
