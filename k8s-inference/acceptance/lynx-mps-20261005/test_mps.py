import importlib.util
from pathlib import Path
import sys
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
