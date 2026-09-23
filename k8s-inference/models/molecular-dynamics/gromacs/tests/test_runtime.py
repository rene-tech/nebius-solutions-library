import io
import json
import tarfile

import pytest
from jsonschema import ValidationError

from fs2_gromacs import PARAMETER_SCHEMA
from fs2_gromacs.contracts import normalize
from fs2_gromacs.files import atomic_json, extract_inputs, inventory
from fs2_gromacs.worker import Workflow, expand_args, parse_mdp, publish_final_coordinates


def request():
    return {"schema": PARAMETER_SCHEMA, "jobs": [{"id": "replica", "steps": [
        {"id": "md", "command": "mdrun", "args": ["-s", "run.tpr"]},
    ]}]}


def test_defaults_do_not_change_physics_or_assume_snapshot_support():
    original = request()
    actual = normalize(original)
    assert actual["jobs"][0]["steps"][0]["args"] == ["-s", "run.tpr"]
    assert actual["threads"] == 8 and actual["checkpoint_minutes"] == actual["segment_minutes"] == 5
    assert "threads" not in original


@pytest.mark.parametrize("field,value", [("threads", 9), ("threads", True), ("segment_minutes", 0),
                                       ("gpu_snapshot", True), ("max_wall_seconds", 0)])
def test_invalid_options_are_not_silently_ignored(field, value):
    body = request()
    body[field] = value
    with pytest.raises(ValidationError):
        normalize(body)


@pytest.mark.parametrize("argument", ["-ntmpi", "-maxh", "-cpi", "-multidir", "-replex", "-plumed"])
def test_platform_managed_or_unqualified_mdrun_flags_are_explicit(argument):
    body = request()
    body["jobs"][0]["steps"][0]["args"] += [argument, "1"]
    with pytest.raises(ValueError):
        normalize(body)


@pytest.mark.parametrize("name", ["../outside", "/absolute", "dir/../file", "data\\file", "x\x00y"])
def test_input_paths_remain_relative(name):
    body = request()
    body["jobs"][0]["steps"][0]["restart_checkpoint"] = name
    with pytest.raises(ValueError):
        normalize(body)


def test_independent_jobs_and_step_ids_are_unambiguous():
    body = request()
    body["jobs"].append(body["jobs"][0].copy())
    with pytest.raises(ValueError, match="job IDs"):
        normalize(body)
    body = request()
    body["jobs"][0]["steps"] *= 2
    with pytest.raises(ValueError, match="step IDs"):
        normalize(body)


def test_explicit_file_expansion_retains_native_parts(tmp_path):
    for number in (3, 1, 2):
        (tmp_path / f"md.part{number:04d}.xtc").write_bytes(b"frame")
    assert expand_args(["-f", {"files": "md.part*.xtc"}, "-o", "combined.xtc"], tmp_path) == [
        "-f", "md.part0001.xtc", "md.part0002.xtc", "md.part0003.xtc", "-o", "combined.xtc",
    ]
    assert expand_args(["*.xtc"], tmp_path) == ["*.xtc"]  # no implicit shell
    with pytest.raises(ValueError, match="no files"):
        expand_args([{"files": "absent*.xtc"}], tmp_path)


def test_final_coordinates_are_a_copy_not_a_renamed_trajectory(tmp_path):
    (tmp_path / "md.part0001.gro").write_bytes(b"early")
    (tmp_path / "md.part0002.gro").write_bytes(b"final")
    publish_final_coordinates(tmp_path, ["-deffnm", "md"])
    assert (tmp_path / "md.gro").read_bytes() == b"final"
    assert (tmp_path / "md.part0001.gro").read_bytes() == b"early"
    assert (tmp_path / "md.part0002.gro").read_bytes() == b"final"


def test_dump_mdp_is_read_as_metadata_not_used_as_a_new_protocol():
    assert parse_mdp("; comment\nintegrator = md ; inline\ninit_step = 100\nnsteps = 200\n") == {
        "integrator": "md", "init-step": "100", "nsteps": "200",
    }


def test_archive_extraction_preserves_includes_and_checks_expanded_bytes(tmp_path):
    archive = tmp_path / "input.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        file = tarfile.TarInfo("forcefield/atoms.itp")
        file.size = 12
        output.addfile(file, io.BytesIO(b"parameters\n\n"))
    extract_inputs(archive, tmp_path / "data", max_bytes=1000)
    assert (tmp_path / "data/forcefield/atoms.itp").read_bytes() == b"parameters\n\n"
    assert len(inventory(tmp_path / "data", max_bytes=1000)) == 1
    with pytest.raises(ValueError, match="budget"):
        extract_inputs(archive, tmp_path / "too-small", max_bytes=5)


def test_local_checkpoint_binds_the_recipe_operation_and_job(tmp_path):
    worker = Workflow(request(), job_id="replica", operation_id="one", workspace=tmp_path)
    worker.data.mkdir()
    (worker.data / "state.cpt").write_bytes(b"closed checkpoint bytes")
    worker.checkpoint()
    ready = json.loads((worker.meta / "checkpoint-ready.json").read_text())
    assert ready["state"]["operation_id"] == "one" and ready["state"]["generation"] == 1
    assert ready["files"][0]["path"] == "state.cpt"
    state = ready["state"]
    state["operation_id"] = "different"
    atomic_json(worker.meta / "gromacs-state.json", state)
    with pytest.raises(ValueError, match="another workflow"):
        worker.initialize()


def test_inventory_rejects_changing_output_type(tmp_path):
    (tmp_path / "link").symlink_to("elsewhere")
    with pytest.raises(ValueError, match="regular files"):
        inventory(tmp_path, max_bytes=1000)


@pytest.mark.parametrize("phase", ["restore", "publish"])
def test_peer_transport_failure_releases_engine_without_the_handoff_timeout(tmp_path, monkeypatch, phase):
    worker = Workflow(request(), job_id="replica", operation_id="one", workspace=tmp_path,
                      checkpoint_mode="companion")
    atomic_json(worker.meta / "transport-error.json", {"status": "failed", "phase": phase})
    monkeypatch.setattr("fs2_gromacs.worker.time.sleep",
                        lambda _: (_ for _ in ()).throw(AssertionError("should not wait after peer failure")))
    if phase == "publish":
        with pytest.raises(RuntimeError, match="transport failed"):
            worker._wait_json(worker.meta / "checkpoint-ack.json", lambda _: True)
    else:
        result = worker.run()
        assert result["status"] == "failed"
        assert result["completed_steps"] == []
        assert result["error"] == "durable checkpoint transport failed; see operation logs"


@pytest.mark.parametrize("phase", ["restore", "publish"])
def test_pod_termination_does_not_wait_for_the_terminated_companion(tmp_path, monkeypatch, phase):
    from fs2_gromacs.worker import Interrupted

    worker = Workflow(request(), job_id="replica", operation_id="one", workspace=tmp_path,
                      checkpoint_mode="companion")
    worker.stopped = True
    monkeypatch.setattr("fs2_gromacs.worker.time.sleep",
                        lambda _: (_ for _ in ()).throw(AssertionError("must release within Pod grace")))
    if phase == "publish":
        with pytest.raises(Interrupted, match="last committed remote checkpoint"):
            worker._wait_json(worker.meta / "checkpoint-ack.json", lambda _: True)
    else:
        result = worker.run()
        assert result["status"] == "interrupted"
        assert result["completed_steps"] == []
