"""Evidence integrity tests; these do not certify the subjective adjudications."""

import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path

from clinical_fidelity import FACTS, GRADES, MODELS, evidence, normalized

HERE = Path(__file__).resolve().parent


class ClinicalEvidenceTests(unittest.TestCase):
    def test_checklist_is_explicit_and_complete(self):
        self.assertEqual(len(FACTS), 44)
        self.assertEqual(len({fact[0] for fact in FACTS}), 44)
        for identifier, category, fact, anchor, grades, note in FACTS:
            self.assertEqual(len(grades), 3)
            self.assertTrue(set(grades) <= GRADES.keys())
            self.assertTrue(all((identifier, category, fact, anchor, note)))

    def test_literal_evidence_survives_annotation_normalization(self):
        text = "She said <UNSURE>no</UNSURE> fever.\nTwo tablets."
        item = evidence(text, "no fever")[0]
        self.assertEqual(item["quote"], normalized(text)[item["start"]:item["end"]])
        self.assertIn("no fever", item["quote"])

    def test_saved_review_is_traceable_to_actual_first_outputs(self):
        path = HERE / "clinical-fidelity.json"
        if not path.exists():
            self.skipTest("Run clinical_fidelity.py after collecting the benchmark receipts")
        report = json.loads(path.read_text())
        self.assertEqual(len(report["items"]), 44)
        counts = {model: Counter() for model in MODELS}
        for item in report["items"]:
            for model in MODELS:
                row = json.loads((HERE / "results-r1" / f"{model}-{item['case']}-r1.json").read_text())
                observed = item["models"][model]
                self.assertEqual(observed["operation_id"], row["operation_id"])
                self.assertEqual(observed["transcript_sha256"], hashlib.sha256(row["text"].encode()).hexdigest())
                counts[model][observed["grade"]] += 1
                for expected_text, quotes in ((row["reference"], item["reference_evidence"]), (row["text"], observed["evidence"])):
                    for quote in quotes:
                        self.assertEqual(quote["quote"], normalized(expected_text)[quote["start"]:quote["end"]])
        for model in MODELS:
            self.assertEqual(report["totals"][model], {grade: counts[model][grade] for grade in GRADES})


if __name__ == "__main__":
    unittest.main()
