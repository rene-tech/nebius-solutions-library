"""Offline contract tests for the RFdiffusion runtime adapter.

No cluster, no GPU, no registry, no network. Every test either proves a bound is
enforced or proves a defect the adapter is meant to catch.
"""

from __future__ import annotations

import json
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime_entrypoint as rt  # noqa: E402


def parameters(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": rt.SCHEMA_PARAMETERS,
        "operation": rt.OPERATION_DESIGN_BACKBONE,
        "contigs": ["76-76"],
        "num_designs": 1,
        "seed": 0,
        "diffuser_T": 50,
    }
    payload.update(overrides)
    return payload


def pdb_line(serial: int, atom: str, resname: str, chain: str, resseq: int, x: float, y: float, z: float) -> str:
    return (
        f"ATOM  {serial:>5} {atom:^4}{'':1}{resname:>3} {chain}{resseq:>4}{'':1}   "
        f"{x:>8.3f}{y:>8.3f}{z:>8.3f}  1.00  0.00\n"
    )


def write_pdb(path: Path, residues: list[tuple[str, int, str, tuple[float, float, float]]]) -> None:
    serial = 1
    with path.open("w", encoding="utf-8") as handle:
        for chain, resseq, resname, (x, y, z) in residues:
            for atom, offset in (("N", -1.2), ("CA", 0.0), ("C", 1.2)):
                handle.write(pdb_line(serial, atom, resname, chain, resseq, x + offset, y, z))
                serial += 1
        handle.write("TER\n")


def backbone(count: int, chain: str = "A", start: int = 1, resname: str = "GLY") -> list:
    return [(chain, start + i, resname, (float(i) * 3.8, 0.0, 0.0)) for i in range(count)]


class ContigGrammarTests(unittest.TestCase):
    def test_fixed_length_span(self) -> None:
        p = rt.parse_parameters(parameters(contigs=["76-76"]))
        self.assertEqual(p.total_min_residues, 76)
        self.assertEqual(p.total_max_residues, 76)
        self.assertTrue(p.contig_groups[0].is_fixed_length)
        self.assertEqual(p.contig_literal, "[76-76]")

    def test_variable_span_keeps_both_bounds(self) -> None:
        p = rt.parse_parameters(parameters(contigs=["60-80"]))
        self.assertEqual((p.total_min_residues, p.total_max_residues), (60, 80))
        self.assertFalse(p.contig_groups[0].is_fixed_length)

    def test_motif_span_requires_scaffold_operation(self) -> None:
        with self.assertRaises(rt.RequestError) as ctx:
            rt.parse_parameters(parameters(contigs=["10-40/A163-181/10-40"]))
        self.assertIn("scaffold-motif", str(ctx.exception))

    def test_scaffold_motif_literal_and_lengths(self) -> None:
        p = rt.parse_parameters(
            parameters(
                operation=rt.OPERATION_SCAFFOLD_MOTIF,
                contigs=["10-40/A163-181/10-40"],
                input_pdb_artifact_id="artifact.target",
            )
        )
        self.assertEqual(p.contig_literal, "[10-40/A163-181/10-40]")
        # motif A163-181 is 19 residues, flanks are 10..40 each
        self.assertEqual(p.total_min_residues, 10 + 19 + 10)
        self.assertEqual(p.total_max_residues, 40 + 19 + 40)
        self.assertEqual(len(p.motif_segments), 1)
        self.assertEqual(p.motif_segments[0].motif_length, 19)

    def test_chain_break_token_allowed(self) -> None:
        p = rt.parse_parameters(parameters(contigs=["50-50/0/50-50"]))
        self.assertEqual(p.total_max_residues, 100)

    def test_scaffold_motif_requires_input_pdb(self) -> None:
        with self.assertRaises(rt.RequestError) as ctx:
            rt.parse_parameters(
                parameters(operation=rt.OPERATION_SCAFFOLD_MOTIF, contigs=["10-10/A1-5/10-10"])
            )
        self.assertIn("input_pdb_artifact_id", str(ctx.exception))

    def test_design_backbone_rejects_input_pdb(self) -> None:
        with self.assertRaises(rt.RequestError):
            rt.parse_parameters(parameters(input_pdb_artifact_id="artifact.target"))


