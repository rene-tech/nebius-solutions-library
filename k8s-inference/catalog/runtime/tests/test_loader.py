from __future__ import annotations

import copy
import json
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from fs2_serve_catalog.artifacts import canonical_bytes, load_artifact_manifest
from fs2_serve_catalog.loader import (
    CatalogError,
    REQUIRED_TESTED_MODEL_IDS,
    SUPPORTED_MODEL_FAMILIES,
    execution_identity,
    load_catalog,
    resource_placement_identity,
)


CATALOG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CATALOG_ROOT / "packaged-repository"


class CatalogLoaderTests(unittest.TestCase):
    def load(self):
        return load_catalog(CATALOG_ROOT, repo_root=REPO_ROOT)

    def copy_catalog(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        target = Path(temporary.name) / "catalog"
        shutil.copytree(CATALOG_ROOT, target)
        return temporary, target

    def mutate_variant_contract(self, target: Path, mutate) -> None:
        index_path = target / "catalog.json"
        index = json.loads(index_path.read_text())
        variant_path = target / index["model_variants"]["path"]
        document = json.loads(variant_path.read_text())
        mutate(document)
        variant_path.write_text(json.dumps(document) + "\n")
        index["model_variants"]["sha256"] = hashlib.sha256(
            variant_path.read_bytes()
        ).hexdigest()
        index_path.write_text(json.dumps(index) + "\n")

    def test_loads_required_baseline_subset_and_explicit_blocked_candidate(self) -> None:
        catalog = self.load()
        self.assertTrue(REQUIRED_TESTED_MODEL_IDS.issubset(catalog.tested_model_ids))
        self.assertEqual((), catalog.blocked_candidate_ids)
        self.assertEqual((), catalog.routable_model_ids())

    def test_model_variants_are_typed_paired_and_never_static_route_authority(self) -> None:
        catalog = self.load()
        self.assertEqual(16, len(catalog.model_variants))
        self.assertEqual(tuple(sorted(catalog.model_variants)), catalog.candidate_variant_ids())
        self.assertEqual((), catalog.routable_variant_ids())
        for model_id in (
            "boltz2",
            "diffdock",
            "evo2-40b",
            "molmim",
            "nv-segment-ct",
            "proteinmpnn",
            "rfdiffusion",
        ):
            with self.subTest(model_id=model_id):
                variants = catalog.variants_for(model_id)
                self.assertEqual(
                    {"portable", "blackwell-sm103"},
                    {variant.runtime_architecture for variant in variants},
                )
                self.assertEqual({"exact-model"}, {variant.relationship for variant in variants})
                self.assertTrue(all(not item.to_dict()["promotion"]["route_exposed"] for item in variants))

        molmim = catalog.model("molmim").to_dict()
        self.assertEqual("ngc-nim", molmim["model"]["source"]["kind"])
        self.assertEqual(
            "sha256:7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa",
            molmim["model"]["source"]["revision"],
        )
        molmim_variants = catalog.variants_for("molmim")
        self.assertEqual(2, len(molmim_variants))
        self.assertEqual(
            {"portable", "blackwell-sm103"},
            {variant.runtime_architecture for variant in molmim_variants},
        )
        self.assertTrue(all(variant.relationship == "exact-model" for variant in molmim_variants))
        alternatives = catalog.variants_for("genmol")
        self.assertEqual(2, len(alternatives))
        for alternative in alternatives:
            value = alternative.to_dict()
            self.assertEqual("exact-model", alternative.relationship)
            self.assertEqual("genmol", alternative.exposed_model_id)
            self.assertEqual("nvidia/NV-GenMol-89M-v2", value["source"]["repository"])
            self.assertFalse(value["relationship"]["distinct_base_record_required"])
            self.assertEqual("genmol", value["relationship"]["subject_model_id"])
        candidate = catalog.fallback_candidate("genmol-hf-v2")
        self.assertEqual("genmol", candidate.lane_id)
        self.assertEqual("exact-model", candidate.relationship)
        self.assertEqual(
            {
                "reference_model_id": "molmim",
                "relationship": "capability-equivalent",
                "alias_allowed": False,
            },
            candidate.to_dict()["secondary_non_alias_alternative"],
        )
        segment = catalog.model_variant("nv-segment-ct-upstream-blackwell-sm103").to_dict()
        self.assertEqual(
            {
                "architecture": "blackwell-sm103",
                "build_state": "built-attested",
                "image_digest": (
                    "sha256:834b6694b7e096c393193d12306ef9b3f0bb313efa806a9c253f49f1f47281fd"
                ),
                "device_capability": "sm103-qualified",
                "network_startup": "deny-until-exact-mounted-artifact-ready",
            },
            segment["runtime"],
        )
        self.assertEqual("candidate-unqualified", segment["promotion"]["state"])
        self.assertFalse(segment["promotion"]["route_exposed"])
        self.assertTrue(
            all(
                segment["promotion"][field] is None
                for field in (
                    "supply_receipt_digest",
                    "qualification_receipt_digest",
                    "independent_review_receipt_digest",
                )
            )
        )
        self.assertEqual((), catalog.routable_model_ids())

    def test_all_fallback_candidates_have_one_explicit_fail_closed_identity_join(self) -> None:
        catalog = self.load()
        self.assertEqual(11, len(catalog.fallback_candidates))
        self.assertEqual(
            {
                "boltz2-hf",
                "diffdock-upstream-v1-1",
                "evo2-40b-hf",
                "genmol-hf-v2",
                "molmim-ngc-70m-v24-3",
                "msa-search-pdb70-colabfold",
                "nv-segment-ct-hf",
                "openfold2-hf-mirror",
                "openfold3-preview2-hf",
                "proteinmpnn-upstream-2023-06",
                "rfdiffusion-upstream",
            },
            set(catalog.fallback_candidates),
        )
        mapped = {
            variant_id: catalog.fallback_for_variant(variant_id)
            for variant_id in catalog.model_variants
        }
        self.assertEqual(set(catalog.model_variants), set(mapped))
        self.assertEqual(
            ("proteinmpnn-upstream-2023-06", "portable"),
            (mapped["proteinmpnn-upstream-portable"][0].candidate_id,
             mapped["proteinmpnn-upstream-portable"][1]),
        )
        self.assertEqual(
            "mapped-source-only",
            catalog.fallback_candidate("molmim-ngc-70m-v24-3").state,
        )
        self.assertEqual(
            "blocked-license",
            catalog.fallback_candidate("msa-search-pdb70-colabfold").state,
        )

    def test_model_variant_exact_source_identities_are_regression_locked(self) -> None:
        catalog = self.load()
        expected = {
            "boltz2-hf-portable": "6fdef46d763fee7fbb83ca5501ccceff43b85607",
            "diffdock-upstream-portable": "85c49b60d3e0b0182a59ee43a34a6d7036981284",
            "evo2-40b-upstream-portable": "d529aa57c30771814217ad89baaeaf6e2315c7d7",
            "nv-genmol-89m-v2-portable": "2acccbd6eee62f2a90334ccf14dc7d2b17ef9e80",
            "nv-segment-ct-upstream-portable": "afb51518689f71e6abb367ee6301b2cd0225c66a",
            "proteinmpnn-upstream-portable": "8907e6671bfbfc92303b5f79c4b5e6ce47cdef57",
            "rfdiffusion-upstream-portable": "86507b6538f51fce57b5a72477165f03999ed7ae",
        }
        for variant_id, revision in expected.items():
            with self.subTest(variant_id=variant_id):
                value = catalog.model_variant(variant_id).to_dict()
                self.assertEqual(revision, value["source"]["revision"])
                self.assertIn(revision, value["source"]["revision_url"])
                self.assertEqual(
                    "expected-only-incomplete-manifest",
                    value["source"]["artifact"]["identity_state"],
                )
                self.assertIsNone(value["source"]["artifact"]["manifest_sha256"])
                self.assertIsNone(value["runtime"]["image_digest"])
                self.assertEqual(
                    "metadata-reviewed-artifact-unbound",
                    value["source"]["license"]["state"],
                )

    def test_retained_sm103_variant_images_are_exact_and_still_non_authoritative(self) -> None:
        catalog = self.load()
        expected = {
            "boltz2-hf-blackwell-sm103": "sha256:ec4ccb67476f0783d1b756959362318691ef44477e485a62eb4f1c77eff10c46",
            "diffdock-upstream-blackwell-sm103": "sha256:cb3875f7d66b8d170d0e3f16d3d9a63aee8d63fbb23fdf65ec7ea0214d849529",
            "evo2-40b-upstream-blackwell-sm103": "sha256:5bee4a3103f4111a5ff4dc597d2e052b39e1d66c782941b5fb64957bb1ab601c",
            "molmim-exact-weights-blackwell-sm103": "sha256:b51c758f2785accb2121db2dd322f81ed6ebb6142bd7a9988714867c5fe51641",
            "nv-genmol-89m-v2-blackwell-sm103": "sha256:c0ce8cab57295b6ba2fc4be17d5f5a78751f76b7e93754309fa353d9c2f54a1f",
            "proteinmpnn-upstream-blackwell-sm103": "sha256:13d195ac5e24ca9d75de058f08141ece37e60962dcdffb50b6d24ea474313d47",
            "rfdiffusion-upstream-blackwell-sm103": "sha256:c715c5ec39e1f62761039f07867fe30ae08c60442dbdc05e700084becc456409",
        }
        for variant_id, image in expected.items():
            with self.subTest(variant_id=variant_id):
                value = catalog.model_variant(variant_id).to_dict()
                self.assertEqual("built-attested", value["runtime"]["build_state"])
                self.assertEqual("sm103-qualified", value["runtime"]["device_capability"])
                self.assertEqual(image, value["runtime"]["image_digest"])
                self.assertFalse(value["promotion"]["route_exposed"])

    def test_model_variant_identity_supply_and_promotion_substitution_fail_closed(self) -> None:
        cases = {
            "genmol-relabelled-molmim": lambda value: value["variants"][
                "nv-genmol-89m-v2-portable"
            ].update({"exposed_model_id": "molmim"}),
            "genmol-subject-relabelled-molmim": lambda value: value["variants"][
                "nv-genmol-89m-v2-portable"
            ]["relationship"].update({"subject_model_id": "molmim"}),
            "exact-variant-changes-model-id": lambda value: value["variants"][
                "diffdock-upstream-portable"
            ].update({"exposed_model_id": "diffdock-alternative"}),
            "variant-map-key-substitution": lambda value: value["variants"][
                "diffdock-upstream-portable"
            ].update({"variant_id": "diffdock-upstream-blackwell-sm103"}),
            "mutable-source-revision": lambda value: value["variants"][
                "evo2-40b-upstream-portable"
            ]["source"].update({"revision": "main"}),
            "source-url-not-revision-bound": lambda value: value["variants"][
                "rfdiffusion-upstream-portable"
            ]["source"].update({"revision_url": "https://github.com/RosettaCommons/RFdiffusion"}),
            "license-url-not-revision-bound": lambda value: value["variants"][
                "rfdiffusion-upstream-portable"
            ]["source"]["license"].update(
                {"source_url": "https://github.com/RosettaCommons/RFdiffusion"}
            ),
            "vendor-baseline-substituted": lambda value: value["variants"][
                "diffdock-upstream-portable"
            ]["relationship"]["vendor_baseline"].update({"model_id": "evo2-40b"}),
            "invented-complete-manifest": lambda value: value["variants"][
                "proteinmpnn-upstream-portable"
            ]["source"]["artifact"].update(
                {"manifest_sha256": hashlib.sha256(b"invented-manifest").hexdigest()}
            ),
            "license-verified-without-bytes": lambda value: value["variants"][
                "nv-segment-ct-upstream-portable"
            ]["source"]["license"].update({"state": "verified-artifact"}),
            "incomplete-supply-claims": lambda value: value["variants"][
                "diffdock-upstream-portable"
            ]["receipt_requirements"].update({"supply_claims": ["immutable-image"]}),
            "portable-relabelled-sm103": lambda value: value["variants"][
                "evo2-40b-upstream-portable"
            ]["runtime"].update({"architecture": "blackwell-sm103"}),
            "route-without-promotion": lambda value: value["variants"][
                "rfdiffusion-upstream-portable"
            ]["promotion"].update({"route_exposed": True}),
            "digest-shaped-static-promotion": lambda value: value["variants"][
                "diffdock-upstream-portable"
            ]["promotion"].update(
                {
                    "state": "qualified",
                    "supply_receipt_digest": hashlib.sha256(b"supply").hexdigest(),
                    "qualification_receipt_digest": hashlib.sha256(
                        b"qualification"
                    ).hexdigest(),
                    "independent_review_receipt_digest": hashlib.sha256(
                        b"review"
                    ).hexdigest(),
                }
            ),
            "fallback-key-substitution": lambda value: value["fallback_candidates"][
                "proteinmpnn-upstream-2023-06"
            ].update({"candidate_id": "rfdiffusion-upstream"}),
            "fallback-profile-substitution": lambda value: value["fallback_candidates"][
                "proteinmpnn-upstream-2023-06"
            ]["profile_variants"].update(
                {"portable": "proteinmpnn-upstream-blackwell-sm103"}
            ),
            "variant-mapped-twice": lambda value: value["fallback_candidates"][
                "diffdock-upstream-v1-1"
            ]["profile_variants"].update(
                {"portable": "proteinmpnn-upstream-portable"}
            ),
            "genmol-secondary-alias": lambda value: value["fallback_candidates"][
                "genmol-hf-v2"
            ]["secondary_non_alias_alternative"].update({"alias_allowed": True}),
            "static-contract-route-authority": lambda value: value.update(
                {"route_authority": True}
            ),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                self.mutate_variant_contract(target, mutate)
                with self.assertRaises(CatalogError):
                    load_catalog(target, repo_root=REPO_ROOT)

    def test_built_variant_remains_candidate_until_signed_overlay_qualification(self) -> None:
        _, target = self.copy_catalog()

        def qualify(value) -> None:
            item = value["variants"]["diffdock-upstream-portable"]
            item["source"]["artifact"].update(
                {
                    "identity_state": "verified-full-per-file-manifest",
                    "expected_content_sha256": [],
                    "manifest_sha256": hashlib.sha256(b"full-path-size-sha-manifest").hexdigest(),
                }
            )
            item["source"]["license"].update(
                {
                    "state": "verified-artifact",
                    "artifact_sha256": hashlib.sha256(b"revision-bound-license").hexdigest(),
                }
            )
            item["runtime"].update(
                {
                    "build_state": "built-attested",
                    "image_digest": "sha256:"
                    + hashlib.sha256(b"immutable-built-image").hexdigest(),
                    "device_capability": "portable-qualified",
                }
            )
        self.mutate_variant_contract(target, qualify)
        catalog = load_catalog(target, repo_root=REPO_ROOT)
        variant = catalog.model_variant("diffdock-upstream-portable").to_dict()
        self.assertEqual("candidate-unqualified", variant["promotion"]["state"])
        self.assertEqual("built-attested", variant["runtime"]["build_state"])
        self.assertEqual(
            "verified-full-per-file-manifest",
            variant["source"]["artifact"]["identity_state"],
        )
        self.assertFalse(variant["promotion"]["route_exposed"])
        self.assertEqual((), catalog.routable_variant_ids())
        self.assertEqual((), catalog.routable_model_ids())

    def test_variant_receipt_schemas_close_manifest_supply_and_cohort_semantics(self) -> None:
        variant_schema = json.loads(
            (CATALOG_ROOT / "schema" / "model-variants.schema.json").read_text()
        )
        requirements = variant_schema["$defs"]["variant"]["properties"][
            "receipt_requirements"
        ]["properties"]
        self.assertEqual(
            [
                "artifact-manifest",
                "build-materials",
                "builder",
                "immutable-image",
                "license-artifact",
                "provenance",
                "sbom",
                "scan",
                "signature",
                "source-repository-revision",
            ],
            requirements["supply_claims"]["const"],
        )
        supply = json.loads(
            (
                CATALOG_ROOT
                / "schema"
                / "model-variant-supply-receipt.schema.json"
            ).read_text()
        )
        self.assertTrue(
            {
                "source",
                "artifact",
                "license",
                "runtime",
                "build",
                "attestations",
            }.issubset(supply["required"])
        )
        self.assertEqual(
            ["path", "bytes", "sha256"],
            json.loads(
                (CATALOG_ROOT / "schema" / "artifact-manifest.schema.json").read_text()
            )["properties"]["content"]["properties"]["files"]["items"]["required"],
        )
        qualification = json.loads(
            (
                CATALOG_ROOT
                / "schema"
                / "model-variant-qualification-receipt.schema.json"
            ).read_text()
        )
        measurement = qualification["properties"]["measurement"]["properties"]
        self.assertEqual(10, measurement["warm_attempts_total"]["minimum"])
        self.assertEqual(2, measurement["semantic_responses_per_success"]["const"])
        self.assertEqual(3, measurement["determinism_attempt_ids"]["minItems"])
        self.assertEqual(3, measurement["kernel_dispatch_attempt_ids"]["minItems"])
        self.assertTrue(
            {
                "runtime_tuple_digest",
                "cold_cohort_digest",
                "warm_cohort_digest",
                "quality",
                "preemption_receipt_digest",
                "lifecycle",
                "gateway",
                "vendor_baseline",
            }.issubset(qualification["required"])
        )
        cohort = json.loads(
            (CATALOG_ROOT / "schema" / "model-variant-cohort.schema.json").read_text()
        )
        self.assertTrue(cohort["properties"]["failures_in_denominator"]["const"])
        semantic = json.loads(
            (
                CATALOG_ROOT
                / "schema"
                / "model-variant-semantic-receipt.schema.json"
            ).read_text()
        )
        self.assertEqual(2, semantic["properties"]["requests"]["minItems"])
        self.assertEqual(2, semantic["properties"]["requests"]["maxItems"])

    def test_scale_contracts_bind_exact_controller_target_and_cleanup_ownership(self) -> None:
        catalog = self.load()
        qwen = catalog.scale_contract("qwen3-8b").to_dict()
        self.assertEqual("replica-scale", qwen["activation_mode"])
        self.assertEqual(
            {
                "api_version": "apps/v1",
                "kind": "Deployment",
                "namespace": "fs2-models",
                "name": "qwen3-8b",
                "uid_source": "signed-serving-binding",
            },
            {
                key: qwen["target"][key]
                for key in (
                    "api_version",
                    "kind",
                    "namespace",
                    "name",
                    "uid_source",
                )
            },
        )
        self.assertEqual(0, qwen["policy"]["desired_floor"])
        self.assertEqual(1, qwen["policy"]["desired_max"])
        self.assertEqual(
            "fs2-model-activation-controller",
            qwen["policy"]["replica_scaler_owner"],
        )
        self.assertEqual(
            "forbidden", qwen["controller_boundary"]["gateway_kubernetes_mutation"]
        )
        boundary = qwen["controller_boundary"]
        self.assertEqual(
            {
                "namespace": "fs2-system",
                "deployment_name": "fs2-serve-control-plane-activation",
                "service_account_name": "fs2-model-activation-controller",
                "leader_lease_name": "fs2-serve-activation-controller",
                "leader_role_namespace": "fs2-system",
                "leader_role_name": "fs2-serve-control-plane-activation-leader",
                "target_role_namespace": "fs2-models",
                "target_role_name": "fs2-serve-control-plane-activation-targets",
            },
            boundary["activation_controller"],
        )
        self.assertEqual(
            "postgresql-durable-row",
            boundary["activation_intent_interface"]["transport"],
        )
        self.assertEqual(
            "fs2_activation_intents",
            boundary["activation_intent_interface"]["intent_table"],
        )
        self.assertEqual(
            [
                "fence_operation_id",
                "controller_id",
                "previous_fencing_token",
                "fencing_token",
                "database_now",
                "claim_started_at",
                "claim_lease_expires_at",
                "leader_lease_uid",
                "leader_lease_resource_version",
                "leader_lease_holder_identity",
                "submitter_service_account_uid",
                "claim_owner_service_account_uid",
            ],
            boundary["activation_intent_interface"]["claim_fence_fields"],
        )
        self.assertEqual(
            ["Deployment", "Pod"],
            qwen["policy"]["cleanup"]["expected_resource_kinds"],
        )

        nim = catalog.scale_contract("openfold2").to_dict()
        self.assertEqual("NIMService", nim["target"]["kind"])
        self.assertEqual(
            "apps.nvidia.com/v1alpha1", nim["target"]["api_version"]
        )
        msa = catalog.scale_contract("msa-search-pdb70").to_dict()
        self.assertEqual("replica-scale", msa["activation_mode"])
        self.assertEqual(
            {
                "api_version": "apps.nvidia.com/v1alpha1",
                "kind": "NIMService",
                "name": "msa-search-pdb70",
                "uid_source": "signed-serving-binding",
            },
            {
                key: msa["target"][key]
                for key in ("api_version", "kind", "name", "uid_source")
            },
        )
        segment = catalog.scale_contract("nv-segment-ct").to_dict()
        self.assertEqual("replica-scale", segment["activation_mode"])
        self.assertEqual(
            {
                "api_version": "apps/v1",
                "kind": "Deployment",
                "name": "nv-segment-ct",
                "uid_source": "signed-serving-binding",
            },
            {
                key: segment["target"][key]
                for key in ("api_version", "kind", "name", "uid_source")
            },
        )
        for model_id in catalog.records:
            with self.subTest(model_id=model_id):
                contract = catalog.scale_contract(model_id).to_dict()
                self.assertEqual(0, contract["policy"]["desired_floor"])
                if contract["activation_mode"] == "replica-scale":
                    self.assertEqual(
                        "fs2-model-activation-controller",
                        contract["policy"]["replica_scaler_owner"],
                    )
        self.assertEqual((), catalog.routable_model_ids())

    def test_scale_contract_rejects_gateway_mutation_and_subject_substitution(self) -> None:
        cases = {
            "gateway-rbac": lambda value: value["controller_boundary"].update(
                {"gateway_kubernetes_mutation": "allowed"}
            ),
            "invented-http-transport": lambda value: value["controller_boundary"][
                "activation_intent_interface"
            ].update({"transport": "https-mtls"}),
            "foreign-intent-table": lambda value: value["controller_boundary"][
                "activation_intent_interface"
            ].update({"intent_table": "other_activation_intents"}),
            "weakened-operation-fence": lambda value: value["controller_boundary"][
                "activation_intent_interface"
            ].update({"claim_fence_fields": ["controller_id"]}),
            "foreign-controller-deployment": lambda value: value[
                "controller_boundary"
            ]["activation_controller"].update({"deployment_name": "another-controller"}),
            "foreign-controller-leader-role": lambda value: value[
                "controller_boundary"
            ]["activation_controller"].update({"leader_role_name": "another-role"}),
            "foreign-controller-target-role": lambda value: value[
                "controller_boundary"
            ]["activation_controller"].update({"target_role_name": "another-role"}),
            "target-name": lambda value: value["contracts"]["qwen3-8b"][
                "target"
            ].update({"name": "another-model"}),
            "placement-identity": lambda value: value["contracts"]["qwen3-8b"].update(
                {
                    "resource_placement_identity_sha256": hashlib.sha256(
                        b"substituted-placement"
                    ).hexdigest()
                }
            ),
            "replica-bound": lambda value: value["policy_profiles"][
                "http-deployment-zero-to-one-v1"
            ].update({"desired_max": 2}),
            "scaler-owner": lambda value: value["policy_profiles"][
                "http-deployment-zero-to-one-v1"
            ].update({"replica_scaler_owner": "fs2-model-batch-controller"}),
            "foreign-cleanup": lambda value: value["policy_profiles"][
                "http-deployment-zero-to-one-v1"
            ]["cleanup"].update({"foreign_uid_action": "delete"}),
        }
        for case, mutate in cases.items():
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                index_path = target / "catalog.json"
                index = json.loads(index_path.read_text())
                scale_path = target / index["scale_contracts"]["path"]
                value = json.loads(scale_path.read_text())
                mutate(value)
                scale_path.write_text(json.dumps(value) + "\n")
                index["scale_contracts"]["sha256"] = hashlib.sha256(
                    scale_path.read_bytes()
                ).hexdigest()
                index_path.write_text(json.dumps(index) + "\n")
                with self.assertRaises(CatalogError):
                    load_catalog(target, repo_root=REPO_ROOT)

    def test_reviewed_extra_tested_record_is_extensible(self) -> None:
        _, target = self.copy_catalog()
        source = json.loads((target / "models" / "qwen3-8b.json").read_text())
        source["model"]["id"] = "qwen3-8b-extra"
        source["model"]["display_name"] = "Qwen3-8B extra contract fixture"
        source["cache"]["shared_path"] = "/mnt/fs2-serve-cache/models/qwen3-8b-extra"
        source["cache"]["local_path"] = "/var/lib/fs2-serve/cache/models/qwen3-8b-extra"
        alias_index = source["runtime"]["command"].index("--served-model-name") + 1
        source["runtime"]["command"][alias_index] = "qwen3-8b-extra"
        source["resources"]["gpu"]["placement"] = None
        path = target / "models" / "qwen3-8b-extra.json"
        path.write_text(json.dumps(source) + "\n")
        index_path = target / "catalog.json"
        index = json.loads(index_path.read_text())
        index["model_files"].append(path.name)
        index["model_files"].sort()
        index["tested_model_ids"].append("qwen3-8b-extra")
        index["tested_model_ids"].sort()
        acquisition_path = target / index["artifact_acquisition"]["path"]
        acquisition = json.loads(acquisition_path.read_text())
        acquisition["plans"]["qwen3-8b-extra"] = dict(
            acquisition["plans"]["qwen3-8b"]
        )
        acquisition["plans"]["qwen3-8b-extra"]["destination_prefix"] = (
            "/mnt/fs2-serve-cache/models/qwen3-8b-extra"
        )
        acquisition["plans"]["qwen3-8b-extra"]["required_prerequisites"] = [
            "fs2-models/cache-service-account",
            "fs2-models/runtime-registry-secret",
            "fs2-models/shared-cache-pvc",
        ]
        acquisition["plans"]["qwen3-8b-extra"]["publication"] = (
            "atomic-content-addressed-sfs"
        )
        acquisition["plans"]["qwen3-8b-extra"]["promotion_policy"] = (
            "license-review-and-live-qualification-required"
        )
        acquisition["plans"]["qwen3-8b-extra"].pop("storage_contract")
        acquisition_path.write_text(json.dumps(acquisition) + "\n")
        index["artifact_acquisition"]["sha256"] = hashlib.sha256(
            acquisition_path.read_bytes()
        ).hexdigest()
        compatibility_path = target / index["compatibility_audit"]["path"]
        compatibility = json.loads(compatibility_path.read_text())
        compatibility["records"]["qwen3-8b-extra"] = dict(
            compatibility["records"]["qwen3-8b"]
        )
        compatibility["records"]["qwen3-8b-extra"][
            "execution_identity_sha256"
        ] = execution_identity(source)
        compatibility_path.write_text(json.dumps(compatibility) + "\n")
        index["compatibility_audit"]["sha256"] = hashlib.sha256(
            compatibility_path.read_bytes()
        ).hexdigest()
        semantic_path = target / index["semantic_requests"]["path"]
        semantic = json.loads(semantic_path.read_text())
        semantic["contracts"]["qwen3-8b-extra"] = copy.deepcopy(
            semantic["contracts"]["qwen3-8b"]
        )
        semantic_path.write_text(json.dumps(semantic) + "\n")
        index["semantic_requests"]["sha256"] = hashlib.sha256(
            semantic_path.read_bytes()
        ).hexdigest()
        scale_path = target / index["scale_contracts"]["path"]
        scale = json.loads(scale_path.read_text())
        scale_item = copy.deepcopy(scale["contracts"]["qwen3-8b"])
        model_digest = hashlib.sha256(canonical_bytes(source)).hexdigest()
        executable_digest = execution_identity(source)
        scale_item["model_digest"] = model_digest
        scale_item["execution_identity_sha256"] = executable_digest
        scale_item["resource_placement_identity_sha256"] = resource_placement_identity(
            source
        )
        scale_item["target"]["name"] = "qwen3-8b-extra"
        scale_item["target"]["selector"] = {
            "fs2-serve.nebius.ai/model-id": "qwen3-8b-extra"
        }
        template_subject = {
            "api_version": scale_item["target"]["api_version"],
            "kind": scale_item["target"]["kind"],
            "namespace": scale_item["target"]["namespace"],
            "name": scale_item["target"]["name"],
            "selector": scale_item["target"]["selector"],
            "model_digest": model_digest,
            "execution_identity_sha256": executable_digest,
            "resource_placement_identity_sha256": resource_placement_identity(source),
        }
        scale_item["target"]["template_identity_sha256"] = hashlib.sha256(
            canonical_bytes(template_subject)
        ).hexdigest()
        scale["contracts"]["qwen3-8b-extra"] = scale_item
        scale_path.write_text(json.dumps(scale) + "\n")
        index["scale_contracts"]["sha256"] = hashlib.sha256(
            scale_path.read_bytes()
        ).hexdigest()
        index_path.write_text(json.dumps(index) + "\n")
        catalog = load_catalog(target, repo_root=REPO_ROOT)
        self.assertIn("qwen3-8b-extra", catalog.tested_model_ids)
        self.assertTrue(REQUIRED_TESTED_MODEL_IDS.issubset(catalog.tested_model_ids))

    def test_qwen_revision_is_the_authoritative_campaign_revision(self) -> None:
        catalog = self.load()
        qwen = catalog.model("qwen3-8b").to_dict()
        acquisition = catalog.acquisition_plan("qwen3-8b").to_dict()
        authoritative = "b968826d9c46dd6066d109eabc6255188de91218"
        self.assertEqual(authoritative, qwen["model"]["source"]["revision"])
        self.assertNotIn(authoritative, qwen["runtime"]["command"])
        self.assertEqual("{FS2_MODEL_CONTENT_PATH}", qwen["runtime"]["command"][2])
        alias_index = qwen["runtime"]["command"].index("--served-model-name") + 1
        self.assertEqual("qwen3-8b", qwen["runtime"]["command"][alias_index])
        self.assertNotIn("Qwen/Qwen3-8B", qwen["runtime"]["command"])
        self.assertEqual(
            {"id": "apache-2.0", "state": "verified"},
            {
                key: qwen["model"]["source"]["license"][key]
                for key in ("id", "state")
            },
        )
        self.assertEqual(
            {
                "url": (
                    "https://huggingface.co/Qwen/Qwen3-8B/raw/"
                    f"{authoritative}/LICENSE"
                ),
                "sha256": (
                    "832dd9e00a68dd83b3c3fb9f5588dad7"
                    "dcf337a0db50f7d9483f310cd292e92e"
                ),
            },
            qwen["model"]["source"]["license"]["artifact"],
        )
        self.assertEqual("allowed", qwen["interface"]["policy"]["commercial_use"])
        self.assertEqual(
            "exact-hf-weight-manifest-provider-block-handoff-and-live-runtime-semantic-qualification-required",
            acquisition["promotion_policy"],
        )

        artifact = qwen["cache"]["artifact"]
        self.assertEqual("platform-verified", artifact["state"])
        self.assertEqual(
            "2cf721c69d9e1b66860274de129f0dd486172ef1dad289483ea891dab5b80806",
            artifact["manifest_digest"],
        )
        self.assertEqual(16_397_461_266, artifact["expanded_bytes"])
        self.assertFalse(artifact["staged"])
        self.assertEqual(
            "fs2-serve/exact-hf-weight-per-file-sha256-manifest/v1",
            artifact["qualification_gate"],
        )
        self.assertEqual(
            {
                "identity_sha256": "1e89964f62cbba0c316f76db2e4ea56d2a79fcf5b8ec678bec48d53c457a30cc",
                "identity_scope": "canonical-path-and-size-only",
                "file_count": 15,
                "logical_bytes": 16_397_461_266,
                "source_revision": authoritative,
                "per_file_sha256_complete": False,
            },
            artifact["historical_inventory"],
        )
        self.assertEqual(
            {
                "kind": "snapshot",
                "manifest_digest": "76b8845141df43882a142f9085ff233a0b5bf27b55f19ae385dd9ac88dab6394",
                "expanded_bytes": 27_306_999_047,
                "file_count": 505,
                "hardware": "NVIDIA H100 80GB HBM3",
                "compatibility": "incompatible-with-b300",
                "file_signatures": ["cgroup.img", "core-*.img"],
            },
            qwen["startup"]["experiments"][0]["historical_artifact"],
        )
        self.assertIn(
            "exact-hf-weight-per-file-sha256-manifest",
            acquisition["qualification_priority"]["remaining_gates"],
        )
        self.assertEqual(
            "c653272690ef3247479467a23d991f68c2ed0e7c78f4c90fe7bd2b2de17ba3e7",
            acquisition["source_review"]["metadata_sha256"],
        )
        self.assertEqual(
            "832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e",
            acquisition["source_review"]["license_file_sha256"],
        )
        self.assertEqual(
            {
                "id": "Qwen/Qwen3-8B",
                "sha": authoritative,
                "private": False,
                "gated": False,
                "disabled": False,
                "license": "apache-2.0",
            },
            acquisition["source_review"]["metadata"],
        )
        self.assertEqual(1, acquisition["qualification_priority"]["rank"])
        self.assertEqual(
            "candidate-unroutable", acquisition["qualification_priority"]["state"]
        )
        self.assertEqual(
            "conventional", acquisition["qualification_priority"]["startup_mechanism"]
        )
        self.assertFalse(qwen["support"]["route_exposed"])
        self.assertEqual((), catalog.routable_model_ids())
        self.assertEqual(
            "ce3daebc291f4b35807989959cf24068cdf500a7",
            qwen["provenance"][0]["commit"],
        )

    def test_cosmos_exact_revision_manifest_and_bootstrap_state_are_bound(self) -> None:
        cosmos = self.load().model("cosmos3-nano").to_dict()
        artifact = load_artifact_manifest(
            CATALOG_ROOT.parent.parent
            / "models"
            / "general-media"
            / "evidence"
            / "cosmos3-nano-artifact-manifest.json"
        )

        self.assertEqual("nvidia/Cosmos3-Nano", cosmos["model"]["source"]["repository"])
        self.assertEqual(
            "7a312c868bcce8e40b3eb40861300a9d0ba3fde1",
            cosmos["model"]["source"]["revision"],
        )
        self.assertEqual("openmdw1.1-license", cosmos["model"]["source"]["license"]["id"])
        self.assertEqual(68, len(artifact.files))
        self.assertEqual(34_986_890_561, artifact.expanded_bytes)
        self.assertEqual(
            "dfa7b03382ba78d7f80703652706c3cfa777cefac48634df49345c4302af2c95",
            artifact.content_digest,
        )
        self.assertEqual(
            cosmos["cache"]["artifact"]["manifest_digest"],
            artifact.digest,
        )
        self.assertFalse(cosmos["support"]["route_exposed"])
        self.assertFalse(cosmos["interface"]["mcp"]["invocable"])

    def test_vllm_serving_argv_uses_only_mounted_content_and_canonical_alias(self) -> None:
        catalog = self.load()
        for model_id in ("qwen3-8b", "glm-5-2-fp8", "nv-reason-cxr-3b"):
            with self.subTest(model_id=model_id):
                value = catalog.model(model_id).to_dict()
                command = value["runtime"]["command"]
                self.assertEqual(1, command.count("{FS2_MODEL_CONTENT_PATH}"))
                self.assertNotIn(value["model"]["source"]["repository"], command)
                self.assertNotIn(value["model"]["source"]["revision"], command)
                self.assertNotIn("--revision", command)
                alias_index = command.index("--served-model-name") + 1
                self.assertEqual(model_id, command[alias_index])

        qwen_command = catalog.model("qwen3-8b").to_dict()["runtime"]["command"]
        self.assertEqual(
            ["--served-model-name", "qwen3-8b"],
            qwen_command[3:5],
        )

        mutations = {
            "remote-model": lambda command: command.__setitem__(
                command.index("{FS2_MODEL_CONTENT_PATH}"), "Qwen/Qwen3-8B"
            ),
            "alias-substitution": lambda command: command.__setitem__(
                command.index("--served-model-name") + 1, "attacker-alias"
            ),
            "revision-redownload": lambda command: command.extend(
                ["--revision", "b968826d9c46dd6066d109eabc6255188de91218"]
            ),
        }
        for case, mutate in mutations.items():
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                path = target / "models" / "qwen3-8b.json"
                value = json.loads(path.read_text())
                mutate(value["runtime"]["command"])
                path.write_text(json.dumps(value) + "\n")
                with self.assertRaises(CatalogError):
                    load_catalog(target, repo_root=REPO_ROOT)

    def test_qwen_allocation_is_one_gpu_on_the_exact_eight_b300_burst_placement(self) -> None:
        gpu = self.load().model("qwen3-8b").to_dict()["resources"]["gpu"]
        self.assertEqual((1, "single-gpu"), (gpu["count"], gpu["topology"]))
        self.assertEqual(8, gpu["placement"]["node_gpu_count"])
        self.assertEqual("b300-8x", gpu["placement"]["node_preset"])
        self.assertEqual(
            {
                "capacity.fs2.nebius/gpu-count": "8",
                "capacity.fs2.nebius/pool": "burst",
                "capacity.fs2.nebius/preset": "b300-8x",
                "capacity.fs2.nebius/type": "preemptible",
                "workload.fs2.nebius/gpu": "true",
            },
            gpu["placement"]["node_selector"],
        )
        self.assertEqual(
            ["provider-block-pvc", "sfs-pvc", "local-nvme"],
            [
                item["storage_mode"]
                for item in gpu["placement"]["qualification_sequence"]
            ],
        )
        self.assertEqual(
            [
                "provider-block-pvc-lifecycle",
                "replacement-node-rwx-canary",
                "activation-generation-pvc",
            ],
            [
                item["cohort_binding"]
                for item in gpu["placement"]["qualification_sequence"]
            ],
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
            gpu["placement"]["tolerations"],
        )
        self.assertEqual(
            {
                "state": "gated-unimplemented",
                "contract": "fs2-serve.nebius.ai/local-pv-pvc-lifecycle/v1",
                "storage_class_name": "fs2-local-nvme",
                "volume_binding_mode": "WaitForFirstConsumer",
                "node_affinity": "exact-serving-node-required",
                "preemption_fencing": "pod-pvc-pv-node-uid",
                "lost_node_fencing": "invalidate-and-recreate-next-activation-generation",
                "activation_generation_recreation": True,
                "model_pod_volume_type": "persistentVolumeClaim",
                "host_path_forbidden": True,
            },
            gpu["placement"]["local_pv_pvc"],
        )
        self.assertEqual(
            ["provider-block-pvc-candidate"],
            gpu["placement"]["cache_capabilities"],
        )
        self.assertNotIn(
            "provider-block-pvc-qualified", gpu["placement"]["cache_capabilities"]
        )
        provider = gpu["placement"]["provider_block_pvc"]
        self.assertEqual("compute.csi.nebius.com", provider["storage_class"]["provisioner"])
        self.assertEqual("Retain", provider["storage_class"]["reclaim_policy"])
        self.assertEqual(
            "WaitForFirstConsumer", provider["storage_class"]["volume_binding_mode"]
        )
        self.assertEqual(
            {
                "type": "NETWORK_SSD",
                "csi.storage.k8s.io/fstype": "ext4",
            },
            provider["storage_class"]["parameters"],
        )
        self.assertEqual(64 * 1024**3, provider["claim"]["requested_bytes"])
        self.assertEqual(["ReadWriteOnce"], provider["claim"]["access_modes"])
        self.assertEqual(0, provider["acquisition"]["gpu_count"])
        self.assertTrue(provider["runtime"]["read_only"])
        self.assertNotIn("node-local-pv-pvc-qualified", gpu["placement"]["cache_capabilities"])
        self.assertNotIn("sfs-conventional-qualified", gpu["placement"]["cache_capabilities"])

    def test_qwen_provider_block_priority_is_consistent_across_published_surfaces(self) -> None:
        qwen = self.load().model("qwen3-8b").to_dict()
        placement = qwen["resources"]["gpu"]["placement"]
        acquisition = json.loads(
            (CATALOG_ROOT / "contracts" / "artifact-acquisition.json").read_text()
        )
        golden = json.loads(
            (CATALOG_ROOT / "contracts" / "golden-identities.json").read_text()
        )
        layout = json.loads(
            (CATALOG_ROOT / "contracts" / "live-evidence-layout.json").read_text()
        )
        gateway = json.loads(
            (CATALOG_ROOT / "contracts" / "gateway-consumer.fixture.json").read_text()
        )

        expected_modes = ["provider-block-pvc", "sfs-pvc", "local-nvme"]
        self.assertEqual(
            expected_modes,
            [item["storage_mode"] for item in placement["qualification_sequence"]],
        )
        self.assertEqual(
            expected_modes,
            [
                item["storage_mode"]
                for item in golden["models"]["qwen3-8b"]["gpu_placement"][
                    "qualification_sequence"
                ]
            ],
        )
        self.assertEqual(
            "atomic-content-addressed-provider-block-pvc",
            acquisition["plans"]["qwen3-8b"]["publication"],
        )
        self.assertEqual(
            "/mnt/fs2-provider-block/models/qwen3-8b",
            acquisition["plans"]["qwen3-8b"]["destination_prefix"],
        )
        self.assertEqual(1, acquisition["qualification_priority"]["rank"])
        self.assertEqual("qwen3-8b", acquisition["qualification_priority"]["model_id"])
        self.assertEqual(
            "fs2-serve.nebius.ai/provider-block-pvc-lifecycle-receipt/v4",
            layout["directories"]["provider-block-pvc"],
        )
        self.assertIn(
            "explicit-provider-block-sfs-or-exact-node-local-placement-with-separate-cohorts",
            gateway["required_promotion_gates"],
        )
        self.assertIn(
            "signed-server-observed-provider-block-storageclass-uid-resourceversion-exact-spec",
            gateway["required_promotion_gates"],
        )
        self.assertNotIn(
            "explicit-sfs-or-exact-node-local-placement-with-separate-cohorts",
            gateway["required_promotion_gates"],
        )

        documents = {
            "catalog README": (CATALOG_ROOT / "README.md").read_text(),
        }
        for name, document in documents.items():
            with self.subTest(document=name):
                self.assertIn("provider block", document.lower())
                self.assertNotIn("Qwen must qualify SFS", document)
                self.assertNotIn("Qwen is SFS-first", document)
                self.assertNotIn("qualify SFS conventional first", document)

    def test_qwen_rejects_one_node_nvme_and_qualification_order_substitution(self) -> None:
        mutations = {
            "one-node-placement": lambda placement: placement.update(
                {"node_gpu_count": 1, "node_preset": "b300-1x"}
            ),
            "nvme-first": lambda placement: placement["qualification_sequence"].reverse(),
            "selector-substitution": lambda placement: placement["node_selector"].update(
                {"capacity.fs2.nebius/gpu-count": "1"}
            ),
            "claim-local-pv-implemented": lambda placement: placement["local_pv_pvc"].update(
                {"state": "reviewed-implemented"}
            ),
            "permit-hostpath": lambda placement: placement["local_pv_pvc"].update(
                {"host_path_forbidden": False}
            ),
            "delete-storage-class": lambda placement: placement["provider_block_pvc"][
                "storage_class"
            ].update({"reclaim_policy": "Delete"}),
            "immediate-binding": lambda placement: placement["provider_block_pvc"][
                "storage_class"
            ].update({"volume_binding_mode": "Immediate"}),
            "volume-type-drift": lambda placement: placement["provider_block_pvc"][
                "storage_class"
            ]["parameters"].update({"type": "NETWORK_SSD_NON_REPLICATED"}),
            "filesystem-parameter-drift": lambda placement: placement[
                "provider_block_pvc"
            ]["storage_class"]["parameters"].update(
                {"csi.storage.k8s.io/fstype": "xfs"}
            ),
            "provider-block-demoted": lambda placement: placement[
                "qualification_sequence"
            ].sort(
                key=lambda item: item["storage_mode"] != "sfs-pvc"
            ),
            "drop-placement": lambda placement: placement.clear(),
        }
        for case, mutate in mutations.items():
            with self.subTest(case=case):
                _, target = self.copy_catalog()
                path = target / "models" / "qwen3-8b.json"
                value = json.loads(path.read_text())
                mutate(value["resources"]["gpu"]["placement"])
                path.write_text(json.dumps(value) + "\n")
                with self.assertRaises(CatalogError):
                    load_catalog(target, repo_root=REPO_ROOT)

    def test_qwen_legal_review_and_first_route_priority_fail_closed(self) -> None:
        mutations = (
            (
                "gated",
                lambda document: document["legal_reviews"]["qwen3-8b"]["metadata"].update(
                    {"gated": True}
                ),
                "exact public ungated subject",
            ),
            (
                "metadata-digest",
                lambda document: document["legal_reviews"]["qwen3-8b"].update(
                    {
                        "metadata_sha256": hashlib.sha256(
                            b"substituted-qwen-legal-observation"
                        ).hexdigest()
                    }
                ),
                "metadata digest differs",
            ),
            (
                "mutable-source-url",
                lambda document: document["legal_reviews"]["qwen3-8b"].update(
                    {"source_url": "https://huggingface.co/api/models/Qwen/Qwen3-8B"}
                ),
                "exact-revision official sources",
            ),
            (
                "mutable-license-url",
                lambda document: document["legal_reviews"]["qwen3-8b"].update(
                    {
                        "license_file_url": (
                            "https://huggingface.co/Qwen/Qwen3-8B/raw/main/LICENSE"
                        )
                    }
                ),
                "exact-revision official sources",
            ),
            (
                "license-byte-digest",
                lambda document: document["legal_reviews"]["qwen3-8b"].update(
                    {
                        "license_file_sha256": hashlib.sha256(
                            b"substituted-license-bytes"
                        ).hexdigest()
                    }
                ),
                "license artifact differs from the model source",
            ),
            (
                "priority-promoted",
                lambda document: document["qualification_priority"].update(
                    {"state": "qualified"}
                ),
                "fail-closed Qwen B300 candidate",
            ),
            (
                "license-ambiguity",
                lambda document: document["plans"]["qwen3-8b"].update(
                    {"promotion_policy": "license-review-and-live-qualification-required"}
                ),
                "may not skip exact HF weights",
            ),
        )
        for label, mutate, message in mutations:
            with self.subTest(label=label):
                _, target = self.copy_catalog()
                index_path = target / "catalog.json"
                index = json.loads(index_path.read_text())
                acquisition_path = target / index["artifact_acquisition"]["path"]
                acquisition = json.loads(acquisition_path.read_text())
                mutate(acquisition)
                acquisition_path.write_text(json.dumps(acquisition) + "\n")
                index["artifact_acquisition"]["sha256"] = hashlib.sha256(
                    acquisition_path.read_bytes()
                ).hexdigest()
                index_path.write_text(json.dumps(index) + "\n")
                with self.assertRaisesRegex(CatalogError, message):
                    load_catalog(target, repo_root=REPO_ROOT)

    def test_qwen_h100_snapshot_and_path_size_inventory_cannot_become_weights(self) -> None:
        mutations = {
            "h100-snapshot-as-weights": lambda value: value["cache"]["artifact"].update(
                {
                    "state": "historical-verified",
                    "manifest_digest": "76b8845141df43882a142f9085ff233a0b5bf27b55f19ae385dd9ac88dab6394",
                    "expanded_bytes": 27_306_999_047,
                }
            ),
            "path-size-as-manifest": lambda value: value["cache"]["artifact"].update(
                {
                    "state": "historical-verified",
                    "manifest_digest": "1e89964f62cbba0c316f76db2e4ea56d2a79fcf5b8ec678bec48d53c457a30cc",
                    "expanded_bytes": 16_397_461_266,
                }
            ),
            "drop-weight-gate": lambda value: value["cache"]["artifact"].pop(
                "qualification_gate"
            ),
            "claim-content-hashes": lambda value: value["cache"]["artifact"][
                "historical_inventory"
            ].update({"per_file_sha256_complete": True}),
            "relabel-snapshot": lambda value: value["startup"]["experiments"][0][
                "historical_artifact"
            ].update({"kind": "weights"}),
            "claim-b300-compatible": lambda value: value["startup"]["experiments"][0][
                "historical_artifact"
            ].update({"compatibility": "qualified"}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                _, target = self.copy_catalog()
                path = target / "models" / "qwen3-8b.json"
                value = json.loads(path.read_text())
                mutate(value)
                path.write_text(json.dumps(value) + "\n")
                with self.assertRaises(CatalogError):
                    load_catalog(target, repo_root=REPO_ROOT)

    def test_qwen_expected_tree_identity_is_exact_but_not_promotion_evidence(self) -> None:
        qwen = self.load().model("qwen3-8b").to_dict()
        expected = qwen["cache"]["artifact"]["expected_identity"]
        self.assertEqual("expected-only-unverified", expected["state"])
        self.assertEqual(15, expected["file_count"])
        self.assertEqual(16_397_461_266, expected["expanded_bytes"])
        self.assertEqual(
            "5b0f0f64ddb02ee2deeed4772968b9e2139a922acc9b9bb9c3488d23c678971d",
            expected["content_digest"],
        )
        self.assertEqual(
            "2cf721c69d9e1b66860274de129f0dd486172ef1dad289483ea891dab5b80806",
            expected["manifest_digest"],
        )
        self.assertEqual("platform-verified", qwen["cache"]["artifact"]["state"])
        self.assertEqual(
            "2cf721c69d9e1b66860274de129f0dd486172ef1dad289483ea891dab5b80806",
            qwen["cache"]["artifact"]["manifest_digest"],
        )
        self.assertEqual(
            16_397_461_266, qwen["cache"]["artifact"]["expanded_bytes"]
        )
        self.assertFalse(qwen["cache"]["artifact"]["staged"])
        self.assertFalse(qwen["support"]["route_exposed"])
        for label, mutate in {
            "content": lambda value: value["cache"]["artifact"]["expected_identity"].update(
                {"content_digest": hashlib.sha256(b"substitute").hexdigest()}
            ),
            "manifest": lambda value: value["cache"]["artifact"]["expected_identity"].update(
                {"manifest_digest": hashlib.sha256(b"substitute-manifest").hexdigest()}
            ),
            "file-hash": lambda value: value["cache"]["artifact"]["expected_identity"][
                "files"
            ][0].update({"sha256": hashlib.sha256(b"substitute-file").hexdigest()}),
            "promoted": lambda value: value["cache"]["artifact"]["expected_identity"].update(
                {"state": "verified"}
            ),
        }.items():
            with self.subTest(label=label):
                _, target = self.copy_catalog()
                path = target / "models" / "qwen3-8b.json"
                value = json.loads(path.read_text())
                mutate(value)
                path.write_text(json.dumps(value) + "\n")
                with self.assertRaises(CatalogError):
                    load_catalog(target, repo_root=REPO_ROOT)

        _, target = self.copy_catalog()
        qwen_path = target / "models" / "qwen3-8b.json"
        qwen = json.loads(qwen_path.read_text())
        qwen["model"]["source"]["license"].update(
            {"id": "UNVERIFIED", "state": "unverified"}
        )
        qwen_path.write_text(json.dumps(qwen) + "\n")
        with self.assertRaisesRegex(CatalogError, "exact verified model source"):
            load_catalog(target, repo_root=REPO_ROOT)

        _, target = self.copy_catalog()
        qwen_path = target / "models" / "qwen3-8b.json"
        qwen = json.loads(qwen_path.read_text())
        qwen["model"]["source"]["license"]["artifact"]["sha256"] = hashlib.sha256(
            b"substituted-model-license-artifact"
        ).hexdigest()
        qwen_path.write_text(json.dumps(qwen) + "\n")
        with self.assertRaisesRegex(CatalogError, "license artifact differs"):
            load_catalog(target, repo_root=REPO_ROOT)

        _, target = self.copy_catalog()
        qwen_path = target / "models" / "qwen3-8b.json"
        qwen = json.loads(qwen_path.read_text())
        qwen["model"]["source"]["license"]["artifact"]["url"] = (
            "https://huggingface.co/Qwen/Qwen3-8B/raw/main/LICENSE"
        )
        qwen_path.write_text(json.dumps(qwen) + "\n")
        with self.assertRaisesRegex(CatalogError, "exact repository revision"):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_cxr_request_contract_binds_payloads_and_licensed_image_assets(self) -> None:
        contract = self.load().semantic_request_contract("nv-reason-cxr-3b")
        self.assertEqual(("normal-pa", "left-pneumothorax"), contract.request_ids)
        self.assertEqual("qualified", contract.state)
        self.assertEqual(2, len(contract.to_dict()["assets"]))

        for mutation, message in (
            ("payload", "packaged fixture payloads"),
            ("licensed-image", "licensed image fixture"),
        ):
            with self.subTest(mutation=mutation):
                _, target = self.copy_catalog()
                index_path = target / "catalog.json"
                index = json.loads(index_path.read_text())
                contract_path = target / index["semantic_requests"]["path"]
                document = json.loads(contract_path.read_text())
                cxr = document["contracts"]["nv-reason-cxr-3b"]
                if mutation == "payload":
                    cxr["requests"][0]["payload_sha256"] = hashlib.sha256(
                        b"substituted-cxr-request"
                    ).hexdigest()
                else:
                    cxr["assets"][0]["content_sha256"] = hashlib.sha256(
                        b"substituted-cxr-image"
                    ).hexdigest()
                contract_path.write_text(json.dumps(document) + "\n")
                index["semantic_requests"]["sha256"] = hashlib.sha256(
                    contract_path.read_bytes()
                ).hexdigest()
                index_path.write_text(json.dumps(index) + "\n")
                with self.assertRaisesRegex(CatalogError, message):
                    load_catalog(target, repo_root=REPO_ROOT)

    def test_exact_sm90_federation_inventory_is_fail_closed_and_never_aliased(self) -> None:
        catalog = self.load()
        expected = {
            "molmim": (
                "sha256:7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa",
                "federated-kserve-nim",
                "gated",
            ),
            "evo2-40b": (
                "sha256:561886bab1d2d0da836ebf5bec403f9de2baf6e92deb7eedf1b316aa994b5dd2",
                "federated-serverless",
                "credential-compromised",
            ),
            "diffdock": (
                "sha256:300696eb8331d78face40f84d835cc1e278c7d3c391c5aabbbee5884366da480",
                "historical-h100-bridge",
                "disabled",
            ),
            "rfdiffusion": (
                "sha256:15e40e466d8ebe9a53f1feea599373720428c9de65da750bf4271c96ec35ceb4",
                "historical-h100-bridge",
                "disabled",
            ),
            "proteinmpnn": (
                "sha256:b55a0aa6733e267e6e6fe06434e98aea61eff14bc5545127555607fef6f38aa5",
                "historical-h100-bridge",
                "disabled",
            ),
        }
        for model_id, (digest, backend_class, route_state) in expected.items():
            with self.subTest(model_id=model_id):
                backend = catalog.federated_backend(model_id)
                self.assertIsNotNone(backend)
                value = backend.to_dict()
                self.assertEqual(digest, value["runtime_image_digest"])
                self.assertEqual(backend_class, value["backend_class"])
                self.assertEqual(route_state, value["route_state"])
                self.assertIsNone(value["endpoint_identity_sha256"])
                self.assertIsNone(value["trust_bundle_sha256"])
                self.assertFalse(catalog.model(model_id).route_exposed)
        self.assertEqual("us-central1", catalog.federated_backend("molmim").to_dict()["region"])
        self.assertEqual(
            "best-current-exact-upstream",
            catalog.federated_backend("molmim").to_dict()["preference"],
        )
        self.assertEqual(
            "credential-exposed-by-provider-diagnostics",
            catalog.federated_backend("evo2-40b").to_dict()["trust_state"],
        )

    def test_federated_inventory_rejects_professional_service_digest_alias(self) -> None:
        _, target = self.copy_catalog()
        contract_path = target / "contracts" / "federated-backends.json"
        contract = json.loads(contract_path.read_text())
        contract["records"]["diffdock"]["runtime_image_digest"] = (
            "sha256:" + hashlib.sha256(b"newer-professional-diffdock-service").hexdigest()
        )
        contract_path.write_text(json.dumps(contract) + "\n")
        index_path = target / "catalog.json"
        index = json.loads(index_path.read_text())
        index["federated_backends"]["sha256"] = hashlib.sha256(
            contract_path.read_bytes()
        ).hexdigest()
        index_path.write_text(json.dumps(index) + "\n")
        with self.assertRaisesRegex(CatalogError, "aliases a different runtime image digest"):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_cache_ownership_matches_the_binding_architecture(self) -> None:
        catalog = self.load()
        for model_id, record in catalog.records.items():
            with self.subTest(model_id=model_id):
                value = record.to_dict()
                if value["support"]["state"] == "blocked":
                    expected = "none"
                elif value["runtime"]["kind"] == "nim":
                    expected = "nim-operator-nimcache"
                else:
                    expected = "fs2-serve-localizer"
                self.assertEqual(expected, value["cache"]["owner"])

    def test_nim_routes_require_precreated_secret_observation_and_v2_canary(self) -> None:
        catalog = self.load()
        ngc_secret_ids = (
            "fs2-models/ngc-pull-secret",
            "fs2-models/ngc-runtime-secret",
        )
        for requirement_id in ngc_secret_ids:
            with self.subTest(requirement_id=requirement_id):
                prerequisite = catalog.prerequisite(requirement_id).to_dict()
                self.assertEqual(
                    "platform-security-bootstrap", prerequisite["owner"]
                )
                self.assertEqual(
                    "metadata-only-values-suppressed",
                    prerequisite["value_policy"],
                )
        contract = json.loads(
            (CATALOG_ROOT / "contracts" / "runtime-prerequisites.json").read_text()
        )["ngc_credential_contract"]
        default = contract["default_secret_delivery"]
        self.assertEqual(
            "securely-pre-created-existing-kubernetes-secrets",
            default["mode"],
        )
        self.assertEqual(
            ["fs2-ngc-pull", "fs2-ngc-runtime"],
            [item["secret_name"] for item in default["outputs"]],
        )
        self.assertEqual(
            "server-observed-uid-resourceVersion-type-key-set",
            default["required_observation"],
        )
        optional = contract["optional_secret_backends"][0]
        self.assertEqual("ineligible", optional["foundation_crd_state"])
        self.assertEqual("disabled", optional["rendering"])
        self.assertIn("eligible-provider-build-receipt", optional["status"])
        nim_model_ids = {
            model_id
            for model_id, record in catalog.records.items()
            if record.to_dict()["runtime"]["kind"] == "nim"
        }
        self.assertEqual(
            {
                "boltz2",
                "diffdock",
                "evo2-40b",
                "genmol",
                "molmim",
                "msa-search-pdb70",
                "openfold2",
                "openfold3",
                "proteinmpnn",
                "rfdiffusion",
            },
            nim_model_ids,
        )
        expected_qualified_nim_model_ids = {
            "msa-search-pdb70",
            "openfold2",
            "openfold3",
        }
        actual_qualified_nim_model_ids = set()
        for model_id in sorted(nim_model_ids):
            with self.subTest(model_id=model_id):
                record = catalog.model(model_id).to_dict()
                plan = catalog.acquisition_plan(model_id).to_dict()
                self.assertEqual("ngc-target-node-nimcache", plan["method"])
                self.assertEqual(
                    "fs2-serve.nebius.ai/target-node-pull-canary/v2",
                    plan["pull_canary"]["receipt_schema"],
                )
                self.assertTrue(
                    set(ngc_secret_ids).issubset(plan["required_prerequisites"])
                )
                self.assertFalse(record["support"]["route_exposed"])
                self.assertFalse(record["interface"]["mcp"]["invocable"])
                if record["support"]["state"] == "qualified":
                    actual_qualified_nim_model_ids.add(model_id)
                else:
                    self.assertNotIn(model_id, expected_qualified_nim_model_ids)
        self.assertEqual(
            expected_qualified_nim_model_ids, actual_qualified_nim_model_ids
        )
        self.assertEqual((), catalog.routable_model_ids())

    def test_optional_eso_backend_cannot_be_selected_from_ineligible_foundation(self) -> None:
        _, target = self.copy_catalog()
        contract_path = target / "contracts" / "runtime-prerequisites.json"
        contract = json.loads(contract_path.read_text())
        optional = contract["ngc_credential_contract"]["optional_secret_backends"][0]
        optional["foundation_crd_state"] = "eligible"
        optional["status"] = "eligible"
        optional["rendering"] = "enabled"
        optional["eligibility_receipt_sha256"] = hashlib.sha256(
            b"caller-invented-eligibility"
        ).hexdigest()
        contract_path.write_text(json.dumps(contract) + "\n")
        index_path = target / "catalog.json"
        index = json.loads(index_path.read_text())
        index["runtime_prerequisites"]["sha256"] = hashlib.sha256(
            contract_path.read_bytes()
        ).hexdigest()
        index_path.write_text(json.dumps(index) + "\n")
        with self.assertRaisesRegex(
            CatalogError, "NGC credential contract permits legacy or incomplete promotion"
        ):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_every_tested_lane_defaults_conventional_with_audited_b300_state(self) -> None:
        catalog = self.load()
        expected_states = {
            "historical-supported": {
                "boltz2",
                "genmol",
            },
            "incompatible-sm103": {
                "diffdock",
                "evo2-40b",
                "molmim",
                "proteinmpnn",
                "rfdiffusion",
            },
            "qualified": {
                "cosmos3-nano",
                "glm-5-2-fp8",
                "msa-search-pdb70",
                "nv-reason-cxr-3b",
                "nv-segment-ct",
                "openfold2",
                "openfold3",
                "qwen3-8b",
                "sdxl",
            },
        }
        actual_states = {state: set() for state in expected_states}
        for model_id in catalog.tested_model_ids:
            with self.subTest(model_id=model_id):
                value = catalog.model(model_id).to_dict()
                self.assertEqual("conventional", value["startup"]["default"])
                self.assertEqual("conventional", value["startup"]["fallback"])
                self.assertEqual(["conventional"], value["startup"]["enabled_mechanisms"])
                self.assertEqual("unproven-disabled", value["startup"]["multi_gpu_criu"])
                state = value["resources"]["gpu"]["b300_state"]
                self.assertIn(state, actual_states)
                actual_states[state].add(model_id)
                self.assertFalse(value["support"]["route_exposed"])
                self.assertFalse(value["cache"]["artifact"]["staged"])
        self.assertEqual(expected_states, actual_states)

    def test_snapshot_candidates_are_only_exact_tuple_single_gpu(self) -> None:
        catalog = self.load()
        gated = []
        for model_id, record in catalog.records.items():
            value = record.to_dict()
            for experiment in value["startup"]["experiments"]:
                if experiment["mechanism"] == "snapshot" and experiment["state"] == "gated":
                    gated.append(model_id)
                    self.assertEqual(1, value["resources"]["gpu"]["count"])
                    self.assertEqual(
                        "fs2-serve/exact-b300-single-gpu-runtime-tuple/v1",
                        experiment["gate"],
                    )
        self.assertEqual(["openfold2", "qwen3-8b"], sorted(gated))

    def test_glm_ram_and_resident_boundary_are_explicit(self) -> None:
        value = self.load().model("glm-5-2-fp8").to_dict()
        self.assertEqual(893_000_000_000, value["resources"]["host_ram_min_bytes"])
        self.assertEqual(8, value["resources"]["gpu"]["count"])
        self.assertEqual("single-node-multi-gpu", value["resources"]["gpu"]["topology"])
        self.assertNotIn("sleep-wake", value["startup"]["enabled_mechanisms"])
        self.assertEqual("sleep-wake", value["startup"]["experiments"][0]["mechanism"])

    def test_medical_identity_and_nonclinical_boundaries_do_not_substitute(self) -> None:
        catalog = self.load()
        cxr = catalog.model("nv-reason-cxr-3b").to_dict()
        segment = catalog.model("nv-segment-ct").to_dict()
        self.assertTrue(cxr["support"]["non_clinical"])
        self.assertEqual("qualified", cxr["support"]["state"])
        self.assertTrue(segment["support"]["non_clinical"])
        self.assertEqual("qualified", segment["support"]["state"])
        self.assertEqual(["conventional"], cxr["startup"]["enabled_mechanisms"])
        self.assertEqual(["conventional"], segment["startup"]["enabled_mechanisms"])
        for record in (cxr, segment):
            self.assertFalse(record["support"]["route_exposed"])
            self.assertFalse(record["interface"]["mcp"]["invocable"])
        self.assertNotEqual(cxr["model"]["source"]["repository"], segment["model"]["source"]["repository"])

    def test_record_values_are_returned_as_defensive_copies(self) -> None:
        record = self.load().model("qwen3-8b")
        value = record.to_dict()
        value["support"]["route_exposed"] = True
        self.assertFalse(record.to_dict()["support"]["route_exposed"])

    def test_unqualified_accelerated_route_fails_closed(self) -> None:
        _, target = self.copy_catalog()
        path = target / "models" / "qwen3-8b.json"
        value = json.loads(path.read_text())
        value["startup"]["enabled_mechanisms"].append("snapshot")
        path.write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(CatalogError, "qualified experiments"):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_snapshot_gate_cannot_be_relaxed(self) -> None:
        _, target = self.copy_catalog()
        path = target / "models" / "openfold2.json"
        value = json.loads(path.read_text())
        value["startup"]["experiments"][0]["gate"] = "gpu-name-only"
        path.write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(CatalogError, "exact single-GPU B300 tuple"):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_repository_validator_digest_is_enforced(self) -> None:
        _, target = self.copy_catalog()
        path = target / "models" / "proteinmpnn.json"
        value = json.loads(path.read_text())
        value["semantic_validator"]["source_sha256"] = "0" * 64
        path.write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(
            CatalogError,
            "placeholder rather than a content digest|semantic validator digest mismatch",
        ):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_packaged_provenance_lock_digest_is_enforced(self) -> None:
        _, target = self.copy_catalog()
        path = target / "contracts" / "provenance-lock.json"
        value = json.loads(path.read_text())
        value["entries"][0]["content_sha256"] = "0123456789abcdef" * 4
        path.write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(CatalogError, "packaged provenance lock digest mismatch"):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_external_provenance_must_match_an_immutable_locked_subject(self) -> None:
        _, target = self.copy_catalog()
        path = target / "models" / "cosmos3-nano.json"
        value = json.loads(path.read_text())
        substituted_revision = "0" * 40
        value["provenance"][0] = {
            "url": (
                "https://huggingface.co/nvidia/Cosmos3-Nano/raw/"
                f"{substituted_revision}/README.md"
            ),
            "revision": substituted_revision,
            "classification": "reviewed-input",
        }
        path.write_text(json.dumps(value) + "\n")

        with self.assertRaisesRegex(
            CatalogError,
            "external provenance is absent from the packaged provenance lock",
        ):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_a_second_cache_controller_is_rejected(self) -> None:
        _, target = self.copy_catalog()
        path = target / "models" / "openfold2.json"
        value = json.loads(path.read_text())
        value["cache"]["owner"] = "fs2-serve-localizer"
        path.write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(CatalogError, "NIMCache and the fs2 localizer"):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_index_cannot_silently_omit_a_model_file(self) -> None:
        _, target = self.copy_catalog()
        path = target / "catalog.json"
        value = json.loads(path.read_text())
        value["model_files"].remove("boltz2.json")
        path.write_text(json.dumps(value) + "\n")
        with self.assertRaisesRegex(CatalogError, "index and model directory differ"):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_duplicate_json_key_is_rejected(self) -> None:
        _, target = self.copy_catalog()
        path = target / "models" / "qwen3-8b.json"
        raw = path.read_text()
        path.write_text(raw.replace('"schema":', '"schema":"duplicate", "schema":', 1))
        with self.assertRaisesRegex(CatalogError, "duplicate JSON key"):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_symlinked_model_file_is_rejected(self) -> None:
        _, target = self.copy_catalog()
        path = target / "models" / "qwen3-8b.json"
        replacement = target / "qwen-copy.json"
        shutil.copy2(path, replacement)
        path.unlink()
        path.symlink_to(replacement)
        with self.assertRaisesRegex(CatalogError, "regular non-symlink"):
            load_catalog(target, repo_root=REPO_ROOT)

    def test_schema_is_versioned_valid_json(self) -> None:
        value = json.loads((CATALOG_ROOT / "schema" / "model.schema.json").read_text())
        self.assertEqual("https://json-schema.org/draft/2020-12/schema", value["$schema"])
        self.assertEqual("https://fs2-serve.nebius.ai/schema/model/v1", value["$id"])
        self.assertEqual(
            SUPPORTED_MODEL_FAMILIES,
            frozenset(value["properties"]["model"]["properties"]["family"]["enum"]),
        )
        variant_schema = json.loads(
            (CATALOG_ROOT / "schema" / "model-variants.schema.json").read_text()
        )
        self.assertEqual(
            "https://fs2-serve.nebius.ai/schema/model-variants/v4",
            variant_schema["$id"],
        )


if __name__ == "__main__":
    unittest.main()
