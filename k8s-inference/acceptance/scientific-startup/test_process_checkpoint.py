"""Offline lifecycle checks; live receipts qualify actual CUDA/CRIU behaviour."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "models/scientific-snapshot/process_checkpoint.py"


def module():
    spec = importlib.util.spec_from_file_location("process_checkpoint", SOURCE)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def execute(monkeypatch, capsys, tmp_path, *, action="capture", fail_criu=False):
    helper = module()
    identity = {"runtime_image": "example@sha256:locked", "gpu_uuid": "GPU-test", "driver_version": "580"}
    monkeypatch.setattr(helper, "runtime_identity", lambda: identity)
    directory = tmp_path / "images"
    if action == "restore":
        directory.mkdir()
        (directory / "worker-pid").write_text("173")
        (directory / "compatibility.json").write_text(json.dumps({
            "schema": "fs2-serve.nebius.ai/scientific-process-checkpoint/v1",
            "runtime_identity": identity, "generated_cache": [],
        }))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        failed = fail_criu and "/tools/criu" in command
        return subprocess.CompletedProcess(command, 1 if failed else 0, "", "failure" if failed else "")

    monkeypatch.setattr(helper.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", [str(SOURCE), action, "--pid", "173", "--directory", str(directory)])
    if fail_criu:
        with pytest.raises(SystemExit, match="1"):
            helper.main()
    else:
        helper.main()
    return calls, json.loads(capsys.readouterr().out), directory


def test_capture_orders_cuda_before_cpu_dump(monkeypatch, capsys, tmp_path):
    calls, receipt, directory = execute(monkeypatch, capsys, tmp_path)
    assert [call[2] for call in calls[:2]] == ["lock", "checkpoint"]
    assert calls[2][:5] == [
        "/tools/lib/ld-linux-x86-64.so.2", "--library-path", "/tools/lib", "/tools/criu", "dump",
    ]
    assert "-v4" in calls[2]
    assert "--log-level" not in calls[2]
    assert (directory / "worker-pid").read_text() == "173"
    assert receipt["status"] == "passed"


def test_failed_dump_recovers_donor(monkeypatch, capsys, tmp_path):
    calls, receipt, _ = execute(monkeypatch, capsys, tmp_path, fail_criu=True)
    assert [call[2] for call in calls[-2:]] == ["restore", "unlock"]
    assert receipt["status"] == "failed"
    assert receipt["donor_recovered"] is True


def test_restore_orders_cpu_before_cuda(monkeypatch, capsys, tmp_path):
    calls, receipt, _ = execute(monkeypatch, capsys, tmp_path, action="restore")
    assert calls[0][4] == "restore"
    assert "--restore-detached" in calls[0]
    assert [call[2] for call in calls[1:]] == ["restore", "unlock"]
    assert all(call[-1] == "173" for call in calls[1:])
    assert receipt["status"] == "passed"


def test_checkpoint_directory_is_not_overwritten(monkeypatch, capsys, tmp_path):
    directory = tmp_path / "images"
    directory.mkdir()
    (directory / "keep").write_text("existing checkpoint")
    helper = module()
    monkeypatch.setattr(helper, "runtime_identity", lambda: {})
    monkeypatch.setattr(sys, "argv", [str(SOURCE), "capture", "--pid", "173", "--directory", str(directory)])
    with pytest.raises(SystemExit, match="1"):
        helper.main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["records"] == []
    assert (directory / "keep").read_text() == "existing checkpoint"


def test_generated_code_cache_survives_donor_container(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("supervisor", SOURCE.with_name("supervisor.py"))
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
    for variable in (
        "XDG_CACHE_HOME", "TORCHINDUCTOR_CACHE_DIR", "TRITON_CACHE_DIR",
        "TORCH_EXTENSIONS_DIR", "CUDA_CACHE_PATH",
    ):
        monkeypatch.setenv(variable, "/tmp/ephemeral-cache")
    supervisor.configure_runtime_cache(tmp_path)
    assert os.environ["TRITON_CACHE_DIR"] == str(tmp_path / "cache" / "triton")
    assert Path(os.environ["TRITON_CACHE_DIR"]).is_dir()
    assert all(str(tmp_path) in os.environ[variable] for variable in (
        "XDG_CACHE_HOME", "TORCHINDUCTOR_CACHE_DIR", "TORCH_EXTENSIONS_DIR", "CUDA_CACHE_PATH",
    ))


def test_new_request_kernels_do_not_invalidate_captured_code(tmp_path):
    helper = module()
    cache = tmp_path / "cache"
    cache.mkdir()
    captured = cache / "captured.so"
    captured.write_bytes(b"captured executable")
    manifest = helper.generated_cache_manifest(tmp_path / "images")
    (cache / "new-input-shape.so").write_bytes(b"later request executable")
    helper.validate_generated_cache(tmp_path / "images", manifest)
    captured.write_bytes(b"different executable")
    with pytest.raises(ValueError, match="missing or differs"):
        helper.validate_generated_cache(tmp_path / "images", manifest)


def test_restore_scratch_copies_only_mutable_small_files(tmp_path):
    spec = importlib.util.spec_from_file_location("supervisor", SOURCE.with_name("supervisor.py"))
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
    bundle = tmp_path / "bundle"
    (bundle / "cache").mkdir(parents=True)
    (bundle / "cache" / "kernel.so").write_bytes(b"jit kernel")
    (bundle / "worker.log").write_text("worker ready\n")
    (bundle / "images").mkdir()
    (bundle / "images" / "pages.img").write_bytes(b"shared checkpoint")
    scratch = tmp_path / "attempt"
    scratch.mkdir()
    supervisor.prepare_restore_scratch(bundle, scratch)
    assert (scratch / "cache" / "kernel.so").read_bytes() == b"jit kernel"
    assert (scratch / "worker.log").read_text() == "worker ready\n"
    assert not (scratch / "images").exists()
    with pytest.raises(FileExistsError):
        supervisor.prepare_restore_scratch(bundle, scratch)
