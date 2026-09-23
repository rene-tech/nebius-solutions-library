import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "delivery_manifest", Path(__file__).with_name("delivery_manifest.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def case(tmp_path):
    for name in module.REQUIRED:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unit-fixture, not scientific data\n")
    (tmp_path / "analysis/receipt.json").write_text(
        json.dumps({"status": "analysis-passed"})
    )
    (tmp_path / "videos/receipt.json").write_text(
        json.dumps({"status": "real-trajectories-rendered-and-encoding-validated"})
    )
    return tmp_path


def test_create_verify_and_no_overwrite(case):
    assert module.run(case, True)["status"] == "inventory-created"
    assert module.run(case)["status"] == "inventory-verified"
    with pytest.raises(FileExistsError):
        module.run(case, True)


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra"])
def test_inventory_rejects_changed_tree(case, mutation):
    module.run(case, True)
    if mutation == "missing":
        (case / "videos/amber.mp4").unlink()
    elif mutation == "changed":
        (case / "videos/amber.mp4").write_text("different fixture\n")
    else:
        (case / "unexpected.txt").write_text("extra fixture\n")
    with pytest.raises(ValueError):
        module.run(case)


def test_no_incomplete_science_receipts(case):
    (case / "analysis/receipt.json").write_text(
        json.dumps({"status": "partial-analysis-passed"})
    )
    with pytest.raises(ValueError, match="not complete"):
        module.run(case, True)


def test_no_environment_dependent_symlink(case):
    (case / "shortcut").symlink_to(case / "README.md")
    with pytest.raises(ValueError, match="actual files"):
        module.run(case, True)
