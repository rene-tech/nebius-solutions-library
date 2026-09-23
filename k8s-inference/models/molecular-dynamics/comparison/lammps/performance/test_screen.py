import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("canonical_performance_screen", Path(__file__).with_name("screen.py"))
screen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screen)


def test_orthogonal_control_changes_only_cell_representation_before_kspace_definition():
    baseline = screen.configuration("baseline-triclinic", 1000, 200)
    ortho = screen.configuration("orthogonal", 1000, 200)
    delta = 'if "$(xy) != 0 || $(xz) != 0 || $(yz) != 0" then "quit 17"\nchange_box all ortho\n'
    assert ortho.replace(delta, "") == baseline
    assert ortho.index("change_box") < ortho.index("include nonbonded.inc")
    assert "run 0\n" not in ortho
    assert ortho.index("fix thermal") < ortho.index("include constraints.inc") < ortho.index("fix integrate")
    static = screen.configuration("orthogonal", 1000, 200, static=True)
    assert "run 0\n" in static and "fix thermal" not in static
    assert "initial-forces" in static
    assert "run 200 post no\nrun 1000 pre no\n" in baseline
    assert "pre no" not in screen.configuration("orthogonal", 1000, 0)
    assert "dump coordinates all custom 500" in baseline


@pytest.mark.parametrize("args", [("unregistered", 1000, 200), ("orthogonal", 0, 200), ("orthogonal", 1000, -1)])
def test_invalid_control_is_not_silently_substituted(args):
    with pytest.raises(ValueError):
        screen.configuration(*args)


def test_native_library_path_matches_worker_and_diagnostics_do_not_leak_into_timing(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/usr/local/cuda/lib64")
    monkeypatch.setenv("CUDA_LAUNCH_BLOCKING", "1")
    env = screen.native_environment("/usr/local/lammps/sm90/bin/lmp")
    assert env["LD_LIBRARY_PATH"] == "/usr/local/lammps/sm90/lib:/usr/local/cuda/lib64"
    assert "CUDA_LAUNCH_BLOCKING" not in env
    assert screen.native_environment("/usr/local/lammps/sm90/bin/lmp", launch_blocking=True)["CUDA_LAUNCH_BLOCKING"] == "1"


def test_bounded_rank_control_keeps_one_gpu_and_one_thread():
    single = screen.native_command("lmp", 1, "benchmark.in")
    assert single[:7] == ["lmp", "-k", "on", "g", "1", "t", "1"]
    for ranks in (2, 4):
        assert screen.native_command("lmp", ranks, "benchmark.in") == ["mpirun", "--bind-to", "core", "-np", str(ranks)] + single
    with pytest.raises(ValueError):
        screen.native_command("lmp", 8, "benchmark.in")
