import argparse
import json
from pathlib import Path
import stat
import tempfile
import unittest

from lynx_workloads import ACTUAL_SOURCES, ASSUMED_CASES, digest, prepare


class Preparation(unittest.TestCase):
    def inputs(self, root):
        client = root / "client"
        qualification = client / "scripts/qualification"
        qualification.mkdir(parents=True)
        (qualification / "default-release-cases.json").write_text(json.dumps({"cases": [
            {"case_id": name, "prompt": "Assumed " + name} for name in ASSUMED_CASES]}))
        (qualification / "hosted-default-cases.json").write_text(json.dumps({"cases": [
            {"case_id": "hosted-gromacs", "prompt": "Assumed unchanged alanine example"}]}))
        original = root / "original.json"
        original.write_text(json.dumps({"cases": [{"case_id": name, "prompt":
            "Inventory /workspace/private/original.cif." if name == "cif-inventory" else "Private original " + name}
            for name in ACTUAL_SOURCES]}))
        cif = root / "original.cif"
        cif.write_text("private input")
        return argparse.Namespace(client_root=client, private_cases=original,
            private_cases_sha256=digest(original), private_cif=cif, private_cif_sha256=digest(cif),
            public_cif=None, public_cif_sha256=None,
            output=root / "output")

    def test_exact_actual_prompts_and_assumed_cases_stay_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.inputs(Path(directory))
            before = args.private_cases.read_bytes()
            plan = prepare(args)
            actual = json.loads((args.output / "actual-agent-cases.json").read_text())["cases"]
            self.assertEqual([row["prompt"] for row in actual],
                             [row["prompt"] for row in json.loads(before)["cases"]])
            self.assertTrue(all(row["evidence_class"] == "customer_reported" for row in actual))
            self.assertEqual(len(plan["assumed_cpu_cases"]), 6)
            self.assertTrue(all(name.startswith("assumed-") for name in plan["assumed_cpu_cases"]))
            self.assertNotIn("Private original", json.dumps(plan))
            self.assertEqual(args.private_cases.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(args.output.stat().st_mode), 0o700)
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in args.output.iterdir()))
            self.assertEqual(plan["private_input_bindings"][0]["target"], "/workspace/private/original.cif")

    def test_original_hash_drift_refused_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.inputs(Path(directory))
            args.private_cif.write_text("modified customer input")
            with self.assertRaisesRegex(ValueError, "hash differs"):
                prepare(args)
            self.assertFalse(args.output.exists())

    def test_no_overwrite_or_private_manifest_in_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.inputs(root)
            prepare(args)
            with self.assertRaises(FileExistsError):
                prepare(args)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").write_text("gitdir: elsewhere")
            args.output = repo / "private"
            with self.assertRaisesRegex(ValueError, "Git checkout"):
                prepare(args)

    def test_assumed_only_has_no_original_customer_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.inputs(Path(directory))
            args.private_cases = args.private_cases_sha256 = args.private_cif = args.private_cif_sha256 = None
            plan = prepare(args)
            self.assertEqual(plan["actual_cases"], [])
            self.assertEqual(plan["private_input_bindings"], [])
            self.assertFalse(plan["release_qualified"])

    def test_missing_original_field_or_mixed_actual_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.inputs(Path(directory))
            args.private_cif_sha256 = None
            with self.assertRaisesRegex(ValueError, "both explicit files"):
                prepare(args)
            args.private_cif_sha256 = digest(args.private_cif)
            original = json.loads(args.private_cases.read_text())
            original["cases"].append({"case_id": "hosted-gromacs", "prompt": "not a customer case"})
            args.private_cases.write_text(json.dumps(original))
            args.private_cases_sha256 = digest(args.private_cases)
            with self.assertRaisesRegex(ValueError, "three actual Lynx"):
                prepare(args)

    def test_public_equivalent_control_has_separate_pinned_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.inputs(Path(directory))
            args.public_cif = Path(directory) / "1UBQ.cif"
            args.public_cif.write_text("public structure control")
            args.public_cif_sha256 = digest(args.public_cif)
            plan = prepare(args)
            self.assertTrue(plan["public_inventory_ready"])
            binding = plan["public_input_bindings"][0]
            self.assertEqual(binding["evidence_class"], "assumed_representative")
            self.assertEqual(binding["target"], "/workspace/inputs/1UBQ.cif")
            self.assertNotEqual(binding["sha256"], plan["private_input_bindings"][0]["sha256"])
            args.output = Path(directory) / "second-output"
            args.public_cif.write_text("changed public bytes")
            with self.assertRaisesRegex(ValueError, "Public inventory control hash"):
                prepare(args)


if __name__ == "__main__":
    unittest.main()
