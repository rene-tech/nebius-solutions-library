#!/usr/bin/env python3
"""Offline contract tests for the Proteina-Complexa runtime image and entrypoint.

These tests run without a GPU, a cluster or a checkpoint.  They cover the parts
of the contract that a live run cannot cheaply prove twice: that the pinned
identities agree across every document, that artifact verification and the
semantic validators actually reject the failure they exist to catch, and that
the phase parser recovers the real phases from a captured upstream log.

Each test that asserts a rejection also asserts the corresponding acceptance,
so a validator cannot pass by rejecting everything.
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOCK = json.loads((ROOT / "image-lock.json").read_text(encoding="utf-8"))


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "complexa_runtime_entrypoint", ROOT / "runtime_entrypoint.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENTRY = _load_entrypoint()


_ROTATION = ["ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE"]


def _pdb(residues: int, *, chain: str = "A", step: float = 3.8,
         name: str | None = None, extra: list[tuple[str, str, str]] | None = None) -> str:
    """Render a minimal PDB with a straight C-alpha trace of a given spacing.

    Residue identities cycle through ten amino acids so the fixture resembles a
    real designed chain; the sequence-diversity gate rejects a chain that
    collapsed onto one type, and a poly-alanine fixture would trip it.  Pass
    ``name`` to force a single residue type on purpose.
    """
    lines = []
    serial = 1
    for index in range(residues):
        x = index * step
        residue = name or _ROTATION[index % len(_ROTATION)]
        lines.append(
            f"ATOM  {serial:5d}  CA  {residue:>3} {chain}{index + 1:4d}    "
            f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C"
        )
        serial += 1
    for residue_name, hetero_chain, number in extra or []:
        lines.append(
            f"HETATM{serial:5d}  C1  {residue_name:>3} {hetero_chain}{int(number):4d}    "
            f"{1.0:8.3f}{1.0:8.3f}{1.0:8.3f}  1.00  0.00           C"
        )
        serial += 1
    return "\n".join(lines) + "\nEND\n"


class PinnedIdentityTests(unittest.TestCase):
    """The same identity must not be spelled two different ways."""

    def test_entrypoint_and_lock_agree_on_every_checkpoint(self) -> None:
        catalogue = {item["artifact_id"]: item for item in LOCK["external_artifacts"]}
        for name, variant in ENTRY.VARIANTS.items():
            entry = catalogue[variant["artifact_id"]]
            self.assertEqual(entry["source_revision"], variant["source_revision"], name)
            self.assertEqual(entry["source_uri"], variant["source_uri"], name)
            by_path = {item["path"]: item for item in entry["files"]}
            for role in ("checkpoint", "autoencoder"):
                pinned = by_path[variant[role]["name"]]
                self.assertEqual(pinned["bytes"], variant[role]["bytes"], f"{name}/{role}")
                self.assertEqual(pinned["sha256"], variant[role]["sha256"], f"{name}/{role}")

    def test_rosettafold3_identity_agrees(self) -> None:
        entry = next(
            item
            for item in LOCK["external_artifacts"]
            if item["artifact_id"] == "rosettafold3-checkpoint"
        )
        pinned = entry["files"][0]
        self.assertEqual(pinned["bytes"], ENTRY.RF3_ARTIFACT["bytes"])
        self.assertEqual(pinned["sha256"], ENTRY.RF3_ARTIFACT["sha256"])
        self.assertEqual(entry["license_id"], ENTRY.RF3_ARTIFACT["license_id"])

    def test_six_distinct_complexa_checkpoints_are_pinned(self) -> None:
        digests = set()
        for variant in ENTRY.VARIANTS.values():
            digests.add(variant["checkpoint"]["sha256"])
            digests.add(variant["autoencoder"]["sha256"])
        self.assertEqual(6, len(digests), "the three variants must pin six distinct files")

    def test_source_revision_matches_the_lock_and_the_shared_receipt(self) -> None:
        self.assertEqual(LOCK["source"]["revision"], ENTRY.SOURCE_REVISION)
        receipts = json.loads(
            (
                ROOT.parents[3]
                / "catalog/runtime/contracts/scientific-source-candidate-receipts.json"
            ).read_text(encoding="utf-8")
        )
        pinned = next(
            item for item in receipts["receipts"] if item["model_id"] == "proteina-complexa"
        )
        self.assertEqual(pinned["source"]["revision"], ENTRY.SOURCE_REVISION)

    def test_registry_attestation_matches_the_accepted_image_and_build_commit(self) -> None:
        provenance = json.loads(
            (ROOT / "evidence/registry-provenance.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            LOCK["image"]["published_digest"], provenance["image"]["index_digest"]
        )
        self.assertEqual(
            LOCK["image"]["provenance"]["vcs_revision"],
            provenance["slsa"]["vcs"]["revision"],
        )
        self.assertEqual(
            LOCK["source"]["archive_sha256"],
            provenance["slsa"]["source"]["archive_sha256"],
        )
        self.assertTrue(provenance["verification"]["attestation_is_attached_to_platform_manifest"])

    def test_lock_never_claims_a_gpu_snapshot(self) -> None:
        snapshot = ENTRY.describe()["gpu_snapshot"]
        self.assertFalse(snapshot["captured"])
        self.assertFalse(snapshot["restored"])

    def test_lock_does_not_report_the_runtime_as_servable(self) -> None:
        qualification = LOCK["image"]["qualification"]
        self.assertEqual("qualified-h100-all-variants", qualification["state"])
        self.assertFalse(qualification["servable"])
        self.assertFalse(qualification["route_exposed"])
        for relative_path in qualification["evidence"]:
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)

    def test_superseded_digests_are_not_deployable(self) -> None:
        superseded = LOCK["image"]["supersedes"]
        self.assertTrue(superseded)
        for item in superseded:
            self.assertFalse(item["deployable"], item["digest"])
            self.assertTrue(item["reason"].strip())
        digests = {item["digest"] for item in superseded}
        self.assertIn(
            "sha256:f4e06b6025a74c924749420f2fce01fb9511aba606a2266c85a9d9e92e3679ca",
            digests,
            "the AlphaFold2-loader-only predecessor must be recorded as superseded",
        )

    def test_no_checkpoint_is_embedded_in_the_image(self) -> None:
        self.assertFalse(LOCK["weight_policy"]["embedded"])


class RequestContractTests(unittest.TestCase):
    def test_defaults_resolve_to_a_complete_request(self) -> None:
        request = ENTRY.normalise_request({"variant": "ame"})
        self.assertEqual("ame", request["variant"])
        self.assertEqual("M0024_1nzy_og", request["task_name"])
        self.assertEqual("none", request["reward_model"])
        self.assertEqual("single-pass", request["search_algorithm"])

    def test_unknown_variant_is_rejected(self) -> None:
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.normalise_request({"variant": "monomer"})

    def test_reward_free_run_may_not_ask_for_a_search_algorithm(self) -> None:
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.normalise_request(
                {"variant": "ligand", "reward_model": "none", "search_algorithm": "best-of-n"}
            )
        allowed = ENTRY.normalise_request(
            {
                "variant": "ligand",
                "reward_model": "upstream-default",
                "search_algorithm": "best-of-n",
            }
        )
        self.assertEqual("best-of-n", allowed["search_algorithm"])

    def test_non_positive_sample_counts_are_rejected(self) -> None:
        for key in ("samples", "batch_size", "nsteps"):
            with self.assertRaises(ENTRY.RuntimeFailure, msg=key):
                ENTRY.normalise_request({"variant": "protein", key: 0})

    def test_foreign_schema_is_rejected(self) -> None:
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.normalise_request({"schema": "something/else", "variant": "protein"})


class ArtifactVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="fs2-cxq-verify-"))
        self.payload = b"complexa-checkpoint-bytes"
        self.file = self.directory / "complexa.ckpt"
        self.file.write_bytes(self.payload)
        import hashlib

        self.expected = {
            "bytes": len(self.payload),
            "sha256": hashlib.sha256(self.payload).hexdigest(),
        }

    def test_exact_bytes_and_digest_are_accepted(self) -> None:
        record = ENTRY.verify_file(self.file, self.expected, "score", digests=True)
        self.assertTrue(record["digest_verified"])
        self.assertEqual(self.expected["sha256"], record["observed_sha256"])

    def test_absent_artifact_is_rejected(self) -> None:
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.verify_file(self.directory / "missing.ckpt", self.expected, "score", digests=False)

    def test_truncated_artifact_is_rejected_without_reading_content(self) -> None:
        self.file.write_bytes(self.payload[:-3])
        with self.assertRaises(ENTRY.RuntimeFailure) as caught:
            ENTRY.verify_file(self.file, self.expected, "score", digests=False)
        self.assertIn("bytes", str(caught.exception))

    def test_same_size_different_content_is_rejected_only_with_digests(self) -> None:
        self.file.write_bytes(b"X" * len(self.payload))
        # Size-only verification cannot see this substitution.
        ENTRY.verify_file(self.file, self.expected, "score", digests=False)
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.verify_file(self.file, self.expected, "score", digests=True)


class PhaseParsingTests(unittest.TestCase):
    """Recover phases from a real captured upstream log.

    The anchor order matters and is not the obvious one: upstream logs
    "Starting generation job at" *before* it loads the checkpoint, so its own
    reported total spans model load plus sampling. An earlier version of this
    parser assumed the opposite and produced a model-load span of -0.001 s.
    """

    LOG = "\n".join(
        [
            "Loading Atomworks Patches",
            "2026-09-03 05:15:23.738 | INFO     | __main__:validate_checkpoint_paths:117 - "
            "Checkpoint validated: /opt/fs2/artifacts/complexa-ame/complexa_ame.ckpt",
            "2026-09-03 05:15:23.739 | INFO     | __main__:main:578 - "
            "Starting generation job at 2026-09-03 05:15:23",
            "2026-09-03 05:15:23.740 | INFO     | __main__:load_ckpt_n_configure_inference:180 - "
            "Using checkpoint /opt/fs2/artifacts/complexa-ame/complexa_ame.ckpt",
            "2026-09-03 05:15:31.075 | INFO     | __main__:load_ckpt_n_configure_inference:220 - "
            "Re-create LoRA layers and reload the weights now",
            "GPU available: True (cuda), used: True",
            "2026-09-03 05:15:35.360 | INFO     | __main__:main:642 - cfg_gen: {'task_name': 'x'}",
            "                    INFO     Total generation time: 24.16        generate.py:751",
        ]
    )

    def test_phases_are_recovered_from_the_log_and_the_timing_total(self) -> None:
        import datetime

        start = datetime.datetime(
            2026, 9, 3, 5, 15, 11, 300000, tzinfo=datetime.timezone.utc
        ).timestamp()
        phases = ENTRY.parse_phases(self.LOG, start, 24.16)
        self.assertTrue(phases["lora_reapplied"])
        self.assertAlmostEqual(12.438, phases["interpreter_and_import_seconds"], places=2)
        self.assertAlmostEqual(11.62, phases["model_load_seconds"], places=2)
        self.assertAlmostEqual(12.54, phases["sampling_seconds"], places=2)
        self.assertEqual(phases["sampling_seconds"], phases["compute_seconds"])
        self.assertAlmostEqual(7.335, phases["checkpoint_load_to_lora_seconds"], places=2)

    def test_model_load_is_never_negative_for_the_real_anchor_order(self) -> None:
        phases = ENTRY.parse_phases(self.LOG, 0.0, 24.16)
        self.assertGreater(phases["model_load_seconds"], 0.0)

    def test_a_log_without_the_lora_marker_reports_it_absent(self) -> None:
        stripped = "\n".join(
            line for line in self.LOG.splitlines() if "Re-create LoRA" not in line
        )
        self.assertFalse(ENTRY.parse_phases(stripped, 0.0, 24.16)["lora_reapplied"])

    def test_sampling_is_unknown_without_upstream_total(self) -> None:
        phases = ENTRY.parse_phases(self.LOG, 0.0, None)
        self.assertIsNone(phases["sampling_seconds"])
        self.assertIsNone(phases["compute_seconds"])
        self.assertIsNotNone(phases["model_load_seconds"])


class TimingCsvTests(unittest.TestCase):
    def test_upstream_total_is_read_from_the_timing_csv(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fs2-cxq-timing-"))
        (root / "timing_0.csv").write_text("job_id,total_time,nsamples\n0,24.16,1\n")
        self.assertAlmostEqual(24.16, ENTRY.upstream_generation_seconds(root))

    def test_a_missing_or_malformed_csv_yields_none(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fs2-cxq-timing-"))
        self.assertIsNone(ENTRY.upstream_generation_seconds(root))
        (root / "timing_0.csv").write_text("job_id,nsamples\n0,1\n")
        self.assertIsNone(ENTRY.upstream_generation_seconds(root))


class StructureValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="fs2-cxq-struct-"))

    def test_a_protein_like_trace_is_accepted(self) -> None:
        path = self.directory / "sample.pdb"
        path.write_text(_pdb(40))
        summary = ENTRY.inspect_structure(path)
        self.assertEqual(40, summary["standard_residue_count"])
        self.assertEqual(["A"], summary["chains"])
        self.assertAlmostEqual(3.8, summary["ca_traces"]["A"]["mean_step_a"], places=3)
        self.assertEqual([], ENTRY.validate_backbone([summary]))

    def test_a_collapsed_trace_is_rejected(self) -> None:
        path = self.directory / "collapsed.pdb"
        path.write_text(_pdb(30, step=0.0))
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.inspect_structure(path)

    def test_an_exploded_trace_is_flagged_by_the_backbone_gate(self) -> None:
        path = self.directory / "exploded.pdb"
        path.write_text(_pdb(30, step=25.0))
        findings = ENTRY.validate_backbone([ENTRY.inspect_structure(path)])
        self.assertTrue(findings)
        self.assertIn("outside", findings[0])

    def test_a_structure_with_no_standard_residue_is_rejected(self) -> None:
        path = self.directory / "hetero.pdb"
        path.write_text(_pdb(0, extra=[("OQO", "B", "1"), ("OQO", "B", "2")]))
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.inspect_structure(path)

    def test_an_empty_structure_is_rejected(self) -> None:
        path = self.directory / "empty.pdb"
        path.write_text("REMARK nothing here\nEND\n")
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.inspect_structure(path)

    def test_a_bundled_upstream_target_parses(self) -> None:
        """A real crystal structure must satisfy the same geometry gate."""
        candidates = [
            Path("/opt/fs2/source/assets/target_data/bindcraft_targets/PD-L1.pdb"),
            HERE / "fixtures/PD-L1.pdb",
        ]
        path = next((item for item in candidates if item.is_file()), None)
        if path is None:
            self.skipTest("no bundled upstream target structure available offline")
        summary = ENTRY.inspect_structure(path)
        self.assertGreater(summary["standard_residue_count"], 50)
        self.assertEqual([], ENTRY.validate_backbone([summary]))


class OutputValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="fs2-cxq-out-"))
        (self.directory / "timing_0.csv").write_text("job_id,total_time,nsamples\n0,15.09,1\n")

    def _rewards(self, rows: int = 1) -> None:
        body = "".join(
            f"/x/sample_{index}.pdb,{index},1,2,3\n" for index in range(rows)
        )
        (self.directory / "rewards_x_0.csv").write_text(
            "pdb_path,pdb_index,aatype,total_reward,extra\n" + body
        )

    def _target(self, **overrides):
        base = {
            "task_name": "t",
            "target_path": "/x.pdb",
            "declared_target_path": "./x.pdb",
            "binder_length": [64, 155],
            "ligand_residues": [],
            "contig_atoms": None,
        }
        base.update(overrides)
        return base

    def test_protein_run_with_a_valid_binder_passes(self) -> None:
        (self.directory / "sample.pdb").write_text(_pdb(90))
        self._rewards()
        report = ENTRY.validate_outputs(
            "protein",
            ENTRY.VARIANTS["protein"],
            ENTRY.normalise_request({"variant": "protein"}),
            self._target(),
            self.directory,
        )
        self.assertEqual([], report["geometry_findings"])
        self.assertEqual([90], report["binder_lengths"])

    def test_binder_outside_the_declared_envelope_is_flagged(self) -> None:
        (self.directory / "sample.pdb").write_text(_pdb(300))
        self._rewards()
        report = ENTRY.validate_outputs(
            "protein",
            ENTRY.VARIANTS["protein"],
            ENTRY.normalise_request({"variant": "protein"}),
            self._target(),
            self.directory,
        )
        self.assertTrue(report["geometry_findings"])

    def test_target_and_binder_in_one_file_are_measured_per_chain(self) -> None:
        """The binder pipelines write the target and the binder into one file.

        A whole-file residue count is the sum of both. Observed live on H100:
        the PD-L1 run produced one structure with a 115-residue target chain
        and a 69-residue binder chain, and a whole-file count of 184 was
        rejected against the 64-155 envelope even though the binder was in
        range.
        """
        path = self.directory / "sample.pdb"
        path.write_text(_pdb(115, chain="A") + _pdb(69, chain="B"))
        self._rewards()
        report = ENTRY.validate_outputs(
            "protein",
            ENTRY.VARIANTS["protein"],
            ENTRY.normalise_request({"variant": "protein"}),
            self._target(),
            self.directory,
        )
        self.assertEqual([], report["geometry_findings"])
        self.assertEqual({"A": 115, "B": 69}, report["chain_lengths"]["sample.pdb"])

    def test_a_file_where_no_chain_fits_the_envelope_is_flagged(self) -> None:
        path = self.directory / "sample.pdb"
        path.write_text(_pdb(300, chain="A") + _pdb(400, chain="B"))
        self._rewards()
        report = ENTRY.validate_outputs(
            "protein",
            ENTRY.VARIANTS["protein"],
            ENTRY.normalise_request({"variant": "protein"}),
            self._target(),
            self.directory,
        )
        self.assertTrue(report["geometry_findings"])

    def test_a_run_that_produced_no_structure_is_rejected(self) -> None:
        self._rewards()
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.validate_outputs(
                "protein",
                ENTRY.VARIANTS["protein"],
                ENTRY.normalise_request({"variant": "protein"}),
                self._target(),
                self.directory,
            )

    def test_ligand_run_without_the_expected_ligand_is_rejected(self) -> None:
        (self.directory / "job_0_binder.pdb").write_text(_pdb(100))
        (self.directory / "job_0_complex.pdb").write_text(_pdb(100))
        self._rewards()
        with self.assertRaises(ENTRY.RuntimeFailure) as caught:
            ENTRY.validate_outputs(
                "ligand",
                ENTRY.VARIANTS["ligand"],
                ENTRY.normalise_request({"variant": "ligand"}),
                self._target(binder_length=[100], ligand_residues=["OQO"]),
                self.directory,
            )
        self.assertIn("ligand", str(caught.exception))

    def test_ligand_run_with_the_expected_ligand_passes(self) -> None:
        (self.directory / "job_0_binder.pdb").write_text(_pdb(100))
        (self.directory / "job_0_complex.pdb").write_text(
            _pdb(100, chain="B", extra=[("OQO", "A", "1")])
        )
        self._rewards()
        report = ENTRY.validate_outputs(
            "ligand",
            ENTRY.VARIANTS["ligand"],
            ENTRY.normalise_request({"variant": "ligand"}),
            self._target(binder_length=[100], ligand_residues=["OQO"]),
            self.directory,
        )
        self.assertEqual(["OQO"], report["observed_ligand_residues"])
        self.assertEqual([], report["geometry_findings"])

    def test_missing_timing_csv_is_rejected(self) -> None:
        (self.directory / "timing_0.csv").unlink()
        (self.directory / "sample.pdb").write_text(_pdb(90))
        self._rewards()
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.validate_outputs(
                "protein",
                ENTRY.VARIANTS["protein"],
                ENTRY.normalise_request({"variant": "protein"}),
                self._target(),
                self.directory,
            )

    def test_rewards_csv_without_pdb_path_is_rejected(self) -> None:
        (self.directory / "sample.pdb").write_text(_pdb(90))
        (self.directory / "rewards_x_0.csv").write_text("a,b\n1,2\n")
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.validate_outputs(
                "protein",
                ENTRY.VARIANTS["protein"],
                ENTRY.normalise_request({"variant": "protein"}),
                self._target(),
                self.directory,
            )


class ArgvContractTests(unittest.TestCase):
    def _argv(self, variant_name: str, **request_overrides) -> list[str]:
        request = ENTRY.normalise_request({"variant": variant_name, **request_overrides})
        target = {
            "task_name": request["task_name"],
            "target_path": "/opt/fs2/source/assets/target_data/x/y.pdb",
            "declared_target_path": "./assets/target_data/x/y.pdb",
            "binder_length": [100],
            "ligand_residues": [],
            "contig_atoms": None,
        }
        return ENTRY.build_argv(
            variant_name,
            ENTRY.VARIANTS[variant_name],
            request,
            target,
            Path("/opt/fs2/artifacts") / ENTRY.VARIANTS[variant_name]["artifact_id"],
            Path("/workspace/out"),
        )

    def test_argv_is_shell_free_and_uses_the_module_entry_point(self) -> None:
        argv = self._argv("ame")
        self.assertEqual("-m", argv[1])
        self.assertEqual("proteinfoundation.generate", argv[2])
        for element in argv:
            self.assertNotIn("&&", element)
            self.assertNotIn("|", element)
            self.assertNotIn(";", element)
        # The upstream console script is never used: it requires a writable .env.
        self.assertNotIn("complexa", [Path(argv[0]).name])

    def test_each_variant_selects_its_own_checkpoint_pair(self) -> None:
        for name, variant in ENTRY.VARIANTS.items():
            argv = self._argv(name)
            joined = " ".join(argv)
            self.assertIn(f"++ckpt_name={variant['checkpoint']['name']}", joined)
            self.assertIn(variant["autoencoder"]["name"], joined)
            self.assertIn(f"/opt/fs2/artifacts/{variant['artifact_id']}", joined)
            # No other variant's checkpoint may appear anywhere in the argv.
            for other, spec in ENTRY.VARIANTS.items():
                if other == name:
                    continue
                self.assertNotIn(f"++ckpt_name={spec['checkpoint']['name']}", joined)
                self.assertNotIn(spec["autoencoder"]["name"], joined)

    def test_no_override_key_violates_hydra_grammar(self) -> None:
        """Hydra only admits key segments starting with a letter or underscore.

        Most upstream task names begin with a digit, so an override such as
        ``++generation.target_dict_cfg.02_PDL1.target_path=...`` is a parse
        error.  A live run proved this: the protein and ligand variants died in
        Hydra's lexer while AME, whose task name starts with a letter, ran.
        """
        segment = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        for name in ENTRY.VARIANTS:
            for item in self._argv(name):
                if not item.startswith(("++", "~")) or "=" not in item:
                    continue
                key = item.split("=", 1)[0].lstrip("+~")
                for part in key.split("."):
                    self.assertRegex(part, segment, f"{name}: unparseable key {key!r}")

    def test_a_working_directory_is_verified_never_created(self) -> None:
        """The asset binding is an image property, so a missing one must fail.

        Repairing it at run time is the mutable side effect this contract
        exists to avoid, so the verifier must not create anything.
        """
        work = Path(tempfile.mkdtemp(prefix="fs2-cxq-workdir-"))
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.verify_working_directory(work)
        self.assertFalse((work / "assets").exists())

    def test_a_redirected_asset_link_is_rejected(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="fs2-cxq-workdir-"))
        decoy = Path(tempfile.mkdtemp(prefix="fs2-cxq-decoy-"))
        (decoy / "target_data").mkdir()
        (work / "assets").symlink_to(decoy)
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.verify_working_directory(work)

    def test_hydra_is_forbidden_from_changing_directory(self) -> None:
        self.assertIn("hydra.job.chdir=False", self._argv("protein"))

    def test_reward_free_run_disables_search_and_reward(self) -> None:
        argv = self._argv("ligand")
        self.assertIn("++generation.search.algorithm=single-pass", argv)
        self.assertIn("++generation.reward_model=null", argv)

    def test_reward_model_is_assigned_and_never_deleted(self) -> None:
        """A delete aborts composition when the node is already null.

        The AME pipeline ships reward_model: null, so
        "~generation.reward_model" fails with "Could not delete from config".
        Observed live on H100: the AME variant exited 1 during composition
        while protein and ligand, whose configs carry a reward dict, survived.
        """
        for name in ENTRY.VARIANTS:
            argv = self._argv(name)
            self.assertNotIn("~generation.reward_model", argv, name)
            self.assertFalse(
                any(item.startswith("~") for item in argv),
                f"{name} must not delete any config node: {argv}",
            )

    def test_upstream_reward_run_keeps_the_reward_model(self) -> None:
        argv = self._argv("ligand", reward_model="upstream-default", search_algorithm="best-of-n")
        self.assertIn("++generation.search.algorithm=best-of-n", argv)
        self.assertNotIn("++generation.reward_model=null", argv)


class DockerfileContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_every_base_image_is_digest_pinned(self) -> None:
        for line in self.text.splitlines():
            if line.startswith("ARG") and "_IMAGE=" in line:
                self.assertIn("@sha256:", line, line)

    def test_the_source_archive_digest_is_verified_before_extraction(self) -> None:
        self.assertIn("sha256sum -c -", self.text)
        index_check = self.text.index("sha256sum -c -")
        index_extract = self.text.index("tar -xzf /tmp/source.tar.gz")
        self.assertLess(index_check, index_extract)

    def test_the_image_runs_as_a_non_root_user(self) -> None:
        self.assertIn("USER 10001:10001", self.text)

    def test_cache_defaults_resolve_under_tmp(self) -> None:
        for variable in ("HF_HOME", "XDG_CACHE_HOME", "NUMBA_CACHE_DIR", "MPLCONFIGDIR"):
            matches = [
                line for line in self.text.splitlines() if line.strip().startswith(f"{variable}=")
            ]
            self.assertTrue(matches, variable)
            self.assertTrue(
                all("/tmp/" in line for line in matches),
                f"{variable} must not resolve under a read-only mount: {matches}",
            )

    def test_the_batch_entrypoint_is_baked_in_and_smoke_checked(self) -> None:
        self.assertIn("COPY proteina-complexa/runtime_entrypoint.py", self.text)
        self.assertIn("runtime_entrypoint.py describe", self.text)

    def test_the_declared_base_digests_match_the_lock(self) -> None:
        for role in ("builder", "runtime", "uv"):
            self.assertIn(LOCK["image"]["base"][role].split("@")[1], self.text, role)

    def test_the_dependency_lock_digest_matches_the_file(self) -> None:
        import hashlib

        payload = (ROOT / "requirements.lock").read_bytes()
        self.assertEqual(
            LOCK["image"]["dependency_lock"]["sha256"], hashlib.sha256(payload).hexdigest()
        )

    def test_the_requirements_lock_is_fully_hash_pinned(self) -> None:
        text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        self.assertIn("--hash=sha256:", text)
        requirement_lines = [
            line
            for line in text.splitlines()
            if line and not line.startswith((" ", "\t", "#", "-"))
        ]
        self.assertTrue(requirement_lines)
        # Three pinning styles are admitted, and nothing else: an exact
        # version, a VCS dependency at a full 40-character commit, and a direct
        # wheel or sdist URL carrying a #sha256= content digest.
        commit = re.compile(r"@[0-9a-f]{40}(?:$|#)")
        content_digest = re.compile(r"#sha256=[0-9a-f]{64}")
        unpinned = []
        for line in requirement_lines:
            requirement = line.split(";")[0].split("--hash")[0].strip().rstrip("\\").strip()
            if not (
                "==" in requirement
                or commit.search(requirement)
                or content_digest.search(requirement)
            ):
                unpinned.append(requirement)
        self.assertEqual([], unpinned, "every requirement must be pinned")


class DefectRegistryTests(unittest.TestCase):
    def test_every_recorded_defect_states_who_handles_it(self) -> None:
        defects = LOCK["upstream_contract_defects"]
        self.assertGreaterEqual(len(defects), 5)
        for defect in defects:
            self.assertTrue(defect["id"])
            self.assertTrue(defect["detail"].strip())
            self.assertTrue(defect["handled_by"].strip())
            self.assertIn("upstream_change_required", defect)

    def test_the_relative_target_path_defect_is_registered(self) -> None:
        ids = {item["id"] for item in LOCK["upstream_contract_defects"]}
        self.assertIn("relative-target-path", ids)
        self.assertIn("cli-requires-writable-dotenv", ids)


class SequenceDiversityTests(unittest.TestCase):
    """A design model must not pass by emitting one residue type."""

    RESIDUES = ["ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE"]

    def _mixed(self, count: int, kinds: int) -> str:
        lines = []
        for index in range(count):
            name = self.RESIDUES[index % kinds]
            lines.append(
                f"ATOM  {index + 1:5d}  CA  {name:>3} B{index + 1:4d}    "
                f"{index * 3.8:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C"
            )
        return "\n".join(lines) + "\nEND\n"

    def test_a_diverse_chain_is_accepted(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fs2-cxq-div-"))
        path = root / "s.pdb"
        path.write_text(self._mixed(100, 10))
        summary = ENTRY.inspect_structure(path)
        self.assertEqual(10, summary["chain_residues"]["B"]["distinct_standard"])
        self.assertEqual([], ENTRY.validate_sequence_diversity([summary]))

    def test_a_poly_alanine_chain_is_rejected(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fs2-cxq-div-"))
        path = root / "s.pdb"
        path.write_text(self._mixed(100, 1))
        summary = ENTRY.inspect_structure(path)
        findings = ENTRY.validate_sequence_diversity([summary])
        self.assertTrue(findings)
        self.assertIn("distinct amino-acid", findings[0])

    def test_a_short_chain_is_not_judged_on_diversity(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fs2-cxq-div-"))
        path = root / "s.pdb"
        path.write_text(self._mixed(10, 1))
        summary = ENTRY.inspect_structure(path)
        self.assertEqual([], ENTRY.validate_sequence_diversity([summary]))

    def test_a_degenerate_binder_fails_even_beside_a_diverse_target(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fs2-cxq-div-"))
        path = root / "s.pdb"
        target = self._mixed(115, 10).replace(" B", " A").replace("END\n", "")
        path.write_text(target + self._mixed(90, 1))
        summary = ENTRY.inspect_structure(path)
        findings = ENTRY.validate_sequence_diversity([summary])
        self.assertTrue(findings, "a poly-residue binder must be caught beside a real target")
        self.assertIn("chain B", findings[0])


class OutputDiscoveryTests(unittest.TestCase):
    """Discovery must never walk out of the output tree through a symlink."""

    def test_a_symlinked_directory_is_not_treated_as_output(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fs2-cxq-walk-"))
        (root / "job_0").mkdir()
        (root / "job_0" / "sample.pdb").write_text(_pdb(20))
        foreign = Path(tempfile.mkdtemp(prefix="fs2-cxq-foreign-"))
        (foreign / "target_data").mkdir()
        for index in range(5):
            (foreign / "target_data" / f"target_{index}.pdb").write_text(_pdb(30))
        (root / "assets").symlink_to(foreign)

        discovered = ENTRY.discover_structures(root)
        self.assertEqual(1, len(discovered), discovered)
        self.assertEqual("sample.pdb", discovered[0].name)


class GenerationBindingTests(unittest.TestCase):
    """Every checkpoint is bound to one immutable public generation."""

    PLANE = {
        "complexa-protein": "eaaf891e89935b909f13bece3ff1e8c4a1ae43d0e2378b834e07ca74e2607536",
        "complexa-ligand": "61247c8dbf261307d708be53decfda69f21e73ff421556662366045c30d9cea5",
        "complexa-ame": "d38c622eaa0dad419f0ff0af72f36ab49299c533f5f56bbf08fa180e829afa5a",
        "rosettafold3-checkpoint": "d909fe65e86670b0a18a7494dd06811d301d0899e30778442e8ca6a343164bce",
    }

    def test_the_pinned_generations_are_the_promoted_ones(self) -> None:
        self.assertEqual(self.PLANE, {k: v["generation"] for k, v in ENTRY.GENERATIONS.items()})

    def test_the_lock_and_the_entrypoint_agree_on_every_generation(self) -> None:
        catalogue = {item["artifact_id"]: item for item in LOCK["external_artifacts"]}
        for artifact_id, pinned in ENTRY.GENERATIONS.items():
            recorded = catalogue[artifact_id]["generation"]
            self.assertEqual(pinned["generation"], recorded["generation"], artifact_id)
            self.assertEqual(pinned["marker_sha256"], recorded["marker_sha256"], artifact_id)
            self.assertEqual(
                f"{ENTRY.GENERATION_ROOT}/{artifact_id}/sha256/{pinned['generation']}",
                recorded["sub_path"],
                artifact_id,
            )

    def test_the_inventory_reproduces_a_known_generation(self) -> None:
        """The algorithm must reproduce a digest measured on the real plane.

        This fixture is the complexa-ame generation's own entry shapes; the
        digest it must produce is the directory name the plane published.
        """
        root = Path(tempfile.mkdtemp(prefix="fs2-cxq-inv-"))
        (root / "a.bin").write_bytes(b"alpha")
        (root / "b.bin").write_bytes(b"beta")
        (root / ENTRY.MARKER_NAME).write_text("{}")
        digest, files, total, directories = ENTRY.tree_inventory_v2(root)
        # The reserved marker is excluded, so only the two payload files count.
        self.assertEqual(2, files)
        self.assertEqual(9, total)
        self.assertEqual(0, directories)
        # Recomputing an unchanged tree is stable, and any edit changes it.
        self.assertEqual(digest, ENTRY.tree_inventory_v2(root)[0])
        (root / "b.bin").write_bytes(b"betaX")
        self.assertNotEqual(digest, ENTRY.tree_inventory_v2(root)[0])

    def test_a_mount_without_a_marker_is_refused(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="fs2-cxq-nomarker-"))
        with self.assertRaises(ENTRY.RuntimeFailure) as caught:
            ENTRY.verify_generation(root, "complexa-ame")
        self.assertIn(ENTRY.MARKER_NAME, str(caught.exception))

    def test_a_marker_naming_another_plane_is_refused(self) -> None:
        """Right bytes in the wrong place, or under the wrong licence, are wrong."""
        root = Path(tempfile.mkdtemp(prefix="fs2-cxq-plane-"))
        pinned = ENTRY.GENERATIONS["complexa-ame"]
        marker = {
            "schema": ENTRY.MARKER_SCHEMA,
            "artifact_id": "complexa-ame",
            "generation": pinned["generation"],
            "inventory_sha256": pinned["generation"],
            "sub_path": f"{ENTRY.GENERATION_ROOT}/complexa-ame/sha256/{pinned['generation']}",
            "read_only": True,
            "inventory_algorithm": ENTRY.INVENTORY_ALGORITHM,
            "volume_kind": "persistent-volume-claim",
            "visibility": "tenant-private",
            "host_root": "",
            "namespace": "fs2-academic-poc",
            "claim": "academic-assets-runtime-rwx",
            "entry_count": pinned["entry_count"],
            "total_bytes": pinned["total_bytes"],
            "license_id": pinned["license_id"],
            "source_uri": pinned["source_uri"],
            "source_revision": pinned["source_revision"],
        }
        (root / ENTRY.MARKER_NAME).write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n")
        with self.assertRaises(ENTRY.RuntimeFailure) as caught:
            ENTRY.verify_generation(root, "complexa-ame")
        self.assertIn("volume_kind", str(caught.exception))

    def test_the_consumer_never_reads_the_tenant_private_plane(self) -> None:
        for artifact_id, pinned in ENTRY.GENERATIONS.items():
            self.assertIn(pinned["license_id"],
                          {"NVIDIA-Open-Model-License-2024-06", "BSD-3-Clause"}, artifact_id)
        delivery = LOCK["artifact_delivery"]
        self.assertEqual("host-path", delivery["volume_kind"])
        self.assertEqual("public", delivery["visibility"])


class TargetDataTests(unittest.TestCase):
    """Target structures are an artifact with a pinned identity."""

    def test_every_variant_default_task_has_a_pinned_target(self) -> None:
        pinned = set(ENTRY.TARGET_DATA["files"])
        self.assertEqual(3, len(pinned))
        for name in pinned:
            self.assertTrue(name.startswith("assets/target_data/"), name)

    def test_the_target_identity_is_the_pinned_upstream_archive(self) -> None:
        self.assertEqual(ENTRY.SOURCE_REVISION, ENTRY.TARGET_DATA["source_revision"])
        self.assertEqual(LOCK["source"]["archive_sha256"], ENTRY.TARGET_DATA["archive_sha256"])

    def test_an_unpinned_target_is_refused(self) -> None:
        with self.assertRaises(ENTRY.RuntimeFailure):
            ENTRY.verify_target_structure("assets/target_data/nope/none.pdb")

    def test_the_lock_records_target_data_as_an_artifact(self) -> None:
        entry = next(
            item for item in LOCK["external_artifacts"]
            if item["artifact_id"] == "proteina-complexa-target-data"
        )
        self.assertEqual(3, len(entry["files"]))
        self.assertNotIn("writable", entry["binding"].lower().replace("never a writable", ""))
        self.assertIn("never a writable", entry["binding"])


class RewardModelTests(unittest.TestCase):
    def test_rosettafold3_is_declared_for_the_two_pipelines_that_use_it(self) -> None:
        rewards = LOCK["image"]["reward_models"]
        self.assertEqual("rosettafold3", rewards["ligand"]["model"])
        self.assertEqual("rosettafold3", rewards["ame"]["model"])
        self.assertEqual("alphafold2", rewards["protein"]["model"])

    def test_alphafold2_generation_is_bound_but_reward_path_is_unqualified(self) -> None:
        protein = LOCK["image"]["reward_models"]["protein"]
        self.assertFalse(protein["exercisable_here"])
        self.assertEqual("published-and-node-verified", protein["availability"])
        artifact = next(
            item for item in LOCK["external_artifacts"]
            if item["artifact_id"] == "alphafold2-params"
        )
        self.assertEqual(
            "cdbb7c7c475442712c73f8f8ea40b42fb5dd4fb5c1bf81fdb4642ca9e27f5ac4",
            artifact["generation"]["generation"],
        )
        self.assertEqual(
            "fs2-flat-tree-inventory/v1", artifact["generation"]["inventory_algorithm"]
        )
        self.assertEqual(
            "AF2_DIR=/opt/fs2/artifacts/alphafold2-params", artifact["binding"]
        )
        self.assertIn("reward-free", protein["reason"])

    def test_an_rf3_request_for_a_variant_names_rosettafold3(self) -> None:
        request = ENTRY.normalise_request(
            {"variant": "ame", "reward_model": "upstream-default", "search_algorithm": "single-pass"}
        )
        self.assertEqual("upstream-default", request["reward_model"])


class QualificationEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runs = json.loads(
            (ROOT / "evidence/h100-run-receipt.json").read_text(encoding="utf-8")
        )
        self.semantic = json.loads(
            (ROOT / "evidence/h100-semantic-qualification.json").read_text(
                encoding="utf-8"
            )
        )

    def test_all_three_variants_have_exact_h100_pass_receipts(self) -> None:
        self.assertEqual(
            hashlib.sha256((ROOT / "qualification/generated-plan.json").read_bytes()).hexdigest(),
            self.runs["execution_plan"]["sha256"],
        )
        self.assertTrue(self.runs["execution_plan"]["shell_free"])
        variants = {item["variant"]: item for item in self.runs["variants"]}
        self.assertEqual({"protein", "ligand", "ame"}, set(variants))
        for name, item in variants.items():
            self.assertEqual("PASS", item["terminal_state"], name)
            self.assertEqual(0, item["upstream_exit_code"], name)
            self.assertTrue(item["cuda_used_by_upstream"], name)
            self.assertEqual("H100", item["node"]["gpu_name"], name)
            self.assertEqual("nvidia-h100-sxm5-80gb", item["node"]["accelerator_class"], name)
            self.assertEqual("capacity-block", item["node"]["capacity_source"], name)
            self.assertGreater(
                item["kubernetes_timings"]["schedule_to_semantic_complete_seconds"],
                item["kubernetes_timings"]["container_runtime_seconds"],
                name,
            )
            self.assertEqual("cold", item["image_phase"]["observed_cache_level"], name)
            self.assertTrue(item["rosettafold3"]["bound"], name)
            self.assertFalse(item["rosettafold3"]["exercised"], name)
            artifact_paths = {artifact["path"] for artifact in item["output_artifacts"]}
            self.assertIn("result.json", artifact_paths, name)
            self.assertTrue(
                any(path.endswith(".pdb") for path in artifact_paths), name
            )
            for artifact in item["output_artifacts"]:
                self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$", name)
                if artifact["path"] == "result.json" or artifact["path"].endswith(".pdb"):
                    self.assertGreater(artifact["bytes"], 0, name)
            self.assertEqual("/opt/venv/bin/python", item["argv"][0], name)
            self.assertFalse(any(token in item["argv"] for token in ("bash", "sh", "-c")), name)

    def test_independent_semantic_gate_passed_without_failures(self) -> None:
        self.assertTrue(self.semantic["all_variants_passed"])
        self.assertEqual(
            LOCK["image"]["published_digest"], self.semantic["image_digest"]
        )
        self.assertEqual(
            {"protein", "ligand", "ame"},
            {item["variant"] for item in self.semantic["variants"]},
        )
        for item in self.semantic["variants"]:
            self.assertTrue(item["passed"], item["variant"])
            self.assertEqual([], item["failures"], item["variant"])

    def test_dependency_states_and_snapshot_posture_are_truthful(self) -> None:
        self.assertEqual(
            {
                "captured": False,
                "restored": False,
                "reason": "no device snapshot was captured or restored; the cache levels "
                "reported here are image and artifact locality only",
            },
            self.runs["gpu_snapshot"],
        )
        bindings = self.runs["dependency_bindings"]
        self.assertEqual(
            "cdbb7c7c475442712c73f8f8ea40b42fb5dd4fb5c1bf81fdb4642ca9e27f5ac4",
            bindings["alphafold2"]["generation"]["generation"],
        )
        self.assertIn("not mounted", bindings["alphafold2"]["qualification_state"])
        self.assertIn(
            "not exercised", bindings["rosettafold3"]["qualification_state"]
        )
        self.assertFalse(self.semantic["servable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
