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
        self.assertEqual(11, len(schemas))
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
            "namespace": "fs2-academic-poc",
            "local_queue_name": "academic-scientific",
            "service_account_name": "tenant-selected",
            "persistent_volume_claim": "academic-assets-runtime-rwx",
            "host_path": "/mnt/fs2-reference-data/data",
            "node_selector": {"storage.fs2.nebius/reference-data": "true"},
            "tolerations": [{"key": "dedicated", "value": "fs2-inference"}],
            "supplemental_groups": [1000, 65532],
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
        self.assert_valid("scientific-source-candidate-receipts.schema.json", receipts)
        expected = {
            (
                "alphafold3",
                "upstream-v3-0-4",
            ): "85c4d20505fd5cef05eac22b534d4e793971ae69",
            ("esmfold2", "biohub-v3-4-0"): "827ec128e4cdaf80f7d6f95fb367a08980b34918",
            (
                "esmfold2-fast",
                "biohub-v3-4-0",
            ): "827ec128e4cdaf80f7d6f95fb367a08980b34918",
            (
                "openfold3",
                "upstream-openbind-v0-5-0",
            ): "c4771653c5d0a3ebb0b3af71b05efd64bc44ee86",
            (
                "protenix-v2",
                "upstream-v2-0-0",
            ): "2475421477ab414b571149ad4a875c390ff8a35d",
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
        self.assertEqual({"alphafold3"}, academic)

    def test_trusted_execution_targets_are_exact_and_not_request_fields(self) -> None:
        contract = self.load(CONTRACT_ROOT / "scientific-execution-targets.json")
        self.assert_valid("scientific-execution-targets.schema.json", contract)
        self.assertEqual("fs2-models", contract["default_execution_namespace"])
        self.assertEqual(
            {
                "namespace": "fs2-system",
                "name": "fs2-serve-control-plane-runtime",
            },
            contract["controller_service_account"],
        )
        bindings = {
            (item["model_id"], item["stage_id"]): item for item in contract["bindings"]
        }
        # Both AlphaFold 3 stages share the namespace that holds the licensed
        # claim and the durable controller state; only the queue and pool differ.
        for stage_id in ("data-pipeline", "inference"):
            self.assertEqual(
                "fs2-academic-poc",
                bindings[("alphafold3", stage_id)]["execution_namespace"],
            )
            self.assertEqual(
                "fs2-academic-runner",
                bindings[("alphafold3", stage_id)]["service_account_name"],
            )
        self.assertEqual(
            ("academic-scientific-cpu", "reference-data-cpu"),
            (
                bindings[("alphafold3", "data-pipeline")]["local_queue_name"],
                bindings[("alphafold3", "data-pipeline")]["cluster_queue_name"],
            ),
        )
        self.assertEqual(
            ("academic-scientific", "inference-accelerators"),
            (
                bindings[("alphafold3", "inference")]["local_queue_name"],
                bindings[("alphafold3", "inference")]["cluster_queue_name"],
            ),
        )
        self.assertEqual(
            [
                {
                    "key": "workload.fs2.nebius/reference-data",
                    "operator": "Equal",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            ],
            bindings[("alphafold3", "data-pipeline")]["tolerations"],
        )
        self.assertEqual(
            [
                {
                    "key": "dedicated",
                    "operator": "Equal",
                    "value": "fs2-inference",
                    "effect": "NoSchedule",
                }
            ],
            bindings[("alphafold3", "inference")]["tolerations"],
        )
        # CPU preprocessing carries no accelerator selector, so an MSA can never
        # occupy an idle H100.
        self.assertEqual(
            {
                "capacity.fs2.nebius/pool": "reference-data",
                "capacity.fs2.nebius/type": "regular",
                "storage.fs2.nebius/reference-data": "true",
                "workload.fs2.nebius/reference-data": "true",
            },
            bindings[("alphafold3", "data-pipeline")]["node_selector"],
        )
        self.assertEqual(
            {"accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb"},
            bindings[("alphafold3", "inference")]["node_selector"],
        )
        database = bindings[("alphafold3", "data-pipeline")]["mounts"][0]
        self.assertEqual("operator-host-path", database["kind"])
        self.assertEqual("/mnt/fs2-reference-data/data", database["host_path"])
        # The whole reference plane, with no subPath: the terminal receipt, the
        # dataset tree, its marker and the sibling manifest must all resolve.
        self.assertEqual("/reference-data", database["mount_path"])
        self.assertIsNone(database["sub_path"])
        self.assertEqual(
            "datasets/alphafold3-public-databases-v3.0/"
            "v3.0-paper-snapshot-2022-09-28/sha256/{content_digest_sha256}",
            database["aggregate_tree_policy"]["dataset_relative_path_template"],
        )
        self.assertEqual(
            ".fs2-manifest-sha256",
            database["aggregate_tree_policy"]["manifest_marker"],
        )
        self.assertEqual(
            "file:///mnt/fs2-reference-data/data/datasets/"
            "alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28/"
            "sha256/{content_digest_sha256}",
            database["aggregate_tree_policy"]["dataset_uri_template"],
        )
        self.assertEqual(4097, database["aggregate_tree_policy"]["minimum_file_count"])
        self.assertTrue(
            database["aggregate_tree_policy"]["node_accessibility_receipt_required"]
        )
        self.assertEqual(
            {"storage.fs2.nebius/reference-data": "true"},
            database["aggregate_tree_policy"]["required_node_selector"],
        )
        self.assertTrue(database["operator_owned"])
        self.assertTrue(database["read_only"])
        self.assertEqual([1000], database["supplemental_groups"])
        validator = self.validator("scientific-execution-targets.schema.json")
        # A narrowed root is refused in either direction: any subPath at all
        # would hide the terminal receipt and the sibling manifest.
        for field, replacement in (
            ("host_path", "/mnt/fs2-reference-data"),
            ("mount_path", "/databases"),
            (
                "sub_path",
                "datasets/alphafold3-public-databases-v3.0/"
                "v3.0-paper-snapshot-2022-09-28/sha256/{content_digest_sha256}",
            ),
            (
                "sub_path",
                "datasets/alphafold3-public-databases-v3.0/"
                "v3.0-paper-snapshot-2022-09-28/sha256/{localization_manifest_sha256}",
            ),
        ):
            with self.subTest(operator_host_path_field=field, replacement=replacement):
                candidate = copy.deepcopy(contract)
                candidate_database = next(
                    mount
                    for binding in candidate["bindings"]
                    if (binding["model_id"], binding["stage_id"])
                    == ("alphafold3", "data-pipeline")
                    for mount in binding["mounts"]
                    if mount["kind"] == "operator-host-path"
                )
                candidate_database[field] = replacement
                self.assertTrue(list(validator.iter_errors(candidate)))
        candidate = copy.deepcopy(contract)
        candidate_binding = next(
            binding
            for binding in candidate["bindings"]
            if (binding["model_id"], binding["stage_id"])
            == ("alphafold3", "data-pipeline")
        )
        candidate_binding["node_selector"].pop("storage.fs2.nebius/reference-data")
        self.assertTrue(list(validator.iter_errors(candidate)))
        for stage_id in ("data-pipeline", "inference"):
            with self.subTest(af3_storage_group_stage=stage_id):
                candidate = copy.deepcopy(contract)
                candidate_binding = next(
                    binding
                    for binding in candidate["bindings"]
                    if (binding["model_id"], binding["stage_id"])
                    == ("alphafold3", stage_id)
                )
                candidate_binding["mounts"][0]["supplemental_groups"] = []
                self.assertTrue(list(validator.iter_errors(candidate)))
        parameters = bindings[("alphafold3", "inference")]["mounts"][0]
        self.assertEqual("academic-assets-runtime-rwx", parameters["claim_name"])
        self.assertEqual("fs2-academic-poc", parameters["claim_namespace"])
        self.assertEqual("alphafold3/af3.bin.zst", parameters["sub_path"])
        self.assertEqual("/models/af3.bin.zst", parameters["mount_path"])
        self.assertEqual([65532], parameters["supplemental_groups"])
        af3_inference = bindings[("alphafold3", "inference")]
        af3_cache = next(
            item for item in af3_inference["mounts"] if item["kind"] == "cache"
        )
        self.assertEqual("scientific-alphafold3-cache", af3_cache["claim_name"])
        self.assertEqual("fs2-academic-poc", af3_cache["claim_namespace"])
        self.assertEqual("/cache/alphafold3", af3_cache["mount_path"])
        self.assertIsNone(af3_cache["artifact_id"])
        self.assertFalse(af3_cache["read_only"])
        self.assertEqual(
            {
                "FS2_AF3_CACHE_ROOT": "/cache/alphafold3",
                "FS2_AF3_JAX_CACHE_DIR": "/cache/alphafold3/jax",
                "FS2_AF3_TRITON_CACHE_DIR": "/cache/alphafold3/triton",
                "FS2_AF3_XDG_CACHE_DIR": "/cache/alphafold3/xdg",
            },
            af3_inference["environment"],
        )
        openfold = bindings[("openfold3", "inference")]
        openfold_cache = next(
            item for item in openfold["mounts"] if item["kind"] == "cache"
        )
        self.assertEqual("scientific-openfold3-cache", openfold_cache["claim_name"])
        self.assertEqual("fs2-models", openfold_cache["claim_namespace"])
        self.assertEqual("/cache/openfold3", openfold_cache["mount_path"])
        self.assertIsNone(openfold_cache["artifact_id"])
        self.assertFalse(openfold_cache["read_only"])
        self.assertEqual(
            {
                "TRITON_CACHE_DIR": "/cache/openfold3/triton",
                "TORCH_EXTENSIONS_DIR": "/cache/openfold3/torch-extensions",
                "XDG_CACHE_HOME": "/cache/openfold3/xdg",
            },
            openfold["environment"],
        )
        profiles = self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")
        profile_validator = self.validator("scientific-workload-profile.schema.json")
        for model_id in ("alphafold3", "openfold3", "protenix-v2"):
            with self.subTest(unqualified_cache_model=model_id):
                profile = next(
                    item
                    for item in profiles["profiles"]
                    if item["model_id"] == model_id
                )
                self.assertFalse(profile["route_exposed"])
                self.assertEqual(
                    {
                        "requested_level": "Off",
                        "maximum_level": "L1",
                        "qualified_level": "Off",
                        "state": "candidate-unqualified",
                        "cache_strategy": "shared-pvc",
                        "cache_role": "auxiliary-compiler-jit-l1-plus",
                    },
                    profile["policy"]["fast_start"],
                )
                for field in ("maximum_level", "qualified_level"):
                    with self.subTest(model_id=model_id, forbidden_level_field=field):
                        candidate = copy.deepcopy(profile)
                        candidate["policy"]["fast_start"][field] = "L2"
                        self.assertTrue(list(profile_validator.iter_errors(candidate)))
        for identity, field, replacement in (
            (("alphafold3", "inference"), "claim_namespace", "fs2-models"),
            (("alphafold3", "inference"), "artifact_id", "alphafold3-parameters"),
            (("openfold3", "inference"), "mount_path", "/cache/tenant"),
            (("openfold3", "inference"), "claim_namespace", "tenant-a"),
        ):
            with self.subTest(cache_identity=identity, field=field):
                candidate = copy.deepcopy(contract)
                candidate_binding = next(
                    binding
                    for binding in candidate["bindings"]
                    if (binding["model_id"], binding["stage_id"]) == identity
                )
                candidate_cache = next(
                    item
                    for item in candidate_binding["mounts"]
                    if item["kind"] == "cache"
                )
                candidate_cache[field] = replacement
                self.assertTrue(list(validator.iter_errors(candidate)))
        for identity, field in (
            (("alphafold3", "inference"), "FS2_AF3_JAX_CACHE_DIR"),
            (("openfold3", "inference"), "TORCH_EXTENSIONS_DIR"),
        ):
            with self.subTest(cache_environment_identity=identity, field=field):
                candidate = copy.deepcopy(contract)
                candidate_binding = next(
                    binding
                    for binding in candidate["bindings"]
                    if (binding["model_id"], binding["stage_id"]) == identity
                )
                candidate_binding["environment"].pop(field)
                self.assertTrue(list(validator.iter_errors(candidate)))
        protenix = bindings[("protenix-v2", "sample-structure")]
        cache = next(item for item in protenix["mounts"] if item["kind"] == "cache")
        self.assertEqual("/cache/protenix", cache["mount_path"])
        self.assertFalse(cache["read_only"])
        self.assertEqual(
            {
                "TRITON_CACHE_DIR": "/cache/protenix/triton",
                "CUEQ_TRITON_CACHE_DIR": "/cache/protenix/cueq-triton",
                "TORCH_EXTENSIONS_DIR": "/cache/protenix/torch-extensions",
                "XDG_CACHE_HOME": "/cache/protenix/xdg",
            },
            protenix["environment"],
        )

    def test_af3_profile_schema_accepts_bounded_aggregate_tree_without_file_inventory(
        self,
    ) -> None:
        profiles = self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")
        af3 = copy.deepcopy(
            next(
                item
                for item in profiles["profiles"]
                if item["model_id"] == "alphafold3"
            )
        )
        reference = next(
            item
            for item in af3["artifact_requirements"]
            if item["artifact_id"] == "alphafold3-public-databases-v3.0"
        )
        tree_sha256 = "c" * 64
        relative_path = (
            "datasets/alphafold3-public-databases-v3.0/"
            f"v3.0-paper-snapshot-2022-09-28/sha256/{tree_sha256}"
        )
        reference.update(
            {
                "content_digest_sha256": tree_sha256,
                "localization_manifest_sha256": "d" * 64,
                "required_files": [".fs2-manifest-sha256"],
                "aggregate_tree": {
                    "kind": "aggregate-tree",
                    "dataset_relative_path": relative_path,
                    "dataset_uri": f"file:///mnt/fs2-reference-data/data/{relative_path}",
                    "file_count": 5001,
                },
            }
        )
        self.assertEqual(
            [],
            list(
                self.validator("scientific-workload-profile.schema.json").iter_errors(
                    af3
                )
            ),
        )

        declaration = self.load(
            ROOT / "model-onboarding/declarations/cancer-immunotherapy/alphafold3.json"
        )
        declared_reference = next(
            item
            for item in declaration["model"]["artifacts"]
            if item["artifact_id"] == "alphafold3-public-databases-v3.0"
        )
        declared_reference.update(copy.deepcopy(reference))
        declaration_schema = self.load(
            ROOT / "model-onboarding/model-declaration.schema.json"
        )
        Draft202012Validator.check_schema(declaration_schema)
        self.assertEqual(
            [], list(Draft202012Validator(declaration_schema).iter_errors(declaration))
        )

        truncated = copy.deepcopy(af3)
        truncated_reference = next(
            item
            for item in truncated["artifact_requirements"]
            if item["artifact_id"] == "alphafold3-public-databases-v3.0"
        )
        truncated_reference["file_manifest"] = [
            {"path": ".fs2-manifest-sha256", "sha256": "d" * 64, "size_bytes": 64}
        ]
        self.assertTrue(
            list(
                self.validator("scientific-workload-profile.schema.json").iter_errors(
                    truncated
                )
            )
        )

    def test_af3_checked_in_profile_preserves_live_assets_without_inventing_database_identity(
        self,
    ) -> None:
        profiles = self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")
        af3 = next(
            item for item in profiles["profiles"] if item["model_id"] == "alphafold3"
        )
        self.assertFalse(af3["route_exposed"])
        parameters = next(
            item
            for item in af3["artifact_requirements"]
            if item["artifact_id"] == "alphafold3-parameters"
        )
        self.assertEqual(
            "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff",
            parameters["content_digest_sha256"],
        )
        self.assertEqual(1_020_545_840, parameters["total_size_bytes"])
        reference = next(
            item
            for item in af3["artifact_requirements"]
            if item["artifact_id"] == "alphafold3-public-databases-v3.0"
        )
        self.assertEqual("unresolved", reference["supply_state"])
        self.assertEqual([], reference["required_files"])
        for field in (
            "content_digest_sha256",
            "localization_manifest_sha256",
            "aggregate_tree",
            "file_manifest",
        ):
            self.assertNotIn(field, reference)

    def test_compiled_candidate_profiles_are_canonical_and_non_invocable(self) -> None:
        profiles = self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")
        self.assert_valid("scientific-workload-profiles.schema.json", profiles)
        validator = self.validator("scientific-workload-profile.schema.json")
        expected = {
            ("alphafold3", "upstream-v3-0-4"),
            ("esmfold2", "biohub-v3-4-0"),
            ("esmfold2-fast", "biohub-v3-4-0"),
            ("openfold3", "upstream-openbind-v0-5-0"),
            ("protenix-v2", "upstream-v2-0-0"),
        }
        actual = set()
        for profile in profiles["profiles"]:
            with self.subTest(profile=(profile["model_id"], profile["variant_id"])):
                self.assertFalse(list(validator.iter_errors(profile)))
                self.assertEqual("candidate-unqualified", profile["state"])
                self.assertFalse(profile["route_exposed"])
                self.assertFalse(profile["interface"]["mcp"]["invocable"])
                self.assertEqual("L1", profile["policy"]["fast_start"]["maximum_level"])
                self.assertEqual(
                    "Off", profile["policy"]["fast_start"]["qualified_level"]
                )
                # A published, independently accepted runtime image may be
                # pinned while the workload stays a candidate: the image is one
                # gate, and the route, queue and reference data are others. What
                # a candidate must never do is claim a digest it does not have.
                image_state = profile["execution_identity"]["runtime_image_state"]
                image_digest = profile["execution_identity"]["runtime_image_digest"]
                self.assertIn(image_state, {"build-required", "digest-pinned"})
                if image_state == "build-required":
                    self.assertIsNone(image_digest)
                else:
                    self.assertRegex(str(image_digest), r"^sha256:[0-9a-f]{64}$")
                self.assertNotIn(
                    ".",
                    profile["execution_identity"]["runtime_image_repository"].split(
                        "/", 1
                    )[0],
                )
                self.assertFalse(
                    profile["interface"]["parameter_schema_definition"][
                        "additionalProperties"
                    ]
                )
                actual.add((profile["model_id"], profile["variant_id"]))
        self.assertEqual(expected, actual)

    def test_access_storage_and_offline_artifacts_remain_fail_closed(self) -> None:
        profiles = {
            item["model_id"]: item
            for item in self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")[
                "profiles"
            ]
        }
        for model_id in ("alphafold3",):
            self.assertEqual(
                "license-acceptance-pending", profiles[model_id]["access"]["state"]
            )
            self.assertEqual(
                "LicenseAcceptancePending", profiles[model_id]["access"]["condition"]
            )
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
            self.assertEqual(
                ("cpu", 0), (data["resource_class"], data["resources"]["gpu_count"])
            )
            self.assertEqual(
                ("gpu", 1),
                (inference["resource_class"], inference["resources"]["gpu_count"]),
            )
            reference = next(
                item
                for item in profile["workload"]["storage"]
                if item["purpose"] == "reference-data"
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

        full_artifacts: dict[str, tuple[str, set[str]]] = {}
        for item in profiles["esmfold2"]["artifact_requirements"]:
            repository = item["source"]["repository"]
            revision = item["source"]["revision"]
            if repository in full_artifacts:
                self.assertEqual(revision, full_artifacts[repository][0])
                full_artifacts[repository][1].update(item["required_files"])
            else:
                full_artifacts[repository] = (revision, set(item["required_files"]))
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
            full_artifacts,
        )

    def test_public_profiles_do_not_publish_runtime_locations(self) -> None:
        profiles = self.load(CONTRACT_ROOT / "scientific-workload-profiles.json")
        forbidden_keys = {
            "uri",
            "local_path",
            "filesystem_path",
            "object_path",
            "presigned_url",
        }

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


if __name__ == "__main__":
    unittest.main()
