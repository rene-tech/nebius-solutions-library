"""Unit tests for the AlphaFold 3 runtime entrypoint.

These run without the image, without a GPU and without any licensed byte. The
parameter identity is exercised against small synthetic artifacts by injecting a
synthetic expectation, so the logic is proven without a 1 GB fixture.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[3]

_spec = importlib.util.spec_from_file_location(
    "af3_runtime", ROOT / "runtime" / "af3_runtime.py"
)
assert _spec and _spec.loader
af3 = importlib.util.module_from_spec(_spec)
sys.modules["af3_runtime"] = af3
_spec.loader.exec_module(af3)

AUTHORIZED_SHA256 = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
AUTHORIZED_BYTES = 1020545840
UPSTREAM_COMMIT = "85c4d20505fd5cef05eac22b534d4e793971ae69"

BUNDLE = "alphafold3-public-databases-v3.0"
REVISION = "v3.0-paper-snapshot-2022-09-28"
TREE_SHA = "a" * 64


def synthetic_expectation(payload: bytes) -> af3.ParameterExpectation:
    """An expectation describing a small synthetic artifact."""
    return af3.ParameterExpectation(
        artifact_id="alphafold3-parameters",
        filename="af3.bin.zst",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        magic_hex="28b52ffd",
        decompressed_sha256=None,
        decompressed_size_bytes=None,
        asset_gid=65532,
        expect_distribution_version="3.0.4",
        expect_min_parameter_arrays=100,
    )


ZSTD_MAGIC = bytes.fromhex("28b52ffd")


class ParameterVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.payload = ZSTD_MAGIC + b"synthetic-parameter-body"
        self.path = self.dir / "af3.bin.zst"
        self.path.write_bytes(self.payload)
        self.expect = synthetic_expectation(self.payload)

    def test_exact_artifact_verifies_and_reports_its_identity(self) -> None:
        report = af3.verify_parameter_artifact(self.path, self.expect)
        self.assertEqual(self.expect.sha256, report["sha256"])
        self.assertEqual(len(self.payload), report["size_bytes"])
        self.assertEqual("file-digest", report["identity_kind"])
        self.assertFalse(report["deep_verified"])
        self.assertEqual("alphafold3-parameters", report["artifact_id"])

    def test_missing_artifact_names_the_claim_and_the_supplemental_group(self) -> None:
        with self.assertRaises(af3.ContractError) as caught:
            af3.verify_parameter_artifact(self.dir / "absent.bin.zst", self.expect)
        message = str(caught.exception)
        self.assertIn("alphafold3/af3.bin.zst", message)
        self.assertIn("65532", message)

    def test_wrong_size_fails_before_the_digest_is_read(self) -> None:
        self.path.write_bytes(self.payload + b"extra")
        with self.assertRaises(af3.ContractError) as caught:
            af3.verify_parameter_artifact(self.path, self.expect)
        self.assertIn("size mismatch", str(caught.exception))

    def test_wrong_content_of_the_same_size_fails_on_the_digest(self) -> None:
        tampered = bytearray(self.payload)
        tampered[-1] ^= 0xFF
        self.path.write_bytes(bytes(tampered))
        with self.assertRaises(af3.ContractError) as caught:
            af3.verify_parameter_artifact(self.path, self.expect)
        self.assertIn("digest mismatch", str(caught.exception))

    def test_wrong_magic_is_rejected(self) -> None:
        payload = b"\x00\x00\x00\x00" + b"not-zstd-at-all........."
        self.path.write_bytes(payload)
        expect = synthetic_expectation(payload)
        # Same size and digest, but the declared zstd magic no longer matches.
        with self.assertRaises(af3.ContractError) as caught:
            af3.verify_parameter_artifact(self.path, expect)
        self.assertIn("zstd magic", str(caught.exception))

    def test_a_directory_at_the_parameter_path_is_rejected(self) -> None:
        target = self.dir / "as-directory"
        target.mkdir()
        with self.assertRaises(af3.ContractError) as caught:
            af3.verify_parameter_artifact(target, self.expect)
        self.assertIn("is a directory", str(caught.exception))

    def test_committed_binding_declares_the_authorized_identity(self) -> None:
        contract = json.loads(
            (ROOT / "contracts" / "af3-parameter-binding.json").read_text(encoding="utf-8")
        )
        expect = af3.ParameterExpectation.from_contract(contract)
        self.assertEqual(AUTHORIZED_SHA256, expect.sha256)
        self.assertEqual(AUTHORIZED_BYTES, expect.size_bytes)
        self.assertEqual(65532, expect.asset_gid)
        self.assertEqual("3.0.4", expect.expect_distribution_version)


class ModelDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_exactly_one_parameter_object_resolves(self) -> None:
        (self.dir / "af3.bin.zst").write_bytes(b"x")
        model_dir, candidates = af3.resolve_model_dir(self.dir / "af3.bin.zst")
        self.assertEqual(self.dir, model_dir)
        self.assertEqual(["af3.bin.zst"], candidates)

    def test_a_second_parameter_object_is_refused(self) -> None:
        (self.dir / "af3.bin.zst").write_bytes(b"x")
        (self.dir / "other.bin.zst").write_bytes(b"y")
        with self.assertRaises(af3.ContractError) as caught:
            af3.resolve_model_dir(self.dir / "af3.bin.zst")
        message = str(caught.exception)
        self.assertIn("exactly one", message)
        self.assertIn("subPath", message)

    def test_an_unselectable_name_is_refused(self) -> None:
        target = self.dir / "parameters.tar"
        target.write_bytes(b"x")
        with self.assertRaises(af3.ContractError):
            af3.resolve_model_dir(target)


class CanonicalizationTests(unittest.TestCase):
    """The manifest digest is only a real check if canonicalization agrees."""

    def test_canonicalization_matches_the_publisher(self) -> None:
        publisher = (REPO / "reference-data" / "reference_data.py").read_text(encoding="utf-8")
        self.assertIn(
            'json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)',
            publisher,
            "the publisher's canonical_json changed; update af3_runtime.canonical_json",
        )
        sample = {"b": 1, "a": [2, {"d": None, "c": "x"}]}
        self.assertEqual(
            json.dumps(
                sample, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8"),
            af3.canonical_json(sample),
        )

    def test_equated_identities_are_refused_by_the_guard(self) -> None:
        digest = "1" * 64
        with self.assertRaises(af3.ContractError) as caught:
            af3.assert_independent_identities(digest, digest)
        self.assertIn("must never be equated", str(caught.exception))
        af3.assert_independent_identities("1" * 64, "2" * 64)

    def test_committed_contract_keeps_identities_unpublished(self) -> None:
        contract = json.loads(
            (ROOT / "contracts" / "af3-reference-data-binding.json").read_text(encoding="utf-8")
        )
        self.assertEqual("pending-publication", contract["state"])
        self.assertIsNone(contract["identities"]["content_tree_sha256"])
        self.assertIsNone(contract["identities"]["manifest_sha256"])
        receipt = contract["producer_receipt"]
        self.assertEqual(
            "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1", receipt["schema"]
        )
        self.assertEqual(
            ["schema", "bundle_id", "revision", "created_at", "storage", "content", "placement"],
            receipt["exact_fields"],
        )
        self.assertEqual(
            ["host_root", "mount_path", "dataset_sub_path", "read_only"],
            receipt["exact_storage_fields"],
        )
        self.assertTrue(receipt["carries_manifest_digest"])
        self.assertFalse(receipt["carries_manifest_uri"])
        self.assertFalse(receipt["carries_file_list"])
        self.assertEqual(
            ["bundle_id", "revision", "manifest_uri", "manifest_sha256"],
            contract["preprocess_request_transform"]["output_fields"],
        )
        # The invented names from earlier iterations must stay refused.
        forbidden = contract["identities"]["forbidden_field_names"]
        for alias in ("published_manifest_sha256", "source_sub_path", "shared_filesystem_uri"):
            self.assertIn(alias, forbidden)


class StageSeparationTests(unittest.TestCase):
    def test_a_stage_may_not_hold_both_bindings(self) -> None:
        with self.assertRaises(af3.ContractError) as caught:
            af3.StageBindings(stage="data", parameters_bound=True, reference_bound=True).enforce()
        self.assertIn("must never hold both", str(caught.exception))

    def test_the_cpu_stage_may_not_bind_parameters(self) -> None:
        with self.assertRaises(af3.ContractError) as caught:
            af3.StageBindings(stage="data", parameters_bound=True, reference_bound=False).enforce()
        self.assertIn("must not bind the licensed parameters", str(caught.exception))

    def test_the_gpu_stage_may_not_bind_reference_databases(self) -> None:
        with self.assertRaises(af3.ContractError) as caught:
            af3.StageBindings(
                stage="inference", parameters_bound=True, reference_bound=True
            ).enforce()
        self.assertIn("must never hold both", str(caught.exception))

    def test_correctly_separated_stages_pass(self) -> None:
        af3.StageBindings(stage="data", parameters_bound=False, reference_bound=True).enforce()
        af3.StageBindings(
            stage="inference", parameters_bound=True, reference_bound=False
        ).enforce()

    def test_reference_databases_are_detected_by_their_published_names(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.assertEqual([], af3.reference_databases_present(root))
            (root / "mgy_clusters_2022_05.fa").write_bytes(b"x")
            (root / "mmcif_files").mkdir()
            found = af3.reference_databases_present(root)
            self.assertIn("mgy_clusters_2022_05.fa", found)
            self.assertIn("mmcif_files", found)


class CacheTests(unittest.TestCase):
    def test_a_writable_cache_is_reported_and_never_called_a_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            report = af3.prepare_caches({"FS2_AF3_CACHE_ROOT": name})
            receipt = report.as_receipt()
            self.assertTrue(receipt["writable"])
            self.assertIsNone(receipt["degraded_reason"])
            self.assertFalse(receipt["is_gpu_snapshot"])
            self.assertEqual("auxiliary-compiler-cache", receipt["cache_level"])
            self.assertEqual("xla-and-triton-compilation-cache", receipt["kind"])

    def test_an_unwritable_cache_degrades_truthfully_instead_of_failing(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "locked"
            root.mkdir()
            root.chmod(stat.S_IRUSR | stat.S_IXUSR)
            try:
                report = af3.prepare_caches({"FS2_AF3_CACHE_ROOT": str(root)})
                receipt = report.as_receipt()
                self.assertFalse(receipt["writable"])
                self.assertIn("not writable", receipt["degraded_reason"])
                self.assertFalse(receipt["is_gpu_snapshot"])
                self.assertEqual({}, af3.cache_environment(report))
            finally:
                # Restore write permission inside the context so the temporary
                # directory can still be removed.
                root.chmod(0o700)


class ArgvCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = af3.prepare_caches({"FS2_AF3_CACHE_ROOT": self.tmp.name})

    def test_the_cpu_stage_disables_inference_and_never_passes_a_model_dir(self) -> None:
        plan = af3.compose_data_argv(
            json_path=Path("/input/fold.json"),
            output_dir=Path("/output"),
            database_root=Path("/reference-data/alphafold3"),
            cache=self.cache,
            threads=6,
            cpu_request=6,
        )
        self.assertEqual("data", plan.stage)
        self.assertIn("--norun_inference", plan.argv)
        self.assertIn("--db_dir=/reference-data/alphafold3", plan.argv)
        self.assertFalse([arg for arg in plan.argv if arg.startswith("--model_dir")])
        self.assertNotIn("--norun_data_pipeline", plan.argv)

    def test_the_gpu_stage_disables_the_pipeline_and_never_passes_a_db_dir(self) -> None:
        plan = af3.compose_inference_argv(
            json_path=Path("/handoff/data.json"),
            output_dir=Path("/output"),
            model_dir=Path("/models"),
            cache=self.cache,
        )
        self.assertEqual("inference", plan.stage)
        self.assertIn("--norun_data_pipeline", plan.argv)
        self.assertIn("--model_dir=/models", plan.argv)
        self.assertFalse([arg for arg in plan.argv if arg.startswith("--db_dir")])
        self.assertNotIn("--norun_inference", plan.argv)
        self.assertIn("--flash_attention_implementation=triton", plan.argv)
        self.assertTrue(
            [arg for arg in plan.argv if arg.startswith("--jax_compilation_cache_dir=")]
        )

    def test_the_gpu_stage_omits_the_cache_flag_when_the_cache_is_unusable(self) -> None:
        degraded = af3.CacheReport(
            root="/cache/alphafold3",
            jax_dir=None,
            triton_dir=None,
            xdg_dir=None,
            writable=False,
            degraded_reason="not writable: test",
        )
        plan = af3.compose_inference_argv(
            json_path=Path("/handoff/data.json"),
            output_dir=Path("/output"),
            model_dir=Path("/models"),
            cache=degraded,
        )
        self.assertFalse(
            [arg for arg in plan.argv if arg.startswith("--jax_compilation_cache_dir")]
        )

    def test_flash_attention_stays_selectable_for_gpu_portability(self) -> None:
        plan = af3.compose_inference_argv(
            json_path=Path("/handoff/data.json"),
            output_dir=Path("/output"),
            model_dir=Path("/models"),
            cache=self.cache,
            flash_attention="xla",
        )
        self.assertIn("--flash_attention_implementation=xla", plan.argv)


class CpuEnvelopeDriftTests(unittest.TestCase):
    """AlphaFold 3 derives its MSA thread default from the node, not the pod.

    Upstream defaults both --jackhmmer_n_cpu and --nhmmer_n_cpu to
    min(cpu_count, 8), read from the node. That is wrong in both directions: on
    a stage smaller than eight CPUs it oversubscribes the cgroup, and on the
    declared sixteen-CPU AlphaFold 3 preprocessing stage it silently caps both
    tools at eight. Both flags are therefore always emitted from the
    controller-frozen value and validated against the stage's CPU request.
    """

    MSA_FLAGS = ("--jackhmmer_n_cpu", "--nhmmer_n_cpu")
    # The reference-data plane declares this stage as 16 CPU and refuses less.
    CANONICAL_CPU = 16
    UPSTREAM_NODE_DEFAULT = 8

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = af3.prepare_caches({"FS2_AF3_CACHE_ROOT": self.tmp.name})

    def _data_argv(self, **kwargs) -> list[str]:
        return af3.compose_data_argv(
            json_path=Path("/input/fold.json"),
            output_dir=Path("/output"),
            database_root=Path("/reference-data/alphafold3"),
            cache=self.cache,
            **kwargs,
        ).argv

    def test_both_msa_thread_flags_are_always_emitted(self) -> None:
        argv = self._data_argv(threads=self.CANONICAL_CPU, cpu_request=self.CANONICAL_CPU)
        for flag in self.MSA_FLAGS:
            matching = [arg for arg in argv if arg.startswith(f"{flag}=")]
            self.assertEqual(
                1, len(matching), f"{flag} must appear exactly once, got {matching}"
            )
            self.assertEqual(f"{flag}={self.CANONICAL_CPU}", matching[0])

    def test_the_canonical_stage_never_falls_back_to_the_node_default(self) -> None:
        argv = self._data_argv(threads=self.CANONICAL_CPU, cpu_request=self.CANONICAL_CPU)
        for flag in self.MSA_FLAGS:
            self.assertNotIn(f"{flag}={self.UPSTREAM_NODE_DEFAULT}", argv)
            self.assertIn(f"{flag}={self.CANONICAL_CPU}", argv)

    def test_a_stage_smaller_than_the_node_default_is_not_oversubscribed(self) -> None:
        """The guard also protects a stage smaller than upstream's default."""
        argv = self._data_argv(threads=4, cpu_request=4)
        for flag in self.MSA_FLAGS:
            self.assertIn(f"{flag}=4", argv)
        with self.assertRaises(af3.ContractError):
            self._data_argv(threads=self.UPSTREAM_NODE_DEFAULT, cpu_request=4)

    def test_the_canonical_cpu_matches_the_reference_planes_declared_capacity(self) -> None:
        """The declared AlphaFold 3 preprocessing capacity is the authority."""
        requirements = REPO / "reference-data" / "model-requirements.json"
        document = json.loads(requirements.read_text(encoding="utf-8"))
        capacity = document.get("models", {}).get("alphafold3", {}).get(
            "preprocessing_capacity"
        )
        if capacity is None:
            self.skipTest(
                "reference-data has not declared an alphafold3 preprocessing capacity yet"
            )
        self.assertEqual(str(self.CANONICAL_CPU), capacity["cpu"])

    def test_threads_above_the_cpu_request_are_refused(self) -> None:
        with self.assertRaises(af3.ContractError) as caught:
            self._data_argv(threads=self.CANONICAL_CPU + 1, cpu_request=self.CANONICAL_CPU)
        message = str(caught.exception)
        self.assertIn("exceeds the stage CPU request", message)
        self.assertIn("oversubscribe", message)

    def test_threads_equal_to_the_cpu_request_are_allowed(self) -> None:
        argv = self._data_argv(threads=self.CANONICAL_CPU, cpu_request=self.CANONICAL_CPU)
        self.assertIn(f"--nhmmer_n_cpu={self.CANONICAL_CPU}", argv)

    def test_thread_counts_outside_the_producer_bounds_are_refused(self) -> None:
        for bad in (0, -1, 129):
            with self.subTest(threads=bad):
                with self.assertRaises(af3.ContractError):
                    af3.resolve_msa_threads(bad, None)

    def test_a_boolean_thread_count_is_refused(self) -> None:
        with self.assertRaises(af3.ContractError):
            af3.resolve_msa_threads(True, None)

    def test_the_producer_bounds_still_match_the_request_schema(self) -> None:
        schema = json.loads(
            (REPO / "reference-data" / "preprocess-request.schema.json").read_text(
                encoding="utf-8"
            )
        )
        threads = schema["properties"]["backend"]["properties"]["threads"]
        self.assertEqual(af3.MIN_THREADS, threads["minimum"])
        self.assertEqual(af3.MAX_THREADS, threads["maximum"])

    def test_the_gpu_stage_does_not_carry_msa_thread_flags(self) -> None:
        argv = af3.compose_inference_argv(
            json_path=Path("/handoff/data.json"),
            output_dir=Path("/output"),
            model_dir=Path("/models"),
            cache=self.cache,
        ).argv
        for flag in self.MSA_FLAGS:
            self.assertFalse([arg for arg in argv if arg.startswith(flag)])

    def test_the_handoff_contract_declares_both_flags_for_the_cpu_stage(self) -> None:
        handoff = json.loads(
            (ROOT / "contracts" / "af3-runtime-handoff.json").read_text(encoding="utf-8")
        )
        stages = {stage["stage"]: stage for stage in handoff["stages"]}
        data = stages["data"]
        for flag in self.MSA_FLAGS:
            self.assertIn(flag, data["composed_flags"])
            self.assertIn(flag, data["cpu_envelope"]["msa_thread_flags"])
        self.assertEqual(0, data["gpu"])
        self.assertEqual(1, stages["inference"]["gpu"])
        for flag in self.MSA_FLAGS:
            self.assertNotIn(flag, stages["inference"]["composed_flags"])

    def test_the_reference_contract_records_why_the_flags_are_pinned(self) -> None:
        contract = json.loads(
            (ROOT / "contracts" / "af3-reference-data-binding.json").read_text(encoding="utf-8")
        )
        envelope = contract["cpu_envelope"]
        self.assertIn("min(cpu_count, 8)", envelope["upstream_default"])
        self.assertIn("16", envelope["why"])
        self.assertEqual(
            "preprocess request backend.threads, an integer between 1 and 128",
            envelope["producer_field"],
        )


