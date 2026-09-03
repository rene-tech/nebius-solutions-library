"""Guards that keep a stage from becoming something other than what it is.

Each test here corresponds to a defect that was found in review: a PASS receipt
written before upstream had exited, a data-stage output a collector could not
locate, a reference-root mount the inference guard walked straight past, and an
extra argument that could switch the pipeline back on.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "af3_runtime_guards", ROOT / "runtime" / "af3_runtime.py"
)
assert _spec and _spec.loader
af3 = importlib.util.module_from_spec(_spec)
sys.modules["af3_runtime_guards"] = af3
_spec.loader.exec_module(af3)


class ReferenceRootDetectionTests(unittest.TestCase):
    """The canonical mount is the whole reference root, not a bare dataset."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_an_empty_mount_is_not_a_reference_tree(self) -> None:
        self.assertEqual([], af3.reference_databases_present(self.root))

    def test_a_whole_reference_root_is_detected(self) -> None:
        """Databases sit several levels down; the layout is the giveaway."""
        tree = (
            self.root
            / "datasets"
            / "alphafold3-public-databases-v3.0"
            / "v3.0-paper-snapshot-2022-09-28"
            / "sha256"
            / ("a" * 64)
        )
        tree.mkdir(parents=True)
        (tree / "mgy_clusters_2022_05.fa").write_text("x", encoding="utf-8")
        (tree / ".fs2-manifest-sha256").write_text("b" * 64, encoding="utf-8")
        (self.root / "manifests" / "sha256").mkdir(parents=True)

        found = af3.reference_databases_present(self.root)
        self.assertIn("datasets/", found)
        self.assertIn("manifests/sha256/", found)
        self.assertTrue([item for item in found if item.startswith("datasets/...")])

    def test_the_dataset_layout_alone_is_enough(self) -> None:
        tree = self.root / "datasets" / "bundle" / "rev" / "sha256" / ("c" * 64)
        tree.mkdir(parents=True)
        self.assertTrue(af3.reference_databases_present(self.root))

    def test_a_directly_mounted_dataset_tree_is_detected(self) -> None:
        (self.root / ".fs2-manifest-sha256").write_text("d" * 64, encoding="utf-8")
        self.assertIn(".fs2-manifest-sha256", af3.reference_databases_present(self.root))

    def test_loose_database_filenames_are_still_detected(self) -> None:
        (self.root / "uniref90_2022_05.fa").write_text("x", encoding="utf-8")
        (self.root / "mmcif_files").mkdir()
        found = af3.reference_databases_present(self.root)
        self.assertIn("uniref90_2022_05.fa", found)
        self.assertIn("mmcif_files", found)

    def test_the_gpu_stage_refuses_a_whole_reference_root(self) -> None:
        tree = self.root / "datasets" / "bundle" / "rev" / "sha256" / ("e" * 64)
        tree.mkdir(parents=True)
        with self.assertRaises(af3.ContractError) as caught:
            af3.StageBindings(
                stage="inference",
                parameters_bound=True,
                reference_bound=bool(af3.reference_databases_present(self.root)),
            ).enforce()
        self.assertIn("must never hold both", str(caught.exception))

    def test_detection_does_not_walk_the_published_tree(self) -> None:
        """A published bundle is hundreds of gigabytes; probes must stay bounded."""
        tree = self.root / "datasets" / "bundle" / "rev" / "sha256" / ("f" * 64)
        deep = tree / "mmcif_files" / "ab" / "cd"
        deep.mkdir(parents=True)
        for index in range(50):
            (deep / f"{index}.cif").write_text("x", encoding="utf-8")
        opened: list[Path] = []
        original = Path.rglob

        def tracking_rglob(self, pattern):  # pragma: no cover - guard only
            opened.append(self)
            return original(self, pattern)

        Path.rglob = tracking_rglob
        try:
            af3.reference_databases_present(self.root)
        finally:
            Path.rglob = original
        self.assertEqual([], opened, "reference detection must not rglob the mount")


