"""Offline checks for the customer-only wrapper, not another live campaign."""

import json
import unittest
from pathlib import Path

import run_customer_trial as trial

# The live command decorates the existing scenario function for its isolated
# process. Do not leak that decoration into unrelated tests sharing pytest.
trial.scenario_client.prepare_scenario = trial.BASE_PREPARE


class CustomerTrialTests(unittest.TestCase):
    def test_campaign_keeps_declared_fixture_bytes(self):
        scenarios = json.loads(Path(trial.__file__).with_name("scenarios.json").read_bytes())
        fragments = {item.model_id: item.path for item in trial.fleet.discover_inputs(trial.ROOT)}
        self.assertEqual(len(scenarios), 14)
        for scenario in scenarios:
            config = trial.public.RunConfig(
                endpoint="https://example.invalid", repository_root=trial.ROOT,
                activation_fragment=fragments[scenario["model_id"]],
                receipt_path=Path("unused"), run_id=f"trial-test.{scenario['id']}",
            )
            original = trial.public._activation(config)
            _, request, declarations, _ = trial.prepare_trial_scenario(config, scenario)
            self.assertEqual(declarations, original[2])
            self.assertEqual(request["input_manifest"], original[1]["input_manifest"])
            self.assertEqual(request["client_context"]["correlation_id"], config.run_id)
            if "parameters" not in scenario:
                self.assertEqual(request["parameters"], original[1]["parameters"])

    def test_bulk_override_is_already_accepted_scenario(self):
        scenarios = json.loads(Path(trial.__file__).with_name("scenarios.json").read_bytes())
        source = json.loads((trial.ROOT / "acceptance/scientific-fleet/scenarios/customer-readiness.json").read_bytes())
        accepted = next(row for row in source if row["id"] == "rfdiffusion-four-designs")
        actual = next(row for row in scenarios if row["id"] == "batch-09-rfdiffusion-bulk")
        for key in ("model_id", "parameters", "expected_shards", "service_class"):
            self.assertEqual(actual[key], accepted[key])

    def test_resource_release_requires_all_attempts(self):
        receipt = {"queue": {"observed_stages": [{"attempts": [{"resource_released": True}, {"resource_released": False}]}]}}
        self.assertFalse(trial.resource_release(receipt))
        receipt["queue"]["observed_stages"][0]["attempts"][1]["resource_released"] = True
        self.assertTrue(trial.resource_release(receipt))

    def test_trace_projects_only_status_fields(self):
        result = trial.projection({"operation": {"id": "op", "status": "queued", "unrelated_request": "not retained"}, "batch": {"status": "pending"}, "unrelated": "not retained"})
        self.assertEqual(result["operation"]["id"], "op")
        self.assertNotIn("unrelated_request", result["operation"])
        self.assertNotIn("unrelated", result)
        trial.public._assert_redacted(result)


if __name__ == "__main__":
    unittest.main()
