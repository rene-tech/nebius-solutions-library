import copy

import pytest
from jsonschema import ValidationError

from fs2_amber import PARAMETER_SCHEMA
from fs2_amber.advanced import validate_advanced
from fs2_amber.contracts import normalize
from fs2_amber.worker import native_argv


def normalized(step):
    return normalize({"schema": PARAMETER_SCHEMA, "jobs": [{"id": "tools", "steps": [step]}]})["jobs"][0]["steps"][0]


def ligand():
    return {"id": "charge", "kind": "antechamber", "input": "ligand.pdb", "input_format": "pdb", "output": "charged.mol2"}


def binding():
    return {"id": "binding", "kind": "mmpbsa", "input": "gb.in", "complex_topology": "complex.prmtop", "receptor_topology": "receptor.prmtop", "ligand_topology": "ligand.prmtop", "trajectories": ["part1.nc", "part2.nc"], "expected_frames": 2}


def test_antechamber_has_fixed_bcc_flags_and_explicit_scientific_defaults():
    step = normalized(ligand())
    command = native_argv(step, "/native/antechamber")
    assert command[command.index("-c") + 1] == "bcc"
    assert command[command.index("-at") + 1] == "gaff2"
    assert command[command.index("-eq") + 1] == "1"
    assert step["charge_tolerance_e"] == 0.001
    assert step["expected_outputs"] == ["charged.mol2"]
    assert normalized(step) == step


def test_charge_runs_preserve_separate_native_sqm_context():
    first, second = ligand(), {**ligand(), "id": "second", "output": "second.mol2"}
    body = {"schema": PARAMETER_SCHEMA, "jobs": [{"id": "tools", "steps": [first, second]}]}
    with pytest.raises(ValueError, match="distinct directory"):
        normalize(body)
    second["directory"] = "ligand2"
    normalize(body)


def test_parmchk2_and_mmpbsa_flags_are_typed():
    parm = normalized({"id": "parameters", "kind": "parmchk2", "input": "charged.mol2", "input_format": "mol2", "output": "ligand.frcmod"})
    assert native_argv(parm, "parmchk2") == ["parmchk2", "-i", "charged.mol2", "-f", "mol2", "-o", "ligand.frcmod", "-s", "gaff2"]
    step = normalized({**binding(), "use_mdins": True})
    command = native_argv(step, "MMPBSA.py")
    assert command[-3:] == ["-y", "part1.nc", "part2.nc"]
    assert "-use-mdins" in command
    assert command[command.index("-prefix") + 1] == "binding.intermediate_"
    assert step["expected_outputs"] == ["binding.csv", "binding.dat"]


@pytest.mark.parametrize("extra", [{"argv": ["sh", "-c", "true"]}, {"backend": "cpu"}, {"trajectories": ["../other.nc"]}])
def test_advanced_tools_reject_untyped_or_inapplicable_fields(extra):
    with pytest.raises((ValueError, ValidationError)):
        normalized({**binding(), **extra})


def test_binding_components_and_output_frame_assertions_are_not_optional():
    value = binding()
    del value["ligand_topology"]
    with pytest.raises(ValueError, match="both receptor"):
        normalized(value)
    value = binding()
    del value["expected_frames"]
    with pytest.raises(ValueError, match="expected_frames"):
        normalized(value)


def test_charge_residual_is_checked_never_silently_corrected(tmp_path):
    raw = "@<TRIPOS>MOLECULE\nLIG\n2 1 1 0 0\nSMALL\nbcc\n@<TRIPOS>ATOM\n1 C 0 0 0 c3 1 LIG 0.1000\n2 H 1 0 0 hc 1 LIG -0.1020\n@<TRIPOS>BOND\n1 1 2 1\n"
    (tmp_path / "charged.mol2").write_text(raw)
    (tmp_path / "sqm.out").write_text("Calculation Completed\n")
    step = normalized(ligand())
    with pytest.raises(ValueError, match="charges were not altered"):
        validate_advanced(tmp_path, step)
    assert (tmp_path / "charged.mol2").read_text() == raw
    step["charge_tolerance_e"] = 0.003
    result = validate_advanced(tmp_path, step)
    assert result["charge_residual_e"] == pytest.approx(-0.002)
    assert result["scientific_parameter_suitability_claimed"] is False


def test_frcmod_requires_manual_revision_for_unresolved_parameters(tmp_path):
    path = tmp_path / "ligand.frcmod"
    path.write_text("MASS\nBOND\nANGLE\nDIHE\nIMPROPER\nNONBON\nATTN: needs revision\n")
    step = normalized({"id": "parameters", "kind": "parmchk2", "input": "charged.mol2", "input_format": "mol2", "output": "ligand.frcmod"})
    with pytest.raises(ValueError, match="manual revision"):
        validate_advanced(tmp_path, step)


def test_mmpbsa_checks_every_requested_finite_frame(tmp_path):
    step = normalized(binding())
    (tmp_path / "binding.dat").write_text("GENERALIZED BORN:\n")
    path = tmp_path / "binding.csv"
    path.write_text("Complex:\nFrame #,VDWAALS,EEL,TOTAL\n1,-1,-2,-3\n2,-2,-3,-5\n")
    assert validate_advanced(tmp_path, step)["frames_per_table"] == 2
    path.write_text(path.read_text().replace("2,-2,-3,-5", "2,-2,NaN,-5"))
    with pytest.raises(ValueError, match="nonfinite"):
        validate_advanced(tmp_path, step)
    path.write_text("Complex:\nFrame #,TOTAL\n1,-3\n")
    with pytest.raises(ValueError, match="expected_frames"):
        validate_advanced(tmp_path, step)
