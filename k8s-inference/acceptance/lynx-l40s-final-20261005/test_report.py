import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("final_l40s_report", Path(__file__).with_name("report.py"))
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_native_clocks_are_distinct_and_never_customer_delivered():
    result = report.rates(3, 1200, 1230, 1250)
    assert result["native_counter_ns_per_day"] == 216
    assert result["mdrun_process_ns_per_day"] == pytest.approx(210.731707317)
    assert result["native_worker_ns_per_day"] == 207.36
    assert result["public_delivered_ns_per_day"] is None


def test_missing_duration_is_unknown_not_zero_cost_or_speed():
    result = report.rates(3, None, 0, None)
    assert result["native_counter_ns_per_day"] is None
    assert result["mdrun_process_ns_per_day"] is None
    assert result["native_worker_ns_per_day"] is None
