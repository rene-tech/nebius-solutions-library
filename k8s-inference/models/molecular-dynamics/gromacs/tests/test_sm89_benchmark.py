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
