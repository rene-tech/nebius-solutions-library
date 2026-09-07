"""Offline equivalence checks; these tests never contact a cluster."""

import copy
import json
import unittest

import run_remediation_trial as trial


class RemediationTrialTests(unittest.TestCase):
    def setUp(self):
        self.baseline = json.loads(trial.BASELINE.read_bytes())

    def test_new_cohort_metadata_does_not_change_scientific_fixture(self):
        candidate = copy.deepcopy(self.baseline)
        candidate["run_id"] = "new-cohort"
        candidate["source_commit"] = "updated-control-plane-source"
        for row in candidate["scenarios"]:
            row["client_context"]["correlation_id"] = f"new-cohort.{row['id']}"
            row["request_sha256"] = "different-because-metadata-changed"
        trial.verify_same_campaign(self.baseline, candidate)

    def test_fixture_parameters_and_priorities_cannot_be_weakened(self):
        for field in trial.SCENARIO_FIELDS:
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.baseline)
                candidate["scenarios"][0][field] = "changed"
                with self.assertRaises(ValueError):
                    trial.verify_same_campaign(self.baseline, candidate)

    def test_runner_and_concurrency_are_unchanged(self):
        for field in ("runner_sha256", "scenarios_sha256", "max_parallel_clients", "operation_count"):
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.baseline)
                candidate[field] = "changed"
                with self.assertRaises(ValueError):
                    trial.verify_same_campaign(self.baseline, candidate)


if __name__ == "__main__":
    unittest.main()
