import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "models/molecular-dynamics/gromacs/qualification"))
spec = importlib.util.spec_from_file_location("final_screen_inside", Path(__file__).with_name("screen_inside.py"))
screen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screen)
spec = importlib.util.spec_from_file_location("final_run_screen", Path(__file__).with_name("run_screen.py"))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_affinity_never_escapes_allowed_mask_or_duplicates_physical_cores():
    topology = {i: [i // 8, i % 4] for i in range(16)}
    result = screen.select_physical_cpus(range(16), topology, set(range(8, 16)))
    assert result == [8, 9, 10, 11, 0, 1, 2, 3]
    assert len({tuple(topology[c]) for c in result}) == 8
    with pytest.raises(ValueError, match="eight distinct"):
        screen.select_physical_cpus([0, 1, 4, 5], topology, {0, 1})


def test_cpu_range_parser():
    assert screen.cpu_list("0-3,8,10-11") == {0, 1, 2, 3, 8, 10, 11}


def test_only_finite_steps_may_change_not_tolerance_or_outputs():
    original = {"nsteps": "-1", "verlet-buffer-tolerance": "0.005", "nstxout-compressed": "500000"}
    finite = {**original, "nsteps": "50000"}
    assert screen.parameter_difference(original, finite) == {"nsteps": ["-1", "50000"]}
    for field, value in (("verlet-buffer-tolerance", "0.01"), ("nstxout-compressed", "0")):
        with pytest.raises(ValueError, match="science"):
            screen.parameter_difference(original, {**finite, field: value})


def test_one_gpu_same_limits_and_nonprivileged_process_only():
    args = SimpleNamespace(name="fs2-lynx-perf-cpu-final-r1", node="node", image=launcher.IMAGE, cpus=8)
    pod = launcher.pod_spec(args)
    container = pod["spec"]["containers"][0]
    assert container["resources"]["requests"] == container["resources"]["limits"]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert container["resources"]["limits"]["cpu"] == "8"
    assert container["securityContext"] == {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["securityContext"]["runAsUser"] == 10001
    assert all("hostPath" not in volume for volume in pod["spec"]["volumes"])
    args.cpus = 16
    with pytest.raises(ValueError, match="eight-CPU"):
        launcher.pod_spec(args)


def test_no_alternate_image_or_unowned_name():
    args = SimpleNamespace(name="unrelated", node="node", image=launcher.IMAGE, cpus=8)
    with pytest.raises(ValueError):
        launcher.pod_spec(args)
    args.name = "fs2-lynx-perf-cpu-final-r1"
    args.image = "gromacs:latest"
    with pytest.raises(ValueError):
        launcher.pod_spec(args)


def test_public_pin_control_has_no_external_affinity(tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_PLACES", "{999}")
    monkeypatch.setenv("OMP_PROC_BIND", "close")
    monkeypatch.setenv("GMX_CUDA_GRAPH", "1")
    tpr = tmp_path / "test.tpr"
    tpr.write_bytes(b"test")
    captured = {}

    class Completed:
        returncode = 0

        def poll(self):
            return 0

    def start(command, **kwargs):
        captured.update(command=command, environment=kwargs["env"])
        output = Path(command[command.index("--output") + 1])
        output.mkdir()
        (output / "measurements.json").write_text(json.dumps([
            {"cohort": f"warm-{i}", "native_ns_per_day": 200, "validation": {"passed": True}}
            for i in (1, 2, 3)
        ]))
        return Completed()

    monkeypatch.setattr(screen.subprocess, "Popen", start)
    result = screen.run_case(tmp_path, tpr, "public-pin-on", 200, pin="on")
    command = captured["command"]
    assert command[command.index("--pin") + 1] == "on"
    assert "taskset" not in command
    assert not {"OMP_PLACES", "OMP_PROC_BIND", "GMX_CUDA_GRAPH"} & captured["environment"].keys()
    assert result["validated_warm_runs"] == 3
    assert result["pin"] == "on"
