"""Validate the portable solution export without rewriting operator evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import public_export as export  # noqa: E402

# Kept for local diagnostic consumers of the former test-only scanner.
REPOSITORY_ROOT = export.REPOSITORY_ROOT
FORBIDDEN_REFERENCES = export.FORBIDDEN_REFERENCES
text_content = export.text_content
export_files = export.export_files


class PublicExportTests(unittest.TestCase):
    def test_export_contains_no_private_references(self) -> None:
        self.assertTrue(export.validate())

    def fixture(self, root: Path, *, historical: bool = True) -> tuple[Path, Path]:
        subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S603,S607 - isolated temporary fixture
        source = root / "k8s-inference/example.py"
        source.parent.mkdir()
        source.write_text("print('portable')\n")
        record = root / "k8s-inference/acceptance/run/receipt.json"
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({"location": "/" + "home/operator/old-run"}))
        policy = root / export.HISTORY_POLICY
        policy.parent.mkdir(parents=True)
        policy.write_text(
            json.dumps(
                {
                    "schema": "scientific-ai/public-export-history/v1",
                    "files": {
                        record.relative_to(root).as_posix(): {
                            "sha256": hashlib.sha256(record.read_bytes()).hexdigest(),
                            "reason": "historical-operator-record",
                        }
                    }
                    if historical
                    else {},
                }
            )
        )
        return source, record

    def test_export_omits_reviewed_history_but_preserves_its_original_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            source, record = self.fixture(root)
            before = record.read_bytes()
            target = Path(folder) / "export"
            manifest = export.write_export(root, target)
            self.assertEqual(record.read_bytes(), before)
            self.assertEqual((target / source.relative_to(root)).read_bytes(), source.read_bytes())
            self.assertFalse((target / record.relative_to(root)).exists())
            self.assertEqual(set(manifest), {"k8s-inference/example.py"})
            self.assertTrue((target / "public-export-manifest.json").is_file())

    def test_changed_history_requires_review_not_silent_exemption(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, record = self.fixture(root)
            record.write_text(record.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "history changed"):
                export.validate(root)

    def test_uninventoried_history_still_fails_export(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root, historical=False)
            with self.assertRaisesRegex(ValueError, "Non-portable references"):
                export.validate(root)

    def test_runtime_sources_cannot_be_hidden_in_history_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, _ = self.fixture(root)
            target = root / "k8s-inference/components/control-plane/src/hidden.py"
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())
            policy_path = root / export.HISTORY_POLICY
            policy = json.loads(policy_path.read_text())
            policy["files"][target.relative_to(root).as_posix()] = {
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "reason": "not permitted",
            }
            policy_path.write_text(json.dumps(policy))
            with self.assertRaisesRegex(ValueError, "invalid operator-history"):
                export.validate(root)

    def test_operator_directory_name_cannot_hide_application_code(self) -> None:
        self.assertFalse(export.is_operator_record("k8s-inference/components/control-plane/src/evidence/hidden.py"))
        self.assertFalse(export.is_operator_record("k8s-inference/catalog/runtime/contracts/qualification/hidden.json"))

    def test_private_source_change_fails_before_creating_export(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            source, _ = self.fixture(root)
            source.write_text("checkout = '" + "/" + "home/operator/private" + "'\n")
            target = Path(folder) / "export"
            with self.assertRaisesRegex(ValueError, "Non-portable references"):
                export.write_export(root, target)
            self.assertFalse(target.exists())

    def test_container_account_and_registry_names_are_not_developer_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "Containerfile"
            path.write_text(
                "RUN useradd --home-dir /" + "home/fs2 fs2\n"
                "FROM registry.test/fs2-platform/" + "fs2-serve-control-plane\n"
            )
            self.assertEqual(export.findings(path, root), [])
            path.write_text("COPY /" + "home/alice/private /build\n")
            self.assertTrue(export.findings(path, root))

    def test_overwrite_of_existing_export_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            self.fixture(root)
            target = Path(folder) / "export"
            target.mkdir()
            marker = target / "keep"
            marker.write_text("unchanged")
            with self.assertRaises(FileExistsError):
                export.write_export(root, target)
            self.assertEqual(marker.read_text(), "unchanged")


if __name__ == "__main__":
    unittest.main()
