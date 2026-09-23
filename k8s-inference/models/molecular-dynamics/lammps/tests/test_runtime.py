import copy
import json
import signal
from pathlib import Path

import pytest
from fs2_lammps import PARAMETER_SCHEMA
from fs2_lammps.contracts import normalize
from fs2_lammps.worker import Interrupted, Workflow, restart_step


def request(**kwargs):
    return {"schema": PARAMETER_SCHEMA, "jobs": [{"id": "lj", "steps": [{"id": "md", "input": "in.lj"}]}], **kwargs}


def worker(tmp_path, body=None, **kwargs):
    w = Workflow(body or request(), job_id="lj", operation_id="op", workspace=tmp_path, binary="/test/lmp", **kwargs)
    w.data.mkdir(parents=True)
    w.meta.mkdir()
    (w.data / "in.lj").write_text("run 10\n")
    return w


def test_native_variables_are_single_argv_values_not_shell_tokens():
    body = request()
    body["jobs"][0]["steps"][0]["variables"] = {"seed": "12345", "label": "native value with spaces"}
    assert normalize(body)["jobs"][0]["steps"][0]["variables"]["seed"] == "12345"


@pytest.mark.parametrize("path", ["../input", "/input", "a/../../input", ".fs2/state", "a\\b"])
def test_unsafe_bundle_paths_rejected(path):
    body = request()
    body["jobs"][0]["steps"][0]["input"] = path
    with pytest.raises(ValueError):
        normalize(body)


def test_request_never_silently_discards_unknown_parameters():
    from jsonschema import ValidationError
    with pytest.raises(ValidationError):
        normalize(request(timestep=0.5))
    body = request()
    body["jobs"][0]["steps"][0]["variables"] = {"fs2_restart": "0"}
    with pytest.raises(ValueError):
        normalize(body)


def test_checkpoint_binds_full_recipe_and_keeps_complete_inputs(tmp_path):
    w = worker(tmp_path)
    (w.data / "potential.eam").write_bytes(b"potential coefficients")
    w.checkpoint()
    ready = json.loads((w.meta / "checkpoint-ready.json").read_text())
    assert {f["path"] for f in ready["files"]} == {"in.lj", "potential.eam"}
    assert ready["state"]["recipe_sha256"] == w.recipe
    assert ready["state"]["generation"] == 1
    changed = request(threads=1)
    other = Workflow(changed, job_id="lj", operation_id="op", workspace=tmp_path, binary="/test/lmp")
    with pytest.raises(ValueError, match="recipe"):
        other.initialize()


def test_transport_failure_and_cancellation_never_wait_until_gpu_timeout(tmp_path):
    w = worker(tmp_path, checkpoint_mode="companion")
    (w.meta / "transport-error.json").write_text('{"status":"failed"}')
    with pytest.raises(RuntimeError, match="transport failed"):
        w._wait_json(w.meta / "checkpoint-ack.json", lambda _: True)
    w.stop(signal.SIGTERM, None)
    with pytest.raises(Interrupted):
        w._wait_json(w.meta / "checkpoint-ack.json", lambda _: True)


def test_segmented_restart_checks_native_timestep_and_preserves_all_parts(tmp_path, monkeypatch):
    body = request()
    step = body["jobs"][0]["steps"][0]
    step["continuation"] = {"input": "in.resume", "restart_file": "state.restart", "progress_file": "progress", "target_step": 20}
    step["expected_outputs"] = ["trajectory.1", "trajectory.2"]
    w = worker(tmp_path, body)
    (w.data / "in.resume").write_text("read_restart state.restart\ninclude physics.inc\nrun 20 upto\n")
    (w.data / "physics.inc").write_text("fix integrator all nve\n")
    calls = []

    def execute(argv, *, cwd, log):
        calls.append(argv)
        (cwd / "state.restart").write_bytes(b"native restart")
        (cwd / "progress").write_text(str(len(calls) * 10))
        (cwd / f"trajectory.{len(calls)}").write_bytes(b"closed trajectory")
        log.write_text("Loop time of 1 on 1 procs for 10 steps with 32 atoms\n")
        return 0, 1.0

    monkeypatch.setattr(w, "execute", execute)
    monkeypatch.setattr(w, "read_restart_step", lambda *args: len(calls) * 10)
    w.run_step(w.job["steps"][0])
    assert [c[-1] for c in calls] == ["in.lj", "in.resume"]
    assert w.state["completed_steps"] == ["md"]
    assert w.state["generation"] == 2
    assert (w.data / "physics.inc").is_file()
    assert len(list(w.data.glob("trajectory.*"))) == 2


def test_native_progress_disagreement_is_not_committed(tmp_path, monkeypatch):
    body = request()
    body["jobs"][0]["steps"][0]["continuation"] = {"input": "in.resume", "restart_file": "state.restart", "progress_file": "progress", "target_step": 20}
    w = worker(tmp_path, body)
    def execute(argv, *, cwd, log):
        (cwd / "state.restart").write_bytes(b"restart")
        (cwd / "progress").write_text("20")
        return 0, 1.0
    monkeypatch.setattr(w, "execute", execute)
    monkeypatch.setattr(w, "read_restart_step", lambda *args: 10)
    with pytest.raises(ValueError, match="disagree"):
        w.run_step(w.job["steps"][0])
    assert w.state["completed_steps"] == []
    assert not (w.meta / "checkpoint-ready.json").exists()


def test_failure_retains_diagnostics_without_success_checkpoint(tmp_path, monkeypatch):
    w = worker(tmp_path)
    monkeypatch.setattr(w, "initialize", lambda: None)
    def execute(argv, *, cwd, log):
        log.write_text("ERROR: invalid potential\n")
        return 1, 0.1
    monkeypatch.setattr(w, "execute", execute)
    result = w.run()
    assert result["status"] == "failed"
    assert result["completed_steps"] == []
    assert result["native_checkpoint_generation"] == 0
    assert any(f["path"].endswith(".log") for f in result["files"])
    assert result["gpu_snapshot_used"] is False


def test_inventory_failure_still_produces_explicit_failure_receipt(tmp_path, monkeypatch):
    w = worker(tmp_path)
    monkeypatch.setattr(w, "initialize", lambda: None)
    monkeypatch.setattr(w, "run_step", lambda step: (w.data / "bad").symlink_to("in.lj"))
    result = w.run()
    assert result["status"] == "failed"
    assert result["inventory_error"]
    assert (tmp_path / "result.json").is_file()


def test_native_restart_metadata_parser():
    assert restart_step("Timestep = 25000\n") == 25000
    with pytest.raises(ValueError):
        restart_step("version 25000")
