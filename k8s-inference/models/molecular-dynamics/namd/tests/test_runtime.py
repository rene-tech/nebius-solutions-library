import copy
import json
import math
import struct

import pytest

from fs2_namd import PARAMETER_SCHEMA
from fs2_namd.contracts import normalize
from fs2_namd.worker import Workflow, binary_vectors, log_metrics, tcl, xsc_step


def request():
    return {"schema": PARAMETER_SCHEMA, "jobs": [{"id": "rep-1", "steps": [{
        "id": "md", "mode": "dynamics", "config": "run.namd", "steps": 1000,
        "output_prefix": "md", "expected_outputs": ["md.coor"]}]}]}


def test_unknown_or_ambiguous_requests_fail():
    value = request()
    value["jobs"].append(copy.deepcopy(value["jobs"][0]))
    with pytest.raises(ValueError, match="unique"):
        normalize(value)
    value = request()
    value["jobs"][0]["steps"][0]["config"] = "../outside.namd"
    with pytest.raises(ValueError):
        normalize(value)
    value = request()
    value["jobs"][0]["steps"][0]["shell"] = "anything"
    with pytest.raises(Exception):
        normalize(value)


def test_native_requires_explicit_outputs():
    value = request()
    value["jobs"][0]["steps"] = [{"id": "native", "mode": "native", "config": "run.namd"}]
    with pytest.raises(ValueError, match="expected_outputs"):
        normalize(value)


def test_vectors_validate_lengths_and_finiteness(tmp_path):
    path = tmp_path / "state.coor"
    path.write_bytes(struct.pack("<i3d", 1, 1, 2, 3))
    assert binary_vectors(path) == 1
    path.write_bytes(struct.pack("<i3d", 2, 1, 2, 3))
    with pytest.raises(ValueError, match="count/size"):
        binary_vectors(path)
    path.write_bytes(struct.pack("<i3d", 1, math.nan, 2, 3))
    with pytest.raises(ValueError, match="non-finite"):
        binary_vectors(path)


def test_xsc_requires_finite_single_state(tmp_path):
    path = tmp_path / "state.xsc"
    path.write_text("# cell\n1000 10 0 0 0 10 0 0 0 10 0 0 0\n")
    assert xsc_step(path) == 1000
    path.write_text("# cell\n1000 10 0 0 0 nan 0 0 0 10 0 0 0\n")
    with pytest.raises(ValueError, match="non-finite"):
        xsc_step(path)


def test_restart_log_does_not_confuse_first_step_with_timestep(tmp_path):
    path = tmp_path / "md.log"
    path.write_text("Info: TIMESTEP 2\nInfo: FIRST TIMESTEP 1000\nInfo: 92224 ATOMS\n"
                    "Info: 4 ATOMS IN LARGEST GROUP\n"
                    "TIMING: 2000 CPU: 1, 0.0006/step Wall: 1, 0.0005/step\n"
                    "ENERGY: " + " ".join(map(str, [2000] + [1.0] * 19)) + "\n")
    metrics = log_metrics(path)
    assert metrics["atoms"] == 92224
    assert metrics["timestep_fs"] == 2
    assert metrics["energy_records"] == 1
    assert metrics["performance_ns_per_day"] == pytest.approx(345.6)
    assert metrics["last_energy"]["TS"] == 2000


def test_input_hash_changed_after_checkpoint_is_rejected(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "run.namd").write_text("timestep 2\n")
    worker = Workflow(request(), job_id="rep-1", operation_id="op", workspace=tmp_path, checkpoint_mode="local")
    worker.initialize()
    (data / "run.namd").write_text("timestep 4\n")
    with pytest.raises(ValueError, match="immutable"):
        worker.check_inputs()
    worker = Workflow(request(), job_id="rep-1", operation_id="other", workspace=tmp_path, checkpoint_mode="local")
    with pytest.raises(ValueError, match="another operation"):
        worker.initialize()


def test_managed_script_leaves_output_cadence_and_physics_in_configuration(tmp_path):
    worker = Workflow(request(), job_id="rep-1", operation_id="op", workspace=tmp_path, checkpoint_mode="local")
    step = worker.job["steps"][0]
    script = worker.script(step, prefix="md.part000001", current=1000, count=1000,
                           restart={"coordinates": "old.coor", "velocities": "old.vel", "cell": "old.xsc"})
    assert "DCDFreq" not in script and "timestep 2" not in script
    assert "requires matching bias state" in script
    assert "cv getconfig" in script
    assert 'binCoordinates "old.coor"' in script
    assert "GPUresident]} {CUDASOAintegrate" in script
    assert tcl('$x[cmd]"') == '"\\$x\\[cmd\\]\\\""'