class ContigInjectionTests(unittest.TestCase):
    """The contig literal is concatenated into a Hydra override, so the grammar is
    the only thing standing between a caller and arbitrary upstream configuration."""

    INJECTIONS = [
        "76-76] inference.deterministic=False [",
        "76-76,inference.output_prefix=/tmp/escape",
        "76-76 inference.ckpt_override_path=/etc/passwd",
        "$(id)",
        "`id`",
        "76-76;rm -rf /",
        "../../etc/passwd",
        "76-76\ninference.deterministic=False",
        "+inference.foo=bar",
        "76--76",
        "76-76/",
        "A163-181",  # motif without scaffold operation is rejected elsewhere, grammar-valid
        "",
        "-1-10",
        "1e5-1e5",
    ]

    def test_injection_attempts_are_rejected(self) -> None:
        for payload in self.INJECTIONS:
            with self.subTest(payload=payload):
                with self.assertRaises(rt.RequestError):
                    rt.parse_parameters(parameters(contigs=[payload]))

    def test_every_accepted_literal_is_bracket_safe(self) -> None:
        for contig in ["76-76", "10-40/0/50-60", "100-100"]:
            with self.subTest(contig=contig):
                literal = rt.parse_parameters(parameters(contigs=[contig])).contig_literal
                self.assertTrue(literal.startswith("[") and literal.endswith("]"))
                self.assertEqual(literal.count("["), 1)
                self.assertEqual(literal.count("]"), 1)
                for forbidden in (" ", ";", "$", "`", "\n", "=", "..", "'", '"'):
                    self.assertNotIn(forbidden, literal)


class BoundsTests(unittest.TestCase):
    def test_total_residue_ceiling(self) -> None:
        with self.assertRaises(rt.RequestError) as ctx:
            rt.parse_parameters(parameters(contigs=["512-512", "1-1"]))
        self.assertIn(str(rt.MAX_TOTAL_RESIDUES), str(ctx.exception))

    def test_contig_group_ceiling(self) -> None:
        with self.assertRaises(rt.RequestError):
            rt.parse_parameters(parameters(contigs=["10-10"] * (rt.MAX_CONTIG_GROUPS + 1)))

    def test_segment_ceiling(self) -> None:
        with self.assertRaises(rt.RequestError):
            rt.parse_parameters(
                parameters(contigs=["/".join(["1-1"] * (rt.MAX_SEGMENTS_PER_GROUP + 1))])
            )

    def test_num_designs_bounds(self) -> None:
        with self.assertRaises(rt.RequestError):
            rt.parse_parameters(parameters(num_designs=0))
        with self.assertRaises(rt.RequestError):
            rt.parse_parameters(parameters(num_designs=rt.MAX_NUM_DESIGNS + 1))

    def test_diffuser_t_bounds(self) -> None:
        with self.assertRaises(rt.RequestError):
            rt.parse_parameters(parameters(diffuser_T=0))
        with self.assertRaises(rt.RequestError):
            rt.parse_parameters(parameters(diffuser_T=rt.MAX_DIFFUSER_T + 1))

    def test_seed_plus_designs_cannot_overflow_index_space(self) -> None:
        with self.assertRaises(rt.RequestError):
            rt.parse_parameters(parameters(seed=rt.MAX_SEED, num_designs=2))

    def test_boolean_is_not_an_integer(self) -> None:
        with self.assertRaises(rt.RequestError):
            rt.parse_parameters(parameters(num_designs=True))

    def test_unknown_key_rejected(self) -> None:
        with self.assertRaises(rt.RequestError) as ctx:
            rt.parse_parameters(parameters(hydra_overrides=["inference.deterministic=False"]))
        self.assertIn("hydra_overrides", str(ctx.exception))

    def test_wrong_schema_rejected(self) -> None:
        with self.assertRaises(rt.RequestError):
            rt.parse_parameters(parameters(schema="something-else/v1"))

    def test_hotspot_grammar(self) -> None:
        p = rt.parse_parameters(parameters(hotspot_residues=["a12", "B7"]))
        self.assertEqual(p.hotspot_residues, ("A12", "B7"))
        with self.assertRaises(rt.RequestError):
            rt.parse_parameters(parameters(hotspot_residues=["A12,inference.foo=1"]))


