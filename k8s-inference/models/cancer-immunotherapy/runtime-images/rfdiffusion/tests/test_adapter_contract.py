"""Adapter-to-image contract tests.

The scientific batch adapter owner translates a typed public request into this
image's CLI. These tests make that handoff machine-checkable from this side: the
golden argv must be current, and driving the real main() over each fixture must
execute exactly the argv the golden file promises.

Without this, "the adapter matches the image" is an assertion by inspection, and
the r8 defect showed inspection is not enough.
"""

from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import runtime_entrypoint as rt  # noqa: E402

CONTRACT = HERE / "contract"
FIXTURES = CONTRACT / "fixtures"

STUB = '''import json, os, pickle, sys
overrides = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
with open(os.environ["FS2_TEST_ARGV_RECORD"], "w") as handle:
    json.dump({"argv": sys.argv, "overrides": overrides}, handle)
prefix = overrides["inference.output_prefix"]
start = int(overrides["inference.design_startnum"])
count = int(overrides["inference.num_designs"])
reference = overrides.get("inference.input_pdb")
os.makedirs(os.path.dirname(prefix), exist_ok=True)
print("Making design %s_%d" % (prefix, start))

MOTIF = list(range(23, 35))  # the contig asks for A23-34

def read_reference(path):
    """Pull the motif residues' real N/CA/C coordinates out of the target."""
    out = {}
    for line in open(path):
        if not line.startswith("ATOM"):
            continue
        chain, seq, atom = line[21], int(line[22:26]), line[12:16].strip()
        if chain != "A" or seq not in MOTIF or atom not in ("N", "CA", "C"):
            continue
        entry = out.setdefault(seq, {"name": line[17:20].strip(), "atoms": {}})
        entry["atoms"][atom] = (
            float(line[30:38]), float(line[38:46]), float(line[46:54])
        )
    return out

if reference:
    # A faithful scaffold keeps the motif where the reference put it. Emitting
    # invented coordinates here would only prove the validator rejects them, which
    # MotifPreservationTests already covers.
    ref = read_reference(reference)
    anchor = ref[MOTIF[0]]["atoms"]["CA"]
    plan = []
    for offset in range(10):
        base = (anchor[0] - (10 - offset) * 3.8, anchor[1], anchor[2])
        plan.append(("GLY", 1 + offset, {"N": (base[0] - 1.2, base[1], base[2]),
                                         "CA": base,
                                         "C": (base[0] + 1.2, base[1], base[2])}))
    for position, seq in enumerate(MOTIF):
        plan.append((ref[seq]["name"], 11 + position, ref[seq]["atoms"]))
    tail_anchor = ref[MOTIF[-1]]["atoms"]["CA"]
    for offset in range(10):
        base = (tail_anchor[0] + (offset + 1) * 3.8, tail_anchor[1], tail_anchor[2])
        plan.append(("GLY", 23 + offset, {"N": (base[0] - 1.2, base[1], base[2]),
                                          "CA": base,
                                          "C": (base[0] + 1.2, base[1], base[2])}))
else:
    plan = []
    for position in range(76):
        base = (position * 3.8, 0.0, 0.0)
        plan.append(("GLY", position + 1, {"N": (base[0] - 1.2, base[1], base[2]),
                                           "CA": base,
                                           "C": (base[0] + 1.2, base[1], base[2])}))

for index in range(start, start + count):
    with open("%s_%d.pdb" % (prefix, index), "w") as handle:
        serial = 1
        for name, seq, atoms in plan:
            for atom in ("N", "CA", "C"):
                x, y, z = atoms[atom]
                handle.write(
                    "ATOM  %5d %-4s %3s %s%4d    %8.3f%8.3f%8.3f  1.00  0.00\\n"
                    % (serial, atom, name, "A", seq, x, y, z)
                )
                serial += 1
    trb = {"device": "NVIDIA H100 80GB HBM3", "time": 1.0}
    if reference:
        trb["con_ref_pdb_idx"] = [("A", seq) for seq in MOTIF]
        trb["con_hal_pdb_idx"] = [("A", 11 + i) for i in range(len(MOTIF))]
    with open("%s_%d.trb" % (prefix, index), "wb") as handle:
        pickle.dump(trb, handle)
print("Finished design in 0.01 minutes")
'''


def fixture_names() -> list[str]:
    return sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())


