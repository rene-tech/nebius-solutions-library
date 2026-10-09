"""The common comparison must not benchmark alternative cutoffs mid-trajectory."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from fs2_gromacs.contracts import normalize


DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DIRECTORY))
SPEC = importlib.util.spec_from_file_location("fixed_pme_generator", DIRECTORY / "make_gromacs_workflow.py")
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


def test_all_four_mdrun_stages_explicitly_disable_pme_tuning(tmp_path):
    master, converted = tmp_path / "master", tmp_path / "converted"
    master.mkdir()
    (converted / "gromacs").mkdir(parents=True)
    protocol = {f"{stage}_seed": seed for stage, seed in zip(("nvt", "npt", "production"), (23, 24, 25))}
    protocol.update(nvt_steps=50000, npt_steps=50000, production_steps=500000)
    (master / "protocol.json").write_text(json.dumps(protocol))
    (master / "master-manifest.json").write_text("{}")
    for name in ("system.top", "system.gro"):
        (converted / "gromacs" / name).write_text("unchanged converted input\n")
    conversion = converted / "conversion.json"
    conversion.write_text("{}")
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"status": "passed", "input_hashes": {str(conversion): hashlib.sha256(conversion.read_bytes()).hexdigest()}}))
    output = tmp_path / "frozen"
    GENERATOR.make(master, converted, audit, output)
    request = json.loads((output / "request.json").read_text())
    normalize(request)  # Actual worker contract accepts the native flag.
    native = [step for step in request["jobs"][0]["steps"] if step["command"] == "mdrun"]
    assert [step["id"] for step in native] == ["minimize", "nvt", "npt", "production"]
    for step in native:
        assert step["args"] == ["-s", step["id"] + ".tpr", "-deffnm", step["id"], "-notunepme"]
        mdp = (output / "data" / (step["id"] + ".mdp")).read_text()
        for setting in ("fourier-nx = 64", "fourier-ny = 64", "fourier-nz = 64", "pme-order = 4", "rcoulomb = 1.0", "rvdw = 1.0"):
            assert setting in mdp
    assert (output / "data/system.top").read_bytes() == (converted / "gromacs/system.top").read_bytes()
    assert (output / "data/system.gro").read_bytes() == (converted / "gromacs/system.gro").read_bytes()
