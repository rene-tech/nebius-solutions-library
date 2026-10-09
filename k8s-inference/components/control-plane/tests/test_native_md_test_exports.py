import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[3] / "models/molecular-dynamics/gromacs/qualification/archive_failed_exports.py"
SPEC = importlib.util.spec_from_file_location("task_export_archive", PATH)
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


def status(operation_id, model="namd", state="succeeded"):
    return {"operation": {"id": operation_id, "model_id": model, "status": state,
                          "tenant_id": "rene", "principal_id": "rene"},
            "batch": {"stages": [{"attempts": [{"resource_released": True}]}]}}


def test_only_exact_terminal_task_prefix_can_be_archived():
    op = "3a49aa8e-a02a-429a-936e-813b0425a9ba"
    value = status(op)
    assert archive.target(value, op) == f"qualification/namd/{op}/"
    failed = "f963df4d-1141-49c6-906e-1b05a2953b7a"
    assert archive.target(status(failed, "lammps", "failed"), failed) == f"runs/lammps/sustained-six-case/{failed}/"
    with pytest.raises(ValueError):
        archive.target(status("another-customer-run"), "another-customer-run")
    with pytest.raises(ValueError):
        archive.target(status(op, state="running"), op)
    with pytest.raises(ValueError):
        archive.target(status(op, model="amber"), op)
    value["batch"]["stages"][0]["attempts"][0]["resource_released"] = False
    with pytest.raises(ValueError):
        archive.target(value, op)
