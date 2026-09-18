import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

source = Path(__file__).resolve().parents[3] / "models/scientific-snapshot/serving_entrypoint_logging.py"
spec = importlib.util.spec_from_file_location("serving_entrypoint", source)
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


def test_partial_filesystem_failure_falls_back_before_starting_model(tmp_path):
    shm = tmp_path / "shm"
    shm.mkdir()
    (shm / "preexisting").write_bytes(b"unchanged")
    marker = tmp_path / "ready"
    calls = []

    def restore(*_):
        (shm / "partial").write_bytes(b"captured")
        raise ValueError("incomplete test filesystem")

    def main():
        calls.append(sys.argv.copy())
        assert not marker.exists()
        supervisor.print(json.dumps({"event": "worker_started", "pid": 150}))

    supervisor = SimpleNamespace(__file__="supervisor.py", main=main)
    argv = [
        "--directory",
        "/checkpoints/test",
        "--source-directory",
        "/bundle",
        "--fallback",
        "normal-load",
        "restore",
        "--",
        "original-server",
        "unchanged",
    ]
    entrypoint.run(
        argv,
        supervisor=supervisor,
        filesystem=SimpleNamespace(restore=restore),
        marker=marker,
        shared_memory=shm,
    )
    assert "donor" in calls[0]
    assert calls[0][-2:] == ["original-server", "unchanged"]
    assert marker.is_file()
    assert list(shm.iterdir()) == [shm / "preexisting"]
    assert "restore" in argv  # Caller policy was not mutated.


def test_require_does_not_silently_run_normal_and_cuda_event_controls_marker(tmp_path):
    marker = tmp_path / "ready"
    shm = tmp_path / "shm"
    shm.mkdir()

    def fail(*_):
        raise ValueError("test unavailable")

    supervisor = SimpleNamespace(__file__="supervisor.py", main=lambda: pytest.fail("must not launch"))
    args = ["--source-directory", "/bundle", "--fallback", "fail", "restore"]
    with pytest.raises(ValueError, match="unavailable"):
        entrypoint.run(
            args,
            supervisor=supervisor,
            filesystem=SimpleNamespace(restore=fail),
            marker=marker,
            shared_memory=shm,
        )
    assert not marker.exists()

    def restored():
        assert not marker.exists()
        supervisor.print(json.dumps({"event": "unrelated"}))
        assert not marker.exists()
        supervisor.print(json.dumps({"event": "serving_snapshot_runtime", "mechanism": "cuda-criu-restored"}))

    supervisor.main = restored
    entrypoint.run(
        args,
        supervisor=supervisor,
        filesystem=SimpleNamespace(restore=lambda *_: None),
        marker=marker,
        shared_memory=shm,
    )
    assert json.loads(marker.read_text())["mechanism"] == "cuda-criu-restored"


def test_http_health_alone_cannot_mark_gpu_restore_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(
        entrypoint.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("early HTTP probe"),
    )
    assert not entrypoint.readiness(tmp_path / "missing-marker", "http://127.0.0.1:8000/health")


def worker_messages(capsys):
    return [
        item["message"]
        for line in capsys.readouterr().out.splitlines()
        if (item := json.loads(line)).get("event") == "snapshot_worker_log"
    ]


def test_log_bridge_skips_donor_and_drains_final_incomplete_error(tmp_path, capsys):
    source = tmp_path / "source"
    source.mkdir()
    history = b"old donor prompt\nold success\n"
    (source / "worker.log").write_bytes(history)
    directory = tmp_path / "checkpoint"
    directory.mkdir()
    shm = tmp_path / "shm"
    shm.mkdir()

    def restored():
        (directory / "worker.log").write_bytes(history + b"new startup\n")
        supervisor.print(json.dumps({"event": "serving_snapshot_runtime", "mechanism": "cuda-criu-restored"}))
        with (directory / "worker.log").open("ab") as stream:
            stream.write(b"new runtime traceback without final newline")

    supervisor = SimpleNamespace(__file__="supervisor.py", main=restored)
    entrypoint.run(
        ["--directory", str(directory), "--source-directory", str(source), "--fallback", "fail", "restore"],
        supervisor=supervisor,
        filesystem=SimpleNamespace(restore=lambda *_: None),
        marker=tmp_path / "ready",
        shared_memory=shm,
    )
    assert worker_messages(capsys) == ["new startup", "new runtime traceback without final newline"]


def test_log_bridge_rotation_and_truncation_preserve_new_bytes(tmp_path, capsys):
    path = tmp_path / "worker.log"
    path.write_bytes(b"history\n")
    bridge = entrypoint.WorkerLogBridge(path, historical_bytes=path.stat().st_size)
    bridge.drain()
    with path.open("ab") as stream:
        stream.write(b"first appended\npartial")
    bridge.drain()
    path.rename(tmp_path / "worker.log.previous")
    path.write_bytes(b"rotated error\n")
    bridge.drain()
    path.write_bytes(b"cut\n")
    bridge.drain()
    bridge.stop()
    assert worker_messages(capsys) == ["first appended", "partial", "rotated error", "cut"]


def test_log_bridge_waits_for_creation_and_bounds_long_lines(tmp_path, capsys):
    path = tmp_path / "worker.log"
    bridge = entrypoint.WorkerLogBridge(path)
    bridge.drain()
    path.write_bytes(b"a" * 140000 + b"\nlast")
    bridge.drain()
    assert len(bridge.pending) < 65536
    bridge.stop()
    assert "".join(worker_messages(capsys)) == "a" * 140000 + "last"


def test_normal_fallback_logs_and_real_restore_log_directory(tmp_path, capsys):
    directory = tmp_path / "checkpoint"
    (directory / "images").mkdir(parents=True)
    (directory / "images/restore.log").write_text("Error restored example\n")
    shm = tmp_path / "shm"
    shm.mkdir()

    def failed_filesystem(*_):
        raise ValueError("missing snapshot backing file")

    def normal():
        (directory / "worker.log").write_text("fresh load\n")
        supervisor.print(json.dumps({"event": "worker_started", "pid": 150}))

    supervisor = SimpleNamespace(__file__="supervisor.py", main=normal)
    entrypoint.run(
        [
            "--directory",
            str(directory),
            "--source-directory",
            str(tmp_path / "missing"),
            "--fallback",
            "normal-load",
            "restore",
        ],
        supervisor=supervisor,
        filesystem=SimpleNamespace(restore=failed_filesystem),
        marker=tmp_path / "ready",
        shared_memory=shm,
    )
    lines = capsys.readouterr().out.splitlines()
    assert any('"message": "fresh load"' in line for line in lines)
    assert "CRIU_RESTORE_ERROR Error restored example" in lines
