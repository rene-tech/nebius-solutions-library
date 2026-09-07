"""CPU tests for optional serving process-tree capture support."""

import importlib.util
from pathlib import Path
import subprocess

SOURCE = (
    Path(__file__).resolve().parents[3]
    / "models/scientific-snapshot/process_checkpoint.py"
)


def module():
    spec = importlib.util.spec_from_file_location("fleet_checkpoint", SOURCE)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def test_process_tree_includes_thread_children_but_not_unrelated_process(tmp_path):
    for pid, children in ((10, "12 11"), (11, ""), (12, "13"), (13, ""), (99, "")):
        task = tmp_path / str(pid) / "task" / str(pid)
        task.mkdir(parents=True)
        (task / "children").write_text(children)
    assert module().process_tree(10, tmp_path) == [11, 13, 12, 10]


def test_cuda_probe_selects_only_initialized_running_descendants(monkeypatch):
    helper = module()

    def probe(command, **_):
        initialized = command[-1] == "13"
        return subprocess.CompletedProcess(
            command, 0 if initialized else 1, "running\n" if initialized else "", ""
        )

    monkeypatch.setattr(helper.subprocess, "run", probe)
    selected, probes = helper.cuda_processes([11, 13, 12, 10], Path("/tools"))
    assert selected == [13]
    assert len(probes) == 4
