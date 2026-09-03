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


def _pdb(residues: int, *, chain: str = "A", step: float = 3.8,
         name: str = "ALA", extra: list[tuple[str, str, str]] | None = None) -> str:
    """Render a minimal PDB with a straight C-alpha trace of a given spacing."""
    lines = []
    serial = 1
    for index in range(residues):
        x = index * step
        lines.append(
            f"ATOM  {serial:5d}  CA  {name:>3} {chain}{index + 1:4d}    "
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

    def test_lock_never_claims_a_gpu_snapshot(self) -> None:
        snapshot = ENTRY.describe()["gpu_snapshot"]
        self.assertFalse(snapshot["captured"])
        self.assertFalse(snapshot["restored"])

    def test_lock_does_not_report_the_runtime_as_servable(self) -> None:
        qualification = LOCK["image"]["qualification"]
        self.assertFalse(qualification["servable"])
        self.assertFalse(qualification["route_exposed"])

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
    """Recover phases from a real captured upstream log."""

    LOG = "\n".join(
        [
            "Loading Atomworks Patches",
            "2026-09-03 04:36:52.223 | INFO     | __main__:validate_checkpoint_paths:117 - "
            "Checkpoint validated: /opt/fs2/artifacts/complexa-ame/complexa_ame.ckpt",
            "2026-09-03 04:36:52.224 | INFO     | __main__:validate_checkpoint_paths:119 - "
            "Autoencoder checkpoint validated: /opt/fs2/artifacts/complexa-ame/complexa_ame_ae.ckpt",
            "2026-09-03 04:36:52.225 | INFO     | __main__:load_ckpt_n_configure_inference:180 - "
            "Using checkpoint /opt/fs2/artifacts/complexa-ame/complexa_ame.ckpt",
            "2026-09-03 04:36:59.557 | INFO     | __main__:load_ckpt_n_configure_inference:220 - "
            "Re-create LoRA layers and reload the weights now",
            "GPU available: True (cuda), used: True",
            "2026-09-03 04:36:52.500 | INFO     | __main__:main:578 - "
            "Starting generation job at 2026-09-03 04:36:52",
            "2026-09-03 04:37:07.590 | INFO     | __main__:main:750 - "
            "Generation job finished at 2026-09-03 04:37:07",
            "2026-09-03 04:37:07.591 | INFO     | __main__:main:751 - "
            "Total generation time: 15.09 seconds",
        ]
    )

    def test_phases_and_lora_marker_are_recovered(self) -> None:
        import datetime

        start = (
            datetime.datetime(2026, 9, 3, 4, 36, 40, tzinfo=datetime.timezone.utc).timestamp()
        )
        phases = ENTRY.parse_phases(self.LOG, start)
        self.assertTrue(phases["lora_reapplied"])
        self.assertAlmostEqual(15.09, phases["compute_seconds"], places=2)
        self.assertAlmostEqual(12.223, phases["interpreter_and_import_seconds"], places=2)
        self.assertAlmostEqual(7.332, phases["checkpoint_load_to_lora_seconds"], places=2)

    def test_a_log_without_the_lora_marker_reports_it_absent(self) -> None:
        stripped = "\n".join(
            line for line in self.LOG.splitlines() if "Re-create LoRA" not in line
        )
        self.assertFalse(ENTRY.parse_phases(stripped, 0.0)["lora_reapplied"])


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

    def test_absolute_target_override_is_always_passed(self) -> None:
        for name, variant in ENTRY.VARIANTS.items():
            argv = self._argv(name)
            namespace = variant["target_namespace"]
            self.assertTrue(
                any(item.startswith(f"++generation.{namespace}.") and ".target_path=/" in item
                    for item in argv),
                f"{name} must override target_path with an absolute path",
            )

    def test_hydra_is_forbidden_from_changing_directory(self) -> None:
        self.assertIn("hydra.job.chdir=False", self._argv("protein"))

    def test_reward_free_run_disables_search_and_reward(self) -> None:
        argv = self._argv("ligand")
        self.assertIn("++generation.search.algorithm=single-pass", argv)
        self.assertIn("++generation.reward_model=null", argv)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
