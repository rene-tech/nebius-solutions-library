"""Offline contract tests for the RFdiffusion runtime adapter.

No cluster, no GPU, no registry, no network. Every test either proves a bound is
enforced or proves a defect the adapter is meant to catch.
"""

from __future__ import annotations

import json
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

    def _designed_outputs(self, tmp: Path, motif_names: list[str], shift: float = 0.0) -> Path:
        designs = tmp / "designs"
        designs.mkdir(parents=True, exist_ok=True)
        prefix = designs / "design"
        residues = backbone(5, start=1)
        for offset, name in enumerate(motif_names):
            residues.append(("A", 6 + offset, name, (3.8 * offset + shift, 0.0, 0.0)))
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
            self.assertIsNotNone(verification.motif_ca_rmsd)
            self.assertAlmostEqual(verification.motif_ca_rmsd, 0.0, places=6)

    def test_mutated_motif_residue_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._designed_outputs(Path(tmp), ["LYS", "ALA", "ASP"])
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, self._scaffold_parameters(), self._reference())
            self.assertIn("identity changed", str(ctx.exception))

    def test_displaced_motif_exceeds_rmsd_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._designed_outputs(Path(tmp), ["LYS", "TRP", "ASP"], shift=9.0)
            with self.assertRaises(rt.VerificationError) as ctx:
                rt.verify_design(0, prefix, self._scaffold_parameters(), self._reference())
            self.assertIn("RMSD", str(ctx.exception))

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