class GoldenArgvTests(unittest.TestCase):
    def test_every_fixture_has_a_current_golden(self) -> None:
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(CONTRACT / "generate_golden_argv.py"), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_golden_covers_both_operations(self) -> None:
        operations = set()
        for name in fixture_names():
            golden = json.loads((FIXTURES / name / "golden-argv.json").read_text(encoding="utf-8"))
            operations.add(golden["operation"])
        self.assertEqual(operations, {"design-backbone", "scaffold-motif"})

    def test_golden_never_emits_a_nonexistent_seed_key(self) -> None:
        for name in fixture_names():
            golden = json.loads((FIXTURES / name / "golden-argv.json").read_text(encoding="utf-8"))
            joined = " ".join(golden["upstream_argv"])
            self.assertNotIn("inference.seed=", joined)
            self.assertIn("inference.deterministic=True", golden["upstream_argv"])

    def test_golden_redirects_writes_off_the_image_tree(self) -> None:
        for name in fixture_names():
            golden = json.loads((FIXTURES / name / "golden-argv.json").read_text(encoding="utf-8"))
            schedule = next(
                a for a in golden["upstream_argv"] if a.startswith("inference.schedule_directory_path=")
            )
            self.assertNotIn("/opt/rfdiffusion", schedule)

    def test_declared_artifacts_are_relative_and_hashed(self) -> None:
        for name in fixture_names():
            golden = json.loads((FIXTURES / name / "golden-argv.json").read_text(encoding="utf-8"))
            for artifact in golden["required_artifacts"]:
                self.assertFalse(artifact["relative_path"].startswith("/"))
                self.assertNotIn("..", artifact["relative_path"])
                self.assertRegex(artifact["sha256"], r"^[0-9a-f]{64}$")
                self.assertGreater(artifact["size_bytes"], 0)


