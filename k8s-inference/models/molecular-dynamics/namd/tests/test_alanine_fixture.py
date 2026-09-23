import hashlib
import json
import struct
import tarfile

import pytest

from make_alanine_fixture import audit_master, common, make, parm7, thermostat
from validate_alanine import ENERGY_FIELDS, coordinate_identity, energy_rows, submitted_request


PROTOCOL = {
    "single_point_electrostatic_tolerance": 1e-6, "electrostatic_target_tolerance": 1e-5,
    "timestep_fs": 2.0, "cutoff_A": 10.0, "constraint_tolerance": 1e-6,
    "output_every_steps": 500, "temperature_K": 300.0, "langevin_friction_per_ps": 1.0,
    "pressure_bar": 1.0, "lj_switching": False, "lj_potential_shift": False,
    "lj_isotropic_tail_correction": True, "production_ensemble": "NPT",
    "minimization_max_iterations": 5000, "nvt_steps": 50000, "npt_steps": 50000,
    "production_steps": 500000, "nvt_seed": 20260923, "npt_seed": 20260924,
    "production_seed": 20260925,
}


def master(tmp_path, *, active_scee=1.2, excluded=True):
    result = tmp_path / "master"
    result.mkdir()
    sections = {
        "POINTERS": [4], "ATOM_NAME": ["A", "B", "C", "D"], "MASS": [12., 12., 12., 12.],
        "CHARGE": [0.] * 4, "ATOM_TYPE_INDEX": [1] * 4,
        "NUMBER_EXCLUDED_ATOMS": [1, 1, 1, 1], "EXCLUDED_ATOMS_LIST": [4 if excluded else 0, 0, 0, 0],
        "LENNARD_JONES_ACOEF": [1.], "LENNARD_JONES_BCOEF": [1.], "NONBONDED_PARM_INDEX": [1],
        "HBOND_ACOEF": [], "HBOND_BCOEF": [], "BONDS_INC_HYDROGEN": [], "BONDS_WITHOUT_HYDROGEN": [],
        "ANGLES_INC_HYDROGEN": [], "ANGLES_WITHOUT_HYDROGEN": [],
        "DIHEDRALS_INC_HYDROGEN": [0, 3, 6, 9, 1, 0, 3, -6, 9, 2, 0, 3, 6, -9, 2],
        "DIHEDRALS_WITHOUT_HYDROGEN": [], "SCEE_SCALE_FACTOR": [active_scee, 0.], "SCNB_SCALE_FACTOR": [2., 0.],
        "BOND_FORCE_CONSTANT": [], "BOND_EQUIL_VALUE": [], "ANGLE_FORCE_CONSTANT": [], "ANGLE_EQUIL_VALUE": [],
        "DIHEDRAL_FORCE_CONSTANT": [1., 1.], "DIHEDRAL_PERIODICITY": [1., 1.], "DIHEDRAL_PHASE": [0., 0.],
    }
    text = "%VERSION TEST\n"
    for name, values in sections.items():
        kind = "a" if values and isinstance(values[0], str) else "I" if values and isinstance(values[0], int) else "E"
        text += f"%FLAG {name}\n%FORMAT(5{kind}16" + (".8" if kind == "E" else "") + ")\n"
        for start in range(0, len(values), 5):
            text += "".join(f"{v:16.8E}" if kind == "E" else f"{v:>16}" for v in values[start:start + 5]) + "\n"
    (result / "system.prmtop").write_text(text)
    (result / "system.rst7").write_text("synthetic test coordinates\n")
    (result / "system.pdb").write_text("synthetic test PDB\n")
    (result / "protocol.json").write_text(json.dumps(PROTOCOL))
    records = [{"path": p.name, "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in result.iterdir()]
    (result / "master-manifest.json").write_text(json.dumps({"atoms": 4, "box_A_degrees": [44., 44., 44., 90., 90., 90.], "files": records}))
    return result


def test_active_scaling_ignores_intentional_zero_improper_and_suppressed_terms(tmp_path):
    report = audit_master(master(tmp_path))
    assert report["active_1_4_pairs"] == 1
    assert report["torsions_not_generating_1_4"] == 2
    assert report["active_1_4_scaling"]["native_oneFourScaling"] == pytest.approx(1 / 1.2)
    assert report["native_topology_conversion"] is False


@pytest.mark.parametrize("kwargs,match", [({"active_scee": 0.}, "valid exclusion/scaling"),
                                         ({"active_scee": 1.1}, "uniform native"),
                                         ({"excluded": False}, "valid exclusion/scaling")])
def test_invalid_active_scaling_or_exclusions_cannot_claim_native_mapping(tmp_path, kwargs, match):
    with pytest.raises(ValueError, match=match):
        audit_master(master(tmp_path, **kwargs))


def test_master_hash_change_is_detected_before_parameter_interpretation(tmp_path):
    path = master(tmp_path)
    with (path / "system.prmtop").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="identity changed"):
        audit_master(path)


def test_native_configuration_maps_amber_tail_bar_and_all_atom_thermostat():
    config = common(PROTOCOL)
    for line in ("readexclusions on", "exclude scaled1-4", "scnb 2.0", "LJcorrection on", "switching off", "fullElectFrequency 1", "rigidBonds all"):
        assert line + "\n" in config
    assert "LJcorrection off\n" in common(PROTOCOL, singlepoint=True, tail=False)
    assert "rigidBonds none\n" in common(PROTOCOL, singlepoint=True)
    assert "nonbondedFreq 1\n" in config and "nonbondedFrequency" not in config
    assert "PMEGridSizeX 64\nPMEGridSizeY 64\nPMEGridSizeZ 64\n" in common(PROTOCOL, singlepoint=True)
    assert "langevinPistonTarget 1.0\n" in thermostat(PROTOCOL, pressure=True)
    assert "langevinHydrogen on\n" in thermostat(PROTOCOL)


def test_fixture_preserves_master_and_exact_stage_steps_without_rng_restarts(tmp_path):
    source = master(tmp_path)
    output = tmp_path / "fixture"
    make(source, output)
    request = json.loads((output / "request.json").read_text())
    stages = request["jobs"][0]["steps"]
    assert [s["id"] for s in stages] == ["minimize", "nvt", "npt", "production"]
    assert [(s["first_step"], s["steps"], s["segment_steps"]) for s in stages[2:]] == [(55000, 50000, 50000), (105000, 500000, 500000)]
    nvt = (output / "inputs/alanine/nvt.namd").read_text()
    assert "temperature 300.0\n" in nvt and "binVelocities" not in nvt and "seed 20260923\n" in nvt
    assert "seed 20260925\n" in (output / "inputs/alanine/production.namd").read_text()
    point = json.loads((output / "singlepoint/request.json").read_text())
    assert [j["id"] for j in point["jobs"]] == ["singlepoint-tail", "singlepoint-no-tail"]
    with tarfile.open(output / "input.tar.gz") as bundle:
        assert bundle.extractfile("alanine/system.prmtop").read() == (source / "system.prmtop").read_bytes()
    assert parm7(source / "system.prmtop")["SCEE_SCALE_FACTOR"] == [1.2, 0.]


def test_singlepoint_compares_every_coordinate_and_rejects_geometry_projection(tmp_path):
    source, native = tmp_path / "system.rst7", tmp_path / "point.coor"
    coords = [1., -2., 3., 4., 5., -6.]
    source.write_text("coordinates\n 2\n" + "".join(f"{v:12.7f}" for v in coords) + "\n")
    native.write_bytes(struct.pack("<i6d", 2, *coords))
    assert coordinate_identity(source, native)["maximum_coordinate_difference_A"] == 0
    native.write_bytes(struct.pack("<i6d", 2, *coords[:-1], -6.01))
    with pytest.raises(ValueError, match="moved"):
        coordinate_identity(source, native)


def test_canonical_observable_reader_uses_native_labels_and_checks_all_values(tmp_path):
    path = tmp_path / "native.log"
    path.write_text("ETITLE: " + " ".join(ENERGY_FIELDS) + "\nENERGY: " + " ".join(str(i) for i in range(len(ENERGY_FIELDS))) + "\n")
    assert energy_rows(path)[0]["GPRESSURE"] == ENERGY_FIELDS.index("GPRESSURE")
    path.write_text("ENERGY: " + " ".join(["nan"] + ["0"] * (len(ENERGY_FIELDS) - 1)) + "\n")
    with pytest.raises(ValueError, match="non-finite"):
        energy_rows(path)


@pytest.mark.parametrize("change", ["transport", "steps", "seed-script"])
def test_hosted_validation_binds_transport_request_without_changing_physics(tmp_path, change):
    output = tmp_path / "fixture"
    make(master(tmp_path), output)
    request = json.loads((output / "request.json").read_text())
    request["output_destination"] = "platform-artifacts"
    if change == "steps":
        request["jobs"][0]["steps"][-1]["steps"] += 500
    elif change == "seed-script":
        request["jobs"][0]["steps"][0]["config"] = "other-seed.namd"
    actual = tmp_path / "submitted.json"
    actual.write_text(json.dumps(request))
    if change == "transport":
        assert submitted_request(output, actual) == request
    else:
        with pytest.raises(ValueError, match="more than output transport"):
            submitted_request(output, actual)
