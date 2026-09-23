"""SYNTHETIC transport fixtures only; no simulated or scientific evidence."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from package_analysis import digest, package
from spec_paths import map_paths, resolve_spec


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-portable-analysis-")
        self.root = Path(self.temp.name)
        self.source = self.root / "original"
        self.bundle = self.root / "delivered"
        self.source.mkdir()
        self.bundle.mkdir()
        master = self.source / "master"
        master.mkdir()
        (master / "protocol.json").write_text('{"fixture":"SYNTHETIC"}')
        (master / "system.prmtop").write_text("SYNTHETIC not a topology")
        (master / "master-manifest.json").write_text(json.dumps({"files": [{"path": "system.prmtop", "sha256": digest(master / "system.prmtop")}]}))
        data = self.source / "run" / "data"
        data.mkdir(parents=True)
        for filename in ("part1.dump", "part2.dump", "topology", "log1", "log2"):
            (data / filename).write_text("SYNTHETIC " + filename)
        (self.source / "receipt.json").write_text('{"scientific_evidence":false}')
        self.spec = {"evidence_kind": "synthetic-unit-fixture", "path_base": "spec-directory", "master_directory": "master", "runs": [{"engine": "lammps", "trajectory": ["run/data/part1.dump", "run/data/part2.dump"], "native_topology": "run/data/topology", "production_log": ["run/data/log1", "run/data/log2"], "thermo": {"path": ["run/data/log1", "run/data/log2"]}, "provenance_files": ["receipt.json"], "provenance": {"informational_string": "keep this unchanged"}}]}
        self.spec_file = self.source / "spec.json"
        self.spec_file.write_text(json.dumps(self.spec))
        shutil.copytree(master, self.bundle / "master")
        shutil.copytree(self.source / "run", self.bundle / "runs" / "lammps")
        self.maps = [(master, "master"), (self.source / "run", "runs/lammps")]

    def tearDown(self):
        self.temp.cleanup()

    def test_relative_resolution_is_spec_based_and_does_not_mutate(self):
        actual = resolve_spec(self.spec, self.spec_file)
        self.assertEqual(actual["runs"][0]["trajectory"][1], str(self.source / "run/data/part2.dump"))
        self.assertEqual(self.spec["master_directory"], "master")
        self.assertEqual(actual["runs"][0]["provenance"], self.spec["runs"][0]["provenance"])
        missing_base = {key: value for key, value in self.spec.items() if key != "path_base"}
        with self.assertRaisesRegex(ValueError, "relative dependency"):
            resolve_spec(missing_base, self.spec_file)
        with self.assertRaisesRegex(ValueError, "unknown"):
            resolve_spec({**self.spec, "path_base": "cwd"}, self.spec_file)

    def test_package_relocates_without_original_and_without_raw_duplicates(self):
        output = package(self.spec_file, self.bundle, self.maps)
        receipt = json.loads((output / "packaging-receipt.json").read_text())
        self.assertFalse(receipt["raw_files_duplicated"])
        self.assertFalse(list(output.rglob("*.dump")))
        self.assertEqual(len(list((output / "provenance").iterdir())), 1)
        moved = self.root / "unpacked elsewhere"
        self.bundle.rename(moved)
        shutil.rmtree(self.source)  # Exact synthetic temp fixture only.
        portable = moved / "analysis-inputs/spec.json"
        resolved = resolve_spec(json.loads(portable.read_text()), portable)
        seen = []
        map_paths(resolved, lambda value: seen.append(Path(value)) or value)
        self.assertTrue(all(path.exists() and path.is_relative_to(moved) for path in seen))
        self.assertTrue((moved / "analysis-inputs/regenerate.py").is_file())
        self.assertTrue((moved / "analysis-inputs/code/compare.py").is_file())

    def test_mismatched_delivered_bytes_rejected_and_failure_retained(self):
        (self.bundle / "runs/lammps/data/part1.dump").write_text("TAMPERED SYNTHETIC")
        with self.assertRaisesRegex(ValueError, "differs"):
            package(self.spec_file, self.bundle, self.maps)
        self.assertTrue((self.bundle / "analysis-inputs/packaging-failure.json").is_file())

    def test_unmapped_native_input_rejected(self):
        with self.assertRaisesRegex(ValueError, "explicit source-to-bundle map"):
            package(self.spec_file, self.bundle, self.maps[:1])

    def test_pressure_dependency_paths_and_uri_rejection(self):
        spec = json.loads(json.dumps(self.spec))
        spec["runs"][0]["pressure_observations"] = {"path": "p.csv", "provenance_files": ["source.json"], "method": "untouched description"}
        resolved = resolve_spec(spec, self.spec_file)
        self.assertEqual(resolved["runs"][0]["pressure_observations"]["path"], str(self.source / "p.csv"))
        spec["runs"][0]["trajectory"] = "https://example.invalid/input"
        with self.assertRaisesRegex(ValueError, "local paths"):
            resolve_spec(spec, self.spec_file)


if __name__ == "__main__":
    unittest.main()
