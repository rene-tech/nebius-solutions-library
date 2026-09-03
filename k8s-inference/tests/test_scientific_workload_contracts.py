from __future__ import annotations

import copy
import json
import re
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
        self.assertEqual(10, len(schemas))
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

    def test_primary_profiles_are_schema_valid_candidate_only_and_unroutable(self) -> None:
        profiles = self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")
        self.assert_valid("scientific-workload-profiles.schema.json", profiles)
        full_validator = self.validator("scientific-workload-profile.schema.json")
        self.assertEqual(
            ["boltzgen", "proteina-complexa"],
            [profile["model_id"] for profile in profiles["profiles"]],
        )
        for profile in profiles["profiles"]:
            with self.subTest(model_id=profile["model_id"]):
                self.assertEqual([], list(full_validator.iter_errors(profile)))
                self.assertEqual("candidate-unqualified", profile["state"])
                self.assertFalse(profile["route_exposed"])
                self.assertEqual(
                    {
                        "boltzgen": "sha256:1cdc8e5f71d8e2d887c593cab858bc22ea7550cdadb5484eab25f35be5ba5544",
                        "proteina-complexa": "sha256:d3f3c9bc5a2285b09932eb05a57ef73da3201bc69b77462420c0d42a0aaa91d8",
                    }[profile["model_id"]],
                    profile["execution_identity"]["runtime_image_digest"],
                )
                self.assertIsNone(
                    profile["execution_identity"]["artifact_manifest_digest"]
                )
                self.assertIsNone(
                    profile["execution_identity"]["execution_identity_sha256"]
                )

    def test_primary_parameter_schemas_validate_only_canonical_request_parameters(
        self,
    ) -> None:
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
                    [], list(self.validator(schema_name).iter_errors(request["parameters"]))
                )

    def test_artifact_localization_contract_matches_its_schema(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        self.assert_valid("scientific-artifact-localization.schema.json", contract)
        artifacts = {item["artifact_id"]: item for item in contract["artifacts"]}
        self.assertEqual(
            {
                "boltzgen-inference-molecules",
                "alphafold2-params",
                "alphafold2-params-bindcraft",
                "colabdesign-mpnn-weights-vanilla",
                "colabdesign-mpnn-weights-soluble",
            },
            set(artifacts),
        )

    def test_archive_provenance_never_doubles_as_extracted_tree_identity(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        for artifact in contract["artifacts"]:
            with self.subTest(artifact=artifact["artifact_id"]):
                archive = artifact["archive"]
                tree = artifact["tree"]
                self.assertNotEqual(archive["sha256"], tree["inventory_sha256"])
                self.assertNotEqual(archive["bytes"], tree["total_bytes"])
                for mount in tree["mount_paths"]:
                    # A mount under the runtime artifact root must be this
                    # artifact's own directory; an installed-package tree lives
                    # where the package is installed instead.
                    if mount.startswith("/opt/fs2/artifacts/"):
                        self.assertEqual(
                            "/opt/fs2/artifacts/" + artifact["artifact_id"], mount
                        )
                # A runtime mount is a directory of content, so the archive name
                # must never be something the tree could legitimately contain.
                self.assertIsNone(
                    re.compile(tree["entry_path_pattern"]).fullmatch(archive["filename"])
                )

    def test_localization_contract_rejects_a_tree_holding_its_own_archive(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        validator = self.validator("scientific-artifact-localization.schema.json")
        for index, artifact in enumerate(contract["artifacts"]):
            with self.subTest(artifact=artifact["artifact_id"]):
                candidate = copy.deepcopy(contract)
                candidate["artifacts"][index]["tree"]["mount_paths"] = ["relative/path"]
                self.assertTrue(list(validator.iter_errors(candidate)))

    def test_localized_runtime_bindings_name_the_expected_model_surface(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        observed = {
            (artifact["artifact_id"], consumer["model_id"], consumer["binding_name"])
            for artifact in contract["artifacts"]
            for consumer in artifact["consumers"]
        }
        for artifact in contract["artifacts"]:
            declared = set(artifact["tree"]["mount_paths"])
            read = {consumer["mount_path"] for consumer in artifact["consumers"]}
            # One verified identity may serve several consumer paths, but every
            # declared path must have a reader and every reader a declared path.
            self.assertEqual(declared, read, artifact["artifact_id"])
        self.assertEqual(
            {
                ("boltzgen-inference-molecules", "boltzgen", "--moldir"),
                ("alphafold2-params", "proteina-complexa", "AF2_DIR"),
                ("alphafold2-params-bindcraft", "bindcraft", "FS2_ARTIFACT_ROOT"),
                (
                    "colabdesign-mpnn-weights-vanilla",
                    "bindcraft",
                    "colabdesign.mpnn.weights",
                ),
                (
                    "colabdesign-mpnn-weights-soluble",
                    "bindcraft",
                    "colabdesign.mpnn.weights_soluble",
                ),
            },
            observed,
        )

    def test_vanilla_and_soluble_mpnn_are_two_distinct_verified_identities(self) -> None:
        """One mount cannot serve both ColabDesign MPNN directories.

        ColabDesign picks the directory by import, so the two trees are read from
        two installed package paths and their contents genuinely differ.
        """

        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        trees = {
            artifact["artifact_id"]: artifact["tree"]
            for artifact in contract["artifacts"]
            if artifact["artifact_id"].startswith("colabdesign-mpnn-weights-")
        }
        vanilla = trees["colabdesign-mpnn-weights-vanilla"]
        soluble = trees["colabdesign-mpnn-weights-soluble"]
        self.assertNotEqual(vanilla["inventory_sha256"], soluble["inventory_sha256"])
        self.assertNotEqual(vanilla["total_bytes"], soluble["total_bytes"])
        self.assertNotEqual(vanilla["mount_paths"], soluble["mount_paths"])
        self.assertTrue(vanilla["mount_paths"][0].endswith("/colabdesign/mpnn/weights"))
        self.assertTrue(
            soluble["mount_paths"][0].endswith("/colabdesign/mpnn/weights_soluble")
        )
        vanilla_digests = {entry["sha256"] for entry in vanilla["probe_entries"]}
        soluble_digests = {entry["sha256"] for entry in soluble["probe_entries"]}
        # Only the shared one-byte package marker may coincide.
        self.assertEqual(1, len(vanilla_digests & soluble_digests))

    def test_localized_subtrees_declare_the_prefix_they_are_lifted_from(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        for artifact in contract["artifacts"]:
            prefix = artifact["archive"].get("member_prefix")
            with self.subTest(artifact=artifact["artifact_id"]):
                if artifact["artifact_id"].startswith("colabdesign-mpnn-weights-"):
                    self.assertIsNotNone(prefix)
                    self.assertTrue(prefix.endswith("/"))
                    self.assertIn(artifact["archive"]["source_revision"], prefix)
                else:
                    self.assertIsNone(prefix)

    def test_published_localized_identities_are_a_stable_public_interface(self) -> None:
        """Other workers stage against these exact names, digests and paths.

        The artifact-cache plane mirrors trees by artifact ID and admits them by
        inventory digest, so renaming an artifact or recomputing a digest without
        coordinating breaks a consumer that has already staged bytes. Changing a
        row here is therefore a deliberate interface change, not a refactor.
        """

        published = {
            "alphafold2-params": (
                "cdbb7c7c475442712c73f8f8ea40b42fb5dd4fb5c1bf81fdb4642ca9e27f5ac4",
                "/opt/fs2/artifacts/alphafold2-params",
            ),
            "alphafold2-params-bindcraft": (
                "9e25d394b1a7296f7705a5be794c5e29b853beb967835db088069f7cc007aa4f",
                "/models/alphafold2",
            ),
            "colabdesign-mpnn-weights-vanilla": (
                "2602ff1e01c8bdfd5773334e5724fcf0bdfecb3963100f05ad67ad6a5824ee4f",
                "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights",
            ),
            "colabdesign-mpnn-weights-soluble": (
                "54da6672d5677ab27bea0939bbbc591f8877484175a182736ca79af045d0f146",
                "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights_soluble",
            ),
            "boltzgen-inference-molecules": (
                "8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc",
                "/opt/fs2/artifacts/boltzgen-inference-molecules",
            ),
        }
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        observed = {
            item["artifact_id"]: (item["tree"]["inventory_sha256"], item["tree"]["mount_paths"][0])
            for item in contract["artifacts"]
        }
        self.assertEqual(published, observed)

    def test_every_localized_tree_has_a_distinct_identity_and_mount(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        digests = [item["tree"]["inventory_sha256"] for item in contract["artifacts"]]
        mounts = [path for item in contract["artifacts"] for path in item["tree"]["mount_paths"]]
        self.assertEqual(len(set(digests)), len(digests))
        self.assertEqual(len(set(mounts)), len(mounts))

    def test_the_bindcraft_alphafold_tree_carries_its_admission_manifest(self) -> None:
        """The published image admits /models/alphafold2 only through a manifest.

        `artifact_gate.verify_manifest` reads FS2_ARTIFACT_MANIFEST and checks
        artifact_kind and source_revision against the runtime, so the sixteen-file
        upstream tree does not run that image on its own.
        """

        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        artifacts = {item["artifact_id"]: item for item in contract["artifacts"]}
        proteina = artifacts["alphafold2-params"]["tree"]
        bindcraft = artifacts["alphafold2-params-bindcraft"]["tree"]

        self.assertNotIn("generated_entries", proteina)
        generated = bindcraft["generated_entries"]
        self.assertEqual(1, len(generated))
        self.assertEqual("manifest.json", generated[0]["path"])
        self.assertEqual("external-model-artifact-manifest/v1", generated[0]["generator"])
        self.assertEqual(
            {"artifact_kind": "bindcraft-af2-params", "source_revision": "7cd4ace1b7407adf66a50dfefa47de2270f5e4a9"},
            generated[0]["generator_inputs"],
        )
        # The generated document is inside the tree identity, and accounts for
        # exactly the difference between the two trees.
        self.assertEqual(proteina["entry_count"] + 1, bindcraft["entry_count"])
        self.assertEqual(proteina["total_bytes"] + generated[0]["bytes"], bindcraft["total_bytes"])
        self.assertEqual(["/models/alphafold2"], bindcraft["mount_paths"])
        self.assertEqual(
            artifacts["alphafold2-params"]["archive"]["sha256"],
            artifacts["alphafold2-params-bindcraft"]["archive"]["sha256"],
        )

    def test_localization_receipt_example_validates_and_separates_identities(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        artifact = contract["artifacts"][0]
        receipt = {
            "schema": "fs2-serve.nebius.ai/scientific-localization-receipt/v1",
            "artifact_id": artifact["artifact_id"],
            "observed_at": "2026-09-03T02:00:00Z",
            "mount_path": artifact["tree"]["mount_paths"][0],
            "state": "verified",
            "archive_provenance": {
                "filename": artifact["archive"]["filename"],
                "bytes": artifact["archive"]["bytes"],
                "sha256": artifact["archive"]["sha256"],
                "source_uri": artifact["archive"]["source_uri"],
                "source_revision": artifact["archive"]["source_revision"],
                "license_id": artifact["archive"]["license_id"],
                "present_in_mount": False,
            },
            "tree_identity": {
                "entry_count": artifact["tree"]["entry_count"],
                "total_bytes": artifact["tree"]["total_bytes"],
                "inventory_algorithm": "fs2-flat-tree-inventory/v1",
                "inventory_sha256": artifact["tree"]["inventory_sha256"],
                "probe_entries_verified": len(artifact["tree"]["probe_entries"]),
            },
        }
        self.assert_valid("scientific-localization-receipt.schema.json", receipt)
        validator = self.validator("scientific-localization-receipt.schema.json")
        for field in ("archive_provenance", "tree_identity"):
            candidate = copy.deepcopy(receipt)
            del candidate[field]
            self.assertTrue(list(validator.iter_errors(candidate)))


if __name__ == "__main__":
    unittest.main()
