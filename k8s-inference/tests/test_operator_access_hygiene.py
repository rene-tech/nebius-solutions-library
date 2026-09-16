from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def hcl_blocks(source: str, block_type: str) -> list[tuple[str, str]]:
    labels = (
        r'"([^"]+)"\s+"([^"]+)"'
        if block_type in {"resource", "ephemeral"}
        else r'"([^"]+)"'
    )
    pattern = re.compile(rf"{re.escape(block_type)}\s+{labels}\s*\{{")
    blocks: list[tuple[str, str]] = []
    for match in pattern.finditer(source):
        depth = 0
        end = None
        for index in range(match.end() - 1, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            raise AssertionError(
                f"unterminated {block_type} block {match.group(match.lastindex or 1)}"
            )
        name_group = 2 if block_type in {"resource", "ephemeral"} else 1
        blocks.append((match.group(name_group), source[match.start() : end]))
    return blocks


class OperatorAccessHygieneTests(unittest.TestCase):
    def test_generated_passwords_are_ephemeral(self) -> None:
        sources = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "stages/workloads/secrets.tf",
                "stages/workloads/bootstrap_access.tf",
            )
        )
        self.assertNotIn('resource "random_password"', sources)
        names = {
            name
            for name, _ in hcl_blocks(sources, "ephemeral")
            if f'ephemeral "random_password" "{name}"' in sources
        }
        self.assertEqual(
            names,
            {
                "database",
                "key_material",
                "admin_token",
                "bootstrap_access_token_secret",
                "scientific_access_token_secret",
            },
        )

    def test_every_credential_secret_uses_write_only_data(self) -> None:
        paths = (
            "stages/foundation/cluster_contract.tf",
            "stages/workloads/secrets.tf",
            "stages/workloads/bootstrap_access.tf",
            "stages/workloads/database.tf",
            "stages/workloads/modelexpress.tf",
            "stages/workloads/scientific_artifacts.tf",
        )
        for path in paths:
            source = (ROOT / path).read_text(encoding="utf-8")
            for name, block in hcl_blocks(source, "resource"):
                if not block.startswith('resource "kubernetes_secret_v1"'):
                    continue
                with self.subTest(path=path, resource=name):
                    self.assertRegex(block, r"(?m)^\s*data_wo\s*=")
                    self.assertRegex(block, r"(?m)^\s*data_wo_revision\s*=")
                    self.assertNotRegex(block, r"(?m)^\s*data\s*=")

    def test_sensitive_variables_and_outputs_do_not_persist_values(self) -> None:
        variables = (ROOT / "stages/workloads/variables.tf").read_text(
            encoding="utf-8"
        )
        for name in ("ngc_api_key", "nvcrio_dockerconfigjson"):
            block = dict(hcl_blocks(variables, "variable"))[name]
            self.assertRegex(block, r"(?m)^\s*ephemeral\s*=\s*true$")

        outputs = (ROOT / "stages/workloads/outputs.tf").read_text(encoding="utf-8")
        for output_name in (
            "admin_token",
            "admin_bootstrap_token",
            "mcp_access_token",
            "inference_access_token",
            "scientific_access_token",
            "grafana_admin_username",
            "grafana_admin_password",
        ):
            self.assertNotIn(f'output "{output_name}"', outputs)
        access = dict(hcl_blocks(outputs, "output"))["access_bundle"]
        self.assertIn("access-bundle-contract/v2", access)
        self.assertIn("credential_secret_refs", access)
        self.assertNotIn("random_password", access)
        self.assertNotIn(".data[", access)

    def test_control_plane_allowlist_and_viewer_handoff_fail_closed(self) -> None:
        root_variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        infrastructure_variables = (
            ROOT / "stages/infrastructure/variables.tf"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "length(var.deployment.cluster.control_plane_allowed_cidrs) >= 1",
            root_variables,
        )
        self.assertIn('toset(["0.0.0.0/0", "::/0"])', root_variables)
        allowlist = dict(hcl_blocks(infrastructure_variables, "variable"))[
            "control_plane_allowed_cidrs"
        ]
        self.assertNotRegex(allowlist, r"(?m)^\s*default\s*=")
        self.assertIn("length(var.control_plane_allowed_cidrs) >= 1", allowlist)
        self.assertIn(
            '!contains(var.control_plane_allowed_cidrs, "0.0.0.0/0")', allowlist
        )
        self.assertIn('!contains(var.control_plane_allowed_cidrs, "::/0")', allowlist)

        iam = (ROOT / "stages/infrastructure/iam.tf").read_text(encoding="utf-8")
        resources = dict(hcl_blocks(iam, "resource"))
        handoff = resources["operator_handoff_viewer"]
        self.assertIn('role        = "viewer"', handoff)
        self.assertNotRegex(handoff, r'role\s*=\s*"(?:editor|admin)"')
        self.assertIn("operator_handoff", resources)
        self.assertIn("operator_handoff_viewers", resources)


if __name__ == "__main__":
    unittest.main()
