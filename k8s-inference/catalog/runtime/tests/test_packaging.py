from __future__ import annotations

import hashlib
import json
import tomllib
import unittest
from pathlib import Path


CATALOG_ROOT = Path(__file__).resolve().parents[1]


class PackagingContractTests(unittest.TestCase):
    def test_locked_installable_package_contains_runtime_and_validator_contracts(self) -> None:
        project = tomllib.loads((CATALOG_ROOT / "pyproject.toml").read_text())
        self.assertEqual("fs2-serve-catalog", project["project"]["name"])
        self.assertEqual(["cryptography==41.0.7"], project["project"]["dependencies"])
        self.assertEqual(
            "fs2_serve_catalog.validators.validate_response:main",
            project["project"]["scripts"]["fs2-serve-validate-response"],
        )
        packages = project["tool"]["setuptools"]["packages"]
        self.assertIn("fs2_serve_catalog.validators", packages)
        self.assertIn("fs2_serve_catalog.validators.assets", packages)
        data_files = project["tool"]["setuptools"]["data-files"]
        for key in (
            "share/fs2-serve/catalog/contracts",
            "share/fs2-serve/catalog/models",
            "share/fs2-serve/catalog/schema",
            "share/fs2-serve/catalog/sql",
            "share/fs2-serve/catalog/validators",
            "share/fs2-serve/catalog/validators/assets",
        ):
            self.assertIn(key, data_files)

        lock = (CATALOG_ROOT / "uv.lock").read_text()
        self.assertIn('name = "fs2-serve-catalog"', lock)
        self.assertIn('name = "cryptography"', lock)
        self.assertIn('version = "41.0.7"', lock)
        self.assertIn(
            'hash = "sha256:13f93ce9bea8016c253b34afc6bd6a75993e5c40672ed5405a9c832f0d4a00bc"',
            lock,
        )
        self.assertTrue((CATALOG_ROOT / "validators" / "validate_response.py").is_file())
        for relative in (
            "pyproject.toml",
            "uv.lock",
            "contracts/scale-contracts.json",
            "contracts/model-variant-consumer.fixture.json",
            "schema/scale-contracts.schema.json",
            "schema/postgres-activation-intent.schema.json",
            "schema/model-variants.schema.json",
            "schema/model-variant-supply-receipt.schema.json",
            "schema/model-variant-supply-object.schema.json",
            "schema/model-variant-attestor-policy.schema.json",
            "schema/model-variant-qualification-receipt.schema.json",
            "schema/model-variant-promotions.schema.json",
            "schema/model-variant-runtime-tuple.schema.json",
            "schema/model-variant-semantic-receipt.schema.json",
            "schema/model-variant-cohort.schema.json",
            "schema/model-variant-backend-readiness-receipt.schema.json",
            "schema/model-variant-kubernetes-observation.schema.json",
            "schema/model-variant-cold-boundary-receipt.schema.json",
            "schema/model-variant-preemption-receipt.schema.json",
            "schema/model-variant-lifecycle-receipt.schema.json",
            "schema/model-variant-review-receipt.schema.json",
            "schema/protected-storage-class-receipt.schema.json",
            "schema/provider-block-writer-admission.schema.json",
            "schema/zero-to-ready-receipt.schema.json",
            "schema/return-to-zero-receipt.schema.json",
            "schema/provider-block-pvc-lifecycle-receipt.schema.json",
            "schema/runtime-startup-receipt.schema.json",
            "schema/replica-field-ownership-receipt.schema.json",
            "sql/0001_activation_store.sql",
        ):
            self.assertTrue((CATALOG_ROOT / relative).is_file(), relative)
        self.assertEqual(
            8,
            len(list((CATALOG_ROOT / "validators" / "assets").glob("*.json"))),
        )

        packaged_paths: set[str] = set()
        for model_path in (CATALOG_ROOT / "models").glob("*.json"):
            semantic = json.loads(model_path.read_text())["semantic_validator"]
            for path_key, digest_key in (
                ("source_path", "source_sha256"),
                ("fixture_path", "fixture_sha256"),
            ):
                relative = semantic[path_key]
                if relative is None or relative.startswith(
                    "k8s-inference/catalog/runtime/"
                ):
                    continue
                mirror = CATALOG_ROOT / "packaged-repository" / relative
                self.assertTrue(mirror.is_file() and not mirror.is_symlink(), relative)
                self.assertEqual(
                    semantic[digest_key], hashlib.sha256(mirror.read_bytes()).hexdigest()
                )
                packaged_paths.add(relative)
        self.assertEqual(13, len(packaged_paths))


if __name__ == "__main__":
    unittest.main()
