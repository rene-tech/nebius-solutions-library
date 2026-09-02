from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ONBOARDING_ROOT = ROOT / "model-onboarding"
SCRIPT = ONBOARDING_ROOT / "compile_model.py"
EXAMPLE = ONBOARDING_ROOT / "examples/vllm-huggingface.json"
BATCH_EXAMPLE = ONBOARDING_ROOT / "examples/scientific-batch-git.json"
HYBRID_EXAMPLE = ONBOARDING_ROOT / "examples/hybrid-huggingface.json"

SPEC = importlib.util.spec_from_file_location("fs2_model_onboarding", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
COMPILER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = COMPILER
SPEC.loader.exec_module(COMPILER)


class ModelOnboardingCompilerTests(unittest.TestCase):
    def declaration(self) -> dict:
        return json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def compile(self, declaration: dict | None = None):
        value = self.declaration() if declaration is None else declaration
        return COMPILER.compile_artifacts(value, ROOT)

    def fixture(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_example_compiles_deterministically(self) -> None:
        declaration = COMPILER.load_declaration(EXAMPLE)
        first = COMPILER.compile_artifacts(declaration, ROOT)
        second = COMPILER.compile_artifacts(declaration, ROOT)

        self.assertEqual(
            [(item.path, item.target, item.payload, item.sha256) for item in first],
            [(item.path, item.target, item.payload, item.sha256) for item in second],
        )
        self.assertEqual(
            [item.path for item in first],
            sorted(item.path for item in first),
        )
        bundle = json.loads(
            next(
                item.payload for item in first if item.path == "onboarding-bundle.json"
            )
        )
        self.assertFalse(bundle["promotion_ready"])
        self.assertEqual(bundle["model_id"], "example-7b")
        self.assertEqual(len(bundle["artifacts"]), 5)
        self.assertGreaterEqual(len(bundle["remaining_promotion_work"]), 6)

    def test_scientific_batch_is_candidate_contract_not_a_runtime(self) -> None:
        declaration = COMPILER.load_declaration(BATCH_EXAMPLE)
        artifacts = COMPILER.compile_artifacts(declaration, ROOT)
        self.assertEqual(
            [item.path for item in artifacts],
            ["onboarding-bundle.json", "projections/scientific-workload-profile.json"],
        )
        projection = json.loads(artifacts[1].payload)
        profile = projection["profile"]
        self.assertEqual(profile["state"], "candidate-unqualified")
        self.assertFalse(profile["route_exposed"])
        self.assertFalse(profile["interface"]["mcp"]["invocable"])
        self.assertIsNone(profile["execution_identity"]["artifact_manifest_digest"])
        self.assertNotIn("command", profile)
        self.assertEqual(
            projection["merge_target"],
            "catalog/runtime/contracts/scientific-workload-profiles.json",
        )

    def test_hybrid_emits_http_and_separate_batch_candidate(self) -> None:
        declaration = COMPILER.load_declaration(HYBRID_EXAMPLE)
        artifacts = COMPILER.compile_artifacts(declaration, ROOT)
        paths = {item.path for item in artifacts}
        self.assertEqual(len(artifacts), 7)
        self.assertIn("models/generated/example-hybrid-7b.yaml", paths)
        self.assertIn("projections/live-service-route.json", paths)
        self.assertIn("projections/scientific-workload-profile.json", paths)

    def test_scientific_protein_hybrid_emits_both_contracts(self) -> None:
        declaration = self.fixture(HYBRID_EXAMPLE)
        declaration["model"]["family"] = "scientific-protein"
        artifacts = COMPILER.compile_artifacts(declaration, ROOT)
        catalog = json.loads(
            next(
                item.payload
                for item in artifacts
                if item.path == "catalog/runtime/models/example-hybrid-7b.json"
            )
        )
        self.assertEqual("scientific-protein", catalog["model"]["family"])

    def test_hybrid_interfaces_require_distinct_mcp_tools(self) -> None:
        declaration = self.fixture(HYBRID_EXAMPLE)
        declaration["batch"]["mcp"]["tool_name"] = declaration["serving"]["mcp"][
            "tool_name"
        ]
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "require distinct MCP tool names"
        ):
            COMPILER._custom_validate(declaration)

    def test_batch_dag_must_be_topological_and_bounded(self) -> None:
        declaration = self.fixture(BATCH_EXAMPLE)
        declaration["batch"]["stages"][0]["needs"] = ["score"]
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "unknown or later stages: score"
        ):
            COMPILER._custom_validate(declaration)

        declaration = self.fixture(BATCH_EXAMPLE)
        declaration["batch"]["stages"][0]["min_parallelism"] = 65
        declaration["batch"]["stages"][0]["max_parallelism"] = 64
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "min_parallelism exceeds max_parallelism"
        ):
            COMPILER._custom_validate(declaration)

    def test_academic_batch_can_declare_but_not_materialize_entitlement(self) -> None:
        declaration = self.fixture(BATCH_EXAMPLE)
        entitlement = declaration["model"]["source"]["entitlement"]
        entitlement.update(
            {
                "required": True,
                "state": "unverified",
                "credential_contract": "academic-pyrosetta/v1",
            }
        )
        declaration["batch"]["access_profile"] = "academic"
        declaration["batch"]["access_state"] = "unverified"
        COMPILER._custom_validate(declaration)

        http = self.declaration()
        http_entitlement = http["model"]["source"]["entitlement"]
        http_entitlement.update(
            {
                "required": True,
                "state": "unverified",
                "credential_contract": "gated-model/v1",
            }
        )
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "does not materialize gated-source"
        ):
            COMPILER._custom_validate(http)

    def test_generate_then_check_detects_content_and_set_drift(self) -> None:
        artifacts = self.compile()
        with tempfile.TemporaryDirectory(prefix="fs2-onboarding-test-") as temporary:
            output = Path(temporary) / "output"
            COMPILER.write_artifacts(output, artifacts)
            self.assertEqual(COMPILER.check_artifacts(output, artifacts), ())

            profile = output / "projections/model-profile.json"
            profile.write_text("{}\n", encoding="utf-8")
            (output / "stale.json").write_text("{}\n", encoding="utf-8")
            drift = COMPILER.check_artifacts(output, artifacts)
            self.assertIn("different: projections/model-profile.json", drift)
            self.assertIn("unexpected: stale.json", drift)

    def test_generate_refuses_unknown_files_in_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fs2-onboarding-test-") as temporary:
            output = Path(temporary)
            (output / "operator-notes.txt").write_text("keep me", encoding="utf-8")
            with self.assertRaisesRegex(
                COMPILER.OnboardingError, "files outside this bundle"
            ):
                COMPILER.write_artifacts(output, self.compile())
            self.assertEqual(
                (output / "operator-notes.txt").read_text(encoding="utf-8"),
                "keep me",
            )

    def test_dry_run_prints_plan_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fs2-onboarding-dry-run-") as temporary:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "dry-run", str(EXAMPLE)],
                cwd=temporary,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            value = json.loads(result.stdout)
            self.assertFalse(value["writes"])
            self.assertEqual(value["model_id"], "example-7b")
            self.assertEqual(len(value["artifacts"]), 6)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_generated_manifest_has_expected_workload_contract(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not available")
        manifest = next(
            item.payload
            for item in self.compile()
            if item.path == "models/generated/example-7b.yaml"
        )
        documents = list(yaml.safe_load_all(manifest.decode("utf-8")))
        self.assertEqual(
            [document["kind"] for document in documents],
            [
                "ServiceAccount",
                "PersistentVolumeClaim",
                "Deployment",
                "Service",
                "NetworkPolicy",
            ],
        )
        deployment = documents[2]
        self.assertEqual(deployment["spec"]["replicas"], 0)
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(
            pod["containers"][0]["resources"]["limits"]["nvidia.com/gpu"], "1"
        )
        self.assertEqual(
            pod["nodeSelector"]["accelerator.fs2.nebius/class"],
            "nvidia-h100-sxm5-80gb",
        )
        self.assertIn("/model-cache/content", pod["containers"][0]["args"])

    def test_sensitive_environment_and_noncanonical_vllm_alias_fail_closed(
        self,
    ) -> None:
        sensitive = self.declaration()
        sensitive["runtime"]["environment"]["HF_TOKEN"] = "do-not-store"
        with self.assertRaisesRegex(COMPILER.OnboardingError, "credential-like"):
            COMPILER._custom_validate(sensitive)

        wrong_alias = self.declaration()
        alias_index = wrong_alias["runtime"]["args"].index("--served-model-name") + 1
        wrong_alias["runtime"]["args"][alias_index] = "another-model"
        with self.assertRaisesRegex(COMPILER.OnboardingError, "must equal model.id"):
            COMPILER._custom_validate(wrong_alias)

    def test_source_and_review_revisions_must_match(self) -> None:
        declaration = self.declaration()
        declaration["model"]["source"]["review"]["revision"] = "2" * 40
        declaration["model"]["source"]["review"]["url"] = (
            "https://huggingface.co/example-org/example-7b/tree/" + "2" * 40
        )
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "source.revision must equal"
        ):
            COMPILER._custom_validate(declaration)

    def test_huggingface_repository_must_be_a_repo_id_not_a_url(self) -> None:
        declaration = self.declaration()
        declaration["model"]["source"]["repository"] = (
            "https://huggingface.co/example-org/example-7b"
        )
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "canonical namespace/repository ID"
        ):
            COMPILER._custom_validate(declaration)

    def test_review_url_is_bound_to_exact_repository_and_revision(self) -> None:
        declaration = self.declaration()
        declaration["model"]["source"]["repository"] = "different-org/example-7b"
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "canonical exact-revision source tree URL"
        ):
            COMPILER._custom_validate(declaration)

    def test_vllm_port_must_match_service_port(self) -> None:
        declaration = self.declaration()
        port_index = declaration["runtime"]["args"].index("--port") + 1
        declaration["runtime"]["args"][port_index] = "8001"
        with self.assertRaisesRegex(COMPILER.OnboardingError, "--port must equal"):
            COMPILER._custom_validate(declaration)

    def test_vllm_host_is_exactly_one_wildcard_binding(self) -> None:
        loopback = self.declaration()
        host_index = loopback["runtime"]["args"].index("--host") + 1
        loopback["runtime"]["args"][host_index] = "127.0.0.1"
        with self.assertRaisesRegex(COMPILER.OnboardingError, "--host must equal"):
            COMPILER._custom_validate(loopback)

        omitted = self.declaration()
        host_flag = omitted["runtime"]["args"].index("--host")
        del omitted["runtime"]["args"][host_flag : host_flag + 2]
        with self.assertRaisesRegex(COMPILER.OnboardingError, "--host exactly once"):
            COMPILER._custom_validate(omitted)

    def test_vllm_served_model_name_is_exactly_one_alias(self) -> None:
        duplicate = self.declaration()
        duplicate["runtime"]["args"].extend(["--served-model-name", "example-7b"])
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "--served-model-name exactly once"
        ):
            COMPILER._custom_validate(duplicate)

        extra_alias = self.declaration()
        alias_index = extra_alias["runtime"]["args"].index("--served-model-name") + 2
        extra_alias["runtime"]["args"].insert(alias_index, "example-7b-alias")
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "--served-model-name must have exactly one value"
        ):
            COMPILER._custom_validate(extra_alias)

        inline = self.declaration()
        flag_index = inline["runtime"]["args"].index("--served-model-name")
        inline["runtime"]["args"][flag_index : flag_index + 2] = [
            "--served-model-name=example-7b"
        ]
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "must use a separate flag and value"
        ):
            COMPILER._custom_validate(inline)

    def test_vllm_tensor_parallel_size_must_match_gpu_count(self) -> None:
        declaration = self.declaration()
        tensor_parallel_index = (
            declaration["runtime"]["args"].index("--tensor-parallel-size") + 1
        )
        declaration["runtime"]["args"][tensor_parallel_index] = "2"
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "--tensor-parallel-size must equal"
        ):
            COMPILER._custom_validate(declaration)

    def test_resource_values_are_derived_from_one_typed_source(self) -> None:
        artifacts = self.compile()
        model = json.loads(
            next(
                item.payload
                for item in artifacts
                if item.path == "catalog/runtime/models/example-7b.json"
            )
        )
        profile = json.loads(
            next(
                item.payload
                for item in artifacts
                if item.path == "projections/model-profile.json"
            )
        )
        self.assertEqual(model["resources"]["cpu_millis"], 8000)
        self.assertEqual(model["resources"]["memory_bytes"], 64 * (1 << 30))
        self.assertEqual(
            profile["model_autoscaling_target"]["ephemeral_storage_request_gib"],
            4,
        )

    def test_each_resource_limit_must_cover_its_request(self) -> None:
        cases = {
            "cpu": "7",
            "memory": "32Gi",
            "ephemeral_storage": "2Gi",
        }
        for resource_name, limit in cases.items():
            with self.subTest(resource=resource_name):
                declaration = self.declaration()
                declaration["resources"]["limits"][resource_name] = limit
                with self.assertRaisesRegex(
                    COMPILER.OnboardingError,
                    f"limits.{resource_name} must be greater than or equal",
                ):
                    COMPILER._custom_validate(declaration)

    def test_image_and_placement_labels_use_canonical_grammars(self) -> None:
        bad_image = self.declaration()
        bad_image["runtime"]["image"] = (
            "https://registry.example.invalid/image@sha256:" + "a" * 64
        )
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "canonical lowercase OCI"
        ):
            COMPILER._custom_validate(bad_image)

        bad_registry = self.declaration()
        bad_registry["runtime"]["image"] = (
            "registry..example.invalid/image@sha256:" + "a" * 64
        )
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "canonical lowercase OCI"
        ):
            COMPILER._custom_validate(bad_registry)

        bad_key = self.declaration()
        bad_key["placement"]["required_node_labels"]["bad prefix/key"] = "value"
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "label key is not canonical"
        ):
            COMPILER._custom_validate(bad_key)

        bad_value = self.declaration()
        bad_value["placement"]["required_node_labels"][
            "accelerator.fs2.nebius/class"
        ] = "bad/value"
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "label value is not canonical"
        ):
            COMPILER._custom_validate(bad_value)

    def test_compile_artifacts_cannot_bypass_declaration_validation(self) -> None:
        declaration = self.declaration()
        declaration["runtime"]["image"] = "not-an-image"
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "model declaration validation"
        ):
            COMPILER.compile_artifacts(declaration, ROOT)

    def test_manifest_path_must_be_canonical(self) -> None:
        declaration = self.declaration()
        declaration["profile"] = {"manifest_path": "models/../escape.yaml"}
        with self.assertRaisesRegex(
            COMPILER.OnboardingError, "canonical repository-relative"
        ):
            COMPILER._custom_validate(declaration)

    def test_existing_model_manifest_and_mcp_tool_collisions_fail(self) -> None:
        existing_model = self.declaration()
        existing_model["model"]["id"] = "qwen3-8b"
        alias_index = existing_model["runtime"]["args"].index("--served-model-name") + 1
        existing_model["runtime"]["args"][alias_index] = "qwen3-8b"
        COMPILER._custom_validate(existing_model)
        with self.assertRaisesRegex(COMPILER.OnboardingError, "catalog model qwen3-8b"):
            COMPILER._validate_collisions(existing_model, ROOT)

        manifest_and_tool = self.declaration()
        manifest_and_tool["profile"] = {
            "manifest_path": "models/general-media/k8s/cosmos3-nano.yaml"
        }
        manifest_and_tool["serving"]["mcp"]["tool_name"] = "qwen3_8b_chat"
        with self.assertRaisesRegex(COMPILER.OnboardingError, "MCP tool qwen3_8b_chat"):
            COMPILER._validate_collisions(manifest_and_tool, ROOT)

    def test_existing_service_collision_fails(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="fs2-onboarding-collision-"
        ) as temporary:
            root = Path(temporary)
            inventory = (
                root
                / "components/control-plane/contracts/all-models-live-services.json"
            )
            inventory.parent.mkdir(parents=True)
            inventory.write_text(
                json.dumps(
                    {
                        "routes": {
                            "another-model": {
                                "service": {"name": "example-7b", "port": 8000},
                                "mcp": {"tool_name": "another_model_chat"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                COMPILER.OnboardingError, "live service example-7b"
            ):
                COMPILER._validate_collisions(self.declaration(), root)

    def test_scientific_mcp_tool_cannot_shadow_live_http_tool(self) -> None:
        declaration = self.fixture(BATCH_EXAMPLE)
        declaration["batch"]["mcp"]["tool_name"] = "qwen3_8b_chat"
        with self.assertRaisesRegex(
            COMPILER.OnboardingError,
            "scientific MCP tool qwen3_8b_chat conflicts with live-service",
        ):
            COMPILER._validate_collisions(declaration, ROOT)

    def test_cli_check_returns_one_for_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fs2-onboarding-cli-") as temporary:
            output = Path(temporary) / "output"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    COMPILER.main(
                        ["generate", str(EXAMPLE), "--output-dir", str(output)]
                    ),
                    0,
                )
            (output / "projections/catalog-index.json").write_text(
                "{}\n", encoding="utf-8"
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    COMPILER.main(["check", str(EXAMPLE), "--output-dir", str(output)]),
                    1,
                )
            self.assertIn(
                "different: projections/catalog-index.json", stderr.getvalue()
            )


if __name__ == "__main__":
    unittest.main()
