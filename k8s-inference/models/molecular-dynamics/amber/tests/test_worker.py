import hashlib
import json
import signal

import pytest

from fs2_amber import ENGINE_ID, PARAMETER_SCHEMA
from fs2_amber.contracts import canonical, normalize
from fs2_amber.validation import native_completion, restart_metadata, validate_tool_log
from fs2_amber.worker import Workflow, native_argv


def request():
    return {"schema": PARAMETER_SCHEMA, "jobs": [{"id": "test", "steps": [{"id": "md", "input": "md.in", "topology": "system.top", "coordinates": "start.rst7", "expected_nsteps": 10}]}]}


def output(path, *, steps=10, ending=""):
    path.write_text(f"NATOM = 3\nimin = 0, nstlim = 10, dt = 0.002\nNSTEP = {steps} TIME(PS) = 0.020 TEMP(K) = 298.0\nEtot = -10.0 EKtot = 1.0 EPtot = -11.0\n5. TIMINGS\n| Run   done at now\n{ending}")


def restart(path, *, velocities=True):
    numbers = [1.0] * (18 if velocities else 9)
    path.write_text("test restart\n3 0.020\n" + "\n".join("".join(f"{v:12.7f}" for v in numbers[i:i + 6]) for i in range(0, len(numbers), 6)) + "\n")


def worker(tmp_path, *, companion=False):
    w = Workflow(request(), job_id="test", operation_id="op", workspace=tmp_path, binaries={"cuda-spfp": "/test/pmemd"}, checkpoint_mode="companion" if companion else "local")
    w.data.mkdir()
    w.meta.mkdir()
    for name in ("md.in", "system.top", "start.rst7"):
        (w.data / name).write_text("native fixture placeholder\n")
    return w


def test_fixed_native_flags_and_subprocess_tools_environment(tmp_path):
    w = worker(tmp_path)
    step = w.job["steps"][0]
    argv = native_argv(step, "/test/pmemd")
    assert argv[:8] == ["/test/pmemd", "-O", "-i", "md.in", "-p", "system.top", "-c", "start.rst7"]
    assert argv[argv.index("-r") + 1] == "md.rst7"
    env = w.environment({"kind": "cpptraj"})
    assert env["AMBERHOME"] == "/opt/ambertools"
    assert env["LD_LIBRARY_PATH"] == "/opt/ambertools/lib"
    assert native_argv({"kind": "tleap", "input": "leap.in"}, "tleap") == ["tleap", "-f", "leap.in"]


def test_native_completion_is_exact_and_finite(tmp_path):
    path = tmp_path / "out"
    step = normalize(request())["jobs"][0]["steps"][0]
    output(path)
    assert native_completion(path, step)["completed_native_steps"] == 10
    output(path, steps=9)
    with pytest.raises(ValueError, match="exact"):
        native_completion(path, step)
    output(path, ending="Etot = NaN")
    with pytest.raises(ValueError, match="non-finite"):
        native_completion(path, step)
    output(path, ending="| Wall clock limit reached (timlim = 1 s), step 9")
    with pytest.raises(InterruptedError, match="timlim"):
        native_completion(path, step)


def test_ascii_restart_checks_velocity_context(tmp_path):
    path = tmp_path / "rst7"
    restart(path)
    assert restart_metadata(path, require_velocities=True)["atoms"] == 3
    restart(path, velocities=False)
    with pytest.raises(ValueError, match="velocities"):
        restart_metadata(path, require_velocities=True)
    assert restart_metadata(path, require_velocities=False)["velocities"] is False


def test_clean_stages_commit_before_success_result(tmp_path, monkeypatch):
    w = worker(tmp_path, companion=True)
    calls = []
    def ack(path, predicate, **kwargs):
        assert not (tmp_path / "result.json").exists()
        value = {"status": "ready"} if path.name == "restore-complete.json" else {"status": "committed", "generation": w.state["generation"]}
        assert predicate(value)
        calls.append(path.name)
        return value
    monkeypatch.setattr(w, "_wait_json", ack)
    def execute(argv, *, step, cwd, log):
        log.write_text("native stdout\n")
        output(cwd / "md.mdout")
        restart(cwd / "md.rst7")
        return 0, 1.5
    monkeypatch.setattr(w, "execute", execute)
    result = w.run()
    assert result["status"] == "succeeded"
    assert result["completed_steps"] == ["md"]
    assert calls == ["restore-complete.json", "checkpoint-ack.json", "checkpoint-ack.json"]
    assert result["recipe_sha256"] == hashlib.sha256(canonical({"request": normalize(request()), "job": "test", "image": ENGINE_ID})).hexdigest()
    state = json.loads((w.meta / "amber-state.json").read_text())
    assert state["generation"] == 2
    assert result["gpu_snapshot_used"] is False


def test_native_failure_diagnostic_does_not_create_success_generation(tmp_path, monkeypatch):
    w = worker(tmp_path)
    def execute(argv, *, step, cwd, log):
        log.write_text("Native input error\n")
        return 1, 0.1
    monkeypatch.setattr(w, "execute", execute)
    result = w.run()
    assert result["status"] == "failed"
    assert result["completed_steps"] == []
    ready = json.loads((w.meta / "checkpoint-ready.json").read_text())
    assert ready["state"]["generation"] == 1
    assert not any(item["path"].endswith(".log") for item in ready["files"])
    assert any(item["path"].endswith(".log") for item in result["files"])


def test_restored_recipe_must_match_and_completed_prefix_is_exact(tmp_path):
    w = worker(tmp_path)
    w.checkpoint()
    wrong = request()
    wrong["threads"] = 2
    other = Workflow(wrong, job_id="test", operation_id="op", workspace=tmp_path, binaries={"cpu": "test"})
    with pytest.raises(ValueError, match="recipe"):
        other.initialize()
    state = json.loads((w.meta / "amber-state.json").read_text())
    state["completed_steps"] = ["other"]
    (w.meta / "amber-state.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="prefix"):
        w.initialize()


def test_cancellation_and_transport_failure_are_not_success(tmp_path):
    w = worker(tmp_path)
    (w.meta / "transport-error.json").write_text("{}")
    with pytest.raises(RuntimeError, match="transport failed"):
        w._wait_json(w.meta / "ack", lambda _: True)
    w.stop(signal.SIGTERM, None)
    assert w.run()["status"] == "interrupted"


def test_leap_error_count_and_tool_native_errors(tmp_path):
    path = tmp_path / "tool.log"
    path.write_text("Exiting LEaP: Errors = 0; Warnings = 2; Notes = 1.\n")
    assert validate_tool_log(path, "tleap")["native_error_scan"] == "passed"
    path.write_text("Exiting LEaP: Errors = 1; Warnings = 2; Notes = 1.\n")
    with pytest.raises(ValueError):
        validate_tool_log(path, "tleap")
    path.write_text("Error: native topology mismatch\n")
    with pytest.raises(ValueError):
        validate_tool_log(path, "cpptraj")
