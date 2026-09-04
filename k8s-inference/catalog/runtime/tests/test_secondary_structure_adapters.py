from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters"
HANDOFF_PATH = ADAPTER_ROOT / "secondary-r4-image-handoff.json"
MODEL_IDS = ("esmfold2", "esmfold2-fast", "protenix-v2", "openfold3-openbind")
ADAPTER_DIRECTORIES = {
    model_id: "openfold3" if model_id == "openfold3-openbind" else model_id
    for model_id in MODEL_IDS
}
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SEMANTICALLY_H100_QUALIFIED: set[str] = set()
IMAGE_GENERATIONS = {
    "esmfold2": "h100-r6",
    "esmfold2-fast": "h100-r6",
    "protenix-v2": "h100-r6",
    "openfold3-openbind": "h100-r7",
}
PREDECESSOR_ACTIVATION_DIGESTS = {
    "esmfold2": "sha256:870b9f647f41bb02cfcbf08d5eec6cdf6b5171e8771c776248c5865c2f762a4a",
    "esmfold2-fast": "sha256:fc7b8687849511a04b04afd9c477bcc0fb85a2837eac6ac658609e8b7e2702e0",
    "protenix-v2": "sha256:b90a02bdffe3eefa8a251eb1e3666f3748a72e68fdec0b3cd867c2f08b426af8",
    "openfold3-openbind": "sha256:f44860c3216a9f526d055be61aecc2a2041594d3dd091ba8059ad825be1952d5",
}


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
            model_id: load_json(
                ADAPTER_ROOT / ADAPTER_DIRECTORIES[model_id] / "contract.json"
            )
            for model_id in MODEL_IDS
        }
        self.profiles = {
            model_id: load_json(
                ADAPTER_ROOT
                / ADAPTER_DIRECTORIES[model_id]
                / "activation/workload-profile.json"
            )["profile"]
            for model_id in MODEL_IDS
        }

    def test_handoff_is_exactly_four_non_af3_published_repaired_images(self) -> None:
        self.assertEqual(set(self.images), set(MODEL_IDS))
        self.assertNotIn("alphafold3", self.images)
        self.assertEqual(self.handoff["state"], "published-build-only-not-activated")
        self.assertIs(self.handoff["semantic_h100_qualification"], False)
        self.assertIs(self.handoff["route_activation_allowed"], False)
        self.assertIs(self.handoff["production_protocol_compatible"], True)
        self.assertIn("qualification", str(self.handoff["successor_requirement"]))
        self.assertRegex(str(self.handoff["image_source_commit"]), r"^[0-9a-f]{40}$")
        for model_id, image in self.images.items():
            self.assertTrue(
                str(image["tag"]).endswith(f"-{IMAGE_GENERATIONS[model_id]}")
            )
            self.assertRegex(str(image["digest"]), SHA256_PATTERN)

    def test_contracts_bind_the_exact_handoff_and_fail_closed(self) -> None:
        for model_id, contract in self.contracts.items():
            with self.subTest(model_id=model_id):
                image = self.images[model_id]
                semantic_qualified = model_id in SEMANTICALLY_H100_QUALIFIED
                self.assertEqual(contract["model_id"], model_id)
                self.assertEqual(
                    contract["runtime_image"],  # type: ignore[index]
                    {
                        "repository": image["repository"],
                        "tag": image["tag"],
                        "digest": image["digest"],
                        "workspace_uid": 10001,
                        "workspace_gid": 10001,
                        "state": (
                            "semantic-h100-qualified"
                            if semantic_qualified
                            else "build-only-not-semantic-qualified"
                        ),
                    },
                )
                self.assertEqual(
                    contract["activation"],
                    {
                        "profile_state": "candidate-unqualified",
                        "route_exposed": False,
                        "semantic_h100_qualified": semantic_qualified,
                    },
                )
                profile = self.profiles[model_id]
                self.assertEqual(profile["model_id"], model_id)
                self.assertEqual(
                    profile["execution_identity"]["runtime_image_digest"],
                    PREDECESSOR_ACTIVATION_DIGESTS[model_id],
                )
                self.assertNotEqual(
                    profile["execution_identity"]["runtime_image_digest"],
                    image["digest"],
                    "a predecessor H100 receipt must not qualify a successor image",
                )
                seam = str(contract["seam"])
                self.assertIn("scheduler", seam)
                self.assertIn("Terraform", seam)
                self.assertIn("activation", seam)

    def test_cpu_preprocessing_precedes_gpu_inference_and_closure_is_explicit(
        self,
    ) -> None:
        for model_id, contract in self.contracts.items():
            with self.subTest(model_id=model_id):
                stages = contract["stages"]
                self.assertEqual(
                    [stage["resource_class"] for stage in stages], ["cpu", "gpu"]
                )
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
                paths = sorted(
                    (ADAPTER_ROOT / ADAPTER_DIRECTORIES[model_id] / "fixtures").glob(
                        "*.json"
                    )
                )
                self.assertEqual(len(paths), 3)
                self.assertEqual(
                    sum(path.name.startswith("positive-") for path in paths), 2
                )
                self.assertEqual(
                    sum(path.name.startswith("negative-") for path in paths), 1
                )
                fixtures = [load_json(path) for path in paths]
                self.assertTrue(
                    all(
                        fixture["schema"]
                        == "fs2-serve.nebius.ai/scientific-run-request/v1"
                        for fixture in fixtures
                    )
                )

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
            self.contracts["openfold3-openbind"]["relationship"],
            "independent-non-equivalent-alternative-to-alphafold3",
        )

    def test_model_owned_profiles_are_active_and_prefer_reserved_capacity(self) -> None:
        for model_id, profile in self.profiles.items():
            with self.subTest(model_id=model_id):
                self.assertEqual(profile["state"], "active")
                self.assertIs(profile["route_exposed"], True)
                self.assertEqual(profile["source"]["classification"], "qualified-input")
                self.assertIs(profile["interface"]["mcp"]["invocable"], True)
                self.assertEqual(profile["semantic_validation"]["state"], "active")
                self.assertRegex(
                    profile["qualification"]["h100_semantic_receipt_sha256"],
                    r"^[0-9a-f]{64}$",
                )
                self.assertIsNone(
                    profile["qualification"]["public_completion_receipt_sha256"]
                )
                self.assertIsNone(
                    profile["qualification"]["scheduler_eligibility_receipt_sha256"]
                )
                self.assertEqual(
                    profile["resources"]["compatible_pool_ids"],
                    ["h100-reserved-8x", "h100-1x"],
                )
                self.assertEqual(
                    profile["resources"]["required_node_labels"],
                    {"accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"},
                )


if __name__ == "__main__":
    unittest.main()
