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
        nebius_profile="sandbox",
    )


def contract() -> dict:
    return {
        "schema_version": 1,
        "name": "fs2-wrapper-test",
        "run_id": "r0123456789",
        "profiles": {
            "capacity": "full_catalog",
            "accelerators": "full_catalog",
            "models": "full_catalog",
        },
        "target": {
            "project_id": "project-test",
            "region": "us-north1",
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
        "accelerator_pool_contract": {"schema": "accelerators-test/v1"},
        "public_edge_contract": {"mode": "internal-only"},
    }


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
            self.assertEqual(credentials["username"], secret_values["TEST_FS2_GRAFANA_USERNAME"])
            self.assertEqual(credentials["password"], secret_values["TEST_FS2_GRAFANA_PASSWORD"])
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

    def test_clean_environment_cannot_override_generated_stage_inputs(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"TF_VAR_project_id": "wrong-project", "TF_DATA_DIR": "/wrong-data"},
            clear=False,
        ):
            cleaned = STACK.clean_environment()
        self.assertNotIn("TF_VAR_project_id", cleaned)
        self.assertNotIn("TF_DATA_DIR", cleaned)

    def test_public_grafana_origin_and_allowlist_come_from_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inference-stack-grafana-") as temporary:
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
                    "prometheus": {"url": "", "verified_external_route": False},
                    "loki": {"url": "", "verified_external_route": False},
                },
            )

    def test_workload_secrets_follow_selected_artifact_requirements(self) -> None:
        configuration = contract()
        configuration["secret_requirements"]["ngc_api_key"] = False
        configuration["secret_requirements"]["nvcr_dockerconfig"] = False
        with tempfile.TemporaryDirectory(prefix="inference-stack-secrets-") as temporary:
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

    def test_nebius_profile_is_used_for_kubeconfig_retrieval(self) -> None:
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory(prefix="inference-stack-profile-") as temporary:
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
            "target_contract": {"schema": "target-test/v1"},
            "accelerator_pool_contract": {"schema": "accelerators-test/v2"},
            "public_edge_contract": {"mode": "internal-only"},
        }
        terraform_outputs = {
            name: {"sensitive": False, "value": value}
            for name, value in required_outputs.items()
        }

        with tempfile.TemporaryDirectory(prefix="inference-stack-contract-") as temporary:
            run_root = Path(temporary)
            with (
                mock.patch.object(STACK, "terraform_init"),
                mock.patch.object(
                    STACK,
                    "terraform_json_output",
                    side_effect=lambda _terraform, _root, name, _environment: required_outputs[
                        name
                    ],
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
            terraform_output.assert_called_once_with(
                [
                    "terraform-test",
                    f"-chdir={STACK.INFRA_ROOT}",
                    "output",
                    "-json",
                ],
                env=mock.ANY,
                capture=True,
            )

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

    def test_clean_environment_removes_workspace_and_cli_argument_overrides(self) -> None:
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

    def test_apply_uses_infrastructure_foundation_workloads_order(self) -> None:
        run_root = Path("/private/test-run")
        planned_stages: list[str] = []
        endpoint_values = {
            "mcp_endpoint_url": "https://192.0.2.10/mcp",
            "admin_web_interface_url": "https://192.0.2.10/admin/",
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
                STACK, "write_infrastructure_variables", return_value=Path("/infra.json")
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
            mock.patch.object(
                STACK,
                "terraform_json_output",
                side_effect=fake_endpoint_output,
            ),
            mock.patch.object(STACK, "stage_environment", return_value={}),
            redirect_stdout(output),
        ):
            STACK.apply_stack(arguments(), run_root, contract(), "c" * 40)

        self.assertEqual(
            planned_stages, ["infrastructure", "foundation", "workloads"]
        )
        self.assertEqual(
            [call.args[1] for call in apply_plan.call_args_list],
            [STACK.INFRA_ROOT, STACK.FOUNDATION_ROOT, STACK.WORKLOADS_ROOT],
        )
        self.assertEqual(
            json.loads(output.getvalue()), {"status": "applied", **endpoint_values}
        )

    def test_status_emits_only_the_two_non_secret_workload_endpoints(self) -> None:
        endpoint_values = {
            "mcp_endpoint_url": "https://192.0.2.11/mcp",
            "admin_web_interface_url": "https://192.0.2.11/admin/",
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
                redirect_stdout(output),
            ):
                STACK.status_stack(arguments(), run_root, contract())

        payload = json.loads(output.getvalue())
        self.assertEqual(
            {name: payload[name] for name in endpoint_values}, endpoint_values
        )
        self.assertEqual(
            [call.args[2] for call in terraform_json_output.call_args_list],
            ["mcp_endpoint_url", "admin_web_interface_url"],
        )
        self.assertTrue(
            all(
                call.kwargs.get("include_secrets") is False
                for call in stage_environment.call_args_list
            )
        )

    def test_output_emits_only_the_two_non_secret_workload_endpoints(self) -> None:
        endpoint_values = {
            "mcp_endpoint_url": "https://192.0.2.12/mcp",
            "admin_web_interface_url": "https://192.0.2.12/admin/",
        }
        output = io.StringIO()
        with (
            mock.patch.object(STACK, "state_ready", return_value=True),
            mock.patch.object(
                STACK,
                "workload_endpoint_outputs",
                return_value=endpoint_values,
            ) as workload_endpoint_outputs,
            redirect_stdout(output),
        ):
            STACK.output_stack(arguments(), Path("/private/test-run"), contract())

        self.assertEqual(json.loads(output.getvalue()), endpoint_values)
        workload_endpoint_outputs.assert_called_once_with(
            "terraform-test", Path("/private/test-run"), contract()
        )

    def test_output_rejects_an_incomplete_workloads_stage(self) -> None:
        with (
            mock.patch.object(STACK, "state_ready", return_value=False),
            mock.patch.object(STACK, "workload_endpoint_outputs") as endpoint_outputs,
        ):
            with self.assertRaisesRegex(
                STACK.DeploymentError,
                "run inference-stack apply",
            ):
                STACK.output_stack(
                    arguments(), Path("/private/test-run"), contract()
                )
        endpoint_outputs.assert_not_called()

    def test_internal_proxy_command_uses_only_terraform_owned_runtime_contract(self) -> None:
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
                ["foundation"],
            ),
            (
                {"infrastructure": True, "foundation": True, "workloads": False},
                ["workloads"],
            ),
            (
                {"infrastructure": True, "foundation": True, "workloads": True},
                ["infrastructure", "foundation", "workloads"],
            ),
        )
        run_root = Path("/private/test-run")
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
                        side_effect=lambda _terraform, _root, stage, _contract: readiness[
                            stage
                        ],
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
                    mock.patch.object(STACK, "plan_stage") as plan_stage,
                    redirect_stdout(io.StringIO()),
                ):
                    STACK.plan_stack(arguments(), run_root, contract(), "d" * 40)
                self.assertEqual(
                    [call.kwargs["stage"] for call in plan_stage.call_args_list],
                    expected_stages,
                )

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
                STACK, "write_infrastructure_variables", return_value=Path("/infra.json")
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
            redirect_stdout(io.StringIO()),
        ):
            STACK.destroy_stack(arguments(), run_root, contract(), "e" * 40)

        self.assertEqual(
            planned_stages, ["workloads", "foundation", "infrastructure"]
        )
        self.assertEqual(destroy_flags, [True, True, True])
        self.assertEqual(
            [call.args[1] for call in apply_plan.call_args_list],
            [STACK.WORKLOADS_ROOT, STACK.FOUNDATION_ROOT, STACK.INFRA_ROOT],
        )

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
