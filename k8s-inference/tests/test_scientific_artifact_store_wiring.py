"""The Terraform-to-chart wiring for the scientific artifact store.

A key name that Terraform emits but the chart does not declare would be
silently ignored by Helm and leave the store unconfigured at runtime, so the
two sides are compared directly here rather than trusted to stay in step.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

DEPLOY_ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = DEPLOY_ROOT / "stages/workloads/scientific_artifacts.tf"
CHART = DEPLOY_ROOT / "charts/control-plane/fs2-serve-control-plane"
VALUES_SCHEMA = CHART / "values.schema.json"
VALUES = CHART / "values.yaml"
HELPERS = CHART / "templates/_helpers.tpl"


def emitted_keys(block: str) -> set[str]:
    return set(re.findall(r"^\s+(\w+)\s*=", block, flags=re.MULTILINE))


class ScientificArtifactStoreWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.terraform = WORKLOADS.read_text(encoding="utf-8")
        cls.schema = json.loads(VALUES_SCHEMA.read_text(encoding="utf-8"))
        cls.values = VALUES.read_text(encoding="utf-8")
        cls.helpers = HELPERS.read_text(encoding="utf-8")

    def emitted_chart_values(self) -> set[str]:
        start = self.terraform.index("scientificArtifacts = {")
        end = self.terraform.index("secrets = {", start)
        return emitted_keys(self.terraform[start:end]) - {"scientificArtifacts"}

    def chart_properties(self) -> dict[str, object]:
        return self.schema["properties"]["scientificArtifacts"]["properties"]

    def test_every_terraform_override_is_a_declared_chart_value(self) -> None:
        emitted = self.emitted_chart_values()
        self.assertTrue(emitted, "the workloads stage must emit chart values")
        declared = set(self.chart_properties())
        self.assertEqual(
            emitted - declared,
            set(),
            "Terraform emits chart values the control-plane chart does not declare",
        )

    def test_every_required_chart_value_is_supplied_by_terraform(self) -> None:
        emitted = self.emitted_chart_values()
        required = set(self.schema["properties"]["scientificArtifacts"]["required"])
        # egressCidrs is the one value the chart defaults to empty on purpose.
        self.assertEqual(required - emitted, set())

    def test_the_secret_reference_matches_the_terraform_owned_secret(self) -> None:
        self.assertIn('scientific_artifacts_secret  = "fs2-serve-artifact-store"', self.terraform)
        self.assertIn("name = local.scientific_artifacts_secret", self.terraform)
        self.assertIn('key  = "credentials.json"', self.terraform)
        self.assertIn("artifactStore", self.schema["properties"]["secrets"]["properties"])
        self.assertIn("fs2-serve-artifact-store", self.values)

    def test_the_credential_is_written_write_only_and_never_persisted(self) -> None:
        # data_wo keeps the value out of state; plain data would persist it.
        self.assertIn("data_wo = {", self.terraform)
        self.assertIn("data_wo_revision = local.scientific_artifacts_credential_revision", self.terraform)
        self.assertNotIn("\n  data = {", self.terraform)
        self.assertIn("ephemeral = true", (DEPLOY_ROOT / "stages/workloads/variables.tf").read_text(encoding="utf-8"))

    def test_the_chart_mounts_the_credential_read_only_and_never_as_env(self) -> None:
        self.assertIn("FS2_ARTIFACT_STORE_CREDENTIALS_FILE", self.helpers)
        self.assertNotIn("FS2_ARTIFACT_STORE_SECRET_KEY", self.helpers)
        self.assertNotIn("FS2_ARTIFACT_STORE_ACCESS_KEY", self.helpers)
        self.assertIn("defaultMode: 0400", self.helpers)

    def test_large_integers_are_rendered_as_integers_not_scientific_notation(self) -> None:
        # Helm parses YAML numbers as float64; unguarded values become 1e+12.
        for key in ("handleTtlSeconds", "maxBytes", "retentionSeconds"):
            with self.subTest(value=key):
                self.assertIn(
                    f"value: {{{{ .Values.scientificArtifacts.{key} | int64 | quote }}}}",
                    self.helpers,
                )


if __name__ == "__main__":
    unittest.main()
