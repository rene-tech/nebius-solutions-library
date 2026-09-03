"""Cross-contract tests between the private academic contract and model onboarding.

The two sides legitimately name the same object differently: the private contract
calls it asset `alphafold3` under `/opt/fs2/academic/alphafold3`, while onboarding
addresses artifact `alphafold3-parameters` at `/models/af3.bin.zst`. That is fine
as long as both point at the same verified bytes and the localizer says how. A
divergence in digest, size, filename or path must fail here rather than at runtime.

Onboarding owns its declarations, so when they are present in the tree these tests
check the live agreement; the checked-in expectation file keeps the interface
pinned either way.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import academic_assets as aa  # noqa: E402

ASSET_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ASSET_ROOT.parent
CONTRACT = ASSET_ROOT / "contracts" / "academic-assets.json"
EXPECTATIONS = ASSET_ROOT / "contracts" / "onboarding-binding-expectations.json"
PROJECTION = REPO_ROOT / "catalog/runtime/contracts/academic-asset-readiness.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


class OnboardingBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = aa.load_contract(CONTRACT)
        self.expectations = load(EXPECTATIONS)
        self.by_asset = {item["asset_id"]: item for item in self.expectations["expectations"]}

    def test_every_asset_has_an_onboarding_expectation(self) -> None:
        self.assertEqual(set(self.contract["assets"]), set(self.by_asset))

    def test_binding_matches_what_onboarding_addresses(self) -> None:
        for asset_id, expected in self.by_asset.items():
            binding = self.contract["assets"][asset_id]["delivery"]["runtime_binding"]
            with self.subTest(asset=asset_id):
                self.assertEqual(expected["artifact_id"], binding["artifact_id"])
                self.assertEqual(expected["source_sub_path"], binding["source_sub_path"])
                self.assertEqual(expected["consumer_path"], binding["consumer_path"])
                self.assertEqual(expected["mechanism"], binding["mechanism"])

    def test_binding_points_at_the_same_verified_bytes(self) -> None:
        for asset_id, expected in self.by_asset.items():
            artifact = self.contract["assets"][asset_id]["artifact"]
            with self.subTest(asset=asset_id):
                self.assertEqual(expected["content_digest_sha256"], artifact["sha256"])
                self.assertEqual(expected["total_size_bytes"], artifact["size_bytes"])
                self.assertIn(artifact["filename"], expected["required_files"])

    def test_a_directory_binding_carries_tree_identity_not_archive_identity(self) -> None:
        for asset_id, expected in self.by_asset.items():
            binding = self.contract["assets"][asset_id]["delivery"]["runtime_binding"]
            artifact = self.contract["assets"][asset_id]["artifact"]
            with self.subTest(asset=asset_id):
                if binding["mechanism"] == "subpath-directory-mount":
                    self.assertEqual("tree-manifest", binding["content_identity_kind"])
                    self.assertIsNone(binding["content_digest_sha256"])
                    self.assertIsNone(binding["size_bytes"])
                else:
                    self.assertEqual("file-digest", binding["content_identity_kind"])
                    self.assertEqual(artifact["sha256"], binding["content_digest_sha256"])
                # The source archive is recorded separately either way.
                self.assertEqual(artifact["sha256"], binding["source_artifact"]["sha256"])
                self.assertEqual(artifact["size_bytes"], binding["source_artifact"]["size_bytes"])

    def test_published_projection_never_labels_a_tree_with_archive_identity(self) -> None:
        projection = load(PROJECTION)
        for model in projection["models"]:
            binding = model["runtime_binding"]
            if binding["content_identity_kind"] != "tree-manifest":
                continue
            with self.subTest(model=model["model_id"]):
                self.assertIsNotNone(binding["content_digest_sha256"])
                self.assertNotEqual(binding["source_artifact"]["sha256"], binding["content_digest_sha256"])
                self.assertNotEqual(binding["source_artifact"]["size_bytes"], binding["content_bytes"])

    def test_localizer_never_duplicates_or_embeds(self) -> None:
        for asset_id in self.by_asset:
            binding = self.contract["assets"][asset_id]["delivery"]["runtime_binding"]
            with self.subTest(asset=asset_id):
                self.assertFalse(binding["duplicates_bytes"])
                self.assertFalse(binding["embeds_bytes"])
                self.assertTrue(binding["read_only"])
                self.assertTrue(binding["mechanism"].startswith("subpath-"))

    def test_alphafold3_is_addressable_exactly_as_onboarding_requests(self) -> None:
        """The specific divergence this contract exists to reconcile."""

        binding = self.contract["assets"]["alphafold3"]["delivery"]["runtime_binding"]
        self.assertEqual("alphafold3-parameters", binding["artifact_id"])
        self.assertEqual("alphafold3/af3.bin.zst", binding["source_sub_path"])
        self.assertEqual("/models/af3.bin.zst", binding["consumer_path"])
        # The private mount root is unchanged; the localizer is additive.
        self.assertEqual(
            "/opt/fs2/academic/alphafold3",
            self.contract["assets"]["alphafold3"]["delivery"]["mount_path"],
        )

    def test_catalog_projection_publishes_the_binding(self) -> None:
        projection = load(PROJECTION)
        by_model = {model["model_id"]: model for model in projection["models"]}
        for asset_id, expected in self.by_asset.items():
            model_id = self.contract["assets"][asset_id]["model_id"]
            with self.subTest(model=model_id):
                published = by_model[model_id]["runtime_binding"]
                self.assertEqual(expected["artifact_id"], published["artifact_id"])
                self.assertEqual(expected["consumer_path"], published["consumer_path"])

    def test_stock_image_is_evidence_and_not_the_final_wrapper(self) -> None:
        image = self.contract["assets"]["alphafold3"]["runtime"]["runtime_image"]
        self.assertEqual("historical-semantic-evidence", image["role"])
        self.assertFalse(image["final_wrapper"])
        # An evidence image must never be published as the model's runtime image.
        projection = load(PROJECTION)
        alphafold3 = next(m for m in projection["models"] if m["model_id"] == "alphafold3")
        self.assertIsNone(alphafold3["runtime_image_digest"])
        self.assertIsNotNone(alphafold3["runtime_environment_digest"])
        self.assertEqual("historical-semantic-evidence", alphafold3["runtime_invocation"]["image_role"])
        self.assertFalse(alphafold3["runtime_invocation"]["image_is_final_wrapper"])


class LiveOnboardingDeclarationTests(unittest.TestCase):
    """Checks the real declarations when the onboarding work is present in the tree."""

    def setUp(self) -> None:
        self.expectations = load(EXPECTATIONS)
        self.root = REPO_ROOT / self.expectations["declaration_root"]

    def declaration(self, name: str) -> dict | None:
        path = self.root / name
        return load(path) if path.is_file() else None

    def test_declared_artifacts_agree_with_the_binding(self) -> None:
        checked = 0
        for expected in self.expectations["expectations"]:
            document = self.declaration(expected["declaration"])
            if document is None:
                continue
            artifacts = {item["artifact_id"]: item for item in document["model"].get("artifacts", [])}
            with self.subTest(declaration=expected["declaration"]):
                self.assertIn(
                    expected["artifact_id"],
                    artifacts,
                    "onboarding no longer declares the artifact this binding localizes",
                )
                artifact = artifacts[expected["artifact_id"]]
                if artifact.get("content_digest_sha256") is not None:
                    self.assertEqual(expected["content_digest_sha256"], artifact["content_digest_sha256"])
                if artifact.get("total_size_bytes") is not None:
                    self.assertEqual(expected["total_size_bytes"], artifact["total_size_bytes"])
                for required in artifact.get("required_files", []):
                    self.assertIn(required, expected["required_files"])
            checked += 1
        if checked == 0:
            self.skipTest("model onboarding declarations are not present in this tree yet")


if __name__ == "__main__":
    unittest.main()
