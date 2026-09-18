"""The single-query serving lane must not inherit training IPC defaults."""
import importlib.util
from pathlib import Path
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("openfold3_inline_adapter", ROOT / "run_openfold3.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


@pytest.mark.parametrize("existing", [None, {"num_workers": 10, "prefetch_factor": 2,
                                          "persistent_workers": True, "data_seed": 23}])
def test_single_query_inline_loader_preserves_seed_and_other_settings(tmp_path, existing):
    document = {"experiment_settings": {"seeds": [42]}, "pl_trainer_args": {"precision": "bf16-mixed"}}
    if existing is not None:
        document["data_module_args"] = existing
    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump(document))
    original = base.read_bytes()
    output = tmp_path / "generated.yaml"
    adapter._write_seeded_runner(base, output, [7, 42])
    generated = yaml.safe_load(output.read_text())
    assert generated["data_module_args"]["num_workers"] == 0
    assert generated["data_module_args"]["prefetch_factor"] is None
    assert generated["data_module_args"]["persistent_workers"] is False
    if existing is not None:
        assert generated["data_module_args"]["data_seed"] == 23
    assert generated["experiment_settings"]["seeds"] == [7, 42]
    assert generated["experiment_settings"]["use_templates"] is False
    assert generated["experiment_settings"]["use_msa_server"] is False
    assert generated["pl_trainer_args"] == document["pl_trainer_args"]
    assert base.read_bytes() == original


def test_invalid_data_module_mapping_rejected(tmp_path):
    base = tmp_path / "base.yaml"
    base.write_text("data_module_args: []\n")
    with pytest.raises(SystemExit, match="data_module_args must be a mapping"):
        adapter._write_seeded_runner(base, tmp_path / "generated.yaml", [1])
