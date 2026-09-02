from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from verify_plan import (  # noqa: E402
    REFERENCE_DATA_RESOURCE_TYPES,
    REQUIRED_ADDRESSES,
)
from verify_state import (  # noqa: E402
    ALLOWED_DATA_ADDRESSES,
    ALLOWED_DATA_RESOURCE_TYPES,
    BASE_REQUIRED_MANAGED_ADDRESSES,
    PUBLIC_EDGE_MANAGED_ADDRESSES,
    REFERENCE_DATA_MANAGED_ADDRESSES,
    REFERENCE_DATA_MANAGED_RESOURCE_COUNTS,
    REFERENCE_DATA_MANAGED_RESOURCE_TYPES,
    REQUIRED_MANAGED_ADDRESSES,
    load_state_resources,
    validate_addresses,
    validate_resources,
)

FIXTURES = TESTS / "fixtures"


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
            any("state address set differs" in error for error in errors)
        )

    def test_destroy_accepts_empty_state(self) -> None:
        self.assertEqual(validate_addresses([], "destroy"), [])

    def test_destroy_rejects_data_or_managed_state(self) -> None:
        data_errors = validate_addresses(sorted(ALLOWED_DATA_ADDRESSES), "destroy")
        self.assertTrue(
            any("state address set differs" in error for error in data_errors)
        )

        addresses = {"nebius_mk8s_v1_cluster.validation"}
        managed_errors = validate_addresses(sorted(addresses), "destroy")
        self.assertTrue(
            any("state address set differs" in error for error in managed_errors)
        )

    def test_duplicate_address_is_rejected(self) -> None:
        addresses = sorted(REQUIRED_MANAGED_ADDRESSES | ALLOWED_DATA_ADDRESSES)
        addresses.append(addresses[0])
        errors = validate_addresses(addresses, "create")
        self.assertTrue(any("duplicate state addresses" in error for error in errors))

    def test_enabled_reference_modes_have_exact_optional_count(self) -> None:
        for reference_data_mode in ("retain", "disposable"):
            self.assertEqual(
                REFERENCE_DATA_MANAGED_RESOURCE_TYPES[reference_data_mode],
                REFERENCE_DATA_RESOURCE_TYPES[reference_data_mode],
            )
            addresses = sorted(
                REQUIRED_MANAGED_ADDRESSES
                | REFERENCE_DATA_MANAGED_ADDRESSES[reference_data_mode]
                | ALLOWED_DATA_ADDRESSES
            )
            self.assertEqual(
                validate_addresses(
                    addresses,
                    "create",
                    reference_data_mode=reference_data_mode,
                ),
                [],
            )
            self.assertEqual(len(addresses), 26)
            self.assertEqual(
                REFERENCE_DATA_MANAGED_RESOURCE_COUNTS[reference_data_mode], 7
            )

    def test_enabled_reference_provider_state_fixtures_match_exact_types(
        self,
    ) -> None:
        baseline = [
            {
                "address": address,
                "mode": "managed",
                "type": address.split(".", maxsplit=1)[0],
            }
            for address in sorted(REQUIRED_MANAGED_ADDRESSES)
        ] + [
            {"address": address, "mode": "data", "type": resource_type}
            for address, resource_type in sorted(ALLOWED_DATA_RESOURCE_TYPES.items())
        ]
        for reference_data_mode in ("retain", "disposable"):
            with self.subTest(mode=reference_data_mode):
                provider_resources = load_state_resources(
                    FIXTURES
                    / f"reference-data-{reference_data_mode}.provider-state.json"
                )
                self.assertEqual(len(provider_resources), 7)
                self.assertEqual(
                    {
                        resource["address"]: resource["type"]
                        for resource in provider_resources
                    },
                    REFERENCE_DATA_MANAGED_RESOURCE_TYPES[reference_data_mode],
                )
                self.assertEqual(
                    validate_resources(
                        baseline + provider_resources,
                        "create",
                        reference_data_mode=reference_data_mode,
                    ),
                    [],
                )

    def test_provider_state_fixture_rejects_type_and_count_drift(self) -> None:
        resources = load_state_resources(
            FIXTURES / "reference-data-disposable.provider-state.json"
        )
        resources[0]["type"] = "nebius_storage_v1_bucket"
        resources.append(dict(resources[1]))
        errors = validate_resources(
            resources,
            "create",
            edge_mode="internal-only",
            reference_data_mode="disposable",
        )
        self.assertTrue(any("count differs" in error for error in errors))
        self.assertTrue(any("duplicate state addresses" in error for error in errors))
        self.assertTrue(any("resource type" in error for error in errors))

    def test_retained_production_state_is_truthful_and_not_destroyed(self) -> None:
        addresses = sorted(
            REQUIRED_MANAGED_ADDRESSES
            | REFERENCE_DATA_MANAGED_ADDRESSES["retain"]
            | ALLOWED_DATA_ADDRESSES
        )
        self.assertEqual(
            validate_addresses(
                addresses, "retained", reference_data_mode="retain"
            ),
            [],
        )
        errors = validate_addresses([], "destroy", reference_data_mode="retain")
        self.assertTrue(any("cannot satisfy" in error for error in errors))

    def test_disposable_reference_acceptance_teardown_is_empty(self) -> None:
        self.assertEqual(
            validate_addresses([], "destroy", reference_data_mode="disposable"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
