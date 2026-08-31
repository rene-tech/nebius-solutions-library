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
