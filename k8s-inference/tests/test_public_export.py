"""Prevent private checkout details from returning to the public solution."""

from __future__ import annotations

import unittest
from pathlib import Path

from public_export import (
    forbidden_findings,
    project_working_tree_file,
    validate_public_projection,
    working_tree_export_files,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class PublicExportTests(unittest.TestCase):
    def test_export_contains_no_private_references(self) -> None:
        findings: list[str] = []
        for path in working_tree_export_files(REPOSITORY_ROOT, include_untracked=True):
            if not path.exists() and not path.is_symlink():
                continue
            projected = project_working_tree_file(REPOSITORY_ROOT, path)
            try:
                content = projected.content.decode("utf-8")
            except UnicodeDecodeError:
                content = ""
            for label in forbidden_findings(f"{projected.export_path}\n{content}"):
                findings.append(f"{projected.export_path}: {label}")

        self.assertEqual(
            [],
            findings,
            "Public k8s-inference export contains private references:\n"
            + "\n".join(findings),
        )

    def test_redacted_evidence_retains_source_and_export_provenance(self) -> None:
        source = REPOSITORY_ROOT / "k8s-inference/acceptance/admin-apps-20260908/RELEASE.md"
        before = source.read_bytes()

        projected = project_working_tree_file(REPOSITORY_ROOT, source)

        self.assertEqual(before, source.read_bytes())
        self.assertNotEqual(projected.source_sha256, projected.export_sha256)
        self.assertTrue(projected.redactions)
        self.assertEqual([], forbidden_findings(projected.content.decode("utf-8")))
        record = projected.manifest_record()
        self.assertEqual(projected.source_sha256, record["source_sha256"])
        self.assertEqual(projected.export_sha256, record["export_sha256"])
        validate_public_projection([projected])


if __name__ == "__main__":
    unittest.main()
