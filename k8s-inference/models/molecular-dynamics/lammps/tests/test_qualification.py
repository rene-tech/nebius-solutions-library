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
