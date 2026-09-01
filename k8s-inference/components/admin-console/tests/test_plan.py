from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "acceptance"))

from validate_plan import derive_status, validate_contracts  # noqa: E402


class AdminConsolePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = json.loads(
            (ROOT / "contracts" / "admin-console-plan.json").read_text()
        )
        cls.inventory = json.loads(
            (ROOT / "acceptance" / "inventory.fixture.json").read_text()
        )
        cls.fixtures = json.loads(
            (ROOT / "acceptance" / "status-cases.json").read_text()
        )

    def validate(
        self, plan: dict | None = None, inventory: dict | None = None
    ) -> list[str]:
        return validate_contracts(
            plan or self.plan, inventory or self.inventory, self.fixtures
        )

    def test_sealed_contract_passes(self) -> None:
        self.assertEqual(self.validate(), [])

    def test_every_status_fixture_matches(self) -> None:
        for fixture in self.fixtures:
            with self.subTest(fixture=fixture["name"]):
                self.assertEqual(derive_status(fixture["input"]), fixture["expected"])

    def test_unknown_fields_fail_closed(self) -> None:
        self.assertEqual(derive_status({"catalog_supported": True}), "unknown")
        self.assertEqual(
            derive_status(
                {
                    "catalog_supported": True,
                    "sources_fresh": True,
                    "health_failure": False,
                    "activation_phase": "none",
                    "desired_replicas": None,
                    "ready_replicas": 0,
                    "queued_operations": 0,
                }
            ),
            "unknown",
        )

    def test_duplicate_route_is_rejected(self) -> None:
        plan = copy.deepcopy(self.plan)
        plan["routes"][1]["path"] = plan["routes"][0]["path"]
        self.assertTrue(
            any("duplicate route paths" in error for error in self.validate(plan=plan))
        )

    def test_direct_browser_source_is_rejected(self) -> None:
        plan = copy.deepcopy(self.plan)
        plan["data_sources"][0]["browser_direct"] = True
        self.assertTrue(
            any("server-side" in error for error in self.validate(plan=plan))
        )

    def test_absent_observability_launch_is_rejected(self) -> None:
        plan = copy.deepcopy(self.plan)
        next(row for row in plan["observability_launches"] if row["id"] == "tempo")[
            "enabled"
        ] = True
        self.assertTrue(
            any(
                "absent component is launchable" in error
                for error in self.validate(plan=plan)
            )
        )

    def test_protected_resource_identifier_is_rejected(self) -> None:
        inventory = copy.deepcopy(self.inventory)
        inventory["scope"]["forbidden_example"] = "private-cluster-example"
        self.assertTrue(
            any(
                "protected resource identifier" in error
                for error in self.validate(inventory=inventory)
            )
        )

    def test_hard_coded_gpu_component_is_rejected(self) -> None:
        plan = copy.deepcopy(self.plan)
        plan["routes"][0]["components"].append("B300OnlyPanel")
        self.assertTrue(
            any("GPU family hard-coded" in error for error in self.validate(plan=plan))
        )

    def test_mutating_vertical_slice_endpoint_is_rejected(self) -> None:
        plan = copy.deepcopy(self.plan)
        plan["vertical_slice"]["bff_endpoints"].append("POST /admin/api/v1/keys")
        self.assertTrue(
            any(
                "not a read-only BFF GET" in error for error in self.validate(plan=plan)
            )
        )

    def test_route_inventory_matches_executable_bff_paths(self) -> None:
        endpoints = {
            endpoint
            for route in self.plan["routes"]
            for endpoint in route["bff"]
        }
        self.assertNotIn("/admin/api/v1/queues", endpoints)
        self.assertNotIn("/admin/api/v1/observability/capabilities", endpoints)
        self.assertIn("/admin/api/v1/capacity", endpoints)
        self.assertIn("/admin/api/v1/observability", endpoints)
        self.assertTrue(
            {
                "/admin/api/v1/configuration",
                "/admin/api/v1/configuration:diff",
                "/admin/api/v1/configuration:validate",
                "/admin/api/v1/configuration:plan",
                "/admin/api/v1/configuration:reconcile",
                "/admin/api/v1/configuration/reconciliations/{reconciliation_id}",
                "/admin/api/v1/configuration:rollback",
            }.issubset(endpoints)
        )


if __name__ == "__main__":
    unittest.main()
