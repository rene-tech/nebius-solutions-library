"""Small offline tests for coverage and evidence freshness, without cluster use."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("aggregate", Path(__file__).with_name("aggregate.py"))
aggregate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aggregate)


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "report").mkdir()
        assignments = {
            "boltz2": ["boltz2"], "openfold": ["openfold2", "openfold3"],
            "protenix": ["protenix-v2"],
            "coverage": sorted(aggregate.EXPECTED - {"boltz2", "openfold2", "openfold3", "protenix-v2"}),
        }
        for lane, names in assignments.items():
            self.write(f"{lane}/result.json", {"models": [
                {**{key: None for key in aggregate.REQUIRED}, "model_id": name}
                for name in names
            ]})
        for source in aggregate.SNAPSHOT_SOURCES:
            self.write(source, {"models": [], "test_fixture": True})

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def run_aggregate(self):
        with patch.object(aggregate, "ROOT", self.root), patch("sys.argv", ["aggregate.py"]), contextlib.redirect_stdout(io.StringIO()):
            aggregate.main()
        return json.loads((self.root / "report/comparison.json").read_text())

    def record_review(self):
        sources = [f"{lane}/result.json" for lane in ("boltz2", "openfold", "coverage", "protenix")]
        sources += list(aggregate.SNAPSHOT_SOURCES)
        self.write("report/review.json", {"passed": True, "source_sha256": {
            source: hashlib.sha256((self.root / source).read_bytes()).hexdigest()
            for source in sources
        }})

    def test_complete_coverage_is_not_review_approval(self):
        result = self.run_aggregate()
        self.assertTrue(result["scope_complete"])
        self.assertFalse(result["acceptance_review_complete"])
        self.assertEqual(12, len(result["models"]))
        self.assertFalse(result["production_promoted"])

    def test_missing_required_model_fails(self):
        self.write("boltz2/result.json", {"models": []})
        with self.assertRaisesRegex(ValueError, "coverage incomplete"):
            self.run_aggregate()

    def test_changed_evidence_invalidates_review(self):
        self.record_review()
        self.assertTrue(self.run_aggregate()["acceptance_review_complete"])
        self.write("snapshot/protenix/result.json", {"changed": True})
        self.assertFalse(self.run_aggregate()["acceptance_review_complete"])

    def test_partial_review_cannot_approve_missing_snapshot(self):
        (self.root / "snapshot/openfold3/result.json").unlink()
        self.write("report/review.json", {"passed": True, "source_sha256": {}})
        result = self.run_aggregate()
        self.assertFalse(result["acceptance_review_complete"])
        self.assertEqual(["snapshot/openfold3/result.json"], result["missing_snapshot_studies"])

    def test_duplicate_model_fails(self):
        document = json.loads((self.root / "boltz2/result.json").read_text())
        document["models"].append(document["models"][0])
        self.write("boltz2/result.json", document)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.run_aggregate()


if __name__ == "__main__":
    unittest.main()
