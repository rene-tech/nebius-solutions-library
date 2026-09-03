"""Root-to-infrastructure-to-workloads contract for the general CPU batch pool.

The lane exists so scientific preprocessing and aggregation scale independently
of the dedicated reference-data nodes. These tests hold that separation: the two
CPU lanes must never share a pool, a flavor, a queue or a namespace, and the
general lane must stay fully tfvars driven with no project, region, accelerator
or live node identity anywhere in its source.
"""

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
CONTRACT_PATH = DEPLOY_ROOT / "scheduling" / "cpu-class-contract.json"

# Synthetic plan fixture for values below the minimal profile's nominal pool
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
INFRASTRUCTURE_CLUSTER = DEPLOY_ROOT / "stages/infrastructure/cluster.tf"
INFRASTRUCTURE_OUTPUTS = DEPLOY_ROOT / "stages/infrastructure/outputs.tf"
WORKLOADS_GENERAL_CPU = DEPLOY_ROOT / "stages/workloads/general_cpu.tf"
MODULE_MAIN = DEPLOY_ROOT / "modules/general-cpu-scheduling/main.tf"
CONTROL_PLANE = DEPLOY_ROOT / "stages/workloads/control_plane.tf"
STACK = DEPLOY_ROOT / "inference-stack"

TEST_TARGET = {
    "project_id": "project-testinference",
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
SMALL_POOL = {
    "platform": "cpu-d3",
    "preset": "8vcpu-32gb",
    "capacity_type": "preemptible",
    "autoscaling": {"min_nodes": 0, "max_nodes": 4},
    "schedulable_capacity": {
        "cpu_millicores": 7000,
        "memory_mib": 28672,
        "ephemeral_storage_mib": 114688,
    },
}
# The measured capacity of a 32vcpu-128gb node, which is what makes one
# AlphaFold 3 raw-input pod (16 CPU / 64 GiB) fit on a single node.
REFERENCE_POOL_16C64G = {
    "platform": "cpu-d3",
    "preset": "32vcpu-128gb",
    "node_count": 1,
    "schedulable_capacity": {
        "cpu_millicores": 30000,
        "memory_mib": 122880,
        "ephemeral_storage_mib": 114688,
    },
}


class GeneralCpuPoolTests(unittest.TestCase):
    terraform: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.terraform = shutil.which("terraform")
        if cls.terraform is None:
            raise unittest.SkipTest("terraform is required for general CPU pool tests")

        cls.temporary = tempfile.TemporaryDirectory(prefix="fs2-general-cpu-tests-")
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
            timeout=120,
        )

    @classmethod
    def _write(cls, name: str, deployment: dict[str, Any]) -> Path:
        deployment = dict(deployment)
        deployment.setdefault("applications", TEST_APPLICATIONS)
        deployment.setdefault("schema_version", 1)
        deployment.setdefault("target", TEST_TARGET)
        # A CPU pool and the reference-data plane both budget cpu and memory,
        # which Kueue drops before admission unless core admission is on, so
        # the facade refuses either without it. Every fixture that declares
        # one therefore declares the capacity too, bounded by measured node
        # facts rather than by a preset's nominal size. A test that exercises
        # that gate passes "core_capacity": None and it is left absent.
        budgets_core = bool(deployment.get("cpu_pools")) or (
            deployment.get("storage", {}).get("reference_data", {}).get("enabled")
            is True
        )
        if budgets_core:
            scheduling = dict(deployment.get("scheduling", {}))
            if "budget_core_resources" not in scheduling:
                scheduling["budget_core_resources"] = True
                scheduling.setdefault(
                    "accelerator_schedulable_capacity", ACCELERATOR_CAPACITY_FIXTURE
                )
            deployment["scheduling"] = scheduling
        path = cls.run_root / f"{name}.tfvars.json"
        path.write_text(
            json.dumps({"deployment": deployment}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _plan(self, name: str, deployment: dict[str, Any]):
        variable_file = self._write(name, deployment)
        return self._terraform(
            "plan",
            "-input=false",
            "-lock=false",
            "-refresh=false",
            "-no-color",
            f"-var-file={variable_file}",
            f"-out={self.run_root / f'{name}.tfplan'}",
        )

    def _outputs_for_file(self, variable_file: Path, name: str) -> dict[str, Any]:
        result = self._terraform(
            "plan",
            "-input=false",
            "-lock=false",
            "-refresh=false",
            "-no-color",
            f"-var-file={variable_file}",
            f"-out={self.run_root / f'{name}.tfplan'}",
        )
        if result.returncode != 0:
            raise AssertionError(f"terraform plan failed:\n{result.stderr}")
        shown = self._terraform("show", "-json", str(self.run_root / f"{name}.tfplan"))
        if shown.returncode != 0:
            raise AssertionError(f"terraform show failed:\n{shown.stderr}")
        document = json.loads(shown.stdout)
        return {
            key: output["value"]
            for key, output in document["planned_values"]["outputs"].items()
        }

    def _outputs(self, name: str, deployment: dict[str, Any]) -> dict[str, Any]:
        result = self._plan(name, deployment)
        if result.returncode != 0:
            raise AssertionError(f"terraform plan failed:\n{result.stderr}")
        shown = self._terraform("show", "-json", str(self.run_root / f"{name}.tfplan"))
        if shown.returncode != 0:
            raise AssertionError(f"terraform show failed:\n{shown.stderr}")
        document = json.loads(shown.stdout)
        return {
            key: output["value"]
            for key, output in document["planned_values"]["outputs"].items()
        }

    def test_a_cpu_pool_without_core_admission_is_refused(self) -> None:
        """Kueue drops cpu and memory before admission while they are excluded.

        A general CPU lane whose quota is never enforced is decoration, so the
        facade refuses the combination before the infrastructure stage creates
        a node group.
        """

        result = self._plan(
            "fs2-general-cpu-no-core",
            {
                "name": "fs2-general-cpu-no-core",
                "cpu_pools": {"general-cpu-8x": SMALL_POOL},
                "scheduling": {
                    "general_cpu": {"namespace": "fs2-models"},
                    "budget_core_resources": False,
                },
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(
            f"{result.stdout}\n{result.stderr}",
            re.compile("budget_core_resources", re.IGNORECASE),
        )

    def test_a_node_label_the_api_would_reject_is_refused(self) -> None:
        """These labels reach Nebius node metadata; a plan must not pass them."""

        rejected = {
            "two slashes in a key": {"a.example.com/b/c": "true"},
            "underscore in a DNS prefix": {"a_b.example.com/pool": "true"},
            "a 254-character prefix": {
                ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 62]) + "/" + "b" * 62: "true"
            },
            "a space in a value": {"workload.fs2.nebius/general-cpu": "has space"},
        }
        for index, (label, node_labels) in enumerate(rejected.items()):
            with self.subTest(rejected=label):
                pool = {**SMALL_POOL, "node_labels": node_labels}
                result = self._plan(
                    f"fs2-general-cpu-bad-label-{index}",
                    {
                        "name": f"fs2-general-cpu-bad-label-{index}",
                        "cpu_pools": {"general-cpu-8x": pool},
                        "scheduling": {"general_cpu": {"namespace": "fs2-models"}},
                    },
                )
                self.assertNotEqual(result.returncode, 0)

        # A qualified key with a 253-character prefix and a valid value is the
        # boundary and must still be accepted.
        accepted = {
            ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 61]) + "/" + "b" * 63: "true"
        }
        outputs = self._outputs(
            "fs2-general-cpu-boundary-label",
            {
                "name": "fs2-general-cpu-boundary-label",
                "cpu_pools": {
                    "general-cpu-8x": {**SMALL_POOL, "node_labels": accepted}
                },
                "scheduling": {"general_cpu": {"namespace": "fs2-models"}},
            },
        )
        pool = outputs["deployment_contract"]["stages"]["infrastructure"]["cpu_pools"][
            "general-cpu-8x"
        ]
        self.assertEqual({key: pool["node_labels"][key] for key in accepted}, accepted)

    def test_a_deployment_without_cpu_pools_creates_no_lane(self) -> None:
        outputs = self._outputs("no-cpu-pools", {"name": "fs2-no-general-cpu"})
        lane = outputs["effective_configuration"]["general_cpu"]

        self.assertFalse(lane["enabled"])
        self.assertEqual(lane["pool_ids"], [])
        self.assertIsNone(lane["namespace"])
        self.assertIsNone(lane["cluster_queue"])
        self.assertIsNone(lane["local_queue"])
        self.assertEqual(
            outputs["deployment_contract"]["stages"]["infrastructure"]["cpu_pools"], {}
        )
        self.assertFalse(
            outputs["deployment_contract"]["stages"]["workloads"]["general_cpu_lane"][
                "enabled"
            ]
        )

    def test_a_preemptible_scale_from_zero_pool_reaches_both_stages(self) -> None:
        outputs = self._outputs(
            "scale-from-zero",
            {
                "name": "fs2-general-cpu-elastic",
                "cpu_pools": {"general-cpu-8x": SMALL_POOL},
                "scheduling": {"general_cpu": {"namespace": "fs2-models"}},
            },
        )
        lane = outputs["effective_configuration"]["general_cpu"]
        infrastructure = outputs["deployment_contract"]["stages"]["infrastructure"]
        pool = infrastructure["cpu_pools"]["general-cpu-8x"]

        self.assertTrue(lane["enabled"])
        self.assertEqual(lane["scale_from_zero"], ["general-cpu-8x"])
        self.assertEqual(lane["elastic_pool_ids"], ["general-cpu-8x"])
        self.assertEqual(lane["preemptible_pools"], ["general-cpu-8x"])
        self.assertTrue(pool["elastic"])
        self.assertEqual((pool["min_nodes"], pool["max_nodes"]), (0, 4))
        # Quota is measured capacity times the authorized ceiling, not a guess.
        self.assertEqual(lane["lane_capacity"]["cpu_millicores"], 7000 * 4)
        self.assertEqual(lane["lane_capacity"]["memory_mib"], 28672 * 4)
        # A pod runs on one node, so the lane also reports its largest node.
        self.assertEqual(lane["largest_node"]["cpu_millicores"], 7000)

    def test_a_fixed_pool_pins_one_node_count(self) -> None:
        fixed = {key: value for key, value in SMALL_POOL.items() if key != "autoscaling"}
        fixed["fixed_nodes"] = 2
        fixed["capacity_type"] = "regular"
        outputs = self._outputs(
            "fixed-capacity",
            {
                "name": "fs2-general-cpu-fixed",
                "cpu_pools": {"general-cpu-fixed": fixed},
                "scheduling": {"general_cpu": {"namespace": "fs2-models"}},
            },
        )
        pool = outputs["deployment_contract"]["stages"]["infrastructure"]["cpu_pools"][
            "general-cpu-fixed"
        ]
        lane = outputs["effective_configuration"]["general_cpu"]

        self.assertFalse(pool["elastic"])
        self.assertEqual((pool["min_nodes"], pool["max_nodes"]), (2, 2))
        self.assertEqual(lane["elastic_pool_ids"], [])
        self.assertEqual(lane["scale_from_zero"], [])
        self.assertEqual(lane["lane_capacity"]["cpu_millicores"], 7000 * 2)

    def test_more_than_one_general_pool_is_refused_in_v1(self) -> None:
        """One shared flavor over several pools could not name the pool that ran a stage."""

        large = {
            "platform": "cpu-d3",
            "preset": "32vcpu-128gb",
            "capacity_type": "regular",
            "fixed_nodes": 1,
            "schedulable_capacity": {
                "cpu_millicores": 30000,
                "memory_mib": 122880,
                "ephemeral_storage_mib": 114688,
            },
        }
        result = self._plan(
            "heterogeneous-cpu",
            {
                "name": "fs2-general-cpu-mixed",
                "cpu_pools": {"general-cpu-8x": SMALL_POOL, "general-cpu-32x": large},
                "scheduling": {"general_cpu": {"namespace": "fs2-models"}},
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(
            f"{result.stdout}\n{result.stderr}",
            re.compile("exactly one", re.IGNORECASE),
        )

    def test_a_pool_too_small_for_a_bound_workload_is_refused_before_apply(self) -> None:
        tiny = dict(SMALL_POOL)
        tiny["schedulable_capacity"] = {
            "cpu_millicores": 2000,
            "memory_mib": 4096,
            "ephemeral_storage_mib": 114688,
        }
        result = self._plan(
            "undersized-general-pool",
            {
                "name": "fs2-general-cpu-small",
                "cpu_pools": {"general-cpu-tiny": tiny},
                "scheduling": {"general_cpu": {"namespace": "fs2-models"}},
            },
        )
        self.assertNotEqual(result.returncode, 0)
        combined = f"{result.stdout}\n{result.stderr}"
        # It must name the workload that does not fit, not just fail.
        self.assertRegex(combined, re.compile("bindcraft", re.IGNORECASE))

    def test_measured_capacity_must_be_declared_and_positive(self) -> None:
        broken = dict(SMALL_POOL)
        broken["schedulable_capacity"] = {
            "cpu_millicores": 0,
            "memory_mib": 28672,
            "ephemeral_storage_mib": 114688,
        }
        result = self._plan(
            "invalid-capacity",
            {
                "name": "fs2-general-cpu-invalid",
                "cpu_pools": {"general-cpu-8x": broken},
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(
            f"{result.stdout}\n{result.stderr}",
            re.compile("measured schedulable", re.IGNORECASE),
        )

    def test_a_pool_must_choose_exactly_one_capacity_mode(self) -> None:
        both = dict(SMALL_POOL)
        both["fixed_nodes"] = 2
        result = self._plan(
            "two-capacity-modes",
            {"name": "fs2-general-cpu-both", "cpu_pools": {"general-cpu-8x": both}},
        )
        self.assertNotEqual(result.returncode, 0)

        neither = {
            key: value for key, value in SMALL_POOL.items() if key != "autoscaling"
        }
        result = self._plan(
            "no-capacity-mode",
            {"name": "fs2-general-cpu-none", "cpu_pools": {"general-cpu-8x": neither}},
        )
        self.assertNotEqual(result.returncode, 0)

    def test_a_general_pool_cannot_label_itself_as_another_pool(self) -> None:
        masquerading = dict(SMALL_POOL)
        masquerading["node_labels"] = {"workload.fs2.nebius/reference-data": "true"}
        result = self._plan(
            "masquerading-labels",
            {
                "name": "fs2-general-cpu-masq",
                "cpu_pools": {"general-cpu-8x": masquerading},
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(
            f"{result.stdout}\n{result.stderr}",
            re.compile("reserved", re.IGNORECASE),
        )

    def test_the_lane_cannot_reuse_a_reference_data_queue_identity(self) -> None:
        result = self._plan(
            "queue-collision",
            {
                "name": "fs2-general-cpu-collide",
                "cpu_pools": {"general-cpu-8x": SMALL_POOL},
                "scheduling": {
                    "general_cpu": {
                        "cluster_queue": "reference-data-cpu",
                        "namespace": "fs2-models",
                    }
                },
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(
            f"{result.stdout}\n{result.stderr}",
            re.compile("reference-data", re.IGNORECASE),
        )

    def test_the_lane_cannot_admit_from_the_reference_data_namespace(self) -> None:
        result = self._plan(
            "namespace-collision",
            {
                "name": "fs2-general-cpu-ns",
                "cpu_pools": {"general-cpu-8x": SMALL_POOL},
                "scheduling": {"general_cpu": {"namespace": "fs2-reference-data"}},
            },
        )
        self.assertNotEqual(result.returncode, 0)

    def test_the_default_namespace_is_owned_when_the_academic_tenant_is_off(
        self,
    ) -> None:
        """A lane must never point at a namespace no owner creates."""

        outputs = self._outputs(
            "default-namespace",
            {
                "name": "fs2-general-cpu-default-ns",
                "cpu_pools": {"general-cpu-8x": SMALL_POOL},
            },
        )
        lane = outputs["effective_configuration"]["general_cpu"]

        # fs2-models is provisioned by the platform itself.
        self.assertEqual(lane["namespace"], "fs2-models")
        self.assertTrue(lane["enabled"])

    def test_the_selected_namespace_is_exactly_what_tfvars_asked_for(self) -> None:
        """Terraform drops unknown object attributes silently.

        A removed knob left in tfvars would therefore not fail; it would be
        ignored, and the lane would route somewhere the file does not say. So
        assert the resolved namespace for each way of selecting one.
        """

        # Explicit, and owned by this stack.
        outputs = self._outputs(
            "explicit-namespace",
            {
                "name": "fs2-general-cpu-explicit-ns",
                "cpu_pools": {"general-cpu-8x": SMALL_POOL},
                "scheduling": {"general_cpu": {"namespace": "fs2-models"}},
            },
        )
        lane = outputs["effective_configuration"]["general_cpu"]
        self.assertEqual(lane["namespace"], "fs2-models")
        # The stage receives the same namespace the facade resolved, so nothing
        # downstream can re-derive a different one.
        self.assertEqual(
            outputs["deployment_contract"]["stages"]["workloads"]["general_cpu_lane"][
                "namespace"
            ],
            "fs2-models",
        )

        # Implicit, with the academic tenant enabled: the lane follows the
        # licensed claim its BindCraft stage mounts.
        academic = self._write(
            "academic-namespace",
            {
                "schema_version": 1,
                "name": "fs2-general-cpu-academic-ns",
                "target": TEST_TARGET,
                "applications": TEST_APPLICATIONS,
                "cpu_pools": {"general-cpu-8x": SMALL_POOL},
                # The stable ClusterQueue now serves the model lane and the
                # licensed lane, and Kueue orders them by decayed fair-share
                # usage before priority, which the operator must accept.
                "scheduling": {
                    "fair_share_precedence_acknowledged": True,
                    # AlphaFold 3 is scientific-only, so nothing derives which
                    # accelerators it is qualified for and the licensed lane
                    # routes it.
                    "model_eligible_pool_ids": {
                        "alphafold3": [
                            "nebius-b300-preemptible-1x",
                            "nebius-b300-preemptible-8x",
                        ]
                    },
                },
            },
        )
        academic.write_text(
            json.dumps(
                {
                    "deployment": json.loads(academic.read_text())["deployment"],
                    # The academic lane must name this deployment's actual
                    # stable ClusterQueue, not the default name from another
                    # profile, or it would reference a queue nothing renders.
                    # A licensed lane exists to run a licensed model, so the
                    # tenant declares one; a lane with tenants and service
                    # classes but no model could never match a route, and the
                    # facade refuses it.
                    "academic_assets": {
                        "enabled": True,
                        "execution": {
                            "enabled": True,
                            "cluster_queue": "fs2-b300-async",
                        },
                        "assets": {
                            "alphafold3-parameters": {
                                "model_id": "alphafold3",
                                "relative_path": "alphafold3/af3.bin.zst",
                            }
                        },
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        result = self._terraform(
            "plan",
            "-input=false",
            "-lock=false",
            "-refresh=false",
            "-no-color",
            f"-var-file={academic}",
            f"-out={self.run_root / 'academic-namespace.tfplan'}",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        shown = self._terraform(
            "show", "-json", str(self.run_root / "academic-namespace.tfplan")
        )
        planned = json.loads(shown.stdout)["planned_values"]["outputs"]
        self.assertEqual(
            planned["effective_configuration"]["value"]["general_cpu"]["namespace"],
            "fs2-academic-poc",
        )

    def test_the_shipped_examples_carry_no_removed_lane_knobs(self) -> None:
        # An object type silently discards attributes it does not declare, so a
        # stale knob in a shipped example is not a plan error; it is a file that
        # says one thing while Terraform does another.
        removed = ("include_academic_namespace", "tenant_namespaces")
        for name in ("terraform.tfvars.example", "examples/heterogeneous.tfvars"):
            text = (DEPLOY_ROOT / name).read_text(encoding="utf-8")
            for knob in removed:
                with self.subTest(example=name, knob=knob):
                    self.assertNotIn(knob, text)

        # And the H100 example resolves to the namespace it actually prints.
        outputs = self._outputs_for_file(
            DEPLOY_ROOT / "examples/heterogeneous.tfvars", "shipped-h100-namespace"
        )
        self.assertEqual(
            outputs["effective_configuration"]["general_cpu"]["namespace"],
            "fs2-models",
        )

    def test_a_namespace_no_owner_creates_is_rejected(self) -> None:
        result = self._plan(
            "unowned-namespace",
            {
                "name": "fs2-general-cpu-unowned",
                "cpu_pools": {"general-cpu-8x": SMALL_POOL},
                "scheduling": {"general_cpu": {"namespace": "some-other-namespace"}},
            },
        )
        self.assertNotEqual(result.returncode, 0)

    def test_a_general_pool_cannot_take_an_accelerator_pool_id(self) -> None:
        result = self._plan(
            "pool-id-collision",
            {
                "name": "fs2-general-cpu-dup",
                "accelerator_pools": {
                    "shared-id": {
                        "platform": "gpu-h100-sxm",
                        "preset": "1gpu-16vcpu-200gb",
                        "accelerator_class": "nvidia-h100-sxm5-80gb",
                        "gpus_per_node": 1,
                        "capacity_type": "preemptible",
                        "min_nodes": 0,
                        "max_nodes": 1,
                        "driver": {"mode": "managed", "preset": "cuda13.0"},
                    }
                },
                "cpu_pools": {"shared-id": SMALL_POOL},
                "scheduling": {"general_cpu": {"namespace": "fs2-models"}},
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(
            f"{result.stdout}\n{result.stderr}",
            re.compile("collide", re.IGNORECASE),
        )

    def test_the_two_cpu_lanes_stay_distinct_when_both_are_enabled(self) -> None:
        outputs = self._outputs(
            "both-cpu-lanes",
            {
                "name": "fs2-both-cpu-lanes",
                "cpu_pools": {"general-cpu-8x": SMALL_POOL},
                "scheduling": {"general_cpu": {"namespace": "fs2-models"}},
                "storage": {
                    "reference_data": {
                        "enabled": True,
                        "cpu_pool": REFERENCE_POOL_16C64G,
                        "queue": {"nominal_cpu": "16", "nominal_memory": "64Gi"},
                    }
                },
            },
        )
        configuration = outputs["effective_configuration"]
        lane = configuration["general_cpu"]
        reference = configuration["reference_data"]
        infrastructure = outputs["deployment_contract"]["stages"]["infrastructure"]

        self.assertTrue(lane["enabled"])
        self.assertTrue(reference["enabled"])
        self.assertTrue(lane["distinct_from_reference_data"])
        self.assertNotEqual(lane["cluster_queue"], "reference-data-cpu")
        self.assertNotEqual(lane["namespace"], "fs2-reference-data")
        # Two separate owners: the reference pool is its own block and the
        # general pools never appear inside it.
        self.assertNotIn("cpu_pools", infrastructure["reference_data"])
        self.assertEqual(list(infrastructure["cpu_pools"]), ["general-cpu-8x"])
        # The general lane never joins a cohort, so it can neither borrow
        # reference-database capacity nor lend its own.
        self.assertIsNone(lane["cohort"])

    def test_the_enlarged_reference_pool_fits_one_af3_raw_input_pod(self) -> None:
        outputs = self._outputs(
            "af3-raw-input-fit",
            {
                "name": "fs2-af3-raw-fit",
                "storage": {
                    "reference_data": {
                        "enabled": True,
                        "cpu_pool": REFERENCE_POOL_16C64G,
                        "queue": {"nominal_cpu": "16", "nominal_memory": "64Gi"},
                    }
                },
            },
        )
        reference = outputs["effective_configuration"]["reference_data"]
        requirement = json.loads(
            (DEPLOY_ROOT / "reference-data/model-requirements.json").read_text(
                encoding="utf-8"
            )
        )["models"]["alphafold3"]["preprocessing_capacity"]

        required_cpu_millicores = int(requirement["cpu"]) * 1000
        required_memory_mib = int(requirement["memory"].removesuffix("Gi")) * 1024
        schedulable = reference["cpu_pool_schedulable"]

        # One pod, one node: the node itself must hold the whole request.
        self.assertGreaterEqual(schedulable["cpu_millicores"], required_cpu_millicores)
        self.assertGreaterEqual(schedulable["memory_mib"], required_memory_mib)
        # The pool the old 8vcpu-32gb class could not satisfy.
        self.assertLess(7000, required_cpu_millicores)

    def test_the_shipped_h100_example_ships_both_pools(self) -> None:
        example = (DEPLOY_ROOT / "examples/heterogeneous.tfvars").read_text(
            encoding="utf-8"
        )
        self.assertIn("cpu_pools = {", example)
        self.assertIn('preset        = "8vcpu-32gb"', example)
        self.assertIn('preset     = "32vcpu-128gb"', example)

        result = self._terraform(
            "plan",
            "-input=false",
            "-lock=false",
            "-refresh=false",
            "-no-color",
            f"-var-file={DEPLOY_ROOT / 'examples/heterogeneous.tfvars'}",
            f"-out={self.run_root / 'shipped-h100.tfplan'}",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class GeneralCpuSourceTests(unittest.TestCase):
    """Source-level invariants that a plan alone cannot show."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        cls.cluster = INFRASTRUCTURE_CLUSTER.read_text(encoding="utf-8")
        cls.infrastructure_outputs = INFRASTRUCTURE_OUTPUTS.read_text(encoding="utf-8")
        cls.workloads = WORKLOADS_GENERAL_CPU.read_text(encoding="utf-8")
        cls.module = MODULE_MAIN.read_text(encoding="utf-8")
        cls.control_plane = CONTROL_PLANE.read_text(encoding="utf-8")
        cls.stack = STACK.read_text(encoding="utf-8")

    def test_the_node_group_is_tfvars_driven_and_region_agnostic(self) -> None:
        block = re.split(
            r"\n(?:resource |moved )",
            self.cluster.split('resource "nebius_mk8s_v1_node_group" "general_cpu"')[1],
        )[0]
        self.assertIn("for_each = var.cpu_pools", block)
        self.assertIn("platform = each.value.platform", block)
        self.assertIn("preset   = each.value.preset", block)
        self.assertIn("autoscaling", block)
        self.assertIn("fixed_node_count", block)
        # No hard-coded target, hardware or live identity anywhere in the pool.
        for forbidden in (
            "project-e00rene",
            "eu-north1",
            "k8s-inference-h100",
            "h100",
            "b300",
            "nvidia",
            "capacityblockgroup",
            "mk8snodegroup-",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, block.lower())

    def test_the_general_pool_is_tainted_and_never_mounts_reference_data(self) -> None:
        block = re.split(
            r"\n(?:resource |moved )",
            self.cluster.split('resource "nebius_mk8s_v1_node_group" "general_cpu"')[1],
        )[0]
        self.assertIn('key    = "workload.fs2.nebius/general-cpu"', block)
        self.assertIn('effect = "NO_SCHEDULE"', block)
        self.assertIn('"capacity.fs2.nebius/pool"        = "general-cpu"', block)
        # The reference-data filesystem and its attachment are not options here.
        self.assertNotIn("reference_data_filesystem_attachment", block)
        self.assertNotIn("reference_data_cloud_init_user_data", block)
        self.assertIn("reference_data_filesystem = false", self.infrastructure_outputs)

    def test_every_lane_object_has_exactly_one_owner(self) -> None:
        # The general lane owns its flavor, queue and LocalQueues here.
        for resource in (
            'resource "kubernetes_manifest" "general_cpu_flavor"',
            'resource "kubernetes_manifest" "general_cpu_cluster_queue"',
            'resource "kubernetes_manifest" "general_cpu_local_queue"',
        ):
            with self.subTest(resource=resource):
                self.assertEqual(self.workloads.count(resource), 1)

        # And it does not create the shared scheduling ConfigMap, which belongs
        # to the scheduling workstream; it contributes an entry instead.
        self.assertNotIn("kubernetes_config_map", self.workloads)
        self.assertIn("scheduling_contribution", self.workloads)

    def test_the_scheduler_remains_the_sole_assembler_and_configmap_owner(self) -> None:
        # This producer contributes an entry and its digest; it never assembles
        # a document, creates the ConfigMap, or injects a contract into Helm.
        self.assertNotIn("kubernetes_config_map", self.workloads)
        self.assertNotIn("cpuScheduling", self.workloads)
        self.assertNotIn("general_cpu_chart_overrides", self.control_plane)
        self.assertIn("cpu_class_digests", self.workloads)
        self.assertIn("external_lane_facts", self.workloads)

    def test_the_contributed_entry_is_digested_exactly(self) -> None:
        self.assertIn("cpu_class_digests", self.module)
        self.assertIn("sha256(jsonencode(entry))", self.module)

    def test_quantity_and_measured_capacity_must_agree_exactly(self) -> None:
        for field, unit in (
            ("cpu", "cpu_millicores}m"),
            ("memory", "memory_mib}Mi"),
            ("ephemeral_storage", "ephemeral_storage_mib}Mi"),
        ):
            with self.subTest(field=field):
                self.assertIn(unit, self.module)

        # The negative fixture must actually violate the rule it documents.
        invalid = json.loads(
            (
                DEPLOY_ROOT
                / "scheduling/integration/general-cpu-class-entry.invalid-capacity.fixture.json"
            ).read_text(encoding="utf-8")
        )
        capacity = invalid["cpu_classes"]["general-cpu"]["schedulable_capacity"]
        self.assertNotEqual(capacity["memory"], f"{capacity['memory_mib']}Mi")

    def test_the_integration_fixture_matches_the_canonical_fields(self) -> None:
        fixture = json.loads(
            (
                DEPLOY_ROOT
                / "scheduling/integration/general-cpu-class-entry.fixture.json"
            ).read_text(encoding="utf-8")
        )
        entry = fixture["cpu_classes"]["general-cpu"]
        self.assertEqual(
            set(entry), set(self.contract["class_fields"]["required"])
        )
        self.assertNotIn("pool_id", entry)
        self.assertEqual(entry["pool_resolution"]["mode"], "per-pool-flavor")
        self.assertIn(
            entry["pool_resolution"]["pool_id"], entry["eligible_pool_ids"]
        )
        capacity = entry["schedulable_capacity"]
        self.assertEqual(capacity["cpu"], f"{capacity['cpu_millicores']}m")
        self.assertEqual(capacity["memory"], f"{capacity['memory_mib']}Mi")

    def test_the_rendered_class_uses_the_canonical_shape(self) -> None:
        for field in self.contract["class_fields"]["required"]:
            with self.subTest(field=field):
                self.assertIn(field, self.module)
        self.assertIn("fs2-serve.nebius.ai/cpu-stage-classes/v1", self.module)
        # The actual pool appears only inside pool_resolution.
        self.assertNotIn("pool_id         = local.class_pool_id", self.module)
        # This producer contributes its own class and never another owner's.
        self.assertIn('local.enabled ? { "general-cpu" = local.general_cpu_class }', self.module)
        self.assertNotIn('"reference-data" =', self.module)

    def test_bindcraft_aggregation_is_bound_to_the_general_cpu_class(self) -> None:
        general = self.contract["classes"]["general-cpu"]
        bound = {
            (entry["model_id"], entry["stage"]) for entry in general["bound_workloads"]
        }
        self.assertIn(("bindcraft", "aggregation"), bound)
        self.assertIn(("freebindcraft", "aggregation"), bound)

        reference = self.contract["classes"]["reference-data"]
        reference_bound = {
            (entry["model_id"], entry["stage"]) for entry in reference["bound_workloads"]
        }
        self.assertIn(("alphafold3", "raw-input"), reference_bound)
        # Neither class may claim the other's work.
        self.assertNotIn(("alphafold3", "raw-input"), bound)
        self.assertNotIn(("bindcraft", "aggregation"), reference_bound)

    def test_resolution_is_documented_as_fail_closed(self) -> None:
        self.assertEqual(self.contract["consumer_contract"]["resolution"], "fail-closed")
        self.assertIsNone(self.contract["quota"]["cohort"])
        self.assertRegex(self.workloads, re.compile(r"cohort\s*=\s*null"))

    def test_the_orchestrator_refuses_a_contaminated_pool_handoff(self) -> None:
        self.assertIn("general_cpu_pool_contract", self.stack)
        self.assertIn(
            "a general CPU pool must never carry the reference-data filesystem",
            self.stack,
        )
        self.assertIn("fs2-serve.nebius.ai/general-cpu-pools/v1", self.stack)

    def test_the_class_document_digest_is_reproducible(self) -> None:
        # Terraform's jsonencode sorts object keys, so the same document always
        # produces the same bytes and therefore the same digest a controller
        # recomputes from what it mounted.
        document = {
            "schema": "fs2-serve.nebius.ai/cpu-scheduling-classes/v1",
            "cpu_classes": {},
        }
        first = hashlib.sha256(
            json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        second = hashlib.sha256(
            json.dumps(document, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
