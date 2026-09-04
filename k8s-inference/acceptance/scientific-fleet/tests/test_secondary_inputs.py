from __future__ import annotations

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "validate_secondary_inputs.py"
SPEC = importlib.util.spec_from_file_location(
    "fs2_scientific_secondary_acceptance_inputs", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SecondaryAcceptanceInputTest(unittest.TestCase):
    def test_all_five_inputs_are_runner_consumable_and_deterministic(self) -> None:
        first = MODULE.validate_all()
        second = MODULE.validate_all()

        self.assertEqual(first, second)
        self.assertEqual(set(first["models"]), MODULE.EXPECTED_MODELS)
        self.assertEqual(
            first["models"]["esmfold2"]["payload_sha256"],
            first["models"]["esmfold2-fast"]["payload_sha256"],
        )
        for model_id, record in first["models"].items():
            with self.subTest(model_id=model_id):
                self.assertEqual(len(record["payload_sha256"]), 64)
                self.assertGreater(record["payload_size_bytes"], 0)
                self.assertEqual(len(record["stages"]), 2)

    def test_schema_keeps_activation_closed(self) -> None:
        path = MODULE.ADAPTER_ROOT / "openfold3/activation/public-acceptance.json"
        fragment = json.loads(path.read_bytes())
        unsafe = copy.deepcopy(fragment)
        unsafe["activation_gate"]["route_exposed"] = True

        errors = MODULE._schema_errors(unsafe, MODULE.INPUT_SCHEMA)

        self.assertTrue(
            any(error.startswith("activation_gate.route_exposed:") for error in errors)
        )

    def test_alphafold3_public_input_never_declares_private_parameter_bytes(
        self,
    ) -> None:
        fragment = json.loads(
            (
                MODULE.ADAPTER_ROOT / "alphafold3/activation/public-acceptance.json"
            ).read_bytes()
        )
        paths = {
            item["path"] for item in fragment["public_fixtures"]["supporting_inputs"]
        }

        self.assertEqual(
            paths,
            {
                "models/structure/batch-adapters/alphafold3/activation/public-input-manifest.json",
                "models/cancer-immunotherapy/fast-start-campaign/af3-fold-input.json",
            },
        )
        self.assertFalse(any("parameter" in value.lower() for value in paths))


if __name__ == "__main__":
    unittest.main()
