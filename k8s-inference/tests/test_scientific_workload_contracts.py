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
        self.assertEqual(9, len(schemas))
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
        missing_admission = copy.deepcopy(result)
        missing_admission["attempts"][0]["scheduling_admission"] = None
        self.assertTrue(list(validator.iter_errors(missing_admission)))
        preempted_without_admission = copy.deepcopy(result)
        preempted_without_admission["terminal_status"] = "failed"
        preempted_without_admission["output_manifest"] = None
        preempted_without_admission["semantic_validation"] = {
            "validator_id": "example-protein-design-v1",
            "status": "not-run",
            "receipt_digest": None,
        }
        preempted_without_admission["error"] = {
            "code": "PREEMPTED",
            "message": "Capacity was reclaimed before the retry completed.",
            "retryable": True,
        }
        preempted_without_admission["attempts"][0]["status"] = "preempted"
        preempted_without_admission["attempts"][0]["scheduling_admission"] = None
        self.assertTrue(list(validator.iter_errors(preempted_without_admission)))

    def test_scheduling_is_exact_for_each_stage_and_attempt(self) -> None:
        result = self.load(EXAMPLE_ROOT / "scientific-run-result.example.json")
        validator = self.validator("scientific-run-result.schema.json")

        cpu_stage = copy.deepcopy(result["scheduling_snapshot"]["stages"][0])
        cpu_stage.update(
            {
                "stage_id": "prepare",
                "resource_class": "cpu",
                "resolved_pool_preference": [],
                "accelerator_resource_name": None,
                "accelerator_count": 0,
            }
        )
        result["scheduling_snapshot"]["stages"].insert(0, cpu_stage)
        self.assertFalse(list(validator.iter_errors(result)))

        unknown_stage_field = copy.deepcopy(result)
        unknown_stage_field["scheduling_snapshot"]["stages"][0][
            "provider_instance_type"
        ] = "gpu-vendor-specific"
        self.assertTrue(list(validator.iter_errors(unknown_stage_field)))

        unknown_admission_field = copy.deepcopy(result)
        unknown_admission_field["attempts"][0]["scheduling_admission"][
            "provider_instance_id"
        ] = "instance-secret"
        self.assertTrue(list(validator.iter_errors(unknown_admission_field)))

        invalid_gpu_stage = copy.deepcopy(result)
        invalid_gpu_stage["scheduling_snapshot"]["stages"][1][
            "accelerator_resource_name"
        ] = None
        invalid_gpu_stage["scheduling_snapshot"]["stages"][1]["accelerator_count"] = 0
        self.assertTrue(list(validator.iter_errors(invalid_gpu_stage)))

    def test_attempt_bound_covers_every_stage_shard_and_retry(self) -> None:
        schema = self.load(SCHEMA_ROOT / "scientific-run-result.schema.json")
        self.assertEqual(64 * 1024 * 10, schema["properties"]["attempts"]["maxItems"])

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

    def test_source_receipts_match_qualified_exact_pins(self) -> None:
        receipts = self.load(
            CONTRACT_ROOT / "scientific-source-candidate-receipts.json"
        )
        self.assert_valid(
            "scientific-source-candidate-receipts.schema.json", receipts
        )
        expected = {
            ("alphafold3", "upstream-v3-0-4"): "85c4d20505fd5cef05eac22b534d4e793971ae69",
            ("bindcraft", "upstream-pyrosetta"): "7cd4ace1b7407adf66a50dfefa47de2270f5e4a9",
            ("boltzgen", "upstream-v0-3-2"): "31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0",
            ("esmfold2", "biohub-v3-4-0"): "827ec128e4cdaf80f7d6f95fb367a08980b34918",
            ("esmfold2-fast", "biohub-v3-4-0"): "827ec128e4cdaf80f7d6f95fb367a08980b34918",
            ("freebindcraft", "upstream-v1-0-5"): "28c43fc48942eebd7918f504e9812c5c17bb3411",
            ("mosaic", "escalante-20260801"): "70fec525423f5f87156a1a957b4a4048f9f8e676",
            ("openfold3", "upstream-openbind-v0-5-0"): "c4771653c5d0a3ebb0b3af71b05efd64bc44ee86",
            ("proteina-complexa", "upstream-dev-20260827"): "54058860d43444c7289873f77d3e50b5b02348cd",
            ("proteinmpnn", "upstream-8907e667"): "8907e6671bfbfc92303b5f79c4b5e6ce47cdef57",
            ("protenix-v2", "upstream-v2-0-0"): "2475421477ab414b571149ad4a875c390ff8a35d",
            ("rfdiffusion", "upstream-v1-1-0"): "9273ef67335acaf91df0150473a274759229cdf6",
        }
        actual = {
            (item["model_id"], item["variant_id"]): item["source"]["revision"]
            for item in receipts["receipts"]
        }
        self.assertEqual(expected, actual)
        self.assertEqual(len(actual), len(receipts["receipts"]))
        self.assertTrue(
            all(item["status"] == "candidate" for item in receipts["receipts"])
        )
        self.assertTrue(
            all(
                item["qualification_state"] == "source-qualified"
                for item in receipts["receipts"]
            )
        )
        academic = {
            item["model_id"]
            for item in receipts["receipts"]
            if item["access_profile"] == "academic"
        }
        self.assertEqual({"alphafold3", "bindcraft"}, academic)

    def test_compiled_candidate_profiles_are_canonical_and_non_invocable(self) -> None:
        profiles = self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")
        self.assert_valid("scientific-workload-profiles.schema.json", profiles)
        validator = self.validator("scientific-workload-profile.schema.json")
        expected = {
            ("alphafold3", "upstream-v3-0-4"),
            ("bindcraft", "upstream-pyrosetta"),
            ("boltzgen", "upstream-v0-3-2"),
            ("esmfold2", "biohub-v3-4-0"),
            ("esmfold2-fast", "biohub-v3-4-0"),
            ("freebindcraft", "upstream-v1-0-5"),
            ("mosaic", "escalante-20260801"),
            ("openfold3", "upstream-openbind-v0-5-0"),
            ("proteina-complexa", "upstream-dev-20260827"),
            ("proteinmpnn", "upstream-8907e667"),
            ("protenix-v2", "upstream-v2-0-0"),
            ("rfdiffusion", "upstream-v1-1-0"),
        }
        actual = set()
        for profile in profiles["profiles"]:
            with self.subTest(profile=(profile["model_id"], profile["variant_id"])):
                self.assertFalse(list(validator.iter_errors(profile)))
                self.assertEqual("candidate-unqualified", profile["state"])
                self.assertFalse(profile["route_exposed"])
                self.assertFalse(profile["interface"]["mcp"]["invocable"])
                self.assertEqual(
                    "build-required",
                    profile["execution_identity"]["runtime_image_state"],
                )
                self.assertIsNone(
                    profile["execution_identity"]["runtime_image_digest"]
                )
                self.assertNotIn(
                    ".",
                    profile["execution_identity"]["runtime_image_repository"].split("/", 1)[0],
                )
                self.assertFalse(
                    profile["interface"]["parameter_schema_definition"]["additionalProperties"]
                )
                actual.add((profile["model_id"], profile["variant_id"]))
        self.assertEqual(expected, actual)

    def test_access_storage_and_offline_artifacts_remain_fail_closed(self) -> None:
        profiles = {
            item["model_id"]: item
            for item in self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")["profiles"]
        }
        for model_id in ("alphafold3", "bindcraft"):
            self.assertEqual("license-acceptance-pending", profiles[model_id]["access"]["state"])
            self.assertEqual("LicenseAcceptancePending", profiles[model_id]["access"]["condition"])
            self.assertEqual(
                "restricted-quarantine-poc-authorized",
                profiles[model_id]["access"]["materialization"],
            )
            self.assertEqual(
                "user-authorized-academic-poc",
                profiles[model_id]["access"]["operational_activation"],
            )
            self.assertEqual(
                "production-promotion-only",
                profiles[model_id]["access"]["license_gate_scope"],
            )

        for model_id in ("alphafold3", "openfold3"):
            profile = profiles[model_id]
            data, inference = profile["workload"]["stages"][:2]
            self.assertEqual(("cpu", 0), (data["resource_class"], data["resources"]["gpu_count"]))
            self.assertEqual(("gpu", 1), (inference["resource_class"], inference["resources"]["gpu_count"]))
            reference = next(
                item for item in profile["workload"]["storage"] if item["purpose"] == "reference-data"
            )
            self.assertGreaterEqual(reference["minimum_bytes"], 1024**4)
            self.assertEqual("ReadWriteMany", reference["access_mode"])

        expected_fast = {
            "biohub/ESMFold2-Fast": (
                "c6c7958d63f5f2f1f0fed0bb9462316f8ccceea6",
                {"model.safetensors", "config.json"},
            ),
            "biohub/ESMC-6B": (
                "45b0fa5d7fb06faefbd5e3b89bdcef35d564e79a",
                {
                    "config.json",
                    "model-00001-of-00006.safetensors",
                    "model-00002-of-00006.safetensors",
                    "model-00003-of-00006.safetensors",
                    "model-00004-of-00006.safetensors",
                    "model-00005-of-00006.safetensors",
                    "model-00006-of-00006.safetensors",
                    "model.safetensors.index.json",
                    "special_tokens_map.json",
                    "tokenizer.json",
                    "tokenizer_config.json",
                },
            ),
            "biohub/ESMFold2": (
                "8fc3ff471022fdce52c77030685eb775de0c00a3",
                {"ccd.pkl"},
            ),
        }
        fast_artifacts = {
            item["source"]["repository"]: (
                item["source"]["revision"],
                set(item["required_files"]),
            )
            for item in profiles["esmfold2-fast"]["artifact_requirements"]
            if item["source"]["kind"] == "huggingface"
        }
        self.assertEqual(expected_fast, fast_artifacts)

        self.assertEqual(
            {
                "biohub/ESMFold2": (
                    "8fc3ff471022fdce52c77030685eb775de0c00a3",
                    {"model.safetensors", "config.json", "ccd.pkl"},
                ),
                "biohub/ESMC-6B": (
                    "45b0fa5d7fb06faefbd5e3b89bdcef35d564e79a",
                    {
                        "config.json",
                        "model-00001-of-00006.safetensors",
                        "model-00002-of-00006.safetensors",
                        "model-00003-of-00006.safetensors",
                        "model-00004-of-00006.safetensors",
                        "model-00005-of-00006.safetensors",
                        "model-00006-of-00006.safetensors",
                        "model.safetensors.index.json",
                        "special_tokens_map.json",
                        "tokenizer.json",
                        "tokenizer_config.json",
                    },
                ),
            },
            {
                item["source"]["repository"]: (
                    item["source"]["revision"],
                    set(item["required_files"]),
                )
                for item in profiles["esmfold2"]["artifact_requirements"]
            },
        )
        boltz = profiles["boltzgen"]["artifact_requirements"][0]
        self.assertEqual("boltzgen/boltzgen-1", boltz["source"]["repository"])
        self.assertEqual("c1be29e1f82ffcc72264f64b993c43fb4e0d17f0", boltz["source"]["revision"])
        self.assertEqual(6, len(boltz["required_files"]))
        molecules = profiles["boltzgen"]["artifact_requirements"][1]
        self.assertEqual(["mols.zip"], molecules["required_files"])
        self.assertEqual(
            "3d4f56ac4262e745bb3d09cfaa19099b1d01be208122d501667b952e45521e53",
            molecules["content_digest_sha256"],
        )

        proteina = {
            item["source"]["repository"]: item["source"]["revision"]
            for item in profiles["proteina-complexa"]["artifact_requirements"]
            if item["source"]["kind"] == "huggingface"
        }
        self.assertEqual(
            {
                "nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1": "ffed199e32612b98ffa04f4640d34d37b137fca5",
                "nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1": "bc90c8b2c701ceb52d5faef72600b6b5be880244",
                "nvidia/NV-Proteina-Complexa-AME-160M-v1": "9743d749a8754080a32fda857d95579dfa4dabae",
                "facebook/esm2_t33_650M_UR50D": "08e4846e537177426273712802403f7ba8261b6c",
            },
            proteina,
        )

    def test_public_profiles_do_not_publish_runtime_locations(self) -> None:
        profiles = self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")
        forbidden_keys = {"uri", "local_path", "filesystem_path", "object_path", "presigned_url"}

        def walk(value: object) -> None:
            if isinstance(value, dict):
                self.assertFalse(forbidden_keys.intersection(value))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(profiles)
        self.assertNotIn("eu-north1", json.dumps(profiles))

    def test_profile_collection_rejects_unknown_nested_fields(self) -> None:
        profiles = self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")
        validator = self.validator("scientific-workload-profiles.schema.json")
        attacks = [
            ("source", "object_uri"),
            ("execution_identity", "registry_override"),
            ("interface", "unreviewed_route"),
            ("resources", "node_selector_override"),
            ("policy", "production_ready"),
        ]
        for parent, field in attacks:
            with self.subTest(parent=parent, field=field):
                candidate = copy.deepcopy(profiles)
                candidate["profiles"][0][parent][field] = "attacker-controlled"
                self.assertTrue(list(validator.iter_errors(candidate)))

    def test_primary_parameter_schemas_accept_only_canonical_parameters(self) -> None:
        cases = (
            (
                "proteina-complexa-parameters.schema.json",
                ROOT
                / "models/structure/batch-adapters/proteina-complexa/fixtures/positive-protein.json",
            ),
            (
                "boltzgen-parameters.schema.json",
                ROOT
                / "models/structure/batch-adapters/boltzgen/fixtures/positive-design.json",
            ),
        )
        request_validator = self.validator("scientific-run-request.schema.json")
        for schema_name, fixture_path in cases:
            with self.subTest(schema=schema_name):
                request = self.load(fixture_path)
                self.assertEqual([], list(request_validator.iter_errors(request)))
                self.assertEqual(
                    [],
                    list(self.validator(schema_name).iter_errors(request["parameters"])),
                )


if __name__ == "__main__":
    unittest.main()
