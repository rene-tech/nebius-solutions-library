import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from analyze import aggregate
from medical_details import hesitation_normalized

SPEC = importlib.util.spec_from_file_location("medical_comparison", HERE / "benchmark.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class QualityTests(unittest.TestCase):
    def test_identical_ignoring_case_punctuation(self):
        result = MODULE.score("No allergies. Take two tablets.", "no allergies take two tablets")
        self.assertEqual(result["wer"], 0)
        self.assertEqual(result["cer"], 0)

    def test_negation_and_dose_errors_remain_errors(self):
        result = MODULE.score("no allergy two tablets", "allergy three tablets")
        self.assertEqual(result["deletions"], 1)
        self.assertEqual(result["substitutions"], 1)
        self.assertEqual(result["wer"], 0.5)

    def test_empty_hypothesis_is_not_removed(self):
        result = MODULE.score("no allergies", "")
        self.assertEqual(result["deletions"], 2)
        self.assertEqual(result["wer"], 1)
        self.assertEqual(result["cer"], 1)

    def test_hesitation_sensitivity_preserves_clinical_content(self):
        self.assertEqual(hesitation_normalized("Um no allergy. Uh two paracetamol tablets."),
                         "no allergy two paracetamol tablets")

    def test_aggregate_counts_failures_and_pending_separately(self):
        failed = {"status": "failed", "audio_seconds": 10, "wall_seconds": 4,
                  "quality": MODULE.score("no allergy", "")}
        pending = {"status": "started", "audio_seconds": 5}
        value = aggregate([failed, pending])
        self.assertEqual(value["requests"], 2)
        self.assertEqual(value["scored"], 1)
        self.assertEqual(value["failed_or_empty"], 1)
        self.assertEqual(value["pending"], 1)
        self.assertEqual(value["wer"], 1)


if __name__ == "__main__":
    unittest.main()
