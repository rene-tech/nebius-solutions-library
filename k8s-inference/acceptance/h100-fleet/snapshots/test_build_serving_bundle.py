import importlib.util
from pathlib import Path

import pytest


SOURCE = Path(__file__).with_name("build_serving_bundle.py")
spec = importlib.util.spec_from_file_location("build_serving_bundle", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_interpreter_projection_uses_measured_donor_and_restore_and_omits_defaults():
    runtime = {"name": "model", "image": "exact@sha256:" + "a" * 64,
               "command": ["python3", "/snapshot-source/serving_supervisor.py"],
               "env": [{"name": "PATH", "value": module.DEFAULT_SUPERVISOR_PATH}]}
    assert module.captured_interpreters(runtime) == {}
    interpreter = "/opt/openfold3/.pixi/envs/openfold3-cuda12/bin/python3"
    runtime["env"][0]["value"] = str(Path(interpreter).parent) + ":" + module.DEFAULT_SUPERVISOR_PATH
    restored = {"spec": {"containers": [dict(runtime)], "initContainers": [
        {"name": "snapshot-local-address", "command": [interpreter, "restore-address"]}]}}
    assert module.captured_interpreters(runtime, restored) == {
        "supervisor_path": runtime["env"][0]["value"], "address_python": interpreter,
    }
    restored["spec"]["containers"][0]["image"] = "different-image"
    with pytest.raises(ValueError, match="captured image"):
        module.captured_interpreters(runtime, restored)


def test_unwraps_standard_cpu_loop_launcher():
    native, prefix = module.unwrap_captured_command(
        ["supervisor", "--", "python3", "/snapshot-source/serving_launcher.py", "vllm", "serve"]
    )
    assert native == ["vllm", "serve"]
    assert prefix == ["python3", "/snapshot-source/serving_launcher.py"]


def test_unwraps_exact_non_root_working_directory_launcher():
    launcher = [
        "python3",
        "/snapshot-source/working_directory_launcher.py",
        "--directory",
        "/opt/fs2",
        "--uid",
        "1000",
        "--gid",
        "1000",
        "--",
    ]
    native, prefix = module.unwrap_captured_command(
        ["supervisor", "--", *launcher, "python", "-m", "uvicorn", "server:app"]
    )
    assert native == ["python", "-m", "uvicorn", "server:app"]
    assert prefix == launcher


@pytest.mark.parametrize(
    "launcher",
    [
        ["python3", "/snapshot-source/unqualified.py"],
        [
            "python3",
            "/snapshot-source/working_directory_launcher.py",
            "--directory",
            "relative",
            "--uid",
            "1000",
            "--gid",
            "1000",
            "--",
        ],
        [
            "python3",
            "/snapshot-source/working_directory_launcher.py",
            "--directory",
            "/opt/fs2",
            "--uid",
            "0",
            "--gid",
            "1000",
            "--",
        ],
    ],
)
def test_rejects_unqualified_or_identity_changing_launcher(launcher):
    with pytest.raises(ValueError):
        module.unwrap_captured_command(["supervisor", "--", *launcher, "server"])
