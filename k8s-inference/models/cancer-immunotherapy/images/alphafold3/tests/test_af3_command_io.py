"""The published command/IO contract and fixtures must track the implementation.

The contract is generated from the runtime's own argv composers and the fixtures
are generated from the runtime and the reference-data producer. These tests
regenerate both and fail on any drift, so a controller that binds to the
published contract is binding to what the image actually runs.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "af3_runtime_io", ROOT / "runtime" / "af3_runtime.py"
)
assert _spec and _spec.loader
af3 = importlib.util.module_from_spec(_spec)
sys.modules["af3_runtime_io"] = af3
_spec.loader.exec_module(af3)

CONTRACT = json.loads(
    (ROOT / "contracts" / "af3-command-io-contract.json").read_text(encoding="utf-8")
)


def fixture(name: str) -> dict:
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


class ContractFreshnessTests(unittest.TestCase):
    def test_the_committed_contract_matches_the_implementation(self) -> None:
        """Generated, not transcribed: build.py regenerates and compares it."""
        result = subprocess.run(
            [sys.executable, str(ROOT / "build.py"), "contract", "--check-only"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("PASS", json.loads(result.stdout)["status"])


class CommandContractTests(unittest.TestCase):
    def test_it_declares_one_entrypoint_and_the_real_modes(self) -> None:
        entrypoint = CONTRACT["entrypoint"]
        self.assertEqual(
            ["/alphafold3_venv/bin/python3", "/opt/fs2/af3_runtime.py"], entrypoint["command"]
        )
        self.assertEqual(["verify"], entrypoint["default_args"])
        self.assertEqual("/alphafold3_venv/bin/python3", entrypoint["interpreter"])
        declared = set(entrypoint["modes"])
        actual = set(af3.build_parser()._actions[1].choices)
        self.assertEqual(actual, declared)

    def test_the_cpu_stage_argv_matches_what_the_runtime_composes(self) -> None:
        argv = CONTRACT["stages"]["data"]["composed_upstream_argv"]
        self.assertIn("--norun_inference", argv)
        self.assertIn("--db_dir={database_root}", argv)
        self.assertIn("--jackhmmer_n_cpu={msa_threads}", argv)
        self.assertIn("--nhmmer_n_cpu={msa_threads}", argv)
        self.assertFalse([item for item in argv if item.startswith("--model_dir")])
        self.assertNotIn("--norun_data_pipeline", argv)

    def test_the_gpu_stage_argv_matches_what_the_runtime_composes(self) -> None:
        argv = CONTRACT["stages"]["inference"]["composed_upstream_argv"]
        self.assertIn("--norun_data_pipeline", argv)
        self.assertIn("--model_dir={model_dir}", argv)
        self.assertFalse([item for item in argv if item.startswith("--db_dir")])
        self.assertNotIn("--norun_inference", argv)
        self.assertFalse([item for item in argv if "n_cpu" in item])

    def test_the_stages_declare_their_gpu_counts_and_forbidden_bindings(self) -> None:
        stages = CONTRACT["stages"]
        self.assertEqual(0, stages["data"]["gpu"])
        self.assertEqual(1, stages["inference"]["gpu"])
        self.assertIn(
            "the licensed parameter binding", stages["data"]["forbidden"]
        )
        forbidden = " ".join(stages["inference"]["forbidden"])
        self.assertIn("reference database tree", forbidden)
        self.assertIn("whole reference root", forbidden)

    def test_the_stages_publish_a_positive_allowlist(self) -> None:
        for name in ("data", "inference"):
            with self.subTest(stage=name):
                policy = CONTRACT["stages"][name]["extra_arg_policy"]
                self.assertEqual("positive-allowlist", policy["mode"])
                self.assertTrue(policy["denies_duplicates"])
                self.assertIn("num_recycles", policy["allowed_flags"])
                for flag in ("run_inference", "run_data_pipeline", "model_dir", "db_dir"):
                    self.assertIn(flag, policy["stage_critical_flags"])
                    self.assertNotIn(flag, policy["allowed_flags"])
                self.assertIn("flagfile", policy["rejected_parser_directives"])
                self.assertIn("undefok", policy["rejected_parser_directives"])

    def test_the_result_envelope_publishes_the_terminal_rule(self) -> None:
        envelope = CONTRACT["result_envelope"]
        self.assertIn("only after run_alphafold.py exits", envelope["terminal_rule"])
        self.assertIn("never leaves a PASS receipt", envelope["terminal_rule"])
        self.assertEqual(
            ["upstream", "exit_code", "terminal_state"], envelope["execution_block"]
        )

    def test_the_data_stage_publishes_a_packaged_portable_handoff(self) -> None:
        outputs = {item["name"]: item for item in CONTRACT["stages"]["data"]["outputs"]}
        handoff = outputs["data_handoff"]
        self.assertTrue(handoff["path"].endswith("fs2-af3-handoff"))
        self.assertEqual("index.json", handoff["index"])
        self.assertEqual("fs2-serve.nebius.ai/alphafold3-data-handoff/v2", handoff["schema"])
        self.assertIn("one entry per fold job", handoff["multiplicity"])
        self.assertIn("relative", handoff["portability"])
        self.assertIn("No absolute path", handoff["portability"])
        self.assertIn("sanitized", outputs["data_pipeline_output"]["description"])

    def test_the_gpu_stage_consumes_the_packaged_handoff(self) -> None:
        stage = CONTRACT["stages"]["inference"]
        self.assertIn("--handoff-dir", stage["runtime_args"])
        self.assertIn("--fold-job", stage["optional_runtime_args"])
        self.assertIn("Exactly one of", stage["input_selection"])
        self.assertIn("--fold-job is required", stage["input_selection"])
        inputs = {item["name"]: item for item in stage["inputs"]}
        self.assertEqual("index.json", inputs["handoff"]["index"])
        self.assertIn("reconstructs each payload path under this mount", inputs["handoff"]["description"])

    def test_the_single_reference_root_layout_is_published(self) -> None:
        layout = CONTRACT["root_layout"]["reference_root"]
        self.assertEqual("/reference-data", layout["mount_path"])
        self.assertEqual("/mnt/fs2-reference-data/data", layout["host_root"])
        self.assertTrue(layout["single_mount"])
        self.assertTrue(layout["read_only"])
        self.assertEqual(
            [
                "{mount_path}/{dataset_sub_path}",
                "{mount_path}/manifests/sha256/{manifest_sha256}.json",
            ],
            layout["resolves"],
        )
        self.assertEqual(".fs2-manifest-sha256", layout["readiness_marker"])
        self.assertIn("only the dataset", layout["note"])

    def test_the_result_envelope_and_exit_codes_are_published(self) -> None:
        envelope = CONTRACT["result_envelope"]
        self.assertEqual(af3.RECEIPT_SCHEMA, envelope["schema"])
        self.assertEqual(["schema", "mode", "status"], envelope["required_fields"])
        self.assertEqual(["PASS", "PLANNED", "FAIL"], envelope["status_values"])
        self.assertEqual("error", envelope["failure_field"])
        self.assertIn("2", envelope["exit_codes"])

    def test_the_cache_is_not_described_as_a_snapshot(self) -> None:
        cache = CONTRACT["root_layout"]["cache"]
        self.assertFalse(cache["is_gpu_snapshot"])
        self.assertTrue(cache["optional"])

    def test_the_obsolete_adapter_command_surface_is_refused(self) -> None:
        legacy = CONTRACT["legacy_aliases"]["fs2-run-alphafold3"]
        self.assertFalse(legacy["supported"])
        self.assertIn("adapter", legacy["action"])
        # No alias may leak into the runtime itself.
        entrypoint = (ROOT / "runtime" / "af3_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("fs2-run-alphafold3", entrypoint)


class FixtureTests(unittest.TestCase):
    def test_the_terminal_receipt_fixture_is_valid_for_this_runtime(self) -> None:
        receipt = fixture("reference-terminal-receipt.json")
        validated = af3.validate_terminal_receipt(receipt)
        self.assertEqual(
            "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1", validated["schema"]
        )
        self.assertEqual("/mnt/fs2-reference-data/data", validated["storage"]["host_root"])
        self.assertEqual("/reference-data", validated["storage"]["mount_path"])

    def test_the_manifest_fixture_hashes_to_the_receipts_manifest_identity(self) -> None:
        receipt = fixture("reference-terminal-receipt.json")
        manifest = fixture("reference-published-manifest.json")
        self.assertEqual(
            receipt["content"]["manifest_sha256"], af3.manifest_self_digest(manifest)
        )
        self.assertNotEqual(
            receipt["content"]["manifest_sha256"], receipt["content"]["tree_sha256"]
        )

    def test_the_preprocess_fixture_carries_exactly_the_producer_fields(self) -> None:
        reference_data = fixture("preprocess-reference-data.json")
        self.assertEqual(
            ["bundle_id", "manifest_sha256", "manifest_uri", "revision"],
            sorted(reference_data),
        )
        receipt = fixture("reference-terminal-receipt.json")
        self.assertEqual(
            receipt["content"]["manifest_sha256"], reference_data["manifest_sha256"]
        )
        self.assertTrue(
            reference_data["manifest_uri"].endswith(
                f"/{reference_data['manifest_sha256']}.json"
            )
        )

    def test_every_stage_fixture_validates_against_the_receipt_schema(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (ROOT / "schemas" / "af3-runtime-receipt.schema.json").read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        for name in (
            "data-stage-receipt.json",
            "inference-stage-receipt.json",
            "failure-receipt.json",
        ):
            with self.subTest(fixture=name):
                validator.validate(fixture(name))

    def test_the_data_fixture_argv_matches_the_published_contract(self) -> None:
        argv = fixture("data-stage-receipt.json")["plan"]["argv"]
        template = CONTRACT["stages"]["data"]["composed_upstream_argv"]
        rendered = [
            item.replace("{json_path}", "/input/fold_input.json")
            .replace("{output_dir}", "/output")
            .replace("{msa_threads}", "16")
            for item in template
        ]
        # The database root is fixture specific; compare everything else.
        self.assertEqual(
            [item for item in rendered if not item.startswith("--db_dir")],
            [item for item in argv if not item.startswith("--db_dir")],
        )

    def test_the_data_fixture_respects_the_declared_cpu_envelope(self) -> None:
        """16 CPU is what reference-data declares for AlphaFold 3 preprocessing."""
        envelope = fixture("data-stage-receipt.json")["cpu_envelope"]
        self.assertEqual(16, envelope["msa_threads"])
        self.assertEqual(16, envelope["cpu_request"])
        self.assertEqual(16, envelope["jackhmmer_n_cpu"])
        self.assertEqual(16, envelope["nhmmer_n_cpu"])
        self.assertTrue(envelope["upstream_default_overridden"])
        # Never upstream's node-derived default.
        self.assertNotEqual(8, envelope["msa_threads"])

    def test_the_data_fixture_binds_a_single_reference_root(self) -> None:
        reference = fixture("data-stage-receipt.json")["reference_data"]
        self.assertTrue(reference["single_root_mount"])
        self.assertEqual("mounted-tree-name-and-marker", reference["bound_by"])
        self.assertTrue(reference["marker_matched_receipt"])
        self.assertTrue(reference["manifest_document_verified"])
        self.assertEqual(
            reference["content_tree_sha256"], Path(reference["database_root"]).name
        )
        self.assertTrue(
            reference["manifest_path"].startswith("/reference-data/manifests/sha256/")
        )

    def test_the_failure_fixture_is_the_terminal_envelope(self) -> None:
        failure = fixture("failure-receipt.json")
        self.assertEqual("FAIL", failure["status"])
        self.assertIn("error", failure)
        self.assertNotIn("plan", failure)


if __name__ == "__main__":
    unittest.main()
