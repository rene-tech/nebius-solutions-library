#!/usr/bin/env python3
"""Verify the exact Terraform state address contract before and after destroy."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

BASE_REQUIRED_MANAGED_ADDRESSES = frozenset(
    {
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
)
PUBLIC_EDGE_MANAGED_ADDRESSES = frozenset(
    {
        "nebius_vpc_v1_security_rule.workers_public_edge_ingress[0]",
        "nebius_vpc_v1_allocation.gateway[0]",
    }
)

# Opt-in durable result storage. Its bucket deliberately survives a teardown of
# the disposable cluster, so it is verified as its own selectable address set.
SCIENTIFIC_ARTIFACT_MANAGED_ADDRESSES = frozenset(
    {
        "terraform_data.scientific_artifacts_contract[0]",
        "nebius_storage_v1_bucket.scientific_artifacts[0]",
        "nebius_iam_v1_service_account.scientific_artifacts_writer[0]",
        "nebius_iam_v1_group.scientific_artifacts_writers[0]",
        "nebius_iam_v1_group_membership.scientific_artifacts_writer[0]",
        "nebius_iam_v1_access_permit.scientific_artifacts_writer[0]",
        "nebius_iam_v2_access_key.scientific_artifacts[0]",
    }
)

REQUIRED_MANAGED_ADDRESSES = (
    BASE_REQUIRED_MANAGED_ADDRESSES | PUBLIC_EDGE_MANAGED_ADDRESSES
)

ALLOWED_DATA_ADDRESSES = frozenset(
    {
        "data.nebius_iam_v2_project.target",
        "data.nebius_vpc_v1_network.target",
        "data.nebius_vpc_v1_subnet.target",
    }
)


def selected_managed_addresses(
    edge_mode: str, scientific_artifacts: bool = False
) -> frozenset[str]:
    """Return the exact managed address set the selected options require."""

    return (
        BASE_REQUIRED_MANAGED_ADDRESSES
        | (PUBLIC_EDGE_MANAGED_ADDRESSES if edge_mode == "public" else frozenset())
        | (SCIENTIFIC_ARTIFACT_MANAGED_ADDRESSES if scientific_artifacts else frozenset())
    )


def validate_addresses(
    lines: list[str],
    mode: str,
    edge_mode: str = "public",
    scientific_artifacts: bool = False,
) -> list[str]:
    addresses = [line.strip() for line in lines if line.strip()]
    counts = Counter(addresses)
    actual = set(addresses)
    actual_data = {address for address in actual if address.startswith("data.")}
    actual_managed = actual - actual_data
    selected_managed = selected_managed_addresses(edge_mode, scientific_artifacts)
    expected_managed = selected_managed if mode == "create" else frozenset()
    expected_data = ALLOWED_DATA_ADDRESSES if mode == "create" else frozenset()
    errors: list[str] = []

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
    parser.add_argument("--mode", choices=("create", "destroy"), required=True)
    parser.add_argument(
        "--public-edge-mode",
        choices=("public", "internal-only"),
        default="public",
    )
    parser.add_argument(
        "--scientific-artifacts",
        action="store_true",
        help="expect the opt-in durable scientific result storage addresses",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    errors = validate_addresses(
        args.state_list.read_text(encoding="utf-8").splitlines(),
        args.mode,
        args.public_edge_mode,
        args.scientific_artifacts,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    selected_managed = selected_managed_addresses(
        args.public_edge_mode, args.scientific_artifacts
    )
    managed_count = len(selected_managed) if args.mode == "create" else 0
    data_count = len(ALLOWED_DATA_ADDRESSES) if args.mode == "create" else 0
    print(
        f"PASS: {args.mode} state contains exactly {managed_count} managed and "
        f"{data_count} allowlisted data-source addresses"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
