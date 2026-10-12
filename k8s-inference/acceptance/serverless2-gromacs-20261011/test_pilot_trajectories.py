"""Offline checks of the acceptance observer; not scientific-result evidence."""

import hashlib
import json
import os
import subprocess

import pilot
import pytest


@pytest.mark.parametrize("fail", [False, True])
def test_private_receipts_use_operator_uid_and_keep_diagnostics(tmp_path, monkeypatch, fail):
    directory = tmp_path / "run-files"
    directory.mkdir()
    files = []
    for index in range(2):
        data = f"mock trajectory {index}".encode()
        digest = hashlib.sha256(data).hexdigest()
        source = directory / digest
        source.write_bytes(data)
        source.chmod(0o600)
        files.append({"path": f"part{index}.xtc", "sha256": digest, "size_bytes": len(data)})
    pilot.write(tmp_path / "run.json", {"steps": 2000})
    pilot.write(tmp_path / "run-result.json", {"native_result": {"files": files}})
    monkeypatch.setattr(pilot, "command", lambda *args: "sha256:mock-image")
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        assert args[args.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
        assert "--read-only" in args and args[args.index("--network") + 1] == "none"
        if "check" in args:
            first = 0 if len(calls) == 1 else 2
            output = f"# Atoms 4096\nReading frame 0 time {first}\nCoords 2 2.0\n"
        else:
            output = "a" * 64 + " /tmp/previous.xtc\n"
        return subprocess.CompletedProcess(args, 1 if fail else 0, output, "mock diagnostic")

    monkeypatch.setattr(pilot.subprocess, "run", execute)
    if fail:
        with pytest.raises(subprocess.CalledProcessError):
            pilot.trajectories(tmp_path, "run", "mock-image")
    else:
        pilot.trajectories(tmp_path, "run", "mock-image")
        assert len(calls) == 3  # Both checks and the native boundary comparison.
        assert json.loads((tmp_path / "run-trajectory-check.json").read_text())["unique_frames"] == 3
    diagnostic = tmp_path / "run-gmx-check-0.json"
    assert diagnostic.stat().st_mode & 0o777 == 0o600
    assert json.loads(diagnostic.read_text())["stderr"] == "mock diagnostic"
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())
