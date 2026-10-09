from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "l40s-incremental-20261006"),
                str(HERE.parent / "lynx-l40s-final-20261005"),
                str(HERE.parents[1] / "models/molecular-dynamics/gromacs/qualification")]
import hopper_inside as worker
import run_hopper as launcher


@pytest.mark.parametrize("gpu", ["H100", "H200"])
def test_exactly_one_gpu_and_customer_exclusion(gpu):
    args = SimpleNamespace(gpu=gpu, cpus=8, node="idle-node", name="fs2-lynx-perf-cpu-hopper-test")
    spec = launcher.specification(args)
    pod = launcher.supervisor.pod_spec(args, spec)
    assert pod["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert pod["spec"]["containers"][0]["image"] == launcher.supervisor.IMAGE
    assert pod["metadata"]["labels"]["scientific-ai.nebius.com/task"] == "hopper-single-gpu-20261006"
    assert "l40s-1x" not in spec.allowed_pools
    args.node = launcher.supervisor.CUSTOMER_NODE
    with pytest.raises(ValueError):
        launcher.supervisor.pod_spec(args, spec)


def test_cases_preserve_single_rank_and_only_execution_tuning():
    cases = worker.cases_for("tune", 8, {"selected_cpus": list(range(8))}, True)
    assert len(cases) == 9
    assert all(case["threads"] <= 8 for case in cases)
    assert all(not (set(case) & {"ntmpi", "dt", "cutoff", "nsteps"}) for case in cases)
    assert worker.cases_for("cpu-envelope", 32, {}, False) == [
        {"name": "baseline", "threads": 8}, {"name": "threads-16", "threads": 16},
        {"name": "threads-32", "threads": 32}]


def test_combination_requires_two_independent_screen_gains():
    cases = [{"name": "baseline", "threads": 8}, {"name": "threads-6", "threads": 6},
             {"name": "original-pme", "threads": 8, "extra_args": ["-notunepme"]}]
    summary = {name: {"process_inclusive_median_ns_day": rate}
               for name, rate in (("baseline", 200), ("threads-6", 203), ("original-pme", 210))}
    assert worker.combined_candidate(cases, summary)[0] is None
    summary["threads-6"]["process_inclusive_median_ns_day"] = 208
    combined, used = worker.combined_candidate(cases, summary)
    assert combined == {"name": "combined", "threads": 6, "extra_args": ["-notunepme"]}
    assert set(used) == {"threads-6", "original-pme"}


def test_cpu_pme_screen_is_resource_bounded():
    with pytest.raises(ValueError, match="16 allocated"):
        worker.cases_for("cpu-pme", 8, {}, False)
    assert worker.cases_for("cpu-pme", 16, {}, False) == [
        {"name": "baseline", "threads": 8}, {"name": "threads-16", "threads": 16},
        {"name": "original-pme", "threads": 8, "extra_args": ["-notunepme"]}]