class SeedContractTests(unittest.TestCase):
    """Upstream v1.1.0 seeds each design from its design index, so the adapter's
    seed must be design_startnum. These tests pin that mapping."""

    def test_design_indices_follow_seed(self) -> None:
        p = rt.parse_parameters(parameters(seed=8100, num_designs=3))
        self.assertEqual(p.design_indices, (8100, 8101, 8102))

    def test_argv_carries_deterministic_and_startnum(self) -> None:
        p = rt.parse_parameters(parameters(seed=8100, num_designs=2))
        argv = rt.build_argv(
            p,
            checkpoint=Path("/artifacts/rfdiffusion/Base_ckpt.pt"),
            output_prefix=Path("/out/designs/design"),
            hydra_run_dir=Path("/tmp/hydra"),
            schedule_directory=Path("/tmp/fs2-rfdiffusion/schedules"),
            input_pdb=None,
            upstream_home=Path("/opt/rfdiffusion"),
            python_executable="/usr/bin/python",
        )
        self.assertIn("inference.deterministic=True", argv)
        self.assertIn("inference.design_startnum=8100", argv)
        self.assertIn("inference.num_designs=2", argv)
        self.assertNotIn("inference.seed=8100", argv)

    def test_argv_has_no_shell_indirection(self) -> None:
        p = rt.parse_parameters(parameters())
        argv = rt.build_argv(
            p,
            checkpoint=Path("/artifacts/Base_ckpt.pt"),
            output_prefix=Path("/out/design"),
            hydra_run_dir=Path("/tmp/hydra"),
            schedule_directory=Path("/tmp/fs2-rfdiffusion/schedules"),
            input_pdb=None,
            upstream_home=Path("/opt/rfdiffusion"),
            python_executable="/usr/bin/python",
        )
        self.assertTrue(all(isinstance(token, str) for token in argv))
        joined = " ".join(argv)
        for metacharacter in ("&&", "||", "|", ";", "$(", "`", ">", "<"):
            self.assertNotIn(metacharacter, joined)
        self.assertTrue(argv[1].endswith("scripts/run_inference.py"))

    def test_hydra_output_is_redirected_off_the_readonly_tree(self) -> None:
        p = rt.parse_parameters(parameters())
        argv = rt.build_argv(
            p,
            checkpoint=Path("/artifacts/Base_ckpt.pt"),
            output_prefix=Path("/out/design"),
            hydra_run_dir=Path("/tmp/hydra"),
            schedule_directory=Path("/tmp/fs2-rfdiffusion/schedules"),
            input_pdb=None,
            upstream_home=Path("/opt/rfdiffusion"),
        )
        self.assertIn("hydra.run.dir=/tmp/hydra", argv)
        self.assertIn("hydra.output_subdir=null", argv)


