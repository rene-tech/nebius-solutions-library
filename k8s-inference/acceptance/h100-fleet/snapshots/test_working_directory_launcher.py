import importlib.util
from pathlib import Path


SOURCE = Path(__file__).parents[3] / "models/scientific-snapshot/working_directory_launcher.py"
spec = importlib.util.spec_from_file_location("working_directory_launcher", SOURCE)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_launch_restores_workdir_and_identity_before_exact_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher.os, "chdir", lambda value: calls.append(("cwd", value)))
    monkeypatch.setattr(launcher.os, "setgroups", lambda value: calls.append(("groups", value)))
    monkeypatch.setattr(launcher.os, "setgid", lambda value: calls.append(("gid", value)))
    monkeypatch.setattr(launcher.os, "setuid", lambda value: calls.append(("uid", value)))
    monkeypatch.setattr(
        launcher.os, "execvp", lambda executable, argv: calls.append(("exec", executable, argv))
    )
    command = ["python", "-m", "uvicorn", "server:app"]
    launcher.launch(command, "/opt/fs2", 1000, 1000)
    assert calls == [
        ("cwd", "/opt/fs2"),
        ("groups", []),
        ("gid", 1000),
        ("uid", 1000),
        ("exec", "python", command),
    ]
