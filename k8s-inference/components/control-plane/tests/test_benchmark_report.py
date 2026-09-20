import importlib.util
import sys

from conftest import SOLUTION_ROOT

HERE = SOLUTION_ROOT / "acceptance/performance-placement-20260920"
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("benchmark_report", HERE / "report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_report_excludes_failed_and_missing_latency_from_success_median():
    trials = []
    for index, (status, elapsed) in enumerate([("succeeded", 2), ("succeeded", None), ("failed", 100), ("running", None), ("unsupported", None)]):
        trials.append({"model_id": "example", "workload_class": "two-requests", "status": status,
                       "id": str(index), "result": {"elapsed_seconds": elapsed, "error_code": "fixture_failure"}})
    rendered = report.render([{"id": "campaign", "name": "baseline", "spec_sha256": "a" * 64, "trials": trials}], "now")
    assert "| example / two-requests | 2 | 1 | 1 | 1 | 1; 2.00 / 2.00 / 2.00 |" in rendered
    assert "fixture_failure" in rendered
    assert "Not a cold-start, hardware-winner or customer-readiness claim" in rendered


def test_unmeasured_report_never_substitutes_zero():
    rendered = report.render([{"id": "c", "name": "n", "spec_sha256": "a" * 64, "trials": [
        {"model_id": "m", "workload_class": "w", "status": "succeeded", "result": {"elapsed_seconds": None}}
    ]}], "now")
    assert "| m / w | 1 | 0 | 0 | 0 | — |" in rendered
