import argparse
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "lynx-l40s-final-20261005"),
                str(HERE.parents[1] / "models/molecular-dynamics/gromacs/qualification")]
import experiment_inside as inside
import run_experiment as supervisor


def args(node="idle-test-node"):
    return argparse.Namespace(node=node, name="fs2-lynx-perf-cpu-incremental-test")


def test_customer_node_is_excluded():
    with pytest.raises(ValueError):
        supervisor.pod_spec(args(supervisor.CUSTOMER_NODE))


def test_exact_resources_and_no_customer_storage_or_privilege():
    pod = supervisor.pod_spec(args())
    spec = pod["spec"]
    runtime = spec["containers"][0]
    assert runtime["image"] == supervisor.IMAGE
    assert runtime["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert runtime["resources"]["requests"]["cpu"] == "8"
    assert spec["automountServiceAccountToken"] is False
    assert runtime["securityContext"]["allowPrivilegeEscalation"] is False
    assert all("emptyDir" in volume for volume in spec["volumes"])
    assert not spec.get("hostPID") and not spec.get("hostNetwork")
    assert "nodeName" not in spec  # ordinary scheduler still enforces resource allocation


def test_only_run_length_changes():
    original = {"nsteps": "100000000", "dt": "0.002", "verlet-buffer-tolerance": "0.005"}
    finite = {**original, "nsteps": "50000"}
    assert set(inside.topology_tools.parameter_difference(original, finite)) == {"nsteps"}
    with pytest.raises(ValueError):
        inside.topology_tools.parameter_difference(original, {**finite, "dt": "0.004"})


def test_environment_does_not_leak_graph_or_affinity_flags(monkeypatch):
    monkeypatch.setenv("GMX_CUDA_GRAPH", "1")
    monkeypatch.setenv("OMP_PLACES", "cores")
    env, prefix = inside.environment(Path("/tmp/experiment"), 7)
    assert "GMX_CUDA_GRAPH" not in env and "OMP_PLACES" not in env
    assert env["OMP_NUM_THREADS"] == "7" and prefix == []


def test_affinity_applies_only_to_process():
    env, prefix = inside.environment(Path("/tmp/experiment"), 7, [0, 2, 4, 6, 8, 10, 12, 14])
    assert prefix == ["taskset", "--cpu-list", "0,2,4,6,8,10,12,14"]
    assert env["OMP_PROC_BIND"] == "close"


def test_aggregate_reports_median_and_all_samples():
    rows = [{"case": "test", "process_inclusive_ns_per_day": n, "native_ns_per_day": n + 2,
             "validation": {"passed": True}} for n in (100, 150, 101)]
    summary = inside.aggregate(rows)["test"]
    assert summary["process_inclusive_median_ns_day"] == 101
    assert summary["process_inclusive_range_ns_day"] == [100, 150]
    assert summary["repetitions"] == 3