class PdbParsingTests(unittest.TestCase):
    def test_parses_residues_and_ca(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.pdb"
            write_pdb(path, backbone(5))
            residues = rt.parse_pdb_residues(path)
            self.assertEqual(len(residues), 5)
            self.assertTrue(all(r.ca is not None for r in residues))
            self.assertEqual({r.chain for r in residues}, {"A"})

    def test_truncated_atom_record_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.pdb"
            path.write_text("ATOM      1  CA  GLY A   1\n", encoding="utf-8")
            with self.assertRaises(rt.VerificationError):
                rt.parse_pdb_residues(path)


class _VerifyHarness(unittest.TestCase):
    def build_outputs(
        self,
        tmp: Path,
        *,
        residue_count: int = 76,
        design_index: int = 0,
        device: str = "NVIDIA H100 80GB HBM3",
        trb_extra: dict | None = None,
        resname: str = "GLY",
    ) -> Path:
        designs = tmp / "designs"
        designs.mkdir(parents=True, exist_ok=True)
        prefix = designs / "design"
        write_pdb(Path(f"{prefix}_{design_index}.pdb"), backbone(residue_count, resname=resname))
        payload = {"device": device, "time": 42.5}
        payload.update(trb_extra or {})
        with Path(f"{prefix}_{design_index}.trb").open("wb") as handle:
            pickle.dump(payload, handle)
        return prefix


class DesignVerificationTests(_VerifyHarness):
    def test_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self.build_outputs(Path(tmp))
            p = rt.parse_parameters(parameters(contigs=["76-76"]))
            verification = rt.verify_design(0, prefix, p, None)
            self.assertEqual(verification.residue_count, 76)
            self.assertEqual(verification.device, "NVIDIA H100 80GB HBM3")

    def test_missing_pdb_marker_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self.build_outputs(Path(tmp))
            Path(f"{prefix}_0.pdb").unlink()
            p = rt.parse_parameters(parameters())
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, p, None)
            self.assertIn("missing", str(ctx.exception))

    def test_missing_trb_marker_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self.build_outputs(Path(tmp))
            Path(f"{prefix}_0.trb").unlink()
            p = rt.parse_parameters(parameters())
            with self.assertRaises(rt.VerificationError):
                rt.verify_design(0, prefix, p, None)

    def test_empty_marker_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self.build_outputs(Path(tmp))
            Path(f"{prefix}_0.pdb").write_text("", encoding="utf-8")
            p = rt.parse_parameters(parameters())
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, p, None)
            self.assertIn("empty", str(ctx.exception))

    def test_cpu_device_is_a_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self.build_outputs(Path(tmp), device="CPU")
            p = rt.parse_parameters(parameters())
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, p, None)
            self.assertIn("CUDA", str(ctx.exception))

    def test_missing_device_record_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            designs = Path(tmp) / "designs"
            designs.mkdir()
            prefix = designs / "design"
            write_pdb(Path(f"{prefix}_0.pdb"), backbone(76))
            with Path(f"{prefix}_0.trb").open("wb") as handle:
                pickle.dump({"time": 1.0}, handle)
            p = rt.parse_parameters(parameters())
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, p, None)
            self.assertIn("device", str(ctx.exception))

    def test_wrong_residue_count_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self.build_outputs(Path(tmp), residue_count=64)
            p = rt.parse_parameters(parameters(contigs=["76-76"]))
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, p, None)
            self.assertIn("64", str(ctx.exception))

    def test_variable_contig_accepts_in_range_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self.build_outputs(Path(tmp), residue_count=70)
            p = rt.parse_parameters(parameters(contigs=["60-80"]))
            self.assertEqual(rt.verify_design(0, prefix, p, None).residue_count, 70)

    def test_non_standard_residue_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self.build_outputs(Path(tmp), resname="XYZ")
            p = rt.parse_parameters(parameters())
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, p, None)
            self.assertIn("XYZ", str(ctx.exception))

    def test_unreadable_trb_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self.build_outputs(Path(tmp))
            Path(f"{prefix}_0.trb").write_bytes(b"not a pickle")
            p = rt.parse_parameters(parameters())
            with self.assertRaises(rt.VerificationError):
                rt.verify_design(0, prefix, p, None)


