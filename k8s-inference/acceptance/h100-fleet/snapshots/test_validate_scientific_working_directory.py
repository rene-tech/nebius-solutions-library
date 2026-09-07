"""The acceptance harness must preserve each controller-issued workingDir."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


def test_original_stage_runs_in_its_own_working_directory(
    tmp_path, monkeypatch, capsys
):
    path = Path(__file__).with_name("validate_scientific.py")
    spec = importlib.util.spec_from_file_location("scientific_validation_cwd", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "result"
    output.mkdir()
    (output / "result.pdb").write_text(
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00 20.00           C  \n"
    )
    workdir = tmp_path / "second-controller-request"
    workdir.mkdir()
    request = {
        "environment": {},
        "worker_variable": "FS2_RFDIFFUSION_WORKER_URL",
        "command": ["original"],
        "output": str(output),
        "working_directory": str(workdir),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr(os, "setgid", lambda value: None)
    monkeypatch.setattr(os, "setuid", lambda value: None)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="original command completed", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    exec(compile(module.REMOTE, str(path), "exec"), {})
    assert calls[0][0] == ["original"]
    assert calls[0][1]["cwd"] == str(workdir)
    assert '"status": "passed"' in capsys.readouterr().out
