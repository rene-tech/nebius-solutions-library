from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "catalog/runtime/schema"
CONTRACT_ROOT = ROOT / "catalog/runtime/contracts"
EXAMPLE_ROOT = CONTRACT_ROOT / "examples"


class ScientificWorkloadContractTests(unittest.TestCase):
    def load(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def validator(self, schema_name: str) -> Draft202012Validator:
        schema = self.load(SCHEMA_ROOT / schema_name)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())

    def assert_valid(self, schema_name: str, value: dict) -> None:
        errors = sorted(
            self.validator(schema_name).iter_errors(value),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        self.assertEqual([], errors, [error.message for error in errors])

    def test_all_scientific_schemas_are_valid_draft_2020_12(self) -> None:
        schemas = sorted(SCHEMA_ROOT.glob("scientific-*.schema.json"))
        self.assertEqual(8, len(schemas))
        for schema_path in schemas:
            with self.subTest(schema=schema_path.name):
                Draft202012Validator.check_schema(self.load(schema_path))

    def test_checked_in_request_result_and_artifact_examples_validate(self) -> None:
        cases = {
            "scientific-artifact-manifest.schema.json": (
                "scientific-artifact-manifest.example.json"
            ),
            "scientific-run-request.schema.json": "scientific-run-request.example.json",
            "scientific-run-result.schema.json": "scientific-run-result.example.json",
        }
        for schema_name, example_name in cases.items():
            with self.subTest(example=example_name):
                self.assert_valid(schema_name, self.load(EXAMPLE_ROOT / example_name))

    def test_request_cannot_select_operator_owned_execution_fields(self) -> None:
        request = self.load(EXAMPLE_ROOT / "scientific-run-request.example.json")
        forbidden = {
            "queue_name": "priority",
            "runtime_image": "registry.invalid/image@sha256:" + "a" * 64,
            "command": ["sh", "-c", "arbitrary"],
            "environment": {"TOKEN": "secret"},
            "gpu_class": "H100",
        }
        validator = self.validator("scientific-run-request.schema.json")
        for field, value in forbidden.items():
            with self.subTest(field=field):
                candidate = copy.deepcopy(request)
                candidate[field] = value
                self.assertTrue(list(validator.iter_errors(candidate)))

    def test_artifact_references_reject_locations_and_bearer_urls(self) -> None:
        artifact = {
            "artifact_id": "artifact.01",
            "sha256": "a" * 64,
            "size_bytes": 1,
            "media_type": "application/json",
        }
        validator = self.validator("scientific-artifact-pointer.schema.json")
        self.assertFalse(list(validator.iter_errors(artifact)))
        for field in ("uri", "local_path", "presigned_url"):
            with self.subTest(field=field):
                candidate = dict(artifact)
                candidate[field] = "https://storage.invalid/bearer"
                self.assertTrue(list(validator.iter_errors(candidate)))

    def test_embedded_artifact_reference_shapes_do_not_drift(self) -> None:
        canonical = self.load(
            SCHEMA_ROOT / "scientific-artifact-pointer.schema.json"
        )
        canonical_shape = {
            key: canonical[key]
            for key in ("type", "additionalProperties", "required", "properties")
        }
        for schema_name in (
            "scientific-artifact-manifest.schema.json",
            "scientific-run-request.schema.json",
            "scientific-run-result.schema.json",
        ):
            with self.subTest(schema=schema_name):
                embedded = self.load(SCHEMA_ROOT / schema_name)["$defs"][
                    "artifact_ref"
                ]
                self.assertEqual(canonical_shape, embedded)

    def test_terminal_success_requires_output_and_semantic_pass(self) -> None:
        result = self.load(EXAMPLE_ROOT / "scientific-run-result.example.json")
        validator = self.validator("scientific-run-result.schema.json")
        missing_output = copy.deepcopy(result)
        missing_output["output_manifest"] = None
        self.assertTrue(list(validator.iter_errors(missing_output)))
        failed_semantics = copy.deepcopy(result)
        failed_semantics["semantic_validation"]["status"] = "failed"
        self.assertTrue(list(validator.iter_errors(failed_semantics)))

    def test_queued_cancellation_can_have_no_attempt_but_academic_run_needs_receipt(
        self,
    ) -> None:
        result = self.load(EXAMPLE_ROOT / "scientific-run-result.example.json")
        validator = self.validator("scientific-run-result.schema.json")
        cancelled = copy.deepcopy(result)
        cancelled["terminal_status"] = "cancelled"
        cancelled["output_manifest"] = None
        cancelled["attempts"] = []
        cancelled["semantic_validation"] = {
            "validator_id": "example-protein-design-v1",
            "status": "not-run",
            "receipt_digest": None,
        }
        cancelled["error"] = None
        self.assertFalse(list(validator.iter_errors(cancelled)))

        academic = copy.deepcopy(result)
        academic["access_admission"] = {
            "profile": "academic",
            "state": "verified",
            "receipt_digest": None,
        }
        self.assertTrue(list(validator.iter_errors(academic)))

    def test_source_observations_are_exactly_candidate_and_unqualified(self) -> None:
        receipts = self.load(
            CONTRACT_ROOT / "scientific-source-candidate-receipts.json"
        )
        self.assert_valid(
            "scientific-source-candidate-receipts.schema.json", receipts
        )
        expected = {
            "alphafold3": "c0f97eda2f1f482fd94d3a38bece18c7069b4a5c",
            "bindcraft": "efb5bfeb8b4b1a5944256f979c34e0c8e6a82d9d",
            "boltzgen": "a3149cf18eeb58648d1abbb27539bd73f746cdda",
            "esmfold2": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
            "esmfold2-fast": "827ec128e4cdaf80f7d6f95fb367a08980b34918",
            "mosaic": "70fec525423f5f87156a1a957b4a4048f9f8e676",
            "proteina-complexa": "54058860d43444c7289873f77d3e50b5b02348cd",
            "protenix-v2": "4c355be4553512f72453ecbfb65e69f4c35d1413",
            "rfdiffusion-upstream": "86507b6538f51fce57b5a72477165f03999ed7ae",
        }
        actual = {
            item["model_id"]: item["source"]["revision"]
            for item in receipts["receipts"]
        }
        self.assertEqual(expected, actual)
        self.assertEqual(len(actual), len(receipts["receipts"]))
        self.assertTrue(
            all(item["status"] == "candidate" for item in receipts["receipts"])
        )
        self.assertTrue(
            all(
                item["qualification_state"] == "unqualified"
                for item in receipts["receipts"]
            )
        )
        academic = {
            item["model_id"]
            for item in receipts["receipts"]
            if item["access_profile"] == "academic"
        }
        self.assertEqual({"alphafold3", "bindcraft"}, academic)

    def test_empty_profile_set_is_honest_until_runtime_qualification(self) -> None:
        profiles = self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")
        self.assert_valid("scientific-workload-profiles.schema.json", profiles)
        self.assertEqual([], profiles["profiles"])


if __name__ == "__main__":
    unittest.main()