class MotifPreservationTests(_VerifyHarness):
    def _scaffold_parameters(self) -> rt.RFdiffusionParameters:
        return rt.parse_parameters(
            parameters(
                operation=rt.OPERATION_SCAFFOLD_MOTIF,
                contigs=["5-5/A1-3/5-5"],
                input_pdb_artifact_id="artifact.target",
            )
        )

    def _reference(self) -> list[rt.Residue]:
        return [
            rt.Residue(chain="A", seq=1, insertion_code="", name="LYS", ca=(0.0, 0.0, 0.0)),
            rt.Residue(chain="A", seq=2, insertion_code="", name="TRP", ca=(3.8, 0.0, 0.0)),
            rt.Residue(chain="A", seq=3, insertion_code="", name="ASP", ca=(7.6, 0.0, 0.0)),
        ]

    def _designed_outputs(
        self,
        tmp: Path,
        motif_names: list[str],
        shift: float = 0.0,
        rotate_degrees: float = 0.0,
        distort: float = 0.0,
    ) -> Path:
        import math

        designs = tmp / "designs"
        designs.mkdir(parents=True, exist_ok=True)
        prefix = designs / "design"
        residues = backbone(5, start=1)
        angle = math.radians(rotate_degrees)
        for offset, name in enumerate(motif_names):
            # Reference motif CA positions are (3.8 * offset, 0, 0).
            x, y, z = 3.8 * offset, 0.0, 0.0
            if distort:
                # Move alternate residues apart: a real change of shape, which no
                # rigid transform can undo.
                y += distort if offset % 2 == 0 else -distort
            rx = x * math.cos(angle) - y * math.sin(angle)
            ry = x * math.sin(angle) + y * math.cos(angle)
            residues.append(("A", 6 + offset, name, (rx + shift, ry, z)))
        residues.extend(backbone(5, start=11))
        write_pdb(Path(f"{prefix}_0.pdb"), residues)
        trb = {
            "device": "NVIDIA H100 80GB HBM3",
            "time": 12.0,
            "con_ref_pdb_idx": [("A", 1), ("A", 2), ("A", 3)],
            "con_hal_pdb_idx": [("A", 6), ("A", 7), ("A", 8)],
        }
        with Path(f"{prefix}_0.trb").open("wb") as handle:
            pickle.dump(trb, handle)
        return prefix

    def test_motif_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._designed_outputs(Path(tmp), ["LYS", "TRP", "ASP"])
            verification = rt.verify_design(0, prefix, self._scaffold_parameters(), self._reference())
            self.assertEqual(verification.motif_positions, 3)
            self.assertIsNotNone(verification.motif_fit)
            self.assertAlmostEqual(verification.motif_fit["rmsd"], 0.0, places=6)

    def test_mutated_motif_residue_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._designed_outputs(Path(tmp), ["LYS", "ALA", "ASP"])
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, self._scaffold_parameters(), self._reference())
            self.assertIn("identity changed", str(ctx.exception))

    def test_rigid_body_translation_is_not_motif_loss(self) -> None:
        """Regression for the r10 false negative.

        RFdiffusion emits designs in its own recentred frame, so a perfectly
        scaffolded motif can sit far from the reference. A pure translation must
        superpose away to zero, not be reported as a destroyed motif.
        """
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._designed_outputs(Path(tmp), ["LYS", "TRP", "ASP"], shift=48.0)
            verification = rt.verify_design(
                0, prefix, self._scaffold_parameters(), self._reference()
            )
            self.assertAlmostEqual(verification.motif_fit["rmsd"], 0.0, places=6)
            self.assertGreater(verification.motif_fit["rmsd_unaligned"], 40.0)

    def test_rigid_body_rotation_is_not_motif_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._designed_outputs(
                Path(tmp), ["LYS", "TRP", "ASP"], rotate_degrees=137.0, shift=25.0
            )
            verification = rt.verify_design(
                0, prefix, self._scaffold_parameters(), self._reference()
            )
            # The synthetic motif is collinear, so rotation about its own axis is
            # underdetermined; 1e-3 A is far below any structural threshold.
            self.assertLess(verification.motif_fit["rmsd"], 1e-3)
            self.assertGreater(verification.motif_fit["rotation_degrees"], 1.0)

    def test_real_motif_distortion_still_fails(self) -> None:
        """Superposition must not become a way to launder a broken motif."""
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._designed_outputs(
                Path(tmp), ["LYS", "TRP", "ASP"], distort=4.0
            )
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, self._scaffold_parameters(), self._reference())
            self.assertIn("after optimal superposition", str(ctx.exception))

    def test_missing_motif_mapping_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            designs = Path(tmp) / "designs"
            designs.mkdir()
            prefix = designs / "design"
            write_pdb(Path(f"{prefix}_0.pdb"), backbone(13))
            with Path(f"{prefix}_0.trb").open("wb") as handle:
                pickle.dump({"device": "NVIDIA H100 80GB HBM3", "time": 1.0}, handle)
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, self._scaffold_parameters(), self._reference())
            self.assertIn("motif mapping", str(ctx.exception))

    def test_mapping_length_must_match_request(self) -> None:
        """A 5-residue motif request against a 3-position mapping must fail on the
        mapping, so the contig total is held at 13 to keep the length check silent."""
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._designed_outputs(Path(tmp), ["LYS", "TRP", "ASP"])
            wider = rt.parse_parameters(
                parameters(
                    operation=rt.OPERATION_SCAFFOLD_MOTIF,
                    contigs=["4-4/A1-5/4-4"],
                    input_pdb_artifact_id="artifact.target",
                )
            )
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, wider, self._reference())
            self.assertIn("covers 3 positions", str(ctx.exception))


