"""Committed evidence must be exportable and must say so truthfully.

The repository-wide export gate catches a few regex classes. These tests are
stricter and local: a committed evidence file may not carry any identity of one
live deployment, and it must still carry the evidence that makes it worth keeping.
A file that claims identities are withheld while carrying them is the specific
contradiction guarded against here.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from public_identity_classes import (  # noqa: E402
    IDENTITY_FIELDS,
    IDENTITY_PARENTS,
    WITHHELD,
    findings,
    walk,
)

EVIDENCE = Path(__file__).resolve().parents[1] / "evidence"
PUBLIC_FILES = sorted(EVIDENCE.glob("*.json"))
ACCEPTANCE = EVIDENCE / "live-acceptance-state.json"
STAGING = EVIDENCE / "live-private-cache-staging-20260902.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


class PublicEvidenceIdentityTests(unittest.TestCase):
    def test_every_evidence_file_is_present(self) -> None:
        self.assertTrue(PUBLIC_FILES, "no committed evidence to check")

    def test_no_evidence_file_exports_a_deployment_identity(self) -> None:
        for path in PUBLIC_FILES:
            with self.subTest(evidence=path.name):
                self.assertEqual([], findings(load(path)))

    def test_identity_fields_are_withheld_rather_than_deleted(self) -> None:
        """Withholding is visible; silently dropping a field is not."""
        withheld = 0
        for path in PUBLIC_FILES:
            for _, key, value in walk(load(path)):
                if key in IDENTITY_FIELDS and value == WITHHELD:
                    withheld += 1
        self.assertGreater(withheld, 0, "no identity field is marked withheld")

    def test_each_sanitized_file_states_why(self) -> None:
        for path in (ACCEPTANCE, STAGING, EVIDENCE / "read-only-discovery-20260902.json"):
            with self.subTest(evidence=path.name):
                note = load(path).get("public_export_note", "")
                self.assertIn("withheld", note.lower())
                self.assertIn("private Task Deck evidence", note)

    def test_identity_classes_cover_what_the_reviewer_named(self) -> None:
        for field in ("registry_id", "pv_uid", "pvc_uid", "project_id", "cluster_context"):
            self.assertIn(field, IDENTITY_FIELDS)
        self.assertIn("private_registry", IDENTITY_PARENTS)

    def test_a_reintroduced_identity_would_be_caught(self) -> None:
        """Probe values are assembled at runtime so this file stays exportable itself."""

        cluster = "mk8scluster-" + "e00" + "j5z9te7x5dd9g6a"
        registry = "registry-" + "e00" + "akg9ndpx77eaexh"
        volume = "11111111-2222-3333-4444-555555555555"
        home = "/" + "home" + "/someone/.kube/config"
        legacy = "fs2-" + "platform/" + "fs2-serve" + "-control-plane"
        host = "cr." + "eu-north1.nebius.cloud/tenant/model"
        for probe in (
            {"target": {"cluster_id": cluster}},
            {"canonical_volume": {"pvc_uid": volume}},
            {"environment": {"registry_id": registry}},
            {"private_registry": {"name": "some-live-cluster"}},
            {"image": {"repository": host}},
            {"target": {"kubeconfig": home}},
            {"transfer": {"loader": legacy}},
        ):
            with self.subTest(probe=probe):
                self.assertNotEqual([], findings(probe))


class PublicEvidenceRetentionTests(unittest.TestCase):
    """Sanitizing must not have taken the evidence with it."""

    def setUp(self) -> None:
        self.acceptance = load(ACCEPTANCE)
        self.staging = load(STAGING)

    def test_exact_asset_hashes_and_sizes_survive(self) -> None:
        expected = {
            "alphafold3": ("74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff", 1020545840),
            "pyrosetta-bindcraft": ("4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242", 1667097173),
        }
        artifacts = self.staging["source_artifacts"]
        for asset, (digest, size) in expected.items():
            with self.subTest(asset=asset):
                self.assertEqual(digest, artifacts[asset]["sha256"])
                self.assertEqual(size, artifacts[asset]["size_bytes"])

    def test_semantic_evidence_survives(self) -> None:
        semantic = self.acceptance["semantic_evidence"]
        alphafold3 = semantic["alphafold3"]
        self.assertEqual(405, alphafold3["loaded_parameter_arrays"])
        self.assertEqual(368384602, alphafold3["loaded_parameter_elements"])
        self.assertIn("H100", alphafold3["gpu"])
        self.assertIn("denied", alphafold3["egress"])
        pyrosetta = semantic["pyrosetta-bindcraft"]
        self.assertEqual("2026.29+releasequarterly.80a0635615", pyrosetta["installed_distribution_version"])
        self.assertIn("29.967244", pyrosetta["functional_proof"])
        self.assertEqual(8697, semantic["installed_tree"]["files_installed"])
        self.assertEqual(0, semantic["installed_tree"]["world_or_group_writable_paths"])

    def test_neutral_resource_kinds_survive(self) -> None:
        volume = self.acceptance["canonical_volume"]
        self.assertEqual("csi-mounted-fs-path-sc", volume["storage_class"])
        self.assertEqual(["ReadWriteMany"], volume["access_modes"])
        self.assertEqual("Bound", volume["phase"])
        self.assertEqual(65532, volume["asset_gid"])
        self.assertEqual("/opt/fs2/academic", volume["mount_root"])

    def test_runtime_image_digest_and_version_survive(self) -> None:
        image = self.acceptance["runtime_images"]["alphafold3"]
        self.assertEqual(
            "sha256:eaea560ce2ddba8d828371d1cba01da954d9a68ff5e77ba4d43b36b107141887", image["digest"]
        )
        self.assertEqual("3.0.4", image["packaged_distribution_version"])
        self.assertFalse(image["contains_licensed_bytes"])

    def test_authorized_academic_status_and_mount_semantics_survive(self) -> None:
        authorization = self.staging["authorization"]
        self.assertTrue(authorization["artifact_acquisition_and_private_quarantine"])
        self.assertFalse(authorization["license_terms_acceptance_recorded"])
        safety = self.staging["confidentiality"]
        self.assertFalse(safety["licensed_bytes_in_git_images_or_receipts"])
        self.assertFalse(safety["credentials_in_git_logs_or_receipts"])


if __name__ == "__main__":
    unittest.main()
