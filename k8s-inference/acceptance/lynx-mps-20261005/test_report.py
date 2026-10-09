import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("mps_report", Path(__file__).with_name("report.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def records():
    return [{"mps": enabled, "clients": clients, "repetition": repetition,
             "input_sha256": "a" * 64, "passed": True, "cpu_threads_total": 8,
             "threads_per_process": 8 // clients, "expected_client_pids": [1, 2],
             "observed_mps_client_pids": [1, 2], "aggregate_process_ns_per_day": 100 * clients,
             "cohort_wall_seconds": 50, "cgroup_cpu_delta": {"usage_usec": 200000000},
             "simulations": [{"exit_code": 0, "validation": {"passed": True},
                              "wall_seconds": 50, "native_ns_per_day": 100} for _ in range(clients)]}
            for enabled in (False, True) for clients in (1, 2, 4) for repetition in (1, 2, 3)]


def test_separates_latency_throughput_and_cpu():
    result = module.summarize(records())
    assert result["native_trajectories_checked"] == 42
    assert result["groups"][-1]["aggregate_process_ns_per_day"]["median"] == 400
    assert result["groups"][-1]["per_simulation_wall_seconds"]["median"] == 50
    assert result["groups"][-1]["throughput_ratio_to_one_process_no_mps"] == 4
    assert result["groups"][-1]["mean_cpu_cores_per_cohort"]["median"] == 4
    assert not result["hosted_gpu_sharing_qualified"]


@pytest.mark.parametrize("failure", ["missing", "duplicate", "mps", "input", "science", "cpu"])
def test_incomplete_or_unmatched_trials_cannot_be_reported(failure):
    rows = records()
    if failure == "missing":
        rows.pop()
    elif failure == "duplicate":
        rows.append(rows[0])
    elif failure == "mps":
        rows[-1]["observed_mps_client_pids"] = [1]
    elif failure == "input":
        rows[0]["input_sha256"] = "b" * 64
    elif failure == "science":
        rows[0]["simulations"][0]["validation"]["passed"] = False
    elif failure == "cpu":
        rows[0]["cpu_threads_total"] = 16
    with pytest.raises(ValueError):
        module.summarize(rows)


def test_missing_cpu_is_unknown_not_zero():
    rows = records()
    rows[0]["cgroup_cpu_delta"].clear()
    assert module.summarize(rows)["groups"][0]["mean_cpu_cores_per_cohort"] is None


def test_gpu_units_and_missing_measurements(tmp_path):
    path = tmp_path / "gpu.csv"
    path.write_text("timestamp, uuid, utilization.gpu [%], utilization.memory [%], memory.used [MiB], power.draw [W], clocks.sm [MHz]\n"
                    "2026/10/05 12:00:00.000, GPU-1, 80 %, 30 %, 2048 MiB, 120 W, [N/A]\n"
                    "2026/10/05 12:00:00.500, GPU-1, 90 %, 40 %, 4096 MiB, 140 W, [N/A]\n")
    result = module.gpu_samples(path)
    assert result["rows"] == 2
    assert result["metrics"]["gpu_utilization_percent"]["mean"] == 85
    assert result["metrics"]["memory_used_mib"]["maximum"] == 4096
    assert result["metrics"]["sm_clock_mhz"] == {"samples": 0, "mean": None, "maximum": None}


def retained_run(tmp_path, *, passed=True, results_directory="results"):
    """A complete 18-cohort export, including files and independent receipts."""
    root = tmp_path / "failed-workspace" if not passed else tmp_path
    results = root / ("results" if not passed else results_directory)
    results.mkdir(parents=True)
    fixture = b"retained input"
    (root / "benchmark.tpr").write_bytes(fixture)
    receipt = {"passed": passed, "exit_code": 0, "cleanup": "owned Pod absent",
               "node": "node", "pod_uid": "uid", "image": "repo@sha256:abc",
               "image_id": "repo@sha256:abc", "finite_tpr_sha256": "a" * 64,
               "uploaded_sources": [{"name": "benchmark.tpr", "sha256": hashlib.sha256(fixture).hexdigest()}]}
    if passed:
        receipt["results_directory"] = results_directory
    else:
        receipt["error"] = "initial observer export failed"
    (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    rows = records()
    for row in rows:
        cohort = results / f"repeat-{row['repetition']}-mps-{'on' if row['mps'] else 'off'}-clients-{row['clients']}"
        for index, simulation in enumerate(row["simulations"]):
            member = cohort / f"simulation-{index}"
            member.mkdir(parents=True)
            payload = f"native output {index}".encode()
            (member / "md.log").write_bytes(payload)
            simulation.update(index=index, files=[{"name": "md.log", "bytes": len(payload),
                                                   "sha256": hashlib.sha256(payload).hexdigest()}])
        (cohort / "receipt.json").write_text(json.dumps(row))
    (results / "measurements.json").write_text(json.dumps(rows))
    (results / "environment.json").write_text("{}")
    (results / "summary.json").write_text('{"passed": true}')
    return results


def test_recovery_is_explicit_preserves_failure_and_hashes_every_output(tmp_path):
    retained_run(tmp_path, passed=False)
    original = (tmp_path / "receipt.json").read_bytes()
    with pytest.raises(ValueError, match="explicit recovery"):
        module.read_run(tmp_path)
    result = module.read_run(tmp_path, recover_export=True)
    assert result["observer_export_recovered"]
    assert not result["original_launcher_passed"]
    assert result["original_observer_error"] == "initial observer export failed"
    assert result["verified_native_files"] == 42
    assert (tmp_path / "receipt.json").read_bytes() == original


@pytest.mark.parametrize("failure", ["native_exit", "cleanup", "fixture", "output", "receipt", "input"])
def test_recovery_does_not_excuse_execution_or_evidence_failure(tmp_path, failure):
    results = retained_run(tmp_path, passed=False)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    if failure == "native_exit":
        receipt["exit_code"] = 1
    elif failure == "cleanup":
        receipt.pop("cleanup")
    elif failure == "fixture":
        (tmp_path / "failed-workspace/benchmark.tpr").write_bytes(b"different input")
    elif failure == "output":
        next(results.glob("*/simulation-*/md.log")).write_bytes(b"truncated")
    elif failure == "receipt":
        next(results.glob("*/receipt.json")).write_text("{}")
    else:
        receipt["finite_tpr_sha256"] = "b" * 64
    (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        module.read_run(tmp_path, recover_export=True)


def test_successful_retry_uses_recorded_results_directory(tmp_path):
    retained_run(tmp_path, results_directory="results-copy-2")
    result = module.read_run(tmp_path)
    assert result["original_launcher_passed"]
    assert not result["observer_export_recovered"]
    assert result["verified_native_files"] == 42