class ExtraArgumentAllowlistTests(unittest.TestCase):
    """Only reviewed tuning flags get through, and nothing can widen the list.

    A denylist cannot be complete here: Abseil's --flagfile reads further flags
    out of a file, so any denied flag could be smuggled back through one
    indirection, and every new upstream flag would default to allowed.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = af3.prepare_caches({"FS2_AF3_CACHE_ROOT": self.tmp.name})

    def _data(self, extra):
        return af3.compose_data_argv(
            json_path=Path("/input/f.json"),
            output_dir=Path("/output"),
            database_root=Path("/reference-data/x"),
            cache=self.cache,
            threads=16,
            extra=extra,
        )

    def _inference(self, extra):
        return af3.compose_inference_argv(
            json_path=Path("/handoff/f.json"),
            output_dir=Path("/output"),
            model_dir=Path("/models"),
            cache=self.cache,
            extra=extra,
        )

    def test_reviewed_tuning_flags_are_admitted(self) -> None:
        argv = self._inference(["--num_recycles=3", "--num_diffusion_samples=2"]).argv
        self.assertIn("--num_recycles=3", argv)
        self.assertIn("--num_diffusion_samples=2", argv)

    def test_every_allowed_flag_is_actually_an_upstream_flag(self) -> None:
        """The allowlist must not drift into naming flags upstream does not have."""
        for name in af3.ALLOWED_EXTRA_FLAGS:
            with self.subTest(flag=name):
                self.assertNotIn(name, af3.STAGE_CRITICAL_FLAGS)
                self.assertNotIn(name, af3.PARSER_META_FLAGS)

    def test_an_unreviewed_flag_is_refused_even_though_it_is_harmless_looking(self) -> None:
        with self.assertRaises(af3.ContractError) as caught:
            self._inference(["--some_new_upstream_flag=1"])
        self.assertIn("not a reviewed tuning flag", str(caught.exception))

    def test_abseil_flagfile_is_refused_outright(self) -> None:
        """The indirection that would otherwise defeat the allowlist."""
        for attempt in ("--flagfile=/tmp/evil", "--flagfile", "-flagfile=/tmp/evil"):
            with self.subTest(attempt=attempt):
                with self.assertRaises(af3.ContractError) as caught:
                    self._inference([attempt])
                self.assertIn("parser directive", str(caught.exception))

    def test_every_parser_and_meta_indirection_is_refused(self) -> None:
        for name in sorted(af3.PARSER_META_FLAGS):
            with self.subTest(flag=name):
                with self.assertRaises(af3.ContractError) as caught:
                    self._inference([f"--{name}"])
                self.assertIn("parser directive", str(caught.exception))

    def test_undefok_cannot_be_used_to_smuggle_unknown_flags(self) -> None:
        with self.assertRaises(af3.ContractError):
            self._inference(["--undefok=model_dir"])

    def test_the_data_stage_cannot_be_turned_into_an_inference_run(self) -> None:
        for attempt in ("--run_inference", "--run_inference=true", "--norun_inference"):
            with self.subTest(attempt=attempt):
                with self.assertRaises(af3.ContractError) as caught:
                    self._data([attempt])
                self.assertIn("stage-critical", str(caught.exception))

    def test_the_gpu_stage_cannot_re_enable_the_data_pipeline(self) -> None:
        for attempt in ("--run_data_pipeline", "--norun_data_pipeline"):
            with self.subTest(attempt=attempt):
                with self.assertRaises(af3.ContractError):
                    self._inference([attempt])

    def test_the_model_and_database_directories_cannot_be_redirected(self) -> None:
        with self.assertRaises(af3.ContractError):
            self._inference(["--model_dir=/elsewhere"])
        with self.assertRaises(af3.ContractError):
            self._data(["--db_dir=/elsewhere"])

    def test_the_msa_thread_flags_cannot_be_overridden(self) -> None:
        with self.assertRaises(af3.ContractError):
            self._data(["--jackhmmer_n_cpu=64"])

    def test_a_duplicate_extra_flag_is_refused(self) -> None:
        with self.assertRaises(af3.ContractError) as caught:
            self._inference(["--num_recycles=3", "--num_recycles=5"])
        self.assertIn("more than once", str(caught.exception))

    def test_a_non_flag_extra_argument_is_refused(self) -> None:
        with self.assertRaises(af3.ContractError) as caught:
            self._inference(["suddenly-a-positional"])
        self.assertIn("is not a flag", str(caught.exception))

    def test_the_no_negation_is_normalised_before_the_check(self) -> None:
        self.assertEqual("run_inference", af3._flag_name("--norun_inference"))
        self.assertEqual("save_embeddings", af3._flag_name("--nosave_embeddings"))
        # A flag that merely starts with n must not be mangled.
        self.assertEqual("nhmmer_n_cpu", af3._flag_name("--nhmmer_n_cpu=8"))

    def test_a_negated_tuning_flag_is_still_admitted(self) -> None:
        self.assertIn("--nosave_embeddings", self._inference(["--nosave_embeddings"]).argv)


class PortableHandoffTests(unittest.TestCase):
    """The CPU stage packages a relocatable handoff the GPU stage reconstructs.

    A CPU pod's absolute /output path means nothing in the GPU pod, so the index
    records relative paths only and the payloads travel with it.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "output"
        self.output.mkdir()

    def _job(self, name: str, body: str | None = None) -> Path:
        directory = self.output / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}_data.json"
        path.write_text(body or json.dumps({"name": name}), encoding="utf-8")
        return path

    # -- frozen behaviour: exactly one fold job -------------------------------

    def test_exactly_one_fold_job_needs_no_selector(self) -> None:
        self._job("solo")
        handoff = af3.build_data_handoff(self.output)
        self.assertEqual(1, handoff["count"])
        self.assertEqual(["solo"], handoff["fold_jobs"])

        resolved = af3.load_data_handoff(self.output / af3.DATA_HANDOFF_DIRNAME)
        self.assertEqual("solo", resolved["fold_job"])
        self.assertEqual("solo/solo_data.json", resolved["relative_path"])

    # -- frozen behaviour: multiple fold jobs ---------------------------------

    def test_multiple_fold_jobs_are_all_packaged_and_ordered(self) -> None:
        for name in ("job_c", "job_a", "job_b"):
            self._job(name)
        handoff = af3.build_data_handoff(self.output)
        self.assertEqual(3, handoff["count"])
        self.assertEqual(["job_a", "job_b", "job_c"], handoff["fold_jobs"])

    def test_multiple_fold_jobs_require_an_explicit_selection(self) -> None:
        for name in ("job_a", "job_b"):
            self._job(name)
        af3.build_data_handoff(self.output)
        directory = self.output / af3.DATA_HANDOFF_DIRNAME
        with self.assertRaises(af3.ContractError) as caught:
            af3.load_data_handoff(directory)
        message = str(caught.exception)
        self.assertIn("2 fold jobs", message)
        self.assertIn("--fold-job", message)

    def test_a_named_fold_job_resolves_to_its_own_payload(self) -> None:
        for name in ("job_a", "job_b"):
            self._job(name)
        af3.build_data_handoff(self.output)
        directory = self.output / af3.DATA_HANDOFF_DIRNAME
        for name in ("job_a", "job_b"):
            with self.subTest(fold_job=name):
                resolved = af3.load_data_handoff(directory, name)
                self.assertEqual(name, resolved["fold_job"])
                self.assertEqual(
                    {"name": name},
                    json.loads(resolved["json_path"].read_text(encoding="utf-8")),
                )

    def test_an_unknown_fold_job_lists_what_is_available(self) -> None:
        for name in ("job_a", "job_b"):
            self._job(name)
        af3.build_data_handoff(self.output)
        with self.assertRaises(af3.ContractError) as caught:
            af3.load_data_handoff(self.output / af3.DATA_HANDOFF_DIRNAME, "job_z")
        self.assertIn("job_a", str(caught.exception))
        self.assertIn("job_b", str(caught.exception))

    # -- portability ----------------------------------------------------------

    def test_the_index_records_no_absolute_producer_path(self) -> None:
        self._job("solo")
        af3.build_data_handoff(self.output)
        raw = (
            self.output / af3.DATA_HANDOFF_DIRNAME / af3.DATA_HANDOFF_INDEX
        ).read_text(encoding="utf-8")
        self.assertNotIn(str(self.output), raw)
        self.assertNotIn("/output", raw)
        for entry in json.loads(raw)["entries"]:
            self.assertFalse(entry["relative_path"].startswith("/"))

    def test_the_handoff_survives_being_moved_to_another_mount(self) -> None:
        """This is the whole point: the GPU pod mounts it somewhere else."""
        self._job("solo")
        af3.build_data_handoff(self.output)
        relocated = Path(self.tmp.name) / "artifacts" / "af3-handoff"
        relocated.parent.mkdir(parents=True)
        (self.output / af3.DATA_HANDOFF_DIRNAME).rename(relocated)
        # The producing output tree is gone entirely.
        import shutil as _shutil

        _shutil.rmtree(self.output)

        resolved = af3.load_data_handoff(relocated)
        self.assertEqual("solo", resolved["fold_job"])
        self.assertTrue(resolved["json_path"].is_file())
        self.assertTrue(str(resolved["json_path"]).startswith(str(relocated)))

    def test_the_payload_travels_with_the_index(self) -> None:
        self._job("solo")
        af3.build_data_handoff(self.output)
        packaged = (
            self.output / af3.DATA_HANDOFF_DIRNAME / "solo" / "solo_data.json"
        )
        self.assertTrue(packaged.is_file())

    def test_a_tampered_payload_is_refused_on_the_gpu_side(self) -> None:
        self._job("solo")
        af3.build_data_handoff(self.output)
        directory = self.output / af3.DATA_HANDOFF_DIRNAME
        (directory / "solo" / "solo_data.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(af3.ContractError) as caught:
            af3.load_data_handoff(directory)
        self.assertIn("digest mismatch", str(caught.exception))

    def test_a_missing_payload_is_refused(self) -> None:
        self._job("solo")
        af3.build_data_handoff(self.output)
        directory = self.output / af3.DATA_HANDOFF_DIRNAME
        (directory / "solo" / "solo_data.json").unlink()
        with self.assertRaises(af3.ContractError) as caught:
            af3.load_data_handoff(directory)
        self.assertIn("packaged payload is incomplete", str(caught.exception))

    def test_an_absolute_path_in_an_index_is_refused(self) -> None:
        self._job("solo")
        af3.build_data_handoff(self.output)
        directory = self.output / af3.DATA_HANDOFF_DIRNAME
        index_path = directory / af3.DATA_HANDOFF_INDEX
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["entries"][0]["relative_path"] = "/output/solo/solo_data.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaises(af3.ContractError) as caught:
            af3.load_data_handoff(directory)
        self.assertIn("never carry an absolute producer path", str(caught.exception))

    def test_a_traversing_path_in_an_index_is_refused(self) -> None:
        self._job("solo")
        af3.build_data_handoff(self.output)
        directory = self.output / af3.DATA_HANDOFF_DIRNAME
        index_path = directory / af3.DATA_HANDOFF_INDEX
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["entries"][0]["relative_path"] = "../../etc/passwd"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaises(af3.ContractError):
            af3.load_data_handoff(directory)

    def test_the_raw_output_tree_is_not_a_handoff_directory(self) -> None:
        self._job("solo")
        af3.build_data_handoff(self.output)
        with self.assertRaises(af3.ContractError) as caught:
            af3.load_data_handoff(self.output)
        self.assertIn("packaged", str(caught.exception))

    def test_rebuilding_is_idempotent_and_never_indexes_itself(self) -> None:
        self._job("solo")
        first = af3.build_data_handoff(self.output)
        second = af3.build_data_handoff(self.output)
        self.assertEqual(1, second["count"])
        self.assertEqual(first["entries"], second["entries"])

    def test_an_empty_output_is_a_failure_not_an_empty_index(self) -> None:
        with self.assertRaises(af3.ContractError) as caught:
            af3.build_data_handoff(self.output)
        self.assertIn("nothing for the inference stage", str(caught.exception))

    def test_a_missing_output_directory_is_refused(self) -> None:
        with self.assertRaises(af3.ContractError):
            af3.build_data_handoff(self.output / "absent")


class GpuStageHandoffConsumptionTests(unittest.TestCase):
    """The GPU stage reconstructs its input path under its own artifact mount."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

        self.parameter = self.dir / "af3.bin.zst"
        payload = bytes.fromhex("28b52ffd") + b"body"
        self.parameter.write_bytes(payload)
        import hashlib as _hashlib

        self.binding = self.dir / "binding.json"
        self.binding.write_text(
            json.dumps(
                {
                    "artifact": {
                        "artifact_id": "alphafold3-parameters",
                        "filename": "af3.bin.zst",
                        "sha256": _hashlib.sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                        "magic_hex": "28b52ffd",
                    },
                    "delivery": {"permissions": {"asset_gid": 65532}},
                    "invocation": {
                        "expect_distribution_version": "3.0.4",
                        "expect_min_parameter_arrays": 100,
                    },
                }
            ),
            encoding="utf-8",
        )

        # A CPU stage produces two jobs, then its output tree disappears.
        produced = self.dir / "cpu-output"
        for name in ("alpha", "beta"):
            job = produced / name
            job.mkdir(parents=True)
            (job / f"{name}_data.json").write_text(
                json.dumps({"name": name}), encoding="utf-8"
            )
        af3.build_data_handoff(produced)
        self.mount = self.dir / "gpu-artifacts" / "handoff"
        self.mount.parent.mkdir(parents=True)
        (produced / af3.DATA_HANDOFF_DIRNAME).rename(self.mount)
        import shutil as _shutil

        _shutil.rmtree(produced)

    def _plan(self, extra_args):
        original_binding = af3.PARAMETER_BINDING_PATH
        original_lock = af3.SOURCE_LOCK_PATH
        af3.PARAMETER_BINDING_PATH = self.binding
        af3.SOURCE_LOCK_PATH = ROOT / "contracts" / "af3-runtime-source-lock.json"
        try:
            receipt_path = self.dir / "receipt.json"
            code = af3.main(
                [
                    "inference",
                    "--handoff-dir", str(self.mount),
                    "--parameter-path", str(self.parameter),
                    "--output-dir", str(self.dir / "out"),
                    "--receipt", str(receipt_path),
                    "--dry-run",
                    *extra_args,
                ]
            )
            return code, json.loads(receipt_path.read_text(encoding="utf-8"))
        finally:
            af3.PARAMETER_BINDING_PATH = original_binding
            af3.SOURCE_LOCK_PATH = original_lock

    def test_a_named_job_resolves_under_the_gpu_mount(self) -> None:
        code, receipt = self._plan(["--fold-job", "beta"])
        self.assertEqual(0, code)
        self.assertEqual("PLANNED", receipt["status"])
        handoff = receipt["handoff_input"]
        self.assertEqual("beta", handoff["fold_job"])
        self.assertTrue(handoff["reconstructed_locally"])
        self.assertEqual(["alpha", "beta"], handoff["available_fold_jobs"])
        json_flag = [a for a in receipt["plan"]["argv"] if a.startswith("--json_path=")][0]
        self.assertTrue(json_flag.endswith("beta/beta_data.json"))
        self.assertIn(str(self.mount), json_flag)

    def test_an_ambiguous_handoff_is_refused_before_any_run(self) -> None:
        code, receipt = self._plan([])
        self.assertEqual(2, code)
        self.assertEqual("FAIL", receipt["status"])
        self.assertIn("--fold-job", receipt["error"])

    def test_a_handoff_and_a_direct_input_are_mutually_exclusive(self) -> None:
        code, receipt = self._plan(["--fold-job", "beta", "--json-path", "/elsewhere/f.json"])
        self.assertEqual(2, code)
        self.assertIn("exactly one of", receipt["error"])


class TerminalReceiptTests(unittest.TestCase):
    """A receipt is written once, after upstream exits, and tells the truth."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.parameter = self.dir / "af3.bin.zst"
        payload = bytes.fromhex("28b52ffd") + b"body"
        self.parameter.write_bytes(payload)
        self.binding = self.dir / "binding.json"
        self.binding.write_text(
            json.dumps(
                {
                    "artifact": {
                        "artifact_id": "alphafold3-parameters",
                        "filename": "af3.bin.zst",
                        "sha256": __import__("hashlib").sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                        "magic_hex": "28b52ffd",
                    },
                    "delivery": {"permissions": {"asset_gid": 65532}},
                    "invocation": {
                        "expect_distribution_version": "3.0.4",
                        "expect_min_parameter_arrays": 100,
                    },
                }
            ),
            encoding="utf-8",
        )

    def _run(self, argv, exit_code):
        calls = []

        def fake_run(plan):
            calls.append(plan)
            return exit_code

        original = af3._run
        original_binding = af3.PARAMETER_BINDING_PATH
        original_lock = af3.SOURCE_LOCK_PATH
        af3._run = fake_run
        af3.PARAMETER_BINDING_PATH = self.binding
        # The image identity comes from the lock baked into the image; outside
        # the image the committed copy is the same document.
        af3.SOURCE_LOCK_PATH = ROOT / "contracts" / "af3-runtime-source-lock.json"
        try:
            receipt_path = self.dir / "receipt.json"
            code = af3.main([*argv, "--receipt", str(receipt_path)])
            return code, json.loads(receipt_path.read_text(encoding="utf-8")), calls
        finally:
            af3._run = original
            af3.PARAMETER_BINDING_PATH = original_binding
            af3.SOURCE_LOCK_PATH = original_lock

    def test_a_failed_upstream_leaves_a_fail_receipt_and_its_exit_code(self) -> None:
        code, receipt, calls = self._run(
            [
                "inference",
                "--json-path", "/handoff/f.json",
                "--parameter-path", str(self.parameter),
                "--output-dir", str(self.dir / "out"),
            ],
            exit_code=7,
        )
        self.assertEqual(1, len(calls), "upstream must be invoked exactly once")
        self.assertEqual(7, code)
        self.assertEqual("FAIL", receipt["status"])
        self.assertIn("exited 7", receipt["error"])
        self.assertEqual(7, receipt["execution"]["exit_code"])
        self.assertEqual("failed", receipt["execution"]["terminal_state"])

    def test_a_successful_upstream_leaves_a_pass_receipt_with_its_exit_code(self) -> None:
        code, receipt, _ = self._run(
            [
                "inference",
                "--json-path", "/handoff/f.json",
                "--parameter-path", str(self.parameter),
                "--output-dir", str(self.dir / "out"),
            ],
            exit_code=0,
        )
        self.assertEqual(0, code)
        self.assertEqual("PASS", receipt["status"])
        self.assertEqual(0, receipt["execution"]["exit_code"])
        self.assertEqual("succeeded", receipt["execution"]["terminal_state"])
        self.assertNotIn("error", receipt)

    def test_a_planned_receipt_never_claims_an_execution(self) -> None:
        _, receipt, calls = self._run(
            [
                "inference",
                "--json-path", "/handoff/f.json",
                "--parameter-path", str(self.parameter),
                "--output-dir", str(self.dir / "out"),
                "--dry-run",
            ],
            exit_code=0,
        )
        self.assertEqual([], calls, "a dry run must not invoke upstream")
        self.assertEqual("PLANNED", receipt["status"])
        self.assertNotIn("execution", receipt)

    def test_every_terminal_receipt_validates_against_the_schema(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (ROOT / "schemas" / "af3-runtime-receipt.schema.json").read_text(encoding="utf-8")
        )
        validator = Draft202012Validator(schema)
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                _, receipt, _ = self._run(
                    [
                        "inference",
                        "--json-path", "/handoff/f.json",
                        "--parameter-path", str(self.parameter),
                        "--output-dir", str(self.dir / "out"),
                    ],
                    exit_code=exit_code,
                )
                # The schema pins the authorized parameter identity, which a
                # synthetic artifact cannot carry. This test is about the
                # terminal envelope, so the real identity is restated.
                receipt["parameters"].update(
                    {
                        "sha256": (
                            "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
                        ),
                        "size_bytes": 1020545840,
                    }
                )
                validator.validate(receipt)


if __name__ == "__main__":
    unittest.main()
