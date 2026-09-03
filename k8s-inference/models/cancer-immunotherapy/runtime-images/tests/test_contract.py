from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeImageContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))

    def test_exact_sources_targets_and_base_digests(self) -> None:
        self.assertEqual("fs2.nebius.ai/scientific-runtime-image-catalog/v1", self.catalog["schema"])
        self.assertEqual(3, len(self.catalog["images"]))
        for image in self.catalog["images"]:
            revision = image["source"]["revision"]
            self.assertRegex(revision, r"^[0-9a-f]{40}$")
            suffix = "-" + image["tag_suffix"] if image.get("tag_suffix") else ""
            self.assertTrue(image["target_tag"].endswith(":" + revision + suffix))
            self.assertTrue(image["base"]["builder"].startswith("nvidia/cuda:"))
            self.assertTrue(image["base"]["runtime"].startswith("nvidia/cuda:"))
            self.assertRegex(image["base"]["builder"], r"@sha256:[0-9a-f]{64}$")
            self.assertRegex(image["base"]["runtime"], r"@sha256:[0-9a-f]{64}$")

    def test_dependency_locks_are_bound_by_digest(self) -> None:
        for image in self.catalog["images"]:
            path = ROOT / image["dependency_lock"]["path"]
            self.assertTrue(path.is_file())
            self.assertEqual(
                image["dependency_lock"]["sha256"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )

    def test_default_images_are_weight_free_and_non_root(self) -> None:
        forbidden = ("DOWNLOAD_WEIGHTS=true", "complexa download", "huggingface_hub.snapshot_download")
        for image in self.catalog["images"]:
            self.assertIs(image["weight_policy"]["embedded"], False)
            dockerfile = (ROOT / image["dockerfile"]).read_text(encoding="utf-8")
            for text in forbidden:
                self.assertNotIn(text, dockerfile)
            self.assertIn("USER 10001:10001", dockerfile)
            self.assertIn("FS2_ARTIFACT_ROOT=/opt/fs2/artifacts", dockerfile)
            self.assertIn("ENTRYPOINT []", dockerfile)
        mosaic = (ROOT / "mosaic" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("src/mosaic/proteinmpnn/weights", mosaic)
        self.assertIn("-delete", mosaic)

    def test_mosaic_image_consumes_exact_adapter_interface(self) -> None:
        image = next(item for item in self.catalog["images"] if item["id"] == "mosaic")
        self.assertEqual("adapter-v2", image["tag_suffix"])
        adapter = image["adapter"]
        adapter_dir = ROOT / image["adapter_context"]
        self.assertEqual("6551d8708c829fe99d229d1f547b8bd8cab0231e", adapter["commit"])
        self.assertEqual(
            adapter["entrypoint_sha256"],
            hashlib.sha256((adapter_dir / "bin/mosaic-batch").read_bytes()).hexdigest(),
        )
        self.assertEqual(
            adapter["recipe_sha256"],
            hashlib.sha256((adapter_dir / "recipe.json").read_bytes()).hexdigest(),
        )
        entrypoint = (ROOT / "mosaic" / "runtime_entrypoint.py").read_text(encoding="utf-8")
        self.assertNotIn('boltz_cache / "ccd.pkl"', entrypoint)

    def test_boltzgen_pins_source_compatible_cuequivariance(self) -> None:
        image = next(item for item in self.catalog["images"] if item["id"] == "boltzgen")
        self.assertEqual("cueq-v3", image["tag_suffix"])
        self.assertEqual("0.10.0", image["compatibility_overrides"][0]["version"])
        dockerfile = (ROOT / image["dockerfile"]).read_text(encoding="utf-8")
        self.assertGreaterEqual(dockerfile.count("cuequivariance"), 4)
        self.assertIn("gcc libboost-filesystem", dockerfile)
        self.assertIn("python3 python3-dev", dockerfile)
        for digest in (
            "340f5160b99efe57f8a220db75747e59b8a0f9f3bbced7ec527c46f8cc615e87",
            "3a13afba71c5e2c2dc154032879c640e9d8653a177efeca0bc9fb99e607cf540",
            "ca09836949cd86dd64fe7ef224b6212531049b94b9b5b4c1d928985eff0cf0b3",
            "36c24d187f456f36e25cdbba475fb08297652f43415e6c183fea1af4074de652",
        ):
            self.assertIn(digest, dockerfile)

    def test_smoke_commands_are_shell_free_argument_vectors(self) -> None:
        for image in self.catalog["images"]:
            for command in [*image["smoke"], image["gpu_smoke"]]:
                self.assertIsInstance(command, list)
                self.assertGreaterEqual(len(command), 2)
                self.assertNotIn("sh", command[0])
                self.assertNotIn("bash", command[0])
                self.assertFalse(any("&&" in argument or ";" in argument for argument in command))

    def test_no_deployment_manifests_in_image_slice(self) -> None:
        self.assertFalse(list(ROOT.rglob("*.yaml")))
        self.assertFalse(list(ROOT.rglob("*.yml")))

    def test_semantic_validator_excludes_boltzgen_input_visualization(self) -> None:
        validator = (ROOT / "qualification" / "validate_outputs.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('output / "intermediate_designs"', validator)
        self.assertIn('chain_extent <= 1_000.0', validator)


if __name__ == "__main__":
    unittest.main()
