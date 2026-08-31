from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from verify_plan import REQUIRED_ADDRESSES  # noqa: E402
from verify_state import (  # noqa: E402
    ALLOWED_DATA_ADDRESSES,
    BASE_REQUIRED_MANAGED_ADDRESSES,
    PUBLIC_EDGE_MANAGED_ADDRESSES,
    REQUIRED_MANAGED_ADDRESSES,
    validate_addresses,
)


class TerraformStateContractTests(unittest.TestCase):
    def test_managed_address_contract_matches_plan_verifier(self) -> None:
        self.assertEqual(REQUIRED_MANAGED_ADDRESSES, REQUIRED_ADDRESSES)
        self.assertEqual(len(REQUIRED_MANAGED_ADDRESSES), 16)
        self.assertEqual(len(ALLOWED_DATA_ADDRESSES), 3)

    def test_internal_only_create_accepts_no_public_edge_addresses(self) -> None:
        addresses = sorted(BASE_REQUIRED_MANAGED_ADDRESSES | ALLOWED_DATA_ADDRESSES)
        self.assertEqual(validate_addresses(addresses, "create", "internal-only"), [])
        self.assertTrue(
            PUBLIC_EDGE_MANAGED_ADDRESSES.isdisjoint(BASE_REQUIRED_MANAGED_ADDRESSES)
        )

    def test_create_accepts_exact_managed_and_data_sets(self) -> None:
        addresses = sorted(REQUIRED_MANAGED_ADDRESSES | ALLOWED_DATA_ADDRESSES)
        self.assertEqual(validate_addresses(addresses, "create"), [])

    def test_create_rejects_missing_and_unexpected_addresses(self) -> None:
        addresses = set(REQUIRED_MANAGED_ADDRESSES | ALLOWED_DATA_ADDRESSES)
        addresses.remove(
            'nebius_mk8s_v1_node_group.gpu["nebius-b300-preemptible-8x"]'
        )
        addresses.add("nebius_mk8s_v1_node_group.unreviewed")
        errors = validate_addresses(sorted(addresses), "create")
        self.assertTrue(
            any("managed state address set differs" in error for error in errors)
        )

    def test_destroy_accepts_empty_state(self) -> None:
        self.assertEqual(validate_addresses([], "destroy"), [])

    def test_destroy_rejects_data_or_managed_state(self) -> None:
        data_errors = validate_addresses(sorted(ALLOWED_DATA_ADDRESSES), "destroy")
        self.assertTrue(
            any(
                "data-source state address set differs" in error
                for error in data_errors
            )
        )

        addresses = {"nebius_mk8s_v1_cluster.validation"}
        managed_errors = validate_addresses(sorted(addresses), "destroy")
        self.assertTrue(
            any(
                "managed state address set differs" in error for error in managed_errors
            )
        )

    def test_duplicate_address_is_rejected(self) -> None:
        addresses = sorted(REQUIRED_MANAGED_ADDRESSES | ALLOWED_DATA_ADDRESSES)
        addresses.append(addresses[0])
        errors = validate_addresses(addresses, "create")
        self.assertTrue(any("duplicate state addresses" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
