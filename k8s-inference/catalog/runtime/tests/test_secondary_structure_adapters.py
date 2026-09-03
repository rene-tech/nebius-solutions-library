from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters"
HANDOFF_PATH = ADAPTER_ROOT / "secondary-r4-image-handoff.json"
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
MODEL_IDS = ("esmfold2", "esmfold2-fast", "protenix-v2", "openfold3")
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


class SecondaryStructureAdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handoff = load_json(HANDOFF_PATH)
        self.images = {
            image["model_id"]: image
            for image in self.handoff["images"]  # type: ignore[index]
        }
        self.contracts = {
            model_id: load_json(ADAPTER_ROOT / model_id / "contract.json")
            for model_id in MODEL_IDS
        }

    def test_handoff_is_exactly_four_non_af3_build_only_r4_images(self) -> None:
        self.assertEqual(set(self.images), set(MODEL_IDS))
        self.assertNotIn("alphafold3", self.images)
        self.assertEqual(self.handoff["state"], "build-only-not-activated")
        self.assertIs(self.handoff["semantic_h100_qualification"], False)
        self.assertIs(self.handoff["route_activation_allowed"], False)
        self.assertRegex(str(self.handoff["image_source_commit"]), r"^[0-9a-f]{40}$")
        for image in self.images.values():
            self.assertTrue(str(image["tag"]).endswith("-h100-r4"))
            self.assertRegex(str(image["digest"]), SHA256_PATTERN)

    def test_contracts_bind_the_exact_handoff_and_fail_closed(self) -> None:
        for model_id, contract in self.contracts.items():
            with self.subTest(model_id=model_id):
                image = self.images[model_id]
                self.assertEqual(contract["model_id"], model_id)
                self.assertEqual(
                    contract["runtime_image"],  # type: ignore[index]
                    {
                        "repository": image["repository"],
                        "tag": image["tag"],
                        "digest": image["digest"],
                        "state": "build-only-not-semantic-qualified",
                    },
                )
                self.assertEqual(
                    contract["activation"],
                    {
                        "profile_state": "candidate-unqualified",
                        "route_exposed": False,
                        "semantic_h100_qualified": False,
                    },
                )
                seam = str(contract["seam"])
                self.assertIn("scheduler", seam)
                self.assertIn("Terraform", seam)
                self.assertIn("activation", seam)

    def test_cpu_preprocessing_precedes_gpu_inference_and_closure_is_explicit(self) -> None:
        for model_id, contract in self.contracts.items():
            with self.subTest(model_id=model_id):
                stages = contract["stages"]
                self.assertEqual([stage["resource_class"] for stage in stages], ["cpu", "gpu"])
                declared = {
                    artifact["artifact_id"]
                    for artifact in contract["runtime_artifacts"]
                }
                bound = {
                    artifact_id
                    for stage in stages
                    for artifact_id in stage["runtime_artifacts"]
                }
                self.assertEqual(bound, declared)
                self.assertEqual(len({stage["collector_id"] for stage in stages}), 2)
                self.assertEqual(len({stage["validator_id"] for stage in stages}), 2)

    def test_each_adapter_has_two_positive_and_one_negative_fixture(self) -> None:
        for model_id in MODEL_IDS:
            with self.subTest(model_id=model_id):
                paths = sorted((ADAPTER_ROOT / model_id / "fixtures").glob("*.json"))
                self.assertEqual(len(paths), 3)
                self.assertEqual(sum(path.name.startswith("positive-") for path in paths), 2)
                self.assertEqual(sum(path.name.startswith("negative-") for path in paths), 1)
                fixtures = [load_json(path) for path in paths]
                self.assertTrue(all(fixture["schema"] == "fs2-serve.nebius.ai/scientific-run-request/v1" for fixture in fixtures))

    def test_capability_and_backend_identity_boundaries_are_explicit(self) -> None:
        self.assertEqual(
            self.contracts["esmfold2"]["capabilities"],
            {"single_sequence": True, "precomputed_msa": True},
        )
        fast_capabilities = self.contracts["esmfold2-fast"]["capabilities"]
        self.assertIs(fast_capabilities["single_sequence"], True)
        self.assertIs(fast_capabilities["precomputed_msa"], False)
        self.assertIn("before GPU allocation", fast_capabilities["rejection"])
        self.assertEqual(
            self.contracts["openfold3"]["relationship"],
            "independent-non-equivalent-alternative-to-alphafold3",
        )

    def test_no_public_workload_profile_is_accidentally_activated(self) -> None:
        profiles = load_json(PROFILE_PATH)["profiles"]
        for profile in profiles:
            if profile["model_id"] not in MODEL_IDS:
                continue
            with self.subTest(model_id=profile["model_id"]):
                self.assertEqual(profile["state"], "candidate-unqualified")
                self.assertIs(profile["route_exposed"], False)
                self.assertIsNone(profile["runtime_image_digest"])


if __name__ == "__main__":
    unittest.main()
