from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


INFRA_ROOT = Path(__file__).resolve().parents[1]
EDGE = re.compile(r'^\s*"(?P<source>.+)" -> "(?P<target>.+)"$')


class GpuBootstrapGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        terraform = shutil.which("terraform")
        if terraform is None:
            raise unittest.SkipTest("terraform is required for dependency-graph tests")

        cls.temporary = tempfile.TemporaryDirectory(prefix="fs2-infra-graph-")
        cls.addClassCleanup(cls.temporary.cleanup)
        run_root = Path(cls.temporary.name)
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("TF_VAR_")
            and not key.startswith("TF_CLI_ARGS")
            and key not in {"TF_DATA_DIR", "TF_WORKSPACE"}
        }
        environment.update(
            {
                "TF_DATA_DIR": str(run_root / "terraform-data"),
                "TF_IN_AUTOMATION": "1",
            }
        )

        initialized = subprocess.run(
            [
                terraform,
                f"-chdir={INFRA_ROOT}",
                "init",
                "-input=false",
                "-no-color",
                "-reconfigure",
                f"-backend-config=path={run_root / 'empty.tfstate'}",
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
        if initialized.returncode != 0:
            raise AssertionError(
                "terraform init failed for dependency graph:\n"
                f"{initialized.stdout}\n{initialized.stderr}"
            )

        graphed = subprocess.run(
            [terraform, f"-chdir={INFRA_ROOT}", "graph", "-type=plan"],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
        if graphed.returncode != 0:
            raise AssertionError(
                "terraform graph failed:\n"
                f"{graphed.stdout}\n{graphed.stderr}"
            )
        cls.edges = {
            (match["source"], match["target"])
            for line in graphed.stdout.splitlines()
            if (match := EDGE.fullmatch(line)) is not None
        }

    def test_gpu_software_releases_wait_for_system_capacity(self) -> None:
        system = "[root] nebius_mk8s_v1_node_group.system (expand)"
        for module in ("device_plugin", "gpu_operator", "network_operator"):
            with self.subTest(module=module):
                self.assertIn((f"[root] module.{module} (expand)", system), self.edges)

    def test_system_capacity_waits_for_all_registry_access(self) -> None:
        system = "[root] nebius_mk8s_v1_node_group.system (expand)"
        for resource in (
            "nebius_iam_v1_group_membership.nodepull_target_registry",
            "nebius_iam_v1_group_membership.nodepull_external_registry",
            "nebius_iam_v1_access_permit.nodepull_registry",
            "nebius_iam_v1_access_permit.nodepull_external_registry",
        ):
            with self.subTest(resource=resource):
                self.assertIn((system, f"[root] {resource} (expand)"), self.edges)

    def test_gpu_capacity_waits_for_software_release_completion(self) -> None:
        for node_group in ("gpu", "nvlink_rack"):
            source = f"[root] nebius_mk8s_v1_node_group.{node_group} (expand)"
            for module in ("device_plugin", "gpu_operator", "network_operator"):
                with self.subTest(node_group=node_group, module=module):
                    self.assertIn(
                        (source, f"[root] module.{module} (close)"), self.edges
                    )


if __name__ == "__main__":
    unittest.main()


class ScientificArtifactStoreGraphTests(GpuBootstrapGraphTests):
    """Ordering proofs for the opt-in durable scientific result store."""

    def test_the_access_key_waits_for_the_scoped_bucket_permit(self) -> None:
        key = "[root] nebius_iam_v2_access_key.scientific_artifacts (expand)"
        permit = "[root] nebius_iam_v1_access_permit.scientific_artifacts_writer (expand)"
        self.assertIn((key, permit), self.edges)

    def reaches(self, source: str, target: str) -> bool:
        """Whether Terraform must create target before source, directly or not.

        The bucket identity reaches the permit through a local value, so the
        ordering is a path rather than a single edge.
        """

        seen: set[str] = set()
        frontier = [source]
        while frontier:
            node = frontier.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(child for parent, child in self.edges if parent == node)
        return False

    def test_the_permit_waits_for_the_bucket_and_the_writer_group(self) -> None:
        permit = "[root] nebius_iam_v1_access_permit.scientific_artifacts_writer (expand)"
        for resource in (
            "nebius_storage_v1_bucket.scientific_artifacts",
            "nebius_iam_v1_group.scientific_artifacts_writers",
        ):
            with self.subTest(resource=resource):
                self.assertTrue(self.reaches(permit, f"[root] {resource} (expand)"))

    def test_every_artifact_cloud_resource_waits_for_the_validated_target(self) -> None:
        target = "[root] terraform_data.target_contract (expand)"
        receipt = "[root] terraform_data.scientific_artifacts_contract (expand)"
        for resource in (
            "nebius_storage_v1_bucket.scientific_artifacts",
            "nebius_iam_v1_service_account.scientific_artifacts_writer",
            "nebius_iam_v1_group.scientific_artifacts_writers",
        ):
            with self.subTest(resource=resource):
                node = f"[root] {resource} (expand)"
                self.assertIn((node, target), self.edges)
                self.assertIn((node, receipt), self.edges)

    def test_the_result_store_is_independent_of_the_disposable_model_cache(self) -> None:
        cache = "[root] nebius_compute_v1_filesystem.cache (expand)"
        for resource in (
            "nebius_storage_v1_bucket.scientific_artifacts",
            "nebius_iam_v2_access_key.scientific_artifacts",
        ):
            with self.subTest(resource=resource):
                node = f"[root] {resource} (expand)"
                self.assertNotIn((node, cache), self.edges)
                self.assertNotIn((cache, node), self.edges)
