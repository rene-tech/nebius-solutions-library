"""Offline lifecycle checks; live receipts qualify actual CUDA/CRIU behaviour."""

import importlib.util
import json
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
    directory = tmp_path / "images"
    if action == "restore":
        directory.mkdir()
        (directory / "worker-pid").write_text("173")
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
    monkeypatch.setattr(sys, "argv", [str(SOURCE), "capture", "--pid", "173", "--directory", str(directory)])
    with pytest.raises(SystemExit, match="1"):
        helper.main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["records"] == []
    assert (directory / "keep").read_text() == "existing checkpoint"
