import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("frames,marker,passed", [(10, True, True), (9, True, False), (10, False, False)])
def test_snapshot_requires_complete_native_outputs(tmp_path, monkeypatch, capsys, frames, marker, passed):
    (tmp_path / "work").mkdir()
    (tmp_path / "worker.log").write_text("End of program\n" + ("FS2_SEGMENT_COMPLETE 121000\n" if marker else ""))
    native = SimpleNamespace(
        binary_vectors=lambda path: 92224,
        xsc_step=lambda path: 121000,
        dcd=lambda path: {"atoms": 92224, "frames": frames, "first_step": 30000, "interval_steps": 10000, "last_step": 120000},
        energy_drift=lambda paths: {"max_relative_total_energy_deviation": 0.001},
    )
    monkeypatch.setitem(sys.modules, "validate_namd", native)
    monkeypatch.setattr(sys, "argv", ["validator", str(tmp_path)])
    spec = importlib.util.spec_from_file_location("namd_snapshot", Path(__file__).with_name("validate_namd_snapshot.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if passed:
        module.main()
    else:
        with pytest.raises(SystemExit, match="1"):
            module.main()
    assert json.loads(capsys.readouterr().out)["passed"] is passed
