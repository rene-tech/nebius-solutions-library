#!/usr/bin/env python3
"""Verify the exact Terraform state address contract before and after destroy."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

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
REFERENCE_DATA_MANAGED_ADDRESSES = {
    mode: frozenset(resources)
    for mode, resources in REFERENCE_DATA_MANAGED_RESOURCE_TYPES.items()
}

ALLOWED_DATA_ADDRESSES = frozenset(
    {
        "data.nebius_iam_v2_project.target",
        "data.nebius_vpc_v1_network.target",
        "data.nebius_vpc_v1_subnet.target",
    }
)


def validate_addresses(
    lines: list[str],
    mode: str,
    edge_mode: str = "public",
    reference_data_mode: str = "disabled",
) -> list[str]:
    addresses = [line.strip() for line in lines if line.strip()]
    counts = Counter(addresses)
    actual = set(addresses)
    actual_data = {address for address in actual if address.startswith("data.")}
    actual_managed = actual - actual_data
    selected_managed = BASE_REQUIRED_MANAGED_ADDRESSES | (
        PUBLIC_EDGE_MANAGED_ADDRESSES if edge_mode == "public" else frozenset()
    ) | REFERENCE_DATA_MANAGED_ADDRESSES[reference_data_mode]
    expected_managed = selected_managed if mode in {"create", "retained"} else frozenset()
    expected_data = ALLOWED_DATA_ADDRESSES if mode in {"create", "retained"} else frozenset()
    errors: list[str] = []

    if mode == "destroy" and reference_data_mode == "retain":
        errors.append(
            "retained reference data cannot satisfy an empty full-destroy state contract"
        )

    duplicates = sorted(address for address, count in counts.items() if count > 1)
    if duplicates:
        errors.append(f"duplicate state addresses: {duplicates}")
    if actual_managed != expected_managed:
        errors.append(
            "managed state address set differs from contract: "
            f"missing={sorted(expected_managed - actual_managed)} "
            f"extra={sorted(actual_managed - expected_managed)}"
        )
    if actual_data != expected_data:
        errors.append(
            "data-source state address set differs from contract: "
            f"missing={sorted(expected_data - actual_data)} "
            f"extra={sorted(actual_data - expected_data)}"
        )
    return errors


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
    errors = validate_addresses(
        args.state_list.read_text(encoding="utf-8").splitlines(),
        args.mode,
        args.public_edge_mode,
        args.reference_data_mode,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    selected_managed = BASE_REQUIRED_MANAGED_ADDRESSES | (
        PUBLIC_EDGE_MANAGED_ADDRESSES
        if args.public_edge_mode == "public"
        else frozenset()
    ) | REFERENCE_DATA_MANAGED_ADDRESSES[args.reference_data_mode]
    managed_count = len(selected_managed) if args.mode in {"create", "retained"} else 0
    data_count = len(ALLOWED_DATA_ADDRESSES) if args.mode in {"create", "retained"} else 0
    print(
        f"PASS: {args.mode} state contains exactly {managed_count} managed and "
        f"{data_count} allowlisted data-source addresses"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
