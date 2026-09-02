from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


DEPLOY_ROOT = Path(__file__).resolve().parents[1]
STACK_PATH = DEPLOY_ROOT / "inference-stack"
MODULE_NAME = "inference_stack_under_test"
LOADER = importlib.machinery.SourceFileLoader(MODULE_NAME, str(STACK_PATH))
SPEC = importlib.util.spec_from_loader(MODULE_NAME, LOADER)
assert SPEC is not None
STACK = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = STACK
LOADER.exec_module(STACK)


def arguments() -> Namespace:
    return Namespace(
        terraform="terraform-test",
        nebius="nebius-test",
        kubectl="kubectl-test",
        crane="crane-test",
        nebius_profile="sandbox",
    )


def contract() -> dict:
    return {
        "schema_version": 1,
        "name": "fs2-wrapper-test",
        "run_id": "r0123456789",
        "selected_model_ids": ["proteinmpnn"],
        "profiles": {
            "capacity": "full_catalog",
            "accelerators": "full_catalog",
            "models": "full_catalog",
        },
        "target": {
            "project_id": "project-test",
            "region": "us-north1",
        },
        "artifact_delivery": {
            "mode": "direct-source",
            "repository_prefix": "",
            "upstream_registry_ids": [],
            "source_hosts": [],
        },
        "stages": {
            "infrastructure": {
                "project_id": "project-test",
                "run_id": "r0123456789",
            },
            "foundation": {
                "grafana_admin_secret_ref": {
                    "name": "fs2-grafana-admin",
                    "user_key": "admin-user",
                    "password_key": "admin-password",
                },
                "grafana_publication": {
                    "enabled": False,
                    "external_base_url": "",
                },
            },
            "workloads": {
                "deployment_profile": "full_catalog",
                "enabled_model_ids": ["proteinmpnn"],
            },
        },
        "secret_environment": {
            "grafana_username": "TEST_FS2_GRAFANA_USERNAME",
            "grafana_password": "TEST_FS2_GRAFANA_PASSWORD",
            "ngc_api_key": "TEST_FS2_NGC_API_KEY",
            "nvcr_dockerconfig": "TEST_FS2_NVCR_DOCKERCONFIGJSON",
        },
        "secret_requirements": {
            "grafana_bootstrap": True,
            "ngc_api_key": True,
            "nvcr_dockerconfig": True,
        },
    }


def dynamic_outputs(run_root: Path) -> dict:
    return {
        "run_root": str(run_root),
        "kubeconfig_path": str(run_root / "kubeconfig"),
        "run_id": "r0123456789",
        "cluster_id": "mk8scluster-test",
        "cluster_name": "fs2-wrapper-test",
        "kube_context": "fs2-wrapper-test",
        "kube_system_uid": "11111111-2222-3333-4444-555555555555",
        "project_id": "project-test",
        "target_contract": {"schema": "target-test/v1"},
        "infrastructure_contract": {"schema": "infrastructure-test/v1"},
        "accelerator_pool_contract": {
            "schema": "accelerators-test/v1",
            "artifact_delivery": {"mode": "direct-source"},
        },
        "registry_delivery_contract": {
            "schema": "fs2-serve.nebius.ai/registry-delivery/v1",
            "mode": "direct-source",
            "repository_prefix": "",
            "target_registry": {
                "id": "registry-test",
                "project_id": "project-test",
                "region": "us-north1",
                "fqdn": "cr.us-north1.nebius.cloud",
                "repository_root": "cr.us-north1.nebius.cloud/test",
            },
        },
        "public_edge_contract": {"mode": "internal-only"},
    }


def complete_access_bundle() -> dict:
    return {
        "schema": "fs2-serve.nebius.ai/access-bundle/v1",
        "cluster": {
            "project_id": "project-test",
            "region": "us-north1",
            "cluster_id": "mk8scluster-test",
            "cluster_name": "k8s-inference-test",
            "kube_context": "k8s-inference-test",
            "kubeconfig_command": (
                "KUBECONFIG=/private/run/kubeconfig nebius mk8s cluster get-credentials"
            ),
        },
        "endpoints": {
            "admin_portal_url": "https://192.0.2.12/admin/",
            "mcp_url": "https://192.0.2.12/mcp",
            "inference_base_url": "https://192.0.2.12/v1",
            "grafana_url": "https://192.0.2.12/admin/observability/grafana",
        },
        "credentials": {
            "admin_bootstrap_token": "test-only-admin-token",
            "mcp_inference_token": "test-only-client-token",
            "inference_access_token": "test-only-client-token",
            "grafana": {
                "username": "test-only-grafana-user",
                "password": "test-only-grafana-password",
            },
        },
        "mcp_access": {
            "principal_id": "terraform-bootstrap-client",
            "tenant_id": "tenant-test",
            "scopes": ["mcp.invoke", "inference.invoke"],
        },
    }


def regional_contract() -> dict:
    configuration = json.loads(json.dumps(contract()))
    configuration["artifact_delivery"] = {
        "mode": "regional-mirror",
        "repository_prefix": "",
        "upstream_registry_ids": ["registry-source"],
        "source_hosts": ["cr.eu-north1.nebius.cloud"],
    }
    configuration["stages"]["workloads"].update(
        {
            "control_plane_image": {
                "repository": "cr.eu-north1.nebius.cloud/source/fs2-platform/control-plane",
                "digest": f"sha256:{'a' * 64}",
            },
            "admin_console": {
                "image": {
                    "repository": "cr.eu-north1.nebius.cloud/source/fs2-platform/admin-console",
                    "digest": f"sha256:{'b' * 64}",
                }
            },
            "model_image_overrides": {
                "proteinmpnn": "cr.eu-north1.nebius.cloud/source/fs2-models/proteinmpnn@"
                f"sha256:{'c' * 64}"
            },
        }
    )
    return configuration


def regional_dynamic(run_root: Path) -> dict:
    dynamic = dynamic_outputs(run_root)
    dynamic["registry_delivery_contract"].update(
        {
            "mode": "regional-mirror",
            "repository_prefix": "",
        }
    )
    return dynamic


