import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("mps_report", Path(__file__).with_name("report.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def records():
    return [{"mps": enabled, "clients": clients, "repetition": repetition,
             "input_sha256": "a" * 64, "passed": True, "cpu_threads_total": 8,
             "threads_per_process": 8 // clients, "expected_client_pids": [1, 2],
             "observed_mps_client_pids": [1, 2], "aggregate_process_ns_per_day": 100 * clients,
             "cohort_wall_seconds": 50, "cgroup_cpu_delta": {"usage_usec": 200000000},
             "simulations": [{"exit_code": 0, "validation": {"passed": True},
                              "wall_seconds": 50, "native_ns_per_day": 100} for _ in range(clients)]}
            for enabled in (False, True) for clients in (1, 2, 4) for repetition in (1, 2, 3)]


def test_separates_latency_throughput_and_cpu():
    result = module.summarize(records())
    assert result["native_trajectories_checked"] == 42
    assert result["groups"][-1]["aggregate_process_ns_per_day"]["median"] == 400
    assert result["groups"][-1]["per_simulation_wall_seconds"]["median"] == 50
    assert result["groups"][-1]["throughput_ratio_to_one_process_no_mps"] == 4
    assert result["groups"][-1]["mean_cpu_cores_per_cohort"]["median"] == 4
    assert not result["hosted_gpu_sharing_qualified"]


@pytest.mark.parametrize("failure", ["missing", "duplicate", "mps", "input", "science", "cpu"])
def test_incomplete_or_unmatched_trials_cannot_be_reported(failure):
    rows = records()
    if failure == "missing":
        rows.pop()
    elif failure == "duplicate":
        rows.append(rows[0])
    elif failure == "mps":
        rows[-1]["observed_mps_client_pids"] = [1]
    elif failure == "input":
        rows[0]["input_sha256"] = "b" * 64
    elif failure == "science":
        rows[0]["simulations"][0]["validation"]["passed"] = False
    elif failure == "cpu":
        rows[0]["cpu_threads_total"] = 16
    with pytest.raises(ValueError):
        module.summarize(rows)


def test_missing_cpu_is_unknown_not_zero():
    rows = records()
    rows[0]["cgroup_cpu_delta"].clear()
    assert module.summarize(rows)["groups"][0]["mean_cpu_cores_per_cohort"] is None
