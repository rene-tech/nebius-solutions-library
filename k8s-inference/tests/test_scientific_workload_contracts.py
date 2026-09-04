from __future__ import annotations

import copy
import hashlib
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
            "scientific-artifact-manifest.schema.json": ("scientific-artifact-manifest.example.json"),
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
        canonical = self.load(SCHEMA_ROOT / "scientific-artifact-pointer.schema.json")
        canonical_shape = {key: canonical[key] for key in ("type", "additionalProperties", "required", "properties")}
        for schema_name in (
            "scientific-artifact-manifest.schema.json",
            "scientific-run-request.schema.json",
            "scientific-run-result.schema.json",
        ):
            with self.subTest(schema=schema_name):
                embedded = self.load(SCHEMA_ROOT / schema_name)["$defs"]["artifact_ref"]
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
        unknown_stage_field["scheduling_snapshot"]["stages"][0]["provider_instance_type"] = "gpu-vendor-specific"
        self.assertTrue(list(validator.iter_errors(unknown_stage_field)))

        unknown_admission_field = copy.deepcopy(result)
        unknown_admission_field["attempts"][0]["scheduling_admission"]["provider_instance_id"] = "instance-secret"
        self.assertTrue(list(validator.iter_errors(unknown_admission_field)))

        invalid_gpu_stage = copy.deepcopy(result)
        invalid_gpu_stage["scheduling_snapshot"]["stages"][1]["accelerator_resource_name"] = None
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
        receipts = self.load(CONTRACT_ROOT / "scientific-source-candidate-receipts.json")
        self.assert_valid("scientific-source-candidate-receipts.schema.json", receipts)
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
        actual = {item["model_id"]: item["source"]["revision"] for item in receipts["receipts"]}
        self.assertEqual(expected, actual)
        self.assertEqual(len(actual), len(receipts["receipts"]))
        self.assertTrue(all(item["status"] == "candidate" for item in receipts["receipts"]))
        self.assertTrue(all(item["qualification_state"] == "unqualified" for item in receipts["receipts"]))
        academic = {item["model_id"] for item in receipts["receipts"] if item["access_profile"] == "academic"}
        self.assertEqual({"alphafold3", "bindcraft"}, academic)

    # BoltzGen is dispatchable on real H100 and localization evidence. It is active, not
    # qualified: its public-completion and scheduler-eligibility receipts can only exist
    # after a real run through the public path, so they are null and the schema forbids
    # calling it qualified while they are.
    DISPATCHABLE = {"boltzgen"}

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
                if profile["model_id"] in self.DISPATCHABLE:
                    self.assertEqual("active", profile["state"])
                    qualification = profile["qualification"]
                    self.assertIsNone(qualification["public_completion_receipt_sha256"])
                    self.assertIsNone(qualification["scheduler_eligibility_receipt_sha256"])
                    self.assertIsNotNone(qualification["h100_semantic_receipt_sha256"])
                    continue
                self.assertEqual("candidate-unqualified", profile["state"])
                self.assertFalse(profile["route_exposed"])
                self.assertEqual(
                    {
                        "boltzgen": "sha256:9c3230424e02d725dc145b8f21a18f283910e1beba1f37466598ee832813820e",
                        "proteina-complexa": "sha256:f4e06b6025a74c924749420f2fce01fb9511aba606a2266c85a9d9e92e3679ca",
                    }[profile["model_id"]],
                    profile["execution_identity"]["runtime_image_digest"],
                )
                self.assertIsNone(profile["execution_identity"]["artifact_manifest_digest"])
                self.assertIsNone(profile["execution_identity"]["execution_identity_sha256"])

    def test_qualified_profile_requires_complete_runnable_identity(self) -> None:
        profile = copy.deepcopy(
            self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")["profiles"][0]
        )
        profile["state"] = "qualified"
        profile["route_exposed"] = True
        profile["source"]["classification"] = "qualified-input"
        profile["execution_identity"]["artifact_manifest_digest"] = "a" * 64
        profile["execution_identity"]["execution_identity_sha256"] = "b" * 64
        profile["interface"]["mcp"]["invocable"] = True
        profile["access"]["state"] = "not-required"
        profile["semantic_validation"]["state"] = "qualified"
        profile["qualification"] = {
            "h100_semantic_receipt_sha256": "c" * 64,
            "public_completion_receipt_sha256": "d" * 64,
            "scheduler_eligibility_receipt_sha256": "e" * 64,
            "execution_map_sha256": "f" * 64,
            "qualified_at": "2026-09-03T08:00:00Z",
        }

        validator = self.validator("scientific-workload-profile.schema.json")
        self.assertEqual([], list(validator.iter_errors(profile)))
        self.assert_valid(
            "scientific-workload-profiles.schema.json",
            {
                "schema": "fs2-serve.nebius.ai/scientific-workload-profiles/v1",
                "profiles": [profile],
            },
        )

        for path, value in (
            (("route_exposed",), False),
            (("source", "classification"), "candidate-input"),
            (("execution_identity", "artifact_manifest_digest"), None),
            (("execution_identity", "execution_identity_sha256"), None),
            (("interface", "mcp", "invocable"), False),
            (("access", "state"), "unverified"),
            (("semantic_validation", "state"), "candidate-unqualified"),
        ):
            with self.subTest(path=path):
                candidate = copy.deepcopy(profile)
                target = candidate
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.assertTrue(list(validator.iter_errors(candidate)))
        missing_receipt = copy.deepcopy(profile)
        del missing_receipt["qualification"]["public_completion_receipt_sha256"]
        self.assertTrue(list(validator.iter_errors(missing_receipt)))

    def test_primary_parameter_schemas_validate_only_canonical_request_parameters(
        self,
    ) -> None:
        cases = (
            (
                "proteina-complexa-parameters.schema.json",
                ROOT / "models/structure/batch-adapters/proteina-complexa/fixtures/positive-protein.json",
            ),
            (
                "boltzgen-parameters.schema.json",
                ROOT / "models/structure/batch-adapters/boltzgen/fixtures/positive-design.json",
            ),
        )
        request_validator = self.validator("scientific-run-request.schema.json")
        for schema_name, fixture_path in cases:
            with self.subTest(schema=schema_name):
                request = self.load(fixture_path)
                self.assertEqual([], list(request_validator.iter_errors(request)))
                self.assertEqual([], list(self.validator(schema_name).iter_errors(request["parameters"])))

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
                "bindcraft-pyrosetta-installed-tree",
                "rfdiffusion-base-checkpoint",
            },
            set(artifacts),
        )

    def test_archive_provenance_never_doubles_as_extracted_tree_identity(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        for artifact in contract["artifacts"]:
            with self.subTest(artifact=artifact["artifact_id"]):
                if artifact["transform"] == "verified-copy":
                    continue
                archive = artifact["archive"]
                tree = artifact["tree"]
                self.assertNotEqual(archive["sha256"], tree["inventory_sha256"])
                self.assertNotEqual(archive["bytes"], tree["total_bytes"])
                for mount in tree["mount_paths"]:
                    # A mount under the runtime artifact root must be this
                    # artifact's own directory; an installed-package tree lives
                    # where the package is installed instead.
                    if mount.startswith("/opt/fs2/artifacts/"):
                        self.assertEqual("/opt/fs2/artifacts/" + artifact["artifact_id"], mount)
                # A runtime mount is a directory of content, so the archive name
                # must never be something the tree could legitimately contain.
                self.assertIsNone(re.compile(tree["entry_path_pattern"]).fullmatch(archive["filename"]))

    def test_rfdiffusion_raw_file_identity_matches_the_artifact_and_runtime_catalogs(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        localized = next(item for item in contract["artifacts"] if item["artifact_id"] == "rfdiffusion-base-checkpoint")
        artifact_manifest = self.load(ROOT / "model-artifacts/manifest-rfdiffusion-checkpoints.json")
        catalog_file = next(item for item in artifact_manifest["content"]["files"] if item["path"] == "Base_ckpt.pt")
        image_lock = self.load(ROOT / "models/cancer-immunotherapy/runtime-images/rfdiffusion/image-lock.json")
        runtime_file = next(item for item in image_lock["external_artifacts"] if item["file"] == "Base_ckpt.pt")

        self.assertEqual("verified-copy", localized["transform"])
        self.assertNotIn("archive", localized)
        for field, runtime_name in (("sha256", "sha256"), ("bytes", "bytes")):
            self.assertEqual(catalog_file[field], localized["file"][field])
            self.assertEqual(runtime_file[runtime_name], localized["file"][field])
        self.assertEqual(runtime_file["source"], localized["file"]["source_uri"])
        self.assertEqual("fs2-raw-file/v1", localized["tree"]["inventory_algorithm"])
        self.assertNotEqual(localized["file"]["sha256"], localized["tree"]["inventory_sha256"])
        self.assertEqual(localized["file"]["bytes"], localized["tree"]["total_bytes"])

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
                ("bindcraft-pyrosetta-installed-tree", "bindcraft", "PYTHONPATH"),
                ("rfdiffusion-base-checkpoint", "rfdiffusion", "--artifact-root"),
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
        self.assertTrue(soluble["mount_paths"][0].endswith("/colabdesign/mpnn/weights_soluble"))
        vanilla_digests = {entry["sha256"] for entry in vanilla["probe_entries"]}
        soluble_digests = {entry["sha256"] for entry in soluble["probe_entries"]}
        # Only the shared one-byte package marker may coincide.
        self.assertEqual(1, len(vanilla_digests & soluble_digests))

    def test_localized_subtrees_declare_the_prefix_they_are_lifted_from(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        for artifact in contract["artifacts"]:
            with self.subTest(artifact=artifact["artifact_id"]):
                if artifact["transform"] == "verified-copy":
                    self.assertNotIn("archive", artifact)
                    self.assertNotIn("member_prefix", artifact["file"])
                    continue
                prefix = artifact["archive"].get("member_prefix")
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
            # Identified by the academic-assets plane, not by this one. The digest
            # is that plane's fs2-tree-manifest/v1 value, reused verbatim.
            "bindcraft-pyrosetta-installed-tree": (
                "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d",
                "/opt/fs2/academic/pyrosetta-bindcraft/site-packages",
            ),
            "rfdiffusion-base-checkpoint": (
                "7f34c945e580dbf5ba96596dcd325150f6452f7a76ee06a3784b2891a9d4c03c",
                "/opt/fs2/artifacts/rfdiffusion-base-checkpoint",
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

    def test_raw_file_receipt_requires_truthful_presence_semantics(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        artifact = next(item for item in contract["artifacts"] if item["artifact_id"] == "rfdiffusion-base-checkpoint")
        receipt = {
            "schema": "fs2-serve.nebius.ai/scientific-localization-receipt/v1",
            "artifact_id": artifact["artifact_id"],
            "observed_at": "2026-09-03T06:30:00Z",
            "mount_path": artifact["tree"]["mount_paths"][0],
            "state": "verified",
            "file_provenance": {
                **artifact["file"],
                "present_in_mount": True,
            },
            "tree_identity": {
                "entry_count": 1,
                "directory_count": 0,
                "symlink_count": 0,
                "total_bytes": artifact["tree"]["total_bytes"],
                "inventory_algorithm": "fs2-raw-file/v1",
                "inventory_sha256": artifact["tree"]["inventory_sha256"],
                "probe_entries_verified": 1,
            },
        }
        self.assert_valid("scientific-localization-receipt.schema.json", receipt)
        receipt["file_provenance"]["present_in_mount"] = False
        self.assertTrue(list(self.validator("scientific-localization-receipt.schema.json").iter_errors(receipt)))

    def test_probes_correlate_nodes_by_digest_and_never_publish_the_node_name(self) -> None:
        """These receipts are checked into a public repository.

        `spec.nodeName` is an opaque Nebius instance ID here, so a probe that
        copied it into its report leaked a private resource ID on every run.
        Catch that at the probe, not only at the export gate, which can see it
        only once a leaking receipt is already committed.
        """

        localization = ROOT / "models/cancer-immunotherapy/artifact-localization"
        probes = sorted((localization / "probes").glob("*_probe.py"))
        self.assertEqual(4, len(probes))
        for probe in probes:
            with self.subTest(probe=probe.name):
                source = probe.read_text(encoding="utf-8")
                self.assertIn('"node_digest": node_digest()', source)
                self.assertNotIn('"node": os.environ', source)

        for receipt_path in sorted((localization / "evidence").glob("*probe*.json")):
            with self.subTest(evidence=receipt_path.name):
                report = self.load(receipt_path)
                self.assertNotIn("node", report)
                self.assertRegex(report["node_digest"], r"^[0-9a-f]{16}$")

    def test_runtime_probes_exclude_the_generation_marker_from_model_entries(self) -> None:
        """The admission marker is metadata, never an input owned by a model."""

        localization = ROOT / "models/cancer-immunotherapy/artifact-localization"
        for probe in sorted((localization / "probes").glob("*_probe.py")):
            with self.subTest(probe=probe.name):
                source = probe.read_text(encoding="utf-8")
                self.assertIn('RUNTIME_MARKER_NAME = ".fs2-runtime-tree.json"', source)
                self.assertIn("item.name != RUNTIME_MARKER_NAME", source)

    def test_the_bindcraft_handoff_joins_exactly_four_immutable_trees(self) -> None:
        """BindCraft needs AF2, both MPNN weight sets, and PyRosetta.

        The handoff is what an artifact-catalog consumer reads instead of
        rediscovering these paths, so the join, the content-addressed paths and
        the marker digests are pinned here rather than left to a renderer run.
        """

        handoff = self.load(ROOT / "models/cancer-immunotherapy/artifact-localization/evidence/binding-handoff.json")
        self.assertEqual(
            [
                "alphafold2-params-bindcraft",
                "bindcraft-pyrosetta-installed-tree",
                "colabdesign-mpnn-weights-soluble",
                "colabdesign-mpnn-weights-vanilla",
            ],
            handoff["models"]["bindcraft"],
        )
        by_id = {entry["artifact_id"]: entry for entry in handoff["artifacts"]}
        for artifact_id in handoff["models"]["bindcraft"]:
            with self.subTest(artifact=artifact_id):
                entry = by_id[artifact_id]
                marker = entry["marker"]
                self.assertRegex(marker["manifest_digest"], r"^[0-9a-f]{64}$")
                # The digest a consumer pins is the SHA-256 of exactly these bytes.
                rendered = (json.dumps(marker["document"], indent=2, sort_keys=True) + "\n").encode()
                self.assertEqual(hashlib.sha256(rendered).hexdigest(), marker["manifest_digest"])
                # Identity, placement and provenance all agree.
                document = marker["document"]
                self.assertEqual(entry["generation"], document["generation"])
                self.assertEqual(entry["generation"], document["inventory_sha256"])
                self.assertEqual(entry["volume"]["sub_path"], document["sub_path"])
                self.assertEqual(entry["tree_identity"]["inventory_algorithm"], document["inventory_algorithm"])
                self.assertTrue(entry["volume"]["immutable"])
                self.assertTrue(entry["volume"]["read_only"])
                self.assertNotEqual(entry["archive_provenance"]["sha256"], entry["generation"])

    def test_every_runtime_binding_is_content_addressed_and_never_a_mutable_path(self) -> None:
        """A producer's install path says where bytes were built, not which bytes.

        Binding a runtime to it would let the tree change underneath an already
        admitted workload, so every binding, licensed trees included, is a
        content-addressed generation and the install path is recorded only as a
        non-bindable input.
        """

        handoff = self.load(ROOT / "models/cancer-immunotherapy/artifact-localization/evidence/binding-handoff.json")
        for entry in handoff["artifacts"]:
            with self.subTest(artifact=entry["artifact_id"]):
                sub_path = entry["volume"]["sub_path"]
                prefix = (
                    "scientific-localization/private"
                    if entry["visibility"] == "tenant-private"
                    else "scientific-localization/public"
                )
                self.assertEqual(
                    f"{prefix}/generations/{entry['artifact_id']}/sha256/{entry['generation']}",
                    sub_path,
                )
                self.assertTrue(entry["volume"]["immutable"])
                self.assertTrue(entry["marker"]["in_generation"])
                self.assertEqual(f"{sub_path}/.fs2-runtime-tree.json", entry["marker"]["path"])
                promoted = entry.get("promoted_from")
                if promoted is not None:
                    # The producer path is provenance, and is marked unusable as a binding.
                    self.assertTrue(promoted["mutable"])
                    self.assertFalse(promoted["runtime_bindable"])
                    self.assertNotIn("/sha256/", promoted["sub_path"])
                    self.assertNotEqual(promoted["sub_path"], sub_path)

    def test_public_bytes_and_licensed_bytes_use_separate_storage_planes(self) -> None:
        """The academic claim is licensed and tenant-scoped, not artifact storage.

        Public model artifacts belong on the Terraform-managed reference-data
        host root that every labelled node mounts; the licensed tree stays in the
        academic claim. Putting public bytes on that claim would freeze a volume
        provisioned for one licence chain into a general cache.
        """

        handoff = self.load(ROOT / "models/cancer-immunotherapy/artifact-localization/evidence/binding-handoff.json")
        public = handoff["volumes"]["public"]
        private = handoff["volumes"]["tenant-private"]
        self.assertEqual("host-path", public["kind"])
        self.assertEqual("/mnt/fs2-reference-data/data", public["host_root"])
        self.assertEqual("true", public["node_selector"]["storage.fs2.nebius/reference-data"])
        self.assertEqual("persistent-volume-claim", private["kind"])
        self.assertEqual("academic-assets-runtime-rwx", private["claim"])
        # Both planes exist. The five public generations are qualified, while
        # the licensed generation remains a rendered interface only.
        self.assertEqual("provisioned", public["plane_state"])
        self.assertEqual("provisioned", private["plane_state"])
        self.assertEqual("qualified", public["binding_state"])
        self.assertEqual("rendered", private["binding_state"])

        for entry in handoff["artifacts"]:
            with self.subTest(artifact=entry["artifact_id"]):
                volume = entry["volume"]
                if entry["visibility"] == "public":
                    self.assertEqual("qualified", volume["binding_state"])
                    # A host plane is addressed by host root and node label, and
                    # carries no claim that a reader could try to mount.
                    self.assertEqual("host-path", volume["kind"])
                    self.assertNotIn("claim", volume)
                    self.assertEqual(f"{public['host_root']}/{volume['sub_path']}", volume["host_path"])
                    self.assertEqual(public["node_selector"], volume["node_selector"])
                else:
                    self.assertEqual("rendered", volume["binding_state"])
                    self.assertEqual("persistent-volume-claim", volume["kind"])
                    self.assertEqual(private["claim"], volume["claim"])
                    self.assertEqual(private["namespace"], volume["namespace"])
                    self.assertNotIn("host_root", volume)

    def test_the_marker_addresses_the_plane_its_generation_actually_lives_on(self) -> None:
        """A claim for a host directory, or a host root for a claim, names nothing."""

        handoff = self.load(ROOT / "models/cancer-immunotherapy/artifact-localization/evidence/binding-handoff.json")
        for entry in handoff["artifacts"]:
            with self.subTest(artifact=entry["artifact_id"]):
                document = entry["marker"]["document"]
                volume = entry["volume"]
                self.assertEqual(volume["kind"], document["volume_kind"])
                self.assertEqual(volume["sub_path"], document["sub_path"])
                if volume["kind"] == "host-path":
                    self.assertEqual(volume["host_root"], document["host_root"])
                    self.assertEqual("", document["namespace"])
                    self.assertEqual("", document["claim"])
                else:
                    self.assertEqual(volume["claim"], document["claim"])
                    self.assertEqual(volume["namespace"], document["namespace"])
                    self.assertEqual("", document["host_root"])

    def test_pyrosetta_reuses_the_academic_plane_identity_verbatim(self) -> None:
        """One tree, one identity. The producing plane's digest is authoritative."""

        contract = self.load(CONTRACT_ROOT / "scientific-artifact-localization.json")
        tree = next(
            item for item in contract["artifacts"] if item["artifact_id"] == "bindcraft-pyrosetta-installed-tree"
        )
        state = self.load(ROOT / "academic-assets/evidence/live-acceptance-state.json")
        installed = state["semantic_evidence"]["installed_tree"]
        self.assertEqual(installed["tree_manifest_algorithm"], tree["tree"]["inventory_algorithm"])
        self.assertEqual(installed["tree_manifest_sha256"], tree["tree"]["inventory_sha256"])
        self.assertEqual(installed["files_installed"], tree["tree"]["entry_count"])
        self.assertEqual(installed["tree_total_bytes"], tree["tree"]["total_bytes"])
        # Provenance stays distinct from the tree it produced.
        wheel = self.load(ROOT / "academic-assets/contracts/academic-assets.json")
        artifact = wheel["assets"]["pyrosetta-bindcraft"]["artifact"]
        self.assertEqual(artifact["sha256"], tree["archive"]["sha256"])
        self.assertEqual(artifact["size_bytes"], tree["archive"]["bytes"])
        self.assertNotEqual(tree["archive"]["sha256"], tree["tree"]["inventory_sha256"])

    def test_public_qualified_bindings_have_exact_receipts_and_node_probes(self) -> None:
        """Qualified is evidence-backed; the pending licensed tree stays rendered."""

        evidence_root = ROOT / "models/cancer-immunotherapy/artifact-localization/evidence"
        handoff = self.load(evidence_root / "binding-handoff.json")
        publication = self.load(evidence_root / "public-generation-publication-20260903.json")
        qualification = self.load(evidence_root / "public-generation-node-qualification-20260903.json")
        evidence = handoff["evidence"]
        self.assertEqual("public-qualified-private-rendered", evidence["state"])
        self.assertFalse(evidence["generations_published"])
        self.assertTrue(evidence["public_generations_published"])
        self.assertEqual("published", publication["outcome"]["state"])
        self.assertEqual("qualified", qualification["outcome"]["state"])
        self.assertEqual(2, qualification["outcome"]["h100_nodes_admitted"])
        self.assertEqual(0, qualification["outcome"]["gpu_allocations_created"])
        self.assertTrue(publication["cleanup"]["exact_name_absence_verified"])
        self.assertTrue(qualification["cleanup"]["exact_name_absence_verified"])
        self.assertTrue(publication["cleanup"]["immutable_generations_retained"])
        self.assertTrue(qualification["cleanup"]["immutable_generations_retained"])

        public_ids = {entry["artifact_id"] for entry in handoff["artifacts"] if entry["visibility"] == "public"}
        raw_artifact_id = "rfdiffusion-base-checkpoint"
        original_public_ids = public_ids - {raw_artifact_id}
        self.assertEqual(public_ids, set(evidence["qualified_artifacts"]))
        self.assertEqual({"bindcraft-pyrosetta-installed-tree"}, set(evidence["pending_artifacts"]))

        receipts = {item["receipt"]["artifact_id"]: item["receipt"] for item in publication["artifacts"]}
        receipts[raw_artifact_id] = self.load(evidence_root / "rfdiffusion-base-checkpoint-stage-20260903.json")
        receipt_refs = {item["artifact_id"]: item for item in evidence["promotion_receipts"]}
        probe_refs = {item["artifact_id"]: item for item in evidence["node_probes"]}
        self.assertEqual(
            original_public_ids,
            {item["receipt"]["artifact_id"] for item in publication["artifacts"]},
        )
        self.assertEqual(public_ids, set(receipts))
        self.assertEqual(public_ids, set(receipt_refs))
        self.assertEqual(public_ids, set(probe_refs))

        admission_jobs = [job for job in qualification["jobs"] if job["kind"] == "all-public-marker-admission"]
        self.assertEqual(2, len(admission_jobs))
        self.assertEqual(2, len({job["node_digest"] for job in admission_jobs}))
        model_jobs = {job["job"]: job for job in qualification["jobs"] if job["kind"] == "model-native-loader"}
        receipt_validator = self.validator("scientific-localization-receipt.schema.json")
        by_id = {entry["artifact_id"]: entry for entry in handoff["artifacts"]}

        for artifact_id in public_ids:
            with self.subTest(artifact=artifact_id):
                entry = by_id[artifact_id]
                receipt = receipts[artifact_id]
                self.assertFalse(list(receipt_validator.iter_errors(receipt)))
                self.assertEqual("qualified", entry["volume"]["binding_state"])
                self.assertEqual("verified", receipt["state"])
                self.assertEqual(entry["generation"], receipt["tree_identity"]["inventory_sha256"])
                self.assertEqual(entry["generation"], receipt["observation"]["generation"])
                self.assertEqual(
                    entry["volume"]["sub_path"],
                    receipt["observation"]["generation_sub_path"],
                )
                self.assertEqual(
                    entry["marker"]["manifest_digest"],
                    receipt["observation"]["marker_sha256"],
                )
                if artifact_id == raw_artifact_id:
                    self.assertNotIn("archive_provenance", receipt)
                    self.assertTrue(receipt["file_provenance"]["present_in_mount"])
                else:
                    self.assertFalse(receipt["archive_provenance"]["present_in_mount"])
                self.assertEqual(entry["generation"], receipt_refs[artifact_id]["generation"])
                self.assertEqual(
                    entry["marker"]["manifest_digest"],
                    receipt_refs[artifact_id]["marker_sha256"],
                )

                if artifact_id in original_public_ids:
                    for job in admission_jobs:
                        admitted = {item["artifact_id"]: item for item in job["marker_admissions"]}[artifact_id]
                        self.assertEqual("admitted", admitted["state"])
                        self.assertEqual(entry["generation"], admitted["generation"])
                        self.assertEqual(entry["marker"]["manifest_digest"], admitted["manifest_digest"])

                    model_job = model_jobs[probe_refs[artifact_id]["model_native_probe_job"]]
                    self.assertEqual("passed", model_job["result"]["state"])
                    self.assertEqual([0], model_job["exit_codes"])

        raw_admission = self.load(evidence_root / "node-admit-rfdiffusion-base-checkpoint-h100-20260903.json")
        raw_probe = self.load(evidence_root / "rfdiffusion-checkpoint-probe-h100-20260903.json")
        self.assertEqual("admitted", raw_admission["state"])
        self.assertEqual(receipts[raw_artifact_id]["observation"]["marker_sha256"], raw_admission["manifest_digest"])
        self.assertEqual("passed", raw_probe["state"])
        self.assertEqual(receipts[raw_artifact_id]["file_provenance"]["sha256"], raw_probe["checkpoint"]["sha256"])
        self.assertEqual(receipts[raw_artifact_id]["tree_identity"]["inventory_sha256"], raw_probe["generation"])

        private = by_id["bindcraft-pyrosetta-installed-tree"]
        self.assertEqual("rendered", private["volume"]["binding_state"])
        self.assertNotIn(private["artifact_id"], receipts)

        proteina = model_jobs["fs2-localize-qualify-proteina-complexa-r20260903pub2-proteina"]
        self.assertEqual(16, len(proteina["result"]["entries"]))
        bindcraft = model_jobs["fs2-localize-qualify-bindcraft-public-r20260903pub2-bindcraft"]
        for weight in bindcraft["result"]["mpnn_weights"].values():
            self.assertEqual(5, len(weight["entries"]))
            self.assertNotIn(".fs2-runtime-tree.json", weight["entries"])
        boltzgen = model_jobs["fs2-localize-qualify-boltzgen-r20260903pub2-boltzgen"]
        self.assertEqual(45227, boltzgen["result"]["entry_count"])

    def test_no_checked_in_localization_evidence_claims_an_unproven_live_state(self) -> None:
        """Catch the overclaim in any evidence file, not only the handoff."""

        evidence = ROOT / "models/cancer-immunotherapy/artifact-localization/evidence"
        for path in sorted(evidence.glob("*.json")):
            with self.subTest(evidence=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn('"binding_state": "live"', text)
                document = self.load(path)
                # This whole handoff remains false until the tenant-private
                # PyRosetta generation is independently promoted and qualified.
                if "evidence" in document:
                    self.assertIsNot(document["evidence"].get("generations_published"), True)


if __name__ == "__main__":
    unittest.main()
