#!/usr/bin/env python3
"""Executable tests for the independent semantic gate, qualification/validate_result.py.

The runtime entrypoint and this validator implement the acceptance rules twice,
on purpose, so that a bug making one lenient cannot make the other lenient too.
Before this module existed the suite only exercised the entrypoint's copy, and
``run_checks.sh`` only byte-compiled the validator.  The validator's own rules
were therefore never run, which is exactly where the gate holes lived: a
162-byte, two-atom PDB passed the entire ligand gate with exit 0.

Every negative case here is paired with the positive case it must not reject,
so the gate cannot pass these tests by failing everything.  The positive
fixtures are reconstructed from the committed H100 evidence rather than
invented, so a correction that would retroactively disqualify the three genuine
runs fails this module.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VALIDATOR = ROOT / "qualification" / "validate_result.py"
LOCK = json.loads((ROOT / "image-lock.json").read_text(encoding="utf-8"))
RECEIPT = json.loads((ROOT / "evidence/h100-run-receipt.json").read_text(encoding="utf-8"))
QUALIFICATION = json.loads(
    (ROOT / "evidence/h100-semantic-qualification.json").read_text(encoding="utf-8")
)

RECEIPT_VARIANTS = {item["variant"]: item for item in RECEIPT["variants"]}
QUALIFIED_VARIANTS = {item["variant"]: item for item in QUALIFICATION["variants"]}

# Twenty standard types, so a fixture can be given an exact diversity.
_TYPES = [
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
]


def _load_validator():
    spec = importlib.util.spec_from_file_location("complexa_validate_result", VALIDATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = _load_validator()


def _chain(chain: str, residues: int, *, distinct: int = 18, step: float = 3.8,
           serial: int = 1, only: str | None = None) -> tuple[list[str], int]:
    """A straight C-alpha trace of a given length, spacing and diversity."""
    lines = []
    for index in range(residues):
        name = only or _TYPES[index % max(1, min(distinct, len(_TYPES)))]
        x = index * step
        lines.append(
            f"ATOM  {serial:5d}  CA  {name} {chain}{index + 1:4d}    "
            f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C"
        )
        serial += 1
    return lines, serial


def _hetatm(chain: str, residue: str, serial: int) -> str:
    return (
        f"HETATM{serial:5d}  C1  {residue} {chain}{1:4d}    "
        f"{0.0:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C"
    )


def _write_pdb(path: Path, chains: list[dict], ligand: tuple[str, str] | None = None) -> None:
    lines: list[str] = []
    serial = 1
    for spec in chains:
        block, serial = _chain(serial=serial, **spec)
        lines.extend(block)
    if ligand:
        lines.append(_hetatm(ligand[0], ligand[1], 9000))
    lines.append("END")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _faithful(variant: str, root: Path, *, mutate=None) -> Path:
    """Rebuild one variant's output tree from the committed H100 evidence."""
    receipt = RECEIPT_VARIANTS[variant]
    qualified = QUALIFIED_VARIANTS[variant]
    directory = root / variant
    directory.mkdir(parents=True, exist_ok=True)

    ligand_residues = qualified["expected_ligands"] or []
    for name, chains in qualified["chain_lengths"].items():
        distinct = qualified["chain_distinct_residues"].get(name, {})
        specs = [
            {"chain": chain, "residues": length,
             "distinct": max(1, distinct.get(chain, 18))}
            for chain, length in sorted(chains.items())
            if length > 0
        ]
        # A zero-length chain in the evidence is the ligand-only chain.
        empty = [chain for chain, length in sorted(chains.items()) if length == 0]
        pair = (empty[0], ligand_residues[0]) if empty and ligand_residues else (
            ("L", ligand_residues[0]) if ligand_residues else None
        )
        _write_pdb(directory / name, specs, pair)

    (directory / "upstream.log").write_text(
        "INFO GPU available: True (cuda), used: True\nLightning using CUDA\n",
        encoding="utf-8",
    )

    markers = [
        {
            "label": item["label"],
            "expected_bytes": item["bytes"],
            "observed_bytes": item["bytes"],
            "expected_sha256": item["sha256"],
            "observed_sha256": item["sha256"],
            "digest_verified": item["content_digest_verified"],
        }
        for item in receipt["checkpoint_pair"]
    ]
    envelope = {
        "variant": variant,
        "terminal_state": receipt["terminal_state"],
        "upstream_exit_code": receipt["upstream_exit_code"],
        "argv": receipt["argv"],
        "artifact_verification": {
            "markers": markers,
            "content_digests_verified": True,
            "rosettafold3": {
                "bound": receipt["rosettafold3"]["bound"],
                "exercised": receipt["rosettafold3"]["exercised"],
            },
        },
        "phases": dict(receipt["phases"]),
        "target": {
            "binder_length": qualified["binder_length_envelope"],
            "ligand_residues": ligand_residues,
        },
    }
    if mutate:
        mutate(envelope, directory)
    (directory / "result.json").write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    return directory


