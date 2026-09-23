import importlib.util
import json
from pathlib import Path

import pytest


def test_summary_keeps_native_timing_energy_and_gpu_evidence(tmp_path, monkeypatch):
    source = Path(__file__).parents[1] / "qualification/summarize_sm89.py"
    monkeypatch.syspath_prepend(str(source.parent))
    spec = importlib.util.spec_from_file_location("sm89_summary", source)
    summary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(summary)
    assert summary.timing_buckets(
        " Neighbor search  1  8  201  14.361  229.227  10.5\n"
        " Force  1  8  20001  80.247  1280.888  58.9\n"
        " Rest  8.466  135.134  6.2\n"
    ) == {"Neighbor search": 10.5, "Force": 58.9, "Rest": 6.2}
    run = tmp_path / "warm-1"
    run.mkdir()
    (tmp_path / "measurements.json").write_text(json.dumps([{
        "cohort": "warm-1", "exit_code": 0, "process_wall_seconds": 12,
        "native_ns_per_day": 25,
        "validation": {"passed": True, "expected_steps": 200000, "expected_time_ps": 400},
        "output_inventory": [],
        "pod_cpu_stat_delta": {"nr_periods": 10, "nr_throttled": 2},
    }]))
    (tmp_path / "input.mdp").write_text("init-step = 0\nnstxout-compressed = 1000\n")
    (run / "md.xtc.check.log").write_text("Last frame 200 time 400.000\n")
    (run / "time.txt").write_text("Percent of CPU this job got: 700%\n")
    (run / "gpu-samples.csv").write_text(
        "utilization.gpu [%], power.draw [W], clocks.current.sm [MHz], memory.used [MiB]\n"
        "60 %, 240 W, 2500 MHz, 900 MiB\n"
    )
    (run / "md.log").write_text("Time: 80 10.0 800\n Force 1 8 100 5 10 50.0\n")
    (run / "energy-validation.xvg").write_text('@ s0 legend "Temperature"\n0 298\n40 300\n')
    result = summary.summarize(tmp_path)
    row = result["runs"][0]
    assert row["outside_native_timed_wall_seconds"] == 2
    assert row["timing_bucket_percent"] == {"Force": 50}
    assert row["pod_throttled_period_fraction"] == 0.2
    assert row["process_sm_clock_mhz_mean"] == 2500
    assert row["process_gpu_memory_mib_max"] == 900
    assert row["energy_terms"]["Temperature"] == {"minimum": 298, "maximum": 300, "final": 300}
    assert row["native_trajectory_checks"]["md.xtc"]["expected_frames_verified"] == 201
    assert result["validated_warm_successes"] == 1
    (run / "md.xtc.check.log").write_text("Last frame 100 time 400.000\n")
    with pytest.raises(AssertionError):
        summary.summarize(tmp_path)
