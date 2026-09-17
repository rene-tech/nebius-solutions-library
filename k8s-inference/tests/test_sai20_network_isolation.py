"""Static regression contract for SAI-20 network isolation.

These tests intentionally inspect the Terraform source boundary. Live denial
still has to be proved after an independently accepted integration rollout.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "stages/workloads/sai20_network_isolation.tf"
CONTROL_PLANE = ROOT / "stages/workloads/control_plane.tf"
DATABASE = ROOT / "stages/workloads/database.tf"
OUTPUTS = ROOT / "stages/workloads/outputs.tf"


def balanced_block(source: str, opening: str) -> str:
    start = source.index(opening) + len(opening)
    depth = 1
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index]
    raise AssertionError(f"unbalanced source block after {opening!r}")


class Sai20NetworkIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = POLICY.read_text(encoding="utf-8")
        cls.control_plane = CONTROL_PLANE.read_text(encoding="utf-8")
        cls.database_policy = balanced_block(
            cls.policy,
            'resource "kubernetes_network_policy_v1" "control_database_ingress" {',
        )

    def test_control_plane_api_egress_excludes_the_private_subnet(self) -> None:
        exact_hosts = re.search(
            r"sai20_kubernetes_api_egress_cidrs\s*=\s*setunion\((.*?)\n\s*\)",
            self.policy,
            re.DOTALL,
        )
        self.assertIsNotNone(exact_hosts)
        body = exact_hosts.group(1)
        self.assertIn("local.kubernetes_api_service_cidrs", body)
        self.assertIn("local.kubernetes_api_endpoint_cidrs", body)
        self.assertNotIn("private_subnet_cidr", body)
        self.assertNotIn("kubernetes_api_egress_cidrs", body)

    def test_exact_api_hosts_are_the_final_control_plane_values_overlay(self) -> None:
        broad = "yamlencode(local.control_plane_overrides)"
        exact = "yamlencode(local.sai20_control_plane_network_policy_overrides)"
        self.assertIn(broad, self.control_plane)
        self.assertIn(exact, self.control_plane)
        self.assertLess(self.control_plane.index(broad), self.control_plane.index(exact))
        overlay = balanced_block(
            self.policy,
            "sai20_control_plane_network_policy_overrides = {",
        )
        self.assertIn("kubernetesApiCidrs", overlay)
        self.assertIn("local.sai20_kubernetes_api_egress_cidrs", overlay)

    def test_database_policy_selects_only_the_control_database(self) -> None:
        self.assertIn('name      = "fs2-control-db-ingress"', self.database_policy)
        self.assertIn('namespace = "fs2-data"', self.database_policy)
        self.assertIn('"cnpg.io/cluster" = "fs2-control-db"', self.database_policy)
        self.assertIn('policy_types = ["Ingress"]', self.database_policy)
        self.assertNotIn("pod_selector {}", self.database_policy)
        self.assertNotIn('cidr = "0.0.0.0/0"', self.database_policy)
        self.assertNotIn('cidr = "::/0"', self.database_policy)

    def test_database_client_component_allowlist_is_finite(self) -> None:
        match = re.search(
            r"sai20_database_client_components\s*=\s*\[(.*?)\n\s*\]",
            self.policy,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        self.assertEqual(
            set(re.findall(r'"([a-z0-9-]+)"', match.group(1))),
            {
                "bootstrap-access",
                "bootstrap-scientific-access",
                "bootstrap-website-access",
                "gateway",
                "maintenance",
                "migration",
                "model-controller",
                "storage-disclosure",
                "storage-reconciler",
            },
        )
        self.assertIn('"kubernetes.io/metadata.name" = "fs2-system"', self.database_policy)
        self.assertIn('"app.kubernetes.io/instance" = "fs2-serve-control-plane"', self.database_policy)
        self.assertIn('"app.kubernetes.io/name"     = "fs2-serve-control-plane"', self.database_policy)
        self.assertIn("values   = local.sai20_database_client_components", self.database_policy)

    def test_grafana_and_run_bound_acceptance_are_explicit_database_peers(self) -> None:
        self.assertIn('"app.kubernetes.io/name" = "grafana"', self.database_policy)
        self.assertEqual(
            self.database_policy.count(
                '"app.kubernetes.io/component"  = "acceptance"'
            ),
            2,
        )
        self.assertEqual(
            self.database_policy.count('"fs2.nebius.ai/run-id"         = var.run_id'),
            2,
        )
        self.assertGreaterEqual(
            self.database_policy.count('port     = "5432"'),
            6,
        )

    def test_cnpg_ha_status_and_metrics_paths_remain_bounded(self) -> None:
        self.assertIn('"kubernetes.io/metadata.name" = "cnpg-system"', self.database_policy)
        self.assertIn('"app.kubernetes.io/name" = "cloudnative-pg"', self.database_policy)
        self.assertEqual(self.database_policy.count('port     = "8000"'), 2)
        self.assertEqual(self.database_policy.count('port     = "9187"'), 1)
        self.assertIn('"app.kubernetes.io/name" = "prometheus"', self.database_policy)

    def test_policy_precedes_database_and_is_counted_as_a_managed_address(self) -> None:
        database = DATABASE.read_text(encoding="utf-8")
        outputs = OUTPUTS.read_text(encoding="utf-8")
        self.assertIn(
            '"fs2.nebius.ai/ingress-network-policy" = '
            "kubernetes_network_policy_v1.control_database_ingress.metadata[0].name",
            database,
        )
        self.assertIn(
            "kubernetes_network_policy_v1.control_database_ingress,",
            self.control_plane,
        )
        self.assertIn(
            "# SAI-20 adds one Terraform-owned fs2-data database ingress policy.\n"
            "    1 +",
            outputs,
        )


if __name__ == "__main__":
    unittest.main()
