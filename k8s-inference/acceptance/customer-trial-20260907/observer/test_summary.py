"""Pure tests for trial measurement boundaries; no network/model invocation."""

import copy
import unittest

from summarize_observation import summarize


def sample(at="2026-09-07T14:52:28Z"):
    return {
        "started_at": at,
        "completed_at": at,
        "elapsed_seconds": 1,
        "pods": {"data": []},
        "nodes": {"data": []},
        "prom_operations": {"data": {"data": {"result": []}}},
    }


def pod(uid="historical", phase="Failed"):
    return {
        "uid": uid,
        "name": uid,
        "namespace": "fs2-models",
        "node": "node",
        "created_at": "2026-09-07T14:00:00Z",
        "phase": phase,
        "labels": {},
        "resources": [],
        "container_statuses": [],
    }


class SummaryTest(unittest.TestCase):
    def test_missing_queue_is_not_zero(self):
        result = summarize([sample()])
        self.assertEqual(result["queued_operations"]["samples"], 0)
        self.assertIsNone(result["queued_operations"]["max"])
        self.assertEqual(result["missing_metric_sample_counts"], {"operations": 1})

    def test_historical_failure_is_not_new_incident(self):
        baseline = sample()
        baseline["pods"]["data"] = [pod()]
        after = copy.deepcopy(baseline)
        after["started_at"] = after["completed_at"] = "2026-09-07T14:52:53Z"
        after["pods"]["data"].append(pod("new-failure"))
        result = summarize([baseline, after])
        self.assertEqual(len(result["baseline_failed_pods_excluded"]), 1)
        self.assertEqual(
            [row["name"] for row in result["new_failed_pods"]], ["new-failure"]
        )

    def test_gpu_integral_is_explicit_sample_approximation(self):
        baseline = sample()
        baseline["prom_gpu_utilization"] = {
            "data": {
                "data": {
                    "result": [
                        {
                            "metric": {"UUID": "gpu1", "namespace": "fs2-models"},
                            "value": [0, "20"],
                        }
                    ]
                }
            }
        }
        after = sample("2026-09-07T14:52:53Z")
        result = summarize([baseline, after])["approximate_sampled_gpu_seconds"]
        self.assertEqual(result["hardware_busy_fraction_integral"], 5)
        self.assertEqual(result["dcgm_coverage_seconds"], 25)

    def test_missing_kube_data_does_not_report_empty_cluster(self):
        baseline = sample()
        baseline["nodes"] = {"error": "timeout"}
        baseline["pods"] = {"error": "timeout"}
        result = summarize([baseline])
        self.assertIsNone(result["allocatable_gpus"]["min"])
        self.assertIsNone(result["scheduler_requested_gpus"]["min"])


if __name__ == "__main__":
    unittest.main()
