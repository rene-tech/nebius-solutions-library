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
# Synthetic plan fixture for values below the catalog profiles' nominal B300
# sizes. Its fixture source names the exact UTF-8 bytes hashed below, so it is
# truthful test provenance rather than a fabricated kubectl measurement.
ACCELERATOR_CAPACITY_FIXTURE = {
    "nebius-b300-preemptible-1x": {
        "cpu_millicores": 22000,
        "memory_mib": 344064,
        "evidence": {
            "pool_id": "nebius-b300-preemptible-1x",
            "source": "fixture:utf8:nebius-b300-preemptible-1x",
            "captured_at": "2026-09-03T06:00:00Z",
            "payload_sha256": "85cae37a96eff77ba331fdb643f4ba282e3f4f945ec19297ab22dadef7157663",
        },
    },
    "nebius-b300-preemptible-8x": {
        "cpu_millicores": 188000,
        "memory_mib": 2801664,
        "evidence": {
            "pool_id": "nebius-b300-preemptible-8x",
            "source": "fixture:utf8:nebius-b300-preemptible-8x",
            "captured_at": "2026-09-03T06:00:00Z",
            "payload_sha256": "e86ec303bf8c775b8ce347e6d333f2418baf4f763bf67d97575e07fa233e1a4e",
        },
    },
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
    def _write_configuration(
        cls,
        name: str,
        deployment: dict[str, Any],
        **top_level: Any,
    ) -> Path:
        deployment = dict(deployment)
        deployment.setdefault("applications", TEST_APPLICATIONS)
        # core_capacity is bounded by measured schedulable capacity, never by
        # a preset's nominal size, so a profile-pool fixture that budgets core
        # resources states the measurement. A custom-pool fixture declares it
        # on the pool itself.
        scheduling = deployment.get("scheduling")
        if (
            isinstance(scheduling, dict)
            and scheduling.get("budget_core_resources") is True
            and "accelerator_schedulable_capacity" not in scheduling
            and not deployment.get("accelerator_pools")
        ):
            deployment["scheduling"] = {
                **scheduling,
                "accelerator_schedulable_capacity": ACCELERATOR_CAPACITY_FIXTURE,
            }
        path = cls.run_root / f"{name}.tfvars.json"
        path.write_text(
            json.dumps({"deployment": deployment, **top_level}, indent=2, sort_keys=True) + "\n",
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
                "fast_start_mechanisms_file": None,
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
        self.assertEqual(
            contract["stages"]["workloads"]["control_plane_autoscaling"],
            {
                "enabled": True,
                "min_replicas": 2,
                "max_replicas": 8,
                "target_cpu_utilization_percentage": 70,
            },
        )
        self.assertEqual(
            contract["stages"]["workloads"]["control_plane_rollout"],
            {"max_unavailable": 1, "max_surge": 0},
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

    def test_control_plane_hpa_envelope_is_a_tfvars_only_workload_contract(
        self,
    ) -> None:
        applications = json.loads(json.dumps(TEST_APPLICATIONS))
        applications["control_plane"]["autoscaling"] = {
            "enabled": True,
            "min_replicas": 2,
            "max_replicas": 3,
            "target_cpu_utilization_percentage": 75,
        }
        applications["control_plane"]["rollout"] = {
            "max_unavailable": 1,
            "max_surge": 0,
        }
        deployment = {
            "schema_version": 1,
            "name": "fs2-control-plane-hpa",
            "target": self.catalog_target(),
            "applications": applications,
        }
        outputs = self._planned_outputs(
            self._write_configuration("control-plane-hpa", deployment),
            "control-plane-hpa",
        )
        self.assertEqual(
            outputs["deployment_contract"]["stages"]["workloads"][
                "control_plane_autoscaling"
            ],
            applications["control_plane"]["autoscaling"],
        )
        self.assertEqual(
            outputs["deployment_contract"]["stages"]["workloads"][
                "control_plane_rollout"
            ],
            applications["control_plane"]["rollout"],
        )

        control_plane_source = (
            DEPLOY_ROOT / "stages/workloads/control_plane.tf"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "maxReplicas                    = var.control_plane_autoscaling.max_replicas",
            control_plane_source,
        )
        self.assertIn(
            "maxUnavailable = var.control_plane_rollout.max_unavailable",
            control_plane_source,
        )

    def test_system_pool_inotify_ceiling_is_a_bounded_tfvars_setting(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-system-inotify",
            "target": self.catalog_target(),
            "cluster": {
                "system_pool": {"inotify_max_user_instances": 16384},
            },
        }
        outputs = self._planned_outputs(
            self._write_configuration("system-inotify", deployment),
            "system-inotify",
        )
        self.assertEqual(
            outputs["deployment_contract"]["stages"]["infrastructure"][
                "system_pool"
            ]["inotify_max_user_instances"],
            16384,
        )

        deployment["cluster"]["system_pool"]["inotify_max_user_instances"] = 128
        result, _ = self._plan_file(
            self._write_configuration("system-inotify-too-low", deployment),
            "system-inotify-too-low",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cluster.system_pool", result.stderr)

        cluster_source = (
            DEPLOY_ROOT / "stages/infrastructure/cluster.tf"
        ).read_text(encoding="utf-8")
        system_resource, non_system_resources = cluster_source.split(
            'resource "nebius_mk8s_v1_node_group" "reference_data"', 1
        )
        system_resource = system_resource.split(
            'resource "nebius_mk8s_v1_node_group" "system"', 1
        )[1]
        self.assertIn(
            "local.system_shared_cache_cloud_init_user_data", system_resource
        )
        self.assertIn(
            "local.system_shared_cache_reference_data_cloud_init_user_data",
            system_resource,
        )
        self.assertNotIn("local.system_shared_cache", non_system_resources)

    def test_dynamic_priority_class_label_boundary_is_rejected_at_root(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-priority-label-boundary",
            "target": self.catalog_target(),
            "dynamic_models": {
                "priority_classes": {
                    "standard": 0,
                    "p" * 64: 1,
                }
            },
        }
        variable_file = self._write_configuration("priority-label-boundary", deployment)
        result, _ = self._plan_file(variable_file, "priority-label-boundary")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("at most 63 characters", result.stderr)

    def test_alertmanager_is_a_tfvars_only_foundation_contract(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-alertmanager-contract",
            "target": self.catalog_target(),
            "observability": {
                "grafana": {"publish_external": True},
                "alertmanager": {
                    "enabled": True,
                    "retention": "240h",
                    "storage": {
                        "storage_class_name": "compute-csi-default-sc",
                        "size_gib": 20,
                    },
                },
            },
            "edge": {
                "mode": "public",
                "source_cidrs": ["192.0.2.0/24"],
                "acme_email": "operator@example.invalid",
            },
        }
        variable_file = self._write_configuration("alertmanager-contract", deployment)
        outputs = self._planned_outputs(variable_file, "alertmanager-contract")

        expected = deployment["observability"]["alertmanager"]
        self.assertEqual(
            outputs["deployment_contract"]["stages"]["foundation"]["alertmanager"],
            expected,
        )
        self.assertEqual(
            outputs["effective_configuration"]["observability"]["alertmanager"],
            {
                "enabled": True,
                "retention": "240h",
                "storage_class_name": "compute-csi-default-sc",
                "storage_size_gib": 20,
            },
        )

    def test_invalid_alertmanager_storage_and_retention_are_rejected_at_root(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-alertmanager-invalid",
            "target": self.catalog_target(),
            "observability": {
                "alertmanager": {
                    "enabled": True,
                    "retention": "forever",
                    "storage": {
                        "storage_class_name": "INVALID_CLASS",
                        "size_gib": 0,
                    },
                }
            },
        }
        variable_file = self._write_configuration("alertmanager-invalid", deployment)
        result, _ = self._plan_file(variable_file, "alertmanager-invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, r"positive bounded Go\s+duration")

    def test_root_rejects_forbidden_kueue_flavor_preference_before_stages(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-invalid-flavor-preference",
            "target": self.catalog_target(),
            "scheduling": {
                "cluster_queues": {
                    "customer-batch": {
                        "flavor_fungibility": {
                            "when_can_borrow": "MayStopSearch",
                            "when_can_preempt": "TryNextFlavor",
                            "preference": "BorrowingOverPreemption",
                        }
                    }
                }
            },
        }
        variable_file = self._write_configuration("invalid-flavor-preference", deployment)
        result, _ = self._plan_file(variable_file, "invalid-flavor-preference")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scheduling must use", result.stderr)

    def test_root_rejects_kueue_fair_sharing_weight_at_webhook_floor(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-invalid-fair-sharing-weight",
            "target": self.catalog_target(),
            "scheduling": {
                "cohort": {"fair_sharing_weight": 0.000000001},
            },
        }
        variable_file = self._write_configuration("invalid-fair-sharing-weight", deployment)
        result, _ = self._plan_file(variable_file, "invalid-fair-sharing-weight")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("greater than 1e-9", result.stderr)

    def test_root_rejects_duplicate_admission_checks_and_flavors(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-duplicate-admission-checks",
            "target": self.catalog_target(),
            "scheduling": {
                "cluster_queues": {
                    "customer-batch": {
                        "admission_checks": [
                            {"name": "capacity", "on_flavors": ["one", "one"]},
                            {"name": "capacity", "on_flavors": []},
                        ]
                    }
                }
            },
        }
        variable_file = self._write_configuration("duplicate-admission-checks", deployment)
        result, _ = self._plan_file(variable_file, "duplicate-admission-checks")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scheduling must use", result.stderr)

    def test_root_rejects_effective_scheduling_invariants_before_stages(self) -> None:
        pool_profiles = json.loads(
            (PROFILES_ROOT / "accelerator-pool-profiles.json").read_text(encoding="utf-8")
        )
        minimal = pool_profiles["profiles"]["minimal"]
        pool_ids = list(minimal["pool_order"])
        stable_cluster_queue = minimal["queue"]["cluster_queue_name"]
        stable_local_queue = minimal["queue"]["local_queue_name"]

        service_classes = {
            name: {
                "workload_priority_class": priority_name,
                "priority": priority,
                "preemption_mode": "restartable",
            }
            for name, priority_name, priority in (
                ("platform-critical", "platform-critical", 10000),
                ("presentation", "presentation", 1000),
                ("interactive", "interactive", 100),
                ("customer-batch", "standard", 0),
                ("bulk-backfill", "batch", -100),
            )
        }
        valid_queue = {
            "namespace": "fs2-models",
            "flavor_order": pool_ids,
            "pool_quotas": {},
            "preemption": {
                "reclaim_within_cohort": "LowerPriority",
                "within_cluster_queue": "LowerPriority",
            },
        }
        cases: dict[str, tuple[dict[str, Any], str]] = {
            "duplicate-pool-order": (
                {"cluster_queues": {"customer": {**valid_queue, "flavor_order": [pool_ids[0], pool_ids[0]]}}},
                "pool orders must be exact",
            ),
            "foreign-quota-pool": (
                {"cluster_queues": {"customer": {**valid_queue, "pool_quotas": {"foreign": {"nominal_quota": 0}}}}},
                "quota and AdmissionCheck pool keys",
            ),
            "floor-above-capacity": (
                {"cluster_queues": {"customer": {**valid_queue, "pool_quotas": {pool_ids[0]: {"nominal_quota": 2}}}}},
                "summed floors cannot exceed",
            ),
            "stable-localqueue-rebind": (
                {
                    "cluster_queues": {"customer": valid_queue},
                    "local_queues": {
                        stable_local_queue: {
                            "cluster_queue": "customer",
                        }
                    },
                },
                "preserve the stable",
            ),
            "foreign-localqueue-namespace": (
                {
                    "local_queues": {
                        "foreign": {
                            "namespace": "other-models",
                            "cluster_queue": stable_cluster_queue,
                        }
                    }
                },
                "namespace their ClusterQueue admits",
            ),
            "invalid-model-label": (
                {
                    "local_queues": {
                        "invalid-model": {
                            "cluster_queue": stable_cluster_queue,
                            "model_ids": ["a.b"],
                            "service_classes": ["customer-batch"],
                        }
                    }
                },
                "strict DNS-label model IDs",
            ),
            "ambiguous-route": (
                {
                    "local_queues": {
                        name: {
                            "cluster_queue": stable_cluster_queue,
                            "model_ids": ["proteinmpnn"],
                            "service_classes": ["customer-batch"],
                        }
                        for name in ("lane-a", "lane-b")
                    }
                },
                "unambiguous",
            ),
            "high-route-without-displacement": (
                {
                    "cluster_queues": {
                        "unprotected": {
                            **valid_queue,
                            "preemption": {
                                "reclaim_within_cohort": "Never",
                                "within_cluster_queue": "Never",
                            },
                        }
                    },
                    "local_queues": {
                        "presentation-lane": {
                            "cluster_queue": "unprotected",
                            "model_ids": ["proteinmpnn"],
                            "service_classes": ["presentation"],
                        }
                    },
                },
                "high-priority routes require both",
            ),
            "missing-service-default": (
                {
                    "service_classes": {
                        **service_classes,
                        "customer-batch": {
                            **service_classes["customer-batch"],
                            "default_local_queue": "missing",
                        },
                    }
                },
                "existing LocalQueue",
            ),
            "shared-priority-conflict": (
                {
                    "service_classes": {
                        **service_classes,
                        "customer-batch": {
                            **service_classes["customer-batch"],
                            "priority": 1,
                        },
                    }
                },
                "shared WorkloadPriorityClass",
            ),
        }
        for index, (name, (scheduling, message)) in enumerate(cases.items()):
            with self.subTest(name=name):
                deployment = {
                    "schema_version": 1,
                    "name": f"fs2-root-scheduling-{index}",
                    "target": self.catalog_target(),
                    "scheduling": scheduling,
                }
                variable_file = self._write_configuration(f"root-scheduling-{name}", deployment)
                result, _ = self._plan_file(variable_file, f"root-scheduling-{name}")
                self.assertNotEqual(result.returncode, 0)
                diagnostics = re.sub(r"\s+", " ", f"{result.stdout}\n{result.stderr}")
                self.assertIn(message, diagnostics)

    def _modelexpress_deployment(self, name: str, rdma_resource_name: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": name,
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
                                "rdma_resource_name": rdma_resource_name,
                                "rdma_resource_quantity": 8,
                            },
                            "pool_transports": {
                                "nebius-b300-preemptible-8x": {
                                    "mode": "nixl-rdma",
                                    "rdma_resource_name": "networking.example.com/rdma_shared_device_b",
                                }
                            },
                        }
                    },
                }
            },
        }

    def test_model_express_rdma_resources_reach_the_kueue_configuration(self) -> None:
        """Kueue budgets accelerators only; an auxiliary device must be excluded."""

        deployment = self._modelexpress_deployment(
            "fs2-rdma-exclusions", "example.com/rdma_shared_device_a"
        )
        contract = self._planned_outputs(
            self._write_configuration("rdma-exclusions", deployment), "rdma-exclusions"
        )["deployment_contract"]
        self.assertEqual(
            contract["stages"]["foundation"]["kueue"]["exclude_resource_prefixes"],
            [
                "example.com/rdma_shared_device_a",
                "networking.example.com/rdma_shared_device_b",
            ],
        )

    def test_root_rejects_an_auxiliary_prefix_that_shadows_an_accelerator(self) -> None:
        deployment = self._modelexpress_deployment("fs2-rdma-shadow", "nvidia.com/gpu")
        variable_file = self._write_configuration("rdma-shadow", deployment)
        result, _ = self._plan_file(variable_file, "rdma-shadow")
        self.assertNotEqual(result.returncode, 0)
        diagnostics = re.sub(r"\s+", " ", f"{result.stdout}\n{result.stderr}")
        self.assertIn("would also exclude an accelerator resource", diagnostics)

    def test_kueue_exclusion_grammar_accepts_a_literal_prefix(self) -> None:
        """Kueue matches these with strings.HasPrefix, so a bare prefix is valid."""

        source = (DEPLOY_ROOT / "stages/foundation/variables.tf").read_text(encoding="utf-8")
        match = re.search(
            r'can\(regex\("(\^\[a-z0-9\][^"]*?)", prefix\)\)',
            source,
        )
        self.assertIsNotNone(match)
        pattern = re.compile(match.group(1).replace("\\\\", "\\"))
        for accepted in (
            "networking.example.com/",
            "example.com/rdma_shared_device_a",
            "example.com/rdma",
            "cpu",
            "ephemeral-storage",
        ):
            with self.subTest(accepted=accepted):
                self.assertIsNotNone(pattern.fullmatch(accepted))
        for rejected in ("Example.com/gpu", "example.com//gpu", "example.com/gpu/extra", "-bad"):
            with self.subTest(rejected=rejected):
                self.assertIsNone(pattern.fullmatch(rejected))

    def test_core_admission_gates_a_prefix_that_shadows_cpu_or_memory(self) -> None:
        """Kueue matches prefixes literally, so "c" would exclude cpu."""

        # A ModelExpress RDMA name is always qualified and so can never prefix
        # cpu or memory. The reachable source of a bare prefix is the
        # foundation's own operator input, which is gated there and mirrored at
        # the root for anything the root derives.
        foundation = (DEPLOY_ROOT / "stages/foundation/releases.tf").read_text(encoding="utf-8")
        root = (DEPLOY_ROOT / "main.tf").read_text(encoding="utf-8")
        self.assertIn("!startswith(core_name, prefix)", foundation)
        self.assertIn('for core_name in ["cpu", "memory"] : !startswith(core_name, prefix)', root)
        self.assertIn("prefix of cpu or memory", root)

    def test_core_admission_accepts_a_qualified_auxiliary_prefix(self) -> None:
        deployment = self._modelexpress_deployment(
            "fs2-core-admission", "example.com/rdma_shared_device_a"
        )
        deployment["scheduling"] = {
            "budget_core_resources": True,
        }
        contract = self._planned_outputs(
            self._write_configuration("core-admission", deployment), "core-admission"
        )["deployment_contract"]
        kueue = contract["stages"]["foundation"]["kueue"]
        self.assertTrue(kueue["budget_core_resources"])
        # The facade must hand the same measured values to workloads.  The
        # stage narrows away provenance after variable conversion, but a missing
        # sibling input here previously made host-memory validation see an
        # empty map even while the root accepted the measurement.
        self.assertEqual(
            contract["stages"]["workloads"]["accelerator_node_schedulable_capacity"],
            {
                pool_id: {
                    **capacity,
                    "evidence": {**capacity["evidence"], "node_group_id": None},
                }
                for pool_id, capacity in ACCELERATOR_CAPACITY_FIXTURE.items()
            },
        )
        self.assertEqual(
            kueue["exclude_resource_prefixes"],
            [
                "example.com/rdma_shared_device_a",
                "networking.example.com/rdma_shared_device_b",
            ],
        )

    def test_raw_alphafold3_configuration_passes_the_root_preflight(self) -> None:
        """Root preflight only. The rendered lanes are proved in the stage test.

        See stages/workloads/tests/scientific_scheduling_render.tftest.hcl for
        the actual contract this configuration produces; a facade plan never
        instantiates the workloads stage.
        """

        example = DEPLOY_ROOT / "examples/scheduling-academic-raw-af3.tfvars"
        outputs = self._planned_outputs(example, "raw-af3")
        scheduling = outputs["effective_configuration"]["scheduling"]
        self.assertTrue(scheduling["academic_raw_data_stages"])
        # Pool-coupled and derived: each pool's budget is its measured
        # per-node capacity times its maximum node count, so the numbers
        # cannot drift from the pools that get created.
        self.assertEqual(scheduling["core_admission"], "pool-coupled")
        self.assertEqual(
            scheduling["core_pool_capacity"],
            {
                "h100-warm": {"cpu_millicores": 124000, "memory_mib": 1540096},
                "h100-preemptible": {
                    "cpu_millicores": 248000,
                    "memory_mib": 3080192,
                },
            },
        )
        # The canonical request is derived, and it fits the declared per-node
        # capacity and the ClusterQueue quota.
        self.assertEqual(
            scheduling["cpu_stage_requests"]["reference-data"],
            {"cpu_millicores": 16000, "memory_mib": 65536},
        )
        self.assertEqual(scheduling["academic_cpu_local_queue"], "academic-scientific-cpu")
        self.assertEqual(scheduling["reference_cluster_queue"], "reference-data-cpu")
        # Warm capacity is tried first. Alphabetical pool order would put
        # h100-preemptible first, so this is an explicit operator decision and
        # not the default. The example sets it once: every service class
        # inherits the queue's order rather than repeating the list.
        self.assertEqual(
            scheduling["default_queue_pool_order"], ["h100-warm", "h100-preemptible"]
        )
        self.assertNotEqual(
            scheduling["default_queue_pool_order"],
            sorted(scheduling["default_queue_pool_order"]),
        )
        example_text = example.read_text(encoding="utf-8")
        self.assertEqual(example_text.count('["h100-warm", "h100-preemptible"]'), 2)
        # One setting, not six: no service class repeats the list.
        self.assertNotIn("pool_preference  ", example_text)
        self.assertNotIn("pool_preference =", example_text)
        self.assertNotIn("service_classes", example_text)
        self.assertEqual(
            scheduling["service_class_pool_preference"],
            {
                service_class: ["h100-warm", "h100-preemptible"]
                for service_class in (
                    "platform-critical",
                    "presentation",
                    "interactive",
                    "customer-batch",
                    "bulk-backfill",
                )
            },
        )
        # AlphaFold 3's declared set spans two pools that advertise the same
        # extended resource, so Kueue can actually fall back between them.
        self.assertEqual(
            scheduling["model_eligible_pools"]["alphafold3"],
            ["h100-warm", "h100-preemptible"],
        )
        self.assertEqual(
            {scheduling["pool_resource_names"][pool] for pool in ("h100-warm", "h100-preemptible")},
            {"nvidia.com/gpu"},
        )

    def test_the_scientific_batch_contract_is_declared_exactly_once(self) -> None:
        """A repeated object attribute silently wins and drops fields.

        HCL accepts a duplicate key in an object literal and the later one
        wins, so a stale copy with fewer fields removes them from the
        published contract without any error.
        """

        deployment = {
            "schema_version": 1,
            "name": "scientific-batch-shape",
            "target": self.catalog_target(),
            # Enabling scientific batch narrows the cluster to the Kueue and
            # JobSet tested intersection.
            "cluster": {"kubernetes_version": "1.34"},
            "scientific_batch": {"enabled": True, "namespace": "fs2-scientific"},
            "storage": {
                "scientific_artifacts": {
                    "enabled": True,
                    "egress_cidrs": ["203.0.113.10/32"],
                }
            },
        }
        variable_file = self._write_configuration("scientific-batch-shape", deployment)
        # This is the complete customer-authored scientific batch surface. The
        # generated map belongs to the repository, not terraform.tfvars.
        customer_batch = json.loads(variable_file.read_text(encoding="utf-8"))["deployment"]["scientific_batch"]
        self.assertNotIn("execution_map", customer_batch)

        outputs = self._planned_outputs(variable_file, "scientific-batch-shape")
        stage = outputs["deployment_contract"]["stages"]["workloads"]["scientific_batch"]
        committed_map_path = DEPLOY_ROOT / "catalog/runtime/contracts/scientific-execution-map.json"
        committed_map = json.loads(committed_map_path.read_text(encoding="utf-8"))
        self.assertEqual(
            stage,
            {
                "api_timeout_seconds": "5",
                "enabled": True,
                "execution_map": committed_map,
                "lease_seconds": "30",
                "writes_enabled": False,
                "namespace": "fs2-scientific",
                "poll_seconds": "0.25",
                "runtime_cache": {
                    "enabled": False,
                    "size_gib": 128,
                    "storage_class_name": "csi-mounted-fs-path-sc",
                },
                "token_expiration_seconds": 600,
                "workers": 2,
            },
        )
        effective = outputs["effective_configuration"]["scientific_batch"]
        self.assertEqual(effective["namespace"], "fs2-scientific")
        self.assertTrue(effective["artifact_store_required"])
        self.assertEqual(
            effective["execution_map_source"],
            "catalog/runtime/contracts/scientific-execution-map.json",
        )
        helm_bytes = json.dumps(committed_map, separators=(",", ":"), sort_keys=True).encode()
        self.assertEqual(effective["execution_map_sha256"], hashlib.sha256(helm_bytes).hexdigest())
        profiles = json.loads(
            (DEPLOY_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json").read_text(
                encoding="utf-8"
            )
        )["profiles"]
        profiles_by_id = {profile["model_id"]: profile for profile in profiles}
        for model in committed_map["models"]:
            self.assertEqual(
                profiles_by_id[model["model_id"]]["qualification"]["execution_map_sha256"],
                effective["execution_map_sha256"],
            )

        for relative in ("locals.tf", "outputs.tf"):
            with self.subTest(source=relative):
                source = (DEPLOY_ROOT / relative).read_text(encoding="utf-8")
                self.assertEqual(source.count("    scientific_batch = {"), 1)

    def test_scientific_execution_map_advanced_override_preserves_exact_object(self) -> None:
        committed_map = json.loads(
            (DEPLOY_ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text(encoding="utf-8")
        )
        deployment = {
            "schema_version": 1,
            "name": "scientific-map-override",
            "target": self.catalog_target(),
            "cluster": {"kubernetes_version": "1.34"},
            "scientific_batch": {
                "enabled": True,
                "execution_map": committed_map,
            },
            "storage": {
                "scientific_artifacts": {
                    "enabled": True,
                    "egress_cidrs": ["203.0.113.10/32"],
                }
            },
        }
        outputs = self._planned_outputs(
            self._write_configuration("scientific-map-override", deployment),
            "scientific-map-override",
        )
        stage = outputs["deployment_contract"]["stages"]["workloads"]
        self.assertEqual(stage["scientific_batch"]["execution_map"], committed_map)
        self.assertEqual(
            outputs["effective_configuration"]["scientific_batch"]["execution_map_source"],
            "deployment.scientific_batch.execution_map",
        )

    def test_invalid_or_tampered_scientific_execution_map_is_refused(self) -> None:
        committed_map = json.loads(
            (DEPLOY_ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text(encoding="utf-8")
        )
        invalid_schema = json.loads(json.dumps(committed_map))
        invalid_schema["schema"] = "fs2-serve.nebius.ai/scientific-execution-map/v2"
        empty_map = {
            "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
            "models": [],
        }
        tampered_map = json.loads(json.dumps(committed_map))
        tampered_map["models"][0]["workload_namespace"] = "fs2-tampered"

        for label, execution_map, diagnostic in (
            ("schema", invalid_schema, "non-empty schema-v3 execution map"),
            ("empty", empty_map, "non-empty schema-v3 execution map"),
            (
                "tampered",
                tampered_map,
                "does not match the committed workload-profile qualification digest",
            ),
        ):
            with self.subTest(rejected=label):
                deployment = {
                    "schema_version": 1,
                    "name": f"scientific-map-{label}",
                    "target": self.catalog_target(),
                    "cluster": {"kubernetes_version": "1.34"},
                    "scientific_batch": {
                        "enabled": True,
                        "execution_map": execution_map,
                    },
                    "storage": {
                        "scientific_artifacts": {
                            "enabled": True,
                            "egress_cidrs": ["203.0.113.10/32"],
                        }
                    },
                }
                variable_file = self._write_configuration(f"scientific-map-{label}", deployment)
                result, _ = self._plan_file(variable_file, f"scientific-map-{label}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    diagnostic,
                    re.sub(r"\s+", " ", f"{result.stdout}\n{result.stderr}"),
                )

    def test_customer_tfvars_documents_the_automatic_scientific_map(self) -> None:
        example = (DEPLOY_ROOT / "terraform.tfvars.example").read_text(encoding="utf-8")
        start = example.index("  # Staged scientific batch execution.")
        end = example.index("  # By default the wrapper copies", start)
        scientific_example = example[start:end]
        self.assertNotIn("execution_map =", scientific_example)
        self.assertIn("catalog/runtime/contracts/scientific-execution-map.json", scientific_example)

    def test_measured_capacity_without_a_verifiable_origin_is_refused(self) -> None:
        """A pair of integers with no origin is a claim, not a measurement."""

        broken = {
            "a mismatched pool identity": {"pool_id": "some-other-pool"},
            "an empty source": {"source": "   "},
            "a capture time that is not RFC3339 UTC": {"captured_at": "yesterday"},
            "a payload digest that is not a SHA-256": {"payload_sha256": "abc"},
            "a fixture source naming another pool": {
                "source": "fixture:utf8:some-other-pool"
            },
            "a fixture digest not matching its exact bytes": {
                "payload_sha256": "0" * 64
            },
            "a node group that is not a node group": {"node_group_id": "not-a-group"},
        }
        for index, (label, mutation) in enumerate(broken.items()):
            with self.subTest(rejected=label):
                capacity = json.loads(json.dumps(ACCELERATOR_CAPACITY_FIXTURE))
                capacity["nebius-b300-preemptible-1x"]["evidence"].update(mutation)
                deployment = {
                    "schema_version": 1,
                    "name": f"fs2-measured-evidence-{index}",
                    "target": self.catalog_target(),
                    "cluster": {"kubernetes_version": "1.34"},
                    "scheduling": {
                        "budget_core_resources": True,
                        "accelerator_schedulable_capacity": capacity,
                    },
                }
                variable_file = self._write_configuration(
                    f"measured-evidence-{index}", deployment
                )
                result, _ = self._plan_file(variable_file, f"measured-evidence-{index}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "not a measurement",
                    re.sub(r"\s+", " ", f"{result.stdout}\n{result.stderr}"),
                )

    def test_a_pool_with_no_measurement_cannot_budget_core_resources(self) -> None:
        """Fail closed: an unknown total cannot bound a pool-coupled quota."""

        capacity = json.loads(json.dumps(ACCELERATOR_CAPACITY_FIXTURE))
        del capacity["nebius-b300-preemptible-8x"]
        deployment = {
            "schema_version": 1,
            "name": "fs2-measured-missing",
            "target": self.catalog_target(),
            "cluster": {"kubernetes_version": "1.34"},
            "scheduling": {
                "budget_core_resources": True,
                "accelerator_schedulable_capacity": capacity,
            },
        }
        variable_file = self._write_configuration("measured-missing", deployment)
        result, _ = self._plan_file(variable_file, "measured-missing")
        self.assertNotEqual(result.returncode, 0)
        diagnostics = re.sub(r"\s+", " ", f"{result.stdout}\n{result.stderr}")
        self.assertIn("nebius-b300-preemptible-8x", diagnostics)

    def test_a_pool_id_with_a_dot_and_underscore_is_accepted_everywhere(self) -> None:
        """Pool IDs are label values, not DNS labels, at every layer."""

        variable_file = self._two_pool_af3_deployment(
            "raw-af3-label-value-ids", pool_id_suffix="_1.x"
        )
        outputs = self._planned_outputs(variable_file, "raw-af3-label-value-ids")
        scheduling = outputs["effective_configuration"]["scheduling"]
        self.assertEqual(
            scheduling["default_queue_pool_order"],
            ["h100-warm_1.x", "h100-preemptible_1.x"],
        )
        self.assertEqual(
            scheduling["model_eligible_pools"]["alphafold3"],
            ["h100-warm_1.x", "h100-preemptible_1.x"],
        )
        # The same grammar, stated once, is what every layer checks against.
        grammar = '^[a-z0-9](?:[-_a-z0-9.]{0,61}[a-z0-9])?$'
        for relative in (
            "variables.tf",
            "stages/workloads/variables.tf",
            "stages/infrastructure/variables.tf",
            "modules/kueue-scheduling/variables.tf",
        ):
            with self.subTest(layer=relative):
                self.assertIn(grammar, (DEPLOY_ROOT / relative).read_text(encoding="utf-8"))
        schema = json.loads(
            (
                DEPLOY_ROOT / "catalog/runtime/schema/cpu-stage-classes.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(schema["$defs"]["pool_id"]["pattern"], grammar)
        self.assertEqual(schema["$defs"]["pool_id"]["maxLength"], 63)

    def test_an_eligible_pool_set_spanning_two_resources_fails_the_root_preflight(
        self,
    ) -> None:
        """A set Kueue cannot fall back across must fail before any stage runs.

        A Workload requests exactly one extended resource and Kueue never
        falls back across a resourceGroup, so the second pool is unreachable.
        The workloads stage refuses it too, but the infrastructure stage runs
        first and would already have created the pools.
        """

        # The shipped example's two-pool shape, with the burst pool advertising
        # a MIG-slice resource instead of the full GPU.
        variable_file = self._two_pool_af3_deployment(
            "raw-af3-mixed", burst_resource_name="nvidia.com/mig-1g.10gb"
        )
        result, _ = self._plan_file(variable_file, "raw-af3-mixed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "single extended resource name",
            re.sub(r"\s+", " ", f"{result.stdout}\n{result.stderr}"),
        )

        # The same deployment with one resource name across both pools is
        # accepted, so the gate rejects the mix and not the two-pool set.
        accepted = self._two_pool_af3_deployment("raw-af3-same-resource")
        result, _ = self._plan_file(accepted, "raw-af3-same-resource")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_declaration_cannot_overwrite_an_authoritative_placement_at_root(
        self,
    ) -> None:
        """The workloads stage refuses it, but infrastructure runs first."""

        variable_file = self._two_pool_af3_deployment(
            "raw-af3-collision", declare_placed_model=True
        )
        result, _ = self._plan_file(variable_file, "raw-af3-collision")
        self.assertNotEqual(result.returncode, 0)
        diagnostics = re.sub(r"\s+", " ", f"{result.stdout}\n{result.stderr}")
        self.assertIn("may not overwrite a model", diagnostics)
        self.assertIn("proteinmpnn", diagnostics)

    def test_an_empty_pool_order_is_derived_warm_first_at_the_facade(self) -> None:
        variable_file = self._two_pool_af3_deployment("raw-af3-derived-warm-first")
        value = json.loads(variable_file.read_text(encoding="utf-8"))
        scheduling = value["deployment"]["scheduling"]
        del scheduling["default_queue_pool_order"]
        for policy in scheduling["service_classes"].values():
            policy["pool_preference"] = []
        variable_file.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        outputs = self._planned_outputs(variable_file, "raw-af3-derived-warm-first")
        self.assertEqual(
            outputs["effective_configuration"]["scheduling"][
                "default_queue_pool_order"
            ],
            ["h100-warm", "h100-preemptible"],
        )

    def test_a_reversed_explicit_pool_order_is_refused_at_the_facade(self) -> None:
        variable_file = self._two_pool_af3_deployment("raw-af3-reversed-order")
        value = json.loads(variable_file.read_text(encoding="utf-8"))
        scheduling = value["deployment"]["scheduling"]
        reversed_order = ["h100-preemptible", "h100-warm"]
        scheduling["default_queue_pool_order"] = reversed_order
        for policy in scheduling["service_classes"].values():
            policy["pool_preference"] = reversed_order
        variable_file.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        result, _ = self._plan_file(variable_file, "raw-af3-reversed-order")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "warm-first", re.sub(r"\s+", " ", f"{result.stdout}\n{result.stderr}")
        )

    def test_all_preemptible_pools_still_prefer_the_node_floor(self) -> None:
        variable_file = self._two_pool_af3_deployment("raw-af3-all-preemptible")
        value = json.loads(variable_file.read_text(encoding="utf-8"))
        deployment = value["deployment"]
        deployment["accelerator_pools"]["h100-warm"]["capacity_type"] = "preemptible"
        scheduling = deployment["scheduling"]
        del scheduling["default_queue_pool_order"]
        for policy in scheduling["service_classes"].values():
            policy["pool_preference"] = []
        variable_file.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        outputs = self._planned_outputs(variable_file, "raw-af3-all-preemptible")
        self.assertEqual(
            outputs["effective_configuration"]["scheduling"][
                "default_queue_pool_order"
            ],
            ["h100-warm", "h100-preemptible"],
        )

    def _two_pool_af3_deployment(
        self,
        name: str,
        burst_resource_name: str = "nvidia.com/gpu",
        declare_placed_model: bool = False,
        pool_id_suffix: str = "",
    ) -> Path:
        """The shipped raw example's warm-plus-burst shape, as JSON tfvars."""

        def pool(
            pool_id: str, capacity_type: str, minimum: int, maximum: int, **extra: Any
        ) -> dict[str, Any]:
            return {
                "platform": "gpu-h100-sxm",
                "preset": "8gpu-128vcpu-1600gb",
                "accelerator_class": "nvidia-h100-sxm5-80gb",
                "gpus_per_node": 8,
                "gpu_memory_gb": 80,
                "capacity_type": capacity_type,
                "min_nodes": minimum,
                "max_nodes": maximum,
                "driver": {"mode": "managed", "preset": "cuda12.4"},
                # Synthetic plan fixture below the nominal preset size. The
                # source names the exact UTF-8 bytes hashed by the digest.
                "schedulable_capacity": {
                    "cpu_millicores": 124000,
                    "memory_mib": 1540096,
                    "evidence": {
                        "pool_id": pool_id,
                        "source": f"fixture:utf8:{pool_id}",
                        "captured_at": "2026-09-03T06:00:00Z",
                        "payload_sha256": hashlib.sha256(
                            pool_id.encode("utf-8")
                        ).hexdigest(),
                    },
                },
                **extra,
            }

        order = [f"h100-warm{pool_id_suffix}", f"h100-preemptible{pool_id_suffix}"]
        warm, burst = order
        deployment = {
            "schema_version": 1,
            "name": name,
            "target": self.catalog_target(),
            "profiles": {
                "capacity": "minimal",
                "accelerators": "minimal",
                # A selected model exists only in this profile, and the
                # collision case needs one with an authoritative placement.
                "models": "minimal" if declare_placed_model else "none",
            },
            "cluster": {"kubernetes_version": "1.34"},
            "accelerator_pools": {
                warm: pool(warm, "regular", 1, 1),
                burst: pool(
                    burst, "preemptible", 0, 2, resource_name=burst_resource_name
                ),
            },
            "models": {"selection": "profile"},
            "scientific_batch": {"enabled": True},
            "scheduling": {
                "cohort": {"enabled": True, "name": "inference-shared"},
                "fair_share_precedence_acknowledged": True,
                "academic_raw_data_stages": True,
                "default_queue_pool_order": order,
                "model_eligible_pool_ids": (
                    {"alphafold3": order, "proteinmpnn": order}
                    if declare_placed_model
                    else {"alphafold3": order}
                ),
                "budget_core_resources": True,
                "service_classes": {
                    service_class: {
                        "workload_priority_class": priority_class,
                        "priority": priority,
                        "preemption_mode": "restartable",
                        "pool_preference": order,
                    }
                    for service_class, priority_class, priority in (
                        ("platform-critical", "platform-critical", 10000),
                        ("presentation", "presentation", 1000),
                        ("interactive", "interactive", 100),
                        ("customer-batch", "standard", 0),
                        ("bulk-backfill", "batch", -100),
                    )
                },
            },
            "storage": {
                "scientific_artifacts": {
                    "enabled": True,
                    "egress_cidrs": ["203.0.113.10/32"],
                },
                "reference_data": {
                    "enabled": True,
                    "cpu_pool": {
                        "platform": "cpu-d3",
                        "preset": "32vcpu-128gb",
                        "node_count": 1,
                        "schedulable_capacity": {
                            "cpu_millicores": 30000,
                            "memory_mib": 122880,
                            "ephemeral_storage_mib": 114688,
                        },
                    },
                    "filesystem": {"size_gib": 2048},
                    "object_storage": {"max_size_gib": 2048},
                    "queue": {"nominal_cpu": "24", "nominal_memory": "96Gi"},
                },
            },
            "edge": {"mode": "internal-only"},
        }
        return self._write_configuration(
            name,
            deployment,
            academic_assets={
                "enabled": True,
                "tenant_id": "tenant-academic",
                "namespace": "fs2-academic-poc",
                "execution": {"enabled": True},
                "assets": {
                    "alphafold3-parameters": {
                        "model_id": "alphafold3",
                        "relative_path": "alphafold3/af3.bin.zst",
                        "runtime_binding": {
                            "artifact_id": "alphafold3-parameters",
                            "source_sub_path": "alphafold3/af3.bin.zst",
                            "consumer_path": "/models/af3.bin.zst",
                            "mechanism": "subpath-file-mount",
                        },
                    }
                },
            },
        )

    def test_academic_readiness_defaults_to_the_checked_in_contract_bytes(self) -> None:
        variable_file = self._two_pool_af3_deployment("academic-readiness-default")
        configuration = json.loads(variable_file.read_text(encoding="utf-8"))
        configuration["deployment"]["scientific_batch"]["runtime_cache"] = {
            "enabled": True
        }
        variable_file.write_text(
            json.dumps(configuration, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        outputs = self._planned_outputs(variable_file, "academic-readiness-default")
        readiness_path = (
            DEPLOY_ROOT
            / "catalog"
            / "runtime"
            / "contracts"
            / "academic-asset-readiness.json"
        )
        expected = hashlib.sha256(readiness_path.read_bytes()).hexdigest()

        self.assertEqual(
            outputs["academic_assets"]["readiness_manifest_sha256"], expected
        )
        self.assertEqual(
            outputs["deployment_contract"]["stages"]["workloads"][
                "academic_assets"
            ]["readiness_manifest_sha256"],
            expected,
        )

    def _raw_af3_deployment(self, name: str, override: dict[str, Any] | None = None) -> Path:
        """The shipped raw configuration's scheduling inputs, as a tfvars file."""

        scheduling: dict[str, Any] = {
            "cohort": {"enabled": True, "name": "inference-shared"},
            "fair_share_precedence_acknowledged": True,
            "academic_raw_data_stages": True,
            "budget_core_resources": True,
            # AlphaFold 3 is scientific-only, so nothing derives its
            # qualification. The licensed lane routes it, so the root refuses
            # the plan without a declaration, exactly as the stage does. Both
            # profile pools advertise the same extended resource.
            "model_eligible_pool_ids": {
                "alphafold3": [
                    "nebius-b300-preemptible-1x",
                    "nebius-b300-preemptible-8x",
                ]
            },
        }
        if override is not None:
            scheduling["cpu_stage_requests"] = override
        deployment = {
            "schema_version": 1,
            "name": name,
            "target": self.catalog_target(),
            "profiles": {"capacity": "minimal", "accelerators": "minimal", "models": "none"},
            "cluster": {"kubernetes_version": "1.34"},
            "scientific_batch": {"enabled": True},
            "scheduling": scheduling,
            "storage": {
                # Batch execution commits results to the artifact store, which
                # main couples to the gate.
                "scientific_artifacts": {
                    "enabled": True,
                    "egress_cidrs": ["203.0.113.10/32"],
                },
                "reference_data": {
                    "enabled": True,
                    "cpu_pool": {
                        "preset": "32vcpu-128gb",
                        "schedulable_capacity": {
                            "cpu_millicores": 30000,
                            "memory_mib": 122880,
                            "ephemeral_storage_mib": 114688,
                        },
                    },
                    "queue": {"nominal_cpu": "24", "nominal_memory": "96Gi"},
                }
            },
        }
        return self._write_configuration(
            name,
            deployment,
            # The licensed claim and its execution identity are a separate
            # top-level input, and raw mode needs them.
            academic_assets={
                "enabled": True,
                "tenant_id": "tenant-academic",
                "namespace": "fs2-academic-poc",
                # The lane must name this deployment's actual stable
                # ClusterQueue, not the default name from another profile.
                "execution": {"enabled": True, "cluster_queue": "fs2-b300-async"},
                "assets": {
                    "alphafold3-parameters": {
                        "model_id": "alphafold3",
                        "relative_path": "alphafold3/af3.bin.zst",
                    }
                },
            },
        )

    def test_a_smaller_override_cannot_lower_the_raw_stage_floor(self) -> None:
        variable_file = self._raw_af3_deployment(
            "raw-af3-override", {"reference-data": {"cpu_millicores": 1, "memory_mib": 1}}
        )
        scheduling = self._planned_outputs(variable_file, "raw-af3-override")[
            "effective_configuration"
        ]["scheduling"]
        # The floor survives: an override may raise the canonical request and
        # can never lower it below what the stage actually needs.
        self.assertEqual(
            scheduling["cpu_stage_requests"]["reference-data"],
            {"cpu_millicores": 16000, "memory_mib": 65536},
        )

    def test_an_undersized_reference_pool_or_quota_is_rejected(self) -> None:
        """One raw stage Pod must fit one node and its ClusterQueue quota."""

        for name, mutation, expected in (
            (
                "raw-af3-small-node",
                {
                    "cpu_pool": {
                        "schedulable_capacity": {
                            "cpu_millicores": 7000,
                            "memory_mib": 28672,
                            "ephemeral_storage_mib": 114688,
                        }
                    }
                },
                "schedulable capacity",
            ),
            (
                "raw-af3-small-quota",
                {"queue": {"nominal_cpu": "6", "nominal_memory": "24Gi"}},
                "quota of the ClusterQueue that admits it",
            ),
        ):
            with self.subTest(name=name):
                variable_file = self._raw_af3_deployment(name)
                deployment = json.loads(variable_file.read_text(encoding="utf-8"))
                reference = deployment["deployment"]["storage"]["reference_data"]
                for key, value in mutation.items():
                    reference[key] = {**reference.get(key, {}), **value}
                variable_file.write_text(json.dumps(deployment, indent=2), encoding="utf-8")
                result, _ = self._plan_file(variable_file, name)
                self.assertNotEqual(result.returncode, 0)
                diagnostics = re.sub(r"\s+", " ", f"{result.stdout}\n{result.stderr}")
                self.assertIn(expected, diagnostics)


    def test_root_and_workloads_derive_one_academic_scheduling_lane(self) -> None:
        """Both stages must derive the licensed lane from the same facts."""

        root_locals = (DEPLOY_ROOT / "locals.tf").read_text(encoding="utf-8")
        queue_source = (DEPLOY_ROOT / "stages/workloads/queue.tf").read_text(encoding="utf-8")
        for expression in (
            "var.academic_assets.execution.local_queue",
            "var.academic_assets.execution.cluster_queue",
            "var.academic_assets.namespace",
            "var.academic_assets.tenant_id",
        ):
            with self.subTest(expression=expression):
                self.assertIn(expression, root_locals)
                self.assertIn(expression, queue_source)
        # Both derive the model list from the declared assets rather than a
        # separately maintained copy.
        self.assertIn("for asset in values(var.academic_assets.assets) : asset.model_id", root_locals)
        self.assertIn("for asset in values(var.academic_assets.assets) : asset.model_id", queue_source)
        # Both reject an operator lane that collides with the derived one.
        self.assertIn("root_academic_lane_queue_collisions", root_locals)
        self.assertIn("academic_lane_queue_collisions", queue_source)
        # Both mirror the same rank-separated route keys.
        for source in (root_locals, queue_source + root_locals):
            self.assertIn("jsonencode([service_class, tenant_id, model_id])", source)

    def test_root_rejects_active_custom_mig_before_infrastructure(self) -> None:
        pool = {
            "platform": "gpu-h100-sxm",
            "preset": "8gpu-128vcpu-1600gb",
            "accelerator_class": "nvidia-h100-sxm5-80gb",
            "gpus_per_node": 8,
            "capacity_type": "preemptible",
            "min_nodes": 0,
            "max_nodes": 1,
            "resource_name": "nvidia.com/mig-1g.10gb",
            "driver": {"mode": "operator"},
            "mig": {"strategy": "single", "config": "all-1g.10gb"},
        }
        deployment = {
            "schema_version": 1,
            "name": "fs2-root-active-mig",
            "profiles": {"capacity": "minimal", "models": "none"},
            "target": self.catalog_target(),
            "accelerator_pools": {"h100-mig": pool},
        }
        variable_file = self._write_configuration("root-active-mig", deployment)
        result, _ = self._plan_file(variable_file, "root-active-mig")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Active custom MIG scheduling is blocked", result.stderr)

    def test_root_enforces_kueue_and_jobset_minor_compatibility(self) -> None:
        for name, version, scientific in (
            ("kueue-untested-minor", "1.32.9", False),
            ("jobset-unqualified-minor", "1.36.0", True),
        ):
            deployment = {
                "schema_version": 1,
                "name": f"fs2-{name}",
                "target": self.catalog_target(),
                "cluster": {"kubernetes_version": version},
            }
            if scientific:
                deployment["scientific_batch"] = {"enabled": True}
            variable_file = self._write_configuration(name, deployment)
            result, _ = self._plan_file(variable_file, name)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("No wider minor is claimed", result.stderr)

        deployment = {
            "schema_version": 1,
            "name": "fs2-jobset-qualified-minor",
            "target": self.catalog_target(),
            "cluster": {"kubernetes_version": "1.35.6"},
            "scientific_batch": {"enabled": True},
            "storage": {
                "scientific_artifacts": {
                    "enabled": True,
                    "egress_cidrs": ["203.0.113.10/32"],
                }
            },
        }
        variable_file = self._write_configuration(
            "jobset-qualified-minor", deployment
        )
        outputs = self._planned_outputs(variable_file, "jobset-qualified-minor")
        jobset = outputs["deployment_contract"]["stages"]["foundation"]["jobset"]
        self.assertEqual(
            jobset, {"enabled": True, "kubernetes_version": "1.35.6"}
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
                "fast_start_mechanisms_file": None,
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
            "try(each.value.features.reference_data_filesystem, false)",
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

    def test_reference_data_plane_defaults_to_disposable_full_teardown(self) -> None:
        image = f"cr.eu-north1.nebius.cloud/test/reference-stager@sha256:{'a' * 64}"
        deployment = {
            "schema_version": 1,
            "name": "fs2-reference-data-test",
            "target": self.catalog_target(),
            # Its ClusterQueue budgets cpu and memory, which Kueue only
            # counts when core admission is on.
            "scheduling": {
                "budget_core_resources": True,
            },
            "storage": {
                "reference_data": {
                    "enabled": True,
                    "filesystem": {"size_gib": 2048},
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
        self.assertEqual("disposable", infrastructure["lifecycle"]["retention_mode"])
        self.assertEqual("fs2-reference-data", workloads["namespace"])
        self.assertEqual("8vcpu-32gb", infrastructure["cpu_pool"]["preset"])
        self.assertEqual(1, infrastructure["cpu_pool"]["node_count"])
        self.assertEqual(
            {
                "cpu_millicores": 7000,
                "memory_mib": 28672,
                "ephemeral_storage_mib": 114688,
            },
            infrastructure["cpu_pool"]["schedulable_capacity"],
        )
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
        self.assertEqual(
            "full-only-when-versioned-bucket-empty",
            outputs["effective_configuration"]["reference_data"]["destroy_completion"],
        )
        self.assertFalse(
            outputs["effective_configuration"]["reference_data"]["adoption_required"]
        )
        self.assertFalse(
            outputs["effective_configuration"]["reference_data"][
                "filesystem_forbid_deletion"
            ]
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
        workload_outputs = (
            DEPLOY_ROOT / "stages/workloads/outputs.tf"
        ).read_text(encoding="utf-8")
        self.assertIn('versioning_policy     = "ENABLED"', infrastructure_source)
        self.assertIn('forbid_deletion  = var.reference_data.filesystem.forbid_deletion', infrastructure_source)
        self.assertIn('mount_tag   = "fs2reference"', cluster_source)
        self.assertIn('resource "nebius_mk8s_v1_node_group" "reference_data"', cluster_source)
        self.assertIn('"workload.fs2.nebius/reference-data" = "true"', cluster_source)
        self.assertIn('effect = "NO_SCHEDULE"', cluster_source)
        self.assertNotIn('"nvidia.com/gpu"', pipeline_source)
        self.assertIn('suspend                 = true', pipeline_source)
        self.assertIn('"--object-store-prefix", "s3://${var.object_bucket_name}/reference-data"', pipeline_source)
        self.assertIn('"placement-contract.json" = file(', pipeline_source)
        self.assertIn("tools_sha256     = sha256(jsonencode(local.tools_files))", pipeline_source)
        self.assertRegex(pipeline_source, r"data\s*=\s*local\.tools_files")
        self.assertRegex(
            pipeline_source,
            r'(?s)resource "kubernetes_config_map_v1" "tools" \{.*?create_before_destroy = true',
        )
        self.assertIn("spec = local.pipeline_job_contract.spec", pipeline_source)
        self.assertIn('path = "/healthz"', pipeline_source)
        self.assertIn(
            "reference_data_contract = var.reference_data.enabled ? terraform_data.reference_data_contract[0].output : null",
            workload_outputs,
        )
        self.assertIn(
            "local.reference_data_required_capacity",
            (DEPLOY_ROOT / "main.tf").read_text(encoding="utf-8"),
        )

    def test_reference_data_retention_is_an_explicit_matched_opt_in(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-reference-retained-opt-in",
            "target": self.catalog_target(),
            # Its ClusterQueue budgets cpu and memory, which Kueue only
            # counts when core admission is on.
            "scheduling": {
                "budget_core_resources": True,
            },
            "storage": {
                "reference_data": {
                    "enabled": True,
                    "lifecycle": {"retention_mode": "retain"},
                    "filesystem": {"forbid_deletion": True},
                }
            },
        }
        variable_file = self._write_configuration(
            "reference-retained-opt-in", deployment
        )
        outputs = self._planned_outputs(variable_file, "reference-retained-opt-in")
        reference = outputs["effective_configuration"]["reference_data"]
        self.assertEqual("retain", reference["retention_mode"])
        self.assertTrue(reference["filesystem_forbid_deletion"])
        self.assertTrue(reference["adoption_required"])
        self.assertEqual(
            "full-stack-destroy-incomplete-infrastructure-retained",
            reference["destroy_completion"],
        )

        unmatched = {
            "schema_version": 1,
            "name": "fs2-reference-retained-unmatched",
            "target": self.catalog_target(),
            "storage": {
                "reference_data": {
                    "enabled": True,
                    "lifecycle": {"retention_mode": "retain"},
                }
            },
        }
        unmatched_file = self._write_configuration(
            "reference-retained-unmatched", unmatched
        )
        result, _ = self._plan_file(
            unmatched_file, "reference-retained-unmatched"
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "explicit retain+forbid_deletion semantics",
            f"{result.stdout}\n{result.stderr}",
        )

    def test_reference_filesystem_attachment_is_explicit_per_accelerator_pool(self) -> None:
        variables = (DEPLOY_ROOT / "variables.tf").read_text(encoding="utf-8")
        infrastructure = (
            DEPLOY_ROOT / "stages/infrastructure/cluster.tf"
        ).read_text(encoding="utf-8")
        pool_locals = (
            DEPLOY_ROOT / "stages/infrastructure/variables.tf"
        ).read_text(encoding="utf-8")
        self.assertIn("reference_data_filesystem = optional(bool, false)", variables)
        self.assertIn("reference_data_filesystem = optional(bool, false)", pool_locals)
        self.assertIn(
            "try(each.value.features.reference_data_filesystem, false) ? local.reference_data_filesystem_attachment : []",
            infrastructure,
        )
        self.assertNotIn(
            "each.value.features.shared_filesystem && var.reference_data.enabled",
            infrastructure,
        )

    def test_reference_data_rejects_the_live_database_namespace_at_root(self) -> None:
        deployment = {
            "schema_version": 1,
            "name": "fs2-ref-ns-rejected",
            "target": self.catalog_target(),
            "storage": {
                "reference_data": {
                    "enabled": True,
                    "namespace": "fs2-data",
                    "filesystem": {"size_gib": 2048},
                    "object_storage": {"max_size_gib": 2048},
                }
            },
        }
        variable_file = self._write_configuration(
            "reference-database-namespace-rejected", deployment
        )
        result, _ = self._plan_file(
            variable_file, "reference-database-namespace-rejected"
        )
        self.assertNotEqual(0, result.returncode)
        self.assertRegex(
            f"{result.stdout}\n{result.stderr}",
            r"(?s)dedicated fs2-reference-data\s+namespace.*fs2-data database namespace",
        )

    def test_reference_namespace_has_one_cross_state_owner(self) -> None:
        foundation_locals = (
            DEPLOY_ROOT / "stages/foundation/locals.tf"
        ).read_text(encoding="utf-8")
        namespace_block_start = foundation_locals.index("namespaces = toset([")
        namespace_block_end = foundation_locals.index("])", namespace_block_start)
        foundation_namespaces = set(
            re.findall(
                r'"([a-z0-9-]+)"',
                foundation_locals[namespace_block_start:namespace_block_end],
            )
        )
        workloads = (
            DEPLOY_ROOT / "stages/workloads/reference_data.tf"
        ).read_text(encoding="utf-8")
        reference_module = (
            DEPLOY_ROOT / "reference-data/terraform/main.tf"
        ).read_text(encoding="utf-8")

        self.assertIn("fs2-data", foundation_namespaces)
        self.assertNotIn("fs2-reference-data", foundation_namespaces)
        self.assertRegex(
            workloads,
            r'(?s)module "reference_data".*?'
            r'source\s*=\s*"\.\./\.\./reference-data/terraform".*?'
            r'namespace\s*=\s*var\.reference_data\.namespace',
        )
        self.assertRegex(
            reference_module,
            r'(?s)resource "kubernetes_namespace_v1" "reference_data"\s*\{.*?'
            r'name\s*=\s*var\.namespace',
        )

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
        self.assertRegex(f"{result.stdout}\n{result.stderr}", r"at least\s+1611\s+GiB")

    def test_reference_data_work_rejects_underdeclared_worker_capacity(self) -> None:
        image = f"cr.eu-north1.nebius.cloud/test/reference-stager@sha256:{'a' * 64}"
        deployment = {
            "schema_version": 1,
            "name": "fs2-reference-capacity-too-small",
            "target": self.catalog_target(),
            "storage": {
                "reference_data": {
                    "enabled": True,
                    "cpu_pool": {
                        "schedulable_capacity": {
                            "cpu_millicores": 6000,
                            "memory_mib": 28672,
                            "ephemeral_storage_mib": 114688,
                        }
                    },
                    "network": {"allow_public_source_staging": True},
                    "status": {"enabled": True, "image": image},
                    "pipeline": {"enabled": True, "image": image},
                }
            },
        }
        variable_file = self._write_configuration("reference-capacity-too-small", deployment)
        result, _ = self._plan_file(variable_file, "reference-capacity-too-small")
        self.assertNotEqual(0, result.returncode)
        self.assertRegex(
            f"{result.stdout}\n{result.stderr}",
            r"dedicated tainted CPU preprocessing\s+pool",
        )

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
                "reference_data_filesystem": False,
                # Measured allocatable is optional; a pool that budgets core
                # resources must state it, and this one does not.
                "schedulable_capacity": None,
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
            DEPLOY_ROOT / "examples/scheduling-h100-lanes.tfvars",
            DEPLOY_ROOT / "examples/scheduling-academic-raw-af3.tfvars",
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

        # Helm installs the chart by immutable digest, and the same digest is
        # what the verification provisioner resolves, so both cannot install
        # different bytes. The archive SHA-256 is a recorded identity of that
        # digest's tarball, not a separate thing Helm consumes.
        foundation_locals = (DEPLOY_ROOT / "stages/foundation/locals.tf").read_text(encoding="utf-8")
        # Helm installs the exact archive the verifier checked, materialized
        # during plan at a content-addressed path under the run root.
        self.assertIn("chart            = local.kueue_chart_archive", releases)
        self.assertIn('data "external" "kueue_chart"', releases)
        self.assertIn("FS2_KUEUE_CHART_ARCHIVE        = local.kueue_chart_archive", releases)
        self.assertIn(
            "kueue_chart_archive = data.external.kueue_chart.result.path",
            foundation_locals,
        )
        self.assertIn("yamlencode(local.kueue_effective_values)", releases)
        self.assertIn(
            "oci://registry.k8s.io/kueue/charts/kueue@sha256:e5f000fcf0604e5dea0025e0ffdd20e6712de432bcca0ec254d71d97f012a354",
            foundation_locals,
        )
        self.assertIn(
            'chart_archive_sha256 = "409de6260d2b7834fece5044502822bcb4e74ed8a03b8ea22bb78bcdfa1627db"',
            foundation_locals,
        )
        # The pinned controller image lives only in the values file, so the
        # release and the verifier cannot pin two different images.
        self.assertEqual(
            values["controllerManager"]["manager"]["image"]["tag"],
            "v0.17.8@sha256:cecba825d0b0feab9bed2835efe2eb8d825512f1616c8762ab80c53f2ea6afe6",
        )
        self.assertNotIn("cecba825d0b0feab9bed2835efe2eb8d825512f1616c8762ab80c53f2ea6afe6", releases)
        self.assertEqual(
            values["controllerManager"]["nodeSelector"],
            {"workload.fs2.nebius/system": "true"},
        )
        self.assertEqual(
            manager["resources"]["excludeResourcePrefixes"],
            ["cpu", "memory", "ephemeral-storage"],
        )
        self.assertIn("deployment", manager["integrations"]["frameworks"])


if __name__ == "__main__":
    unittest.main()
