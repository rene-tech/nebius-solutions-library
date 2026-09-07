import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


source = (
    Path(__file__).resolve().parents[3]
    / "models/scientific-snapshot/serving_entrypoint.py"
)
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

    supervisor = SimpleNamespace(
        __file__="supervisor.py", main=lambda: pytest.fail("must not launch")
    )
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
        supervisor.print(
            json.dumps(
                {"event": "serving_snapshot_runtime", "mechanism": "cuda-criu-restored"}
            )
        )

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
    assert not entrypoint.readiness(
        tmp_path / "missing-marker", "http://127.0.0.1:8000/health"
    )