class ReceiptTests(unittest.TestCase):
    def test_emit_publishes_a_complete_receipt_by_atomic_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "data-runtime-receipt.json"
            receipt.write_text('{"generation":"old"}\n', encoding="utf-8")
            document = {"schema": af3.RECEIPT_SCHEMA, "mode": "data", "status": "PASS"}
            expected = json.dumps(document, indent=2, sort_keys=True) + "\n"
            real_replace = os.replace

            def observe_replace(source, destination):
                self.assertEqual(Path(destination), receipt)
                self.assertEqual(receipt.read_text(encoding="utf-8"), '{"generation":"old"}\n')
                self.assertEqual(Path(source).read_text(encoding="utf-8"), expected)
                real_replace(source, destination)

            with mock.patch.object(af3.os, "replace", side_effect=observe_replace):
                af3.emit(document, receipt)
            self.assertEqual(receipt.read_text(encoding="utf-8"), expected)
            self.assertFalse(any(root.glob(".*.partial")))

    def test_data_handoff_content_bound_is_inclusive_and_rejects_one_byte_over(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            source = output / "fixture" / "fixture_data.json"
            source.parent.mkdir()
            source.write_bytes(b"x" * 257)
            with mock.patch.object(af3, "MAX_DATA_HANDOFF_BYTES", 16 * 1024):
                af3.build_data_handoff(output, {"artifact_id": "fold-input", "sha256": "a" * 64})
            index = output / af3.DATA_HANDOFF_DIRNAME / af3.DATA_HANDOFF_INDEX
            exact_total = source.stat().st_size + index.stat().st_size

            with mock.patch.object(af3, "MAX_DATA_HANDOFF_BYTES", exact_total):
                handoff = af3.build_data_handoff(
                    output, {"artifact_id": "fold-input", "sha256": "a" * 64}
                )
            self.assertEqual(handoff["count"], 1)

            with mock.patch.object(af3, "MAX_DATA_HANDOFF_BYTES", exact_total - 1):
                with self.assertRaisesRegex(af3.ContractError, "handoff plus index"):
                    af3.build_data_handoff(output, {"artifact_id": "fold-input", "sha256": "a" * 64})

    def test_a_failure_receipt_matches_the_published_schema(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (ROOT / "schemas" / "af3-runtime-receipt.schema.json").read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        validator.validate(
            {
                "schema": "fs2-serve.nebius.ai/alphafold3-runtime-receipt/v1",
                "mode": "inference",
                "status": "FAIL",
                "error": "parameter object digest mismatch",
            }
        )

    def test_a_params_load_receipt_requires_its_semantic_block(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (ROOT / "schemas" / "af3-runtime-receipt.schema.json").read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        incomplete = {
            "schema": "fs2-serve.nebius.ai/alphafold3-runtime-receipt/v1",
            "mode": "params-load",
            "status": "PASS",
        }
        self.assertTrue(list(validator.iter_errors(incomplete)))

    def test_a_receipt_may_not_claim_embedded_parameters(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (ROOT / "schemas" / "af3-runtime-receipt.schema.json").read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        lying = {
            "schema": "fs2-serve.nebius.ai/alphafold3-runtime-receipt/v1",
            "mode": "verify",
            "status": "PASS",
            "image": {
                "runtime_id": "alphafold3",
                "upstream_version": "3.0.4",
                "upstream_commit": UPSTREAM_COMMIT,
                "parameters_embedded": True,
                "reference_databases_embedded": False,
            },
        }
        self.assertTrue(list(validator.iter_errors(lying)))


if __name__ == "__main__":
    unittest.main()
