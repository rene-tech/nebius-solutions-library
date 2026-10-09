import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from fs2_gromacs.files import extract_inputs
from fs2_lammps.contracts import normalize

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


def test_customer_starter_contract_and_bundle_are_accepted(tmp_path):
    example = Path(__file__).parents[1] / "examples/lj-native"
    request = normalize(json.loads((example / "request.json").read_text()))
    archive = tmp_path / "input.tar.gz"
    names = ["in.prepare", "in.production", "in.resume", "in.analyze", "protocol.inc", "production.inc"]
    subprocess.run(["tar", "-C", str(example / "inputs"), "-czf", str(archive), *names], check=True)
    extract_inputs(archive, tmp_path / "data", max_bytes=1000000)
    assert len(list((tmp_path / "data").iterdir())) == 6
    assert request["jobs"][0]["steps"][1]["continuation"]["target_step"] == 51000


def test_restart_comparison_never_compares_different_physical_steps():
    row = [1000, 100, 300, -10, 2, -8, 1, 200]
    following = [1100, 100, 302, -10.1, 2, -8.1, 1, 200]
    result = validation.restart_continuity([[row], [following]], [{"native_restart_step": 1100}])
    assert result[0]["numerical_continuity_measured"] is False
    assert "relative_energy_jump" not in result[0]
    with pytest.raises(ValueError, match="independently decoded"):
        validation.restart_continuity([[row], [following]], [{"native_restart_step": 1200}])
    measured = validation.restart_continuity([[row], [row]], [{"native_restart_step": 1000}])
    assert measured[0]["relative_energy_jump"] == 0


def test_native_empty_part_is_allowed_only_with_aggregate_frame_coverage(tmp_path):
    path = tmp_path / "trajectory.1.lammpstrj"
    path.touch()
    empty = validation.trajectory(path, 8000, allow_empty=True)
    assert empty["frames"] == 0
    with pytest.raises(ValueError, match="no frames"):
        validation.trajectory(path, 8000)
    protocol = {"warmup_steps": 2000, "target_step": 22000, "trajectory_every_steps": 10000}
    validation.coverage([empty, part(10000), empty, part(20000)], protocol)
    with pytest.raises(ValueError, match="missing"):
        validation.coverage([empty, part(10000), empty], protocol)
