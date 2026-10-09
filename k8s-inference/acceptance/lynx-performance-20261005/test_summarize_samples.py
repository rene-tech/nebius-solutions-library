import json

import pytest

from summarize_samples import numeric, summarize


def sample(second, usage, utilization="50"):
    return {"observed_at": f"2026-10-05T16:00:{second:02d}+00:00", "pod_uid": "uid", "pod": "pod", "node": "node",
            "labels": {"fs2.nebius.ai/operation-id": "op"}, "container": "scientific-stage", "measurements": {
                "gpu": {"returncode": 0, "stdout": f"GPU-a, NVIDIA L40S, 580, 46068, 1000, {utilization}, 1, 120\n"},
                "cpu_memory_counters": {"returncode": 0, "stdout": f"usage_usec {usage}\nnr_periods {second+1}\n"
                                       f"nr_throttled {second//2}\nthrottled_usec {second*1000}\n4096\n8192\n"}}}


def test_observed_samples_are_not_allocation_integrals(tmp_path):
    path = tmp_path / "samples.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in (sample(0, 1_000_000), sample(10, 81_000_000, "70"))))
    pod = summarize(path)["pods"][0]
    assert pod["mean_cpu_cores_over_sample_window"] == 8
    assert pod["throttled_period_fraction"] == .5
    assert pod["devices"][0]["gpu_utilization_percent"]["sample_mean"] == 60
    assert pod["observed_memory_peak_bytes"] == 8192
    assert pod["counter_delta"]["throttled_usec"] == 10000
    assert "cost" not in pod


def test_reset_and_unavailable_samples_remain_unknown(tmp_path):
    path = tmp_path / "samples.jsonl"
    a, b = sample(0, 100), sample(10, 10, "[N/A]")
    a["measurements"]["gpu"] = {"error": "observation_timeout"}
    path.write_text("\n".join(json.dumps(row) for row in (a, b)))
    pod = summarize(path)["pods"][0]
    assert pod["mean_cpu_cores_over_sample_window"] is None
    assert pod["reset_counter_fields"] == 1 and pod["failed_gpu_queries"] == 1
    assert pod["devices"][0]["gpu_utilization_percent"]["sample_mean"] is None
    assert summarize(tmp_path / "missing")["status"] == "missing"


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "[N/A]", None])
def test_invalid_device_counter_is_not_zero(value):
    assert numeric(value) is None