class ArtifactResolutionTests(unittest.TestCase):
    def _artifacts(self, sha: str, size: int, path: str = "rfdiffusion/Base_ckpt.pt") -> dict:
        return {
            "artifact.rfdiffusion.base-ckpt": {
                "artifact_id": "artifact.rfdiffusion.base-ckpt",
                "path": path,
                "sha256": sha,
                "size_bytes": size,
            }
        }

    def test_resolves_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "rfdiffusion" / "Base_ckpt.pt"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"weights")
            sha = rt.sha256_file(target)
            resolved = rt.resolve_artifact(
                "artifact.rfdiffusion.base-ckpt", self._artifacts(sha, 7), root, verify_digest=True
            )
            self.assertTrue(resolved.verified)
            self.assertEqual(resolved.sha256, sha)

    def test_digest_mismatch_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "rfdiffusion" / "Base_ckpt.pt"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"weights")
            with self.assertRaises(rt.RequestError) as ctx:
                rt.resolve_artifact(
                    "artifact.rfdiffusion.base-ckpt", self._artifacts("0" * 64, 7), root, verify_digest=True
                )
            self.assertIn("sha256 mismatch", str(ctx.exception))

    def test_size_mismatch_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "rfdiffusion" / "Base_ckpt.pt"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"weights")
            sha = rt.sha256_file(target)
            with self.assertRaises(rt.RequestError) as ctx:
                rt.resolve_artifact(
                    "artifact.rfdiffusion.base-ckpt", self._artifacts(sha, 999), root, verify_digest=True
                )
            self.assertIn("bytes", str(ctx.exception))

    def test_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            with self.assertRaises(rt.RequestError) as ctx:
                rt.resolve_artifact(
                    "artifact.rfdiffusion.base-ckpt",
                    self._artifacts("0" * 64, 1, path="../escape.pt"),
                    root,
                    verify_digest=False,
                )
            self.assertIn("contained", str(ctx.exception))

    def test_absolute_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(rt.RequestError):
                rt.resolve_artifact(
                    "artifact.rfdiffusion.base-ckpt",
                    self._artifacts("0" * 64, 1, path="/etc/passwd"),
                    root,
                    verify_digest=False,
                )

    def test_unknown_artifact_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rt.RequestError) as ctx:
                rt.resolve_artifact("artifact.absent", {}, Path(tmp), verify_digest=False)
            self.assertIn("not present in the input manifest", str(ctx.exception))


