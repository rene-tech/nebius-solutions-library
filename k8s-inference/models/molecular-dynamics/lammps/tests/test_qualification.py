import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("validate_case", Path(__file__).parents[1] / "qualification/validate_case.py")
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


def part(*steps):
    return {"first_step": steps[0], "last_step": steps[-1], "steps": steps}


def test_trajectory_requires_every_expected_frame():
    protocol = {"warmup_steps": 2000, "target_step": 52000, "trajectory_every_steps": 10000}
    validation.coverage([part(10000, 20000), part(30000, 40000, 50000)], protocol)
    with pytest.raises(ValueError, match="missing"):
        validation.coverage([part(10000, 20000), part(40000, 50000)], protocol)


def test_same_closed_boundary_can_appear_in_both_native_parts():
    protocol = {"warmup_steps": 1000, "target_step": 5000, "trajectory_every_steps": 1000}
    validation.coverage([part(1000, 2000), part(2000, 3000, 4000, 5000)], protocol)
    with pytest.raises(ValueError, match="overlap"):
        validation.coverage([part(1000, 2000, 3000), part(2000, 3000, 4000, 5000)], protocol)
