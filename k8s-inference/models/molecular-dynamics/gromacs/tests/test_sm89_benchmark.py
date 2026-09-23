import importlib.util
from pathlib import Path


source = Path(__file__).parents[1] / "qualification/benchmark_sm89.py"
spec = importlib.util.spec_from_file_location("sm89_benchmark", source)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_benchmark_requires_unchanged_tpr_output_obligations():
    assert benchmark.expected_trajectories("nstxout = 0\nnstxout-compressed = 0") == []
    assert benchmark.expected_trajectories("nstxout = 20\nnstxout-compressed = 30") == ["md.trr", "md.xtc"]
    assert benchmark.expected_trajectories("nstvout = 10 ; velocities only\nnstxout_compressed = 0") == ["md.trr"]


def test_benchmark_captures_cpu_throttling_counters(tmp_path, monkeypatch):
    path = tmp_path / "cpu.stat"
    monkeypatch.setattr(benchmark, "Path", lambda _: path)
    assert benchmark.cpu_stat() == {}
    path.write_text("usage_usec 1000\nnr_throttled 3\nthrottled_usec 90\n")
    assert benchmark.cpu_stat() == {"usage_usec": 1000, "nr_throttled": 3, "throttled_usec": 90}


def test_timeout_terminates_task_owned_process_group(tmp_path, monkeypatch):
    class TimedOutProcess:
        pid = 12345

        def communicate(self, **kwargs):
            raise benchmark.subprocess.TimeoutExpired("test-owned-process", 1)

        def wait(self, **kwargs):
            return -9

    calls = []

    def launch(argv, **kwargs):
        assert kwargs["start_new_session"] is True
        return TimedOutProcess()

    monkeypatch.setattr(benchmark.subprocess, "Popen", launch)
    monkeypatch.setattr(benchmark.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    assert benchmark.run_logged(["test-owned-process"], tmp_path / "timeout.log", timeout=1) == 124
    assert calls == [(12345, benchmark.signal.SIGKILL)]