class EndToEndArgvTests(unittest.TestCase):
    """Drive the real main() against a stub upstream.

    Testing build_argv alone is not enough: the r8 image shipped a correct
    build_argv while main() never passed a schedule directory, and the defect only
    surfaced on a live H100 run. These tests assert on the argv that main()
    actually executes, and on the directories it prepares before executing it.
    """

    STUB = '''import json, os, pickle, sys
argv = sys.argv[1:]
overrides = dict(a.split("=", 1) for a in argv if "=" in a)
record = {"argv": sys.argv, "overrides": overrides}
prefix = overrides["inference.output_prefix"]
start = int(overrides["inference.design_startnum"])
count = int(overrides["inference.num_designs"])
schedules = overrides["inference.schedule_directory_path"]
record["schedule_directory_exists"] = os.path.isdir(schedules)
record["schedule_directory_writable"] = os.access(schedules, os.W_OK)
os.makedirs(os.path.dirname(prefix), exist_ok=True)
with open(os.environ["FS2_TEST_ARGV_RECORD"], "w") as handle:
    json.dump(record, handle)
print("Making design %s_%d" % (prefix, start))
for index in range(start, start + count):
    with open("%s_%d.pdb" % (prefix, index), "w") as handle:
        for residue in range(76):
            for atom, off in (("N", -1.2), ("CA", 0.0), ("C", 1.2)):
                handle.write(
                    "ATOM  %5d %-4s %3s %s%4d    %8.3f%8.3f%8.3f  1.00  0.00\\n"
                    % (residue * 3, atom, "GLY", "A", residue + 1,
                       residue * 3.8 + off, 0.0, 0.0)
                )
    with open("%s_%d.trb" % (prefix, index), "wb") as handle:
        pickle.dump({"device": "NVIDIA H100 80GB HBM3", "time": 1.0}, handle)
print("Finished design in 0.01 minutes")
'''

    def _stub_upstream(self, root: Path) -> Path:
        home = root / "upstream"
        (home / "scripts").mkdir(parents=True)
        (home / "scripts" / "run_inference.py").write_text(self.STUB, encoding="utf-8")
        return home

    def _run(self, tmp: Path, *, seed: int = 8100) -> tuple[int, dict, dict]:
        artifact_root = tmp / "artifacts"
        artifact_root.mkdir()
        checkpoint = artifact_root / "Base_ckpt.pt"
        checkpoint.write_bytes(b"checkpoint bytes")
        sha = rt.sha256_file(checkpoint)

        request = tmp / "request.json"
        request.write_text(
            json.dumps(
                {
                    "schema": rt.SCHEMA_REQUEST,
                    "operation": "design-backbone",
                    "parameters": parameters(seed=seed),
                }
            ),
            encoding="utf-8",
        )
        manifest = tmp / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": rt.SCHEMA_MANIFEST,
                    "entries": [
                        {
                            "name": "base_checkpoint",
                            "artifact": {
                                "artifact_id": "artifact.rfdiffusion.base-ckpt",
                                "path": "Base_ckpt.pt",
                                "sha256": sha,
                                "size_bytes": checkpoint.stat().st_size,
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        record_path = tmp / "argv-record.json"
        output = tmp / "out"
        os.environ["FS2_TEST_ARGV_RECORD"] = str(record_path)
        try:
            code = rt.main(
                [
                    "run",
                    "--request", str(request),
                    "--input-manifest", str(manifest),
                    "--output", str(output),
                    "--artifact-root", str(artifact_root),
                    "--upstream-home", str(self._stub_upstream(tmp)),
                    "--scratch", str(tmp / "scratch"),
                    "--cache-level", "artifact-local",
                ]
            )
        finally:
            os.environ.pop("FS2_TEST_ARGV_RECORD", None)
        envelope = json.loads((output / "result.json").read_text(encoding="utf-8"))
        record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
        return code, envelope, record

    def test_main_emits_a_writable_schedule_directory(self) -> None:
        """The exact r8 live failure: upstream must never be left to create its
        schedule cache inside its own read-only package tree."""
        with tempfile.TemporaryDirectory() as tmp:
            code, envelope, record = self._run(Path(tmp))
            self.assertEqual(code, 0, envelope.get("error"))
            override = record["overrides"].get("inference.schedule_directory_path")
            self.assertIsNotNone(override, "main() emitted no schedule_directory_path")
            self.assertTrue(record["schedule_directory_exists"])
            self.assertTrue(record["schedule_directory_writable"])
            self.assertNotIn("/opt/rfdiffusion", override)
            self.assertTrue(override.startswith(str(Path(tmp) / "scratch")))

    def test_main_emits_the_full_override_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, record = self._run(Path(tmp), seed=4242)
            overrides = record["overrides"]
            self.assertEqual(overrides["inference.design_startnum"], "4242")
            self.assertEqual(overrides["inference.deterministic"], "True")
            self.assertEqual(overrides["contigmap.contigs"], "[76-76]")
            self.assertEqual(overrides["diffuser.T"], "50")
            self.assertEqual(overrides["hydra.output_subdir"], "null")
            self.assertNotIn("inference.seed", overrides)

    def test_main_succeeds_only_after_verifying_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, envelope, _ = self._run(Path(tmp))
            self.assertEqual(code, 0)
            self.assertEqual(envelope["status"], "succeeded")
            self.assertEqual(len(envelope["designs"]), 1)
            self.assertEqual(envelope["designs"][0]["residue_count"], 76)
            self.assertEqual(envelope["designs"][0]["seed"], 8100)
            self.assertTrue(envelope["checkpoint"]["digest_verified"])
            self.assertTrue(envelope["accelerator"]["cuda_execution_confirmed"])
            self.assertIn("upstream_execute", envelope["phases_seconds"])
            self.assertFalse(envelope["cache_level"]["gpu_snapshot_used"])


class EnvelopeTests(unittest.TestCase):
    """A failed run must still leave a truthful, machine-readable envelope."""

    def test_failure_writes_envelope_with_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            request = tmp_path / "request.json"
            manifest = tmp_path / "manifest.json"
            output = tmp_path / "out"
            request.write_text(
                json.dumps(
                    {
                        "schema": rt.SCHEMA_REQUEST,
                        "operation": "design-backbone",
                        "parameters": parameters(contigs=["nonsense"]),
                    }
                ),
                encoding="utf-8",
            )
            manifest.write_text(
                json.dumps({"schema": rt.SCHEMA_MANIFEST, "entries": []}), encoding="utf-8"
            )
            code = rt.main(
                [
                    "run",
                    "--request", str(request),
                    "--input-manifest", str(manifest),
                    "--output", str(output),
                    "--artifact-root", str(tmp_path),
                ]
            )
            self.assertEqual(code, 1)
            envelope = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(envelope["status"], "failed")
            self.assertEqual(envelope["error"]["type"], "RequestError")
            self.assertIn("phases_seconds", envelope)

    def test_non_empty_output_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output = tmp_path / "out"
            output.mkdir()
            (output / "designs").mkdir()
            (output / "designs" / "design_0.pdb").write_text("stale", encoding="utf-8")
            request = tmp_path / "request.json"
            manifest = tmp_path / "manifest.json"
            request.write_text(
                json.dumps(
                    {"schema": rt.SCHEMA_REQUEST, "operation": "design-backbone", "parameters": parameters()}
                ),
                encoding="utf-8",
            )
            manifest.write_text(
                json.dumps({"schema": rt.SCHEMA_MANIFEST, "entries": []}), encoding="utf-8"
            )
            code = rt.main(
                [
                    "run",
                    "--request", str(request),
                    "--input-manifest", str(manifest),
                    "--output", str(output),
                    "--artifact-root", str(tmp_path),
                ]
            )
            self.assertEqual(code, 1)
            envelope = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertIn("cautious", envelope["error"]["message"])

    def test_cache_level_never_claims_a_gpu_snapshot(self) -> None:
        self.assertNotIn("snapshot", " ".join(rt.CACHE_LEVELS))
        self.assertNotIn("criu", " ".join(rt.CACHE_LEVELS).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
