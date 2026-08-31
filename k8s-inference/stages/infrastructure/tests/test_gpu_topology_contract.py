from __future__ import annotations

import unittest
from pathlib import Path


INFRA_ROOT = Path(__file__).resolve().parents[1]


class GpuTopologyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cluster = (INFRA_ROOT / "cluster.tf").read_text(encoding="utf-8")
        cls.software = (INFRA_ROOT / "gpu_software.tf").read_text(
            encoding="utf-8"
        )
        cls.variables = (INFRA_ROOT / "variables.tf").read_text(encoding="utf-8")

    def test_generic_standard_pools_project_provider_values_without_allowlists(
        self,
    ) -> None:
        self.assertIn(
            'if pool.node.topology != "nvlink_rack"', self.cluster
        )
        self.assertIn(
            'resource "nebius_mk8s_v1_node_group" "gpu"', self.cluster
        )
        self.assertIn("for_each = local.standard_gpu_pools", self.cluster)
        self.assertIn("platform = each.value.provider.platform", self.cluster)
        self.assertIn("preset   = each.value.provider.preset", self.cluster)
        self.assertIn("platform               = pool.platform", self.variables)
        self.assertIn("preset                 = pool.preset", self.variables)
        self.assertIn("selected_gpu_pools = merge(", self.variables)
        self.assertNotIn("gpu-platforms.json", self.variables)
        self.assertNotIn("contains([\"gpu-", self.variables)

    def test_gpu_cluster_pools_own_fabric_and_network_operator(self) -> None:
        self.assertIn(
            "if try(length(trimspace(pool.topology.infiniband_fabric)) > 0, false)",
            self.cluster,
        )
        self.assertIn(
            'resource "nebius_compute_v1_gpu_cluster" "pool"', self.cluster
        )
        self.assertIn(
            "infiniband_fabric = each.value.topology.infiniband_fabric",
            self.cluster,
        )
        self.assertIn(
            'gpu_cluster = each.value.node.topology == "gpu_cluster" ? '
            "nebius_compute_v1_gpu_cluster.pool[each.key] : null",
            self.cluster,
        )
        self.assertIn(
            "network_operator_required = length(local.gpu_cluster_pools) > 0",
            self.software,
        )
        self.assertIn(
            "count  = local.network_operator_required ? 1 : 0", self.software
        )

    def test_gb300_nvlink_racks_are_whole_managed_groups(self) -> None:
        for invariant in (
            'pool.platform == "gpu-gb300"',
            'pool.preset == "4gpu-112vcpu-800gb"',
            'pool.accelerator_class == "nvidia-gb300"',
            "pool.gpus_per_node == 4",
            'pool.host_architecture == "arm64"',
            'pool.capacity_type == "regular"',
            'pool.driver.mode == "managed"',
            'pool.mig.strategy == "none"',
            "pool.topology.nodes_per_rack == 18",
        ):
            self.assertIn(invariant, self.variables)
        self.assertIn(
            'resource "nebius_compute_v1_nvl_instance_group" "rack"',
            self.cluster,
        )
        self.assertIn('type      = "GB300"', self.cluster)
        self.assertIn(
            "fixed_node_count = each.value.pool.topology.nodes_per_rack",
            self.cluster,
        )
        self.assertIn(
            "taints      = each.value.pool.scheduling.taints", self.cluster
        )
        self.assertIn(
            "gpu_cluster        = try(length(trimspace(each.value.pool.topology.infiniband_fabric)) > 0, false) ? "
            "nebius_compute_v1_gpu_cluster.pool[each.value.pool_id] : null",
            self.cluster,
        )
        self.assertIn(
            "pool.topology.rack_count == 1 || "
            "try(length(trimspace(pool.topology.infiniband_fabric)) > 0, false)",
            self.variables,
        )

    def test_driver_ownership_is_cluster_wide_and_mig_is_operator_only(
        self,
    ) -> None:
        self.assertIn(
            'each.value.provider.driver.owner == "provider-managed" ? {',
            self.cluster,
        )
        self.assertIn(
            "count  = length(local.managed_gpu_pools) > 0 ? 1 : 0",
            self.software,
        )
        self.assertIn(
            "count  = length(local.operator_gpu_pools) > 0 ? 1 : 0",
            self.software,
        )
        self.assertIn(
            'pool.driver.mode == "managed" ? '
            "try(length(trimspace(pool.driver.preset)) > 0, false) : "
            "pool.driver.preset == null",
            self.variables,
        )
        self.assertIn(
            "length(local.managed_gpu_pools) == 0 || "
            "length(local.operator_gpu_pools) == 0",
            self.software,
        )
        self.assertIn('pool.driver.mode == "operator"', self.variables)
        self.assertIn(
            "resource_name = pool.resource_name", self.variables
        )
        mig_validation = self.variables.split(
            'contains(["none", "single", "mixed"], pool.mig.strategy)', 1
        )[1].split("error_message", 1)[0]
        self.assertNotIn('pool.resource_name == "nvidia.com/gpu"', mig_validation)
        self.assertNotIn('pool.resource_name != "nvidia.com/gpu"', mig_validation)
        self.assertIn(
            '"nvidia.com/mig.config" = each.value.features.mig.config',
            self.cluster,
        )


if __name__ == "__main__":
    unittest.main()
