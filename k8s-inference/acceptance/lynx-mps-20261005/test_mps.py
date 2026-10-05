import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "models/molecular-dynamics/gromacs/qualification"))


def load(name):
    spec = importlib.util.spec_from_file_location("mps_test_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("clients", [1, 2, 4])
def test_cpu_budget_and_science_are_identical(clients):
    argv = load("benchmark_mps").command(Path("/work/benchmark.tpr"), clients)
    assert clients * int(argv[argv.index("-ntomp") + 1]) == 8
    assert argv[argv.index("-ntmpi") + 1] == "1"
    assert argv[argv.index("-nstlist") + 1] == "200"
    assert "-nsteps" not in argv and "-dt" not in argv
    assert "-cpi" not in argv


def test_pod_owns_one_gpu_without_host_wide_mps_or_mig():
    args = SimpleNamespace(name="fs2-lynx-perf-cpu-mps-test", cpus=8, image="repo@sha256:" + "a" * 64,
                           node="explicit-existing-node")
    pod = load("launch").pod_spec(args)
    spec = pod["spec"]
    assert spec["containers"][0]["resources"]["requests"]["nvidia.com/gpu"] == "1"
    assert spec["containers"][0]["resources"]["requests"]["cpu"] == "8"
    assert spec["securityContext"]["runAsUser"] == 10001
    assert not spec.get("hostIPC") and not spec.get("hostPID") and not spec.get("hostNetwork")
    assert all("hostPath" not in volume for volume in spec["volumes"])
    assert spec["activeDeadlineSeconds"] == 5400
    assert pod["metadata"]["labels"]["scientific-ai.nebius.com/tenant"] == "system"


def test_mps_queries_record_only_real_numeric_client_pids(monkeypatch):
    module = load("benchmark_mps")
    replies = {"get_server_list": "123\n", "get_client_list 123": "234\n235\n"}
    monkeypatch.setattr(module, "mps_control", lambda command, env: replies[command])
    assert module.mps_clients({}) == {"123": [234, 235]}


def test_export_retry_retains_failure_and_uses_new_destination(tmp_path, monkeypatch):
    module = load("launch")
    calls = []

    def copy(command):
        calls.append(command)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, command, output=b"observer connection lost")

    monkeypatch.setattr(module.native, "call", copy)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    record = {}
    result = module.copy_results(SimpleNamespace(name="owned-pod", output=tmp_path), record)
    assert result == tmp_path / "results-copy-2"
    assert len(calls) == 2 and calls[0][-1] != calls[1][-1]
    attempts = json.loads((tmp_path / "copy-attempts.json").read_text())
    assert attempts[0]["detail"] == "observer connection lost"
    assert attempts[1]["passed"]
    assert record["results_directory"] == "results-copy-2"


def test_export_retries_are_bounded_and_preserve_all_failures(tmp_path, monkeypatch):
    module = load("launch")
    calls = []

    def copy(command):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command, output=b"copy failed")

    monkeypatch.setattr(module.native, "call", copy)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    with pytest.raises(subprocess.CalledProcessError):
        module.copy_results(SimpleNamespace(name="owned-pod", output=tmp_path), {})
    attempts = json.loads((tmp_path / "copy-attempts.json").read_text())
    assert len(calls) == len(attempts) == 3
    assert len({command[-1] for command in calls}) == 3
    assert not any(attempt["passed"] for attempt in attempts)
