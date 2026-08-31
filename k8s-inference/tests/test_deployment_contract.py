from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any


DEPLOY_ROOT = Path(__file__).resolve().parents[1]
PROFILES_ROOT = DEPLOY_ROOT / "catalog" / "profiles"
TEST_PROJECT_ID = "project-testinference"
TEST_TARGET = {
    "project_id": TEST_PROJECT_ID,
    "project_name": "inference-test-project",
    "region": "us-north1",
    "network": {
        "network_name": "default-network",
        "subnet_name": "default-subnet",
        "private_subnet_cidr": "10.0.0.0/16",
    },
    "system_update_strategy": {"max_surge": 1, "max_unavailable": 0},
}
TEST_APPLICATIONS = {
    "control_plane": {
        "repository": "registry.example.invalid/inference/control-plane",
        "digest": f"sha256:{'0' * 64}",
        "catalog_rollout_digest": f"sha256:{'0' * 64}",
    },
    "admin_console": {
        "repository": "registry.example.invalid/inference/admin-console",
        "digest": f"sha256:{'0' * 64}",
        "provenance": {
            "source_commit": "1" * 40,
            "source_tree": "2" * 40,
            "sbom_sha256": "3" * 64,
            "sbom_format": "cyclonedx-json",
        },
    },
}


class DeploymentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.terraform = shutil.which("terraform")
        if cls.terraform is None:
            raise unittest.SkipTest("terraform is required for deployment-contract tests")

        cls.model_profiles = json.loads(
            (PROFILES_ROOT / "model-profiles.json").read_text(encoding="utf-8")
        )["profiles"]

        cls.temporary = tempfile.TemporaryDirectory(prefix="fs2-deploy-tests-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.run_root = Path(cls.temporary.name)
        cls.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("TF_VAR_") and key != "TF_DATA_DIR"
        }
        cls.environment.update(
            {
                "TF_DATA_DIR": str(cls.run_root / "terraform-data"),
                "TF_IN_AUTOMATION": "1",
            }
        )
        result = cls._terraform(
            "init",
            "-input=false",
            "-no-color",
            "-reconfigure",
            f"-backend-config=path={cls.run_root / 'configuration.tfstate'}",
        )
        if result.returncode != 0:
            raise AssertionError(f"terraform init failed:\n{result.stderr}")

    @classmethod
    def _terraform(cls, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [cls.terraform, f"-chdir={DEPLOY_ROOT}", *arguments],
            env=cls.environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )

    @classmethod
    def _write_configuration(cls, name: str, deployment: dict[str, Any]) -> Path:
        deployment = dict(deployment)
        deployment.setdefault("applications", TEST_APPLICATIONS)
        path = cls.run_root / f"{name}.tfvars.json"
        path.write_text(
            json.dumps({"deployment": deployment}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    @classmethod
    def _plan_file(
        cls, variable_file: Path, name: str
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        plan_path = cls.run_root / f"{name}.tfplan"
        result = cls._terraform(
            "plan",
            "-input=false",
            "-lock=false",
            "-refresh=false",
            "-no-color",
            f"-var-file={variable_file}",
            f"-out={plan_path}",
        )
        return result, plan_path

    @classmethod
    def _planned_outputs(cls, variable_file: Path, name: str) -> dict[str, Any]:
        result, plan_path = cls._plan_file(variable_file, name)
        if result.returncode != 0:
            raise AssertionError(f"terraform plan failed:\n{result.stderr}")
        shown = cls._terraform("show", "-json", str(plan_path))
        if shown.returncode != 0:
            raise AssertionError(f"terraform show failed:\n{shown.stderr}")
        document = json.loads(shown.stdout)
        return {
            key: output["value"]
            for key, output in document["planned_values"]["outputs"].items()
        }

    def catalog_target(self) -> dict[str, Any]:
        return TEST_TARGET

    def test_one_configuration_normalizes_to_a_deterministic_stage_contract(
        self,
    ) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-normalization-test",
            "target": self.catalog_target(),
        }
        variable_file = self._write_configuration("normalized", deployment)
        outputs = self._planned_outputs(variable_file, "normalized")
        contract = outputs["deployment_contract"]

        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(contract["name"], deployment["name"])
        self.assertEqual(
            contract["target"],
            {"project_id": TEST_PROJECT_ID, "region": TEST_TARGET["region"]},
        )
        self.assertEqual(
            contract["profiles"],
            {"capacity": "minimal", "accelerators": "minimal", "models": "minimal"},
        )
        self.assertEqual(contract["selected_model_ids"], ["proteinmpnn"])
        self.assertEqual(
            contract["selected_accelerator_pool_ids"],
            ["nebius-b300-preemptible-1x", "nebius-b300-preemptible-8x"],
        )
        self.assertEqual(
            set(contract["stages"]), {"infrastructure", "foundation", "workloads"}
        )
        self.assertEqual(
            contract["stages"]["infrastructure"]["capacity_profile"], "minimal"
        )
        self.assertEqual(
            contract["stages"]["infrastructure"]["accelerator_pool_profile"],
            "minimal",
        )
        self.assertEqual(
            contract["stages"]["infrastructure"]["port_forward_local_ports"],
            {
                "control_plane": 18080,
                "admin_console": 18081,
                "operator_proxy": 18082,
            },
        )
        self.assertEqual(
            contract["stages"]["workloads"]["deployment_profile"], "minimal"
        )
        self.assertNotIn("nebius_profile", contract["stages"]["infrastructure"])
        self.assertEqual(
            contract["secret_environment"],
            {
                "grafana_username": "FS2_GRAFANA_ADMIN_USERNAME",
                "grafana_password": "FS2_GRAFANA_ADMIN_PASSWORD",
                "ngc_api_key": "FS2_NGC_API_KEY",
                "nvcr_dockerconfig": "FS2_NVCR_DOCKERCONFIGJSON",
            },
        )
        self.assertEqual(
            contract["secret_requirements"],
            {
                "grafana_bootstrap": True,
                "ngc_api_key": False,
                "nvcr_dockerconfig": False,
            },
        )
        self.assertIsNotNone(contract["stages"]["workloads"]["admin_console"])
        self.assertRegex(
            contract["stages"]["workloads"]["admin_console"]["image"]["digest"],
            r"^sha256:[a-f0-9]{64}$",
        )

        identity = json.dumps(
            {
                "name": deployment["name"],
                "project_id": TEST_PROJECT_ID,
                "region": TEST_TARGET["region"],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        self.assertEqual(
            contract["run_id"], f"r{hashlib.sha256(identity.encode()).hexdigest()[:10]}"
        )
        payload = {key: value for key, value in contract.items() if key != "sha256"}
        canonical_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        self.assertEqual(
            contract["sha256"], hashlib.sha256(canonical_payload.encode()).hexdigest()
        )
        self.assertEqual(outputs["effective_configuration"]["profiles"], contract["profiles"])
        self.assertEqual(
            outputs["effective_configuration"]["contract_sha256"], contract["sha256"]
        )
        self.assertEqual(
            outputs["effective_configuration"]["port_forward_ports"],
            contract["stages"]["infrastructure"]["port_forward_local_ports"],
        )

    def test_internal_edge_ports_can_be_offset_per_cluster(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-port-offset-test",
            "target": self.catalog_target(),
            "edge": {
                "mode": "internal-only",
                "port_forward_ports": {
                    "control_plane": 28080,
                    "admin_console": 28081,
                    "operator_proxy": 28082,
                },
            },
        }
        variable_file = self._write_configuration("port-offset", deployment)
        outputs = self._planned_outputs(variable_file, "port-offset")

        self.assertEqual(
            outputs["deployment_contract"]["stages"]["infrastructure"]
            ["port_forward_local_ports"],
            deployment["edge"]["port_forward_ports"],
        )
        self.assertEqual(
            outputs["effective_configuration"]["port_forward_ports"],
            deployment["edge"]["port_forward_ports"],
        )

    def test_internal_edge_ports_must_be_distinct_non_privileged_ports(self) -> None:
        invalid_ports = (
            {"control_plane": 28080, "admin_console": 28080, "operator_proxy": 28082},
            {"control_plane": 443, "admin_console": 28081, "operator_proxy": 28082},
            {"control_plane": 28080.5, "admin_console": 28081, "operator_proxy": 28082},
        )
        for index, ports in enumerate(invalid_ports):
            with self.subTest(ports=ports):
                deployment = {
                    "schema_version": 1,
                    "name": f"fs2-invalid-ports-{index}",
                    "target": self.catalog_target(),
                    "edge": {
                        "mode": "internal-only",
                        "port_forward_ports": ports,
                    },
                }
                variable_file = self._write_configuration(
                    f"invalid-ports-{index}", deployment
                )
                result, _ = self._plan_file(variable_file, f"invalid-ports-{index}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "three distinct whole TCP ports",
                    f"{result.stdout}\n{result.stderr}",
                )

    def test_full_catalog_b300_can_have_zero_hot_nodes_and_models(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-b300-zero-hot",
            "profiles": {
                "capacity": "full_catalog",
                "accelerators": "full_catalog",
                "models": "full_catalog",
            },
            "target": self.catalog_target(),
            "accelerator_pool_capacity": {
                "nebius-b300-preemptible-1x": {"min_nodes": 0, "max_nodes": 6},
                "nebius-b300-preemptible-8x": {"min_nodes": 0, "max_nodes": 2},
            },
            "models": {
                "selection": "profile",
                "enabled": [],
                "scaling": {"mode": "keda", "hot": []},
            },
            "edge": {"mode": "internal-only", "source_cidrs": []},
        }
        variable_file = self._write_configuration("b300-zero-hot", deployment)
        contract = self._planned_outputs(variable_file, "b300-zero-hot")[
            "deployment_contract"
        ]

        self.assertEqual(
            contract["selected_model_ids"],
            sorted(self.model_profiles["full_catalog"]["canonical_routes"]),
        )
        self.assertIn("glm-5-2-fp8", contract["selected_model_ids"])
        self.assertIn("qwen3-8b", contract["selected_model_ids"])
        self.assertTrue(
            all("b300-preemptible" in pool for pool in contract["selected_accelerator_pool_ids"])
        )
        infrastructure = contract["stages"]["infrastructure"]
        self.assertEqual(infrastructure["gpu_floor_profile"], "zero")
        self.assertEqual(
            infrastructure["accelerator_pool_capacity_overrides"],
            deployment["accelerator_pool_capacity"],
        )
        workloads = contract["stages"]["workloads"]
        self.assertEqual(workloads["hot_model_ids"], [])
        self.assertEqual(workloads["model_scaling_mode"], "keda")
        self.assertTrue(contract["secret_requirements"]["ngc_api_key"])
        self.assertTrue(contract["secret_requirements"]["nvcr_dockerconfig"])

    def test_pool_override_preserves_scale_from_zero_selector_contract(self) -> None:
        source = (DEPLOY_ROOT / "stages" / "workloads" / "locals.tf").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '"accelerator.fs2.nebius/class"   = local.selected_queue_pools',
            source,
        )
        self.assertIn(
            '"accelerator.fs2.nebius/pool-id" = var.model_pool_overrides',
            source,
        )
        self.assertIn(
            '"kubernetes.io/arch"             = local.selected_queue_pools',
            source,
        )
        self.assertIn(
            "capacity.scale_from_zero &&\n"
            "                    contains(\n"
            "                      local.selected_queue_pools[var.model_pool_overrides[document.model_id]].scheduling.forbidden_scale_zero_selectors,\n"
            "                      key,",
            source,
        )
        self.assertIn(
            "} : key => value\n"
            "                  if !(\n"
            "                    local.selected_queue_pools",
            source,
        )

    def test_replica_override_uses_compatible_accelerator_capacity(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-replica-capacity",
            "profiles": {
                "capacity": "full_catalog",
                "accelerators": "full_catalog",
                "models": "full_catalog",
            },
            "target": self.catalog_target(),
            "models": {
                "selection": "explicit",
                "enabled": ["qwen3-8b"],
                "scaling": {
                    "mode": "keda",
                    "overrides": {
                        "qwen3-8b": {
                            "min_replicas": 0,
                            "max_replicas": 2,
                            "target_queue_depth": 1,
                            "polling_interval_seconds": 5,
                            "cooldown_seconds": 300,
                        }
                    },
                },
            },
            "edge": {"mode": "internal-only"},
        }
        variable_file = self._write_configuration("replica-capacity", deployment)
        contract = self._planned_outputs(variable_file, "replica-capacity")[
            "deployment_contract"
        ]
        self.assertEqual(contract["selected_model_replica_ceilings"]["qwen3-8b"], 16)

        deployment["models"]["scaling"]["overrides"]["qwen3-8b"][
            "max_replicas"
        ] = 17
        variable_file = self._write_configuration(
            "replica-over-capacity", deployment
        )
        result, _ = self._plan_file(variable_file, "replica-over-capacity")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "exceeds the maximum replicas supported",
            f"{result.stdout}\n{result.stderr}",
        )

    def test_future_gpu_platform_and_preset_pass_through_the_facade(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "future-gpu-contract",
            "profiles": {"capacity": "minimal", "models": "none"},
            "target": self.catalog_target(),
            "accelerator_pools": {
                "future-gpu-pool": {
                    "platform": "gpu-future-sxm",
                    "preset": "4gpu-96vcpu-1024gb",
                    "accelerator_class": "nvidia-future-sxm",
                    "gpus_per_node": 4,
                    "capacity_type": "preemptible",
                    "min_nodes": 0,
                    "max_nodes": 3,
                    "driver": {"mode": "managed", "preset": "cuda-future"},
                }
            },
            "models": {"selection": "profile"},
            "edge": {"mode": "internal-only"},
        }
        variable_file = self._write_configuration("future-gpu", deployment)
        contract = self._planned_outputs(variable_file, "future-gpu")[
            "deployment_contract"
        ]

        self.assertTrue(contract["custom_accelerator_pools"])
        self.assertEqual(contract["selected_accelerator_pool_ids"], ["future-gpu-pool"])
        self.assertEqual(contract["selected_model_ids"], [])
        self.assertEqual(
            contract["stages"]["infrastructure"]["custom_accelerator_pools"]
            ["future-gpu-pool"]["platform"],
            "gpu-future-sxm",
        )

    def test_unqualified_heterogeneous_profile_fails_before_cloud_plan(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-heterogeneous-test",
            "profiles": {
                "capacity": "minimal",
                "accelerators": "heterogeneous_reference",
                "models": "minimal",
            },
            "target": self.catalog_target(),
            "models": {"selection": "profile"},
            "edge": {"mode": "internal-only"},
        }
        variable_file = self._write_configuration("heterogeneous", deployment)
        result, plan_path = self._plan_file(variable_file, "heterogeneous")

        self.assertNotEqual(result.returncode, 0)
        diagnostics = f"{result.stdout}\n{result.stderr}"
        self.assertIn(
            "selected accelerator-pool profile is not enabled and hardware-validated",
            diagnostics,
        )
        # Terraform may retain a local failed-plan artifact, but this facade has
        # no cloud provider or child module from which a cloud plan could run.
        providers = self._terraform("providers")
        self.assertEqual(providers.returncode, 0, providers.stderr)
        self.assertNotIn("registry.terraform.io/nebius", providers.stdout)
        self.assertNotIn('module "', (DEPLOY_ROOT / "main.tf").read_text())
        if plan_path.exists():
            self.assertGreater(plan_path.stat().st_size, 0)

    def test_shipped_examples_track_the_executable_contract(self) -> None:
        successful = (
            DEPLOY_ROOT / "terraform.tfvars.example",
            DEPLOY_ROOT / "examples/b300-zero-hot.tfvars",
        )
        for index, variable_file in enumerate(successful):
            with self.subTest(example=variable_file.name):
                result, _ = self._plan_file(variable_file, f"shipped-{index}")
                self.assertEqual(result.returncode, 0, result.stderr)

        rejected = DEPLOY_ROOT / "examples/heterogeneous-unqualified.tfvars"
        result, _ = self._plan_file(rejected, "shipped-heterogeneous")
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(
            f"{result.stdout}\n{result.stderr}",
            re.compile(r"not enabled and hardware-validated", re.IGNORECASE),
        )


if __name__ == "__main__":
    unittest.main()
