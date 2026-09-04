from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "validate_fragments.py"
SPEC = importlib.util.spec_from_file_location("primary_activation", MODULE_PATH)
assert SPEC and SPEC.loader
activation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activation)


class PrimaryActivationFragmentTests(unittest.TestCase):
    def test_all_fragments_validate(self) -> None:
        for model_id, path in activation.FRAGMENTS.items():
            with self.subTest(model_id=model_id):
                _, errors = activation.validate_fragment(path)
                self.assertEqual(errors, [])

    def test_render_is_deterministic_and_does_not_include_shared_wrapper(self) -> None:
        fragment, errors = activation.validate_fragment(
            activation.FRAGMENTS["proteina-complexa"]
        )
        self.assertEqual(errors, [])
        first = json.dumps(
            activation.render(fragment), sort_keys=True, separators=(",", ":")
        )
        second = json.dumps(
            activation.render(fragment), sort_keys=True, separators=(",", ":")
        )
        self.assertEqual(first, second)
        self.assertNotIn('"profiles"', first)
        self.assertNotIn('"models"', first)

    def test_missing_generation_is_fail_closed(self) -> None:
        for model_id in ("mosaic", "rfdiffusion"):
            fragment, errors = activation.validate_fragment(
                activation.FRAGMENTS[model_id]
            )
            self.assertEqual(errors, [])
            projection = fragment["execution_projection"]
            self.assertEqual(
                projection["state"], "blocked-missing-canonical-generation"
            )
            self.assertTrue(projection["blockers"])
            for artifact in projection["runtime_artifacts"]:
                self.assertEqual(
                    artifact["state"], "blocked-missing-canonical-generation"
                )
                self.assertIsNone(artifact["generation"])
                self.assertIsNone(artifact["source"]["sub_path"])

    def test_ready_bindings_use_read_only_generation_subpaths(self) -> None:
        for model_id in ("proteina-complexa", "bindcraft"):
            fragment, errors = activation.validate_fragment(
                activation.FRAGMENTS[model_id]
            )
            self.assertEqual(errors, [])
            for artifact in fragment["execution_projection"]["runtime_artifacts"]:
                self.assertEqual(artifact["state"], "ready")
                self.assertTrue(artifact["source"]["read_only"])
                self.assertIn(
                    f"generations/{artifact['artifact_id']}/sha256/",
                    artifact["source"]["sub_path"],
                )

    def test_recipes_are_derived_from_the_exact_integration_source(self) -> None:
        registry = (
            "components/control-plane/src/fs2_serve/"
            "scientific_batch/adapters/__init__.py"
        )
        for model_id, path in activation.FRAGMENTS.items():
            with self.subTest(model_id=model_id):
                fragment, errors = activation.validate_fragment(path)
                self.assertEqual(errors, [])
                recipe = fragment["accepted_evidence"]["runtime_recipe"]
                profile = fragment["profile_projection"]["profile"]
                identity = profile["execution_identity"]
                self.assertEqual(
                    recipe["source_revision"], activation.INTEGRATION_SOURCE_REVISION
                )
                self.assertIn(registry, recipe["paths"])
                self.assertEqual(
                    identity["runtime_recipe_sha256"],
                    activation.runtime_recipe_sha256(recipe["paths"]),
                )
                workload_digest = hashlib.sha256(
                    json.dumps(
                        profile["workload"], sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest()
                self.assertEqual(identity["workload_recipe_sha256"], workload_digest)

        profiles = activation.load_json(
            activation.PROFILE_SCHEMA.parent.parent
            / "contracts/scientific-workload-profiles.json"
        )
        canonical = next(
            item
            for item in profiles["profiles"]
            if item["model_id"] == "proteina-complexa"
        )
        fragment = activation.load_json(activation.FRAGMENTS["proteina-complexa"])
        self.assertEqual(
            fragment["profile_projection"]["profile"]["execution_identity"][
                "runtime_recipe_sha256"
            ],
            canonical["execution_identity"]["runtime_recipe_sha256"],
        )

    def test_shared_aggregates_are_untouched(self) -> None:
        self.assertEqual(activation.validate_no_aggregate_edits(), [])


if __name__ == "__main__":
    unittest.main()
