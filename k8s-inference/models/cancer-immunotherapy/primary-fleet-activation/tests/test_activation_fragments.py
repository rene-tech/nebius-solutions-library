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
        """A blocked artifact must stay unbound and a ready one must bind exactly.

        Every fragment now carries real canonical generations, so the fail-closed
        behaviour is asserted against synthetic mutations rather than against
        whichever model happens to be unlocalized today. Each probe starts from a
        clean copy, because the validator reports several independent errors and
        reusing one mutated document would test a different fragment each time.
        """
        path = activation.FRAGMENTS["rfdiffusion"]
        base = activation.load_json(path)

        def probe(mutate) -> list[str]:
            fragment = json.loads(json.dumps(base))
            mutate(fragment)
            return activation.validate_fragment_document(fragment, path)

        def unbind(artifact) -> None:
            artifact["generation"] = None
            artifact["content_digest"] = None
            artifact["source"]["root"] = None
            artifact["source"]["sub_path"] = None
            artifact["source"]["kind"] = "unresolved"

        # A blocked artifact may not keep its generation binding.
        errors = probe(
            lambda f: f["execution_projection"]["runtime_artifacts"][0].update(
                state="blocked-missing-canonical-generation"
            )
        )
        self.assertIn(
            "rfdiffusion/rfdiffusion-base-checkpoint: blocked artifact invents a generation binding",
            errors,
        )
        self.assertIn(
            "rfdiffusion/rfdiffusion-base-checkpoint: blocked artifact must be unresolved",
            errors,
        )

        # A ready projection may not contain a blocked artifact.
        def block_artifact_only(fragment):
            artifact = fragment["execution_projection"]["runtime_artifacts"][0]
            artifact["state"] = "blocked-missing-canonical-generation"
            unbind(artifact)

        self.assertIn(
            "rfdiffusion: ready execution has blocked artifacts or blockers",
            probe(block_artifact_only),
        )

        # A consistently blocked fragment is accepted, so the guard is not
        # merely rejecting everything.
        def block_fully(fragment):
            block_artifact_only(fragment)
            projection = fragment["execution_projection"]
            projection["state"] = "blocked-missing-canonical-generation"
            projection["blockers"] = ["synthetic blocker"]

        self.assertEqual(probe(block_fully), [])

        # A ready artifact must name its own exact generation sub-path.
        def wrong_generation(fragment):
            artifact = fragment["execution_projection"]["runtime_artifacts"][0]
            artifact["source"]["sub_path"] = (
                f"scientific-localization/public/generations/{artifact['artifact_id']}/sha256/{'0' * 64}"
            )

        self.assertIn(
            "rfdiffusion/rfdiffusion-base-checkpoint: generation subPath is not exact",
            probe(wrong_generation),
        )

    def test_ready_bindings_use_read_only_generation_subpaths(self) -> None:
        for model_id in (
            "boltzgen",
            "proteina-complexa",
            "bindcraft",
            "mosaic",
            "rfdiffusion",
        ):
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
        registry = "components/control-plane/src/fs2_serve/scientific_batch/adapters/__init__.py"
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
        canonical_by_id = {item["model_id"]: item for item in profiles["profiles"]}
        for model_id, path in activation.FRAGMENTS.items():
            with self.subTest(serialized_identity=model_id):
                fragment = activation.load_json(path)
                projected = fragment["profile_projection"]["profile"][
                    "execution_identity"
                ]
                canonical = canonical_by_id[model_id]["execution_identity"]
                self.assertEqual(
                    projected["runtime_recipe_sha256"],
                    canonical["runtime_recipe_sha256"],
                )
                self.assertEqual(
                    projected["workload_recipe_sha256"],
                    canonical["workload_recipe_sha256"],
                )

    def test_shared_aggregates_carry_no_authored_change(self) -> None:
        self.assertEqual(activation.validate_no_aggregate_edits(), [])

    def test_serialized_boltz_mount_cleanup_allowance_is_exact_and_atomic(self) -> None:
        relative = "catalog/runtime/contracts/scientific-execution-map.json"
        source = activation._aggregate_at_baseline(relative)

        def clone(value):
            return json.loads(json.dumps(value))

        accepted_baseline = clone(source)
        accepted_current = clone(source)
        boltz = next(
            item
            for item in accepted_current["models"]
            if item["model_id"] == "boltzgen"
        )
        for stage in boltz["stages"]:
            if stage["stage_id"] in activation.BOLTZGEN_GPU_STAGES:
                stage["mounts"].remove(activation.BOLTZGEN_LEGACY_BROAD_MOUNT)
        activation._normalize_serialized_boltzgen_mount_cleanup(
            relative, accepted_baseline, accepted_current
        )
        self.assertEqual(accepted_baseline, accepted_current)

        partial_baseline = clone(source)
        partial_current = clone(source)
        boltz = next(
            item for item in partial_current["models"] if item["model_id"] == "boltzgen"
        )
        first = next(
            item for item in boltz["stages"] if item["stage_id"] == "configure"
        )
        first["mounts"].remove(activation.BOLTZGEN_LEGACY_BROAD_MOUNT)
        activation._normalize_serialized_boltzgen_mount_cleanup(
            relative, partial_baseline, partial_current
        )
        self.assertNotEqual(partial_baseline, partial_current)

        tampered_baseline = clone(source)
        tampered_current = clone(accepted_current)
        boltz = next(
            item
            for item in tampered_current["models"]
            if item["model_id"] == "boltzgen"
        )
        boltz["stages"][0]["mounts"][1]["mount_path"] = "/unreviewed"
        activation._normalize_serialized_boltzgen_mount_cleanup(
            relative, tampered_baseline, tampered_current
        )
        self.assertNotEqual(tampered_baseline, tampered_current)

    def test_derived_identity_refresh_is_accepted_and_authorship_is_not(self) -> None:
        """The guard must separate a recomputed digest from authored content.

        Integrating a shared recipe input, such as a new localization transform,
        makes every pinned derived digest stale, and the repository's own
        refresher is what recomputes them. Forbidding that would make the guard
        unsatisfiable. Forbidding a lane from writing its own profile or
        execution-map entry is the whole point of the guard, so both halves are
        pinned here rather than left to the diff of the day.
        """
        relative = "catalog/runtime/contracts/scientific-workload-profiles.json"
        baseline = activation._aggregate_at_baseline(relative)
        real = activation.load_json(activation.repository_path(relative))
        path = activation.repository_path(relative)
        original = path.read_text(encoding="utf-8")
        try:
            for field in sorted(activation.DERIVED_IDENTITY_FIELDS):
                probe = json.loads(json.dumps(baseline))
                identity = probe["profiles"][0]["execution_identity"]
                qualification = probe["profiles"][0].get("qualification", {})
                target = identity if field in identity else qualification
                if field not in target:
                    continue
                target[field] = "0" * 64
                path.write_text(json.dumps(probe, indent=2) + "\n", encoding="utf-8")
                with self.subTest(derived=field):
                    self.assertEqual(
                        activation._derived_identity_refresh_only(relative), []
                    )

            # Authored content: a lane adding its own profile.
            probe = json.loads(json.dumps(baseline))
            probe["profiles"].append(json.loads(json.dumps(probe["profiles"][0])))
            path.write_text(json.dumps(probe, indent=2) + "\n", encoding="utf-8")
            errors = activation._derived_identity_refresh_only(relative)
            self.assertTrue(errors, "adding a profile must be rejected")
            self.assertTrue(
                any("adds authored content" in item for item in errors), errors
            )

            # Authored content: a lane opening its own route.
            probe = json.loads(json.dumps(baseline))
            probe["profiles"][0]["route_exposed"] = not probe["profiles"][0][
                "route_exposed"
            ]
            path.write_text(json.dumps(probe, indent=2) + "\n", encoding="utf-8")
            errors = activation._derived_identity_refresh_only(relative)
            self.assertTrue(errors, "flipping route exposure must be rejected")
            self.assertTrue(
                any("changes authored content" in item for item in errors), errors
            )

            # Authored content: a lane deleting a sibling's profile.
            probe = json.loads(json.dumps(baseline))
            probe["profiles"] = probe["profiles"][:1]
            path.write_text(json.dumps(probe, indent=2) + "\n", encoding="utf-8")
            errors = activation._derived_identity_refresh_only(relative)
            self.assertTrue(errors, "removing a profile must be rejected")
            self.assertTrue(any("removes content" in item for item in errors), errors)
        finally:
            path.write_text(original, encoding="utf-8")
        self.assertEqual(
            activation.load_json(activation.repository_path(relative)), real
        )


if __name__ == "__main__":
    unittest.main()
