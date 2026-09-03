from __future__ import annotations

import copy
import json
import re
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
        cls.api_contract = json.loads(
            (ROOT / "contracts" / "admin-api-v1.json").read_text()
        )
        cls.scientific_fixture_contract = json.loads(
            (ROOT / "contracts" / "scientific-admin-fixture-v1.json").read_text()
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

    def test_plan_bff_paths_are_sealed_by_the_admin_api_contract(self) -> None:
        endpoints = {
            endpoint
            for route in self.plan["routes"]
            for endpoint in route["bff"]
        }
        contract_routes = {
            (route["method"], route["path"])
            for route in self.api_contract["routes"]
        }
        contract_paths = {path for _, path in contract_routes}
        self.assertEqual(len(contract_routes), len(self.api_contract["routes"]))
        self.assertTrue(endpoints.issubset(contract_paths))
        self.assertNotIn("/admin/api/v1/queues", endpoints)
        self.assertNotIn("/admin/api/v1/observability/capabilities", endpoints)
        self.assertNotIn("/admin/api/v1/queues", contract_paths)
        self.assertNotIn(
            "/admin/api/v1/observability/capabilities", contract_paths
        )
        self.assertIn("/admin/api/v1/capacity", endpoints)
        self.assertIn("/admin/api/v1/observability", endpoints)

        configuration_routes = {
            ("GET", "/admin/api/v1/configuration"),
            ("POST", "/admin/api/v1/configuration:diff"),
            ("POST", "/admin/api/v1/configuration:validate"),
            ("POST", "/admin/api/v1/configuration:plan"),
            ("POST", "/admin/api/v1/configuration:reconcile"),
            (
                "GET",
                "/admin/api/v1/configuration/reconciliations/{reconciliation_id}",
            ),
            ("POST", "/admin/api/v1/configuration:rollback"),
        }
        self.assertTrue(configuration_routes.issubset(contract_routes))
        self.assertTrue(
            {path for _, path in configuration_routes}.issubset(endpoints)
        )

        model_deployment_routes = {
            ("GET", "/admin/api/v1/model-deployments"),
            ("GET", "/admin/api/v1/model-deployments/{name}"),
            ("GET", "/admin/api/v1/model-deployments/{name}/history"),
            ("GET", "/admin/api/v1/model-deployments/{name}/status"),
            ("POST", "/admin/api/v1/model-deployments:validate-preview"),
            ("POST", "/admin/api/v1/model-deployments:plan-preview"),
            ("GET", "/admin/api/v1/model-deployments:capabilities"),
            ("POST", "/admin/api/v1/model-deployments:apply"),
            ("POST", "/admin/api/v1/model-deployments/{name}:drain"),
            ("POST", "/admin/api/v1/model-deployments/{name}:rollback"),
            ("POST", "/admin/api/v1/model-deployments/{name}:reconcile"),
        }
        self.assertTrue(model_deployment_routes.issubset(contract_routes))
        self.assertTrue(
            {path for _, path in model_deployment_routes}.issubset(endpoints)
        )
        scientific_routes = {
            ("GET", "/admin/api/v1/scientific-capabilities"),
            ("GET", "/admin/api/v1/scientific-runs"),
            ("GET", "/admin/api/v1/scientific-runs/{run_id}"),
            ("GET", "/admin/api/v1/scientific-models"),
        }
        self.assertTrue(scientific_routes.issubset(contract_routes))

        feature_paths = {
            path
            for group in self.api_contract["feature_gated_route_groups"]
            for path in group["paths"]
        }
        self.assertEqual(
            feature_paths,
            {
                path
                for _, path in configuration_routes | model_deployment_routes | scientific_routes
            },
        )
        disabled_only = {
            (route["method"], route["path"], route["status"], route["code"])
            for route in self.api_contract["feature_disabled_problem_routes"]
        }
        self.assertEqual(
            disabled_only,
            {
                (
                    "POST",
                    "/admin/api/v1/model-deployments:apply",
                    501,
                    "model_deployment_writer_disabled",
                ),
                (
                    "POST",
                    "/admin/api/v1/model-deployments/{name}:adopt",
                    501,
                    "model_deployment_writer_disabled",
                ),
                (
                    "DELETE",
                    "/admin/api/v1/model-deployments/{name}",
                    501,
                    "model_deployment_writer_disabled",
                ),
            },
        )

    def test_qualified_model_options_are_sealed_to_installed_evidence(self) -> None:
        contract = self.api_contract["model_deployment_configuration_options"]
        contract_paths = {route["path"] for route in self.api_contract["routes"]}
        self.assertEqual(
            contract["endpoint"],
            "/admin/api/v1/model-deployments:capabilities",
        )
        self.assertIn(contract["endpoint"], contract_paths)
        self.assertIn("installed InfrastructureEnvelope", contract["authority"])
        self.assertEqual(contract["source_revision_field"], "configuration_revision")
        self.assertEqual(
            set(contract["option_fields"]),
            {
                "model_ref",
                "suggested_name",
                "namespace",
                "default_spec",
                "pool_choices",
                "local_queue_choices",
                "priority_class_choices",
                "tenant_choices",
                "scale_to_zero_qualified",
            },
        )
        self.assertIn("accepted disposition", contract["default_invariant"])
        self.assertIn("omitted", contract["failure_contract"])
        self.assertIn("never fabricates", contract["failure_contract"])
        self.assertIn(
            "qualified-model-options-fail-closed",
            self.plan["acceptance_cases"],
        )

    def test_client_bounds_match_the_live_query_contract(self) -> None:
        bounds = self.api_contract["bounds"]
        self.assertEqual(bounds["maximum_model_search_characters"], 128)
        self.assertEqual(bounds["maximum_model_page_size"], 256)
        self.assertEqual(bounds["maximum_operation_page_size"], 200)
        self.assertEqual(bounds["maximum_access_page_size"], 1000)
        self.assertEqual(bounds["maximum_model_deployment_namespace_characters"], 63)
        self.assertEqual(bounds["maximum_model_deployment_name_characters"], 253)
        self.assertEqual(bounds["maximum_model_deployment_page_size"], 200)

    def test_plan_pages_match_the_executable_react_routes(self) -> None:
        source = (ROOT / "src" / "app" / "App.tsx").read_text()
        for route in self.plan["routes"]:
            with self.subTest(route=route["id"]):
                if route["path"] == "/admin":
                    pattern = rf'<Route\s+index\s+element=\{{<{route["page"]}\s*/>\}}'
                else:
                    relative_path = route["path"].removeprefix("/admin/")
                    pattern = (
                        rf'<Route\s+path="{re.escape(relative_path)}"\s+'
                        rf'element=\{{<{route["page"]}(?:\s|/>)'
                    )
                self.assertRegex(source, pattern)

    def test_scientific_admin_contract_gates_routes_by_real_producer_capabilities(self) -> None:
        contract = self.scientific_fixture_contract
        self.assertEqual(contract["status"], "capability-gated-live-model-readiness")
        self.assertFalse(contract["access"]["credentials_exposed"])
        self.assertEqual(
            contract["gpu_accounting"]["evidence_states"],
            ["measured", "estimated", "unavailable"],
        )
        self.assertIn("never rendered as measured", contract["gpu_accounting"]["invariant"])
        self.assertIn("explicit-alternative", contract["access"]["native_gate_invariant"])

        # Fixtures back tests and Vite fixture mode only; production must not
        # fall back to them, and the contract must say which producers are absent.
        self.assertIn("no fixture fallback", contract["production_behavior"])
        self.assertEqual(
            set(contract["pending_producers"]),
            {"scientific-controller", "scientific-artifacts"},
        )

        # The capability route and every potentially enabled data route are
        # sealed in the API contract; runtime registration remains producer-gated.
        scientific_paths = {route["path"] for route in contract["routes"]}
        live_paths = {route["path"] for route in self.api_contract["routes"]}
        self.assertTrue(scientific_paths.issubset(live_paths))
        self.assertEqual(
            scientific_paths,
            {
                "/admin/api/v1/scientific-capabilities",
                "/admin/api/v1/scientific-runs",
                "/admin/api/v1/scientific-runs/{run_id}",
                "/admin/api/v1/scientific-models",
            },
        )
        gated = {group["id"]: group for group in self.api_contract["feature_gated_route_groups"]}
        self.assertEqual(set(gated["scientific-operations"]["paths"]), scientific_paths)

        client_source = (ROOT / "src" / "api" / "client.ts").read_text()
        self.assertIn('request<ScientificCapabilities>("/scientific-capabilities"', client_source)
        self.assertIn('request<ScientificRunList>("/scientific-runs"', client_source)
        self.assertIn('request<ScientificModelReadinessList>("/scientific-models"', client_source)

        app_source = (ROOT / "src" / "app" / "App.tsx").read_text()
        self.assertIn('<Route path="scientific-runs" element={<ScientificRunsPage />} />', app_source)
        self.assertIn('<Route path="scientific-runs/:runId" element={<ScientificRunDetailPage />} />', app_source)


if __name__ == "__main__":
    unittest.main()
