import importlib.util

from conftest import SOLUTION_ROOT

spec = importlib.util.spec_from_file_location(
    "benchmark_measurements", SOLUTION_ROOT / "acceptance/performance-placement-20260920/measurements.py"
)
measurements = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measurements)


def test_only_observed_phase_values_are_reported_not_estimates_or_missing():
    assert measurements.observed({"value": 0, "unit": "seconds", "evidence": "measured"}, "seconds") == 0
    assert measurements.observed({"value": 8, "unit": "seconds", "evidence": "estimated"}, "seconds") is None
    assert measurements.observed(None, "seconds") is None


def test_hardware_requires_actual_matching_node_and_homogeneous_identity():
    node = {
        "uid": "node",
        "pool": "h100-full",
        "gpu_product": "H100",
        "gpus_per_node": "8",
        "cpu_arch": "amd64",
        "driver_version": "580.173.02",
        "local_storage": "absent",
    }
    image = "sha256:" + "1" * 64
    actual = measurements.hardware([node], {"node"}, 1, image, {"weights": "fixed"})
    assert actual["gpus_per_node"] == 8
    assert actual["gpu_count"] == 1 and actual["topology"] == "single-device"
    assert measurements.hardware([node], {"missing"}, 1, image, {}) is None
    assert (
        measurements.hardware([node, {**node, "uid": "other", "pool": "other"}], {"node", "other"}, 2, image, {})
        is None
    )
