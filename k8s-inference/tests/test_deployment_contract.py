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

import yaml


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
        # Application and catalog are independently immutable tfvars inputs;
        # deployment must not depend on a source-code digest-pair allowlist.
        "catalog_rollout_digest": f"sha256:{'1' * 64}",
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


def ephemeral_storage_gib(quantity: object | None) -> float:
    if quantity is None:
        return 0.0
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti)?", str(quantity))
    if match is None:
        raise AssertionError(f"unsupported ephemeral-storage quantity: {quantity!r}")
    value = float(match.group(1))
    return value * {
        None: 1 / 1073741824,
        "Ki": 1 / 1048576,
        "Mi": 1 / 1024,
        "Gi": 1,
        "Ti": 1024,
    }[match.group(2)]


def container_ephemeral_request_gib(container: dict[str, Any]) -> float:
    resources = container.get("resources", {})
    request = resources.get("requests", {}).get("ephemeral-storage")
    if request is None:
        request = resources.get("limits", {}).get("ephemeral-storage")
    return ephemeral_storage_gib(request)


def pod_ephemeral_request_gib(pod_spec: dict[str, Any]) -> float:
    init_stages: list[float] = []
    restartable_init = 0.0
    for container in pod_spec.get("initContainers", []):
        request = container_ephemeral_request_gib(container)
        if container.get("restartPolicy") == "Always":
            restartable_init += request
            init_stages.append(restartable_init)
        else:
            init_stages.append(restartable_init + request)
    application = restartable_init + sum(
        container_ephemeral_request_gib(container)
        for container in pod_spec.get("containers", [])
    )
    overhead = ephemeral_storage_gib(
        pod_spec.get("overhead", {}).get("ephemeral-storage")
    )
    return max([application, *init_stages, 0.0]) + overhead


class DeploymentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.terraform = shutil.which("terraform")
        if cls.terraform is None:
            raise unittest.SkipTest("terraform is required for deployment-contract tests")

        cls.model_contract = json.loads(
            (PROFILES_ROOT / "model-profiles.json").read_text(encoding="utf-8")
        )
        cls.model_profiles = cls.model_contract["profiles"]

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
        runtime_catalog = json.loads(
            (DEPLOY_ROOT / "catalog/runtime/models/proteinmpnn.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            contract["stages"]["workloads"]["model_image_overrides"],
            {"proteinmpnn": runtime_catalog["runtime"]["image"]["reference"]},
        )
        self.assertEqual(
            contract["stages"]["workloads"]["model_controller"],
            {
                "enabled": False,
                "writes_enabled": False,
                "workload_owner": "terraform",
                "bootstrap_model_ids": [],
                "fresh_install": False,
                "handoff_receipt": None,
                "fast_start_evidence_file": None,
                "fast_start_environment_qualifications_file": None,
                "fast_start_measurement_contracts_file": None,
                "fast_start_wait_second_value": 0.01,
                "fast_start_mechanism_hourly_costs": {},
                "priority_classes": {
                    "interactive": 100,
                    "standard": 0,
                    "batch": -100,
                },
            },
        )
        self.assertEqual(contract["artifact_delivery"]["mode"], "regional-mirror")
        self.assertEqual(contract["artifact_delivery"]["repository_prefix"], "")
        self.assertIn(
            "nvcr.io", contract["stages"]["infrastructure"]["registry_delivery"]["source_hosts"]
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

    def test_dynamic_model_tfvars_normalize_without_internal_json(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-dynamic-model-test",
            "target": self.catalog_target(),
            "profiles": {"models": "full_catalog"},
            "models": {
                "selection": "explicit",
                "enabled": ["qwen3-8b"],
                "scaling": {"mode": "keda", "hot": ["qwen3-8b"]},
            },
            "dynamic_models": {
                "enabled": True,
                "writes_enabled": True,
                "workload_owner": "controller",
                "bootstrap_model_ids": ["qwen3-8b"],
                "fresh_install": True,
            },
        }
        variable_file = self._write_configuration("dynamic-models", deployment)
        outputs = self._planned_outputs(variable_file, "dynamic-models")
        dynamic = outputs["deployment_contract"]["stages"]["workloads"][
            "model_controller"
        ]

        self.assertEqual(
            dynamic,
            {
                "enabled": True,
                "writes_enabled": True,
                "workload_owner": "controller",
                "bootstrap_model_ids": ["qwen3-8b"],
                "fresh_install": True,
                "handoff_receipt": None,
                "fast_start_evidence_file": None,
                "fast_start_environment_qualifications_file": None,
                "fast_start_measurement_contracts_file": None,
                "fast_start_wait_second_value": 0.01,
                "fast_start_mechanism_hourly_costs": {},
                "priority_classes": {
                    "interactive": 100,
                    "standard": 0,
                    "batch": -100,
                },
            },
        )
        self.assertNotIn("infrastructure_envelope_json", dynamic)
        self.assertNotIn("renderer_bundles_json", dynamic)

    def test_modelexpress_tfvars_resolve_managed_service_and_exact_model_clients(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-modelexpress-test",
            "target": self.catalog_target(),
            "profiles": {"models": "full_catalog"},
            "models": {
                "selection": "explicit",
                "enabled": ["qwen3-8b"],
                "scaling": {"mode": "keda", "hot": ["qwen3-8b"]},
            },
            "dynamic_models": {
                "enabled": True,
                "writes_enabled": True,
                "workload_owner": "controller",
                "bootstrap_model_ids": ["qwen3-8b"],
                "fresh_install": True,
            },
            "acceleration": {
                "model_express": {
                    "enabled": True,
                    "deployment_mode": "managed",
                    "server_image": {
                        "repository": "nvcr.io/nvidia/ai-dynamo/modelexpress-server",
                        "digest": f"sha256:{'9' * 64}",
                    },
                    "cache": {"enabled": True, "size_gib": 200},
                    "models": {
                        "qwen3-8b": {
                            "runtime_adapter": "vllm",
                            "transport": {
                                "mode": "nixl-rdma",
                                "rdma_resource_name": "example.com/rdma_shared_device_a",
                                "rdma_resource_quantity": 8,
                                "nixl_backend": "UCX",
                                "nic_pin": "auto",
                            },
                        }
                    },
                }
            },
        }
        outputs = self._planned_outputs(
            self._write_configuration("modelexpress", deployment),
            "modelexpress",
        )
        configured = outputs["deployment_contract"]["stages"]["workloads"][
            "model_express"
        ]
        self.assertEqual(
            configured["endpoint"],
            "fs2-modelexpress.fs2-modelexpress.svc.cluster.local:8001",
        )
        self.assertEqual(configured["server_image"]["digest"], f"sha256:{'9' * 64}")
        self.assertTrue(outputs["deployment_contract"]["secret_requirements"]["nvcr_dockerconfig"])
        self.assertTrue(
            outputs["effective_configuration"]["model_express"][
                "managed_nvcr_server_requires_pull_secret"
            ]
        )
        self.assertEqual(
            configured["models"],
            {
                "qwen3-8b": {
                    "runtime_adapter": "vllm",
                    "client_package_version": "0.5.1",
                    "transport": {
                        "mode": "nixl-rdma",
                        "rdma_resource_name": "example.com/rdma_shared_device_a",
                        "rdma_resource_quantity": 8,
                        "nixl_backend": "UCX",
                        "nic_pin": "auto",
                    },
                    "pool_transports": {},
                }
            },
        )
        self.assertEqual(outputs["effective_configuration"]["model_express"]["model_ids"], ["qwen3-8b"])
        self.assertEqual(
            outputs["effective_configuration"]["model_express"]["models"]["qwen3-8b"]["transport_default"]["mode"],
            "nixl-rdma",
        )

    def test_modelexpress_rejects_a_runtime_kind_that_only_claims_the_vllm_adapter(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-modelexpress-runtime-kind-test",
            "target": self.catalog_target(),
            "profiles": {"models": "full_catalog"},
            "models": {
                "selection": "explicit",
                "enabled": ["cosmos3-nano"],
                "scaling": {"mode": "keda"},
            },
            "dynamic_models": {
                "enabled": True,
                "writes_enabled": True,
                "workload_owner": "controller",
                "bootstrap_model_ids": ["cosmos3-nano"],
                "fresh_install": True,
            },
            "acceleration": {
                "model_express": {
                    "enabled": True,
                    "deployment_mode": "managed",
                    "server_image": {
                        "repository": "registry.example.test/modelexpress-server",
                        "digest": f"sha256:{'9' * 64}",
                    },
                    "models": {
                        # A tfvars assertion cannot turn vLLM-Omni into the
                        # explicitly supported text-vLLM integration.
                        "cosmos3-nano": {"runtime_adapter": "vllm"}
                    },
                }
            },
        }
        variable_file = self._write_configuration("modelexpress-runtime-kind", deployment)
        result, _ = self._plan_file(variable_file, "modelexpress-runtime-kind")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "select explicit vLLM catalog models",
            f"{result.stdout}\n{result.stderr}",
        )

    def test_modelexpress_external_endpoint_and_network_route_fail_closed(self) -> None:
        base = {
            "schema_version": 1,
            "name": "fs2-modelexpress-external-test",
            "target": self.catalog_target(),
            "profiles": {"models": "full_catalog"},
            "models": {
                "selection": "explicit",
                "enabled": ["qwen3-8b"],
                "scaling": {"mode": "keda"},
            },
            "dynamic_models": {
                "enabled": True,
                "writes_enabled": True,
                "workload_owner": "controller",
                "bootstrap_model_ids": ["qwen3-8b"],
                "fresh_install": True,
            },
        }
        invalid_configs = (
            {
                "endpoint": "modelexpress.example.test:99999",
                "external_network": {"coordinator_cidrs": ["192.0.2.10/32"]},
            },
            {"endpoint": "modelexpress.example.test:8001"},
            {
                "endpoint": "modelexpress.example.test:8001",
                "external_network": {"coordinator_cidrs": ["192.0.2.1/24"]},
            },
            {
                "endpoint": "modelexpress.example.test:8001",
                "external_network": {
                    "coordinator_namespace": "Invalid_Namespace",
                    "coordinator_pod_labels": {"app": "modelexpress"},
                },
            },
            {
                "endpoint": "modelexpress.example.test:8001",
                "external_network": {"coordinator_cidrs": ["192.0.2.10/32"]},
                "models": {
                    "qwen3-8b": {
                        "runtime_adapter": "vllm",
                        "transport": {"nic_pin": "invalid pin"},
                    }
                },
            },
        )
        for index, config in enumerate(invalid_configs):
            with self.subTest(config=config):
                deployment = json.loads(json.dumps(base))
                deployment["acceleration"] = {
                    "model_express": {
                        "enabled": True,
                        "deployment_mode": "external",
                        "metadata_backend": "redis",
                        "models": {"qwen3-8b": {"runtime_adapter": "vllm"}},
                        **config,
                    }
                }
                variable_file = self._write_configuration(
                    f"modelexpress-external-invalid-{index}", deployment
                )
                result, _ = self._plan_file(
                    variable_file, f"modelexpress-external-invalid-{index}"
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "Kubernetes namespace/Pod selector or CIDR route",
                    f"{result.stdout}\n{result.stderr}",
                )

    def test_fast_start_inputs_propagate_to_the_workload_stage(self) -> None:
        evidence_file = self.run_root / "fast-start-evidence.json"
        evidence_file.write_text("{}\n", encoding="utf-8")
        deployment = {
            "schema_version": 1,
            "name": "fs2-fast-start-input-test",
            "target": self.catalog_target(),
            "profiles": {"models": "full_catalog"},
            "models": {
                "selection": "explicit",
                "enabled": ["qwen3-8b"],
                "scaling": {"mode": "keda", "hot": ["qwen3-8b"]},
            },
            "dynamic_models": {
                "enabled": True,
                "writes_enabled": True,
                "workload_owner": "controller",
                "bootstrap_model_ids": ["qwen3-8b"],
                "fresh_install": True,
                "fast_start_evidence_file": str(evidence_file),
                "fast_start_wait_second_value": 0.025,
                "fast_start_mechanism_hourly_costs": {
                    "shared-cache": 0.1,
                    "ram-resident": 1.25,
                },
            },
        }

        outputs = self._planned_outputs(
            self._write_configuration("fast-start-inputs", deployment),
            "fast-start-inputs",
        )
        dynamic = outputs["deployment_contract"]["stages"]["workloads"][
            "model_controller"
        ]

        self.assertEqual(dynamic["fast_start_evidence_file"], str(evidence_file))
        self.assertEqual(dynamic["fast_start_wait_second_value"], 0.025)
        self.assertEqual(
            dynamic["fast_start_mechanism_hourly_costs"],
            {"shared-cache": 0.1, "ram-resident": 1.25},
        )

    def test_dynamic_model_ownership_rejects_a_concurrent_writer(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-invalid-owner-test",
            "target": self.catalog_target(),
            "models": {"selection": "profile", "scaling": {"mode": "keda"}},
            "dynamic_models": {
                "enabled": True,
                "writes_enabled": True,
                "workload_owner": "terraform",
            },
        }
        variable_file = self._write_configuration("invalid-owner", deployment)
        result, _ = self._plan_file(variable_file, "invalid-owner")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dynamic_models must use one exclusive ownership mode", result.stderr)

    def test_dynamic_model_workload_contract_is_derived_and_single_writer(self) -> None:
        controller_source = (
            DEPLOY_ROOT / "stages/workloads/model_controller.tf"
        ).read_text(encoding="utf-8")
        workload_locals = (DEPLOY_ROOT / "stages/workloads/locals.tf").read_text(
            encoding="utf-8"
        )
        models_source = (DEPLOY_ROOT / "stages/workloads/models.tf").read_text(
            encoding="utf-8"
        )
        root_variables = (DEPLOY_ROOT / "variables.tf").read_text(encoding="utf-8")

        self.assertNotIn("infrastructure_envelope_json", root_variables)
        self.assertNotIn("renderer_bundles_json", root_variables)
        self.assertIn("model_controller_pool_envelope", controller_source)
        self.assertIn("model_controller_qualifications", controller_source)
        self.assertIn("model_controller_bundle_resources", controller_source)
        self.assertIn("model_controller_expected_handoff_receipt", controller_source)
        controller_owned_gvks = controller_source.split(
            "model_controller_supported_template_gvks = toset([", 1
        )[1].split("])" , 1)[0]
        self.assertNotIn('"v1/PersistentVolumeClaim"', controller_owned_gvks)
        self.assertIn(
            'document.manifest.kind == "PersistentVolumeClaim"', controller_source
        )
        self.assertIn('cache.artifact.state == "platform-verified"', controller_source)
        self.assertIn('support.state == "qualified"', controller_source)
        self.assertIn('binding.state == "hardware-validated"', controller_source)
        self.assertIn("model_controller_qualification_rows", controller_source)
        self.assertIn("model_controller_ineligible_reasons", controller_source)
        self.assertIn("artifactRevisions", controller_source)
        self.assertIn("scaleToZeroQualified", controller_source)
        # Fast-start levels need explicit benchmark evidence; the envelope must
        # never derive them from activation-based elasticity timings.
        self.assertIn(
            "fastStartEvidence = try(local.model_controller_fast_start_evidence[model_id], [])",
            controller_source,
        )
        self.assertIn("model_controller_fast_start_evidence_valid", controller_source)
        self.assertIn('"compatibilityTupleDigest"', controller_source)
        self.assertIn('"compatibilityTupleComplete"', controller_source)
        self.assertNotIn("sha256(jsonencode({ source = model.model.source", controller_source)
        self.assertIn(
            "!contains(local.model_controller_dynamic_model_ids, model_id)",
            workload_locals,
        )
        self.assertIn(
            "sum(max by (model, state) (fs2_serve_operations",
            workload_locals,
        )
        self.assertNotIn("fallback = {", models_source)
        self.assertNotIn("fallback_failure_threshold", root_variables)
        self.assertIn(
            'implementation_sha256 = filesha256("${path.module}/model_controller.tf")',
            controller_source,
        )
        self.assertIn(
            '!contains(local.model_controller_dynamic_model_ids, document.model_id)',
            controller_source,
        )
        self.assertIn(
            "for_each = local.terraform_owned_model_manifests", models_source
        )
        self.assertIn(
            "for_each = local.terraform_owned_model_scalers", models_source
        )
        self.assertIn(
            '"/admin/api/v1/model-deployments:plan-preview"', controller_source
        )
        self.assertIn(
            '"/admin/api/v1/model-deployments:apply"', controller_source
        )
        self.assertIn(
            "public_authority = urllib.parse.urlsplit(public_origin).netloc",
            controller_source,
        )
        self.assertIn('"Host": public_authority', controller_source)
        self.assertIn('"Origin": public_origin', controller_source)
        self.assertIn('name  = "FS2_BOOTSTRAP_PUBLIC_ORIGIN"', controller_source)
        self.assertNotIn('kind = "ModelDeployment"', controller_source)

    def test_model_cache_is_shared_rwx_without_changing_the_default_storage_class(
        self,
    ) -> None:
        infrastructure = (
            DEPLOY_ROOT / "stages/infrastructure/cluster.tf"
        ).read_text(encoding="utf-8")
        foundation = (DEPLOY_ROOT / "stages/foundation/releases.tf").read_text(
            encoding="utf-8"
        )
        workload_locals = (DEPLOY_ROOT / "stages/workloads/locals.tf").read_text(
            encoding="utf-8"
        )
        controller = (
            DEPLOY_ROOT / "stages/workloads/model_controller.tf"
        ).read_text(encoding="utf-8")

        self.assertIn('shared_cache_mount_path', infrastructure)
        self.assertIn('"storage.fs2.nebius/shared-cache" = "true"', infrastructure)
        self.assertIn(
            "each.value.features.shared_filesystem ? local.shared_cache_cloud_init_user_data : null",
            infrastructure,
        )
        self.assertIn('name             = "csi-mounted-fs-path"', foundation)
        self.assertIn(
            'repository       = "oci://cr.eu-north1.nebius.cloud/mk8s/helm"',
            foundation,
        )
        self.assertIn('key      = "storage.fs2.nebius/shared-cache"', foundation)
        self.assertNotIn("is-default-class", foundation)
        self.assertIn('accessModes      = ["ReadWriteMany"]', workload_locals)
        self.assertIn(
            'storageClassName = "csi-mounted-fs-path-sc"', workload_locals
        )
        self.assertIn("shared_cache_claim_names", workload_locals)
        self.assertIn(
            "claimName = local.shared_cache_claim_names[volume.persistentVolumeClaim.claimName]",
            workload_locals,
        )
        self.assertIn(
            "model_controller_bundle_requires_shared_cache", controller
        )
        self.assertIn(
            "!local.model_controller_bundle_requires_shared_cache[model_id] || pool.features.shared_filesystem",
            controller,
        )

    def test_reference_data_plane_is_root_configured_sized_and_cpu_only(self) -> None:
        image = f"cr.eu-north1.nebius.cloud/test/reference-stager@sha256:{'a' * 64}"
        deployment = {
            "schema_version": 1,
            "name": "fs2-reference-data-test",
            "target": self.catalog_target(),
            "storage": {
                "reference_data": {
                    "enabled": True,
                    "filesystem": {"size_gib": 2048, "forbid_deletion": True},
                    "object_storage": {"max_size_gib": 2048},
                    "network": {
                        "allow_public_source_staging": True,
                        "allow_public_msa_opt_in": False,
                    },
                    "status": {"enabled": True, "image": image},
                    "pipeline": {"enabled": True, "image": image},
                }
            },
        }
        variable_file = self._write_configuration("reference-data", deployment)
        outputs = self._planned_outputs(variable_file, "reference-data")
        contract = outputs["deployment_contract"]
        infrastructure = contract["stages"]["infrastructure"]["reference_data"]
        workloads = contract["stages"]["workloads"]["reference_data"]

        self.assertTrue(infrastructure["enabled"])
        self.assertEqual("fs2-reference-data", workloads["namespace"])
        self.assertEqual("8vcpu-32gb", infrastructure["cpu_pool"]["preset"])
        self.assertEqual(1, infrastructure["cpu_pool"]["node_count"])
        self.assertEqual("6", workloads["queue"]["nominal_cpu"])
        self.assertEqual("24Gi", workloads["queue"]["nominal_memory"])
        self.assertEqual(2048, infrastructure["filesystem"]["size_gib"])
        self.assertEqual(2048, infrastructure["object_storage"]["max_size_gib"])
        self.assertRegex(
            infrastructure["object_storage"]["bucket_name"],
            r"^fs2-reference-data-test-r[0-9a-f]{10}-reference-data$",
        )
        self.assertTrue(workloads["pipeline"]["enabled"])
        self.assertEqual("alphafold3-public-databases-v3.0", workloads["pipeline"]["bundle_id"])
        self.assertFalse(workloads["network"]["allow_public_msa_opt_in"])
        self.assertEqual(
            2048, outputs["effective_configuration"]["reference_data"]["filesystem_size_gib"]
        )

        infrastructure_source = (
            DEPLOY_ROOT / "stages/infrastructure/storage.tf"
        ).read_text(encoding="utf-8")
        cluster_source = (
            DEPLOY_ROOT / "stages/infrastructure/cluster.tf"
        ).read_text(encoding="utf-8")
        pipeline_source = (
            DEPLOY_ROOT / "reference-data/terraform/main.tf"
        ).read_text(encoding="utf-8")
        self.assertIn('versioning_policy     = "ENABLED"', infrastructure_source)
        self.assertIn('forbid_deletion  = var.reference_data.filesystem.forbid_deletion', infrastructure_source)
        self.assertIn('mount_tag   = "fs2reference"', cluster_source)
        self.assertIn('resource "nebius_mk8s_v1_node_group" "reference_data"', cluster_source)
        self.assertIn('"workload.fs2.nebius/reference-data" = "true"', cluster_source)
        self.assertIn('effect = "NO_SCHEDULE"', cluster_source)
        self.assertNotIn('"nvidia.com/gpu"', pipeline_source)
        self.assertIn('suspend                 = true', pipeline_source)
        self.assertIn('"--object-store-prefix", local.object_prefix', pipeline_source)
        self.assertRegex(pipeline_source, r"nodeSelector\s*= var\.cpu_pool\.node_labels")
        self.assertIn('path = "/healthz"', pipeline_source)

    def test_reference_data_capacity_below_af3_plus_one_tib_is_rejected(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-reference-too-small",
            "target": self.catalog_target(),
            "storage": {
                "reference_data": {
                    "enabled": True,
                    "filesystem": {"size_gib": 1610},
                    "object_storage": {"max_size_gib": 2048},
                }
            },
        }
        variable_file = self._write_configuration("reference-too-small", deployment)
        result, _ = self._plan_file(variable_file, "reference-too-small")
        self.assertNotEqual(0, result.returncode)
        self.assertRegex(f"{result.stdout}\n{result.stderr}", r"at least 1611\s+GiB")

    def test_regional_mirror_rejects_tag_only_model_override(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-tag-only-rejected",
            "target": self.catalog_target(),
            "models": {
                "selection": "explicit",
                "enabled": ["proteinmpnn"],
                "image_overrides": {
                    "proteinmpnn": "nvcr.io/nim/ipd/proteinmpnn:latest"
                },
            },
        }
        variable_file = self._write_configuration("tag-only", deployment)
        result, _ = self._plan_file(variable_file, "tag-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "regional-mirror requires every models.image_overrides value",
            f"{result.stdout}\n{result.stderr}",
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
        storage = contract["scale_from_zero_storage"]
        self.assertEqual(storage["model_effective_request_gib"]["glm-5-2-fp8"], 768)
        budgets = storage["pool_synthetic_storage_budget_gib"]
        self.assertEqual(
            budgets["nebius-b300-preemptible-8x"],
            1606,
        )

    def test_scale_from_zero_rejects_boot_disk_that_cannot_fit_glm(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-glm-storage-template",
            "profiles": {
                "capacity": "full_catalog",
                "models": "full_catalog",
            },
            "target": self.catalog_target(),
            "accelerator_pools": {
                "b300-8x-local": {
                    "platform": "gpu-b300-sxm",
                    "preset": "8gpu-192vcpu-2768gb",
                    "accelerator_class": "nvidia-b300-sxm6-288gb",
                    "gpus_per_node": 8,
                    "gpu_memory_gb": 288,
                    "capacity_type": "preemptible",
                    "min_nodes": 0,
                    "max_nodes": 1,
                    "driver": {"mode": "managed", "preset": "cuda13.0"},
                    "boot_disk": {"type": "NETWORK_SSD", "size_gib": 320},
                    "local_nvme": True,
                    "local_nvme_mode": "kubelet-ephemeral",
                }
            },
            "models": {
                "selection": "explicit",
                "enabled": ["glm-5-2-fp8"],
                "pool_overrides": {"glm-5-2-fp8": "b300-8x-local"},
                "scaling": {"mode": "static"},
            },
            "edge": {"mode": "internal-only"},
        }
        variable_file = self._write_configuration("glm-small-boot", deployment)
        result, _ = self._plan_file(variable_file, "glm-small-boot")

        self.assertNotEqual(result.returncode, 0)
        diagnostics = f"{result.stdout}\n{result.stderr}"
        self.assertIn("cannot trigger its zero-node pool", diagnostics)
        self.assertIn("glm-5-2-fp8 requires 768.000 GiB", diagnostics)
        self.assertIn("only 224 GiB", diagnostics)

        deployment["accelerator_pools"]["b300-8x-local"]["boot_disk"][
            "size_gib"
        ] = 2048
        variable_file = self._write_configuration("glm-large-boot", deployment)
        contract = self._planned_outputs(variable_file, "glm-large-boot")[
            "deployment_contract"
        ]
        budgets = contract["scale_from_zero_storage"][
            "pool_synthetic_storage_budget_gib"
        ]
        self.assertEqual(
            budgets["b300-8x-local"],
            1606,
        )

    def test_catalog_ephemeral_requests_match_selected_deployments(self) -> None:
        targets = self.model_contract["model_autoscaling_targets"]
        for model_id, target in targets.items():
            deployments: list[dict[str, Any]] = []
            for relative_path in self.model_contract["model_artifacts"][model_id][
                "manifest_paths"
            ]:
                deployments.extend(
                    document
                    for document in yaml.safe_load_all(
                        (DEPLOY_ROOT / relative_path).read_text(encoding="utf-8")
                    )
                    if document is not None
                    and document.get("kind") == "Deployment"
                    and document["metadata"]["name"] == target["deployment"]
                )
            with self.subTest(model=model_id):
                self.assertEqual(len(deployments), 1)
                actual = pod_ephemeral_request_gib(
                    deployments[0]["spec"]["template"]["spec"]
                )
                self.assertAlmostEqual(
                    actual,
                    target["ephemeral_storage_request_gib"],
                    places=9,
                )

    def test_full_catalog_surfaces_have_exact_model_set_coverage(self) -> None:
        canonical = set(
            self.model_profiles["full_catalog"]["canonical_routes"]
        )
        self.assertEqual(canonical, set(self.model_contract["model_artifacts"]))
        self.assertEqual(
            canonical,
            set(self.model_contract["model_autoscaling_targets"]),
        )
        self.assertEqual(
            canonical,
            {
                placement["model_id"]
                for placement in self.model_contract["workload_placements"].values()
            },
        )
        runtime_catalog = json.loads(
            (DEPLOY_ROOT / "catalog" / "runtime" / "catalog.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(canonical, set(runtime_catalog["tested_model_ids"]))
        accelerator_compatibility = json.loads(
            (
                DEPLOY_ROOT
                / "catalog"
                / "profiles"
                / "model-accelerator-compatibility.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(canonical, set(accelerator_compatibility["models"]))

    def test_cosmos_manifest_is_gpu_agnostic_and_exact_image_rewrite_is_model_scoped(
        self,
    ) -> None:
        model = json.loads(
            (
                DEPLOY_ROOT
                / "catalog"
                / "runtime"
                / "models"
                / "cosmos3-nano.json"
            ).read_text(encoding="utf-8")
        )
        manifest = next(
            document
            for document in yaml.safe_load_all(
                (
                    DEPLOY_ROOT
                    / "models"
                    / "general-media"
                    / "k8s"
                    / "cosmos3-nano.yaml"
                ).read_text(encoding="utf-8")
            )
            if document is not None and document.get("kind") == "Deployment"
        )
        pod_spec = manifest["spec"]["template"]["spec"]
        self.assertNotIn("nodeSelector", pod_spec)
        exact_image = model["runtime"]["image"]["reference"]
        self.assertEqual(
            {container["image"] for container in pod_spec["containers"]},
            {exact_image},
        )

        mirror = "cr.eu-north1.nebius.cloud/registry/fs2-models/vllm-omni@sha256:" + "1" * 64

        def rewrite(model_id: str, image: str) -> str:
            runtime_images = {
                "cosmos3-nano": exact_image,
                "other-model": "example.invalid/other@sha256:" + "2" * 64,
            }
            overrides = {"cosmos3-nano": mirror}
            is_runtime_image = (
                image == runtime_images[model_id]
                or image.startswith(
                    "registry.example.invalid/k8s-inference/models/"
                )
            )
            return (
                overrides[model_id]
                if model_id in overrides and is_runtime_image
                else image
            )

        self.assertEqual(rewrite("cosmos3-nano", exact_image), mirror)
        adapter = "registry.example.invalid/k8s-inference/sidecars/adapter@sha256:" + "3" * 64
        self.assertEqual(rewrite("cosmos3-nano", adapter), adapter)
        self.assertEqual(rewrite("other-model", exact_image), exact_image)

        source = (DEPLOY_ROOT / "stages" / "workloads" / "locals.tf").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(
            source.count(
                'try(container.image, "") == local.catalog_model_runtime_images[document.model_id]'
            ),
            2,
        )
        self.assertGreaterEqual(
            source.count(
                '"registry.example.invalid/k8s-inference/models/"'
            ),
            2,
        )
        self.assertNotIn("regexreplace(container.image", source)

        placement = next(
            item
            for item in self.model_contract["workload_placements"].values()
            if item["model_id"] == "cosmos3-nano"
        )
        self.assertEqual(
            placement["required_node_labels"],
            {"accelerator.fs2.nebius/class": "nvidia-b300-sxm6-288gb"},
        )
        self.assertEqual(
            set(placement["compatible_pool_ids"]),
            {"nebius-b300-preemptible-1x", "nebius-b300-preemptible-8x"},
        )
        self.assertIn("document.placement.required_node_labels", source)
        self.assertIn("contains(keys(var.model_pool_overrides), document.model_id)", source)

    def test_full_catalog_runtime_images_rewrite_without_sidecar_overreach(
        self,
    ) -> None:
        catalog = json.loads(
            (DEPLOY_ROOT / "catalog" / "runtime" / "catalog.json").read_text(
                encoding="utf-8"
            )
        )
        runtime_images = {}
        for model_file in catalog["model_files"]:
            model = json.loads(
                (
                    DEPLOY_ROOT
                    / "catalog"
                    / "runtime"
                    / "models"
                    / model_file
                ).read_text(encoding="utf-8")
            )
            runtime_images[model["model"]["id"]] = model["runtime"]["image"][
                "reference"
            ]

        inventory = json.loads(
            (
                DEPLOY_ROOT
                / "components"
                / "control-plane"
                / "contracts"
                / "all-models-live-services.json"
            ).read_text(encoding="utf-8")
        )
        overrides = {
            model_id: "mirror.invalid/fs2-models/"
            + model_id
            + "@"
            + route["runtime_image_digest"]
            for model_id, route in inventory["routes"].items()
        }
        reserved_prefix = "registry.example.invalid/k8s-inference/models/"

        def rewrite(model_id: str, image: str) -> str:
            if image == runtime_images[model_id] or image.startswith(reserved_prefix):
                return overrides[model_id]
            return image

        source_models = {
            path: [
                model_id
                for model_id, artifact in self.model_contract[
                    "model_artifacts"
                ].items()
                if path in artifact["manifest_paths"]
            ]
            for path in self.model_profiles["full_catalog"]["manifest_paths"]
        }
        reserved_placeholders = 0
        rewritten_images = []
        for relative_path in self.model_profiles["full_catalog"]["manifest_paths"]:
            for document in yaml.safe_load_all(
                (DEPLOY_ROOT / relative_path).read_text(encoding="utf-8")
            ):
                if document is None or document.get("kind") != "Deployment":
                    continue
                labels = document["metadata"].get("labels", {})
                model_id = labels.get("fs2-serve.nebius.ai/model-id")
                if model_id is None:
                    model_id = labels.get("fs2.nebius.ai/model-id")
                if (
                    model_id is None
                    and labels.get("app.kubernetes.io/name")
                    in self.model_profiles["full_catalog"]["canonical_routes"]
                ):
                    model_id = labels["app.kubernetes.io/name"]
                if model_id is None and len(source_models[relative_path]) == 1:
                    model_id = source_models[relative_path][0]
                self.assertIn(model_id, runtime_images, document["metadata"]["name"])
                pod_spec = document["spec"]["template"]["spec"]
                for container in pod_spec.get("containers", []) + pod_spec.get(
                    "initContainers", []
                ):
                    image = container["image"]
                    rendered = rewrite(model_id, image)
                    rewritten_images.append(rendered)
                    if image.startswith(reserved_prefix):
                        reserved_placeholders += 1
                        self.assertEqual(rendered, overrides[model_id])
                    elif image == runtime_images[model_id]:
                        self.assertEqual(rendered, overrides[model_id])
                    else:
                        self.assertEqual(rendered, image)

        self.assertGreater(reserved_placeholders, 0)
        self.assertFalse(
            any(image.startswith(reserved_prefix) for image in rewritten_images)
        )
        unrelated_sidecar = (
            "registry.example.invalid/k8s-inference/sidecars/metrics@sha256:"
            + "4" * 64
        )
        self.assertEqual(
            rewrite("cosmos3-nano", unrelated_sidecar),
            unrelated_sidecar,
        )

    def test_cosmos_uses_default_placement_or_an_explicit_preemptible_h100_pool(
        self,
    ) -> None:
        default_deployment = {
            "schema_version": 1,
            "name": "cosmos-default-placement",
            "profiles": {"models": "full_catalog"},
            "target": self.catalog_target(),
            "models": {
                "selection": "explicit",
                "enabled": ["cosmos3-nano"],
                "scaling": {"mode": "keda", "hot": []},
            },
            "edge": {"mode": "internal-only"},
        }
        default_contract = self._planned_outputs(
            self._write_configuration("cosmos-default", default_deployment),
            "cosmos-default",
        )["deployment_contract"]
        self.assertEqual(
            default_contract["stages"]["workloads"]["model_pool_overrides"],
            {},
        )
        self.assertEqual(
            set(default_contract["selected_accelerator_pool_ids"]),
            {"nebius-b300-preemptible-1x", "nebius-b300-preemptible-8x"},
        )

        h100_deployment = {
            **default_deployment,
            "name": "cosmos-h100-placement",
            "target": {**default_deployment["target"], "region": "eu-north1"},
            "accelerator_pools": {
                "h100-preemptible-1x": {
                    "platform": "gpu-h100-sxm",
                    "preset": "1gpu-16vcpu-200gb",
                    "accelerator_class": "nvidia-h100-sxm5-80gb",
                    "gpus_per_node": 1,
                    "gpu_memory_gb": 80,
                    "capacity_type": "preemptible",
                    "min_nodes": 0,
                    "max_nodes": 1,
                    "driver": {"mode": "managed", "preset": "cuda13.0"},
                    "local_nvme": False,
                }
            },
            "models": {
                **default_deployment["models"],
                "enabled": ["cosmos3-nano", "qwen3-8b"],
                "pool_overrides": {
                    "cosmos3-nano": "h100-preemptible-1x",
                    "qwen3-8b": "h100-preemptible-1x",
                },
            },
        }
        h100_contract = self._planned_outputs(
            self._write_configuration("cosmos-h100", h100_deployment),
            "cosmos-h100",
        )["deployment_contract"]
        self.assertEqual(
            h100_contract["selected_accelerator_pool_ids"],
            ["h100-preemptible-1x"],
        )
        self.assertEqual(
            h100_contract["stages"]["workloads"]["model_pool_overrides"],
            {
                "cosmos3-nano": "h100-preemptible-1x",
                "qwen3-8b": "h100-preemptible-1x",
            },
        )
        self.assertEqual(
            h100_contract["target"],
            {"project_id": TEST_PROJECT_ID, "region": "eu-north1"},
        )
        self.assertEqual(
            h100_contract["secret_requirements"],
            {
                "grafana_bootstrap": True,
                "ngc_api_key": False,
                # This is the one-time full-catalog DCGM exporter credential,
                # not a requirement introduced by Cosmos or Qwen.
                "nvcr_dockerconfig": True,
            },
        )

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

    def test_runtime_lean_routes_carry_the_exact_v4_placement_contract(self) -> None:
        locals_source = (DEPLOY_ROOT / "stages" / "workloads" / "locals.tf").read_text(
            encoding="utf-8"
        )
        catalog_source = (DEPLOY_ROOT / "stages" / "workloads" / "catalog.tf").read_text(
            encoding="utf-8"
        )
        control_plane_source = (
            DEPLOY_ROOT / "stages" / "workloads" / "control_plane.tf"
        ).read_text(encoding="utf-8")

        lean_routes = locals_source.split("  lean_routes = {", maxsplit=1)[1].split(
            "\n  }", maxsplit=1
        )[0]
        self.assertIn('schema = "fs2-serve.nebius.ai/lean-routes/v4"', lean_routes)
        self.assertIn("routes = [", lean_routes)
        self.assertIn("region            = local.selected_target.region", lean_routes)
        self.assertIn(
            'accelerator_class = local.effective_model_placements[model_id].required_node_labels["accelerator.fs2.nebius/class"]',
            lean_routes,
        )
        self.assertIn(
            'pool_id           = try(local.effective_model_placements[model_id].required_node_labels["accelerator.fs2.nebius/pool-id"], null)',
            lean_routes,
        )
        self.assertNotIn("qualification", lean_routes)
        self.assertNotIn("lean-routes/v3", lean_routes)
        self.assertIn(
            '"qualification-projection.json" = jsonencode(local.qualification_projection)',
            locals_source,
        )
        self.assertIn("data = local.lean_routes_config_map_data", catalog_source)
        self.assertIn(
            "configMapName = kubernetes_config_map_v1.lean_routes.metadata[0].name",
            control_plane_source,
        )

    def test_h100_cosmos_and_qwen_open_the_exact_distinct_runtime_ports(self) -> None:
        inventory = json.loads(
            (
                DEPLOY_ROOT
                / "components/control-plane/contracts/all-models-live-services.json"
            ).read_text(encoding="utf-8")
        )
        selected = ("cosmos3-nano", "qwen3-8b")
        self.assertEqual(
            sorted({inventory["routes"][model_id]["service"]["port"] for model_id in selected}),
            [8000, 8080],
        )

        locals_source = (DEPLOY_ROOT / "stages/workloads/locals.tf").read_text(
            encoding="utf-8"
        )
        control_plane_source = (
            DEPLOY_ROOT / "stages/workloads/control_plane.tf"
        ).read_text(encoding="utf-8")
        catalog_source = (DEPLOY_ROOT / "stages/workloads/catalog.tf").read_text(
            encoding="utf-8"
        )
        self.assertIn("selected_runtime_ports = [", locals_source)
        self.assertIn("format(\"%05d\", local.selected_routes[model_id].service.port)", locals_source)
        self.assertIn("ports = local.selected_runtime_ports", control_plane_source)
        self.assertIn(
            'nodeScalerProvider = local.admin_configuration_enabled ? "nebius-managed-node-group-autoscaler" : ""',
            control_plane_source,
        )
        self.assertIn("port >= 1 && port <= 65535", catalog_source)

    def test_loki_is_scraped_and_publishes_its_grafana_dashboards(self) -> None:
        values = yaml.safe_load(
            (DEPLOY_ROOT / "stages/foundation/values/loki.yaml").read_text(encoding="utf-8")
        )
        self.assertIs(values["monitoring"]["serviceMonitor"]["enabled"], True)
        self.assertIs(values["monitoring"]["dashboards"]["enabled"], True)

    def test_lean_route_config_map_name_covers_its_complete_data_map(self) -> None:
        locals_source = (DEPLOY_ROOT / "stages" / "workloads" / "locals.tf").read_text(
            encoding="utf-8"
        )
        catalog_source = (DEPLOY_ROOT / "stages" / "workloads" / "catalog.tf").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "lean_routes_config_map_digest = sha256(jsonencode(local.lean_routes_config_map_data))",
            locals_source,
        )
        self.assertIn(
            'lean_routes_config_map_name   = "fs2-serve-lean-routes-terraform-${substr(local.lean_routes_config_map_digest, 0, 12)}"',
            locals_source,
        )
        self.assertIn(
            "name      = local.lean_routes_config_map_name",
            catalog_source,
        )
        self.assertIn(
            "data = local.lean_routes_config_map_data",
            catalog_source,
        )
        self.assertIn(
            "lifecycle {\n    create_before_destroy = true",
            catalog_source,
        )
        self.assertIn(
            "Selected model routes must resolve to a nonempty bounded set of distinct runtime ports.",
            catalog_source,
        )

    def test_all_catalog_dependent_immutable_config_maps_are_content_addressed(self) -> None:
        locals_source = (DEPLOY_ROOT / "stages" / "workloads" / "locals.tf").read_text(
            encoding="utf-8"
        )
        catalog_source = (DEPLOY_ROOT / "stages" / "workloads" / "catalog.tf").read_text(
            encoding="utf-8"
        )

        for prefix in ("serving_bindings", "platform_contract"):
            self.assertIn(
                f"{prefix}_config_map_digest = sha256(jsonencode(local.{prefix}_config_map_data))",
                locals_source,
            )
            self.assertIn(
                f"name      = local.{prefix}_config_map_name",
                catalog_source,
            )

        self.assertNotIn(
            'name      = "fs2-serve-serving-bindings-terraform"',
            catalog_source,
        )
        self.assertNotIn(
            'name      = "fs2-terraform-workloads-contract"',
            catalog_source,
        )
        self.assertEqual(catalog_source.count("create_before_destroy = true"), 3)

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

    def test_capacity_block_pool_preserves_the_provider_reservation_shape(
        self,
    ) -> None:
        reserved_pool = {
            "platform": "gpu-h100-sxm",
            "preset": "8gpu-128vcpu-1600gb",
            "accelerator_class": "nvidia-h100-sxm5-80gb",
            "gpus_per_node": 8,
            "capacity_type": "regular",
            "min_nodes": 2,
            "max_nodes": 2,
            "reservation_policy": {
                "policy": "STRICT",
                "reservation_ids": ["capacityblockgroup-testreservation"],
            },
            "driver": {"mode": "managed", "preset": "cuda13.0"},
        }
        deployment = {
            "schema_version": 1,
            "name": "fs2-capacity-block-test",
            "profiles": {"capacity": "minimal", "models": "none"},
            "target": self.catalog_target(),
            "accelerator_pools": {"h100-reserved-8x": reserved_pool},
            "models": {"selection": "profile"},
        }
        variable_file = self._write_configuration("capacity-block", deployment)
        outputs = self._planned_outputs(variable_file, "capacity-block")

        rendered = outputs["deployment_contract"]["stages"]["infrastructure"]
        self.assertEqual(
            rendered["custom_accelerator_pools"]["h100-reserved-8x"],
            {
                **reserved_pool,
                "boot_disk": {"size_gib": 320, "type": "NETWORK_SSD"},
                "drain_timeout": "30m",
                "gpu_memory_gb": None,
                "host_architecture": "amd64",
                "local_nvme": False,
                "local_nvme_mode": "raw",
                "mig": {"config": None, "strategy": "none"},
                "os": "ubuntu24.04",
                "resource_name": "nvidia.com/gpu",
                "shared_filesystem": True,
                "topology": {
                    "infiniband_fabric": None,
                    "mode": "standalone",
                    "nodes_per_rack": 18,
                    "rack_count": 0,
                },
            },
        )

    def test_capacity_block_rejects_preemptible_or_elastic_pool(self) -> None:
        base_pool = {
            "platform": "gpu-h100-sxm",
            "preset": "8gpu-128vcpu-1600gb",
            "accelerator_class": "nvidia-h100-sxm5-80gb",
            "gpus_per_node": 8,
            "capacity_type": "regular",
            "min_nodes": 2,
            "max_nodes": 2,
            "reservation_policy": {
                "policy": "STRICT",
                "reservation_ids": ["capacityblockgroup-testreservation"],
            },
            "driver": {"mode": "managed", "preset": "cuda13.0"},
        }
        invalid_pools = (
            {**base_pool, "capacity_type": "preemptible"},
            {**base_pool, "min_nodes": 0},
            {
                **base_pool,
                "reservation_policy": {
                    "policy": "FORBID",
                    "reservation_ids": ["capacityblockgroup-testreservation"],
                },
            },
        )
        for index, pool in enumerate(invalid_pools):
            with self.subTest(pool=pool):
                variable_file = self._write_configuration(
                    f"invalid-capacity-block-{index}",
                    {
                        "schema_version": 1,
                        "name": f"fs2-invalid-capacity-block-{index}",
                        "profiles": {"capacity": "minimal", "models": "none"},
                        "target": self.catalog_target(),
                        "accelerator_pools": {"h100-reserved-8x": pool},
                        "models": {"selection": "profile"},
                    },
                )
                result, _ = self._plan_file(
                    variable_file, f"invalid-capacity-block-{index}"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(
                    f"{result.stdout}\n{result.stderr}",
                    r"reservations require fixed regular\s+capacity",
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

    def test_kueue_quotas_only_accelerator_resources(self) -> None:
        releases = (DEPLOY_ROOT / "stages/foundation/releases.tf").read_text(
            encoding="utf-8"
        )
        values_path = DEPLOY_ROOT / "stages/foundation/values/kueue.yaml"
        values = yaml.safe_load(values_path.read_text(encoding="utf-8"))
        manager = yaml.safe_load(
            values["managerConfig"]["controllerManagerConfigYaml"]
        )

        self.assertIn('values = [file("${path.module}/values/kueue.yaml")]', releases)
        self.assertEqual(
            manager["resources"]["excludeResourcePrefixes"],
            ["cpu", "memory", "ephemeral-storage"],
        )
        self.assertIn("deployment", manager["integrations"]["frameworks"])


if __name__ == "__main__":
    unittest.main()
