"""Checkpoint processes must use only the device allocated to their Pod."""

import importlib.util
import os
import json
from pathlib import Path
import signal

import pytest

SOURCE = (
    Path(__file__).resolve().parents[3] / "models/scientific-snapshot/supervisor.py"
)


def load():
    spec = importlib.util.spec_from_file_location("snapshot_supervisor", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_explicit_device_plugin_assignment_is_preserved(monkeypatch):
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "GPU-allocated")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    load().bind_allocated_gpu()
    assert os.environ["CUDA_VISIBLE_DEVICES"] == "GPU-allocated"


def test_cdi_assignment_resolves_single_accessible_device(monkeypatch):
    supervisor = load()
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "void")
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)

    class Driver:
        def cuInit(self, flags):
            return 0

        def cuDeviceGetCount(self, count):
            count._obj.value = 1
            return 0

        def cuDeviceGetUuid(self, identity, ordinal):
            assert ordinal == 0
            identity._obj[:] = bytes(range(16))
            return 0

    monkeypatch.setattr(supervisor.ctypes, "CDLL", lambda _: Driver())
    supervisor.bind_allocated_gpu()
    assert (
        os.environ["CUDA_VISIBLE_DEVICES"] == "GPU-00010203-0405-0607-0809-0a0b0c0d0e0f"
    )


def test_multiple_exposed_devices_are_not_guessed(monkeypatch):
    supervisor = load()
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "void")

    class Driver:
        def cuInit(self, flags):
            return 0

        def cuDeviceGetCount(self, count):
            count._obj.value = 8
            return 0

    monkeypatch.setattr(supervisor.ctypes, "CDLL", lambda _: Driver())
    with pytest.raises(ValueError, match="exactly one accessible"):
        supervisor.bind_allocated_gpu()


def test_fallback_releases_entire_captured_cohort(monkeypatch, tmp_path):
    supervisor = load()
    images = tmp_path / "images"
    images.mkdir()
    (images / "worker-pid").write_text("170")
    (images / "cuda-pids.json").write_text(json.dumps([190]))
    (images / "process-pids.json").write_text(json.dumps([190, 180, 170]))
    killed = []
    monkeypatch.setattr(
        supervisor.os, "kill", lambda pid, sig: killed.append((pid, sig))
    )
    supervisor.stop_restored_worker(tmp_path)
    assert set(killed) == {
        (170, signal.SIGKILL),
        (180, signal.SIGKILL),
        (190, signal.SIGKILL),
    }
    assert len(killed) == 3


def test_fallback_never_kills_pid_one(monkeypatch, tmp_path):
    supervisor = load()
    images = tmp_path / "images"
    images.mkdir()
    (images / "worker-pid").write_text("170")
    (images / "process-pids.json").write_text("[1, 170]")
    monkeypatch.setattr(
        supervisor.os, "kill", lambda *args: pytest.fail("must validate before killing")
    )
    with pytest.raises(ValueError, match="child processes"):
        supervisor.stop_restored_worker(tmp_path)


def test_flashinfer_has_durable_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASHINFER_WORKSPACE_BASE", "/root")
    load().configure_runtime_cache(tmp_path)
    assert os.environ["FLASHINFER_WORKSPACE_BASE"] == str(
        tmp_path / "cache/flashinfer-workspace"
    )