class ExecutedArgvMatchesGoldenTests(unittest.TestCase):
    """Drive the real main() over each fixture and compare the argv it runs."""

    def _stub_upstream(self, root: Path) -> Path:
        home = root / "upstream"
        (home / "scripts").mkdir(parents=True)
        (home / "scripts" / "run_inference.py").write_text(STUB, encoding="utf-8")
        return home

    def _materialise_artifacts(self, name: str, model_root: Path, input_root: Path | None = None) -> None:
        """Lay fixture artifacts out under one root or split model/input roots.

        The checkpoint is huge, so a placeholder stands in and the manifest is
        rewritten to its real digest. The point of this test is the argv and the
        path resolution, not the checkpoint bytes; digest verification itself is
        covered by ArtifactResolutionTests.
        """
        manifest = json.loads(
            (FIXTURES / name / "input-manifest.json").read_text(encoding="utf-8")
        )
        for entry in manifest["entries"]:
            artifact = entry["artifact"]
            root = (
                model_root
                if artifact["artifact_id"] == "artifact.rfdiffusion.base-ckpt"
                else (input_root or model_root)
            )
            target = root / artifact["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            source = FIXTURES / name / Path(artifact["path"]).name
            if source.is_file():
                target.write_bytes(source.read_bytes())
            else:
                target.write_bytes(b"placeholder checkpoint")
            artifact["sha256"] = rt.sha256_file(target)
            artifact["size_bytes"] = target.stat().st_size
        (model_root.parent / f"{name}-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )

    def _run_fixture(
        self,
        name: str,
        tmp: Path,
        *,
        split_input_root: bool = False,
    ) -> tuple[int, dict, dict, dict]:
        golden = json.loads((FIXTURES / name / "golden-argv.json").read_text(encoding="utf-8"))
        artifact_root = tmp / "artifacts"
        artifact_root.mkdir()
        input_root = tmp / "request-artifacts" if split_input_root else artifact_root
        if split_input_root:
            input_root.mkdir()
        self._materialise_artifacts(name, artifact_root, input_root)

        record = tmp / "argv.json"
        output = tmp / "out"
        os.environ["FS2_TEST_ARGV_RECORD"] = str(record)
        try:
            command = [
                "run",
                "--request",
                str(FIXTURES / name / "request.json"),
                "--input-manifest",
                str(tmp / f"{name}-manifest.json"),
                "--output",
                str(output),
                "--artifact-root",
                str(artifact_root),
                "--upstream-home",
                str(self._stub_upstream(tmp)),
                "--scratch",
                str(tmp / "scratch"),
            ]
            if split_input_root:
                command.extend(("--input-artifact-root", str(input_root)))
            code = rt.main(command)
        finally:
            os.environ.pop("FS2_TEST_ARGV_RECORD", None)
        envelope = json.loads((output / "result.json").read_text(encoding="utf-8"))
        executed = json.loads(record.read_text(encoding="utf-8")) if record.exists() else {}
        return code, envelope, executed, golden

    def _normalise(self, argv: list[str], tmp: Path, artifact_root: Path) -> list[str]:
        """Map run-local paths onto the container paths the golden uses."""
        mapping = [
            (str(artifact_root), "/opt/fs2/artifacts"),
            (str(tmp / "scratch"), "/tmp/fs2-rfdiffusion"),
            (str(tmp / "out"), "/workspace/run"),
            (str(tmp / "upstream"), "/opt/rfdiffusion"),
        ]
        out = []
        for token in argv:
            for local, container in mapping:
                token = token.replace(local, container)
            out.append(token)
        return out

    def test_executed_argv_equals_golden(self) -> None:
        for name in fixture_names():
            with self.subTest(fixture=name):
                with tempfile.TemporaryDirectory() as raw:
                    tmp = Path(raw)
                    code, envelope, executed, golden = self._run_fixture(name, tmp)
                    self.assertEqual(code, 0, envelope.get("error"))
                    # The stub records sys.argv, which starts at the script path;
                    # the golden's first element is the interpreter.
                    actual = self._normalise(executed["argv"], tmp, tmp / "artifacts")
                    self.assertEqual(actual, list(golden["upstream_argv"])[1:])

    def test_expected_markers_are_the_files_produced(self) -> None:
        for name in fixture_names():
            with self.subTest(fixture=name):
                with tempfile.TemporaryDirectory() as raw:
                    tmp = Path(raw)
                    code, envelope, _, golden = self._run_fixture(name, tmp)
                    self.assertEqual(code, 0, envelope.get("error"))
                    produced = set()
                    for design in envelope["designs"]:
                        produced.add(design["pdb"]["path"])
                        produced.add(design["run_metadata"]["path"])
                    self.assertEqual(produced, set(golden["expected_markers"]))

    def test_motif_fixture_reports_preserved_motif(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            code, envelope, _, _ = self._run_fixture("scaffold-motif", tmp)
            self.assertEqual(code, 0, envelope.get("error"))
            design = envelope["designs"][0]
            self.assertEqual(design["motif_positions_preserved"], 12)
            self.assertIsNotNone(design["motif_ca_rmsd_angstrom"])
            self.assertEqual(envelope["operation"], "scaffold-motif")
            self.assertEqual(design["residue_count"], 32)

    def test_checkpoint_and_motif_input_can_use_distinct_roots(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            code, envelope, _, _ = self._run_fixture(
                "scaffold-motif",
                tmp,
                split_input_root=True,
            )
            self.assertEqual(code, 0, envelope.get("error"))
            self.assertTrue(envelope["checkpoint"]["path"].startswith(str(tmp / "artifacts")))
            self.assertEqual(envelope["input_pdb"]["artifact_id"], "artifact.rfdiffusion.target.1ubq")

    def test_design_indices_match_golden(self) -> None:
        for name in fixture_names():
            with self.subTest(fixture=name):
                with tempfile.TemporaryDirectory() as raw:
                    tmp = Path(raw)
                    _, envelope, _, golden = self._run_fixture(name, tmp)
                    self.assertEqual(
                        [d["design_index"] for d in envelope["designs"]],
                        golden["design_indices"],
                    )


class FrozenContractTests(unittest.TestCase):
    """The CLI is frozen while the adapter owner translates against it."""

    def test_cli_surface_is_unchanged(self) -> None:
        parser = rt.build_parser()
        actions = {a.dest for a in parser._subparsers._group_actions[0].choices["run"]._actions}
        for required in (
            "request", "input_manifest", "output", "artifact_root", "input_artifact_root", "upstream_home",
            "checkpoint_artifact_id", "scratch", "timeout_seconds", "cache_level",
            "skip_checkpoint_digest",
        ):
            self.assertIn(required, actions, f"run subcommand lost --{required.replace('_','-')}")

    def test_internal_parameter_schema_is_unchanged(self) -> None:
        self.assertEqual(rt.SCHEMA_PARAMETERS, "fs2-serve.nebius.ai/rfdiffusion-parameters/v1")
        self.assertEqual(rt.ADAPTER_ID, "rfdiffusion-v1-1-0-base-v1")
        self.assertEqual(set(rt.OPERATIONS), {"design-backbone", "scaffold-motif"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
