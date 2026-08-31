from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

VERIFIER_PATH = Path(__file__).with_name("verify_plan.py")
SYNTHETIC_TARGETS = Path(__file__).with_name("fixtures") / "public-synthetic-targets.json"
os.environ["K8S_INFERENCE_TARGET_CONTRACT_PATH"] = str(SYNTHETIC_TARGETS)
SPEC = importlib.util.spec_from_file_location("fs2_infra_verify_plan", VERIFIER_PATH)
assert SPEC and SPEC.loader
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)

EXPECTED_PROJECT_ID = "project-syntheticlocal"
EXPECTED_SOURCE_COMMIT = "a" * 40


class InfrastructurePlanContractTests(unittest.TestCase):
    def fixture(
        self,
        root: Path,
        *,
        mode: str = "noop",
        wrong_action: bool = False,
        project_id: str = EXPECTED_PROJECT_ID,
        source_commit: str = EXPECTED_SOURCE_COMMIT,
        metadata_project_id: str | None = None,
        metadata_source_commit: str | None = None,
        capacity_profile: str = "full_catalog",
        gpu_floor_profile: str = "zero",
        acceptance_mode: str = VERIFY.DEFAULT_ACCEPTANCE_MODE,
        public_edge_mode: str = "public",
        port_forward_local_ports: dict[str, int] | None = None,
        topology_override: tuple[str, tuple[str, ...], object] | None = None,
        contract_override: tuple[tuple[str, ...], object] | None = None,
    ) -> tuple[Path, Path]:
        run_id = "rtest01"
        local_ports = port_forward_local_ports or {
            "control_plane": 18080,
            "admin_console": 18081,
            "operator_proxy": 18082,
        }
        ownership_labels = {
            "environment": "fs2-disposable",
            "retention": "ephemeral",
            "run-id": run_id,
            "task": "fs2-terraform-recipe",
        }
        contract = VERIFY.expected_infrastructure_contract(
            project_id,
            source_commit,
            acceptance_mode,
        )
        if contract_override is not None:
            path, replacement = contract_override
            current = contract
            for key in path[:-1]:
                current = current[key]
            current[path[-1]] = replacement

        selected = VERIFY.APPROVED_TARGETS[project_id]
        target_input = {
            "project_id": project_id,
            "project_name": selected["project_name"],
            "region": selected["region"],
            "network_name": selected["network_name"],
            "subnet_name": selected["subnet_name"],
            "private_subnet_cidr": selected["private_subnet_cidr"],
            "system_update_strategy": selected["system_update_strategy"],
            "tenant_id": VERIFY.TARGET_CONTRACT["tenant_id"],
            "network_id": "vpcnetwork-test",
            "subnet_id": "vpcsubnet-test",
            "public_edge_mode": public_edge_mode,
            "infrastructure_contract": contract,
            "infrastructure_contract_sha256": VERIFY.canonical_sha256(contract),
        }
        capacity = contract["capacity"]
        values_by_address: dict[str, dict] = {
            "terraform_data.target_contract": {"input": target_input},
            "nebius_compute_v1_filesystem.cache": {
                "size_gibibytes": capacity["shared_cache_size_gib"]
            },
            "nebius_mk8s_v1_node_group.system": {
                "fixed_node_count": capacity["system"]["nodes"],
                "strategy": {
                    "max_surge": {"count": capacity["system"]["max_surge"]},
                    "max_unavailable": {"count": capacity["system"]["max_unavailable"]},
                },
            },
            VERIFY.POOL_ADDRESSES["gpu_b300_1x"]: {
                "autoscaling": {
                    "min_node_count": capacity["gpu_b300_1x"]["min_nodes"],
                    "max_node_count": capacity["gpu_b300_1x"]["max_nodes"],
                },
                "template": {
                    "resources": {"preset": capacity["gpu_b300_1x"]["preset"]}
                },
            },
            VERIFY.POOL_ADDRESSES["gpu_b300_8x"]: {
                "autoscaling": {
                    "min_node_count": capacity["gpu_b300_8x"]["min_nodes"],
                    "max_node_count": capacity["gpu_b300_8x"]["max_nodes"],
                },
                "template": {
                    "resources": {"preset": capacity["gpu_b300_8x"]["preset"]}
                },
            },
            "nebius_vpc_v1_security_rule.workers_public_edge_ingress[0]": {
                "access": "ALLOW",
                "protocol": "TCP",
                "type": "STATEFUL",
                "priority": 90,
                "ingress": {
                    "source_cidrs": ["0.0.0.0/0"],
                    "destination_ports": [80, 443, 10080, 10443, 31425, 32633],
                },
            },
            "nebius_vpc_v1_allocation.gateway[0]": {
                "parent_id": project_id,
                "ipv4_public": {"cidr": "/32", "subnet_id": "vpcsubnet-test"},
            },
        }
        if topology_override is not None:
            address, path, replacement = topology_override
            current = values_by_address[address]
            for key in path[:-1]:
                current = current[key]
            current[path[-1]] = replacement

        expected_action = {"create": "create", "noop": "no-op", "destroy": "delete"}[
            mode
        ]
        side = "before" if mode == "destroy" else "after"
        changes = []
        required_addresses = set(
            VERIFY.ACCEPTANCE_MODES[acceptance_mode]["required_addresses"]
        )
        required_addresses.update(VERIFY.EDGE_MODES[public_edge_mode])
        for index, address in enumerate(sorted(required_addresses)):
            resource_type = address.split(".", maxsplit=1)[0]
            values = copy.deepcopy(values_by_address.get(address, {}))
            if resource_type != "terraform_data":
                values.update(
                    {
                        "name": f"fs2-disposable-{run_id}-fixture",
                        "labels": ownership_labels,
                    }
                )
            change = {
                "actions": ["update"]
                if wrong_action and index == 0
                else [expected_action],
                side: values,
            }
            changes.append(
                {
                    "address": address,
                    "mode": "managed",
                    "type": resource_type,
                    "change": change,
                }
            )

        plan_path = root / f"infra-{mode}.plan.json"
        metadata_path = root / f"infra-{mode}.run-metadata.json"
        document = {
            "variables": {
                "project_id": {"value": project_id},
                "source_commit": {"value": source_commit},
                "capacity_profile": {"value": capacity_profile},
                "gpu_floor_profile": {"value": gpu_floor_profile},
                "gpu_driver_preset": {"value": "cuda13.0"},
                "public_edge_mode": {"value": public_edge_mode},
                "public_edge_source_cidrs": {
                    "value": ["0.0.0.0/0"] if public_edge_mode == "public" else []
                },
                "port_forward_local_ports": {"value": local_ports},
                "public_edge_service_ports": {
                    "value": {
                        "http": {"listener_port": 80, "target_port": 10080, "node_port": 31425},
                        "https": {"listener_port": 443, "target_port": 10443, "node_port": 32633},
                    }
                },
            },
            "resource_changes": changes,
        }
        if mode != "destroy":
            allocation_id = (
                "vpcallocation-test" if public_edge_mode == "public" else None
            )
            public_ip = "203.0.113.17" if public_edge_mode == "public" else None
            public_edge_contract = {
                "schema": "fs2-serve.nebius.ai/public-edge/v1",
                "mode": public_edge_mode,
                "transport": (
                    "public-https"
                    if public_edge_mode == "public"
                    else "kubectl-port-forward"
                ),
                "public_origin": (
                    f"https://{public_ip}" if public_edge_mode == "public" else None
                ),
                "allocation_project_id": (
                    project_id if public_edge_mode == "public" else None
                ),
                "allocation_id": allocation_id,
                "public_ipv4_address": public_ip,
                "external_traffic_policy": "Cluster",
                "service_ports": document["variables"]["public_edge_service_ports"][
                    "value"
                ],
                "port_forward": {
                    "enabled": public_edge_mode == "internal-only",
                    "bind_address": (
                        "127.0.0.1" if public_edge_mode == "internal-only" else None
                    ),
                    "application_origin": (
                        f"http://localhost:{local_ports['operator_proxy']}"
                        if public_edge_mode == "internal-only"
                        else None
                    ),
                    "operator_endpoint": (
                        f"http://127.0.0.1:{local_ports['operator_proxy']}"
                        if public_edge_mode == "internal-only"
                        else None
                    ),
                    "operator_proxy_port": (
                        local_ports["operator_proxy"]
                        if public_edge_mode == "internal-only"
                        else None
                    ),
                    "control_plane_service": "fs2-serve-control-plane",
                    "control_plane_port": 8080,
                    "control_plane_local_port": (
                        local_ports["control_plane"]
                        if public_edge_mode == "internal-only"
                        else None
                    ),
                    "admin_console_service": "fs2-serve-control-plane-admin-console",
                    "admin_console_port": 8080,
                    "admin_console_local_port": (
                        local_ports["admin_console"]
                        if public_edge_mode == "internal-only"
                        else None
                    ),
                },
                "security_group_destination_ports": (
                    [80, 443, 10080, 10443, 31425, 32633]
                    if public_edge_mode == "public"
                    else []
                ),
            }
            document["planned_values"] = {
                "outputs": {
                    "infrastructure_contract": {"value": contract},
                    "public_edge_contract": {"value": public_edge_contract},
                    "gateway_allocation_id": {"value": allocation_id},
                    "gateway_public_cidr": {
                        "value": f"{public_ip}/32" if public_ip is not None else None
                    },
                    "owned_resource_ids": {
                        "value": {"gateway_allocation": allocation_id}
                    },
                }
            }
        plan_path.write_text(json.dumps(document), encoding="utf-8")
        metadata_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "labels": {
                        "owner": "k8s-elastic-inference-platform",
                        "task": "fs2-terraform-recipe",
                        "managed-by": "terraform",
                        "environment": "fs2-disposable",
                        "retention": "ephemeral",
                        "run-id": run_id,
                    },
                    "project_id": metadata_project_id or project_id,
                    "source_commit": metadata_source_commit or source_commit,
                    "capacity_profile": capacity_profile,
                    "gpu_floor_profile": gpu_floor_profile,
                    "public_edge_mode": public_edge_mode,
                    "paths": {
                        "backend": str(root / "terraform.tfstate"),
                        "kubeconfig": str(root / "kubeconfig"),
                        "plan_json": str(plan_path),
                    },
                }
            ),
            encoding="utf-8",
        )
        os.chmod(plan_path, 0o600)
        os.chmod(metadata_path, 0o600)
        return plan_path, metadata_path

    def invoke(
        self,
        plan_path: Path,
        metadata_path: Path,
        *,
        mode: str = "noop",
        expected_project_id: str = EXPECTED_PROJECT_ID,
        expected_source_commit: str = EXPECTED_SOURCE_COMMIT,
        acceptance_mode: str = VERIFY.DEFAULT_ACCEPTANCE_MODE,
        public_edge_mode: str = "public",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(VERIFIER_PATH),
                str(plan_path),
                "--mode",
                mode,
                "--run-id",
                "rtest01",
                "--run-metadata",
                str(metadata_path),
                "--expected-project-id",
                expected_project_id,
                "--expected-source-commit",
                expected_source_commit,
                "--acceptance-mode",
                acceptance_mode,
                "--public-edge-mode",
                public_edge_mode,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_exact_sixteen_address_contract_passes_all_modes(self) -> None:
        for mode in ("create", "noop", "destroy"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                os.chmod(root, 0o700)
                result = self.invoke(*self.fixture(root, mode=mode), mode=mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("exactly 16 disposable managed resources", result.stdout)
                self.assertIn("full_catalog/zero", result.stdout)

    def test_internal_only_exact_fourteen_address_contract_passes_all_modes(self) -> None:
        for mode in ("create", "noop", "destroy"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                os.chmod(root, 0o700)
                fixture = self.fixture(root, mode=mode, public_edge_mode="internal-only")
                result = self.invoke(
                    *fixture, mode=mode, public_edge_mode="internal-only"
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("exactly 14 disposable managed resources", result.stdout)
                self.assertIn("edge=internal-only", result.stdout)

    def test_internal_only_accepts_a_distinct_second_cluster_port_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            local_ports = {
                "control_plane": 28080,
                "admin_console": 28081,
                "operator_proxy": 28082,
            }
            fixture = self.fixture(
                root,
                public_edge_mode="internal-only",
                port_forward_local_ports=local_ports,
            )
            result = self.invoke(*fixture, public_edge_mode="internal-only")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_internal_only_rejects_an_invalid_local_port_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            fixture = self.fixture(
                root,
                public_edge_mode="internal-only",
                port_forward_local_ports={
                    "control_plane": 28080,
                    "admin_console": 28080,
                    "operator_proxy": 28082,
                },
            )
            result = self.invoke(*fixture, public_edge_mode="internal-only")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("port_forward_local_ports variable is invalid", result.stdout)

    def test_internal_only_rejects_public_allocation_or_ingress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            plan_path, metadata_path = self.fixture(
                root, public_edge_mode="internal-only"
            )
            document = json.loads(plan_path.read_text(encoding="utf-8"))
            document["resource_changes"].append(
                {
                    "address": "nebius_vpc_v1_allocation.gateway[0]",
                    "mode": "managed",
                    "type": "nebius_vpc_v1_allocation",
                    "change": {"actions": ["no-op"], "after": {}},
                }
            )
            plan_path.write_text(json.dumps(document), encoding="utf-8")
            result = self.invoke(
                plan_path,
                metadata_path,
                public_edge_mode="internal-only",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("managed address set differs", result.stdout)

    def test_internal_only_rejects_nonnull_allocation_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            plan_path, metadata_path = self.fixture(
                root, public_edge_mode="internal-only"
            )
            document = json.loads(plan_path.read_text(encoding="utf-8"))
            document["planned_values"]["outputs"]["gateway_allocation_id"][
                "value"
            ] = "vpcallocation-synthetic"
            plan_path.write_text(json.dumps(document), encoding="utf-8")
            result = self.invoke(
                plan_path,
                metadata_path,
                public_edge_mode="internal-only",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("internal-only outputs contain a public identity", result.stdout)

    def test_noop_outputs_bind_owned_allocation_nullability_to_edge_mode(self) -> None:
        for public_edge_mode, replacement in (
            ("public", None),
            ("internal-only", "vpcallocation-synthetic"),
        ):
            with (
                self.subTest(public_edge_mode=public_edge_mode),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                os.chmod(root, 0o700)
                plan_path, metadata_path = self.fixture(
                    root,
                    mode="noop",
                    public_edge_mode=public_edge_mode,
                )
                document = json.loads(plan_path.read_text(encoding="utf-8"))
                document["planned_values"]["outputs"]["owned_resource_ids"][
                    "value"
                ]["gateway_allocation"] = replacement
                plan_path.write_text(json.dumps(document), encoding="utf-8")
                result = self.invoke(
                    plan_path,
                    metadata_path,
                    mode="noop",
                    public_edge_mode=public_edge_mode,
                )
                self.assertNotEqual(result.returncode, 0)
                expected = (
                    "public no-op outputs lack the concrete run-owned allocation"
                    if public_edge_mode == "public"
                    else "internal-only outputs contain a public identity"
                )
                self.assertIn(expected, result.stdout)

    def test_noop_plan_rejects_update_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            result = self.invoke(*self.fixture(root, wrong_action=True))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("expected ['no-op']", result.stdout)

    def test_each_approved_project_passes_only_when_explicitly_expected(self) -> None:
        for project_id in sorted(VERIFY.APPROVED_TARGETS):
            with (
                self.subTest(project_id=project_id),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                os.chmod(root, 0o700)
                result = self.invoke(
                    *self.fixture(root, project_id=project_id),
                    expected_project_id=project_id,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_project_cannot_drift_from_caller_supplied_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            plan_path, metadata_path = self.fixture(root)
            document = json.loads(plan_path.read_text(encoding="utf-8"))
            document["variables"]["project_id"]["value"] = (
                "project-syntheticdrift"
            )
            plan_path.write_text(json.dumps(document), encoding="utf-8")
            result = self.invoke(plan_path, metadata_path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("plan variable project_id differs", result.stdout)

    def test_source_commit_is_exact_in_plan_and_metadata(self) -> None:
        for fixture_kwargs, expected_error in (
            (
                {"source_commit": "b" * 40},
                "plan variable source_commit differs",
            ),
            (
                {"metadata_source_commit": "b" * 40},
                "run metadata source_commit differs",
            ),
        ):
            with (
                self.subTest(fixture_kwargs=fixture_kwargs),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                os.chmod(root, 0o700)
                result = self.invoke(*self.fixture(root, **fixture_kwargs))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stdout)

    def test_minimal_or_nonzero_floor_fails_closed(self) -> None:
        for fixture_kwargs, expected_error in (
            (
                {"capacity_profile": "minimal"},
                "plan variable capacity_profile differs",
            ),
            (
                {"gpu_floor_profile": "representative"},
                "plan variable gpu_floor_profile differs",
            ),
        ):
            with (
                self.subTest(fixture_kwargs=fixture_kwargs),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                os.chmod(root, 0o700)
                result = self.invoke(*self.fixture(root, **fixture_kwargs))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stdout)

    def test_admin_minimal_zero_is_an_explicit_exact_additive_mode(self) -> None:
        for mode in ("create", "noop", "destroy"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                os.chmod(root, 0o700)
                fixture = self.fixture(
                    root,
                    mode=mode,
                    capacity_profile="minimal",
                    gpu_floor_profile="zero",
                    acceptance_mode=VERIFY.ADMIN_MINIMAL_ZERO_MODE,
                )
                result = self.invoke(
                    *fixture,
                    mode=mode,
                    acceptance_mode=VERIFY.ADMIN_MINIMAL_ZERO_MODE,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("exact minimal/zero infrastructure contract", result.stdout)
                self.assertIn("(admin-minimal-zero)", result.stdout)

    def test_admin_minimal_zero_rejects_capacity_filesystem_and_address_drift(self) -> None:
        cases = (
            (
                {
                    "topology_override": (
                        "nebius_compute_v1_filesystem.cache",
                        ("size_gibibytes",),
                        2048,
                    )
                },
                "nebius_compute_v1_filesystem.cache.size_gibibytes differs from the exact minimal/zero topology",
            ),
            (
                {
                    "topology_override": (
                        VERIFY.POOL_ADDRESSES["gpu_b300_8x"],
                        ("autoscaling", "max_node_count"),
                        2,
                    )
                },
                (
                    f"{VERIFY.POOL_ADDRESSES['gpu_b300_8x']}.autoscaling.max_node_count "
                    "differs from the exact minimal/zero topology"
                ),
            ),
        )
        for fixture_kwargs, expected_error in cases:
            with (
                self.subTest(fixture_kwargs=fixture_kwargs),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                os.chmod(root, 0o700)
                fixture = self.fixture(
                    root,
                    capacity_profile="minimal",
                    acceptance_mode=VERIFY.ADMIN_MINIMAL_ZERO_MODE,
                    **fixture_kwargs,
                )
                result = self.invoke(
                    *fixture,
                    acceptance_mode=VERIFY.ADMIN_MINIMAL_ZERO_MODE,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stdout)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            plan_path, metadata_path = self.fixture(
                root,
                capacity_profile="minimal",
                acceptance_mode=VERIFY.ADMIN_MINIMAL_ZERO_MODE,
            )
            document = json.loads(plan_path.read_text(encoding="utf-8"))
            document["resource_changes"] = document["resource_changes"][1:]
            plan_path.write_text(json.dumps(document), encoding="utf-8")
            result = self.invoke(
                plan_path,
                metadata_path,
                acceptance_mode=VERIFY.ADMIN_MINIMAL_ZERO_MODE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("managed address set differs", result.stdout)

    def test_admin_mode_does_not_accept_the_full_catalog_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            result = self.invoke(
                *self.fixture(root),
                acceptance_mode=VERIFY.ADMIN_MINIMAL_ZERO_MODE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("plan variable capacity_profile differs", result.stdout)

    def test_each_concrete_topology_field_fails_closed(self) -> None:
        mutations = (
            ("nebius_compute_v1_filesystem.cache", ("size_gibibytes",), 128),
            ("nebius_mk8s_v1_node_group.system", ("fixed_node_count",), 1),
            (
                "nebius_mk8s_v1_node_group.system",
                ("strategy", "max_surge", "count"),
                0,
            ),
            (
                "nebius_mk8s_v1_node_group.system",
                ("strategy", "max_unavailable", "count"),
                1,
            ),
            (
                VERIFY.POOL_ADDRESSES["gpu_b300_1x"],
                ("autoscaling", "min_node_count"),
                1,
            ),
            (
                VERIFY.POOL_ADDRESSES["gpu_b300_1x"],
                ("autoscaling", "max_node_count"),
                1,
            ),
            (
                VERIFY.POOL_ADDRESSES["gpu_b300_8x"],
                ("autoscaling", "min_node_count"),
                1,
            ),
            (
                VERIFY.POOL_ADDRESSES["gpu_b300_8x"],
                ("autoscaling", "max_node_count"),
                1,
            ),
        )
        for address, path, value in mutations:
            with (
                self.subTest(address=address, path=path),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                os.chmod(root, 0o700)
                result = self.invoke(
                    *self.fixture(root, topology_override=(address, path, value))
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "differs from the exact full_catalog/zero topology", result.stdout
                )

    def test_contract_maximum_gpu_or_source_field_fails_closed(self) -> None:
        for path, value in (
            (("capacity", "maximum_gpus"), 9),
            (("capacity", "shared_cache_size_gib"), 128),
            (("source_commit",), "b" * 40),
            (("target", "region"), "region-wrong"),
        ):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                os.chmod(root, 0o700)
                result = self.invoke(
                    *self.fixture(root, contract_override=(path, value))
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "planned infrastructure_contract output differs", result.stdout
                )

    def test_expected_arguments_are_mandatory(self) -> None:
        result = subprocess.run(
            [sys.executable, str(VERIFIER_PATH), "missing.json"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--expected-project-id", result.stderr)
        self.assertIn("--expected-source-commit", result.stderr)


if __name__ == "__main__":
    unittest.main()