class FaithfulRunsStillQualifyTests(unittest.TestCase):
    """The correction must not retroactively disqualify the three real runs."""

    def test_each_committed_variant_shape_passes_the_corrected_gate(self) -> None:
        for variant in ("protein", "ligand", "ame"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _faithful(variant, root)
                report = GATE.validate(variant, root / variant)
                self.assertEqual([], report["failures"], variant)
                self.assertTrue(report["passed"], variant)
                self.assertGreaterEqual(report["protein_like_structures"], 1, variant)

    def test_the_command_line_gate_exits_zero_for_all_three(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for variant in ("protein", "ligand", "ame"):
                _faithful(variant, root)
            finished = subprocess.run(
                [sys.executable, str(VALIDATOR), "--root", str(root)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, finished.returncode, finished.stderr)
            verdict = json.loads(finished.stdout)
            self.assertTrue(verdict["all_passed"])


class TwoAtomCounterexampleTests(unittest.TestCase):
    """The exact counterexample that passed the published gate must now fail.

    Reproduced independently before the fix: exit 0, no failures,
    protein_like_structures 1, from a single alanine C-alpha.
    """

    COUNTEREXAMPLE = (
        "ATOM      1  CA  ALA B   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "HETATM 9000  C1  OQO A   1       0.000   0.000   0.000  1.00  0.00           C\n"
        "END\n"
    )

    def _stage(self, root: Path) -> Path:
        directory = root / "ligand"
        directory.mkdir(parents=True)
        (directory / "designed.pdb").write_text(self.COUNTEREXAMPLE, encoding="utf-8")
        (directory / "upstream.log").write_text(
            "INFO GPU available: True (cuda), used: True\nLightning using CUDA\n",
            encoding="utf-8",
        )
        (directory / "result.json").write_text(json.dumps({
            "terminal_state": "PASS",
            "variant": "ligand",
            "upstream_exit_code": 0,
            "argv": ["python", "generate.py",
                     "++ckpt_name=/ckpt/complexa_ligand.ckpt",
                     "++ae_ckpt_name=/ckpt/complexa_ligand_ae.ckpt"],
            "artifact_verification": {
                "content_digests_verified": False,
                "markers": [
                    {"label": "complexa_ligand.ckpt", "digest_verified": False},
                    {"label": "complexa_ligand_ae.ckpt", "digest_verified": False},
                ],
                "rosettafold3": {"bound": True, "exercised": False},
            },
            "phases": {"interpreter_and_import_seconds": 1.0, "model_load_seconds": 1.0,
                       "sampling_seconds": 1.0, "compute_seconds": 1.0,
                       "upstream_reported_generation_seconds": 2.0,
                       "upstream_process_seconds": 3.0, "lora_reapplied": True},
            "target": {"binder_length": [100], "ligand_residues": ["OQO"]},
        }, indent=2), encoding="utf-8")
        return directory

    def test_the_two_atom_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = self._stage(Path(raw))
            report = GATE.validate("ligand", directory)
        self.assertFalse(report["passed"])
        self.assertEqual(0, report["protein_like_structures"])
        joined = " | ".join(report["failures"])
        self.assertIn("did not verify checkpoint content digests", joined)
        self.assertIn("C-alpha atoms", joined)
        self.assertIn("binder envelope 100", joined)

    def test_the_command_line_gate_exits_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._stage(root)
            finished = subprocess.run(
                [sys.executable, str(VALIDATOR), "--root", str(root), "--variant", "ligand"],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(1, finished.returncode)
        self.assertIn("ligand: FAIL", finished.stderr)


class SingleValuedBinderEnvelopeTests(unittest.TestCase):
    """Ligand and AME declare exactly one length; that envelope must be enforced."""

    def test_a_binder_outside_a_single_valued_envelope_is_rejected(self) -> None:
        def shrink(envelope: dict, directory: Path) -> None:
            for path in directory.glob("*.pdb"):
                path.unlink()
            _write_pdb(directory / "job_0_n_100_id_0_single_orig0_binder.pdb",
                       [{"chain": "B", "residues": 61, "distinct": 18}], ("A", "OQO"))

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ligand", Path(raw), mutate=shrink)
            report = GATE.validate("ligand", directory)
        self.assertFalse(report["passed"])
        self.assertIn("binder envelope 100", " | ".join(report["failures"]))

    def test_the_exact_declared_length_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ligand", Path(raw))
            report = GATE.validate("ligand", directory)
        self.assertEqual([], report["failures"])
        self.assertEqual([100], report["binder_length_envelope"])

    def test_the_protein_range_envelope_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("protein", Path(raw))
            report = GATE.validate("protein", directory)
        self.assertEqual([64, 155], report["binder_length_envelope"])
        self.assertTrue(report["passed"])


class BackboneEvidenceTests(unittest.TestCase):
    def test_a_lone_c_alpha_is_not_a_backbone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw) / "protein"
            directory.mkdir(parents=True)
            _write_pdb(directory / "one.pdb", [{"chain": "A", "residues": 1}])
            summary = GATE._summarise(directory / "one.pdb")
        self.assertEqual({}, summary["mean_ca_step"])
        self.assertEqual({"A": 1}, summary["ca_counts"])

    def test_two_c_alphas_at_a_real_spacing_are_a_backbone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "two.pdb"
            _write_pdb(path, [{"chain": "A", "residues": 2}])
            summary = GATE._summarise(path)
        self.assertAlmostEqual(3.8, summary["mean_ca_step"]["A"], places=3)

    def test_an_exploded_trace_is_rejected(self) -> None:
        def explode(envelope: dict, directory: Path) -> None:
            for path in directory.glob("*.pdb"):
                path.unlink()
            _write_pdb(directory / "job_0_n_100_id_0_single_orig0_binder.pdb",
                       [{"chain": "B", "residues": 100, "distinct": 18, "step": 12.0}],
                       ("A", "OQO"))

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ligand", Path(raw), mutate=explode)
            report = GATE.validate("ligand", directory)
        self.assertFalse(report["passed"])
        self.assertIn("non-protein C-alpha spacing", " | ".join(report["failures"]))

    def test_a_zero_standard_residue_trace_is_still_judged_on_geometry(self) -> None:
        """A poly-UNK trace used to be skipped before the geometry rule ran."""
        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ligand", Path(raw))
            _write_pdb(directory / "unknown.pdb",
                       [{"chain": "C", "residues": 40, "distinct": 1, "step": 15.0}])
            # Force every residue to a non-standard name.
            path = directory / "unknown.pdb"
            path.write_text(path.read_text().replace(" ALA ", " UNK "), encoding="utf-8")
            summary = GATE._summarise(path)
            self.assertEqual(0, summary["standard"])
            report = GATE.validate("ligand", directory)
        self.assertFalse(report["passed"])
        self.assertIn("non-protein C-alpha spacing", " | ".join(report["failures"]))


class ContentDigestTests(unittest.TestCase):
    def test_an_unverified_digest_flag_fails_closed(self) -> None:
        def clear(envelope: dict, directory: Path) -> None:
            envelope["artifact_verification"]["content_digests_verified"] = False
            for marker in envelope["artifact_verification"]["markers"]:
                marker["digest_verified"] = False

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ame", Path(raw), mutate=clear)
            report = GATE.validate("ame", directory)
        self.assertFalse(report["passed"])
        self.assertIn("did not verify checkpoint content digests",
                      " | ".join(report["failures"]))

    def test_a_missing_flag_fails_closed(self) -> None:
        def drop(envelope: dict, directory: Path) -> None:
            envelope["artifact_verification"].pop("content_digests_verified")

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ame", Path(raw), mutate=drop)
            report = GATE.validate("ame", directory)
        self.assertFalse(report["passed"])
        self.assertIn("did not verify checkpoint content digests",
                      " | ".join(report["failures"]))

    def test_two_absent_byte_counts_do_not_compare_equal(self) -> None:
        def strip(envelope: dict, directory: Path) -> None:
            for marker in envelope["artifact_verification"]["markers"]:
                marker.pop("observed_bytes")
                marker.pop("expected_bytes")

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("protein", Path(raw), mutate=strip)
            report = GATE.validate("protein", directory)
        self.assertFalse(report["passed"])
        self.assertIn("carries no observed and expected byte count",
                      " | ".join(report["failures"]))

    def test_a_mismatched_byte_count_is_rejected(self) -> None:
        def bend(envelope: dict, directory: Path) -> None:
            envelope["artifact_verification"]["markers"][0]["observed_bytes"] = 1

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("protein", Path(raw), mutate=bend)
            report = GATE.validate("protein", directory)
        self.assertFalse(report["passed"])
        self.assertIn("byte count did not match", " | ".join(report["failures"]))


class EnvelopeAndPhaseTests(unittest.TestCase):
    def test_a_non_zero_exit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful(
                "protein", Path(raw),
                mutate=lambda envelope, _: envelope.__setitem__("upstream_exit_code", 1),
            )
            report = GATE.validate("protein", directory)
        self.assertFalse(report["passed"])

    def test_a_missing_cuda_marker_is_rejected(self) -> None:
        def blank(envelope: dict, directory: Path) -> None:
            (directory / "upstream.log").write_text("no accelerator\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("protein", Path(raw), mutate=blank)
            report = GATE.validate("protein", directory)
        self.assertFalse(report["passed"])
        self.assertFalse(report["cuda_marker_in_log"])

    def test_the_wrong_lora_posture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful(
                "protein", Path(raw),
                mutate=lambda envelope, _: envelope["phases"].__setitem__(
                    "lora_reapplied", True),
            )
            report = GATE.validate("protein", directory)
        self.assertFalse(report["passed"])
        self.assertIn("LoRA", " | ".join(report["failures"]))

    def test_a_missing_sampling_phase_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful(
                "ligand", Path(raw),
                mutate=lambda envelope, _: envelope["phases"].__setitem__(
                    "sampling_seconds", 0),
            )
            report = GATE.validate("ligand", directory)
        self.assertFalse(report["passed"])
        self.assertIn("no sampling phase was recorded", " | ".join(report["failures"]))

    def test_a_leaked_other_variant_checkpoint_is_rejected(self) -> None:
        def leak(envelope: dict, directory: Path) -> None:
            envelope["argv"] = list(envelope["argv"]) + ["++ckpt_name=complexa_ame.ckpt"]

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ligand", Path(raw), mutate=leak)
            report = GATE.validate("ligand", directory)
        self.assertFalse(report["passed"])
        self.assertIn("leaks the ame checkpoint", " | ".join(report["failures"]))

    def test_a_missing_ligand_residue_is_rejected(self) -> None:
        def strip(envelope: dict, directory: Path) -> None:
            for path in directory.glob("*.pdb"):
                path.unlink()
            _write_pdb(directory / "job_0_n_100_id_0_single_orig0_binder.pdb",
                       [{"chain": "B", "residues": 100, "distinct": 18}])

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ligand", Path(raw), mutate=strip)
            report = GATE.validate("ligand", directory)
        self.assertFalse(report["passed"])
        self.assertIn("none of the expected ligand residues",
                      " | ".join(report["failures"]))

    def test_a_degenerate_long_chain_is_rejected_on_diversity(self) -> None:
        def flatten(envelope: dict, directory: Path) -> None:
            for path in directory.glob("*.pdb"):
                path.unlink()
            _write_pdb(directory / "job_0_n_100_id_0_single_orig0_binder.pdb",
                       [{"chain": "B", "residues": 100, "only": "ALA"}], ("A", "OQO"))

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ligand", Path(raw), mutate=flatten)
            report = GATE.validate("ligand", directory)
        self.assertFalse(report["passed"])
        self.assertIn("distinct amino-acid type", " | ".join(report["failures"]))


class PublishedEvidenceBindingTests(unittest.TestCase):
    """Close the tautologies: bind published claims to what actually ran."""

    def test_every_variant_ran_the_published_image_digest(self) -> None:
        published = LOCK["image"]["published_digest"]
        self.assertEqual(published, QUALIFICATION["image_digest"])
        for variant, item in RECEIPT_VARIANTS.items():
            # image_id is the per-variant ground truth from the live pod.
            self.assertTrue(item["image_id"].endswith("@" + published), variant)
        self.assertEqual(3, len(RECEIPT_VARIANTS))

    def test_the_published_requirements_match_what_the_gate_enforces(self) -> None:
        """D3: the requirement list used to be prose with no link to the code."""
        source = VALIDATOR.read_text(encoding="utf-8")
        requirements = QUALIFICATION["gate"]["requirements"]
        self.assertEqual(len(requirements), len(set(requirements)))
        for needle in (
            'content_digests_verified") is not True',
            "MIN_CA_FOR_BACKBONE",
            "if declared and chain_lengths:",
            "MIN_DISTINCT_RESIDUES",
            "carries no observed and expected byte count",
        ):
            self.assertIn(needle, source, needle)
        joined = " ".join(requirements).lower()
        for phrase in ("content digest", "binder-length envelope", "c-alpha",
                       "distinct amino-acid", "ligand residue"):
            self.assertIn(phrase, joined, phrase)

    def test_the_judged_by_claim_admits_what_is_read_back(self) -> None:
        """D1: it used to claim every verdict was re-derived from artifacts."""
        judged = QUALIFICATION["gate"]["judged_by"].lower()
        self.assertIn("never imports the runtime entrypoint", judged)
        self.assertNotIn("re-derives every verdict", judged)
        for phrase in ("result.json", "re-derived", "cannot independently re-measure"):
            self.assertIn(phrase, judged, phrase)

    def test_no_committed_claim_cites_a_receipt_key_that_does_not_exist(self) -> None:
        """B3: image-lock.json cited h100-run-receipt.json contract_proving_runs."""
        receipt_text = (ROOT / "evidence/h100-run-receipt.json").read_text(encoding="utf-8")
        lock_text = (ROOT / "image-lock.json").read_text(encoding="utf-8")
        for key in ("contract_proving_runs",):
            if key in lock_text:
                self.assertIn(key, receipt_text,
                              f"image-lock.json cites {key}, which the receipt does not carry")

    def test_the_sampling_figure_is_described_as_derived_not_measured(self) -> None:
        """D2: compute_seconds is the same variable as sampling_seconds."""
        for item in RECEIPT_VARIANTS.values():
            phases = item["phases"]
            self.assertEqual(phases["sampling_seconds"], phases["compute_seconds"],
                             item["variant"])
        joined = " ".join(QUALIFICATION["gate"]["requirements"]).lower()
        self.assertIn("derived", joined)


class PerStepGeometryTests(unittest.TestCase):
    """A mean in range must not hide an alternating trace."""

    @staticmethod
    def _alternating(path: Path, steps: list[float]) -> None:
        lines, x = [], 0.0
        for index, step in enumerate(steps + [0.0]):
            name = _TYPES[index % 18]
            lines.append(
                f"ATOM  {index + 1:5d}  CA  {name} B{index + 1:4d}    "
                f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00           C"
            )
            x += step
        lines.append(_hetatm("A", "OQO", 9000))
        lines.append("END")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_a_collapsed_and_exploded_trace_is_rejected_despite_a_sane_mean(self) -> None:
        def swap(envelope: dict, directory: Path) -> None:
            for stale in directory.glob("*.pdb"):
                stale.unlink()
            self._alternating(
                directory / "job_0_n_100_id_0_single_orig0_binder.pdb",
                [1.0 if index % 2 else 6.6 for index in range(99)],
            )

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ligand", Path(raw), mutate=swap)
            summary = GATE._summarise(
                directory / "job_0_n_100_id_0_single_orig0_binder.pdb"
            )
            mean = summary["mean_ca_step"]["B"]
            report = GATE.validate("ligand", directory)

        # The mean alone would have passed the published gate.
        self.assertTrue(GATE.CA_MIN_A <= mean <= GATE.CA_MAX_A, mean)
        self.assertFalse(report["passed"])
        joined = " | ".join(report["failures"])
        self.assertIn("closer than", joined)
        self.assertIn("steps in range", joined)

    def test_a_uniform_real_spacing_trace_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ligand", Path(raw))
            report = GATE.validate("ligand", directory)
        self.assertEqual([], report["failures"])

    def test_a_single_chain_break_is_tolerated(self) -> None:
        """A real multi-segment chain must not be failed for one long step."""
        def brk(envelope: dict, directory: Path) -> None:
            for stale in directory.glob("*.pdb"):
                stale.unlink()
            steps = [3.8] * 99
            steps[50] = 6.2
            self._alternating(
                directory / "job_0_n_100_id_0_single_orig0_binder.pdb", steps
            )

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("ligand", Path(raw), mutate=brk)
            report = GATE.validate("ligand", directory)
        self.assertEqual([], report["failures"])


class NegativeDurationTests(unittest.TestCase):
    def test_a_negative_span_is_not_a_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful(
                "protein", Path(raw),
                mutate=lambda envelope, _: envelope["phases"].__setitem__(
                    "compute_seconds", -99.0),
            )
            report = GATE.validate("protein", directory)
        self.assertFalse(report["passed"])
        self.assertIn("compute phase", " | ".join(report["failures"]))

    def test_a_boolean_is_not_a_duration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful(
                "protein", Path(raw),
                mutate=lambda envelope, _: envelope["phases"].__setitem__(
                    "model_load_seconds", True),
            )
            report = GATE.validate("protein", directory)
        self.assertFalse(report["passed"])


class CheckpointDirectoryTests(unittest.TestCase):
    """A run must not read one variant's artifacts while qualifying as another."""

    def test_borrowing_another_variants_directory_is_rejected(self) -> None:
        def borrow(envelope: dict, directory: Path) -> None:
            envelope["argv"] = [
                item.replace("complexa-protein", "complexa-ame")
                for item in envelope["argv"]
            ]

        with tempfile.TemporaryDirectory() as raw:
            directory = _faithful("protein", Path(raw), mutate=borrow)
            report = GATE.validate("protein", directory)
        self.assertFalse(report["passed"])
        joined = " | ".join(report["failures"])
        self.assertIn("does not reference the complexa-protein artifact directory", joined)
        self.assertIn("references the ame artifact directory", joined)

    def test_each_real_variant_names_its_own_directory(self) -> None:
        for variant in ("protein", "ligand", "ame"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as raw:
                directory = _faithful(variant, Path(raw))
                report = GATE.validate(variant, directory)
                self.assertEqual([], report["failures"], variant)


class PartialVerdictTests(unittest.TestCase):
    def test_a_single_variant_verdict_is_not_all_variants(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _faithful("ligand", root)
            finished = subprocess.run(
                [sys.executable, str(VALIDATOR), "--root", str(root),
                 "--variant", "ligand"],
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(0, finished.returncode, finished.stderr)
        verdict = json.loads(finished.stdout)
        self.assertTrue(verdict["all_passed"])
        self.assertFalse(verdict["covers_all_variants"])
        self.assertEqual(["ligand"], verdict["variants_requested"])

    def test_all_three_together_do_cover_every_variant(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for variant in ("protein", "ligand", "ame"):
                _faithful(variant, root)
            finished = subprocess.run(
                [sys.executable, str(VALIDATOR), "--root", str(root)],
                capture_output=True, text=True, check=False,
            )
        verdict = json.loads(finished.stdout)
        self.assertTrue(verdict["covers_all_variants"])


class RenderedImageMustBeDigestPinnedTests(unittest.TestCase):
    RENDER = ROOT / "qualification" / "render_plan.py"

    def _render(self, image: str, destination: Path):
        return subprocess.run(
            [sys.executable, str(self.RENDER), "--image", image,
             "--run-prefix", "fs2-cxq-pin-test", "--output", str(destination)],
            capture_output=True, text=True, check=False,
        )

    def test_a_mutable_tag_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            finished = self._render(
                "cr.eu-north1.nebius.cloud/x/y/proteina-complexa:latest",
                Path(raw) / "plan.json",
            )
        self.assertNotEqual(0, finished.returncode)
        self.assertIn("digest-pinned", finished.stderr)

    def test_a_digest_is_accepted_and_renders_three_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "plan.json"
            finished = self._render("sha256:" + "0" * 64, destination)
            self.assertEqual(0, finished.returncode, finished.stderr)
            plan = json.loads(destination.read_text(encoding="utf-8"))
        jobs = [item for item in plan["manifests"] if item["kind"] == "Job"]
        self.assertEqual(3, len(jobs))


class DigestVerificationDefaultTests(unittest.TestCase):
    """The only cryptographic content check must be opt-out, not opt-in.

    The in-image tree inventory identifies each file by length and CRC32.  That
    is forgeable by construction -- a same-length payload with a matching CRC32
    is solved for algebraically, not searched -- so a plan that omits the
    SHA-256 pass has no content check at all.
    """

    RENDER = ROOT / "qualification" / "render_plan.py"

    def _flags(self, extra: list[str]) -> set:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "plan.json"
            finished = subprocess.run(
                [sys.executable, str(self.RENDER),
                 "--image", "sha256:" + "0" * 64,
                 "--run-prefix", "fs2-cxq-default-test",
                 "--output", str(destination), *extra],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, finished.returncode, finished.stderr)
            plan = json.loads(destination.read_text(encoding="utf-8"))
        found = set()
        for item in plan["manifests"]:
            for value in (item.get("data") or {}).values():
                if "verify_content_digests" in value:
                    found.add(json.loads(value)["verify_content_digests"])
        return found

    def test_digest_verification_is_on_by_default(self) -> None:
        self.assertEqual({True}, self._flags([]))

    def test_the_flag_is_still_accepted(self) -> None:
        self.assertEqual({True}, self._flags(["--verify-digests"]))

    def test_opting_out_is_explicit(self) -> None:
        self.assertEqual({False}, self._flags(["--no-verify-digests"]))

    def test_the_committed_plan_requested_digest_verification(self) -> None:
        plan = json.loads(
            (ROOT / "qualification/generated-plan.json").read_text(encoding="utf-8")
        )
        found = set()
        for item in plan["manifests"]:
            for value in (item.get("data") or {}).values():
                if "verify_content_digests" in value:
                    found.add(json.loads(value)["verify_content_digests"])
        self.assertEqual({True}, found)


class GeneratorAndEvidenceAgreeTests(unittest.TestCase):
    """D3: the requirement list was prose with no link to the emitting code."""

    def test_the_committed_requirements_are_what_the_generator_emits(self) -> None:
        import ast
        import re

        source = (ROOT / "qualification/assemble_evidence.py").read_text(encoding="utf-8")
        match = re.search(r'"requirements": \[\n(.*?)\n            \],', source, re.S)
        self.assertIsNotNone(match)
        emitted = ast.literal_eval("[" + match.group(1).replace("\n", "") + "]")
        self.assertEqual(QUALIFICATION["gate"]["requirements"], emitted)

    def test_the_committed_judged_by_is_what_the_generator_emits(self) -> None:
        import ast
        import re

        source = (ROOT / "qualification/assemble_evidence.py").read_text(encoding="utf-8")
        self.assertNotIn("re-derives every verdict", source)
        # The literal is wrapped across source lines, so evaluate it rather
        # than grepping the raw text.
        match = re.search(r'"judged_by": (".*?"),\n            "requirements"', source, re.S)
        self.assertIsNotNone(match)
        emitted = ast.literal_eval("(" + match.group(1) + ")")
        self.assertEqual(QUALIFICATION["gate"]["judged_by"], emitted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