class InferenceStackTests(unittest.TestCase):
    def test_generated_stage_files_are_deterministic_private_and_secret_free(
        self,
    ) -> None:
        secret_values = {
            "TEST_FS2_GRAFANA_USERNAME": "grafana-user-SENTINEL",
            "TEST_FS2_GRAFANA_PASSWORD": "grafana-password-SENTINEL",
            "TEST_FS2_NGC_API_KEY": "ngc-key-SENTINEL",
            "TEST_FS2_NVCR_DOCKERCONFIGJSON": '{"auths":{"SENTINEL":"value"}}',
        }
        with tempfile.TemporaryDirectory(prefix="inference-stack-files-") as temporary:
            run_root = Path(temporary) / "private-run"
            STACK.private_directory(run_root)
            configuration = contract()
            infrastructure_path = STACK.write_infrastructure_variables(
                run_root, configuration, "b" * 40, "sandbox"
            )
            foundation_path, workloads_path = STACK.write_downstream_variables(
                run_root, configuration, dynamic_outputs(run_root)
            )
            paths = (infrastructure_path, foundation_path, workloads_path)
            first_bytes = {path.name: path.read_bytes() for path in paths}

            with mock.patch.dict(os.environ, secret_values, clear=False):
                foundation_environment = STACK.stage_environment(
                    run_root, "foundation", configuration
                )
                workloads_environment = STACK.stage_environment(
                    run_root, "workloads", configuration
                )

            credentials = json.loads(
                foundation_environment["TF_VAR_bootstrap_grafana_credentials"]
            )
            self.assertEqual(
                credentials["username"], secret_values["TEST_FS2_GRAFANA_USERNAME"]
            )
            self.assertEqual(
                credentials["password"], secret_values["TEST_FS2_GRAFANA_PASSWORD"]
            )
            self.assertEqual(
                workloads_environment["TF_VAR_ngc_api_key"],
                secret_values["TEST_FS2_NGC_API_KEY"],
            )
            self.assertEqual(
                workloads_environment["TF_VAR_nvcrio_dockerconfigjson"],
                secret_values["TEST_FS2_NVCR_DOCKERCONFIGJSON"],
            )

            generated = b"".join(first_bytes.values()).decode("utf-8")
            for value in secret_values.values():
                self.assertNotIn(value, generated)
            self.assertEqual(stat.S_IMODE(run_root.stat().st_mode), 0o700)
            for path in paths:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            STACK.write_infrastructure_variables(
                run_root, configuration, "b" * 40, "sandbox"
            )
            STACK.write_downstream_variables(
                run_root, configuration, dynamic_outputs(run_root)
            )
            self.assertEqual(
                first_bytes, {path.name: path.read_bytes() for path in paths}
            )
            self.assertEqual(list(run_root.glob(".*.tmp-*")), [])

    def test_reference_data_handoff_stays_non_secret_and_exact(self) -> None:
        configuration = contract()
        configuration["stages"]["workloads"]["reference_data"] = {
            "enabled": True,
            "namespace": "fs2-reference-data",
            "queue": {},
            "network": {"allow_public_msa_opt_in": False},
            "status": {},
            "pipeline": {"enabled": True},
        }
        dynamic = dynamic_outputs(Path("/private/run"))
        dynamic["nebius_profile"] = "sandbox"
        dynamic["reference_data_storage_contract"] = {
            "schema": "fs2-serve.nebius.ai/reference-data-storage/v1",
            "project_id": "project-test",
            "region": "us-north1",
            "filesystem": {"host_path": "/mnt/fs2-reference-data/data"},
            "object_storage": {
                "name": "fs2-reference-data-test",
                "endpoint": "https://storage.us-north1.nebius.cloud",
            },
        }
        dynamic["reference_data_object_storage_access"] = {
            "access_key_id": "TESTACCESSKEY",
            "secret_reference_id": "mysteryboxsecret-test",
            "revision": 1,
        }
        with tempfile.TemporaryDirectory(prefix="reference-data-handoff-") as temporary:
            run_root = Path(temporary)
            STACK.private_directory(run_root)
            _foundation, workloads_path = STACK.write_downstream_variables(
                run_root, configuration, dynamic
            )
            generated_text = workloads_path.read_text(encoding="utf-8")
            workloads = json.loads(generated_text)

        self.assertEqual(
            dynamic["reference_data_storage_contract"],
            workloads["reference_data"]["storage_contract"],
        )
        self.assertEqual(
            dynamic["reference_data_object_storage_access"],
            workloads["reference_data"]["object_storage_access"],
        )
        self.assertNotIn("secret-access-key", generated_text)

    def test_clean_environment_cannot_override_generated_stage_inputs(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"TF_VAR_project_id": "wrong-project", "TF_DATA_DIR": "/wrong-data"},
            clear=False,
        ):
            cleaned = STACK.clean_environment()
        self.assertNotIn("TF_VAR_project_id", cleaned)
        self.assertNotIn("TF_DATA_DIR", cleaned)

    def test_admin_baseline_is_derived_from_selected_tfvars_and_live_pool_contract(
        self,
    ) -> None:
        configuration = contract()
        configuration["selected_model_ids"] = ["qwen3-8b"]
        configuration["profiles"]["accelerators"] = "minimal"
        configuration["admin_configuration"] = {
            "enabled": True,
            "source": "derived-terraform-baseline",
        }
        configuration["stages"]["workloads"].update(
            {
                "enabled_model_ids": ["qwen3-8b"],
                "model_image_overrides": {
                    "qwen3-8b": "registry.example.invalid/fs2-models/qwen3-8b@"
                    f"sha256:{'2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635'}"
                },
                "model_pool_overrides": {"qwen3-8b": "h100-1x"},
                "model_scaling_mode": "static",
                "model_scaling_overrides": {},
                "hot_model_ids": [],
                "keda_polling_interval_seconds": 5,
                "keda_cooldown_period_seconds": 300,
            }
        )
        with tempfile.TemporaryDirectory(prefix="inference-stack-admin-") as temporary:
            run_root = Path(temporary)
            dynamic = dynamic_outputs(run_root)
            dynamic["accelerator_pool_contract"] = {
                "schema": "fs2-serve.nebius.ai/terraform-accelerator-pools/v2",
                "pools": {
                    "h100-1x": {
                        "accelerator_class": "nvidia-h100-sxm5-80gb",
                        "capacity": {
                            "type": "preemptible",
                            "min_nodes": 0,
                            "max_nodes": 2,
                        },
                        "node": {"gpus_per_node": 1},
                        "resource_api": {"resource_name": "nvidia.com/gpu"},
                        "features": {
                            "local_cache": "shared-filesystem",
                            "shared_filesystem": True,
                        },
                        "scheduling": {
                            "stable_node_labels": {
                                "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
                                "accelerator.fs2.nebius/pool-id": "h100-1x",
                            },
                            "tolerations": [
                                {
                                    "key": "dedicated",
                                    "operator": "Equal",
                                    "value": "fs2-inference",
                                    "effect": "NoSchedule",
                                }
                            ],
                        },
                    }
                },
            }
            _, workloads_path = STACK.write_downstream_variables(
                run_root,
                configuration,
                dynamic,
            )
            workloads = json.loads(workloads_path.read_text(encoding="utf-8"))

        baseline = workloads["admin_configuration"]
        self.assertEqual(baseline["schema_version"], "fs2.admin-configuration/v1")
        self.assertEqual(list(baseline["models"]), ["qwen3-8b"])
        self.assertEqual(
            baseline["models"]["qwen3-8b"]["placement"]["pool_ids"], ["h100-1x"]
        )
        self.assertEqual(
            baseline["models"]["qwen3-8b"]["artifact"]["image_digest"],
            "sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635",
        )
        self.assertEqual(
            baseline["models"]["qwen3-8b"]["mcp"],
            {"exposed": True, "tool_name": "qwen3_8b_chat"},
        )
        self.assertEqual(
            baseline["pools"]["h100-1x"]["tolerations"],
            [
                {
                    "key": "dedicated",
                    "operator": "Equal",
                    "value": "fs2-inference",
                    "effect": "NoSchedule",
                    "toleration_seconds": None,
                }
            ],
        )
        self.assertEqual(
            workloads["model_scaling_overrides"]["qwen3-8b"]["min_replicas"], 1
        )
        self.assertNotIn("admin_configuration_bootstrap_baseline_accepted", workloads)
        self.assertEqual(
            workloads["admin_configuration_sha256"],
            STACK.canonical_sha256(baseline),
        )

    def test_public_grafana_origin_and_allowlist_come_from_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="inference-stack-grafana-"
        ) as temporary:
            run_root = Path(temporary)
            configuration = contract()
            configuration["stages"]["foundation"]["grafana_publication"] = {
                "enabled": True,
                "external_base_url": "",
            }
            dynamic = dynamic_outputs(run_root)
            dynamic["public_edge_contract"] = {
                "mode": "public",
                "public_origin": "https://192.0.2.20",
            }
            foundation_path, workloads_path = STACK.write_downstream_variables(
                run_root, configuration, dynamic
            )

            foundation = json.loads(foundation_path.read_text(encoding="utf-8"))
            workloads = json.loads(workloads_path.read_text(encoding="utf-8"))
            self.assertEqual(
                foundation["grafana_publication"]["external_base_url"],
                "https://192.0.2.20",
            )
            self.assertEqual(
                workloads["admin_observability_links"],
                {
                    "allowed_hosts": ["192.0.2.20"],
                    "grafana": {
                        "url": "https://192.0.2.20/admin/observability/grafana",
                        "verified_external_route": True,
                    },
                    "prometheus": {
                        "url": "https://192.0.2.20/admin/observability/grafana/explore",
                        "verified_external_route": True,
                    },
                    "loki": {
                        "url": "https://192.0.2.20/admin/observability/grafana/explore",
                        "verified_external_route": True,
                    },
                    "otel": {
                        "url": "https://192.0.2.20/admin/observability/grafana/dashboards",
                        "verified_external_route": True,
                    },
                    "dcgm": {
                        "url": "https://192.0.2.20/admin/observability/grafana/dashboards",
                        "verified_external_route": True,
                    },
                    "kueue": {
                        "url": "https://192.0.2.20/admin/observability/grafana/dashboards",
                        "verified_external_route": True,
                    },
                    "keda": {
                        "url": "https://192.0.2.20/admin/observability/grafana/dashboards",
                        "verified_external_route": True,
                    },
                    "alertmanager": {"url": "", "verified_external_route": False},
                    "tempo": {"url": "", "verified_external_route": False},
                },
            )

    def test_workload_secrets_follow_selected_artifact_requirements(self) -> None:
        configuration = contract()
        configuration["secret_requirements"]["ngc_api_key"] = False
        configuration["secret_requirements"]["nvcr_dockerconfig"] = False
        with tempfile.TemporaryDirectory(
            prefix="inference-stack-secrets-"
        ) as temporary:
            with mock.patch.dict(
                os.environ,
                {
                    "TEST_FS2_NGC_API_KEY": "",
                    "TEST_FS2_NVCR_DOCKERCONFIGJSON": "",
                },
                clear=False,
            ):
                environment = STACK.stage_environment(
                    Path(temporary), "workloads", configuration
                )
        self.assertNotIn("TF_VAR_ngc_api_key", environment)
        self.assertNotIn("TF_VAR_nvcrio_dockerconfigjson", environment)

    def test_capacity_block_preflight_is_repeatable_for_an_allocated_block(
        self,
    ) -> None:
        configuration = contract()
        configuration["target"]["region"] = "eu-north1"
        configuration["stages"]["infrastructure"].update(
            {
                "kubernetes_version": "1.35",
                "custom_accelerator_pools": {
                    "h100-reserved-8x": {
                        "platform": "gpu-h100-sxm",
                        "preset": "8gpu-128vcpu-1600gb",
                        "gpus_per_node": 8,
                        "capacity_type": "regular",
                        "max_nodes": 2,
                        "os": "ubuntu24.04",
                        "driver": {"mode": "managed", "preset": "cuda13.0"},
                        "topology": {"mode": "standalone"},
                        "reservation_policy": {
                            "policy": "STRICT",
                            "reservation_ids": ["capacityblockgroup-testreservation"],
                        },
                    }
                },
            }
        )

        def fake_run(command, **_kwargs):
            if "capacity-block-group" in command:
                payload = {
                    "metadata": {
                        "id": "capacityblockgroup-testreservation",
                        "parent_id": "tenant-test",
                    },
                    "status": {
                        "state": "STATE_ACTIVE",
                        "region": "eu-north1",
                        "resource_affinity": {
                            "compute_v1": {"platform": "gpu-h100-sxm"}
                        },
                        "current_limit": "16",
                        "usage": "16",
                        "usage_percentage": "1.00",
                        "current_continuous_interval": {
                            "end_time": "2027-03-01T00:00:00Z"
                        },
                    },
                }
            elif "get-by-name" in command:
                payload = {
                    "spec": {
                        "presets": [
                            {
                                "name": "8gpu-128vcpu-1600gb",
                                "resources": {"gpu_count": 8},
                            }
                        ]
                    },
                    "status": {"allowed_for_preemptibles": True},
                }
            else:
                payload = {
                    "versions": [
                        {
                            "kubernetes_version": "1.35",
                            "items": [
                                {
                                    "compatible_platforms": ["gpu-h100-sxm"],
                                    "os": "ubuntu24.04",
                                    "drivers_preset": "cuda13.0",
                                }
                            ],
                        }
                    ]
                }
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(payload), stderr=""
            )

        output = io.StringIO()
        with (
            mock.patch.object(STACK, "run", side_effect=fake_run),
            redirect_stdout(output),
        ):
            STACK.preflight_accelerators(arguments(), configuration)

        evidence = json.loads(output.getvalue())
        reservation = evidence["accelerator_pools"][0]["reservation"]
        self.assertEqual(reservation["required_gpu_units"], 16)
        self.assertEqual(reservation["total_gpu_units"], 16.0)
        self.assertEqual(reservation["available_gpu_units"], 0.0)
        self.assertEqual(
            reservation["groups"][0]["id"],
            "capacityblockgroup-testreservation",
        )

    def test_capacity_block_preflight_rejects_wrong_region(self) -> None:
        configuration = contract()
        configuration["target"]["region"] = "eu-north1"
        configuration["stages"]["infrastructure"].update(
            {
                "kubernetes_version": "1.35",
                "custom_accelerator_pools": {
                    "h100-reserved-8x": {
                        "platform": "gpu-h100-sxm",
                        "preset": "8gpu-128vcpu-1600gb",
                        "gpus_per_node": 8,
                        "capacity_type": "regular",
                        "max_nodes": 2,
                        "os": "ubuntu24.04",
                        "driver": {"mode": "managed", "preset": "cuda13.0"},
                        "topology": {"mode": "standalone"},
                        "reservation_policy": {
                            "policy": "STRICT",
                            "reservation_ids": ["capacityblockgroup-testreservation"],
                        },
                    }
                },
            }
        )

        def fake_run(command, **_kwargs):
            if "capacity-block-group" in command:
                payload = {
                    "metadata": {
                        "id": "capacityblockgroup-testreservation",
                        "parent_id": "tenant-test",
                    },
                    "status": {
                        "state": "STATE_ACTIVE",
                        "region": "eu-west1",
                        "resource_affinity": {
                            "compute_v1": {"platform": "gpu-h100-sxm"}
                        },
                        "current_limit": "16",
                        "usage": "0",
                        "usage_percentage": "0.00",
                    },
                }
            elif "get-by-name" in command:
                payload = {
                    "spec": {
                        "presets": [
                            {
                                "name": "8gpu-128vcpu-1600gb",
                                "resources": {"gpu_count": 8},
                            }
                        ]
                    }
                }
            else:
                payload = {
                    "versions": [
                        {
                            "kubernetes_version": "1.35",
                            "items": [
                                {
                                    "compatible_platforms": ["gpu-h100-sxm"],
                                    "os": "ubuntu24.04",
                                    "drivers_preset": "cuda13.0",
                                }
                            ],
                        }
                    ]
                }
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(payload), stderr=""
            )

        with mock.patch.object(STACK, "run", side_effect=fake_run):
            with self.assertRaisesRegex(
                STACK.DeploymentError, "not an active eu-north1/gpu-h100-sxm"
            ):
                STACK.preflight_accelerators(arguments(), configuration)

    def test_nebius_profile_is_used_for_kubeconfig_retrieval(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory(
            prefix="inference-stack-profile-"
        ) as temporary:
            run_root = Path(temporary)

            def fake_run(arguments, **_kwargs):
                calls.append(list(arguments))
                if arguments[0] == "nebius-test":
                    (run_root / "kubeconfig").write_text("test", encoding="utf-8")
                    return subprocess.CompletedProcess(arguments, 0, stdout="")
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout="11111111-2222-3333-4444-555555555555",
                )

            with mock.patch.object(STACK, "run", side_effect=fake_run):
                STACK.ensure_kubeconfig(
                    nebius="nebius-test",
                    nebius_profile="explicit-profile",
                    kubectl="kubectl-test",
                    run_root=run_root,
                    cluster_id="mk8scluster-test",
                    cluster_name="fs2-wrapper-test",
                )

        self.assertEqual(
            calls[0][:5],
            [
                "nebius-test",
                "--profile",
                "explicit-profile",
                "mk8s",
                "cluster",
            ],
        )

    def test_infrastructure_outputs_accepts_omitted_legacy_contract(self) -> None:
        configuration = contract()
        required_outputs = {
            "cluster_id": "mk8scluster-test",
            "cluster_name": "fs2-wrapper-test",
            "target_contract": {
                "schema": "target-test/v1",
                "source_registry": {
                    "id": "registry-test",
                    "project_id": "project-test",
                    "fqdn": "cr.us-north1.nebius.cloud",
                },
            },
            "accelerator_pool_contract": {"schema": "accelerators-test/v2"},
            "public_edge_contract": {"mode": "internal-only"},
        }
        terraform_outputs = {
            name: {"sensitive": False, "value": value}
            for name, value in required_outputs.items()
        }

        with tempfile.TemporaryDirectory(
            prefix="inference-stack-contract-"
        ) as temporary:
            run_root = Path(temporary)
            with (
                mock.patch.object(STACK, "terraform_init"),
                mock.patch.object(
                    STACK,
                    "terraform_json_output",
                    side_effect=lambda _terraform,
                    _root,
                    name,
                    _environment: required_outputs[name],
                ),
                mock.patch.object(
                    STACK,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, stdout=json.dumps(terraform_outputs), stderr=""
                    ),
                ) as terraform_output,
                mock.patch.object(
                    STACK,
                    "ensure_kubeconfig",
                    return_value=(
                        run_root / "kubeconfig",
                        "11111111-2222-3333-4444-555555555555",
                    ),
                ),
            ):
                outputs = STACK.infrastructure_outputs(
                    "terraform-test",
                    run_root,
                    configuration,
                    nebius="nebius-test",
                    nebius_profile="sandbox",
                    kubectl="kubectl-test",
                )

            self.assertIsNone(outputs["infrastructure_contract"])
            self.assertEqual(
                outputs["accelerator_pool_contract"],
                required_outputs["accelerator_pool_contract"],
            )
            self.assertEqual(
                outputs["registry_delivery_contract"]["target_registry"][
                    "repository_root"
                ],
                "cr.us-north1.nebius.cloud/test",
            )
            self.assertEqual(terraform_output.call_count, 2)

    def test_stage_readiness_requires_completion_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inference-stack-ready-") as temporary:
            run_root = Path(temporary)
            (run_root / "terraform.tfstate").touch()
            with (
                mock.patch.object(STACK, "terraform_init"),
                mock.patch.object(STACK, "stage_environment", return_value={}),
                mock.patch.object(STACK.subprocess, "run") as terraform_output,
            ):
                terraform_output.return_value = subprocess.CompletedProcess(
                    [], 1, stdout="", stderr="missing output"
                )
                self.assertFalse(
                    STACK.state_ready(
                        "terraform-test", run_root, "infrastructure", contract()
                    )
                )
                terraform_output.return_value = subprocess.CompletedProcess(
                    [], 0, stdout='{"schema":"ready/v1"}', stderr=""
                )
                self.assertTrue(
                    STACK.state_ready(
                        "terraform-test", run_root, "infrastructure", contract()
                    )
                )

    def test_clean_environment_removes_workspace_and_cli_argument_overrides(
        self,
    ) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "TF_WORKSPACE": "wrong-workspace",
                "TF_CLI_ARGS": "-destroy",
                "TF_CLI_ARGS_plan": "-refresh=false",
            },
            clear=False,
        ):
            cleaned = STACK.clean_environment()
        self.assertNotIn("TF_WORKSPACE", cleaned)
        self.assertNotIn("TF_CLI_ARGS", cleaned)
        self.assertNotIn("TF_CLI_ARGS_plan", cleaned)

    def test_apply_converges_stages_then_records_root_configuration(self) -> None:
        run_root = Path("/private/test-run")
        planned_stages: list[str] = []
        endpoint_values = {
            "mcp_endpoint_url": "https://192.0.2.10/mcp",
            "admin_web_interface_url": "https://192.0.2.10/admin/",
            "inference_base_url": "https://192.0.2.10/v1",
            "grafana_url": "https://192.0.2.10/admin/observability/grafana",
        }

        def fake_plan(*_args, **kwargs):
            stage = kwargs["stage"]
            planned_stages.append(stage)
            return Path(f"/{stage}.tfplan"), {}, {"stage": stage}

        def fake_endpoint_output(_terraform, _root, name, _environment):
            return endpoint_values[name]

        output = io.StringIO()
        with (
            mock.patch.object(
                STACK,
                "write_infrastructure_variables",
                return_value=Path("/infra.json"),
            ),
            mock.patch.object(STACK, "plan_stage", side_effect=fake_plan),
            mock.patch.object(STACK, "apply_plan") as apply_plan,
            mock.patch.object(
                STACK,
                "infrastructure_outputs",
                return_value=dynamic_outputs(run_root),
            ),
            mock.patch.object(
                STACK,
                "write_downstream_variables",
                return_value=(Path("/foundation.json"), Path("/workloads.json")),
            ),
            mock.patch.object(STACK, "mirror_selected_images") as mirror_images,
            mock.patch.object(
                STACK,
                "terraform_json_output",
                side_effect=fake_endpoint_output,
            ),
            mock.patch.object(
                STACK,
                "terraform_optional_json_output",
                return_value=endpoint_values["grafana_url"],
            ),
            mock.patch.object(STACK, "stage_environment", return_value={}),
            redirect_stdout(output),
        ):
            STACK.apply_stack(
                arguments(),
                run_root,
                contract(),
                "c" * 40,
                {"stage": "configuration"},
            )

        self.assertEqual(planned_stages, ["infrastructure", "foundation", "workloads"])
        self.assertEqual(
            [call.args[1] for call in apply_plan.call_args_list],
            [
                STACK.INFRA_ROOT,
                STACK.FOUNDATION_ROOT,
                STACK.WORKLOADS_ROOT,
                STACK.DEPLOY_ROOT,
            ],
        )
        self.assertEqual(
            apply_plan.call_args_list[-1].args[2],
            run_root / "configuration.tfplan",
        )
        self.assertEqual(
            apply_plan.call_args_list[-1].args[3],
            {"stage": "configuration"},
        )
        self.assertEqual(
            json.loads(output.getvalue()), {"status": "applied", **endpoint_values}
        )
        mirror_images.assert_called_once_with(
            mock.ANY, run_root, mock.ANY, dynamic_outputs(run_root)
        )

    def test_status_emits_all_non_secret_workload_endpoints(self) -> None:
        endpoint_values = {
            "mcp_endpoint_url": "https://192.0.2.11/mcp",
            "admin_web_interface_url": "https://192.0.2.11/admin/",
            "inference_base_url": "https://192.0.2.11/v1",
            "grafana_url": "https://192.0.2.11/admin/observability/grafana",
        }

        def fake_endpoint_output(_terraform, _root, name, _environment):
            return endpoint_values[name]

        output = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="inference-stack-status-") as temporary:
            run_root = Path(temporary)
            (run_root / "workloads.tfstate").touch()
            with (
                mock.patch.object(STACK, "terraform_init"),
                mock.patch.object(
                    STACK, "state_addresses", return_value=["resource.ready"]
                ),
                mock.patch.object(STACK, "state_ready", return_value=True),
                mock.patch.object(
                    STACK, "stage_environment", return_value={}
                ) as stage_environment,
                mock.patch.object(
                    STACK,
                    "terraform_json_output",
                    side_effect=fake_endpoint_output,
                ) as terraform_json_output,
                mock.patch.object(
                    STACK,
                    "terraform_optional_json_output",
                    return_value=endpoint_values["grafana_url"],
                ) as terraform_optional_json_output,
                redirect_stdout(output),
            ):
                STACK.status_stack(arguments(), run_root, contract())

        payload = json.loads(output.getvalue())
        self.assertEqual(
            {name: payload[name] for name in endpoint_values}, endpoint_values
        )
        self.assertEqual(
            [call.args[2] for call in terraform_json_output.call_args_list],
            [
                "mcp_endpoint_url",
                "admin_web_interface_url",
                "inference_base_url",
            ],
        )
        self.assertEqual(
            [call.args[2] for call in terraform_optional_json_output.call_args_list],
            ["grafana_url"],
        )
        self.assertTrue(
            all(
                call.kwargs.get("include_secrets") is False
                for call in stage_environment.call_args_list
            )
        )

    def test_endpoint_output_allows_unpublished_grafana(self) -> None:
        required = {
            "mcp_endpoint_url": "https://192.0.2.11/mcp",
            "admin_web_interface_url": "https://192.0.2.11/admin/",
            "inference_base_url": "https://192.0.2.11/v1",
        }
        with (
            mock.patch.object(STACK, "stage_environment", return_value={}),
            mock.patch.object(
                STACK,
                "terraform_json_output",
                side_effect=lambda _terraform, _root, name, _environment: required[
                    name
                ],
            ),
            mock.patch.object(
                STACK, "terraform_optional_json_output", return_value=None
            ),
        ):
            self.assertEqual(
                STACK.workload_endpoint_outputs(
                    "terraform-test", Path("/private/test-run"), contract()
                ),
                {**required, "grafana_url": None},
            )

    def test_output_explicitly_emits_the_sensitive_access_bundle(self) -> None:
        access_bundle = complete_access_bundle()
        output = io.StringIO()
        with (
            mock.patch.object(STACK, "state_ready", return_value=True),
            mock.patch.object(
                STACK,
                "workload_access_bundle",
                return_value=access_bundle,
            ) as workload_access_bundle,
            redirect_stdout(output),
        ):
            STACK.output_stack(arguments(), Path("/private/test-run"), contract())

        self.assertEqual(json.loads(output.getvalue()), access_bundle)
        workload_access_bundle.assert_called_once_with(
            "terraform-test", Path("/private/test-run"), contract()
        )

    def test_access_bundle_validation_requires_requested_connection_fields(
        self,
    ) -> None:
        bundle = complete_access_bundle()
        with (
            mock.patch.object(STACK, "stage_environment", return_value={}),
            mock.patch.object(
                STACK,
                "terraform_json_output",
                return_value=bundle,
            ),
        ):
            self.assertEqual(
                STACK.workload_access_bundle(
                    "terraform-test", Path("/private/test-run"), contract()
                ),
                bundle,
            )

    def test_access_bundle_validation_rejects_missing_connection_fields(
        self,
    ) -> None:
        required_paths = (
            ("cluster", "kubeconfig_command"),
            ("endpoints", "admin_portal_url"),
            ("endpoints", "mcp_url"),
            ("endpoints", "inference_base_url"),
            ("endpoints", "grafana_url"),
            ("credentials", "admin_bootstrap_token"),
            ("credentials", "mcp_inference_token"),
            ("credentials", "inference_access_token"),
            ("credentials", "grafana", "username"),
            ("credentials", "grafana", "password"),
            ("mcp_access", "scopes"),
        )
        for path in required_paths:
            with self.subTest(path=path):
                bundle = complete_access_bundle()
                parent = bundle
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                with (
                    mock.patch.object(STACK, "stage_environment", return_value={}),
                    mock.patch.object(
                        STACK,
                        "terraform_json_output",
                        return_value=bundle,
                    ),
                    self.assertRaisesRegex(
                        STACK.DeploymentError, "missing or malformed"
                    ),
                ):
                    STACK.workload_access_bundle(
                        "terraform-test", Path("/private/test-run"), contract()
                    )

    def test_access_bundle_validation_requires_shared_token_alias(self) -> None:
        bundle = complete_access_bundle()
        bundle["credentials"]["inference_access_token"] = "different-token"
        with (
            mock.patch.object(STACK, "stage_environment", return_value={}),
            mock.patch.object(
                STACK,
                "terraform_json_output",
                return_value=bundle,
            ),
            self.assertRaisesRegex(STACK.DeploymentError, "missing or malformed"),
        ):
            STACK.workload_access_bundle(
                "terraform-test", Path("/private/test-run"), contract()
            )

    def test_output_rejects_an_incomplete_workloads_stage(self) -> None:
        with (
            mock.patch.object(STACK, "state_ready", return_value=False),
            mock.patch.object(STACK, "workload_access_bundle") as access_bundle,
        ):
            with self.assertRaisesRegex(
                STACK.DeploymentError,
                "run inference-stack apply",
            ):
                STACK.output_stack(arguments(), Path("/private/test-run"), contract())
        access_bundle.assert_not_called()

    def test_internal_proxy_command_uses_only_terraform_owned_runtime_contract(
        self,
    ) -> None:
        run_root = Path("/private/test-run")
        port_forward = {
            "enabled": True,
            "bind_address": "127.0.0.1",
            "control_plane_service": "fs2-serve-control-plane",
            "control_plane_port": 8080,
            "control_plane_local_port": 28080,
            "admin_console_service": "fs2-serve-control-plane-admin-console",
            "admin_console_port": 8080,
            "admin_console_local_port": 28081,
            "operator_proxy_port": 28082,
            "application_origin": "http://localhost:28082",
            "operator_endpoint": "http://127.0.0.1:28082",
        }
        outputs = {
            "cluster_id": "mk8scluster-test",
            "cluster_name": "k8s-inference-test",
            "public_edge_contract": {
                "mode": "internal-only",
                "port_forward": port_forward,
            },
        }
        with (
            mock.patch.object(STACK, "state_ready", return_value=True),
            mock.patch.object(STACK, "stage_environment", return_value={}),
            mock.patch.object(STACK, "terraform_init"),
            mock.patch.object(
                STACK,
                "terraform_json_output",
                side_effect=lambda _terraform, _root, name, _environment: outputs[name],
            ),
            mock.patch.object(
                STACK,
                "ensure_kubeconfig",
                return_value=(run_root / "kubeconfig", "namespace-uid"),
            ),
            mock.patch.object(
                STACK,
                "workload_endpoint_outputs",
                return_value={
                    "mcp_endpoint_url": "http://localhost:28082/mcp",
                    "admin_web_interface_url": "http://localhost:28082/admin/",
                },
            ),
        ):
            command = STACK.internal_proxy_command(arguments(), run_root, contract())

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], str(STACK.INTERNAL_EDGE_PROXY))
        self.assertIn("k8s-inference-test", command)
        self.assertIn("--control-plane-local-port", command)
        self.assertIn("28080", command)
        self.assertIn("--admin-console-local-port", command)
        self.assertIn("28081", command)
        self.assertIn("--operator-proxy-port", command)
        self.assertIn("28082", command)
        self.assertEqual(
            command[-4:],
            [
                "--mcp-endpoint-url",
                "http://localhost:28082/mcp",
                "--admin-web-interface-url",
                "http://localhost:28082/admin/",
            ],
        )
        self.assertNotIn("admin_token", " ".join(command))

    def test_internal_proxy_rejects_public_edge_contract(self) -> None:
        with (
            mock.patch.object(STACK, "state_ready", return_value=True),
            mock.patch.object(STACK, "stage_environment", return_value={}),
            mock.patch.object(STACK, "terraform_init"),
            mock.patch.object(
                STACK,
                "terraform_json_output",
                side_effect=(
                    "mk8scluster-test",
                    "k8s-inference-test",
                    {"mode": "public"},
                ),
            ),
            mock.patch.object(
                STACK,
                "ensure_kubeconfig",
                return_value=(Path("/private/test-run/kubeconfig"), "namespace-uid"),
            ),
            self.assertRaisesRegex(STACK.DeploymentError, "public edge"),
        ):
            STACK.internal_proxy_command(
                arguments(), Path("/private/test-run"), contract()
            )

    def test_plan_resumes_at_the_first_stage_without_state(self) -> None:
        scenarios = (
            (
                {"infrastructure": False, "foundation": False, "workloads": False},
                ["infrastructure"],
            ),
            (
                {"infrastructure": True, "foundation": False, "workloads": False},
                ["infrastructure", "foundation"],
            ),
            (
                {"infrastructure": True, "foundation": True, "workloads": False},
                ["infrastructure", "foundation", "workloads"],
            ),
            (
                {"infrastructure": True, "foundation": True, "workloads": True},
                ["infrastructure", "foundation", "workloads"],
            ),
        )
        run_root = Path("/private/test-run")

        def no_op_plan(*_args, **kwargs):
            return (
                Path(f"/{kwargs['stage']}.tfplan"),
                {
                    "resource_changes": [
                        {"mode": "managed", "change": {"actions": ["no-op"]}}
                    ],
                    "output_changes": {"contract": {"actions": ["no-op"]}},
                },
                {},
            )

        for readiness, expected_stages in scenarios:
            with self.subTest(readiness=readiness):
                with (
                    mock.patch.object(
                        STACK,
                        "write_infrastructure_variables",
                        return_value=Path("/infra.json"),
                    ),
                    mock.patch.object(
                        STACK,
                        "state_ready",
                        side_effect=lambda _terraform,
                        _root,
                        stage,
                        _contract: readiness[stage],
                    ),
                    mock.patch.object(
                        STACK,
                        "infrastructure_outputs",
                        return_value=dynamic_outputs(run_root),
                    ),
                    mock.patch.object(
                        STACK,
                        "write_downstream_variables",
                        return_value=(
                            Path("/foundation.json"),
                            Path("/workloads.json"),
                        ),
                    ),
                    mock.patch.object(
                        STACK, "plan_stage", side_effect=no_op_plan
                    ) as plan_stage,
                    mock.patch.object(STACK, "mirror_selected_images") as mirror_images,
                    redirect_stdout(io.StringIO()),
                ):
                    STACK.plan_stack(arguments(), run_root, contract(), "d" * 40)
                self.assertEqual(
                    [call.kwargs["stage"] for call in plan_stage.call_args_list],
                    expected_stages,
                )
                mirror_images.assert_not_called()

    def test_existing_states_stop_after_changed_infrastructure_plan(self) -> None:
        run_root = Path("/private/test-run")
        changed_documents = (
            {
                "resource_changes": [
                    {"mode": "managed", "change": {"actions": ["update"]}}
                ],
                "output_changes": {},
            },
            {
                "resource_changes": [{"mode": "data", "change": {"actions": ["read"]}}],
                "output_changes": {
                    "accelerator_pool_contract": {"actions": ["update"]}
                },
            },
        )
        for changed in changed_documents:
            for foundation_ready in (False, True):
                with self.subTest(changed=changed, foundation_ready=foundation_ready):
                    readiness = {
                        "infrastructure": True,
                        "foundation": foundation_ready,
                        "workloads": foundation_ready,
                    }
                    with (
                        mock.patch.object(
                            STACK,
                            "write_infrastructure_variables",
                            return_value=Path("/infra.json"),
                        ),
                        mock.patch.object(
                            STACK,
                            "state_ready",
                            side_effect=lambda _terraform,
                            _root,
                            stage,
                            _contract: readiness[stage],
                        ),
                        mock.patch.object(
                            STACK,
                            "infrastructure_outputs",
                            return_value=dynamic_outputs(run_root),
                        ) as infrastructure_outputs,
                        mock.patch.object(
                            STACK,
                            "write_downstream_variables",
                            return_value=(
                                Path("/foundation.json"),
                                Path("/workloads.json"),
                            ),
                        ) as downstream,
                        mock.patch.object(
                            STACK,
                            "plan_stage",
                            return_value=(Path("/infra.tfplan"), changed, {}),
                        ) as plan_stage,
                        mock.patch.object(
                            STACK, "mirror_selected_images"
                        ) as mirror_images,
                        redirect_stdout(io.StringIO()),
                    ):
                        STACK.plan_stack(arguments(), run_root, contract(), "d" * 40)
                self.assertEqual(
                    [call.kwargs["stage"] for call in plan_stage.call_args_list],
                    ["infrastructure"],
                )
                infrastructure_outputs.assert_not_called()
                downstream.assert_not_called()
                mirror_images.assert_not_called()

    def test_existing_states_stop_after_changed_foundation_plan(self) -> None:
        run_root = Path("/private/test-run")
        no_op = {
            "resource_changes": [{"mode": "managed", "change": {"actions": ["no-op"]}}],
            "output_changes": {"contract": {"actions": ["no-op"]}},
        }
        changed_foundation = {
            "resource_changes": [],
            "output_changes": {"cluster_contract": {"actions": ["update"]}},
        }

        def staged_plan(*_args, **kwargs):
            document = (
                no_op if kwargs["stage"] == "infrastructure" else changed_foundation
            )
            return Path(f"/{kwargs['stage']}.tfplan"), document, {}

        for workloads_ready in (False, True):
            with self.subTest(workloads_ready=workloads_ready):
                readiness = {
                    "infrastructure": True,
                    "foundation": True,
                    "workloads": workloads_ready,
                }
                with (
                    mock.patch.object(
                        STACK,
                        "write_infrastructure_variables",
                        return_value=Path("/infra.json"),
                    ),
                    mock.patch.object(
                        STACK,
                        "state_ready",
                        side_effect=lambda _terraform,
                        _root,
                        stage,
                        _contract: readiness[stage],
                    ),
                    mock.patch.object(
                        STACK,
                        "infrastructure_outputs",
                        return_value=dynamic_outputs(run_root),
                    ),
                    mock.patch.object(
                        STACK,
                        "write_downstream_variables",
                        return_value=(
                            Path("/foundation.json"),
                            Path("/workloads.json"),
                        ),
                    ),
                    mock.patch.object(
                        STACK, "plan_stage", side_effect=staged_plan
                    ) as plan_stage,
                    mock.patch.object(STACK, "mirror_selected_images") as mirror_images,
                    redirect_stdout(io.StringIO()),
                ):
                    STACK.plan_stack(arguments(), run_root, contract(), "d" * 40)
                self.assertEqual(
                    [call.kwargs["stage"] for call in plan_stage.call_args_list],
                    ["infrastructure", "foundation"],
                )
                mirror_images.assert_not_called()

    def test_destroy_plans_and_applies_in_reverse_dependency_order(self) -> None:
        run_root = Path("/private/test-run")
        planned_stages: list[str] = []
        destroy_flags: list[bool] = []

        def fake_plan(*_args, **kwargs):
            stage = kwargs["stage"]
            planned_stages.append(stage)
            destroy_flags.append(kwargs["destroy"])
            return Path(f"/{stage}-destroy.tfplan"), {}, {"stage": stage}

        with (
            mock.patch.object(
                STACK,
                "write_infrastructure_variables",
                return_value=Path("/infra.json"),
            ),
            mock.patch.object(STACK, "state_has_resources", return_value=True),
            mock.patch.object(STACK, "state_ready", return_value=True),
            mock.patch.object(
                STACK,
                "infrastructure_outputs",
                return_value=dynamic_outputs(run_root),
            ),
            mock.patch.object(
                STACK,
                "write_downstream_variables",
                return_value=(Path("/foundation.json"), Path("/workloads.json")),
            ),
            mock.patch.object(STACK, "plan_stage", side_effect=fake_plan),
            mock.patch.object(STACK, "apply_plan") as apply_plan,
            mock.patch.object(STACK, "mirror_selected_images") as mirror_images,
            redirect_stdout(io.StringIO()),
        ):
            STACK.destroy_stack(arguments(), run_root, contract(), "e" * 40)

        self.assertEqual(planned_stages, ["workloads", "foundation", "infrastructure"])
        self.assertEqual(destroy_flags, [True, True, True])
        self.assertEqual(
            [call.args[1] for call in apply_plan.call_args_list],
            [STACK.WORKLOADS_ROOT, STACK.FOUNDATION_ROOT, STACK.INFRA_ROOT],
        )
        mirror_images.assert_not_called()

    def test_destroy_reuses_legacy_cached_downstream_inputs_without_mirroring(
        self,
    ) -> None:
        planned_files: list[Path] = []

        def fake_plan(*_args, **kwargs):
            planned_files.append(kwargs["variable_file"])
            return Path(f"/{kwargs['stage']}-destroy.tfplan"), {}, {}

        with tempfile.TemporaryDirectory(prefix="fs2-legacy-destroy-") as temporary:
            run_root = Path(temporary)
            foundation = run_root / "foundation.tfvars.json"
            workloads = run_root / "workloads.tfvars.json"
            STACK.private_json(
                foundation,
                {"accelerator_pool_contract": {"artifact_source": {"legacy": True}}},
            )
            STACK.private_json(
                workloads,
                {"accelerator_pool_contract": {"artifact_source": {"legacy": True}}},
            )
            with (
                mock.patch.object(
                    STACK,
                    "write_infrastructure_variables",
                    return_value=run_root / "infrastructure.tfvars.json",
                ),
                mock.patch.object(STACK, "state_has_resources", return_value=True),
                mock.patch.object(STACK, "state_ready") as readiness,
                mock.patch.object(STACK, "infrastructure_outputs") as infrastructure,
                mock.patch.object(STACK, "write_downstream_variables") as downstream,
                mock.patch.object(STACK, "plan_stage", side_effect=fake_plan),
                mock.patch.object(STACK, "apply_plan"),
                mock.patch.object(STACK, "mirror_selected_images") as mirror_images,
                redirect_stdout(io.StringIO()),
            ):
                STACK.destroy_stack(arguments(), run_root, contract(), "e" * 40)
        self.assertEqual(
            planned_files[:2],
            [workloads, foundation],
        )
        readiness.assert_not_called()
        infrastructure.assert_not_called()
        downstream.assert_not_called()
        mirror_images.assert_not_called()

    def test_regional_mirror_rewrites_every_selected_image_without_changing_digest(
        self,
    ) -> None:
        workloads = STACK.rewritten_workloads(
            regional_contract(), regional_dynamic(Path("/private/test-run"))
        )
        target_root = "cr.us-north1.nebius.cloud/test"
        self.assertEqual(
            workloads["control_plane_image"],
            {
                "repository": f"{target_root}/fs2-platform/control-plane",
                "digest": f"sha256:{'a' * 64}",
            },
        )
        self.assertEqual(
            workloads["model_image_overrides"]["proteinmpnn"],
            f"{target_root}/fs2-models/proteinmpnn@sha256:{'c' * 64}",
        )

    def test_regional_mirror_rejects_tag_only_model_source(self) -> None:
        configuration = regional_contract()
        configuration["stages"]["workloads"]["model_image_overrides"]["proteinmpnn"] = (
            "nvcr.io/nim/ipd/proteinmpnn:latest"
        )
        with self.assertRaisesRegex(STACK.DeploymentError, "repository@sha256"):
            STACK.require_deployable_images(configuration)

    def test_existing_verified_mirrors_are_skipped_and_use_explicit_profile(
        self,
    ) -> None:
        configuration = regional_contract()
        expected_digests = {f"sha256:{character * 64}" for character in ("a", "b", "c")}

        def digest_from_reference(_crane, reference, environment, **_kwargs):
            self.assertEqual(environment["NEBIUS_PROFILE"], "explicit-profile")
            digest = f"sha256:{reference[-64:]}"
            self.assertIn(digest, expected_digests)
            return digest

        with tempfile.TemporaryDirectory(prefix="fs2-mirror-test-") as temporary:
            with (
                mock.patch.object(
                    STACK, "crane_digest", side_effect=digest_from_reference
                ) as digest_lookup,
                mock.patch.object(STACK, "crane_copy") as copy_image,
            ):
                for _ in range(2):
                    receipt = STACK.mirror_selected_images(
                        Namespace(
                            crane="crane-test", nebius_profile="explicit-profile"
                        ),
                        Path(temporary),
                        configuration,
                        regional_dynamic(Path(temporary)),
                    )
            self.assertIsNotNone(receipt)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(
                {image["status"] for image in payload["images"]}, {"existing"}
            )
            copy_image.assert_not_called()
            self.assertFalse(
                any(
                    call.args[1].startswith("cr.eu-north1.nebius.cloud/")
                    for call in digest_lookup.call_args_list
                )
            )

    def test_missing_images_are_copied_then_verified(self) -> None:
        configuration = regional_contract()
        configuration["selected_model_ids"] = []
        tag_lookups: dict[str, int] = {}

        def digest_result(_crane, reference, _environment, **_kwargs):
            expected = f"sha256:{reference[-64:]}"
            if ":mirror-sha256-" in reference:
                count = tag_lookups.get(reference, 0)
                tag_lookups[reference] = count + 1
                return None if count == 0 else expected
            return expected

        with tempfile.TemporaryDirectory(prefix="fs2-mirror-copy-") as temporary:
            receipt_path = Path(temporary) / "registry-mirror.receipt.json"
            with (
                mock.patch.object(STACK, "crane_digest", side_effect=digest_result),
                mock.patch.object(STACK, "crane_copy") as copy_image,
            ):
                STACK.mirror_selected_images(
                    Namespace(crane="crane-test", nebius_profile="explicit-profile"),
                    Path(temporary),
                    configuration,
                    regional_dynamic(Path(temporary)),
                )
            self.assertEqual(copy_image.call_count, 2)
            self.assertTrue(receipt_path.is_file())
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {image["status"] for image in payload["images"]}, {"copied"}
            )

    def test_post_copy_digest_mismatch_aborts_without_receipt_or_downstream_plan(
        self,
    ) -> None:
        configuration = regional_contract()
        configuration["selected_model_ids"] = []
        tag_calls = 0

        def mismatched_digest(_crane, reference, _environment, **_kwargs):
            nonlocal tag_calls
            if ":mirror-sha256-" in reference:
                tag_calls += 1
                return None if tag_calls == 1 else f"sha256:{'f' * 64}"
            return f"sha256:{reference[-64:]}"

        with tempfile.TemporaryDirectory(prefix="fs2-mirror-mismatch-") as temporary:
            receipt_path = Path(temporary) / "registry-mirror.receipt.json"
            with (
                mock.patch.object(STACK, "crane_digest", side_effect=mismatched_digest),
                mock.patch.object(STACK, "crane_copy") as copy_image,
                self.assertRaisesRegex(STACK.DeploymentError, "post-copy"),
            ):
                STACK.mirror_selected_images(
                    Namespace(crane="crane-test", nebius_profile="explicit-profile"),
                    Path(temporary),
                    configuration,
                    regional_dynamic(Path(temporary)),
                )
            copy_image.assert_called_once()
            self.assertFalse(receipt_path.exists())

        with (
            mock.patch.object(
                STACK,
                "write_infrastructure_variables",
                return_value=Path("/infra.json"),
            ),
            mock.patch.object(
                STACK,
                "plan_stage",
                return_value=(Path("/infra.tfplan"), {}, {}),
            ) as plan_stage,
            mock.patch.object(STACK, "apply_plan"),
            mock.patch.object(
                STACK,
                "infrastructure_outputs",
                return_value=regional_dynamic(Path("/private/test-run")),
            ),
            mock.patch.object(
                STACK,
                "mirror_selected_images",
                side_effect=STACK.DeploymentError("post-copy mismatch"),
            ),
            mock.patch.object(STACK, "write_downstream_variables") as downstream,
            self.assertRaisesRegex(STACK.DeploymentError, "post-copy mismatch"),
        ):
            STACK.apply_stack(
                arguments(),
                Path("/private/test-run"),
                configuration,
                "a" * 40,
                {"stage": "configuration"},
            )
        self.assertEqual(
            [call.kwargs["stage"] for call in plan_stage.call_args_list],
            ["infrastructure"],
        )
        downstream.assert_not_called()

    def test_same_repository_accepts_multiple_digests_but_cross_host_collapse_fails(
        self,
    ) -> None:
        configuration = regional_contract()
        configuration["selected_model_ids"] = ["model-a", "model-b"]
        images = configuration["stages"]["workloads"]["model_image_overrides"]
        images.clear()
        images.update(
            {
                "model-a": f"nvcr.io/acme/runtime@sha256:{'d' * 64}",
                "model-b": f"nvcr.io/acme/runtime@sha256:{'e' * 64}",
            }
        )
        artifacts = STACK.selected_image_artifacts(
            configuration,
            regional_dynamic(Path("/private/test-run"))["registry_delivery_contract"],
        )
        model_targets = [
            artifact["target_reference"]
            for artifact in artifacts
            if artifact["name"].startswith("model/")
        ]
        self.assertEqual(len(set(model_targets)), 2)

        images["model-b"] = f"registry.example.com/acme/runtime@sha256:{'e' * 64}"
        with self.assertRaisesRegex(STACK.DeploymentError, "collapse"):
            STACK.selected_image_artifacts(
                configuration,
                regional_dynamic(Path("/private/test-run"))[
                    "registry_delivery_contract"
                ],
            )

    def test_regional_target_must_match_terraform_project_region_and_registry(
        self,
    ) -> None:
        dynamic = regional_dynamic(Path("/private/test-run"))
        dynamic["registry_delivery_contract"]["target_registry"]["project_id"] = (
            "project-wrong"
        )
        with self.assertRaisesRegex(STACK.DeploymentError, "target registry root"):
            STACK.selected_image_artifacts(
                regional_contract(), dynamic["registry_delivery_contract"]
            )

    def test_legacy_accelerator_contract_plans_only_infrastructure_upgrade(
        self,
    ) -> None:
        run_root = Path("/private/test-run")
        dynamic = dynamic_outputs(run_root)
        dynamic["accelerator_pool_contract"].pop("artifact_delivery")
        with (
            mock.patch.object(
                STACK,
                "write_infrastructure_variables",
                return_value=Path("/infra.json"),
            ),
            mock.patch.object(STACK, "state_ready", return_value=True),
            mock.patch.object(
                STACK, "infrastructure_outputs", return_value=dynamic
            ) as infrastructure_outputs,
            mock.patch.object(STACK, "write_downstream_variables") as downstream,
            mock.patch.object(
                STACK,
                "plan_stage",
                return_value=(
                    Path("/infra.tfplan"),
                    {
                        "resource_changes": [],
                        "output_changes": {
                            "accelerator_pool_contract": {"actions": ["update"]}
                        },
                    },
                    {},
                ),
            ) as plan_stage,
            mock.patch.object(STACK, "mirror_selected_images") as mirror_images,
            redirect_stdout(io.StringIO()),
        ):
            STACK.plan_stack(arguments(), run_root, contract(), "f" * 40)
        self.assertEqual(
            [call.kwargs["stage"] for call in plan_stage.call_args_list],
            ["infrastructure"],
        )
        infrastructure_outputs.assert_not_called()
        downstream.assert_not_called()
        mirror_images.assert_not_called()

    def test_facade_contains_no_quota_or_limit_raising_path(self) -> None:
        terraform_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(DEPLOY_ROOT.glob("*.tf"))
        )
        stack_source = STACK_PATH.read_text(encoding="utf-8")
        executable_source = f"{terraform_source}\n{stack_source}".lower()
        for pattern in (
            r"\bservice[_ -]?quotas?\b",
            r"\bquota[_ -]?(?:request|increase|update|raise)\b",
            r"\b(?:increase|raise)[_ -]?(?:a[_ -]?)?limits?\b",
            r"\blimit[_ -]?increase\b",
        ):
            self.assertIsNone(re.search(pattern, executable_source), pattern)

        resource_types = re.findall(r'resource\s+"([^"]+)"', terraform_source)
        self.assertEqual(resource_types, ["terraform_data"])
        self.assertNotRegex(stack_source, r'"(?:iam|quota|service-quota)"')
        self.assertEqual(STACK.parse_args(["output"]).command, "output")
        self.assertEqual(STACK.SOLUTION_ROOT, DEPLOY_ROOT)
        self.assertEqual(
            STACK.INFRA_ROOT,
            DEPLOY_ROOT / "stages" / "infrastructure",
        )
        self.assertEqual(STACK.FOUNDATION_ROOT, DEPLOY_ROOT / "stages" / "foundation")
        self.assertEqual(STACK.WORKLOADS_ROOT, DEPLOY_ROOT / "stages" / "workloads")


if __name__ == "__main__":
    unittest.main()
